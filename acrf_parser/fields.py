"""Phase 3 - field extraction.

Turns the layout pass's `TextGroup`s into `Field` records: the left half of the
`(form_name, field_text)` primary key. A group is already one logical label
(wrapped lines rejoined), so the work here is not "find the text" - it is
deciding *which* groups are data-entry items, cleaning the label down to
something stable enough to key on, and hanging each field's response options and
controls off it.

Two things are deliberately kept apart:

* `raw_text` - exactly what the page prints, so a template can be checked
  against the PDF by eye.
* `text` - item numbering and the trailing colon removed. "1. Have
  hypochondroplasia..." and "Have hypochondroplasia..." are the same field, and
  renumbering a criteria list must not invalidate the key.

Response options do not become fields. On a two-column CRF the options
("Original", "Amendment 1", ...) are one question's codelist, so they attach to
the question that owns their row band. They keep their own bboxes: an option can
carry markup of its own, and Phase 6 needs somewhere to link it.
"""
from __future__ import annotations

import re

from . import forms, layout
from .models import Control, Field, Page, ResponseOption, TextGroup
from .normalize import normalize

# --- tunables --------------------------------------------------------------
ROW_OVERLAP = layout.ROW_OVERLAP
OPTION_ROW_TOL = 4.0       # an option this far above a question still belongs to it
MIN_LABEL_CHARS = 1

_ITEM_NUMBER = re.compile(r"^\s*(\d+[.)]|\(\d+\)|[a-z][.)]|[ivxl]+[.)]|[-•*])\s+", re.I)
_TRAILING = re.compile(r"[\s:?*]+$")
_LEADING = re.compile(r"^[\s\-–•*]+")


def extract_fields(pages: list[Page]) -> list[Field]:
    """Extract every page's fields, in page order."""
    out: list[Field] = []
    for page in pages:
        page.fields = extract_page_fields(page)
        out.extend(page.fields)
    return out


def extract_page_fields(page: Page) -> list[Field]:
    """Fields for one page. Form name comes from Phase 2 and is copied onto each."""
    groups = [g for g in page.groups if not g.from_annotation_only() and g.text.strip()]
    title = normalize(page.form_name)
    candidates = [g for g in groups if _is_field(g, title)]
    candidates.sort(key=lambda g: (g.bbox.y0, g.bbox.x0))
    options = [g for g in groups if g.role == layout.RESPONSE_OPTION]
    # A section header that only repeats the form title is not a section: every
    # field on the page would carry it, and form_name already says it.
    sections = sorted((g for g in groups if g.role == layout.SECTION_HEADER
                       and normalize(forms.clean_title(g.text)) != title),
                      key=lambda g: g.bbox.y0)

    fields: list[Field] = []
    for i, g in enumerate(candidates):
        item, label = _split_label(g.text)
        controls = _controls_for(g, page, candidates)
        fields.append(Field(
            id=f"p{page.number}f{i}",
            form_name=page.form_name,
            page=page.number,
            text=label,
            raw_text=g.text,
            bbox=g.bbox,
            group_id=page.groups.index(g),
            column=g.column,
            role=g.role,
            section=_section_for(g, sections),
            item_number=item,
            control_kinds=sorted({c.kind for c in controls}),
            confidence=_confidence(g, controls),
            evidence=list(g.role_evidence),
        ))
    # Filter before attaching, or an option can be hung on a field that is then
    # dropped and vanish with it.
    fields = [f for f in fields if len(f.text) >= MIN_LABEL_CHARS]
    _attach_options(fields, options, page)
    return fields


# --- which groups are fields ----------------------------------------------
def _is_field(g: TextGroup, form_key: str) -> bool:
    """Questions are fields. Unclassified body text is a low-confidence field.

    Nothing in the body is discarded outright - the layout pass already scored
    every group, and a wrong QUESTION call should cost confidence, not the row.
    Headers, footers and options are the only roles excluded, plus the group the
    form title was read from (it names the form, it is not a field on it).
    """
    if g.region != layout.BODY:
        return False
    if g.role in (layout.RESPONSE_OPTION, layout.PAGE_HEADER, layout.FOOTER_ROLE):
        return False
    if g.role == layout.SECTION_HEADER:
        return False
    return normalize(g.text) != form_key


