"""Layout analysis: regions, columns, wrapped-line grouping, question/response roles.

Runs after Phase 1 extraction and before field extraction. Phase 1 stays raw -
this pass only adds derived objects and tags, so every decision stays auditable
against the original lines.

Why it exists: PyMuPDF yields one TextLine per *rendered* line, so a single CRF
question ("Please record / protocol / version on / which subject / is currently /
enrolled:") arrives as six fields. Grouping must happen before field extraction or
the (form_name, field_text) key gets built from fragments.
"""
from __future__ import annotations

import re
from statistics import median

from .models import BBox, ColumnBand, Page, TextGroup, TextLine

# --- tunables (calibrate against a real aCRF before trusting on new studies) ---
GAP_RATIO = 0.5          # merge when line gap <= this * font size
GUTTER_MIN_PT = 12.0     # narrowest whitespace that can count as a column gutter
GUTTER_ROW_COVERAGE = 0.6  # fraction of body lines that must be paired across it
ALIGN_TOL = 2.0          # left/right/centre alignment tolerance in points
SIZE_TOL = 0.5           # font size difference that still counts as "same style"
ROW_OVERLAP = 0.5        # vertical overlap that counts as "same row"
FULL_WIDTH = 0.7         # span_pct at which a rule delimits the page body
CONTROL_NEAR_PT = 72.0   # a control further than an inch away belongs to another column
MARGIN = 0.12            # page-margin band where running headers/footers may live
MIN_REPEATS = 3          # pages a line must repeat on to count as a running header
MAX_BANDS = 6

HEADER, BODY, FOOTER = "HEADER", "BODY", "FOOTER"
QUESTION_ZONE, RESPONSE_ZONE, UNKNOWN_ZONE = "QUESTION_ZONE", "RESPONSE_ZONE", "UNKNOWN"
QUESTION, RESPONSE_OPTION, SECTION_HEADER = "QUESTION", "RESPONSE_OPTION", "SECTION_HEADER"
PAGE_HEADER, FOOTER_ROLE, UNKNOWN = "PAGE_HEADER", "FOOTER", "UNKNOWN"

_NEW_ITEM = re.compile(r"^\s*(\d+[.)]|[a-z][.)]|[-•*])\s+", re.I)
_TERMINATED = re.compile(r"[:?]\s*$")


def analyze_document(pages: list[Page]) -> list[Page]:
    """Layout pass over a whole document.

    Running headers can only be found by comparing pages, and region tags decide
    grouping, so the repeated-text scan happens before any page is analyzed.
    """
    repeated = find_repeated_lines(pages)
    for page in pages:
        analyze(page, repeated)
    return pages


def analyze(page: Page, repeated: set[tuple[str, int]] | None = None) -> Page:
    """Run the full layout pass over one extracted page."""
    detect_regions(page, repeated)
    detect_columns(page)
    page.groups = group_lines(page)
    assign_roles(page)
    return page


# --- 1. regions ------------------------------------------------------------
def detect_regions(page: Page, repeated: set[tuple[str, int]] | None = None) -> Page:
    """Split the page into HEADER / BODY / FOOTER using full-width rules."""
    h = page.height or 1.0
    top_rules = [r for r in page.rules
                 if r.orientation == "H" and r.span_pct >= FULL_WIDTH and r.bbox.y1 < h * 0.25]
    bot_rules = [r for r in page.rules
                 if r.orientation == "H" and r.span_pct >= FULL_WIDTH and r.bbox.y0 > h * 0.85]
    page.body_top = min((r.bbox.y1 for r in top_rules), default=h * 0.08)
    page.body_bottom = max((r.bbox.y0 for r in bot_rules), default=h * 0.92)

    for line in page.lines:
        cy = line.bbox.cy
        line.region = HEADER if cy < page.body_top else FOOTER if cy > page.body_bottom else BODY
        # A repeated line only counts as a running header/footer if it also sits in
        # the page margin: a field label like "Condition" repeats across the pages
        # of one form without being a header.
        if repeated and _line_key(line) in repeated:
            if cy < h * MARGIN:
                line.region = HEADER
            elif cy > h * (1 - MARGIN):
                line.region = FOOTER
    return page


