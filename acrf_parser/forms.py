"""Phase 2 - form detection.

Answers "which form is this page?" for every page, then folds pages into `Form`
records. Everything downstream needs this first: the primary key is
`(form_name, field_text)`, so a field extracted before its form is known cannot
be keyed at all.

Four independent signals, tried in order of how much they can be trusted:

1. a printed title line ("Form: Medical History")
2. the best-scoring heading in the top of the page (see `_title_score`)
3. the domain-header annotation ("DS=Disposition") - a page can carry no title
   at all and still be unambiguous, which is exactly the Disposition fixture page
4. a "See Page 2" cross-reference, or plain inheritance from the previous page

1-3 are page-local and run first. 4 needs its neighbours resolved, so it runs as
a second pass - which is also what lets a *forward* reference ("See Page 7" on a
page that precedes 7) resolve at all.

Why signal 2 is scored rather than "the topmost heading"
--------------------------------------------------------
It used to be the topmost bold heading, and on a real CRF that is almost never
the form. Every page of the CDISC MSG example CRF opens with a bordered
identification band - the study ("CDISC Study CDISC01"), the visit
("Screening"), the assessment date - printed bold, above and to the left of the
form's own name. Taking the topmost heading names all 22 pages after the study,
which collapses the whole CRF into three or four "forms" and destroys the
`(form_name, field_text)` key that everything downstream is built on.

What actually distinguishes "DEMOGRAPHY" from "CDISC Study CDISC01" is not
height on the page. It is that a form title is *centred*, *short*, and *not
printed on every other page*, while an identification band is flush to a margin,
spans the page, and repeats. None of those is decisive alone - page 8's title is
noticeably off-centre, and "Enrollment Form" is not all-caps - so they are
scored together and the best heading wins, with the score kept as evidence.
"""
from __future__ import annotations

import re
from statistics import median

from .layout import FOOTER_ROLE, PAGE_HEADER, SECTION_HEADER
from .models import CrossReference, Form, Page, TextGroup
from .normalize import normalize

# --- tunables --------------------------------------------------------------
TITLE_ZONE = 0.35        # fraction of page height a title may live in
CONF_TITLE_LINE = 0.95   # "Form: X" - explicit and unambiguous
CONF_SECTION_HEADER = 0.7
CONF_DOMAIN_ANNOT = 0.6
CONF_CROSS_REF = 0.5
CONF_INHERITED = 0.4

# Title scoring (see the module docstring).
CENTRE_TOL_PCT = 0.10      # |group centre - page centre| that still reads as centred
TITLE_MAX_WIDTH_PCT = 0.70  # a title is a phrase; a banner spanning the page is not
TITLE_MAX_WORDS = 8        # beyond this it is an instruction, not a name
MIN_TITLE_LETTERS = 4      # "To" and ":" are not form names, however centred
HEADER_REPEAT_PAGES = 3    # a heading printed on this many pages is page furniture
ROW_MATE_OVERLAP = 0.5     # vertical overlap at which two headings share a row
ROW_MATES_ARE_A_TABLE = 2  # this many headings on one row is a table header row
MIN_TITLE_SCORE = 0.4      # below this, no heading on the page is a form title

TITLE_LINE, DOMAIN_ANNOTATION = "TITLE_LINE", "DOMAIN_ANNOTATION"
CROSS_REFERENCE, INHERITED = "CROSS_REFERENCE", "INHERITED"

_TITLE_PREFIX = re.compile(r"^\s*(?:form|crf|module|page)\s*[:\-–]\s*(.+)$", re.I)
# A continuation marker at the end of a title. "(page 2 of 2)" is one: the CSDD
# questionnaire in the MSG CRF spells its two pages that way, and reading it as
# page furniture rather than as a continuation cost the form its name on both.
_CONTINUED = re.compile(
    r"[\s\(\[\-–]*\b(continued|cont(?:'?d)?\.?|page\s*\d+\s*of\s*\d+)\b"
    r"\s*[\)\]]?\s*$", re.I)
_SEE_PAGE = re.compile(r"\bsee\s+(?:page|pg\.?)\s*(\d+)", re.I)
# "DM=Demographics": a two-letter domain code assigned a human-readable name.
# The two-letter LHS is what separates it from "MHENRF=ONGOING".
_DOMAIN_HEADER = re.compile(r"^([A-Z]{2})\s*=\s*([A-Za-z][A-Za-z0-9 /,&'\-]*)$")

