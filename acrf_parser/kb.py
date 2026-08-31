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
    -- Rendered appearance, so house style can be derived from the database
    -- without re-parsing every PDF in the corpus.
    text_color    TEXT,
    fill_color    TEXT,
    fill_source   TEXT,
    font_name     TEXT,
    font_size     REAL,
    UNIQUE (document_id, annot_ref)
);

CREATE TABLE IF NOT EXISTS links (
    id             INTEGER PRIMARY KEY,
    document_id    INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    field_id       INTEGER NOT NULL REFERENCES fields(id) ON DELETE CASCADE,
    annotation_id  INTEGER NOT NULL REFERENCES annotations(id) ON DELETE CASCADE,
    link_score     REAL NOT NULL,
    rejected       INTEGER NOT NULL DEFAULT 0,
    evidence       TEXT,
    -- GEOMETRIC | HUMAN_APPROVED | HUMAN_REJECTED. A reviewer's decision is a
    -- different kind of fact from an inference and must not lose a tie to one.
    trust          TEXT NOT NULL DEFAULT 'GEOMETRIC',
    -- Placement relative to the field, computed once at write time so the
    -- geometry logic is not reimplemented against stored coordinates.
    relative_label TEXT,
    offset_x_pct   REAL,
    offset_y_pct   REAL
);

CREATE INDEX IF NOT EXISTS ix_fields_key   ON fields(field_key);
CREATE INDEX IF NOT EXISTS ix_links_field  ON links(field_id, rejected);
CREATE INDEX IF NOT EXISTS ix_annot_var    ON annotations(variable);

-- The headline relation: (form, field) -> annotation, one row per accepted link.
CREATE VIEW IF NOT EXISTS field_annotations AS
SELECT d.file_name, fm.name AS form_name, fm.domain, f.field_key, f.page,
       f.text AS field_text, f.normalized_text,
       a.text AS annotation_text, a.annot_type, a.variable, a.value, a.qnam,
       l.link_score, l.trust, f.confidence AS field_confidence
FROM   links l
JOIN   fields f       ON f.id = l.field_id
JOIN   annotations a  ON a.id = l.annotation_id
JOIN   forms fm       ON fm.id = f.form_id
JOIN   documents d    ON d.id = f.document_id
WHERE  l.rejected = 0;

-- What a reviewer explicitly turned down. Consulted so the corpus stops
-- proposing an answer a human has already rejected.
CREATE VIEW IF NOT EXISTS rejected_suggestions AS
SELECT f.field_key, a.text AS annotation_text, a.variable, d.file_name
FROM   links l
JOIN   fields f      ON f.id = l.field_id
JOIN   annotations a ON a.id = l.annotation_id
JOIN   documents d   ON d.id = f.document_id
WHERE  l.trust = 'HUMAN_REJECTED';

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


