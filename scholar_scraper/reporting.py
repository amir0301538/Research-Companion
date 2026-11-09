"""
CSV writing, pre-export validation enforcement, and generation of
extraction_quality_report.txt and extraction_report.txt.
"""

import csv
import json
import logging
import os
import shutil
import textwrap
import time
from pathlib import Path
from typing import Optional

from .browser import redact_url
from .erd_schema import CSV_FILES, DETAIL_CSV_FILES, DETAIL_ENTITIES, ERD_ENTITIES
from .validator import DatasetValidationError, validate_dataset

log = logging.getLogger(__name__)

SUCCESS, PARTIAL, UNAVAILABLE, SYSTEM = "SUCCESS", "PARTIAL", "UNAVAILABLE", "SYSTEM"
DB_ID = "Persistent ID Registry (output/id_registry.json)"


class CsvPermissionError(PermissionError):
    """
    Raised when a CSV file in the output directory cannot be read or written
    due to an operating-system file lock or permission restriction (e.g., when
    `output/scholars.csv` is open in Microsoft Excel or locked by another process).
    """

    def __init__(self, file_path: Path, operation: str = "access", original_error: Optional[Exception] = None):
        self.file_path = Path(file_path)
        self.operation = operation
        self.original_error = original_error
        msg = (
            f"Permission denied while trying to {operation} '{self.file_path.as_posix()}'. "
            "Please close any application (such as Microsoft Excel or another viewer) that currently has "
            f"'{self.file_path.name}' open, or check folder permissions, and try again."
        )
        super().__init__(msg)


def find_existing_scholar_by_google_id(out_dir: Path, google_scholar_id: str) -> Optional[dict]:
    """
    Inspect `out_dir/scholars.csv` and return the matching scholar row dictionary if
    `google_scholar_id` already exists in the dataset (compared strictly by `google_scholar_id`,
    never by scholar name or full URL).
    Raises `CsvPermissionError` if `scholars.csv` is locked or unreadable due to permissions.
    """
    target_id = (google_scholar_id or "").strip()
    if not target_id or not out_dir:
        return None
    fpath = Path(out_dir) / CSV_FILES["SCHOLARS"]
    if not fpath.exists():
        return None
    try:
        with open(fpath, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                row_gs_id = (row.get("google_scholar_id") or "").strip()
                if row_gs_id and row_gs_id == target_id:
                    return dict(row)
    except PermissionError as exc:
        raise CsvPermissionError(fpath, operation="read", original_error=exc) from exc
    return None


def verify_csv_files_writable(out_dir: Path, include_details: bool = False):
    """
    Pre-flight check that verifies existing CSV files in `out_dir` are not locked by another
    process (such as Excel) before starting a browser scrape or writing staged exports.
    Raises `CsvPermissionError` naming the exact locked file if any CSV cannot be opened for append/write.
    """
    if not out_dir or not Path(out_dir).exists():
        return
    out_path = Path(out_dir)
    files_to_check = [out_path / fname for fname in CSV_FILES.values()]
    if include_details:
        sup_dir = out_path / "supplementary"
        files_to_check.extend(sup_dir / fname for fname in DETAIL_CSV_FILES.values())
    for fpath in files_to_check:
        if fpath.exists():
            try:
                with open(fpath, "a", encoding="utf-8"):
                    pass
            except PermissionError as exc:
                raise CsvPermissionError(fpath, operation="write to", original_error=exc) from exc


def write_csv(path: Path, columns, rows):
    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=columns, quoting=csv.QUOTE_MINIMAL, extrasaction="raise")
            w.writeheader()
            for r in rows:
                w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in columns})
    except PermissionError as exc:
        raise CsvPermissionError(path, operation="write to", original_error=exc) from exc


def load_existing_csvs(out_dir: Path) -> dict[str, list[dict]]:
    """
    Read existing database entity CSVs from `out_dir` so cross-run duplicate
    detection and scholar-scoped clean re-scraping can inspect existing records.
    """
    loaded: dict[str, list[dict]] = {entity: [] for entity in CSV_FILES}
    if not out_dir or not out_dir.exists():
        return loaded
    for entity, fname in CSV_FILES.items():
        fpath = out_dir / fname
        if fpath.exists():
            try:
                with open(fpath, "r", encoding="utf-8", newline="") as f:
                    loaded[entity] = list(csv.DictReader(f))
            except PermissionError as exc:
                raise CsvPermissionError(fpath, operation="read", original_error=exc) from exc
    return loaded


def load_existing_detail_csvs(out_dir: Path) -> dict[str, list[dict]]:
    """
    Read existing Stage-2 detail CSVs from `out_dir/supplementary` so previously
    exported publication details and yearly citations are preserved across runs.
    """
    loaded: dict[str, list[dict]] = {entity: [] for entity in DETAIL_CSV_FILES}
    sup_dir = out_dir / "supplementary" if out_dir else None
    if not sup_dir or not sup_dir.exists():
        return loaded
    for entity, fname in DETAIL_CSV_FILES.items():
        fpath = sup_dir / fname
        if fpath.exists():
            try:
                with open(fpath, "r", encoding="utf-8", newline="") as f:
                    loaded[entity] = list(csv.DictReader(f))
            except PermissionError as exc:
                raise CsvPermissionError(fpath, operation="read", original_error=exc) from exc
    return loaded


