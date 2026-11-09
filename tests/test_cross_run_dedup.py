"""
Offline unit tests for CSV-based cross-run duplicate detection, scholar-scoped
clean re-scraping, multi-scholar shared record preservation, and atomic staging
export protection.

Does not contact Google Scholar or perform live network requests.

Run:  python tests/test_cross_run_dedup.py
"""

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scholar_scraper.browser import ProfileData  # noqa: E402
from scholar_scraper.id_registry import IdRegistry  # noqa: E402
from scholar_scraper.reporting import load_existing_csvs, write_all_csvs  # noqa: E402
from scholar_scraper.transform import clean_scholar_from_tables, transform  # noqa: E402
from scholar_scraper.validator import DatasetValidationError, validate_dataset  # noqa: E402

TMP = Path(__file__).resolve().parent / "_tmp_cross_run"
TS1 = "2026-11-07 10:00:00"
TS2 = "2026-11-07 15:00:00"


def make_profile_a(
    pubs: list[tuple[str, str, str]],
    interests: list[str] | None = None,
    affiliation: str = "Campus Director, Hamdard University Islamabad Campus",
) -> ProfileData:
    d = ProfileData(
        requested_url="https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ",
        google_scholar_id="vdh-mOQAAAAJ",
        loaded_url="https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ",
        http_status=200,
        listing_complete=True,
    )
    d.header = {
        "scholar_name": "Prof. Dr. Umar Shahbaz Khan",
        "affiliation_text": affiliation,
        "verification_text": "Verified email at ceme.nust.edu.pk",
        "header_text": "Prof. Dr. Umar Shahbaz Khan",
        "interests": interests if interests is not None else ["Embedded Systems", "Robotics"],
    }
    d.metrics = {"Citations": {"All": "2,375"}, "h-index": {"All": "28"}}
    d.publications = [
        {
            "row_index": idx,
            "title": title,
            "authors": authors,
            "venue": "IEEE Access",
            "year": year,
            "cited_by": "42",
            "source_url": f"https://scholar.google.com/citations?view_op=view_citation&citation_for_view=vdh-mOQAAAAJ:p{idx}",
        }
        for idx, (title, year, authors) in enumerate(pubs, 1)
    ]
    return d


