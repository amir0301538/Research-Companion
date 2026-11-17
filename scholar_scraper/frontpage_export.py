"""Canonical CSV storage, equivalent JSON export, and scholar image metadata."""
import csv
import io
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from .dataset_ops import _commit_files
from .erd_schema import CSV_FILES, ERD_ENTITIES
from .output_paths import OutputPaths
from .validator import DatasetValidationError, validate_dataset

IMAGE_COLUMNS = ("scholar_id", "google_scholar_id", "local_profile_image_path", "original_image_url", "image_status", "last_image_synced_at")
DEFAULT_IMAGE = "assets/default_anonymous.svg"


def image_extension(body, content_type):
    if len(body) > 5_000_000:
        return ""
    mime = content_type.split(";", 1)[0].strip().lower()
    if body.startswith(b"\xff\xd8\xff") and mime in ("image/jpeg", "image/jpg"):
        return ".jpg"
    if body.startswith(b"\x89PNG\r\n\x1a\n") and mime == "image/png":
        return ".png"
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP" and mime == "image/webp":
        return ".webp"
    if body.startswith((b"GIF87a", b"GIF89a")) and mime == "image/gif":
        return ".gif"
    return ""


def image_record(out_dir, scholar, profile, previous, timestamp):
    """Return metadata and optional staged image, retaining verified older photos."""
    sid, gid = scholar["scholar_id"], scholar["google_scholar_id"]
    url = getattr(profile, "profile_image_url", None) or ""
    parsed = urlparse(url)
    allowed = parsed.scheme == "https" and (parsed.hostname in ("scholar.google.com", "scholar.googleusercontent.com") or
              (parsed.hostname or "").endswith(".googleusercontent.com"))
    body = getattr(profile, "profile_image_bytes", b"") if allowed else b""
    extension = image_extension(body, getattr(profile, "profile_image_content_type", ""))
    # Scholar's anonymous avatar is not a verified researcher photograph.
    if any(marker in url.lower() for marker in ("anonymous", "default", "avatar")):
        extension = ""
    old = next((row for row in previous if row["scholar_id"] == sid), None)
    old_path = old.get("local_profile_image_path", "") if old else ""
    old_real = (old and old.get("image_status") in ("REAL", "PRESERVED") and
                old_path.startswith("profile_images/") and
                (Path(out_dir) / old_path).resolve().parent == (Path(out_dir) / "profile_images").resolve() and
                (Path(out_dir) / old_path).is_file())
    if extension:
        relative = f"profile_images/{gid}{extension}"
        status, extra = "REAL", {relative: body}
    elif old_real:
        relative, status, extra = old_path, "PRESERVED", {}
    else:
        relative = Path(os.path.relpath(Path(__file__).resolve().parent.parent / DEFAULT_IMAGE, out_dir)).as_posix()
        failed_download = bool(url) and not any(marker in url.lower() for marker in ("anonymous", "default", "avatar"))
        status, extra = "FAILED" if failed_download else "DEFAULT", {}
    row = dict(scholar_id=sid, google_scholar_id=gid, local_profile_image_path=relative,
               original_image_url=url, image_status=status, last_image_synced_at=timestamp)
    return row, extra


def csv_bytes(columns, rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows({column: row.get(column, "") for column in columns} for row in rows)
    return stream.getvalue().encode("utf-8")


def load_images(out_dir):
    path = OutputPaths(out_dir).image_metadata
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != IMAGE_COLUMNS:
            raise ValueError("scholar_images.csv has an unexpected header")
        return list(reader)


def validate_images(tables, images):
    scholars = {row["scholar_id"]: row["google_scholar_id"] for row in tables["SCHOLARS"]}
    seen = set()
    for row in images:
        sid = row["scholar_id"]
        if sid in seen or scholars.get(sid) != row["google_scholar_id"]:
            raise ValueError("Invalid scholar image relationship")
        seen.add(sid)
        if row["image_status"] not in ("REAL", "DEFAULT", "PRESERVED", "FAILED"):
            raise ValueError("Invalid scholar image status")


def export_replacements(tables, images):
    errors = validate_dataset(tables)
    if errors:
        raise DatasetValidationError(errors)
    validate_images(tables, images)
    replacements = {f"csv/{name}": csv_bytes(ERD_ENTITIES[entity], tables[entity])
                    for entity, name in CSV_FILES.items()}
    replacements["csv/scholar_images.csv"] = csv_bytes(IMAGE_COLUMNS, images)
    payload = {entity: tables[entity] for entity in CSV_FILES}
    payload["SCHOLAR_IMAGES"] = images
    replacements["json/dataset.json"] = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return replacements


def commit_export(out_dir, tables, images, extra=None):
    replacements = export_replacements(tables, images)
    replacements.update(extra or {})
    def verify():
        from .reporting import load_existing_csvs
        current = load_existing_csvs(out_dir)
        if current != tables or load_images(out_dir) != images:
            raise ValueError("Committed export differs from validated dataset")
    _commit_files(Path(out_dir), replacements, validate_after=verify)
