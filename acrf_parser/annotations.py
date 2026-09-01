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
    CONDITIONAL_VARIABLE IEORRES when IETESTCD = INCL01
    SUPP_QUALIFIER       RACEOTH when SUPPDM.QNAM=RACEOTH
    NOT_SUBMITTED        [NOT SUBMITTED]
    CROSS_REFERENCE      See Page 7
    DERIVATION_RULE      AGE derived from BRTHDTC and RFSTDTC
    NOTE                 anything else - free prose, kept, never guessed at

`CONDITIONAL_VARIABLE` is how every SDTM findings domain is annotated, and it
was the largest gap here. A findings observation is not identified by its
variable - `QSORRES` is *every* answer on a questionnaire - but by the variable
plus the test code that says which question it is, so an annotator writes
`QSORRES when QSTESTCD = MMSEA1`. On the CDISC MSG example CRF that shape
accounts for seventy of two hundred and nine annotations, a third of the file,
and all seventy read as unclassified prose: no variable parsed, no domain
resolved, so no fill colour, no domain check, and `NOTE` shown to the reviewer
for the one thing on the page that is completely structured.

It is checked after SUPP and before assignment. `RACEOTH when SUPPDM.QNAM =
RACEOTH` has the same shape and is the more specific case, and an assignment
pattern anchored at the start of the string cannot match a conditional anyway.

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
CONDITIONAL_VARIABLE = "CONDITIONAL_VARIABLE"
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
# "RACEOTH in SUPPDM": the qualifier variable, then the dataset it lands in.
_SUPP_IN = re.compile(rf"^\s*(?P<var>{_VAR_TOKEN})\s+in\s+SUPP(?P<dom>[A-Z]{{2}})\s*$", re.I)
_ASSIGNMENT = re.compile(rf"^\s*{_QUALIFIED}\s*=\s*(?P<value>.+?)\s*$")
_PURE_VAR = re.compile(rf"^\s*{_QUALIFIED}\s*$")
_VAR_LIST = re.compile(rf"^\s*{_VAR_TOKEN}(?:\.{_VAR_TOKEN})?"
                       rf"(?:\s*[,/|]\s*|\s+){_VAR_TOKEN}(?:\.{_VAR_TOKEN})?.*$")
_DERIVATION = re.compile(
    r"\b(deriv\w*|calculat\w*|comput\w*|concatenat\w*|imput\w*|assigned\s+by|"
    r"populated\s+(?:from|by)|mapped\s+(?:to|from)|if\b.*\bthen\b|not\s+collected)\b", re.I)
_CODED_VALUE = re.compile(r"^[\"']?[A-Z0-9][A-Z0-9 _\-/]*[\"']?$")
_SPLIT = re.compile(r"\s*[,/|]\s*|\s+")

# "IEORRES when IETESTCD = INCL01", "VSORRES / VSORRESU when VSTESTCD = SYSBP,
# DIABP". One or more variables, a condition variable, and the value(s) that
# pick out which observation this is.
_VAR_SEQ = rf"(?:[A-Z]{{2}}\.)?{_VAR_TOKEN}"
_CONDITIONAL = re.compile(
    rf"^\s*(?P<vars>{_VAR_SEQ}(?:\s*[/,]\s*{_VAR_SEQ})*)"
    rf"\s+[Ww][Hh][Ee][Nn]\s+(?P<cond>{_VAR_SEQ})\s*=\s*(?P<vals>\S.*?)\s*$")
# `when` written hard against the variable before it - "QSORRESwhen QSTESTCD =
# CSDD19". Two of the MSG CRF's annotations are typed that way and they are as
# structured as the rest; a parser that only reads tidy input reports the
# annotator's typo as prose.
_TIGHT_WHEN = re.compile(r"(?<=[A-Z])(?=[Ww][Hh][Ee][Nn]\s)")
_VALUE_SPLIT = re.compile(r"\s*[,/]\s*")

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
                 _as_conditional, _as_assignment, _as_derivation, _as_variable):
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
    # "RACEOTH in SUPPDM" - the shorthand the MSG CRF uses throughout, and the
    # only one of the three shapes where the variable is not otherwise readable.
    in_supp = _SUPP_IN.match(t)
    if lead:                        # "RACEOTH when SUPPDM.QNAM=RACEOTH"
        parsed["variable"] = lead.group("var")
        ev.append(f"qualifier variable {parsed['variable']}")
    elif supp_var:                  # "SUPPDS.QVAL when QNAM = "PROTVER""
        parsed["variable"] = supp_var.group("var").upper()
        ev.append(f"qualifier variable {parsed['variable']}")
    elif in_supp:                   # "RACEOTH in SUPPDM"
        parsed["variable"] = in_supp.group("var")
        # The variable *is* the QNAM in this shorthand: "RACEOTH in SUPPDM" says
        # a SUPPDM record whose QNAM is RACEOTH. Saying so is what lets the
        # importer and the corpus treat it like the longhand spelling.
        parsed.setdefault("qnam", in_supp.group("var"))
        ev.append(f"qualifier variable {parsed['variable']} in SUPP{parsed.get('domain', '')}")
    conf = CONF_STRUCTURAL if qnam else CONF_LIKELY
    return SUPP_QUALIFIER, conf, parsed, ev


