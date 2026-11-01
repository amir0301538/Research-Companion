"""
Playwright-based extraction from a Google Scholar profile FRONT PAGE.

Scope rules enforced here:
  * Only the given profile URL is opened (one page.goto call).
  * "SHOW MORE" is clicked to load the rest of the publication table. This is an
    in-page AJAX request; the browser never navigates away from the profile.
  * Publication links are READ from the DOM, never opened.
  * CAPTCHA / "unusual traffic" pages are detected and reported, never bypassed.
"""

import logging
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

log = logging.getLogger(__name__)

ROW_SELECTOR = "#gsc_a_b tr.gsc_a_tr"
SHOW_MORE = "#gsc_bpf_more"


class ScraperError(Exception):
    """Base error for scraping failures."""


class AccessBlockedError(ScraperError):
    """Google Scholar returned a CAPTCHA, rate-limit or access-denied page."""


class InvalidProfileUrlError(ScraperError):
    """The supplied URL is not a Google Scholar profile URL."""


@dataclass
class ProfileData:
    requested_url: str
    google_scholar_id: Optional[str] = None
    loaded_url: Optional[str] = None
    http_status: Optional[int] = None
    header: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    publications: list = field(default_factory=list)
    footer_text: Optional[str] = None
    listing_complete: bool = False
    show_more_clicks: int = 0
    navigations: list = field(default_factory=list)
    browser_used: Optional[str] = None
    warnings: list = field(default_factory=list)
    network_stats: Optional[dict] = None


SENSITIVE_OR_ID_PARAMS = {
    "q", "continue", "user", "citation_for_view", "id", "token", "auth",
    "signature", "sig", "key", "session", "code", "state", "secret"
}

SAFE_QUERY_PARAMS = {
    "hl", "view_op", "cstart", "pagesize", "sortby"
}


