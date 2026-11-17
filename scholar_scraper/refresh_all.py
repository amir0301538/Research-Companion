"""Sequential, cancellable refresh of stored Scholar front pages."""
import json
import os
from datetime import datetime
from pathlib import Path

from .browser import parse_google_scholar_id
from .output_migration import migrate_output
from .output_paths import OutputPaths
from .reporting import load_existing_csvs


def stored_profiles(out_dir):
    migrate_output(out_dir)
    seen = set()
    ready, skipped = [], []
    for scholar in load_existing_csvs(Path(out_dir))["SCHOLARS"]:
        google_id = (scholar.get("google_scholar_id") or "").strip()
        url = (scholar.get("google_scholar_profile_url") or "").strip()
        if google_id in seen:
            skipped.append({"google_scholar_id": google_id, "reason": "Duplicate stored Google Scholar ID"})
            continue
        seen.add(google_id)
        try:
            if parse_google_scholar_id(url) != google_id:
                raise ValueError("Saved profile URL does not match Google Scholar ID")
        except Exception as error:
            skipped.append({"google_scholar_id": google_id, "reason": str(error)})
            continue
        ready.append({"google_scholar_id": google_id, "scholar_id": scholar["scholar_id"],
                      "scholar_name": scholar["scholar_name"], "url": url})
    return ready, skipped


def _write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_suffix(".json.tmp")
    with staged.open("w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(staged, path)


def refresh_all(out_dir, *, pipeline_runner=None, progress_callback=None,
                cancel_event=None, **browser_options):
    from run_scraper import execute_scraper_pipeline
    run = pipeline_runner or execute_scraper_pipeline
    paths = OutputPaths(out_dir)
    ready, skipped = stored_profiles(out_dir)
    report = {"started_at": datetime.now().astimezone().isoformat(), "ended_at": None,
              "total_scholars": len(ready) + len(skipped), "profile_urls_available": len(ready),
              "successfully_refreshed": 0, "failed": 0, "blocked": 0,
              "skipped": len(skipped), "skipped_scholars": skipped,
              "new_publications": 0, "existing_publications_updated": 0,
              "citation_count_changes": 0, "scholar_metric_changes": 0,
              "new_research_areas": 0, "updated_affiliations": 0,
              "profile_image_updates": 0, "changed_records": 0,
              "manual_review": [], "validation_results": [],
              "scholars": [], "status": "RUNNING"}
    emit = progress_callback or (lambda event: None)
    _write_report(paths.report("refresh_all_report.json"), report)
    for index, scholar in enumerate(ready, 1):
        if cancel_event and cancel_event.is_set():
            report["status"] = "CANCELLED"
            break
        emit({"phase": "refresh_start", "status": f"Refreshing scholar {index} of {len(ready)}",
              "researcher_name": scholar["scholar_name"], "google_scholar_id": scholar["google_scholar_id"],
              "current_scholar": index, "total_scholars": len(ready),
              "progress_pct": 100 * (index - 1) / len(ready)})
        def on_scholar(event):
            emit({**event, "current_scholar": index, "total_scholars": len(ready),
                  "google_scholar_id": scholar["google_scholar_id"],
                  "progress_pct": 100 * (index - 1 + float(event.get("progress_pct", 0)) / 100) / len(ready)})
        try:
            result = run(url=scholar["url"], output_dir=out_dir, refresh_existing=True,
                         progress_callback=on_scholar, **browser_options)
        except Exception as error:
            report["failed"] += 1
            report["scholars"].append({
                "google_scholar_id": scholar["google_scholar_id"], "scholar_id": scholar["scholar_id"],
                "scholar_name": scholar["scholar_name"], "status": "ERROR",
                "message": str(error), "changes": {}, "manual_review": []})
            report["validation_results"].append({
                "google_scholar_id": scholar["google_scholar_id"], "errors": [str(error)]})
            _write_report(paths.report("refresh_all_report.json"), report)
            continue
        item = {"google_scholar_id": scholar["google_scholar_id"],
                "scholar_id": scholar["scholar_id"], "scholar_name": scholar["scholar_name"],
                "status": result.status, "message": result.message,
                "changes": result.changes, "manual_review": result.review}
        report["scholars"].append(item)
        if result.status == "SUCCESS":
            report["successfully_refreshed"] += 1
            change = result.changes
            report["new_publications"] += change.get("new_publications", 0)
            report["existing_publications_updated"] += change.get("updated_publications", 0)
            report["citation_count_changes"] += len(change.get("citation_count_changes", []))
            report["scholar_metric_changes"] += sum(k in ("h_index", "total_citations")
                                                    for k in change.get("scholar_fields", {}))
            report["new_research_areas"] += change.get("new_research_areas", 0)
            report["updated_affiliations"] += bool(change.get("updated_affiliation"))
            report["profile_image_updates"] += bool(change.get("profile_image_updated"))
            report["changed_records"] += (len(change.get("scholar_fields", {})) +
                change.get("new_publications", 0) + change.get("updated_publications", 0) +
                change.get("research_area_links_added", 0) +
                change.get("research_area_links_removed", 0) + bool(change.get("profile_image_updated")))
            report["manual_review"].extend(change.get("manual_review", []))
            report["validation_results"].append({"google_scholar_id": scholar["google_scholar_id"], "errors": []})
        elif result.stopped_by_block or result.status in ("BLOCKED", "MANUAL_UNSUCCESSFUL"):
            report["blocked"] += 1
            report["status"] = "BLOCKED"
            _write_report(paths.report("refresh_all_report.json"), report)
            break
        elif result.status == "MANUAL_CANCELLED":
            report["status"] = "CANCELLED"
            _write_report(paths.report("refresh_all_report.json"), report)
            break
        else:
            report["failed"] += 1
            report["validation_results"].append({"google_scholar_id": scholar["google_scholar_id"],
                                                  "errors": [result.message]})
        emit({"phase": "refresh_progress", "status": result.status, "current_scholar": index,
              "total_scholars": len(ready), "successful_refreshes": report["successfully_refreshed"],
              "failed_refreshes": report["failed"], "skipped_scholars": report["skipped"],
              "changed_records": report["changed_records"],
              "progress_pct": 100 * index / len(ready)})
        _write_report(paths.report("refresh_all_report.json"), report)
    if report["status"] == "RUNNING":
        report["status"] = "COMPLETED_WITH_ERRORS" if report["failed"] else "COMPLETED"
    report["ended_at"] = datetime.now().astimezone().isoformat()
    _write_report(paths.report("refresh_all_report.json"), report)
    return report
