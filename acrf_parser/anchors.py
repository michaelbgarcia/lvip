"""Phase 6b - form-level markup, and where it goes.

Some of an aCRF's markup is not about any question on the page. A real
Disposition page opens with

    DS=Disposition   DM=Demographics   DSSCAT   DSCAT = PROTOCOL MILESTONE

drawn across the top, above the printed form title, before the first field. Two
domain headers, not one - a page routinely carries variables from more than one
domain - and two form-level constants beside them. None of it belongs to a
`Field`, and every mechanism downstream of Phase 3 is keyed on a field id, so
this markup was parsed correctly, classified correctly, and then had nowhere to
live: it never reached the staging workbook and never reached the PDF.

This phase supplies the two missing halves.

**Reading it, on an annotated CRF.** Two deterministic signals, and no
inference beyond them:

* the type. `DOMAIN_HEADER` is form-level by definition - Phase 2 already reads
  it to name the page's form, and Phase 6 already refuses to link it.
* the position. Markup that reached no field *and* sits above the first field on
  the page is form-level: a page's questions start below its header band, so
  markup above all of them is not late-arriving field markup, it is the form's.
  Anything unlinked *among* the fields stays unlinked and stays a finding -
  usually a qualifier whose question is on another page, and guessing at it here
  would bury exactly the case a reviewer needs to see.

`CROSS_REFERENCE` is deliberately excluded even though it is form-level in every
other sense. "See Page 7" is a fact about one document's pagination; learned as
the form's markup it would be proposed for a study where page 7 is a different
form entirely. It is recorded in the evidence and left out.

**Writing it, on a blank CRF.** A blank CRF has no such markup - that is the
whole point - so there is no annotation to key on and nothing on the page to
place it beside. What there is, on every page, is the header band above the
first field, and that is what an anchor is: one per page, a zero-width point at
the left of that band, with an id shaped like a field's. The first form-level
annotation lands on it and the rest chain rightwards, which is the order the
headers are drawn in and the order a reviewer reads them back.
"""
from __future__ import annotations

from . import annotations as ann
from .models import FORM_SCOPE, Annotation, BBox, Document, FormAnchor, Page

# --- tunables --------------------------------------------------------------
BAND_TOP_PT = 3.0        # how far below the page edge the header band starts
BAND_HEIGHT_PT = 13.0    # nominal height of the band, ~ one line of markup
FIELD_CLEARANCE_PT = 2.0  # markup this far above the first field is still above it
DEFAULT_LEFT_PCT = 0.08  # left margin to fall back on when the page has no content

# Form-level by type, before position is even considered. CROSS_REFERENCE is
# form-level too but is not learnable - see the module docstring.
ALWAYS_FORM_LEVEL = frozenset({ann.DOMAIN_HEADER})


def resolve_form_level(doc: Document) -> list[Annotation]:
    """Mark every page's form-level markup and hang an anchor on every page.

    Runs after Phase 6, because "reached no field" is one of the two signals and
    only linking can answer it.
    """
    linked = {l.annotation_id for l in doc.links if not l.rejected}
    out: list[Annotation] = []
    for page in doc.pages:
        out += mark_page(page, linked)
        page.anchor = build_anchor(page)
    return out


def mark_page(page: Page, linked: set[str]) -> list[Annotation]:
    """Set `scope` on the page's annotations; return the form-level ones in draw order."""
    top = _first_field_top(page)
    found: list[Annotation] = []
    for a in page.annotations:
        why = _form_level_reason(a, page, linked, top)
        if not why:
            continue
        a.scope = FORM_SCOPE
        a.scope_evidence = [why]
        found.append(a)
    # Left to right: the order the headers were drawn in, and the order the
    # writer will lay them back down in.
    found.sort(key=lambda a: (a.bbox.x0, a.bbox.y0))
    return found


def _form_level_reason(a: Annotation, page: Page, linked: set[str],
                       first_field_top: float | None) -> str:
    if a.annot_type in ALWAYS_FORM_LEVEL:
        return f"{a.annot_type} markup describes the form, not a field"
    if a.annot_type == ann.CROSS_REFERENCE:
        return ""            # form-level, but not learnable - see the docstring
    if a.id in linked:
        return ""
    if not a.text.strip():
        return ""
    if first_field_top is None:
        # A page with no fields at all: unlinked markup on it has no field it
        # could have belonged to, so the form is the only thing left.
        return "unlinked markup on a page with no fields"
    if a.bbox.y1 <= first_field_top + FIELD_CLEARANCE_PT:
        return "unlinked markup above the first field on the page"
    return ""


def _first_field_top(page: Page) -> float | None:
    return min((f.bbox.y0 for f in page.fields), default=None)


# --- anchors ---------------------------------------------------------------
def build_anchor(page: Page) -> FormAnchor | None:
    """The page's hook for form-level markup, or None if no form claims the page."""
    if not page.form_name:
        return None
    x = _left_margin(page)
    y0 = min(BAND_TOP_PT, max(0.0, page.height - BAND_HEIGHT_PT))
    return FormAnchor(
        id=f"p{page.number}h",
        form_name=page.form_name,
        page=page.number,
        # Zero width on purpose: the anchor is where the first annotation starts,
        # not a box it is placed to the right of.
        bbox=BBox(round(x, 2), round(y0, 2), round(x, 2), round(y0 + BAND_HEIGHT_PT, 2)),
        domain=page.form_domain,
        evidence=[f"header band of page {page.number}"]
        + ([f"domain {page.form_domain} from this CRF"] if page.form_domain else []))


def _left_margin(page: Page) -> float:
    """Where the page's own content starts, so the markup lines up with it."""
    xs = [l.bbox.x0 for l in page.content_lines if l.text.strip()]
    return min(xs) if xs else round(page.width * DEFAULT_LEFT_PCT, 2)


# --- reporting -------------------------------------------------------------
def summarize_form_level(doc: Document) -> dict:
    by_form: dict[str, list[str]] = {}
    for a in doc.form_annotations():
        by_form.setdefault(a.form_name or f"page {a.page}", []).append(a.text)
    return {
        "anchors": sum(1 for _ in doc.iter_anchors()),
        "form_annotations": len(doc.form_annotations()),
        "forms_with_several": sum(1 for v in by_form.values() if len(v) > 1),
        "by_form": {k: v for k, v in sorted(by_form.items())},
    }
