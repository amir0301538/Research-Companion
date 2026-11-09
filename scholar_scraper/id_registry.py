"""
Persistent, deterministic ID registry for database primary keys.

Natural keys per entity:
  * HEIs           : normalized source/canonical HEI name + "|" + normalized country
  * SCHOLARS       : Google Scholar profile ID (google_scholar_id)
  * PUBLICATIONS   : normalized publication title + "|" + publication year
  * RESEARCH_AREAS : normalized research area name

Guarantees:
  * Every assigned ID is a positive integer fitting Oracle NUMBER(19).
  * The same natural key always receives the exact same numeric ID across runs.
  * Different natural keys never receive the same ID (verified via bijective
    mapping and explicit collision checks).
  * Updates are written atomically via a temporary file and os.replace() so an
    interrupted run cannot corrupt existing mappings.
"""

import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Optional

MAX_NUMBER_19 = 10**19 - 1
ENTITIES = ("HEIs", "SCHOLARS", "PUBLICATIONS", "RESEARCH_AREAS")


class RegistryError(Exception):
    """Base error for ID registry problems."""


class RegistryCollisionError(RegistryError):
    """Raised when two distinct natural keys map to the same numeric ID or vice versa."""


def normalize_text(value: Optional[str]) -> str:
    """Normalize text for case-insensitive canonical key comparison."""
    if not value:
        return ""
    norm = unicodedata.normalize("NFKC", str(value)).casefold()
    norm = re.sub(r"\s+", " ", norm).strip()
    return norm


def normalize_pub_title(title: Optional[str]) -> str:
    """
    Normalize a publication title for canonical identity across scholar profiles.
    Strips punctuation variations and collapses whitespace while preserving words/numbers.
    """
    if not title:
        return ""
    norm = unicodedata.normalize("NFKC", str(title)).casefold()
    # Replace punctuation/symbols with space so minor hyphen/quote differences normalize cleanly
    norm = re.sub(r"[^\w\s]", " ", norm)
    norm = re.sub(r"\s+", " ", norm).strip()
    return norm


def hei_natural_key(hei_name: str, country: str) -> str:
    name_norm = normalize_text(hei_name)
    country_norm = normalize_text(country)
    if not name_norm:
        raise RegistryError(f"HEI natural key requires a non-empty hei_name (got {hei_name!r}).")
    return f"{name_norm}|{country_norm}"


def scholar_natural_key(google_scholar_id: str) -> str:
    gs_id = (google_scholar_id or "").strip()
    if not gs_id:
        raise RegistryError("SCHOLARS natural key requires a non-empty google_scholar_id.")
    return gs_id


def publication_natural_key(title: str, publication_year: Optional[str]) -> str:
    title_norm = normalize_pub_title(title)
    if not title_norm:
        raise RegistryError("PUBLICATIONS natural key requires a non-empty publication title.")
    year_norm = (str(publication_year) if publication_year is not None else "").strip()
    return f"{title_norm}|{year_norm}"


def research_area_natural_key(area_name: str) -> str:
    area_norm = normalize_text(area_name)
    if not area_norm:
        raise RegistryError("RESEARCH_AREAS natural key requires a non-empty area_name.")
    return area_norm


def validate_positive_id(value, label: str = "id") -> int:
    """Validate that value is a positive integer fitting Oracle NUMBER(19)."""
    if isinstance(value, bool):
        raise RegistryError(f"Invalid {label}: boolean {value!r} is not a valid numeric ID.")
    try:
        int_val = int(str(value).strip())
    except (ValueError, TypeError) as exc:
        raise RegistryError(f"Invalid {label}: {value!r} is not a valid integer.") from exc
    if int_val < 1 or int_val > MAX_NUMBER_19:
        raise RegistryError(
            f"Invalid {label}: {int_val} is outside valid NUMBER(19) range (1..{MAX_NUMBER_19})."
        )
    return int_val


