"""
OFFLINE pipeline test suite (no network, no live Google Scholar requests).

Verifies all 14 required database-readiness, primary/foreign key, deduplication,
ID stability, validation, and scope-enforcement test cases using synthetic
ProfileData fixtures in tests/_tmp_output.

Run:  python tests/test_pipeline_offline.py
"""

import csv
import inspect
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scholar_scraper import browser as browser_mod  # noqa: E402
from scholar_scraper.browser import ProfileData  # noqa: E402
from scholar_scraper.docx_schema import validate_against_docx  # noqa: E402
from scholar_scraper.erd_schema import CSV_FILES, ERD_ENTITIES  # noqa: E402
from scholar_scraper.id_registry import (  # noqa: E402
    IdRegistry,
    RegistryCollisionError,
)
from scholar_scraper.reporting import (  # noqa: E402
    assess,
    build_quality_report,
    build_report,
    write_all_csvs,
)
from scholar_scraper.transform import transform, transform_profiles  # noqa: E402
from scholar_scraper.validator import DatasetValidationError, validate_dataset  # noqa: E402

DOCX = ROOT / "Research_Companion_ERD_Listing_Exact_Names_V2.docx"
TMP = Path(__file__).resolve().parent / "_tmp_output"
TS = "2026-11-07 09:00:00"


def make_scholar_1() -> ProfileData:
    d = ProfileData(
        requested_url="https://scholar.google.com/citations?hl=en&user=SCHOLAR_ONE",
        google_scholar_id="SCHOLAR_ONE",
    )
    d.loaded_url = d.requested_url
    d.http_status = 200
    d.listing_complete = True
    d.navigations = [d.requested_url]
    d.header = {
        "scholar_name": "Prof. Dr. Test One",
        "affiliation_text": "Campus Director, Hamdard University Islamabad Campus",
        "verification_text": "Verified email at hamdard.edu.pk",
        "header_text": "Prof. Dr. Test One Verified email at hamdard.edu.pk",
        "interests": ["Machine Learning", "machine learning", "Robotics", "Embedded Systems"],
    }
    d.metrics = {"Citations": {"All": "1,234"}, "h-index": {"All": "12"}}

    def row(i, title, authors, venue, year, cited, key):
        return {
            "row_index": i,
            "title": title,
            "authors": authors,
            "venue": venue,
            "year": year,
            "cited_by": cited,
            "source_url": (
                f"https://scholar.google.com/citations?view_op=view_citation"
                f"&citation_for_view=SCHOLAR_ONE:{key}"
            ),
        }

    d.publications = [
        row(1, "Shared Paper on Robotics", "T One, S Two", "Journal of Robotics 10, 1-12", "2022", "25", "p1"),
        # Duplicate of row 1 on the same profile (even with different citation key)
        row(2, "Shared Paper on Robotics!", "T One, S Two", "Journal of Robotics 10, 1-12", "2022", "25", "p1_dup"),
        # Truncated venue and truncated author list, missing year/citations
        row(
            3,
            "Deep Learning for Edge Devices",
            "B Other, C Third, D Fourth ...",
            "Proceedings of the International Conference on Embedded ...",
            "",
            "",
            "p2",
        ),
    ]
    return d


def make_scholar_2() -> ProfileData:
    d = ProfileData(
        requested_url="https://scholar.google.com/citations?hl=en&user=SCHOLAR_TWO",
        google_scholar_id="SCHOLAR_TWO",
    )
    d.loaded_url = d.requested_url
    d.http_status = 200
    d.listing_complete = True
    d.navigations = [d.requested_url]
    d.header = {
        "scholar_name": "Dr. Second Scholar",
        # Spelling/format variation of the same canonical institution (Hamdard University)
        "affiliation_text": "Associate Professor, Department of Computing, Hamdard Univ",
        "verification_text": "Verified email at hamdard.edu.pk",
        "header_text": "Dr. Second Scholar Verified email at hamdard.edu.pk",
        "interests": ["Robotics", "Computer Vision"],
    }
    d.metrics = {"Citations": {"All": "450"}, "h-index": {"All": "9"}}

    def row(i, title, authors, venue, year, cited, key):
        return {
            "row_index": i,
            "title": title,
            "authors": authors,
            "venue": venue,
            "year": year,
            "cited_by": cited,
            "source_url": (
                f"https://scholar.google.com/citations?view_op=view_citation"
                f"&citation_for_view=SCHOLAR_TWO:{key}"
            ),
        }

    d.publications = [
        # Shared publication with Scholar 1 (different profile citation_for_view key, same title + year)
        row(1, "Shared Paper on Robotics", "T One, S Scholar", "Journal of Robotics 10, 1-12", "2022", "25", "s2_p1"),
        row(2, "Vision-Based Autonomous Navigation", "S Scholar, X Yan", "IEEE Access 11, 500-512", "2023", "14", "s2_p2"),
    ]
    return d