def _confidence(g: TextGroup, controls: list[Control]) -> float:
    """Layout's role confidence, plus a bump for having a response control."""
    base = g.role_confidence if g.role == layout.QUESTION else 0.2
    return round(min(1.0, base + (0.2 if controls else 0.0)), 2)


# --- label cleanup ---------------------------------------------------------
def _split_label(text: str) -> tuple[str, str]:
    """Peel the item number off the front and the colon off the back."""
    m = _ITEM_NUMBER.match(text)
    item = m.group(1) if m else ""
    label = text[m.end():] if m else text
    return item, _TRAILING.sub("", _LEADING.sub("", label)).strip()


# --- options and controls --------------------------------------------------
def _attach_options(fields: list[Field], options: list[TextGroup], page: Page) -> None:
    """Give each response option to the question that owns its row band.

    Row overlap first (the usual two-column CRF: options sit level with their
    question), then fall back to the nearest question at or above the option -
    which is what a vertical codelist running past the end of its label needs.
    """
    if not fields or not options:
        return
    by_id = {f.id: f for f in fields}
    for g in sorted(options, key=lambda g: g.bbox.y0):
        overlapping = [f for f in fields
                       if f.column != g.column and f.bbox.v_overlap(g.bbox) >= ROW_OVERLAP]
        if overlapping:
            owner = max(overlapping, key=lambda f: f.bbox.v_overlap(g.bbox))
        else:
            above = [f for f in fields
                     if f.column != g.column and f.bbox.y0 <= g.bbox.cy + OPTION_ROW_TOL]
            if not above:
                continue
            owner = max(above, key=lambda f: f.bbox.y0)
        by_id[owner.id].options.append(
            ResponseOption(text=g.text, bbox=g.bbox, group_id=page.groups.index(g)))


def _controls_for(g: TextGroup, page: Page, others: list[TextGroup]) -> list[Control]:
    """The controls that make up this field's answer area.

    Row-aware rather than distance-based. A CRF puts its answer boxes on a fixed
    left edge while labels vary in length, so "Age" sits 100pt from its box and
    "Date of birth" 66pt from the identical box - any fixed radius picks one and
    drops the other. What actually disqualifies a control is another field's
    label standing between it and this one, and the column gutter: the
    Disposition question must not claim the radio circles in the response band,
    which belong to its options.
    """
    band = next((b for b in page.column_bands if b.index == g.column), None)
    out = []
    for c in page.controls:
        if c.bbox.v_overlap(g.bbox) < ROW_OVERLAP or c.bbox.x1 < g.bbox.x0:
            continue
        if band is not None and len(page.column_bands) > 1 and not band.contains(c.bbox, 0.3):
            continue
        if _label_between(g, c, others):
            continue
        out.append(c)
    return out


def _label_between(g: TextGroup, c: Control, others: list[TextGroup]) -> bool:
    """Another field's label sits on this row, between the label and the control."""
    return any(o is not g and o.bbox.v_overlap(g.bbox) >= ROW_OVERLAP
               and g.bbox.x1 <= o.bbox.x0 < c.bbox.x0 for o in others)


def _section_for(g: TextGroup, sections: list[TextGroup]) -> str:
    """Nearest section header above the group, if any."""
    above = [s for s in sections if s.bbox.y1 <= g.bbox.y0]
    return above[-1].text if above else ""


# --- reporting -------------------------------------------------------------
def summarize_fields(fields: list[Field]) -> dict:
    return {
        "fields": len(fields),
        "with_options": sum(1 for f in fields if f.options),
        "with_controls": sum(1 for f in fields if f.control_kinds),
        "numbered": sum(1 for f in fields if f.item_number),
        "low_confidence": sum(1 for f in fields if f.confidence < 0.5),
    }
