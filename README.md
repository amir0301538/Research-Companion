# Google Scholar profile front-page scraper

The application opens a user-supplied Google Scholar researcher profile, clicks
**SHOW MORE** until the publication listing is complete, and extracts only data
shown on that profile. Publication title links are saved as `source_url`; they
are never opened by the scraper. Google Scholar HTTP 429 and CAPTCHA restrictions
stop the run. Manual verification is available only for an interactive challenge
that Google permits the user to solve.

## Run

```powershell
python gui_app.py
python run_scraper.py --url "https://scholar.google.com/citations?user=vdh-mOQAAAAJ" --export-format csv
python run_scraper.py --url "https://scholar.google.com/citations?user=vdh-mOQAAAAJ" --export-format json
python -m pytest -q
```

Install dependencies with `python -m pip install -r requirements.txt`.
The GUI defaults to CSV. The six CSV tables in `output/csv/` are canonical
persistent storage. `output/json/dataset.json` mirrors the same validated
tables and scholar image metadata after every scrape, refresh, and deletion,
regardless of the selected format. The selection controls the highlighted
export location. Switching format does not reassign IDs. Existing scholars are
reported without scraping again.

## Output layout and migration

```text
output/
  csv/                 six core tables and scholar_images.csv
  json/                dataset.json
  profile_images/      validated scholar photos
  reports/             extraction and refresh_all_report.json
  logs/                scraper.log
  system/              id_registry.json, .dataset.lock, record_linkage.csv
    checkpoints/       retained historical detail checkpoints
    snapshots/         retained global and scholar-scoped profile snapshots
    historical/        retired publication detail and yearly citation CSVs
    legacy/            other preserved files from the old layout
```

Starting the GUI or runner automatically detects the former flat `output/`
layout. Before moving files, it validates the core CSVs, checks image paths,
creates a complete byte-verified backup beside `output/` named
`output_migration_backup_*`, and uses a recovery journal for the migration.
The original ID registry bytes are preserved. The backup remains available for
manual recovery and is excluded from Git. Re-running migration is a no-op.
If validation or a locked file prevents migration, the prior dataset is restored
and the error is shown. Do not manually mix files from the old and new layouts.

**Refresh All Scholars** confirms the number of stored profiles, then visits
their front pages sequentially. It updates existing IDs, metrics, publication
counts, interests, affiliations, and validated profile images. New publications
are added; publications missing from one complete profile remain linked and are
listed for review in `output/reports/refresh_all_report.json`. Ambiguous
title/year changes for an existing citation URL are also listed for review.
The Stop button finishes the current scholar transaction and skips the rest.
HTTP 429 or CAPTCHA stops the batch. No publication detail page is visited.

## Data model

The six DOCX-verified CSV headers remain unchanged:

| File | Columns |
|---|---|
| `heis.csv` | `hei_id,hei_name,short_name,city,country,website_url,target_university,created_at` |
| `scholars.csv` | `scholar_id,hei_id,google_scholar_id,scholar_name,email,google_scholar_profile_url,h_index,total_publications,total_citations,last_synced_at` |
| `publications.csv` | `publication_id,title,publication_year,citation_count,venue,authors_text,digital_object_identifier,source_url,last_synced_at` |
| `research_areas.csv` | `research_area_id,area_name,description,is_computing_area,keywords,created_at` |
| `scholar_research_areas.csv` | `scholar_id,research_area_id,is_primary_area,area_source,updated_at` |
| `authors_publications.csv` | `scholar_id,publication_id,author_order,source_method,recorded_at` |

`scholar_images.csv` is a proposed additional entity keyed by `scholar_id`.
Its columns are `scholar_id,google_scholar_id,local_profile_image_path,original_image_url,image_status,last_image_synced_at`.
Oracle deployments need a separate scholar image table and foreign key to
`SCHOLARS.scholar_id`; the original ERD and core table headers are unchanged.
`dataset.json` has the entity keys `HEIs`, `SCHOLARS`, `PUBLICATIONS`,
`RESEARCH_AREAS`, `Scholar Research Areas`, `Authors Publications`, and
`SCHOLAR_IMAGES`, with the exact field names above.

The browser reads the image URL from `#gsc_prf_pup-img` (src, data-src, or
computed background image), resolves it against the profile URL, and downloads
it with the active browser context. JPEG, PNG, WebP, and GIF signatures and
MIME types must agree. Valid bytes are stored in `output/profile_images/`
under the Google Scholar ID with the actual extension. Missing, anonymous, or
failed downloads use the bundled `assets/default_anonymous.svg`. An existing
verified image is preserved if a later download fails. The default is an SVG
rather than a PNG; its format is declared honestly in metadata.

The ID registry retains historical mappings, including IDs of deleted rows.
Scraping, deletion, and export share an OS dataset lock. Core CSVs, JSON, image
metadata, image bytes, and registry updates use a staged, backed-up transaction
journal. The next operation recovers an interrupted transaction. Deletion keeps
shared publications, HEIs, and research areas and updates image metadata and
JSON in the same transaction.

Historical publication detail and yearly citation CSVs are retained in
`output/system/historical/`; detail checkpoints are retained in
`output/system/checkpoints/`. They remain available for deletion compatibility.
New scraping runs never create or update detail records or checkpoints.
Scholar-scoped raw profile snapshots are under `output/system/snapshots/profiles/`.

Unknown research areas are stored with an empty `is_computing_area`; research
discipline never determines scraper eligibility. The displayed verified email
domain is not converted into an invented email address.