def redact_url(url: Optional[str]) -> str:
    """
    Redact query parameters that may contain identifiers, tracking tokens,
    or sensitive values while preserving structure (scheme, netloc, path)
    for diagnostic visibility.
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        if not parts.query:
            return url
        query_pairs = parse_qsl(parts.query, keep_blank_values=True)
        redacted_pairs = []
        for k, v in query_pairs:
            lower_k = k.lower()
            if lower_k in SENSITIVE_OR_ID_PARAMS or lower_k not in SAFE_QUERY_PARAMS:
                redacted_pairs.append(f"{k}=[REDACTED]")
            else:
                redacted_pairs.append(f"{k}={v}")
        new_query = "&".join(redacted_pairs)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))
    except Exception:
        return url.split("?")[0] + "?[REDACTED]"


@dataclass
class DiagnosticNetworkStats:
    total_requests: int = 0
    navigation_count: int = 0
    requests_current_page: int = 0
    last_navigation_time: Optional[float] = None
    time_between_navigations: list = field(default_factory=list)
    requests_by_type: dict = field(default_factory=dict)
    requests_by_domain: dict = field(default_factory=dict)
    navigation_events: list = field(default_factory=list)
    requests_log: list = field(default_factory=list)
    has_429: bool = False
    last_429_url: Optional[str] = None
    sorry_redirect: bool = False
    last_main_frame_status: Optional[int] = None
    last_main_frame_url: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "total_requests": self.total_requests,
            "navigation_count": self.navigation_count,
            "requests_per_page_load": self.requests_current_page,
            "time_between_navigations": list(self.time_between_navigations),
            "requests_by_type": dict(self.requests_by_type),
            "requests_by_domain": dict(self.requests_by_domain),
            "navigation_events": list(self.navigation_events),
            "has_429": self.has_429,
            "last_429_url": redact_url(self.last_429_url) if self.last_429_url else None,
            "sorry_redirect": self.sorry_redirect,
            "last_main_frame_status": self.last_main_frame_status,
            "last_main_frame_url": redact_url(self.last_main_frame_url) if self.last_main_frame_url else None,
            "requests_log": list(self.requests_log),
        }


def setup_diagnostic_listeners(page, stats: DiagnosticNetworkStats, logger: logging.Logger):
    """
    Attaches diagnostic listeners to the Playwright page to record:
      - Request timestamp
      - Request URL and domain (with sensitive/identifier parameters redacted)
      - Request type (document, script, image, XHR/fetch, etc.)
      - Navigation count
      - Requests per page load
      - Time between navigations
      - HTTP response status, especially 429
    Distinguishes actual top-level page navigations from subresource requests.
    """
    req_map = {}

    def on_request(request):
        now_dt = datetime.now()
        ts_str = now_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        now_mono = time.monotonic()
        url = request.url
        clean_url = redact_url(url)
        domain = urlparse(url).netloc
        res_type = request.resource_type
        is_nav = request.is_navigation_request()

        stats.total_requests += 1
        stats.requests_by_type[res_type] = stats.requests_by_type.get(res_type, 0) + 1
        stats.requests_by_domain[domain] = stats.requests_by_domain.get(domain, 0) + 1

        time_between_navs = None
        if is_nav:
            stats.navigation_count += 1
            if stats.last_navigation_time is not None:
                time_between_navs = round(now_mono - stats.last_navigation_time, 3)
                stats.time_between_navigations.append(time_between_navs)
            stats.last_navigation_time = now_mono
            stats.requests_current_page = 1
            nav_entry = {
                "navigation_index": stats.navigation_count,
                "timestamp": ts_str,
                "url": clean_url,
                "domain": domain,
                "time_since_last_nav_s": time_between_navs,
            }
            stats.navigation_events.append(nav_entry)
            logger.info(
                "[DIAGNOSTIC] [NAV #%d] Navigation request initiated at %s | Elapsed since last nav: %s | Domain: %s | URL: %s",
                stats.navigation_count,
                ts_str,
                f"{time_between_navs}s" if time_between_navs is not None else "N/A (first navigation)",
                domain,
                clean_url,
            )
        else:
            stats.requests_current_page += 1

        rec = {
            "id": stats.total_requests,
            "timestamp": ts_str,
            "url": clean_url,
            "domain": domain,
            "resource_type": res_type,
            "is_navigation": is_nav,
            "requests_per_page_load": stats.requests_current_page,
            "status": None,
        }
        stats.requests_log.append(rec)
        req_map[request] = rec

        logger.info(
            "[DIAGNOSTIC] [REQ #%d] [%s] Type: %-10s | IsNav: %-5s | PageLoadReqCount: %-2d | Domain: %s | URL: %s",
            stats.total_requests,
            ts_str,
            res_type,
            str(is_nav),
            stats.requests_current_page,
            domain,
            clean_url,
        )

    def on_response(response):
        status = response.status
        url = response.url
        clean_url = redact_url(url)
        res_type = response.request.resource_type
        is_nav = response.request.is_navigation_request()

        rec = req_map.get(response.request)
        if rec:
            rec["status"] = status

        # Track latest main-frame navigation response to prevent stale status
        try:
            if is_nav and response.frame == page.main_frame:
                stats.last_main_frame_status = status
                stats.last_main_frame_url = url
        except Exception:
            pass

        logger.info(
            "[DIAGNOSTIC] [RESP] Status: %-3s | Type: %-10s | IsNav: %-5s | URL: %s",
            status,
            res_type,
            str(is_nav),
            clean_url,
        )

        domain = urlparse(url).netloc
        is_google = "scholar.google" in domain or "google." in domain
        if status == 429 and is_google:
            stats.has_429 = True
            stats.last_429_url = url
            logger.warning("[DIAGNOSTIC] HTTP 429 DETECTED on URL: %s", clean_url)
        if "/sorry/" in url:
            stats.sorry_redirect = True
            logger.warning("[DIAGNOSTIC] Google Sorry page redirect DETECTED on URL: %s", clean_url)

    def on_request_failed(request):
        rec = req_map.get(request)
        if rec:
            rec["status"] = f"FAILED: {request.failure}"
        clean_url = redact_url(request.url)
        logger.warning(
            "[DIAGNOSTIC] [REQ FAILED] Type: %-10s | Error: %s | URL: %s",
            request.resource_type,
            request.failure,
            clean_url,
        )

    page.on("request", on_request)
    page.on("response", on_response)
    page.on("requestfailed", on_request_failed)


def parse_google_scholar_id(url: str) -> str:
    parsed = urlparse(url.strip())
    if "scholar.google." not in (parsed.netloc or "") or not parsed.path.startswith("/citations"):
        raise InvalidProfileUrlError(f"Not a Google Scholar profile URL: {url}")
    user = parse_qs(parsed.query).get("user", [None])[0]
    if not user:
        raise InvalidProfileUrlError(f"URL has no 'user' parameter: {url}")
    return user


# --------------------------------------------------------------------------- #
# In-page JavaScript extractors (run against the live DOM)
# --------------------------------------------------------------------------- #
JS_HEADER = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const q = s => document.querySelector(s);
  const lines = [...document.querySelectorAll('#gsc_prf_i .gsc_prf_il')];
  const aff = lines.find(el => el.id !== 'gsc_prf_ivh' && el.id !== 'gsc_prf_int');
  const affOrg = aff ? aff.querySelector('a.gsc_prf_ila') : null;
  const ver = q('#gsc_prf_ivh');
  const home = ver ? ver.querySelector('a') : null;
  return {
    scholar_name: clean(q('#gsc_prf_in') && q('#gsc_prf_in').textContent),
    affiliation_text: aff ? clean(aff.textContent) : null,
    affiliation_org_link: affOrg ? affOrg.href : null,
    verification_text: ver ? clean(ver.textContent) : null,
    homepage_url: home ? home.href : null,
    header_text: clean(q('#gsc_prf_i') && q('#gsc_prf_i').innerText),
    interests: [...document.querySelectorAll('#gsc_prf_int a.gsc_prf_inta')]
                 .map(a => clean(a.textContent)).filter(Boolean),
  };
}
"""

