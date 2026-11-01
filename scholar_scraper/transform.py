"""
Converts raw front-page data into ERD-shaped rows.

Rules:
  * Values are cleaned only by whitespace normalisation / thousands-separator
    removal; source meaning is never changed.
  * Internal IDs (scholar_id, hei_id, publication_id, research_area_id) stay
    empty - they are assigned by the database, not by Google Scholar.
  * Missing values stay empty ("") - never 0, NULL or a guess.
"""

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

from .browser import ProfileData

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
DOMAIN_RE = re.compile(r"Verified email at\s+(\S+?)(?:\s+-\s+|\s*$)", re.I)
DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"<>,]+", re.I)
INT_RE = re.compile(r"^\d{1,3}(?:,\d{3})*$|^\d+$")
YEAR_RE = re.compile(r"^\d{4}$")
TRUNC_RE = re.compile(r"(\.\.\.|\u2026)\s*$")
HONORIFICS = {"prof", "professor", "dr", "engr", "mr", "mrs", "ms", "miss", "sir",
              "phd", "ph.d", "assoc", "asst", "associate", "assistant"}

AREA_SOURCE = "Google Scholar profile"   # provenance value (ERD: "Google Scholar profile or inferred")
SOURCE_METHOD = "Profile"                # provenance value (ERD: "Profile, DOI or metadata matching")


def clean_int(value):
    """'2,367' -> '2367'; anything non-numeric -> '' (never guessed)."""
    if value is None:
        return ""
    v = value.strip()
    return v.replace(",", "") if INT_RE.match(v) else ""


def name_variants(full_name: str) -> set:
    """
    Build the Google Scholar author-list form of the profile owner's name.
    'Prof. Dr. Umar Shahbaz Khan' -> {'us khan', 'umar shahbaz khan'}
    """
    tokens = [t for t in re.split(r"[\s,]+", full_name or "") if t]
    tokens = [t for t in tokens if t.strip(".").lower() not in HONORIFICS]
    if len(tokens) < 2:
        return {" ".join(tokens).lower()} if tokens else set()
    given, surname = tokens[:-1], tokens[-1]
    initials = "".join(t[0] for t in given).upper()
    return {f"{initials} {surname}".lower(), " ".join(tokens).lower()}


def find_author_order(authors_text: str, variants: set):
    """Return (order|None, reason). Only an unambiguous exact match is accepted."""
    if not authors_text:
        return None, "no author list displayed"
    parts = [a.strip() for a in authors_text.split(",") if a.strip()]
    truncated = bool(parts) and TRUNC_RE.search(parts[-1]) is not None and parts[-1].strip(". \u2026") == ""
    names = [p for p in parts if p.strip(". \u2026")]
    hits = [i for i, n in enumerate(names) if n.lower() in variants]
    if len(hits) == 1:
        return hits[0] + 1, "matched"
    if len(hits) > 1:
        return None, "ambiguous: name form appears more than once"
    return None, ("not in displayed (truncated) author list" if truncated
                  else "profile owner's name form not found in author list")


@dataclass
class Transformed:
    timestamp: str
    tables: dict = field(default_factory=dict)        # entity -> list[dict]
    stats: dict = field(default_factory=dict)
    linkage: list = field(default_factory=list)       # supplementary, non-ERD
    row_notes: list = field(default_factory=list)


