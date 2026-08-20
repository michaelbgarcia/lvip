"""Phases 4-5 - annotation extraction and classification.

Phase 4 takes the raw `Annotation` objects Phase 1 pulled off the PDF, gives
each a document-stable id and its page's form name, and splits an annotation box
into the separate statements it actually holds: study annotators routinely put
"AESTDTC AEENDTC" in one box, and a box that maps to two variables must not be
linked to a field as if it were one.

Phase 5 classifies each statement. Every branch is a regex plus a reason, so the
type of any annotation can be explained from `type_evidence` alone:

    DOMAIN_HEADER        DM=Demographics
    VARIABLE             BRTHDTC / DM.BRTHDTC
    CONSTANT_ASSIGNMENT  MHENRF=ONGOING
    SUPP_QUALIFIER       RACEOTH when SUPPDM.QNAM=RACEOTH
    NOT_SUBMITTED        [NOT SUBMITTED]
    CROSS_REFERENCE      See Page 7
    DERIVATION_RULE      AGE derived from BRTHDTC and RFSTDTC
    NOTE                 anything else - free prose, kept, never guessed at

Order matters and runs most-specific first: "SUPPDS.QVAL when QNAM = "PROTVER""
contains an `=` and would read as a constant assignment if the SUPP test did not
come first.

The one genuinely ambiguous shape is `XX=YYYY` with a two-letter left side and an
all-caps right side - "DS=COMPLETED" is a constant, "DS=DISPOSITION" is a domain
header. It is resolved with two deterministic signals, the CDISC domain list and
position on the page (a domain header is the first markup on its page), and the
loser is recorded in the evidence rather than thrown away.
"""
from __future__ import annotations

import re

from .models import Annotation, AnnotationPart, Page

# --- annotation types ------------------------------------------------------
DOMAIN_HEADER = "DOMAIN_HEADER"
VARIABLE = "VARIABLE"
CONSTANT_ASSIGNMENT = "CONSTANT_ASSIGNMENT"
SUPP_QUALIFIER = "SUPP_QUALIFIER"
NOT_SUBMITTED = "NOT_SUBMITTED"
CROSS_REFERENCE = "CROSS_REFERENCE"
DERIVATION_RULE = "DERIVATION_RULE"
NOTE = "NOTE"

# CDISC SDTM domain codes. Reference data, used only as a confidence signal -
# an unknown two-letter code still classifies, it just scores lower.
DOMAINS = frozenset("""
AE AG BE BS CE CM CO CP CV DA DD DM DS DV EC EG EX FA FT GF HO IE IS LB MB MH
MI MK ML MS NV OE PC PE PP PR QS RE RELREC RP RS SC SE SR SS SU SV TA TD TE TI
TM TR TS TU TV UR VS ZA
""".split())

_VAR_TOKEN = r"[A-Z][A-Z0-9]{1,7}"
_QUALIFIED = rf"(?:(?P<dom>[A-Z]{{2}})\.)?(?P<var>{_VAR_TOKEN})"

_NOT_SUBMITTED = re.compile(r"^\W*not\s+submitted\W*$", re.I)
_SEE_PAGE = re.compile(r"\bsee\s+(?:page|pg\.?)\s*(\d+)", re.I)
_SUPP = re.compile(r"\bSUPP(?P<dom>[A-Z]{2})?\b|\bQNAM\b", re.I)
_SUPP_VAR = re.compile(rf"SUPP[A-Z]{{2}}\.(?P<var>{_VAR_TOKEN})", re.I)
_QNAM_VALUE = re.compile(r"QNAM\s*=\s*[\"']?(?P<qnam>[A-Z][A-Z0-9_]*)[\"']?", re.I)
_ASSIGNMENT = re.compile(rf"^\s*{_QUALIFIED}\s*=\s*(?P<value>.+?)\s*$")
_PURE_VAR = re.compile(rf"^\s*{_QUALIFIED}\s*$")
_VAR_LIST = re.compile(rf"^\s*{_VAR_TOKEN}(?:\.{_VAR_TOKEN})?"
                       rf"(?:\s*[,/|]\s*|\s+){_VAR_TOKEN}(?:\.{_VAR_TOKEN})?.*$")
