"""
Offline unit test suite for Stage 2: Google Scholar Individual Publication
Detail Scraping, Metadata Enrichment, Yearly Citations, Checkpoint/Resume,
and Rate-Limit/CAPTCHA Safety.

Does NOT make any live network requests to Google Scholar.

Run:  python tests/test_publication_details_offline.py
"""

import json
import shutil
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scholar_scraper.browser import (  # noqa: E402
    AccessBlockedError,
    InvalidProfileUrlError,
    JS_PUBLICATION_DETAIL,
    JS_PUBLICATIONS,
    ProfileData,
    canonicalize_detail_url,
    is_valid_detail_url,
    parse_detail_dom_payload,
    scrape_publication_detail_on_page,
    scrape_publications_details_batch,
)
from scholar_scraper.id_registry import IdRegistry  # noqa: E402
from scholar_scraper.reporting import (  # noqa: E402
    build_quality_report,
    load_existing_csvs,
    load_existing_detail_csvs,
    write_all_csvs,
)
from scholar_scraper.transform import apply_publication_details, transform  # noqa: E402
from scholar_scraper.validator import DatasetValidationError, validate_dataset  # noqa: E402

TMP = Path(__file__).resolve().parent / "_tmp_detail_tests"
TS = "2026-11-08 06:45:00"

SAMPLE_DETAIL_HTML_1 = """
<!DOCTYPE html>
<html>
<body>
  <div id="gsc_oci_title">
    <a class="gsc_oci_title_link" href="https://ieeexplore.ieee.org/document/9448123">
      Advancements, trends and future prospects of lower limb prosthesis
    </a>
  </div>
  <div id="gsc_oci_table">
    <div class="gs_scl">
      <div class="gsc_oci_field">Authors</div>
      <div class="gsc_oci_value">Muhammad Asif, Mohsin Islam Tiwana, Umar Shahbaz Khan, Waqar Shahid Qureshi, Javaid Iqbal, Nasir Rashid, Noman Naseer</div>
    </div>
    <div class="gs_scl">
      <div class="gsc_oci_field">Publication date</div>
      <div class="gsc_oci_value">2021/6/7</div>
    </div>
    <div class="gs_scl">
      <div class="gsc_oci_field">Journal</div>
      <div class="gsc_oci_value">IEEE access</div>
    </div>
    <div class="gs_scl">
      <div class="gsc_oci_field">Volume</div>
      <div class="gsc_oci_value">9</div>
    </div>
    <div class="gs_scl">
      <div class="gsc_oci_field">Pages</div>
      <div class="gsc_oci_value">85956-85977</div>
    </div>
    <div class="gs_scl">
      <div class="gsc_oci_field">Publisher</div>
      <div class="gsc_oci_value">IEEE</div>
    </div>
    <div class="gs_scl">
      <div class="gsc_oci_field">Description</div>
      <div class="gsc_oci_value">Amputees with lower limb loss need special care during daily life activities to make the movement natural as before amputation. No such work exists covering the main aspects from causes of amputation to the psycho-social impact of the amputees after using the prosthetic device. This review presents for lower limb prosthesis; the study of lower limb amputation, design &amp; development, control strategies &amp; machine learning algorithms, the psycho-social impact of prosthetic users, and design trends in patents. DOI: 10.1109/ACCESS.2021.3088776.</div>
    </div>
    <div class="gs_scl">
      <div class="gsc_oci_field">Total citations</div>
      <div class="gsc_oci_value">
        <div style="margin-bottom:1em">
          <a href="/scholar?oi=bibs&hl=en&cites=123456789">Cited by 136</a>
        </div>
        <div id="gsc_oci_graph">
          <div id="gsc_oci_graph_x">
            <span class="gsc_oci_g_t" style="left:10px">2021</span>
            <span class="gsc_oci_g_t" style="left:40px">2022</span>
            <span class="gsc_oci_g_t" style="left:70px">2023</span>
            <span class="gsc_oci_g_t" style="left:100px">2024</span>
            <span class="gsc_oci_g_t" style="left:130px">2025</span>
            <span class="gsc_oci_g_t" style="left:160px">2026</span>
          </div>
          <div id="gsc_oci_graph_bars">
            <a class="gsc_oci_g_a" href="/scholar?hl=en&as_ylo=2021&as_yhi=2021&cites=123" style="left:10px;height:5px"><span class="gsc_oci_g_al">2</span></a>
            <a class="gsc_oci_g_a" href="/scholar?hl=en&as_ylo=2022&as_yhi=2022&cites=123" style="left:40px;height:15px"><span class="gsc_oci_g_al">8</span></a>
            <a class="gsc_oci_g_a" href="/scholar?hl=en&as_ylo=2023&as_yhi=2023&cites=123" style="left:70px;height:25px"><span class="gsc_oci_g_al">15</span></a>
            <a class="gsc_oci_g_a" href="/scholar?hl=en&as_ylo=2024&as_yhi=2024&cites=123" style="left:100px;height:40px"><span class="gsc_oci_g_al">30</span></a>
            <a class="gsc_oci_g_a" href="/scholar?hl=en&as_ylo=2025&as_yhi=2025&cites=123" style="left:130px;height:60px"><span class="gsc_oci_g_al">45</span></a>
            <a class="gsc_oci_g_a" href="/scholar?hl=en&as_ylo=2026&as_yhi=2026&cites=123" style="left:160px;height:50px"><span class="gsc_oci_g_al">36</span></a>
          </div>
        </div>
      </div>
    </div>
    <div class="gs_scl">
      <div class="gsc_oci_field">Scholar articles</div>
      <div class="gsc_oci_value">
        <a href="/scholar?oi=bibs&cites=123456789">Cited by 136</a>
        <a href="/scholar?q=related:abc:scholar.google.com/">Related articles</a>
        <a href="/scholar?cluster=123456789">All 4 versions</a>
      </div>
    </div>
  </div>
</body>
</html>
"""

