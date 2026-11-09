"""
Converts raw front-page data into database-ready, ERD-shaped rows with
persistent numeric primary keys, validated foreign keys, and deduplicated
entities and bridge tables across both in-memory runs and existing CSV files.

Rules:
  * Values are cleaned only by whitespace normalisation / thousands-separator
    removal; source meaning is never changed.
  * Internal primary keys (hei_id, scholar_id, publication_id, research_area_id)
    are assigned deterministically via IdRegistry using stable natural keys.
  * Existing CSV files are inspected using natural keys (HEIs: hei_name+country,
    SCHOLARS: google_scholar_id, PUBLICATIONS: title+publication_year,
    RESEARCH_AREAS: area_name) and composite bridge keys to prevent cross-run
    duplication while preserving shared records across multiple scholars.
  * Foreign keys across SCHOLARS, Scholar Research Areas, and Authors Publications
    always reference valid parent primary keys.
  * Missing optional values stay empty ("") - never fabricated or guessed.
"""

from collections import OrderedDict
from dataclasses import dataclass, field
import re
from typing import Optional, Sequence
from urllib.parse import parse_qs, urlparse

from .browser import ProfileData, canonicalize_detail_url
from .erd_schema import DETAIL_ENTITIES, ERD_ENTITIES
from .id_registry import (
    IdRegistry,
    hei_natural_key,
    normalize_pub_title,
    publication_natural_key,
    research_area_natural_key,
    scholar_natural_key,
)
from .reference_data import CanonicalHEI, DEFAULT_HEI_CATALOG, resolve_affiliation
from .validator import validate_dataset

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
DOMAIN_RE = re.compile(r"Verified email at\s+(\S+?)(?:\s+-\s+|\s*$)", re.I)
DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"<>,]+", re.I)
INT_RE = re.compile(r"^\d{1,3}(?:,\d{3})*$|^\d+$")
YEAR_RE = re.compile(r"^\d{4}$")
TRUNC_RE = re.compile(r"(\.\.\.|\u2026)\s*$")
HONORIFICS = {
    "prof", "professor", "dr", "engr", "mr", "mrs", "ms", "miss", "sir",
    "phd", "ph.d", "assoc", "asst", "associate", "assistant",
}

AREA_SOURCE = "Google Scholar profile"   # provenance value (ERD: "Google Scholar profile or inferred")
SOURCE_METHOD = "Profile"                # provenance value (ERD: "Profile, DOI or metadata matching")


def clean_int(value) -> str:
    """'2,367' -> '2367'; anything non-numeric -> '' (never guessed)."""
    if value is None:
        return ""
    v = str(value).strip()
    return v.replace(",", "") if INT_RE.match(v) else ""


def name_variants(full_name: str) -> set[str]:
    """
    Build the Google Scholar author-list form of the profile owner's name.
    'Prof. Dr. Umar Shahbaz Khan' -> {'us khan', 'u s khan', 'u khan', 'u shahbaz khan', 'umar shahbaz khan'}
    """
    tokens = [t for t in re.split(r"[\s,]+", full_name or "") if t]
    tokens = [t for t in tokens if t.strip(".").lower() not in HONORIFICS]
    if len(tokens) < 2:
        return {" ".join(tokens).lower()} if tokens else set()
    given, surname = tokens[:-1], tokens[-1]
    initials_compact = "".join(t[0] for t in given).lower()
    initials_spaced = " ".join(t[0] for t in given).lower()
    first_initial = given[0][0].lower()
    variants = {
        f"{initials_compact} {surname.lower()}",
        f"{initials_spaced} {surname.lower()}",
        f"{first_initial} {surname.lower()}",
        " ".join(tokens).lower(),
    }
    if len(given) >= 2:
        middle = " ".join(t.lower() for t in given[1:])
        variants.add(f"{first_initial} {middle} {surname.lower()}")
    return variants


def find_author_order(authors_text: str, variants: set[str]) -> tuple[Optional[int], str]:
    """Return (order|None, reason). Only an unambiguous exact match is accepted."""
    if not authors_text or not authors_text.strip():
        return None, "no author list displayed"
    truncated = TRUNC_RE.search(authors_text) is not None
    raw_parts = [a.strip() for a in authors_text.split(",") if a.strip()]
    names = []
    for p in raw_parts:
        cleaned = TRUNC_RE.sub("", p).strip(" .")
        if cleaned:
            names.append(cleaned)
    hits = [i for i, n in enumerate(names) if re.sub(r"\s+", " ", n.replace(".", "").strip()).lower() in variants]
    if len(hits) == 1:
        return hits[0] + 1, "matched"
    if len(hits) > 1:
        return None, "ambiguous: name form appears more than once"
    return None, (
        "not in displayed (truncated) author list"
        if truncated
        else "profile owner's name form not found in author list"
    )


def _ordered_row(entity: str, values: dict) -> OrderedDict:
    """Build a row dict with exact column names and order from ERD_ENTITIES or DETAIL_ENTITIES."""
    cols = ERD_ENTITIES.get(entity) or DETAIL_ENTITIES.get(entity)
    if not cols:
        raise KeyError(f"Unknown entity {entity!r}")
    return OrderedDict((c, "" if values.get(c) is None else str(values.get(c))) for c in cols)