_DERIVATION = re.compile(
    r"\b(deriv\w*|calculat\w*|comput\w*|concatenat\w*|imput\w*|assigned\s+by|"
    r"populated\s+(?:from|by)|mapped\s+(?:to|from)|if\b.*\bthen\b|not\s+collected)\b", re.I)
_CODED_VALUE = re.compile(r"^[\"']?[A-Z0-9][A-Z0-9 _\-/]*[\"']?$")
_SPLIT = re.compile(r"\s*[,/|]\s*|\s+")

CONF_STRUCTURAL = 0.95   # the pattern is unambiguous
CONF_LIKELY = 0.8
CONF_AMBIGUOUS = 0.55
CONF_FALLBACK = 0.3


def extract_annotations(pages: list[Page]) -> list[Annotation]:
    """Phases 4+5 over a whole document: id, split, classify."""
    out: list[Annotation] = []
    for page in pages:
        for i, a in enumerate(page.annotations):
            a.id = f"p{page.number}a{i}"
            a.form_name = page.form_name
            a.parts = [classify(t, first=i == 0) for t in split_parts(a.text)]
            _roll_up(a)
            out.append(a)
    return out


def _roll_up(a: Annotation) -> None:
    """The annotation's own type is its strongest part; the rest stay in `parts`."""
    if not a.parts:
        a.annot_type, a.type_confidence, a.type_evidence = NOTE, 0.0, ["empty annotation"]
        a.parsed = {}
        return
    best = max(a.parts, key=lambda p: p.confidence)
    a.annot_type, a.type_confidence = best.annot_type, best.confidence
    a.type_evidence = list(best.evidence)
    a.parsed = dict(best.parsed)
    if len(a.parts) > 1:
        a.parsed["parts"] = [{"text": p.text, "type": p.annot_type, **p.parsed}
                             for p in a.parts]
        a.type_evidence.append(f"{len(a.parts)} statements in one box")


# --- Phase 4: splitting ----------------------------------------------------
def split_parts(text: str) -> list[str]:
    """Split a box holding several variables into one statement each.

    Only a *pure* variable list splits. Anything with prose, an `=`, brackets or
    a "when" clause stays whole: "SUPPDS.QVAL when QNAM = "PROTVER"" is one
    statement and splitting it on whitespace would destroy it.
    """
    text = (text or "").strip()
    if not text:
        return []
    if "=" in text or not _VAR_LIST.match(text):
        return [text]
    parts = [p for p in _SPLIT.split(text) if p]
    return parts if all(_PURE_VAR.match(p) for p in parts) else [text]


# --- Phase 5: classification ----------------------------------------------
def classify(text: str, first: bool = False) -> AnnotationPart:
    """Classify one markup statement. Most-specific pattern first.

    `first` says this is the page's topmost annotation - the only signal that
    separates "DS=COMPLETED" from an all-caps domain header.
    """
    t = (text or "").strip()
    part = AnnotationPart(text=t)
    for rule in (_as_not_submitted, _as_cross_reference, _as_supp_qualifier,
                 _as_assignment, _as_derivation, _as_variable):
        hit = rule(t, first)
        if hit:
            part.annot_type, part.confidence, part.parsed, part.evidence = hit
            return part
    part.annot_type, part.confidence = NOTE, CONF_FALLBACK
    part.evidence = ["no markup pattern matched; kept as prose"]
    return part


def _as_not_submitted(t: str, first: bool):
    if _NOT_SUBMITTED.match(t):
        return NOT_SUBMITTED, CONF_STRUCTURAL, {}, ["matches [NOT SUBMITTED]"]
    return None


def _as_cross_reference(t: str, first: bool):
    m = _SEE_PAGE.search(t)
    if m:
        return (CROSS_REFERENCE, CONF_STRUCTURAL, {"target_page": int(m.group(1))},
                [f"see-page pointer to page {m.group(1)}"])
    return None


