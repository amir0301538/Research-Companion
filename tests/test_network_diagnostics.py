"""
Unit tests for network diagnostic logging, manual mode handling, 429 detection,
status tracking on manual navigation, and URL redaction.
Offline test: does not contact Google Scholar or make live network requests.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scholar_scraper.browser import (
    AccessBlockedError,
    DiagnosticNetworkStats,
    ProfileData,
    _check_blocked,
    _scrape,
    _verify_profile_owner,
    redact_url,
    setup_diagnostic_listeners,
    validate_user_data_dir,
)
from scholar_scraper.reporting import build_failure_report


def test_url_redaction():
    # Identifiers and sensitive tokens must be redacted
    url1 = "https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ"
    redacted1 = redact_url(url1)
    assert "user=[REDACTED]" in redacted1
    assert "hl=en" in redacted1
    assert "vdh-mOQAAAAJ" not in redacted1

    url2 = "https://www.google.com/sorry/index?continue=https://scholar.google.com&hl=en&q=secret_token_12345"
    redacted2 = redact_url(url2)
    assert "q=[REDACTED]" in redacted2
    assert "continue=[REDACTED]" in redacted2
    assert "hl=en" in redacted2
    assert "secret_token_12345" not in redacted2

    url3 = "https://scholar.google.com/citations?view_op=view_citation&citation_for_view=vdh-mOQAAAAJ:u5HHmVD_uO8C"
    redacted3 = redact_url(url3)
    assert "citation_for_view=[REDACTED]" in redacted3
    assert "view_op=view_citation" in redacted3
    assert "u5HHmVD_uO8C" not in redacted3

    print("PASS 5. test_query_parameters_redacted_from_diagnostic_logs (redact_url)")


def test_diagnostic_stats_collection_and_redaction():
    stats = DiagnosticNetworkStats()
    mock_page = MagicMock()
    listeners = {}

    def fake_on(event, handler):
        listeners[event] = handler

    mock_page.on = fake_on
    logger = MagicMock()

    setup_diagnostic_listeners(mock_page, stats, logger)

    req1 = MagicMock()
    req1.url = "https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ"
    req1.resource_type = "document"
    req1.is_navigation_request.return_value = True

    listeners["request"](req1)

    assert stats.navigation_count == 1
    assert stats.total_requests == 1
    # Verify redacted in log/stats
    assert "user=[REDACTED]" in stats.navigation_events[0]["url"]
    assert "vdh-mOQAAAAJ" not in stats.navigation_events[0]["url"]
    assert "user=[REDACTED]" in stats.requests_log[0]["url"]

    # Simulate 429 response
    resp1 = MagicMock()
    resp1.request = req1
    resp1.status = 429
    resp1.url = req1.url
    resp1.frame = mock_page.main_frame

    listeners["response"](resp1)

    assert stats.has_429 is True
    assert stats.last_main_frame_status == 429

    print("PASS 5. test_query_parameters_redacted_from_diagnostic_logs (listeners)")


def test_normal_mode_stops_immediately_on_429():
    mock_page = MagicMock()
    mock_page.url = "https://scholar.google.com/citations?hl=en&user=test"

    stats = DiagnosticNetworkStats()
    # In normal mode, _check_blocked raises AccessBlockedError immediately
    try:
        _check_blocked(mock_page, status=429, stats=stats)
        raise AssertionError("Expected AccessBlockedError on HTTP 429")
    except AccessBlockedError as e:
        assert "429" in str(e)

    # In _scrape with manual=False, AccessBlockedError must not be caught
    mock_browser = MagicMock()
    mock_context = MagicMock()
    mock_context.new_page.return_value = mock_page
    mock_browser.new_context.return_value = mock_context

    mock_resp = MagicMock()
    mock_resp.status = 429
    mock_page.goto.return_value = mock_resp

    data = ProfileData(requested_url="https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ")

    with patch("scholar_scraper.browser._launch", return_value=mock_browser), \
         patch("scholar_scraper.browser.sync_playwright"):
        try:
            _scrape(data, data.requested_url, headless=True, delay=1.0, timeout_s=5.0,
                    max_show_more=10, channel="auto", manual=False)
            raise AssertionError("Expected AccessBlockedError to terminate scraping in normal mode")
        except AccessBlockedError:
            pass

    print("PASS 2. test_normal_mode_stops_immediately_on_429")


def test_manual_mode_reaches_await_manual_access_on_429():
    mock_page = MagicMock()
    mock_page.url = "https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ"
    mock_browser = MagicMock()
    mock_context = MagicMock()
    mock_context.new_page.return_value = mock_page
    mock_browser.new_context.return_value = mock_context

    mock_resp = MagicMock()
    mock_resp.status = 429
    mock_page.goto.return_value = mock_resp

    data = ProfileData(requested_url="https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ")

    await_called = []

    def fake_await(page, d, reason, wait_s):
        await_called.append((reason, wait_s))
        # Simulate user resolving to profile
        mock_page.url = "https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ"

    def fake_evaluate(js, *args):
        if "captcha" in js:
            return {"captcha": False, "unusual": False, "profile": True}
        if "JS_PAGE_STATE" in js:
            return "profile"
        if "scholar_name" in js:
            return {
                "scholar_name": "Test Scholar", "affiliation_text": None,
                "affiliation_org_link": None, "verification_text": None,
                "homepage_url": None, "header_text": "Test Scholar", "interests": []
            }
        return {}

    mock_page.evaluate = fake_evaluate
    mock_page.locator.return_value.count.return_value = 0

    with patch("scholar_scraper.browser._launch", return_value=mock_browser), \
         patch("scholar_scraper.browser._await_manual_access", side_effect=fake_await), \
         patch("scholar_scraper.browser.sync_playwright"):
        _scrape(data, data.requested_url, headless=False, delay=1.0, timeout_s=5.0,
                max_show_more=10, channel="auto", manual=True, manual_wait_s=120.0)

    assert len(await_called) == 1
    assert "429" in await_called[0][0]
    assert await_called[0][1] == 120.0
    print("PASS 1. test_manual_mode_reaches_await_manual_access_on_429")


def test_xhr_429_during_show_more_stops_extraction():
    stats = DiagnosticNetworkStats()
    mock_page = MagicMock()
    mock_page.url = "https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ"
    mock_page.evaluate.return_value = {"captcha": False, "unusual": False, "profile": True}

    # Initially not blocked
    _check_blocked(mock_page, status=200, stats=stats)

    # Simulate in-page XHR returning 429 during SHOW MORE
    listeners = {}
    mock_page.on = lambda ev, fn: listeners.update({ev: fn})
    setup_diagnostic_listeners(mock_page, stats, MagicMock())

    req = MagicMock()
    req.url = "https://scholar.google.com/citations?view_op=list_works&user=vdh-mOQAAAAJ"
    req.resource_type = "xhr"
    req.is_navigation_request.return_value = False
    listeners["request"](req)

    resp = MagicMock()
    resp.request = req
    resp.status = 429
    resp.url = req.url
    listeners["response"](resp)

    assert stats.has_429 is True

    # Now _check_blocked must immediately raise AccessBlockedError
    try:
        _check_blocked(mock_page, status=200, stats=stats)
        raise AssertionError("Expected AccessBlockedError on XHR 429 during SHOW MORE")
    except AccessBlockedError as e:
        assert "429" in str(e)
        assert "rate limited" in str(e)

    print("PASS 3. test_xhr_429_during_show_more_stops_extraction")


def test_manual_navigation_updates_final_http_status():
    mock_page = MagicMock()
    mock_page.url = "https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ"
    stats = DiagnosticNetworkStats()

    # Initial response was 429
    data = ProfileData(requested_url=mock_page.url, google_scholar_id="vdh-mOQAAAAJ")
    data.http_status = 429

    listeners = {}
    mock_page.on = lambda ev, fn: listeners.update({ev: fn})
    mock_page.main_frame = MagicMock()
    setup_diagnostic_listeners(mock_page, stats, MagicMock())

    # User manually navigates in main frame, receiving 200
    nav_req = MagicMock()
    nav_req.url = "https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ"
    nav_req.resource_type = "document"
    nav_req.is_navigation_request.return_value = True
    listeners["request"](nav_req)

    nav_resp = MagicMock()
    nav_resp.request = nav_req
    nav_resp.status = 200
    nav_resp.url = nav_req.url
    nav_resp.frame = mock_page.main_frame
    listeners["response"](nav_resp)

    assert stats.last_main_frame_status == 200

    # _verify_profile_owner must update data.http_status from stats.last_main_frame_status
    _verify_profile_owner(mock_page, data, stats=stats)
    assert data.http_status == 200
    assert data.loaded_url == mock_page.url

    print("PASS 4. test_manual_navigation_updates_final_http_status")


def test_failure_report_diagnostic_rendering():
    pdata = ProfileData(requested_url="https://scholar.google.com/citations?user=TEST")
    pdata.http_status = 429
    pdata.loaded_url = "https://www.google.com/sorry/index"
    pdata.network_stats = {
        "total_requests": 2,
        "navigation_count": 2,
        "requests_per_page_load": 1,
        "has_429": True,
        "sorry_redirect": True,
        "requests_by_type": {"document": 2},
        "requests_by_domain": {"scholar.google.com": 1, "www.google.com": 1},
        "navigation_events": [
            {"navigation_index": 1, "timestamp": "2026-11-01 12:00:00.000", "time_since_last_nav_s": None, "url": "https://scholar.google.com/citations?user=[REDACTED]"},
            {"navigation_index": 2, "timestamp": "2026-11-01 12:00:01.200", "time_since_last_nav_s": 1.2, "url": "https://www.google.com/sorry/index?q=[REDACTED]"},
        ],
    }

    rep = build_failure_report(
        url=pdata.requested_url,
        error="Google Scholar responded with HTTP 429",
        run_info={"timestamp_display": "test", "browser_mode": "msedge via Playwright"},
        data=pdata,
    )

    assert "NETWORK & REQUEST AUDIT DIAGNOSTICS" in rep
    assert "Total Network Requests  : 2" in rep
    assert "Navigation Count        : 2" in rep
    assert "HTTP 429 Detected       : YES" in rep
    assert "Google Sorry Redirect   : YES" in rep
    print("PASS test_failure_report_diagnostic_rendering")


def test_user_data_dir_isolation_rejects_default_profiles():
    import os
    appdata = os.environ.get("LOCALAPPDATA", "")
    if appdata:
        # Default Chrome profile must be rejected
        chrome_default = Path(appdata) / "Google" / "Chrome" / "User Data"
        try:
            validate_user_data_dir(chrome_default)
            raise AssertionError("Expected ValueError for default Chrome profile")
        except ValueError as e:
            assert "personal default browser profile" in str(e)

        # Subdirectory of default Chrome profile must be rejected
        chrome_sub = chrome_default / "Default"
        try:
            validate_user_data_dir(chrome_sub)
            raise AssertionError("Expected ValueError for default Chrome profile subfolder")
        except ValueError as e:
            assert "personal default browser profile" in str(e)

        # Edge default profile must be rejected
        edge_default = Path(appdata) / "Microsoft" / "Edge" / "User Data"
        try:
            validate_user_data_dir(edge_default)
            raise AssertionError("Expected ValueError for default Edge profile")
        except ValueError as e:
            assert "personal default browser profile" in str(e)

    # Dedicated project subfolder must pass
    validate_user_data_dir(Path("./.chrome_profile"))
    validate_user_data_dir(Path("./tests/_tmp_safe_profile"))
    print("PASS 10. test_user_data_dir_isolation_rejects_default_profiles")


def test_concurrent_persistent_profile_prevented():
    from playwright.sync_api import sync_playwright, Error as PlaywrightError
    lock_test_dir = ROOT / "tests" / "_tmp_concurrent_lock_test"
    lock_test_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        ctx1 = p.chromium.launch_persistent_context(
            str(lock_test_dir),
            channel="chrome",
            headless=True
        )
        blocked = False
        try:
            ctx2 = p.chromium.launch_persistent_context(
                str(lock_test_dir),
                channel="chrome",
                headless=True
            )
            ctx2.close()
        except PlaywrightError:
            blocked = True

        ctx1.close()

    import shutil
    shutil.rmtree(lock_test_dir, ignore_errors=True)
    assert blocked is True
    print("PASS 10. test_concurrent_persistent_profile_prevented")


if __name__ == "__main__":
    test_url_redaction()
    test_diagnostic_stats_collection_and_redaction()
    test_normal_mode_stops_immediately_on_429()
    test_manual_mode_reaches_await_manual_access_on_429()
    test_xhr_429_during_show_more_stops_extraction()
    test_manual_navigation_updates_final_http_status()
    test_failure_report_diagnostic_rendering()
    test_user_data_dir_isolation_rejects_default_profiles()
    test_concurrent_persistent_profile_prevented()
    print("\nALL REQUIRED TESTS PASSED")
