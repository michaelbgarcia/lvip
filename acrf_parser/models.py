"""Core data structures. Every extracted object carries bbox + text + page."""
from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Iterator

from .normalize import clean, normalize


@dataclass(frozen=True)
class BBox:
    """Axis-aligned box in PDF points, origin top-left (PyMuPDF convention)."""
    x0: float
    y0: float
    x1: float
    y1: float

    @classmethod
    def of(cls, r) -> "BBox":
        """Build from a fitz.Rect, 4-tuple or list."""
        x0, y0, x1, y1 = (r.x0, r.y0, r.x1, r.y1) if hasattr(r, "x0") else tuple(r)[:4]
        return cls(round(min(x0, x1), 2), round(min(y0, y1), 2),
                   round(max(x0, x1), 2), round(max(y0, y1), 2))

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    def merge(self, other: "BBox") -> "BBox":
        return BBox(min(self.x0, other.x0), min(self.y0, other.y0),
                    max(self.x1, other.x1), max(self.y1, other.y1))

    def v_overlap(self, other: "BBox") -> float:
        """Vertical overlap as fraction of the shorter box height (0..1)."""
        span = min(self.y1, other.y1) - max(self.y0, other.y0)
        base = min(self.height, other.height)
        return max(0.0, span) / base if base > 0 else 0.0

    def h_overlap(self, other: "BBox") -> float:
        span = min(self.x1, other.x1) - max(self.x0, other.x0)
        base = min(self.width, other.width)
        return max(0.0, span) / base if base > 0 else 0.0

    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    def inside_frac(self, other: "BBox") -> float:
        """Fraction of this box's area that falls inside `other`."""
        ix = min(self.x1, other.x1) - max(self.x0, other.x0)
        iy = min(self.y1, other.y1) - max(self.y0, other.y0)
        a = self.area()
        return (max(0.0, ix) * max(0.0, iy)) / a if a > 0 else 0.0

    def relative(self, page_width: float, page_height: float) -> dict[str, float]:
        """Page-relative geometry - what templates store instead of raw points."""
        w, h = page_width or 1.0, page_height or 1.0
        return {
            "rel_x_pct": round(self.cx / w, 4),
            "rel_y_pct": round(self.cy / h, 4),
            "rel_w_pct": round(self.width / w, 4),
            "rel_h_pct": round(self.height / h, 4),
        }

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


@dataclass
class Word:
    """One whitespace-delimited word with its layout coordinates."""
    text: str
    bbox: BBox
    page: int
    block_no: int
    line_no: int
    word_no: int
    from_annotation: bool = False   # word sits inside an annotation rect


@dataclass
class TextBlock:
    """A PyMuPDF text block (paragraph-ish grouping) with its lines."""
    text: str
    bbox: BBox
    page: int
    block_no: int
    lines: list["TextLine"] = field(default_factory=list)
    from_annotation: bool = False

    @property
    def normalized_text(self) -> str:
        return normalize(self.text)


@dataclass
class TextLine:
    """A single rendered line - the atom most CRF field labels live on."""
    text: str
    bbox: BBox
    page: int
    block_no: int
    line_no: int
    size: float = 0.0          # dominant font size
    font: str = ""             # dominant font name
    bold: bool = False
    from_annotation: bool = False
    region: str = ""           # HEADER | BODY | FOOTER (set by layout pass)
    column: int | None = None  # index into Page.column_bands
    group_id: int | None = None

    @property
    def normalized_text(self) -> str:
        return normalize(self.text)


@dataclass
class Rule:
    """A ruled line: section separator or table border."""
    page: int
    bbox: BBox
    orientation: str           # H | V
    span_pct: float            # length as fraction of page width (H) or height (V)


@dataclass
class Control:
    """A response control: answer box, checkbox/radio circle, fill-in rule, AcroForm widget."""
    page: int
    bbox: BBox
    kind: str                  # BOX | CIRCLE | WIDGET
    widget_type: str = ""      # AcroForm field type, WIDGET only
    field_name: str = ""


@dataclass
class ColumnBand:
    """A vertical band of the page body found by gutter detection."""
    index: int
    x0: float
    x1: float
    role_hint: str = "UNKNOWN"  # QUESTION_ZONE | RESPONSE_ZONE | UNKNOWN

    def contains(self, bbox: BBox, min_frac: float = 0.5) -> bool:
        span = min(self.x1, bbox.x1) - max(self.x0, bbox.x0)
        return bbox.width <= 0 or span / bbox.width >= min_frac