def make_profile_b() -> ProfileData:
    d = ProfileData(
        requested_url="https://scholar.google.com/citations?hl=en&user=COLLAB_SCHOLAR",
        google_scholar_id="COLLAB_SCHOLAR",
        loaded_url="https://scholar.google.com/citations?hl=en&user=COLLAB_SCHOLAR",
        http_status=200,
        listing_complete=True,
    )
    d.header = {
        "scholar_name": "Dr. Collaborator Two",
        "affiliation_text": "Professor, Hamdard University",
        "verification_text": "Verified email at hamdard.edu.pk",
        "header_text": "Dr. Collaborator Two",
        "interests": ["Robotics", "Computer Vision"],
    }
    d.metrics = {"Citations": {"All": "800"}, "h-index": {"All": "15"}}
    d.publications = [
        {
            "row_index": 1,
            # Shared publication with Profile A (with capitalization & whitespace differences!)
            "title": "  advancements,   trends and future prospects of lower limb prosthesis ",
            "authors": "M Asif, C Two, US Khan",
            "venue": "IEEE Access",
            "year": "2021",
            "cited_by": "136",
            "source_url": "https://scholar.google.com/citations?view_op=view_citation&citation_for_view=COLLAB_SCHOLAR:shared1",
        },
        {
            "row_index": 2,
            "title": "Exclusive Paper by Collaborator Two",
            "authors": "C Two, X Author",
            "venue": "Sensors",
            "year": "2023",
            "cited_by": "19",
            "source_url": "https://scholar.google.com/citations?view_op=view_citation&citation_for_view=COLLAB_SCHOLAR:excl2",
        },
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

    reg_path = TMP / "id_registry.json"

    # ----------------------------------------------------------------------- #
    # Initial Run 1 for Scholar A (includes an in-scrape duplicate on row 3)
    # ----------------------------------------------------------------------- #
    reg1 = IdRegistry(reg_path)
    prof_a_run1 = make_profile_a(
        pubs=[
            ("Advancements, trends and future prospects of lower limb prosthesis", "2021", "M Asif, MI Tiwana, US Khan"),
            ("Background removal from GPR data using eigenvalues", "2010", "US Khan, W Al-Nuaimy"),
            # Duplicate within same scrape (case & whitespace variation)
            ("  ADVANCEMENTS, TRENDS   and future prospects of lower limb prosthesis ", "2021", "M Asif, MI Tiwana, US Khan"),
        ]
    )
    t1 = transform(prof_a_run1, TS1, registry=reg1, existing_tables=load_existing_csvs(TMP), clean_target_scholars=True)
    write_all_csvs(TMP, t1.tables, unresolved_errors=t1.unresolved_errors)
    reg1.save()

    check(
        "5. Duplicate publication entries within one scrape are removed",
        len(t1.tables["PUBLICATIONS"]) == 2
        and t1.stats["duplicates"] == 1
        and len(t1.stats["duplicate_publications_details"]) == 1,
    )

    # ----------------------------------------------------------------------- #
    # Run 2: Add Scholar B against existing CSVs on disk
    # ----------------------------------------------------------------------- #
    existing_after_r1 = load_existing_csvs(TMP)
    reg2 = IdRegistry(reg_path)
    prof_b = make_profile_b()
    t2 = transform(prof_b, TS1, registry=reg2, existing_tables=existing_after_r1, clean_target_scholars=True)
    write_all_csvs(TMP, t2.tables, unresolved_errors=t2.unresolved_errors)
    reg2.save()

    pubs_r2 = {r["publication_id"]: r for r in t2.tables["PUBLICATIONS"]}
    aps_r2 = t2.tables["Authors Publications"]

    check(
        "2. Existing publication is detected using normalized title + year across CSV runs",
        len(t2.tables["PUBLICATIONS"]) == 3
        and t2.stats["shared_publications_merged"] == 1,
    )
    check(
        "3. Capitalization differences do not create duplicate publications",
        sum(1 for r in t2.tables["PUBLICATIONS"] if "lower limb prosthesis" in r["title"].lower()) == 1,
    )
    check(
        "4. Whitespace differences do not create duplicate publications",
        "1" in pubs_r2 and pubs_r2["1"]["title"] == "Advancements, trends and future prospects of lower limb prosthesis",
    )
    check(
        "7. Shared publications belonging to multiple scholars remain correctly linked",
        len([r for r in aps_r2 if r["publication_id"] == "1"]) == 2
        and {r["scholar_id"] for r in aps_r2 if r["publication_id"] == "1"} == {"1", "2"},
    )
    check(
        "8. Existing HEIs and research areas are not duplicated across runs",
        len(t2.tables["HEIs"]) == 1
        and len(t2.tables["RESEARCH_AREAS"]) == 3,  # Embedded Systems (1), Robotics (2), Computer Vision (3)
    )
    check(
        "9. Bridge-table composite keys remain unique across multi-scholar CSV merges",
        len(aps_r2) == len({(r["scholar_id"], r["publication_id"]) for r in aps_r2}) == 4
        and len(t2.tables["Scholar Research Areas"])
        == len({(r["scholar_id"], r["research_area_id"]) for r in t2.tables["Scholar Research Areas"]})
        == 4,
    )

    # ----------------------------------------------------------------------- #
    # Run 3: Clean Scholar A and perform a fresh re-scrape of Scholar A
    # (Scholar A drops 'Background removal...' and adds a new paper 'New 2024 Paper')
    # ----------------------------------------------------------------------- #
    existing_before_clean = load_existing_csvs(TMP)
    reg3 = IdRegistry(reg_path)
    cleaned_preview, clean_summary = clean_scholar_from_tables(
        existing_before_clean, "vdh-mOQAAAAJ", registry=reg3
    )

    check(
        "12. Cleaning one scholar does not delete another scholar's records or shared publications/HEIs/areas",
        clean_summary["other_scholars_present"] is True
        and clean_summary["other_scholars_count"] == 1
        and len(cleaned_preview["SCHOLARS"]) == 1
        and cleaned_preview["SCHOLARS"][0]["google_scholar_id"] == "COLLAB_SCHOLAR"
        # Shared HEI (Hamdard University, id=1) and shared Robotics (id=2) and shared Paper (id=1) are preserved!
        and [r["hei_id"] for r in cleaned_preview["HEIs"]] == ["1"]
        and {r["publication_id"] for r in cleaned_preview["PUBLICATIONS"]} == {"1", "3"}
        and {r["research_area_id"] for r in cleaned_preview["RESEARCH_AREAS"]} == {"2", "3"},
    )

    # Now run full transform with clean_target_scholars=True for fresh Scholar A profile
    prof_a_fresh = make_profile_a(
        pubs=[
            ("Advancements, trends and future prospects of lower limb prosthesis", "2021", "M Asif, MI Tiwana, US Khan"),
            ("Background removal from GPR data using eigenvalues", "2010", "US Khan, W Al-Nuaimy"),
            ("Brand New 2024 Paper on Autonomous Robotics", "2024", "US Khan, Z Ali"),
        ]
    )
    t3 = transform(
        prof_a_fresh,
        TS2,
        registry=reg3,
        existing_tables=existing_before_clean,
        clean_target_scholars=True,
    )
    write_all_csvs(TMP, t3.tables, unresolved_errors=t3.unresolved_errors)
    reg3.save()

    scholars_r3 = {r["google_scholar_id"]: r for r in t3.tables["SCHOLARS"]}
    pubs_r3_by_title = {r["title"]: r["publication_id"] for r in t3.tables["PUBLICATIONS"]}

    check(
        "1. Existing scholar is detected using Google Scholar ID and retains scholar_id",
        scholars_r3["vdh-mOQAAAAJ"]["scholar_id"] == "1"
        and scholars_r3["COLLAB_SCHOLAR"]["scholar_id"] == "2"
        and len(t3.tables["SCHOLARS"]) == 2,
    )
    check(
        "6. Duplicate publication entries across multiple scraping runs are removed",
        len(t3.tables["PUBLICATIONS"]) == 4
        and len(validate_dataset(t3.tables, unresolved_errors=t3.unresolved_errors)) == 0,
    )
    check(
        "10. Existing IDs remain unchanged after a fresh scrape",
        pubs_r3_by_title["Advancements, trends and future prospects of lower limb prosthesis"] == "1"
        and pubs_r3_by_title["Background removal from GPR data using eigenvalues"] == "2"
        and pubs_r3_by_title["Exclusive Paper by Collaborator Two"] == "3",
    )
    check(
        "11. New publications receive new stable IDs",
        pubs_r3_by_title["Brand New 2024 Paper on Autonomous Robotics"] == "4",
    )

    # ----------------------------------------------------------------------- #
    # Run 4: Repeating the exact same scrape does not increase exported records
    # ----------------------------------------------------------------------- #
    counts_before_repeat = {ent: len(rows) for ent, rows in load_existing_csvs(TMP).items()}
    reg4 = IdRegistry(reg_path)
    t4 = transform(
        prof_a_fresh,
        TS2,
        registry=reg4,
        existing_tables=load_existing_csvs(TMP),
        clean_target_scholars=True,
    )
    write_all_csvs(TMP, t4.tables, unresolved_errors=t4.unresolved_errors)
    reg4.save()
    counts_after_repeat = {ent: len(rows) for ent, rows in load_existing_csvs(TMP).items()}

    check(
        "14. Repeating the same scrape does not increase the number of exported records",
        counts_before_repeat == counts_after_repeat,
    )

    # ----------------------------------------------------------------------- #
    # Test 13: Failed validation does not replace the previous valid CSV export
    # ----------------------------------------------------------------------- #
    valid_bytes_snapshot = {
        fname: (TMP / fname).read_bytes()
        for fname in [
            "heis.csv",
            "scholars.csv",
            "publications.csv",
            "research_areas.csv",
            "scholar_research_areas.csv",
            "authors_publications.csv",
        ]
    }
    invalid_prof = make_profile_a(
        pubs=[("Brand New 2024 Paper on Autonomous Robotics", "2024", "US Khan, Z Ali")],
        affiliation="",  # Valid source case; corruption below tests atomic validation.
    )
    t_invalid = transform(
        invalid_prof,
        TS2,
        registry=IdRegistry(reg_path),
        existing_tables=load_existing_csvs(TMP),
        clean_target_scholars=True,
    )
    t_invalid.tables["SCHOLARS"][0]["h_index"] = "not-a-number"
    validation_blocked = False
    try:
        write_all_csvs(TMP, t_invalid.tables, unresolved_errors=t_invalid.unresolved_errors)
    except DatasetValidationError:
        validation_blocked = True

    after_failed_bytes = {
        fname: (TMP / fname).read_bytes() for fname in valid_bytes_snapshot
    }
    check(
        "13. Failed validation does not replace the previous valid CSV export",
        validation_blocked is True and valid_bytes_snapshot == after_failed_bytes,
    )

    if TMP.exists():
        shutil.rmtree(TMP)

    print("\nALL 14 CROSS-RUN DEDUPLICATION TESTS PASSED" if ok else "\nFAILURES DETECTED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