SAMPLE_DETAIL_HTML_SPARSE = """
<!DOCTYPE html>
<html>
<body>
  <div id="gsc_oci_title">
    <a class="gsc_oci_title_link" href="https://example.org/paper2">
      A novel framework for classification of two-class motor imagery EEG signals
    </a>
  </div>
  <div id="gsc_oci_table">
    <div class="gs_scl">
      <div class="gsc_oci_field">Authors</div>
      <div class="gsc_oci_value">RA Khan, N Rashid, M Shahzaib, UF Malik, A Arif, J Iqbal, M Saleem, US Khan</div>
    </div>
    <div class="gs_scl">
      <div class="gsc_oci_field">Publication date</div>
      <div class="gsc_oci_value">2023</div>
    </div>
    <!-- No Journal, Volume, Issue, Pages, Publisher, Description, DOI, or Total citations -->
    <div id="gsc_oci_graph">
      <div id="gsc_oci_graph_x">
        <span class="gsc_oci_g_t" style="left:10px">2023</span>
      </div>
      <div id="gsc_oci_graph_bars">
        <!-- Bar with height only and NO exact text count: must NOT be estimated from bar height! -->
        <a class="gsc_oci_g_a" href="/scholar?hl=en&as_ylo=2023&as_yhi=2023&cites=999" style="left:10px;height:42px"><span class="gsc_oci_g_al"></span></a>
      </div>
    </div>
  </div>
</body>
</html>
"""


