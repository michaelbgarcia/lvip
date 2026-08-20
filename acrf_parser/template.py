"""Phase 8 - template creation.

A template is what makes the parse reusable: the `(form_name, field_text) ->
annotation` relation of one study, written down without a single absolute
coordinate, so it can be applied to the *next* study's CRF - typically an
unannotated one that needs the same markup.

Nothing here stores points. Geometry is either page-relative (`rel_x_pct`,
`rel_y_pct` via `BBox.relative`) or a relative label (`right_of_field`), because
a template that hard-codes "x=330" breaks the moment a study re-flows its forms,
and one that says "to the right of the label, 55% across the page" does not.
Page numbers are stored as an offset from the form's first page for the same
reason: Medical History being pages 2-3 here and 11-12 there is not a difference.

Applying a template is a lookup with a stated method, never a guess:

    EXACT_KEY   both form and field text match          - what you want
    POSITION    same form, same page offset, same place - re-worded label
    TEXT_ONLY   field text matches under a *different* form  - suspect, and
                scored accordingly: "Start Date" is MHSTDTC on Medical History
                and AESTDTC on Adverse Events, so a text-only hit is a
                suggestion for a human, not an answer.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field as dc_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Annotation, Document, Field
from .normalize import normalize

TEMPLATE_VERSION = 1

EXACT_KEY, POSITION, TEXT_ONLY = "EXACT_KEY", "POSITION", "TEXT_ONLY"
CONF_EXACT, CONF_POSITION, CONF_TEXT_ONLY = 0.95, 0.6, 0.3
REL_TOL = 0.04           # 4% of the page: how far a field may move and still match

RIGHT_OF, LEFT_OF = "right_of_field", "left_of_field"
BELOW, ABOVE, OVERLAPS = "below_field", "above_field", "overlapping_field"


@dataclass
class TemplateMatch:
    """One field of a new document resolved against a template.

    Not in models.py on purpose: this is not something extracted from a PDF, it
    is the output of comparing one against stored knowledge.
    """
    field_id: str
    field_text: str
    form_name: str
    page: int
    method: str
    confidence: float
    annotations: list[dict[str, Any]] = dc_field(default_factory=list)
    evidence: list[str] = dc_field(default_factory=list)


# --- creation --------------------------------------------------------------
def build_template(doc: Document, name: str | None = None) -> dict[str, Any]:
    """Distil a parsed document into a reusable, coordinate-free template."""
    return {
        "template_version": TEMPLATE_VERSION,
        "name": name or Path(doc.path).stem,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {"file": Path(doc.path).name, "page_count": doc.page_count},
        "forms": [_form_entry(doc, f) for f in doc.forms],
    }


def _form_entry(doc: Document, form) -> dict[str, Any]:
    first = form.first_page
    fields = [f for f in doc.iter_fields() if normalize(f.form_name) == form.normalized_name]
    return {
        "name": form.name,
        "normalized_name": form.normalized_name,
        "domain": form.domain,
        "page_count": len(form.pages),
        "continuation_offsets": [n - first for n in form.continuation_pages],
        "confidence": round(form.confidence, 2),
        "fields": _merge_by_key([_field_entry(doc, f, first) for f in fields]),
    }


def _merge_by_key(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per `(form, field_text)` key - the template's whole premise.

    A field printed on both pages of a two-page form is two occurrences of one
    key, and only one of them usually carries the markup: "Condition" is
    annotated MHTERM on page 1 of Medical History and left bare on the
    continuation page. Keeping both as separate entries lets the bare one shadow
    the annotated one, so occurrences are folded here - annotations unioned,
    page offsets collected, and the best-annotated occurrence supplying the
    stored position.
    """
    merged: dict[str, dict[str, Any]] = {}
    for e in entries:
        key = e["normalized_text"]
        cur = merged.get(key)
        if cur is None:
            merged[key] = dict(e, occurrences=1)
            continue
        cur["occurrences"] += 1
        cur["page_offsets"] = sorted(set(cur["page_offsets"] + e["page_offsets"]))
        if not cur["annotations"] and e["annotations"]:
            cur["position"] = e["position"]        # keep where the markup was found
        seen = {(a["text"], a["type"]) for a in cur["annotations"]}
        cur["annotations"] += [a for a in e["annotations"]
                               if (a["text"], a["type"]) not in seen]
        cur["confidence"] = max(cur["confidence"], e["confidence"])
        cur["control_kinds"] = sorted(set(cur["control_kinds"] + e["control_kinds"]))
    return list(merged.values())


def _field_entry(doc: Document, fld: Field, first_page: int) -> dict[str, Any]:
    page = doc.page(fld.page)
    w, h = (page.width, page.height) if page else (1.0, 1.0)
    links = [l for l in doc.links_for(fld.id)]
    return {
        "key": [normalize(fld.form_name), fld.normalized_text],
        "field_text": fld.text,
        "raw_text": fld.raw_text,
        "normalized_text": fld.normalized_text,
        "item_number": fld.item_number,
        "section": fld.section,
        "role": fld.role,
        "confidence": fld.confidence,
        "page_offsets": [fld.page - first_page],
        "position": fld.bbox.relative(w, h),
        "control_kinds": fld.control_kinds,
        "options": [{"text": o.text, "position": o.bbox.relative(w, h)}
                    for o in fld.options],
        "annotations": [_annotation_entry(doc.annotation(l.annotation_id), fld, l, w, h)
                        for l in links if doc.annotation(l.annotation_id)],
    }