# The identification band every CRF page carries, and nothing a form is called.
# Deliberately about the *study and the visit*, not about clinical content:
# "Medical History" is a form, "Assessment Date" is not.
#
# "Study" is the trap. It heads the identification band on every page of the MSG
# CRF ("CDISC Study CDISC01") and it is also the first word of a real form
# ("STUDY MEDICATION INVENTORY"). What separates them is what follows: an
# identifier carries a *code* - a token with a digit in it - or a colon or a
# number word. A study followed by an ordinary word is naming something.
_FURNITURE = re.compile(
    r"\bstud(?:y|ies)\b\s*(?:[:#]|\b(?:no|number|id|code)\b|[A-Za-z-]*\d)"
    r"|\b(protocol|sponsor|investigator|site\s*#|centre\s*#|center\s*#|"
    r"subject\s*(?:id|no|number|initials)|patient\s*(?:id|no|number)|"
    r"visit(?:\s*(?:date|number|#))?|assessment\s*date|date\s*of\s*(?:visit|assessment)|"
    r"page\s*\d+\s*$|confidential|version)\b", re.I)
# A blank to be written in ("___/___/___"): a fill-in line, never a title.
_BLANKS = re.compile(r"_{2,}")
_TITLE_WORD = re.compile(r"\b(form|crf|questionnaire|log|checklist)\b", re.I)


def detect_forms(pages: list[Page]) -> list[Form]:
    """Assign a form to every page and return the document's forms in page order."""
    repeated = _repeated_headings(pages)
    for page in pages:
        page.cross_references = find_cross_references(page, len(pages))
        _assign_local(page, repeated)
    _resolve_inherited(pages)
    return _collect(pages, repeated)


def _repeated_headings(pages: list[Page]) -> dict[str, int]:
    """How many pages print each heading, so furniture can be told from titles.

    Counted over pages rather than occurrences: a heading printed twice on one
    page is one page's worth of evidence that it is a heading, not two pages'
    worth of evidence that it is furniture.
    """
    counts: dict[str, int] = {}
    for page in pages:
        for text in {normalize(g.text) for g in _headings(page)}:
            if text:
                counts[text] = counts.get(text, 0) + 1
    return counts


# --- 1. page-local signals -------------------------------------------------
def _assign_local(page: Page, repeated: dict[str, int] | None = None) -> None:
    """Signals 1-3: everything decidable from this page alone."""
    for name, raw, source, conf, why in _candidates(page, repeated or {}):
        page.form_name, page.form_confidence, page.form_source = name, conf, source
        page.form_domain = page.form_domain or _page_domain(page)
        page.form_evidence = [why]
        if _CONTINUED.search(raw):
            page.is_continuation = True
            page.form_evidence.append("title marked continued")
        return
    page.form_domain = _page_domain(page)


def _headings(page: Page) -> list[TextGroup]:
    """Groups in the top of the page that could name the form.

    Typography rather than role, because the layout pass's role is answering a
    different question. `SECTION_HEADER` and `PAGE_HEADER` both qualify - a CRF
    that rules a box around its identification band puts the form's own title
    inside that box, and a title excluded for being in the header is a title
    lost - but so does anything simply set bold or larger than the body text.
    The MSG Study Medication Inventory page is why: it is a two-column grid, and
    `assign_roles` will not call a heading a section header unless it spans the
    columns, so the page's one printed title was tagged `RESPONSE_OPTION` and
    never considered. Roles are features here, not filters.

    Widening the candidate list is safe because nothing downstream trusts it:
    `_title_score` is what decides, and it is stricter than any role test.
    """
    limit = page.height * TITLE_ZONE
    body = [g.size for g in page.groups
            if g.region == "BODY" and not g.from_annotation_only() and g.size]
    med = median(body) if body else 0.0
    return [g for g in page.groups
            if not g.from_annotation_only() and g.text.strip()
            and g.bbox.cy <= limit
            and (g.role in (SECTION_HEADER, PAGE_HEADER)
                 or g.bold or (med and g.size > med + 0.5))]


def _candidates(page: Page, repeated: dict[str, int]):
    """Yield (name, raw_title, source, confidence, evidence) best signal first."""
    limit = page.height * TITLE_ZONE
    top = [g for g in page.groups
           if not g.from_annotation_only() and g.bbox.cy <= limit and g.text.strip()]
    for g in top:                                   # 1. explicit "Form: X"
        m = _TITLE_PREFIX.match(g.text)
        if m:
            yield (clean_title(m.group(1)), g.text, TITLE_LINE, CONF_TITLE_LINE,
                   f"title line {g.text!r}")
            return
    best = _best_heading(page, repeated)             # 2. scored heading
    if best:
        g, score, why = best
        yield (clean_title(g.text), g.text, SECTION_HEADER,
               round(CONF_SECTION_HEADER * min(1.0, score), 2),
               f"heading {g.text!r} scored {score:.2f}: {', '.join(why)}")
        return
    dom = _domain_annotation(page)                  # 3. "DS=Disposition"
    if dom:
        code, name = dom
        yield (clean_title(name), name, DOMAIN_ANNOTATION, CONF_DOMAIN_ANNOT,
               f"domain header annotation {code}={name}")


