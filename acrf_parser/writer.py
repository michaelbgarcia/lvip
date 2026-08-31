"""Phase 10 - writing annotations onto the PDF.

The last box of the workflow, and the only one that produces a file someone
submits. It consumes exactly the rows the importer approved, and nothing else:
`ImportedRow.ready` means validated *and* signed off by a human, and a row that
is one but not the other is skipped with a reason rather than drawn hopefully.

Placement is arithmetic, not judgement. The staging workbook carries the field's
box as page fractions and the house style's offsets as page fractions, so a
position is recovered by multiplying - no model, no guessing, and identical
results on every run. That arithmetic gives an *anchor*, not a final answer: the
house style says "12pt right of the label", and on a real CRF that spot is
frequently already occupied. So the anchor is the start of a search, not the
result of one, and everything the search has to move is reported:

* **The form's own text.** A blank CRF is dense - unit hints, response labels,
  instructions in parentheses - and markup dropped on top of a printed sentence
  is unreadable for exactly the reader it was written for. Every word already on
  the page is an obstacle, so an annotation is placed in the nearest genuinely
  empty space instead: along its own row first, then the rows next to it.
* **Running off the page.** A long annotation placed to the right of a label near
  the right margin will not fit. It is pulled back inside the page and the row
  records that it was moved, because an annotation half off the page is worse
  than one a reviewer was told about.
* **Two annotations in the same place.** Fields on adjacent rows with tall
  markup collide. The later one is moved clear and says so. Silently overlapping
  text is the failure a reviewer would only find by eye, on page 40.
* **A field's own annotations.** One field commonly carries several statements -
  DSTERM, DSDECOD=INFORMED CONSENT OBTAINED, RFICDTC, DSSTDTC on a single consent
  date. They arrive as separate rows sharing a `row_id`, and they are drawn in
  `annot_seq` order, each starting from where the last one ended rather than from
  the field again. So a set reads left to right along the row it belongs to, in
  the order the reviewer put them in, and only spills onto a neighbouring row
  when this one genuinely runs out of space.
* **The same statement twice on one row.** A question and its response options
  are separate fields, and pre-fill maps them to the same variable - so "Sex" and
  its Male/Female options all arrive approved as SEX and the page ends up with
  SEX printed twice. One CRF row carries one statement: the first occurrence is
  drawn and the rest are skipped with the row they duplicate named. Repeats down
  a *column* are left alone - a log form's repeating rows are separate fields
  that legitimately share a variable.

Nothing is written in place. The source PDF is opened, annotated and saved to a
new path, so a bad run costs nothing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Iterable

import pymupdf

from .importer import ImportedRow
from .models import BBox
from .normalize import statement_key
from .template import ABOVE, BELOW, LEFT_OF, OVERLAPS, RIGHT_OF

# --- tunables --------------------------------------------------------------
PAD_X, PAD_Y = 4.0, 2.0     # breathing room around the drawn text
MIN_GAP_PT = 2.0            # separation enforced between two annotations
EDGE_PT = 2.0               # closest an annotation may sit to the page edge
LINE_RATIO = 1.35           # box height as a multiple of font size
OBSTACLE_PAD = 1.5          # clearance kept either side of printed page content
ROW_OVERLAP_PT = 0.5        # vertical overlap that counts as "in the way"
ROW_STEP_RATIO = 1.1        # vertical search step, as a multiple of box height
MAX_ROW_STEPS = 4           # rows searched either side of the field's own row
DUP_ROW_TOL = 12.0          # two fields this close vertically are one CRF row
SIBLING_GAP_PT = 6.0        # gap between two annotations on the same field
DEFAULT_SIZE = 8.0
# Every annotation is drawn as a box with a black border; only the background
# comes from the study's own history. A border makes the markup a discrete
# object on a dense page - a reviewer can see where one statement ends and the
# printed form begins - and black is the one colour that reads over any fill.
BORDER_COLOR = b"0 0 0 RG"      # PDF stroking colour, black
BORDER_WIDTH = 0.75

# PyMuPDF strokes a FreeText's border in the *text* colour and offers no way to
# say otherwise (`border_color` is rejected unless the annotation is rich text,
# which would take the text out of the base-14 fonts the placement arithmetic
# measures with). The generated appearance stream says it in one operator, so
# the border colour is set there: an uppercase RG is the stroking colour, and in
# a FreeText appearance the only thing stroked is the box. Text uses lowercase
# `rg` and is untouched.
_STROKE_RGB = re.compile(rb"[\d.]+\s+[\d.]+\s+[\d.]+\s+RG")
DEFAULT_AUTHOR = "acrf_parser"

# PyMuPDF only ships the base-14 fonts for annotation appearances. A study whose
# house font is something else still gets a correct *box*; only the glyphs differ,
# and the substitution is recorded rather than passed off as the real thing.
BASE14 = {
    "helv": "helv", "helvetica": "helv", "arial": "helv",
    "hebo": "hebo", "helvetica-bold": "hebo", "arial-bold": "hebo",
    "cour": "cour", "courier": "cour", "tiro": "tiro",
    "times": "tiro", "times-roman": "tiro", "tibo": "tibo", "symb": "symb",
}


@dataclass
class Placement:
    """Where one annotation ended up, and whether it had to be moved."""
    row_id: str
    page: int
    text: str
    rect: BBox
    adjustments: list[str] = dc_field(default_factory=list)
    annot_seq: int = 1

    @property
    def key(self) -> tuple[str, int]:
        return (self.row_id, self.annot_seq)

    @property
    def label(self) -> str:
        """How this annotation is named in the report."""
        return self.row_id if self.annot_seq <= 1 else f"{self.row_id}#{self.annot_seq}"


@dataclass
class WriteReport:
    path: str
    placements: list[Placement] = dc_field(default_factory=list)
    skipped: list[tuple[str, str]] = dc_field(default_factory=list)

    @property
    def adjusted(self) -> list[Placement]:
        return [p for p in self.placements if p.adjustments]

    @property
    def multi_annotation_fields(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for p in self.placements:
            counts[p.row_id] = counts.get(p.row_id, 0) + 1
        return {k: v for k, v in sorted(counts.items()) if v > 1}

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "written": len(self.placements),
            "fields": len({p.row_id for p in self.placements}),
            "multi_annotation_fields": len(self.multi_annotation_fields),
            "skipped": len(self.skipped),
            "adjusted": len(self.adjusted),
            "adjustments": [f"{p.label}: {'; '.join(p.adjustments)}"
                            for p in self.adjusted],
            "skipped_rows": [f"{rid}: {why}" for rid, why in self.skipped],
        }


def write_annotations(source: str | Path, rows: Iterable[ImportedRow],
                      out_path: str | Path,
                      author: str = DEFAULT_AUTHOR) -> WriteReport:
    """Draw the approved rows onto a copy of `source`."""
    source, out_path = Path(source), Path(out_path)
    report = WriteReport(path=str(out_path))
    ready, report.skipped = _partition(rows)

    # De-duplicated left-to-right, so the annotation a row keeps is the one on
    # its leftmost field - the question, not one of its response options, which
    # sit a few points higher and would otherwise win on y alone.
    ready.sort(key=lambda r: (_page_of(r), _cx_of(r), _cy_of(r), r.row_id, r.annot_seq))
    ready, duplicates = _dedupe(ready)
    report.skipped += duplicates
    # Drawn top-to-bottom, so a move always pushes into space that has not been
    # claimed yet and collision resolution stays deterministic. Within one field,
    # annot_seq is the order - the reviewer's order, and the order the set will
    # be read in.
    ready.sort(key=lambda r: (_page_of(r), _cy_of(r), _cx_of(r), r.row_id, r.annot_seq))

    pdf = pymupdf.open(source)
    try:
        placed: dict[int, list[BBox]] = {}
        obstacles: dict[int, list[BBox]] = {}
        siblings: dict[str, BBox] = {}     # field -> where its last annotation ended
        for row in ready:
            page_no = _page_of(row)
            if not 1 <= page_no <= pdf.page_count:
                report.skipped.append((_label(row), f"page {page_no} is not in {source.name}"))
                continue
            page = pdf[page_no - 1]
            if page_no not in obstacles:
                obstacles[page_no] = _obstacles(page)
            placement = _place(row, page, placed.setdefault(page_no, []),
                               obstacles[page_no], siblings.get(row.row_id))
            _draw(page, row, placement, author)
            placed[page_no].append(placement.rect)
            siblings[row.row_id] = placement.rect
            report.placements.append(placement)
        pdf.save(out_path)
    finally:
        pdf.close()
    return report


def _partition(rows: Iterable[ImportedRow]) -> tuple[list[ImportedRow], list[tuple[str, str]]]:
    """Only `ready` rows are drawn: validated *and* approved, never one alone."""
    ready, skipped = [], []
    for r in rows:
        if r.ready:
            ready.append(r)
        elif not r.ok:
            skipped.append((_label(r), "blocked by validation"))
        else:
            skipped.append((_label(r), f"status is {r.status or 'blank'}, not APPROVED"))
    return ready, skipped


def _label(row: ImportedRow) -> str:
    """How one annotation is named in the report: bare row_id for a field with
    one, `p5f3#2` for the second annotation of a field with several."""
    return row.row_id if row.annot_seq <= 1 else f"{row.row_id}#{row.annot_seq}"


def _page_of(row: ImportedRow) -> int:
    return int(row.geometry.get("page") or 0)


def _cy_of(row: ImportedRow) -> float:
    return float(row.geometry.get("rel_y_pct") or 0.0)


def _cx_of(row: ImportedRow) -> float:
    return float(row.geometry.get("rel_x_pct") or 0.0)


# --- duplicate statements --------------------------------------------------
def _dedupe(rows: list[ImportedRow]) -> tuple[list[ImportedRow], list[tuple[str, str]]]:
    """Drop a statement already placed on the same row of the form.

    Pre-fill answers the question field and each of its response options, so an
    approved sheet routinely says SEX four times across one row of the CRF. The
    row is the unit: fields side by side, vertically level, saying the same
    thing. Nothing stacked is touched - a repeat down a column is a different
    field of a log form, and it needs its own markup.

    Two annotations of the *same* field are never compared. They share a box, so
    the geometric test cannot separate them, and they are there on purpose: the
    reviewer asked for both. A field whose own rows repeat a statement is the
    importer's DUPLICATE_STATEMENT warning, decided by a person, not silently
    dropped here.
    """
    kept: list[ImportedRow] = []
    dropped: list[tuple[str, str]] = []
    seen: dict[tuple[int, tuple[str, ...]], list[tuple[str, BBox]]] = {}
    for row in rows:
        key = statement_key(row.text_to_draw)
        box = _row_box(row)
        if not key or box is None:       # nothing to compare on; never guess
            kept.append(row)
            continue
        page_key = (_page_of(row), key)
        prior = next((label for rid, label, b in seen.get(page_key, [])
                      if rid != row.row_id and _same_row(box, b)), None)
        if prior:
            dropped.append((_label(row),
                            f"same statement as {prior}, already placed on this row"))
            continue
        seen.setdefault(page_key, []).append((row.row_id, _label(row), box))
        kept.append(row)
    return kept, dropped


def _row_box(row: ImportedRow) -> BBox | None:
    g = row.geometry
    try:
        w = float(g["page_width"]) or 1.0
        h = float(g["page_height"]) or 1.0
        return _field_box(g, w, h)
    except (KeyError, TypeError, ValueError):
        return None


def _same_row(a: BBox, b: BBox) -> bool:
    """Side by side on one row: vertically level, horizontally apart."""
    level = a.v_overlap(b) >= 0.3 or abs(a.cy - b.cy) <= DUP_ROW_TOL
    return level and a.h_overlap(b) <= 0


# --- geometry --------------------------------------------------------------
def _place(row: ImportedRow, page: pymupdf.Page, taken: list[BBox],
           obstacles: list[BBox], prior_sibling: BBox | None = None) -> Placement:
    """Turn page fractions back into points, then find clear space for the box."""
    g = row.geometry
    w = float(g.get("page_width") or page.rect.width)
    h = float(g.get("page_height") or page.rect.height)
    field = _field_box(g, w, h)

    size = row.font_size or DEFAULT_SIZE
    font, substituted = _font_for(row.font_name)
    text_w = pymupdf.get_text_length(row.text_to_draw, fontname=font, fontsize=size)
    box_w, box_h = text_w + 2 * PAD_X, size * LINE_RATIO + 2 * PAD_Y

    anchor = _anchor(row, field, g, w, h, box_w, box_h)
    if prior_sibling is not None:
        anchor = _after_sibling(anchor, prior_sibling, row.placement, box_w, box_h)
    adjustments = ["font substituted for " + row.font_name] if substituted else []
    rect, moved = _fit(anchor, taken, obstacles, w, h, box_w)
    adjustments += moved
    return Placement(row_id=row.row_id, annot_seq=row.annot_seq, page=_page_of(row),
                     text=row.text_to_draw, rect=rect, adjustments=adjustments)


def _after_sibling(anchor: BBox, prior: BBox, placement: str,
                   box_w: float, box_h: float) -> BBox:
    """Re-anchor an annotation onto the end of its field's previous one.

    Without this every annotation of a field starts at the same point, the first
    one takes it, and the rest are scattered by collision search into whatever
    gaps happen to be free - reading order lost, and the second statement of a
    field as likely to land two rows down as beside the first. Chaining instead
    keeps the set together and in `annot_seq` order, and `_fit` still has the
    last word if the chained position is occupied too.
    """
    if placement == LEFT_OF:
        x0 = prior.x0 - SIBLING_GAP_PT - box_w
        return BBox(x0, prior.y0, x0 + box_w, prior.y0 + box_h)
    if placement in (ABOVE,):
        y1 = prior.y0 - SIBLING_GAP_PT
        return BBox(prior.x0, y1 - box_h, prior.x0 + box_w, y1)
    if placement in (BELOW,):
        y0 = prior.y1 + SIBLING_GAP_PT
        return BBox(prior.x0, y0, prior.x0 + box_w, y0 + box_h)
    # RIGHT_OF and OVERLAPS both read left to right, which is how aCRF markup is
    # drawn and how a reviewer will read the set back.
    x0 = prior.x1 + SIBLING_GAP_PT
    return BBox(x0, prior.y0, x0 + box_w, prior.y0 + box_h)


def _field_box(g: dict, w: float, h: float) -> BBox:
    """`BBox.relative` stores centre and size as fractions; invert that."""
    cx, cy = float(g["rel_x_pct"]) * w, float(g["rel_y_pct"]) * h
    bw, bh = float(g["rel_w_pct"]) * w, float(g["rel_h_pct"]) * h
    return BBox(cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2)


def _anchor(row: ImportedRow, field: BBox, g: dict, w: float, h: float,
            box_w: float, box_h: float) -> BBox:
    """Position the box relative to its field, per the house style's placement."""
    off_x = float(g.get("offset_x_pct") or 0.0) * w
    off_y = float(g.get("offset_y_pct") or 0.0) * h
    placement = row.placement or RIGHT_OF

    if placement == LEFT_OF:
        x0 = field.x0 - abs(off_x) - box_w
        cy = field.cy + off_y
    elif placement == ABOVE:
        x0, cy = field.x0, field.y0 - abs(off_y) - box_h / 2
    elif placement == BELOW:
        x0, cy = field.x0, field.y1 + abs(off_y) + box_h / 2
    elif placement == OVERLAPS:
        x0, cy = field.cx - box_w / 2, field.cy
    else:                                   # RIGHT_OF, the aCRF default
        x0, cy = field.x1 + off_x, field.cy + off_y
    return BBox(x0, cy - box_h / 2, x0 + box_w, cy + box_h / 2)


