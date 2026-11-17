"""Offline migration and refresh coverage; never touches the real output directory."""
import csv
import json
import threading
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch
import tkinter as tk

import pytest

from run_scraper import execute_scraper_pipeline
from scholar_scraper.browser import AccessBlockedError
from scholar_scraper.erd_schema import CSV_FILES, ERD_ENTITIES
from scholar_scraper.frontpage_export import IMAGE_COLUMNS, csv_bytes, load_images
from scholar_scraper.id_registry import IdRegistry
from scholar_scraper.output_migration import migrate_output
from scholar_scraper.output_paths import OutputPaths
from scholar_scraper.refresh_all import refresh_all, stored_profiles
from scholar_scraper.reporting import load_existing_csvs
from scholar_scraper.transform import transform
from scholar_scraper.validator import validate_dataset
from tests.test_gui_integration import make_profile_for_scholar_a
from tests.test_gui_integration import wait_for_gui_job
from gui_app import ScholarScraperGUI


def legacy_fixture(root):
    profile = make_profile_for_scholar_a()
    registry = IdRegistry(root / "id_registry.json")
    transformed = transform(profile, "2026-11-17 10:00:00", registry=registry)
    assert not transformed.validation_errors
    root.mkdir(parents=True, exist_ok=True)
    for entity, filename in CSV_FILES.items():
        (root / filename).write_bytes(csv_bytes(ERD_ENTITIES[entity], transformed.tables[entity]))
    registry.save()
    (root / "dataset.json").write_text(json.dumps({entity: transformed.tables[entity] for entity in CSV_FILES}),
                                       encoding="utf-8")
    (root / "Run.txt").write_text("legacy note", encoding="utf-8")
    (root / "supplementary").mkdir()
    (root / "supplementary" / "publication_details_snapshot.json").write_text('{"publications":{}}')
    (root / "profile_images").mkdir()
    image = root / "profile_images" / "vdh-mOQAAAAJ.jpg"
    image.write_bytes(b"\xff\xd8\xffphoto")
    rows = [dict(zip(IMAGE_COLUMNS, (transformed.tables["SCHOLARS"][0]["scholar_id"],
                                     profile.google_scholar_id, "profile_images/vdh-mOQAAAAJ.jpg",
                                     "https://scholar.googleusercontent.com/photo", "REAL", "2026-11-17")))]
    (root / "scholar_images.csv").write_bytes(csv_bytes(IMAGE_COLUMNS, rows))
    return transformed.tables


def test_migrate_legacy_idempotently_with_backup_and_images(tmp_path):
    root = tmp_path / "output"
    original = legacy_fixture(root)
    before_registry = (root / "id_registry.json").read_bytes()
    result = migrate_output(root)
    paths = OutputPaths(root)
    assert result["migrated"]
    assert Path(result["backup"]).is_dir()
    assert (Path(result["backup"]) / "Run.txt").read_text() == "legacy note"
    assert paths.registry.read_bytes() == before_registry
    assert load_existing_csvs(root) == original
    assert validate_dataset(load_existing_csvs(root)) == []
    assert load_images(root)[0]["local_profile_image_path"] == "profile_images/vdh-mOQAAAAJ.jpg"
    assert (root / load_images(root)[0]["local_profile_image_path"]).is_file()
    assert paths.json_export.is_file()
    assert not (root / "csv" / "csv").exists()
    assert not (root / "csv" / "json").exists()
    assert not (root / "supplementary").exists()
    assert json.loads(paths.json_export.read_text(encoding="utf-8"))["SCHOLAR_IMAGES"] == load_images(root)
    assert (paths.historical / "publication_details_snapshot.json").exists() is False
    assert (paths.checkpoints / "publication_details_snapshot.json").is_file()
    assert (paths.system_dir / "legacy" / "Run.txt").is_file()
    assert not (root / "scholars.csv").exists()
    assert migrate_output(root)["migrated"] is False
    assert load_existing_csvs(root) == original


def test_failed_migration_keeps_original_dataset(tmp_path):
    root = tmp_path / "output"
    legacy_fixture(root)
    before = {path.relative_to(root).as_posix(): path.read_bytes()
              for path in root.rglob("*") if path.is_file()}
    from scholar_scraper import output_migration
    with patch("scholar_scraper.dataset_ops._commit_files", side_effect=PermissionError(13, "locked")):
        with pytest.raises(PermissionError):
            output_migration.migrate_output(root)
    assert all((root / name).read_bytes() == body for name, body in before.items())
    assert (root / "scholars.csv").exists()
    assert not (root / "csv" / "scholars.csv").exists()


def test_mid_commit_migration_recovers_original_bytes(tmp_path):
    root = tmp_path / "output"
    legacy_fixture(root)
    before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    from scholar_scraper import dataset_ops
    replace = dataset_ops.os.replace
    count = 0
    def interrupted(source, target):
        nonlocal count
        count += 1
        if count == 4:
            raise PermissionError(13, "locked")
        return replace(source, target)
    with patch.object(dataset_ops.os, "replace", side_effect=interrupted):
        with pytest.raises(PermissionError):
            migrate_output(root)
    assert all((root / name).read_bytes() == body for name, body in before.items())
    assert not (root / "csv" / "scholars.csv").exists()


def seed(root):
    profile = make_profile_for_scholar_a()
    result = execute_scraper_pipeline(url=profile.requested_url, output_dir=root,
                                      scrape_profile_fn=Mock(return_value=profile))
    assert result.status == "SUCCESS", result.message
    return profile


def runner(profile):
    return lambda **kwargs: execute_scraper_pipeline(
        **kwargs, scrape_profile_fn=Mock(return_value=profile))