@dataclass
class TextGroup:
    """One logical label: rendered lines merged back into the text a human reads.

    A wrapped question ("Please record / protocol / version on / ...") is six
    TextLines but one TextGroup. Constituent lines are kept so every merge is auditable.
    """
    page: int
    text: str
    bbox: BBox
    lines: list[TextLine] = field(default_factory=list)
    region: str = ""
    column: int | None = None
    size: float = 0.0
    bold: bool = False
    block_ids: list[int] = field(default_factory=list)
    role: str = "UNKNOWN"      # QUESTION | RESPONSE_OPTION | SECTION_HEADER | PAGE_HEADER | FOOTER
    role_confidence: float = 0.0
    role_evidence: list[str] = field(default_factory=list)

    @property
    def normalized_text(self) -> str:
        return normalize(self.text)

    @property
    def line_count(self) -> int:
        return len(self.lines)

    def from_annotation_only(self) -> bool:
        """Group is annotation markup, not a CRF label."""
        return bool(self.lines) and all(l.from_annotation for l in self.lines)


@dataclass
class AnnotationPart:
    """One markup statement. An annotation box often holds several ("AESTDTC
    AEENDTC"), and each is classified on its own."""
    text: str
    annot_type: str = ""
    parsed: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)

    @property
    def normalized_text(self) -> str:
        return normalize(self.text)


@dataclass
class Annotation:
    """A PDF annotation object - the SDTM markup layer (Phases 4-5)."""
    page: int
    text: str
    bbox: BBox
    subtype: str = ""                 # FreeText, Square, Popup, ...
    author: str = ""
    content: str = ""                 # annot /Contents
    title: str = ""                   # annot /T (author field)
    color: tuple[float, ...] | None = None       # annot /C
    # The box's background, resolved from /IC, the appearance stream or /C - see
    # extract.ACRFParser._annot_fill. Kept apart from `text_color` because a
    # house style is a *pair* (red on yellow), and /C alone cannot tell them
    # apart: for a FreeText annotation /C is the fill and /DA carries the text.
    fill_color: tuple[float, ...] | None = None
    fill_source: str = ""             # IC | APPEARANCE | C, blank when unfilled
    # Rendered appearance, read from the /DA string (see extract.parse_da).
    # This is the raw material for house-style derivation: an aCRF's colour and
    # font conventions are measurable facts about the corpus, not judgement calls.
    text_color: tuple[float, ...] | None = None
    font_name: str = ""
    font_size: float = 0.0
    xref: int = 0
    id: str = ""                      # stable within a document: p<page>a<index>
    annot_type: str = ""              # Phase 5 classification, filled later
    parsed: dict[str, Any] = field(default_factory=dict)  # Phase 5 payload
    type_confidence: float = 0.0
    type_evidence: list[str] = field(default_factory=list)
    parts: list[AnnotationPart] = field(default_factory=list)
    form_name: str = ""

    @property
    def normalized_text(self) -> str:
        return normalize(self.text)


@dataclass
class CrossReference:
    """A "See Page 7" pointer: this page's markup lives on another page."""
    page: int
    target_page: int
    text: str
    bbox: BBox
    resolved: bool = False      # target page exists in this document
    target_form: str = ""


@dataclass
class Form:
    """One CRF form, possibly spanning several pages (Phase 2)."""
    name: str
    pages: list[int] = field(default_factory=list)
    domain: str = ""                  # SDTM domain code (DM, MH, ...) when known
    raw_titles: list[str] = field(default_factory=list)
    source: str = ""                  # TITLE_LINE | DOMAIN_ANNOTATION | CROSS_REFERENCE | INHERITED
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)
    continuation_pages: list[int] = field(default_factory=list)
    cross_references: list[CrossReference] = field(default_factory=list)

    @property
    def normalized_name(self) -> str:
        return normalize(self.name)

    @property
    def first_page(self) -> int:
        return min(self.pages) if self.pages else 0


@dataclass
class ResponseOption:
    """One choice in a field's codelist ("Amendment 1"), kept with its geometry
    because an option can carry markup of its own."""
    text: str
    bbox: BBox
    group_id: int | None = None

    @property
    def normalized_text(self) -> str:
        return normalize(self.text)


@dataclass
class Field:
    """One CRF question: the left half of the (form_name, field_text) key (Phase 3)."""
    id: str                           # stable within a document: p<page>f<index>
    form_name: str
    page: int
    text: str                         # label, numbering and trailing colon removed
    raw_text: str                     # exactly as printed, wrapped lines rejoined
    bbox: BBox
    group_id: int | None = None
    column: int | None = None
    role: str = ""
    section: str = ""                 # enclosing section header, when there is one
    item_number: str = ""             # "1." / "a)" / "" - stripped off `text`
    options: list[ResponseOption] = field(default_factory=list)
    control_kinds: list[str] = field(default_factory=list)
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)

    @property
    def option_texts(self) -> list[str]:
        return [o.text for o in self.options]

    @property
    def normalized_text(self) -> str:
        return normalize(self.text)

    @property
    def key(self) -> tuple[str, str]:
        """Primary key. Never field text alone - "Start Date" is domain-dependent."""
        return (normalize(self.form_name), self.normalized_text)


