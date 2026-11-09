"""Offline coverage for discipline-independent profile transformation."""

import csv
from copy import deepcopy

import pytest

from scholar_scraper.browser import ProfileData
from scholar_scraper.dataset_ops import delete_scholar, scoped_detail_path
from scholar_scraper.erd_schema import CSV_FILES, ERD_ENTITIES
from scholar_scraper.id_registry import IdRegistry
from scholar_scraper.reporting import load_existing_csvs, write_all_csvs
from scholar_scraper.transform import apply_publication_details, transform
from scholar_scraper.validator import validate_dataset


TS1 = "2026-11-09 10:00:00"
TS2 = "2026-11-09 11:00:00"


def profile(gs_id="DISCIPLINE1", interests=None, affiliation="Example University"):
    data = ProfileData(
        requested_url=f"https://scholar.google.com/citations?hl=en&user={gs_id}",
        loaded_url=f"https://scholar.google.com/citations?hl=en&user={gs_id}",
        google_scholar_id=gs_id,
        http_status=200,
        listing_complete=True,
    )
    data.header = {
        "scholar_name": f"Scholar {gs_id}",
        "affiliation_text": affiliation,
        "verification_text": "",
        "header_text": f"Scholar {gs_id}",
        "interests": list(interests or []),
    }
    data.metrics = {"Citations": {"All": "12"}, "h-index": {"All": "2"}}
    data.publications = [{
        "row_index": 1,
        "title": f"A study by {gs_id}",
        "authors": f"Scholar {gs_id}",
        "venue": "Journal",
        "year": "2024",
        "cited_by": "1",
        "source_url": (
            "https://scholar.google.com/citations?view_op=view_citation"
            f"&user={gs_id}&citation_for_view={gs_id}:abc"
        ),
    }]
    return data


@pytest.mark.parametrize("area", [
    "Computer Science",
    "Medicine",
    "Business Administration",
    "Quantum Physics",
    "Medieval Humanities",
])
def test_disciplines_are_accepted_without_taxonomy(area):
    result = transform(profile(interests=[area]), TS1)
    assert not result.validation_errors
    assert result.tables["RESEARCH_AREAS"][0]["area_name"] == area
    assert result.tables["RESEARCH_AREAS"][0]["is_computing_area"] == ""


def test_unknown_research_area_is_stored_not_rejected():
    result = transform(profile(interests=["Uncatalogued Interdisciplinary Field"]), TS1)
    assert not result.unresolved_errors
    assert [r["area_name"] for r in result.tables["RESEARCH_AREAS"]] == [
        "Uncatalogued Interdisciplinary Field"
    ]


def test_zero_research_areas_is_valid():
    result = transform(profile(interests=[]), TS1)
    assert result.tables["RESEARCH_AREAS"] == []
    assert result.tables["Scholar Research Areas"] == []
    assert not result.validation_errors


def test_multiple_areas_all_link_without_primary_guess():
    result = transform(profile(interests=["Medicine", "Bioethics", "History"]), TS1)
    assert len(result.tables["RESEARCH_AREAS"]) == 3
    assert len(result.tables["Scholar Research Areas"]) == 3
    assert {r["is_primary_area"] for r in result.tables["Scholar Research Areas"]} == {"0"}
    assert {r["area_source"] for r in result.tables["Scholar Research Areas"]} == {
        "Google Scholar profile"
    }


def test_duplicate_area_normalization_reuses_id(tmp_path):
    registry = IdRegistry(tmp_path / "id_registry.json")
    first = transform(profile("A1", ["Health Economics"]), TS1, registry=registry)
    write_all_csvs(tmp_path, first.tables)
    registry.save()
    second = transform(
        profile("B1", ["  HEALTH   ECONOMICS  "]),
        TS2,
        registry=IdRegistry(tmp_path / "id_registry.json"),
        existing_tables=load_existing_csvs(tmp_path),
    )
    assert len(second.tables["RESEARCH_AREAS"]) == 1
    assert second.tables["RESEARCH_AREAS"][0]["research_area_id"] == first.tables["RESEARCH_AREAS"][0]["research_area_id"]
    assert len(second.tables["Scholar Research Areas"]) == 2


def test_existing_scholar_and_area_ids_remain_stable(tmp_path):
    registry_path = tmp_path / "id_registry.json"
    first = transform(profile("STABLE", ["Robotics"]), TS1, registry=IdRegistry(registry_path))
    write_all_csvs(tmp_path, first.tables)
    first.registry.save()
    ids = (
        first.tables["SCHOLARS"][0]["scholar_id"],
        first.tables["RESEARCH_AREAS"][0]["research_area_id"],
    )
    rerun = transform(
        profile("STABLE", ["Robotics", "Medicine"]),
        TS2,
        registry=IdRegistry(registry_path),
        existing_tables=load_existing_csvs(tmp_path),
        clean_target_scholars=True,
    )
    assert rerun.tables["SCHOLARS"][0]["scholar_id"] == ids[0]
    assert next(r for r in rerun.tables["RESEARCH_AREAS"] if r["area_name"] == "Robotics")["research_area_id"] == ids[1]


