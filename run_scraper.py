"""
Command-line entry point.

Usage:
    python run_scraper.py --url "https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ"
    python run_scraper.py --url "<another profile URL>" --output output/other_scholar
"""

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from scholar_scraper.browser import (
     PROXY_ERROR_CODES,
    SUGGESTED_PROXY_ERROR_MESSAGE,
    AccessBlockedError,
    InvalidProfileUrlError,
    ManualVerificationCancelled,
    ManualVerificationUnsuccessful,
    NetworkConnectionError,
    ProfileData,
    ProxyConfigurationError,
    ProxyConnectionError,
    ScraperError,
    parse_google_scholar_id,
    scrape_profile,
    scrape_publications_details_batch,
    validate_proxy_config,
)
from scholar_scraper.docx_schema import validate_against_docx
from scholar_scraper.dataset_ops import completion_state, dataset_lock, recover_deletion, scoped_detail_path
from scholar_scraper.id_registry import IdRegistry, RegistryError
from scholar_scraper.reporting import (
    CsvPermissionError,
    assess,
    build_failure_report,
    build_quality_report,
    build_report,
    find_existing_scholar_by_google_id,
    load_existing_csvs,
    load_existing_detail_csvs,
    verify_csv_files_writable,
    write_all_csvs,
    write_supplementary,
)
from scholar_scraper.transform import Transformed, apply_publication_details, transform
from scholar_scraper.validator import DatasetValidationError

HERE = Path(__file__).resolve().parent
DEFAULT_DOCX = HERE / "Research_Companion_ERD_Listing_Exact_Names_V2.docx"


def parse_args():
    p = argparse.ArgumentParser(description="Scrape a Google Scholar profile front page into ERD-shaped CSVs.")
    p.add_argument("--url", default=os.environ.get("SCHOLAR_URL"),
                   help="Google Scholar profile URL (or set SCHOLAR_URL env var).")
    p.add_argument("--output", default=str(HERE / "output"), help="Output folder (default: ./output).")
    p.add_argument("--registry", default=None,
                   help="Path to persistent ID registry JSON (default: <output>/id_registry.json).")
    p.add_argument("--docx", default=str(DEFAULT_DOCX), help="ERD Data Dictionary DOCX used to verify headers.")
    p.add_argument("--from-snapshot", default=None,
                   help="Optional path to an existing raw_profile_snapshot.json to transform without browser navigation.")
    p.add_argument("--headed", action="store_true", help="Show the browser window.")
    p.add_argument("--delay", type=float, default=3.0, help="Base seconds to wait between SHOW MORE clicks.")
    p.add_argument("--timeout", type=float, default=45.0, help="Page/element timeout in seconds.")
    p.add_argument("--max-show-more", type=int, default=100, help="Safety cap on SHOW MORE clicks.")
    p.add_argument("--browser-channel", default="auto", choices=["auto", "chrome", "msedge", "chromium"],
                   help="Browser channel: Google Chrome, Edge, or bundled Chromium (auto = Chrome, then Edge, then Chromium).")
    p.add_argument("--persistent", action="store_true",
                   help="Use a dedicated persistent user session to preserve browser storage/cookies across runs.")
    p.add_argument("--user-data-dir", default=str(HERE / ".chrome_profile"),
                   help="Dedicated directory for persistent browser profile (default: ./.chrome_profile).")
    p.add_argument("--setup-session", action="store_true",
                   help="Open Google Chrome in a visible window with dedicated profile to allow manual authorization, then exit.")
    p.add_argument("--manual", action="store_true",
                   help="Manual verification mode: visible browser (implies --headed); if Google shows a CAPTCHA, "
                        "pause so YOU can solve it, then resume. Nothing is bypassed, reloaded or retried.")
    p.add_argument("--manual-wait", type=float, default=600.0,
                   help="Max seconds to wait for manual verification (default 600).")
    p.add_argument("--scrape-details", action="store_true",
                   help="Stage 2: sequentially open each unique publication's Google Scholar detail page and extract full metadata.")
    p.add_argument("--detail-delay", type=float, default=4.0,
                   help="Base polite delay in seconds between individual publication detail requests (default: 4.0).")
    p.add_argument("--interaction-delay", type=float, default=0.5, help="Step 2 interaction delay; minimum 0.5 seconds.")
    p.add_argument("--post-click-delay", type=float, default=1.0, help="Step 2 delay after title click; minimum 1 second.")
    p.add_argument("--return-delay", type=float, default=1.0, help="Step 2 delay after returning to profile; minimum 1 second.")
    p.add_argument("--max-details", type=int, default=None,
                   help="Optional safety cap on number of detail pages to fetch in a single run.")
    p.add_argument("--no-detail-resume", action="store_true",
                   help="Re-scrape all publication detail pages even if already completed in publication_details_snapshot.json.")
    p.add_argument("--proxy", default=None,
                   help="Optional proxy server URL (e.g., http://127.0.0.1:8080 or socks5://127.0.0.1:1080). "
                        "If omitted, uses a direct connection (--no-proxy-server) and ignores OS/env proxies.")
    args = p.parse_args()
    if args.manual:
        args.headed = True
    return args


