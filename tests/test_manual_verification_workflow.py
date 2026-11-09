"""Offline tests for legitimate manual verification and GUI browser options."""
import json
import threading
import time
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gui_app import ScholarScraperGUI
from run_scraper import PipelineResult, execute_scraper_pipeline
from scholar_scraper.browser import (
    AccessBlockedError,
    DiagnosticNetworkStats,
    ManualVerificationCancelled,
    ManualVerificationUnsuccessful,
    ProfileData,
    ProxyConfigurationError,
    JS_BLOCK_CHECK,
    JS_PAGE_STATE,
    _await_manual_access,
    build_playwright_proxy_settings,
    redact_proxy_url,
    scrape_publication_detail_on_page,
    scrape_publications_details_batch,
    validate_proxy_config,
)
from tests.test_cross_run_dedup import make_profile_a
from tests.test_safe_deletion_and_resume import fixture_dataset

DETAIL_URL = "https://scholar.google.com/citations?view_op=view_citation&user=A123&citation_for_view=A123:one"


class StatePage:
    def __init__(self, states, stats=None):
        self.states = iter(states)
        self.url = "https://www.google.com/sorry/index?continue=redacted"
        self.closed = False
        self.stats = stats

    def is_closed(self):
        return self.closed

    def evaluate(self, _script):
        state = next(self.states)
        if state == "detail":
            self.url = DETAIL_URL
            if self.stats:
                self.stats.last_main_frame_status = 200
                self.stats.last_main_frame_url = DETAIL_URL
                self.stats.has_429 = False
                self.stats.sorry_redirect = False
        return state


def test_manual_wait_reports_challenge_and_legitimate_success():
    stats = DiagnosticNetworkStats(last_main_frame_status=429, has_429=True, sorry_redirect=True)
    page = StatePage(["captcha", "detail"], stats)
    events = []
    with patch("scholar_scraper.browser.time.sleep", return_value=None):
        result = _await_manual_access(page, ProfileData(requested_url=DETAIL_URL), "HTTP 429", 5,
                                      expected_state="detail", progress_callback=events.append, stats=stats)
    assert result["http_status"] == 200
    assert [e["phase"] for e in events] == ["manual_verification_required", "manual_verification_waiting",
                                             "manual_verification_succeeded"]


def test_manual_wait_can_be_cancelled_without_navigation():
    event = threading.Event()
    event.set()
    page = StatePage(["captcha"])
    with pytest.raises(ManualVerificationCancelled):
        _await_manual_access(page, ProfileData(requested_url=DETAIL_URL), "CAPTCHA", 10,
                             expected_state="detail", cancel_event=event)


def test_closing_browser_is_manual_cancellation():
    page = StatePage(["captcha"])
    page.closed = True
    with pytest.raises(ManualVerificationCancelled):
        _await_manual_access(page, ProfileData(requested_url=DETAIL_URL), "CAPTCHA", 10,
                             expected_state="detail")


def test_unsolved_challenge_times_out_as_unsuccessful():
    page = StatePage(["captcha"])
    with pytest.raises(ManualVerificationUnsuccessful):
        _await_manual_access(page, ProfileData(requested_url=DETAIL_URL), "CAPTCHA", 0,
                             expected_state="detail")


def test_apparent_detail_with_429_is_not_accepted():
    stats = DiagnosticNetworkStats(last_main_frame_status=429, has_429=True)
    page = StatePage(["detail"])
    with pytest.raises(ManualVerificationUnsuccessful):
        _await_manual_access(page, ProfileData(requested_url=DETAIL_URL), "HTTP 429", 1,
                             expected_state="detail", stats=stats)


def test_http_429_without_interactive_challenge_stops_without_waiting():
    page = MagicMock()
    page.url = "https://www.google.com/sorry/index"
    page.goto.return_value = SimpleNamespace(status=429)
    page.evaluate.side_effect = lambda script: "unusual_traffic" if script == JS_PAGE_STATE else {
        "captcha": False, "unusual": True, "profile": False
    }
    with patch("scholar_scraper.browser._await_manual_access") as wait_mock:
        with pytest.raises(AccessBlockedError) as caught:
            scrape_publication_detail_on_page(page, DETAIL_URL, manual=True)
    assert caught.value.block_kind == "http_429_no_challenge"
    wait_mock.assert_not_called()