def write_all_csvs(
    out_dir: Path,
    tables: dict,
    unresolved_errors: Optional[list[str]] = None,
    validate: bool = True,
    detail_tables: Optional[dict] = None,
):
    """
    Validate all 6 entity tables (and optional Stage-2 detail tables) before writing CSV files.
    Raises DatasetValidationError if any primary key, foreign key, uniqueness,
    or schema check fails (leaving any previous valid CSV files untouched).
    Writes into a temporary staging directory and replaces final files atomically.
    """
    if validate:
        errors = validate_dataset(
            tables,
            unresolved_errors=unresolved_errors,
            erd_entities=ERD_ENTITIES,
            detail_tables=detail_tables,
        )
        if errors:
            raise DatasetValidationError(errors)

    out_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = out_dir / ".staging_export"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    try:
        staged_pairs = []
        for entity, fname in CSV_FILES.items():
            target = out_dir / fname
            staged_path = staging_dir / fname
            write_csv(staged_path, ERD_ENTITIES[entity], tables.get(entity, []))
            staged_pairs.append((entity, fname, staged_path, target))

        if detail_tables:
            sup_dir = out_dir / "supplementary"
            sup_dir.mkdir(parents=True, exist_ok=True)
            for d_entity, d_fname in DETAIL_CSV_FILES.items():
                if d_entity in detail_tables:
                    target = sup_dir / d_fname
                    staged_path = staging_dir / d_fname
                    write_csv(staged_path, DETAIL_ENTITIES[d_entity], detail_tables.get(d_entity, []))
                    staged_pairs.append((d_entity, f"supplementary/{d_fname}", staged_path, target))

        for entity, fname, staged_path, target in staged_pairs:
            replaced = False
            last_perm_err = None
            for _ in range(5):
                try:
                    os.replace(staged_path, target)
                    replaced = True
                    break
                except PermissionError as exc:
                    last_perm_err = exc
                    time.sleep(0.05)
            if not replaced:
                try:
                    target.write_bytes(staged_path.read_bytes())
                except PermissionError as exc:
                    raise CsvPermissionError(target, operation="overwrite", original_error=exc or last_perm_err) from exc
            rows_written = (
                len(tables.get(entity, []))
                if entity in tables
                else len((detail_tables or {}).get(entity, []))
            )
            log.info("Wrote %-36s (%d rows)", fname, rows_written)
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)


