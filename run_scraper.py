"""Single-stage Google Scholar profile scraper. Core CSVs are canonical storage."""
import argparse
import csv
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from scholar_scraper.browser import (
    PROXY_ERROR_CODES, SUGGESTED_PROXY_ERROR_MESSAGE, AccessBlockedError,
    InvalidProfileUrlError, ManualVerificationCancelled,
    ManualVerificationUnsuccessful, NetworkConnectionError, ProfileData,
    ProxyConfigurationError, ProxyConnectionError, ScraperError,
    parse_google_scholar_id, scrape_profile, validate_proxy_config,
)
from scholar_scraper.dataset_ops import dataset_lock, recover_deletion
from scholar_scraper.docx_schema import validate_against_docx
from scholar_scraper.frontpage_export import commit_export, csv_bytes, image_record, load_images
from scholar_scraper.id_registry import IdRegistry, RegistryError
from scholar_scraper.output_migration import migrate_output
from scholar_scraper.output_paths import OutputPaths
from scholar_scraper.refresh_merge import filter_ambiguous, merge_refresh
from scholar_scraper.reporting import (
    CsvPermissionError, assess, build_quality_report, build_report,
    load_existing_csvs, verify_csv_files_writable,
)
from scholar_scraper.transform import transform
from scholar_scraper.validator import DatasetValidationError

HERE = Path(__file__).resolve().parent
DEFAULT_DOCX = HERE / "Research_Companion_ERD_Listing_Exact_Names_V2.docx"


@dataclass
class PipelineResult:
    exit_code: int
    status: str = "IDLE"
    message: str = ""
    google_scholar_id: str = ""
    researcher_name: str = ""
    publications_found: int = 0
    publications_processed: int = 0
    stopped_by_block: bool = False
    block_reason: Optional[str] = None
    output_dir: Optional[Path] = None
    profile_data: Optional[ProfileData] = None
    transformed: object = None
    image_status: str = ""
    export_format: str = "CSV"
    show_more_clicks: int = 0
    changes: dict = field(default_factory=dict)
    review: list = field(default_factory=list)


def _snapshot(path):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    data = ProfileData(requested_url=raw["requested_url"])
    data.google_scholar_id = raw.get("google_scholar_id")
    data.loaded_url = raw.get("loaded_url")
    data.http_status = raw.get("http_status")
    data.header = raw.get("header") or {}
    data.metrics = raw.get("metrics_table") or raw.get("metrics") or {}
    data.publications = raw.get("publications_raw") or raw.get("publications") or []
    data.listing_complete = bool(raw.get("listing_complete"))
    data.show_more_clicks = int(raw.get("show_more_clicks") or 0)
    data.initial_row_count = int(raw.get("initial_row_count") or 0)
    data.profile_image_url = data.header.get("profile_image_url")
    return data