JS_METRICS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const table = document.querySelector('#gsc_rsb_st');
  if (!table) return {};
  const heads = [...table.querySelectorAll('thead th')].map(th => clean(th.textContent));
  const colNames = heads.slice(1);           // e.g. ["All", "Since 2021"]
  const out = {};
  table.querySelectorAll('tbody tr').forEach(tr => {
    const label = clean(tr.querySelector('.gsc_rsb_sc1') && tr.querySelector('.gsc_rsb_sc1').textContent);
    const vals = [...tr.querySelectorAll('td.gsc_rsb_std')].map(td => clean(td.textContent));
    if (label) {
      out[label] = {};
      vals.forEach((v, i) => out[label][colNames[i] || ('col' + i)] = v);
    }
  });
  return out;
}
"""

JS_PUBLICATIONS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  return [...document.querySelectorAll('#gsc_a_b tr.gsc_a_tr')].map((tr, i) => {
    const a = tr.querySelector('a.gsc_a_at');
    let link = null;
    if (a) {
      const raw = a.getAttribute('data-href') ||
                  (a.getAttribute('href') && !a.getAttribute('href').startsWith('javascript')
                     ? a.getAttribute('href') : null);
      if (raw) link = new URL(raw, location.href).href;
    }
    const grays = tr.querySelectorAll('td.gsc_a_t div.gs_gray');
    let venue = null;
    if (grays[1]) {
      const c = grays[1].cloneNode(true);
      c.querySelectorAll('.gs_oph').forEach(n => n.remove());   // hidden ", YEAR" suffix
      venue = clean(c.textContent);
    }
    const cited = tr.querySelector('td.gsc_a_c a.gsc_a_ac');
    const year = tr.querySelector('td.gsc_a_y .gsc_a_h');
    return {
      row_index: i + 1,
      title: a ? clean(a.textContent) : null,
      source_url: link,
      authors: grays[0] ? clean(grays[0].textContent) : null,
      venue: venue,
      cited_by: cited ? clean(cited.textContent) : null,
      year: year ? clean(year.textContent) : null,
    };
  });
}
"""

JS_BLOCK_CHECK = r"""
() => {
  const t = (document.body && document.body.innerText || '').toLowerCase();
  return {
    captcha: !!document.querySelector('#gs_captcha_ccl, #captcha-form, form[action*="sorry"], iframe[src*="recaptcha"]'),
    unusual: t.includes('unusual traffic') || t.includes("not a robot") || t.includes('please show you'),
    profile: !!document.querySelector('#gsc_prf_in'),
  };
}
"""


def _check_blocked(page, status=None, stats=None):
    if status in (403, 429):
        raise AccessBlockedError(f"Google Scholar responded with HTTP {status} (access denied / rate limited).")
    if page.url and "/sorry/" in page.url:
        raise AccessBlockedError(f"Redirected to Google's block page: {redact_url(page.url)}")
    if stats and stats.has_429:
        url_info = f" on {redact_url(stats.last_429_url)}" if getattr(stats, "last_429_url", None) else ""
        raise AccessBlockedError(f"Google Scholar responded with HTTP 429 (rate limited){url_info} during extraction.")
    state = page.evaluate(JS_BLOCK_CHECK)
    if state["captcha"] or (state["unusual"] and not state["profile"]):
        raise AccessBlockedError("Google Scholar displayed a CAPTCHA / unusual-traffic page. Scraping stopped; nothing was bypassed.")


def _polite_pause(base: float):
    time.sleep(base + random.uniform(0, base / 2))