def _as_conditional(t: str, first: bool):
    """`XXORRES when XXTESTCD = CODE`: a findings observation, named by its test.

    The variable alone does not identify the observation - every answer on a
    questionnaire is `QSORRES` - so the test code is part of the mapping and has
    to be parsed as such rather than left in prose. `variable` is the first one,
    which is what the workbook's variable column wants; `variables` is all of
    them, because `VSORRES / VSORRESU when VSTESTCD = HEIGHT` maps two.
    """
    m = _CONDITIONAL.match(_TIGHT_WHEN.sub(" ", t))
    if not m:
        return None
    variables = [v.strip() for v in re.split(r"\s*[/,]\s*", m.group("vars")) if v.strip()]
    values = [v.strip().strip("\"'") for v in _VALUE_SPLIT.split(m.group("vals")) if v.strip()]
    cond = m.group("cond")
    parsed = {"variable": variables[0], "variables": variables,
              "condition": {"variable": cond, "values": values}}
    domain, _how = _prefix_domain(variables[0])
    if domain:
        parsed["domain"] = domain
    ev = [f"{', '.join(variables)} conditioned on {cond} = {', '.join(values)}"]
    # Both sides naming the same domain is the SDTM findings convention itself -
    # QSORRES with QSTESTCD - and it is what separates this from a stray "when".
    same = _prefix_domain(cond)[0] == domain and domain
    if same:
        ev.append(f"variable and test code both in domain {domain}")
    return (CONDITIONAL_VARIABLE, CONF_STRUCTURAL if same else CONF_LIKELY, parsed, ev)


def _prefix_domain(var: str) -> tuple[str, str]:
    """The domain a variable's prefix names, if it names one. ("", "") if not."""
    var = (var or "").upper().split(".")[-1]
    prefix, rest = var[:2], var[2:]
    if prefix in DOMAINS and _DOMAIN_SUFFIX.match(rest):
        return prefix, PREFIX
    return "", UNRESOLVED


def _as_assignment(t: str, first: bool):
    """`LHS = RHS`: a domain header or a constant assignment, never both.

    The left side may be a list. `DSTERM / DSDECOD = RANDOMIZED` assigns one
    constant to two variables, which is ordinary annotator shorthand and read as
    prose while this only accepted a single token.
    """
    m = _ASSIGNMENT.match(t) or _list_assignment(t)
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


_LIST_ASSIGNMENT = re.compile(
    rf"^\s*(?P<vars>{_VAR_SEQ}(?:\s*[/,]\s*{_VAR_SEQ})+)\s*=\s*(?P<value>.+?)\s*$")


def _list_assignment(t: str):
    """`DSTERM / DSDECOD = RANDOMIZED` as if it were `DSTERM = RANDOMIZED`.

    Returned in the shape `_ASSIGNMENT` produces so the caller does not have to
    care which matched; the first variable is the one it reports, which is the
    same choice `_as_conditional` makes for the same reason.
    """
    m = _LIST_ASSIGNMENT.match(t)
    if not m:
        return None
    first_var = re.split(r"\s*[/,]\s*", m.group("vars"))[0]
    dom, _, var = first_var.rpartition(".")
    return _FakeMatch({"var": var, "dom": dom or None, "value": m.group("value")})


class _FakeMatch:
    """Just enough of a match object for `_as_assignment` to read groups off."""

    def __init__(self, groups: dict):
        self._groups = groups

    def group(self, name: str):
        return self._groups.get(name)


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


# --- which domain does a statement belong to? ------------------------------
QUALIFIED, PREFIX, UNRESOLVED = "qualified", "prefix", "unresolved"

# A real suffix behind a domain code. Length is what keeps AGE ("AG" + "E") and
# SEX ("SE" + "X") from reading as AG- and SE-domain variables.
_DOMAIN_SUFFIX = re.compile(r"^[A-Z][A-Z0-9]{2,}$")


def statement_domain(text: str, parsed: dict | None = None) -> tuple[str, str]:
    """The SDTM domain a single statement belongs to, and how that was decided.

    Needed because a sponsor's house style can key on it. On a real Disposition
    page `DSTERM` and `RFICDTC` are both plain VARIABLE markup, on the same
    field, on the same row - and they are drawn in different colours, because
    one is DS and the other is DM. Type alone cannot tell them apart.

    Two deterministic answers and one honest refusal:

        DM.BRTHDTC / SUPPDS.QVAL / DS=Disposition   -> DM, DS, DS   (qualified)
        DSSTDTC                                     -> DS           (prefix)
        RFICDTC, AGE, SEX, BRTHDTC                  -> ""           (unresolved)

    The refusal is the important case and it must stay a refusal. DM's own
    variables carry no prefix, so nothing in `RFICDTC` says DM; falling back to
    the *form's* domain would answer "DS" on the very page where the distinction
    matters. An unresolved domain is a question for history or for a human, not
    something to guess at here.

    `parsed` is optional: given bare text - which is all a staging row has - the
    text is classified with the same rules the parser uses, so the workbook and
    the corpus cannot disagree about which domain a statement belongs to.
    """
    parsed = parsed or classify(text).parsed or {}
    dom = (parsed.get("domain") or "").upper()
    if dom:
        return dom, QUALIFIED
    m = re.match(rf"^\s*(?P<dom>[A-Z]{{2}})\.(?:{_VAR_TOKEN})\s*$", (text or "").strip())
    if m:
        return m.group("dom"), QUALIFIED
    var = (parsed.get("variable") or "").upper()
    if not var:
        bare = _PURE_VAR.match((text or "").strip())
        var = bare.group("var").upper() if bare else ""
    prefix, rest = var[:2], var[2:]
    if prefix in DOMAINS and _DOMAIN_SUFFIX.match(rest):
        return prefix, PREFIX
    return "", UNRESOLVED


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
