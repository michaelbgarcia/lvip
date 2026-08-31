"""House style: how a sponsor renders its annotations, measured from history.

This is the deterministic half of "formatting specs". Colour, font, size and
placement are not judgement calls - they are facts about a corpus of finished
aCRFs, recoverable by counting. Asking a language model to *calculate* placement
invites coordinates that are plausible and subtly wrong, and coordinates are the
one thing a human reviewing a spreadsheet cannot eyeball. So they are computed
here, and the agent is left with the semantic question it is actually good at.

Everything is reported with `samples` and `agreement` - the share of samples
that matched the modal value. Agreement is the point: a study that renders
VARIABLE markup at 8pt on some pages and 9pt on others has an agreement of 0.5,
and that surfaces as a decision for a human instead of being averaged into
8.5pt, which is a size nobody chose.

Placement is stored the way templates store it - a relative label plus offsets
as a fraction of page size - so it survives a form being re-flowed.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field as dc_field
from statistics import median
from typing import Any, Iterable

from .models import Annotation, Document
from .template import relative_label

MIN_SAMPLES = 3          # below this, report the observation but trust it less
LOW_AGREEMENT = 0.7      # flagged as unsettled: the corpus does not agree

ANY_TYPE = "*"


@dataclass
class StyleRule:
    """How one class of annotation is rendered, with how sure we are."""
    scope: str                                  # annot_type, or "*" for the default
    samples: int = 0
    text_color: tuple[float, ...] | None = None
    color_agreement: float = 0.0
    # The box's background. Measured only over the annotations that actually
    # carry one, with `fill_samples` saying how many that was - a corpus where
    # three boxes in two hundred are yellow must not report yellow at 100%.
    fill_color: tuple[float, ...] | None = None
    fill_agreement: float = 0.0
    fill_samples: int = 0
    font_name: str = ""
    font_agreement: float = 0.0
    font_size: float = 0.0
    size_agreement: float = 0.0
    placement: str = ""                         # right_of_field, below_field, ...
    placement_agreement: float = 0.0
    placement_samples: int = 0
    offset_x_pct: float = 0.0                   # gap from the label's right edge
    offset_y_pct: float = 0.0                   # signed, from the label's centre
    evidence: list[str] = dc_field(default_factory=list)

    @property
    def settled(self) -> bool:
        """Enough samples, and they agree. An unsettled rule needs a human."""
        return (self.samples >= MIN_SAMPLES
                and min(self.color_agreement, self.font_agreement,
                        self.size_agreement) >= LOW_AGREEMENT)


@dataclass
class HouseStyle:
    """A sponsor's rendering conventions, per annotation type plus a default."""
    default: StyleRule
    by_type: dict[str, StyleRule] = dc_field(default_factory=dict)
    documents: list[str] = dc_field(default_factory=list)

    def for_type(self, annot_type: str) -> StyleRule:
        """The rule to apply to a new annotation of this type.

        Falls back to the corpus-wide default, because a type seen twice should
        not out-vote a convention seen two hundred times.
        """
        rule = self.by_type.get(annot_type)
        if rule and rule.samples >= MIN_SAMPLES:
            return rule
        return rule if rule and not self.default.samples else self.default

    def unsettled(self) -> list[StyleRule]:
        """Rules the corpus disagrees on - what a human should decide once."""
        return [r for r in [self.default, *self.by_type.values()] if not r.settled]

    def to_dict(self) -> dict[str, Any]:
        return {"documents": self.documents, "default": asdict(self.default),
                "by_type": {k: asdict(v) for k, v in sorted(self.by_type.items())}}


def derive_house_style(docs: Document | Iterable[Document]) -> HouseStyle:
    """Measure the rendering conventions across one or more parsed aCRFs."""
    docs = [docs] if isinstance(docs, Document) else list(docs)
    annots = [a for d in docs for a in d.iter_annotations() if a.text.strip()]
    placements = [p for d in docs for p in _placements(d)]

    style = HouseStyle(default=_rule(ANY_TYPE, annots, placements),
                       documents=[d.path for d in docs])
    for annot_type in sorted({a.annot_type for a in annots}):
        style.by_type[annot_type] = _rule(
            annot_type,
            [a for a in annots if a.annot_type == annot_type],
            [p for p in placements if p[0] == annot_type])
    return style


def _rule(scope: str, annots: list[Annotation], placements: list[tuple]) -> StyleRule:
    rule = StyleRule(scope=scope, samples=len(annots))
    if not annots:
        rule.evidence = ["no annotations of this type in the corpus"]
        return rule

    rule.text_color, rule.color_agreement = _mode(a.text_color for a in annots)
    rule.fill_color, rule.fill_agreement = _mode(a.fill_color for a in annots)
    rule.fill_samples = sum(1 for a in annots if a.fill_color)
    rule.font_name, rule.font_agreement = _mode(a.font_name for a in annots)
    rule.font_size, rule.size_agreement = _mode(a.font_size for a in annots)

    rule.placement_samples = len(placements)
    if placements:
        rule.placement, rule.placement_agreement = _mode(p[1] for p in placements)
        # Median, not mean: one annotation parked in a margin should not drag
        # the convention, and the median of the actual offsets is a real value
        # some annotation had.
        rule.offset_x_pct = round(median(p[2] for p in placements), 4)
        rule.offset_y_pct = round(median(p[3] for p in placements), 4)

    rule.evidence = _evidence(rule)
    return rule


