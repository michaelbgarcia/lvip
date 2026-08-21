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
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field as dc_field
from difflib import SequenceMatcher
from typing import Any, Iterable

from .models import GEOMETRIC, HUMAN_APPROVED, TRUST_RANK, Document, Field
from .normalize import normalize

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
    trust: str = GEOMETRIC
    evidence: list[str] = dc_field(default_factory=list)


@dataclass
class Prefill:
    """The pre-fill verdict for one field of the blank CRF."""
    field_id: str
    form_name: str
    field_text: str
    best: Candidate
    alternates: list[Candidate] = dc_field(default_factory=list)
    aliases: list[str] = dc_field(default_factory=list)   # other labels for this variable

    @property
    def status(self) -> str:
        """Only an exact hit ships unreviewed; everything fuzzy is a suggestion."""
        if self.best.tier == EXACT_KEY and self.best.confidence >= AUTO_THRESHOLD:
            return AUTO
        return NEEDS_REVIEW if self.best.confidence >= REVIEW_THRESHOLD else NEEDS_MAPPING_STATUS


class PrefillIndex:
    """Everything the corpus knows, arranged for lookup. Built once, queried per field."""

    def __init__(self) -> None:
        self.by_key: dict[tuple[str, str], Candidate] = {}
        self.by_text: dict[str, list[dict]] = defaultdict(list)
        self.by_form: dict[str, list[dict]] = defaultdict(list)
        self.suffixes: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        self.variable_labels: dict[tuple[str, str], set[str]] = defaultdict(set)
        # form -> SDTM domain, learned from history. A *blank* CRF carries no
        # domain-header annotations, so its own domain is always empty - without
        # this, DOMAIN_PATTERN could never fire on the one input that needs it.
        self.form_domains: dict[str, Counter] = defaultdict(Counter)
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
            " trust FROM field_annotations").fetchall()
        idx = cls._build(dict(r) for r in rows)
        idx._seed_rejections(kb.con.execute(
            "SELECT field_key, annotation_text FROM rejected_suggestions"))
        # The forms table knows a domain even for a form whose fields were never
        # linked - a domain header is form-level markup and reaches no field, so
        # learning domains from links alone loses exactly those forms.
        idx._seed_domains(kb.con.execute(
            "SELECT normalized_name, domain FROM forms WHERE domain IS NOT NULL"
            " AND domain != ''"))
        return idx

    @classmethod
    def from_documents(cls, docs: Document | Iterable[Document]) -> "PrefillIndex":
        """Build straight from parsed documents, without persisting first."""
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
                rows.append({
                    "file_name": doc.path.rsplit("/", 1)[-1],
                    "form_name": fld.form_name,
                    "domain": form.domain if form else "",
                    "field_key": f"{normalize(fld.form_name)}|{fld.normalized_text}",
                    "field_text": fld.text, "normalized_text": fld.normalized_text,
                    "annotation_text": a.text, "annot_type": a.annot_type,
                    "variable": (a.parsed or {}).get("variable") or "",
                    "link_score": link.link_score,
                })
        idx = cls._build(rows)
        idx._seed_domains((f.normalized_name, f.domain)
                          for d in docs for f in d.forms if f.domain)
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
            prior = idx.by_key.get(key)
            if prior is None or _outranks(trust, r["link_score"], prior):
                approved = trust == HUMAN_APPROVED
                idx.by_key[key] = Candidate(
                    tier=EXACT_KEY,
                    confidence=CONF_APPROVED if approved else CONF_EXACT,
                    variable=r["variable"] or "", annotation_text=r["annotation_text"],
                    annot_type=r["annot_type"], trust=trust,
                    source=f"{r['file_name']} · {r['form_name']} · {r['field_text']}",
                    evidence=[f"approved by a reviewer on {r['file_name']}" if approved
                              else f"(form, field_text) seen in {r['file_name']}"])
            if r.get("domain"):
                idx.form_domains[form_key][r["domain"].upper()] += 1
            if r["variable"]:
                idx.variable_labels[(form_key, r["variable"])].add(r["field_text"])
                idx._learn_suffix(r)
        return idx

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
        found = self._drop_rejected(f"{form_key}|{text_key}", found)
        found.sort(key=lambda c: -c.confidence)
        best = found[0] if found else Candidate(
            tier=NEEDS_MAPPING, confidence=0.0,
            evidence=["no field in the corpus reaches this label"])
        return Prefill(field_id=fld.id, form_name=fld.form_name, field_text=fld.text,
                       best=best, alternates=found[1:],
                       aliases=self._aliases(form_key, best.variable, fld.text))

    def _drop_rejected(self, field_key: str, found: list[Candidate]) -> list[Candidate]:
        """Never re-propose what a reviewer already turned down for this field."""
        kept = []
        for c in found:
            if c.annotation_text and (field_key, c.annotation_text) in self.rejections:
                c.confidence = 0.0
                c.evidence.append("a reviewer rejected this suggestion previously")
            kept.append(c)
        return kept

    def domain_for(self, form_name: str) -> str:
        """The domain history assigns this form, when the document cannot say."""
        counts = self.form_domains.get(normalize(form_name))
        return counts.most_common(1)[0][0] if counts else ""

    def _exact(self, form_key: str, text_key: str) -> Candidate | None:
        return self.by_key.get((form_key, text_key))

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
        form = doc.form(page.form_name)
        own = (form.domain if form else "") or page.form_domain
        domain = own or index.domain_for(page.form_name)
        for fld in page.fields:
            out.append(index.match(fld, domain=domain))
    return out


def summarize_prefill(results: list[Prefill]) -> dict[str, Any]:
    """How much of the blank CRF history answered - the number worth tracking."""
    tiers: dict[str, int] = {}
    statuses: dict[str, int] = {}
    for r in results:
        tiers[r.best.tier] = tiers.get(r.best.tier, 0) + 1
        statuses[r.status] = statuses.get(r.status, 0) + 1
    total = len(results) or 1
    return {
        "fields": len(results),
        "by_tier": dict(sorted(tiers.items())),
        "by_status": dict(sorted(statuses.items())),
        "auto_fill_rate": round(statuses.get(AUTO, 0) / total, 3),
        "reaches_agent": statuses.get(NEEDS_MAPPING_STATUS, 0),
    }
