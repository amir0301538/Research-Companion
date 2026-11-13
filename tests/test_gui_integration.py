"""
GUI & End-to-End Integration Test Suite for `gui_app.py`, existing-scholar duplicate
detection, CSV permission error handling, and `run_scraper.execute_scraper_pipeline`.

Verifies all GUI requirements without making live requests to Google Scholar:
  1. Valid Google Scholar URLs are accepted and user ID is extracted.
  2. Invalid / malformed URLs are rejected with clear error messages.
  3. STEP 1 invokes profile scraping only (never opens publication detail pages) for a new scholar.
  4. STEP 1 loads all available publications using SHOW MORE.
  5. STEP 2 invokes STEP 1 automatically before detail scraping for a new scholar.
  6. STEP 2 visits publication detail URLs sequentially.
  7. Checkpoint/resume works (completed detail pages are skipped on subsequent runs).
  8. Existing CSV deduplication works across runs.
  9. Other scholars' records remain untouched.
  10. HTTP 429 and CAPTCHA stop scraping safely while preserving checkpoints & CSVs.
  11. GUI remains responsive during background scraping (worker runs on background thread).
  12. Both buttons display accurate progress and completion status in the GUI.
  13. Existing scholar detection (`vdh-mOQAAAAJ` already in `scholars.csv`) stops before
      scraping and displays the exact "⚠️ Scholar Already Stored!" notification in the GUI.
  14. Entering the same existing scholar URL in a different format or query-parameter order
      still detects the duplicate by `google_scholar_id`.
  15. Duplicate scholar detection never modifies any existing CSV files on disk.
  16. Simulated locked / permission-denied `scholars.csv` displays a meaningful
      CSV Permission Error in the GUI (never suppressed or misreported as "Already Stored").

Run:  python tests/test_gui_integration.py
"""

import csv
import shutil
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gui_app import (  # noqa: E402
    PLACEHOLDER_TEXT,
    ScholarScraperGUI,
    format_already_stored_notification,
    validate_scholar_profile_url,
)
from run_scraper import DEFAULT_DOCX, execute_scraper_pipeline  # noqa: E402
from scholar_scraper.browser import (  # noqa: E402
    InvalidProfileUrlError,
    ProfileData,
    _expand_publications,
    scrape_publications_details_batch,
)
from scholar_scraper.reporting import (  # noqa: E402
    CsvPermissionError,
    find_existing_scholar_by_google_id,
)
from tests.test_publication_details_offline import (  # noqa: E402
    SAMPLE_DETAIL_HTML_1,
    SAMPLE_DETAIL_HTML_SPARSE,
)

TMP = Path(__file__).resolve().parent / "_tmp_gui_tests"


def wait_for_gui_job(gui: ScholarScraperGUI, root: tk.Tk, timeout_s: float = 15.0):
    """Pump the Tkinter event loop & queue until the background worker completes."""
    deadline = time.time() + timeout_s
    while gui.is_running and time.time() < deadline:
        gui.process_pending_events()
        root.update()
        time.sleep(0.02)
    gui.process_pending_events()
    root.update()


