"""
Offline Test Suite for Browser Proxy & Network Connection Handling (`tests/test_proxy_and_connection.py`).

Covers all 10 required scenarios from the specification:
  1. Direct connection without a proxy (`--no-proxy-server`, bypasses broken OS/env proxy).
  2. Valid proxy configuration (`http`, `https`, `socks5`, auth credentials, reachability check).
  3. Invalid proxy configuration (`ProxyConfigurationError` before starting scraping).
  4. `ERR_PROXY_CONNECTION_FAILED` detection (`ProxyConnectionError` with suggested message & failed URL).
  5. General network connection failure (`NetworkConnectionError` for DNS / internet disconnected / timeout).
  6. HTTP 429 detection remains unchanged (`AccessBlockedError`).
  7. CAPTCHA detection remains unchanged (`AccessBlockedError`).
  8. GUI correctly displays proxy errors (resets progress bar to 0%, status `Proxy Connection Failed`,
     shows suggested message + failed URL, stays responsive for retry).
  9. Failed navigation does not corrupt existing CSV files (byte-for-byte integrity across all 6 CSVs).
  10. Dedicated Chrome profile (`.chrome_profile`) remains functional with direct connection.

Run:  python tests/test_proxy_and_connection.py
"""

import hashlib
import os
import shutil
import socket
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from unittest.mock import MagicMock, patch

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gui_app import ScholarScraperGUI  # noqa: E402
from run_scraper import DEFAULT_DOCX, execute_scraper_pipeline  # noqa: E402
from scholar_scraper.browser import (  # noqa: E402
    SUGGESTED_PROXY_ERROR_MESSAGE,
    AccessBlockedError,
    BrowserConnectionDiagnostics,
    NetworkConnectionError,
    ProfileData,
    ProxyConfigurationError,
    ProxyConnectionError,
    _launch,
    _launch_persistent,
    build_playwright_proxy_settings,
    check_proxy_reachable,
    classify_navigation_error,
    scrape_profile,
    validate_proxy_config,
)
from scholar_scraper.reporting import (  # noqa: E402
    build_failure_report,
    write_all_csvs,
)
from scholar_scraper.transform import transform  # noqa: E402
from tests.test_gui_integration import (  # noqa: E402
    make_profile_for_scholar_a,
    wait_for_gui_job,
)

TMP = Path(__file__).resolve().parent / "_tmp_proxy_tests"


