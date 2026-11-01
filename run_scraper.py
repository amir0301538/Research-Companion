"""
Command-line entry point.

Usage:
    python run_scraper.py --url "https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ"
    python run_scraper.py --url "<another profile URL>" --output output/other_scholar
"""

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from scholar_scraper.browser import ScraperError, scrape_profile
from scholar_scraper.docx_schema import validate_against_docx
from scholar_scraper.reporting import (assess, build_failure_report, build_report,
                                       write_all_csvs, write_supplementary)
from scholar_scraper.transform import transform

HERE = Path(__file__).resolve().parent
DEFAULT_DOCX = HERE / "Research_Companion_ERD_Listing_Exact_Names_V2.docx"


def parse_args():
    p = argparse.ArgumentParser(description="Scrape a Google Scholar profile front page into ERD-shaped CSVs.")
    p.add_argument("--url", default=os.environ.get("SCHOLAR_URL"),
                   help="Google Scholar profile URL (or set SCHOLAR_URL env var).")
    p.add_argument("--output", default=str(HERE / "output"), help="Output folder (default: ./output).")
    p.add_argument("--docx", default=str(DEFAULT_DOCX), help="ERD Data Dictionary DOCX used to verify headers.")
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
    args = p.parse_args()
    if args.manual:
        args.headed = True
    return args


def setup_logging(out_dir: Path):
    fmt = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt,
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(out_dir / "scraper.log", mode="w", encoding="utf-8")])


def main() -> int:
    args = parse_args()
    if args.setup_session:
        from scholar_scraper.browser import setup_interactive_session
        setup_interactive_session(Path(args.user_data_dir), channel=args.browser_channel, timeout_s=args.timeout)
        return 0

    url = args.url or input("Enter Google Scholar profile URL: ").strip()
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(out_dir)
    log = logging.getLogger("run_scraper")

    now = datetime.now().astimezone()
    run_info = {
        "timestamp_display": now.strftime("%Y-%m-%d %H:%M:%S %Z (UTC%z)"),
        "browser_mode": "headed Chromium (Playwright)" if args.headed else "headless Chromium (Playwright)",
        "errors": [],
    }
    ts = now.strftime("%Y-%m-%d %H:%M:%S")   # Oracle TIMESTAMP-friendly, local time

    schema_check = validate_against_docx(args.docx)
    for m in schema_check[1]:
        log.info("Schema check: %s", m)
    if schema_check[0] is False:
        log.error("CSV headers do not match the DOCX. Update erd_schema.py before scraping.")
        return 3

    try:
        data = scrape_profile(url, headless=not args.headed, delay=args.delay,
                              timeout_s=args.timeout, max_show_more=args.max_show_more,
                              channel=args.browser_channel, manual=args.manual,
                              manual_wait_s=args.manual_wait,
                              persistent=args.persistent,
                              user_data_dir=Path(args.user_data_dir) if args.persistent else None)
        run_info["browser_mode"] = ("headed " if args.headed else "headless ") + f"{data.browser_used} via Playwright"
    except (ScraperError, Exception) as e:  # noqa: BLE001 - report every failure honestly
        log.error("Scraping failed: %s", e)
        run_info["errors"].append(str(e))
        pdata = getattr(e, "profile_data", None)
        if pdata and pdata.browser_used:
            run_info["browser_mode"] = ("headed " if args.headed else "headless ") + f"{pdata.browser_used} via Playwright"
        (out_dir / "extraction_report.txt").write_text(
            build_failure_report(url, e, run_info, pdata), encoding="utf-8")
        return 2

    if not data.header.get("scholar_name"):
        run_info["errors"].append("Profile name not found; page may not be a valid profile.")

    t = transform(data, ts)
    write_all_csvs(out_dir, t.tables)
    write_supplementary(out_dir, t.linkage, {
        "requested_url": data.requested_url, "loaded_url": data.loaded_url,
        "http_status": data.http_status, "extracted_at": run_info["timestamp_display"],
        "google_scholar_id": data.google_scholar_id, "header": data.header,
        "metrics_table": data.metrics, "footer_text": data.footer_text,
        "listing_complete": data.listing_complete, "show_more_clicks": data.show_more_clicks,
        "navigations": data.navigations, "publications_raw": data.publications,
        "network_diagnostics": data.network_stats,
    })
    assessment = assess(data, t)
    report = build_report(data, t, assessment, schema_check, run_info)
    (out_dir / "extraction_report.txt").write_text(report, encoding="utf-8")
    log.info("Done: %d publications, %d research areas -> %s",
             t.stats["publication_rows"], t.stats["research_areas"], out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
