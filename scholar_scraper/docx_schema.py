"""
Reads entity/column names directly from the ERD Data Dictionary DOCX and
verifies they match the built-in schema used for CSV headers.

Uses only the Python standard library (a .docx file is a ZIP of XML parts).
"""

import logging
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .erd_schema import ERD_ENTITIES

log = logging.getLogger(__name__)

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
# Headings in the DOCX look like:  "SCHOLARS  |  Strong Entity"
ENTITY_HEADING = re.compile(r"^\s*(.+?)\s+\|\s+[A-Za-z /]+Entity\s*$")


def _text(node) -> str:
    return "".join(t.text or "" for t in node.iter(f"{W_NS}t")).strip()


def read_docx_entities(docx_path: Path) -> dict:
    """Return {entity_name: [column, ...]} in document order."""
    with zipfile.ZipFile(docx_path) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    body = root.find(f"{W_NS}body")
    entities, current = {}, None
    for child in body:
        if child.tag == f"{W_NS}p":
            m = ENTITY_HEADING.match(_text(child))
            if m:
                current = m.group(1).strip()
        elif child.tag == f"{W_NS}tbl" and current:
            cols = []
            for tr in child.findall(f"{W_NS}tr"):
                cells = tr.findall(f"{W_NS}tc")
                if cells:
                    first = _text(cells[0])
                    if first and first != "Column":
                        cols.append(first)
            if cols:
                entities[current] = cols
            current = None
    return entities


def validate_against_docx(docx_path):
    """
    Compare built-in schema with the DOCX.
    Returns (ok: bool, messages: list[str]).
    """
    if not docx_path:
        return None, ["No DOCX path supplied; built-in schema used without verification."]
    docx_path = Path(docx_path)
    if not docx_path.exists():
        return None, [f"DOCX not found at '{docx_path}'; built-in schema used without verification."]

    doc = read_docx_entities(docx_path)
    ok, msgs = True, []
    for entity, cols in ERD_ENTITIES.items():
        if entity not in doc:
            ok = False
            msgs.append(f"Entity '{entity}' not found in DOCX.")
        elif doc[entity] != cols:
            ok = False
            msgs.append(
                f"Column mismatch for '{entity}': DOCX={doc[entity]} built-in={cols}"
            )
        else:
            msgs.append(f"'{entity}': {len(cols)} columns match the DOCX exactly.")
    return ok, msgs