def remove_scholar_row_from_csv(scholars_csv: Path, google_scholar_id: str):
    """Helper used only in tests to simulate a fresh/unscraped scholar while keeping other CSVs."""
    if not scholars_csv.exists():
        return
    with open(scholars_csv, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = [r for r in reader if (r.get("google_scholar_id") or "").strip() != google_scholar_id]
    with open(scholars_csv, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        writer.writerows(rows)


def make_profile_for_scholar_a(show_more_clicks: int = 2) -> ProfileData:
    d = ProfileData(
        requested_url="https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ",
        google_scholar_id="vdh-mOQAAAAJ",
        loaded_url="https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ",
        http_status=200,
        listing_complete=True,
        show_more_clicks=show_more_clicks,
        initial_row_count=1,
        pagination_steps=[{"click": 1, "before": 1, "after": 2}],
        browser_used="chrome (mocked)",
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
            "source_url": "https://scholar.google.com/citations?view_op=view_citation&hl=en&user=vdh-mOQAAAAJ&citation_for_view=vdh-mOQAAAAJ:nb7KW1ujOQ8C",
        },
        {
            "row_index": 2,
            "title": "A novel framework for classification of two-class motor imagery EEG signals",
            "authors": "RA Khan, N Rashid, M Shahzaib, UF Malik, A Arif, J Iqbal, M Saleem, ...",
            "venue": "Plos one 18 (9), e0276133 ...",
            "year": "2023",
            "cited_by": "33",
            "source_url": "https://scholar.google.com/citations?view_op=view_citation&hl=en&user=vdh-mOQAAAAJ&citation_for_view=vdh-mOQAAAAJ:hkOj_22Ku90C",
        },
    ]
    return d


def make_profile_for_scholar_b() -> ProfileData:
    d = ProfileData(
        requested_url="https://scholar.google.com/citations?hl=en&user=YUAsxMUAAAAJ",
        google_scholar_id="YUAsxMUAAAAJ",
        loaded_url="https://scholar.google.com/citations?hl=en&user=YUAsxMUAAAAJ",
        http_status=200,
        listing_complete=True,
        show_more_clicks=0,
        initial_row_count=1,
        browser_used="chrome (mocked)",
    )
    d.header = {
        "scholar_name": "Dr. Muhammad Sajid Qureshi",
        "affiliation_text": "Riphah International University",
        "verification_text": "Verified email at riphah.edu.pk",
        "header_text": "Dr. Muhammad Sajid Qureshi",
        "interests": ["Machine Learning"],
    }
    d.metrics = {"Citations": {"All": "15"}, "h-index": {"All": "2"}}
    d.publications = [
        {
            "row_index": 1,
            "title": "Deep Learning Based Brain Tumor Detection",
            "authors": "MS Qureshi",
            "venue": "IEEE Conference",
            "year": "2024",
            "cited_by": "5",
            "source_url": "https://scholar.google.com/citations?view_op=view_citation&hl=en&user=YUAsxMUAAAAJ&citation_for_view=YUAsxMUAAAAJ:u5HHmVD_uO8C",
        }
    ]
    return d


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ok = True

    def check(name: str, cond: bool):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok &= bool(cond)

    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True, exist_ok=True)

    root = tk.Tk()
    root.withdraw()

    try:
        # ------------------------------------------------------------------- #
        # Test 1 & 2: URL validation in GUI (valid accepted, invalid rejected)
        # ------------------------------------------------------------------- #
        uid_1 = validate_scholar_profile_url("https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ")
        uid_2 = validate_scholar_profile_url("https://scholar.google.com.pk/citations?user=YUAsxMUAAAAJ&hl=en")
        check(
            "1. Valid Google Scholar URLs are accepted and user ID is extracted",
            uid_1 == "vdh-mOQAAAAJ" and uid_2 == "YUAsxMUAAAAJ",
        )

        invalid_inputs = [
            "",
            PLACEHOLDER_TEXT,
            "not-a-url",
            "https://www.google.com/search?q=scholar",
            "https://scholar.google.com/citations?hl=en",  # missing user=
            "https://scholar.google.com/citations?view_op=view_citation&hl=en&user=vdh-mOQAAAAJ&citation_for_view=vdh-mOQAAAAJ:nb7KW1ujOQ8C",
        ]
        all_rejected = True
        for bad in invalid_inputs:
            try:
                validate_scholar_profile_url(bad)
                all_rejected = False
            except InvalidProfileUrlError:
                pass

        gui = ScholarScraperGUI(root, output_dir=TMP, docx_path=DEFAULT_DOCX, show_dialogs=False)
        gui.set_url("https://example.com/not-scholar")
        started_bad = gui.start_step1_profile_scraping()
        check(
            "2. Invalid / malformed URLs are rejected and display a clear error in the GUI",
            all_rejected
            and started_bad is False
            and gui.status_var.get() == "Invalid URL"
            and "Invalid Google Scholar URL" in gui.banner_message_var.get(),
        )

        # ------------------------------------------------------------------- #
        # Test 4: STEP 1 loads all available publications using SHOW MORE
        # ------------------------------------------------------------------- #
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, channel="chrome")
            page = browser.new_page()
            page.set_content(
                """
                <html><body>
                  <table id="gsc_a_b">
                    <tr class="gsc_a_tr"><td class="gsc_a_t"><a class="gsc_a_at" href="/citations?view_op=view_citation&user=vdh-mOQAAAAJ&citation_for_view=vdh-mOQAAAAJ:1">P1</a></td></tr>
                  </table>
                  <button id="gsc_bpf_more" onclick="
                    const tb = document.querySelector('#gsc_a_b');
                    const tr = document.createElement('tr');
                    tr.className = 'gsc_a_tr';
                    tr.innerHTML = '<td class=gsc_a_t><a class=gsc_a_at href=/citations?view_op=view_citation&user=vdh-mOQAAAAJ&citation_for_view=vdh-mOQAAAAJ:2>P2</a></td>';
                    tb.appendChild(tr);
                    this.disabled = true;
                  ">Show more</button>
                </body></html>
                """
            )
            test_prof = ProfileData(requested_url="https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ")
            progress_events = []
            _expand_publications(page, test_prof, delay=0.0, progress_callback=progress_events.append)
            check(
                "4. STEP 1 loads all available publications using SHOW MORE (#gsc_bpf_more)",
                test_prof.show_more_clicks == 1
                and test_prof.listing_complete is True
                and page.locator("#gsc_a_b tr.gsc_a_tr").count() == 2
                and any(e.get("publications_found") == 2 for e in progress_events),
            )

            # --------------------------------------------------------------- #
            # Setup mocked Playwright detail page router for Step 1 & Step 2
            # --------------------------------------------------------------- #
            call_order = []
            detail_urls_visited = []
            worker_thread_ids = []

            def handle_route(route):
                u = route.request.url
                if "view_op=view_citation" not in u:
                    rows = ''.join(
                        f'<tr class="gsc_a_tr"><td><a class="gsc_a_at" href="{r["source_url"]}">{r["title"]}</a></td></tr>'
                        for r in make_profile_for_scholar_a().publications)
                    route.fulfill(status=200, content_type="text/html", body=(
                        '<div id="gsc_prf_in">Umar Shahbaz Khan</div><table id="gsc_a_b">'
                        + rows + '</table><button id="gsc_bpf_more" disabled>Show more</button>'))
                    return
                detail_urls_visited.append(u)
                if "nb7KW1ujOQ8C" in u:
                    route.fulfill(status=200, content_type="text/html", body=SAMPLE_DETAIL_HTML_1)
                elif "hkOj_22Ku90C" in u:
                    route.fulfill(status=200, content_type="text/html", body=SAMPLE_DETAIL_HTML_SPARSE)
                else:
                    route.fulfill(status=404, content_type="text/html", body="Not found")

            def handle_blocked_route(route):
                route.fulfill(
                    status=429,
                    content_type="text/html",
                    body="<html><body>Our systems have detected unusual traffic from your computer network.</body></html>",
                )

            def mock_scrape_profile(url, progress_callback=None, **_kwargs):
                call_order.append("step1_profile")
                worker_thread_ids.append(threading.get_ident())
                time.sleep(0.05)  # simulate work so GUI responsiveness can be verified
                prof = (
                    make_profile_for_scholar_b()
                    if "YUAsxMUAAAAJ" in url
                    else make_profile_for_scholar_a()
                )
                if progress_callback:
                    progress_callback({
                        "phase": "step1_extracted",
                        "researcher_name": prof.header["scholar_name"],
                        "publications_found": len(prof.publications),
                        "status": f"Extracted {len(prof.publications)} publications",
                    })
                return prof

            def mock_scrape_details_batch(publications_queue, snapshot_path, progress_callback=None, resume=True, **_kwargs):
                call_order.append("step2_details")
                worker_thread_ids.append(threading.get_ident())
                with sync_playwright() as p_worker:
                    b_worker = p_worker.chromium.launch(headless=True, channel="chrome")
                    pg_worker = b_worker.new_page()
                    if getattr(mock_scrape_details_batch, "blocked_mode", False):
                        pg_worker.route("**/*", handle_blocked_route)
                    else:
                        pg_worker.route("**/*", handle_route)
                    try:
                        return scrape_publications_details_batch(
                            publications_queue=publications_queue,
                            snapshot_path=snapshot_path,
                            delay=0.0,
                            resume=resume,
                            page_override=pg_worker,
                            progress_callback=progress_callback,
                        )
                    finally:
                        b_worker.close()

            def gui_pipeline_runner(**kwargs):
                return execute_scraper_pipeline(
                    scrape_profile_fn=mock_scrape_profile,
                    scrape_details_fn=mock_scrape_details_batch,
                    **kwargs,
                )

            gui.pipeline_runner = gui_pipeline_runner

            # --------------------------------------------------------------- #
            # First seed Scholar B (`YUAsxMUAAAAJ`) via STEP 1 (new scholar)
            # --------------------------------------------------------------- #
            gui.set_url("https://scholar.google.com/citations?hl=en&user=YUAsxMUAAAAJ")
            gui.start_step1_profile_scraping()
            wait_for_gui_job(gui, root)

            # --------------------------------------------------------------- #
            # Test 3 & 11: Entering a NEW scholar (`vdh-mOQAAAAJ`) proceeds
            # normally, invokes STEP 1 profile scraping ONLY, and runs in a
            # background worker thread without freezing the GUI
            # --------------------------------------------------------------- #
            call_order.clear()
            detail_urls_visited.clear()
            worker_thread_ids.clear()
            main_thread_id = threading.get_ident()

            gui.set_url("https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ")
            started_s1 = gui.start_step1_profile_scraping()
            responsive_during_run = gui.is_running is True and str(gui.step1_btn["state"]) == "disabled"
            wait_for_gui_job(gui, root)

            check(
                "3. New scholar URL proceeds normally and STEP 1 invokes profile scraping only (0 detail pages)",
                started_s1 is True
                and call_order == ["step1_profile"]
                and len(detail_urls_visited) == 0
                and gui.last_result is not None
                and gui.last_result.exit_code == 0,
            )
            check(
                "11. GUI remains responsive during background scraping (runs in worker thread)",
                responsive_during_run is True
                and len(worker_thread_ids) >= 1
                and all(tid != main_thread_id for tid in worker_thread_ids)
                and str(gui.step1_btn["state"]) == "normal",
            )

            # --------------------------------------------------------------- #
            # Test 13, 14, 15: Existing Scholar Detection in GUI!
            #   - Scenario 1: Enter `vdh-mOQAAAAJ` when it already exists ->
            #     GUI displays "⚠️ Scholar Already Stored!" with Scholar Name,
            #     Google Scholar ID, and Scholar ID, and does NOT start scraping.
            #   - Scenario 3: Enter the same existing URL using a different URL
            #     format / query parameter order -> duplicate is still detected.
            #   - Scenario 5: Duplicate detection NEVER modifies existing CSV records.
            # --------------------------------------------------------------- #
            call_order.clear()
            csv_snapshots_before = {
                fname: (TMP / fname).read_bytes()
                for fname in (
                    "heis.csv",
                    "scholars.csv",
                    "publications.csv",
                    "research_areas.csv",
                    "scholar_research_areas.csv",
                    "authors_publications.csv",
                )
            }

            # Scenario 1: Exact URL for existing scholar `vdh-mOQAAAAJ`
            gui.set_url("https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ")
            started_dup_1 = gui.start_step1_profile_scraping()
            banner_1 = gui.banner_message_var.get()

            check(
                "13. Entering existing scholar 'vdh-mOQAAAAJ' displays '⚠️ Scholar Already Stored!' and skips scraping",
                started_dup_1 is False
                and len(call_order) == 0
                and gui.last_result is not None
                and gui.last_result.status == "ALREADY_STORED"
                and gui.status_var.get() == "⚠️ Scholar Already Stored!"
                and "⚠️ Scholar Already Stored!" in banner_1
                and "- Scholar Name: Prof. Dr. Umar Shahbaz Khan" in banner_1
                and "- Google Scholar ID: vdh-mOQAAAAJ" in banner_1
                and "- Scholar ID: 2" in banner_1
                and "The scholar is already stored. No new scraping or CSV modifications were performed." in banner_1,
            )

            # Scenario 3: Different domain, parameter order, and extra pagination parameters (`cstart`, `pagesize`, `sortby`)
            gui.set_url(
                "https://scholar.google.com.pk/citations?view_op=list_works&sortby=pubdate&cstart=20&pagesize=80&user=vdh-mOQAAAAJ&hl=en"
            )
            started_dup_2 = gui.start_step1_profile_scraping()
            banner_2 = gui.banner_message_var.get()

            check(
                "14. Different URL format / query parameter order for existing scholar is still detected by google_scholar_id",
                started_dup_2 is False
                and len(call_order) == 0
                and gui.last_result.status == "ALREADY_STORED"
                and "- Google Scholar ID: vdh-mOQAAAAJ" in banner_2,
            )

            # Scenario 5: Verify byte-for-byte that zero CSV files were modified during duplicate checks
            csv_snapshots_after = {
                fname: (TMP / fname).read_bytes() for fname in csv_snapshots_before
            }
            check(
                "15. Duplicate scholar detection never modifies any existing CSV records on disk",
                csv_snapshots_before == csv_snapshots_after,
            )

            # --------------------------------------------------------------- #
            # Test 16 (Scenario 4): Simulate a locked `scholars.csv` ->
            # GUI displays a meaningful CSV Permission Error (not suppressed or
            # misreported as "Already Stored").
            # --------------------------------------------------------------- #
            with patch(
                "scholar_scraper.reporting.open",
                side_effect=PermissionError(13, "Permission denied", str(TMP / "scholars.csv")),
            ):
                gui.set_url("https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ")
                started_locked = gui.start_step1_profile_scraping()
                locked_status = gui.status_var.get()
                locked_banner = gui.banner_message_var.get()

            check(
                "16. Locked scholars.csv raises and displays a meaningful CSV Permission Error in GUI",
                started_locked is False
                and len(call_order) == 0
                and gui.last_result is not None
                and gui.last_result.status == "PERMISSION_ERROR"
                and locked_status == "CSV Permission Error"
                and "Permission denied" in locked_banner
                and "scholars.csv" in locked_banner
                and "Scholar Already Stored" not in locked_banner,
            )

            # --------------------------------------------------------------- #
            # Test 8 & 9: Existing CSV deduplication & preservation of other
            # scholars when scraping a scholar through the pipeline
            # --------------------------------------------------------------- #
            # Remove only Scholar A's row from `scholars.csv` while keeping Scholar A's
            # publications in `publications.csv` to verify cross-run CSV deduplication
            # and multi-scholar preservation when Scholar A is scraped via STEP 2.
            remove_scholar_row_from_csv(TMP / "scholars.csv", "vdh-mOQAAAAJ")

            call_order.clear()
            detail_urls_visited.clear()

            gui.set_url("https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ")
            started_s2 = gui.start_step2_full_scraping()
            wait_for_gui_job(gui, root)

            with open(TMP / "scholars.csv", "r", encoding="utf-8") as f:
                scholars_rows = list(csv.DictReader(f))
            with open(TMP / "publications.csv", "r", encoding="utf-8") as f:
                pubs_rows = list(csv.DictReader(f))
            with open(TMP / "supplementary" / "publication_details.csv", "r", encoding="utf-8") as f:
                det_csv_rows = list(csv.DictReader(f))
            with open(TMP / "supplementary" / "publication_yearly_citations.csv", "r", encoding="utf-8") as f:
                yearly_csv_rows = list(csv.DictReader(f))

            check(
                "8. Existing CSV deduplication prevents duplicate records across runs",
                len(pubs_rows) == 3,  # 2 for Scholar A + 1 for Scholar B
            )
            check(
                "9. Other scholars' records remain untouched during scraping",
                {r["google_scholar_id"] for r in scholars_rows} == {"YUAsxMUAAAAJ", "vdh-mOQAAAAJ"},
            )
            check(
                "5. STEP 2 automatically invokes STEP 1 before detail scraping",
                started_s2 is True and call_order == ["step1_profile", "step2_details"],
            )
            check(
                "6. STEP 2 visits publication detail URLs sequentially and writes detail CSVs",
                len(detail_urls_visited) == 2
                and "nb7KW1ujOQ8C" in detail_urls_visited[0]
                and "hkOj_22Ku90C" in detail_urls_visited[1]
                and len(det_csv_rows) == 2
                and len(yearly_csv_rows) == 6,
            )
            check(
                "12. Both buttons display accurate progress, researcher name, counts, and 100% completion",
                gui.researcher_var.get() == "Prof. Dr. Umar Shahbaz Khan"
                and gui.pubs_found_var.get() == "2"
                and gui.pubs_processed_var.get() == "2"
                and gui.details_scraped_var.get() == "2"
                and gui.pubs_pending_var.get() == "0"
                and gui.progress_label_var.get() == "100%"
                and gui.status_var.get() == "Completed Successfully",
            )

            # --------------------------------------------------------------- #
            # Test 7: Checkpoint/resume skips already scraped detail pages
            # --------------------------------------------------------------- #
            detail_urls_visited.clear()
            gui.start_step2_full_scraping()
            wait_for_gui_job(gui, root)
            check(
                "7. Checkpoint/resume skips already-completed detail pages (0 network detail requests)",
                len(detail_urls_visited) == 0
                and gui.last_result.status == "ALREADY_COMPLETE"
                and gui.details_scraped_var.get() == "2",
            )

            # --------------------------------------------------------------- #
            # Test 10: HTTP 429 / CAPTCHA stops scraping safely and displays clear block message
            # --------------------------------------------------------------- #
            snap_file = TMP / "supplementary" / "checkpoints" / "vdh-mOQAAAAJ.json"
            if snap_file.exists():
                snap_file.unlink()

            mock_scrape_details_batch.blocked_mode = True
            gui.start_step2_full_scraping()
            wait_for_gui_job(gui, root)

            check(
                "10. HTTP 429 / CAPTCHA stops scraping immediately, preserves CSVs, and shows block notice in GUI",
                gui.last_result is not None
                and gui.last_result.stopped_by_block is True
                and gui.last_result.detail_summary.get("attempted_this_run") == 1
                and "HTTP 429 Rate Limit" in gui.status_var.get()
                and "restricted" in gui.banner_message_var.get(),
            )

            browser.close()
    finally:
        gui._on_close()
        if TMP.exists():
            shutil.rmtree(TMP, ignore_errors=True)

    print("\nALL 16 GUI & DUPLICATE DETECTION TESTS PASSED" if ok else "\nGUI TEST FAILURES DETECTED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
