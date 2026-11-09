"""Offline deletion and detail completion tests; all writes stay in tmp_path."""
import json
import time
import tkinter as tk
from pathlib import Path
from unittest.mock import patch

import pytest

from run_scraper import execute_scraper_pipeline
from gui_app import ScholarScraperGUI
from tests.test_cross_run_dedup import make_profile_a
from scholar_scraper.dataset_ops import (
    all_checkpoints, completion_state, dataset_lock, delete_scholar, preview_deletion, recover_deletion,
    scoped_detail_path,
)
from scholar_scraper.erd_schema import CSV_FILES, DETAIL_CSV_FILES, DETAIL_ENTITIES, ERD_ENTITIES
from scholar_scraper.id_registry import IdRegistry
from scholar_scraper.reporting import load_existing_csvs, load_existing_detail_csvs, write_all_csvs
from scholar_scraper.validator import validate_dataset

TS = "2026-11-09 10:00:00"


def row(columns, **values):
    data = dict.fromkeys(columns, "")
    data.update(values)
    return data


def fixture_dataset(tmp_path):
    u = lambda suffix: f"https://scholar.google.com/citations?view_op=view_citation&user=A123&citation_for_view=A123:{suffix}"
    tables = {
        "HEIs": [row(ERD_ENTITIES["HEIs"], hei_id="1", hei_name="Shared University", country="Pakistan", target_university="1", created_at=TS)],
        "SCHOLARS": [
            row(ERD_ENTITIES["SCHOLARS"], scholar_id="1", hei_id="1", google_scholar_id="A123", scholar_name="Scholar A", google_scholar_profile_url="https://scholar.google.com/citations?user=A123", h_index="1", total_publications="2", total_citations="0", last_synced_at=TS),
            row(ERD_ENTITIES["SCHOLARS"], scholar_id="2", hei_id="1", google_scholar_id="B123", scholar_name="Scholar B", google_scholar_profile_url="https://scholar.google.com/citations?user=B123", h_index="1", total_publications="2", total_citations="0", last_synced_at=TS),
        ],
        "PUBLICATIONS": [row(ERD_ENTITIES["PUBLICATIONS"], publication_id=str(i), title=title, publication_year="2020", source_url=u(str(i)), last_synced_at=TS) for i, title in [(1, "Shared Work"), (2, "A Work"), (3, "B Work")]],
        "RESEARCH_AREAS": [row(ERD_ENTITIES["RESEARCH_AREAS"], research_area_id=str(i), area_name=name, is_computing_area="1", created_at=TS) for i, name in [(1, "Shared Area"), (2, "A Area")]],
        "Scholar Research Areas": [row(ERD_ENTITIES["Scholar Research Areas"], scholar_id=s, research_area_id=a, is_primary_area="1", updated_at=TS) for s, a in [("1", "1"), ("1", "2"), ("2", "1")]],
        "Authors Publications": [row(ERD_ENTITIES["Authors Publications"], scholar_id=s, publication_id=p, recorded_at=TS) for s, p in [("1", "1"), ("1", "2"), ("2", "1"), ("2", "3")]],
    }
    details = {
        "PUBLICATION_DETAILS": [row(DETAIL_ENTITIES["PUBLICATION_DETAILS"], publication_id=str(i), scholar_id="1", title=title, publication_url=u(str(i)), last_synced_at=TS) for i, title in [(1, "Shared Work"), (2, "A Work")]],
        "PUBLICATION_YEARLY_CITATIONS": [row(DETAIL_ENTITIES["PUBLICATION_YEARLY_CITATIONS"], publication_id=str(i), citation_year="2020", citation_count="1", last_synced_at=TS) for i in (1, 2)],
    }
    assert validate_dataset(tables, detail_tables=details) == []
    write_all_csvs(tmp_path, tables, detail_tables=details)
    registry = IdRegistry(tmp_path / "id_registry.json")
    registry.sync_from_tables(tables)
    registry.save()
    for gs, pid, sid in (("A123", "1", "1"), ("B123", "3", "2")):
        path = scoped_detail_path(tmp_path, gs)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"publications": {pid: {"publication_id": pid, "scholar_id": sid,
                                  "publication_url": u(pid), "status": "SUCCESS", "metadata": {"title": "x"}}}}), encoding="utf-8")
    # A's second publication was successfully extracted as well.
    path = scoped_detail_path(tmp_path, "A123")
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    snapshot["publications"]["2"] = {"publication_id": "2", "scholar_id": "1", "publication_url": u("2"), "status": "SUCCESS", "metadata": {"title": "A Work"}}
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    return tables, details