def _as_supp_qualifier(t: str, first: bool):
    """SUPPQUAL markup. Checked before assignment: it contains an `=` too."""
    if not _SUPP.search(t):
        return None
    parsed: dict = {}
    ev = ["mentions SUPP/QNAM"]
    dom = re.search(r"SUPP(?P<dom>[A-Z]{2})", t, re.I)
    if dom:
        parsed["domain"] = dom.group("dom").upper()
        ev.append(f"supp domain {parsed['domain']}")
    qnam = _QNAM_VALUE.search(t)
    if qnam:
        parsed["qnam"] = qnam.group("qnam").upper()
        ev.append(f"QNAM={parsed['qnam']}")
    lead = _PURE_VAR.match(t.split(" when ")[0].strip())
    supp_var = _SUPP_VAR.search(t)
    if lead:                        # "RACEOTH when SUPPDM.QNAM=RACEOTH"
        parsed["variable"] = lead.group("var")
        ev.append(f"qualifier variable {parsed['variable']}")
    elif supp_var:                  # "SUPPDS.QVAL when QNAM = "PROTVER""
        parsed["variable"] = supp_var.group("var").upper()
        ev.append(f"qualifier variable {parsed['variable']}")
    conf = CONF_STRUCTURAL if qnam else CONF_LIKELY
    return SUPP_QUALIFIER, conf, parsed, ev


def _as_assignment(t: str, first: bool):
    """`LHS = RHS`: a domain header or a constant assignment, never both."""
    m = _ASSIGNMENT.match(t)
    if not m:
        return None
    lhs, dom, value = m.group("var"), m.group("dom"), m.group("value").strip()
    if dom:                          # "DM.BRTHDTC = ..." is unambiguously a variable
        return (CONSTANT_ASSIGNMENT, CONF_STRUCTURAL,
                {"domain": dom, "variable": lhs, "value": _unquote(value)},
                [f"qualified variable {dom}.{lhs} assigned a constant"])
    if len(lhs) == 2:
        return _two_letter_assignment(lhs, value, first)
    return (CONSTANT_ASSIGNMENT, CONF_STRUCTURAL,
            {"variable": lhs, "value": _unquote(value)},
            [f"variable {lhs} assigned constant {value!r}"])


def _two_letter_assignment(code: str, value: str, first: bool):
    """"DS=Disposition" or "DS=COMPLETED"? Resolve, and say how.

    A human-readable right side settles it outright. When both sides are code-
    shaped the tie-breakers are the CDISC domain list and page position - a
    domain header is the first markup on its page.
    """
    known = code in DOMAINS
    ev = [f"two-letter left side {code}" + (" (CDISC domain)" if known else "")]
    if re.search(r"[a-z]", value) or " " in value:
        return (DOMAIN_HEADER, CONF_STRUCTURAL if known else CONF_LIKELY,
                {"domain": code, "label": value}, ev + ["right side is a form name"])
    if known and first:
        return (DOMAIN_HEADER, CONF_AMBIGUOUS, {"domain": code, "label": value},
                ev + ["all-caps right side, but first annotation on the page"])
    return (CONSTANT_ASSIGNMENT, CONF_AMBIGUOUS if known else CONF_LIKELY,
            {"variable": code, "value": _unquote(value)},
            ev + ["code-shaped right side, not the page's first annotation"])


def _as_derivation(t: str, first: bool):
    m = _DERIVATION.search(t)
    if m:
        return (DERIVATION_RULE, CONF_LIKELY,
                {"variables": sorted(set(re.findall(rf"\b{_VAR_TOKEN}\b", t)))},
                [f"derivation wording {m.group(0)!r}"])
    return None


def _as_variable(t: str, first: bool):
    m = _PURE_VAR.match(t)
    if not m:
        return None
    dom, var = m.group("dom"), m.group("var")
    parsed = {"variable": var}
    ev = [f"bare variable token {var}"]
    if dom:
        parsed["domain"] = dom
        ev.append(f"qualified by domain {dom}")
    return VARIABLE, CONF_STRUCTURAL if dom else CONF_LIKELY, parsed, ev


def _unquote(v: str) -> str:
    return v.strip().strip('"\'').strip()


# --- reporting -------------------------------------------------------------
def summarize_annotations(annots: list[Annotation]) -> dict:
    counts: dict[str, int] = {}
    for a in annots:
        counts[a.annot_type] = counts.get(a.annot_type, 0) + 1
    return {
        "annotations": len(annots),
        "statements": sum(len(a.parts) for a in annots),
        "by_type": dict(sorted(counts.items())),
        "ambiguous": [a.text for a in annots if a.type_confidence <= CONF_AMBIGUOUS],
    }
