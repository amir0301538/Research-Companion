# PROJECT_HANDOVER_REPORT

Date of audit: 2026-11-01 (local, UTC+5). Workspace: `c:\Users\Muhammad Aamir\Desktop\FYP\Project`

> **HEADLINE:** The scraper code is fully written and its offline logic is tested, but **the live scrape has NEVER succeeded.** Google Scholar answered HTTP 429 and redirected to `google.com/sorry/` (CAPTCHA/rate-limit) on every attempt: the earlier session (2026-10-31 21:26) and again in this session (2026-11-01 07:45). Per the rules, nothing was bypassed and **no real CSVs exist**. All "live-DOM" extraction code (selectors, SHOW MORE loop) is therefore **unverified against the live page**.

## 1. Project Overview
A Python + Playwright tool that opens one Google Scholar profile front page (URL given by `--url`, `SCHOLAR_URL` env var, or prompt), clicks the in-page "SHOW MORE" button until the publication table is fully loaded, reads the live DOM, and writes ERD-shaped CSVs plus `extraction_report.txt`.
Scope: profile front page only. No publication pages are opened, the screenshot is not used, CAPTCHA/429 is detected and reported, never bypassed.
Default profile: `https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ`.

## 2. Work Completed Before This Session (found by inspection)
All of the following already existed and were read in full:
- `run_scraper.py` (CLI, logging, orchestration, failure report path, exit codes 0/2/3)
- `scholar_scraper/browser.py` (Playwright launch with chromium→Edge→Chrome fallback, goto, block detection, SHOW MORE loop, JS DOM extractors)
- `scholar_scraper/erd_schema.py`, `docx_schema.py` (stdlib DOCX reader; headers verified vs DOCX at runtime)
- `scholar_scraper/transform.py` (cleaning, dedup, author_order derivation, no invented IDs)
- `scholar_scraper/reporting.py` (CSV writer, assessment, report builder, failure report)
- `README.md`, `requirements.txt` (`playwright>=1.45`)
- `output/extraction_report.txt` + `output/scraper.log` from the 2026-10-31 run: a **failed** run (HTTP 429). No CSVs.

## 3. Work Completed During This Session
- Full code audit against every requirement (section 4).
- Re-ran the real scraper: browser launched (Edge), profile URL opened, HTTP 429 → `/sorry/` page → scraper stopped cleanly, wrote failure report (now includes HTTP status and landing URL). `output/` files were overwritten by this run's log/report (same failure outcome).
- **Added** `tests/test_pipeline_offline.py`: offline test with **synthetic fake data** that checks DOCX header match, exact CSV headers, dedup, idempotent re-runs, no invented IDs, empty missing values, author_order logic, report sections. Writes to `tests/_tmp_output/` (not `output/`).
- No existing source file was modified. Nothing was rewritten or deleted.
- Added this report.

## 4. Final Requirement Audit
Status key: COMPLETE = implemented and verified; PARTIAL = implemented, not verified live; NOT STARTED.