def validate_user_data_dir(user_data_dir: Path):
    """
    Ensures that the user_data_dir is an isolated dedicated directory and never
    resolves to or overlaps with the user's personal default Chrome or Edge browser profile.
    """
    resolved = user_data_dir.resolve()
    appdata = os.environ.get("LOCALAPPDATA", "")
    if appdata:
        forbidden = [
            Path(appdata) / "Google" / "Chrome" / "User Data",
            Path(appdata) / "Microsoft" / "Edge" / "User Data",
        ]
        for f in forbidden:
            f_resolved = f.resolve()
            if resolved == f_resolved or f_resolved in resolved.parents or resolved in f_resolved.parents:
                raise ValueError(
                    f"Security violation: '{resolved}' points to or overlaps with the personal default browser "
                    f"profile directory ('{f_resolved}'). A dedicated directory (such as ./.chrome_profile) must be used."
                )


def _launch(p, headless: bool, channel: str, data: ProfileData):
    """
    channel: 'auto' (Google Chrome, then Edge, then bundled Chromium), 'chrome', 'chromium', or 'msedge'.
    """
    order = {"auto": ["chrome", "msedge", None], "chromium": [None]}.get(channel, [channel])
    last = None
    for ch in order:
        try:
            b = p.chromium.launch(headless=headless, channel=ch) if ch else p.chromium.launch(headless=headless)
            data.browser_used = ch or "chromium (bundled)"
            log.info("Browser launched: %s", data.browser_used)
            return b
        except PlaywrightError as e:
            last = e
            log.info("Browser '%s' unavailable, trying next option.", ch or "chromium")
    raise ScraperError(f"No usable browser found (tried {order}). Run 'python -m playwright install chromium'. Last error: {last}")


def _launch_persistent(p, user_data_dir: Path, headless: bool, channel: str, data: ProfileData, timeout_ms: int):
    """
    Launches a persistent browser context using a dedicated isolated user-data directory.
    channel: 'auto' (Google Chrome, then Edge, then bundled Chromium), 'chrome', 'chromium', or 'msedge'.
    """
    validate_user_data_dir(user_data_dir)
    user_data_dir.mkdir(parents=True, exist_ok=True)
    order = {"auto": ["chrome", "msedge", None], "chromium": [None]}.get(channel, [channel])
    last = None
    for ch in order:
        try:
            kwargs = {
                "user_data_dir": str(user_data_dir),
                "headless": headless,
                "locale": "en-US",
                "viewport": {"width": 1280, "height": 900},
                "timeout": timeout_ms,
            }
            if ch:
                kwargs["channel"] = ch
            ctx = p.chromium.launch_persistent_context(**kwargs)
            data.browser_used = (ch or "chromium (bundled)") + " (persistent)"
            log.info("Persistent browser launched: %s (profile: %s)", data.browser_used, user_data_dir)
            return ctx
        except PlaywrightError as e:
            last = e
            log.info("Persistent browser '%s' unavailable: %s", ch or "chromium", e)
    raise ScraperError(f"No usable persistent browser found (tried {order}). Last error: {last}")


def setup_interactive_session(user_data_dir: Path, channel: str = "auto", timeout_s: float = 60.0):
    """
    Interactive setup command that launches Google Chrome in a visible window with
    the dedicated persistent profile. Allows manual authorization on Google Scholar.
    """
    validate_user_data_dir(user_data_dir)
    banner = "=" * 80
    print(f"\n{banner}\nPERSISTENT SESSION SETUP (GOOGLE CHROME)\n{banner}")
    print(f"Profile directory : {user_data_dir.resolve()}")
    print("Launching browser to: https://scholar.google.com/")
    print("\nYou may now navigate, log in, or complete verification in the browser window.")
    print("When you are done, press ENTER in this terminal or close the browser window.")
    print(f"{banner}\n", flush=True)

    with sync_playwright() as p:
        dummy_data = ProfileData(requested_url="https://scholar.google.com/")
        timeout_ms = int(timeout_s * 1000)
        context = _launch_persistent(p, user_data_dir, headless=False, channel=channel, data=dummy_data, timeout_ms=timeout_ms)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto("https://scholar.google.com/", wait_until="domcontentloaded")
        except Exception as e:
            log.warning("Initial navigation to scholar.google.com encountered: %s", e)

        try:
            input("Press ENTER to save profile session and exit... ")
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            try:
                context.close()
            except Exception:
                pass
            print(f"\nSession state preserved in: {user_data_dir.resolve()}\n")


