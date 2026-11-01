"""
CSV writing and extraction_report.txt generation.
"""

import csv
import json
import logging
from pathlib import Path

from .browser import redact_url
from .erd_schema import CSV_FILES, ERD_ENTITIES

log = logging.getLogger(__name__)

SUCCESS, PARTIAL, UNAVAILABLE, SYSTEM = "SUCCESS", "PARTIAL", "UNAVAILABLE", "SYSTEM"
DB_ID = "Database-generated internal ID"


def write_csv(path: Path, columns, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, quoting=csv.QUOTE_MINIMAL, extrasaction="raise")
        w.writeheader()
        for r in rows:
            w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in columns})


def write_all_csvs(out_dir: Path, tables: dict):
    for entity, fname in CSV_FILES.items():
        write_csv(out_dir / fname, ERD_ENTITIES[entity], tables.get(entity, []))
        log.info("Wrote %-28s (%d rows)", fname, len(tables.get(entity, [])))


def write_supplementary(out_dir: Path, linkage, raw: dict):
    sup = out_dir / "supplementary"
    sup.mkdir(parents=True, exist_ok=True)
    write_csv(sup / "record_linkage.csv",
              ["entity", "csv_row_number", "google_scholar_id", "source_key", "label"], linkage)
    with open(sup / "raw_profile_snapshot.json", "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)


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

    # ---------------- HEIs (no CSV; assessed for completeness) ----------------
    aff = h.get("affiliation_text") or "(none displayed)"
    E = "HEIs"
    add(E, "hei_id", UNAVAILABLE, "Internal primary key of our HEIs table; Google Scholar has no such value.", DB_ID)
    add(E, "hei_name", UNAVAILABLE,
        f"The front page shows only a free-text affiliation line written by the researcher: \"{aff}\". "
        "It mixes job title and institution, is not a canonical university name, and the verified email "
        f"domain ({s.get('email_domain') or 'n/a'}) may point to a different institution. Mapping it to an "
        "HEI would be a guess. Raw text is preserved in supplementary/raw_profile_snapshot.json.",
        "Manual/curated mapping to our HEIs master list (additional processing)")
    add(E, "short_name", UNAVAILABLE, "No institution abbreviation is displayed on the profile front page.",
        "Another data source (HEIs master list)")
    add(E, "city", UNAVAILABLE, "Institution city is not a structured field on the profile front page.",
        "Another data source (HEIs master list)")
    add(E, "country", UNAVAILABLE, "Institution country is not displayed on the profile front page.",
        "Another data source (HEIs master list)")
    add(E, "website_url", UNAVAILABLE,
        "The profile shows no institution website. The optional 'Homepage' link is the researcher's personal "
        f"page ({h.get('homepage_url') or 'not shown'}), not the HEI's official site.",
        "Another data source (HEIs master list)")
    add(E, "target_university", UNAVAILABLE,
        "Application-defined flag (selected top-25 list); not a Google Scholar attribute.",
        "Application/business rule in our database")
    add(E, "created_at", UNAVAILABLE, "Database record-creation timestamp; set when the HEI row is inserted.",
        "Database-generated value")

    # ------------------------------- SCHOLARS ---------------------------------
    E = "SCHOLARS"
    add(E, "scholar_id", UNAVAILABLE, "Internal primary key of our SCHOLARS table; not provided by Google Scholar.", DB_ID)
    add(E, "hei_id", UNAVAILABLE,
        "Foreign key to HEIs.hei_id. Requires the HEI to exist in our database and the affiliation to be "
        "mapped to it (see HEIs.hei_name).", "Internal ID after HEI mapping")
    by_presence(E, "google_scholar_id", bool(data.google_scholar_id),
                "Taken from the 'user' parameter of the live profile URL (page loaded successfully for this id).",
                "Could not be parsed from the URL.")
    by_presence(E, "scholar_name", bool(h.get("scholar_name")),
                "Read exactly as displayed in the profile header (#gsc_prf_in), including honorifics.",
                "Profile name element not found on the page.")
    if s.get("email_full"):
        add(E, "email", SUCCESS, "A full email address is publicly displayed on the profile header.")
    elif s.get("email_domain"):
        add(E, "email", PARTIAL,
            f"Google Scholar displays only the verified domain (\"Verified email at {s['email_domain']}\"), "
            "never the full mailbox address. A domain is not an email, so the CSV cell is left EMPTY rather "
            "than guessed.", "Another data source (university directory / researcher's homepage)")
    else:
        add(E, "email", UNAVAILABLE, "The researcher has not publicly displayed an email or verified domain.",
            "Another data source")
    by_presence(E, "google_scholar_profile_url", bool(data.loaded_url),
                "URL of the live profile page actually loaded by the browser.", "Page did not load.")
    by_presence(E, "h_index", bool(s.get("h_index")),
                "'All' column of the h-index row in the 'Cited by' metrics table.",
                "Metrics table not displayed on this profile.", "Not publicly displayed")
    by_presence(E, "total_citations", bool(s.get("total_citations")),
                "'All' column of the Citations row in the 'Cited by' metrics table.",
                "Metrics table not displayed on this profile.", "Not publicly displayed")
    if s.get("total_publications"):
        add(E, "total_publications", SUCCESS,
            f"Count of all {n} publication rows after clicking SHOW MORE until Google Scholar disabled the "
            f"button (footer: '{data.footer_text}'). Matches the ERD meaning 'count of publications in the "
            "dataset'. Note: Google Scholar itself may merge/omit some works; this is the profile's own listing.")
    else:
        add(E, "total_publications", UNAVAILABLE,
            "The complete publication listing could not be confirmed (SHOW MORE never reached its disabled "
            "state), so the number of loaded rows is NOT reported as the total.", "Re-run when access allows")

    # ---------------------------- RESEARCH_AREAS ------------------------------
    E = "RESEARCH_AREAS"
    na = s.get("research_areas", 0)
    add(E, "research_area_id", UNAVAILABLE, "Internal primary key of RESEARCH_AREAS.", DB_ID)
    by_presence(E, "area_name", na > 0,
                f"{na} research-interest tag(s) read exactly as displayed (no computing filter applied).",
                "The researcher lists no research interests on the profile.", "Not publicly displayed")
    add(E, "description", UNAVAILABLE,
        "Google Scholar interest tags are bare labels; no definition/scope text exists on the page.",
        "Additional processing / curated taxonomy")
    add(E, "is_computing_area", UNAVAILABLE,
        "Google Scholar does not classify interests by discipline and no validated classification rule is "
        "configured, so no 1/0 value is assumed.", "Additional processing (approved classification rule or manual review)")
    add(E, "keywords", UNAVAILABLE, "No synonyms or matching terms are shown for interest tags.",
        "Additional processing / curated taxonomy")
    add(E, "created_at", UNAVAILABLE,
        "Database insert time of the canonical area row. The area may already exist from another scholar, so "
        "the scraper cannot know it; left for the database (e.g. DEFAULT SYSTIMESTAMP).", "Database-generated value")

    # ----------------------------- PUBLICATIONS -------------------------------
    E = "PUBLICATIONS"
    add(E, "publication_id", UNAVAILABLE, "Internal primary key of PUBLICATIONS.", DB_ID)

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
            f"Venue line extracted as displayed (hidden ', YEAR' suffix removed). {tv} of {n} venues are "
            f"truncated by Google Scholar with '...'; {mv} rows show no venue. Full text exists only on the "
            "individual publication page, which is outside scope.", "Individual publication page (out of scope)")
    elif n:
        add(E, "venue", SUCCESS, "Venue line extracted as displayed (hidden ', YEAR' suffix removed).")

    ta, ma = s.get("trunc_authors", 0), s.get("missing_authors", 0)
    if n and (ta or ma):
        add(E, "authors_text", PARTIAL,
            f"Author line extracted as displayed. {ta} of {n} author lists end with '...' (Google Scholar "
            f"truncates long lists on the profile page); {ma} rows show none. The ERD wants the COMPLETE list, "
            "which only the publication detail page provides (out of scope).", "Individual publication page (out of scope)")
    elif n:
        add(E, "authors_text", SUCCESS, "Complete author line displayed for every row.")

    dn = s.get("doi_found", 0)
    if dn and dn == n:
        add(E, "digital_object_identifier", SUCCESS, "DOI visible for every row.")
    elif dn:
        add(E, "digital_object_identifier", PARTIAL, f"DOI explicitly visible in {dn} of {n} rows only.",
            "Individual publication page or external source (e.g. Crossref)")
    else:
        add(E, "digital_object_identifier", UNAVAILABLE,
            "The profile publication table never displays DOIs (checked every row's title/author/venue text).",
            "Individual publication page (out of scope) or external DOI service such as Crossref")
    col("source_url", "missing_link",
        "Absolute URL of each title hyperlink (Google Scholar citation-detail page). The link was read, not opened; "
        "publisher URLs are only on the detail page.", "title hyperlink")

    # ------------------------- Scholar Research Areas -------------------------
    E = "Scholar Research Areas"
    add(E, "scholar_id", UNAVAILABLE, "Foreign key to SCHOLARS.scholar_id; assigned by our database.", "Internal ID")
    add(E, "research_area_id", UNAVAILABLE, "Foreign key to RESEARCH_AREAS.research_area_id; assigned by our database.",
        "Internal ID")
    add(E, "is_primary_area", UNAVAILABLE,
        "Google Scholar does not mark any interest as primary; tag order is not an explicit designation. "
        "Left empty instead of assuming 1 or 0.", "Application rule or manual curation")

    # ------------------------- Authors Publications ---------------------------
    E = "Authors Publications"
    add(E, "scholar_id", UNAVAILABLE, "Foreign key to SCHOLARS.scholar_id; assigned by our database.", "Internal ID")
    add(E, "publication_id", UNAVAILABLE, "Foreign key to PUBLICATIONS.publication_id; assigned by our database.",
        "Internal ID")
    om = s.get("order_matched", 0)
    variants = ", ".join(f"'{v}'" for v in s.get("name_variants", []))
    if n and om == n:
        add(E, "author_order", SUCCESS, f"Derived for all rows by exact match of {variants} in the displayed author list.")
    elif om:
        add(E, "author_order", PARTIAL,
            f"Derived (additional processing) by unambiguous exact match of the profile owner's name form(s) {variants} "
            f"in the displayed author list: {om} of {n} rows matched. Unmatched rows are left empty because the list "
            "is truncated, the name is spelled differently, or the match is ambiguous.",
            "Complete author list from the publication page (out of scope) or manual verification")
    else:
        add(E, "author_order", UNAVAILABLE, "The profile owner's name could not be matched in any author list.",
            "Manual verification")

    # ------------------------- System-generated values ------------------------
    for ent, attr, why in [
        ("SCHOLARS", "last_synced_at", "Extraction timestamp generated by the scraper (time of this refresh)."),
        ("PUBLICATIONS", "last_synced_at", "Extraction timestamp generated by the scraper."),
        ("Scholar Research Areas", "area_source",
         f"Provenance constant '{'Google Scholar profile'}': every association came from the profile's interest tags."),
        ("Scholar Research Areas", "updated_at", "Extraction timestamp (time this association was refreshed)."),
        ("Authors Publications", "source_method",
         "Provenance constant 'Profile': the paper is listed on the scholar's own profile."),
        ("Authors Publications", "recorded_at", "Extraction timestamp (time this link was recorded)."),
    ]:
        add(ent, attr, SYSTEM, why, "Generated by scraper (not scraped profile content)")
    return A


