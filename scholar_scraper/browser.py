"""
Playwright profile extraction and profile-click-based publication detail scraping.

Scope rules enforced here:
  * Stage 1 opens only the supplied profile URL.
  * "SHOW MORE" is clicked to load the rest of the publication table. This is an
    in-page AJAX request; the browser never navigates away from the profile.
  * Stage 1 only reads publication links. Stage 2 clicks matching profile links.
  * CAPTCHA / "unusual traffic" pages are detected and reported, never bypassed.
"""

import logging
import os
import random
import re
import socket
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlsplit, urlunsplit, urljoin

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

log = logging.getLogger(__name__)

ROW_SELECTOR = "#gsc_a_b tr.gsc_a_tr"
SHOW_MORE = "#gsc_bpf_more"

SUGGESTED_PROXY_ERROR_MESSAGE = (
    "Unable to connect to Google Scholar because the configured proxy connection failed. "
    "Please check your proxy settings or select a direct connection."
)

PROXY_ERROR_CODES = (
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_SOCKS_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED",
    "ERR_PROXY_CERTIFICATE_INVALID",
    "ERR_MANDATORY_PROXY_CONFIGURATION_FAILED",
    "ERR_NO_SUPPORTED_PROXIES",
)

NETWORK_ERROR_CODES = (
    "ERR_INTERNET_DISCONNECTED",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_CONNECTION_REFUSED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_NETWORK_CHANGED",
    "ERR_ADDRESS_UNREACHABLE",
    "ERR_TIMED_OUT",
    "ERR_SSL_PROTOCOL_ERROR",
)

VALID_PROXY_SCHEMES = {"http", "https", "socks4", "socks5"}
PROXY_ENV_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


class ScraperError(Exception):
    """Base error for scraping failures."""


class AccessBlockedError(ScraperError):
    """Google denied access; ``block_kind`` is safe for GUI diagnostics."""

    def __init__(self, message: str, block_kind: str = "blocked", http_status: Optional[int] = None):
        super().__init__(message)
        self.block_kind = block_kind
        self.http_status = http_status


class ManualVerificationCancelled(ScraperError):
    """The user cancelled manual verification or closed its browser window."""


class ManualVerificationUnsuccessful(AccessBlockedError):
    """A challenge was available but legitimate access was not restored."""

    def __init__(self, message: str, http_status: Optional[int] = None):
        super().__init__(message, block_kind="manual_verification_unsuccessful", http_status=http_status)


class InvalidProfileUrlError(ScraperError):
    """The supplied URL is not a Google Scholar profile URL."""


class ProxyConfigurationError(ScraperError):
    """The configured proxy URL or setting is invalid or malformed."""

    def __init__(self, message: str, proxy_server: Optional[str] = None, failed_url: Optional[str] = None):
        self.proxy_server = proxy_server
        self.failed_url = failed_url
        super().__init__(message)


class ProxyConnectionError(ScraperError):
    """Browser navigation or pre-check failed because the proxy server refused/failed connection."""

    def __init__(
        self,
        message: str,
        failed_url: Optional[str] = None,
        proxy_server: Optional[str] = None,
        error_code: str = "ERR_PROXY_CONNECTION_FAILED",
        raw_error: Optional[str] = None,
    ):
        self.failed_url = failed_url
        self.proxy_server = proxy_server
        self.error_code = error_code
        self.raw_error = raw_error
        super().__init__(message)


class NetworkConnectionError(ScraperError):
    """Browser navigation failed due to general network connectivity issues (DNS, offline, timeout)."""

    def __init__(
        self,
        message: str,
        failed_url: Optional[str] = None,
        error_code: str = "NETWORK_FAILURE",
        raw_error: Optional[str] = None,
    ):
        self.failed_url = failed_url
        self.error_code = error_code
        self.raw_error = raw_error
        super().__init__(message)


@dataclass
class BrowserConnectionDiagnostics:
    """
    Lightweight browser & proxy connection diagnostics report.
    Tracks:
      - Browser launched successfully (`browser_launched`)
      - Browser network connection status (`network_connected`)
      - Google Scholar page accessibility (`scholar_accessible`)
      - Proxy status (`proxy_status`)
      - Navigation failure reason (`navigation_failure_reason`)
    """

    browser_launched: bool = False
    browser_name: Optional[str] = None
    connection_mode: str = "direct"  # "direct" or "proxy"
    proxy_configured: bool = False
    proxy_server: Optional[str] = None
    proxy_status: str = "Direct connection (no proxy configured; OS/env proxies bypassed)"
    ambient_os_proxy_detected: Optional[str] = None
    network_connected: bool = False
    scholar_accessible: bool = False
    http_status: Optional[int] = None
    target_url: Optional[str] = None
    failure_category: Optional[str] = None  # None, "proxy_config", "proxy_connection", "network", "blocked_429_captcha", "browser_launch"
    navigation_failure_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "browser_launched": self.browser_launched,
            "browser_name": self.browser_name,
            "connection_mode": self.connection_mode,
            "proxy_configured": self.proxy_configured,
            "proxy_server": self.proxy_server,
            "proxy_status": self.proxy_status,
            "ambient_os_proxy_detected": self.ambient_os_proxy_detected,
            "network_connected": self.network_connected,
            "scholar_accessible": self.scholar_accessible,
            "http_status": self.http_status,
            "target_url": redact_url(self.target_url) if self.target_url else None,
            "failure_category": self.failure_category,
            "navigation_failure_reason": self.navigation_failure_reason,
        }


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
    initial_row_count: int = 0
    pagination_steps: list = field(default_factory=list)
    stop_reason: Optional[str] = None
    navigations: list = field(default_factory=list)
    browser_used: Optional[str] = None
    warnings: list = field(default_factory=list)
    network_stats: Optional[dict] = None
    connection_diagnostics: Optional[dict] = None


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

        # Track the current main-frame result. A legitimate later navigation
        # must clear stale 429/sorry flags before extraction can resume.
        try:
            if is_nav and response.frame == page.main_frame:
                stats.last_main_frame_status = status
                stats.last_main_frame_url = url
                stats.has_429 = status == 429
                stats.last_429_url = url if status == 429 else None
                stats.sorry_redirect = "/sorry/" in url
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
      const raw = a.getAttribute('href') && !a.getAttribute('href').startsWith('javascript')
                    ? a.getAttribute('href')
                    : a.getAttribute('data-href');
      if (raw) {
        const base = (location && location.href && location.href.startsWith('http'))
                       ? location.href
                       : 'https://scholar.google.com/citations';
        link = new URL(raw, base).href;
      }
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

