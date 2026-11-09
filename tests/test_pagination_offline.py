"""
Offline unit tests for dynamic "Show more" (#gsc_bpf_more) publication
expansion, DOM state transitions, ID stability across expansion, and
access-restriction safeguards.

Does not contact Google Scholar or perform live network requests.

Run:  python tests/test_pagination_offline.py
"""

import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import TimeoutError as PlaywrightTimeout  # noqa: E402
from scholar_scraper.browser import (  # noqa: E402
    AccessBlockedError,
    DiagnosticNetworkStats,
    ProfileData,
    ROW_SELECTOR,
    SHOW_MORE,
    _expand_publications,
    _scrape,
)
from scholar_scraper.id_registry import IdRegistry  # noqa: E402
from scholar_scraper.reporting import write_all_csvs  # noqa: E402
from scholar_scraper.transform import transform  # noqa: E402
from scholar_scraper.validator import validate_dataset  # noqa: E402

TMP = Path(__file__).resolve().parent / "_tmp_pagination"
TS = "2026-11-07 15:00:00"


class SimulatedScholarDOM:
    """
    Simulates Google Scholar's real profile DOM & XHR behavior during SHOW MORE:
      - Starts with `batches[0]` rows (e.g. 20).
      - When #gsc_bpf_more is clicked:
          * Immediately sets `in_flight_disabled = True` (just like Google Scholar's
            client JS disables #gsc_bpf_more while the XHR is in flight).
          * Only when `wait_for_function` waits for `rows > before` does the
            simulated XHR complete, appending the next batch of rows and updating
            `disabled` to `False` (if more batches remain) or `True` (if final batch).
    """

    def __init__(
        self,
        batches: list[int],
        disappear_at_end: bool = False,
        stall_on_click: int | None = None,
        trigger_429_on_click: int | None = None,
        stats: DiagnosticNetworkStats | None = None,
    ):
        self.batches = list(batches)
        self.batch_idx = 0
        self.current_rows = batches[0]
        self.disappear_at_end = disappear_at_end
        self.stall_on_click = stall_on_click
        self.trigger_429_on_click = trigger_429_on_click
        self.stats = stats

        self.clicks = 0
        self.in_flight = False
        self.button_present = True
        self.button_visible = True
        self.button_disabled = len(batches) <= 1
        self.goto_urls: list[str] = []

        self.page = MagicMock()
        self.rows_locator = MagicMock()
        self.btn_locator = MagicMock()

        self.rows_locator.count.side_effect = lambda: self.current_rows
        self.btn_locator.count.side_effect = lambda: 1 if self.button_present else 0
        self.btn_locator.is_visible.side_effect = lambda: self.button_visible
        # Crucial: while XHR is in flight, button reports disabled=True just like live Google Scholar!
        self.btn_locator.is_disabled.side_effect = lambda: (True if self.in_flight else self.button_disabled)
        self.btn_locator.click.side_effect = self._on_click

        def locator_dispatch(selector):
            if selector == ROW_SELECTOR:
                return self.rows_locator
            if selector == SHOW_MORE:
                return self.btn_locator
            loc = MagicMock()
            loc.count.return_value = 1
            loc.inner_text.return_value = f"Articles 1–{self.current_rows}"
            return loc

        self.page.locator.side_effect = locator_dispatch
        self.page.wait_for_function.side_effect = self._on_wait_for_function
        self.page.evaluate.side_effect = self._on_evaluate

    def _on_click(self):
        self.clicks += 1
        # Simulate Google Scholar immediately disabling #gsc_bpf_more while XHR is in flight
        self.in_flight = True
        if self.trigger_429_on_click == self.clicks and self.stats is not None:
            self.stats.has_429 = True
            self.stats.last_429_url = "https://scholar.google.com/citations?hl=en&user=[REDACTED]&cstart=20&pagesize=80"

    def _on_wait_for_function(self, js_code: str, arg=None, timeout=None):
        if "querySelectorAll('#gsc_a_b tr.gsc_a_tr').length > n" in js_code:
            # If a test simulates the buggy old condition `|| ...disabled`, fail immediately!
            assert ".disabled" not in js_code, "wait_for_function must NOT short-circuit on btn.disabled!"
            if self.stall_on_click == self.clicks:
                self.in_flight = False
                raise PlaywrightTimeout("Timed out waiting for new rows")
            if self.batch_idx + 1 < len(self.batches):
                self.batch_idx += 1
                self.current_rows = self.batches[self.batch_idx]
                self.in_flight = False
                is_last_batch = self.batch_idx == len(self.batches) - 1
                if is_last_batch:
                    if self.disappear_at_end:
                        self.button_present = False
                        self.button_visible = False
                    else:
                        self.button_disabled = True
                else:
                    self.button_disabled = False
                return True
            self.in_flight = False
            raise PlaywrightTimeout("No more rows to load")
        return True

    def _on_evaluate(self, js_code: str, *args):
        if "captcha" in js_code and "unusual" in js_code:
            return {"captcha": False, "unusual": False, "profile": True}
        if "scholar_name" in js_code:
            return {
                "scholar_name": "Prof. Dr. Umar Shahbaz Khan",
                "affiliation_text": "Campus Director, Hamdard University Islamabad Campus",
                "verification_text": "Verified email at ceme.nust.edu.pk",
                "header_text": "Prof. Dr. Umar Shahbaz Khan",
                "interests": ["Embedded Systems", "Image Processing", "BCI", "Robotics", "Automation"],
            }
        if "gsc_rsb_st" in js_code:
            return {"Citations": {"All": "2375"}, "h-index": {"All": "28"}}
        if "querySelectorAll('#gsc_a_b tr.gsc_a_tr')" in js_code:
            return make_synthetic_publications(self.current_rows)
        return None