def transform(data: ProfileData, timestamp: str) -> Transformed:
    out = Transformed(timestamp=timestamp)
    h = data.header or {}
    m = data.metrics or {}
    s = out.stats

    # ------------------------------ SCHOLARS ----------------------------------
    header_text = h.get("header_text") or ""
    email_match = EMAIL_RE.search(header_text)
    dom = DOMAIN_RE.search(h.get("verification_text") or "")
    s["email_full"] = email_match.group(0) if email_match else ""
    s["email_domain"] = dom.group(1) if dom else ""

    def metric(label):
        row = m.get(label) or {}
        return clean_int(row.get("All")) if row else ""

    pubs_raw = data.publications or []
    s["h_index"] = metric("h-index")
    s["total_citations"] = metric("Citations")
    s["listing_complete"] = data.listing_complete

    # ---------------------------- PUBLICATIONS --------------------------------
    variants = name_variants(h.get("scholar_name"))
    s["name_variants"] = sorted(variants)
    seen, pubs, aps = set(), [], []
    counters = dict(missing_title=0, missing_year=0, missing_cites=0, missing_venue=0,
                    missing_authors=0, trunc_authors=0, trunc_venue=0, doi_found=0,
                    missing_link=0, duplicates=0, order_matched=0)
    unmatched = []
    for r in pubs_raw:
        title = r.get("title") or ""
        link = r.get("source_url") or ""
        key = parse_qs(urlparse(link).query).get("citation_for_view", [None])[0] if link else None
        dedup_key = key or (title.lower(), r.get("year") or "")
        if dedup_key in seen:
            counters["duplicates"] += 1
            continue
        seen.add(dedup_key)

        year = (r.get("year") or "").strip()
        year = year if YEAR_RE.match(year) else ""
        cites = clean_int(r.get("cited_by"))
        authors = r.get("authors") or ""
        venue = r.get("venue") or ""
        visible = " ".join([title, authors, venue])
        doi_m = DOI_RE.search(visible)
        doi = doi_m.group(0).rstrip(".") if doi_m else ""

        counters["missing_title"] += not title
        counters["missing_year"] += not year
        counters["missing_cites"] += not cites
        counters["missing_venue"] += not venue
        counters["missing_authors"] += not authors
        counters["missing_link"] += not link
        counters["doi_found"] += bool(doi)
        if authors and TRUNC_RE.search(authors):
            counters["trunc_authors"] += 1
        if venue and TRUNC_RE.search(venue):
            counters["trunc_venue"] += 1

        pubs.append({
            "publication_id": "",
            "title": title,
            "publication_year": year,
            "citation_count": cites,
            "venue": venue,
            "authors_text": authors,
            "digital_object_identifier": doi,
            "source_url": link,
            "last_synced_at": timestamp,
        })

        order, reason = find_author_order(authors, variants)
        if order:
            counters["order_matched"] += 1
        else:
            unmatched.append((len(pubs), title, reason))
        aps.append({
            "scholar_id": "",
            "publication_id": "",
            "author_order": str(order) if order else "",
            "source_method": SOURCE_METHOD,
            "recorded_at": timestamp,
        })
        out.linkage.append({
            "entity": "PUBLICATIONS / Authors Publications",
            "csv_row_number": len(pubs),
            "google_scholar_id": data.google_scholar_id,
            "source_key": key or "",
            "label": title,
        })
    s.update(counters)
    s["publication_rows"] = len(pubs)
    s["unmatched_author_order"] = unmatched
    s["total_publications"] = str(len(pubs)) if data.listing_complete and pubs else ""

    out.tables["SCHOLARS"] = [{
        "scholar_id": "",
        "hei_id": "",
        "google_scholar_id": data.google_scholar_id or "",
        "scholar_name": h.get("scholar_name") or "",
        "email": s["email_full"],
        "google_scholar_profile_url": data.loaded_url or data.requested_url,
        "h_index": s["h_index"],
        "total_publications": s["total_publications"],
        "total_citations": s["total_citations"],
        "last_synced_at": timestamp,
    }]

    # -------------------- RESEARCH_AREAS + Scholar Research Areas -------------
    areas, sras, seen_a = [], [], set()
    for name in h.get("interests") or []:
        k = name.casefold()
        if k in seen_a:
            continue
        seen_a.add(k)
        areas.append({
            "research_area_id": "",
            "area_name": name,
            "description": "",
            "is_computing_area": "",
            "keywords": "",
            "created_at": "",
        })
        sras.append({
            "scholar_id": "",
            "research_area_id": "",
            "is_primary_area": "",
            "area_source": AREA_SOURCE,
            "updated_at": timestamp,
        })
        out.linkage.append({
            "entity": "RESEARCH_AREAS / Scholar Research Areas",
            "csv_row_number": len(areas),
            "google_scholar_id": data.google_scholar_id,
            "source_key": name,
            "label": name,
        })
    s["research_areas"] = len(areas)

    out.tables["RESEARCH_AREAS"] = areas
    out.tables["PUBLICATIONS"] = pubs
    out.tables["Scholar Research Areas"] = sras
    out.tables["Authors Publications"] = aps
    return out