JS_PUBLICATION_DETAIL = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const titleEl = document.querySelector('#gsc_oci_title, #gsc_vcd_title');
  const titleLink = titleEl ? titleEl.querySelector('a.gsc_oci_title_link, a.gsc_vcd_title_link, a') : null;
  let titleText = '';
  if (titleLink) {
    titleText = clean(titleLink.textContent);
  } else if (titleEl) {
    const clone = titleEl.cloneNode(true);
    clone.querySelectorAll('#gsc_oci_title_gg, .gsc_oci_title_gg').forEach(n => n.remove());
    titleText = clean(clone.textContent);
  }

  const fields = {};
  let citedByCount = null;
  let citedByUrl = null;
  document.querySelectorAll('#gsc_oci_table .gs_scl, #gsc_vcd_table .gs_scl').forEach(row => {
    const fEl = row.querySelector('.gsc_oci_field, .gsc_vcd_field');
    const vEl = row.querySelector('.gsc_oci_value, .gsc_vcd_value');
    if (!fEl || !vEl) return;
    const key = clean(fEl.textContent).toLowerCase();
    if (!key) return;
    if (key === 'total citations') {
      const citeLink = vEl.querySelector('a[href*="cites="], .gsc_oci_g_al, a');
      if (citeLink) {
        const m = clean(citeLink.textContent).match(/(?:Cited by\s+)?(\d[\d,]*)/i);
        if (m) citedByCount = m[1].replace(/,/g, '');
        if (citeLink.href) citedByUrl = citeLink.href;
      }
      fields[key] = clean(vEl.textContent);
    } else if (key === 'scholar articles') {
      fields[key] = clean(vEl.textContent);
    } else {
      fields[key] = clean(vEl.textContent);
    }
  });

  // Extract year-wise citation graph (exact counts from .gsc_oci_g_a / .gsc_oci_g_al and #gsc_oci_graph_x .gsc_oci_g_t)
  const graphYears = [...document.querySelectorAll('#gsc_oci_graph_x .gsc_oci_g_t, #gsc_vcd_graph_x .gsc_vcd_g_t')].map(el => ({
    year: clean(el.textContent),
    style: el.getAttribute('style') || '',
    left: parseFloat(((el.getAttribute('style') || '').match(/left\s*:\s*([\d.]+)px/i) || [])[1] || 'NaN')
  }));
  const graphBars = [...document.querySelectorAll('#gsc_oci_graph_bars a.gsc_oci_g_a, #gsc_vcd_graph_bars a.gsc_vcd_g_a')].map(a => {
    const href = a.getAttribute('href') || '';
    const yMatch = href.match(/[?&]as_ylo=(\d{4})/) || href.match(/[?&]as_yhi=(\d{4})/);
    const al = a.querySelector('.gsc_oci_g_al, .gsc_vcd_g_al');
    const countStr = al ? clean(al.textContent) : clean(a.textContent);
    const zIndexMatch = (a.getAttribute('style') || '').match(/z-index\s*:\s*(\d+)/i);
    const leftMatch = (a.getAttribute('style') || '').match(/left\s*:\s*([\d.]+)px/i);
    return {
      href: href,
      year_from_href: yMatch ? yMatch[1] : null,
      count: countStr,
      left: leftMatch ? parseFloat(leftMatch[1]) : null,
      z_index: zIndexMatch ? parseInt(zIndexMatch[1], 10) : null
    };
  });

  return {
    title: titleText || null,
    external_url: titleLink && titleLink.href ? titleLink.href : null,
    fields: fields,
    cited_by_count: citedByCount,
    cited_by_url: citedByUrl,
    graph_years: graphYears,
    graph_bars: graphBars
  };
}
"""

JS_BLOCK_CHECK = r"""
() => {
  const t = (document.body && document.body.innerText || '').toLowerCase();
  return {
    captcha: !!document.querySelector('#gs_captcha_ccl, #captcha-form, form[action*="sorry"], iframe[src*="recaptcha"]'),
    unusual: t.includes('unusual traffic') || t.includes("not a robot") || t.includes('please show you'),
    profile: !!document.querySelector('#gsc_prf_in, #gsc_oci_title, #gsc_vcd_title'),
  };
}
"""


def _check_blocked(page, status=None, stats=None):
    if status in (403, 429):
        raise AccessBlockedError(
            f"Google Scholar responded with HTTP {status} (access denied / rate limited).",
            block_kind="http_429" if status == 429 else "http_403",
            http_status=status,
        )
    if page.url and "/sorry/" in page.url:
        raise AccessBlockedError(
            f"Redirected to Google's block page: {redact_url(page.url)}",
            block_kind="sorry_page",
            http_status=status,
        )
    if stats and getattr(stats, "sorry_redirect", False):
        raise AccessBlockedError(
            "Redirected to Google's /sorry/ block page during navigation.",
            block_kind="sorry_page",
            http_status=getattr(stats, "last_main_frame_status", None),
        )
    if stats and stats.has_429:
        url_info = f" on {redact_url(stats.last_429_url)}" if getattr(stats, "last_429_url", None) else ""
        raise AccessBlockedError(
            f"Google Scholar responded with HTTP 429 (rate limited){url_info} during extraction.",
            block_kind="http_429",
            http_status=429,
        )
    try:
        state = page.evaluate(JS_BLOCK_CHECK)
    except PlaywrightError as e:
        if (page.url and "/sorry/" in page.url) or (stats and (stats.has_429 or stats.sorry_redirect)):
            raise AccessBlockedError(
                "Redirected to Google's HTTP 429 /sorry/ block page during navigation.",
                block_kind="sorry_page",
                http_status=getattr(stats, "last_main_frame_status", None) if stats else None,
            ) from e
        raise
    if state["captcha"] or (state["unusual"] and not state["profile"]):
        kind = "captcha" if state["captcha"] else "unusual_traffic"
        raise AccessBlockedError(
            "Google Scholar displayed an interactive CAPTCHA challenge."
            if kind == "captcha"
            else "Google Scholar displayed an unusual-traffic rate-limit page without an interactive challenge.",
            block_kind=kind,
            http_status=status,
        )


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


def detect_inherited_system_or_env_proxy() -> dict:
    """
    Inspect current environment variables and OS-level proxy settings (such as Windows
    Internet Settings `HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings`)
    to detect whether an ambient proxy is present on the host system.
    """
    env_proxies = {k: os.environ[k] for k in PROXY_ENV_VARS if os.environ.get(k)}
    try:
        os_proxies = dict(urllib.request.getproxies())
    except Exception:
        os_proxies = {}
    return {
        "env_proxies": env_proxies,
        "os_proxies": os_proxies,
        "has_ambient_proxy": bool(env_proxies or os_proxies),
    }


def redact_proxy_url(proxy_url: Optional[str]) -> str:
    """Return a display-safe proxy URL without username or password."""
    cleaned = str(proxy_url or "").strip()
    try:
        parsed = urlparse(cleaned)
        if parsed.scheme and parsed.hostname:
            port = f":{parsed.port}" if parsed.port else ""
            return f"{parsed.scheme.lower()}://{parsed.hostname}{port}"
    except (ValueError, TypeError):
        pass
    return re.sub(r"(?<=://)[^/@\s]+@", "[REDACTED]@", cleaned)


def validate_proxy_config(proxy_url: Optional[str]) -> Optional[dict]:
    """
    Validate an explicit proxy configuration string.
      - If `proxy_url` is None, empty, or `'direct'` (case-insensitive), returns `None`
        (indicating a direct browser connection).
      - Otherwise validates scheme (`http`, `https`, `socks4`, `socks5`), hostname,
        and port (`1..65535`).
      - Raises `ProxyConfigurationError` if the proxy URL is malformed or invalid.
    """
    if proxy_url is None:
        return None
    cleaned = str(proxy_url).strip()
    display_proxy = redact_proxy_url(cleaned)
    if not cleaned or cleaned.lower() in ("direct", "none", "off"):
        return None

    try:
        parsed = urlparse(cleaned)
    except Exception as exc:
        raise ProxyConfigurationError(
            f"Invalid proxy URL '{display_proxy}': could not parse URL ({exc}). "
            "Expected format: http://host:port or socks5://host:port.",
            proxy_server=display_proxy,
        ) from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in VALID_PROXY_SCHEMES:
        raise ProxyConfigurationError(
            f"Invalid proxy URL '{display_proxy}': unsupported or missing scheme '{parsed.scheme or ''}'. "
            f"Supported proxy schemes are: {', '.join(sorted(VALID_PROXY_SCHEMES))} "
            "(e.g., http://127.0.0.1:8080 or socks5://127.0.0.1:1080).",
            proxy_server=display_proxy,
        )

    try:
        port = parsed.port
    except ValueError as exc:
        raise ProxyConfigurationError(
            f"Invalid proxy URL '{display_proxy}': port must be an integer between 1 and 65535.",
            proxy_server=display_proxy,
        ) from exc

    hostname = (parsed.hostname or "").strip()
    if not hostname or " " in hostname or "/" in hostname:
        raise ProxyConfigurationError(
            f"Invalid proxy URL '{display_proxy}': missing or invalid proxy hostname.",
            proxy_server=display_proxy,
        )
    if port is None or not (1 <= int(port) <= 65535):
        raise ProxyConfigurationError(
            f"Invalid proxy URL '{display_proxy}': an explicit port between 1 and 65535 is required "
            "(e.g., http://127.0.0.1:8080).",
            proxy_server=display_proxy,
        )

    server_str = f"{scheme}://{hostname}:{port}"
    playwright_proxy = {"server": server_str}
    if parsed.username:
        playwright_proxy["username"] = parsed.username
    if parsed.password:
        playwright_proxy["password"] = parsed.password

    return {
        "server": server_str,
        "scheme": scheme,
        "host": hostname,
        "port": int(port),
        "username": parsed.username,
        "password": parsed.password,
        "redacted": server_str,
        "playwright_proxy": playwright_proxy,
    }


def check_proxy_reachable(proxy_cfg: dict, timeout_s: float = 3.0) -> tuple[bool, Optional[str]]:
    """
    Perform a lightweight TCP socket reachability check to `(host, port)` of a validated
    proxy configuration before starting browser navigation.
    """
    host = proxy_cfg["host"]
    port = int(proxy_cfg["port"])
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True, None
    except OSError as exc:
        return False, f"Proxy server {proxy_cfg['redacted']} refused connection or timed out ({exc})"


def build_playwright_proxy_settings(
    proxy_url: Optional[str] = None,
    verify_reachable: bool = True,
    check_timeout_s: float = 2.5,
    target_url: Optional[str] = None,
) -> tuple[Optional[dict], list[str], BrowserConnectionDiagnostics]:
    """
    Build Playwright proxy parameters and Chromium launch arguments while populating
    initial `BrowserConnectionDiagnostics`:
      1. If no proxy is configured (`proxy_url=None` or `'direct'`), configures a direct
         browser connection (`args=['--no-proxy-server']`) so Chromium never unintentionally
         inherits a broken OS-level or environment proxy (such as Windows `socks=127.0.0.1:9050`).
      2. If a proxy is configured, validates its syntax via `validate_proxy_config()` and
         optionally verifies TCP reachability before launching the browser.
    """
    ambient = detect_inherited_system_or_env_proxy()
    ambient_summary = None
    if ambient["has_ambient_proxy"]:
        parts = []
        if ambient["os_proxies"]:
            parts.append(f"OS proxy={ambient['os_proxies']}")
        if ambient["env_proxies"]:
            parts.append(f"ENV proxy={list(ambient['env_proxies'].keys())}")
        ambient_summary = "; ".join(parts)

    cfg = validate_proxy_config(proxy_url)
    if cfg is None:
        diag = BrowserConnectionDiagnostics(
            connection_mode="direct",
            proxy_configured=False,
            proxy_server=None,
            proxy_status=(
                f"Direct connection (--no-proxy-server; ambient {ambient_summary} bypassed)"
                if ambient_summary
                else "Direct connection (no proxy configured)"
            ),
            ambient_os_proxy_detected=ambient_summary,
            target_url=target_url,
        )
        return None, ["--no-proxy-server"], diag

    diag = BrowserConnectionDiagnostics(
        connection_mode="proxy",
        proxy_configured=True,
        proxy_server=cfg["redacted"],
        proxy_status=f"Configured proxy: {cfg['redacted']} (validating...)",
        ambient_os_proxy_detected=ambient_summary,
        target_url=target_url,
    )

    if verify_reachable:
        reachable, err_reason = check_proxy_reachable(cfg, timeout_s=check_timeout_s)
        if not reachable:
            diag.proxy_status = f"Proxy connection failed: {err_reason}"
            diag.network_connected = False
            diag.scholar_accessible = False
            diag.failure_category = "proxy_connection"
            diag.navigation_failure_reason = err_reason
            msg = (
                f"{SUGGESTED_PROXY_ERROR_MESSAGE}\n\n"
                f"Configured Proxy: {cfg['redacted']}\n"
                f"Reason: {err_reason}"
            )
            if target_url:
                msg += f"\nFailed URL: {target_url}"
            raise ProxyConnectionError(
                msg,
                failed_url=target_url,
                proxy_server=cfg["redacted"],
                error_code="ERR_PROXY_CONNECTION_FAILED",
                raw_error=err_reason,
            )
        diag.proxy_status = f"Configured proxy: {cfg['redacted']} (reachable)"
    else:
        diag.proxy_status = f"Configured proxy: {cfg['redacted']}"

    return cfg["playwright_proxy"], [f"--proxy-server={cfg['server']}"], diag


def classify_navigation_error(
    exc: Exception,
    url: str,
    proxy_url: Optional[str] = None,
    diagnostics: Optional[BrowserConnectionDiagnostics] = None,
    context_label: str = "profile",
) -> ScraperError:
    """
    Classify a Playwright navigation exception (`PlaywrightError` or `PlaywrightTimeout`)
    into a specific, user-understandable error:
      - `ProxyConnectionError`: when `ERR_PROXY_CONNECTION_FAILED` or another proxy error code occurs.
      - `NetworkConnectionError`: when DNS, offline, timeout, or general socket errors occur.
      - `ScraperError`: fallback for any non-network browser error.
    """
    raw_text = str(exc)
    upper_text = raw_text.upper()

    for code in PROXY_ERROR_CODES:
        if code in upper_text:
            if diagnostics is not None:
                diagnostics.network_connected = False
                diagnostics.scholar_accessible = False
                diagnostics.failure_category = "proxy_connection"
                diagnostics.navigation_failure_reason = f"Proxy connection failed ({code})"
                diagnostics.proxy_status = f"Proxy connection failed ({code})"
            proxy_info = f"\nConfigured Proxy: {proxy_url}" if proxy_url else ""
            msg = (
                f"{SUGGESTED_PROXY_ERROR_MESSAGE}\n\n"
                f"Failed URL: {url}{proxy_info}\n"
                f"Error Code: {code}"
            )
            return ProxyConnectionError(
                msg,
                failed_url=url,
                proxy_server=proxy_url,
                error_code=code,
                raw_error=raw_text,
            )

    for code in NETWORK_ERROR_CODES:
        if code in upper_text:
            if diagnostics is not None:
                diagnostics.network_connected = False
                diagnostics.scholar_accessible = False
                diagnostics.failure_category = "network"
                diagnostics.navigation_failure_reason = f"General network connection failed ({code})"
            msg = (
                f"Unable to connect to Google Scholar due to a network connectivity failure ({code}). "
                "Please check your internet connection and try again.\n\n"
                f"Failed URL: {url}\n"
                f"Error Code: {code}"
            )
            return NetworkConnectionError(
                msg,
                failed_url=url,
                error_code=code,
                raw_error=raw_text,
            )

    if isinstance(exc, PlaywrightTimeout) or "TIMEOUT" in upper_text:
        if diagnostics is not None:
            diagnostics.network_connected = False
            diagnostics.scholar_accessible = False
            diagnostics.failure_category = "network"
            diagnostics.navigation_failure_reason = f"Navigation timed out loading {context_label} page"
        msg = (
            f"Timed out loading the {context_label} page. Please check your network connection and try again.\n\n"
            f"Failed URL: {url}\n"
            f"Details: {raw_text}"
        )
        return NetworkConnectionError(
            msg,
            failed_url=url,
            error_code="ERR_NAVIGATION_TIMEOUT",
            raw_error=raw_text,
        )

    if diagnostics is not None:
        diagnostics.network_connected = False
        diagnostics.scholar_accessible = False
        diagnostics.failure_category = "network"
        diagnostics.navigation_failure_reason = raw_text
    return ScraperError(f"Browser could not load the {context_label} page ({url}): {raw_text}")


def log_connection_diagnostics(diag: BrowserConnectionDiagnostics, logger: logging.Logger = log):
    """Log the structured browser & proxy connection diagnostics clearly."""
    logger.info(
        "[CONNECTION DIAGNOSTICS] Browser Launched: %s (%s) | Mode: %s | Proxy Status: %s | "
        "Network Connected: %s | Google Scholar Accessible: %s (HTTP %s) | Failure Reason: %s",
        "YES" if diag.browser_launched else "NO",
        diag.browser_name or "none",
        diag.connection_mode.upper(),
        diag.proxy_status,
        "YES" if diag.network_connected else "NO",
        "YES" if diag.scholar_accessible else "NO",
        diag.http_status if diag.http_status is not None else "N/A",
        diag.navigation_failure_reason or "None",
    )


def _clean_env_for_direct_connection() -> dict[str, str]:
    """Return a copy of os.environ with ambient proxy variables removed for direct browser launches."""
    env = dict(os.environ)
    for k in PROXY_ENV_VARS:
        env.pop(k, None)
    return env


def _launch(
    p,
    headless: bool,
    channel: str,
    data: ProfileData,
    proxy_url: Optional[str] = None,
    verify_proxy_reachable: bool = True,
):
    """
    channel: 'auto' (Google Chrome, then Edge, then bundled Chromium), 'chrome', 'chromium', or 'msedge'.
    When `proxy_url` is None/'direct', passes `args=['--no-proxy-server']` so Chromium never
    unintentionally inherits a broken OS-level or environment proxy.
    """
    pw_proxy, launch_args, _ = build_playwright_proxy_settings(
        proxy_url=proxy_url,
        verify_reachable=verify_proxy_reachable,
        target_url=getattr(data, "requested_url", None),
    )
    order = {"auto": ["chrome", "msedge", None], "chromium": [None]}.get(channel, [channel])
    last = None
    for ch in order:
        try:
            kwargs = {
                "headless": headless,
                "args": launch_args,
            }
            if ch:
                kwargs["channel"] = ch
            if pw_proxy:
                kwargs["proxy"] = pw_proxy
            else:
                kwargs["env"] = _clean_env_for_direct_connection()
            b = p.chromium.launch(**kwargs)
            data.browser_used = ch or "chromium (bundled)"
            log.info(
                "Browser launched: %s (connection=%s)",
                data.browser_used,
                f"proxy:{pw_proxy['server']}" if pw_proxy else "direct (--no-proxy-server)",
            )
            return b
        except PlaywrightError as e:
            last = e
            log.info("Browser '%s' unavailable, trying next option.", ch or "chromium")
    raise ScraperError(f"No usable browser found (tried {order}). Run 'python -m playwright install chromium'. Last error: {last}")


def _launch_persistent(
    p,
    user_data_dir: Path,
    headless: bool,
    channel: str,
    data: ProfileData,
    timeout_ms: int,
    proxy_url: Optional[str] = None,
    verify_proxy_reachable: bool = True,
):
    """
    Launches a persistent browser context using a dedicated isolated user-data directory.
    channel: 'auto' (Google Chrome, then Edge, then bundled Chromium), 'chrome', 'chromium', or 'msedge'.
    When `proxy_url` is None/'direct', passes `args=['--no-proxy-server']` so the persistent
    Chrome profile never unintentionally inherits a broken OS-level or environment proxy.
    """
    validate_user_data_dir(user_data_dir)
    pw_proxy, launch_args, _ = build_playwright_proxy_settings(
        proxy_url=proxy_url,
        verify_reachable=verify_proxy_reachable,
        target_url=getattr(data, "requested_url", None),
    )
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
                "args": launch_args,
            }
            if ch:
                kwargs["channel"] = ch
            if pw_proxy:
                kwargs["proxy"] = pw_proxy
            else:
                kwargs["env"] = _clean_env_for_direct_connection()
            ctx = p.chromium.launch_persistent_context(**kwargs)
            data.browser_used = (ch or "chromium (bundled)") + " (persistent)"
            log.info(
                "Persistent browser launched: %s (profile: %s, connection=%s)",
                data.browser_used,
                user_data_dir,
                f"proxy:{pw_proxy['server']}" if pw_proxy else "direct (--no-proxy-server)",
            )
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
  if (document.querySelector('#gsc_oci_title, #gsc_vcd_title')) return 'detail';
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
                "when the target Google Scholar page appears."),
    "unusual_traffic": ("GOOGLE 'UNUSUAL TRAFFIC' / HTTP 429 BLOCK PAGE (no interactive CAPTCHA).",
                        "ACTION: there is nothing to solve or click. Do NOT refresh. Leave the window open and wait; "
                        "Google lifts this block by itself after some time (minutes to hours), or try a different "
                        "network. The scraper only watches the page and resumes if the target page appears."),
    "unknown": ("UNRECOGNISED PAGE (not the profile/detail page, no CAPTCHA widget, no known block text).",
                "ACTION: look at the window. Nothing is automated; the scraper keeps waiting passively."),
    "profile": ("RESEARCHER PROFILE visible.", "Resuming; the user ID will be verified before extraction."),
    "detail": ("PUBLICATION DETAIL PAGE visible.", "Resuming publication detail extraction."),
}


def _await_manual_access(
    page,
    data: ProfileData,
    reason: str,
    wait_s: float,
    expected_state: str = "profile",
    progress_callback=None,
    cancel_event=None,
    stats: Optional[DiagnosticNetworkStats] = None,
):
    """
    MANUAL MODE ONLY. Read-only wait: the page is NEVER reloaded, retried, clicked or
    filled in. Every second the DOM is classified as profile / detail / interactive CAPTCHA /
    unusual-traffic block; the user is told what the page really is and only asked to
    act when an interactive CAPTCHA is present. Returns when expected_state ('profile' or 'detail') appears.
    """
    bar = "!" * 78
    log.warning("Paused in manual mode (%s). Browser stays open; no refresh/retry will be made.", reason)
    deadline, last_state, started = time.time() + wait_s, None, time.time()
    while time.time() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            if progress_callback:
                progress_callback({"phase": "manual_verification_cancelled", "status": "Manual verification cancelled",
                                   "message": "Manual verification was cancelled. No blocked page was retried."})
            raise ManualVerificationCancelled("Manual verification was cancelled by the user.")
        try:
            if page.is_closed():
                if progress_callback:
                    progress_callback({"phase": "manual_verification_cancelled", "status": "Manual verification cancelled",
                                       "message": "The browser window was closed during manual verification."})
                raise ManualVerificationCancelled("Browser window was closed during manual verification.")
            state = page.evaluate(JS_PAGE_STATE)
        except PlaywrightError:
            time.sleep(1.0)            # page mid-navigation; keep waiting
            continue
        if state != last_state:
            title, action = STATE_MESSAGES.get(state, STATE_MESSAGES["unknown"])
            print(f"\n{bar}\n[{time.strftime('%H:%M:%S')}] PAGE STATE: {title}\n{action}\n"
                  f"(Waiting up to {int(wait_s)} s in total. Nothing is reloaded or retried.)\n{bar}\n", flush=True)
            log.info("Page state: %s", state)
            if progress_callback:
                phase = {
                    "captcha": "manual_verification_required",
                    "unusual_traffic": "manual_rate_limited",
                    "detail": "manual_verification_waiting",
                    "profile": "manual_verification_waiting",
                }.get(state, "manual_verification_waiting")
                progress_callback({"phase": phase, "manual_page_state": state, "status": title,
                                   "message": f"{title} {action}"})
            last_state = state
        if state == expected_state:
            latest_status = stats.last_main_frame_status if stats else None
            if latest_status in (403, 429) or (page.url and "/sorry/" in page.url):
                raise ManualVerificationUnsuccessful(
                    f"The {expected_state} page appeared, but Google still reports restricted access "
                    f"(HTTP {latest_status or 'unknown'}).",
                    http_status=latest_status,
                )
            data.warnings.append(f"Access was initially blocked ({reason}); the {expected_state} page became accessible "
                                 f"after {int(time.time() - started)} s of passive waiting.")
            if progress_callback:
                progress_callback({
                    "phase": "manual_verification_succeeded",
                    "manual_page_state": state,
                    "status": "Manual verification succeeded",
                    "message": f"Google restored access to the {expected_state} page. Scraping is resuming.",
                })
            return {"state": state, "http_status": latest_status, "url": page.url}
        time.sleep(1.0)
    if progress_callback:
        progress_callback({"phase": "manual_verification_unsuccessful", "status": "Manual verification unsuccessful",
                           "message": f"Access was not restored within {int(wait_s)} seconds."})
    raise ManualVerificationUnsuccessful(
        f"Target {expected_state} page still not accessible after {int(wait_s)} s of passive manual waiting. "
        f"Last page state: {last_state} ({reason}).",
        http_status=stats.last_main_frame_status if stats else None,
    )


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
                   user_data_dir: Optional[Path] = None,
                   proxy_url: Optional[str] = None,
                   verify_proxy_reachable: bool = True,
                   progress_callback=None,
                   manual_cancel_event=None) -> ProfileData:
    data = ProfileData(requested_url=url)
    try:
        return _scrape(data, url, headless, delay, timeout_s, max_show_more, channel, manual, manual_wait_s,
                       persistent=persistent, user_data_dir=user_data_dir,
                       proxy_url=proxy_url, verify_proxy_reachable=verify_proxy_reachable,
                       progress_callback=progress_callback, manual_cancel_event=manual_cancel_event)
    except ScraperError as e:
        e.profile_data = data          # let the caller report status / landing URL
        raise


def _expand_publications(
    page,
    data: ProfileData,
    stats: Optional[DiagnosticNetworkStats] = None,
    delay: float = 3.0,
    timeout_ms: int = 45000,
    max_show_more: int = 100,
    progress_callback=None,
):
    """
    Dynamically expand the publication list on the profile page by clicking
    Google Scholar's in-page 'Show more' (#gsc_bpf_more) button until all
    publications are loaded, the button is disabled/hidden, or no new rows appear.

    Important DOM detail:
    Google Scholar immediately sets #gsc_bpf_more.disabled = true while the XHR
    for the next batch of publications is in flight, and re-enables it only after
    the XHR response appends the new rows to #gsc_a_b. Therefore, we wait for
    the row count in #gsc_a_b to increase (and the loading spinner #gsc_a_sp to
    settle) before re-checking button state on the next iteration.
    """
    rows = page.locator(ROW_SELECTOR)
    btn = page.locator(SHOW_MORE)
    initial_count = rows.count()
    data.initial_row_count = initial_count
    log.info("Initial publication rows: %d", initial_count)
    if progress_callback:
        progress_callback({"phase": "step1_expanding", "publications_found": initial_count, "show_more_clicks": 0})

    while True:
        _check_blocked(page, stats=stats)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

        if btn.count() == 0:
            if data.show_more_clicks > 0:
                data.listing_complete = True
                data.stop_reason = "SHOW MORE button disappeared"
                log.info("SHOW MORE button disappeared after %d click(s): full publication listing loaded.", data.show_more_clicks)
            else:
                data.stop_reason = "SHOW MORE button not found"
                data.warnings.append("SHOW MORE button not found; listing completeness unknown.")
            break

        is_vis = btn.is_visible() if hasattr(btn, "is_visible") else True
        if is_vis is False:
            data.listing_complete = True
            data.stop_reason = "SHOW MORE button hidden/disappeared"
            log.info("SHOW MORE button is hidden: full publication listing loaded.")
            break

        if btn.is_disabled():
            data.listing_complete = True
            data.stop_reason = "SHOW MORE button is disabled"
            log.info("SHOW MORE is disabled: full publication listing loaded.")
            break

        if data.show_more_clicks >= max_show_more:
            data.stop_reason = f"Reached max_show_more={max_show_more}"
            data.warnings.append(f"Stopped after max_show_more={max_show_more} clicks; listing may be incomplete.")
            break

        before = rows.count()
        _polite_pause(delay)
        btn.scroll_into_view_if_needed()
        btn.click()
        data.show_more_clicks += 1

        try:
            # Wait for newly loaded publication rows to be appended to #gsc_a_b
            page.wait_for_function(
                """(n) => document.querySelectorAll('#gsc_a_b tr.gsc_a_tr').length > n""",
                arg=before,
                timeout=timeout_ms,
            )
            # Wait briefly for Google Scholar's XHR callback to finish updating
            # the loading spinner (#gsc_a_sp) and #gsc_bpf_more.disabled state
            try:
                page.wait_for_function(
                    """() => {
                        const sp = document.querySelector('#gsc_a_sp');
                        const spHidden = !sp || sp.offsetParent === null ||
                            window.getComputedStyle(sp).display === 'none' ||
                            window.getComputedStyle(sp).visibility === 'hidden';
                        const b = document.querySelector('#gsc_bpf_more');
                        return spHidden && (!b || !b.classList.contains('gs_btn_act'));
                    }""",
                    timeout=5000,
                )
            except PlaywrightTimeout:
                pass
        except PlaywrightTimeout:
            _check_blocked(page, stats=stats)
            after_timeout = rows.count()
            if after_timeout <= before:
                btn_gone_or_disabled = (
                    btn.count() == 0
                    or (hasattr(btn, "is_visible") and btn.is_visible() is False)
                    or btn.is_disabled()
                )
                data.stop_reason = f"SHOW MORE click #{data.show_more_clicks} loaded no new rows within timeout"
                data.warnings.append(
                    f"SHOW MORE click #{data.show_more_clicks} loaded no new rows within timeout; listing may be incomplete."
                )
                if btn_gone_or_disabled and after_timeout > 0:
                    log.info(
                        "SHOW MORE click #%d loaded 0 new rows and button is now disabled/hidden; stopping safely.",
                        data.show_more_clicks,
                    )
                break

        _check_blocked(page, stats=stats)
        after = rows.count()
        if after <= before:
            data.stop_reason = f"SHOW MORE click #{data.show_more_clicks} did not increase row count ({before} -> {after})"
            data.warnings.append(
                f"SHOW MORE click #{data.show_more_clicks} loaded no new rows ({before} -> {after}); stopped safely."
            )
            log.info("SHOW MORE click #%d: %d -> %d rows (no new rows; stopping safely)", data.show_more_clicks, before, after)
            break

        data.pagination_steps.append(
            {"click": data.show_more_clicks, "before": before, "after": after}
        )
        log.info("SHOW MORE click #%d: %d -> %d rows", data.show_more_clicks, before, after)
        if progress_callback:
            progress_callback({"phase": "step1_expanding", "publications_found": after, "show_more_clicks": data.show_more_clicks})


def canonicalize_detail_url(url: Optional[str]) -> str:
    """
    Canonicalize a Google Scholar publication detail URL by preserving only
    the identifying query parameters (`view_op=view_citation`, `user`, `citation_for_view`)
    and stripping pagination offsets (`cstart`, `pagesize`, `sortby`).
    """
    if not url:
        return ""
    try:
        p = urlparse(url.strip())
        if "scholar.google." not in (p.netloc or "") or not p.path.startswith("/citations"):
            return url.strip()
        qs = parse_qs(p.query)
        view_op = qs.get("view_op", [None])[0]
        cit_key = qs.get("citation_for_view", [None])[0]
        user = qs.get("user", [None])[0]
        if view_op == "view_citation" and cit_key:
            params = [("view_op", "view_citation"), ("hl", "en")]
            if user:
                params.append(("user", user))
            params.append(("citation_for_view", cit_key))
            return urlunsplit((p.scheme or "https", p.netloc, p.path, urlencode(params, safe=":"), ""))
        return url.strip()
    except Exception:
        return (url or "").strip()


def is_valid_detail_url(url: Optional[str]) -> bool:
    """
    Verify that `url` is strictly a Google Scholar individual publication detail URL
    (`view_op=view_citation` with `citation_for_view=...`), and NOT a 'Cited by' link,
    'Related articles' link, author profile link, or external publisher URL.
    """
    if not url:
        return False
    try:
        p = urlparse(url.strip())
        if "scholar.google." not in (p.netloc or "") or not p.path.startswith("/citations"):
            return False
        qs = parse_qs(p.query)
        return (
            qs.get("view_op", [None])[0] == "view_citation"
            and bool(qs.get("citation_for_view", [None])[0])
            and "cites" not in qs
        )
    except Exception:
        return False


def parse_detail_dom_payload(raw_dom: dict, detail_url: str, timestamp: str = "") -> dict:
    """
    Parse the raw JS_PUBLICATION_DETAIL dictionary extracted from a Google Scholar
    publication detail page into structured metadata and exact yearly citation counts.
    Never guesses or fabricates missing fields; never estimates yearly counts from bar heights.
    """
    if not isinstance(raw_dom, dict):
        raw_dom = {}
    fields = raw_dom.get("fields") or {}
    if not isinstance(fields, dict):
        fields = {}

    def _f(*keys: str) -> str:
        for k in keys:
            v = fields.get(k.lower())
            if v is not None and str(v).strip():
                return str(v).strip()
        return ""

    title = (raw_dom.get("title") or "").strip()
    authors_text = _f("authors", "inventors")
    pub_date = _f("publication date")
    pub_year = ""
    if pub_date:
        import re as _re
        ym = _re.search(r"\b(\d{4})\b", pub_date)
        if ym:
            pub_year = ym.group(1)

    journal = _f("journal", "conference", "book", "source", "institution", "patent office")
    volume = _f("volume")
    issue = _f("issue")
    pages = _f("pages")
    publisher = _f("publisher")
    description = _f("description")
    cited_by = str(raw_dom.get("cited_by_count") or "").strip()
    if cited_by and not cited_by.isdigit():
        cited_by = ""

    # Check for explicit DOI in fields, external_url, description, or title
    import re as _re
    doi_re = _re.compile(r"\b10\.\d{4,9}/[^\s\"<>;&,]+", _re.I)
    doi = _f("doi", "digital object identifier")
    if doi:
        m = doi_re.search(doi)
        doi = m.group(0).rstrip(".)];") if m else doi
    if not doi:
        ext_url = (raw_dom.get("external_url") or "").strip()
        for candidate in (ext_url, description, journal, title):
            if candidate:
                m = doi_re.search(candidate)
                if m:
                    doi = m.group(0).rstrip(".)];")
                    break

    # Extract exact year-wise citations ONLY when both year and exact integer count are present
    yearly_citations: list[dict] = []
    graph_bars = raw_dom.get("graph_bars") or []
    graph_years = raw_dom.get("graph_years") or []
    seen_years: set[str] = set()

    for idx, bar in enumerate(graph_bars):
        if not isinstance(bar, dict):
            continue
        cnt_str = str(bar.get("count") or "").strip().replace(",", "")
        if not cnt_str.isdigit():
            continue
        yr_str = str(bar.get("year_from_href") or "").strip()
        if (not yr_str or not yr_str.isdigit() or len(yr_str) != 4) and len(graph_years) == len(graph_bars):
            y_cand = str(graph_years[idx].get("year") or "").strip()
            if y_cand.isdigit() and len(y_cand) == 4:
                yr_str = y_cand
        if not yr_str or not yr_str.isdigit() or len(yr_str) != 4:
            # Match by identical CSS left px coordinate if available
            b_left = bar.get("left")
            if b_left is not None:
                for gy in graph_years:
                    if gy.get("left") is not None and abs(float(gy["left"]) - float(b_left)) <= 4.0:
                        y_cand = str(gy.get("year") or "").strip()
                        if y_cand.isdigit() and len(y_cand) == 4:
                            yr_str = y_cand
                            break
        if yr_str and yr_str.isdigit() and len(yr_str) == 4 and yr_str not in seen_years:
            seen_years.add(yr_str)
            yearly_citations.append(
                {
                    "citation_year": yr_str,
                    "citation_count": cnt_str,
                }
            )

    yearly_citations.sort(key=lambda x: int(x["citation_year"]))

    publisher_url = (raw_dom.get("external_url") or "").strip()

    return {
        "title": title,
        "authors_text": authors_text,
        "publication_date": pub_date,
        "publication_year": pub_year,
        "journal": journal,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "publisher": publisher,
        "description": description,
        "citation_count": cited_by,
        "publication_url": detail_url,
        "publisher_url": publisher_url,
        "external_url": publisher_url,
        "doi": doi,
        "yearly_citations": yearly_citations,
        "raw_fields": fields,
        "last_synced_at": timestamp,
    }


class UnresolvedPublicationError(ScraperError):
    """The requested citation cannot be identified without guessing."""


def citation_identifier(url: str) -> str:
    """Decode the opaque, case-sensitive citation key; never match by title/index."""
    return parse_qs(urlparse(url).query).get("citation_for_view", [""])[0].strip()


class ProfileDetailNavigator:
    """Open details through the profile UI, retaining its expanded listing."""

    def __init__(self, page, profile_url, stats=None, timeout_ms=45000,
                 interaction_delay=0.5, post_click_delay=1.0, return_delay=1.0,
                 emit=None, manual=False, manual_wait_s=600, manual_callback=None, cancel_event=None):
        parse_google_scholar_id(profile_url)
        self.page, self.profile_url, self.stats = page, profile_url, stats
        self.timeout_ms = timeout_ms
        self.interaction_delay = max(0.5, interaction_delay)
        self.post_click_delay = max(1.0, post_click_delay)
        self.return_delay = max(1.0, return_delay)
        self.emit = emit or (lambda message: None)
        self.ready = False
        self.detail_page = page
        self.manual, self.manual_wait_s = manual, manual_wait_s
        self.manual_callback, self.cancel_event = manual_callback, cancel_event

    def prepare(self):
        if self.ready:
            return
        self.emit("Opening scholar profile")
        time.sleep(self.interaction_delay)
        response = self.page.goto(self.profile_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        try:
            _check_blocked(self.page, status=response.status if response else None, stats=self.stats)
        except AccessBlockedError as blocked:
            if not self.manual or self.page.evaluate(JS_PAGE_STATE) != "captcha":
                raise
            _await_manual_access(self.page, ProfileData(requested_url=self.profile_url), str(blocked),
                                 self.manual_wait_s, expected_state="profile",
                                 progress_callback=self.manual_callback, cancel_event=self.cancel_event,
                                 stats=self.stats)
        self.page.wait_for_selector("#gsc_prf_in", timeout=self.timeout_ms)
        self.page.wait_for_selector("#gsc_a_b", state="attached", timeout=self.timeout_ms)
        data = ProfileData(requested_url=self.profile_url, google_scholar_id=parse_google_scholar_id(self.profile_url))
        _verify_profile_owner(self.page, data, self.stats)
        self.emit("Loading publications / Expanding SHOW MORE")
        _expand_publications(self.page, data, self.stats, delay=self.interaction_delay,
                             timeout_ms=self.timeout_ms)
        self.ready = True

    def open(self, detail_url):
        self.prepare()
        _check_blocked(self.page, stats=self.stats)
        self.emit("Matching publication by citation identifier")
        key = citation_identifier(detail_url)
        links = self.page.locator("#gsc_a_b a.gsc_a_at")
        matches = []
        for i in range(links.count()):
            link = links.nth(i)
            href = urljoin(self.profile_url, link.get_attribute("href") or "")
            if key and is_valid_detail_url(href) and citation_identifier(href) == key:
                matches.append(link)
        if len(matches) != 1:
            raise UnresolvedPublicationError(f"UNRESOLVED: expected exactly one profile link for citation {key!r}; found {len(matches)}")
        popup = []
        def on_popup(page):
            popup.append(page)
            if self.stats:
                setup_diagnostic_listeners(page, self.stats, log)
        self.page.on("popup", on_popup)
        try:
            time.sleep(self.interaction_delay)
            self.emit("Clicking publication title")
            matches[0].click(timeout=self.timeout_ms)
            self.emit("Waiting for detail content")
            self.page.wait_for_timeout(self.post_click_delay * 1000)
            self.detail_page = popup[0] if popup else self.page
            self.detail_page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
            _check_blocked(self.detail_page, stats=self.stats)
            self.detail_page.wait_for_selector("#gsc_oci_title, #gsc_vcd_title", timeout=self.timeout_ms)
            self.verify(detail_url)
            return self.detail_page
        except PlaywrightTimeout:
            _check_blocked(self.detail_page, stats=self.stats)
            raise
        finally:
            self.page.remove_listener("popup", on_popup)

    def verify(self, detail_url):
        expected = citation_identifier(detail_url)
        # Dialogs can leave the address bar on the profile. Their form records the
        # citation key; a different address-bar key must never be accepted.
        actual = citation_identifier(self.detail_page.url)
        if not actual:
            actual = self.detail_page.locator(
                "#gsc_oci input[name='citation_for_view'], #gsc_vcd input[name='citation_for_view']"
            ).evaluate_all("els => els.length === 1 ? els[0].value : ''")
        if actual != expected:
            raise UnresolvedPublicationError("UNRESOLVED: opened detail citation identity could not be verified")

    def return_to_profile(self):
        self.emit("Returning to profile")
        _check_blocked(self.detail_page, stats=self.stats)
        time.sleep(self.interaction_delay)
        if self.detail_page is not self.page:
            self.detail_page.close()
        else:
            close = self.page.locator("#gs_md_cita-d-x, #gsc_oci_close, #gsc_vcd_close")
            visible = [close.nth(i) for i in range(close.count()) if close.nth(i).is_visible()]
            if visible:
                visible[0].click(timeout=self.timeout_ms)
                self.page.locator("#gsc_oci_title, #gsc_vcd_title").wait_for(state="hidden", timeout=self.timeout_ms)
            else:
                self.page.go_back(wait_until="domcontentloaded", timeout=self.timeout_ms)
        _check_blocked(self.page, stats=self.stats)
        self.page.wait_for_selector("#gsc_prf_in", timeout=self.timeout_ms)
        self.page.wait_for_selector("#gsc_a_b", state="attached", timeout=self.timeout_ms)
        time.sleep(self.return_delay)
        # A document navigation may have discarded expanded rows. Expanding an
        # already complete retained listing performs no additional requests.
        _expand_publications(self.page, ProfileData(requested_url=self.profile_url), self.stats,
                             delay=self.interaction_delay, timeout_ms=self.timeout_ms)


def scrape_publication_detail_on_page(
    page,
    detail_url: str,
    stats: Optional[DiagnosticNetworkStats] = None,
    timeout_ms: int = 45000,
    timestamp: str = "",
    manual: bool = False,
    manual_wait_s: float = 600.0,
    proxy_url: Optional[str] = None,
    manual_status_callback=None,
    manual_cancel_event=None,
    navigate: bool = True,
) -> dict:
    """
    Standalone compatibility helper: optionally navigate to a validated detail URL.
    Normal Step 2 passes navigate=False after a verified profile-link click.
    (`view_op=view_citation`), verify no HTTP 429 or CAPTCHA block occurred,
    and extract the complete detail DOM metadata without clicking any outbound
    links ('Cited by', 'Related articles', author links, or publisher links).
    """
    if not is_valid_detail_url(detail_url):
        raise InvalidProfileUrlError(
            f"Refusing to open non-detail URL in Stage 2 detail scraper: {redact_url(detail_url)}"
        )

    if stats and navigate:
        stats.has_429 = False
        stats.sorry_redirect = False

    try:
        resp = page.goto(detail_url, wait_until="domcontentloaded", timeout=timeout_ms) if navigate else None
    except PlaywrightTimeout as e:
        _check_blocked(page, stats=stats)
        raise classify_navigation_error(
            e, detail_url, proxy_url=proxy_url, context_label="publication detail"
        ) from e
    except PlaywrightError as e:
        _check_blocked(page, stats=stats)
        raise classify_navigation_error(
            e, detail_url, proxy_url=proxy_url, context_label="publication detail"
        ) from e

    # If Google Scholar performs an intermediate /sgsync handshake redirect, wait briefly for it to settle
    if page.url and "sgsync" in page.url and "/sorry/" not in page.url:
        try:
            page.wait_for_function(
                """() => !location.href.includes('sgsync') || !!document.querySelector('#gsc_oci_title, #gsc_vcd_title') || location.href.includes('/sorry/')""",
                timeout=10000,
            )
        except PlaywrightError:
            pass

    status = (stats.last_main_frame_status if stats and stats.last_main_frame_status else (resp.status if resp else None))
    try:
        _check_blocked(page, status=status, stats=stats)
    except AccessBlockedError as blk:
        if not manual:
            raise
        try:
            manual_state = page.evaluate(JS_PAGE_STATE)
        except PlaywrightError:
            manual_state = "unknown"
        if manual_state != "captcha":
            if manual_status_callback:
                manual_status_callback({
                    "phase": "manual_rate_limited",
                    "manual_page_state": manual_state,
                    "status": "HTTP 429 / access restriction — no interactive verification available",
                    "message": "Google restricted this request, but no interactive CAPTCHA is available. "
                               "The scraper stopped without refreshing or retrying the blocked endpoint.",
                })
            raise AccessBlockedError(
                f"{blk} No interactive CAPTCHA is available for manual verification.",
                block_kind="http_429_no_challenge" if getattr(blk, "http_status", None) == 429
                else "sorry_page_no_challenge",
                http_status=getattr(blk, "http_status", None),
            ) from blk
        dummy_data = ProfileData(requested_url=detail_url)
        restored = _await_manual_access(
            page, dummy_data, str(blk), manual_wait_s, expected_state="detail",
            progress_callback=manual_status_callback, cancel_event=manual_cancel_event, stats=stats,
        )
        status = restored.get("http_status")
        if status is None:
            status = stats.last_main_frame_status if stats else None
        if status is None:
            raise ManualVerificationUnsuccessful(
                "The detail page appeared after verification, but no successful navigation response was observed."
            )
    if status == 404:
        raise ScraperError(f"Publication detail page returned HTTP 404: {redact_url(detail_url)}")

    try:
        page.wait_for_selector("#gsc_oci_title, #gsc_vcd_title", timeout=timeout_ms)
    except PlaywrightTimeout as e:
        _check_blocked(page, status=status, stats=stats)
        raise ScraperError(
            f"Publication detail title (#gsc_oci_title) did not appear on {redact_url(page.url)}"
        ) from e

    _check_blocked(page, status=status, stats=stats)
    raw_dom = page.evaluate(JS_PUBLICATION_DETAIL)
    parsed = parse_detail_dom_payload(raw_dom, detail_url=canonicalize_detail_url(detail_url), timestamp=timestamp)
    parsed["requested_detail_url"] = detail_url
    parsed["canonical_detail_url"] = canonicalize_detail_url(detail_url)
    parsed["http_status"] = status
    return parsed


def scrape_publications_details_batch(
    publications_queue: list[dict],
    snapshot_path: Path,
    headless: bool = True,
    delay: float = 4.0,
    timeout_s: float = 45.0,
    channel: str = "auto",
    persistent: bool = False,
    user_data_dir: Optional[Path] = None,
    proxy_url: Optional[str] = None,
    verify_proxy_reachable: bool = True,
    resume: bool = True,
    max_details: Optional[int] = None,
    manual: bool = False,
    manual_wait_s: float = 600.0,
    page_override=None,
    progress_callback=None,
    verified_complete_ids: Optional[set[str]] = None,
    manual_cancel_event=None,
    profile_url: Optional[str] = None,
    interaction_delay: float = 0.5,
    post_click_delay: float = 1.0,
    return_delay: float = 1.0,
) -> dict:
    """
    Stage 2: Sequential profile-link clicks with checkpoint/resume support.

    Parameters:
      publications_queue: list of dicts with keys:
        - publication_id (str)
        - scholar_id (str)
        - title (str)
        - source_url (str)
      snapshot_path: path to `output/supplementary/publication_details_snapshot.json`
      resume: if True, loads existing snapshot and skips publications already marked
              with status == 'SUCCESS' (keyed by stable publication_id and canonical_url).
      page_override: optional mock/existing Playwright page for offline testing.

    Stops immediately on AccessBlockedError (HTTP 429 / CAPTCHA / /sorry/ redirect),
    saving checkpoint state atomically after every publication so completed work is preserved.
    """
    timeout_ms = int(timeout_s * 1000)
    if manual:
        headless = False
    queue_ids = {str(item.get("publication_id") or "").strip() for item in publications_queue}
    existing_records: dict[str, dict] = {}
    by_canonical_url: dict[str, str] = {}

    if resume and snapshot_path and snapshot_path.exists():
        try:
            with open(snapshot_path, "r", encoding="utf-8") as f:
                snap_data = json.load(f) if "json" in globals() else __import__("json").load(f)
            for pid, rec in (snap_data.get("publications") or {}).items():
                if isinstance(rec, dict):
                    existing_records[str(pid)] = rec
                    c_url = rec.get("canonical_detail_url") or canonicalize_detail_url(rec.get("publication_url"))
                    if c_url and rec.get("status") == "SUCCESS":
                        by_canonical_url[c_url] = str(pid)
        except Exception as e:
            log.warning("Could not load existing detail checkpoint %s: %s", snapshot_path, e)

    summary = {
        "total_unique_publications": len(publications_queue),
        "manual_mode": bool(manual),
        "browser_visible": not bool(headless),
        "persistent_profile": bool(persistent),
        "connection_mode": "proxy" if proxy_url else "direct",
        "attempted_this_run": 0,
        "succeeded_total": 0,
        "succeeded_this_run": 0,
        "skipped_checkpoint": 0,
        "skipped_duplicate_url": 0,
        "failed_this_run": 0,
        "remaining_incomplete": 0,
        "stopped_by_block": False,
        "block_reason": None,
        "duplicate_detail_urls": [],
        "block_kind": None,
        "manual_verification_outcome": None,
    }

    def _save_checkpoint():
        if not snapshot_path:
            return
        import json as _json
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = snapshot_path.with_suffix(snapshot_path.suffix + ".tmp")
        succ_count = sum(pid in queue_ids and r.get("status") == "SUCCESS" for pid, r in existing_records.items())
        summary["succeeded_total"] = succ_count
        summary["remaining_incomplete"] = max(0, len(publications_queue) - succ_count)
        payload = {
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "summary": summary,
            "publications": existing_records,
        }
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, snapshot_path)

    def _emit_detail_progress(phase: str, current_title: str = "", current_pid: str = "", status: str = ""):
        if not progress_callback:
            return
        total = len(publications_queue)
        succ_count = sum(pid in queue_ids and r.get("status") == "SUCCESS" for pid, r in existing_records.items())
        processed_count = (
            summary["skipped_checkpoint"]
            + summary["skipped_duplicate_url"]
            + summary["attempted_this_run"]
        )
        pending_count = max(0, total - processed_count)
        progress_callback({
            "phase": phase,
            "total_publications": total,
            "publications_processed": processed_count,
            "details_scraped": succ_count,
            "pending_publications": pending_count,
            "current_publication_id": current_pid,
            "current_publication_title": current_title,
            "status": status,
            "summary": dict(summary),
        })

    def _run_loop(page, stats: Optional[DiagnosticNetworkStats]):
        navigator = None
        seen_urls_in_queue: dict[str, str] = dict(by_canonical_url)
        for item in publications_queue:
            pid = str(item.get("publication_id") or "").strip()
            sid = str(item.get("scholar_id") or "").strip()
            pub_title = (item.get("title") or "").strip()
            raw_url = (item.get("source_url") or "").strip()
            canon_url = canonicalize_detail_url(raw_url)
            now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if not pid:
                continue

            # 1. Check checkpoint by publication_id
            prev = existing_records.get(pid)
            if (resume and prev and prev.get("status") == "SUCCESS"
                    and (verified_complete_ids is None or pid in verified_complete_ids)
                    and canonicalize_detail_url(prev.get("publication_url")) == canon_url):
                summary["skipped_checkpoint"] += 1
                if canon_url:
                    seen_urls_in_queue.setdefault(canon_url, pid)
                _emit_detail_progress("step2_progress", current_title=pub_title, current_pid=pid, status="Skipped (checkpoint)")
                continue

            # 2. Check missing or invalid detail URL
            if not raw_url or not is_valid_detail_url(raw_url):
                existing_records[pid] = {
                    "publication_id": pid,
                    "scholar_id": sid,
                    "publication_url": raw_url,
                    "canonical_detail_url": canon_url,
                    "status": "FAILED",
                    "error": "Missing or invalid Google Scholar publication detail URL",
                    "extracted_at": now_ts,
                    "metadata": None,
                    "missing_fields": [],
                }
                summary["failed_this_run"] += 1
                _save_checkpoint()
                _emit_detail_progress("step2_progress", current_title=pub_title, current_pid=pid, status="Failed (invalid URL)")
                continue

            # 3. Check duplicate canonical detail URL across publications
            if canon_url in seen_urls_in_queue and seen_urls_in_queue[canon_url] != pid:
                other_pid = seen_urls_in_queue[canon_url]
                summary["skipped_duplicate_url"] += 1
                summary["duplicate_detail_urls"].append(
                    {
                        "publication_id": pid,
                        "duplicate_of_publication_id": other_pid,
                        "canonical_detail_url": canon_url,
                    }
                )
                existing_records[pid] = {
                    "publication_id": pid,
                    "scholar_id": sid,
                    "publication_url": raw_url,
                    "canonical_detail_url": canon_url,
                    "status": "SKIPPED_DUPLICATE_URL",
                    "duplicate_of_publication_id": other_pid,
                    "extracted_at": now_ts,
                    "metadata": None,
                    "missing_fields": [],
                }
                _save_checkpoint()
                _emit_detail_progress("step2_progress", current_title=pub_title, current_pid=pid, status="Skipped (duplicate URL)")
                continue

            if max_details is not None and summary["attempted_this_run"] >= max_details:
                log.info("Reached max_details=%d; stopping detail batch.", max_details)
                break

            if summary["attempted_this_run"] > 0 and delay > 0:
                _polite_pause(delay)

            summary["attempted_this_run"] += 1
            _emit_detail_progress("step2_opening", current_title=pub_title, current_pid=pid, status=f"Scraping detail page (#{pid})")
            log.info(
                "[Stage 2 Detail %d/%d] Opening publication_id=%s: %s",
                summary["attempted_this_run"],
                len(publications_queue),
                pid,
                redact_url(raw_url),
            )
            try:
                if navigator is None:
                    user = parse_qs(urlparse(raw_url).query).get("user", [""])[0]
                    target_profile = profile_url or ("https://scholar.google.com/citations?" + urlencode({"user": user, "hl": "en"}))
                    navigator = ProfileDetailNavigator(page, target_profile, stats, timeout_ms,
                        interaction_delay, post_click_delay, return_delay, manual=manual,
                        manual_wait_s=manual_wait_s, manual_callback=progress_callback,
                        cancel_event=manual_cancel_event)
                navigator.emit = lambda message: _emit_detail_progress(
                    "step2_navigation", current_title=pub_title, current_pid=pid, status=message)
                def _manual_status(ev: dict):
                    if not progress_callback:
                        return
                    payload = dict(ev)
                    payload.update({
                        "total_publications": len(publications_queue),
                        "publications_processed": summary["skipped_checkpoint"] + summary["attempted_this_run"],
                        "details_scraped": summary["succeeded_this_run"],
                        "pending_publications": max(0, len(publications_queue) - summary["succeeded_this_run"]),
                        "current_publication_id": pid,
                        "current_publication_title": pub_title,
                        "summary": dict(summary),
                    })
                    progress_callback(payload)

                try:
                    detail_page = navigator.open(raw_url)
                except AccessBlockedError:
                    # The existing extractor owns passive manual verification for
                    # a detail challenge. Profile restrictions stop immediately.
                    if not manual or not navigator.ready:
                        raise
                    detail_page = navigator.detail_page
                navigator.emit("Extracting metadata")
                parsed = scrape_publication_detail_on_page(
                    detail_page,
                    raw_url,
                    stats=stats,
                    timeout_ms=timeout_ms,
                    timestamp=now_ts,
                    manual=manual,
                    manual_wait_s=manual_wait_s,
                    proxy_url=proxy_url,
                    manual_status_callback=_manual_status,
                    manual_cancel_event=manual_cancel_event,
                    navigate=False,
                )
                navigator.verify(raw_url)
                if not parsed.get("title"):
                    raise ScraperError("Detail extraction returned no title")
                missing_fields = [
                    k
                    for k in (
                        "title",
                        "authors_text",
                        "publication_date",
                        "publication_year",
                        "journal",
                        "volume",
                        "issue",
                        "pages",
                        "publisher",
                        "description",
                        "citation_count",
                        "publisher_url",
                        "doi",
                    )
                    if not parsed.get(k)
                ]
                existing_records[pid] = {
                    "publication_id": pid,
                    "scholar_id": sid,
                    "publication_url": canonicalize_detail_url(parsed.get("publication_url") or raw_url) or raw_url,
                    "canonical_detail_url": canon_url,
                    "status": "SUCCESS",
                    "error": None,
                    "extracted_at": now_ts,
                    "metadata": parsed,
                    "missing_fields": missing_fields,
                }
                seen_urls_in_queue[canon_url] = pid
                summary["succeeded_this_run"] += 1
                log.info(
                    "[Stage 2 Detail] SUCCESS publication_id=%s | title=%r | date=%r | journal=%r | yearly_cites=%d",
                    pid,
                    (parsed.get("title") or "")[:50],
                    parsed.get("publication_date") or "",
                    (parsed.get("journal") or "")[:40],
                    len(parsed.get("yearly_citations") or []),
                )
                navigator.emit("Saving checkpoint")
                _save_checkpoint()
                _emit_detail_progress("step2_progress", current_title=parsed.get("title") or pub_title, current_pid=pid, status=f"Scraped detail page (#{pid})")
                try:
                    navigator.return_to_profile()
                except Exception as return_error:
                    # The successful record is durable even if returning fails.
                    summary["return_error"] = str(return_error)
                    if isinstance(return_error, AccessBlockedError):
                        summary["stopped_by_block"] = True
                        summary["block_reason"] = str(return_error)
                    _save_checkpoint()
                    _emit_detail_progress("step2_blocked" if summary["stopped_by_block"] else "step2_failed",
                                          pub_title, pid, f"Stopped returning to profile: {return_error}")
                    break
            except ManualVerificationCancelled as cancelled:
                err_str = str(cancelled)
                log.warning("[Stage 2 Detail] MANUAL VERIFICATION CANCELLED on publication_id=%s: %s", pid, err_str)
                existing_records[pid] = {
                    "publication_id": pid, "scholar_id": sid, "publication_url": raw_url,
                    "canonical_detail_url": canon_url, "status": "CANCELLED", "error": err_str,
                    "extracted_at": now_ts, "metadata": None, "missing_fields": [],
                }
                summary["failed_this_run"] += 1
                summary["manual_verification_outcome"] = "CANCELLED"
                _save_checkpoint()
                _emit_detail_progress("manual_verification_cancelled", current_title=pub_title, current_pid=pid,
                                      status="Manual verification cancelled")
                break
            except AccessBlockedError as blk:
                err_str = str(blk)
                log.error(
                    "[Stage 2 Detail] BLOCKED on publication_id=%s (%s). Stopping detail scraping immediately to respect rate limits.",
                    pid,
                    err_str,
                )
                existing_records[pid] = {
                    "publication_id": pid,
                    "scholar_id": sid,
                    "publication_url": raw_url,
                    "canonical_detail_url": canon_url,
                    "status": "BLOCKED",
                    "error": err_str,
                    "extracted_at": now_ts,
                    "metadata": None,
                    "missing_fields": [],
                }
                summary["failed_this_run"] += 1
                summary["stopped_by_block"] = True
                summary["block_reason"] = err_str
                summary["block_kind"] = getattr(blk, "block_kind", "blocked")
                if isinstance(blk, ManualVerificationUnsuccessful):
                    summary["manual_verification_outcome"] = "UNSUCCESSFUL"
                _save_checkpoint()
                phase = "manual_verification_unsuccessful" if isinstance(blk, ManualVerificationUnsuccessful) else "step2_blocked"
                _emit_detail_progress(phase, current_title=pub_title, current_pid=pid,
                                      status=("Manual verification unsuccessful" if isinstance(blk, ManualVerificationUnsuccessful)
                                              else f"Blocked ({summary['block_kind']}): {err_str}"))
                break
            except Exception as exc:  # noqa: BLE001
                err_str = str(exc)
                log.warning("[Stage 2 Detail] FAILED publication_id=%s: %s", pid, err_str)
                existing_records[pid] = {
                    "publication_id": pid,
                    "scholar_id": sid,
                    "publication_url": raw_url,
                    "canonical_detail_url": canon_url,
                    "status": "UNRESOLVED" if isinstance(exc, UnresolvedPublicationError) else "FAILED",
                    "error": err_str,
                    "extracted_at": now_ts,
                    "metadata": None,
                    "missing_fields": [],
                }
                summary["failed_this_run"] += 1
                _save_checkpoint()
                _emit_detail_progress("step2_progress", current_title=pub_title, current_pid=pid, status=f"Failed (#{pid}): {err_str}")
                if not isinstance(exc, UnresolvedPublicationError) or citation_identifier(page.url):
                    break

    if page_override is not None:
        _run_loop(page_override, stats=None)
    else:
        with sync_playwright() as p:
            stats = DiagnosticNetworkStats()
            dummy = ProfileData(requested_url="https://scholar.google.com/")
            browser = None
            context = None
            try:
                if persistent:
                    pdir = Path(user_data_dir or (Path.cwd() / ".chrome_profile"))
                    if proxy_url is not None or not verify_proxy_reachable:
                        context = _launch_persistent(
                            p, pdir, headless, channel, dummy, timeout_ms,
                            proxy_url=proxy_url, verify_proxy_reachable=verify_proxy_reachable
                        )
                    else:
                        context = _launch_persistent(p, pdir, headless, channel, dummy, timeout_ms)
                    page = context.pages[0] if context.pages else context.new_page()
                else:
                    if proxy_url is not None or not verify_proxy_reachable:
                        browser = _launch(
                            p, headless, channel, dummy,
                            proxy_url=proxy_url, verify_proxy_reachable=verify_proxy_reachable
                        )
                    else:
                        browser = _launch(p, headless, channel, dummy)
                    context = browser.new_context(locale="en-US", viewport={"width": 1280, "height": 900})
                    page = context.new_page()
                page.set_default_timeout(timeout_ms)
                setup_diagnostic_listeners(page, stats, log)
                _run_loop(page, stats)
            finally:
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

    _save_checkpoint()
    return {
        "summary": summary,
        "publications": existing_records,
    }


def _scrape(data, url, headless, delay, timeout_s, max_show_more, channel,
            manual=False, manual_wait_s=600.0,
            persistent=False, user_data_dir: Optional[Path] = None,
            proxy_url: Optional[str] = None,
            verify_proxy_reachable: bool = True,
            progress_callback=None, manual_cancel_event=None) -> ProfileData:
    data.google_scholar_id = parse_google_scholar_id(url)
    timeout_ms = int(timeout_s * 1000)

    # Pre-validate proxy configuration and initialize connection diagnostics before launching browser
    conn_diag = BrowserConnectionDiagnostics(target_url=url)
    try:
        _, _, conn_diag = build_playwright_proxy_settings(
            proxy_url=proxy_url,
            verify_reachable=verify_proxy_reachable,
            target_url=url,
        )
    except (ProxyConfigurationError, ProxyConnectionError) as exc:
        if isinstance(exc, ProxyConfigurationError):
            conn_diag.connection_mode = "proxy"
            conn_diag.proxy_configured = True
            conn_diag.proxy_server = str(proxy_url)
            conn_diag.proxy_status = f"Invalid proxy configuration: {exc}"
            conn_diag.failure_category = "proxy_config"
            conn_diag.navigation_failure_reason = str(exc)
        else:
            conn_diag.connection_mode = "proxy"
            conn_diag.proxy_configured = True
            conn_diag.proxy_server = getattr(exc, "proxy_server", str(proxy_url))
            conn_diag.proxy_status = f"Proxy connection failed: {exc.raw_error or exc}"
            conn_diag.failure_category = "proxy_connection"
            conn_diag.navigation_failure_reason = str(exc.raw_error or exc)
        data.connection_diagnostics = conn_diag.to_dict()
        log_connection_diagnostics(conn_diag, log)
        raise

    if progress_callback:
        progress_callback({
            "phase": "step1_opening",
            "google_scholar_id": data.google_scholar_id,
            "status": f"Opening Google Scholar profile ({data.google_scholar_id})...",
        })

    with sync_playwright() as p:
        stats = DiagnosticNetworkStats()
        browser = None
        context = None
        try:
            try:
                if persistent:
                    pdir = Path(user_data_dir or (Path.cwd() / ".chrome_profile"))
                    if proxy_url is not None or not verify_proxy_reachable:
                        context = _launch_persistent(
                            p, pdir, headless, channel, data, timeout_ms,
                            proxy_url=proxy_url, verify_proxy_reachable=False
                        )
                    else:
                        context = _launch_persistent(p, pdir, headless, channel, data, timeout_ms)
                    page = context.pages[0] if context.pages else context.new_page()
                else:
                    if proxy_url is not None or not verify_proxy_reachable:
                        browser = _launch(
                            p, headless, channel, data,
                            proxy_url=proxy_url, verify_proxy_reachable=False
                        )
                    else:
                        browser = _launch(p, headless, channel, data)
                    context = browser.new_context(locale="en-US", viewport={"width": 1280, "height": 900})
                    page = context.new_page()
            except ScraperError as launch_err:
                conn_diag.browser_launched = False
                conn_diag.failure_category = "browser_launch"
                conn_diag.navigation_failure_reason = str(launch_err)
                data.connection_diagnostics = conn_diag.to_dict()
                log_connection_diagnostics(conn_diag, log)
                raise

            conn_diag.browser_launched = True
            conn_diag.browser_name = data.browser_used

            page.set_default_timeout(timeout_ms)

            # Diagnostic request-frequency and network logging
            setup_diagnostic_listeners(page, stats, log)

            page.on("framenavigated",
                    lambda fr: data.navigations.append(fr.url) if fr == page.main_frame else None)

            log.info("Opening profile: %s", url)
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except PlaywrightTimeout as e:
                _check_blocked(page, stats=stats)
                classified = classify_navigation_error(
                    e, url, proxy_url=proxy_url, diagnostics=conn_diag, context_label="profile"
                )
                data.connection_diagnostics = conn_diag.to_dict()
                log_connection_diagnostics(conn_diag, log)
                raise classified from e
            except PlaywrightError as e:
                _check_blocked(page, stats=stats)
                classified = classify_navigation_error(
                    e, url, proxy_url=proxy_url, diagnostics=conn_diag, context_label="profile"
                )
                data.connection_diagnostics = conn_diag.to_dict()
                log_connection_diagnostics(conn_diag, log)
                raise classified from e

            conn_diag.network_connected = True
            data.http_status = resp.status if resp else None
            conn_diag.http_status = data.http_status
            data.loaded_url = page.url
            log.info("HTTP status: %s | landed on: %s", data.http_status, redact_url(page.url))

            try:
                _check_blocked(page, data.http_status, stats=stats)
            except AccessBlockedError as blk:
                if not manual:
                    conn_diag.scholar_accessible = False
                    conn_diag.failure_category = "blocked_429_captcha"
                    conn_diag.navigation_failure_reason = str(blk)
                    data.connection_diagnostics = conn_diag.to_dict()
                    log_connection_diagnostics(conn_diag, log)
                    raise
                try:
                    manual_state = page.evaluate(JS_PAGE_STATE)
                except PlaywrightError:
                    manual_state = "unknown"
                if manual_state != "captcha":
                    if progress_callback:
                        progress_callback({
                            "phase": "manual_rate_limited",
                            "status": "HTTP 429 / access restriction — no interactive verification available",
                            "message": "Google restricted the profile request without an interactive CAPTCHA. "
                                       "The scraper stopped without refreshing or retrying.",
                        })
                    raise AccessBlockedError(
                        f"{blk} No interactive CAPTCHA is available for manual verification.",
                        block_kind="http_429_no_challenge" if getattr(blk, "http_status", None) == 429
                        else "sorry_page_no_challenge",
                        http_status=getattr(blk, "http_status", None),
                    ) from blk
                _await_manual_access(
                    page, data, str(blk), manual_wait_s, progress_callback=progress_callback,
                    cancel_event=manual_cancel_event, stats=stats,
                )
                data.http_status = stats.last_main_frame_status
                _check_blocked(page, status=data.http_status, stats=stats)
            if data.http_status == 404:
                conn_diag.scholar_accessible = False
                conn_diag.failure_category = "http_404"
                conn_diag.navigation_failure_reason = "Profile not found (HTTP 404)"
                data.connection_diagnostics = conn_diag.to_dict()
                log_connection_diagnostics(conn_diag, log)
                raise ScraperError("Profile not found (HTTP 404). Check the 'user' id in the URL.")

            conn_diag.scholar_accessible = True
            data.connection_diagnostics = conn_diag.to_dict()
            log_connection_diagnostics(conn_diag, log)

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
            hdr_preview = page.evaluate(JS_HEADER)
            if progress_callback and hdr_preview.get("scholar_name"):
                progress_callback({
                    "phase": "step1_header_loaded",
                    "researcher_name": hdr_preview.get("scholar_name"),
                    "status": f"Loaded profile for {hdr_preview.get('scholar_name')}. Expanding publications...",
                })
            _expand_publications(
                page,
                data,
                stats=stats,
                delay=delay,
                timeout_ms=timeout_ms,
                max_show_more=max_show_more,
                progress_callback=progress_callback,
            )

            # ---- Extract everything from the live DOM -------------------------
            data.header = page.evaluate(JS_HEADER)
            data.metrics = page.evaluate(JS_METRICS)
            data.publications = page.evaluate(JS_PUBLICATIONS)
            footer = page.locator("#gsc_a_nn")
            data.footer_text = footer.inner_text().strip() if footer.count() else None
            log.info("Extracted %d publication rows; footer reads: %s", len(data.publications), data.footer_text)
            if progress_callback:
                progress_callback({
                    "phase": "step1_extracted",
                    "researcher_name": data.header.get("scholar_name") or "",
                    "publications_found": len(data.publications),
                    "status": f"Extracted {len(data.publications)} raw publication rows from profile.",
                })
        finally:
            data.network_stats = stats.to_dict()
            if data.connection_diagnostics is None:
                data.connection_diagnostics = conn_diag.to_dict()
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