def test_refresh_changes_front_page_without_new_ids_or_missing_deletion(tmp_path):
    original = seed(tmp_path)
    before = load_existing_csvs(tmp_path)
    profile = deepcopy(original)
    profile.metrics["Citations"]["All"] = "321"
    profile.publications[0]["cited_by"] = "55"
    profile.publications = profile.publications[:1] + [{
        **profile.publications[0], "row_index": 3, "title": "New Paper",
        "source_url": "https://scholar.google.com/citations?view_op=view_citation&user=vdh-mOQAAAAJ&citation_for_view=vdh-mOQAAAAJ:new",
    }]
    profile.header["interests"] = ["Physics", "Biology"]
    report = refresh_all(tmp_path, pipeline_runner=runner(profile))
    assert report["status"] == "COMPLETED" and report["successfully_refreshed"] == 1
    after = load_existing_csvs(tmp_path)
    assert after["SCHOLARS"][0]["scholar_id"] == before["SCHOLARS"][0]["scholar_id"]
    assert after["SCHOLARS"][0]["total_citations"] == "321"
    assert len(after["PUBLICATIONS"]) == 3
    assert {r["publication_id"] for r in before["PUBLICATIONS"]} <= {r["publication_id"] for r in after["PUBLICATIONS"]}
    assert after["PUBLICATIONS"][0]["citation_count"] == "55"
    assert report["new_publications"] == 1 and report["citation_count_changes"] == 1
    assert report["manual_review"]
    assert validate_dataset(after) == []
    assert json.loads(OutputPaths(tmp_path).json_export.read_text())["PUBLICATIONS"] == after["PUBLICATIONS"]
    assert OutputPaths(tmp_path).report("refresh_all_report.json").is_file()


def test_unchanged_refresh_has_no_content_changes(tmp_path):
    profile = seed(tmp_path)
    result = refresh_all(tmp_path, pipeline_runner=runner(deepcopy(profile)))
    assert result["successfully_refreshed"] == 1
    assert result["changed_records"] == 0


def test_ambiguous_title_change_is_reported_without_new_publication(tmp_path):
    profile = seed(tmp_path)
    changed = deepcopy(profile)
    changed.publications[0]["title"] = "Renamed Paper"
    result = refresh_all(tmp_path, pipeline_runner=runner(changed))
    assert result["new_publications"] == 0
    assert result["manual_review"]
    assert len(load_existing_csvs(tmp_path)["PUBLICATIONS"]) == 2


def test_refresh_failure_keeps_prior_scholar_and_reports_error(tmp_path):
    profile = seed(tmp_path)
    original = load_existing_csvs(tmp_path)
    result = refresh_all(tmp_path, pipeline_runner=Mock(side_effect=ValueError("invalid fixture")))
    assert result["failed"] == 1 and result["status"] == "COMPLETED_WITH_ERRORS"
    assert load_existing_csvs(tmp_path) == original


def test_batch_block_and_cancel_preserve_prior_commits(tmp_path):
    profile = seed(tmp_path)
    second = deepcopy(profile)
    second.google_scholar_id = "SECOND_ID"
    second.requested_url = "https://scholar.google.com/citations?user=SECOND_ID"
    second.header["scholar_name"] = "Second Scholar"
    created = execute_scraper_pipeline(url=second.requested_url, output_dir=tmp_path,
                                       scrape_profile_fn=Mock(return_value=second))
    assert created.status == "SUCCESS"
    before = load_existing_csvs(tmp_path)
    def mixed(**kwargs):
        if "SECOND_ID" in kwargs["url"]:
            return execute_scraper_pipeline(**kwargs, scrape_profile_fn=Mock(side_effect=AccessBlockedError("HTTP 429")))
        return execute_scraper_pipeline(**kwargs, scrape_profile_fn=Mock(return_value=profile))
    report = refresh_all(tmp_path, pipeline_runner=mixed)
    assert report["status"] == "BLOCKED" and report["blocked"] == 1
    after = load_existing_csvs(tmp_path)["SCHOLARS"]
    assert [{k: v for k, v in r.items() if k != "last_synced_at"} for r in after] == [
        {k: v for k, v in r.items() if k != "last_synced_at"} for r in before["SCHOLARS"]]
    stop = threading.Event()
    stop.set()
    cancelled = refresh_all(tmp_path, pipeline_runner=Mock(side_effect=AssertionError("network")), cancel_event=stop)
    assert cancelled["status"] == "CANCELLED" and cancelled["successfully_refreshed"] == 0


def test_stored_profiles_deduplicates_and_skips_bad_urls(tmp_path):
    seed(tmp_path)
    ready, skipped = stored_profiles(tmp_path)
    assert len(ready) == 1 and not skipped


def test_gui_refresh_confirmation_worker_and_cancel(tmp_path):
    profile = seed(tmp_path)
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Tk display unavailable")
    root.withdraw()
    try:
        gui = ScholarScraperGUI(root, output_dir=tmp_path, show_dialogs=False,
                                confirm_refresh_fn=lambda _title, _message: False)
        assert gui.start_refresh_all_scholars() is False
        assert not gui.is_running
        gui.confirm_refresh_fn = lambda _title, _message: True
        with patch("gui_app.refresh_all", side_effect=lambda *a, **k:
                   refresh_all(*a, pipeline_runner=runner(profile), **k)):
            assert gui.start_refresh_all_scholars()
            assert gui.is_running
            wait_for_gui_job(gui, root)
        assert not gui.is_running
        assert gui.refresh_success_var.get() == "1"
        assert gui.status_var.get() == "Refresh Completed"
        assert OutputPaths(tmp_path).report("refresh_all_report.json").is_file()
    finally:
        root.destroy()
