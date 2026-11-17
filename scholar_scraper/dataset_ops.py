"""Offline dataset operations shared by the GUI and scraper.

Deletion keeps historical IdRegistry entries (tombstones). A transaction journal
contains byte-for-byte backups until every replacement has succeeded. Recovery
runs before the next operation if a process died during a commit.
"""
import csv
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .erd_schema import CSV_FILES, DETAIL_CSV_FILES, DETAIL_ENTITIES, ERD_ENTITIES
from .id_registry import IdRegistry
from .output_paths import OutputPaths
from .reporting import CsvPermissionError, load_existing_csvs, load_existing_detail_csvs, write_csv
from .validator import DatasetValidationError, validate_dataset


@contextmanager
def dataset_lock(out_dir: Path, legacy: bool = False):
    """An OS lock, released even after a crash (Windows and POSIX)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    lock_path = out_dir / ".dataset.lock" if legacy else OutputPaths(out_dir).lock
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    try:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Dataset is busy with another scraping or deletion operation.") from exc
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def _journal(out_dir):
    return Path(out_dir) / ".deletion_transaction"


def recover_deletion(out_dir: Path):
    """Restore an interrupted commit. Caller must hold dataset_lock."""
    journal = _journal(out_dir)
    manifest = journal / "manifest.json"
    if (journal / "committed").exists():
        shutil.rmtree(journal)
        return
    if not manifest.exists():
        if journal.exists():
            shutil.rmtree(journal)
        return
    entries = json.loads(manifest.read_text(encoding="utf-8"))
    for index, relative in enumerate(entries):
        target = Path(out_dir) / relative
        backup = journal / "backup" / str(index)
        if backup.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)
        elif target.exists():
            target.unlink()
    shutil.rmtree(journal)


def _commit_files(out_dir: Path, replacements: dict[str, bytes], validate_after=None):
    journal = _journal(out_dir)
    if journal.exists():
        raise RuntimeError("An unfinished deletion transaction requires recovery.")
    (journal / "backup").mkdir(parents=True)
    (journal / "staged").mkdir()
    names = list(replacements)
    try:
        for index, name in enumerate(names):
            target = out_dir / name
            if target.exists():
                shutil.copy2(target, journal / "backup" / str(index))
            staged = journal / "staged" / str(index)
            if replacements[name] is not None:
                staged.write_bytes(replacements[name])
        manifest = journal / "manifest.json"
        manifest.write_text(json.dumps(names), encoding="utf-8")
        with open(manifest, "r+b") as stream:
            os.fsync(stream.fileno())
        for index, name in enumerate(names):
            target = out_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                if replacements[name] is None:
                    if target.exists():
                        target.unlink()
                else:
                    os.replace(journal / "staged" / str(index), target)
            except PermissionError as exc:
                raise CsvPermissionError(target, operation="replace", original_error=exc) from exc
        if validate_after:
            validate_after()
        (journal / "committed").write_text("ok", encoding="ascii")
    except Exception:
        if (journal / "manifest.json").exists():
            recover_deletion(out_dir)
        elif journal.exists():
            shutil.rmtree(journal)
        raise
    shutil.rmtree(journal)


def _csv_bytes(columns, rows):
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "rows.csv"
        write_csv(path, columns, rows)
        return path.read_bytes()


def scoped_detail_path(out_dir: Path, google_id: str) -> Path:
    safe = "".join(c for c in google_id if c.isalnum() or c in "_-")
    if safe != google_id or not safe:
        raise ValueError("Invalid Google Scholar ID for checkpoint path")
    return OutputPaths(out_dir).checkpoints / f"{safe}.json"


def all_checkpoints(out_dir: Path) -> list[Path]:
    paths = OutputPaths(out_dir)
    return sorted(paths.checkpoints.glob("*.json")) + [paths.checkpoints / "publication_details_snapshot.json"]


def preview_deletion(out_dir: Path, google_id: str):
    out_dir = Path(out_dir)
    from .output_migration import migrate_output
    migrate_output(out_dir)
    with dataset_lock(out_dir):
        recover_deletion(out_dir)
        tables = load_existing_csvs(out_dir)
        detail = load_existing_detail_csvs(out_dir)
    matches = [r for r in tables["SCHOLARS"] if r.get("google_scholar_id") == google_id]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("Duplicate Google Scholar ID in scholars.csv")
    scholar = matches[0]
    sid = scholar["scholar_id"]
    linked = {r["publication_id"] for r in tables["Authors Publications"] if r["scholar_id"] == sid}
    areas = [r for r in tables["Scholar Research Areas"] if r["scholar_id"] == sid]
    return {"scholar": scholar, "linked_publications": len(linked), "research_area_links": len(areas),
            "detail_records": sum(r["publication_id"] in linked for r in detail["PUBLICATION_DETAILS"]),
            "yearly_records": sum(r["publication_id"] in linked for r in detail["PUBLICATION_YEARLY_CITATIONS"])}


def delete_scholar(out_dir: Path, google_id: str):
    """Delete by Google ID after GUI confirmation; never run on real data in tests."""
    out_dir = Path(out_dir)
    from .output_migration import migrate_output
    migrate_output(out_dir)
    with dataset_lock(out_dir):
        recover_deletion(out_dir)
        tables = load_existing_csvs(out_dir)
        detail = load_existing_detail_csvs(out_dir)
        matches = [r for r in tables["SCHOLARS"] if r.get("google_scholar_id") == google_id]
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError("Duplicate Google Scholar ID in scholars.csv")
        errors = validate_dataset(tables, detail_tables=detail)
        if errors:
            raise DatasetValidationError(errors)
        paths = OutputPaths(out_dir)
        registry_path = paths.registry
        registry = IdRegistry(registry_path)
        registry.sync_from_tables(tables)
        from .frontpage_export import export_replacements, image_record, load_images
        from .browser import ProfileData
        images = load_images(out_dir)
        image_ids = {row["scholar_id"] for row in images}
        for stored_scholar in tables["SCHOLARS"]:
            if stored_scholar["scholar_id"] not in image_ids:
                default_image, _ = image_record(out_dir, stored_scholar, ProfileData(requested_url=""),
                                                images, "")
                images.append(default_image)
        scholar = matches[0]
        sid = scholar["scholar_id"]
        old_linked = {r["publication_id"] for r in tables["Authors Publications"] if r["scholar_id"] == sid}
        area_links_removed = sum(r["scholar_id"] == sid for r in tables["Scholar Research Areas"])
        tables["SCHOLARS"] = [r for r in tables["SCHOLARS"] if r["scholar_id"] != sid]
        tables["Authors Publications"] = [r for r in tables["Authors Publications"] if r["scholar_id"] != sid]
        tables["Scholar Research Areas"] = [r for r in tables["Scholar Research Areas"] if r["scholar_id"] != sid]
        keep_pubs = {r["publication_id"] for r in tables["Authors Publications"]}
        removed_pubs = old_linked - keep_pubs
        tables["PUBLICATIONS"] = [r for r in tables["PUBLICATIONS"] if r["publication_id"] not in removed_pubs]
        keep_areas = {r["research_area_id"] for r in tables["Scholar Research Areas"]}
        before_areas = len(tables["RESEARCH_AREAS"])
        tables["RESEARCH_AREAS"] = [r for r in tables["RESEARCH_AREAS"] if r["research_area_id"] in keep_areas]
        keep_heis = {r["hei_id"] for r in tables["SCHOLARS"]}
        hei_removed = scholar["hei_id"] not in keep_heis
        tables["HEIs"] = [r for r in tables["HEIs"] if r["hei_id"] in keep_heis]
        before_detail = len(detail["PUBLICATION_DETAILS"])
        before_yearly = len(detail["PUBLICATION_YEARLY_CITATIONS"])
        detail["PUBLICATION_DETAILS"] = [r for r in detail["PUBLICATION_DETAILS"] if r["publication_id"] not in removed_pubs]
        detail["PUBLICATION_YEARLY_CITATIONS"] = [r for r in detail["PUBLICATION_YEARLY_CITATIONS"] if r["publication_id"] not in removed_pubs]
        owners = {}
        for row in tables["Authors Publications"]:
            owners.setdefault(row["publication_id"], []).append(row["scholar_id"])
        for row in detail["PUBLICATION_DETAILS"]:
            if row["scholar_id"] == sid:
                row["scholar_id"] = sorted(owners[row["publication_id"]], key=int)[0]
        errors = validate_dataset(tables, detail_tables=detail)
        if errors:
            raise DatasetValidationError(errors)
        removed_images = [row for row in images if row["scholar_id"] == sid]
        images = [row for row in images if row["scholar_id"] != sid]
        replacements = {}
        registry_bytes = json.dumps({"version": 1, "entities": registry._data},
                                    ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        if not registry_path.exists() or json.loads(registry_path.read_text(encoding="utf-8")) != {"version": 1, "entities": registry._data}:
            replacements["system/id_registry.json"] = registry_bytes
        # Keep both representations synchronized with the canonical CSV rows.
        replacements.update(export_replacements(tables, images))
        for image in removed_images:
            relative = image.get("local_profile_image_path", "")
            if (relative.startswith("profile_images/") and
                    (out_dir / relative).resolve().parent == (out_dir / "profile_images").resolve() and
                    relative not in {row.get("local_profile_image_path") for row in images} and
                    Path(relative).name == f"{google_id}{Path(relative).suffix}" and
                    (out_dir / relative).is_file()):
                replacements[relative] = None
        for entity, name in DETAIL_CSV_FILES.items():
            replacements[f"system/historical/{name}"] = _csv_bytes(DETAIL_ENTITIES[entity], detail[entity])
        # Keep registry entries and next_id counters as historical tombstones.
        target_checkpoint = scoped_detail_path(out_dir, google_id)
        transfers = {}
        for path in all_checkpoints(out_dir):
            if not path.exists():
                continue
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            records = snapshot.get("publications") or {}
            for pid in list(records):
                if pid in removed_pubs or str(records[pid].get("scholar_id")) == sid:
                    if pid in keep_pubs and records[pid].get("status") == "SUCCESS":
                        new_owner = sorted(owners[pid], key=int)[0]
                        records[pid]["scholar_id"] = new_owner
                        if path == target_checkpoint:
                            owner_row = next(r for r in tables["SCHOLARS"] if r["scholar_id"] == new_owner)
                            transfers.setdefault(owner_row["google_scholar_id"], {})[pid] = records.pop(pid)
                    else:
                        del records[pid]
            replacements[str(path.relative_to(out_dir)).replace("\\", "/")] = json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")
        for owner_google_id, records in transfers.items():
            path = scoped_detail_path(out_dir, owner_google_id)
            relative = str(path.relative_to(out_dir)).replace("\\", "/")
            if relative in replacements:
                snapshot = json.loads(replacements[relative].decode("utf-8"))
            elif path.exists():
                snapshot = json.loads(path.read_text(encoding="utf-8"))
            else:
                snapshot = {"publications": {}}
            snapshot.setdefault("publications", {}).update(records)
            replacements[relative] = json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")
        raw = paths.snapshots / "raw_profile_snapshot.json"
        scoped_raw = paths.profile_snapshot(google_id)
        for path in (raw, scoped_raw):
            if path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload.get("google_scholar_id") == google_id:
                    replacements[str(path.relative_to(out_dir)).replace("\\", "/")] = b"{}\n"
        linkage = paths.linkage
        if linkage.exists():
            with open(linkage, encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                cols = reader.fieldnames
                rows = [r for r in reader if r.get("google_scholar_id") != google_id]
            replacements[paths.relative(linkage)] = _csv_bytes(cols, rows)
        def _verify_committed():
            committed_tables = load_existing_csvs(out_dir)
            committed_detail = load_existing_detail_csvs(out_dir)
            issues = validate_dataset(committed_tables, detail_tables=committed_detail)
            if issues:
                raise DatasetValidationError(issues)
            if committed_tables != tables or load_images(out_dir) != images:
                raise ValueError("Deletion commit did not preserve the validated dataset.")
        _commit_files(out_dir, replacements, validate_after=_verify_committed)
        return {"scholar": scholar, "publications_removed": len(removed_pubs),
                "shared_publications_preserved": len(old_linked - removed_pubs),
                "research_area_links_removed": area_links_removed,
                "orphan_areas_removed": before_areas - len(tables["RESEARCH_AREAS"]),
                "hei_removed": hei_removed, "details_removed": before_detail - len(detail["PUBLICATION_DETAILS"]),
                "yearly_removed": before_yearly - len(detail["PUBLICATION_YEARLY_CITATIONS"])}
