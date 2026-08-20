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
    color: tuple[float, ...] | None = None
    xref: int = 0
    annot_type: str = ""              # Phase 5 classification, filled later
    parsed: dict[str, Any] = field(default_factory=dict)  # Phase 5 payload

    @property
    def normalized_text(self) -> str:
        return normalize(self.text)


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

    @property
    def normalized_text(self) -> str:
        return normalize(self.text)

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

    def page(self, number: int) -> Page | None:
        """1-based page lookup."""
        return next((p for p in self.pages if p.number == number), None)

    def iter_annotations(self) -> Iterator[Annotation]:
        for p in self.pages:
            yield from p.annotations

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict; BBox becomes a 4-list."""
        def enc(o):
            if isinstance(o, BBox):
                return list(o.as_tuple())
            if is_dataclass(o):
                return {f.name: enc(getattr(o, f.name)) for f in fields(o)}
            if isinstance(o, dict):
                return {k: enc(v) for k, v in o.items()}
            if isinstance(o, (list, tuple)):
                return [enc(v) for v in o]
            return o
        return enc(self)