def _line_key(line: TextLine) -> tuple[str, int]:
    """Identity of a line across pages: same words at the same height (±3pt)."""
    return (line.normalized_text, round(line.bbox.cy / 3))


def find_repeated_lines(pages: list[Page], min_share: float = 0.5) -> set[tuple[str, int]]:
    """Text repeating at the same y on most pages is a running header/footer.

    Also feeds Phase 2: a repeated title line is a form-continuation signal.
    """
    if len(pages) < MIN_REPEATS:
        return set()
    seen: dict[tuple[str, int], int] = {}
    for p in pages:
        for key in {_line_key(l) for l in p.lines if not l.from_annotation and l.text.strip()}:
            seen[key] = seen.get(key, 0) + 1
    threshold = max(MIN_REPEATS, int(len(pages) * min_share))
    return {k for k, n in seen.items() if n >= threshold}


# --- 2. columns ------------------------------------------------------------
def _body_lines(page: Page) -> list[TextLine]:
    """Body text only. Annotation markup is excluded: an annotation parked in the
    gutter (SUPPDS.QVAL...) would otherwise hide the column split entirely."""
    return [l for l in page.lines if l.region == BODY and not l.from_annotation and l.text.strip()]


def detect_columns(page: Page) -> list[ColumnBand]:
    """Find column bands by vertical-whitespace (gutter) detection, XY-cut style."""
    lines = _body_lines(page)
    bands = _split_band(lines, 0.0, page.width) if lines else []
    if not bands:
        bands = [(0.0, page.width)]
    page.column_bands = [ColumnBand(i, round(x0, 2), round(x1, 2)) for i, (x0, x1) in enumerate(bands)]
    _hint_zones(page)
    for line in lines:                       # tag each body line with its band
        line.column = next((b.index for b in page.column_bands if b.contains(line.bbox)), None)
    return page.column_bands


def _split_band(lines: list[TextLine], x0: float, x1: float, depth: int = 0) -> list[tuple[float, float]]:
    """Recursively cut a band at its widest valid gutter."""
    if depth >= MAX_BANDS or len(lines) < 2:
        return [(x0, x1)]
    gutter = _find_gutter(lines)
    if gutter is None:
        return [(x0, x1)]
    gx0, gx1 = gutter
    left = [l for l in lines if l.bbox.cx <= gx0]
    right = [l for l in lines if l.bbox.cx >= gx1]
    if len(left) < 2 or len(right) < 2:
        return [(x0, x1)]
    return (_split_band(left, x0, gx0, depth + 1) + _split_band(right, gx1, x1, depth + 1))


def _find_gutter(lines: list[TextLine]) -> tuple[float, float] | None:
    """Widest whitespace run that most body lines are actually paired across.

    The pairing test is what stops a local gap between a few short labels from
    being read as a column boundary.
    """
    spans = sorted((l.bbox.x0, l.bbox.x1) for l in lines)
    best: tuple[float, float] | None = None
    reach = spans[0][1]
    for a, b in spans[1:]:
        if a - reach >= GUTTER_MIN_PT and (best is None or a - reach > best[1] - best[0]):
            best = (reach, a)
        reach = max(reach, b)
    if best is None:
        return None
    paired = sum(1 for l in lines if _has_partner_across(l, lines, best))
    return best if paired / len(lines) >= GUTTER_ROW_COVERAGE else None


def _has_partner_across(line: TextLine, lines: list[TextLine], gutter: tuple[float, float]) -> bool:
    """True when some row-aligned line sits on the opposite side of the gutter."""
    left = line.bbox.cx <= gutter[0]
    return any(
        (o.bbox.cx >= gutter[1] if left else o.bbox.cx <= gutter[0])
        and o.bbox.v_overlap(line.bbox) >= 0.3
        for o in lines
    )


