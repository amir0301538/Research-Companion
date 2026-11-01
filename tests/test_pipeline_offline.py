"""
OFFLINE pipeline test (no network, no Google Scholar).

Uses a SYNTHETIC, clearly fake ProfileData to exercise transform / CSV / report
logic. Output goes to tests/_tmp_output, never to ./output. These results are
NOT scraped data and must never be reported as such.

Run:  python tests/test_pipeline_offline.py
"""
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scholar_scraper.browser import ProfileData  # noqa: E402
from scholar_scraper.docx_schema import validate_against_docx  # noqa: E402
from scholar_scraper.erd_schema import CSV_FILES, ERD_ENTITIES  # noqa: E402
from scholar_scraper.reporting import assess, build_report, write_all_csvs  # noqa: E402
from scholar_scraper.transform import transform  # noqa: E402

DOCX = ROOT / "Research_Companion_ERD_Listing_Exact_Names_V2.docx"
TMP = Path(__file__).resolve().parent / "_tmp_output"


def fake_data():
    d = ProfileData(requested_url="https://scholar.google.com/citations?user=FAKE", google_scholar_id="FAKE")
    d.loaded_url = d.requested_url
    d.http_status = 200
    d.listing_complete = True
    d.header = {"scholar_name": "Dr. Test Person", "affiliation_text": "Lecturer, Fake Univ",
                "verification_text": "Verified email at fake.edu", "header_text": "Dr. Test Person Verified email at fake.edu",
                "interests": ["Machine Learning", "machine learning", "NLP"]}
    d.metrics = {"Citations": {"All": "1,234"}, "h-index": {"All": "12"}}

    def row(i, title, authors, year, cited, key):
        return {"row_index": i, "title": title, "authors": authors, "venue": "Journal X", "year": year,
                "cited_by": cited, "source_url": f"https://scholar.google.com/citations?view_op=view_citation&citation_for_view=FAKE:{key}"}
    d.publications = [
        row(1, "Paper A", "T Person, B Other", "2020", "5", "a"),
        row(2, "Paper A", "T Person, B Other", "2020", "5", "a"),      # duplicate
        row(3, "Paper B", "B Other, C Third ...", "", "", "b"),         # truncated, missing values
    ]
    return d


def main():
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok &= bool(cond)

    TMP.mkdir(exist_ok=True)
    res, _ = validate_against_docx(DOCX)
    check("DOCX headers match built-in schema", res is True)

    runs = []
    for _ in range(2):
        t = transform(fake_data(), "2000-01-01 00:00:00")
        write_all_csvs(TMP, t.tables)
        runs.append({f: (TMP / f).read_bytes() for f in CSV_FILES.values()})
    check("Re-run produces identical CSVs (no accumulation)", runs[0] == runs[1])
    check("Duplicate publication skipped", t.stats["publication_rows"] == 2 and t.stats["duplicates"] == 1)
    check("Duplicate research area skipped", t.stats["research_areas"] == 2)

    for ent, fname in CSV_FILES.items():
        with open(TMP / fname, encoding="utf-8", newline="") as f:
            rdr = csv.reader(f)
            header = next(rdr)
        check(f"{fname} header exact", header == ERD_ENTITIES[ent])

    sch = t.tables["SCHOLARS"][0]
    check("No fabricated internal IDs", all(sch[k] == "" for k in ("scholar_id", "hei_id")) and
          all(r["publication_id"] == "" for r in t.tables["PUBLICATIONS"]))
    check("email left empty when only domain shown", sch["email"] == "")
    check("Missing year/cites stay empty", t.tables["PUBLICATIONS"][1]["publication_year"] == "" and
          t.tables["PUBLICATIONS"][1]["citation_count"] == "")
    check("author_order derived only on unambiguous match",
          t.tables["Authors Publications"][0]["author_order"] == "1" and
          t.tables["Authors Publications"][1]["author_order"] == "")
    check("h_index/citations parsed", sch["h_index"] == "12" and sch["total_citations"] == "1234")

    rep = build_report(fake_data(), t, assess(fake_data(), t), (True, []),
                       {"timestamp_display": "test", "browser_mode": "none", "errors": []})
    check("Report has A/B/C sections", all(s in rep for s in ("A. SUCCESSFULLY", "B. PARTIALLY", "C. UNAVAILABLE")))
    print("\nALL PASSED" if ok else "\nFAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
