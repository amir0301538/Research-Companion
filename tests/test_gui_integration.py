"""Offline front-page pipeline and GUI regression tests."""
import csv
import json
import time
import tkinter as tk
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

import pytest

from gui_app import ScholarScraperGUI
from run_scraper import execute_scraper_pipeline
from scholar_scraper.browser import AccessBlockedError, ProfileData
from scholar_scraper.browser import JS_HEADER
from scholar_scraper.dataset_ops import delete_scholar
from scholar_scraper.erd_schema import CSV_FILES, ERD_ENTITIES
from scholar_scraper.frontpage_export import image_extension, image_record, load_images
from scholar_scraper.reporting import load_existing_csvs
from scholar_scraper.validator import validate_dataset


def make_profile_for_scholar_a(show_more_clicks=2):
    data = ProfileData(requested_url="https://scholar.google.com/citations?user=vdh-mOQAAAAJ",
                       google_scholar_id="vdh-mOQAAAAJ", listing_complete=True,
                       show_more_clicks=show_more_clicks, initial_row_count=1, http_status=200)
    data.header = {"scholar_name": "Scholar A", "affiliation_text": "Example University",
                   "verification_text": "Verified email at example.edu", "header_text": "Scholar A",
                   "interests": ["General Relativity", "Physics"]}
    data.metrics = {"Citations": {"All": "123"}, "h-index": {"All": "5"}}
    data.publications = [
        {"row_index": i, "title": f"Paper {i}", "authors": "A Scholar, B Author",
         "venue": "Example Journal", "year": "2024", "cited_by": "2",
         "source_url": f"https://scholar.google.com/citations?view_op=view_citation&user=vdh-mOQAAAAJ&citation_for_view=vdh-mOQAAAAJ:p{i}"}
        for i in (1, 2)
    ]
    return data


def wait_for_gui_job(gui, root, timeout_s=15):
    deadline = time.time() + timeout_s
    while gui.is_running and time.time() < deadline:
        gui.process_pending_events()
        root.update()
        time.sleep(0.02)
    gui.process_pending_events()
    root.update()


def test_csv_json_equivalence_and_stable_ids(tmp_path):
    profile = make_profile_for_scholar_a()
    scraper = Mock(return_value=profile)
    result = execute_scraper_pipeline(url=profile.requested_url, output_dir=tmp_path,
                                      export_format="JSON", scrape_profile_fn=scraper)
    assert result.status == "SUCCESS", result.message
    tables = load_existing_csvs(tmp_path)
    payload = json.loads((tmp_path / "json" / "dataset.json").read_text(encoding="utf-8"))
    for entity, filename in CSV_FILES.items():
        assert payload[entity] == tables[entity]
        with (tmp_path / "csv" / filename).open(encoding="utf-8", newline="") as stream:
            assert csv.DictReader(stream).fieldnames == ERD_ENTITIES[entity]
    assert validate_dataset(tables) == []
    assert len(tables["PUBLICATIONS"]) == 2
    assert all(row["source_url"] for row in tables["PUBLICATIONS"])
    assert scraper.call_count == 1
    assert load_images(tmp_path)[0]["image_status"] == "DEFAULT"
    before = {name: (tmp_path / "csv" / name).read_bytes() for name in CSV_FILES.values()}
    again = execute_scraper_pipeline(url=profile.requested_url, output_dir=tmp_path,
                                     export_format="CSV", scrape_profile_fn=Mock(side_effect=AssertionError("duplicate scraped")))
    assert again.status == "ALREADY_STORED"
    assert before == {name: (tmp_path / "csv" / name).read_bytes() for name in CSV_FILES.values()}


def test_existing_csv_scholar_can_export_json_without_rescraping(tmp_path):
    profile = make_profile_for_scholar_a()
    created = execute_scraper_pipeline(url=profile.requested_url, output_dir=tmp_path,
                                       export_format="CSV", scrape_profile_fn=Mock(return_value=profile))
    assert created.status == "SUCCESS"
    ids_before = [(row["scholar_id"], row["google_scholar_id"])
                  for row in load_existing_csvs(tmp_path)["SCHOLARS"]]
    (tmp_path / "json" / "dataset.json").unlink()
    result = execute_scraper_pipeline(url=profile.requested_url, output_dir=tmp_path,
                                      export_format="JSON", scrape_profile_fn=Mock(side_effect=AssertionError("network")))
    assert result.status == "ALREADY_STORED", result.message
    assert json.loads((tmp_path / "json" / "dataset.json").read_text(encoding="utf-8"))["SCHOLARS"][0]["scholar_id"] == ids_before[0][0]


def test_image_validation_and_preservation(tmp_path):
    scholar = {"scholar_id": "7", "google_scholar_id": "TEST7"}
    profile = make_profile_for_scholar_a()
    profile.profile_image_url = "https://scholar.googleusercontent.com/photo"
    profile.profile_image_bytes = b"\x89PNG\r\n\x1a\n" + b"safe image bytes"
    profile.profile_image_content_type = "image/png"
    row, extra = image_record(tmp_path, scholar, profile, [], "2026-11-17")
    assert row["image_status"] == "REAL" and row["local_profile_image_path"].endswith(".png")
    assert image_extension(b"not an image", "image/png") == ""
    path = tmp_path / row["local_profile_image_path"]
    path.parent.mkdir()
    path.write_bytes(extra[row["local_profile_image_path"]])
    profile.profile_image_bytes = b""
    failed_without_prior, _ = image_record(tmp_path, scholar, profile, [], "2026-11-18")
    assert failed_without_prior["image_status"] == "FAILED"
    assert (tmp_path / failed_without_prior["local_profile_image_path"]).is_file()
    retained, extra = image_record(tmp_path, scholar, profile, [row], "2026-11-18")
    assert retained["image_status"] == "PRESERVED" and not extra