def _obstacles(page: pymupdf.Page) -> list[BBox]:
    """Everything already on the page that markup must not be printed over.

    Words rather than blocks or lines: a block spans a whole paragraph including
    the whitespace at the end of its last line, and that whitespace is often the
    only place an annotation fits. Existing annotations and form widgets count
    too - re-annotating an already-marked CRF must not bury the earlier markup.
    """
    boxes = [BBox.of(word[:4]) for word in page.get_text("words")]
    boxes += [BBox.of(a.rect) for a in (page.annots() or ())]
    boxes += [BBox.of(wdg.rect) for wdg in (page.widgets() or ())]
    return [BBox(b.x0 - OBSTACLE_PAD, b.y0, b.x1 + OBSTACLE_PAD, b.y1)
            for b in boxes if b.area() > 0]


def _fit(anchor: BBox, taken: list[BBox], obstacles: list[BBox],
         w: float, h: float, box_w: float) -> tuple[BBox, list[str]]:
    """Place the box at its anchor, or in the nearest clear space to it.

    The search is ordered by how far it moves the annotation from where the
    house style put it: the field's own row first, then the rows either side,
    and within a row the free gap nearest the anchor. An annotation two rows
    from its field is a placement a reviewer has to check; one printed on top of
    the question is one they cannot read at all.
    """
    blockers = obstacles + taken
    rect, notes = _clamp(anchor, w, h)
    if not _blocked(rect, blockers):
        return rect, notes
    cause = ("another annotation" if _blocked(rect, taken) else "the form text")

    step = rect.height * ROW_STEP_RATIO
    for dy in _row_offsets(step):
        band = BBox(rect.x0, rect.y0 + dy, rect.x1, rect.y1 + dy)
        if band.y0 < EDGE_PT or band.y1 > h - EDGE_PT:
            continue
        for x0 in _free_starts(band, blockers, box_w, w):
            cand = BBox(x0, band.y0, x0 + box_w, band.y1)
            if not _blocked(cand, blockers):
                cand, extra = _clamp(cand, w, h)
                return cand, notes + extra + [_move_note(anchor, cand, cause)]
    # The page has no gap this box fits in. Two annotations on top of each other
    # is the one outcome with no reader at all, so that is what is resolved, and
    # the row says plainly what the markup landed on.
    rect, nudged = _avoid(rect, taken, h)
    return rect, notes + nudged + [f"no clear space on the page; overlaps {cause}"]