def _hint_zones(page: Page) -> None:
    """Two bands is the common CRF case: questions left, responses right.

    Wider grids (log forms) get per-band evidence instead of the 2-column prior.
    """
    bands = page.column_bands
    if len(bands) < 2:
        return                       # no split found: no zone prior to apply
    if len(bands) == 2:
        bands[0].role_hint, bands[1].role_hint = QUESTION_ZONE, RESPONSE_ZONE
        return
    for b in bands:
        ctrl = sum(1 for c in page.controls if b.contains(c.bbox))
        b.role_hint = RESPONSE_ZONE if ctrl >= 2 else UNKNOWN_ZONE


# --- 3. line grouping (the wrapped-text fix) -------------------------------
def group_lines(page: Page) -> list[TextGroup]:
    """Merge rendered lines back into logical labels, one group per label."""
    groups: list[TextGroup] = []
    lines = sorted((l for l in page.lines if l.text.strip()),
                   key=lambda l: (l.column if l.column is not None else -1, l.bbox.y0, l.bbox.x0))
    current: list[TextLine] = []
    for line in lines:
        if current and _mergeable(current[-1], line, page):
            current.append(line)
        else:
            if current:
                groups.append(_make_group(current, page, len(groups)))
            current = [line]
    if current:
        groups.append(_make_group(current, page, len(groups)))
    return groups


def _mergeable(a: TextLine, b: TextLine, page: Page) -> bool:
    """Is `b` a wrapped continuation of `a`? All conditions must hold."""
    if a.page != b.page or a.region != b.region or a.column != b.column:
        return False
    if a.from_annotation != b.from_annotation:
        return False
    if a.region != BODY and not a.from_annotation:
        return False    # header/footer metadata is parsed by regex, not read as prose
    if _TERMINATED.search(a.text) or _NEW_ITEM.match(b.text):
        return False
    if _rule_between(a, b, page):
        return False
    if _own_control(b, page):        # b has its own answer box -> b is a field, not a wrap
        return False
    same_block = a.block_no == b.block_no and not a.from_annotation
    if not (abs(a.size - b.size) <= SIZE_TOL and a.bold == b.bold):
        return False
    gap = b.bbox.y0 - a.bbox.y1
    if gap > GAP_RATIO * max(a.size, b.size, 1.0):
        return False
    if same_block:                   # MuPDF already grouped these; trust it
        return True
    aligned = (abs(a.bbox.x0 - b.bbox.x0) <= ALIGN_TOL
               or abs(a.bbox.x1 - b.bbox.x1) <= ALIGN_TOL
               or abs(a.bbox.cx - b.bbox.cx) <= ALIGN_TOL)
    return aligned and a.bbox.h_overlap(b.bbox) >= 0.3


def _rule_between(a: TextLine, b: TextLine, page: Page) -> bool:
    """A ruled separator crossing the space between two lines breaks the group."""
    lo, hi = a.bbox.y1, b.bbox.y0
    return any(r.orientation == "H" and lo <= r.bbox.cy <= hi
               and r.bbox.h_overlap(a.bbox) > 0 for r in page.rules)


def _own_control(line: TextLine, page: Page) -> bool:
    """A control row-aligned with the line and belonging to it.

    Scoping matters: the wrapped question's rows do line up with the radio
    circles, but those circles sit in the response column 390pt away, so they
    must not terminate the question. A control counts as the line's own only if
    it is in the same band and horizontally adjacent.
    """
    if len(page.column_bands) < 2:
        return False    # no known column structure: fall back to pitch and alignment
    band = next((b for b in page.column_bands if b.index == line.column), None)
    return any(c.bbox.v_overlap(line.bbox) >= ROW_OVERLAP
               and (band is None or band.contains(c.bbox, 0.3))
               and _near(line.bbox, c.bbox)
               for c in page.controls)


def _near(a: BBox, b: BBox) -> bool:
    """Boxes horizontally adjacent: overlapping, or a short gap on either side."""
    return max(b.x0 - a.x1, a.x0 - b.x1) <= CONTROL_NEAR_PT