| Requirement | Status | Implementation Details | Verification |
|---|---|---|---|
| Opens live profile in Playwright | PARTIAL | `browser._scrape`: `page.goto` | Browser launched and URL opened (log), but Google returned 429, so page content never seen |
| Waits for page load | PARTIAL | waits for `#gsc_prf_in`, networkidle | Not reached (blocked) |
| Inspects live DOM | PARTIAL | `page.evaluate` JS_HEADER/METRICS/PUBLICATIONS | Selectors never tested on live page |
| Dynamic content / scrolling | PARTIAL | scroll to bottom + click `#gsc_bpf_more` until disabled | Not tested live |
| Front page only | COMPLETE (design) | single `goto`; navigations recorded in report; hrefs read only | Code review; blocked run recorded only the profile/sorry navigation |
| CAPTCHA / 429 handling | COMPLETE | `_check_blocked`, `AccessBlockedError`, failure report | **Executed twice**, real 429 handled correctly, exit code 1 shown by shell (see note below) |
| SCHOLARS attributes (10) | PARTIAL | header + metrics extraction | Headers verified; values only offline-synthetic |
| RESEARCH_AREAS (6) | PARTIAL | interests from `#gsc_prf_int` | Headers verified; values offline-synthetic |
| PUBLICATIONS (9) | PARTIAL | table rows | same |
| Scholar Research Areas (5) | PARTIAL | same | same |
| Authors Publications (5) | PARTIAL | author_order by exact name match | logic tested offline |
| Separate CSVs per entity | COMPLETE (code) | 5 files via `write_all_csvs` | Offline test; no real CSVs |
| Exact headers vs DOCX | COMPLETE | `validate_against_docx` at every run | **Executed**: all 6 entities match (log, both runs) |
| Empty missing values, UTF-8 | COMPLETE | `csv.DictWriter`, `encoding="utf-8"` | offline test |
| Duplicate handling / re-run idempotence | COMPLETE | dedup by `citation_for_view` key; CSVs overwritten, not appended | offline test (synthetic) |
| No fabricated data / IDs | COMPLETE | internal IDs always empty | offline test |
| extraction_report.txt (success/partial/unavailable + reasons, system vs scraped, stats, errors) | PARTIAL | `build_report` | Success layout rendered only from synthetic data in test (string-section check); real failure report verified |
| No screenshot use; no publication pages | COMPLETE | code review | n/a |
| Live end-to-end run | **NOT ACHIEVED** | n/a | blocked by Google (429) |

Note: `run_scraper.py` returns exit code 2 on failure; the shell reported exit code 1 because of how `main` was invoked through the wrapper/log. Not independently investigated: treat as an open minor item.

## 5. Project Folder Structure
```
Project/
├── PROJECT_HANDOVER_REPORT.md
├── README.md
├── requirements.txt                 (playwright>=1.45)
├── run_scraper.py
├── Research_Companion_ERD_Listing_Exact_Names_V2.docx
├── scholar_scraper/
│   ├── __init__.py
│   ├── browser.py
│   ├── docx_schema.py
│   ├── erd_schema.py
│   ├── reporting.py
│   └── transform.py
├── tests/
│   ├── test_pipeline_offline.py
│   └── _tmp_output/                 (synthetic test CSVs, NOT real data)
└── output/
    ├── extraction_report.txt        (FAILED report, HTTP 429)
    └── scraper.log
```
No `supplementary/` folder and no real CSVs exist because no run succeeded.

## 6. Source Code Explanation
- **run_scraper.py**: entry point. Parses args, sets up logging to `output/scraper.log`, validates headers against DOCX, calls `scrape_profile`, `transform`, `write_all_csvs`, `write_supplementary`, `assess`/`build_report`. On failure writes `build_failure_report` and returns 2. Depends on all package modules. Output → `output/`.
- **browser.py**: `parse_google_scholar_id`, `_launch`, `_check_blocked`, `scrape_profile`/`_scrape`; JS snippets extract header (name, affiliation, verification line, interests), metrics table, publication rows (title, link, authors, venue, year, cited_by). Returns `ProfileData`.
- **erd_schema.py**: `ERD_ENTITIES` (exact columns) and `CSV_FILES` (HEIs is intentionally not exported).
- **docx_schema.py**: `read_docx_entities`, `validate_against_docx`; stdlib only.
- **transform.py**: `transform` builds rows; `clean_int`, `name_variants`, `find_author_order`; dedup; truncation/DOI counters; `total_publications` set only if listing is complete.
- **reporting.py**: `write_csv`, `write_all_csvs`, `write_supplementary` (record linkage + raw snapshot JSON), `assess` (per-attribute status/reason), `build_report`, `build_failure_report`.
- **tests/test_pipeline_offline.py**: described above.

Flow: parse args → logging → DOCX header check → launch browser → goto profile → block check → wait header → SHOW MORE loop → JS extract → transform → CSVs + supplementary → report.

## 7. Data Extraction Mapping
Status is by design; **no attribute has been confirmed against the live page.**