def sha256_of_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot_csv_hashes(out_dir: Path) -> dict[str, str]:
    hashes = {}
    for csv_file in sorted(out_dir.glob("*.csv")):
        hashes[csv_file.name] = sha256_of_file(csv_file)
    return hashes


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    passed = 0
    failed = 0

    def check(name: str, cond: bool, detail: str = ""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"PASS  {name}")
        else:
            failed += 1
            print(f"FAIL  {name}" + (f" -> {detail}" if detail else ""))

    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True, exist_ok=True)

    root = tk.Tk()
    root.withdraw()

    target_url = "https://scholar.google.com/citations?hl=en&user=niDZqjYAAAAJ"

    try:
        # =================================================================== #
        # Scenario 1: Direct connection without a proxy
        # =================================================================== #
        with patch.dict(os.environ, {"HTTP_PROXY": "socks5://127.0.0.1:9050"}, clear=False):
            pw_proxy, launch_args, diag_direct = build_playwright_proxy_settings(
                proxy_url=None, verify_reachable=False, target_url=target_url
            )
        check(
            "1a. Direct connection builds --no-proxy-server args and detects/bypasses ambient proxy",
            pw_proxy is None
            and "--no-proxy-server" in launch_args
            and diag_direct.connection_mode == "direct"
            and "--no-proxy-server" in diag_direct.proxy_status
            and diag_direct.ambient_os_proxy_detected is not None,
        )

        # Verify _launch passes --no-proxy-server to Playwright Chromium launch in direct mode
        mock_p = MagicMock()
        mock_browser = MagicMock()
        mock_p.chromium.launch.return_value = mock_browser
        dummy_data = ProfileData(requested_url=target_url)
        _launch(mock_p, headless=True, channel="chrome", data=dummy_data, proxy_url=None)
        launch_call_kwargs = mock_p.chromium.launch.call_args.kwargs
        check(
            "1b. _launch passes args=['--no-proxy-server'] when no proxy is configured",
            "--no-proxy-server" in launch_call_kwargs.get("args", [])
            and "proxy" not in launch_call_kwargs
            and dummy_data.browser_used == "chrome",
        )

        # =================================================================== #
        # Scenario 2: Valid proxy configuration
        # =================================================================== #
        # Start a local ephemeral TCP listener to test valid proxy config + reachability
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        ephemeral_port = srv.getsockname()[1]
        try:
            valid_http = validate_proxy_config(f"http://user1:pass1@127.0.0.1:{ephemeral_port}")
            valid_socks = validate_proxy_config(f"socks5://127.0.0.1:{ephemeral_port}")
            reachable_ok, reach_detail = check_proxy_reachable(valid_http, timeout_s=1.5)
            pw_proxy_cfg, proxy_args, diag_proxy = build_playwright_proxy_settings(
                proxy_url=f"http://user1:pass1@127.0.0.1:{ephemeral_port}",
                verify_reachable=True,
                target_url=target_url,
            )
            check(
                "2. Valid proxy configuration is parsed, verified reachable, and builds Playwright proxy dict",
                valid_http["server"] == f"http://127.0.0.1:{ephemeral_port}"
                and valid_http["username"] == "user1"
                and valid_http["password"] == "pass1"
                and valid_socks["scheme"] == "socks5"
                and reachable_ok is True
                and pw_proxy_cfg["server"] == f"http://127.0.0.1:{ephemeral_port}"
                and f"--proxy-server=http://127.0.0.1:{ephemeral_port}" in proxy_args
                and diag_proxy.connection_mode == "proxy"
                and "reachable" in diag_proxy.proxy_status.lower(),
            )
        finally:
            srv.close()

        # =================================================================== #
        # Scenario 3: Invalid proxy configuration
        # =================================================================== #
        invalid_proxies = [
            "ftp://127.0.0.1:8080",         # unsupported scheme
            "http://:8080",                 # missing hostname
            "http://127.0.0.1",             # missing port
            "http://127.0.0.1:99999",       # out-of-range port
            "http://127.0.0.1:notaport",    # non-integer port
            "http://bad host:8080",         # whitespace in host
        ]
        all_invalid_caught = True
        for bad_p in invalid_proxies:
            try:
                validate_proxy_config(bad_p)
                all_invalid_caught = False
            except ProxyConfigurationError:
                pass

        # Also verify unreachable configured proxy raises ProxyConnectionError pre-flight
        unreachable_caught = False
        try:
            build_playwright_proxy_settings(
                proxy_url="socks5://127.0.0.1:59999",
                verify_reachable=True,
                target_url=target_url,
            )
        except ProxyConnectionError as exc:
            unreachable_caught = SUGGESTED_PROXY_ERROR_MESSAGE in str(exc)

        check(
            "3. Invalid proxy configurations raise ProxyConfigurationError and unreachable proxy fails pre-flight",
            all_invalid_caught and unreachable_caught,
        )

        # =================================================================== #
        # Scenario 4: ERR_PROXY_CONNECTION_FAILED detection
        # =================================================================== #
        sim_pw_err = Exception(
            f"Page.goto: net::ERR_PROXY_CONNECTION_FAILED at {target_url}\n"
            f"Call log:\n  - navigating to \"{target_url}\", waiting until \"domcontentloaded\""
        )
        diag_err_proxy = BrowserConnectionDiagnostics(
            browser_launched=True,
            browser_name="chrome (direct)",
            connection_mode="direct",
            target_url=target_url,
        )
        classified_proxy_exc = classify_navigation_error(
            sim_pw_err,
            target_url,
            proxy_url="socks5://127.0.0.1:9050",
            diagnostics=diag_err_proxy,
            context_label="profile",
        )
        check(
            "4. ERR_PROXY_CONNECTION_FAILED is classified as ProxyConnectionError with suggested message & URL",
            isinstance(classified_proxy_exc, ProxyConnectionError)
            and classified_proxy_exc.error_code == "ERR_PROXY_CONNECTION_FAILED"
            and classified_proxy_exc.failed_url == target_url
            and SUGGESTED_PROXY_ERROR_MESSAGE in str(classified_proxy_exc)
            and target_url in str(classified_proxy_exc)
            and diag_err_proxy.failure_category == "proxy_connection"
            and diag_err_proxy.network_connected is False
            and diag_err_proxy.scholar_accessible is False,
        )

        # =================================================================== #
        # Scenario 5: General network connection failure
        # =================================================================== #
        net_errors = [
            ("ERR_INTERNET_DISCONNECTED", f"Page.goto: net::ERR_INTERNET_DISCONNECTED at {target_url}"),
            ("ERR_NAME_NOT_RESOLVED", f"Page.goto: net::ERR_NAME_NOT_RESOLVED at {target_url}"),
            ("ERR_CONNECTION_TIMED_OUT", f"Page.goto: net::ERR_CONNECTION_TIMED_OUT at {target_url}"),
        ]
        all_net_ok = True
        for expected_code, raw_msg in net_errors:
            diag_net = BrowserConnectionDiagnostics(
                browser_launched=True,
                connection_mode="direct",
                target_url=target_url,
            )
            exc_net = classify_navigation_error(
                Exception(raw_msg),
                target_url,
                proxy_url=None,
                diagnostics=diag_net,
                context_label="profile",
            )
            if not (
                isinstance(exc_net, NetworkConnectionError)
                and not isinstance(exc_net, ProxyConnectionError)
                and exc_net.error_code == expected_code
                and exc_net.failed_url == target_url
                and diag_net.failure_category == "network"
                and diag_net.network_connected is False
            ):
                all_net_ok = False
        check(
            "5. General network failures (DNS, internet disconnected, timeout) raise NetworkConnectionError",
            all_net_ok,
        )

        # =================================================================== #
        # Scenario 6: HTTP 429 detection remains unchanged
        # =================================================================== #
        out_429 = TMP / "out_429"
        with patch("run_scraper.scrape_profile") as mock_sp_429:
            mock_sp_429.side_effect = AccessBlockedError(
                f"Google Scholar returned HTTP 429 for {target_url}. Halting immediately."
            )
            res_429 = execute_scraper_pipeline(
                url=target_url,
                output_dir=out_429,
                docx_path=DEFAULT_DOCX,
                scrape_details=False,
            )
        check(
            "6. HTTP 429 detection remains unchanged and stops immediately with status=BLOCKED",
            res_429.status == "BLOCKED"
            and res_429.stopped_by_block is True
            and "429" in (res_429.message or "")
            and mock_sp_429.call_count == 1,
        )

        # =================================================================== #
        # Scenario 7: CAPTCHA detection remains unchanged
        # =================================================================== #
        out_captcha = TMP / "out_captcha"
        with patch("run_scraper.scrape_profile") as mock_sp_captcha:
            mock_sp_captcha.side_effect = AccessBlockedError(
                f"Blocked or CAPTCHA page detected on {target_url}: reason=captcha selector '#gs_captcha_c'"
            )
            res_captcha = execute_scraper_pipeline(
                url=target_url,
                output_dir=out_captcha,
                docx_path=DEFAULT_DOCX,
                scrape_details=False,
            )
        check(
            "7. CAPTCHA detection remains unchanged and stops immediately with status=BLOCKED",
            res_captcha.status == "BLOCKED"
            and res_captcha.stopped_by_block is True
            and "CAPTCHA" in (res_captcha.message or "")
            and mock_sp_captcha.call_count == 1,
        )

        # =================================================================== #
        # Scenario 8: GUI correctly displays proxy errors, resets progress to 0%,
        #             shows suggested message & failed URL, and stays responsive
        # =================================================================== #
        out_gui = TMP / "out_gui"
        out_gui.mkdir(parents=True, exist_ok=True)
        gui = ScholarScraperGUI(root, output_dir=out_gui, docx_path=DEFAULT_DOCX, show_dialogs=False)
        gui.set_url(target_url)

        # 8a: Invalid proxy in GUI input field caught before launching worker
        gui.set_proxy("ftp://invalid-scheme:8080")
        started_invalid_proxy = gui.start_step1_profile_scraping()
        check(
            "8a. GUI catches invalid proxy configuration before starting background scrape",
            started_invalid_proxy is False
            and gui.status_var.get() == "Invalid Proxy Configuration"
            and gui.progress_pct_var.get() == 0.0
            and "Invalid Proxy Configuration" in gui.banner_message_var.get(),
        )

        # 8b: Simulated ERR_PROXY_CONNECTION_FAILED during scraping in GUI
        gui.set_proxy(None)  # switch back to Direct Connection
        with patch("run_scraper.scrape_profile") as mock_sp_proxy_err:
            mock_sp_proxy_err.side_effect = ProxyConnectionError(
                f"{SUGGESTED_PROXY_ERROR_MESSAGE}\n\n"
                f"Browser could not load the profile page (ERR_PROXY_CONNECTION_FAILED).\n"
                f"URL: {target_url}",
                failed_url=target_url,
                error_code="ERR_PROXY_CONNECTION_FAILED",
            )
            started_proxy_err = gui.start_step1_profile_scraping()
            wait_for_gui_job(gui, root)

        check(
            "8b. GUI detects ERR_PROXY_CONNECTION_FAILED, shows suggested message + URL, and resets progress to 0%",
            started_proxy_err is True
            and gui.is_running is False
            and gui.status_var.get() == "Proxy Connection Failed"
            and gui.progress_pct_var.get() == 0.0
            and gui.progress_label_var.get() == "0%"
            and gui.researcher_var.get() == "Connection Failed (niDZqjYAAAAJ)"
            and SUGGESTED_PROXY_ERROR_MESSAGE in gui.banner_message_var.get()
            and target_url in gui.banner_message_var.get(),
            detail=f"status={gui.status_var.get()}, pct={gui.progress_pct_var.get()}, banner={gui.banner_message_var.get()}",
        )

        # 8c: GUI remains responsive and allows retry after fixing connection
        with patch("run_scraper.scrape_profile") as mock_sp_retry:
            retry_prof = make_profile_for_scholar_a()
            retry_prof.requested_url = target_url
            retry_prof.loaded_url = target_url
            retry_prof.google_scholar_id = "niDZqjYAAAAJ"
            mock_sp_retry.return_value = retry_prof
            started_retry = gui.start_step1_profile_scraping()
            wait_for_gui_job(gui, root)

        check(
            "8c. GUI remains responsive after proxy error and succeeds on retry",
            started_retry is True
            and gui.is_running is False
            and gui.status_var.get() == "Completed Successfully"
            and gui.progress_pct_var.get() == 100.0,
        )

        # =================================================================== #
        # Scenario 9: Failed navigation does not corrupt existing CSV files
        # =================================================================== #
        out_csv_safe = TMP / "out_csv_safe"
        out_csv_safe.mkdir(parents=True, exist_ok=True)
        # Seed valid CSV dataset for Scholar A (vdh-mOQAAAAJ)
        with patch("run_scraper.scrape_profile") as mock_seed:
            mock_seed.return_value = make_profile_for_scholar_a()
            seed_res = execute_scraper_pipeline(
                url="https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ",
                output_dir=out_csv_safe,
                docx_path=DEFAULT_DOCX,
                scrape_details=False,
            )
        hashes_before = snapshot_csv_hashes(out_csv_safe)

        # Now run pipeline for new scholar niDZqjYAAAAJ where navigation fails with ERR_PROXY_CONNECTION_FAILED
        with patch("run_scraper.scrape_profile") as mock_sp_fail:
            mock_sp_fail.side_effect = ProxyConnectionError(
                f"{SUGGESTED_PROXY_ERROR_MESSAGE}\nURL: {target_url}",
                failed_url=target_url,
                error_code="ERR_PROXY_CONNECTION_FAILED",
            )
            fail_res = execute_scraper_pipeline(
                url=target_url,
                output_dir=out_csv_safe,
                docx_path=DEFAULT_DOCX,
                scrape_details=True,
            )
        hashes_after = snapshot_csv_hashes(out_csv_safe)
        check(
            "9. Failed navigation (ERR_PROXY_CONNECTION_FAILED) leaves all 6 existing CSV files byte-for-byte untouched",
            seed_res.exit_code == 0
            and fail_res.status == "PROXY_ERROR"
            and len(hashes_before) == 6
            and hashes_before == hashes_after,
        )

        # =================================================================== #
        # Scenario 10: Dedicated Chrome profile (.chrome_profile) remains
        #              functional with direct connection (--no-proxy-server)
        # =================================================================== #
        tmp_profile_dir = TMP / "test_chrome_profile"
        persist_data = ProfileData(requested_url=target_url)
        with sync_playwright() as p:
            context = _launch_persistent(
                p,
                user_data_dir=tmp_profile_dir,
                headless=True,
                channel="chrome",
                data=persist_data,
                timeout_ms=15000,
                proxy_url=None,
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.set_content("<html><head><title>Persistent Profile Test</title></head><body>OK</body></html>")
                title = page.title()
            finally:
                context.close()

        check(
            "10. Dedicated persistent Chrome profile (.chrome_profile) launches and functions in direct mode",
            tmp_profile_dir.exists()
            and "persistent" in persist_data.browser_used
            and title == "Persistent Profile Test",
            detail=f"browser_used={persist_data.browser_used}, title={title}",
        )

    finally:
        root.destroy()
        if TMP.exists():
            shutil.rmtree(TMP, ignore_errors=True)

    print(f"\nSummary: {passed} passed, {failed} failed out of {passed + failed} tests.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
