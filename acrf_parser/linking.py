"""Phase 6 - field <-> annotation linking.

The whole parser exists to produce this relation, and it is the step where a
naive implementation quietly ruins the output. Nearest-neighbour matching looks
right on a clean page and is wrong everywhere else: markup for the *last* field
in a column is nearer to the next section's heading than to its own label, and
an annotation with no field at all (a SUPPQUAL qualifier for a question that
lives on another page) will always have a nearest neighbour to be wrongly glued
to.

So linking is row-aware and scored, not nearest:

* a hard gate - the annotation must share a row with the field, or sit within
  `ROW_SLACK_PT` of it. An annotation floating between rows links to nothing,
  and being reported unlinked is the correct answer for it.
* a weighted score over independent features (row overlap, markup to the right
  of the label, domain agreement, proximity, response zone), each of which
  lands in the link's evidence.
* assignment, not argmax: candidates are consumed strongest-first, an
  annotation links once, and a field takes at most one annotation *per type* -
  a field legitimately carries a VARIABLE and a SUPP_QUALIFIER at once, but two
  bare variables on one label means one of them belongs to the field below.

Losing candidates are kept with `rejected=True` so a wrong link can be argued
with rather than just re-run.

DOMAIN_HEADER and CROSS_REFERENCE markup is never linked to a field - it
describes the form, and Phase 2 has already consumed it.
"""
from __future__ import annotations

from . import annotations as ann
from .layout import RESPONSE_ZONE
from .models import Annotation, Document, Field, Link, Page

# --- tunables --------------------------------------------------------------
W_ROW = 0.45         # shares a row with the label - by far the strongest signal
W_RIGHT_OF = 0.15    # markup sits to the right of the label, as aCRFs are drawn
W_DOMAIN = 0.15      # variable agrees with the form's SDTM domain
W_PROXIMITY = 0.15
W_ZONE = 0.10        # annotation parked over the response column
PROX_SCALE_PT = 40.0     # ~3 lines: beyond this, vertical distance says nothing
ROW_SLACK_PT = 6.0       # markup drawn just off its row still counts as on it
MIN_LINK_SCORE = 0.35

# Markup that describes the form rather than any one field.
FORM_LEVEL = frozenset({ann.DOMAIN_HEADER, ann.CROSS_REFERENCE})


def link_document(doc: Document) -> list[Link]:
    """Link every page, and record the losers. Returns accepted + rejected links."""
    doc.links = [l for page in doc.pages for l in link_page(page, doc)]
    return doc.links


def link_page(page: Page, doc: Document | None = None) -> list[Link]:
    """Score, gate and assign the page's field <-> annotation candidates."""
    domain = page.form_domain
    targets = [a for a in page.annotations if a.annot_type not in FORM_LEVEL]
    scored = [c for a in targets for c in _candidates(a, page, domain)]
    scored.sort(key=lambda c: (-c[2], c[0].id, c[1].id))

    links: list[Link] = []
    linked: set[str] = set()
    taken: dict[tuple[str, str], str] = {}
    for annot, fld, score, evidence in scored:
        slot = (fld.id, annot.annot_type)
        reason = ("annotation already linked to a better field" if annot.id in linked
                  else f"field already has a {annot.annot_type} link" if slot in taken
                  else "")
        links.append(Link(field_id=fld.id, annotation_id=annot.id, page=page.number,
                          link_score=round(score, 3),
                          evidence=evidence + ([reason] if reason else []),
                          rejected=bool(reason)))
        if not reason:
            linked.add(annot.id)
            taken[slot] = annot.id
    return links


# --- scoring ---------------------------------------------------------------
def _candidates(annot: Annotation, page: Page, domain: str):
    """Every (annotation, field) pair that clears the row gate, with its score."""
    for fld in page.fields:
        row = annot.bbox.v_overlap(fld.bbox)
        gap = max(fld.bbox.y0 - annot.bbox.y1, annot.bbox.y0 - fld.bbox.y1)
        if row <= 0 and gap > ROW_SLACK_PT:
            continue                     # not on this field's row: not a candidate
        score, evidence = _score(annot, fld, page, domain, row, gap)
        if score >= MIN_LINK_SCORE:
            yield annot, fld, score, evidence