def _make_group(lines: list[TextLine], page: Page, gid: int) -> TextGroup:
    box = lines[0].bbox
    for l in lines[1:]:
        box = box.merge(l.bbox)
    for l in lines:
        l.group_id = gid
    text = _join(l.text for l in lines)
    return TextGroup(
        page=page.number, text=text, bbox=box, lines=list(lines),
        region=lines[0].region, column=lines[0].column,
        size=lines[0].size, bold=lines[0].bold,
        block_ids=sorted({l.block_no for l in lines}),
    )


def _join(parts) -> str:
    """Join wrapped lines; a trailing hyphen means the word was split."""
    out = ""
    for p in parts:
        p = p.strip()
        if not out:
            out = p
        elif out.endswith("-"):
            out = out[:-1] + p
        else:
            out += " " + p
    return out


# --- 4. roles --------------------------------------------------------------
def assign_roles(page: Page) -> None:
    """Tag each group QUESTION / RESPONSE_OPTION / SECTION_HEADER / header / footer.

    Roles carry confidence and evidence and never filter anything out - Phase 6
    consumes them as linking features, and every call stays explainable.
    """
    body = [g for g in page.groups if g.region == BODY and not g.from_annotation_only()]
    sizes = [g.size for g in body] or [0.0]
    med_size = median(sizes)
    for g in page.groups:
        if g.region in (HEADER, FOOTER):
            g.role = PAGE_HEADER if g.region == HEADER else FOOTER_ROLE
            g.role_confidence, g.role_evidence = 0.9, [f"region={g.region.lower()}"]
            continue
        if g.from_annotation_only():
            g.role, g.role_confidence, g.role_evidence = UNKNOWN, 0.0, ["annotation markup"]
            continue
        band = next((b for b in page.column_bands if b.index == g.column), None)
        hint = band.role_hint if band else UNKNOWN_ZONE
        ev: list[str] = []
        score_q = score_r = 0.0

        if hint == QUESTION_ZONE:
            score_q += 0.5; ev.append("in question zone")
        elif hint == RESPONSE_ZONE:
            score_r += 0.5; ev.append("in response zone")
        if _TERMINATED.search(g.text):
            score_q += 0.3; ev.append("ends with colon/question mark")
        if _control_right_of(g, page):
            score_q += 0.3; ev.append("control to the right")
        if _own_control_group(g, page):
            score_r += 0.4; ev.append("own row-aligned control")
        if len(g.text.split()) <= 4 and not _TERMINATED.search(g.text):
            score_r += 0.1; ev.append("short label")
        if ((g.bold or g.size > med_size + SIZE_TOL)
                and (len(page.column_bands) < 2 or _spans_bands(g, page))):
            g.role, g.role_confidence = SECTION_HEADER, 0.7
            g.role_evidence = ev + ["bold/large, spans columns"]
            continue

        if max(score_q, score_r) == 0:
            g.role, g.role_confidence = UNKNOWN, 0.0
        elif score_q >= score_r:
            g.role, g.role_confidence = QUESTION, round(min(score_q, 1.0), 2)
        else:
            g.role, g.role_confidence = RESPONSE_OPTION, round(min(score_r, 1.0), 2)
        g.role_evidence = ev


def _control_right_of(g: TextGroup, page: Page) -> bool:
    return any(c.bbox.x0 >= g.bbox.x1 and c.bbox.v_overlap(g.bbox) >= ROW_OVERLAP
               for c in page.controls)


def _own_control_group(g: TextGroup, page: Page) -> bool:
    band = next((b for b in page.column_bands if b.index == g.column), None)
    if band is None or len(page.column_bands) < 2:
        return False
    return any(c.bbox.v_overlap(g.bbox) >= ROW_OVERLAP and band.contains(c.bbox, 0.3)
               and _near(g.bbox, c.bbox) for c in page.controls)


def _spans_bands(g: TextGroup, page: Page) -> bool:
    return len(page.column_bands) > 1 and sum(
        1 for b in page.column_bands if b.contains(g.bbox, 0.2)) > 1
