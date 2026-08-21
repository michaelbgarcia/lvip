"""Phase 10 - writing annotations onto the PDF.

The last box of the workflow, and the only one that produces a file someone
submits. It consumes exactly the rows the importer approved, and nothing else:
`ImportedRow.ready` means validated *and* signed off by a human, and a row that
is one but not the other is skipped with a reason rather than drawn hopefully.

Placement is arithmetic, not judgement. The staging workbook carries the field's
box as page fractions and the house style's offsets as page fractions, so a
position is recovered by multiplying - no model, no guessing, and identical
results on every run. What the arithmetic cannot decide, it reports:

* **Running off the page.** A long annotation placed to the right of a label near
  the right margin will not fit. It is pulled back inside the page and the row
  records that it was moved, because an annotation half off the page is worse
  than one a reviewer was told about.
* **Two annotations in the same place.** Fields on adjacent rows with tall
  markup collide. The later one is nudged down and says so. Silently overlapping
  text is the failure a reviewer would only find by eye, on page 40.

Nothing is written in place. The source PDF is opened, annotated and saved to a
new path, so a bad run costs nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Iterable

import pymupdf

from .importer import ImportedRow
from .models import BBox
from .template import ABOVE, BELOW, LEFT_OF, OVERLAPS, RIGHT_OF

# --- tunables --------------------------------------------------------------
PAD_X, PAD_Y = 4.0, 2.0     # breathing room around the drawn text
MIN_GAP_PT = 2.0            # separation enforced between two annotations
EDGE_PT = 2.0               # closest an annotation may sit to the page edge
LINE_RATIO = 1.35           # box height as a multiple of font size
DEFAULT_SIZE = 8.0
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


@dataclass
class WriteReport:
    path: str
    placements: list[Placement] = dc_field(default_factory=list)
    skipped: list[tuple[str, str]] = dc_field(default_factory=list)

    @property
    def adjusted(self) -> list[Placement]:
        return [p for p in self.placements if p.adjustments]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "written": len(self.placements),
            "skipped": len(self.skipped),
            "adjusted": len(self.adjusted),
            "adjustments": [f"{p.row_id}: {'; '.join(p.adjustments)}"
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

    pdf = pymupdf.open(source)
    try:
        placed: dict[int, list[BBox]] = {}
        # Top-to-bottom within a page, so a nudge always pushes into space that
        # has not been claimed yet and collision resolution stays deterministic.
        for row in sorted(ready, key=lambda r: (_page_of(r), _cy_of(r), r.row_id)):
            page_no = _page_of(row)
            if not 1 <= page_no <= pdf.page_count:
                report.skipped.append((row.row_id, f"page {page_no} is not in {source.name}"))
                continue
            page = pdf[page_no - 1]
            placement = _place(row, page, placed.setdefault(page_no, []))
            _draw(page, row, placement, author)
            placed[page_no].append(placement.rect)
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
            skipped.append((r.row_id, "blocked by validation"))
        else:
            skipped.append((r.row_id, f"status is {r.status or 'blank'}, not APPROVED"))
    return ready, skipped


def _page_of(row: ImportedRow) -> int:
    return int(row.geometry.get("page") or 0)


def _cy_of(row: ImportedRow) -> float:
    return float(row.geometry.get("rel_y_pct") or 0.0)


# --- geometry --------------------------------------------------------------
def _place(row: ImportedRow, page: pymupdf.Page, taken: list[BBox]) -> Placement:
    """Turn page fractions back into points, then keep the box on the page."""
    g = row.geometry
    w = float(g.get("page_width") or page.rect.width)
    h = float(g.get("page_height") or page.rect.height)
    field = _field_box(g, w, h)

    size = row.font_size or DEFAULT_SIZE
    font, substituted = _font_for(row.font_name)
    text_w = pymupdf.get_text_length(row.text_to_draw, fontname=font, fontsize=size)
    box_w, box_h = text_w + 2 * PAD_X, size * LINE_RATIO + 2 * PAD_Y

    rect = _anchor(row, field, g, w, h, box_w, box_h)
    adjustments = ["font substituted for " + row.font_name] if substituted else []
    rect, moved = _clamp(rect, w, h)
    adjustments += moved
    rect, nudged = _avoid(rect, taken, h)
    adjustments += nudged
    return Placement(row_id=row.row_id, page=_page_of(row), text=row.text_to_draw,
                     rect=rect, adjustments=adjustments)


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
    )
    # /Contents is what the parser reads first, so a written annotation is
    # re-readable by the same pipeline that produced it.
    annot.set_info(content=placement.text, title=author)
    annot.update()