def test_detail_extracts_only_after_restored_200_and_correct_dom():
    page = MagicMock()
    page.url = "https://www.google.com/sorry/index"
    page.goto.return_value = SimpleNamespace(status=429)
    stats = DiagnosticNetworkStats(last_main_frame_status=429, has_429=True, sorry_redirect=True)
    raw = {"title": "Restored publication"}
    def evaluate(script):
        if script == JS_PAGE_STATE:
            return "captcha"
        if script == JS_BLOCK_CHECK:
            return {"captcha": False, "unusual": False, "profile": True}
        return raw
    page.evaluate.side_effect = evaluate
    def restore(*_args, **_kwargs):
        page.url = DETAIL_URL
        stats.last_main_frame_status = 200
        stats.last_main_frame_url = DETAIL_URL
        stats.has_429 = False
        stats.sorry_redirect = False
        return {"state": "detail", "http_status": 200, "url": DETAIL_URL}
    parsed = {"title": "Restored publication", "publication_url": DETAIL_URL, "yearly_citations": []}
    with patch("scholar_scraper.browser._await_manual_access", side_effect=restore), \
         patch("scholar_scraper.browser.parse_detail_dom_payload", return_value=dict(parsed)):
        result = scrape_publication_detail_on_page(page, DETAIL_URL, stats=stats, manual=True)
    assert result["http_status"] == 200 and result["title"] == "Restored publication"
    page.wait_for_selector.assert_called_once()


def test_blocked_batch_preserves_prior_checkpoint_success(tmp_path):
    snapshot = tmp_path / "checkpoint.json"
    existing = {"publications": {"1": {"publication_id": "1", "scholar_id": "1",
                "publication_url": DETAIL_URL, "canonical_detail_url": DETAIL_URL,
                "status": "SUCCESS", "metadata": {"title": "Old success"}}}}
    snapshot.write_text(json.dumps(existing), encoding="utf-8")
    blocked = MagicMock()
    blocked.url = "https://www.google.com/sorry/index"
    blocked.goto.return_value = SimpleNamespace(status=429)
    blocked.evaluate.return_value = {"captcha": False, "unusual": True, "profile": False}
    queue = [{"publication_id": "2", "scholar_id": "1", "title": "Pending", "source_url": DETAIL_URL + "2"}]
    result = scrape_publications_details_batch(queue, snapshot, page_override=blocked, delay=0, manual=False)
    saved = json.loads(snapshot.read_text(encoding="utf-8"))
    assert saved["publications"]["1"]["status"] == "SUCCESS"
    assert saved["publications"]["2"]["status"] == "BLOCKED"
    assert result["summary"]["stopped_by_block"] is True


def test_cancelled_manual_batch_stops_before_next_publication(tmp_path):
    snapshot = tmp_path / "cancelled.json"
    queue = [
        {"publication_id": "1", "scholar_id": "1", "title": "First", "source_url": DETAIL_URL},
        {"publication_id": "2", "scholar_id": "1", "title": "Second", "source_url": DETAIL_URL + "2"},
    ]
    calls = []
    def cancel_once(_page, detail_url, **_kwargs):
        calls.append(detail_url)
        raise ManualVerificationCancelled("cancelled")
    with patch("scholar_scraper.browser.scrape_publication_detail_on_page", side_effect=cancel_once):
        result = scrape_publications_details_batch(queue, snapshot, page_override=MagicMock(), delay=0, manual=True)
    saved = json.loads(snapshot.read_text(encoding="utf-8"))
    assert len(calls) == 1
    assert saved["publications"]["1"]["status"] == "CANCELLED"
    assert "2" not in saved["publications"]
    assert result["summary"]["manual_verification_outcome"] == "CANCELLED"