def clean_scholar_from_tables(
    existing_tables: Optional[dict[str, list[dict]]],
    google_scholar_id: str,
    registry: Optional[IdRegistry] = None,
) -> tuple[dict[str, list[OrderedDict]], dict]:
    """
    Remove all previous CSV records and relationships exclusive to `google_scholar_id`
    while preserving:
      * All other scholars in `scholars.csv`
      * Any shared publications in `publications.csv` still referenced by other scholars
      * Any shared HEIs in `heis.csv` still referenced by other scholars
      * Any shared research areas in `research_areas.csv` still referenced by other scholars
      * All ID assignments in `registry` (id_registry.json is NEVER deleted or reset)

    Returns (cleaned_tables, cleanup_summary).
    """
    empty_tables = {ent: [] for ent in ERD_ENTITIES}
    target_gs_id = (google_scholar_id or "").strip()
    if not existing_tables or not any(existing_tables.values()):
        return empty_tables, {
            "target_google_scholar_id": target_gs_id,
            "target_scholar_ids": [],
            "had_previous_data": False,
            "other_scholars_present": False,
            "other_scholars_count": 0,
            "removed_counts": {ent: 0 for ent in ERD_ENTITIES},
            "preserved_counts": {ent: 0 for ent in ERD_ENTITIES},
        }

    if registry is not None:
        registry.sync_from_tables(existing_tables)

    scholars_in = existing_tables.get("SCHOLARS", [])
    heis_in = existing_tables.get("HEIs", [])
    pubs_in = existing_tables.get("PUBLICATIONS", [])
    areas_in = existing_tables.get("RESEARCH_AREAS", [])
    sras_in = existing_tables.get("Scholar Research Areas", [])
    aps_in = existing_tables.get("Authors Publications", [])

    target_nk = scholar_natural_key(target_gs_id) if target_gs_id else ""
    target_scholar_ids: set[str] = set()
    if registry is not None and target_nk:
        reg_sid = registry.get_id("SCHOLARS", target_nk)
        if reg_sid:
            target_scholar_ids.add(reg_sid)

    kept_scholars: list[OrderedDict] = []
    for r in scholars_in:
        row_gs_id = (r.get("google_scholar_id") or "").strip()
        row_sid = str(r.get("scholar_id") or "").strip()
        if (target_nk and row_gs_id and scholar_natural_key(row_gs_id) == target_nk) or (
            row_sid and row_sid in target_scholar_ids
        ):
            if row_sid:
                target_scholar_ids.add(row_sid)
        else:
            kept_scholars.append(_ordered_row("SCHOLARS", r))

    other_scholar_ids = {str(r["scholar_id"]).strip() for r in kept_scholars if str(r["scholar_id"]).strip()}

    # Filter bridge tables: keep only links belonging to other remaining scholars
    kept_aps: list[OrderedDict] = []
    for r in aps_in:
        sid = str(r.get("scholar_id") or "").strip()
        if sid not in target_scholar_ids and sid in other_scholar_ids:
            kept_aps.append(_ordered_row("Authors Publications", r))

    kept_sras: list[OrderedDict] = []
    for r in sras_in:
        sid = str(r.get("scholar_id") or "").strip()
        if sid not in target_scholar_ids and sid in other_scholar_ids:
            kept_sras.append(_ordered_row("Scholar Research Areas", r))

    # Reference sets from remaining scholars
    referenced_hei_ids = {str(r.get("hei_id") or "").strip() for r in kept_scholars}
    referenced_pub_ids = {str(r.get("publication_id") or "").strip() for r in kept_aps}
    referenced_area_ids = {str(r.get("research_area_id") or "").strip() for r in kept_sras}

    kept_heis = [
        _ordered_row("HEIs", r)
        for r in heis_in
        if str(r.get("hei_id") or "").strip() in referenced_hei_ids
    ]
    kept_pubs = [
        _ordered_row("PUBLICATIONS", r)
        for r in pubs_in
        if str(r.get("publication_id") or "").strip() in referenced_pub_ids
    ]
    kept_areas = [
        _ordered_row("RESEARCH_AREAS", r)
        for r in areas_in
        if str(r.get("research_area_id") or "").strip() in referenced_area_ids
    ]

    cleaned_tables = {
        "HEIs": kept_heis,
        "SCHOLARS": kept_scholars,
        "PUBLICATIONS": kept_pubs,
        "RESEARCH_AREAS": kept_areas,
        "Scholar Research Areas": kept_sras,
        "Authors Publications": kept_aps,
    }

    initial_counts = {
        "HEIs": len(heis_in),
        "SCHOLARS": len(scholars_in),
        "PUBLICATIONS": len(pubs_in),
        "RESEARCH_AREAS": len(areas_in),
        "Scholar Research Areas": len(sras_in),
        "Authors Publications": len(aps_in),
    }
    preserved_counts = {ent: len(cleaned_tables[ent]) for ent in ERD_ENTITIES}
    removed_counts = {ent: initial_counts[ent] - preserved_counts[ent] for ent in ERD_ENTITIES}

    summary = {
        "target_google_scholar_id": target_gs_id,
        "target_scholar_ids": sorted(target_scholar_ids),
        "had_previous_data": any(v > 0 for v in removed_counts.values()),
        "other_scholars_present": len(kept_scholars) > 0,
        "other_scholars_count": len(kept_scholars),
        "initial_counts": initial_counts,
        "removed_counts": removed_counts,
        "preserved_counts": preserved_counts,
    }
    return cleaned_tables, summary


@dataclass
class Transformed:
    timestamp: str
    tables: dict = field(default_factory=dict)          # entity -> list[OrderedDict]
    detail_tables: dict = field(default_factory=dict)   # detail entity -> list[OrderedDict]
    stats: dict = field(default_factory=dict)
    linkage: list = field(default_factory=list)         # supplementary, non-ERD
    row_notes: list = field(default_factory=list)
    unresolved_errors: list = field(default_factory=list)
    validation_errors: list = field(default_factory=list)
    registry: Optional[IdRegistry] = None