def _row_offsets(step: float) -> list[float]:
    """Vertical search order: this row, then alternately below and above it."""
    offsets = [0.0]
    for i in range(1, MAX_ROW_STEPS + 1):
        offsets += [i * step, -i * step]
    return offsets


def _free_starts(band: BBox, blockers: list[BBox], box_w: float,
                 page_w: float) -> list[float]:
    """Left edges of the gaps in `band` wide enough to hold the box.

    One candidate per gap - the position in it closest to where the annotation
    wanted to be - so a row with four gaps costs four tests rather than a sweep.
    """
    occupied = sorted((b.x0, b.x1) for b in blockers
                      if min(b.y1, band.y1) - max(b.y0, band.y0) > ROW_OVERLAP_PT)
    starts: list[float] = []
    cursor = EDGE_PT
    for x0, x1 in occupied + [(page_w - EDGE_PT, page_w)]:
        if x0 - cursor >= box_w:
            starts.append(min(max(band.x0, cursor), x0 - box_w))
        cursor = max(cursor, x1)
    # Nearest first; a tie goes rightwards, the direction aCRF markup reads in.
    return sorted(starts, key=lambda x: (round(abs(x - band.x0), 2), x < band.x0))


def _blocked(rect: BBox, boxes: list[BBox]) -> bool:
    return any(_overlaps(rect, b) for b in boxes)