def test_unknown_hei_is_source_observed_and_not_mapped_to_catalog():
    result = transform(
        profile(interests=["Medicine"], affiliation="Starlight Medical Research Centre"), TS1
    )
    hei = result.tables["HEIs"][0]
    assert hei["hei_name"] == "Starlight Medical Research Centre"
    assert hei["hei_name"] != "Hamdard University"
    assert hei["country"] == hei["city"] == hei["website_url"] == ""
    assert hei["target_university"] == "0"
    assert not result.validation_errors


def test_missing_affiliation_does_not_reject_scholar():
    result = transform(profile(interests=["Physics"], affiliation=""), TS1)
    assert result.tables["HEIs"] == []
    assert result.tables["SCHOLARS"][0]["hei_id"] == ""
    assert not result.unresolved_errors
    assert not result.validation_errors


def test_existing_rows_remain_byte_for_byte_values_when_adding_scholar(tmp_path):
    first = transform(profile("KEEP", ["Robotics"], "Hamdard University"), TS1)
    write_all_csvs(tmp_path, first.tables)
    before = deepcopy(load_existing_csvs(tmp_path))
    second = transform(
        profile("ADD", ["Medicine"], "Uncatalogued Hospital"),
        TS2,
        registry=first.registry,
        existing_tables=before,
    )
    old_scholar = next(r for r in second.tables["SCHOLARS"] if r["google_scholar_id"] == "KEEP")
    assert old_scholar == before["SCHOLARS"][0]


def test_step1_transformation_has_no_taxonomy_dependency():
    result = transform(
        profile("STEP1", ["Clinical Medicine", "Macroeconomics", "Poetry"]),
        TS1,
        area_taxonomy={},
    )
    assert len(result.tables["PUBLICATIONS"]) == 1
    assert len(result.tables["RESEARCH_AREAS"]) == 3
    assert not result.validation_errors


def test_step2_detail_merge_has_no_taxonomy_dependency():
    result = transform(profile("STEP2", ["Medicine"]), TS1)
    publication = result.tables["PUBLICATIONS"][0]
    pid = publication["publication_id"]
    detail = {
        "publications": {pid: {
            "status": "SUCCESS",
            "publication_id": pid,
            "scholar_id": result.tables["SCHOLARS"][0]["scholar_id"],
            "publication_url": publication["source_url"],
            "extracted_at": TS2,
            "metadata": {"title": publication["title"], "authors_text": publication["authors_text"]},
        }},
        "summary": {"total_unique_publications": 1, "succeeded_this_run": 1},
    }
    apply_publication_details(result, detail)
    assert len(result.detail_tables["PUBLICATION_DETAILS"]) == 1
    assert not result.validation_errors


def test_no_duplicate_bridge_keys():
    result = transform(profile(interests=["Medicine", " medicine ", "Physics"]), TS1)
    keys = [(r["scholar_id"], r["research_area_id"]) for r in result.tables["Scholar Research Areas"]]
    assert len(keys) == len(set(keys)) == 2


def test_all_foreign_keys_validate():
    result = transform(profile(interests=["Humanities", "Unknown Field"]), TS1)
    assert validate_dataset(result.tables) == []


def test_six_core_headers_remain_exact(tmp_path):
    result = transform(profile(interests=["Physics"]), TS1)
    write_all_csvs(tmp_path, result.tables)
    for entity, filename in CSV_FILES.items():
        with open(tmp_path / filename, encoding="utf-8", newline="") as stream:
            assert next(csv.reader(stream)) == ERD_ENTITIES[entity]


def test_deletion_and_checkpoint_remain_compatible(tmp_path):
    result = transform(profile("DELETE1", ["Medicine"]), TS1)
    write_all_csvs(tmp_path, result.tables)
    registry = result.registry
    registry.path = tmp_path / "id_registry.json"
    registry.save()
    checkpoint = scoped_detail_path(tmp_path, "DELETE1")
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text('{"publications": {}}', encoding="utf-8")
    deleted = delete_scholar(tmp_path, "DELETE1")
    assert deleted["scholar"]["google_scholar_id"] == "DELETE1"
    assert load_existing_csvs(tmp_path)["SCHOLARS"] == []
    assert scoped_detail_path(tmp_path, "DELETE1").exists()