def transform_profiles(
    profiles: Sequence[ProfileData],
    timestamp: str,
    registry: Optional[IdRegistry] = None,
    existing_tables: Optional[dict[str, list[dict]]] = None,
    hei_catalog: Optional[tuple[CanonicalHEI, ...]] = None,
    area_taxonomy: Optional[dict[str, str]] = None,
    clean_target_scholars: bool = False,
) -> Transformed:
    """
    Transform one or more ProfileData objects into the 6 ERD entity tables,
    sharing a persistent IdRegistry and comparing against existing_tables
    using canonical natural keys and composite bridge keys.

    If `clean_target_scholars=True`, any previous CSV records exclusive to the
    scraped scholar(s) in `profiles` are first cleaned from `existing_tables`
    before rebuilding their records from the fresh scrape.
    """
    reg = registry if registry is not None else IdRegistry()
    catalog = hei_catalog if hei_catalog is not None else DEFAULT_HEI_CATALOG
    # ``area_taxonomy`` is retained only for API compatibility with older
    # callers.  Eligibility and transformation are deliberately independent
    # of it; unknown fields are stored with an empty classification.
    del area_taxonomy

    out = Transformed(timestamp=timestamp, registry=reg)
    s = out.stats

    # Synchronize registry with existing CSV tables before any cleanup or transformation
    if existing_tables:
        reg.sync_from_tables(existing_tables, default_timestamp=timestamp)

    cleanup_summaries = []
    working_existing = existing_tables
    if existing_tables and clean_target_scholars:
        for prof in profiles:
            gs_id = (prof.google_scholar_id or "").strip()
            if gs_id:
                working_existing, c_sum = clean_scholar_from_tables(
                    working_existing, gs_id, registry=reg
                )
                cleanup_summaries.append(c_sum)

    # Indexed stores keyed by primary key AND natural key to guarantee zero duplicates
    heis_by_id: OrderedDict[str, OrderedDict] = OrderedDict()
    heis_by_nk: dict[str, str] = {}

    scholars_by_id: OrderedDict[str, OrderedDict] = OrderedDict()
    scholars_by_nk: dict[str, str] = {}

    pubs_by_id: OrderedDict[str, OrderedDict] = OrderedDict()
    pubs_by_nk: dict[str, str] = {}
    pub_id_remap: dict[str, str] = {}

    areas_by_id: OrderedDict[str, OrderedDict] = OrderedDict()
    areas_by_nk: dict[str, str] = {}
    area_id_remap: dict[str, str] = {}

    sras_by_key: OrderedDict[tuple[str, str], OrderedDict] = OrderedDict()
    aps_by_key: OrderedDict[tuple[str, str], OrderedDict] = OrderedDict()

    counters = dict(
        missing_title=0,
        missing_year=0,
        missing_cites=0,
        missing_venue=0,
        missing_authors=0,
        trunc_authors=0,
        trunc_venue=0,
        doi_found=0,
        missing_link=0,
        duplicates=0,
        csv_duplicates_removed=0,
        duplicate_areas=0,
        duplicate_bridge_sra=0,
        duplicate_bridge_ap=0,
        shared_publications_merged=0,
        order_matched=0,
    )

    # Seed from working_existing using natural-key deduplication
    if working_existing:
        for r in working_existing.get("HEIs", []):
            hname = (r.get("hei_name") or "").strip()
            country = (r.get("country") or "").strip()
            if not hname:
                continue
            nk = hei_natural_key(hname, country)
            if nk in heis_by_nk:
                counters["csv_duplicates_removed"] += 1
                continue
            hid, created_at = reg.get_or_create(
                "HEIs", nk, timestamp=r.get("created_at") or timestamp, label=hname
            )
            row_copy = dict(r, hei_id=hid, created_at=r.get("created_at") or created_at)
            heis_by_id[hid] = _ordered_row("HEIs", row_copy)
            heis_by_nk[nk] = hid

        for r in working_existing.get("SCHOLARS", []):
            gs_id = (r.get("google_scholar_id") or "").strip()
            if not gs_id:
                continue
            nk = scholar_natural_key(gs_id)
            if nk in scholars_by_nk:
                counters["csv_duplicates_removed"] += 1
                continue
            sid, _ = reg.get_or_create(
                "SCHOLARS", nk, timestamp=r.get("last_synced_at") or timestamp, label=r.get("scholar_name") or gs_id
            )
            row_copy = dict(r, scholar_id=sid)
            scholars_by_id[sid] = _ordered_row("SCHOLARS", row_copy)
            scholars_by_nk[nk] = sid

        for r in working_existing.get("PUBLICATIONS", []):
            title = re.sub(r"\s+", " ", (r.get("title") or "").strip())
            year = (r.get("publication_year") or "").strip()
            old_pid = str(r.get("publication_id") or "").strip()
            if not title or not normalize_pub_title(title):
                continue
            nk = publication_natural_key(title, year)
            if nk in pubs_by_nk:
                canonical_pid = pubs_by_nk[nk]
                if old_pid:
                    pub_id_remap[old_pid] = canonical_pid
                counters["csv_duplicates_removed"] += 1
                counters["duplicates"] += 1
                continue
            pid, _ = reg.get_or_create(
                "PUBLICATIONS", nk, timestamp=r.get("last_synced_at") or timestamp, label=title
            )
            if old_pid:
                pub_id_remap[old_pid] = pid
            row_copy = dict(r, publication_id=pid, title=title, publication_year=year)
            pubs_by_id[pid] = _ordered_row("PUBLICATIONS", row_copy)
            pubs_by_nk[nk] = pid

        for r in working_existing.get("RESEARCH_AREAS", []):
            aname = re.sub(r"\s+", " ", (r.get("area_name") or "").strip())
            old_aid = str(r.get("research_area_id") or "").strip()
            if not aname:
                continue
            nk = research_area_natural_key(aname)
            if nk in areas_by_nk:
                canonical_aid = areas_by_nk[nk]
                if old_aid:
                    area_id_remap[old_aid] = canonical_aid
                counters["csv_duplicates_removed"] += 1
                counters["duplicate_areas"] += 1
                continue
            aid, created_at = reg.get_or_create(
                "RESEARCH_AREAS", nk, timestamp=r.get("created_at") or timestamp, label=aname
            )
            if old_aid:
                area_id_remap[old_aid] = aid
            row_copy = dict(r, research_area_id=aid, area_name=aname, created_at=r.get("created_at") or created_at)
            areas_by_id[aid] = _ordered_row("RESEARCH_AREAS", row_copy)
            areas_by_nk[nk] = aid

        for r in working_existing.get("Scholar Research Areas", []):
            sid = str(r.get("scholar_id") or "").strip()
            old_aid = str(r.get("research_area_id") or "").strip()
            aid = area_id_remap.get(old_aid, old_aid)
            if sid and aid:
                comp = (sid, aid)
                if comp in sras_by_key:
                    counters["duplicate_bridge_sra"] += 1
                    counters["csv_duplicates_removed"] += 1
                    continue
                row_copy = dict(r, scholar_id=sid, research_area_id=aid)
                sras_by_key[comp] = _ordered_row("Scholar Research Areas", row_copy)

        for r in working_existing.get("Authors Publications", []):
            sid = str(r.get("scholar_id") or "").strip()
            old_pid = str(r.get("publication_id") or "").strip()
            pid = pub_id_remap.get(old_pid, old_pid)
            if sid and pid:
                comp = (sid, pid)
                if comp in aps_by_key:
                    counters["duplicate_bridge_ap"] += 1
                    counters["csv_duplicates_removed"] += 1
                    continue
                row_copy = dict(r, scholar_id=sid, publication_id=pid)
                aps_by_key[comp] = _ordered_row("Authors Publications", row_copy)

    unmatched_author_order: list[tuple] = []
    truncated_venues_list: list[dict] = []
    truncated_authors_list: list[dict] = []
    unclassified_areas_list: list[dict] = []
    hei_mappings: list[dict] = []
    duplicate_publications_details: list[dict] = []
    missing_year_publications: list[dict] = []

    for data in profiles:
        h = data.header or {}
        m = data.metrics or {}
        gs_id = (data.google_scholar_id or "").strip()
        scholar_name = re.sub(r"\s+", " ", (h.get("scholar_name") or "").strip())
        raw_affiliation = re.sub(r"\s+", " ", (h.get("affiliation_text") or "").strip())

        # -------------------------- 1. HEIs -----------------------------------
        hei_info, hei_note = resolve_affiliation(raw_affiliation, catalog=catalog)
        if hei_info is not None:
            h_nk = hei_natural_key(hei_info["hei_name"], hei_info["country"])
            hei_id, hei_created_at = reg.get_or_create(
                "HEIs", h_nk, timestamp=timestamp, label=hei_info["hei_name"]
            )
            if h_nk not in heis_by_nk and hei_id not in heis_by_id:
                heis_by_id[hei_id] = _ordered_row(
                    "HEIs",
                    {
                        "hei_id": hei_id,
                        "hei_name": hei_info["hei_name"],
                        "short_name": hei_info["short_name"],
                        "city": hei_info["city"],
                        "country": hei_info["country"],
                        "website_url": hei_info["website_url"],
                        "target_university": hei_info["target_university"],
                        "created_at": hei_created_at,
                    },
                )
                heis_by_nk[h_nk] = hei_id
            hei_mappings.append(
                {
                    "google_scholar_id": gs_id,
                    "scholar_name": scholar_name,
                    "raw_affiliation": raw_affiliation,
                    "hei_id": hei_id,
                    "hei_name": hei_info["hei_name"],
                    "city": heis_by_id[hei_id]["city"],
                    "country": hei_info["country"],
                    "status": "RESOLVED",
                    "note": hei_note,
                }
            )
        else:
            hei_id = ""
            hei_mappings.append(
                {
                    "google_scholar_id": gs_id,
                    "scholar_name": scholar_name,
                    "raw_affiliation": raw_affiliation,
                    "hei_id": "",
                    "hei_name": "",
                    "city": "",
                    "country": "",
                    "status": "NOT_DISPLAYED",
                    "note": hei_note,
                }
            )

        # ------------------------ 2. SCHOLARS ---------------------------------
        header_text = h.get("header_text") or ""
        email_match = EMAIL_RE.search(header_text)
        dom = DOMAIN_RE.search(h.get("verification_text") or "")
        email_full = email_match.group(0) if email_match else ""
        email_domain = dom.group(1) if dom else ""
        s["email_full"] = email_full
        s["email_domain"] = email_domain

        def metric(label):
            row = m.get(label) or {}
            return clean_int(row.get("All")) if row else ""

        h_index = metric("h-index")
        total_citations = metric("Citations")
        s["h_index"] = h_index
        s["total_citations"] = total_citations
        s["listing_complete"] = data.listing_complete

        if gs_id:
            s_nk = scholar_natural_key(gs_id)
            scholar_id, _ = reg.get_or_create(
                "SCHOLARS", s_nk, timestamp=timestamp, label=scholar_name
            )
            scholars_by_nk[s_nk] = scholar_id
        else:
            scholar_id = ""
            out.unresolved_errors.append("Scholar profile is missing required google_scholar_id.")

        # ------------ 3. PUBLICATIONS & Authors Publications ------------------
        variants = name_variants(scholar_name)
        s["name_variants"] = sorted(variants)
        pubs_raw = data.publications or []
        seen_pub_for_scholar: dict[str, int] = {}
        scholar_pub_count = 0

        for idx_raw, r in enumerate(pubs_raw, 1):
            title = re.sub(r"\s+", " ", (r.get("title") or "").strip())
            link = (r.get("source_url") or "").strip()
            citation_key = (
                parse_qs(urlparse(link).query).get("citation_for_view", [None])[0] if link else None
            )
            year = (r.get("year") or "").strip()
            year = year if YEAR_RE.match(year) else ""

            if not title or not normalize_pub_title(title):
                counters["missing_title"] += 1
                out.unresolved_errors.append(
                    f"Scholar {gs_id!r} has a publication row (#{idx_raw}) with an empty or non-identifying title."
                )
                continue

            pub_nk = publication_natural_key(title, year)
            if pub_nk in seen_pub_for_scholar:
                counters["duplicates"] += 1
                duplicate_publications_details.append(
                    {
                        "row_index": r.get("row_index", idx_raw),
                        "first_row_index": seen_pub_for_scholar[pub_nk],
                        "title": title,
                        "publication_year": year,
                        "natural_key": pub_nk,
                    }
                )
                continue
            seen_pub_for_scholar[pub_nk] = r.get("row_index", idx_raw)
            scholar_pub_count += 1

            pub_id, _ = reg.get_or_create(
                "PUBLICATIONS", pub_nk, timestamp=timestamp, label=title
            )

            cites = clean_int(r.get("cited_by"))
            authors = re.sub(r"\s+", " ", (r.get("authors") or "").strip())
            venue = re.sub(r"\s+", " ", (r.get("venue") or "").strip())
            visible = " ".join([title, authors, venue])
            doi_m = DOI_RE.search(visible)
            doi = doi_m.group(0).rstrip(".") if doi_m else ""

            if not year:
                missing_year_publications.append(
                    {"publication_id": pub_id, "title": title, "google_scholar_id": gs_id}
                )

            if pub_nk not in pubs_by_nk and pub_id not in pubs_by_id:
                counters["missing_year"] += not year
                counters["missing_cites"] += not cites
                counters["missing_venue"] += not venue
                counters["missing_authors"] += not authors
                counters["missing_link"] += not link
                counters["doi_found"] += bool(doi)
                if authors and TRUNC_RE.search(authors):
                    counters["trunc_authors"] += 1
                    truncated_authors_list.append(
                        {
                            "publication_id": pub_id,
                            "title": title,
                            "authors_text": authors,
                        }
                    )
                if venue and TRUNC_RE.search(venue):
                    counters["trunc_venue"] += 1
                    truncated_venues_list.append(
                        {
                            "publication_id": pub_id,
                            "title": title,
                            "venue": venue,
                        }
                    )

                pubs_by_id[pub_id] = _ordered_row(
                    "PUBLICATIONS",
                    {
                        "publication_id": pub_id,
                        "title": title,
                        "publication_year": year,
                        "citation_count": cites,
                        "venue": venue,
                        "authors_text": authors,
                        "digital_object_identifier": doi,
                        "source_url": link,
                        "last_synced_at": timestamp,
                    },
                )
                pubs_by_nk[pub_nk] = pub_id
            else:
                pub_id = pubs_by_nk.get(pub_nk, pub_id)
                counters["shared_publications_merged"] += 1

            order, reason = find_author_order(authors, variants)
            if order:
                counters["order_matched"] += 1
            else:
                unmatched_author_order.append((int(pub_id), title, reason))

            ap_comp = (scholar_id, pub_id)
            if ap_comp not in aps_by_key:
                aps_by_key[ap_comp] = _ordered_row(
                    "Authors Publications",
                    {
                        "scholar_id": scholar_id,
                        "publication_id": pub_id,
                        "author_order": str(order) if order else "",
                        "source_method": SOURCE_METHOD,
                        "recorded_at": timestamp,
                    },
                )
            else:
                counters["duplicate_bridge_ap"] += 1

            out.linkage.append(
                {
                    "entity": "PUBLICATIONS / Authors Publications",
                    "csv_row_number": len(pubs_by_id),
                    "google_scholar_id": gs_id,
                    "source_key": citation_key or f"publication_id:{pub_id}",
                    "label": title,
                }
            )

        total_pubs_str = str(scholar_pub_count) if data.listing_complete else ""
        s["total_publications"] = total_pubs_str

        scholars_by_id[scholar_id or f"_unresolved_{ len(scholars_by_id) + 1 }"] = _ordered_row(
            "SCHOLARS",
            {
                "scholar_id": scholar_id,
                "hei_id": hei_id,
                "google_scholar_id": gs_id,
                "scholar_name": scholar_name,
                "email": email_full,
                "google_scholar_profile_url": data.loaded_url or data.requested_url,
                "h_index": h_index,
                "total_publications": total_pubs_str,
                "total_citations": total_citations,
                "last_synced_at": timestamp,
            },
        )

        # -------- 4. RESEARCH_AREAS & Scholar Research Areas ------------------
        seen_area_for_scholar: set[str] = set()
        for raw_area in h.get("interests") or []:
            area_name = re.sub(r"\s+", " ", (raw_area or "").strip())
            if not area_name:
                continue
            area_nk = research_area_natural_key(area_name)
            if area_nk in seen_area_for_scholar:
                counters["duplicate_areas"] += 1
                continue
            seen_area_for_scholar.add(area_nk)

            existing_aid = areas_by_nk.get(area_nk)
            existing_row = areas_by_id.get(existing_aid) if existing_aid else None
            existing_val = str((existing_row or {}).get("is_computing_area") or "").strip()
            is_comp_val = existing_val if existing_val in ("0", "1") else ""

            area_id, area_created_at = reg.get_or_create(
                "RESEARCH_AREAS", area_nk, timestamp=timestamp, label=area_name
            )
            if not is_comp_val:
                unclassified_areas_list.append(
                    {
                        "research_area_id": area_id,
                        "area_name": area_name,
                        "google_scholar_id": gs_id,
                        "reason": "No explicit classification is available; value preserved as empty.",
                    }
                )

            if area_nk not in areas_by_nk and area_id not in areas_by_id:
                areas_by_id[area_id] = _ordered_row(
                    "RESEARCH_AREAS",
                    {
                        "research_area_id": area_id,
                        "area_name": area_name,
                        "description": "",
                        "is_computing_area": is_comp_val,
                        "keywords": "",
                        "created_at": area_created_at,
                    },
                )
                areas_by_nk[area_nk] = area_id
            else:
                area_id = areas_by_nk.get(area_nk, area_id)

            sra_comp = (scholar_id, area_id)
            if sra_comp not in sras_by_key:
                sras_by_key[sra_comp] = _ordered_row(
                    "Scholar Research Areas",
                    {
                        "scholar_id": scholar_id,
                        "research_area_id": area_id,
                        "is_primary_area": "0",
                        "area_source": AREA_SOURCE,
                        "updated_at": timestamp,
                    },
                )
            else:
                counters["duplicate_bridge_sra"] += 1

            out.linkage.append(
                {
                    "entity": "RESEARCH_AREAS / Scholar Research Areas",
                    "csv_row_number": len(areas_by_id),
                    "google_scholar_id": gs_id,
                    "source_key": area_name,
                    "label": area_name,
                }
            )

    out.tables["HEIs"] = list(heis_by_id.values())
    out.tables["SCHOLARS"] = list(scholars_by_id.values())
    out.tables["PUBLICATIONS"] = list(pubs_by_id.values())
    out.tables["RESEARCH_AREAS"] = list(areas_by_id.values())
    out.tables["Scholar Research Areas"] = list(sras_by_key.values())
    out.tables["Authors Publications"] = list(aps_by_key.values())

    s.update(counters)
    s["hei_rows"] = len(out.tables["HEIs"])
    s["scholar_rows"] = len(out.tables["SCHOLARS"])
    s["publication_rows"] = len(out.tables["PUBLICATIONS"])
    s["research_areas"] = len(out.tables["RESEARCH_AREAS"])
    s["scholar_research_area_rows"] = len(out.tables["Scholar Research Areas"])
    s["author_publication_rows"] = len(out.tables["Authors Publications"])
    s["unmatched_author_order"] = unmatched_author_order
    s["truncated_venues_list"] = truncated_venues_list
    s["truncated_authors_list"] = truncated_authors_list
    s["unclassified_areas_list"] = unclassified_areas_list
    s["hei_mappings"] = hei_mappings
    s["duplicate_publications_details"] = duplicate_publications_details
    s["missing_year_publications"] = missing_year_publications
    s["cleanup_summaries"] = cleanup_summaries

    out.validation_errors = validate_dataset(
        out.tables, unresolved_errors=out.unresolved_errors, erd_entities=ERD_ENTITIES
    )
    s["validation_errors"] = out.validation_errors
    return out


