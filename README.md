# Google Scholar Profile Scraper → ERD CSV Export

## Step 2 profile-based navigation

For both new and stored scholars, pending details are opened by visiting the
requested profile, expanding SHOW MORE, and clicking the unique `a.gsc_a_at`
whose decoded, case-sensitive `citation_for_view` matches the saved URL.
Row numbers and titles are not identity keys. Missing/ambiguous links or an
unverifiable opened citation produce `UNRESOLVED` (incomplete), never success.
A shared publication saved under another scholar's citation key may therefore
remain unresolved; the scraper does not guess an alternative title match.

The batch uses the existing metadata extractor with `navigate=False`. Its
standalone direct-navigation default remains for compatibility and parser tests;
normal CLI/GUI Step 2 never uses that direct-navigation branch. Canonical Scholar
URLs remain in `source_url` and `publication_url`; publisher links stay separate.

The CLI options `--interaction-delay` (default/minimum 0.5), `--post-click-delay`
(default/minimum 1.0), and `--return-delay` (default/minimum 1.0) configure the
navigation pauses in seconds. The GUI uses these defaults. DOM visibility,
profile ownership, citation identity, and block checks supplement these pauses.
The existing `--detail-delay` (default 4 seconds) remains between publications.

Dialogs close through their UI control; new tabs close to reveal the profile;
same-tab pages return using browser history. Retained listings are reused;
SHOW MORE is repeated only when rows were lost on navigation. Each success is
checkpointed before returning. Return failures stop the batch without erasing
that successful record. Existing completed records remain skipped.

HTTP 429/CAPTCHA stops automated requests. In manual mode a recognized challenge
can be resolved by the user with passive waiting and cancellation; a bare rate
limit never triggers retries. Unrecognized dialog markup fails safely. Clicking
does not guarantee Google Scholar access. New scholars still finish the existing
Step 1 lifecycle before Step 2 launches; persistent mode reuses the dedicated
profile on disk, not the same open browser context.

## Desktop workflow and deletion policy

Launch the existing desktop GUI with `python gui_app.py`. STEP 1 stores a new
scholar's profile and front-page publications; it reports an existing scholar
without scraping again. STEP 2 runs the full pipeline for a new scholar. For
an existing scholar it reads the author-publication links, CSV detail rows and
checkpoints, then opens only pending detail pages. A successful detail requires
a `SUCCESS` checkpoint with extracted metadata, a matching publication ID and
canonical detail-row URL, and an exported detail row with a title and sync
timestamp. For a shared publication, a different scholar-specific front-page
URL is accepted only when the checkpoint and detail URL agree and extracted
titles match the publication.
Missing optional DOI, issue, publisher and similar fields do not prevent
completion. `FAILED`, `BLOCKED`, `PARTIAL`, `SKIPPED_DUPLICATE_URL`, and absent
(`NOT_ATTEMPTED`) checkpoints remain pending. HTTP 429 or CAPTCHA stops the
detail loop; the GUI reports blocked or partial work rather than completion.

DELETE SCHOLAR matches the URL's `user` parameter against `scholars.csv` and
requires confirmation. It removes the scholar's bridge rows and publications
with no remaining author link. Detail and yearly citation rows are removed
only for deleted publications. A shared detail row whose provenance scholar
was deleted is reassigned to a remaining linked scholar. Orphan research
areas and HEIs are removed, matching the existing scholar cleanup policy.
The historical `id_registry.json` entries and `next_id` counters are retained
as tombstones; surviving IDs are never renumbered and deleted IDs are not
reused. If a registry file is missing, deletion creates one from the complete
pre-deletion dataset as part of the transaction.

Scraping and deletion hold the same operating-system dataset lock. Deletion
validates the input and result, stages every changed file, backs up originals
in `output/.deletion_transaction`, replaces files, and validates the committed
CSV dataset. Any ordinary commit failure restores the originals. An
interrupted process is recovered from the transaction journal before the next
scrape, deletion, or deletion preview. Keep that journal if a recovery error
occurs; it contains the original bytes. The existing global detail and raw
profile snapshots are read as legacy data; new checkpoints live in
`output/supplementary/checkpoints/<google_scholar_id>.json` and new raw profile
snapshots in `output/supplementary/profiles/<google_scholar_id>.json`.

Offline verification: `python -m pytest -q` and run each legacy script in
`tests/` directly (for example, `python tests/test_gui_integration.py`).

### Browser options and manual verification

- **Persistent Chrome Profile** defaults on. Both stages open the same isolated
  `.chrome_profile` directory with Playwright `launch_persistent_context`, so
  legitimate cookies and browser storage survive between runs. Only one browser
  process may own that directory at a time.
- **Show Browser Window** defaults off and controls Playwright `headless`.
  Manual Verification always forces a visible browser.
- **Manual Verification** defaults off. When enabled, the scraper waits only if
  an interactive challenge widget is present. It never solves, clicks, reloads,
  or resubmits the challenge. The GUI displays a cancel button during the wait.
  Resume requires the requested Scholar page, a non-403/429 top-level response,
  and the expected page DOM. Closing Chrome is treated as cancellation.