# How a link came to exist. Ranked: a reviewer's decision outranks anything the
# geometry inferred, and an explicit rejection is evidence in its own right.
GEOMETRIC, HUMAN_APPROVED, HUMAN_REJECTED = "GEOMETRIC", "HUMAN_APPROVED", "HUMAN_REJECTED"
TRUST_RANK = {HUMAN_APPROVED: 2, GEOMETRIC: 1, HUMAN_REJECTED: 0}


@dataclass
class Link:
    """A scored field <-> annotation association (Phase 6)."""
    field_id: str
    annotation_id: str
    page: int
    link_score: float
    evidence: list[str] = field(default_factory=list)
    rejected: bool = False            # kept for audit: scored but lost to a better link
    trust: str = GEOMETRIC


@dataclass
class Page:
    """Everything Phase 1 extracts for one page."""
    number: int                        # 1-based
    width: float
    height: float
    rotation: int
    text: str
    blocks: list[TextBlock] = field(default_factory=list)
    lines: list[TextLine] = field(default_factory=list)
    words: list[Word] = field(default_factory=list)
    annotations: list[Annotation] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    controls: list[Control] = field(default_factory=list)
    # filled by the layout pass
    groups: list[TextGroup] = field(default_factory=list)
    column_bands: list[ColumnBand] = field(default_factory=list)
    body_top: float = 0.0
    body_bottom: float = 0.0
    # filled by Phase 2 / 3
    form_name: str = ""
    form_domain: str = ""
    form_source: str = ""             # TITLE_LINE | SECTION_HEADER | DOMAIN_ANNOTATION | ...
    is_continuation: bool = False
    form_confidence: float = 0.0
    form_evidence: list[str] = field(default_factory=list)
    cross_references: list[CrossReference] = field(default_factory=list)
    fields: list[Field] = field(default_factory=list)

    @property
    def normalized_text(self) -> str:
        return normalize(self.text)

    def groups_by_role(self, role: str) -> list[TextGroup]:
        return [g for g in self.groups if g.role == role]

    # Page-body text only: annotation markup stripped out (PyMuPDF folds
    # annotation appearance text into page text). Phase 3 reads this.
    @property
    def content_lines(self) -> list[TextLine]:
        return [l for l in self.lines if not l.from_annotation]

    @property
    def content_words(self) -> list[Word]:
        return [w for w in self.words if not w.from_annotation]

    @property
    def content_text(self) -> str:
        return "\n".join(l.text for l in self.content_lines)


@dataclass
class Document:
    """Parsed PDF: pages plus file-level metadata."""
    path: str
    page_count: int
    metadata: dict[str, Any] = field(default_factory=dict)
    pages: list[Page] = field(default_factory=list)
    forms: list[Form] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)

    def page(self, number: int) -> Page | None:
        """1-based page lookup."""
        return next((p for p in self.pages if p.number == number), None)

    def iter_annotations(self) -> Iterator[Annotation]:
        for p in self.pages:
            yield from p.annotations

    def iter_fields(self) -> Iterator[Field]:
        for p in self.pages:
            yield from p.fields

    def form(self, name: str) -> Form | None:
        """Look a form up by name, normalized so "Form: Demographics" matches."""
        key = normalize(name)
        return next((f for f in self.forms if f.normalized_name == key), None)

    def annotation(self, annot_id: str) -> Annotation | None:
        return next((a for a in self.iter_annotations() if a.id == annot_id), None)

    def field(self, field_id: str) -> Field | None:
        return next((f for f in self.iter_fields() if f.id == field_id), None)

    def links_for(self, field_id: str) -> list[Link]:
        """Accepted links for one field, best score first."""
        return sorted((l for l in self.links if l.field_id == field_id and not l.rejected),
                      key=lambda l: -l.link_score)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict; BBox becomes a 4-list."""
        def enc(o) -> Any:
            if isinstance(o, BBox):
                return list(o.as_tuple())
            if isinstance(o, TextGroup):     # compact line refs, not full copies
                d = {f.name: enc(getattr(o, f.name)) for f in fields(o) if f.name != "lines"}
                d["lines"] = [{"block_no": l.block_no, "line_no": l.line_no,
                               "text": l.text, "bbox": list(l.bbox.as_tuple())} for l in o.lines]
                d["normalized_text"], d["line_count"] = o.normalized_text, o.line_count
                page = self.page(o.page)
                if page:            # templates key off relative position, not points
                    d["relative"] = o.bbox.relative(page.width, page.height)
                return d
            if is_dataclass(o):
                return {f.name: enc(getattr(o, f.name)) for f in fields(o)}
            if isinstance(o, dict):
                return {k: enc(v) for k, v in o.items()}
            if isinstance(o, (list, tuple)):
                return [enc(v) for v in o]
            return o
        return enc(self)