def _pump(gui, root, timeout=5):
    deadline = time.monotonic() + timeout
    while gui.is_running and time.monotonic() < deadline:
        root.update()
        gui.process_pending_events()
        time.sleep(0.01)
    gui.process_pending_events()


def test_gui_manual_option_forces_visible_stage2_and_passes_cancel_event(tmp_path):
    fixture_dataset(tmp_path)
    captured = {}
    def runner(**kwargs):
        captured.update(kwargs)
        return PipelineResult(0, "PARTIAL", "pending", "A123", "Scholar A", 2, 0, 0, 2,
                              output_dir=tmp_path)
    root = tk.Tk(); root.withdraw()
    gui = ScholarScraperGUI(root, output_dir=tmp_path, pipeline_runner=runner, show_dialogs=False)
    try:
        assert gui.persistent_var.get() is True
        assert gui.headed_var.get() is False and gui.manual_var.get() is False
        assert gui.connection_mode_var.get() == "direct"
        assert str(gui.proxy_entry["state"]) == "disabled"
        gui.connection_mode_var.set("proxy"); gui._on_connection_mode_changed()
        assert str(gui.proxy_entry["state"]) == "normal"
        assert len(gui.option_tooltips) == 6
        gui.connection_mode_var.set("direct"); gui._on_connection_mode_changed()
        gui.set_url("https://scholar.google.com/citations?user=A123")
        gui.manual_var.set(True)
        gui.headed_var.set(False)
        assert gui.start_step2_full_scraping()
        _pump(gui, root)
        assert captured["manual"] is True and captured["headed"] is True
        assert captured["scrape_details"] is True
        assert captured["manual_cancel_event"] is gui.manual_cancel_event
        gui.is_running = True
        gui._handle_progress_event({"phase": "manual_verification_required", "status": "CAPTCHA",
                                    "message": "Complete verification in Chrome."})
        root.update()
        assert str(gui.cancel_manual_btn["state"]) == "normal"
        gui.cancel_manual_verification()
        assert gui.manual_cancel_event.is_set()
        assert str(gui.cancel_manual_btn["state"]) == "disabled"
        gui.is_running = False
    finally:
        gui._on_close()


def test_backend_passes_same_persistent_profile_to_both_stages(tmp_path):
    profile = make_profile_a([("Paper", "2021", "US Khan")])
    profile_args = {}
    detail_args = {}
    def profile_fn(*_args, **kwargs):
        profile_args.update(kwargs); return profile
    def details_fn(**kwargs):
        detail_args.update(kwargs)
        return {"summary": {"total_unique_publications": 1}, "publications": {}}
    profile_dir = tmp_path / ".chrome_profile"
    execute_scraper_pipeline(url=profile.requested_url, output_dir=tmp_path, scrape_details=True,
                             persistent=True, user_data_dir=profile_dir, headed=True, manual=True,
                             scrape_profile_fn=profile_fn, scrape_details_fn=details_fn)
    assert profile_args["persistent"] is True and detail_args["persistent"] is True
    assert profile_args["user_data_dir"] == profile_dir == detail_args["user_data_dir"]
    assert profile_args["headless"] is False and detail_args["headless"] is False


def test_direct_connection_bypasses_ambient_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    proxy, args, diag = build_playwright_proxy_settings(None)
    assert proxy is None and args == ["--no-proxy-server"]
    assert diag.connection_mode == "direct" and diag.proxy_configured is False


def test_configured_proxy_credentials_are_separate_and_redacted():
    cfg = validate_proxy_config("http://alice:secret@example.test:8080")
    assert cfg["playwright_proxy"] == {"server": "http://example.test:8080", "username": "alice", "password": "secret"}
    assert cfg["redacted"] == "http://example.test:8080"
    assert redact_proxy_url("http://alice:secret@example.test:8080") == "http://example.test:8080"


@pytest.mark.parametrize("value", ["example.test:8080", "ftp://example.test:21", "http://example.test", "http://bad host:80"])
def test_invalid_proxy_is_rejected(value):
    with pytest.raises(ProxyConfigurationError):
        validate_proxy_config(value)

# All cases in this module are offline; no Google Scholar request is made.