def make_test_profile() -> ProfileData:
    d = ProfileData(
        requested_url="https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ",
        google_scholar_id="vdh-mOQAAAAJ",
        loaded_url="https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ",
        http_status=200,
        listing_complete=True,
    )
    d.header = {
        "scholar_name": "Prof. Dr. Umar Shahbaz Khan",
        "affiliation_text": "Campus Director, Hamdard University Islamabad Campus",
        "verification_text": "Verified email at ceme.nust.edu.pk",
        "header_text": "Prof. Dr. Umar Shahbaz Khan",
        "interests": ["Embedded Systems", "Robotics"],
    }
    d.metrics = {"Citations": {"All": "2,375"}, "h-index": {"All": "28"}}
    d.publications = [
        {
            "row_index": 1,
            "title": "Advancements, trends and future prospects of lower limb prosthesis",
            "authors": "M Asif, MI Tiwana, US Khan, WS Qureshi, J Iqbal, N Rashid, N Naseer",
            "venue": "IEEE access 9, 85956-85977",
            "year": "2021",
            "cited_by": "136",
            "source_url": "https://scholar.google.com/citations?view_op=view_citation&hl=en&user=vdh-mOQAAAAJ&cstart=0&pagesize=20&citation_for_view=vdh-mOQAAAAJ:nb7KW1ujOQ8C",
        },
        {
            "row_index": 2,
            "title": "A novel framework for classification of two-class motor imagery EEG signals",
            # Truncated front-page author list where US Khan is 8th author (hidden behind ...)
            "authors": "RA Khan, N Rashid, M Shahzaib, UF Malik, A Arif, J Iqbal, M Saleem, ...",
            "venue": "Plos one 18 (9), e0276133 ...",
            "year": "2023",
            "cited_by": "33",
            "source_url": "https://scholar.google.com/citations?view_op=view_citation&hl=en&user=vdh-mOQAAAAJ&cstart=20&pagesize=80&citation_for_view=vdh-mOQAAAAJ:hkOj_22Ku90C",
        },
        {
            "row_index": 3,
            "title": "Third Paper With Duplicate Detail URL",
            "authors": "US Khan, A Coauthor",
            "venue": "Sensors",
            "year": "2022",
            "cited_by": "10",
            # Points to same canonical detail URL as row 1 (different cstart/pagesize)
            "source_url": "https://scholar.google.com/citations?view_op=view_citation&hl=en&user=vdh-mOQAAAAJ&cstart=100&pagesize=100&citation_for_view=vdh-mOQAAAAJ:nb7KW1ujOQ8C",
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

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page()

        # ------------------------------------------------------------------- #
        # Test 1: Correct publication title link identification on profile DOM
        # ------------------------------------------------------------------- #
        page.set_content(
            """
            <table id="gsc_a_b">
              <tr class="gsc_a_tr">
                <td class="gsc_a_t">
                  <a class="gsc_a_at" href="/citations?view_op=view_citation&amp;hl=en&amp;user=vdh-mOQAAAAJ&amp;citation_for_view=vdh-mOQAAAAJ:nb7KW1ujOQ8C">Paper One</a>
                  <div class="gs_gray">US Khan</div>
                  <div class="gs_gray">IEEE Access</div>
                </td>
                <td class="gsc_a_c"><a class="gsc_a_ac" href="https://scholar.google.com/scholar?oi=bibs&amp;hl=en&amp;cites=111">136</a></td>
                <td class="gsc_a_y"><span class="gsc_a_h">2021</span></td>
              </tr>
            </table>
            """
        )
        extracted_rows = page.evaluate(JS_PUBLICATIONS)
        check(
            "1. Correct publication title link identification (ignores Cited by link)",
            len(extracted_rows) == 1
            and "view_op=view_citation" in extracted_rows[0]["source_url"]
            and "citation_for_view=vdh-mOQAAAAJ:nb7KW1ujOQ8C" in extracted_rows[0]["source_url"]
            and "cites=" not in extracted_rows[0]["source_url"],
        )

        # ------------------------------------------------------------------- #
        # Test 14: Unrelated links (Cited by, Related articles, author profile,
        # external publisher URLs) are rejected by is_valid_detail_url & stage 2
        # ------------------------------------------------------------------- #
        bad_urls = [
            "https://scholar.google.com/scholar?oi=bibs&hl=en&cites=123456789",
            "https://scholar.google.com/scholar?q=related:abc:scholar.google.com/",
            "https://scholar.google.com/citations?user=vdh-mOQAAAAJ&hl=en",
            "https://ieeexplore.ieee.org/document/9448123",
        ]
        rejected_all = all(not is_valid_detail_url(u) for u in bad_urls)
        raised_on_bad = False
        try:
            scrape_publication_detail_on_page(page, bad_urls[0], timestamp=TS)
        except InvalidProfileUrlError:
            raised_on_bad = True
        check(
            "14. No unrelated links (Cited by, Related articles, author profile, external URLs) are opened",
            rejected_all
            and raised_on_bad
            and is_valid_detail_url(
                "https://scholar.google.com/citations?view_op=view_citation&hl=en&user=vdh-mOQAAAAJ&citation_for_view=vdh-mOQAAAAJ:nb7KW1ujOQ8C"
            ),
        )

        # ------------------------------------------------------------------- #
        # Tests 2, 3, 4, 5, 6, 15: Detail DOM extraction of all fields, DOI,
        # citation count, publication URL, and exact yearly citation counts
        # ------------------------------------------------------------------- #
        detail_url_1 = "https://scholar.google.com/citations?view_op=view_citation&hl=en&user=vdh-mOQAAAAJ&citation_for_view=vdh-mOQAAAAJ:nb7KW1ujOQ8C"
        detail_url_2 = "https://scholar.google.com/citations?view_op=view_citation&hl=en&user=vdh-mOQAAAAJ&citation_for_view=vdh-mOQAAAAJ:hkOj_22Ku90C"

        navigated_urls = []

        def handle_route(route):
            req_url = route.request.url
            if "view_op=view_citation" not in req_url:
                route.fulfill(status=200, content_type="text/html", body=(
                    '<div id="gsc_prf_in">Umar Shahbaz Khan</div><table id="gsc_a_b">'
                    f'<tr class="gsc_a_tr"><td><a class="gsc_a_at" href="{detail_url_1}">Paper</a></td></tr>'
                    f'<tr class="gsc_a_tr"><td><a class="gsc_a_at" href="{detail_url_2}">Paper</a></td></tr>'
                    '</table><button id="gsc_bpf_more" disabled>Show more</button>'))
                return
            navigated_urls.append(req_url)
            if "nb7KW1ujOQ8C" in req_url:
                route.fulfill(status=200, content_type="text/html", body=SAMPLE_DETAIL_HTML_1)
            elif "hkOj_22Ku90C" in req_url:
                route.fulfill(status=200, content_type="text/html", body=SAMPLE_DETAIL_HTML_SPARSE)
            else:
                route.fulfill(status=404, content_type="text/html", body="Not found")

        page.route("**/*", handle_route)
        parsed_1 = scrape_publication_detail_on_page(page, detail_url_1, timestamp=TS)

        check(
            "2. Correct detail-page navigation to view_op=view_citation URL",
            len(navigated_urls) == 1 and navigated_urls[0] == detail_url_1,
        )
        check(
            "3. Extraction of title, authors, date, journal, volume, issue, pages, publisher and description",
            parsed_1["title"] == "Advancements, trends and future prospects of lower limb prosthesis"
            and parsed_1["authors_text"] == "Muhammad Asif, Mohsin Islam Tiwana, Umar Shahbaz Khan, Waqar Shahid Qureshi, Javaid Iqbal, Nasir Rashid, Noman Naseer"
            and parsed_1["publication_date"] == "2021/6/7"
            and parsed_1["publication_year"] == "2021"
            and parsed_1["journal"] == "IEEE access"
            and parsed_1["volume"] == "9"
            and parsed_1["issue"] == ""
            and parsed_1["pages"] == "85956-85977"
            and parsed_1["publisher"] == "IEEE"
            and "Amputees with lower limb loss need special care" in parsed_1["description"],
        )
        check(
            "4. Extraction of total citation count from detail page",
            parsed_1["citation_count"] == "136",
        )
        check(
            "5. Extraction of publication URL and external publisher URL",
            parsed_1["publication_url"] == detail_url_1
            and parsed_1["publisher_url"] == "https://ieeexplore.ieee.org/document/9448123"
            and parsed_1["external_url"] == "https://ieeexplore.ieee.org/document/9448123",
        )
        check(
            "6. Extraction of DOI when available on detail page",
            parsed_1["doi"] == "10.1109/ACCESS.2021.3088776",
        )
        expected_yearly = [
            {"citation_year": "2021", "citation_count": "2"},
            {"citation_year": "2022", "citation_count": "8"},
            {"citation_year": "2023", "citation_count": "15"},
            {"citation_year": "2024", "citation_count": "30"},
            {"citation_year": "2025", "citation_count": "45"},
            {"citation_year": "2026", "citation_count": "36"},
        ]
        check(
            "15a. Year-wise citation counts extracted accurately when exact values are available",
            parsed_1["yearly_citations"] == expected_yearly,
        )

        # ------------------------------------------------------------------- #
        # Test 7 & 15b: Missing optional metadata produces empty/NULL rather
        # than fabricated values; bar height without exact count is NOT guessed
        # ------------------------------------------------------------------- #
        parsed_2 = scrape_publication_detail_on_page(page, detail_url_2, timestamp=TS)
        check(
            "7. Missing optional metadata produces NULL/empty rather than fabricated values",
            parsed_2["title"] == "A novel framework for classification of two-class motor imagery EEG signals"
            and parsed_2["publication_date"] == "2023"
            and parsed_2["journal"] == ""
            and parsed_2["volume"] == ""
            and parsed_2["issue"] == ""
            and parsed_2["pages"] == ""
            and parsed_2["publisher"] == ""
            and parsed_2["description"] == ""
            and parsed_2["doi"] == ""
            and parsed_2["citation_count"] == "",
        )
        check(
            "15b. Yearly citation bar with height but no exact count label is NOT estimated",
            parsed_2["yearly_citations"] == [],
        )

        # ------------------------------------------------------------------- #
        # Tests 8, 9, 10, 12: Batch scraping, ID mapping, duplicate detail URL
        # handling, existing ID stability, and checkpoint/resume support
        # ------------------------------------------------------------------- #
        reg = IdRegistry(TMP / "id_registry.json")
        prof = make_test_profile()
        t = transform(prof, TS, registry=reg, clean_target_scholars=True)
        initial_pub_ids = [r["publication_id"] for r in t.tables["PUBLICATIONS"]]

        pub_queue = [
            {
                "publication_id": r["publication_id"],
                "scholar_id": "1",
                "title": r["title"],
                "source_url": r["source_url"],
            }
            for r in t.tables["PUBLICATIONS"]
        ]
        snap_path = TMP / "supplementary" / "publication_details_snapshot.json"
        navigated_urls.clear()

        batch_res_1 = scrape_publications_details_batch(
            publications_queue=pub_queue,
            snapshot_path=snap_path,
            delay=0.0,
            resume=True,
            page_override=page,
        )
        apply_publication_details(t, batch_res_1)
        write_all_csvs(TMP, t.tables, unresolved_errors=t.unresolved_errors, detail_tables=t.detail_tables)
        reg.save()

        det_rows = {r["publication_id"]: r for r in t.detail_tables["PUBLICATION_DETAILS"]}
        yearly_rows = t.detail_tables["PUBLICATION_YEARLY_CITATIONS"]
        ap_by_pid = {r["publication_id"]: r for r in t.tables["Authors Publications"]}

        check(
            "8. Multiple publications map to their correct publication_id, scholar_id, publication_url, and publisher_url",
            set(det_rows.keys()) == {"1", "2"}
            and all(r["scholar_id"] == "1" for r in det_rows.values())
            and det_rows["1"]["journal"] == "IEEE access"
            and det_rows["1"]["publication_date"] == "2021/6/7"
            and det_rows["1"]["publisher_url"] == "https://ieeexplore.ieee.org/document/9448123"
            and det_rows["1"]["publication_url"] == detail_url_1
            and det_rows["2"]["authors_text"].endswith("US Khan")
            # And truncated front-page author_order for pub_id=2 is now resolved to 8!
            and ap_by_pid["2"]["author_order"] == "8"
            # And full-name detail-page author_order for pub_id=1 ("Umar Shahbaz Khan") is 3!
            and ap_by_pid["1"]["author_order"] == "3",
        )
        check(
            "9. Duplicate detail URLs are detected and handled safely without re-fetching",
            batch_res_1["summary"]["skipped_duplicate_url"] == 1
            and len(batch_res_1["summary"]["duplicate_detail_urls"]) == 1
            and batch_res_1["publications"]["3"]["status"] == "SKIPPED_DUPLICATE_URL"
            and len(navigated_urls) == 2,  # Only 2 network navigations for 3 items!
        )
        check(
            "10. Existing publication IDs, scholar IDs, and registry mappings remain unchanged",
            [r["publication_id"] for r in t.tables["PUBLICATIONS"]] == initial_pub_ids
            and len(t.tables["PUBLICATIONS"]) == 3
            and len(yearly_rows) == 6
            and all(r["publication_id"] == "1" for r in yearly_rows),
        )

        # A duplicate URL is not a successful extraction for its own publication ID.
        navigated_urls.clear()
        batch_res_2 = scrape_publications_details_batch(
            publications_queue=pub_queue,
            snapshot_path=snap_path,
            delay=0.0,
            resume=True,
            page_override=page,
        )
        check(
            "12. Checkpoint/resume skips successful details and keeps duplicate URL incomplete (0 network requests)",
            batch_res_2["summary"]["skipped_checkpoint"] == 2
            and batch_res_2["summary"]["skipped_duplicate_url"] == 1
            and batch_res_2["summary"]["attempted_this_run"] == 0
            and len(navigated_urls) == 0,
        )

        # ------------------------------------------------------------------- #
        # Test 11 & 13: HTTP 429 and CAPTCHA stop detail processing safely and
        # failed detail-page extraction does not corrupt existing CSVs
        # ------------------------------------------------------------------- #
        valid_pubs_bytes = (TMP / "publications.csv").read_bytes()
        valid_details_bytes = (TMP / "supplementary" / "publication_details.csv").read_bytes()

        def handle_blocked_route(route):
            route.fulfill(
                status=429,
                content_type="text/html",
                body="<html><body>Our systems have detected unusual traffic from your computer network.</body></html>",
            )

        page.unroute("**/*")
        page.route("**/*", handle_blocked_route)

        snap_block_path = TMP / "supplementary" / "blocked_snapshot.json"
        batch_blocked = scrape_publications_details_batch(
            publications_queue=pub_queue,
            snapshot_path=snap_block_path,
            delay=0.0,
            resume=False,
            page_override=page,
        )
        check(
            "13. HTTP 429 / CAPTCHA stops detail processing immediately on first blocked request",
            batch_blocked["summary"]["stopped_by_block"] is True
            and batch_blocked["summary"]["attempted_this_run"] == 1
            and batch_blocked["publications"]["1"]["status"] == "BLOCKED",
        )
        check(
            "11. Failed/blocked detail-page extraction does not corrupt existing valid CSV exports",
            (TMP / "publications.csv").read_bytes() == valid_pubs_bytes
            and (TMP / "supplementary" / "publication_details.csv").read_bytes() == valid_details_bytes,
        )

        browser.close()

    if TMP.exists():
        shutil.rmtree(TMP)

    print("\nALL 15 PUBLICATION DETAIL OFFLINE TESTS PASSED" if ok else "\nFAILURES DETECTED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