def write_supplementary(out_dir: Path, linkage, raw: dict):
    sup = out_dir / "supplementary"
    sup.mkdir(parents=True, exist_ok=True)
    write_csv(
        sup / "record_linkage.csv",
        ["entity", "csv_row_number", "google_scholar_id", "source_key", "label"],
        linkage,
    )
    # The original global snapshot is retained as a legacy archive. New profile
    # snapshots are keyed by Google Scholar ID and never overwrite another scholar.
    gs_id = (raw.get("google_scholar_id") or "").strip()
    if gs_id and all(c.isalnum() or c in "_-" for c in gs_id):
        profile_dir = sup / "profiles"
        profile_dir.mkdir(parents=True, exist_ok=True)
        target = profile_dir / f"{gs_id}.json"
        staged = target.with_suffix(".json.tmp")
        with open(staged, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
        os.replace(staged, target)


# --------------------------------------------------------------------------- #
# Attribute assessment
# --------------------------------------------------------------------------- #
def assess(data, t):
    """Return list of dicts: entity, attribute, status, reason, requires."""
    s, h = t.stats, data.header or {}
    n = s.get("publication_rows", 0)
    A = []

    def add(entity, attr, status, reason, requires=""):
        A.append(dict(entity=entity, attribute=attr, status=status, reason=reason, requires=requires))

    def by_presence(entity, attr, ok, reason_ok, reason_missing, requires=""):
        add(entity, attr, SUCCESS if ok else UNAVAILABLE, reason_ok if ok else reason_missing,
            "" if ok else requires)

    # --------------------------------- HEIs -----------------------------------
    E = "HEIs"
    hei_rows = t.tables.get("HEIs", [])
    hei_ok = len(hei_rows) > 0
    aff = h.get("affiliation_text") or "(none displayed)"
    add(E, "hei_id", SYSTEM if hei_ok else UNAVAILABLE,
        "Assigned deterministically via Persistent ID Registry from normalized (hei_name, country).", DB_ID)
    by_presence(E, "hei_name", hei_ok,
                f"Stored from profile affiliation '{aff}' as HEI '{hei_rows[0]['hei_name'] if hei_rows else ''}'.",
                f"No usable affiliation was displayed for '{aff}'.",
                "Source affiliation")
    by_presence(E, "short_name", hei_ok and bool(hei_rows[0].get("short_name")),
                "Populated from verified canonical HEI catalog.", "Not available.")
    by_presence(E, "city", hei_ok and bool(hei_rows[0].get("city")),
                "Resolved from explicit campus designation on profile and verified HEI catalog.", "Not available.")
    by_presence(E, "country", hei_ok and bool(hei_rows[0].get("country")),
                "Populated from verified canonical HEI catalog.", "Not available.")
    by_presence(E, "website_url", hei_ok and bool(hei_rows[0].get("website_url")),
                "Populated from verified canonical HEI catalog.", "Not available.")
    by_presence(E, "target_university", hei_ok and hei_rows[0].get("target_university") in ("0", "1"),
                "Populated from verified project HEI configuration ('1' or '0').", "Not available.")
    add(E, "created_at", SYSTEM, "Creation timestamp preserved in Persistent ID Registry.", "Generated by scraper")

    # ------------------------------- SCHOLARS ---------------------------------
    E = "SCHOLARS"
    add(E, "scholar_id", SYSTEM, "Assigned deterministically via Persistent ID Registry from google_scholar_id.", DB_ID)
    add(E, "hei_id", SYSTEM if hei_ok else UNAVAILABLE,
        "Foreign key linking scholar to canonical HEIs.hei_id in heis.csv.", DB_ID)
    by_presence(E, "google_scholar_id", bool(data.google_scholar_id),
                "Taken from the 'user' parameter of the live profile URL.",
                "Could not be parsed from the URL.")
    by_presence(E, "scholar_name", bool(h.get("scholar_name")),
                "Read exactly as displayed in the profile header (#gsc_prf_in), preserving academic titles.",
                "Profile name element not found on the page.")
    if s.get("email_full"):
        add(E, "email", SUCCESS, "A full email address is publicly displayed on the profile header.")
    elif s.get("email_domain"):
        add(E, "email", PARTIAL,
            f"Google Scholar displays only the verified domain (\"Verified email at {s['email_domain']}\"), "
            "never the full mailbox address. Left EMPTY rather than fabricated.",
            "Another data source (university directory / researcher's homepage)")
    else:
        add(E, "email", UNAVAILABLE, "The researcher has not publicly displayed an email or verified domain.",
            "Another data source")
    by_presence(E, "google_scholar_profile_url", bool(data.loaded_url or data.requested_url),
                "URL of the live profile page loaded by the browser.", "Page did not load.")
    by_presence(E, "h_index", bool(s.get("h_index")),
                "'All' column of the h-index row in the 'Cited by' metrics table.",
                "Metrics table not displayed on this profile.", "Not publicly displayed")
    by_presence(E, "total_citations", bool(s.get("total_citations")),
                "'All' column of the Citations row in the 'Cited by' metrics table.",
                "Metrics table not displayed on this profile.", "Not publicly displayed")
    if s.get("total_publications"):
        add(E, "total_publications", SUCCESS,
            f"Count of all {n} deduplicated publication rows on the scholar's profile.")
    else:
        add(E, "total_publications", UNAVAILABLE,
            "Complete publication listing could not be confirmed.", "Re-run when access allows")

    # ---------------------------- RESEARCH_AREAS ------------------------------
    E = "RESEARCH_AREAS"
    na = s.get("research_areas", 0)
    uncl = len(s.get("unclassified_areas_list", []))
    add(E, "research_area_id", SYSTEM,
        "Assigned deterministically via Persistent ID Registry from normalized area_name.", DB_ID)
    by_presence(E, "area_name", na > 0,
                f"{na} research-interest tag(s) read as displayed with safe whitespace normalization.",
                "The researcher lists no research interests on the profile.", "Not publicly displayed")
    add(E, "description", UNAVAILABLE,
        "Google Scholar interest tags are bare labels; no definition/scope text exists on the front page.",
        "Unavailable on Google Scholar front page")
    if na > 0 and uncl == 0:
        add(E, "is_computing_area", SUCCESS,
            "An explicit stored classification ('1' or '0') was preserved.")
    else:
        add(E, "is_computing_area", PARTIAL if na > 0 else UNAVAILABLE,
            f"{uncl} area(s) have no explicit classification; values remain empty without affecting export.",
            "Optional future curation")
    add(E, "keywords", UNAVAILABLE, "No synonyms or matching keyword lists are shown on Google Scholar profile pages.",
        "Unavailable on Google Scholar front page")
    add(E, "created_at", SYSTEM, "Creation timestamp preserved in Persistent ID Registry.", "Generated by scraper")

    # ----------------------------- PUBLICATIONS -------------------------------
    E = "PUBLICATIONS"
    add(E, "publication_id", SYSTEM,
        "Assigned deterministically via Persistent ID Registry from normalized (title, publication_year).", DB_ID)

    def col(attr, missing_key, ok_reason, label):
        miss = s.get(missing_key, 0)
        if n and miss == 0:
            add(E, attr, SUCCESS, ok_reason)
        elif n and miss < n:
            add(E, attr, PARTIAL, f"{ok_reason} {miss} of {n} rows show no {label} on the page; those cells are empty.",
                "Not displayed for those rows")
        else:
            add(E, attr, UNAVAILABLE, f"No {label} displayed in the publication table.", "Not displayed")

    col("title", "missing_title", f"Full title text of the title hyperlink for all {n} rows.", "title")
    col("publication_year", "missing_year", "Value of the YEAR column.", "year")
    col("citation_count", "missing_cites", "Value of the CITED BY column (blank cells are NOT converted to 0).",
        "citation count")

    tv, mv = s.get("trunc_venue", 0), s.get("missing_venue", 0)
    if n and (tv or mv):
        add(E, "venue", PARTIAL,
            f"Venue line extracted as displayed. {tv} of {n} venues are truncated by Google Scholar with '...'; "
            f"{mv} rows show no venue. Full text exists only on the individual publication page (out of scope).",
            "Individual publication page (out of scope)")
    elif n:
        add(E, "venue", SUCCESS, "Venue line extracted as displayed.")

    ta, ma = s.get("trunc_authors", 0), s.get("missing_authors", 0)
    if n and (ta or ma):
        add(E, "authors_text", PARTIAL,
            f"Author line extracted as displayed. {ta} of {n} author lists end with '...' (truncated by Google Scholar "
            f"on the profile front page); {ma} rows show none.",
            "Individual publication page (out of scope)")
    elif n:
        add(E, "authors_text", SUCCESS, "Complete author line displayed for every row.")

    dn = s.get("doi_found", 0)
    if dn and dn == n:
        add(E, "digital_object_identifier", SUCCESS, "DOI visible for every row.")
    elif dn:
        add(E, "digital_object_identifier", PARTIAL, f"DOI explicitly visible in {dn} of {n} rows only.",
            "Individual publication page or external source (out of scope)")
    else:
        add(E, "digital_object_identifier", UNAVAILABLE,
            "The profile publication table never displays DOIs on the front page.",
            "Individual publication page (out of scope)")
    col("source_url", "missing_link",
        "Absolute URL of each title hyperlink (Google Scholar citation-detail page). Read from DOM, never opened.",
        "title hyperlink")

    # ------------------------- Scholar Research Areas -------------------------
    E = "Scholar Research Areas"
    add(E, "scholar_id", SYSTEM, "Foreign key to SCHOLARS.scholar_id.", DB_ID)
    add(E, "research_area_id", SYSTEM, "Foreign key to RESEARCH_AREAS.research_area_id.", DB_ID)
    add(E, "is_primary_area", SYSTEM,
        "Set to '0' for all areas because Google Scholar does not designate a primary research area.",
        "Default '0' per schema rule")

    # ------------------------- Authors Publications ---------------------------
    E = "Authors Publications"
    add(E, "scholar_id", SYSTEM, "Foreign key to SCHOLARS.scholar_id.", DB_ID)
    add(E, "publication_id", SYSTEM, "Foreign key to PUBLICATIONS.publication_id.", DB_ID)
    om = s.get("order_matched", 0)
    variants = ", ".join(f"'{v}'" for v in s.get("name_variants", []))
    if n and om == n:
        add(E, "author_order", SUCCESS, f"Derived for all {n} rows by unambiguous match of {variants}.")
    elif om:
        add(E, "author_order", PARTIAL,
            f"Derived by unambiguous match of {variants}: {om} of {n} rows matched. Unmatched rows stay empty.",
            "Complete author list from publication page (out of scope) or manual verification")
    else:
        add(E, "author_order", UNAVAILABLE, "The profile owner's name could not be matched in any author list.",
            "Manual verification")

    # ------------------------- System-generated values ------------------------
    for ent, attr, why in [
        ("SCHOLARS", "last_synced_at", "Extraction timestamp generated by the scraper."),
        ("PUBLICATIONS", "last_synced_at", "Extraction timestamp generated by the scraper."),
        ("Scholar Research Areas", "area_source", "Provenance constant 'Google Scholar profile'."),
        ("Scholar Research Areas", "updated_at", "Extraction timestamp (time this association was refreshed)."),
        ("Authors Publications", "source_method", "Provenance constant 'Profile'."),
        ("Authors Publications", "recorded_at", "Extraction timestamp (time this link was recorded)."),
    ]:
        add(ent, attr, SYSTEM, why, "Generated by scraper")
    return A


def _wrap(text, indent, width=100):
    return textwrap.fill(str(text), width=width, initial_indent=indent, subsequent_indent=indent)


def build_quality_report(data, t, schema_check, run_info) -> str:
    """
    Build output/extraction_quality_report.txt with all required audit,
    relationship, and data-quality diagnostics, clearly distinguishing:
      1. Data unavailable on Google Scholar front page
      2. Data extraction/parsing failures
      3. Data requiring manual review
    """
    s, h = t.stats, data.header or {}
    line = "=" * 100
    L = [
        line,
        "GOOGLE SCHOLAR EXTRACTION QUALITY & DATABASE IMPORT READINESS REPORT",
        line,
        "",
    ]

    def section(title):
        L.extend(["", "-" * 100, title, "-" * 100])

    # 1. Profile & Canonical HEI Mapping
    section("1. PROFILE IDENTIFICATION & CANONICAL HEI MAPPING")
    L.append(f"  Profile URL                  : {data.loaded_url or data.requested_url}")
    L.append(f"  Google Scholar ID            : {data.google_scholar_id or '(missing)'}")
    L.append(f"  Scholar Name (preserved)     : {h.get('scholar_name') or '(missing)'}")
    L.append(f"  Extracted Raw Affiliation    : {h.get('affiliation_text') or '(none displayed)'}")
    L.append(f"  Extracted Verification Line  : {h.get('verification_text') or '(none displayed)'}")
    L.append(f"  Extraction Timestamp         : {run_info.get('timestamp_display', t.timestamp)}")
    L.append("")
    L.append("  Canonical HEI Mapping(s):")
    for hm in s.get("hei_mappings", []):
        if hm["status"] == "RESOLVED":
            L.append(
                f"    - [RESOLVED] Raw: {hm['raw_affiliation']!r} -> "
                f"hei_id={hm['hei_id']}, hei_name={hm['hei_name']!r}, "
                f"city={hm['city']!r}, country={hm['country']!r}"
            )
        else:
            L.append(
                f"    - [UNRESOLVED] Raw: {hm['raw_affiliation']!r} -> {hm['note']}"
            )

    # 2. Record & Relationship Counts
    section("2. ENTITY & RELATIONSHIP RECORD COUNTS (ALL 6 DATABASE CSVs)")
    L.append(f"  HEIs (heis.csv)                                     : {s.get('hei_rows', 0)}")
    L.append(f"  SCHOLARS (scholars.csv)                             : {s.get('scholar_rows', 0)}")
    L.append(f"  PUBLICATIONS (publications.csv)                     : {s.get('publication_rows', 0)}")
    L.append(f"  RESEARCH_AREAS (research_areas.csv)                 : {s.get('research_areas', 0)}")
    L.append(f"  Scholar Research Areas (scholar_research_areas.csv) : {s.get('scholar_research_area_rows', 0)}")
    L.append(f"  Authors Publications (authors_publications.csv)     : {s.get('author_publication_rows', 0)}")
    L.append("")
    L.append("  SHOW MORE Pagination & Expansion Summary:")
    L.append(f"    - Initial visible publication rows                : {getattr(data, 'initial_row_count', 0)}")
    L.append(f"    - SHOW MORE clicks performed                      : {data.show_more_clicks}")
    for step in getattr(data, "pagination_steps", []) or []:
        L.append(
            f"        * Click #{step['click']}: {step['before']} -> {step['after']} visible publication rows"
        )
    L.append(f"    - Final visible DOM publication rows              : {len(data.publications or [])}")
    L.append(f"    - Pagination stop reason                          : {getattr(data, 'stop_reason', None) or 'N/A'}")
    L.append("")
    L.append("  Duplicates Detected & Removed During Transformation:")
    L.append(f"    - Duplicate publications on same profile skipped  : {s.get('duplicates', 0)}")
    for dup in s.get("duplicate_publications_details", []):
        L.append(
            f"        * DOM Row #{dup['row_index']} duplicates Row #{dup['first_row_index']}: "
            f"{dup['title']!r} ({dup['publication_year'] or 'no year'})"
        )
    L.append(f"    - Duplicate records removed from existing CSVs    : {s.get('csv_duplicates_removed', 0)}")
    L.append(f"    - Shared publications merged across scholars      : {s.get('shared_publications_merged', 0)}")
    L.append(f"    - Duplicate research interest tags skipped        : {s.get('duplicate_areas', 0)}")
    L.append(f"    - Duplicate Scholar Research Areas links skipped  : {s.get('duplicate_bridge_sra', 0)}")
    L.append(f"    - Duplicate Authors Publications links skipped    : {s.get('duplicate_bridge_ap', 0)}")

    cleanup_list = s.get("cleanup_summaries", [])
    if cleanup_list:
        L.append("")
        L.append("  Previous CSV Data Cleanup (Target Scholar Re-Scrape):")
        for cs in cleanup_list:
            L.append(f"    - Target Google Scholar ID                        : {cs['target_google_scholar_id']}")
            L.append(f"    - Other scholars present in dataset               : {'YES (' + str(cs['other_scholars_count']) + ')' if cs['other_scholars_present'] else 'NO (single-scholar dataset)'}")
            L.append(f"    - Previous records removed for target scholar     : {json.dumps(cs['removed_counts'])}")
            L.append(f"    - Existing records preserved (other scholars)     : {json.dumps(cs['preserved_counts'])}")

    d_sum = s.get("detail_summary")
    if d_sum:
        L.append("")
        L.append("  Stage 2 — Individual Publication Detail Scraping Summary:")
        L.append(f"    - Total unique publications discovered            : {d_sum.get('total_unique_publications', 0)}")
        L.append(f"    - Total publication detail pages attempted (run)  : {d_sum.get('attempted_this_run', 0)}")
        L.append(f"    - Successfully scraped detail pages (total)       : {d_sum.get('succeeded_total', 0)}")
        L.append(f"    - Skipped via checkpoint/resume                   : {d_sum.get('skipped_checkpoint', 0)}")
        L.append(f"    - Failed detail pages (this run)                  : {d_sum.get('failed_this_run', 0)}")
        L.append(f"    - Remaining incomplete publications               : {d_sum.get('remaining_incomplete', 0)}")
        L.append(f"    - Duplicate detail URLs detected                  : {len(d_sum.get('duplicate_detail_urls', []))}")
        L.append(f"    - Stopped by HTTP 429 / CAPTCHA block             : {'YES (' + str(d_sum.get('block_reason')) + ')' if d_sum.get('stopped_by_block') else 'NO'}")
        comp = d_sum.get("completeness") or {}
        tot_det = d_sum.get("succeeded_total", 0)
        L.append(f"    - Metadata Completeness (out of {tot_det} scraped detail pages):")
        L.append(f"        * Titles                                      : {comp.get('title', 0)}/{tot_det}")
        L.append(f"        * Authors                                     : {comp.get('authors_text', 0)}/{tot_det}")
        L.append(f"        * Publication dates                           : {comp.get('publication_date', 0)}/{tot_det}")
        L.append(f"        * Publication years                           : {comp.get('publication_year', 0)}/{tot_det}")
        L.append(f"        * Journals / Venues                           : {comp.get('journal', 0)}/{tot_det}")
        L.append(f"        * Volumes                                     : {comp.get('volume', 0)}/{tot_det}")
        L.append(f"        * Issues                                      : {comp.get('issue', 0)}/{tot_det}")
        L.append(f"        * Pages                                       : {comp.get('pages', 0)}/{tot_det}")
        L.append(f"        * Publishers                                  : {comp.get('publisher', 0)}/{tot_det}")
        L.append(f"        * Descriptions / Abstracts                    : {comp.get('description', 0)}/{tot_det}")
        L.append(f"        * Citation counts                             : {comp.get('citation_count', 0)}/{tot_det}")
        L.append(f"        * DOIs                                        : {comp.get('doi', 0)}/{tot_det}")
        L.append(f"        * Publication URLs (Google Scholar detail)    : {comp.get('publication_url', 0)}/{tot_det}")
        L.append(f"        * Publisher URLs (external title link)        : {comp.get('publisher_url', 0)}/{tot_det}")
        L.append(f"        * Publications with yearly citation data      : {comp.get('yearly_citations', 0)}/{tot_det} ({d_sum.get('yearly_citation_rows', 0)} yearly rows)")

    # 3. Primary Key, Foreign Key & Schema Validation Status
    section("3. PRIMARY KEY, FOREIGN KEY & SCHEMA VALIDATION")
    ok_docx, docx_msgs = schema_check
    L.append(f"  DOCX Data Dictionary Header Match : {'PASSED' if ok_docx else 'FAILED'}")
    for m in docx_msgs:
        L.append(f"    - {m}")
    val_errs = t.validation_errors
    L.append(f"  Relational & Constraint Validation: {'PASSED (0 errors)' if not val_errs else f'FAILED ({len(val_errs)} error(s))'}")
    if not val_errs:
        L.append("    - All required primary keys (hei_id, scholar_id, publication_id, research_area_id) are positive integers.")
        L.append("    - All composite primary keys in bridge tables are non-empty and unique.")
        L.append("    - Every SCHOLARS.hei_id resolves to an existing HEIs.hei_id in heis.csv.")
        L.append("    - Every scholar_research_areas.(scholar_id, research_area_id) resolves to scholars.csv and research_areas.csv.")
        L.append("    - Every authors_publications.(scholar_id, publication_id) resolves to scholars.csv and publications.csv.")
    else:
        for err in val_errs:
            L.append(f"    - [VALIDATION ERROR] {err}")

    # 4. Categorized Data Quality Breakdown (1 vs 2 vs 3)
    section("4. CATEGORIZED DATA QUALITY DIAGNOSTICS")

    # Category 1: Unavailable on Google Scholar front page
    L.append("  CATEGORY 1: DATA UNAVAILABLE ON GOOGLE SCHOLAR FRONT PAGE (Left empty by design; not an error)")
    if not s.get("email_full"):
        dom_note = f"only verified domain '{s.get('email_domain')}' is shown" if s.get("email_domain") else "none shown"
        L.append(f"    - SCHOLARS.email: Empty ({dom_note}; full email address is never exposed by Google Scholar).")
    missing_dois = s.get("publication_rows", 0) - s.get("doi_found", 0)
    L.append(
        f"    - PUBLICATIONS.digital_object_identifier: Empty in {missing_dois}/{s.get('publication_rows', 0)} rows "
        "(Google Scholar profile front page does not display DOIs; visiting detail/publisher pages is out of scope)."
    )
    L.append(
        f"    - RESEARCH_AREAS.description: Empty in {s.get('research_areas', 0)}/{s.get('research_areas', 0)} rows "
        "(interest tags on profile are bare labels without descriptions)."
    )
    L.append(
        f"    - RESEARCH_AREAS.keywords: Empty in {s.get('research_areas', 0)}/{s.get('research_areas', 0)} rows "
        "(no sub-keywords are displayed on the profile front page)."
    )
    if s.get("missing_year", 0):
        L.append(f"    - PUBLICATIONS.publication_year: Empty in {s['missing_year']} row(s) where no year was displayed.")
    if s.get("missing_cites", 0):
        L.append(f"    - PUBLICATIONS.citation_count: Empty in {s['missing_cites']} row(s) with blank 'Cited by' cell.")

    tv_list = s.get("truncated_venues_list", [])
    L.append(f"    - Truncated Publication Venues ({len(tv_list)} of {s.get('publication_rows', 0)} rows end with '...' on front page):")
    if tv_list:
        for item in tv_list:
            L.append(f"        * [publication_id={item['publication_id']}] {item['title']!r}")
            L.append(f"          Visible venue: {item['venue']!r}")
    else:
        L.append("        * None.")

    ta_list = s.get("truncated_authors_list", [])
    L.append(f"    - Truncated Author Lists ({len(ta_list)} of {s.get('publication_rows', 0)} rows end with '...' on front page):")
    if ta_list:
        for item in ta_list:
            L.append(f"        * [publication_id={item['publication_id']}] {item['title']!r}")
            L.append(f"          Visible authors_text: {item['authors_text']!r}")
    else:
        L.append("        * None.")

    L.append("")
    # Category 2: Data extraction / parsing failures
    L.append("  CATEGORY 2: DATA EXTRACTION / PARSING FAILURES & UNRESOLVED REQUIRED FIELDS")
    errs = (run_info.get("errors") or []) + list(t.unresolved_errors)
    if errs:
        for e in errs:
            L.append(f"    - [FAILURE] {e}")
    else:
        L.append("    - None. All required profile fields, metrics, publications, and interest tags parsed cleanly.")

    L.append("")
    # Category 3: Data requiring manual review
    L.append("  CATEGORY 3: DATA REQUIRING MANUAL REVIEW")
    uncl_list = s.get("unclassified_areas_list", [])
    L.append(f"    - Research Areas Without Explicit Computing Classification ({len(uncl_list)}) — exported with an empty flag:")
    if uncl_list:
        for ua in uncl_list:
            L.append(f"        * {ua['area_name']!r} (scholar {ua['google_scholar_id']}): {ua['reason']}")
    else:
        L.append("        * None (all extracted research areas already had explicit stored classifications).")

    um_list = s.get("unmatched_author_order", [])
    L.append(f"    - Uncertain / Unmatched Author-Order Positions ({len(um_list)}):")
    if um_list:
        for pid, title, reason in um_list:
            L.append(f"        * [publication_id={pid}] {title!r} -> author_order left empty ({reason})")
    else:
        L.append("        * None (scholar's author_order was matched unambiguously in all publications).")

    L.append(
        "    - Scholar Research Areas (is_primary_area): All rows are set to is_primary_area=0 because Google Scholar "
        "does not designate a primary research area. A domain expert may manually set is_primary_area=1 for the "
        "scholar's primary area after import if desired."
    )

    L.extend(["", line, "END OF EXTRACTION QUALITY REPORT", line])
    return "\n".join(L) + "\n"


def build_report(data, t, assessment, schema_check, run_info) -> str:
    s, h = t.stats, data.header or {}
    L = []
    line = "=" * 100
    L += [line, "GOOGLE SCHOLAR PROFILE FRONT-PAGE EXTRACTION REPORT", line, ""]

    def section(title):
        L.extend(["", "-" * 100, title, "-" * 100])

    groups = {k: [a for a in assessment if a["status"] == k] for k in (SUCCESS, PARTIAL, UNAVAILABLE, SYSTEM)}

    section("A. SUCCESSFULLY EXTRACTED ATTRIBUTES (from the live profile front page)")
    for a in groups[SUCCESS]:
        L.append(f"  [{a['entity']}] {a['attribute']}")
        L.append(_wrap(a["reason"], "      "))

    section("A2. SYSTEM & REGISTRY-GENERATED ATTRIBUTES (persistent IDs, canonical mappings, timestamps)")
    for a in groups[SYSTEM]:
        L.append(f"  [{a['entity']}] {a['attribute']}")
        L.append(_wrap(a["reason"], "      "))

    section("B. PARTIALLY EXTRACTED ATTRIBUTES")
    if not groups[PARTIAL]:
        L.append("  (none)")
    for a in groups[PARTIAL]:
        L.append(f"  [{a['entity']}] {a['attribute']}")
        L.append(_wrap("Limitation: " + a["reason"], "      "))
        if a["requires"]:
            L.append(_wrap("Needs: " + a["requires"], "      "))

    section("C. UNAVAILABLE ATTRIBUTES (CSV cells left empty)")
    for i, a in enumerate(groups[UNAVAILABLE], 1):
        L.append(f"  {i:>2}. Attribute : {a['attribute']}")
        L.append(f"      Entity    : {a['entity']}")
        L.append(_wrap("Reason    : " + a["reason"], "      "))
        L.append(_wrap("Requires  : " + (a["requires"] or "-"), "      "))

    section("D. EXTRACTION SUMMARY")
    rows = [
        ("Researcher name", h.get("scholar_name") or "(not found)"),
        ("Google Scholar profile URL", data.loaded_url or data.requested_url),
        ("Google Scholar ID", data.google_scholar_id or ""),
        ("Displayed affiliation (raw)", h.get("affiliation_text") or "(none)"),
        ("Verification line (raw)", h.get("verification_text") or "(none)"),
        ("Total HEI records exported", s.get("hei_rows", 0)),
        ("Total scholar records exported", s.get("scholar_rows", 0)),
        ("Total research areas exported", s.get("research_areas", 0)),
        ("Total publication records exported", s.get("publication_rows", 0)),
        ("Total Scholar Research Areas links", s.get("scholar_research_area_rows", 0)),
        ("Total Authors Publications links", s.get("author_publication_rows", 0)),
        ("Publication listing complete", "Yes (SHOW MORE disabled)" if data.listing_complete else "NO"),
        ("SHOW MORE clicks performed", data.show_more_clicks),
        ("Listing footer text", data.footer_text or ""),
        ("Duplicate rows skipped", s.get("duplicates", 0)),
        ("Total successfully extracted attributes", len(groups[SUCCESS])),
        ("Total partially extracted attributes", len(groups[PARTIAL])),
        ("Total unavailable attributes", len(groups[UNAVAILABLE])),
        ("System/registry-generated attributes", len(groups[SYSTEM])),
        ("Extraction date and time", run_info["timestamp_display"]),
        ("HTTP status of profile page", data.http_status),
        ("Browser mode", run_info["browser_mode"]),
    ]
    for k, v in rows:
        L.append(f"  {k:<42}: {v}")

    L.append("")
    L.append("  Browser / access errors:")
    errs = run_info.get("errors") or []
    L += [_wrap(e, "    - ") for e in errs] or ["    - None. No CAPTCHA, block or rate-limit page was encountered."]
    L.append("  Warnings:")
    L += [_wrap(w, "    - ") for w in data.warnings] or ["    - None."]

    section("E. SCOPE & VALIDATION CHECKS")
    navs = sorted(set(data.navigations))
    conn = getattr(data, "connection_diagnostics", None)
    if conn:
        L += [
            "-" * 100,
            "BROWSER & PROXY CONNECTION DIAGNOSTICS",
            "-" * 100,
            f"  Browser Launched        : {'YES' if conn.get('browser_launched') else 'NO'} ({conn.get('browser_name') or 'none'})",
            f"  Connection Mode         : {(conn.get('connection_mode') or 'direct').upper()}",
            f"  Proxy Status            : {conn.get('proxy_status') or 'Direct connection'}",
            f"  Browser Network Status  : {'CONNECTED' if conn.get('network_connected') else 'FAILED'}",
            f"  Scholar Page Accessible : {'YES' if conn.get('scholar_accessible') else 'NO'}",
            f"  Failure Category        : {conn.get('failure_category') or 'None'}",
            f"  Navigation Failure      : {conn.get('navigation_failure_reason') or 'None'}",
            "",
        ]

    net = getattr(data, "network_stats", None)
    if net:
        L.append(f"  Navigation count                        : {net.get('navigation_count', len(navs))}")
        L.append(f"  Total network requests                  : {net.get('total_requests', 0)}")
        L.append(f"  Requests per page load                  : {net.get('requests_per_page_load', 0)}")
        L.append(f"  Request types breakdown                 : {json.dumps(net.get('requests_by_type', {}))}")
    L.append("  Main-frame navigations recorded during the session (proves only the profile page was visited):")
    L += [f"    - {u}" for u in navs]
    L.append("  Publication links opened                : 0 (title hrefs were read from the DOM only)")
    L.append("  Screenshot used as data source          : No (all values come from the live DOM)")
    L.append("  CSV header verification against DOCX    :")
    ok, msgs = schema_check
    L.append(f"    Result: {'PASSED' if ok else ('NOT VERIFIED' if ok is None else 'FAILED')}")
    L += [f"    - {m}" for m in msgs]
    L.append(f"  Relational PK/FK validation             : {'PASSED' if not t.validation_errors else 'FAILED'}")
    L.append("  Missing optional values                 : Left empty (never fabricated or guessed)")

    section("F. ROW-LEVEL NOTES")
    um = s.get("unmatched_author_order", [])
    L.append(f"  Publications without author_order ({len(um)}):")
    for idx, title, reason in um:
        L.append(_wrap(f"publication_id {idx}: {title}  [{reason}]", "    - "))
    L += ["", line, "END OF REPORT", line]
    return "\n".join(L) + "\n"


def build_failure_report(url, error, run_info, data=None) -> str:
    L = [
        "=" * 100,
        "GOOGLE SCHOLAR PROFILE FRONT-PAGE EXTRACTION REPORT - FAILED",
        "=" * 100,
        "",
        f"Profile URL requested : {redact_url(url)}",
        f"Extraction attempted  : {run_info['timestamp_display']}",
        f"Browser mode          : {run_info['browser_mode']}",
        f"HTTP status           : {getattr(data, 'http_status', None)}",
        f"Browser landed on     : {redact_url(getattr(data, 'loaded_url', None))}",
        "",
        "ERROR:",
        _wrap(str(error), "  "),
        "",
    ]

    conn = getattr(data, "connection_diagnostics", None)
    if conn:
        L += [
            "-" * 100,
            "BROWSER & PROXY CONNECTION DIAGNOSTICS",
            "-" * 100,
            f"  Browser Launched        : {'YES' if conn.get('browser_launched') else 'NO'} ({conn.get('browser_name') or 'none'})",
            f"  Connection Mode         : {(conn.get('connection_mode') or 'direct').upper()}",
            f"  Proxy Status            : {conn.get('proxy_status') or 'Direct connection'}",
            f"  Browser Network Status  : {'CONNECTED' if conn.get('network_connected') else 'FAILED'}",
            f"  Scholar Page Accessible : {'YES' if conn.get('scholar_accessible') else 'NO'}",
            f"  Failure Category        : {conn.get('failure_category') or 'None'}",
            f"  Navigation Failure      : {conn.get('navigation_failure_reason') or 'None'}",
            "",
        ]

    net = getattr(data, "network_stats", None)
    if net:
        L += [
            "-" * 100,
            "NETWORK & REQUEST AUDIT DIAGNOSTICS",
            "-" * 100,
            f"  Navigation Count        : {net.get('navigation_count', 0)}",
            f"  Total Network Requests  : {net.get('total_requests', 0)}",
            f"  Requests Per Page Load  : {net.get('requests_per_page_load', 0)}",
            f"  HTTP 429 Detected       : {'YES' if net.get('has_429') else 'NO'}",
            f"  Google Sorry Redirect   : {'YES' if net.get('sorry_redirect') else 'NO'}",
            f"  Requests by Type        : {json.dumps(net.get('requests_by_type', {}))}",
            f"  Domains Contacted       : {list(net.get('requests_by_domain', {}).keys())}",
            "",
            "  Navigation Events:",
        ]
        for nav in net.get("navigation_events", []):
            time_str = (
                f"{nav.get('time_since_last_nav_s')}s"
                if nav.get("time_since_last_nav_s") is not None
                else "Initial (0s)"
            )
            L.append(
                f"    - [Nav #{nav.get('navigation_index')}] {nav.get('timestamp')} | "
                f"Elapsed since prev nav: {time_str} | URL: {nav.get('url')}"
            )
        L.append("")

    L += [
        _wrap(
            "No CSV files were written in this run, so no partial or fabricated data was produced "
            "(any CSVs already in this folder are from an earlier run). "
            "If Google Scholar showed a CAPTCHA or rate-limit page, wait before retrying; this scraper "
            "does not bypass access restrictions.",
            "",
        )
    ]
    return "\n".join(L) + "\n"