def _overlaps(a: BBox, b: BBox) -> bool:
    """True area overlap, so boxes that merely touch edges are not a collision."""
    return (min(a.x1, b.x1) - max(a.x0, b.x0) > 0.1
            and min(a.y1, b.y1) - max(a.y0, b.y0) > 0.1)


def _move_note(anchor: BBox, rect: BBox, cause: str) -> str:
    dx, dy = rect.x0 - anchor.x0, rect.cy - anchor.cy
    moves = []
    if abs(dx) >= 0.5:
        moves.append(f"{abs(dx):.0f}pt {'right' if dx > 0 else 'left'}")
    if abs(dy) >= 0.5:
        moves.append(f"{abs(dy):.0f}pt {'down' if dy > 0 else 'up'}")
    return f"moved {' and '.join(moves) or 'slightly'} to clear {cause}"


def _clamp(rect: BBox, w: float, h: float) -> tuple[BBox, list[str]]:
    """Pull a box back onto the page, preserving its size where it can."""
    notes: list[str] = []
    x0, y0, x1, y1 = rect.as_tuple()
    if x1 > w - EDGE_PT:
        shift = x1 - (w - EDGE_PT)
        x0, x1 = x0 - shift, x1 - shift
        notes.append(f"moved {shift:.0f}pt left to stay on the page")
    if x0 < EDGE_PT:
        # Too wide to fit at all: keep the left edge and accept a narrower box
        # rather than pushing text off the right side where it cannot be read.
        x0, x1 = EDGE_PT, max(EDGE_PT + 1, min(x1 + (EDGE_PT - x0), w - EDGE_PT))
        notes.append("truncated to the page width")
    if y1 > h - EDGE_PT:
        shift = y1 - (h - EDGE_PT)
        y0, y1 = y0 - shift, y1 - shift
        notes.append(f"moved {shift:.0f}pt up to stay on the page")
    if y0 < EDGE_PT:
        y1 += EDGE_PT - y0
        y0 = EDGE_PT
        notes.append("moved down to stay on the page")
    return BBox(round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)), notes