| Entity | Attribute | Source | Status | Notes |
|---|---|---|---|---|
| SCHOLARS | scholar_id | n/a | Database-generated, left empty | |
| SCHOLARS | hei_id | n/a | Unavailable | needs HEI mapping |
| SCHOLARS | google_scholar_id | `user=` in URL | Scraped (from URL) | |
| SCHOLARS | scholar_name | `#gsc_prf_in` | Scraped (unverified live) | |
| SCHOLARS | email | header | Partial: GS shows only domain, cell left empty | |
| SCHOLARS | google_scholar_profile_url | loaded URL | Scraped | |
| SCHOLARS | h_index | `#gsc_rsb_st` "All" | Scraped (unverified live) | |
| SCHOLARS | total_publications | row count after full listing | Additional processing | empty if listing not confirmed complete |
| SCHOLARS | total_citations | `#gsc_rsb_st` "All" | Scraped (unverified live) | |
| SCHOLARS | last_synced_at | clock | System-generated | |
| RESEARCH_AREAS | research_area_id | n/a | Database-generated | |
| RESEARCH_AREAS | area_name | `#gsc_prf_int` tags | Scraped (unverified live) | |
| RESEARCH_AREAS | description, is_computing_area, keywords | n/a | Unavailable | need curated taxonomy |
| RESEARCH_AREAS | created_at | n/a | Database-generated | |
| PUBLICATIONS | publication_id | n/a | Database-generated | |
| PUBLICATIONS | title, publication_year, citation_count, source_url | table | Scraped (unverified live) | blank citations stay empty |
| PUBLICATIONS | venue, authors_text | table | Partial: GS truncates with "..." | full text needs detail page (out of scope) |
| PUBLICATIONS | digital_object_identifier | n/a | Unavailable on front page | regex only if DOI visible in text |
| PUBLICATIONS | last_synced_at | clock | System-generated | |
| Scholar Research Areas | scholar_id, research_area_id | n/a | Database-generated | |
| Scholar Research Areas | is_primary_area | n/a | Unavailable | |
| Scholar Research Areas | area_source / updated_at | constant / clock | System-generated | |
| Authors Publications | scholar_id, publication_id | n/a | Database-generated | |
| Authors Publications | author_order | derived from authors_text | Additional processing, partial | |
| Authors Publications | source_method / recorded_at | constant / clock | System-generated | |
| HEIs (not exported) | all 8 attributes | n/a | Unavailable/DB/application-defined | assessed in report only |

## 8. CSV Output Details
**No real CSV files exist.** The only run attempts failed with HTTP 429, so records = 0, populated = 0, empty = 0 for all five files (`scholars.csv`, `research_areas.csv`, `publications.csv`, `scholar_research_areas.csv`, `authors_publications.csv`).
Column names (verified identical to the DOCX by `validate_against_docx` and by the offline test, using synthetic data):
- SCHOLARS: scholar_id, hei_id, google_scholar_id, scholar_name, email, google_scholar_profile_url, h_index, total_publications, total_citations, last_synced_at
- RESEARCH_AREAS: research_area_id, area_name, description, is_computing_area, keywords, created_at
- PUBLICATIONS: publication_id, title, publication_year, citation_count, venue, authors_text, digital_object_identifier, source_url, last_synced_at
- Scholar Research Areas: scholar_id, research_area_id, is_primary_area, area_source, updated_at
- Authors Publications: scholar_id, publication_id, author_order, source_method, recorded_at

Validation status of real files: not applicable (none generated). Synthetic files in `tests/_tmp_output/` passed header checks but are fake.

## 9. Actual Test Results
**Live run (2026-11-01 07:45 +05:00):**
- URL: `https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ`
- Researcher name: not extracted. Research areas: 0. Publications: 0.
- Browser: Playwright bundled Chromium unavailable; Microsoft Edge launched OK.
- Result: HTTP 429, redirected to `https://www.google.com/sorry/index?...`; scraper stopped; failure report written; no CSVs.
- DOCX schema check: PASSED (all 6 entities).