def _evidence(rule: StyleRule) -> list[str]:
    ev = [f"{rule.samples} annotation(s) measured"]
    if rule.text_color is not None:
        ev.append(f"colour {rule.text_color} in {rule.color_agreement:.0%} of them")
    if rule.fill_color is not None:
        ev.append(f"fill {rule.fill_color} in {rule.fill_agreement:.0%} of the "
                  f"{rule.fill_samples} filled box(es)")
    if rule.font_name:
        ev.append(f"font {rule.font_name} in {rule.font_agreement:.0%}")
    if rule.font_size:
        ev.append(f"size {rule.font_size}pt in {rule.size_agreement:.0%}")
    if rule.placement:
        ev.append(f"placed {rule.placement} in {rule.placement_agreement:.0%} "
                  f"of {rule.placement_samples} linked annotation(s)")
    if rule.samples < MIN_SAMPLES:
        ev.append(f"fewer than {MIN_SAMPLES} samples - weak evidence")
    if not rule.settled:
        ev.append("corpus does not agree; needs a human decision")
    return ev


def _placements(doc: Document) -> list[tuple[str, str, float, float]]:
    """(annot_type, relative_label, dx_pct, dy_pct) for every accepted link.

    Placement can only be measured where an annotation is tied to a field, so
    this runs over links rather than over annotations - unlinked markup still
    contributes its colour and font, just not its position.
    """
    out = []
    for link in doc.links:
        if link.rejected:
            continue
        fld, annot = doc.field(link.field_id), doc.annotation(link.annotation_id)
        page = doc.page(link.page)
        if not (fld and annot and page):
            continue
        w, h = page.width or 1.0, page.height or 1.0
        out.append((annot.annot_type, relative_label(annot, fld),
                    round((annot.bbox.x0 - fld.bbox.x1) / w, 4),
                    round((annot.bbox.cy - fld.bbox.cy) / h, 4)))
    return out


def _mode(values: Iterable) -> tuple[Any, float]:
    """Most common value, and the share of samples that agreed with it."""
    counts = Counter(v for v in values if v not in ("", None))
    if not counts:
        return (None, 0.0)
    value, n = counts.most_common(1)[0]
    return (value, round(n / sum(counts.values()), 3))


def summarize_style(style: HouseStyle) -> dict[str, Any]:
    d = style.default
    return {
        "documents": len(style.documents),
        "samples": d.samples,
        "text_color": d.text_color,
        "fill_color": d.fill_color,
        "font": f"{d.font_name} {d.font_size}pt" if d.font_name else "",
        "placement": d.placement,
        "settled": d.settled,
        "unsettled_scopes": [r.scope for r in style.unsettled()],
    }


def derive_house_style_from_kb(kb) -> HouseStyle:
    """Measure house style from a knowledge base instead of from parsed PDFs.

    The offline path: ingest the corpus once, then generate staging workbooks
    without touching the source files again. Appearance is read from the columns
    Phase 7 persists, and placement from the `relative_label` and offsets
    computed at write time - so the geometry logic lives in exactly one place.
    """
    rows = [dict(r) for r in kb.con.execute(
        "SELECT annot_type, text_color, fill_color, font_name, font_size"
        " FROM annotations")]
    placements = [dict(r) for r in kb.con.execute(
        "SELECT a.annot_type, l.relative_label, l.offset_x_pct, l.offset_y_pct"
        " FROM links l JOIN annotations a ON a.id = l.annotation_id"
        " WHERE l.rejected = 0 AND l.relative_label IS NOT NULL")]
    docs = [r["path"] for r in kb.con.execute("SELECT path FROM documents")]

    annots = [_row_as_annotation(r) for r in rows]
    places = [(p["annot_type"], p["relative_label"],
               p["offset_x_pct"] or 0.0, p["offset_y_pct"] or 0.0) for p in placements]

    house = HouseStyle(default=_rule(ANY_TYPE, annots, places), documents=docs)
    for annot_type in sorted({a.annot_type for a in annots}):
        house.by_type[annot_type] = _rule(
            annot_type,
            [a for a in annots if a.annot_type == annot_type],
            [p for p in places if p[0] == annot_type])
    return house


def _row_as_annotation(row: dict) -> Annotation:
    """Just enough of an Annotation for the aggregator; geometry comes separately."""
    import json
    from .models import BBox
    color = json.loads(row["text_color"]) if row.get("text_color") else None
    fill = json.loads(row["fill_color"]) if row.get("fill_color") else None
    return Annotation(page=0, text="x", bbox=BBox.of((0, 0, 1, 1)),
                      annot_type=row["annot_type"] or "",
                      text_color=tuple(color) if color else None,
                      fill_color=tuple(fill) if fill else None,
                      font_name=row.get("font_name") or "",
                      font_size=row.get("font_size") or 0.0)
