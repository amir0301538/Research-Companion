"""Recoverable, idempotent migration of the legacy flat output layout."""
import csv
import hashlib
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from .erd_schema import CSV_FILES, ERD_ENTITIES
from .output_paths import OutputPaths
from .validator import DatasetValidationError, validate_dataset


def _legacy_tables(root):
    tables = {}
    for entity, filename in CSV_FILES.items():
        path = root / filename
        if not path.exists():
            raise FileNotFoundError(f"Legacy dataset is missing {path}")
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != ERD_ENTITIES[entity]:
                raise ValueError(f"Unexpected legacy CSV header: {path}")
            tables[entity] = list(reader)
    return tables


def _destination(relative):
    name = relative.as_posix()
    if relative.parent == Path("."):
        if name in CSV_FILES.values() or name == "scholar_images.csv":
            return f"csv/{name}"
        if name == "dataset.json":
            return f"json/{name}"
        if name in ("extraction_report.txt", "extraction_quality_report.txt", "refresh_all_report.json"):
            return f"reports/{name}"
        if name == "scraper.log":
            return f"logs/{name}"
        if name == "id_registry.json":
            return f"system/{name}"
        return f"system/legacy/{name}"
    if relative.parts[0] == "profile_images":
        return name
    if relative.parts[0] == "supplementary":
        rest = Path(*relative.parts[1:]).as_posix()
        if rest in ("publication_details.csv", "publication_yearly_citations.csv"):
            return f"system/historical/{rest}"
        if rest == "publication_details_snapshot.json":
            return f"system/checkpoints/{rest}"
        if rest == "raw_profile_snapshot.json":
            return f"system/snapshots/{rest}"
        if rest == "record_linkage.csv":
            return "system/record_linkage.csv"
        if rest.startswith("profiles/"):
            return f"system/snapshots/{rest}"
        if rest.startswith("checkpoints/"):
            return f"system/{rest}"
        return f"system/legacy/supplementary/{rest}"
    return f"system/legacy/{name}"


def migrate_output(root):
    """Back up and move old files transactionally; return a manifest of the work."""
    root = Path(root)
    paths = OutputPaths(root)
    if not (root / "heis.csv").exists():
        residual = [name for name in (*CSV_FILES.values(), "scholar_images.csv", "id_registry.json")
                    if (root / name).exists()]
        if residual:
            raise RuntimeError(f"Incomplete legacy output layout: {', '.join(residual)}")
        paths.ensure_dirs()
        return {"migrated": False, "backup": None, "files": [], "counts": None}
    if paths.csv("heis.csv").exists() or paths.registry.exists():
        raise RuntimeError("Mixed legacy and organized output detected; resolve the existing files before migration.")
    from .dataset_ops import _commit_files, dataset_lock, recover_deletion
    from .frontpage_export import IMAGE_COLUMNS, export_replacements
    from .id_registry import IdRegistry
    with dataset_lock(root, legacy=True):
        recover_deletion(root)
        tables = _legacy_tables(root)
        errors = validate_dataset(tables)
        if errors:
            raise DatasetValidationError(errors)
        registry = IdRegistry(root / "id_registry.json")
        registry.sync_from_tables(tables)
        image_file = root / "scholar_images.csv"
        images = []
        if image_file.exists():
            with image_file.open(encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                if tuple(reader.fieldnames or ()) != IMAGE_COLUMNS:
                    raise ValueError("Unexpected scholar_images.csv header")
                images = list(reader)
        for image in images:
            local = image.get("local_profile_image_path", "")
            if not local or not (root / local).is_file():
                raise ValueError(f"Missing scholar image for {image.get('google_scholar_id')}")
            if not any(s["scholar_id"] == image["scholar_id"] and
                       s["google_scholar_id"] == image["google_scholar_id"]
                       for s in tables["SCHOLARS"]):
                raise ValueError(f"Scholar image has an invalid scholar reference: {image.get('google_scholar_id')}")
        legacy_files = [p for p in root.rglob("*") if p.is_file() and
                        p.name != ".dataset.lock" and
                        ".deletion_transaction" not in p.parts and
                        ".staging_export" not in p.parts]
        legacy_files.sort()
        backup = root.parent / f"{root.name}_migration_backup_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
        shutil.copytree(root, backup, ignore=shutil.ignore_patterns(".dataset.lock", ".deletion_transaction"))
        for source in legacy_files:
            relative = source.relative_to(root)
            saved = backup / relative
            if not saved.is_file() or hashlib.sha256(source.read_bytes()).digest() != hashlib.sha256(saved.read_bytes()).digest():
                raise IOError(f"Backup verification failed for {relative}")
        replacements = {}
        moved = []
        for source in legacy_files:
            relative = source.relative_to(root)
            if relative.as_posix() == ".dataset.lock" or relative.parts[0] == "profile_images":
                continue
            target = _destination(relative)
            if target in replacements:
                raise ValueError(f"Conflicting migration destination: {target}")
            replacements[target] = source.read_bytes()
            replacements[relative.as_posix()] = None
            moved.append({"from": relative.as_posix(), "to": target})
        # Always regenerate the JSON export from the validated canonical rows.
        exported = export_replacements(tables, images)
        for target, body in exported.items():
            replacements[target] = body
        def verify():
            for entity, filename in CSV_FILES.items():
                with paths.csv(filename).open(encoding="utf-8", newline="") as stream:
                    if list(csv.DictReader(stream)) != tables[entity]:
                        raise ValueError(f"Migration changed rows in {filename}")
            payload = json.loads(paths.json_export.read_text(encoding="utf-8"))
            if any(payload.get(entity) != tables[entity] for entity in CSV_FILES) or payload.get("SCHOLAR_IMAGES") != images:
                raise ValueError("Migrated JSON does not match canonical CSV and image rows")
            IdRegistry(paths.registry).sync_from_tables(tables)
        _commit_files(root, replacements, validate_after=verify)
        old_supplementary = root / "supplementary"
        if old_supplementary.is_dir():
            for folder in sorted((p for p in old_supplementary.rglob("*") if p.is_dir()),
                                 key=lambda p: len(p.parts), reverse=True):
                try:
                    folder.rmdir()
                except OSError:
                    pass
            try:
                old_supplementary.rmdir()
            except OSError:
                pass
    paths.ensure_dirs()
    old_lock = root / ".dataset.lock"
    if old_lock.exists():
        old_lock.replace(paths.lock)
    return {"migrated": True, "backup": str(backup), "files": moved,
            "counts": {entity: len(rows) for entity, rows in tables.items()}}