def bytes_before(path):
    return {str(f.relative_to(path)): f.read_bytes() for f in path.rglob("*") if f.is_file() and ".deletion_transaction" not in str(f) and f.name != ".dataset.lock"}


def test_delete_unique_and_shared_records(tmp_path):
    tables, _ = fixture_dataset(tmp_path)
    before_registry = (tmp_path / "id_registry.json").read_bytes()
    preview = preview_deletion(tmp_path, "A123")
    assert (preview["linked_publications"], preview["research_area_links"], preview["detail_records"], preview["yearly_records"]) == (2, 2, 2, 2)
    result = delete_scholar(tmp_path, "A123")
    assert (result["publications_removed"], result["shared_publications_preserved"], result["orphan_areas_removed"], result["details_removed"], result["yearly_removed"], result["research_area_links_removed"]) == (1, 1, 1, 1, 1, 2)
    assert result["hei_removed"] is False
    remaining = load_existing_csvs(tmp_path)
    detail = load_existing_detail_csvs(tmp_path)
    assert [r["scholar_id"] for r in remaining["SCHOLARS"]] == ["2"]
    assert {r["publication_id"] for r in remaining["PUBLICATIONS"]} == {"1", "3"}
    assert detail["PUBLICATION_DETAILS"][0]["scholar_id"] == "2"
    assert {r["publication_id"] for r in detail["PUBLICATION_YEARLY_CITATIONS"]} == {"1"}
    assert validate_dataset(remaining, detail_tables=detail) == []
    assert (tmp_path / "id_registry.json").read_bytes() == before_registry
    assert {r["publication_id"] for r in remaining["PUBLICATIONS"]} <= {r["publication_id"] for r in tables["PUBLICATIONS"]}
    assert json.loads(scoped_detail_path(tmp_path, "A123").read_text(encoding="utf-8"))["publications"] == {}
    assert "1" in json.loads(scoped_detail_path(tmp_path, "B123").read_text(encoding="utf-8"))["publications"]


def test_unknown_scholar_is_read_only(tmp_path):
    fixture_dataset(tmp_path)
    before = bytes_before(tmp_path)
    assert preview_deletion(tmp_path, "UNKNOWN") is None
    assert delete_scholar(tmp_path, "UNKNOWN") is None
    assert bytes_before(tmp_path) == before


@pytest.mark.parametrize("failure_index", [1, 4, 8])
def test_failed_commit_restores_all_files(tmp_path, failure_index):
    fixture_dataset(tmp_path)
    before = bytes_before(tmp_path)
    from scholar_scraper import dataset_ops
    real_replace = dataset_ops.os.replace
    calls = 0
    def fail_once(src, dst):
        nonlocal calls
        if ".deletion_transaction" in str(src) and "staged" in str(src):
            calls += 1
            if calls == failure_index:
                raise PermissionError(13, "file locked", str(dst))
        return real_replace(src, dst)
    with patch.object(dataset_ops.os, "replace", side_effect=fail_once):
        with pytest.raises(PermissionError):
            delete_scholar(tmp_path, "A123")
    assert bytes_before(tmp_path) == before
    assert not (tmp_path / ".deletion_transaction").exists()


def test_completion_requires_success_checkpoint_and_csv(tmp_path):
    fixture_dataset(tmp_path)
    state = completion_state(tmp_path, "1")
    assert state["complete"] == {"1", "2"}
    path = scoped_detail_path(tmp_path, "A123")
    snap = json.loads(path.read_text(encoding="utf-8"))
    snap["publications"]["2"]["status"] = "BLOCKED"
    path.write_text(json.dumps(snap), encoding="utf-8")
    assert completion_state(tmp_path, "1")["pending"] == {"2"}
    snap["publications"]["2"]["status"] = "FAILED"
    path.write_text(json.dumps(snap), encoding="utf-8")
    assert completion_state(tmp_path, "1")["pending"] == {"2"}
    snap["publications"]["2"]["status"] = "SUCCESS"
    snap["publications"]["2"]["publication_url"] = "https://scholar.google.com/citations?view_op=view_citation&user=wrong&citation_for_view=wrong:2"
    path.write_text(json.dumps(snap), encoding="utf-8")
    assert completion_state(tmp_path, "1")["pending"] == {"2"}


