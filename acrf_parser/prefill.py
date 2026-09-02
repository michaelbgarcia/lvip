"""Deterministic pre-fill: answer what history already answers, before any AI.

Runs offline against the knowledge base - stdlib and SQLite only, no network,
no model. Its job is to shrink the agent's work to the rows that genuinely need
reasoning, and to make sure the rows it *does* fill arrive with their reasoning
attached.

Five tiers, all scored, all explainable:

    EXACT_KEY             (form, field_text) seen before. The answer, not a guess.
    CROSS_FORM_CONSENSUS  this label maps to one variable in every form that has
                          ever used it - so the form does not disambiguate it
    DOMAIN_PATTERN        SDTM's own naming convention, learned: "start date"
                          became STDTC in MH and in AE, so on a CM form it is
                          CMSTDTC - even though CM was never seen
    FUZZY_SAME_FORM       same form, near-identical wording
    NEEDS_MAPPING         nothing in history reaches it. The agent's real job.

The safety property that matters: **only EXACT_KEY can auto-approve.** Every
fuzzy tier lands as NEEDS_REVIEW carrying the source study, the source label and
the score, so a reviewer sees "matched 'Start Date' from STUDY-XYZ Medical
History at 0.86" rather than a bare variable name that looks as authoritative as
an exact hit. Silent fuzzy matches are the failure mode that would make this
whole pipeline untrustworthy.

CROSS_FORM_CONSENSUS is where the corpus decides its own reliability. "Sex" maps
to SEX on every form that has one, so text alone is sufficient. "Start Date" maps
to MHSTDTC, AESTDTC and CMSTDTC, so text alone is *insufficient* - and the
algorithm learns that from the disagreement rather than being told. Where the
corpus disagrees, no cross-form suggestion is offered and the reason is recorded.

One field, several annotations
------------------------------
A CRF field routinely carries more than one statement. "Date of informed
consent" is annotated DSTERM, DSDECOD=INFORMED CONSENT OBTAINED, RFICDTC *and*
DSSTDTC - four statements about one label, and none of them is the "best" one.
So an exact key returns the whole **set** it was seen with, not a single winner:
`Prefill.best` is the first of them and `Prefill.companions` the rest, and the
staging workbook exports one row per member.

Only EXACT_KEY contributes companions. A fuzzy tier is a guess about *which*
variable a label means, and multiplying a guess by four multiplies the reviewer's
work by four for no extra evidence - so every fuzzy tier still proposes at most
one thing.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field as dc_field, replace
from difflib import SequenceMatcher
from typing import Any, Iterable

from .models import (GEOMETRIC, HUMAN_APPROVED, TRUST_RANK, Document, Field,
                     FormAnchor)
from .normalize import normalize, statement_key

# --- tiers -----------------------------------------------------------------
EXACT_KEY = "EXACT_KEY"
CROSS_FORM_CONSENSUS = "CROSS_FORM_CONSENSUS"
DOMAIN_PATTERN = "DOMAIN_PATTERN"
FUZZY_SAME_FORM = "FUZZY_SAME_FORM"
NEEDS_MAPPING = "NEEDS_MAPPING"

# --- tunables --------------------------------------------------------------
CONF_EXACT = 0.95
CONF_APPROVED = 1.0        # a human signed this exact pair; nothing scores higher
AUTO_THRESHOLD = 0.9       # at or above this a row may ship as AUTO
REVIEW_THRESHOLD = 0.5     # below this the row goes to the agent unfilled
FUZZY_FLOOR = 0.6          # similarity below this is not a candidate at all
MIN_CONSENSUS_FORMS = 2    # distinct forms that must agree for a cross-form hit
MIN_PATTERN_DOMAINS = 2    # distinct domains that must attest a role suffix
# A ceiling on how many statements one exact key may propose. Real fields reach
# four or five; a key that reaches twenty is a corpus problem (one label reused
# across studies for different things), and shipping twenty rows for one label
# would bury the reviewer rather than help them.
MAX_ANNOTATIONS_PER_FIELD = 8
# The same guard for a *form*, at a scale a form actually reaches. A form's own
# markup is its domain header(s), its form-level constants, and every statement
# the linker could not place on a field - and on a questionnaire page that is one
# per question. The MSG Cornell Scale page carries nineteen. Eight is the right
# ceiling for one label and quietly discards half a page here.
MAX_ANNOTATIONS_PER_FORM = 40

AUTO, NEEDS_REVIEW, NEEDS_MAPPING_STATUS = "AUTO", "NEEDS_REVIEW", "NEEDS_MAPPING"

_SUFFIX_OK = re.compile(r"^[A-Z][A-Z0-9]{1,6}$")


@dataclass
class Candidate:
    """One deterministic proposal for a field, with why it was proposed."""
    tier: str
    confidence: float
    variable: str = ""
    annotation_text: str = ""
    annot_type: str = ""
    source: str = ""                 # "STUDY-XYZ · Medical History · Start Date"
    # Which CRF this occurrence was read from, on its own so it can be grouped
    # on. `source` is for a human to read and is not reliably splittable.
    # Form-level dedupe needs the file: two occurrences of one statement from
    # two studies are one statement seen twice, and two from the *same* study
    # are two statements.
    file_name: str = ""
    trust: str = GEOMETRIC
    evidence: list[str] = dc_field(default_factory=list)
    # Where this annotation was drawn on the page it came from, as its left edge.
    # A field's set is ordered by it, so the set comes back in the order the last
    # study read it in - which, since annotations are drawn left to right in the
    # reviewer's own order, is the reviewer's order. 0.0 when unknown.
    drawn_x: float = 0.0
    # And where it was drawn, in a form the next CRF can be placed by: the
    # annotation's own box as page fractions (`rel_*`), plus - for markup tied to
    # a field - the relative label and offsets that box worked out to.
    #
    # This is placement evidence the house style cannot supply. A house style
    # reports the *median* offset for an annotation type across a whole corpus,
    # which is the right answer for a statement nobody has seen before and a poor
    # one for a statement that was seen, on this form, on this field, in a spot
    # somebody chose. It matters most for form-level markup, which has no field
    # to be offset from at all: its own page position is the only record of where
    # it belongs, and without it a page's domain headers were all redrawn in a
    # band at the top left whatever the study actually did.
    drawn: dict[str, Any] = dc_field(default_factory=dict)
    # How it was rendered: text colour, fill, font and size, as the study drew
    # this exact statement. The same argument as `drawn`, for appearance instead
    # of position - the house style reports the corpus mode for an annotation
    # *type*, which is the right answer for a statement nobody has seen and a
    # weaker one for a statement that was seen. The MSG CRF sets its domain
    # headers at 18pt, most variables at 12pt and a run of questionnaire markup
    # at 10pt; a single per-type mode gets a fifth of them wrong, and since the
    # box is measured from the text at that size, a wrong size is a wrong width
    # and so a wrong position too.
    style: dict[str, Any] = dc_field(default_factory=dict)

    @property
    def anchor(self) -> dict[str, Any] | None:
        """The page-relative point this statement was drawn at, if history has it.

        A *point*, not a box: the left edge on the row's centre line. The box is
        re-measured from the text and the font on the way out, so keeping the old
        width would only pin the new box's right edge to a string that is no
        longer being drawn. The left edge is what a reader's eye follows down a
        column of markup, so that is what is preserved.
        """
        d = self.drawn
        if "rel_x_pct" not in d or "rel_y_pct" not in d:
            return None
        return {"rel_x_pct": round(d["rel_x_pct"] - d.get("rel_w_pct", 0.0) / 2, 4),
                "rel_y_pct": round(d["rel_y_pct"], 4),
                "rel_w_pct": 0.0, "rel_h_pct": 0.0}

    @property
    def box_width(self) -> float:
        """How wide the study drew this statement's box, as a page fraction.

        Separate from `anchor` because it answers a different question: not
        where the box starts but what shape it is. Annotators wrap long markup
        into a narrow column rather than running it across the page - the MSG
        CRF sets "Reason for Discontinuation / 0 Ongoing / 1 Adverse Event / ..."
        into six lines 189 points wide - and drawing that as one 600-point line
        does not merely look different, it will not fit on the page at all, so
        the placement arithmetic gives up and clamps it to the margin.

        0.0 when history has no box for this statement, which means "measure it".
        """
        return round(float(self.drawn.get("rel_w_pct") or 0.0), 4)


@dataclass
class Prefill:
    """The pre-fill verdict for one field of the blank CRF.

    `best` and `companions` together are the annotation *set* history proposes -
    one workbook row each. `alternates` is a different thing: rival answers to
    the same question, kept as evidence and never exported as rows.
    """
    field_id: str
    form_name: str
    field_text: str
    best: Candidate
    alternates: list[Candidate] = dc_field(default_factory=list)
    aliases: list[str] = dc_field(default_factory=list)   # other labels for this variable
    # Further statements this same field carried in history: a second row each,
    # not competitors of `best`.
    companions: list[Candidate] = dc_field(default_factory=list)

    @property
    def status(self) -> str:
        """Only an exact hit ships unreviewed; everything fuzzy is a suggestion."""
        return self.status_of(self.best)

    def status_of(self, candidate: Candidate) -> str:
        """Per-candidate, because each one becomes its own row to sign off."""
        if candidate.tier == EXACT_KEY and candidate.confidence >= AUTO_THRESHOLD:
            return AUTO
        return (NEEDS_REVIEW if candidate.confidence >= REVIEW_THRESHOLD
                else NEEDS_MAPPING_STATUS)

    @property
    def annotations(self) -> list[Candidate]:
        """Every candidate that earns a row, in the order they are exported."""
        return [self.best, *self.companions]


class PrefillIndex:
    """Everything the corpus knows, arranged for lookup. Built once, queried per field."""

    def __init__(self) -> None:
        self.by_key: dict[tuple[str, str], Candidate] = {}
        # Every *distinct statement* the key was seen with, best evidence per
        # statement. `by_key` is the head of this; the tail is the companions.
        self.key_sets: dict[tuple[str, str], dict[str, Candidate]] = defaultdict(dict)
        self.by_text: dict[str, list[dict]] = defaultdict(list)
        self.by_form: dict[str, list[dict]] = defaultdict(list)
        self.suffixes: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        self.variable_labels: dict[tuple[str, str], set[str]] = defaultdict(set)
        # form -> SDTM domain, learned from history. A *blank* CRF carries no
        # domain-header annotations, so its own domain is always empty - without
        # this, DOMAIN_PATTERN could never fire on the one input that needs it.
        self.form_domains: dict[str, Counter] = defaultdict(Counter)
        # Markup that belongs to a form rather than to any of its fields: the
        # domain headers at the top of a page and the constants drawn beside
        # them. Keyed on the form, ordered by where it was drawn, exactly as
        # `key_sets` is - the same set-of-statements idea, one level up.
        self.form_sets: dict[str, dict[str, Candidate]] = defaultdict(dict)
        # domain -> the header text a study writes for it ("DS" -> "DS=Disposition").
        # Learned, not a built-in CDISC table: a sponsor that writes
        # "DS = Disposition" with spaces gets its own spacing back.
        self.domain_headers: dict[str, Counter] = defaultdict(Counter)
        # (field_key, annotation) pairs a reviewer turned down. Suggesting one
        # again is worse than suggesting nothing: it burns the reviewer's trust
        # in every other row on the sheet.
        self.rejections: set[tuple[str, str]] = set()
        self.rows = 0

    # ---- construction -----------------------------------------------------
    @classmethod
    def from_kb(cls, kb) -> "PrefillIndex":
        """Build from a knowledge base - the offline path."""
        rows = kb.con.execute(
            "SELECT file_name, form_name, domain, field_key, field_text,"
            " normalized_text, annotation_text, annot_type, variable, link_score,"
            " trust, annotation_bbox, annotation_relative,"
            " relative_label, offset_x_pct, offset_y_pct,"
            " text_color, fill_color, font_name, font_size"
            " FROM field_annotations").fetchall()
        idx = cls._build(dict(r) for r in rows)
        idx._seed_rejections(kb.con.execute(
            "SELECT field_key, annotation_text FROM rejected_suggestions"))
        # The forms table knows a domain even for a form whose fields were never
        # linked - a domain header is form-level markup and reaches no field, so
        # learning domains from links alone loses exactly those forms.
        idx._seed_domains(kb.con.execute(
            "SELECT normalized_name, domain FROM forms WHERE domain IS NOT NULL"
            " AND domain != ''"))
        # Which page *of its form* each piece of form-level markup sat on. The
        # view knows the PDF page; the form's own first page turns that into an
        # offset, which is the only form portable to the next study. Computed
        # here rather than in the view because `forms.pages` is stored as JSON,
        # and reading it in Python beats picking it apart in SQL.
        first = _form_first_pages(kb)
        idx._load_form_annotations(
            dict(r, page_offset=_offset_from(r, first))
            for r in (dict(x) for x in kb.con.execute("SELECT * FROM form_annotations")))
        idx._seed_rejections(kb.con.execute(
            "SELECT form_key, annotation_text FROM rejected_form_suggestions"))
        return idx

    @classmethod
    def from_documents(cls, docs: Document | Iterable[Document]) -> "PrefillIndex":
        """Build straight from parsed documents, without persisting first."""
        from .template import placement_of

        docs = [docs] if isinstance(docs, Document) else list(docs)
        rows = []
        for doc in docs:
            for link in doc.links:
                if link.rejected:
                    continue
                fld, a = doc.field(link.field_id), doc.annotation(link.annotation_id)
                form = doc.form(fld.form_name) if fld else None
                if not (fld and a):
                    continue
                page = doc.page(a.page)
                w, h = (page.width or 1.0, page.height or 1.0) if page else (1.0, 1.0)
                label, off_x, off_y = placement_of(a.bbox, fld.bbox, w, h)
                rows.append({
                    "file_name": doc.path.rsplit("/", 1)[-1],
                    "form_name": fld.form_name,
                    "domain": form.domain if form else "",
                    "field_key": f"{normalize(fld.form_name)}|{fld.normalized_text}",
                    "field_text": fld.text, "normalized_text": fld.normalized_text,
                    "annotation_text": a.text, "annot_type": a.annot_type,
                    "variable": (a.parsed or {}).get("variable") or "",
                    "annotation_bbox": list(a.bbox.as_tuple()),
                    # Placement, computed exactly as `kb.build_kb` computes it for
                    # the SQLite path - the two must agree or a corpus ingested
                    # from disk would place markup differently from one held in
                    # memory, and only one of them could be right.
                    "annotation_relative": a.bbox.relative(w, h),
                    "text_color": a.text_color, "fill_color": a.fill_color,
                    "font_name": a.font_name, "font_size": a.font_size,
                    "relative_label": label,
                    "offset_x_pct": off_x,
                    "offset_y_pct": off_y,
                    "link_score": link.link_score,
                })
        idx = cls._build(rows)
        idx._seed_domains((f.normalized_name, f.domain)
                          for d in docs for f in d.forms if f.domain)
        idx._load_form_annotations(
            {"file_name": doc.path.rsplit("/", 1)[-1],
             "form_name": a.form_name,
             "normalized_name": normalize(a.form_name),
             "domain": (doc.form(a.form_name).domain if doc.form(a.form_name) else ""),
             "annotation_text": a.text, "annot_type": a.annot_type,
             "variable": (a.parsed or {}).get("variable") or "",
             "annotation_bbox": list(a.bbox.as_tuple()),
             "annotation_relative": a.bbox.relative(
                 (doc.page(a.page).width if doc.page(a.page) else 1.0) or 1.0,
                 (doc.page(a.page).height if doc.page(a.page) else 1.0) or 1.0),
             # Which page *of the form* it was on, never which page of the PDF -
             # the same reason templates store page offsets. A form's markup is
             # per page, and without this every page of a two-page questionnaire
             # is proposed the markup of both.
             "page_offset": a.page - (doc.form(a.form_name).first_page
                                      if doc.form(a.form_name) else a.page),
             "text_color": a.text_color, "fill_color": a.fill_color,
             "font_name": a.font_name, "font_size": a.font_size,
             "trust": GEOMETRIC}
            for doc in docs for a in doc.form_annotations() if a.form_name)
        return idx

    @classmethod
    def _build(cls, rows: Iterable[dict]) -> "PrefillIndex":
        idx = cls()
        for r in rows:
            idx.rows += 1
            form_key = r["field_key"].split("|", 1)[0]
            idx.by_text[r["normalized_text"]].append(r)
            idx.by_form[form_key].append(r)
            # Trust first, then score: a reviewer's decision must not lose a tie
            # to a lucky geometric match, however well that match scored.
            trust = r.get("trust") or GEOMETRIC
            key = (form_key, r["normalized_text"])
            # Kept per *statement*, not per key: a field that carried DSTERM and
            # RFICDTC has two answers, and collapsing them to one loses half the
            # markup the last reviewer approved.
            statement = statement_key(r["annotation_text"])
            prior = idx.key_sets[key].get(statement)
            if prior is None or _outranks(trust, r["link_score"], prior):
                approved = trust == HUMAN_APPROVED
                idx.key_sets[key][statement] = Candidate(
                    tier=EXACT_KEY,
                    confidence=CONF_APPROVED if approved else CONF_EXACT,
                    variable=r["variable"] or "", annotation_text=r["annotation_text"],
                    annot_type=r["annot_type"], trust=trust,
                    drawn_x=_left_edge(r.get("annotation_bbox")),
                    drawn=_drawn(r), style=_style(r),
                    source=f"{r['file_name']} · {r['form_name']} · {r['field_text']}",
                    evidence=[f"approved by a reviewer on {r['file_name']}" if approved
                              else f"(form, field_text) seen in {r['file_name']}"])
            if r.get("domain"):
                idx.form_domains[form_key][r["domain"].upper()] += 1
            if r["variable"]:
                idx.variable_labels[(form_key, r["variable"])].add(r["field_text"])
                idx._learn_suffix(r)
        idx._settle_key_sets()
        return idx

    def _settle_key_sets(self) -> None:
        """Order each key's statements and publish the head as `by_key`.

        Ordering is deliberate and stable: a reviewer's approval first, then the
        stronger evidence, then where the annotation was drawn - so a set comes
        back in the reading order it had on the page it came from rather than in
        alphabetical order, which no reviewer chose. The text breaks the last tie,
        so two equal statements never swap places between runs and make a
        workbook diff meaningless.
        """
        for key, statements in self.key_sets.items():
            ranked = sorted(statements.values(),
                            key=lambda c: (-TRUST_RANK.get(c.trust, 1), -c.confidence,
                                           c.drawn_x, c.annotation_text))
            self.key_sets[key] = {statement_key(c.annotation_text): c for c in ranked}
            self.by_key[key] = ranked[0]

    def _load_form_annotations(self, rows: Iterable[dict]) -> None:
        """Index the markup a form carries in its own right.

        Same shape as `key_sets` one level up: distinct statements per key, best
        evidence per statement, then ordered by where they were drawn so the set
        comes back in the order the last study read it in - which for a row of
        domain headers across the top of a page is left to right.
        """
        for r in rows:
            key = r["normalized_name"]
            if not key or not (r.get("annotation_text") or "").strip():
                continue
            trust = r.get("trust") or GEOMETRIC
            # Keyed by statement, page of the form, *and* where on that page it
            # was drawn - an occurrence, not a conclusion, which is what the
            # knowledge base stores everywhere else for the same reason.
            #
            # Both extra parts of the key earn their place. Without the page, a
            # two-page eligibility form that prints `VISIT` and `IEDTC` at the
            # top of both pages gives the pair to whichever page wins and leaves
            # the other bare. Without the position, a page that says
            # `[NOT SUBMITTED]` against three different questions gets one.
            statement = (statement_key(r["annotation_text"]), r.get("page_offset"),
                         _spot(r))
            prior = self.form_sets[key].get(statement)
            score = 1.0 if trust == HUMAN_APPROVED else CONF_EXACT
            if prior is None or _outranks(trust, score, prior):
                approved = trust == HUMAN_APPROVED
                self.form_sets[key][statement] = Candidate(
                    tier=EXACT_KEY,
                    confidence=CONF_APPROVED if approved else CONF_EXACT,
                    variable=r.get("variable") or "",
                    annotation_text=r["annotation_text"],
                    annot_type=r.get("annot_type") or "", trust=trust,
                    drawn_x=_left_edge(r.get("annotation_bbox")),
                    drawn=_drawn(r), style=_style(r),
                    file_name=str(r.get("file_name") or ""),
                    source=f"{r.get('file_name', '')} · {r.get('form_name', '')}",
                    evidence=[f"form-level markup on {r.get('form_name')!r} in "
                              f"{r.get('file_name')}"])
            if r.get("annot_type") == "DOMAIN_HEADER" and r.get("domain"):
                self.domain_headers[str(r["domain"]).upper()][r["annotation_text"]] += 1
        for key, statements in self.form_sets.items():
            kept = _one_study_per_statement(statements.values())
            ranked = sorted(kept, key=lambda c: (_statement_page(c) or 0,
                                                 -TRUST_RANK.get(c.trust, 1), -c.confidence,
                                                 c.drawn_x, c.annotation_text))
            self.form_sets[key] = {
                (statement_key(c.annotation_text), _statement_page(c),
                 _spot({"annotation_relative": c.drawn})): c for c in ranked}

    def _seed_rejections(self, pairs) -> None:
        for key, annotation in pairs:
            if key and annotation:
                self.rejections.add((str(key), str(annotation)))

    def _seed_domains(self, pairs) -> None:
        for name, domain in pairs:
            if name and domain:
                self.form_domains[str(name)][str(domain).upper()] += 1

    def _learn_suffix(self, r: dict) -> None:
        """Learn SDTM's prefix convention: MHSTDTC under domain MH teaches STDTC.

        This is what lets a form whose domain was never seen still be pre-filled:
        the convention is a property of SDTM, and the corpus is only evidence of
        which labels map to which role.
        """
        domain, var = (r.get("domain") or "").upper(), (r["variable"] or "").upper()
        if not domain or not var.startswith(domain) or len(var) <= len(domain):
            return
        suffix = var[len(domain):]
        if _SUFFIX_OK.match(suffix):
            self.suffixes[r["normalized_text"]][suffix].add(domain)

    # ---- lookup -----------------------------------------------------------
    def match(self, fld: Field, domain: str = "") -> Prefill:
        """Every tier that fires for this field, best first."""
        form_key, text_key = normalize(fld.form_name), fld.normalized_text
        found = [c for c in (
            self._exact(form_key, text_key),
            self._consensus(form_key, text_key),
            self._domain_pattern(text_key, domain),
            self._fuzzy(form_key, text_key),
        ) if c]
        field_key = f"{form_key}|{text_key}"
        found = self._drop_rejected(field_key, found)
        found.sort(key=lambda c: -c.confidence)
        best = found[0] if found else Candidate(
            tier=NEEDS_MAPPING, confidence=0.0,
            evidence=["no field in the corpus reaches this label"])
        companions = (self._companions(form_key, text_key, field_key, best)
                      if best.tier == EXACT_KEY else [])
        return Prefill(field_id=fld.id, form_name=fld.form_name, field_text=fld.text,
                       best=best, alternates=found[1:], companions=companions,
                       aliases=self._aliases(form_key, best.variable, fld.text))

    def _companions(self, form_key: str, text_key: str, field_key: str,
                    best: Candidate) -> list[Candidate]:
        """The rest of the statements this exact key was seen carrying.

        Each becomes its own workbook row, so each is filtered against the
        rejection list on its own - a reviewer who struck DSSTDTC off this field
        last study must not get it back because DSTERM happened to survive.
        """
        rest = [_fresh(c) for k, c in self.key_sets.get((form_key, text_key), {}).items()
                if k != statement_key(best.annotation_text)]
        kept = [c for c in self._drop_rejected(field_key, rest) if c.confidence > 0]
        for c in kept:
            c.evidence.append("carried alongside "
                              f"{best.annotation_text!r} on the same field")
        return kept[:MAX_ANNOTATIONS_PER_FIELD - 1]

    def _drop_rejected(self, field_key: str, found: list[Candidate]) -> list[Candidate]:
        """Never re-propose what a reviewer already turned down for this field."""
        kept = []
        for c in found:
            if c.annotation_text and (field_key, c.annotation_text) in self.rejections:
                c.confidence = 0.0
                c.evidence.append("a reviewer rejected this suggestion previously")
            kept.append(c)
        return kept

    def match_form(self, anchor: FormAnchor, domain: str = "",
                   page_offset: int | None = None) -> Prefill:
        """What markup this form carries in its own right, as a set of rows.

        Three outcomes, and the third is the one that matters most:

        * the form was annotated before - return the whole set it carried, in
          the order it was drawn, one row each.
        * the form is new but its domain is known - propose the domain header
          alone, in whatever wording the corpus writes it in. One row, and it is
          a suggestion, not an answer: a page carrying a second domain's markup
          is common and no amount of history about *this* form can predict it.
        * nothing at all - one NEEDS_MAPPING row. This is the fix. Previously
          the form-level layer was silently absent from the sheet, so nobody was
          ever asked about it and the annotated PDF came out missing its headers.

        `page_offset` is which page *of this form* the anchor is - 0 for its
        first page. A form's markup belongs to particular pages of it, so a
        two-page questionnaire whose second page carries eleven `QSORRES ...`
        statements must not have all eleven proposed on its first page as well.
        Statements the corpus recorded no page for are offered on every page,
        which is the honest answer when there is nothing to place them by.
        """
        key = normalize(anchor.form_name)
        form_key = f"form|{key}"
        seen = [_fresh(c) for c in self.form_sets.get(key, {}).values()]
        seen = [c for c in self._drop_rejected(form_key, seen) if c.confidence > 0]
        if page_offset is not None:
            seen = [c for c in seen
                    if _statement_page(c) in (None, page_offset)]
        if seen:
            return Prefill(field_id=anchor.id, form_name=anchor.form_name,
                           field_text=anchor.text, best=seen[0],
                           companions=seen[1:MAX_ANNOTATIONS_PER_FORM])

        proposed = self._domain_header(domain or anchor.domain, form_key)
        best = proposed or Candidate(
            tier=NEEDS_MAPPING, confidence=0.0,
            annot_type="DOMAIN_HEADER",
            evidence=["no form-level markup in the corpus for this form, and no "
                      "domain to propose a header from - name the form's "
                      "domain(s) here"])
        return Prefill(field_id=anchor.id, form_name=anchor.form_name,
                       field_text=anchor.text, best=best)

    def _domain_header(self, domain: str, form_key: str) -> Candidate | None:
        """The header text this corpus writes for a domain, if it writes one."""
        if not domain:
            return None
        wordings = self.domain_headers.get(domain.upper())
        text = (wordings.most_common(1)[0][0] if wordings else f"{domain.upper()}=")
        if not wordings:
            return None          # never invent a domain label we have not seen
        candidate = Candidate(
            tier=DOMAIN_PATTERN, confidence=0.7, annotation_text=text,
            annot_type="DOMAIN_HEADER", source=f"corpus header wording for {domain}",
            evidence=[f"this form is {domain}; the corpus writes its header as "
                      f"{text!r}", "a page may carry a second domain's markup - "
                      "add a row for each"])
        kept = self._drop_rejected(form_key, [candidate])[0]
        return kept if kept.confidence > 0 else None

    def domain_for(self, form_name: str) -> str:
        """The domain history assigns this form, when the document cannot say."""
        counts = self.form_domains.get(normalize(form_name))
        return counts.most_common(1)[0][0] if counts else ""

    def _exact(self, form_key: str, text_key: str) -> Candidate | None:
        return _fresh(self.by_key.get((form_key, text_key)))

    def _consensus(self, form_key: str, text_key: str) -> Candidate | None:
        """This label under *other* forms. Only offered when they all agree.

        Disagreement is the finding: if one label maps to three variables
        depending on the form, the form is load-bearing and text alone must not
        be trusted. That is discovered here, not assumed.
        """
        rows = [r for r in self.by_text.get(text_key, [])
                if r["field_key"].split("|", 1)[0] != form_key and r["variable"]]
        if not rows:
            return None
        variables = {r["variable"] for r in rows}
        forms = {r["form_name"] for r in rows}
        if len(variables) > 1:
            return Candidate(
                tier=CROSS_FORM_CONSENSUS, confidence=0.0,
                evidence=[f"label maps to {sorted(variables)} depending on the form - "
                          "the form is load-bearing, so no cross-form suggestion"])
        if len(forms) < MIN_CONSENSUS_FORMS:
            return None
        r = rows[0]
        return Candidate(
            tier=CROSS_FORM_CONSENSUS,
            confidence=round(min(0.85, 0.55 + 0.05 * len(forms)), 3),
            variable=r["variable"], annotation_text=r["annotation_text"],
            annot_type=r["annot_type"],
            source=f"{r['file_name']} · {r['form_name']} · {r['field_text']}",
            evidence=[f"same label maps to {r['variable']} in all "
                      f"{len(forms)} forms that use it"])

    def _domain_pattern(self, text_key: str, domain: str) -> Candidate | None:
        """Apply SDTM's prefix convention to this form's domain."""
        if not domain:
            return None
        options = self.suffixes.get(text_key)
        if not options:
            return None
        suffix, domains = max(options.items(), key=lambda kv: len(kv[1]))
        if len(domains) < MIN_PATTERN_DOMAINS:
            return None
        if len(options) > 1:            # the label plays different roles elsewhere
            return None
        return Candidate(
            tier=DOMAIN_PATTERN,
            confidence=round(min(0.85, 0.6 + 0.05 * len(domains)), 3),
            variable=f"{domain}{suffix}", annotation_text=f"{domain}{suffix}",
            annot_type="VARIABLE",
            source=f"convention {suffix} in {', '.join(sorted(domains))}",
            evidence=[f"'{text_key}' became {suffix} in {len(domains)} domains "
                      f"({', '.join(sorted(domains))}); this form is {domain}"])

    def _fuzzy(self, form_key: str, text_key: str) -> Candidate | None:
        """Nearest wording within the same form. Never across forms."""
        best_row, best_sim = None, FUZZY_FLOOR
        for r in self.by_form.get(form_key, []):
            if not r["variable"]:
                continue
            sim = similarity(text_key, r["normalized_text"])
            if sim > best_sim:
                best_row, best_sim = r, sim
        if not best_row:
            return None
        return Candidate(
            tier=FUZZY_SAME_FORM,
            confidence=round(0.5 + 0.35 * (best_sim - FUZZY_FLOOR) / (1 - FUZZY_FLOOR), 3),
            variable=best_row["variable"], annotation_text=best_row["annotation_text"],
            annot_type=best_row["annot_type"],
            source=f"{best_row['file_name']} · {best_row['form_name']} · {best_row['field_text']}",
            evidence=[f"same form; wording {best_sim:.2f} similar to "
                      f"{best_row['field_text']!r}"])

    def _aliases(self, form_key: str, variable: str, current: str) -> list[str]:
        """Other labels this form has used for the same variable - reviewer context."""
        if not variable:
            return []
        return sorted(l for l in self.variable_labels.get((form_key, variable), set())
                      if normalize(l) != normalize(current))


def _left_edge(bbox: Any) -> float:
    """x0 out of a stored bbox, whatever shape it arrives in. 0.0 when unknown."""
    if isinstance(bbox, str):
        try:
            bbox = json.loads(bbox)
        except ValueError:
            return 0.0
    try:
        return float(bbox[0])
    except (TypeError, ValueError, IndexError, KeyError):
        return 0.0


# Placement facts a corpus row may carry: the annotation's own page-relative box,
# and - only where it was linked to a field - what that box was relative to the
# field. Both are optional, and a row missing them simply falls back to the
# house style, which is what every row did before they existed.
_PLACEMENT_KEYS = ("relative_label", "offset_x_pct", "offset_y_pct", "page_offset")


def _drawn(row: dict) -> dict[str, Any]:
    """Where one stored annotation was drawn, in re-placeable form.

    Tolerant about shape on purpose: the same fields arrive as JSON strings from
    SQLite and as dicts from an in-memory parse, and a corpus written before
    these columns existed has neither. A missing value is not an error - it means
    the house style answers for that row.
    """
    out: dict[str, Any] = {}
    rel = row.get("annotation_relative")
    if isinstance(rel, str):
        try:
            rel = json.loads(rel)
        except ValueError:
            rel = None
    if isinstance(rel, dict):
        for k, v in rel.items():
            if k.startswith("rel_") and isinstance(v, (int, float)):
                out[k] = float(v)
    for k in _PLACEMENT_KEYS:
        v = row.get(k)
        if v not in (None, ""):
            out[k] = v if isinstance(v, str) else float(v)
    return out


def _form_first_pages(kb) -> dict[tuple[str, str], int]:
    """(file, normalized form name) -> the form's first page, from the corpus.

    Keyed by file as well as form because two studies in one corpus each have
    their own copy of a form, on their own pages.
    """
    out: dict[tuple[str, str], int] = {}
    for r in kb.con.execute(
            "SELECT d.file_name, fm.normalized_name, fm.pages FROM forms fm"
            " JOIN documents d ON d.id = fm.document_id"):
        r = dict(r)
        try:
            pages = json.loads(r["pages"] or "[]")
        except ValueError:
            pages = []
        if pages:
            out[(r["file_name"], r["normalized_name"])] = min(int(p) for p in pages)
    return out


def _offset_from(row: dict, first: dict[tuple[str, str], int]) -> int | None:
    page = row.get("page")
    start = first.get((row.get("file_name"), row.get("normalized_name")))
    return int(page) - start if page is not None and start is not None else None


_STYLE_KEYS = ("text_color", "fill_color", "font_name", "font_size")


def _style(row: dict) -> dict[str, Any]:
    """How one stored annotation was rendered, in the form the workbook wants.

    Colours arrive as RGB tuples in memory and as JSON strings from SQLite, and
    a corpus written before these columns existed has neither - a missing value
    simply means the house style answers for that row, which is what every row
    did before this existed.
    """
    out: dict[str, Any] = {}
    for k in _STYLE_KEYS:
        v = row.get(k)
        if isinstance(v, str) and v.startswith("["):
            try:
                v = json.loads(v)
            except ValueError:
                v = None
        if v in (None, "", []):
            continue
        out[k] = tuple(v) if isinstance(v, (list, tuple)) else v
    return out


# How finely two drawn positions have to differ to be different occurrences.
# One percent of the page: far coarser than the placement it feeds, because the
# question here is only "is this the same piece of markup or another one", and
# the same statement re-drawn in the same spot by a second study should fold
# into one candidate rather than becoming a second row on the sheet.
SPOT_PCT = 2


def _spot(row: dict) -> tuple[float, float] | None:
    """Where a stored annotation sat, rounded to `SPOT_PCT` decimal places."""
    rel = row.get("annotation_relative")
    if isinstance(rel, str):
        try:
            rel = json.loads(rel)
        except ValueError:
            rel = None
    if not isinstance(rel, dict):
        return None
    x, y = rel.get("rel_x_pct"), rel.get("rel_y_pct")
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None
    return (round(float(x), SPOT_PCT), round(float(y), SPOT_PCT))


def _one_study_per_statement(candidates: Iterable["Candidate"]) -> list["Candidate"]:
    """Collapse a form's markup so one study answers for each statement.

    `form_sets` keys on where a statement was drawn as well as on its text,
    which is right: a page that says `[NOT SUBMITTED]` against three different
    questions carries three statements, and keying on text alone would keep one.
    But across studies that same key stops discriminating - two sponsors draw
    `DS=Disposition` at 8% and 12% from the left, both survive, and the page
    comes out with the header on it twice. Every form-level statement in a
    corpus of N studies was drawn N times.

    The signal that separates the two cases is the file. Repetition *within* one
    study is real; repetition *across* studies is one statement seen twice. So
    for each (statement, page of the form) one study wins and supplies its whole
    set of positions - never a union. The winner is the one with the strongest
    evidence: highest trust, then confidence, then the fullest record of the
    statement, then file name so a tie is at least deterministic.

    Occurrences with no file recorded are left alone, all of them. That is a
    corpus written before this column existed, and guessing which of them are
    the same study would lose the repetitions this exists to protect.
    """
    groups: dict[tuple, dict[str, list["Candidate"]]] = defaultdict(lambda: defaultdict(list))
    for c in candidates:
        groups[(statement_key(c.annotation_text),
                _statement_page(c))][c.file_name].append(c)
    kept: list["Candidate"] = []
    for by_file in groups.values():
        if len(by_file) == 1 or "" in by_file:
            kept.extend(c for cs in by_file.values() for c in cs)
            continue
        winner = max(by_file.items(),
                     key=lambda kv: (max(TRUST_RANK.get(c.trust, 1) for c in kv[1]),
                                     max(c.confidence for c in kv[1]),
                                     len(kv[1]), kv[0]))
        kept.extend(winner[1])
    return kept


def _statement_page(candidate: "Candidate") -> int | None:
    """Which page of its form a form-level statement was drawn on, if recorded."""
    offset = candidate.drawn.get("page_offset")
    return int(offset) if offset is not None else None


def _fresh(candidate: Candidate | None) -> Candidate | None:
    """A per-field copy of an indexed candidate.

    `_drop_rejected` edits the candidate it is handed - it zeroes the score and
    says why. Handing out the stored object would make that edit permanent for
    every later field that looks the same key up, so the index only ever lends
    copies.
    """
    if candidate is None:
        return None
    return replace(candidate, evidence=list(candidate.evidence))


def _outranks(trust: str, score: float, prior: Candidate) -> bool:
    """Higher trust always wins; within the same trust, the better link wins."""
    mine, theirs = TRUST_RANK.get(trust, 1), TRUST_RANK.get(prior.trust, 1)
    return (mine, score) > (theirs, prior.confidence)


def similarity(a: str, b: str) -> float:
    """How close two CRF labels are. Deterministic, stdlib only.

    Three signals, because each alone fails on a re-wording that really happens:

    * sequence ratio - catches character-level drift, but reads
      "Start Date" vs "Start Date of Condition" as only 0.61 because the extra
      words dominate the length.
    * containment - the share of the *shorter* label found in the longer one.
      This is the qualifier case: "Start Date" fully inside "Start Date of
      Condition" is strong evidence, and Jaccard punishes it for being short.
      Suppressed for single-token labels, where "Date" would otherwise sit
      fully inside "Start Date" and score 1.0.
    * Jaccard - keeps containment honest by still caring about the words the
      longer label added.

    Tokens are lightly stemmed (trailing "s") because "Condition" and
    "Conditions" are one field, and raw token sets score them 0.0 alike.
    """
    if not a or not b:
        return 0.0
    seq = SequenceMatcher(None, a, b).ratio()
    ta, tb = _stems(a), _stems(b)
    if not (ta and tb):
        return round(seq, 4)
    shared = len(ta & tb)
    jaccard = shared / len(ta | tb)
    contain = shared / min(len(ta), len(tb))
    if min(len(ta), len(tb)) == 1:      # a one-word label proves nothing by fitting
        contain = jaccard
    return round(0.4 * seq + 0.35 * contain + 0.25 * jaccard, 4)


def _stems(text: str) -> set[str]:
    """Crude singularisation - enough for CRF noun phrases, no dependency."""
    return {t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t
            for t in text.split()}


def prefill_document(doc: Document, index: PrefillIndex) -> list[Prefill]:
    """Pre-fill every field of a blank CRF against the corpus."""
    out = []
    for page in doc.pages:
        for fld in page.fields:
            out.append(index.match(fld, domain=page_domain(doc, page, index)))
    return out


def prefill_forms(doc: Document, index: PrefillIndex) -> list[Prefill]:
    """Pre-fill the form-level layer: one result per page that belongs to a form."""
    return [index.match_form(page.anchor, domain=page_domain(doc, page, index),
                             page_offset=_form_page_offset(doc, page))
            for page in doc.pages if page.anchor is not None]


def _form_page_offset(doc: Document, page) -> int | None:
    """Which page of its own form this page is - 0 for the form's first.

    An offset, never a PDF page number, for the reason templates give: Medical
    History being pages 2-3 here and 11-12 in the next study is not a difference.
    """
    form = doc.form(page.form_name)
    return page.number - form.first_page if form and form.pages else None


def page_domain(doc: Document, page, index: PrefillIndex) -> str:
    """This page's SDTM domain: what the document says, else what history says.

    A *blank* CRF carries no domain headers, so its own answer is empty on every
    page - which is exactly the input that needs the corpus most.
    """
    form = doc.form(page.form_name)
    own = (form.domain if form else "") or page.form_domain
    return own or index.domain_for(page.form_name)


def summarize_prefill(results: list[Prefill]) -> dict[str, Any]:
    """How much of the blank CRF history answered - the number worth tracking."""
    tiers: dict[str, int] = {}
    statuses: dict[str, int] = {}
    for r in results:
        tiers[r.best.tier] = tiers.get(r.best.tier, 0) + 1
        statuses[r.status] = statuses.get(r.status, 0) + 1
    total = len(results) or 1
    multi = [r for r in results if r.companions]
    return {
        "fields": len(results),
        "by_tier": dict(sorted(tiers.items())),
        "by_status": dict(sorted(statuses.items())),
        "auto_fill_rate": round(statuses.get(AUTO, 0) / total, 3),
        "reaches_agent": statuses.get(NEEDS_MAPPING_STATUS, 0),
        # Rows, not fields: what the workbook will actually be this many of.
        "annotations": sum(len(r.annotations) for r in results),
        "multi_annotation_fields": len(multi),
    }