def _best_heading(page: Page, repeated: dict[str, int]):
    """The heading on this page most likely to be the form's name."""
    heads = _headings(page)
    scored = [(g, *_title_score(g, page, repeated, heads)) for g in heads]
    scored = [s for s in scored if s[1] >= MIN_TITLE_SCORE]
    if not scored:
        return None
    # Best score wins; a tie goes to the higher heading, which is where a form
    # title sits relative to the section headings under it.
    return max(scored, key=lambda s: (round(s[1], 3), -s[0].bbox.y0))


def _title_score(g: TextGroup, page: Page, repeated: dict[str, int],
                 siblings: list[TextGroup] | None = None) -> tuple[float, list[str]]:
    """How much this heading looks like a form title, and why.

    Positive signals are about *being a title*; negative ones are about being
    the page's identification band. Both are needed: the band is bold, large and
    at the very top of the page, so every positive signal a naive rule would use
    it also satisfies.
    """
    score, why = 0.0, []
    body = [x.size for x in page.groups
            if x.region == "BODY" and not x.from_annotation_only() and x.size]
    med = median(body) if body else 0.0
    width = page.width or 1.0
    # Scored on the *cleaned* title, so a continuation marker is judged as what
    # it is rather than counted against the name it is attached to.
    text = clean_title(g.text)
    if sum(c.isalpha() for c in text) < MIN_TITLE_LETTERS:
        return 0.0, ["too short to be a form name"]

    if abs(g.bbox.cx - width / 2) <= CENTRE_TOL_PCT * width:
        score += 0.4
        why.append("centred")
    if g.bbox.width <= TITLE_MAX_WIDTH_PCT * width:
        score += 0.2
        why.append("short enough to be a phrase")
    if len(text) >= 5 and text == text.upper() and any(c.isalpha() for c in text):
        score += 0.15
        why.append("all caps")
    if _TITLE_WORD.search(text):
        score += 0.15
        why.append("names itself a form")
    if med and (g.size > med + 0.5 or (g.bold and g.size >= med)):
        score += 0.15
        why.append("larger or bolder than the body")

    if _FURNITURE.search(text):
        score -= 0.5
        why.append("reads as study/visit identification, not a form name")
    if _BLANKS.search(text):
        score -= 0.4
        why.append("contains a fill-in blank")
    if len(text.split()) > TITLE_MAX_WORDS:
        # An instruction line is centred, bold and near the top exactly as a
        # title is; its length is the one thing that is never true of a name.
        score -= 0.5
        why.append(f"{len(text.split())} words - reads as an instruction, not a name")
    row = _row_mates(g, siblings or [])
    if row >= ROW_MATES_ARE_A_TABLE:
        # A form title stands alone on its line. Several bold headings side by
        # side on one row is the header row of a table - "Number of Tablets
        # Dispensed | Date Tablets Returned | Number of Tablets Returned" - and
        # any one cell of it can look like a centred, bold, short title.
        score -= 0.3
        why.append(f"{row} other headings on the same row - a table header row")
    seen = repeated.get(normalize(text), 0)
    if seen >= HEADER_REPEAT_PAGES:
        score -= 0.35
        why.append(f"printed as a heading on {seen} pages - page furniture")
    return max(0.0, score), why


def _row_mates(g: TextGroup, headings: list[TextGroup]) -> int:
    """How many other heading candidates share this one's row."""
    return sum(1 for o in headings
               if o is not g and o.bbox.v_overlap(g.bbox) >= ROW_MATE_OVERLAP)


def _domain_annotation(page: Page) -> tuple[str, str] | None:
    """The `XX=Name` markup a study puts at the top of each form's first page."""
    for a in sorted(page.annotations, key=lambda a: a.bbox.y0):
        m = _DOMAIN_HEADER.match(a.text.strip())
        if m:
            return m.group(1), m.group(2).strip()
    return None


def _page_domain(page: Page) -> str:
    dom = _domain_annotation(page)
    return dom[0] if dom else ""


def clean_title(text: str) -> str:
    """Strip the "Form:" prefix and any continuation marker off a printed title.

    Continuation pages must land on the *same* name as page 1 of the form, so
    "Medical History (continued)" has to normalize to "Medical History".
    """
    text = _TITLE_PREFIX.sub(r"\1", text.strip())
    text = _CONTINUED.sub("", text).strip(" -–:()[]")
    return re.sub(r"\s+", " ", text).strip()