JS_PAGE_STATE = r"""
() => {
  const t = (document.body && document.body.innerText || '').toLowerCase();
  if (document.querySelector('#gsc_prf_in')) return 'profile';
  // An interactive challenge exists only if a challenge widget/form is really present.
  const widget = document.querySelector('iframe[src*="recaptcha"], .g-recaptcha, #recaptcha, #captcha-form, #gs_captcha_ccl');
  if (widget) return 'captcha';
  if (t.includes('unusual traffic') || t.includes('try your request again later')) return 'unusual_traffic';
  return 'unknown';
}
"""

STATE_MESSAGES = {
    "captcha": ("INTERACTIVE CAPTCHA CHALLENGE detected.",
                "ACTION: solve the CAPTCHA yourself in the open browser window. The scraper resumes automatically "
                "when the researcher's profile appears."),
    "unusual_traffic": ("GOOGLE 'UNUSUAL TRAFFIC' / HTTP 429 BLOCK PAGE (no interactive CAPTCHA).",
                        "ACTION: there is nothing to solve or click. Do NOT refresh. Leave the window open and wait; "
                        "Google lifts this block by itself after some time (minutes to hours), or try a different "
                        "network. The scraper only watches the page and resumes if the profile appears."),
    "unknown": ("UNRECOGNISED PAGE (not the profile, no CAPTCHA widget, no known block text).",
                "ACTION: look at the window. Nothing is automated; the scraper keeps waiting passively."),
    "profile": ("RESEARCHER PROFILE visible.", "Resuming; the user ID will be verified before extraction."),
}


def _await_manual_access(page, data: ProfileData, reason: str, wait_s: float):
    """
    MANUAL MODE ONLY. Read-only wait: the page is NEVER reloaded, retried, clicked or
    filled in. Every second the DOM is classified as profile / interactive CAPTCHA /
    unusual-traffic block; the user is told what the page really is and only asked to
    act when an interactive CAPTCHA is present. Returns when the profile appears.
    """
    bar = "!" * 78
    log.warning("Paused in manual mode (%s). Browser stays open; no refresh/retry will be made.", reason)
    deadline, last_state, started = time.time() + wait_s, None, time.time()
    while time.time() < deadline:
        try:
            if page.is_closed():
                raise ScraperError("Browser window was closed during manual verification.")
            state = page.evaluate(JS_PAGE_STATE)
        except PlaywrightError:
            time.sleep(1.0)            # page mid-navigation; keep waiting
            continue
        if state != last_state:
            title, action = STATE_MESSAGES[state]
            print(f"\n{bar}\n[{time.strftime('%H:%M:%S')}] PAGE STATE: {title}\n{action}\n"
                  f"(Waiting up to {int(wait_s)} s in total. Nothing is reloaded or retried.)\n{bar}\n", flush=True)
            log.info("Page state: %s", state)
            last_state = state
        if state == "profile":
            data.warnings.append(f"Access was initially blocked ({reason}); the profile became accessible "
                                 f"after {int(time.time() - started)} s of passive waiting.")
            return
        time.sleep(1.0)
    raise AccessBlockedError(f"Profile still not accessible after {int(wait_s)} s of passive manual waiting. "
                             f"Last page state: {last_state} ({reason}). No CSVs were produced.")


def _verify_profile_owner(page, data: ProfileData, stats: Optional[DiagnosticNetworkStats] = None):
    """Confirm the page now shown is the requested researcher's profile."""
    data.loaded_url = page.url
    if stats and stats.last_main_frame_status is not None:
        data.http_status = stats.last_main_frame_status
    shown = parse_qs(urlparse(page.url).query).get("user", [None])[0]
    if shown != data.google_scholar_id:
        raise ScraperError(f"Loaded page belongs to user '{shown}', not requested '{data.google_scholar_id}'. Stopped.")


def scrape_profile(url: str, headless: bool = True, delay: float = 3.0,
                   timeout_s: float = 45.0, max_show_more: int = 100,
                   channel: str = "auto", manual: bool = False,
                   manual_wait_s: float = 600.0,
                   persistent: bool = False,
                   user_data_dir: Optional[Path] = None) -> ProfileData:
    data = ProfileData(requested_url=url)
    try:
        return _scrape(data, url, headless, delay, timeout_s, max_show_more, channel, manual, manual_wait_s,
                       persistent=persistent, user_data_dir=user_data_dir)
    except ScraperError as e:
        e.profile_data = data          # let the caller report status / landing URL
        raise