**Offline synthetic test:** 15/15 checks passed (`python tests/test_pipeline_offline.py`). This tests transformation logic only, not Google Scholar.

## 10. Remaining Limitations
- **Blocking:** Google Scholar currently blocks this machine/IP for automated access. Live extraction is unverified. The scraper deliberately does not bypass.
- Selectors (`#gsc_prf_in`, `#gsc_a_b`, `#gsc_bpf_more`, `#gsc_rsb_st`, `#gsc_prf_int`) are based on known Scholar markup, not confirmed in this session.
- Email: Scholar exposes only the verified domain. DOI absent from front page. Authors/venue are truncated. `description`, `is_computing_area`, `keywords`, `is_primary_area`, HEI data unavailable.
- Internal IDs and `created_at` need the database.
- Chromium bundled browser isn't installed (Edge fallback is used).
- Exit code discrepancy noted in section 4.
- Success-path report text has only been rendered from synthetic data.

## 11. How to Run the Project (Windows PowerShell)
```powershell
cd "C:\Users\Muhammad Aamir\Desktop\FYP\Project"
pip install -r requirements.txt
python -m playwright install chromium        # optional; Edge/Chrome used if absent
python run_scraper.py --url "https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ"
# or
$env:SCHOLAR_URL = "https://scholar.google.com/citations?user=<ID>"; python run_scraper.py
python tests\test_pipeline_offline.py        # offline logic tests
```
CSVs: `output\*.csv` (+ `output\supplementary\`). Report: `output\extraction_report.txt`. Log: `output\scraper.log`.
If blocked: wait (hours), try another network, or use `--headed` and a larger `--delay`. Do not add CAPTCHA-solving or proxy rotation.

## 12. Instructions for the Next AI Assistant
- **Status:** code complete; live verification outstanding.
- **Inspect first:** `run_scraper.py`, `scholar_scraper/browser.py`, `transform.py`, `reporting.py`.
- **Do not rewrite:** schema modules, DOCX validation, transform rules (no fabricated IDs/values), block detection.
- **Pending:**
  1. Obtain one successful live run (when Google allows) and verify selectors, SHOW MORE loop, and the success-path report.
  2. Verify real CSVs (counts, empty cells), and re-run for idempotence.
  3. Check the exit-code behaviour (2 vs 1).
  4. Update section 8/9 of this report with real numbers.
- **Next steps:** optionally add a saved-HTML fixture test (saved by a human from a normal browser) for the extractors; add an HEIs mapping step and DB loader that resolves the row-N linkage via `supplementary/record_linkage.csv`.
- Keep the scope rules: front page only, no screenshot data, no bypassing of access restrictions.

## 13. Addendum: Manual verification mode (session 3)
- **Modified files:** `scholar_scraper/browser.py` (JS_PAGE_STATE, STATE_MESSAGES, `_await_manual_access`, `_verify_profile_owner`), `run_scraper.py` (`--manual`, `--manual-wait`). Nothing else changed.
- **429 detection point:** `browser._check_blocked`, right after `page.goto` (HTTP 403/429, `/sorry/` URL, CAPTCHA element, unusual-traffic text) raises `AccessBlockedError`. Without `--manual`, `run_scraper.py` writes the failure report and exits.
- **Manual mode:** visible browser; on a block it classifies the page every second as profile / interactive CAPTCHA (real widget present) / unusual-traffic 429 block / unknown, and prints state-specific instructions. The user is asked to solve something only for a real CAPTCHA. It never reloads, retries, clicks or fills; no stealth. When the profile appears, the `user=` ID is verified against the request before extraction. On timeout it raises `AccessBlockedError`, writes the failure report, and writes no CSVs.
- **Command:** `python run_scraper.py --manual --manual-wait 900 --url "https://scholar.google.com/citations?hl=en&user=vdh-mOQAAAAJ"`
- **Verification so far:** the 2026-11-01 07:55 run was classified correctly as the unusual-traffic 429 block (no CAPTCHA). Live selector verification and the offline test re-run after these edits are PENDING (blocked).