def test_step1_existing_and_step2_complete_skip_network(tmp_path):
    fixture_dataset(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("Unnecessary browser scrape")
    url = "https://scholar.google.com/citations?user=A123"
    result1 = execute_scraper_pipeline(url=url, output_dir=tmp_path, scrape_profile_fn=forbidden)
    assert result1.status == "ALREADY_STORED"
    result2 = execute_scraper_pipeline(url=url, output_dir=tmp_path, scrape_details=True,
                                       scrape_profile_fn=forbidden, scrape_details_fn=forbidden)
    assert result2.status == "ALREADY_COMPLETE"
    assert result2.details_scraped == 2 and result2.pending_publications == 0


def test_step2_existing_resumes_only_pending(tmp_path):
    fixture_dataset(tmp_path)
    path = scoped_detail_path(tmp_path, "A123")
    snap = json.loads(path.read_text(encoding="utf-8"))
    snap["publications"]["2"]["status"] = "BLOCKED"
    path.write_text(json.dumps(snap), encoding="utf-8")
    seen = []
    def details(**kwargs):
        seen.extend(r["publication_id"] for r in kwargs["publications_queue"])
        return {"summary": {"total_unique_publications": 1, "stopped_by_block": True,
                            "block_reason": "HTTP 429"}, "publications": {}}
    result = execute_scraper_pipeline(url="https://scholar.google.com/citations?user=A123",
                                      output_dir=tmp_path, scrape_details=True,
                                      scrape_profile_fn=lambda *a, **k: pytest.fail("Step 1 repeated"),
                                      scrape_details_fn=details)
    assert seen == ["2"]
    assert result.status == "BLOCKED_PARTIAL" and result.details_scraped == 1 and result.pending_publications == 1


def test_checkpoint_isolation(tmp_path):
    fixture_dataset(tmp_path)
    a = scoped_detail_path(tmp_path, "A123")
    b = scoped_detail_path(tmp_path, "B123")
    before_b = b.read_bytes()
    snapshot = json.loads(a.read_text(encoding="utf-8"))
    snapshot["publications"]["2"]["status"] = "PARTIAL"
    a.write_text(json.dumps(snapshot), encoding="utf-8")
    assert b.read_bytes() == before_b
    assert completion_state(tmp_path, "2")["complete"] == {"1"}


def test_headers_and_registry_ids_stable(tmp_path):
    fixture_dataset(tmp_path)
    delete_scholar(tmp_path, "A123")
    for entity, name in CSV_FILES.items():
        header = (tmp_path / name).read_text(encoding="utf-8").splitlines()[0].split(",")
        assert header == ERD_ENTITIES[entity]
    registry = IdRegistry(tmp_path / "id_registry.json")
    assert registry.get_id("SCHOLARS", "A123") == "1"
    assert registry.get_id("SCHOLARS", "B123") == "2"
    assert registry.get_or_create("SCHOLARS", "C123")[0] == "3"


def test_gui_cancel_and_background_delete(tmp_path):
    fixture_dataset(tmp_path)
    root = tk.Tk()
    root.withdraw()
    gui = ScholarScraperGUI(root, output_dir=tmp_path, show_dialogs=False,
                            confirm_delete_fn=lambda _title, _body: False)
    try:
        gui.set_url("https://scholar.google.com/citations?hl=en&user=A123")
        before = bytes_before(tmp_path)
        assert gui.start_delete_scholar() is False
        assert bytes_before(tmp_path) == before
        assert gui.status_var.get() == "Deletion Cancelled"
        gui.confirm_delete_fn = lambda _title, body: "Shared publications" in body
        assert gui.start_delete_scholar() is True
        assert gui.is_running and str(gui.step1_btn["state"]) == "disabled"
        assert str(gui.step2_btn["state"]) == "disabled" and str(gui.delete_btn["state"]) == "disabled"
        deadline = time.monotonic() + 5
        while gui.is_running and time.monotonic() < deadline:
            root.update()
            gui.process_pending_events()
            time.sleep(0.01)
        gui.process_pending_events()
        assert not gui.is_running
        assert gui.status_var.get() == "Scholar Deleted Successfully"
    finally:
        gui._on_close()


def test_step2_zero_details_queues_all(tmp_path):
    tables, _ = fixture_dataset(tmp_path)
    write_all_csvs(tmp_path, tables, detail_tables={k: [] for k in DETAIL_ENTITIES})
    seen = []
    def details(**kwargs):
        seen.extend(r["publication_id"] for r in kwargs["publications_queue"])
        return {"summary": {"total_unique_publications": 2}, "publications": {}}
    result = execute_scraper_pipeline(url="https://scholar.google.com/citations?user=A123",
                                      output_dir=tmp_path, scrape_details=True,
                                      scrape_profile_fn=lambda *a, **k: pytest.fail("Step 1 repeated"),
                                      scrape_details_fn=details)
    assert seen == ["1", "2"]
    assert result.status == "PARTIAL" and result.pending_publications == 2


def test_new_scholar_step1_then_step2(tmp_path):
    profile = make_profile_a([("A New Paper", "2021", "US Khan, X Author")])
    url = profile.requested_url
    calls = []
    def profile_fn(*args, **kwargs):
        calls.append("profile")
        return profile
    step1 = execute_scraper_pipeline(url=url, output_dir=tmp_path,
                                     scrape_profile_fn=profile_fn)
    assert step1.status == "SUCCESS" and calls == ["profile"]
    assert len(load_existing_csvs(tmp_path)["PUBLICATIONS"]) == 1
    # A fresh output directory makes STEP 2 run both stages.
    second = tmp_path / "second"
    def details_fn(**kwargs):
        calls.append("details")
        queue = kwargs["publications_queue"]
        records = {}
        for item in queue:
            pid = item["publication_id"]
            records[pid] = {"publication_id": pid, "scholar_id": item["scholar_id"],
                            "publication_url": item["source_url"], "status": "SUCCESS",
                            "extracted_at": TS,
                            "metadata": {"title": item["title"], "authors_text": "US Khan, X Author",
                                         "publication_url": item["source_url"], "yearly_citations": []}}
        path = kwargs["snapshot_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"publications": records}), encoding="utf-8")
        return {"summary": {"total_unique_publications": len(queue), "succeeded_this_run": len(queue)},
                "publications": records}
    step2 = execute_scraper_pipeline(url=url, output_dir=second, scrape_details=True,
                                     scrape_profile_fn=profile_fn, scrape_details_fn=details_fn)
    assert calls[-2:] == ["profile", "details"]
    assert step2.status == "SUCCESS" and step2.details_scraped == 1 and step2.pending_publications == 0
    assert len(load_existing_detail_csvs(second)["PUBLICATION_DETAILS"]) == 1


def test_orphan_hei_is_removed(tmp_path):
    tables, detail = fixture_dataset(tmp_path)
    tables["HEIs"].append(row(ERD_ENTITIES["HEIs"], hei_id="2", hei_name="B University",
                             country="Pakistan", target_university="0", created_at=TS))
    tables["SCHOLARS"][1]["hei_id"] = "2"
    write_all_csvs(tmp_path, tables, detail_tables=detail)
    result = delete_scholar(tmp_path, "A123")
    assert result["hei_removed"] is True
    assert [r["hei_id"] for r in load_existing_csvs(tmp_path)["HEIs"]] == ["2"]


def test_interrupted_commit_recovers_from_journal(tmp_path):
    fixture_dataset(tmp_path)
    before = bytes_before(tmp_path)
    from scholar_scraper import dataset_ops
    real_replace = dataset_ops.os.replace
    count = 0
    def interrupt(src, dst):
        nonlocal count
        if ".deletion_transaction" in str(src) and "staged" in str(src):
            count += 1
            if count == 3:
                raise KeyboardInterrupt()
        return real_replace(src, dst)
    with patch.object(dataset_ops.os, "replace", side_effect=interrupt):
        with pytest.raises(KeyboardInterrupt):
            delete_scholar(tmp_path, "A123")
    assert (tmp_path / ".deletion_transaction" / "manifest.json").exists()
    with dataset_lock(tmp_path):
        recover_deletion(tmp_path)
    assert bytes_before(tmp_path) == before


def test_detail_provenance_requires_link(tmp_path):
    tables, detail = fixture_dataset(tmp_path)
    detail["PUBLICATION_DETAILS"][1]["scholar_id"] = "2"
    assert any("Provenance" in error for error in validate_dataset(tables, detail_tables=detail))


def test_gui_already_complete_message(tmp_path):
    fixture_dataset(tmp_path)
    root = tk.Tk()
    root.withdraw()
    gui = ScholarScraperGUI(root, output_dir=tmp_path, show_dialogs=False)
    try:
        gui.set_url("https://scholar.google.com/citations?user=A123")
        assert gui.start_step2_full_scraping()
        deadline = time.monotonic() + 5
        while gui.is_running and time.monotonic() < deadline:
            root.update()
            gui.process_pending_events()
            time.sleep(0.01)
        gui.process_pending_events()
        assert gui.status_var.get() == "Publication Detail Scraping Already Complete"
        assert gui.details_scraped_var.get() == "2"
    finally:
        gui._on_close()


def test_partial_resume_completes_only_missing_detail(tmp_path):
    fixture_dataset(tmp_path)
    path = scoped_detail_path(tmp_path, "A123")
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    snapshot["publications"]["2"]["status"] = "FAILED"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    requested = []
    def details(**kwargs):
        items = kwargs["publications_queue"]
        requested.extend(item["publication_id"] for item in items)
        item = items[0]
        record = {"publication_id": "2", "scholar_id": "1", "status": "SUCCESS",
                  "publication_url": item["source_url"], "extracted_at": TS,
                  "metadata": {"title": item["title"], "authors_text": "Scholar A",
                               "publication_url": item["source_url"], "yearly_citations": []}}
        snapshot["publications"]["2"] = record
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        return {"summary": {"total_unique_publications": 1, "succeeded_this_run": 1},
                "publications": {"2": record}}
    args = dict(url="https://scholar.google.com/citations?user=A123", output_dir=tmp_path,
                scrape_details=True, scrape_profile_fn=lambda *a, **k: pytest.fail("Step 1 repeated"))
    first = execute_scraper_pipeline(**args, scrape_details_fn=details)
    assert requested == ["2"]
    assert first.status == "SUCCESS" and first.details_scraped == 2 and first.pending_publications == 0
    second = execute_scraper_pipeline(**args, scrape_details_fn=lambda **k: pytest.fail("Completed detail repeated"))
    assert second.status == "ALREADY_COMPLETE"


def test_deletion_cleans_only_target_raw_snapshots(tmp_path):
    fixture_dataset(tmp_path)
    sup = tmp_path / "supplementary"
    profiles = sup / "profiles"
    profiles.mkdir()
    (profiles / "A123.json").write_text(json.dumps({"google_scholar_id": "A123", "name": "Scholar A"}), encoding="utf-8")
    (profiles / "B123.json").write_text(json.dumps({"google_scholar_id": "B123", "name": "Scholar B"}), encoding="utf-8")
    (sup / "raw_profile_snapshot.json").write_text(json.dumps({"google_scholar_id": "B123"}), encoding="utf-8")
    before_b = (profiles / "B123.json").read_bytes()
    before_legacy = (sup / "raw_profile_snapshot.json").read_bytes()
    delete_scholar(tmp_path, "A123")
    assert json.loads((profiles / "A123.json").read_text(encoding="utf-8")) == {}
    assert (profiles / "B123.json").read_bytes() == before_b
    assert (sup / "raw_profile_snapshot.json").read_bytes() == before_legacy


def test_shared_publication_uses_successful_other_scholar_url(tmp_path):
    tables, details = fixture_dataset(tmp_path)
    tables["PUBLICATIONS"][0]["source_url"] = (
        "https://scholar.google.com/citations?view_op=view_citation&user=B123&citation_for_view=B123:shared")
    write_all_csvs(tmp_path, tables, detail_tables=details)
    a = scoped_detail_path(tmp_path, "A123")
    snapshot = json.loads(a.read_text(encoding="utf-8"))
    snapshot["publications"]["1"]["metadata"]["title"] = "Shared Work"
    a.write_text(json.dumps(snapshot), encoding="utf-8")
    state = completion_state(tmp_path, "2")
    assert "1" in state["complete"]