def _execute_scraper_pipeline(
    url: Optional[str] = None, output_dir: Path = HERE / "output",
    registry_path: Optional[Path] = None, docx_path: Path = DEFAULT_DOCX,
    from_snapshot: Optional[Path] = None, headed: bool = False,
    delay: float = 3.0, timeout_s: float = 45.0, max_show_more: int = 100,
    browser_channel: str = "auto", persistent: bool = False,
    user_data_dir: Optional[Path] = None, proxy_url: Optional[str] = None,
    manual: bool = False, manual_wait_s: float = 600.0,
    export_format: str = "CSV",
    refresh_existing: bool = False,
    progress_callback: Optional[Callable[[dict], None]] = None,
    scrape_profile_fn=None, manual_cancel_event=None,
) -> PipelineResult:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = OutputPaths(out_dir)
    fmt = export_format.upper()
    if fmt not in ("CSV", "JSON"):
        return PipelineResult(2, "INVALID_FORMAT", "Export format must be CSV or JSON.", output_dir=out_dir)
    emit = progress_callback or (lambda event: None)
    schema_check = validate_against_docx(str(docx_path))
    if not schema_check[0]:
        return PipelineResult(3, "SCHEMA_ERROR", "Core CSV headers do not match the ERD document.", output_dir=out_dir)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    try:
        verify_csv_files_writable(out_dir)
        registry = IdRegistry(Path(registry_path) if registry_path else paths.registry)
        existing = load_existing_csvs(out_dir)
        registry.sync_from_tables(existing, default_timestamp=stamp)
        previous_images = load_images(out_dir)
        known_image_ids = {row["scholar_id"] for row in previous_images}
        for stored_scholar in existing["SCHOLARS"]:
            if stored_scholar["scholar_id"] not in known_image_ids:
                default_image, _ = image_record(out_dir, stored_scholar, ProfileData(requested_url=""),
                                                previous_images, stamp)
                previous_images.append(default_image)
        request_url = _snapshot(from_snapshot).requested_url if from_snapshot else (url or "").strip()
        google_id = parse_google_scholar_id(request_url)
        old = next((r for r in existing["SCHOLARS"] if r["google_scholar_id"] == google_id), None)
        if refresh_existing and not old:
            raise ValueError(f"Scholar {google_id} is not stored; refresh cannot create a new scholar.")
        if old and not refresh_existing:
            if fmt == "JSON" or not paths.json_export.exists() or not paths.image_metadata.exists():
                commit_export(out_dir, existing, previous_images)
            msg = f"Scholar Already Stored — Profile front-page data already exists. Scholar: {old['scholar_name']}; Google Scholar ID: {google_id}."
            if fmt == "JSON":
                msg += f" JSON export synchronized at {paths.json_export}."
            emit(dict(phase="already_stored", status="Scholar Already Stored", message=msg,
                      researcher_name=old["scholar_name"], google_scholar_id=google_id, progress_pct=100))
            return PipelineResult(0, "ALREADY_STORED", msg, google_id, old["scholar_name"],
                                  output_dir=out_dir, export_format=fmt)
        validate_proxy_config(proxy_url)
        emit(dict(phase="step1_opening", status="Opening scholar profile", google_scholar_id=google_id, progress_pct=5))
        if from_snapshot:
            data = _snapshot(from_snapshot)
        else:
            scraper = scrape_profile_fn or scrape_profile
            kwargs = dict(headless=not (headed or manual), delay=delay, timeout_s=timeout_s,
                          max_show_more=max_show_more, channel=browser_channel, persistent=persistent,
                          user_data_dir=Path(user_data_dir) if persistent and user_data_dir else None,
                          manual=manual, manual_wait_s=manual_wait_s,
                          manual_cancel_event=manual_cancel_event,
                          progress_callback=lambda event: emit({**event, "progress_pct": 30}))
            if proxy_url is not None:
                kwargs["proxy_url"] = proxy_url
            data = scraper(request_url, **kwargs)
        if data.google_scholar_id != google_id:
            raise ScraperError("Loaded profile Google Scholar ID does not match the requested scholar.")
        if not data.listing_complete:
            raise ScraperError("Publication listing was not fully loaded; export was stopped.")
        review = []
        transform_data = data
        if refresh_existing:
            transform_data, review = filter_ambiguous(data, existing, old["scholar_id"])
        transformed = transform(transform_data, stamp, registry=registry, existing_tables=existing,
                                clean_target_scholars=not refresh_existing)
        changes = {}
        if refresh_existing:
            changes = merge_refresh(existing, transformed, transform_data, old["scholar_id"], stamp)
            changed_scholar = next(r for r in transformed.tables["SCHOLARS"]
                                   if r["google_scholar_id"] == google_id)
            changed_scholar["total_publications"] = str(len(data.publications))
            changes["manual_review"] = review + [
                {"publication_id": pid, "reason": "Absent from one fully loaded profile; link retained."}
                for pid in changes["publications_missing_from_profile"]]
        scholar = next(r for r in transformed.tables["SCHOLARS"] if r["google_scholar_id"] == google_id)
        new_image, image_extra = image_record(out_dir, scholar, data, previous_images, stamp)
        if refresh_existing:
            old_image = next((r for r in previous_images if r["scholar_id"] == scholar["scholar_id"]), None)
            changes["profile_image_updated"] = bool(
                image_extra and (not old_image or
                not (out_dir / old_image["local_profile_image_path"]).is_file() or
                (out_dir / old_image["local_profile_image_path"]).read_bytes() != next(iter(image_extra.values()))))
        images = [r for r in previous_images if r["scholar_id"] != scholar["scholar_id"]] + [new_image]
        # Historical detail CSVs/checkpoints are left in place; no detail requests occur.
        registry_payload = json.dumps({"version": 1, "entities": registry._data},
                                      ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        raw_profile = {
            "requested_url": data.requested_url, "loaded_url": data.loaded_url,
            "http_status": data.http_status, "google_scholar_id": google_id,
            "header": data.header, "metrics_table": data.metrics,
            "listing_complete": data.listing_complete, "show_more_clicks": data.show_more_clicks,
            "initial_row_count": data.initial_row_count, "publications_raw": data.publications,
        }
        linkage_columns = ("entity", "csv_row_number", "google_scholar_id", "source_key", "label")
        linkage_path = paths.linkage
        previous_linkage = []
        if linkage_path.exists():
            with linkage_path.open(encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                if tuple(reader.fieldnames or ()) != linkage_columns:
                    raise ValueError("Existing record_linkage.csv has an unexpected header.")
                previous_linkage = [row for row in reader if row.get("google_scholar_id") != google_id]
        extra = {"system/id_registry.json": registry_payload, **image_extra,
                 paths.relative(paths.profile_snapshot(google_id)):
                     json.dumps(raw_profile, ensure_ascii=False, indent=2).encode("utf-8"),
                 paths.relative(paths.linkage):
                     csv_bytes(linkage_columns, previous_linkage + transformed.linkage)}
        emit(dict(phase="exporting", status=f"Exporting {fmt}", researcher_name=scholar["scholar_name"],
                  publications_found=len(data.publications), show_more_clicks=data.show_more_clicks,
                  image_status=new_image["image_status"], progress_pct=85))
        commit_export(out_dir, transformed.tables, images, extra)
        report_info = {"timestamp_display": stamp, "browser_mode": data.browser_used or "snapshot", "errors": []}
        paths.reports_dir.mkdir(parents=True, exist_ok=True)
        paths.report("extraction_quality_report.txt").write_text(
            build_quality_report(data, transformed, schema_check, report_info), encoding="utf-8")
        paths.report("extraction_report.txt").write_text(
            build_report(data, transformed, assess(data, transformed), schema_check, report_info), encoding="utf-8")
        count = sum(r["scholar_id"] == scholar["scholar_id"] for r in transformed.tables["Authors Publications"])
        location = paths.json_export if fmt == "JSON" else paths.csv("scholars.csv")
        msg = (f"{'Profile refreshed' if refresh_existing else 'Profile scraped'}: "
               f"{scholar['scholar_name']}; {count} linked publications; image {new_image['image_status']}. Export: {location}")
        emit(dict(phase="completed", status="Completed", message=msg, researcher_name=scholar["scholar_name"],
                  google_scholar_id=google_id, publications_found=count, publications_processed=count,
                  show_more_clicks=data.show_more_clicks, image_status=new_image["image_status"],
                  export_format=fmt, progress_pct=100))
        return PipelineResult(0, "SUCCESS", msg, google_id, scholar["scholar_name"], count, count,
                              output_dir=out_dir, profile_data=data, transformed=transformed,
                              image_status=new_image["image_status"], export_format=fmt,
                              show_more_clicks=data.show_more_clicks, changes=changes, review=review)
    except Exception as error:
        text = str(error)
        if isinstance(error, ManualVerificationCancelled):
            status = "MANUAL_CANCELLED"
        elif isinstance(error, ManualVerificationUnsuccessful):
            status = "MANUAL_UNSUCCESSFUL"
        elif isinstance(error, AccessBlockedError):
            status = "BLOCKED"
        elif isinstance(error, (ProxyConfigurationError, ProxyConnectionError)):
            status = "PROXY_CONFIG_ERROR" if isinstance(error, ProxyConfigurationError) else "PROXY_ERROR"
        elif isinstance(error, NetworkConnectionError):
            status = "NETWORK_ERROR"
        elif isinstance(error, (PermissionError, CsvPermissionError)):
            status = "PERMISSION_ERROR"
        elif isinstance(error, DatasetValidationError):
            status = "VALIDATION_ERROR"
        elif isinstance(error, InvalidProfileUrlError):
            status = "INVALID_URL"
        else:
            status = "ERROR"
        emit(dict(phase="blocked" if status == "BLOCKED" else "error", status=status,
                  message=text, stopped_by_block=status == "BLOCKED", progress_pct=0))
        return PipelineResult(7 if status == "MANUAL_CANCELLED" else 2, status, text,
                              google_scholar_id=locals().get("google_id", ""),
                              stopped_by_block=status == "BLOCKED",
                              block_reason=text if status == "BLOCKED" else None,
                              output_dir=out_dir, export_format=fmt)


def execute_scraper_pipeline(*args, **kwargs):
    out_dir = Path(kwargs.get("output_dir", args[1] if len(args) > 1 else HERE / "output"))
    try:
        migrate_output(out_dir)
        with dataset_lock(out_dir):
            recover_deletion(out_dir)
            return _execute_scraper_pipeline(*args, **kwargs)
    except RuntimeError as error:
        if "Dataset is busy" not in str(error):
            raise
        return PipelineResult(6, "BUSY", str(error), output_dir=out_dir)


def parse_args():
    parser = argparse.ArgumentParser(description="Scrape a Google Scholar profile front page.")
    parser.add_argument("--url", default=os.environ.get("SCHOLAR_URL"))
    parser.add_argument("--output", default=str(HERE / "output"))
    parser.add_argument("--registry")
    parser.add_argument("--docx", default=str(DEFAULT_DOCX))
    parser.add_argument("--from-snapshot")
    parser.add_argument("--export-format", choices=("csv", "json"), default="csv")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--delay", type=float, default=3.0)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--max-show-more", type=int, default=100)
    parser.add_argument("--browser-channel", choices=("auto", "chrome", "msedge", "chromium"), default="auto")
    parser.add_argument("--persistent", action="store_true")
    parser.add_argument("--user-data-dir", default=str(HERE / ".chrome_profile"))
    parser.add_argument("--setup-session", action="store_true")
    parser.add_argument("--manual", action="store_true")
    parser.add_argument("--manual-wait", type=float, default=600.0)
    parser.add_argument("--proxy")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.setup_session:
        from scholar_scraper.browser import setup_interactive_session
        setup_interactive_session(Path(args.user_data_dir), channel=args.browser_channel, timeout_s=args.timeout)
        return 0
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    migrate_output(output)
    paths = OutputPaths(output)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=paths.log(), level=logging.INFO)
    url = args.url or ("" if args.from_snapshot else input("Enter Google Scholar profile URL: ").strip())
    result = execute_scraper_pipeline(
        url=url, output_dir=output, registry_path=args.registry, docx_path=Path(args.docx),
        from_snapshot=args.from_snapshot, headed=args.headed, delay=args.delay,
        timeout_s=args.timeout, max_show_more=args.max_show_more, browser_channel=args.browser_channel,
        persistent=args.persistent, user_data_dir=Path(args.user_data_dir), proxy_url=args.proxy,
        manual=args.manual, manual_wait_s=args.manual_wait, export_format=args.export_format)
    print(result.message)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