_ADDED_COLUMNS = [
    ("annotations", "fill_color", "TEXT"),
    ("annotations", "fill_source", "TEXT"),
]


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) a knowledge base."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def _migrate(con: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created.

    `CREATE TABLE IF NOT EXISTS` silently leaves an older database on its old
    shape, so an existing corpus would fail on insert rather than gain the new
    column. Only additive changes belong here.
    """
    for table, column, decl in _ADDED_COLUMNS:
        have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    con.commit()


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
                " parsed, evidence, bbox, relative, text_color, fill_color,"
                " fill_source, font_name, font_size)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (doc_id, form_ids.get(normalize(a.form_name)), a.id, a.page, a.text,
                 a.annot_type, a.type_confidence, p.get("variable"), p.get("domain"),
                 p.get("value"), p.get("qnam"), p.get("target_page"),
                 json.dumps(p), json.dumps(a.type_evidence),
                 json.dumps(list(a.bbox.as_tuple())),
                 json.dumps(a.bbox.relative(page.width, page.height)),
                 json.dumps(list(a.text_color)) if a.text_color else None,
                 json.dumps(list(a.fill_color)) if a.fill_color else None,
                 a.fill_source or None,
                 a.font_name or None, a.font_size or None))
            ids[a.id] = int(cur.lastrowid)
    return ids


def _insert_links(con, doc: Document, doc_id: int, field_ids, annot_ids) -> None:
    from .template import relative_label
    for l in doc.links:
        fid, aid = field_ids.get(l.field_id), annot_ids.get(l.annotation_id)
        if fid is None or aid is None:
            continue
        fld, a, page = doc.field(l.field_id), doc.annotation(l.annotation_id), doc.page(l.page)
        label = offx = offy = None
        if fld and a and page:
            w, h = page.width or 1.0, page.height or 1.0
            label = relative_label(a, fld)
            offx = round((a.bbox.x0 - fld.bbox.x1) / w, 4)
            offy = round((a.bbox.cy - fld.bbox.cy) / h, 4)
        con.execute(
            "INSERT INTO links (document_id, field_id, annotation_id, link_score,"
            " rejected, evidence, trust, relative_label, offset_x_pct, offset_y_pct)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (doc_id, fid, aid, l.link_score, int(l.rejected), json.dumps(l.evidence),
             l.trust, label, offx, offy))


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


# --- the return edge -------------------------------------------------------
LEARNED_CONFIDENCE = 1.0     # a human said so; nothing scores higher


def ingest_approved(report, blank_doc: Document, db_path: str | Path,
                    source: str | None = None) -> Path:
    """Feed a reviewed staging workbook back into the knowledge base.

    The return edge. Every approved row is a verified `(form, field_text) ->
    annotation` pair, and it is the best data the system ever sees - a person
    signed it. Without this the expensive rows are re-solved from scratch on
    every study.

    Ingested from the *workbook*, deliberately, and not by re-parsing the PDF we
    just wrote. Re-parsing throws away what the reviewer told us and re-derives
    it: on a blank CRF the domain-header annotations are absent, so a page that
    belongs to Disposition is inherited by the form above it, and the pair lands
    under the wrong form name - a machine-made error laundered into ground truth
    at 0.95 confidence. The sheet has a `form_name` column the reviewer could
    correct, so that column is what is believed here.

    Rejections are ingested too, as `HUMAN_REJECTED`. "The reviewer said no to
    MHSTDTC here" is the only thing that stops the same wrong suggestion coming
    back next study.
    """
    doc = _document_from_review(report, blank_doc, source)
    return build_kb(doc, db_path)


def _document_from_review(report, blank_doc: Document, source: str | None) -> Document:
    """Rebuild a Document whose annotations are the reviewer's decisions.

    Works on a deep copy. Ingesting must not reach back into the caller's
    document: this function renames fields to match the sheet and replaces each
    page's annotation list, and doing that to the caller's object would silently
    rewrite the parse they are still holding - and destroy the annotations
    outright if the document had any.
    """
    import copy

    from .models import Document as Doc, Form, HUMAN_APPROVED, HUMAN_REJECTED

    doc = Doc(path=source or f"{blank_doc.path}#reviewed",
              page_count=blank_doc.page_count,
              metadata={"origin": "approved staging workbook",
                        "source_pdf": blank_doc.path})
    doc.pages = copy.deepcopy(blank_doc.pages)
    doc.forms = [Form(name=f.name, pages=list(f.pages), domain=f.domain,
                      source=f.source, confidence=f.confidence,
                      evidence=list(f.evidence),
                      continuation_pages=list(f.continuation_pages))
                 for f in blank_doc.forms]

    by_id = {f.id: f for f in doc.iter_fields()}
    rows = [(r, HUMAN_APPROVED) for r in report.approved()]
    rows += [(r, HUMAN_REJECTED) for r in report.rejected()]

    for page in doc.pages:
        page.annotations = []
    for i, (row, trust) in enumerate(rows):
        fld = by_id.get(row.row_id)
        page = doc.page(fld.page) if fld else None
        if not (fld and page):
            continue
        _apply_form_name(doc, fld, row)
        text = (row.text_to_draw if trust == HUMAN_APPROVED
                else row.suggested_annotation or row.suggested_variable)
        annot = _annotation_from_row(row, fld, page, text, i)
        page.annotations.append(annot)
        doc.links.append(_link_from_row(row, fld, annot, trust))
    return doc


def _apply_form_name(doc: Document, fld, row) -> None:
    """Believe the sheet's form name over the blank CRF's guess at it."""
    if not row.form_name or normalize(row.form_name) == normalize(fld.form_name):
        return
    fld.form_name = row.form_name
    if doc.form(row.form_name) is None:
        from .models import Form
        doc.forms.append(Form(name=row.form_name, pages=[fld.page],
                              source="staging workbook", confidence=1.0,
                              evidence=["form named by the reviewer"]))


def _annotation_from_row(row, fld, page, text: str, index: int):
    """A synthetic Annotation standing for what the reviewer decided.

    Placed where the writer would draw it, so the geometry the next study learns
    its house style from is the geometry that was actually used.
    """
    from .models import Annotation, BBox
    from .annotations import classify
    parsed = classify(text)
    w, h = page.width or 1.0, page.height or 1.0
    x0 = fld.bbox.x1 + (row.geometry.get("offset_x_pct") or 0.0) * w
    cy = fld.bbox.cy + (row.geometry.get("offset_y_pct") or 0.0) * h
    size = row.font_size or 8.0
    box = BBox(round(x0, 2), round(cy - size, 2),
               round(x0 + max(len(text), 1) * size * 0.55, 2), round(cy + size, 2))
    return Annotation(
        page=fld.page, text=text, bbox=box, subtype="FreeText",
        id=f"p{fld.page}r{index}", form_name=row.form_name or fld.form_name,
        annot_type=row.annot_type or parsed.annot_type,
        parsed=dict(parsed.parsed) or ({"variable": row.variable} if row.variable else {}),
        type_confidence=LEARNED_CONFIDENCE, type_evidence=["approved by a reviewer"],
        parts=[parsed], text_color=row.text_color, font_name=row.font_name,
        font_size=row.font_size)


def _link_from_row(row, fld, annot, trust: str):
    from .models import HUMAN_REJECTED, Link
    return Link(field_id=fld.id, annotation_id=annot.id, page=fld.page,
                link_score=LEARNED_CONFIDENCE, trust=trust,
                rejected=trust == HUMAN_REJECTED,
                evidence=[f"{trust.lower().replace('_', ' ')} in the staging workbook"]
                + ([f"pre-fill had suggested {row.suggested_annotation!r} "
                    f"via {row.match_tier}"] if row.suggested_annotation else []))
