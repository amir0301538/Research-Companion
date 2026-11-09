"""
Desktop GUI for the Google Scholar Research Scraper.

Built with Python's standard-library `tkinter` and `tkinter.ttk` (zero external
GUI dependencies) and integrated directly with the existing scraping pipeline
in `run_scraper.execute_scraper_pipeline` and `scholar_scraper.*`.

Launch command:
    python gui_app.py
"""

import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from run_scraper import DEFAULT_DOCX, HERE, PipelineResult, execute_scraper_pipeline
from scholar_scraper.browser import (
    PROXY_ERROR_CODES,
    SUGGESTED_PROXY_ERROR_MESSAGE,
    InvalidProfileUrlError,
    ProxyConfigurationError,
    parse_google_scholar_id,
    redact_proxy_url,
    validate_proxy_config,
)
from scholar_scraper.reporting import (
    CsvPermissionError,
    find_existing_scholar_by_google_id,
    verify_csv_files_writable,
)
from scholar_scraper.dataset_ops import delete_scholar, preview_deletion

PLACEHOLDER_TEXT = "Paste Google Scholar Profile URL here..."


def format_already_stored_notification(existing_row: dict) -> str:
    """
    Build the exact formatted GUI notification when a Google Scholar profile
    already exists in `output/scholars.csv`.
    """
    scholar_name = (existing_row.get("scholar_name") or "").strip() or "Unknown"
    gs_id = (existing_row.get("google_scholar_id") or "").strip()
    scholar_id = str(existing_row.get("scholar_id") or "").strip()
    return (
        "⚠️ Scholar Already Stored!\n\n"
        "Scholar Already Stored — Profile front-page data already exists.\n"
        "This Google Scholar profile already exists in your dataset.\n\n"
        f"- Scholar Name: {scholar_name}\n"
        f"- Google Scholar ID: {gs_id}\n"
        f"- Scholar ID: {scholar_id}\n\n"
        "The scholar is already stored. No new scraping or CSV modifications were performed."
    )


def validate_scholar_profile_url(url: str) -> str:
    """
    Validate a Google Scholar profile URL and return its extracted `user` ID.
    Raises `InvalidProfileUrlError` with a clear, user-friendly explanation if:
      - The URL is empty or still contains the placeholder text.
      - The URL scheme is not http/https or domain is not scholar.google.*.
      - The path is not `/citations`.
      - The URL is a single publication detail URL (`view_op=view_citation`) rather than a profile URL.
      - The `user` query parameter is missing or empty.
    """
    cleaned = (url or "").strip()
    if not cleaned or cleaned == PLACEHOLDER_TEXT:
        raise InvalidProfileUrlError("Please enter a Google Scholar profile URL before starting.")
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https"):
        raise InvalidProfileUrlError(
            "Invalid URL scheme. Please include 'https://' (e.g., https://scholar.google.com/citations?hl=en&user=...)."
        )
    qs = parse_qs(parsed.query)
    if qs.get("view_op", [None])[0] == "view_citation":
        raise InvalidProfileUrlError(
            "This URL points to a single publication detail page (view_op=view_citation). "
            "Please paste the researcher's main Google Scholar profile URL instead."
        )
    return parse_google_scholar_id(cleaned)


class QueueLogHandler(logging.Handler):
    """Thread-safe logging handler that forwards formatted log lines to the GUI queue."""

    def __init__(self, event_queue: "queue.Queue[dict]"):
        super().__init__()
        self.event_queue = event_queue
        self.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            self.event_queue.put({"type": "log", "text": msg, "level": record.levelname})
        except Exception:
            self.handleError(record)


class ToolTip:
    """Small native Tk tooltip used by the browser-option controls."""

    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self.window = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event=None):
        if self.window or not self.text:
            return
        x = self.widget.winfo_rootx() + 16
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_geometry(f"+{x}+{y}")
        tk.Label(self.window, text=self.text, justify=tk.LEFT, wraplength=360,
                 bg="#FFFBEB", fg="#1E293B", relief=tk.SOLID, bd=1,
                 font=("Segoe UI", 9), padx=7, pady=4).pack()

    def _hide(self, _event=None):
        if self.window:
            self.window.destroy()
            self.window = None