def _avoid(rect: BBox, taken: list[BBox], page_h: float) -> tuple[BBox, list[str]]:
    """Push a box below anything already drawn that it would sit on top of."""
    notes: list[str] = []
    moved = 0.0
    for _ in range(len(taken) + 1):
        hit = next((t for t in taken
                    if rect.h_overlap(t) > 0 and rect.v_overlap(t) > 0), None)
        if hit is None:
            break
        drop = hit.y1 + MIN_GAP_PT - rect.y0
        rect = BBox(rect.x0, rect.y0 + drop, rect.x1, rect.y1 + drop)
        moved += drop
    if moved:
        rect, _ = _clamp(rect, rect.x1 + EDGE_PT * 2, page_h)
        notes.append(f"nudged {moved:.0f}pt down to clear another annotation")
    return rect, notes


def _font_for(name: str) -> tuple[str, bool]:
    """Map a study's font onto a base-14 face, saying when it was a substitution."""
    key = (name or "").strip().lower()
    if key in BASE14:
        return BASE14[key], False
    return "helv", bool(key)


# --- drawing ---------------------------------------------------------------
def _draw(page: pymupdf.Page, row: ImportedRow, placement: Placement,
          author: str) -> None:
    """One FreeText annotation, styled from the sheet's own columns."""
    font, _ = _font_for(row.font_name)
    annot = page.add_freetext_annot(
        pymupdf.Rect(*placement.rect.as_tuple()),
        placement.text,
        fontsize=row.font_size or DEFAULT_SIZE,
        fontname=font,
        text_color=row.text_color or (0, 0, 0),
        fill_color=row.fill_color,          # None = transparent, the PDF default
        border_width=BORDER_WIDTH,
    )
    # /Contents is what the parser reads first, so a written annotation is
    # re-readable by the same pipeline that produced it.
    annot.set_info(content=placement.text, title=author)
    # update() regenerates the appearance stream and drops any styling it is not
    # handed, so the colours are repeated here.
    annot.update(fill_color=row.fill_color, text_color=row.text_color or (0, 0, 0))
    _blacken_border(page.parent, annot)


def _blacken_border(pdf: pymupdf.Document, annot: pymupdf.Annot) -> None:
    """Repaint the box outline black in the annotation's appearance stream.

    A no-op if the stream carries no stroking colour - MuPDF's default stroke is
    already black, so the border comes out right either way.
    """
    try:
        kind, value = pdf.xref_get_key(annot.xref, "AP/N")
        if kind != "xref":
            return
        xref = int(value.split()[0])
        stream = pdf.xref_stream(xref)
        patched = _STROKE_RGB.sub(BORDER_COLOR, stream)
        if patched != stream:
            pdf.update_stream(xref, patched)
    except Exception:
        pass                    # a red border is a cosmetic loss, not a failed write
