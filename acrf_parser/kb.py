"""Phase 7 - knowledge base.

Persists what the parser learned into SQLite: forms, fields, annotations and the
scored links between them. One database accumulates across studies, which is the
point - the value of an aCRF parser is not one PDF, it is the growing answer to
"what does this form call this field, and what does it map to?".

Everything is keyed on `(form_name, field_text)`, both normalized. Never on
field text alone: "Start Date" is `MHSTDTC` on Medical History, `CMSTDTC` on
Concomitant Medications and `AESTDTC` on Adverse Events, and a knowledge base
that forgets the form will confidently return the wrong one.

Rows are occurrences, not conclusions. A field appearing on both pages of a
two-page form is two rows, and the `field_map` view is what folds occurrences
into the one-row-per-key answer - so a disagreement between pages stays visible
instead of being silently resolved at write time.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Document, Field
from .normalize import normalize

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL,
    file_name   TEXT NOT NULL,
    page_count  INTEGER NOT NULL,
    parsed_at   TEXT NOT NULL,
    metadata    TEXT,
    UNIQUE (path)
);

CREATE TABLE IF NOT EXISTS forms (
    id                INTEGER PRIMARY KEY,
    document_id       INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    name              TEXT NOT NULL,
    normalized_name   TEXT NOT NULL,
    domain            TEXT,
    pages             TEXT NOT NULL,
    continuation_pages TEXT NOT NULL,
    confidence        REAL NOT NULL,
    source            TEXT,
    evidence          TEXT,
    UNIQUE (document_id, normalized_name)
);

CREATE TABLE IF NOT EXISTS fields (
    id              INTEGER PRIMARY KEY,
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    form_id         INTEGER NOT NULL REFERENCES forms(id) ON DELETE CASCADE,
    field_ref       TEXT NOT NULL,          -- Field.id, unique within a document
    field_key       TEXT NOT NULL,          -- normalized "form|field": the primary key
    page            INTEGER NOT NULL,
    text            TEXT NOT NULL,
    raw_text        TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    item_number     TEXT,
    section         TEXT,
    role            TEXT,
    confidence      REAL NOT NULL,
    bbox            TEXT NOT NULL,
    relative        TEXT NOT NULL,          -- rel_x_pct/rel_y_pct: survives re-layout
    options         TEXT NOT NULL,
    control_kinds   TEXT NOT NULL,
    evidence        TEXT,
    UNIQUE (document_id, field_ref)
);

CREATE TABLE IF NOT EXISTS annotations (
    id            INTEGER PRIMARY KEY,
    document_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    form_id       INTEGER REFERENCES forms(id) ON DELETE CASCADE,
    annot_ref     TEXT NOT NULL,
    page          INTEGER NOT NULL,
    text          TEXT NOT NULL,
    annot_type    TEXT NOT NULL,
    confidence    REAL NOT NULL,
    variable      TEXT,
    domain        TEXT,
    value         TEXT,
    qnam          TEXT,
    target_page   INTEGER,
    parsed        TEXT,
    evidence      TEXT,
    bbox          TEXT NOT NULL,
    relative      TEXT NOT NULL,
    UNIQUE (document_id, annot_ref)
);

CREATE TABLE IF NOT EXISTS links (
    id             INTEGER PRIMARY KEY,
    document_id    INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    field_id       INTEGER NOT NULL REFERENCES fields(id) ON DELETE CASCADE,
    annotation_id  INTEGER NOT NULL REFERENCES annotations(id) ON DELETE CASCADE,
    link_score     REAL NOT NULL,
    rejected       INTEGER NOT NULL DEFAULT 0,
    evidence       TEXT
);

CREATE INDEX IF NOT EXISTS ix_fields_key   ON fields(field_key);
CREATE INDEX IF NOT EXISTS ix_links_field  ON links(field_id, rejected);
CREATE INDEX IF NOT EXISTS ix_annot_var    ON annotations(variable);

-- The headline relation: (form, field) -> annotation, one row per accepted link.
CREATE VIEW IF NOT EXISTS field_annotations AS
SELECT d.file_name, fm.name AS form_name, fm.domain, f.field_key, f.page,
       f.text AS field_text, f.normalized_text,
       a.text AS annotation_text, a.annot_type, a.variable, a.value, a.qnam,
       l.link_score, f.confidence AS field_confidence
FROM   links l
JOIN   fields f       ON f.id = l.field_id
JOIN   annotations a  ON a.id = l.annotation_id
JOIN   forms fm       ON fm.id = f.form_id
JOIN   documents d    ON d.id = f.document_id
WHERE  l.rejected = 0;

-- Occurrences folded into one row per (form, field). `variants` > 1 means the
-- pages of one form disagree - a review item, not something to hide.
CREATE VIEW IF NOT EXISTS field_map AS
SELECT field_key,
       form_name,
       field_text,
       COUNT(*)                        AS occurrences,
       COUNT(DISTINCT annotation_text) AS variants,
       GROUP_CONCAT(DISTINCT annotation_text) AS annotation_texts,
       GROUP_CONCAT(DISTINCT variable)        AS variables,
       MAX(link_score)                 AS best_link_score
FROM   field_annotations
GROUP  BY field_key, form_name, field_text;
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) a knowledge base."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def build_kb(doc: Document, db_path: str | Path, replace: bool = True) -> Path:
    """Write one parsed document into the knowledge base.

    Re-parsing the same file replaces its rows rather than duplicating them, so
    a re-run after a threshold change is safe. `replace=False` refuses instead.
    """
    con = connect(db_path)
    try:
        with con:
            existing = con.execute("SELECT id FROM documents WHERE path = ?",
                                   (doc.path,)).fetchone()
            if existing and not replace:
                raise ValueError(f"{doc.path} is already in this knowledge base")
            if existing:
                con.execute("DELETE FROM documents WHERE id = ?", (existing["id"],))
            doc_id = _insert_document(con, doc)
            form_ids = _insert_forms(con, doc, doc_id)
            field_ids = _insert_fields(con, doc, doc_id, form_ids)
            annot_ids = _insert_annotations(con, doc, doc_id, form_ids)
            _insert_links(con, doc, doc_id, field_ids, annot_ids)
    finally:
        con.close()
    return Path(db_path)


# --- writers ---------------------------------------------------------------
def _insert_document(con: sqlite3.Connection, doc: Document) -> int:
    cur = con.execute(
        "INSERT INTO documents (path, file_name, page_count, parsed_at, metadata)"
        " VALUES (?, ?, ?, ?, ?)",
        (doc.path, Path(doc.path).name, doc.page_count,
         datetime.now(timezone.utc).isoformat(timespec="seconds"),
         json.dumps(doc.metadata)))
    return int(cur.lastrowid)


def _insert_forms(con, doc: Document, doc_id: int) -> dict[str, int]:
    ids: dict[str, int] = {}
    for f in doc.forms:
        cur = con.execute(
            "INSERT INTO forms (document_id, name, normalized_name, domain, pages,"
            " continuation_pages, confidence, source, evidence)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (doc_id, f.name, f.normalized_name, f.domain, json.dumps(f.pages),
             json.dumps(f.continuation_pages), f.confidence, f.source,
             json.dumps(f.evidence)))
        ids[f.normalized_name] = int(cur.lastrowid)
    return ids


def _insert_fields(con, doc: Document, doc_id: int, form_ids: dict[str, int]) -> dict[str, int]:
    ids: dict[str, int] = {}
    for page in doc.pages:
        for fld in page.fields:
            form_id = form_ids.get(normalize(fld.form_name))
            if form_id is None:
                continue          # a field on a page no form claimed: nothing to key it to
            cur = con.execute(
                "INSERT INTO fields (document_id, form_id, field_ref, field_key, page,"
                " text, raw_text, normalized_text, item_number, section, role,"
                " confidence, bbox, relative, options, control_kinds, evidence)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (doc_id, form_id, fld.id, field_key(fld), fld.page, fld.text,
                 fld.raw_text, fld.normalized_text, fld.item_number, fld.section,
                 fld.role, fld.confidence, json.dumps(list(fld.bbox.as_tuple())),
                 json.dumps(fld.bbox.relative(page.width, page.height)),
                 json.dumps(fld.option_texts), json.dumps(fld.control_kinds),
                 json.dumps(fld.evidence)))
            ids[fld.id] = int(cur.lastrowid)
    return ids


def _insert_annotations(con, doc: Document, doc_id: int, form_ids: dict[str, int]) -> dict[str, int]:
    ids: dict[str, int] = {}
    for page in doc.pages:
        for a in page.annotations:
            p = a.parsed or {}
            cur = con.execute(
                "INSERT INTO annotations (document_id, form_id, annot_ref, page, text,"
                " annot_type, confidence, variable, domain, value, qnam, target_page,"
                " parsed, evidence, bbox, relative)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (doc_id, form_ids.get(normalize(a.form_name)), a.id, a.page, a.text,
                 a.annot_type, a.type_confidence, p.get("variable"), p.get("domain"),
                 p.get("value"), p.get("qnam"), p.get("target_page"),
                 json.dumps(p), json.dumps(a.type_evidence),
                 json.dumps(list(a.bbox.as_tuple())),
                 json.dumps(a.bbox.relative(page.width, page.height))))
            ids[a.id] = int(cur.lastrowid)
    return ids


def _insert_links(con, doc: Document, doc_id: int, field_ids, annot_ids) -> None:
    for l in doc.links:
        fid, aid = field_ids.get(l.field_id), annot_ids.get(l.annotation_id)
        if fid is None or aid is None:
            continue
        con.execute(
            "INSERT INTO links (document_id, field_id, annotation_id, link_score,"
            " rejected, evidence) VALUES (?,?,?,?,?,?)",
            (doc_id, fid, aid, l.link_score, int(l.rejected), json.dumps(l.evidence)))


def field_key(fld: Field) -> str:
    """`(form_name, field_text)`, both normalized, as one storable string."""
    form, text = fld.key
    return f"{form}|{text}"


# --- queries ---------------------------------------------------------------
class KnowledgeBase:
    """Read access to a built knowledge base."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.con = connect(self.path)

    def __enter__(self) -> "KnowledgeBase":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.con.close()

    def lookup(self, form_name: str, field_text: str) -> list[dict]:
        """The reason the KB exists: what does this form call this field?"""
        key = f"{normalize(form_name)}|{normalize(field_text)}"
        rows = self.con.execute(
            "SELECT * FROM field_annotations WHERE field_key = ?"
            " ORDER BY link_score DESC", (key,)).fetchall()
        return [dict(r) for r in rows]

    def variable_for(self, form_name: str, field_text: str) -> str | None:
        """Best single SDTM variable for a field, or None if it has no markup."""
        for row in self.lookup(form_name, field_text):
            if row["variable"]:
                return row["variable"]
        return None

    def forms(self) -> list[dict]:
        return [dict(r) for r in self.con.execute(
            "SELECT name, domain, pages, confidence FROM forms ORDER BY name")]

    def field_map(self, form_name: str | None = None) -> list[dict]:
        sql = "SELECT * FROM field_map"
        args: tuple = ()
        if form_name:
            sql += " WHERE field_key LIKE ?"
            args = (f"{normalize(form_name)}|%",)
        return [dict(r) for r in self.con.execute(sql + " ORDER BY field_key", args)]

    def disagreements(self) -> list[dict]:
        """Keys whose occurrences map to more than one annotation - review these."""
        return [dict(r) for r in self.con.execute(
            "SELECT * FROM field_map WHERE variants > 1 ORDER BY field_key")]

    def stats(self) -> dict:
        one = lambda sql: self.con.execute(sql).fetchone()[0]
        return {
            "documents": one("SELECT COUNT(*) FROM documents"),
            "forms": one("SELECT COUNT(*) FROM forms"),
            "fields": one("SELECT COUNT(*) FROM fields"),
            "annotations": one("SELECT COUNT(*) FROM annotations"),
            "links": one("SELECT COUNT(*) FROM links WHERE rejected = 0"),
            "mapped_keys": one("SELECT COUNT(*) FROM field_map"),
        }