def make_synthetic_publications(count: int, include_duplicate_of_row_1: bool = False) -> list[dict]:
    pubs = []
    for i in range(1, count + 1):
        pubs.append(
            {
                "row_index": i,
                "title": f"Research Publication Title Number {i}",
                "authors": "US Khan, Coauthor B",
                "venue": f"IEEE Journal Volume {i}",
                "year": str(2000 + (i % 25)),
                "cited_by": str(max(1, 150 - i)),
                "source_url": (
                    f"https://scholar.google.com/citations?view_op=view_citation"
                    f"&hl=en&user=vdh-mOQAAAAJ&citation_for_view=vdh-mOQAAAAJ:art_{i}"
                ),
            }
        )
    if include_duplicate_of_row_1 and pubs:
        dup = dict(pubs[0])
        dup["row_index"] = count + 1
        dup["source_url"] += "_dup"
        pubs.append(dup)
    return pubs


def main() -> int:
    ok = True

    def check(name: str, cond: bool):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok &= bool(cond)

    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------------------- #
    # Tests 1, 2, 3, 12: 20 initial -> Click #1 (100) -> Click #2 (147) -> Stop
    # ----------------------------------------------------------------------- #
    sim_147 = SimulatedScholarDOM(batches=[20, 100, 147])
    data_147 = ProfileData(
        requested_url="https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ",
        google_scholar_id="vdh-mOQAAAAJ",
    )
    with patch("scholar_scraper.browser._polite_pause"):
        _expand_publications(sim_147.page, data_147, delay=0.0, timeout_ms=5000, max_show_more=100)
        data_147.publications = sim_147.page.evaluate("querySelectorAll('#gsc_a_b tr.gsc_a_tr')")

    check(
        "1. Initial page contains 20 publications",
        data_147.initial_row_count == 20,
    )
    check(
        "2. First Show more click loads additional publications (20 -> 100)",
        len(data_147.pagination_steps) >= 1
        and data_147.pagination_steps[0] == {"click": 1, "before": 20, "after": 100},
    )
    check(
        "3. Second Show more click loads the remaining publications (100 -> 147)",
        len(data_147.pagination_steps) == 2
        and data_147.pagination_steps[1] == {"click": 2, "before": 100, "after": 147}
        and len(data_147.publications) == 147,
    )
    check(
        "12. The scraper does not click the Show more button after all publications are loaded",
        sim_147.clicks == 2
        and data_147.show_more_clicks == 2
        and data_147.listing_complete is True
        and data_147.stop_reason == "SHOW MORE button is disabled",
    )

    # ----------------------------------------------------------------------- #
    # Test 4: Dynamic loading continues if more than two clicks are required
    # ----------------------------------------------------------------------- #
    sim_4clicks = SimulatedScholarDOM(batches=[20, 100, 200, 300, 342])
    data_4clicks = ProfileData(requested_url="https://scholar.google.com/citations?user=X")
    with patch("scholar_scraper.browser._polite_pause"):
        _expand_publications(sim_4clicks.page, data_4clicks, delay=0.0, timeout_ms=5000, max_show_more=100)
    check(
        "4. Dynamic loading continues when more than two clicks are required (4 clicks -> 342 rows)",
        data_4clicks.show_more_clicks == 4
        and sim_4clicks.current_rows == 342
        and data_4clicks.listing_complete is True
        and [s["after"] for s in data_4clicks.pagination_steps] == [100, 200, 300, 342],
    )

    # ----------------------------------------------------------------------- #
    # Test 5: The loop stops when the Show more button disappears
    # ----------------------------------------------------------------------- #
    sim_disappear = SimulatedScholarDOM(batches=[20, 85], disappear_at_end=True)
    data_disappear = ProfileData(requested_url="https://scholar.google.com/citations?user=X")
    with patch("scholar_scraper.browser._polite_pause"):
        _expand_publications(sim_disappear.page, data_disappear, delay=0.0, timeout_ms=5000, max_show_more=100)
    check(
        "5. The loop stops when the Show more button disappears",
        data_disappear.show_more_clicks == 1
        and sim_disappear.current_rows == 85
        and data_disappear.listing_complete is True
        and "disappeared" in (data_disappear.stop_reason or ""),
    )

    # ----------------------------------------------------------------------- #
    # Test 6: The loop stops safely when no new publication rows appear
    # ----------------------------------------------------------------------- #
    sim_stall = SimulatedScholarDOM(batches=[20, 100, 147], stall_on_click=2)
    data_stall = ProfileData(requested_url="https://scholar.google.com/citations?user=X")
    with patch("scholar_scraper.browser._polite_pause"):
        _expand_publications(sim_stall.page, data_stall, delay=0.0, timeout_ms=5000, max_show_more=100)
    check(
        "6. The loop stops safely when no new publication rows appear",
        data_stall.show_more_clicks == 2
        and sim_stall.current_rows == 100
        and data_stall.listing_complete is False
        and any("loaded no new rows within timeout" in w for w in data_stall.warnings),
    )

    # ----------------------------------------------------------------------- #
    # Tests 7, 8, 9: Deduplication, CSV pipeline for 147 pubs, ID stability
    # ----------------------------------------------------------------------- #
    reg_path = TMP / "id_registry.json"
    reg_initial = IdRegistry(reg_path)

    # First run with only initial 20 publications
    p_initial20 = ProfileData(
        requested_url="https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ",
        google_scholar_id="vdh-mOQAAAAJ",
        loaded_url="https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ",
        http_status=200,
        listing_complete=True,
        header=sim_147._on_evaluate("scholar_name"),
        metrics=sim_147._on_evaluate("gsc_rsb_st"),
        publications=make_synthetic_publications(20),
    )
    t_initial20 = transform(p_initial20, TS, registry=reg_initial)
    reg_initial.save()
    first_20_ids = {
        r["title"]: r["publication_id"] for r in t_initial20.tables["PUBLICATIONS"]
    }

    # Second run after SHOW MORE expands to 147 publications (plus 1 overlapping duplicate of row 1)
    reg_expanded = IdRegistry(reg_path)
    p_expanded147 = ProfileData(
        requested_url="https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ",
        google_scholar_id="vdh-mOQAAAAJ",
        loaded_url="https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ",
        http_status=200,
        listing_complete=True,
        header=sim_147._on_evaluate("scholar_name"),
        metrics=sim_147._on_evaluate("gsc_rsb_st"),
        publications=make_synthetic_publications(147, include_duplicate_of_row_1=True),
    )
    t_expanded147 = transform(p_expanded147, TS, registry=reg_expanded)
    write_all_csvs(TMP, t_expanded147.tables, unresolved_errors=t_expanded147.unresolved_errors)
    reg_expanded.save()

    expanded_by_title = {
        r["title"]: r["publication_id"] for r in t_expanded147.tables["PUBLICATIONS"]
    }

    check(
        "7. No publication is extracted twice (overlapping duplicate skipped)",
        len(p_expanded147.publications) == 148
        and len(t_expanded147.tables["PUBLICATIONS"]) == 147
        and t_expanded147.stats["duplicates"] == 1,
    )
    check(
        "8. All 147 loaded publications reach the CSV transformation & validation pipeline",
        len(t_expanded147.tables["PUBLICATIONS"]) == 147
        and len(t_expanded147.tables["Authors Publications"]) == 147
        and t_expanded147.tables["SCHOLARS"][0]["total_publications"] == "147"
        and len(validate_dataset(t_expanded147.tables, unresolved_errors=t_expanded147.unresolved_errors)) == 0,
    )
    check(
        "9. Existing IDs for the first 20 publications remain 100% stable after loading 127 additional publications",
        all(expanded_by_title[title] == pid for title, pid in first_20_ids.items())
        and set(int(r["publication_id"]) for r in t_expanded147.tables["PUBLICATIONS"]) == set(range(1, 148)),
    )

    # ----------------------------------------------------------------------- #
    # Test 10: CAPTCHA and HTTP 429 detection during SHOW MORE still work
    # ----------------------------------------------------------------------- #
    stats_429 = DiagnosticNetworkStats()
    sim_429 = SimulatedScholarDOM(
        batches=[20, 100, 147], trigger_429_on_click=2, stats=stats_429
    )
    data_429 = ProfileData(requested_url="https://scholar.google.com/citations?user=X")
    caught_429 = False
    with patch("scholar_scraper.browser._polite_pause"):
        try:
            _expand_publications(
                sim_429.page, data_429, stats=stats_429, delay=0.0, timeout_ms=5000, max_show_more=100
            )
        except AccessBlockedError as exc:
            caught_429 = "429" in str(exc)
    check(
        "10. CAPTCHA and HTTP 429 detection during SHOW MORE stop extraction immediately",
        caught_429 is True and data_429.show_more_clicks == 2,
    )

    # ----------------------------------------------------------------------- #
    # Test 11: No individual publication detail page is opened
    # ----------------------------------------------------------------------- #
    sim_full = SimulatedScholarDOM(batches=[20, 100, 147])
    sim_full.page.url = "https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ"
    mock_resp = MagicMock()
    mock_resp.status = 200
    sim_full.page.goto.side_effect = lambda u, **kw: (sim_full.goto_urls.append(u), mock_resp)[1]

    mock_browser = MagicMock()
    mock_context = MagicMock()
    mock_context.new_page.return_value = sim_full.page
    mock_browser.new_context.return_value = mock_context

    data_full = ProfileData(requested_url=sim_full.page.url)
    with patch("scholar_scraper.browser._launch", return_value=mock_browser), \
         patch("scholar_scraper.browser.sync_playwright"), \
         patch("scholar_scraper.browser._polite_pause"):
        _scrape(
            data_full,
            data_full.requested_url,
            headless=True,
            delay=0.0,
            timeout_s=5.0,
            max_show_more=100,
            channel="auto",
        )

    check(
        "11. No individual publication detail page is opened during full 147-row scrape",
        sim_full.goto_urls == ["https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ"]
        and all("view_citation" not in u for u in sim_full.goto_urls)
        and len(data_full.publications) == 147,
    )

    if TMP.exists():
        shutil.rmtree(TMP)

    print("\nALL 12 PAGINATION OFFLINE TESTS PASSED" if ok else "\nFAILURES DETECTED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
