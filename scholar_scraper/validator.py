"""
Pre-export relational integrity, primary/foreign key, and schema validator.

Validates all six database entity tables before any CSV files are written:
  * Exact column names and order matching ERD_ENTITIES (from DOCX).
  * No unexpected extra columns.
  * All primary keys and populated foreign keys are positive integers fitting NUMBER(19).
  * Uniqueness of single-column primary keys, composite primary keys, and natural keys.
  * Referential integrity for all 5 foreign key relationships across the 6 tables.
  * Source-populated attributes, nullable unknown classifications, Boolean flags,
    and valid timestamps.  Empty ``SCHOLARS.hei_id``, ``HEIs.country``, and
    ``RESEARCH_AREAS.is_computing_area`` are accepted for source-faithful CSV
    capture; the proposed Oracle schema marks these columns required and needs
    a migration/unknown-state policy before importing such rows.
"""

import re
from datetime import datetime
from typing import Optional

from .erd_schema import DETAIL_ENTITIES, ERD_ENTITIES
from .id_registry import (
    MAX_NUMBER_19,
    hei_natural_key,
    publication_natural_key,
    research_area_natural_key,
    scholar_natural_key,
)

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}$")


class DatasetValidationError(Exception):
    """Raised when pre-export dataset validation fails."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        summary = f"Dataset validation failed with {len(self.errors)} error(s):\n" + "\n".join(
            f"  - {e}" for e in self.errors
        )
        super().__init__(summary)


def _is_valid_positive_int(val) -> bool:
    if val is None or isinstance(val, bool):
        return False
    s = str(val).strip()
    if not s or not s.isdigit():
        return False
    n = int(s)
    return 1 <= n <= MAX_NUMBER_19


def _is_valid_timestamp(val) -> bool:
    if not val or not isinstance(val, str):
        return False
    s = val.strip()
    if not TIMESTAMP_RE.match(s):
        return False
    try:
        datetime.strptime(s.replace("T", " "), "%Y-%m-%d %H:%M:%S")
        return True
    except ValueError:
        return False


def validate_dataset(
    tables: dict[str, list[dict]],
    unresolved_errors: Optional[list[str]] = None,
    erd_entities: dict[str, list[str]] = ERD_ENTITIES,
    detail_tables: Optional[dict[str, list[dict]]] = None,
) -> list[str]:
    """
    Validate all 6 entity tables and return a list of validation error strings.
    If the returned list is empty, the dataset is 100% valid for export.
    """
    errors: list[str] = []
    if unresolved_errors:
        errors.extend(unresolved_errors)

    # 1. Verify all 6 entities exist and have exact column headers / no unexpected columns
    for entity, expected_cols in erd_entities.items():
        if entity not in tables:
            errors.append(f"[{entity}] Table is missing from dataset.")
            continue
        rows = tables[entity]
        for idx, row in enumerate(rows, 1):
            actual_cols = list(row.keys())
            if actual_cols != expected_cols:
                extra = [c for c in actual_cols if c not in expected_cols]
                missing = [c for c in expected_cols if c not in actual_cols]
                errors.append(
                    f"[{entity} row #{idx}] Column mismatch: expected exact order {expected_cols}, "
                    f"got {actual_cols} (extra={extra}, missing={missing})."
                )

    heis = tables.get("HEIs", [])
    scholars = tables.get("SCHOLARS", [])
    pubs = tables.get("PUBLICATIONS", [])
    areas = tables.get("RESEARCH_AREAS", [])
    sras = tables.get("Scholar Research Areas", [])
    aps = tables.get("Authors Publications", [])

    # 2. Validate HEIs
    hei_ids: set[str] = set()
    hei_nat_keys: set[str] = set()
    for idx, r in enumerate(heis, 1):
        hid = str(r.get("hei_id") or "").strip()
        if not _is_valid_positive_int(hid):
            errors.append(f"[HEIs row #{idx}] Invalid or empty primary key hei_id={hid!r}.")
        elif hid in hei_ids:
            errors.append(f"[HEIs row #{idx}] Duplicate primary key hei_id={hid}.")
        else:
            hei_ids.add(hid)

        hname = (r.get("hei_name") or "").strip()
        country = (r.get("country") or "").strip()
        if not hname:
            errors.append(f"[HEIs row #{idx}] Required field hei_name is empty.")
        if hname:
            nk = hei_natural_key(hname, country)
            if nk in hei_nat_keys:
                errors.append(f"[HEIs row #{idx}] Duplicate canonical HEI ({hname!r}, {country!r}).")
            hei_nat_keys.add(nk)

        tu = str(r.get("target_university") or "").strip()
        if tu not in ("0", "1"):
            errors.append(
                f"[HEIs row #{idx}] Required Boolean target_university must be '0' or '1', got {tu!r}."
            )
        ca = (r.get("created_at") or "").strip()
        if not _is_valid_timestamp(ca):
            errors.append(f"[HEIs row #{idx}] Required timestamp created_at is invalid or empty: {ca!r}.")

    # 3. Validate SCHOLARS
    scholar_ids: set[str] = set()
    scholar_gs_ids: set[str] = set()
    for idx, r in enumerate(scholars, 1):
        sid = str(r.get("scholar_id") or "").strip()
        if not _is_valid_positive_int(sid):
            errors.append(f"[SCHOLARS row #{idx}] Invalid or empty primary key scholar_id={sid!r}.")
        elif sid in scholar_ids:
            errors.append(f"[SCHOLARS row #{idx}] Duplicate primary key scholar_id={sid}.")
        else:
            scholar_ids.add(sid)

        fk_hei = str(r.get("hei_id") or "").strip()
        if fk_hei and not _is_valid_positive_int(fk_hei):
            errors.append(f"[SCHOLARS row #{idx}] Invalid foreign key hei_id={fk_hei!r}.")
        elif fk_hei and fk_hei not in hei_ids:
            errors.append(
                f"[SCHOLARS row #{idx}] Foreign key violation: hei_id={fk_hei} does not exist in heis.csv."
            )

        gs_id = (r.get("google_scholar_id") or "").strip()
        if not gs_id:
            errors.append(f"[SCHOLARS row #{idx}] Required field google_scholar_id is empty.")
        else:
            nk = scholar_natural_key(gs_id)
            if nk in scholar_gs_ids:
                errors.append(f"[SCHOLARS row #{idx}] Duplicate google_scholar_id={gs_id!r}.")
            scholar_gs_ids.add(nk)

        if not (r.get("scholar_name") or "").strip():
            errors.append(f"[SCHOLARS row #{idx}] Required field scholar_name is empty.")
        if not (r.get("google_scholar_profile_url") or "").strip():
            errors.append(f"[SCHOLARS row #{idx}] Required field google_scholar_profile_url is empty.")

        for num_col in ("h_index", "total_publications", "total_citations"):
            val = str(r.get(num_col) or "").strip()
            if not val.isdigit():
                errors.append(
                    f"[SCHOLARS row #{idx}] Required numeric field {num_col} must be a non-negative integer, got {val!r}."
                )

    # 4. Validate PUBLICATIONS
    pub_ids: set[str] = set()
    pub_nat_keys: set[str] = set()
    for idx, r in enumerate(pubs, 1):
        pid = str(r.get("publication_id") or "").strip()
        if not _is_valid_positive_int(pid):
            errors.append(f"[PUBLICATIONS row #{idx}] Invalid or empty primary key publication_id={pid!r}.")
        elif pid in pub_ids:
            errors.append(f"[PUBLICATIONS row #{idx}] Duplicate primary key publication_id={pid}.")
        else:
            pub_ids.add(pid)

        title = (r.get("title") or "").strip()
        year = str(r.get("publication_year") or "").strip()
        if not title:
            errors.append(f"[PUBLICATIONS row #{idx}] Required field title is empty.")
        else:
            nk = publication_natural_key(title, year)
            if nk in pub_nat_keys:
                errors.append(
                    f"[PUBLICATIONS row #{idx}] Duplicate publication for normalized (title, year): ({title!r}, {year!r})."
                )
            pub_nat_keys.add(nk)

        if year and (not year.isdigit() or len(year) != 4):
            errors.append(f"[PUBLICATIONS row #{idx}] Invalid publication_year={year!r}.")
        cites = str(r.get("citation_count") or "").strip()
        if cites and not cites.isdigit():
            errors.append(f"[PUBLICATIONS row #{idx}] Invalid citation_count={cites!r}.")

    # 5. Validate RESEARCH_AREAS
    area_ids: set[str] = set()
    area_nat_keys: set[str] = set()
    for idx, r in enumerate(areas, 1):
        aid = str(r.get("research_area_id") or "").strip()
        if not _is_valid_positive_int(aid):
            errors.append(f"[RESEARCH_AREAS row #{idx}] Invalid or empty primary key research_area_id={aid!r}.")
        elif aid in area_ids:
            errors.append(f"[RESEARCH_AREAS row #{idx}] Duplicate primary key research_area_id={aid}.")
        else:
            area_ids.add(aid)

        aname = (r.get("area_name") or "").strip()
        if not aname:
            errors.append(f"[RESEARCH_AREAS row #{idx}] Required field area_name is empty.")
        else:
            nk = research_area_natural_key(aname)
            if nk in area_nat_keys:
                errors.append(f"[RESEARCH_AREAS row #{idx}] Duplicate research area {aname!r}.")
            area_nat_keys.add(nk)

        ica = str(r.get("is_computing_area") or "").strip()
        if ica not in ("", "0", "1"):
            errors.append(
                f"[RESEARCH_AREAS row #{idx}] is_computing_area must be empty, '0', or '1', got {ica!r}."
            )
        ca = (r.get("created_at") or "").strip()
        if not _is_valid_timestamp(ca):
            errors.append(f"[RESEARCH_AREAS row #{idx}] Required timestamp created_at is invalid or empty: {ca!r}.")

    # 6. Validate Scholar Research Areas (Bridge Table)
    sra_keys: set[tuple[str, str]] = set()
    for idx, r in enumerate(sras, 1):
        sid = str(r.get("scholar_id") or "").strip()
        aid = str(r.get("research_area_id") or "").strip()
        if not _is_valid_positive_int(sid):
            errors.append(f"[Scholar Research Areas row #{idx}] Invalid or empty FK scholar_id={sid!r}.")
        elif sid not in scholar_ids:
            errors.append(
                f"[Scholar Research Areas row #{idx}] Foreign key violation: scholar_id={sid} does not exist in scholars.csv."
            )

        if not _is_valid_positive_int(aid):
            errors.append(f"[Scholar Research Areas row #{idx}] Invalid or empty FK research_area_id={aid!r}.")
        elif aid not in area_ids:
            errors.append(
                f"[Scholar Research Areas row #{idx}] Foreign key violation: research_area_id={aid} does not exist in research_areas.csv."
            )

        if _is_valid_positive_int(sid) and _is_valid_positive_int(aid):
            comp = (sid, aid)
            if comp in sra_keys:
                errors.append(
                    f"[Scholar Research Areas row #{idx}] Duplicate composite primary key (scholar_id={sid}, research_area_id={aid})."
                )
            sra_keys.add(comp)

        ipa = str(r.get("is_primary_area") or "").strip()
        if ipa not in ("0", "1"):
            errors.append(
                f"[Scholar Research Areas row #{idx}] Required Boolean is_primary_area must be '0' or '1', got {ipa!r}."
            )
        ua = (r.get("updated_at") or "").strip()
        if not _is_valid_timestamp(ua):
            errors.append(
                f"[Scholar Research Areas row #{idx}] Required timestamp updated_at is invalid or empty: {ua!r}."
            )

    # 7. Validate Authors Publications (Bridge Table)
    ap_keys: set[tuple[str, str]] = set()
    for idx, r in enumerate(aps, 1):
        sid = str(r.get("scholar_id") or "").strip()
        pid = str(r.get("publication_id") or "").strip()
        if not _is_valid_positive_int(sid):
            errors.append(f"[Authors Publications row #{idx}] Invalid or empty FK scholar_id={sid!r}.")
        elif sid not in scholar_ids:
            errors.append(
                f"[Authors Publications row #{idx}] Foreign key violation: scholar_id={sid} does not exist in scholars.csv."
            )

        if not _is_valid_positive_int(pid):
            errors.append(f"[Authors Publications row #{idx}] Invalid or empty FK publication_id={pid!r}.")
        elif pid not in pub_ids:
            errors.append(
                f"[Authors Publications row #{idx}] Foreign key violation: publication_id={pid} does not exist in publications.csv."
            )

        if _is_valid_positive_int(sid) and _is_valid_positive_int(pid):
            comp = (sid, pid)
            if comp in ap_keys:
                errors.append(
                    f"[Authors Publications row #{idx}] Duplicate composite primary key (scholar_id={sid}, publication_id={pid})."
                )
            ap_keys.add(comp)

        ao = str(r.get("author_order") or "").strip()
        if ao and not _is_valid_positive_int(ao):
            errors.append(
                f"[Authors Publications row #{idx}] Optional author_order must be a positive integer when present, got {ao!r}."
            )
        ra = (r.get("recorded_at") or "").strip()
        if not _is_valid_timestamp(ra):
            errors.append(
                f"[Authors Publications row #{idx}] Required timestamp recorded_at is invalid or empty: {ra!r}."
            )

    # 8. Optional Stage-2 Detail Tables Validation (PUBLICATION_DETAILS & PUBLICATION_YEARLY_CITATIONS)
    if detail_tables:
        for d_entity, expected_d_cols in DETAIL_ENTITIES.items():
            if d_entity not in detail_tables:
                continue
            for idx, row in enumerate(detail_tables[d_entity], 1):
                actual_cols = list(row.keys())
                if actual_cols != expected_d_cols:
                    errors.append(
                        f"[{d_entity} row #{idx}] Column mismatch: expected {expected_d_cols}, got {actual_cols}."
                    )

        seen_detail_pids: set[str] = set()
        for idx, r in enumerate(detail_tables.get("PUBLICATION_DETAILS", []), 1):
            pid = str(r.get("publication_id") or "").strip()
            sid = str(r.get("scholar_id") or "").strip()
            if not _is_valid_positive_int(pid):
                errors.append(f"[PUBLICATION_DETAILS row #{idx}] Invalid publication_id={pid!r}.")
            elif pid not in pub_ids:
                errors.append(
                    f"[PUBLICATION_DETAILS row #{idx}] Foreign key violation: publication_id={pid} does not exist in publications.csv."
                )
            elif pid in seen_detail_pids:
                errors.append(f"[PUBLICATION_DETAILS row #{idx}] Duplicate publication_id={pid}.")
            else:
                seen_detail_pids.add(pid)

            if sid and (not _is_valid_positive_int(sid) or sid not in scholar_ids):
                errors.append(
                    f"[PUBLICATION_DETAILS row #{idx}] Foreign key violation: scholar_id={sid!r} does not exist in scholars.csv."
                )
            elif sid and (sid, pid) not in ap_keys:
                errors.append(
                    f"[PUBLICATION_DETAILS row #{idx}] Provenance scholar_id={sid} is not linked to publication_id={pid}."
                )
            if not (r.get("title") or "").strip():
                errors.append(f"[PUBLICATION_DETAILS row #{idx}] Required field title is empty.")
            ls = (r.get("last_synced_at") or "").strip()
            if not _is_valid_timestamp(ls):
                errors.append(
                    f"[PUBLICATION_DETAILS row #{idx}] Required timestamp last_synced_at is invalid or empty: {ls!r}."
                )

        seen_yearly_keys: set[tuple[str, str]] = set()
        for idx, r in enumerate(detail_tables.get("PUBLICATION_YEARLY_CITATIONS", []), 1):
            pid = str(r.get("publication_id") or "").strip()
            yr = str(r.get("citation_year") or "").strip()
            cnt = str(r.get("citation_count") or "").strip()
            if not _is_valid_positive_int(pid):
                errors.append(f"[PUBLICATION_YEARLY_CITATIONS row #{idx}] Invalid publication_id={pid!r}.")
            elif pid not in pub_ids:
                errors.append(
                    f"[PUBLICATION_YEARLY_CITATIONS row #{idx}] Foreign key violation: publication_id={pid} does not exist in publications.csv."
                )
            if not yr.isdigit() or len(yr) != 4:
                errors.append(f"[PUBLICATION_YEARLY_CITATIONS row #{idx}] Invalid citation_year={yr!r}.")
            if not cnt.isdigit():
                errors.append(f"[PUBLICATION_YEARLY_CITATIONS row #{idx}] Invalid citation_count={cnt!r}.")
            if pid and yr:
                comp = (pid, yr)
                if comp in seen_yearly_keys:
                    errors.append(
                        f"[PUBLICATION_YEARLY_CITATIONS row #{idx}] Duplicate composite primary key (publication_id={pid}, citation_year={yr})."
                    )
                seen_yearly_keys.add(comp)
            ls = (r.get("last_synced_at") or "").strip()
            if not _is_valid_timestamp(ls):
                errors.append(
                    f"[PUBLICATION_YEARLY_CITATIONS row #{idx}] Required timestamp last_synced_at is invalid or empty: {ls!r}."
                )

    return errors
