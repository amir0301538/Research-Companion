"""Conservative front-page refresh merging and content-only change detection."""
from copy import deepcopy
from urllib.parse import parse_qs, urlparse

from .id_registry import publication_natural_key, research_area_natural_key
from .transform import clean_int
from .validator import DatasetValidationError, validate_dataset

TIMESTAMPS = {"last_synced_at", "updated_at", "recorded_at", "created_at"}


def citation_key(url):
    return (parse_qs(urlparse(url or "").query).get("citation_for_view") or [""])[0]


def filter_ambiguous(data, existing, scholar_id):
    """Keep uncertain changed title/year identities out of automatic inserts."""
    profile = deepcopy(data)
    owned = {r["publication_id"] for r in existing["Authors Publications"]
             if r["scholar_id"] == scholar_id}
    old_by_citation = {citation_key(r["source_url"]): r for r in existing["PUBLICATIONS"]
                       if r["publication_id"] in owned and citation_key(r["source_url"])}
    kept, reviews = [], []
    for row in data.publications:
        key = citation_key(row.get("source_url"))
        old = old_by_citation.get(key)
        if old and row.get("title") and publication_natural_key(old["title"], old["publication_year"]) != publication_natural_key(
                row.get("title") or "", row.get("year") or ""):
            reviews.append({"citation_for_view": key, "existing_publication_id": old["publication_id"],
                            "old_title": old["title"], "new_title": row.get("title") or "",
                            "reason": "Title or year changed for the same citation URL; identity needs review."})
        else:
            kept.append(row)
    profile.publications = kept
    return profile, reviews


def merge_refresh(original, transformed, data, scholar_id, timestamp):
    tables = transformed.tables
    old_scholar = next(r for r in original["SCHOLARS"] if r["scholar_id"] == scholar_id)
    new_scholar = next(r for r in tables["SCHOLARS"] if r["scholar_id"] == scholar_id)
    for field in ("scholar_name", "hei_id", "email", "h_index", "total_citations"):
        if not new_scholar.get(field):
            new_scholar[field] = old_scholar.get(field, "")
    old_pubs = {r["publication_id"]: r for r in original["PUBLICATIONS"]}
    new_pubs = {r["publication_id"]: r for r in tables["PUBLICATIONS"]}
    old_by_nk = {publication_natural_key(r["title"], r["publication_year"]): r
                 for r in original["PUBLICATIONS"]}
    citation_changes = []
    updated_pubs = set()
    for raw in data.publications:
        title = (raw.get("title") or "").strip()
        if not title:
            continue
        nk = publication_natural_key(title, raw.get("year") or "")
        previous = old_by_nk.get(nk)
        if not previous:
            continue
        target = new_pubs[previous["publication_id"]]
        for field, value in (("citation_count", clean_int(raw.get("cited_by"))),
                             ("authors_text", (raw.get("authors") or "").strip()),
                             ("venue", (raw.get("venue") or "").strip())):
            if value and target[field] != value:
                if field == "citation_count":
                    citation_changes.append({"publication_id": target["publication_id"],
                                             "old": target[field], "new": value})
                target[field] = value
                updated_pubs.add(target["publication_id"])
        if not target.get("source_url") and raw.get("source_url"):
            target["source_url"] = raw["source_url"]
            updated_pubs.add(target["publication_id"])
        if target["publication_id"] in updated_pubs:
            target["last_synced_at"] = timestamp
    visible_areas = {research_area_natural_key(name) for name in data.header.get("interests") or [] if name.strip()}
    area_by_id = {r["research_area_id"]: r for r in tables["RESEARCH_AREAS"]}
    old_area_links = {r["research_area_id"] for r in original["Scholar Research Areas"]
                      if r["scholar_id"] == scholar_id}
    tables["Scholar Research Areas"] = [
        r for r in tables["Scholar Research Areas"]
        if r["scholar_id"] != scholar_id or
           research_area_natural_key(area_by_id[r["research_area_id"]]["area_name"]) in visible_areas
    ]
    new_area_links = {r["research_area_id"] for r in tables["Scholar Research Areas"]
                      if r["scholar_id"] == scholar_id}
    old_links = {r["publication_id"] for r in original["Authors Publications"] if r["scholar_id"] == scholar_id}
    visible_nks = {publication_natural_key(r["title"], r.get("year") or "") for r in data.publications if r.get("title")}
    visible_ids = {r["publication_id"] for r in tables["PUBLICATIONS"]
                   if publication_natural_key(r["title"], r["publication_year"]) in visible_nks}
    changes = {
        "scholar_fields": {k: {"old": old_scholar.get(k, ""), "new": new_scholar.get(k, "")}
                           for k in ("scholar_name", "hei_id", "email", "h_index", "total_citations",
                                     "google_scholar_profile_url", "total_publications")
                           if old_scholar.get(k, "") != new_scholar.get(k, "")},
        "new_publications": len(set(new_pubs) - set(old_pubs)),
        "updated_publications": len(updated_pubs),
        "citation_count_changes": citation_changes,
        "new_research_areas": len({r["research_area_id"] for r in tables["RESEARCH_AREAS"]} -
                                  {r["research_area_id"] for r in original["RESEARCH_AREAS"]}),
        "research_area_links_added": len(new_area_links - old_area_links),
        "research_area_links_removed": len(old_area_links - new_area_links),
        "publications_missing_from_profile": sorted(old_links - visible_ids, key=int),
        "updated_affiliation": old_scholar.get("hei_id") != new_scholar.get("hei_id"),
    }
    # Old publication bridges stay in place; a single disappearance is review only.
    errors = validate_dataset(tables, unresolved_errors=transformed.unresolved_errors)
    if errors:
        raise DatasetValidationError(errors)
    return changes