def _annotation_entry(a: Annotation, fld: Field, link, w: float, h: float) -> dict[str, Any]:
    p = a.parsed or {}
    return {
        "text": a.text,
        "type": a.annot_type,
        "variable": p.get("variable"),
        "domain": p.get("domain"),
        "value": p.get("value"),
        "qnam": p.get("qnam"),
        "type_confidence": a.type_confidence,
        "link_score": link.link_score,
        "placement": {"relative_label": relative_label(a, fld),
                      **a.bbox.relative(w, h)},
        "statements": [{"text": s.text, "type": s.annot_type, **s.parsed}
                       for s in a.parts] if len(a.parts) > 1 else [],
    }


def relative_label(a: Annotation, fld: Field) -> str:
    """Where the markup sits relative to its label, in words rather than points.

    Row alignment decides first: aCRF markup is overwhelmingly placed beside its
    field, and "right_of_field" survives a re-flow that any coordinate would not.
    """
    if a.bbox.v_overlap(fld.bbox) > 0:
        if a.bbox.x0 >= fld.bbox.x1:
            return RIGHT_OF
        if a.bbox.x1 <= fld.bbox.x0:
            return LEFT_OF
        return OVERLAPS
    return BELOW if a.bbox.cy > fld.bbox.cy else ABOVE


def save_template(template: dict, path: str | Path, indent: int = 2) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(template, indent=indent, ensure_ascii=False))
    return path


def load_template(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


# --- application -----------------------------------------------------------
def apply_template(template: dict, doc: Document) -> list[TemplateMatch]:
    """Resolve every field of `doc` against the template, best method first."""
    by_key, by_text, by_form = _index(template)
    out: list[TemplateMatch] = []
    for form in doc.forms:
        first = form.first_page
        for fld in (f for f in doc.iter_fields()
                    if normalize(f.form_name) == form.normalized_name):
            out.append(_match(fld, first, doc, by_key, by_text, by_form))
    return out


def _index(template: dict):
    """Three lookup tables, one per matching method."""
    by_key: dict[tuple[str, str], dict] = {}
    by_text: dict[str, list[dict]] = {}
    by_form: dict[str, list[dict]] = {}
    for form in template.get("forms", []):
        entries = form.get("fields", [])
        by_form[form["normalized_name"]] = entries
        for e in entries:
            by_key[tuple(e["key"])] = e
            by_text.setdefault(e["normalized_text"], []).append(e)
    return by_key, by_text, by_form


def _match(fld: Field, first_page: int, doc: Document, by_key, by_text, by_form) -> TemplateMatch:
    form_key = normalize(fld.form_name)
    make = lambda method, conf, entry, why: TemplateMatch(
        field_id=fld.id, field_text=fld.text, form_name=fld.form_name, page=fld.page,
        method=method, confidence=conf,
        annotations=list(entry.get("annotations", [])) if entry else [],
        evidence=why)

    entry = by_key.get((form_key, fld.normalized_text))
    if entry:
        return make(EXACT_KEY, CONF_EXACT, entry,
                    [f"(form, field_text) matched template key {form_key}|{fld.normalized_text}"])

    hit = _positional(fld, first_page, doc, by_form.get(form_key, []))
    if hit:
        entry, dist = hit
        return make(POSITION, round(CONF_POSITION * (1 - dist / REL_TOL) + 0.2, 2), entry,
                    [f"same form, page offset {fld.page - first_page}, "
                     f"within {dist:.3f} of template position of {entry['field_text']!r}"])

    others = [e for e in by_text.get(fld.normalized_text, []) if e["key"][0] != form_key]
    if others:
        return make(TEXT_ONLY, CONF_TEXT_ONLY, others[0],
                    [f"field text matches, but under form {others[0]['key'][0]!r} - "
                     "the key is (form, field), so treat as a suggestion"])

    return TemplateMatch(field_id=fld.id, field_text=fld.text, form_name=fld.form_name,
                         page=fld.page, method="", confidence=0.0,
                         evidence=["no template entry for this field"])


def _positional(fld: Field, first_page: int, doc: Document, entries: list[dict]):
    """Nearest template field on the same page offset, if it is near enough.

    Only reached when the text did not match, so this is the "label was re-worded
    but the form was not redrawn" case - which is why it is bounded by REL_TOL
    rather than just taking the nearest.
    """
    page = doc.page(fld.page)
    if page is None or not entries:
        return None
    rel = fld.bbox.relative(page.width, page.height)
    offset = fld.page - first_page
    best, best_dist = None, REL_TOL
    for e in entries:
        if offset not in e.get("page_offsets", []):
            continue
        pos = e["position"]
        dist = max(abs(rel["rel_x_pct"] - pos["rel_x_pct"]),
                   abs(rel["rel_y_pct"] - pos["rel_y_pct"]))
        if dist < best_dist:
            best, best_dist = e, dist
    return (best, best_dist) if best else None


# --- reporting -------------------------------------------------------------
def summarize_template(template: dict) -> dict:
    fields = [f for form in template["forms"] for f in form["fields"]]
    return {
        "name": template["name"],
        "forms": len(template["forms"]),
        "fields": len(fields),
        "annotated_fields": sum(1 for f in fields if f["annotations"]),
        "variables": sorted({a["variable"] for f in fields for a in f["annotations"]
                             if a.get("variable")}),
    }


def summarize_matches(matches: list[TemplateMatch]) -> dict:
    counts: dict[str, int] = {}
    for m in matches:
        counts[m.method or "UNMATCHED"] = counts.get(m.method or "UNMATCHED", 0) + 1
    return {"matched": sum(1 for m in matches if m.method), "total": len(matches),
            "by_method": dict(sorted(counts.items()))}


def match_to_dict(m: TemplateMatch) -> dict:
    return asdict(m)