def _score(annot: Annotation, fld: Field, page: Page, domain: str,
           row: float, gap: float) -> tuple[float, list[str]]:
    score, ev = 0.0, []
    if row > 0:
        score += W_ROW * row
        ev.append(f"row overlap {row:.2f}")
    else:
        score += W_ROW * 0.25
        ev.append(f"adjacent row, {gap:.1f}pt off")

    if annot.bbox.x0 >= fld.bbox.x1:
        score += W_RIGHT_OF
        ev.append("markup right of the label")

    if _domain_agrees(annot, domain):
        score += W_DOMAIN
        ev.append(f"variable agrees with form domain {domain}")

    prox = max(0.0, 1.0 - abs(annot.bbox.cy - fld.bbox.cy) / PROX_SCALE_PT)
    if prox:
        score += W_PROXIMITY * prox
        ev.append(f"vertical proximity {prox:.2f}")

    if _over_response_zone(annot, page):
        score += W_ZONE
        ev.append("sits over the response zone")

    opt = _aligned_option(annot, fld)
    if opt:
        ev.append(f"aligned with option {opt!r}")
    return min(score, 1.0), ev


def _domain_agrees(annot: Annotation, domain: str) -> bool:
    """Does the markup name a variable belonging to this form's domain?

    Two shapes count: an explicit `domain` in the parsed payload, and the SDTM
    convention that a domain's variables carry its prefix (MH -> MHTERM). Only a
    bonus, never a penalty: DM's own variables (AGE, SEX, BRTHDTC) carry no
    prefix at all, and identifiers like USUBJID belong to every domain.
    """
    if not domain:
        return False
    if annot.parsed.get("domain") == domain:
        return True
    var = annot.parsed.get("variable") or ""
    return var.startswith(domain) and len(var) > len(domain)


def _over_response_zone(annot: Annotation, page: Page) -> bool:
    return any(b.role_hint == RESPONSE_ZONE and b.contains(annot.bbox, 0.3)
               for b in page.column_bands)


def _aligned_option(annot: Annotation, fld: Field) -> str:
    """Which of the field's response options the markup sits beside, if any."""
    hits = [o for o in fld.options if o.bbox.v_overlap(annot.bbox) > 0]
    return max(hits, key=lambda o: o.bbox.v_overlap(annot.bbox)).text if hits else ""


# --- reporting -------------------------------------------------------------
def unlinked_annotations(doc: Document) -> list[Annotation]:
    """Markup that reached no field. A real finding, not a failure to try.

    Usually one of: a qualifier whose question is on another page, markup for a
    field the layout pass missed, or a genuinely orphaned annotation.
    """
    linked = {l.annotation_id for l in doc.links if not l.rejected}
    return [a for a in doc.iter_annotations()
            if a.id not in linked and a.annot_type not in FORM_LEVEL]


def unlinked_fields(doc: Document) -> list[Field]:
    """Fields with no markup - unannotated, or annotated on a continuation page."""
    linked = {l.field_id for l in doc.links if not l.rejected}
    return [f for f in doc.iter_fields() if f.id not in linked]


def summarize_links(doc: Document) -> dict:
    accepted = [l for l in doc.links if not l.rejected]
    return {
        "links": len(accepted),
        "candidates_scored": len(doc.links),
        "mean_link_score": round(sum(l.link_score for l in accepted) / len(accepted), 3)
        if accepted else 0.0,
        "unlinked_annotations": [a.text for a in unlinked_annotations(doc)],
        "unlinked_fields": [f"{f.form_name}: {f.text[:40]}" for f in unlinked_fields(doc)],
    }