class ScholarScraperGUI:
    """
    Main desktop window for the Google Scholar Research Scraper.
    """

    def __init__(
        self,
        root: tk.Tk,
        output_dir: Path = HERE / "output",
        docx_path: Path = DEFAULT_DOCX,
        pipeline_runner: Optional[Callable[..., PipelineResult]] = None,
        show_dialogs: bool = True,
        confirm_delete_fn: Optional[Callable[[str, str], bool]] = None,
    ):
        self.root = root
        self.output_dir = Path(output_dir)
        self.docx_path = Path(docx_path)
        self.pipeline_runner = pipeline_runner or execute_scraper_pipeline
        self.show_dialogs = show_dialogs
        self.confirm_delete_fn = confirm_delete_fn

        self.event_queue: "queue.Queue[dict]" = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None
        self.run_lock = threading.Lock()
        self.manual_cancel_event = threading.Event()
        self.is_running = False
        self.last_result: Optional[PipelineResult] = None
        self.last_existing_scholar: Optional[dict] = None
        self.last_notification_text: str = ""
        self._placeholder_active = False

        # Attach thread-safe log handler to root logger so all `scholar_scraper` and `run_scraper` logs appear live
        self.log_handler = QueueLogHandler(self.event_queue)
        self.log_handler.setLevel(logging.INFO)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.file_log_handler = logging.FileHandler(
            self.output_dir / "scraper.log", mode="a", encoding="utf-8"
        )
        self.file_log_handler.setLevel(logging.INFO)
        self.file_log_handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
        ))
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(self.log_handler)
        root_logger.addHandler(self.file_log_handler)

        self._configure_window()
        self._build_styles()
        self._build_widgets()
        self._schedule_queue_poll()

    def _configure_window(self):
        self.root.title("Google Scholar Research Scraper")
        self.root.geometry("940x860")
        self.root.minsize(820, 740)
        self.root.configure(bg="#F4F6F9")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_styles(self):
        self.style = ttk.Style(self.root)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        self.style.configure(
            "Scraper.Horizontal.TProgressbar",
            troughcolor="#E2E8F0",
            background="#1D4ED8",
            bordercolor="#CBD5E1",
            lightcolor="#3B82F6",
            darkcolor="#1E40AF",
            thickness=18,
        )

    def _build_widgets(self):
        # ---------------- A. APPLICATION HEADER ----------------
        header_frame = tk.Frame(self.root, bg="#0F172A", padx=24, pady=16)
        header_frame.pack(fill=tk.X)

        title_lbl = tk.Label(
            header_frame,
            text="Google Scholar Research Scraper",
            font=("Segoe UI", 17, "bold"),
            fg="#FFFFFF",
            bg="#0F172A",
            anchor="w",
        )
        title_lbl.pack(fill=tk.X)

        subtitle_lbl = tk.Label(
            header_frame,
            text="Extract researcher profiles, publications, and publication details.",
            font=("Segoe UI", 10),
            fg="#94A3B8",
            bg="#0F172A",
            anchor="w",
        )
        subtitle_lbl.pack(fill=tk.X, pady=(2, 0))

        # Main container
        main_frame = tk.Frame(self.root, bg="#F4F6F9", padx=22, pady=16)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # ---------------- B. GOOGLE SCHOLAR PROFILE URL INPUT ----------------
        url_card = tk.LabelFrame(
            main_frame,
            text=" 1. Google Scholar Profile URL ",
            font=("Segoe UI", 10, "bold"),
            fg="#1E293B",
            bg="#FFFFFF",
            bd=1,
            relief=tk.SOLID,
            padx=16,
            pady=12,
        )
        url_card.pack(fill=tk.X, pady=(0, 12))

        input_row = tk.Frame(url_card, bg="#FFFFFF")
        input_row.pack(fill=tk.X)

        self.url_var = tk.StringVar()
        self.url_entry = tk.Entry(
            input_row,
            textvariable=self.url_var,
            font=("Segoe UI", 11),
            fg="#0F172A",
            bg="#F8FAFC",
            relief=tk.SOLID,
            bd=1,
            insertbackground="#0F172A",
        )
        self.url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6, padx=(0, 10))
        self.url_entry.bind("<FocusIn>", self._on_url_focus_in)
        self.url_entry.bind("<FocusOut>", self._on_url_focus_out)
        self.url_entry.bind("<KeyRelease>", self._on_url_key_release)

        self.scholar_id_badge_var = tk.StringVar(value="Scholar ID: —")
        self.scholar_id_badge = tk.Label(
            input_row,
            textvariable=self.scholar_id_badge_var,
            font=("Consolas", 10, "bold"),
            fg="#334155",
            bg="#E2E8F0",
            padx=10,
            pady=6,
            relief=tk.FLAT,
        )
        self.scholar_id_badge.pack(side=tk.RIGHT)

        # Runtime options bar (Persistent Chrome profile & visible browser / manual CAPTCHA assist)
        opts_row = tk.Frame(url_card, bg="#FFFFFF")
        opts_row.pack(fill=tk.X, pady=(8, 0))

        self.persistent_var = tk.BooleanVar(value=True)
        self.headed_var = tk.BooleanVar(value=False)
        self.manual_var = tk.BooleanVar(value=False)

        self.persistent_check = tk.Checkbutton(
            opts_row,
            text="Use Dedicated Persistent Chrome Profile (.chrome_profile)",
            variable=self.persistent_var,
            font=("Segoe UI", 9),
            bg="#FFFFFF",
            fg="#334155",
            activebackground="#FFFFFF",
        )
        self.persistent_check.pack(side=tk.LEFT, padx=(0, 16))

        self.headed_check = tk.Checkbutton(
            opts_row,
            text="Show Browser Window",
            variable=self.headed_var,
            font=("Segoe UI", 9),
            bg="#FFFFFF",
            fg="#334155",
            activebackground="#FFFFFF",
        )
        self.headed_check.pack(side=tk.LEFT, padx=(0, 16))

        self.manual_check = tk.Checkbutton(
            opts_row,
            text="Manual Verification Mode (Pause on CAPTCHA)",
            variable=self.manual_var,
            font=("Segoe UI", 9),
            bg="#FFFFFF",
            fg="#334155",
            activebackground="#FFFFFF",
        )
        self.manual_check.pack(side=tk.LEFT)

        # Connection / Proxy Configuration Row
        proxy_row = tk.Frame(url_card, bg="#FFFFFF")
        proxy_row.pack(fill=tk.X, pady=(6, 0))

        self.connection_mode_var = tk.StringVar(value="direct")
        self.proxy_var = tk.StringVar(value="")

        tk.Label(
            proxy_row,
            text="Connection:",
            font=("Segoe UI", 9, "bold"),
            fg="#475569",
            bg="#FFFFFF",
        ).pack(side=tk.LEFT, padx=(0, 8))

        self.direct_radio = tk.Radiobutton(
            proxy_row,
            text="Direct Connection (Recommended)",
            variable=self.connection_mode_var,
            value="direct",
            command=self._on_connection_mode_changed,
            font=("Segoe UI", 9),
            bg="#FFFFFF",
            fg="#334155",
            activebackground="#FFFFFF",
        )
        self.direct_radio.pack(side=tk.LEFT, padx=(0, 12))

        self.proxy_radio = tk.Radiobutton(
            proxy_row,
            text="Configured Proxy:",
            variable=self.connection_mode_var,
            value="proxy",
            command=self._on_connection_mode_changed,
            font=("Segoe UI", 9),
            bg="#FFFFFF",
            fg="#334155",
            activebackground="#FFFFFF",
        )
        self.proxy_radio.pack(side=tk.LEFT, padx=(0, 6))

        self.proxy_entry = tk.Entry(
            proxy_row,
            textvariable=self.proxy_var,
            font=("Consolas", 9),
            fg="#0F172A",
            bg="#F8FAFC",
            relief=tk.SOLID,
            bd=1,
            width=34,
            state=tk.DISABLED,
        )
        self.proxy_entry.pack(side=tk.LEFT, ipady=2)

        self.option_tooltips = [
            ToolTip(self.persistent_check, "Save browser session data between runs in the dedicated .chrome_profile folder."),
            ToolTip(self.headed_check, "Display Chrome while scraping. Manual verification always turns this on."),
            ToolTip(self.manual_check, "Pause only when Google presents an interactive verification challenge."),
            ToolTip(self.direct_radio, "Use the normal network connection and bypass inherited system or environment proxies."),
            ToolTip(self.proxy_radio, "Use one user-specified HTTP, HTTPS, SOCKS4, or SOCKS5 proxy server."),
            ToolTip(self.proxy_entry, "Proxy format: scheme://host:port. Credentials may be included as scheme://user:password@host:port."),
        ]

        self._set_placeholder()

        # ---------------- C. SCRAPING AND DELETION BUTTONS ----------------
        btn_card = tk.Frame(main_frame, bg="#F4F6F9")
        btn_card.pack(fill=tk.X, pady=(0, 12))
        btn_card.columnconfigure(0, weight=1)
        btn_card.columnconfigure(1, weight=1)

        # Button 1 Frame (Step 1 — Profile Scraping)
        b1_box = tk.Frame(btn_card, bg="#FFFFFF", bd=1, relief=tk.SOLID, padx=12, pady=10)
        b1_box.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        self.step1_btn = tk.Button(
            b1_box,
            text="STEP 1 — PROFILE SCRAPING",
            command=self.start_step1_profile_scraping,
            font=("Segoe UI", 11, "bold"),
            fg="#FFFFFF",
            bg="#0284C7",
            activeforeground="#FFFFFF",
            activebackground="#0369A1",
            disabledforeground="#E2E8F0",
            relief=tk.FLAT,
            cursor="hand2",
            pady=8,
        )
        self.step1_btn.pack(fill=tk.X)

        tk.Label(
            b1_box,
            text="Extract everything available from the researcher's Google Scholar profile front page "
                 "(auto-clicks SHOW MORE, deduplicates CSVs, never opens detail pages).",
            font=("Segoe UI", 8),
            fg="#475569",
            bg="#FFFFFF",
            wraplength=380,
            justify=tk.LEFT,
        ).pack(fill=tk.X, pady=(6, 0))

        # Button 2 Frame (Step 2 — Full Publication Scraping - Emphasized)
        b2_box = tk.Frame(btn_card, bg="#FFFFFF", bd=2, relief=tk.SOLID, padx=12, pady=10)
        b2_box.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        self.step2_btn = tk.Button(
            b2_box,
            text="STEP 2 — FULL PUBLICATION SCRAPING",
            command=self.start_step2_full_scraping,
            font=("Segoe UI", 11, "bold"),
            fg="#FFFFFF",
            bg="#15803D",
            activeforeground="#FFFFFF",
            activebackground="#166534",
            disabledforeground="#E2E8F0",
            relief=tk.FLAT,
            cursor="hand2",
            pady=8,
        )
        self.step2_btn.pack(fill=tk.X)

        tk.Label(
            b2_box,
            text="COMPLETE PIPELINE: Runs STEP 1 first AND sequentially visits every individual publication "
                 "detail page (with checkpoint/resume & yearly citations).",
            font=("Segoe UI", 8, "bold"),
            fg="#166534",
            bg="#FFFFFF",
            wraplength=380,
            justify=tk.LEFT,
        ).pack(fill=tk.X, pady=(6, 0))

        self.delete_btn = tk.Button(
            btn_card, text="DELETE SCHOLAR", command=self.start_delete_scholar,
            font=("Segoe UI", 11, "bold"), fg="#FFFFFF", bg="#B91C1C",
            activeforeground="#FFFFFF", activebackground="#991B1B",
            disabledforeground="#E2E8F0", relief=tk.FLAT, cursor="hand2", pady=8,
        )
        self.delete_btn.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        # ---------------- D. PROGRESS & STATUS DASHBOARD ----------------
        status_card = tk.LabelFrame(
            main_frame,
            text=" 2. Scraping Progress & Status ",
            font=("Segoe UI", 10, "bold"),
            fg="#1E293B",
            bg="#FFFFFF",
            bd=1,
            relief=tk.SOLID,
            padx=16,
            pady=12,
        )
        status_card.pack(fill=tk.X, pady=(0, 12))

        grid_frame = tk.Frame(status_card, bg="#FFFFFF")
        grid_frame.pack(fill=tk.X)
        for col_idx in range(4):
            grid_frame.columnconfigure(col_idx, weight=1)

        self.researcher_var = tk.StringVar(value="Waiting for input")
        self.status_var = tk.StringVar(value="Idle")
        self.pubs_found_var = tk.StringVar(value="0")
        self.pubs_processed_var = tk.StringVar(value="0")
        self.details_scraped_var = tk.StringVar(value="0")
        self.pubs_pending_var = tk.StringVar(value="0")
        self.current_pub_var = tk.StringVar(value="None")
        self.progress_pct_var = tk.DoubleVar(value=0.0)
        self.progress_label_var = tk.StringVar(value="0%")
        self.banner_message_var = tk.StringVar(value="Ready. Paste a Google Scholar profile URL above and select Step 1 or Step 2.")

        # Row 0: Researcher & Status
        self._add_metric_pair(grid_frame, 0, 0, "Researcher:", self.researcher_var, bold=True)
        self._add_metric_pair(grid_frame, 0, 2, "Status:", self.status_var, bold=True, value_color="#1D4ED8")

        # Row 1: Publications Found & Publications Processed
        self._add_metric_pair(grid_frame, 1, 0, "Publications Found:", self.pubs_found_var)
        self._add_metric_pair(grid_frame, 1, 2, "Publications Processed:", self.pubs_processed_var)

        # Row 2: Details Scraped & Pending Publications
        self._add_metric_pair(grid_frame, 2, 0, "Details Scraped:", self.details_scraped_var)
        self._add_metric_pair(grid_frame, 2, 2, "Pending Publications:", self.pubs_pending_var)

        # Row 3: Current Publication in Step 2
        cur_row = tk.Frame(status_card, bg="#FFFFFF")
        cur_row.pack(fill=tk.X, pady=(6, 4))
        tk.Label(
            cur_row,
            text="Current Publication (Step 2):",
            font=("Segoe UI", 9, "bold"),
            fg="#475569",
            bg="#FFFFFF",
        ).pack(side=tk.LEFT)
        tk.Label(
            cur_row,
            textvariable=self.current_pub_var,
            font=("Segoe UI", 9),
            fg="#0F172A",
            bg="#FFFFFF",
            anchor="w",
        ).pack(side=tk.LEFT, padx=(8, 0), fill=tk.X, expand=True)

        # Row 4: Progress Bar + Percentage
        prog_row = tk.Frame(status_card, bg="#FFFFFF")
        prog_row.pack(fill=tk.X, pady=(4, 6))
        tk.Label(
            prog_row,
            text="Progress:",
            font=("Segoe UI", 9, "bold"),
            fg="#475569",
            bg="#FFFFFF",
        ).pack(side=tk.LEFT)
        self.progress_pct_lbl = tk.Label(
            prog_row,
            textvariable=self.progress_label_var,
            font=("Segoe UI", 9, "bold"),
            fg="#0F172A",
            bg="#FFFFFF",
            width=6,
            anchor="e",
        )
        self.progress_pct_lbl.pack(side=tk.RIGHT)

        self.progress_bar = ttk.Progressbar(
            prog_row,
            style="Scraper.Horizontal.TProgressbar",
            orient=tk.HORIZONTAL,
            mode="determinate",
            maximum=100.0,
            variable=self.progress_pct_var,
        )
        self.progress_bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 8))

        # Status / Alert Banner (Completion, Error, or CAPTCHA / HTTP 429 Notice)
        self.banner_lbl = tk.Label(
            status_card,
            textvariable=self.banner_message_var,
            font=("Segoe UI", 9),
            fg="#1E293B",
            bg="#F1F5F9",
            padx=10,
            pady=6,
            anchor="w",
            justify=tk.LEFT,
            wraplength=810,
        )
        self.banner_lbl.pack(fill=tk.X, pady=(4, 0))

        self.cancel_manual_btn = tk.Button(
            status_card, text="CANCEL MANUAL VERIFICATION", command=self.cancel_manual_verification,
            font=("Segoe UI", 9, "bold"), fg="#FFFFFF", bg="#B45309",
            activeforeground="#FFFFFF", activebackground="#92400E", relief=tk.FLAT,
            cursor="hand2", padx=10, pady=5,
        )

        # ---------------- E. LIVE LOGS PANEL ----------------
        log_card = tk.LabelFrame(
            main_frame,
            text=" 3. Live Logs ",
            font=("Segoe UI", 10, "bold"),
            fg="#1E293B",
            bg="#FFFFFF",
            bd=1,
            relief=tk.SOLID,
            padx=12,
            pady=8,
        )
        log_card.pack(fill=tk.BOTH, expand=True)

        log_container = tk.Frame(log_card, bg="#0F172A")
        log_container.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(
            log_container,
            height=8,
            font=("Consolas", 9),
            fg="#E2E8F0",
            bg="#0F172A",
            insertbackground="#FFFFFF",
            relief=tk.FLAT,
            wrap=tk.WORD,
            state=tk.DISABLED,
            padx=8,
            pady=6,
        )
        log_scroll = ttk.Scrollbar(log_container, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.append_log("GUI initialized. Ready to scrape Google Scholar profiles.")

    def _add_metric_pair(self, parent, row: int, col: int, label_text: str, var: tk.StringVar, bold: bool = False, value_color: str = "#0F172A"):
        lbl = tk.Label(
            parent,
            text=label_text,
            font=("Segoe UI", 9, "bold"),
            fg="#475569",
            bg="#FFFFFF",
            anchor="w",
        )
        lbl.grid(row=row, column=col, sticky="w", pady=3, padx=(0, 6))

        val = tk.Label(
            parent,
            textvariable=var,
            font=("Segoe UI", 10 if bold else 9, "bold" if bold else "normal"),
            fg=value_color,
            bg="#FFFFFF",
            anchor="w",
        )
        val.grid(row=row, column=col + 1, sticky="w", pady=3, padx=(0, 16))
        if label_text == "Status:":
            self.status_val_lbl = val

    # ------------------------------------------------------------------ #
    # Placeholder & URL Validation Helpers
    # ------------------------------------------------------------------ #
    def _set_placeholder(self):
        self.url_entry.delete(0, tk.END)
        self.url_entry.insert(0, PLACEHOLDER_TEXT)
        self.url_entry.configure(fg="#94A3B8")
        self._placeholder_active = True

    def _on_url_focus_in(self, _event=None):
        if self._placeholder_active:
            self.url_entry.delete(0, tk.END)
            self.url_entry.configure(fg="#0F172A")
            self._placeholder_active = False

    def _on_url_focus_out(self, _event=None):
        val = self.url_var.get().strip()
        if not val:
            self._set_placeholder()
            self.scholar_id_badge_var.set("Scholar ID: —")

    def _on_url_key_release(self, _event=None):
        if self._placeholder_active:
            return
        raw = self.url_var.get().strip()
        if not raw:
            self.scholar_id_badge_var.set("Scholar ID: —")
            self.scholar_id_badge.configure(bg="#E2E8F0", fg="#334155")
            return
        try:
            gs_id = validate_scholar_profile_url(raw)
            self.scholar_id_badge_var.set(f"Scholar ID: {gs_id}")
            self.scholar_id_badge.configure(bg="#DCFCE7", fg="#166534")
        except InvalidProfileUrlError:
            self.scholar_id_badge_var.set("Scholar ID: Invalid URL")
            self.scholar_id_badge.configure(bg="#FEE2E2", fg="#991B1B")

    def set_url(self, url: str):
        """Programmatically set the profile URL (used by user or tests)."""
        self._placeholder_active = False
        self.url_entry.configure(fg="#0F172A")
        self.url_var.set(url)
        self._on_url_key_release()

    def _on_connection_mode_changed(self):
        if self.connection_mode_var.get() == "proxy":
            self.proxy_entry.configure(state=tk.NORMAL)
        else:
            self.proxy_entry.configure(state=tk.DISABLED)

    def set_proxy(self, proxy_url: Optional[str]):
        """Programmatically configure direct connection (None / 'direct') or a custom proxy URL."""
        cleaned = (proxy_url or "").strip()
        if not cleaned or cleaned.lower() in ("direct", "none", "off"):
            self.connection_mode_var.set("direct")
            self.proxy_var.set("")
            self.proxy_entry.configure(state=tk.DISABLED)
        else:
            self.connection_mode_var.set("proxy")
            self.proxy_var.set(cleaned)
            self.proxy_entry.configure(state=tk.NORMAL)

    def get_effective_proxy_url(self) -> Optional[str]:
        """Return the configured proxy URL if proxy mode is selected, or None for direct connection."""
        if self.connection_mode_var.get() == "proxy":
            return self.proxy_var.get().strip()
        return None

    def get_clean_url(self) -> str:
        if self._placeholder_active:
            return ""
        return self.url_var.get().strip()

    def append_log(self, message: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = message if message.startswith("20") or "|" in message[:15] else f"{ts} | INFO    | {message}"
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, line + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _set_banner(self, text: str, kind: str = "info"):
        self.banner_message_var.set(text)
        colors = {
            "info": ("#1E293B", "#F1F5F9"),
            "running": ("#1E40AF", "#DBEAFE"),
            "success": ("#166534", "#DCFCE7"),
            "warning": ("#9A3412", "#FFEDD5"),
            "error": ("#991B1B", "#FEE2E2"),
        }
        fg, bg = colors.get(kind, colors["info"])
        self.banner_lbl.configure(fg=fg, bg=bg)

    def _set_buttons_enabled(self, enabled: bool):
        state = tk.NORMAL if enabled else tk.DISABLED
        self.step1_btn.configure(state=state)
        self.step2_btn.configure(state=state)
        self.delete_btn.configure(state=state)
        self.url_entry.configure(state=state)
        if enabled:
            self._on_connection_mode_changed()
        else:
            self.proxy_entry.configure(state=tk.DISABLED)

    def _show_manual_cancel(self, visible: bool):
        if visible:
            if not self.cancel_manual_btn.winfo_manager():
                self.cancel_manual_btn.pack(anchor="e", pady=(6, 0))
            self.cancel_manual_btn.configure(state=tk.NORMAL)
        else:
            self.cancel_manual_btn.configure(state=tk.DISABLED)
            if self.cancel_manual_btn.winfo_manager():
                self.cancel_manual_btn.pack_forget()

    def cancel_manual_verification(self):
        """Signal the passive browser wait; never clicks, refreshes, or submits a challenge."""
        self.manual_cancel_event.set()
        self.cancel_manual_btn.configure(state=tk.DISABLED)
        self.status_var.set("Cancelling Manual Verification...")
        self._set_banner("Cancelling manual verification safely. The current blocked page will not be retried.",
                         kind="warning")
        self.append_log("Manual verification cancellation requested by user.")

    # ------------------------------------------------------------------ #
    # Button Actions (Step 1 & Step 2)
    # ------------------------------------------------------------------ #
    def start_step1_profile_scraping(self) -> bool:
        """
        BUTTON 1: STEP 1 — PROFILE SCRAPING
        Extracts profile info + all publications via dynamic SHOW MORE, applies CSV
        deduplication & scholar-scoped clean re-scraping, and writes CSVs + quality report.
        Does NOT open individual publication detail pages.
        """
        return self._start_scraping_job(scrape_details=False)

    def start_step2_full_scraping(self) -> bool:
        """
        BUTTON 2: STEP 2 — FULL PUBLICATION SCRAPING
        Automatically executes STEP 1 first, then sequentially visits every individual
        publication detail page (`view_op=view_citation`), extracts full metadata +
        exact year-wise citations, updates PUBLICATIONS/author_order, and supports
        checkpoint/resume.
        """
        return self._start_scraping_job(scrape_details=True)

    def _confirm_deletion_dialog(self, title: str, message: str) -> bool:
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.configure(bg="#FFFFFF")
        decision = {"confirmed": False}
        tk.Label(dialog, text=message, justify=tk.LEFT, anchor="w", wraplength=540,
                 font=("Segoe UI", 10), bg="#FFFFFF", fg="#1E293B",
                 padx=20, pady=18).pack(fill=tk.BOTH)
        controls = tk.Frame(dialog, bg="#FFFFFF", padx=20, pady=12)
        controls.pack(fill=tk.X)
        def close(confirmed=False):
            decision["confirmed"] = confirmed
            dialog.destroy()
        tk.Button(controls, text="Cancel", command=lambda: close(False),
                  font=("Segoe UI", 10), padx=14, pady=6).pack(side=tk.RIGHT, padx=(8, 0))
        tk.Button(controls, text="Confirm Delete", command=lambda: close(True),
                  font=("Segoe UI", 10, "bold"), bg="#B91C1C", fg="#FFFFFF",
                  activebackground="#991B1B", activeforeground="#FFFFFF",
                  padx=14, pady=6).pack(side=tk.RIGHT)
        dialog.protocol("WM_DELETE_WINDOW", lambda: close(False))
        dialog.bind("<Escape>", lambda _event: close(False))
        dialog.grab_set()
        dialog.focus_set()
        self.root.wait_window(dialog)
        return decision["confirmed"]

    def start_delete_scholar(self) -> bool:
        if self.is_running or not self.run_lock.acquire(blocking=False):
            return False
        try:
            try:
                gs_id = validate_scholar_profile_url(self.get_clean_url())
                preview = preview_deletion(self.output_dir, gs_id)
            except Exception as exc:
                self.status_var.set("Deletion Failed Safely")
                self.last_notification_text = str(exc)
                self._set_banner(str(exc), kind="error")
                if self.show_dialogs:
                    messagebox.showerror("Deletion Failed", str(exc), parent=self.root)
                return False
            if preview is None:
                msg = "Scholar Not Found — This researcher is not stored in the dataset."
                self.status_var.set("Scholar Not Found")
                self.last_notification_text = msg
                self._set_banner(msg, kind="warning")
                self.append_log(msg)
                if self.show_dialogs:
                    messagebox.showinfo("Scholar Not Found", msg, parent=self.root)
                return False
            scholar = preview["scholar"]
            message = (
                f"Scholar: {scholar['scholar_name']}\nGoogle Scholar ID: {gs_id}\n"
                f"Internal scholar_id: {scholar['scholar_id']}\n"
                f"Linked publications: {preview['linked_publications']}\n"
                f"Linked research areas: {preview['research_area_links']}\n"
                f"Associated detail records: {preview['detail_records']}\n"
                f"Associated yearly citation records: {preview['yearly_records']}\n\n"
                "Are you sure you want to permanently delete this scholar and all records belonging "
                "exclusively to this scholar? Shared publications, research areas, and institutions "
                "used by other scholars will be preserved."
            )
            confirmed = (self.confirm_delete_fn("Confirm Delete", message) if self.confirm_delete_fn
                         else self._confirm_deletion_dialog("Confirm Delete", message)
                         if self.show_dialogs else False)
            if not confirmed:
                self.status_var.set("Deletion Cancelled")
                self.last_notification_text = "Deletion cancelled. No records were changed."
                self._set_banner(self.last_notification_text, kind="info")
                self.append_log(self.last_notification_text)
                return False
            self.is_running = True
            self.manual_cancel_event.clear()
            self._set_buttons_enabled(False)
            self.status_var.set("Deleting Scholar...")
            self._set_banner("Deleting scholar and validating related records...", kind="running")
            def _worker():
                try:
                    result = delete_scholar(self.output_dir, gs_id)
                    self.event_queue.put({"type": "delete_done", "result": result})
                except Exception as exc:
                    self.event_queue.put({"type": "delete_error", "error": str(exc)})
            self.worker_thread = threading.Thread(target=_worker, daemon=True)
            self.worker_thread.start()
            return True
        finally:
            if not self.is_running and self.run_lock.locked():
                self.run_lock.release()

    def _start_scraping_job(self, scrape_details: bool) -> bool:
        if self.is_running or not self.run_lock.acquire(blocking=False):
            return False

        try:
            raw_url = self.get_clean_url()
            try:
                gs_id = validate_scholar_profile_url(raw_url)
            except InvalidProfileUrlError as err:
                err_msg = str(err)
                self.status_var.set("Invalid URL")
                self.status_val_lbl.configure(fg="#DC2626")
                self.progress_pct_var.set(0.0)
                self.progress_label_var.set("0%")
                self._set_banner(f"Invalid Google Scholar URL: {err_msg}", kind="error")
                self.append_log(f"URL validation failed: {err_msg}")
                if self.show_dialogs:
                    messagebox.showerror("Invalid Google Scholar Profile URL", err_msg, parent=self.root)
                self.run_lock.release()
                return False

            self.scholar_id_badge_var.set(f"Scholar ID: {gs_id}")
            self.scholar_id_badge.configure(bg="#DCFCE7", fg="#166534")

            # ---- 0. Validate proxy configuration if "Configured Proxy" mode is selected ----
            effective_proxy = self.get_effective_proxy_url()
            if self.connection_mode_var.get() == "proxy":
                if not effective_proxy:
                    err_msg = (
                        "Configured Proxy mode is selected, but no proxy URL was entered. "
                        "Please enter a valid proxy URL (e.g., http://127.0.0.1:8080 or socks5://127.0.0.1:1080) "
                        "or select Direct Connection."
                    )
                    self.last_result = PipelineResult(
                        exit_code=2,
                        status="PROXY_CONFIG_ERROR",
                        message=err_msg,
                        google_scholar_id=gs_id,
                        output_dir=self.output_dir,
                    )
                    self.researcher_var.set(f"Not Scraped ({gs_id})")
                    self.status_var.set("Invalid Proxy Configuration")
                    self.status_val_lbl.configure(fg="#DC2626")
                    self.progress_pct_var.set(0.0)
                    self.progress_label_var.set("0%")
                    self.current_pub_var.set("None — Check proxy settings or select Direct Connection")
                    self.last_notification_text = err_msg
                    self._set_banner(err_msg, kind="error")
                    self.append_log(f"PROXY CONFIG ERROR: {err_msg}")
                    if self.show_dialogs:
                        messagebox.showerror("Invalid Proxy Configuration", err_msg, parent=self.root)
                    self.run_lock.release()
                    return False
                try:
                    validate_proxy_config(effective_proxy)
                except ProxyConfigurationError as proxy_cfg_err:
                    err_msg = (
                        f"{SUGGESTED_PROXY_ERROR_MESSAGE}\n\n"
                        f"Cause: Invalid Proxy Configuration\n"
                        f"Details: {proxy_cfg_err}\n"
                        f"Failed Scholar URL: {raw_url}"
                    )
                    self.last_result = PipelineResult(
                        exit_code=2,
                        status="PROXY_CONFIG_ERROR",
                        message=err_msg,
                        google_scholar_id=gs_id,
                        output_dir=self.output_dir,
                    )
                    self.researcher_var.set(f"Not Scraped ({gs_id})")
                    self.status_var.set("Invalid Proxy Configuration")
                    self.status_val_lbl.configure(fg="#DC2626")
                    self.progress_pct_var.set(0.0)
                    self.progress_label_var.set("0%")
                    self.current_pub_var.set("None — Check proxy settings or select Direct Connection")
                    self.last_notification_text = err_msg
                    self._set_banner(err_msg, kind="error")
                    self.append_log(f"PROXY CONFIG ERROR: {err_msg}")
                    if self.show_dialogs:
                        messagebox.showerror("Invalid Proxy Configuration", err_msg, parent=self.root)
                    self.run_lock.release()
                    return False

            # ---- 1. Check whether this google_scholar_id already exists in output/scholars.csv ----
            try:
                existing_row = find_existing_scholar_by_google_id(self.output_dir, gs_id)
            except (CsvPermissionError, PermissionError) as perm_exc:
                perm_err = (
                    perm_exc
                    if isinstance(perm_exc, CsvPermissionError)
                    else CsvPermissionError(self.output_dir / "scholars.csv", operation="read", original_error=perm_exc)
                )
                err_msg = str(perm_err)
                self.last_existing_scholar = None
                self.last_notification_text = err_msg
                self.last_result = PipelineResult(
                    exit_code=5,
                    status="PERMISSION_ERROR",
                    message=err_msg,
                    google_scholar_id=gs_id,
                    output_dir=self.output_dir,
                )
                self.researcher_var.set("Unable to read (CSV file locked)")
                self.status_var.set("CSV Permission Error")
                self.status_val_lbl.configure(fg="#DC2626")
                self.pubs_found_var.set("0")
                self.pubs_processed_var.set("0")
                self.details_scraped_var.set("0")
                self.pubs_pending_var.set("0")
                self.current_pub_var.set("None — Please close Excel or any app locking output/scholars.csv")
                self.progress_pct_var.set(0.0)
                self.progress_label_var.set("0%")
                self._set_banner(f"CSV File Locked / Permission Denied:\n{err_msg}", kind="error")
                self.append_log(f"PERMISSION ERROR: {err_msg}")
                if self.show_dialogs:
                    messagebox.showerror("CSV Permission Error", err_msg, parent=self.root)
                self.run_lock.release()
                return False

            if existing_row is not None and not scrape_details:
                scholar_name = (existing_row.get("scholar_name") or "").strip() or "Unknown"
                scholar_id = str(existing_row.get("scholar_id") or "").strip()
                total_pubs_str = str(existing_row.get("total_publications") or "0").strip()
                notice_text = format_already_stored_notification(existing_row)

                self.last_existing_scholar = existing_row
                self.last_notification_text = notice_text
                self.last_result = PipelineResult(
                    exit_code=0,
                    status="ALREADY_STORED",
                    message=notice_text,
                    google_scholar_id=gs_id,
                    researcher_name=scholar_name,
                    publications_found=int(total_pubs_str) if total_pubs_str.isdigit() else 0,
                    output_dir=self.output_dir,
                )

                self.researcher_var.set(scholar_name)
                self.status_var.set("⚠️ Scholar Already Stored!")
                self.status_val_lbl.configure(fg="#B45309")
                self.pubs_found_var.set(total_pubs_str if total_pubs_str.isdigit() else "0")
                self.pubs_processed_var.set("0 (Skipped — Already Stored)")
                self.details_scraped_var.set("0")
                self.pubs_pending_var.set("0")
                self.current_pub_var.set("None — Scraping skipped (scholar already exists in output/scholars.csv)")
                self.progress_pct_var.set(100.0)
                self.progress_label_var.set("100%")
                self._set_banner(notice_text, kind="warning")
                self.append_log(
                    f"Duplicate check: Google Scholar ID '{gs_id}' ({scholar_name}, scholar_id={scholar_id}) "
                    "already exists in output/scholars.csv. No new scraping or CSV modifications were performed."
                )
                if self.show_dialogs:
                    messagebox.showwarning("Scholar Already Stored", notice_text, parent=self.root)
                self.run_lock.release()
                return False

            # ---- 2. Verify CSV files are not locked by Excel/another process before starting browser ----
            try:
                verify_csv_files_writable(self.output_dir, include_details=scrape_details)
            except (CsvPermissionError, PermissionError) as perm_exc:
                perm_err = (
                    perm_exc
                    if isinstance(perm_exc, CsvPermissionError)
                    else CsvPermissionError(self.output_dir / "scholars.csv", operation="write to", original_error=perm_exc)
                )
                err_msg = str(perm_err)
                self.last_existing_scholar = None
                self.last_notification_text = err_msg
                self.last_result = PipelineResult(
                    exit_code=5,
                    status="PERMISSION_ERROR",
                    message=err_msg,
                    google_scholar_id=gs_id,
                    output_dir=self.output_dir,
                )
                self.researcher_var.set("Unable to read (CSV file locked)")
                self.status_var.set("CSV Permission Error")
                self.status_val_lbl.configure(fg="#DC2626")
                self.pubs_found_var.set("0")
                self.pubs_processed_var.set("0")
                self.details_scraped_var.set("0")
                self.pubs_pending_var.set("0")
                self.current_pub_var.set("None — Please close Excel or any app locking output/scholars.csv")
                self.progress_pct_var.set(0.0)
                self.progress_label_var.set("0%")
                self._set_banner(f"CSV File Locked / Permission Denied:\n{err_msg}", kind="error")
                self.append_log(f"PERMISSION ERROR: {err_msg}")
                if self.show_dialogs:
                    messagebox.showerror("CSV Permission Error", err_msg, parent=self.root)
                self.run_lock.release()
                return False

            # ---- 3. Scholar does not exist and CSVs are writable -> proceed with scraping ----
            self.is_running = True
            self.last_result = None
            self.last_existing_scholar = None
            self.last_notification_text = ""
            self._set_buttons_enabled(False)
            mode_name = "STEP 2 — FULL PUBLICATION SCRAPING" if scrape_details else "STEP 1 — PROFILE SCRAPING"
            self.status_var.set(f"Running {mode_name}...")
            self.status_val_lbl.configure(fg="#1D4ED8")
            self.researcher_var.set(f"Loading ({gs_id})...")
            self.pubs_found_var.set("0")
            self.pubs_processed_var.set("0")
            self.details_scraped_var.set("0")
            self.pubs_pending_var.set("0")
            self.current_pub_var.set("Initializing browser..." if scrape_details else "N/A (Step 1 Profile Only)")
            self.progress_pct_var.set(2.0)
            self.progress_label_var.set("2%")
            self._set_banner(
                (f"Scholar Already Stored — Continuing Publication Detail Scraping for {existing_row['scholar_name']}."
                 if scrape_details and existing_row else
                 f"Started {mode_name} for Google Scholar ID '{gs_id}'. Please wait while the browser extracts data..."),
                kind="running",
            )
            self.append_log(
                f"Starting {mode_name} for URL: {raw_url} (user={gs_id}, "
                f"connection={'proxy:' + redact_proxy_url(effective_proxy) if effective_proxy else 'direct'})"
            )

            use_persistent = bool(self.persistent_var.get())
            use_headed = bool(self.headed_var.get()) or bool(self.manual_var.get())
            use_manual = bool(self.manual_var.get())

            def _worker():
                try:
                    runner_kwargs = dict(
                        url=raw_url,
                        output_dir=self.output_dir,
                        docx_path=self.docx_path,
                        headed=use_headed,
                        persistent=use_persistent,
                        user_data_dir=(HERE / ".chrome_profile") if use_persistent else None,
                        manual=use_manual,
                        manual_cancel_event=self.manual_cancel_event,
                        scrape_details=scrape_details,
                        progress_callback=lambda ev: self.event_queue.put({"type": "progress", "data": ev}),
                    )
                    if effective_proxy is not None:
                        runner_kwargs["proxy_url"] = effective_proxy
                    res = self.pipeline_runner(**runner_kwargs)
                    self.event_queue.put({"type": "done", "result": res})
                except Exception as exc:  # noqa: BLE001
                    self.event_queue.put({"type": "exception", "error": str(exc)})

            self.worker_thread = threading.Thread(target=_worker, daemon=True)
            self.worker_thread.start()
            return True
        except Exception:
            if self.run_lock.locked():
                self.run_lock.release()
            raise

    # ------------------------------------------------------------------ #
    # Event Queue Polling (keeps UI responsive on main thread)
    # ------------------------------------------------------------------ #
    def _schedule_queue_poll(self):
        self.process_pending_events()
        try:
            if self.root.winfo_exists():
                self.root.after(80, self._schedule_queue_poll)
        except tk.TclError:
            pass

    def process_pending_events(self):
        """Process all queued worker events immediately on the main thread."""
        while True:
            try:
                item = self.event_queue.get_nowait()
            except queue.Empty:
                break

            etype = item.get("type")
            if etype == "log":
                self.append_log(item.get("text", ""))
            elif etype == "progress":
                self._handle_progress_event(item.get("data") or {})
            elif etype == "done":
                self._handle_done_event(item.get("result"))
            elif etype == "exception":
                self._handle_exception_event(item.get("error") or "Unknown error")
            elif etype == "delete_done":
                self._handle_delete_done(item.get("result"))
            elif etype == "delete_error":
                self._handle_delete_error(item.get("error") or "Unknown deletion error")

    def _finish_delete(self):
        self.is_running = False
        if self.run_lock.locked():
            self.run_lock.release()
        self._set_buttons_enabled(True)

    def _handle_delete_done(self, result):
        self._finish_delete()
        if result is None:
            self.status_var.set("Scholar Not Found")
            self._set_banner("Scholar Not Found — This researcher is not stored in the dataset.", kind="warning")
            return
        msg = (f"Scholar Deleted Successfully\nScholar removed: {result['scholar']['scholar_name']}\n"
               f"Publications removed: {result['publications_removed']}\n"
               f"Shared publications preserved: {result['shared_publications_preserved']}\n"
               f"Research-area links removed: {result['research_area_links_removed']}\n"
               f"Orphan research areas removed: {result['orphan_areas_removed']}\n"
               f"HEI: {'removed' if result['hei_removed'] else 'preserved'}\n"
               f"Detail records removed: {result['details_removed']}\n"
               f"Yearly citation records removed: {result['yearly_removed']}")
        self.status_var.set("Scholar Deleted Successfully")
        self.last_notification_text = msg
        self._set_banner(msg, kind="success")
        self.append_log(msg)
        if self.show_dialogs:
            messagebox.showinfo("Scholar Deleted Successfully", msg, parent=self.root)

    def _handle_delete_error(self, error):
        self._finish_delete()
        self.status_var.set("Deletion Failed Safely")
        self.last_notification_text = error
        self._set_banner(error, kind="error")
        self.append_log(f"DELETION FAILED: {error}")
        if self.show_dialogs:
            messagebox.showerror("Deletion Failed Safely", error, parent=self.root)

    def _handle_progress_event(self, ev: dict):
        phase = ev.get("phase", "")
        if phase in ("manual_verification_required", "manual_verification_waiting"):
            self._show_manual_cancel(True)
        elif phase in ("manual_verification_succeeded", "manual_verification_cancelled",
                       "manual_verification_unsuccessful", "manual_rate_limited", "step2_blocked"):
            self._show_manual_cancel(False)
        if ev.get("researcher_name"):
            self.researcher_var.set(ev["researcher_name"])
        if ev.get("status"):
            self.status_var.set(ev["status"])
        if "publications_found" in ev and ev["publications_found"] is not None:
            self.pubs_found_var.set(str(ev["publications_found"]))
        if "publications_processed" in ev and ev["publications_processed"] is not None:
            self.pubs_processed_var.set(str(ev["publications_processed"]))
        if "details_scraped" in ev and ev["details_scraped"] is not None:
            self.details_scraped_var.set(str(ev["details_scraped"]))
        if "pending_publications" in ev and ev["pending_publications"] is not None:
            self.pubs_pending_var.set(str(ev["pending_publications"]))
        if ev.get("current_publication_title"):
            pid_prefix = f"[#{ev['current_publication_id']}] " if ev.get("current_publication_id") else ""
            self.current_pub_var.set(f"{pid_prefix}{ev['current_publication_title']}")
        if "progress_pct" in ev and ev["progress_pct"] is not None:
            pct = max(0.0, min(100.0, float(ev["progress_pct"])))
            self.progress_pct_var.set(pct)
            self.progress_label_var.set(f"{int(round(pct))}%")
        if ev.get("message"):
            kind = (
                "warning"
                if phase in ("blocked", "manual_verification_required", "manual_verification_waiting",
                             "manual_rate_limited", "manual_verification_unsuccessful")
                else ("error" if phase in ("error", "proxy_error", "network_error", "permission_error") else ("success" if phase == "completed" else "running"))
            )
            self._set_banner(ev["message"], kind=kind)

    def _handle_done_event(self, res: PipelineResult):
        self.is_running = False
        if self.run_lock.locked():
            self.run_lock.release()
        self.last_result = res
        self._set_buttons_enabled(True)
        self._show_manual_cancel(False)

        if res.researcher_name:
            self.researcher_var.set(res.researcher_name)
        self.pubs_found_var.set(str(res.publications_found))
        self.pubs_processed_var.set(str(res.publications_processed))
        self.details_scraped_var.set(str(res.details_scraped))
        self.pubs_pending_var.set(str(res.pending_publications))

        raw_msg = res.message or ""
        upper_msg = raw_msg.upper()
        is_proxy_err = res.status in ("PROXY_ERROR", "PROXY_CONFIG_ERROR") or any(c in upper_msg for c in PROXY_ERROR_CODES)
        is_network_err = res.status == "NETWORK_ERROR"

        if res.status == "PERMISSION_ERROR":
            self.status_var.set("CSV Permission Error")
            self.status_val_lbl.configure(fg="#DC2626")
            self.progress_pct_var.set(0.0)
            self.progress_label_var.set("0%")
            self.last_notification_text = res.message
            self._set_banner(f"CSV File Locked / Permission Denied:\n{res.message}", kind="error")
            self.append_log(f"PERMISSION ERROR: {res.message}")
            if self.show_dialogs:
                messagebox.showerror("CSV Permission Error", res.message, parent=self.root)
        elif is_proxy_err:
            failed_url = self.get_clean_url()
            if res.profile_data and getattr(res.profile_data, "requested_url", None):
                failed_url = res.profile_data.requested_url
            gs_tag = res.google_scholar_id or "Not Scraped"
            self.researcher_var.set(f"Connection Failed ({gs_tag})")
            status_lbl = "Invalid Proxy Configuration" if res.status == "PROXY_CONFIG_ERROR" else "Proxy Connection Failed"
            self.status_var.set(status_lbl)
            self.status_val_lbl.configure(fg="#DC2626")
            self.progress_pct_var.set(0.0)
            self.progress_label_var.set("0%")
            self.current_pub_var.set("None — Check proxy settings or select Direct Connection")

            lines = [SUGGESTED_PROXY_ERROR_MESSAGE, ""]
            lines.append("Failure Cause: Proxy Configuration / Proxy Connection Failure")
            if failed_url:
                lines.append(f"Failed Scholar URL: {failed_url}")
            if raw_msg and raw_msg != SUGGESTED_PROXY_ERROR_MESSAGE:
                for ln in raw_msg.splitlines():
                    ln_s = ln.strip()
                    if ln_s and ln_s != SUGGESTED_PROXY_ERROR_MESSAGE and not ln_s.startswith("Failed URL:"):
                        lines.append(ln_s)
            proxy_notice = "\n".join(lines)
            self.last_notification_text = proxy_notice
            self._set_banner(proxy_notice, kind="error")
            self.append_log(f"PROXY ERROR: {proxy_notice}")
            if self.show_dialogs:
                messagebox.showerror("Proxy Connection Error", proxy_notice, parent=self.root)
        elif is_network_err:
            failed_url = self.get_clean_url()
            if res.profile_data and getattr(res.profile_data, "requested_url", None):
                failed_url = res.profile_data.requested_url
            gs_tag = res.google_scholar_id or "Not Scraped"
            self.researcher_var.set(f"Network Failure ({gs_tag})")
            self.status_var.set("Network Connection Failed")
            self.status_val_lbl.configure(fg="#DC2626")
            self.progress_pct_var.set(0.0)
            self.progress_label_var.set("0%")
            self.current_pub_var.set("None — Check your internet connection and try again")

            net_lines = [
                "Unable to connect to Google Scholar due to a general network connectivity failure. "
                "Please check your internet connection and try again.",
                "",
                "Failure Cause: General Network Connectivity Failure",
            ]
            if failed_url:
                net_lines.append(f"Failed Scholar URL: {failed_url}")
            if raw_msg:
                for ln in raw_msg.splitlines():
                    ln_s = ln.strip()
                    if ln_s and not ln_s.startswith("Failed URL:") and "Unable to connect to Google Scholar" not in ln_s:
                        net_lines.append(ln_s)
            net_notice = "\n".join(net_lines)
            self.last_notification_text = net_notice
            self._set_banner(net_notice, kind="error")
            self.append_log(f"NETWORK ERROR: {net_notice}")
            if self.show_dialogs:
                messagebox.showerror("Network Connection Error", net_notice, parent=self.root)
        elif res.status == "MANUAL_CANCELLED":
            self.status_var.set("Manual Verification Cancelled")
            self.status_val_lbl.configure(fg="#D97706")
            self.last_notification_text = res.message
            self._set_banner(res.message, kind="warning")
            self.append_log(f"MANUAL VERIFICATION CANCELLED: {res.message}")
            if self.show_dialogs:
                messagebox.showwarning("Manual Verification Cancelled", res.message, parent=self.root)
        elif res.status == "MANUAL_UNSUCCESSFUL":
            self.status_var.set("Manual Verification Unsuccessful")
            self.status_val_lbl.configure(fg="#D97706")
            self.last_notification_text = res.message
            self._set_banner(res.message, kind="warning")
            self.append_log(f"MANUAL VERIFICATION UNSUCCESSFUL: {res.message}")
            if self.show_dialogs:
                messagebox.showwarning("Manual Verification Unsuccessful", res.message, parent=self.root)
        elif res.stopped_by_block or res.status in ("BLOCKED", "BLOCKED_PARTIAL"):
            block_kind = (res.detail_summary or {}).get("block_kind") or "blocked"
            if block_kind == "captcha":
                block_status = "Paused — CAPTCHA Requires Manual Verification"
            elif block_kind in ("http_429", "http_429_no_challenge"):
                block_status = "Paused — HTTP 429 Rate Limit"
            else:
                block_status = "Paused — Google Access Restriction"
            self.status_var.set(block_status)
            self.status_val_lbl.configure(fg="#D97706")
            pct = round(100.0 * res.details_scraped / res.publications_found, 1) if res.publications_found else 0.0
            self.progress_pct_var.set(pct)
            self.progress_label_var.set(f"{int(round(pct))}%")
            block_msg = (
                f"Google Scholar restricted the publication-detail request ({res.block_reason or res.message}). "
                f"Scraping stopped safely. All checkpoints and previously scraped CSV records ({res.details_scraped} details, "
                f"{res.publications_found} publications) are preserved. No blocked endpoint was repeatedly retried."
            )
            self.last_notification_text = block_msg
            self._set_banner(block_msg, kind="warning")
            self.append_log(f"WARNING: {block_msg}")
            if self.show_dialogs:
                messagebox.showwarning("Google Scholar Rate Limit / CAPTCHA", block_msg, parent=self.root)
        elif res.status == "ALREADY_COMPLETE":
            self.status_var.set("Publication Detail Scraping Already Complete")
            self.status_val_lbl.configure(fg="#15803D")
            self.progress_pct_var.set(100.0)
            self.progress_label_var.set("100%")
            self.last_notification_text = res.message
            self._set_banner(res.message, kind="success")
            self.append_log(res.message)
            if self.show_dialogs:
                messagebox.showinfo("Publication Detail Scraping Already Complete", res.message, parent=self.root)
        elif res.status == "PARTIAL":
            self.status_var.set("Step 2 Partially Completed")
            self.status_val_lbl.configure(fg="#D97706")
            self.last_notification_text = res.message
            self._set_banner(res.message, kind="warning")
            self.append_log(res.message)
            if self.show_dialogs:
                messagebox.showwarning("Step 2 Partially Completed", res.message, parent=self.root)
        elif res.exit_code == 0:
            self.status_var.set("Completed Successfully")
            self.status_val_lbl.configure(fg="#15803D")
            self.progress_pct_var.set(100.0)
            self.progress_label_var.set("100%")
            self.current_pub_var.set("Completed")
            self.last_notification_text = res.message
            self._set_banner(res.message, kind="success")
            self.append_log(f"SUCCESS: {res.message}")
            if self.show_dialogs:
                messagebox.showinfo("Scraping Completed", res.message, parent=self.root)
        else:
            if not res.researcher_name:
                self.researcher_var.set(f"Failed ({res.google_scholar_id or 'Not Scraped'})")
            self.status_var.set(f"Failed ({res.status})")
            self.status_val_lbl.configure(fg="#DC2626")
            self.progress_pct_var.set(0.0)
            self.progress_label_var.set("0%")
            self.current_pub_var.set("None")
            self.last_notification_text = res.message
            self._set_banner(f"Error: {res.message}", kind="error")
            self.append_log(f"ERROR: {res.message}")
            if self.show_dialogs:
                messagebox.showerror("Scraping Error", res.message, parent=self.root)

    def _handle_exception_event(self, err_str: str):
        self.is_running = False
        if self.run_lock.locked():
            self.run_lock.release()
        self._set_buttons_enabled(True)
        self._show_manual_cancel(False)
        self.status_var.set("Error")
        self.status_val_lbl.configure(fg="#DC2626")
        self.progress_pct_var.set(0.0)
        self.progress_label_var.set("0%")
        self.current_pub_var.set("None")
        self.last_notification_text = err_str
        self._set_banner(f"Unexpected error: {err_str}", kind="error")
        self.append_log(f"EXCEPTION: {err_str}")
        if self.show_dialogs:
            messagebox.showerror("Unexpected Error", err_str, parent=self.root)

    def _on_close(self):
        if self.is_running:
            self.manual_cancel_event.set()
        root_logger = logging.getLogger()
        if self.log_handler in root_logger.handlers:
            root_logger.removeHandler(self.log_handler)
        if self.file_log_handler in root_logger.handlers:
            root_logger.removeHandler(self.file_log_handler)
        self.file_log_handler.close()
        self.root.destroy()


def main() -> int:
    out_dir = Path(os.environ.get("SCHOLAR_OUTPUT_DIR", str(HERE / "output")))
    out_dir.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    _app = ScholarScraperGUI(root, output_dir=out_dir)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