- **Direct Connection** is the default. Chromium receives
  `--no-proxy-server`, and proxy environment variables are removed from its
  launch environment.
- **Configured Proxy** enables the proxy field and accepts
  `http://`, `https://`, `socks4://`, or `socks5://` with an explicit host and
  port. Optional credentials are passed separately to Playwright and redacted
  from GUI/log display. The proxy is syntax-checked and TCP-checked before a
  browser starts. Settings take effect on the next browser launch.

An HTTP 429 or `/sorry/` page without an interactive CAPTCHA stops immediately.
An unresolved challenge times out as **Manual Verification Unsuccessful**.
`CANCELLED`, `BLOCKED`, and failed records remain pending in the scholar-scoped
checkpoint and never count as completed publication details.

Uses Playwright to open a Google Scholar profile **front page** in a real browser. It clicks **SHOW MORE** until the full publication table is loaded, then exports the data to CSV files. The column headers are the exact attribute names from `Research_Companion_ERD_Listing_Exact_Names_V2.docx`.

## Project structure
```
Project/
├── gui_app.py                     # Tkinter desktop GUI (Step 1, Step 2, deletion)
├── run_scraper.py                 # CLI entry point
├── requirements.txt
├── Research_Companion_ERD_Listing_Exact_Names_V2.docx
├── scholar_scraper/
│   ├── erd_schema.py              # exact ERD column lists (+ CSV file names)
│   ├── docx_schema.py             # re-reads the DOCX at runtime and verifies headers
│   ├── browser.py                 # Playwright navigation, SHOW MORE, detail extraction, block detection
│   ├── transform.py               # raw DOM data -> ERD rows with stable internal IDs
│   ├── id_registry.py             # persistent stable ID assignments
│   ├── dataset_ops.py             # safe deletion, locking, checkpoints/resume
│   ├── reference_data.py          # optional canonical HEI enrichment
│   ├── validator.py               # schema and relational-integrity validation
│   └── reporting.py               # staged CSV writer + extraction reports
└── output/
    ├── heis.csv
    ├── scholars.csv
    ├── research_areas.csv
    ├── publications.csv
    ├── scholar_research_areas.csv
    ├── authors_publications.csv
    ├── extraction_report.txt
    ├── scraper.log
    └── supplementary/             # detail CSVs, checkpoints, linkage, profile snapshots
```

## Setup
```powershell
pip install -r requirements.txt
# Optional: Playwright's bundled Chromium. If you skip this, the scraper uses installed Edge/Chrome.
python -m playwright install chromium
```

## Run
```powershell
python run_scraper.py --url "https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ"
```

### Scraping a different researcher (no code changes)
```powershell
python run_scraper.py --url "https://scholar.google.com/citations?hl=en&user=<USER_ID>" --output output\<USER_ID>
# or
$env:SCHOLAR_URL = "https://scholar.google.com/citations?user=<USER_ID>"; python run_scraper.py
# or run with no URL and paste it at the prompt
```

| Option | Default | Purpose |
|---|---|---|
| `--url` | `SCHOLAR_URL` env / prompt | Profile URL |
| `--output` | `output` | Output folder |
| `--browser-channel` | `auto` | `chromium`, `msedge`, `chrome` (auto = first available) |
| `--headed` | off | Show the browser window |
| `--delay` | `3` | Base pause (s) between SHOW MORE clicks (plus random jitter) |
| `--timeout` | `45` | Page/element timeout (s) |
| `--max-show-more` | `100` | Safety cap on SHOW MORE clicks |
| `--docx` | project DOCX | ERD document used to verify headers |

Exit codes include: `0` success/partial checkpoint-preserving completion; `2` scraping, access, proxy, or URL failure; `3` DOCX schema mismatch; `4` validation/registry failure; `5` CSV permission failure; `7` manual verification cancellation.

## Data rules
- Internal IDs (`scholar_id`, `hei_id`, `publication_id`, `research_area_id`) are assigned by the persistent `output/id_registry.json`. Existing IDs are preserved and deleted IDs are not reused.
- Any value the page doesn't show is left empty, never `0`, `NULL` or a guess.
- `total_publications` is filled only after SHOW MORE has been clicked until Google disables it.
- `author_order` is filled only when the owner's name (for example `US Khan`) appears exactly once in the displayed author list.
- Timestamps (`last_synced_at`, `updated_at`, `recorded_at`) are generated by the scraper. The report lists them separately from scraped values.
- Bridge-table relationships use stable ID foreign keys; row positions have no relational meaning. `supplementary/record_linkage.csv` stores source keys for audit.
- Every visible research interest is stored regardless of discipline. `is_computing_area` remains empty when no explicit classification exists.
- An uncatalogued affiliation is preserved as source-observed text with unavailable metadata empty; it is never reassigned to a convenient known university.

## Access restrictions
If Google returns a CAPTCHA, a `/sorry/` page, or HTTP 403/429, the active stage stops immediately. Completed Step 1 data and successful Step 2 checkpoints/detail rows are preserved; blocked records remain pending and are never reported as complete. The scraper never bypasses a challenge or repeatedly retries a blocked endpoint.