def _scrape(data, url, headless, delay, timeout_s, max_show_more, channel,
            manual=False, manual_wait_s=600.0,
            persistent=False, user_data_dir: Optional[Path] = None) -> ProfileData:
    data.google_scholar_id = parse_google_scholar_id(url)
    timeout_ms = int(timeout_s * 1000)

    with sync_playwright() as p:
        stats = DiagnosticNetworkStats()
        browser = None
        context = None
        try:
            if persistent:
                pdir = Path(user_data_dir or (Path.cwd() / ".chrome_profile"))
                context = _launch_persistent(p, pdir, headless, channel, data, timeout_ms)
                page = context.pages[0] if context.pages else context.new_page()
            else:
                browser = _launch(p, headless, channel, data)
                context = browser.new_context(locale="en-US", viewport={"width": 1280, "height": 900})
                page = context.new_page()

            page.set_default_timeout(timeout_ms)

            # Diagnostic request-frequency and network logging
            setup_diagnostic_listeners(page, stats, log)

            page.on("framenavigated",
                    lambda fr: data.navigations.append(fr.url) if fr == page.main_frame else None)

            log.info("Opening profile: %s", url)
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except PlaywrightTimeout as e:
                raise ScraperError(f"Timed out loading the profile page: {e}") from e
            except PlaywrightError as e:
                raise ScraperError(f"Browser could not load the profile page: {e}") from e

            data.http_status = resp.status if resp else None
            data.loaded_url = page.url
            log.info("HTTP status: %s | landed on: %s", data.http_status, redact_url(page.url))

            try:
                _check_blocked(page, data.http_status, stats=stats)
            except AccessBlockedError as blk:
                if not manual:
                    raise
                _await_manual_access(page, data, str(blk), manual_wait_s)
            if data.http_status == 404:
                raise ScraperError("Profile not found (HTTP 404). Check the 'user' id in the URL.")

            try:
                page.wait_for_selector("#gsc_prf_in", timeout=timeout_ms)
            except PlaywrightTimeout as e:
                _check_blocked(page, stats=stats)
                raise ScraperError("Profile header (#gsc_prf_in) never appeared; page layout may have changed.") from e
            _verify_profile_owner(page, data, stats=stats)
            if manual:
                data.warnings.append("Manual mode: browser was visible; the user could inspect the page.")
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeout:
                data.warnings.append("Network did not become idle within 15 s; continued with loaded DOM.")

            # ---- Expand the publication table via SHOW MORE -------------------
            rows = page.locator(ROW_SELECTOR)
            btn = page.locator(SHOW_MORE)
            log.info("Initial publication rows: %d", rows.count())
            while True:
                _check_blocked(page, stats=stats)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                if btn.count() == 0:
                    data.warnings.append("SHOW MORE button not found; listing completeness unknown.")
                    break
                if btn.is_disabled():
                    data.listing_complete = True
                    log.info("SHOW MORE is disabled: full publication listing loaded.")
                    break
                if data.show_more_clicks >= max_show_more:
                    data.warnings.append(f"Stopped after max_show_more={max_show_more} clicks; listing may be incomplete.")
                    break
                before = rows.count()
                _polite_pause(delay)
                btn.scroll_into_view_if_needed()
                btn.click()
                data.show_more_clicks += 1
                try:
                    page.wait_for_function(
                        """(n) => document.querySelectorAll('#gsc_a_b tr.gsc_a_tr').length > n
                                 || (document.querySelector('#gsc_bpf_more') || {}).disabled""",
                        arg=before, timeout=timeout_ms)
                except PlaywrightTimeout:
                    _check_blocked(page, stats=stats)
                    data.warnings.append(f"SHOW MORE click #{data.show_more_clicks} loaded no new rows within timeout; listing may be incomplete.")
                    break
                _check_blocked(page, stats=stats)
                log.info("SHOW MORE click #%d: %d -> %d rows", data.show_more_clicks, before, rows.count())

            # ---- Extract everything from the live DOM -------------------------
            data.header = page.evaluate(JS_HEADER)
            data.metrics = page.evaluate(JS_METRICS)
            data.publications = page.evaluate(JS_PUBLICATIONS)
            footer = page.locator("#gsc_a_nn")
            data.footer_text = footer.inner_text().strip() if footer.count() else None
            log.info("Extracted %d publication rows; footer reads: %s", len(data.publications), data.footer_text)
        finally:
            data.network_stats = stats.to_dict()
            if context:
                try:
                    context.close()
                except Exception:
                    pass
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
    return data