def apply_publication_details(
    out: Transformed,
    detail_results: Optional[dict],
    existing_detail_tables: Optional[dict[str, list[dict]]] = None,
) -> Transformed:
    """
    Merge Stage-2 publication detail metadata into:
      1. Existing `PUBLICATIONS` records in `out.tables["PUBLICATIONS"]` and
         `Authors Publications` (`author_order` re-checked when full author list
         replaces a truncated front-page author list) — WITHOUT changing any
         `publication_id` or `scholar_id` or creating duplicate PUBLICATIONS rows.
      2. Structured detail tables in `out.detail_tables`:
         - `PUBLICATION_DETAILS` (`output/supplementary/publication_details.csv`)
         - `PUBLICATION_YEARLY_CITATIONS` (`output/supplementary/publication_yearly_citations.csv`)
    """
    pubs_by_id = OrderedDict((str(r["publication_id"]), r) for r in out.tables.get("PUBLICATIONS", []))
    scholars_by_id = OrderedDict((str(r["scholar_id"]), r) for r in out.tables.get("SCHOLARS", []))
    aps_by_comp = OrderedDict(
        ((str(r["scholar_id"]), str(r["publication_id"])), r)
        for r in out.tables.get("Authors Publications", [])
    )

    # Map publication_id -> primary scholar_id(s) from Authors Publications
    pub_to_scholars: dict[str, list[str]] = {}
    for (sid, pid) in aps_by_comp:
        pub_to_scholars.setdefault(pid, []).append(sid)

    details_by_pid: OrderedDict[str, OrderedDict] = OrderedDict()
    yearly_by_key: OrderedDict[tuple[str, str], OrderedDict] = OrderedDict()

    # Seed from any existing detail CSVs for publications that still exist in PUBLICATIONS
    if existing_detail_tables:
        for r in existing_detail_tables.get("PUBLICATION_DETAILS", []):
            pid = str(r.get("publication_id") or "").strip()
            if pid and pid in pubs_by_id:
                details_by_pid[pid] = _ordered_row("PUBLICATION_DETAILS", r)
        for r in existing_detail_tables.get("PUBLICATION_YEARLY_CITATIONS", []):
            pid = str(r.get("publication_id") or "").strip()
            yr = str(r.get("citation_year") or "").strip()
            if pid and yr and pid in pubs_by_id:
                yearly_by_key[(pid, yr)] = _ordered_row("PUBLICATION_YEARLY_CITATIONS", r)

    pub_records = (detail_results or {}).get("publications") or {}
    summary = (detail_results or {}).get("summary") or {}

    for pid, rec in pub_records.items():
        pid = str(pid).strip()
        if pid not in pubs_by_id or not isinstance(rec, dict):
            continue
        if rec.get("status") != "SUCCESS" or not isinstance(rec.get("metadata"), dict):
            continue

        meta = rec["metadata"]
        pub_row = pubs_by_id[pid]
        sync_ts = rec.get("extracted_at") or meta.get("last_synced_at") or out.timestamp

        # 1. Enrich PUBLICATIONS in place (preserving publication_id, title natural key, and existing non-empty fields)
        full_title = re.sub(r"\s+", " ", (meta.get("title") or "").strip())
        if full_title and TRUNC_RE.search(pub_row["title"]):
            pub_row["title"] = full_title

        full_authors = re.sub(r"\s+", " ", (meta.get("authors_text") or "").strip())
        if full_authors:
            pub_row["authors_text"] = full_authors

        detail_year = (meta.get("publication_year") or "").strip()
        if not pub_row["publication_year"] and detail_year and YEAR_RE.match(detail_year):
            pub_row["publication_year"] = detail_year

        detail_cites = clean_int(meta.get("citation_count"))
        if detail_cites:
            pub_row["citation_count"] = detail_cites

        journal = re.sub(r"\s+", " ", (meta.get("journal") or "").strip())
        volume = re.sub(r"\s+", " ", (meta.get("volume") or "").strip())
        issue = re.sub(r"\s+", " ", (meta.get("issue") or "").strip())
        pages = re.sub(r"\s+", " ", (meta.get("pages") or "").strip())
        publisher = re.sub(r"\s+", " ", (meta.get("publisher") or "").strip())
        publisher_url = (meta.get("publisher_url") or meta.get("external_url") or "").strip()

        if journal:
            venue_parts = [journal]
            if volume:
                vol_str = f"{volume} ({issue})" if issue else volume
                venue_parts.append(vol_str)
            if pages:
                venue_parts.append(f", {pages}" if len(venue_parts) > 1 else pages)
            reconstructed_venue = " ".join(venue_parts).replace(" ,", ",")
            if not pub_row["venue"] or TRUNC_RE.search(pub_row["venue"]):
                pub_row["venue"] = reconstructed_venue
        elif publisher and not pub_row["venue"]:
            pub_row["venue"] = publisher

        doi = (meta.get("doi") or "").strip()
        if doi and not pub_row["digital_object_identifier"]:
            pub_row["digital_object_identifier"] = doi

        pub_url = (rec.get("publication_url") or meta.get("publication_url") or pub_row["source_url"] or "").strip()
        if pub_url:
            pub_row["source_url"] = canonicalize_detail_url(pub_url) or pub_url
        pub_row["last_synced_at"] = sync_ts

        # 2. If author_order was previously empty due to front-page truncation, re-evaluate using complete authors_text
        if full_authors:
            for sid in pub_to_scholars.get(pid, []):
                ap_row = aps_by_comp.get((sid, pid))
                sch_row = scholars_by_id.get(sid)
                if ap_row and sch_row and not ap_row.get("author_order"):
                    variants = name_variants(sch_row.get("scholar_name") or "")
                    new_order, _ = find_author_order(full_authors, variants)
                    if new_order:
                        ap_row["author_order"] = str(new_order)

        # 3. Populate PUBLICATION_DETAILS row
        linked_sids = pub_to_scholars.get(pid, [])
        sid_for_detail = str(rec.get("scholar_id") or "").strip()
        if not sid_for_detail or sid_for_detail not in scholars_by_id:
            sid_for_detail = linked_sids[0] if linked_sids else ""

        details_by_pid[pid] = _ordered_row(
            "PUBLICATION_DETAILS",
            {
                "publication_id": pid,
                "scholar_id": sid_for_detail,
                "title": full_title or pub_row["title"],
                "authors_text": full_authors or pub_row["authors_text"],
                "publication_date": (meta.get("publication_date") or "").strip(),
                "publication_year": detail_year or pub_row["publication_year"],
                "journal": journal,
                "volume": volume,
                "issue": issue,
                "pages": pages,
                "publisher": publisher,
                "description": re.sub(r"\s+", " ", (meta.get("description") or "").strip()),
                "citation_count": detail_cites or pub_row["citation_count"],
                "publication_url": canonicalize_detail_url(pub_url) or pub_url,
                "publisher_url": publisher_url,
                "doi": doi or pub_row["digital_object_identifier"],
                "last_synced_at": sync_ts,
            },
        )

        # 4. Populate PUBLICATION_YEARLY_CITATIONS rows
        # Remove any stale yearly rows for this publication_id before inserting fresh ones
        for k in [k for k in yearly_by_key if k[0] == pid]:
            del yearly_by_key[k]
        for yc in meta.get("yearly_citations") or []:
            c_yr = str(yc.get("citation_year") or "").strip()
            c_cnt = clean_int(yc.get("citation_count"))
            if c_yr and YEAR_RE.match(c_yr) and c_cnt != "":
                yearly_by_key[(pid, c_yr)] = _ordered_row(
                    "PUBLICATION_YEARLY_CITATIONS",
                    {
                        "publication_id": pid,
                        "citation_year": c_yr,
                        "citation_count": c_cnt,
                        "last_synced_at": sync_ts,
                    },
                )

    out.detail_tables["PUBLICATION_DETAILS"] = list(details_by_pid.values())
    out.detail_tables["PUBLICATION_YEARLY_CITATIONS"] = list(yearly_by_key.values())

    # Compute Stage-2 completeness & quality stats
    detail_rows = out.detail_tables["PUBLICATION_DETAILS"]
    yearly_rows = out.detail_tables["PUBLICATION_YEARLY_CITATIONS"]
    pids_with_yearly = {r["publication_id"] for r in yearly_rows}

    total_target_pubs = summary.get("total_unique_publications") or len(pubs_by_id)
    s = out.stats
    s["detail_summary"] = {
        "total_unique_publications": total_target_pubs,
        "manual_mode": summary.get("manual_mode", False),
        "browser_visible": summary.get("browser_visible", False),
        "persistent_profile": summary.get("persistent_profile", False),
        "connection_mode": summary.get("connection_mode", "direct"),
        "attempted_this_run": summary.get("attempted_this_run", 0),
        "succeeded_total": len(detail_rows),
        "succeeded_this_run": summary.get("succeeded_this_run", 0),
        "skipped_checkpoint": summary.get("skipped_checkpoint", 0),
        "skipped_duplicate_url": summary.get("skipped_duplicate_url", 0),
        "failed_this_run": summary.get("failed_this_run", 0),
        "remaining_incomplete": summary.get("remaining_incomplete", max(0, total_target_pubs - len(detail_rows))),
        "stopped_by_block": summary.get("stopped_by_block", False),
        "block_reason": summary.get("block_reason"),
        "block_kind": summary.get("block_kind"),
        "manual_verification_outcome": summary.get("manual_verification_outcome"),
        "duplicate_detail_urls": summary.get("duplicate_detail_urls", []),
        "yearly_citation_rows": len(yearly_rows),
        "publications_with_yearly_citations": len(pids_with_yearly),
        "completeness": {
            "title": sum(1 for r in detail_rows if r.get("title")),
            "authors_text": sum(1 for r in detail_rows if r.get("authors_text")),
            "publication_date": sum(1 for r in detail_rows if r.get("publication_date")),
            "publication_year": sum(1 for r in detail_rows if r.get("publication_year")),
            "journal": sum(1 for r in detail_rows if r.get("journal")),
            "volume": sum(1 for r in detail_rows if r.get("volume")),
            "issue": sum(1 for r in detail_rows if r.get("issue")),
            "pages": sum(1 for r in detail_rows if r.get("pages")),
            "publisher": sum(1 for r in detail_rows if r.get("publisher")),
            "description": sum(1 for r in detail_rows if r.get("description")),
            "citation_count": sum(1 for r in detail_rows if r.get("citation_count")),
            "doi": sum(1 for r in detail_rows if r.get("doi")),
            "publication_url": sum(1 for r in detail_rows if r.get("publication_url")),
            "publisher_url": sum(1 for r in detail_rows if r.get("publisher_url")),
            "yearly_citations": len(pids_with_yearly),
        },
    }

    # Re-calculate PUBLICATIONS stats (doi_found, trunc_authors, trunc_venue, order_matched)
    s["doi_found"] = sum(1 for r in out.tables["PUBLICATIONS"] if r.get("digital_object_identifier"))
    s["trunc_authors"] = sum(
        1 for r in out.tables["PUBLICATIONS"] if r.get("authors_text") and TRUNC_RE.search(r["authors_text"])
    )
    s["trunc_venue"] = sum(
        1 for r in out.tables["PUBLICATIONS"] if r.get("venue") and TRUNC_RE.search(r["venue"])
    )
    s["order_matched"] = sum(
        1 for r in out.tables["Authors Publications"] if r.get("author_order")
    )

    out.validation_errors = validate_dataset(
        out.tables,
        unresolved_errors=out.unresolved_errors,
        erd_entities=ERD_ENTITIES,
        detail_tables=out.detail_tables,
    )
    s["validation_errors"] = out.validation_errors
    return out


def transform(
    data: ProfileData,
    timestamp: str,
    registry: Optional[IdRegistry] = None,
    existing_tables: Optional[dict[str, list[dict]]] = None,
    hei_catalog: Optional[tuple[CanonicalHEI, ...]] = None,
    area_taxonomy: Optional[dict[str, str]] = None,
    clean_target_scholars: bool = False,
) -> Transformed:
    """Transform a single ProfileData into the 6 ERD entity tables."""
    return transform_profiles(
        [data],
        timestamp=timestamp,
        registry=registry,
        existing_tables=existing_tables,
        hei_catalog=hei_catalog,
        area_taxonomy=area_taxonomy,
        clean_target_scholars=clean_target_scholars,
    )