# --------------------------------------------------------------------------- #
def _wrap(text, indent, width=100):
    import textwrap
    return textwrap.fill(text, width=width, initial_indent=indent, subsequent_indent=indent)


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

    section("A2. SYSTEM-GENERATED METADATA (filled by the scraper, NOT scraped profile content)")
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
    L.append("")
    L.append(_wrap("Note: HEIs has no CSV in this export because none of its columns can be populated "
                   "reliably from a profile front page; it is assessed above for completeness.", "  "))

    section("D. EXTRACTION SUMMARY")
    rows = [
        ("Researcher name", h.get("scholar_name") or "(not found)"),
        ("Google Scholar profile URL", data.loaded_url or data.requested_url),
        ("Google Scholar ID", data.google_scholar_id or ""),
        ("Displayed affiliation (raw)", h.get("affiliation_text") or "(none)"),
        ("Verification line (raw)", h.get("verification_text") or "(none)"),
        ("Total research areas extracted", s.get("research_areas", 0)),
        ("Total publication records extracted", s.get("publication_rows", 0)),
        ("Publication listing complete", "Yes (SHOW MORE disabled)" if data.listing_complete else "NO"),
        ("SHOW MORE clicks performed", data.show_more_clicks),
        ("Listing footer text", data.footer_text or ""),
        ("Duplicate rows skipped", s.get("duplicates", 0)),
        ("Total successfully extracted attributes", len(groups[SUCCESS])),
        ("Total partially extracted attributes", len(groups[PARTIAL])),
        ("Total unavailable attributes", len(groups[UNAVAILABLE])),
        ("System-generated attributes", len(groups[SYSTEM])),
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
    L.append("  Internal IDs fabricated                 : None (all *_id columns are empty)")
    L.append("  Missing values                          : Left empty (never 0 / NULL / guessed)")

    section("F. ROW-LEVEL NOTES")
    L.append(_wrap(
        "Row N of authors_publications.csv refers to row N of publications.csv, and row N of "
        "scholar_research_areas.csv refers to row N of research_areas.csv (internal IDs are not yet assigned). "
        "supplementary/record_linkage.csv carries Google Scholar's own article key for each row to support "
        "later database mapping.", "  "))
    um = s.get("unmatched_author_order", [])
    L.append(f"  Publications without author_order ({len(um)}):")
    for idx, title, reason in um:
        L.append(_wrap(f"row {idx}: {title}  [{reason}]", "    - "))
    L += ["", line, "END OF REPORT", line]
    return "\n".join(L) + "\n"


def build_failure_report(url, error, run_info, data=None) -> str:
    L = ["=" * 100, "GOOGLE SCHOLAR PROFILE FRONT-PAGE EXTRACTION REPORT - FAILED", "=" * 100, "",
         f"Profile URL requested : {redact_url(url)}",
         f"Extraction attempted  : {run_info['timestamp_display']}",
         f"Browser mode          : {run_info['browser_mode']}",
         f"HTTP status           : {getattr(data, 'http_status', None)}",
         f"Browser landed on     : {redact_url(getattr(data, 'loaded_url', None))}", "",
         "ERROR:", _wrap(str(error), "  "), ""]

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
            time_str = f"{nav.get('time_since_last_nav_s')}s" if nav.get('time_since_last_nav_s') is not None else "Initial (0s)"
            L.append(f"    - [Nav #{nav.get('navigation_index')}] {nav.get('timestamp')} | Elapsed since prev nav: {time_str} | URL: {nav.get('url')}")
        L.append("")

    L += [_wrap("No CSV files were written in this run, so no partial or fabricated data was produced "
                "(any CSVs already in this folder are from an earlier run). "
                "If Google Scholar showed a CAPTCHA or rate-limit page, wait before retrying; this scraper "
                "does not bypass access restrictions.", "")]
    return "\n".join(L) + "\n"