def test_delete_synchronizes_json_and_images(tmp_path):
    profile = make_profile_for_scholar_a()
    result = execute_scraper_pipeline(url=profile.requested_url, output_dir=tmp_path,
                                      scrape_profile_fn=Mock(return_value=profile))
    assert result.status == "SUCCESS"
    assert delete_scholar(tmp_path, profile.google_scholar_id)
    assert load_existing_csvs(tmp_path)["SCHOLARS"] == []
    assert load_images(tmp_path) == []
    assert json.loads((tmp_path / "json" / "dataset.json").read_text())["SCHOLARS"] == []


def test_delete_removes_only_owned_real_image(tmp_path):
    profile = make_profile_for_scholar_a()
    profile.profile_image_url = "https://scholar.googleusercontent.com/photo.png"
    profile.profile_image_bytes = b"\x89PNG\r\n\x1a\n" + b"photo"
    profile.profile_image_content_type = "image/png"
    result = execute_scraper_pipeline(url=profile.requested_url, output_dir=tmp_path,
                                      scrape_profile_fn=Mock(return_value=profile))
    assert result.status == "SUCCESS", result.message
    image = load_images(tmp_path)[0]
    path = tmp_path / image["local_profile_image_path"]
    assert path.is_file()
    delete_scholar(tmp_path, profile.google_scholar_id)
    assert not path.exists()
    assert load_images(tmp_path) == []


def test_blocked_profile_writes_no_dataset(tmp_path):
    profile = make_profile_for_scholar_a()
    result = execute_scraper_pipeline(url=profile.requested_url, output_dir=tmp_path,
                                      scrape_profile_fn=Mock(side_effect=AccessBlockedError("HTTP 429")))
    assert result.status == "BLOCKED"
    assert not (tmp_path / "scholars.csv").exists()


def test_profile_image_selector_reads_live_dom_without_detail_navigation():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        try:
            page = browser.new_page()
            page.set_content('<div id="gsc_prf_in">Test Scholar</div>'
                             '<div id="gsc_prf_pup-img" style="background-image:url(https://scholar.googleusercontent.com/photo.png)"></div>')
            header = page.evaluate(JS_HEADER)
            assert header["scholar_name"] == "Test Scholar"
            assert header["profile_image_url"] == "https://scholar.googleusercontent.com/photo.png"
            assert page.url == "about:blank"
        finally:
            browser.close()


def test_failed_export_restores_existing_csv_json_and_registry(tmp_path):
    profile = make_profile_for_scholar_a()
    first = execute_scraper_pipeline(url=profile.requested_url, output_dir=tmp_path,
                                     scrape_profile_fn=Mock(return_value=profile))
    assert first.status == "SUCCESS", first.message
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()
              if path.is_file() and path.name != ".dataset.lock"}
    other = make_profile_for_scholar_a()
    other.google_scholar_id = "DIFFERENT_ID"
    other.requested_url = "https://scholar.google.com/citations?user=DIFFERENT_ID"
    other.header["scholar_name"] = "Other Scholar"
    from scholar_scraper import dataset_ops
    real_replace = dataset_ops.os.replace
    calls = 0
    def fail_mid_commit(source, target):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise PermissionError(13, "locked")
        return real_replace(source, target)
    with patch.object(dataset_ops.os, "replace", side_effect=fail_mid_commit):
        failed = execute_scraper_pipeline(url=other.requested_url, output_dir=tmp_path,
                                          scrape_profile_fn=Mock(return_value=other))
    assert failed.status == "PERMISSION_ERROR"
    assert before == {path.name: path.read_bytes() for path in tmp_path.iterdir()
                      if path.is_file() and path.name != ".dataset.lock"}


def test_gui_worker_and_export_choice(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Tk display unavailable")
    root.withdraw()
    try:
        profile = make_profile_for_scholar_a()
        gui = ScholarScraperGUI(root, output_dir=tmp_path, show_dialogs=False,
                                pipeline_runner=lambda **kwargs: execute_scraper_pipeline(
                                    **kwargs, scrape_profile_fn=Mock(return_value=profile)))
        gui.set_url(profile.requested_url)
        gui.export_format_var.set("JSON")
        assert gui.start_step1_profile_scraping()
        assert gui.is_running
        wait_for_gui_job(gui, root)
        assert gui.last_result is not None, gui.last_notification_text
        assert gui.last_result.status == "SUCCESS", gui.last_result.message
        assert gui.export_status_var.get() == "JSON"
        assert gui.show_more_var.get() == "2"
        assert (tmp_path / "json" / "dataset.json").exists()
        (tmp_path / "json" / "dataset.json").unlink()
        assert gui.start_step1_profile_scraping()
        wait_for_gui_job(gui, root)
        assert gui.last_result.status == "ALREADY_STORED"
        assert (tmp_path / "json" / "dataset.json").exists()
    finally:
        root.destroy()