class IdRegistry:
    """
    Persistent ID registry mapping natural keys to sequential positive integers
    per entity type, with strict collision detection and atomic file saves.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path: Optional[Path] = Path(path) if path else None
        self._data: dict = {
            entity: {
                "next_id": 1,
                "entries": {},   # natural_key -> {"id": int, "created_at": str, "label": str}
            }
            for entity in ENTITIES
        }
        self._reverse: dict = {entity: {} for entity in ENTITIES}  # entity -> {id_int: natural_key}
        if self.path and self.path.exists():
            self.load(self.path)

    def _validate_and_rebuild_index(self):
        """Verify bijective mapping (1-to-1 between natural_key and id) for every entity."""
        self._reverse = {entity: {} for entity in ENTITIES}
        for entity in ENTITIES:
            bucket = self._data.setdefault(entity, {"next_id": 1, "entries": {}})
            entries = bucket.setdefault("entries", {})
            max_seen = 0
            for nat_key, meta in entries.items():
                if not nat_key or not isinstance(nat_key, str):
                    raise RegistryError(f"[{entity}] Empty or non-string natural key in registry.")
                if not isinstance(meta, dict) or "id" not in meta:
                    raise RegistryError(f"[{entity}] Malformed registry entry for key {nat_key!r}.")
                id_val = validate_positive_id(meta["id"], f"{entity}.id")
                meta["id"] = id_val
                if id_val in self._reverse[entity]:
                    other_key = self._reverse[entity][id_val]
                    if other_key != nat_key:
                        raise RegistryCollisionError(
                            f"ID collision in {entity}: ID {id_val} is assigned to both "
                            f"{other_key!r} and {nat_key!r}."
                        )
                self._reverse[entity][id_val] = nat_key
                if id_val > max_seen:
                    max_seen = id_val

            next_id = bucket.get("next_id", max_seen + 1)
            next_id = validate_positive_id(next_id, f"{entity}.next_id")
            if next_id <= max_seen:
                next_id = max_seen + 1
            bucket["next_id"] = next_id

    def load(self, path: Optional[Path] = None):
        target = Path(path) if path else self.path
        if not target or not target.exists():
            return
        with open(target, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise RegistryError(f"Registry file {target} must contain a JSON object.")
        entities_block = raw.get("entities", raw)
        for entity in ENTITIES:
            if entity in entities_block:
                self._data[entity] = entities_block[entity]
        self._validate_and_rebuild_index()

    def save(self, path: Optional[Path] = None):
        target = Path(path) if path else self.path
        if not target:
            return
        self._validate_and_rebuild_index()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target.with_suffix(target.suffix + ".tmp")
        payload = {
            "version": 1,
            "entities": self._data,
        }
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)

    def get_or_create(
        self,
        entity: str,
        natural_key: str,
        timestamp: str = "",
        label: str = "",
    ) -> tuple[str, str]:
        """
        Return (id_str, created_at_str) for (entity, natural_key).
        If natural_key is already registered, returns its existing ID and created_at.
        Otherwise allocates the next positive integer ID, checks for collisions, and records it.
        """
        if entity not in ENTITIES:
            raise RegistryError(f"Unknown registry entity: {entity!r}")
        if not natural_key or not isinstance(natural_key, str) or not natural_key.strip():
            raise RegistryError(f"Cannot allocate ID for {entity} with empty natural key.")

        bucket = self._data[entity]
        entries = bucket["entries"]
        if natural_key in entries:
            existing = entries[natural_key]
            id_val = validate_positive_id(existing["id"], f"{entity}.id")
            bound_key = self._reverse[entity].get(id_val)
            if bound_key is not None and bound_key != natural_key:
                raise RegistryCollisionError(
                    f"ID collision in {entity}: ID {id_val} is already bound to {bound_key!r}, "
                    f"cannot use for {natural_key!r}."
                )
            self._reverse[entity][id_val] = natural_key
            created_at = existing.get("created_at") or timestamp
            if created_at and not existing.get("created_at"):
                existing["created_at"] = created_at
            return str(id_val), created_at

        new_id = validate_positive_id(bucket["next_id"], f"{entity}.next_id")
        if new_id in self._reverse[entity]:
            other_key = self._reverse[entity][new_id]
            raise RegistryCollisionError(
                f"ID collision in {entity}: candidate next_id={new_id} is already assigned to {other_key!r}."
            )

        entries[natural_key] = {
            "id": new_id,
            "created_at": timestamp,
            "label": label or natural_key,
        }
        self._reverse[entity][new_id] = natural_key
        bucket["next_id"] = new_id + 1
        return str(new_id), timestamp

    def get_id(self, entity: str, natural_key: str) -> Optional[str]:
        """Return existing ID string for (entity, natural_key), or None if not registered."""
        if entity not in ENTITIES:
            raise RegistryError(f"Unknown registry entity: {entity!r}")
        entry = self._data[entity]["entries"].get(natural_key)
        return str(entry["id"]) if entry else None

    def sync_from_tables(self, tables: dict[str, list[dict]], default_timestamp: str = ""):
        """
        Synchronize registry state with existing CSV rows loaded from disk so
        any previously exported records and their natural keys are guaranteed
        to be registered with their existing numeric IDs.
        """
        if not tables:
            return

        def _sync_entry(entity: str, nat_key: str, raw_id: str, created_at: str, label: str):
            if not nat_key or not raw_id or not str(raw_id).strip().isdigit():
                return
            id_val = validate_positive_id(raw_id, f"{entity}.id")
            bucket = self._data[entity]
            entries = bucket["entries"]
            if nat_key in entries:
                existing_id = entries[nat_key]["id"]
                if existing_id != id_val:
                    raise RegistryCollisionError(
                        f"CSV/Registry ID mismatch in {entity} for key {nat_key!r}: "
                        f"CSV has id={id_val}, registry has id={existing_id}."
                    )
                return
            if id_val in self._reverse[entity] and self._reverse[entity][id_val] != nat_key:
                raise RegistryCollisionError(
                    f"CSV ID collision in {entity}: id={id_val} is used by both "
                    f"{self._reverse[entity][id_val]!r} and {nat_key!r}."
                )
            entries[nat_key] = {
                "id": id_val,
                "created_at": created_at or default_timestamp,
                "label": label or nat_key,
            }
            self._reverse[entity][id_val] = nat_key
            if bucket["next_id"] <= id_val:
                bucket["next_id"] = id_val + 1

        for r in tables.get("HEIs", []):
            hname = (r.get("hei_name") or "").strip()
            country = (r.get("country") or "").strip()
            if hname:
                _sync_entry(
                    "HEIs",
                    hei_natural_key(hname, country),
                    r.get("hei_id") or "",
                    r.get("created_at") or default_timestamp,
                    hname,
                )

        for r in tables.get("SCHOLARS", []):
            gs_id = (r.get("google_scholar_id") or "").strip()
            if gs_id:
                _sync_entry(
                    "SCHOLARS",
                    scholar_natural_key(gs_id),
                    r.get("scholar_id") or "",
                    r.get("last_synced_at") or default_timestamp,
                    r.get("scholar_name") or gs_id,
                )

        for r in tables.get("PUBLICATIONS", []):
            title = (r.get("title") or "").strip()
            year = (r.get("publication_year") or "").strip()
            if title:
                _sync_entry(
                    "PUBLICATIONS",
                    publication_natural_key(title, year),
                    r.get("publication_id") or "",
                    r.get("last_synced_at") or default_timestamp,
                    title,
                )

        for r in tables.get("RESEARCH_AREAS", []):
            aname = (r.get("area_name") or "").strip()
            if aname:
                _sync_entry(
                    "RESEARCH_AREAS",
                    research_area_natural_key(aname),
                    r.get("research_area_id") or "",
                    r.get("created_at") or default_timestamp,
                    aname,
                )