# --- 2. cross references ---------------------------------------------------
def find_cross_references(page: Page, page_count: int) -> list[CrossReference]:
    """Every "See Page N" on the page, from markup or from the page text itself.

    A reference off the end of the document stays `resolved=False` rather than
    being dropped: a dangling pointer is a finding about the PDF, not noise.
    """
    refs: list[CrossReference] = []
    seen: set[tuple[int, float]] = set()
    sources = [(a.text, a.bbox) for a in page.annotations]
    sources += [(g.text, g.bbox) for g in page.groups if not g.from_annotation_only()]
    for text, bbox in sources:
        for m in _SEE_PAGE.finditer(text or ""):
            target = int(m.group(1))
            key = (target, round(bbox.cy, 1))
            if key in seen:
                continue
            seen.add(key)
            refs.append(CrossReference(
                page=page.number, target_page=target, text=m.group(0), bbox=bbox,
                resolved=1 <= target <= page_count and target != page.number))
    return refs


# --- 3. inheritance --------------------------------------------------------
def _resolve_inherited(pages: list[Page]) -> None:
    """Signal 4, for pages that carry no title of their own.

    Runs after every page has had its local shot, so a "See Page 7" pointing
    forward resolves against a page that is already named.
    """
    by_number = {p.number: p for p in pages}
    for i, page in enumerate(pages):
        for ref in page.cross_references:           # annotate refs either way
            target = by_number.get(ref.target_page)
            if target is not None:
                ref.target_form = target.form_name
        if page.form_name:
            continue
        ref = next((r for r in page.cross_references
                    if r.resolved and by_number[r.target_page].form_name), None)
        if ref:
            src = by_number[ref.target_page]
            page.form_name, page.form_confidence = src.form_name, CONF_CROSS_REF
            page.form_source = CROSS_REFERENCE
            page.form_domain = page.form_domain or src.form_domain
            page.is_continuation = True
            page.form_evidence = [f"cross reference {ref.text!r} -> page {ref.target_page}"]
        elif i and pages[i - 1].form_name:
            prev = pages[i - 1]
            page.form_name, page.form_confidence = prev.form_name, CONF_INHERITED
            page.form_source = INHERITED
            page.form_domain = page.form_domain or prev.form_domain
            page.is_continuation = True
            page.form_evidence = [f"no title; inherited from page {prev.number}"]


# --- 4. pages -> forms -----------------------------------------------------
def _collect(pages: list[Page], repeated: dict[str, int] | None = None) -> list[Form]:
    """Fold pages into Form records, keyed on the normalized name.

    Keyed on the name and not on page adjacency: a study that interleaves visit
    modules can return to the same form later, and both runs are one form.
    """
    forms: dict[str, Form] = {}
    for page in pages:
        if not page.form_name:
            continue
        key = normalize(page.form_name)
        form = forms.get(key)
        if form is None:
            # Pages come in order, so the first one seen is the form's first page
            # and its signal is the one that named the form.
            form = forms[key] = Form(name=page.form_name, source=page.form_source)
            form.confidence = page.form_confidence
        form.pages.append(page.number)
        form.domain = form.domain or page.form_domain
        form.confidence = max(form.confidence, page.form_confidence)
        for e in page.form_evidence:
            if e not in form.evidence:
                form.evidence.append(e)
        form.cross_references.extend(page.cross_references)
        raw = _raw_title(page, repeated or {})
        if raw and raw not in form.raw_titles:
            form.raw_titles.append(raw)
    for form in forms.values():
        # First page of a form is never a continuation, whatever its own title said.
        first = form.first_page
        form.continuation_pages = [n for n in form.pages if n != first]
        for n in form.pages:
            pg = next(p for p in pages if p.number == n)
            pg.is_continuation = n != first
            pg.form_domain = pg.form_domain or form.domain
    return sorted(forms.values(), key=lambda f: f.first_page)


def _raw_title(page: Page, repeated: dict[str, int] | None = None) -> str:
    for _, raw, _, _, _ in _candidates(page, repeated or {}):
        return raw
    return ""


# --- reporting -------------------------------------------------------------
def summarize_forms(forms: list[Form]) -> list[dict]:
    """Compact per-form view for the CLI summary."""
    return [{"name": f.name, "domain": f.domain, "pages": f.pages,
             "continuation_pages": f.continuation_pages,
             "confidence": round(f.confidence, 2), "source": f.source}
            for f in forms]
