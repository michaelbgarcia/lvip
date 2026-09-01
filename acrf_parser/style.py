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
from dataclasses import asdict, dataclass, field as dc_field, replace
from statistics import median
from typing import Any, Iterable

from . import annotations as ann
from .models import Annotation, Document
from .normalize import statement_key
from .template import placement_of

MIN_SAMPLES = 3          # below this, report the observation but trust it less
LOW_AGREEMENT = 0.7      # flagged as unsettled: the corpus does not agree
MIN_FILL_SAMPLES = 2     # filled boxes needed before a domain's fill is a rule

ANY_TYPE = "*"
DOMAIN_SCOPE, STATEMENT_SCOPE = "domain:", "statement:"

# Where a row's fill colour came from. Reported beside the colour, because a
# fill nobody can account for is exactly the one a reviewer should look at.
FILL_TYPE = "house style for this annotation type"
FILL_DOMAIN = "house style for domain {domain}"
FILL_STATEMENT = "this exact statement's fill in previous studies"
FILL_UNDECIDED = ("corpus varies fill by domain and this statement's domain is "
                  "unresolved - decide it")


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
    """A sponsor's rendering conventions, per annotation type plus a default.

    Fill colour gets two extra axes, because on real aCRFs it does not follow
    the annotation type. A Disposition page draws `DSTERM` yellow and `RFICDTC`
    blue - same type, same field, same row, different domain - so fill is
    measured per domain as well, and the domain rule overrides the type rule for
    the fill alone. Everything else (text colour, font, size, placement) still
    comes from the type, because that is what the corpus shows varying with it.

    `by_statement` is the fallback for the case that makes this hard: DM's own
    variables carry no prefix, so nothing in `RFICDTC` says which domain it is.
    Where the statement's domain is unresolvable, what the corpus drew for that
    exact statement is the best evidence there is - and where there is none
    either, the fill is left blank and said to be undecided rather than
    defaulted into whatever the majority domain happens to use.
    """
    default: StyleRule
    by_type: dict[str, StyleRule] = dc_field(default_factory=dict)
    by_domain: dict[str, StyleRule] = dc_field(default_factory=dict)
    by_statement: dict[str, StyleRule] = dc_field(default_factory=dict)
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

    @property
    def fill_varies_by_domain(self) -> bool:
        """Does this corpus actually colour-code by domain?

        Asked before any domain override is applied. A sponsor who fills every
        box the same colour must keep behaving exactly as it did before the
        domain axis existed - splitting a single convention across ten domains
        would turn one settled rule into ten under-sampled ones for no gain.
        """
        fills = {r.fill_color for r in self.by_domain.values()
                 if r.fill_color and r.fill_samples >= MIN_FILL_SAMPLES}
        return len(fills) > 1

    def for_annotation(self, annot_type: str, text: str = "",
                       parsed: dict | None = None) -> tuple[StyleRule, str]:
        """The rule for one specific statement, and where its fill came from.

        Returns a copy: the caller gets a rule it may not mutate the corpus with,
        and the reason string is exported beside the colour so a reviewer can see
        why a row is the colour it is - or that nobody could say.
        """
        rule = replace(self.for_type(annot_type))
        if not self.fill_varies_by_domain:
            return rule, FILL_TYPE if rule.fill_color else ""

        domain, _how = ann.statement_domain(text, parsed)
        dom_rule = self.by_domain.get(domain) if domain else None
        if dom_rule and dom_rule.fill_color and dom_rule.fill_samples >= MIN_FILL_SAMPLES:
            return _with_fill(rule, dom_rule), FILL_DOMAIN.format(domain=domain)

        seen = self.by_statement.get(statement_key(text))
        if seen and seen.fill_color:
            return _with_fill(rule, seen), FILL_STATEMENT

        # No domain, no history. Blank rather than defaulted: on a corpus that
        # colour-codes, the majority colour is a specific claim about which
        # domain this is, and nothing here supports it.
        return _with_fill(rule, None), FILL_UNDECIDED

    def unsettled(self) -> list[StyleRule]:
        """Rules the corpus disagrees on - what a human should decide once."""
        return [r for r in [self.default, *self.by_type.values()] if not r.settled]

    def fill_rules(self) -> list[StyleRule]:
        """The domain and statement rules, for the workbook's palette sheet."""
        return [self.by_domain[k] for k in sorted(self.by_domain)] + \
               [self.by_statement[k] for k in sorted(self.by_statement)]

    def to_dict(self) -> dict[str, Any]:
        return {"documents": self.documents, "default": asdict(self.default),
                "fill_varies_by_domain": self.fill_varies_by_domain,
                "by_type": {k: asdict(v) for k, v in sorted(self.by_type.items())},
                "by_domain": {k: asdict(v) for k, v in sorted(self.by_domain.items())},
                "by_statement": {k: asdict(v)
                                 for k, v in sorted(self.by_statement.items())}}