def setup_logging(out_dir: Path):
    fmt = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(out_dir / "scraper.log", mode="w", encoding="utf-8"),
        ],
    )


def _load_profile_from_snapshot(snapshot_path: Path) -> ProfileData:
    with open(snapshot_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    d = ProfileData(
        requested_url=raw.get("requested_url") or "",
        google_scholar_id=raw.get("google_scholar_id"),
        loaded_url=raw.get("loaded_url"),
        http_status=raw.get("http_status"),
        header=raw.get("header") or {},
        metrics=raw.get("metrics_table") or {},
        publications=raw.get("publications_raw") or [],
        footer_text=raw.get("footer_text"),
        listing_complete=bool(raw.get("listing_complete", True)),
        show_more_clicks=int(raw.get("show_more_clicks") or 0),
        initial_row_count=int(raw.get("initial_row_count") or 0),
        pagination_steps=raw.get("pagination_steps") or [],
        stop_reason=raw.get("stop_reason"),
        navigations=raw.get("navigations") or [],
        network_stats=raw.get("network_diagnostics"),
    )
    return d


@dataclass
class PipelineResult:
    exit_code: int
    status: str = "IDLE"
    message: str = ""
    google_scholar_id: str = ""
    researcher_name: str = ""
    publications_found: int = 0
    publications_processed: int = 0
    details_scraped: int = 0
    pending_publications: int = 0
    stopped_by_block: bool = False
    block_reason: Optional[str] = None
    output_dir: Optional[Path] = None
    profile_data: Optional[ProfileData] = None
    transformed: object = None
    detail_summary: dict = field(default_factory=dict)


def _execute_scraper_pipeline(
    url: Optional[str] = None,
    output_dir: Path = HERE / "output",
    registry_path: Optional[Path] = None,
    docx_path: Path = DEFAULT_DOCX,
    from_snapshot: Optional[Path] = None,
    headed: bool = False,
    delay: float = 3.0,
    timeout_s: float = 45.0,
    max_show_more: int = 100,
    browser_channel: str = "auto",
    persistent: bool = False,
    user_data_dir: Optional[Path] = None,
    proxy_url: Optional[str] = None,
    manual: bool = False,
    manual_wait_s: float = 600.0,
    scrape_details: bool = False,
    detail_delay: float = 4.0,
    max_details: Optional[int] = None,
    detail_resume: bool = True,
    progress_callback: Optional[Callable[[dict], None]] = None,
    scrape_profile_fn=None,
    scrape_details_fn=None,
    manual_cancel_event=None,
    interaction_delay: float = 0.5,
    post_click_delay: float = 1.0,
    return_delay: float = 1.0,
) -> PipelineResult:
    """
    Reusable end-to-end execution pipeline shared by the CLI (`run_scraper.py`)
    and the Desktop GUI (`gui_app.py`).
      * When `scrape_details=False` (STEP 1): Scrapes the scholar's profile front page
        (including dynamic SHOW MORE pagination), deduplicates against existing CSVs,
        cleans previous records exclusive to this scholar while preserving other scholars,
        and atomically exports the 6 ERD CSVs + quality reports. Does NOT open detail pages.
      * When `scrape_details=True` (STEP 2): Automatically executes STEP 1 first, then
        sequentially opens each unique publication's Google Scholar detail page with
        checkpoint/resume and rate-limit safety, enriches PUBLICATIONS/author_order,
        and exports `publication_details.csv` and `publication_yearly_citations.csv`.
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("run_scraper")

    _scrape_profile = scrape_profile_fn or scrape_profile
    _scrape_details = scrape_details_fn or scrape_publications_details_batch

    def _emit(payload: dict):
        if progress_callback:
            progress_callback(payload)

    if manual:
        headed = True

    now = datetime.now().astimezone()
    run_info = {
        "timestamp_display": now.strftime("%Y-%m-%d %H:%M:%S %Z (UTC%z)"),
        "browser_mode": "headed Chromium (Playwright)" if headed else "headless Chromium (Playwright)",
        "errors": [],
    }
    ts = now.strftime("%Y-%m-%d %H:%M:%S")   # Oracle TIMESTAMP-friendly, local time

    schema_check = validate_against_docx(str(docx_path))
    for m in schema_check[1]:
        log.info("Schema check: %s", m)
    if schema_check[0] is False:
        msg = "CSV headers do not match the DOCX. Update erd_schema.py before scraping."
        log.error(msg)
        _emit({"phase": "error", "status": "Schema Error", "message": msg, "progress_pct": 0.0})
        return PipelineResult(exit_code=3, status="ERROR", message=msg, output_dir=out_dir)

    reg_path = Path(registry_path) if registry_path else (out_dir / "id_registry.json")
    try:
        verify_csv_files_writable(out_dir, include_details=scrape_details)
        registry = IdRegistry(reg_path)
        existing_tables = load_existing_csvs(out_dir)
        registry.sync_from_tables(existing_tables, default_timestamp=ts)
    except (CsvPermissionError, PermissionError) as e:
        perm_err = e if isinstance(e, CsvPermissionError) else CsvPermissionError(out_dir / "scholars.csv", operation="access", original_error=e)
        msg = str(perm_err)
        log.error("CSV Permission Error: %s", msg)
        _emit({"phase": "permission_error", "status": "CSV Permission Error", "message": msg, "progress_pct": 0.0})
        return PipelineResult(exit_code=5, status="PERMISSION_ERROR", message=msg, output_dir=out_dir)
    except RegistryError as e:
        msg = f"ID Registry error: {e}"
        log.error(msg)
        _emit({"phase": "error", "status": "Registry Error", "message": msg, "progress_pct": 0.0})
        return PipelineResult(exit_code=4, status="ERROR", message=msg, output_dir=out_dir)

    if from_snapshot:
        snap_path = Path(from_snapshot)
        log.info("Loading profile data from existing snapshot: %s", snap_path)
        data = _load_profile_from_snapshot(snap_path)
        req_url = data.requested_url
        run_info["browser_mode"] = f"snapshot ({snap_path.name})"
    else:
        req_url = (url or "").strip()
        try:
            gs_id_validated = parse_google_scholar_id(req_url)
        except InvalidProfileUrlError as e:
            msg = str(e)
            log.error("Invalid Google Scholar profile URL: %s", msg)
            _emit({"phase": "error", "status": "Invalid URL", "message": msg, "progress_pct": 0.0})
            return PipelineResult(exit_code=2, status="INVALID_URL", message=msg, output_dir=out_dir)

        existing_scholar = next((r for r in existing_tables["SCHOLARS"]
                                 if r.get("google_scholar_id") == gs_id_validated), None)
        if existing_scholar:
            name = existing_scholar["scholar_name"]
            sid = existing_scholar["scholar_id"]
            if not scrape_details:
                msg = f"Scholar Already Stored — Profile front-page data already exists. Scholar: {name}; Google Scholar ID: {gs_id_validated}."
                _emit({"phase": "already_stored", "status": "Scholar Already Stored", "message": msg,
                       "researcher_name": name, "progress_pct": 100.0})
                return PipelineResult(0, "ALREADY_STORED", msg, gs_id_validated, name, output_dir=out_dir)
            detail_tables = load_existing_detail_csvs(out_dir)
            state = completion_state(out_dir, sid, existing_tables, detail_tables)
            total, completed = len(state["linked"]), len(state["complete"])
            pending = len(state["pending"])
            if not pending:
                msg = f"Publication Detail Scraping Already Complete — {completed}/{total} publications for {name}."
                _emit({"phase": "completed", "status": "Publication Detail Scraping Already Complete",
                       "message": msg, "researcher_name": name, "publications_found": total,
                       "details_scraped": completed, "pending_publications": 0, "progress_pct": 100.0})
                return PipelineResult(0, "ALREADY_COMPLETE", msg, gs_id_validated, name,
                                      total, total, completed, 0, output_dir=out_dir)
            _emit({"phase": "step2_pending", "status": "Scholar Already Stored — Continuing Publication Detail Scraping",
                   "message": (f"Scholar Already Stored — Continuing Publication Detail Scraping. "
                               f"Scholar: {name}; total linked publications: {total}; "
                               f"detail records completed: {completed}; detail records pending: {pending}."),
                   "researcher_name": name, "publications_found": total,
                   "details_scraped": completed, "pending_publications": pending,
                   "progress_pct": round(100 * completed / total, 1) if total else 0})
            pub_queue = [{"publication_id": pid, "scholar_id": sid,
                          "title": state["publications"][pid].get("title") or "",
                          "source_url": state["publications"][pid].get("source_url") or ""}
                         for pid in sorted(state["pending"], key=int)]
            def _existing_progress(ev):
                completed_now = completed + ev.get("summary", {}).get("succeeded_this_run", 0)
                _emit({"phase": ev.get("phase", "step2_progress"), "status": ev.get("status"),
                       "message": ev.get("message"), "manual_page_state": ev.get("manual_page_state"),
                       "researcher_name": name, "publications_found": total,
                       "publications_processed": completed + ev.get("publications_processed", 0),
                       "details_scraped": completed_now,
                       "pending_publications": max(0, total - completed_now),
                       "current_publication_id": ev.get("current_publication_id"),
                       "current_publication_title": ev.get("current_publication_title"),
                       "progress_pct": round(100 * completed_now / total, 1) if total else 100})
            kwargs = dict(publications_queue=pub_queue, snapshot_path=scoped_detail_path(out_dir, gs_id_validated),
                          headless=not headed, delay=detail_delay, timeout_s=timeout_s,
                          channel=browser_channel, persistent=persistent,
                          user_data_dir=Path(user_data_dir) if (persistent and user_data_dir) else None,
                          resume=detail_resume, max_details=max_details, manual=manual,
                          manual_wait_s=manual_wait_s, progress_callback=_existing_progress,
                          manual_cancel_event=manual_cancel_event)
            if proxy_url is not None:
                kwargs["proxy_url"] = proxy_url
            if _scrape_details is scrape_publications_details_batch:
                kwargs["profile_url"] = req_url
                kwargs.update(interaction_delay=interaction_delay, post_click_delay=post_click_delay, return_delay=return_delay)
                kwargs["verified_complete_ids"] = set()
            results = _scrape_details(**kwargs)
            transformed = Transformed(timestamp=ts, tables=existing_tables, registry=registry)
            apply_publication_details(transformed, results, existing_detail_tables=detail_tables)
            write_all_csvs(out_dir, transformed.tables, validate=True, detail_tables=transformed.detail_tables)
            state = completion_state(out_dir, sid, transformed.tables, transformed.detail_tables)
            completed, pending = len(state["complete"]), len(state["pending"])
            blocked = bool(results.get("summary", {}).get("stopped_by_block"))
            manual_outcome = results.get("summary", {}).get("manual_verification_outcome")
            cancelled = manual_outcome == "CANCELLED"
            unsuccessful = manual_outcome == "UNSUCCESSFUL"
            status = ("MANUAL_CANCELLED" if cancelled else "MANUAL_UNSUCCESSFUL" if unsuccessful
                      else "BLOCKED_PARTIAL" if blocked else ("SUCCESS" if not pending else "PARTIAL"))
            msg = (f"Step 2 {'manual verification cancelled' if cancelled else 'manual verification unsuccessful' if unsuccessful else 'blocked by Google Scholar' if blocked else 'completed' if not pending else 'partially completed'} "
                   f"for {name}: {completed}/{total} details complete, {pending} pending.")
            _emit({"phase": "manual_verification_cancelled" if cancelled else "manual_verification_unsuccessful" if unsuccessful
                           else "blocked" if blocked else "completed" if not pending else "step2_partial",
                   "status": msg, "message": msg, "researcher_name": name,
                   "publications_found": total, "details_scraped": completed,
                   "pending_publications": pending,
                   "progress_pct": round(100 * completed / total, 1) if total else 100})
            return PipelineResult(7 if cancelled else 0, status, msg, gs_id_validated, name, total,
                                  total - pending, completed, pending, blocked,
                                  results.get("summary", {}).get("block_reason"), out_dir,
                                  transformed=transformed, detail_summary=results.get("summary", {}))

        try:
            validate_proxy_config(proxy_url)
        except ProxyConfigurationError as e:
            msg = str(e)
            log.error("Invalid proxy configuration: %s", msg)
            _emit({
                "phase": "proxy_error",
                "status": "Invalid Proxy Configuration",
                "message": msg,
                "progress_pct": 0.0,
            })
            return PipelineResult(
                exit_code=2,
                status="PROXY_CONFIG_ERROR",
                message=msg,
                google_scholar_id=gs_id_validated,
                output_dir=out_dir,
            )

        _emit({
            "phase": "step1_start",
            "status": "Step 1: Scraping profile front page...",
            "google_scholar_id": gs_id_validated,
            "progress_pct": 5.0,
        })

        def _on_profile_progress(ev: dict):
            p_phase = ev.get("phase", "")
            r_name = ev.get("researcher_name") or ""
            p_found = ev.get("publications_found", 0)
            pct = 15.0 if p_phase == "step1_header_loaded" else (30.0 if p_phase == "step1_expanding" else 40.0)
            if not scrape_details and p_phase == "step1_extracted":
                pct = 75.0
            _emit({
                "phase": p_phase,
                "status": ev.get("status") or f"Step 1: Loaded {p_found} publications...",
                "researcher_name": r_name,
                "publications_found": p_found,
                "progress_pct": pct,
            })

        try:
            prof_kwargs = dict(
                headless=not headed,
                delay=delay,
                timeout_s=timeout_s,
                max_show_more=max_show_more,
                channel=browser_channel,
                manual=manual,
                manual_wait_s=manual_wait_s,
                persistent=persistent,
                user_data_dir=Path(user_data_dir) if (persistent and user_data_dir) else None,
                progress_callback=_on_profile_progress,
                manual_cancel_event=manual_cancel_event,
            )
            if proxy_url is not None:
                prof_kwargs["proxy_url"] = proxy_url
            data = _scrape_profile(req_url, **prof_kwargs)
            run_info["browser_mode"] = ("headed " if headed else "headless ") + f"{data.browser_used} via Playwright"
        except (ScraperError, Exception) as e:  # noqa: BLE001 - report every failure honestly
            log.error("Scraping failed: %s", e)
            err_str = str(e)
            run_info["errors"].append(err_str)
            pdata = getattr(e, "profile_data", None)
            if pdata and pdata.browser_used:
                run_info["browser_mode"] = ("headed " if headed else "headless ") + f"{pdata.browser_used} via Playwright"
            fail_rep = build_failure_report(req_url, e, run_info, pdata)
            (out_dir / "extraction_report.txt").write_text(fail_rep, encoding="utf-8")
            (out_dir / "extraction_quality_report.txt").write_text(fail_rep, encoding="utf-8")

            upper_err = err_str.upper()
            is_proxy_cfg = isinstance(e, ProxyConfigurationError)
            is_proxy_conn = isinstance(e, ProxyConnectionError) or any(c in upper_err for c in PROXY_ERROR_CODES)
            is_net_err = isinstance(e, NetworkConnectionError)
            is_manual_cancelled = isinstance(e, ManualVerificationCancelled)
            is_manual_unsuccessful = isinstance(e, ManualVerificationUnsuccessful)
            is_blocked = (
                not (is_proxy_cfg or is_proxy_conn or is_net_err)
                and (
                    isinstance(e, AccessBlockedError)
                    or "429" in err_str
                    or "captcha" in err_str.lower()
                    or "/sorry/" in err_str.lower()
                )
            )

            if is_manual_cancelled:
                res_status = "MANUAL_CANCELLED"
                status_label = "Manual Verification Cancelled"
                phase_name = "manual_verification_cancelled"
                final_err_msg = err_str
            elif is_manual_unsuccessful:
                res_status = "MANUAL_UNSUCCESSFUL"
                status_label = "Manual Verification Unsuccessful"
                phase_name = "manual_verification_unsuccessful"
                final_err_msg = err_str
            elif is_proxy_cfg:
                res_status = "PROXY_CONFIG_ERROR"
                status_label = "Invalid Proxy Configuration"
                phase_name = "proxy_error"
                final_err_msg = err_str
            elif is_proxy_conn:
                res_status = "PROXY_ERROR"
                status_label = "Proxy Connection Failed"
                phase_name = "proxy_error"
                if SUGGESTED_PROXY_ERROR_MESSAGE not in err_str:
                    final_err_msg = (
                        f"{SUGGESTED_PROXY_ERROR_MESSAGE}\n\n"
                        f"Failed URL: {req_url}\n"
                        f"Details: {err_str}"
                    )
                else:
                    final_err_msg = err_str
            elif is_net_err:
                res_status = "NETWORK_ERROR"
                status_label = "Network Connection Failed"
                phase_name = "network_error"
                final_err_msg = err_str
            elif is_blocked:
                res_status = "BLOCKED"
                status_label = "Blocked (HTTP 429 / CAPTCHA)"
                phase_name = "blocked"
                final_err_msg = err_str
            else:
                res_status = "ERROR"
                status_label = "Error"
                phase_name = "error"
                final_err_msg = err_str

            _emit({
                "phase": phase_name,
                "status": status_label,
                "message": final_err_msg,
                "stopped_by_block": is_blocked,
                "block_reason": final_err_msg if is_blocked else None,
                "progress_pct": 0.0,
            })
            return PipelineResult(
                exit_code=7 if is_manual_cancelled else 2,
                status=res_status,
                message=final_err_msg,
                google_scholar_id=gs_id_validated,
                stopped_by_block=is_blocked,
                block_reason=final_err_msg if is_blocked else None,
                output_dir=out_dir,
                profile_data=pdata,
            )

    if not data.header.get("scholar_name"):
        run_info["errors"].append("Profile name not found; page may not be a valid profile.")

    researcher_name = data.header.get("scholar_name") or ""
    target_gs_id = (data.google_scholar_id or "").strip()

    try:
        t = transform(
            data,
            ts,
            registry=registry,
            existing_tables=existing_tables,
            clean_target_scholars=True,
        )
        for cs in t.stats.get("cleanup_summaries", []):
            log.info(
                "Previous CSV cleanup for scholar %s (other_scholars=%s): removed=%s | preserved=%s",
                cs["target_google_scholar_id"],
                cs["other_scholars_present"],
                cs["removed_counts"],
                cs["preserved_counts"],
            )

        # ---- Stage 2: Individual Publication Detail Scraping (if requested or checkpoint exists) ----
        detail_snap_path = scoped_detail_path(out_dir, target_gs_id)
        existing_detail_tables = load_existing_detail_csvs(out_dir)
        target_scholar_ids = {
            str(r["scholar_id"]).strip()
            for r in t.tables.get("SCHOLARS", [])
            if (r.get("google_scholar_id") or "").strip() == target_gs_id
        }
        target_pub_to_sid = {
            str(r["publication_id"]).strip(): str(r["scholar_id"]).strip()
            for r in t.tables.get("Authors Publications", [])
            if str(r.get("scholar_id") or "").strip() in target_scholar_ids
        }
        scholar_unique_pubs = len(target_pub_to_sid)

        _emit({
            "phase": "step1_transformed",
            "status": f"Step 1 complete: {scholar_unique_pubs} unique publications extracted for {researcher_name}.",
            "researcher_name": researcher_name,
            "google_scholar_id": target_gs_id,
            "publications_found": scholar_unique_pubs,
            "publications_processed": scholar_unique_pubs if not scrape_details else 0,
            "details_scraped": 0,
            "pending_publications": 0 if not scrape_details else scholar_unique_pubs,
            "progress_pct": 90.0 if not scrape_details else 25.0,
        })

        # Save Step-1 CSVs & supplementary snapshot immediately after Step 1 so Step-1 results are safe on disk
        # even before Step 2 begins.
        if scrape_details:
            write_supplementary(
                out_dir,
                t.linkage,
                {
                    "requested_url": data.requested_url,
                    "loaded_url": data.loaded_url,
                    "http_status": data.http_status,
                    "extracted_at": run_info["timestamp_display"],
                    "google_scholar_id": data.google_scholar_id,
                    "header": data.header,
                    "metrics_table": data.metrics,
                    "footer_text": data.footer_text,
                    "listing_complete": data.listing_complete,
                    "show_more_clicks": data.show_more_clicks,
                    "initial_row_count": data.initial_row_count,
                    "pagination_steps": data.pagination_steps,
                    "stop_reason": data.stop_reason,
                    "navigations": data.navigations,
                    "publications_raw": data.publications,
                    "network_diagnostics": data.network_stats,
                },
            )
            write_all_csvs(
                out_dir,
                t.tables,
                unresolved_errors=t.unresolved_errors,
                validate=True,
                detail_tables=None,
            )
            registry.save()

            pub_queue = [
                {
                    "publication_id": str(r["publication_id"]).strip(),
                    "scholar_id": target_pub_to_sid[str(r["publication_id"]).strip()],
                    "title": r.get("title") or "",
                    "source_url": r.get("source_url") or "",
                }
                for r in t.tables.get("PUBLICATIONS", [])
                if str(r.get("publication_id") or "").strip() in target_pub_to_sid
            ]
            pre_detail_state = completion_state(out_dir, next(iter(target_scholar_ids)), t.tables, existing_detail_tables)
            pub_queue = [r for r in pub_queue if r["publication_id"] in pre_detail_state["pending"]]
            log.info(
                "Starting Stage 2 detail scraping for %d unique publications of scholar %s...",
                len(pub_queue),
                target_gs_id,
            )

            def _on_detail_progress(ev: dict):
                tot = scholar_unique_pubs
                already_done = len(pre_detail_state["complete"])
                proc = already_done + ev.get("publications_processed", 0)
                det_ok = already_done + ev.get("summary", {}).get("succeeded_this_run", 0)
                pend = max(0, tot - det_ok)
                cur_title = ev.get("current_publication_title") or ""
                cur_pid = ev.get("current_publication_id") or ""
                pct = 25.0 + (70.0 * (proc / tot)) if tot > 0 else 95.0
                _emit({
                    "phase": ev.get("phase", "step2_progress"),
                    "status": ev.get("status") or f"Step 2: Processing publication {proc}/{tot}...",
                    "message": ev.get("message"),
                    "manual_page_state": ev.get("manual_page_state"),
                    "researcher_name": researcher_name,
                    "google_scholar_id": target_gs_id,
                    "publications_found": tot,
                    "publications_processed": proc,
                    "details_scraped": det_ok,
                    "pending_publications": pend,
                    "current_publication_id": cur_pid,
                    "current_publication_title": cur_title,
                    "progress_pct": min(98.0, pct),
                })

            det_kwargs = dict(
                publications_queue=pub_queue,
                snapshot_path=detail_snap_path,
                headless=not headed,
                delay=detail_delay,
                timeout_s=timeout_s,
                channel=browser_channel,
                persistent=persistent,
                user_data_dir=Path(user_data_dir) if (persistent and user_data_dir) else None,
                resume=detail_resume,
                max_details=max_details,
                manual=manual,
                manual_wait_s=manual_wait_s,
                progress_callback=_on_detail_progress,
                manual_cancel_event=manual_cancel_event,
            )
            if proxy_url is not None:
                det_kwargs["proxy_url"] = proxy_url
            if _scrape_details is scrape_publications_details_batch:
                det_kwargs["profile_url"] = data.requested_url
                det_kwargs.update(interaction_delay=interaction_delay, post_click_delay=post_click_delay, return_delay=return_delay)
                det_kwargs["verified_complete_ids"] = set()
            detail_results = _scrape_details(**det_kwargs)
            apply_publication_details(t, detail_results, existing_detail_tables=existing_detail_tables)
        elif detail_snap_path.exists() or any(existing_detail_tables.values()):
            detail_results = None
            if detail_snap_path.exists():
                with open(detail_snap_path, "r", encoding="utf-8") as f:
                    detail_results = json.load(f)
            apply_publication_details(t, detail_results, existing_detail_tables=existing_detail_tables)

        quality_report = build_quality_report(data, t, schema_check, run_info)
        assessment = assess(data, t)
        report = build_report(data, t, assessment, schema_check, run_info)

        write_supplementary(
            out_dir,
            t.linkage,
            {
                "requested_url": data.requested_url,
                "loaded_url": data.loaded_url,
                "http_status": data.http_status,
                "extracted_at": run_info["timestamp_display"],
                "google_scholar_id": data.google_scholar_id,
                "header": data.header,
                "metrics_table": data.metrics,
                "footer_text": data.footer_text,
                "listing_complete": data.listing_complete,
                "show_more_clicks": data.show_more_clicks,
                "initial_row_count": data.initial_row_count,
                "pagination_steps": data.pagination_steps,
                "stop_reason": data.stop_reason,
                "navigations": data.navigations,
                "publications_raw": data.publications,
                "network_diagnostics": data.network_stats,
            },
        )

        # Validate & write all 6 CSVs (and optional Stage-2 detail CSVs) via staging
        write_all_csvs(
            out_dir,
            t.tables,
            unresolved_errors=t.unresolved_errors,
            validate=True,
            detail_tables=t.detail_tables if t.detail_tables else None,
        )
        registry.save()
    except (CsvPermissionError, PermissionError) as e:
        perm_err = e if isinstance(e, CsvPermissionError) else CsvPermissionError(out_dir / "scholars.csv", operation="write to", original_error=e)
        msg = str(perm_err)
        log.error("CSV Permission Error during export: %s", msg)
        _emit({"phase": "permission_error", "status": "CSV Permission Error", "message": msg})
        return PipelineResult(
            exit_code=5,
            status="PERMISSION_ERROR",
            message=msg,
            researcher_name=researcher_name,
            google_scholar_id=target_gs_id,
            output_dir=out_dir,
            profile_data=data,
        )
    except (DatasetValidationError, RegistryError) as e:
        log.error("Pre-export validation failed: %s", e)
        run_info["errors"].append(str(e))
        if "t" in locals():
            (out_dir / "extraction_quality_report.txt").write_text(
                build_quality_report(data, t, schema_check, run_info), encoding="utf-8"
            )
            (out_dir / "extraction_report.txt").write_text(
                build_report(data, t, assess(data, t), schema_check, run_info), encoding="utf-8"
            )
        _emit({"phase": "error", "status": "Validation Failed", "message": str(e)})
        return PipelineResult(
            exit_code=4,
            status="VALIDATION_ERROR",
            message=str(e),
            researcher_name=researcher_name,
            google_scholar_id=target_gs_id,
            output_dir=out_dir,
            profile_data=data,
        )

    (out_dir / "extraction_quality_report.txt").write_text(quality_report, encoding="utf-8")
    (out_dir / "extraction_report.txt").write_text(report, encoding="utf-8")
    log.info(
        "Done: %d HEIs, %d scholars, %d publications, %d research areas -> %s",
        t.stats["hei_rows"],
        t.stats["scholar_rows"],
        t.stats["publication_rows"],
        t.stats["research_areas"],
        out_dir,
    )

    d_sum = t.stats.get("detail_summary") or {}
    stopped_by_block = bool(d_sum.get("stopped_by_block", False))
    block_reason = d_sum.get("block_reason")
    manual_outcome = d_sum.get("manual_verification_outcome")

    if scrape_details:
        tot_pubs = d_sum.get("total_unique_publications", scholar_unique_pubs)
        state = completion_state(out_dir, next(iter(target_scholar_ids)), t.tables, t.detail_tables)
        det_scraped = len(state["complete"])
        processed = (
            d_sum.get("skipped_checkpoint", 0)
            + d_sum.get("skipped_duplicate_url", 0)
            + d_sum.get("attempted_this_run", 0)
        )
        pending = len(state["pending"])
        if manual_outcome == "CANCELLED":
            final_status = "Manual Verification Cancelled"
            final_msg = (f"Manual verification was cancelled for {researcher_name}. "
                         f"Successfully scraped details: {det_scraped}/{tot_pubs}; {pending} remain pending. "
                         "The checkpoint and completed CSV records were preserved.")
            pct = round(100.0 * det_scraped / tot_pubs, 1) if tot_pubs else 0.0
        elif manual_outcome == "UNSUCCESSFUL":
            final_status = "Manual Verification Unsuccessful"
            final_msg = (f"Google did not restore access to the publication detail page for {researcher_name}. "
                         f"Details complete: {det_scraped}/{tot_pubs}; {pending} remain pending. "
                         "The blocked endpoint was not retried.")
            pct = round(100.0 * det_scraped / tot_pubs, 1) if tot_pubs else 0.0
        elif stopped_by_block:
            final_status = "Paused — Blocked by Google Scholar (HTTP 429 / CAPTCHA)"
            final_msg = (
                f"Step 1 profile data saved ({tot_pubs} publications). "
                f"Step 2 paused because Google Scholar temporarily blocked detail requests ({block_reason}). "
                f"Successfully scraped details: {det_scraped}/{tot_pubs} ({pending} pending). "
                "All checkpoints and CSVs are preserved — you can resume later."
            )
            pct = round(100.0 * det_scraped / tot_pubs, 1) if tot_pubs else 0.0
        elif pending:
            final_status = "Step 2 Partially Completed"
            final_msg = f"Step 2 partially completed for {researcher_name}: {det_scraped}/{tot_pubs} details complete, {pending} pending."
            pct = round(100.0 * det_scraped / tot_pubs, 1) if tot_pubs else 0.0
        else:
            final_status = "Completed (Step 1 + Step 2 Full Publication Scraping)"
            final_msg = (
                f"Completed full profile & publication detail scraping for {researcher_name}. "
                f"Publications: {tot_pubs}, Details scraped: {det_scraped}, Pending: {pending}."
            )
            pct = 100.0
    else:
        tot_pubs = scholar_unique_pubs
        processed = scholar_unique_pubs
        det_scraped = sum(
            1
            for r in t.detail_tables.get("PUBLICATION_DETAILS", [])
            if str(r.get("publication_id") or "").strip() in target_pub_to_sid
        )
        pending = max(0, tot_pubs - det_scraped)
        final_status = "Completed (Step 1 — Profile Scraping)"
        final_msg = (
            f"Completed Step 1 profile scraping for {researcher_name}. "
            f"Extracted {tot_pubs} unique publications and exported all CSV files."
        )
        pct = 100.0

    _emit({
        "phase": "manual_verification_cancelled" if manual_outcome == "CANCELLED"
                 else "manual_verification_unsuccessful" if manual_outcome == "UNSUCCESSFUL"
                 else "blocked" if stopped_by_block else "completed",
        "status": final_status,
        "message": final_msg,
        "researcher_name": researcher_name,
        "google_scholar_id": target_gs_id,
        "publications_found": tot_pubs,
        "publications_processed": processed,
        "details_scraped": det_scraped,
        "pending_publications": pending,
        "current_publication_title": "None (Idle)" if not stopped_by_block else "Paused on rate-limit/CAPTCHA block",
        "stopped_by_block": stopped_by_block,
        "block_reason": block_reason,
        "progress_pct": pct,
    })

    return PipelineResult(
        exit_code=7 if manual_outcome == "CANCELLED" else 0,
        status="MANUAL_CANCELLED" if manual_outcome == "CANCELLED"
               else "MANUAL_UNSUCCESSFUL" if manual_outcome == "UNSUCCESSFUL"
               else "BLOCKED_PARTIAL" if stopped_by_block
               else ("PARTIAL" if scrape_details and pending else "SUCCESS"),
        message=final_msg,
        google_scholar_id=target_gs_id,
        researcher_name=researcher_name,
        publications_found=tot_pubs,
        publications_processed=processed,
        details_scraped=det_scraped,
        pending_publications=pending,
        stopped_by_block=stopped_by_block,
        block_reason=block_reason,
        output_dir=out_dir,
        profile_data=data,
        transformed=t,
        detail_summary=d_sum,
    )


def execute_scraper_pipeline(*args, **kwargs) -> PipelineResult:
    """Serialize CLI and GUI scraping with deletion, including checkpoint writes."""
    out_dir = Path(kwargs.get("output_dir", args[1] if len(args) > 1 else HERE / "output"))
    try:
        with dataset_lock(out_dir):
            recover_deletion(out_dir)
            return _execute_scraper_pipeline(*args, **kwargs)
    except RuntimeError as exc:
        if "Dataset is busy" not in str(exc):
            raise
        return PipelineResult(6, "BUSY", str(exc), output_dir=out_dir)


def main() -> int:
    args = parse_args()
    if args.setup_session:
        from scholar_scraper.browser import setup_interactive_session
        setup_interactive_session(Path(args.user_data_dir), channel=args.browser_channel, timeout_s=args.timeout)
        return 0

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(out_dir)

    url = args.url
    if not args.from_snapshot and not url:
        url = input("Enter Google Scholar profile URL: ").strip()

    res = execute_scraper_pipeline(
        url=url,
        output_dir=out_dir,
        registry_path=Path(args.registry) if args.registry else None,
        docx_path=Path(args.docx),
        from_snapshot=Path(args.from_snapshot) if args.from_snapshot else None,
        headed=args.headed,
        delay=args.delay,
        timeout_s=args.timeout,
        max_show_more=args.max_show_more,
        browser_channel=args.browser_channel,
        persistent=args.persistent,
        user_data_dir=Path(args.user_data_dir) if args.persistent else None,
        proxy_url=args.proxy,
        manual=args.manual,
        manual_wait_s=args.manual_wait,
        scrape_details=args.scrape_details,
        detail_delay=args.detail_delay,
        interaction_delay=args.interaction_delay,
        post_click_delay=args.post_click_delay,
        return_delay=args.return_delay,
        max_details=args.max_details,
        detail_resume=not args.no_detail_resume,
    )
    return res.exit_code


if __name__ == "__main__":
    sys.exit(main())
