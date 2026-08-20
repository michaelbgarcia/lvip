"""Phase 2 - form detection.

Answers "which form is this page?" for every page, then folds pages into `Form`
records. Everything downstream needs this first: the primary key is
`(form_name, field_text)`, so a field extracted before its form is known cannot
be keyed at all.

Four independent signals, tried in order of how much they can be trusted:

1. a printed title line ("Form: Medical History")
2. a section-header group at the top of the page (bold and larger than the body)
3. the domain-header annotation ("DS=Disposition") - a page can carry no title
   at all and still be unambiguous, which is exactly the Disposition fixture page
4. a "See Page 2" cross-reference, or plain inheritance from the previous page

1-3 are page-local and run first. 4 needs its neighbours resolved, so it runs as
a second pass - which is also what lets a *forward* reference ("See Page 7" on a
page that precedes 7) resolve at all.
"""
from __future__ import annotations

import re

from .layout import SECTION_HEADER
from .models import CrossReference, Form, Page
from .normalize import normalize

# --- tunables --------------------------------------------------------------
TITLE_ZONE = 0.35        # fraction of page height a title may live in
CONF_TITLE_LINE = 0.95   # "Form: X" - explicit and unambiguous
CONF_SECTION_HEADER = 0.7
CONF_DOMAIN_ANNOT = 0.6
CONF_CROSS_REF = 0.5
CONF_INHERITED = 0.4

TITLE_LINE, DOMAIN_ANNOTATION = "TITLE_LINE", "DOMAIN_ANNOTATION"
CROSS_REFERENCE, INHERITED = "CROSS_REFERENCE", "INHERITED"

_TITLE_PREFIX = re.compile(r"^\s*(?:form|crf|module|page)\s*[:\-–]\s*(.+)$", re.I)
_CONTINUED = re.compile(
    r"[\s\(\[\-–]*\b(continued|cont(?:'?d)?\.?)\b\s*[\)\]]?\s*$", re.I)
_SEE_PAGE = re.compile(r"\bsee\s+(?:page|pg\.?)\s*(\d+)", re.I)
# "DM=Demographics": a two-letter domain code assigned a human-readable name.
# The two-letter LHS is what separates it from "MHENRF=ONGOING".
_DOMAIN_HEADER = re.compile(r"^([A-Z]{2})\s*=\s*([A-Za-z][A-Za-z0-9 /,&'\-]*)$")


def detect_forms(pages: list[Page]) -> list[Form]:
    """Assign a form to every page and return the document's forms in page order."""
    for page in pages:
        page.cross_references = find_cross_references(page, len(pages))
        _assign_local(page)
    _resolve_inherited(pages)
    return _collect(pages)


# --- 1. page-local signals -------------------------------------------------
def _assign_local(page: Page) -> None:
    """Signals 1-3: everything decidable from this page alone."""
    for name, raw, source, conf, why in _candidates(page):
        page.form_name, page.form_confidence, page.form_source = name, conf, source
        page.form_domain = page.form_domain or _page_domain(page)
        page.form_evidence = [why]
        if _CONTINUED.search(raw):
            page.is_continuation = True
            page.form_evidence.append("title marked continued")
        return
    page.form_domain = _page_domain(page)


def _candidates(page: Page):
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
    heads = [g for g in top if g.role == SECTION_HEADER]
    if heads:                                       # 2. bold/large heading
        g = min(heads, key=lambda g: (g.bbox.y0, -g.size))
        yield (clean_title(g.text), g.text, SECTION_HEADER, CONF_SECTION_HEADER,
               f"section header {g.text!r}")
        return
    dom = _domain_annotation(page)                  # 3. "DS=Disposition"
    if dom:
        code, name = dom
        yield (clean_title(name), name, DOMAIN_ANNOTATION, CONF_DOMAIN_ANNOT,
               f"domain header annotation {code}={name}")


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
def _collect(pages: list[Page]) -> list[Form]:
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
        raw = _raw_title(page)
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


def _raw_title(page: Page) -> str:
    for _, raw, _, _, _ in _candidates(page):
        return raw
    return ""


# --- reporting -------------------------------------------------------------
def summarize_forms(forms: list[Form]) -> list[dict]:
    """Compact per-form view for the CLI summary."""
    return [{"name": f.name, "domain": f.domain, "pages": f.pages,
             "continuation_pages": f.continuation_pages,
             "confidence": round(f.confidence, 2), "source": f.source}
            for f in forms]