def _with_fill(rule: StyleRule, source: StyleRule | None) -> StyleRule:
    """`rule`'s everything, `source`'s fill. Blank fill when there is no source."""
    return replace(rule,
                   fill_color=source.fill_color if source else None,
                   fill_agreement=source.fill_agreement if source else 0.0,
                   fill_samples=source.fill_samples if source else 0)


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
    _add_fill_axes(style, annots)
    return style


def _add_fill_axes(style: HouseStyle, annots: list[Annotation]) -> None:
    """Measure fill per domain, and per statement where the domain is unresolvable.

    Both axes are about the fill and nothing else, so placement is not passed in:
    a domain does not have its own idea of where markup goes, only of what colour
    it is.
    """
    by_domain: dict[str, list[Annotation]] = {}
    orphans: dict[str, list[Annotation]] = {}
    for a in annots:
        domain, _how = ann.statement_domain(a.text, a.parsed)
        if domain:
            by_domain.setdefault(domain, []).append(a)
        elif a.fill_color:
            # No prefix to read a domain off (RFICDTC, AGE, SEX). What the corpus
            # actually drew for this statement is the only evidence available.
            orphans.setdefault(statement_key(a.text), []).append(a)
    for domain, group in sorted(by_domain.items()):
        style.by_domain[domain] = _rule(f"{DOMAIN_SCOPE}{domain}", group, [])
    for key, group in sorted(orphans.items()):
        style.by_statement[key] = _rule(
            f"{STATEMENT_SCOPE}{group[0].text}", group, [])


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
        label, dx, dy = placement_of(annot.bbox, fld.bbox, page.width, page.height)
        out.append((annot.annot_type, label, dx, dy))
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
        "fill_varies_by_domain": style.fill_varies_by_domain,
        "domain_fills": {r.scope.removeprefix(DOMAIN_SCOPE): _hex(r.fill_color)
                         for r in style.by_domain.values() if r.fill_color},
        "statement_fills": len(style.by_statement),
    }


def _hex(color) -> str:
    if not color:
        return ""
    return "#" + "".join(f"{max(0, min(255, round(c * 255))):02X}" for c in color[:3])


def derive_house_style_from_kb(kb) -> HouseStyle:
    """Measure house style from a knowledge base instead of from parsed PDFs.

    The offline path: ingest the corpus once, then generate staging workbooks
    without touching the source files again. Appearance is read from the columns
    Phase 7 persists, and placement from the `relative_label` and offsets
    computed at write time - so the geometry logic lives in exactly one place.
    """
    rows = [dict(r) for r in kb.con.execute(
        "SELECT annot_type, text, parsed, text_color, fill_color, font_name,"
        " font_size FROM annotations")]
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
    _add_fill_axes(house, annots)
    return house


def _row_as_annotation(row: dict) -> Annotation:
    """Just enough of an Annotation for the aggregator; geometry comes separately.

    `text` and `parsed` are carried through so the fill axes can ask which
    domain each statement belongs to - the offline path has to reach the same
    answer as the in-memory one or the two would disagree about colour.
    """
    import json
    from .models import BBox
    load = lambda k: json.loads(row[k]) if row.get(k) else None
    color, fill = load("text_color"), load("fill_color")
    parsed = load("parsed")
    return Annotation(page=0, text=row.get("text") or "x", bbox=BBox.of((0, 0, 1, 1)),
                      annot_type=row["annot_type"] or "",
                      parsed=parsed if isinstance(parsed, dict) else {},
                      text_color=tuple(color) if color else None,
                      fill_color=tuple(fill) if fill else None,
                      font_name=row.get("font_name") or "",
                      font_size=row.get("font_size") or 0.0)