def main() -> int:
    ok = True

    def check(name: str, cond: bool):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok &= bool(cond)

    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True, exist_ok=True)

    # 0. Verify DOCX schema match for all 6 entities
    res, _ = validate_against_docx(DOCX)
    check("0. DOCX headers match built-in schema for all 6 entities", res is True and len(CSV_FILES) == 6)

    # ----------------------------------------------------------------------- #
    # Test Cases 1, 2, 6, 8, 11, 13 on Scholar 1
    # ----------------------------------------------------------------------- #
    reg_path = TMP / "id_registry.json"
    reg1 = IdRegistry(reg_path)
    t1 = transform(make_scholar_1(), TS, registry=reg1)
    write_all_csvs(TMP, t1.tables, unresolved_errors=t1.unresolved_errors)
    reg1.save()

    s1_id = t1.tables["SCHOLARS"][0]["scholar_id"]
    h1_id = t1.tables["HEIs"][0]["hei_id"]
    s1_pub_ids = [r["publication_id"] for r in t1.tables["PUBLICATIONS"]]
    s1_area_ids = [r["research_area_id"] for r in t1.tables["RESEARCH_AREAS"]]

    # Test 1: One scholar linked to multiple research areas
    check(
        "1. One scholar linked to multiple research areas",
        len(t1.tables["RESEARCH_AREAS"]) == 3
        and len(t1.tables["Scholar Research Areas"]) == 3
        and all(r["scholar_id"] == s1_id for r in t1.tables["Scholar Research Areas"])
        and [r["research_area_id"] for r in t1.tables["Scholar Research Areas"]] == s1_area_ids
        and all(r["is_primary_area"] == "0" for r in t1.tables["Scholar Research Areas"]),
    )

    # Test 2: One scholar linked to multiple publications
    check(
        "2. One scholar linked to multiple publications",
        len(t1.tables["PUBLICATIONS"]) == 2
        and len(t1.tables["Authors Publications"]) == 2
        and all(r["scholar_id"] == s1_id for r in t1.tables["Authors Publications"])
        and [r["publication_id"] for r in t1.tables["Authors Publications"]] == s1_pub_ids,
    )

    # Test 6: Re-running the scraper preserves all existing IDs
    first_Export_bytes = {f: (TMP / f).read_bytes() for f in CSV_FILES.values()}
    reg1_reload = IdRegistry(reg_path)
    t1_rerun = transform(make_scholar_1(), TS, registry=reg1_reload)
    write_all_csvs(TMP, t1_rerun.tables, unresolved_errors=t1_rerun.unresolved_errors)
    reg1_reload.save()
    second_export_bytes = {f: (TMP / f).read_bytes() for f in CSV_FILES.values()}
    check(
        "6. Re-running the scraper preserves all existing IDs and produces identical CSVs",
        first_Export_bytes == second_export_bytes
        and t1_rerun.tables["SCHOLARS"][0]["scholar_id"] == s1_id
        and [r["publication_id"] for r in t1_rerun.tables["PUBLICATIONS"]] == s1_pub_ids
        and [r["research_area_id"] for r in t1_rerun.tables["RESEARCH_AREAS"]] == s1_area_ids,
    )

    # Test 7: Adding a new scholar does not change existing IDs
    reg_multi = IdRegistry(reg_path)
    t_multi = transform_profiles(
        [make_scholar_1(), make_scholar_2()],
        TS,
        registry=reg_multi,
    )
    write_all_csvs(TMP, t_multi.tables, unresolved_errors=t_multi.unresolved_errors)
    reg_multi.save()

    sch_by_gs = {r["google_scholar_id"]: r for r in t_multi.tables["SCHOLARS"]}
    pubs_by_title = {r["title"]: r for r in t_multi.tables["PUBLICATIONS"]}
    areas_by_name = {r["area_name"]: r for r in t_multi.tables["RESEARCH_AREAS"]}

    check(
        "7. Adding a new scholar does not change existing IDs",
        sch_by_gs["SCHOLAR_ONE"]["scholar_id"] == s1_id
        and sch_by_gs["SCHOLAR_TWO"]["scholar_id"] != s1_id
        and t_multi.tables["HEIs"][0]["hei_id"] == h1_id
        and pubs_by_title["Shared Paper on Robotics"]["publication_id"] == s1_pub_ids[0]
        and pubs_by_title["Deep Learning for Edge Devices"]["publication_id"] == s1_pub_ids[1]
        and areas_by_name["Machine Learning"]["research_area_id"] == s1_area_ids[0]
        and areas_by_name["Robotics"]["research_area_id"] == s1_area_ids[1]
        and areas_by_name["Embedded Systems"]["research_area_id"] == s1_area_ids[2],
    )

    # Test 3: Two scholars sharing the same publication
    shared_pub_id = pubs_by_title["Shared Paper on Robotics"]["publication_id"]
    ap_for_shared = [
        r for r in t_multi.tables["Authors Publications"] if r["publication_id"] == shared_pub_id
    ]
    check(
        "3. Two scholars sharing the same publication share publication_id and appear once in publications.csv",
        len(t_multi.tables["PUBLICATIONS"]) == 3
        and len(ap_for_shared) == 2
        and {r["scholar_id"] for r in ap_for_shared}
        == {sch_by_gs["SCHOLAR_ONE"]["scholar_id"], sch_by_gs["SCHOLAR_TWO"]["scholar_id"]}
        and {r["author_order"] for r in ap_for_shared} == {"1", "2"},
    )

    # Test 4: Two scholars belonging to the same HEI
    check(
        "4. Two scholars belonging to the same HEI map to a single canonical HEI row and share hei_id",
        len(t_multi.tables["HEIs"]) == 1
        and t_multi.tables["HEIs"][0]["hei_name"] == "Hamdard University"
        and t_multi.tables["HEIs"][0]["city"] == "Islamabad"
        and t_multi.tables["HEIs"][0]["country"] == "Pakistan"
        and sch_by_gs["SCHOLAR_ONE"]["hei_id"] == h1_id
        and sch_by_gs["SCHOLAR_TWO"]["hei_id"] == h1_id,
    )

    # Test 5: Same research area appearing for multiple scholars
    robotics_id = areas_by_name["Robotics"]["research_area_id"]
    sra_for_robotics = [
        r for r in t_multi.tables["Scholar Research Areas"] if r["research_area_id"] == robotics_id
    ]
    check(
        "5. Same research area appearing for multiple scholars receives one research_area_id and links to both",
        len(t_multi.tables["RESEARCH_AREAS"]) == 4
        and len(sra_for_robotics) == 2
        and {r["scholar_id"] for r in sra_for_robotics}
        == {sch_by_gs["SCHOLAR_ONE"]["scholar_id"], sch_by_gs["SCHOLAR_TWO"]["scholar_id"]},
    )

    # Test 8: Duplicate publication title/year does not create duplicate publication records
    check(
        "8. Duplicate publication title/year does not create duplicate publication records",
        t1.stats["duplicates"] == 1
        and t_multi.stats["shared_publications_merged"] == 1
        and len({r["publication_id"] for r in t_multi.tables["PUBLICATIONS"]}) == len(t_multi.tables["PUBLICATIONS"]),
    )

    # Test 9: No duplicate composite keys in bridge tables
    sra_comp_keys = [(r["scholar_id"], r["research_area_id"]) for r in t_multi.tables["Scholar Research Areas"]]
    ap_comp_keys = [(r["scholar_id"], r["publication_id"]) for r in t_multi.tables["Authors Publications"]]
    check(
        "9. No duplicate composite keys in bridge tables",
        len(sra_comp_keys) == len(set(sra_comp_keys)) == 5
        and len(ap_comp_keys) == len(set(ap_comp_keys)) == 4
        and all(k[0] and k[1] for k in sra_comp_keys)
        and all(k[0] and k[1] for k in ap_comp_keys),
    )

    # Test 10: Every foreign key resolves to an existing parent record (and all CSV headers match DOCX)
    val_errors = validate_dataset(t_multi.tables, unresolved_errors=t_multi.unresolved_errors)
    headers_ok = True
    for ent, fname in CSV_FILES.items():
        with open(TMP / fname, encoding="utf-8", newline="") as f:
            header = next(csv.reader(f))
        headers_ok &= header == ERD_ENTITIES[ent]
    check(
        "10. Every foreign key resolves to an existing parent record and all 6 CSV headers match DOCX",
        len(val_errors) == 0 and headers_ok,
    )

    # Test 11: Missing optional email and DOI remain empty
    check(
        "11. Missing optional email and DOI remain empty",
        all(r["email"] == "" for r in t_multi.tables["SCHOLARS"])
        and all(r["digital_object_identifier"] == "" for r in t_multi.tables["PUBLICATIONS"])
        and pubs_by_title["Deep Learning for Edge Devices"]["publication_year"] == ""
        and pubs_by_title["Deep Learning for Edge Devices"]["citation_count"] == "",
    )

    # Test 12: Missing/uncatalogued affiliations are accepted without invention,
    # while the independent ID-collision guard remains active.
    missing_profile = make_scholar_1()
    missing_profile.header = dict(missing_profile.header, affiliation_text="")
    t_missing = transform(missing_profile, TS)
    unknown_profile = make_scholar_1()
    unknown_profile.google_scholar_id = "UNKNOWN_HEI"
    unknown_profile.header = dict(
        unknown_profile.header,
        affiliation_text="Uncatalogued International Research Centre",
    )
    t_unknown = transform(unknown_profile, TS)

    # ID collision in registry must still raise RegistryCollisionError.
    collision_raised = False
    try:
        bad_reg = IdRegistry()
        bad_reg._data["SCHOLARS"]["entries"] = {
            "user_A": {"id": 1, "created_at": TS, "label": "A"},
            "user_B": {"id": 1, "created_at": TS, "label": "B"},
        }
        bad_reg._validate_and_rebuild_index()
    except RegistryCollisionError:
        collision_raised = True

    check(
        "12. Missing/uncatalogued affiliations are accepted without invented metadata; ID collisions still fail",
        not t_missing.validation_errors
        and t_missing.tables["SCHOLARS"][0]["hei_id"] == ""
        and not t_unknown.validation_errors
        and t_unknown.tables["HEIs"][0]["hei_name"] == "Uncatalogued International Research Centre"
        and t_unknown.tables["HEIs"][0]["country"] == ""
        and collision_raised,
    )

    # Test 13: Truncated front-page venue/author values are preserved without guessing
    edge_pub = pubs_by_title["Deep Learning for Edge Devices"]
    ap_edge = [
        r for r in t1.tables["Authors Publications"] if r["publication_id"] == edge_pub["publication_id"]
    ][0]
    q_rep = build_quality_report(
        make_scholar_1(),
        t1,
        (True, ["All 6 entities match DOCX"]),
        {"timestamp_display": TS, "browser_mode": "offline_test", "errors": []},
    )
    std_rep = build_report(
        make_scholar_1(),
        t1,
        assess(make_scholar_1(), t1),
        (True, ["All 6 entities match DOCX"]),
        {"timestamp_display": TS, "browser_mode": "offline_test", "errors": []},
    )
    check(
        "13. Truncated front-page venue/author values are preserved without guessing and reported",
        edge_pub["authors_text"].endswith("...")
        and edge_pub["venue"].endswith("...")
        and ap_edge["author_order"] == ""
        and "Truncated Publication Venues (1 of 2" in q_rep
        and "Truncated Author Lists (1 of 2" in q_rep
        and "CATEGORY 1: DATA UNAVAILABLE ON GOOGLE SCHOLAR FRONT PAGE" in q_rep
        and "CATEGORY 2: DATA EXTRACTION / PARSING FAILURES" in q_rep
        and "CATEGORY 3: DATA REQUIRING MANUAL REVIEW" in q_rep
        and "A. SUCCESSFULLY" in std_rep,
    )

    # Test 14: No publication detail page is opened
    scrape_src = inspect.getsource(browser_mod._scrape)
    goto_calls = [
        line.strip()
        for line in scrape_src.splitlines()
        if "page.goto(" in line and not line.strip().startswith("#")
    ]
    check(
        "14. No publication detail page is opened (single profile page.goto in _scrape, 0 citation navigations)",
        len(goto_calls) == 1
        and all("view_citation" not in u for u in make_scholar_1().navigations)
        and "Publication links opened                : 0" in std_rep,
    )

    print("\nALL 14+ OFFLINE TESTS PASSED" if ok else "\nFAILURES DETECTED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
