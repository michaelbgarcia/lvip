"""The MSG ground-truth pair, and how close the pipeline gets to it.

`data/blankcrf_annotated.pdf` and `data/blankcrf.pdf` are the CDISC SDTM
Metadata Submission Guidelines example CRF: the same 22-page form, once with
the sponsor's 209 SDTM annotations on it and once without. That is a rare
thing - a real aCRF and its own blank, page for page - and it is the only
honest calibration this parser has. Everything in `tests/sample_pdf.py` was
drawn to exercise a specific code path; this was drawn by people annotating a
study.

What it makes measurable
------------------------
Because the two files are the same CRF, the annotated one is a complete answer
key for the blank one. Feed the annotated CRF in as history, stage the blank
one, approve what history filled, draw it - and every annotation that comes out
can be compared with the one the sponsor actually drew: same statement, same
page, same place, same colour, same font. No human labelling anywhere in that
loop, which is what lets it run as a test.

`score()` reports that comparison. The headline number is **geometry
fidelity**: of the statements we reproduced, how far from the sponsor's own
placement did they land. Recall and precision are reported beside it because a
pipeline that draws three annotations perfectly and loses two hundred is not
better than one that draws all of them roughly.

Two deliberate exclusions from the answer key
---------------------------------------------
* `Text` annotations. Four sticky notes ("Accepted set by Me") left by whoever
  reviewed the file in Acrobat. They are review furniture, not SDTM markup.
* Empty annotations. Three carry no text at all.

Both are excluded from ground truth *and* should be excluded by the parser; a
test pins that they are.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from statistics import median
from typing import Any, Iterable

import pymupdf

DATA = Path(__file__).resolve().parents[1] / "data"
ANNOTATED = DATA / "blankcrf_annotated.pdf"
BLANK = DATA / "blankcrf.pdf"

# Subtypes that carry SDTM markup. Everything else on this PDF is review
# furniture - see the module docstring.
MARKUP_SUBTYPES = frozenset({"FreeText"})

_TOKEN = re.compile(r"[A-Za-z0-9]+")
_DA_FONT = re.compile(r"/([^\s/]+)\s+([\d.]+)\s+Tf")
_DA_RGB = re.compile(r"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+rg\b")


def key(text: str) -> tuple[str, ...]:
    """Identity of a statement: its word set, upper-cased.

    Deliberately the same test `normalize.statement_key` uses, restated here so
    the answer key does not move when the parser's idea of sameness does. A
    scorer that shares its comparison function with the thing it scores can be
    made to pass by changing the comparison.
    """
    return tuple(sorted({t.upper() for t in _TOKEN.findall(text or "")}))


@dataclass(frozen=True)
class Truth:
    """One annotation as the sponsor drew it."""
    page: int
    text: str
    rect: tuple[float, float, float, float]
    text_color: tuple[float, ...] | None
    font_name: str
    font_size: float

    @property
    def key(self) -> tuple[str, ...]:
        return key(self.text)

    @property
    def cx(self) -> float:
        return (self.rect[0] + self.rect[2]) / 2

    @property
    def cy(self) -> float:
        return (self.rect[1] + self.rect[3]) / 2


def ground_truth(path: str | Path = ANNOTATED) -> list[Truth]:
    """Every SDTM annotation on the MSG aCRF, read straight off the PDF.

    Read with raw PyMuPDF rather than through `acrf_parser`, on purpose: an
    answer key extracted by the code under test can only ever confirm that the
    code agrees with itself.
    """
    out: list[Truth] = []
    with pymupdf.open(path) as pdf:
        for i, page in enumerate(pdf, start=1):
            # Rotated into reading order, the same way `extract.display_matrix`
            # rotates the parser's own boxes. Three MSG pages carry /Rotate 90,
            # and PyMuPDF reports annotation rects on them in unrotated page
            # space - so without this the answer key for those pages would be
            # transposed relative to everything being scored against it.
            m = page.rotation_matrix if page.rotation % 360 else None
            for a in (page.annots() or []):
                subtype = a.type[1] if a.type else ""
                if subtype not in MARKUP_SUBTYPES:
                    continue
                text = " ".join((a.info or {}).get("content", "").split())
                if not text:
                    continue
                da = _da(pdf, a.xref)
                font = _DA_FONT.search(da)
                rgb = _DA_RGB.search(da)
                rect = a.rect if m is None else a.rect * m
                out.append(Truth(
                    page=i, text=text,
                    rect=tuple(round(v, 2) for v in rect),
                    # The colour the page shows, not the colour /DA claims. On
                    # this CRF /DA says black for every annotation and the
                    # variables are drawn in red, because the appearance stream
                    # overrides it - and the appearance stream is what a reader
                    # paints. An answer key that believed /DA would be marking
                    # the pipeline right for reproducing a colour nobody can see.
                    text_color=_painted_color(pdf, a.xref)
                    or (tuple(round(float(v), 3) for v in rgb.groups()) if rgb else None),
                    font_name=font.group(1) if font else "",
                    font_size=round(float(font.group(2)), 2) if font else 0.0))
    return out


_COLOR_OP = re.compile(r"(?:([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+rg|([\d.]+)\s+g)")
_SHOW_TEXT = re.compile(r"\b(?:Tj|TJ)\b|'|\"")


def _painted_color(pdf: pymupdf.Document, xref: int) -> tuple[float, ...] | None:
    """The non-stroking colour in force when the annotation's text is painted.

    Written independently of `extract._appearance_text_color` rather than
    imported from it, for the reason the whole module exists: an answer key that
    shares its reading with the code under test can only confirm that the code
    agrees with itself.
    """
    try:
        kind, value = pdf.xref_get_key(xref, "AP/N")
        if kind != "xref":
            return None
        stream = pdf.xref_stream(int(value.split()[0])).decode("latin-1", "replace")
    except Exception:
        return None
    show = _SHOW_TEXT.search(stream)
    if not show:
        return None
    last = None
    for m in _COLOR_OP.finditer(stream[:show.start()]):
        last = m
    if last is None:
        return None
    if last.group(4) is not None:
        v = round(float(last.group(4)), 3)
        return (v, v, v)
    return tuple(round(float(v), 3) for v in last.group(1, 2, 3))


def _da(pdf: pymupdf.Document, xref: int) -> str:
    try:
        kind, value = pdf.xref_get_key(xref, "DA")
    except Exception:
        return ""
    return value if kind == "string" else ""


# --- scoring ---------------------------------------------------------------
@dataclass
class Pair:
    """One ground-truth annotation and what the pipeline drew for it."""
    truth: Truth
    drawn_rect: tuple[float, float, float, float] | None = None
    drawn_text: str = ""
    drawn_color: tuple[float, ...] | None = None
    drawn_font: str = ""
    drawn_size: float = 0.0

    @property
    def matched(self) -> bool:
        return self.drawn_rect is not None

    @property
    def dx(self) -> float:
        return (self.drawn_rect[0] + self.drawn_rect[2]) / 2 - self.truth.cx

    @property
    def dy(self) -> float:
        return (self.drawn_rect[1] + self.drawn_rect[3]) / 2 - self.truth.cy

    @property
    def distance(self) -> float:
        return (self.dx ** 2 + self.dy ** 2) ** 0.5


@dataclass
class Score:
    """How close one run came to the sponsor's own aCRF."""
    truth_count: int = 0
    drawn_count: int = 0
    pairs: list[Pair] = dc_field(default_factory=list)
    extra: list[tuple[int, str]] = dc_field(default_factory=list)   # drawn, no truth

    @property
    def matched(self) -> list[Pair]:
        return [p for p in self.pairs if p.matched]

    @property
    def missing(self) -> list[Truth]:
        return [p.truth for p in self.pairs if not p.matched]

    @property
    def recall(self) -> float:
        """Share of the sponsor's annotations we reproduced at all."""
        return len(self.matched) / self.truth_count if self.truth_count else 0.0

    @property
    def precision(self) -> float:
        """Share of what we drew that the sponsor also drew."""
        return len(self.matched) / self.drawn_count if self.drawn_count else 0.0

    def within(self, points: float) -> float:
        """Share of *matched* annotations placed within `points` of the original."""
        m = self.matched
        return sum(1 for p in m if p.distance <= points) / len(m) if m else 0.0

    @property
    def median_distance(self) -> float:
        return round(median([p.distance for p in self.matched]), 1) if self.matched else 0.0

    @property
    def median_dx(self) -> float:
        return round(median([abs(p.dx) for p in self.matched]), 1) if self.matched else 0.0

    @property
    def median_dy(self) -> float:
        return round(median([abs(p.dy) for p in self.matched]), 1) if self.matched else 0.0

    def style_match(self, attr: str) -> float:
        """Share of matched annotations whose colour / font / size we reproduced."""
        m = self.matched
        if not m:
            return 0.0
        get = {"color": lambda p: (p.drawn_color, p.truth.text_color),
               "size": lambda p: (p.drawn_size, p.truth.font_size),
               "font": lambda p: (_family(p.drawn_font), _family(p.truth.font_name))}[attr]
        return sum(1 for p in m if _same(*get(p))) / len(m)

    def to_dict(self) -> dict[str, Any]:
        return {
            "truth": self.truth_count,
            "drawn": self.drawn_count,
            "matched": len(self.matched),
            "recall": round(self.recall, 3),
            "precision": round(self.precision, 3),
            "median_distance_pt": self.median_distance,
            "median_dx_pt": self.median_dx,
            "median_dy_pt": self.median_dy,
            "within_12pt": round(self.within(12), 3),
            "within_36pt": round(self.within(36), 3),
            "within_72pt": round(self.within(72), 3),
            "color_match": round(self.style_match("color"), 3),
            "font_match": round(self.style_match("font"), 3),
            "size_match": round(self.style_match("size"), 3),
        }


def _family(font: str) -> str:
    """Arial,BoldItalic and Helv are the same face for scoring purposes.

    PyMuPDF can only embed the base-14 faces in an annotation appearance, so a
    study drawn in Arial can never come back as Arial. Scoring the exact name
    would measure a limitation of the PDF library rather than anything this
    pipeline decides, so the metric is the family: does our Helvetica stand in
    for their Arial, or did we reach for Courier.
    """
    f = (font or "").split(",")[0].strip().lower()
    if f.startswith(("helv", "he", "arial")) and not f.startswith("her"):
        return "sans"
    if f.startswith(("ti", "times")):
        return "serif"
    if f.startswith(("co", "cour")):
        return "mono"
    return f


def _same(a, b, tol: float = 0.05) -> bool:
    if a is None or b is None or a == "" or b == "":
        return a in (None, "") and b in (None, "")
    if isinstance(a, tuple) and isinstance(b, tuple):
        return len(a) == len(b) and all(abs(x - y) <= tol for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= 0.51        # 8pt vs 8.0pt, not 8pt vs 12pt
    return a == b


def score(drawn: Iterable, truth: Iterable[Truth] | None = None) -> Score:
    """Compare what was drawn with what the sponsor drew.

    `drawn` is any iterable of objects with `.page`, `.text` and a rect - both
    `writer.Placement` and `models.Annotation` qualify, so a run can be scored
    from the write report or from a re-parse of the output PDF.

    Matching is by (page, statement) and then by distance: where a page carries
    the same statement twice - `VISIT` appears on many pages, `QSCAT` twice on
    one - each drawn box is paired with its nearest unclaimed twin, so two
    correct annotations on one page are not scored as one hit and one miss.
    """
    truth = list(truth if truth is not None else ground_truth())
    drawn = list(drawn)
    pairs = [Pair(truth=t) for t in truth]
    by_key: dict[tuple, list[Pair]] = {}
    for p in pairs:
        by_key.setdefault((p.truth.page, p.truth.key), []).append(p)

    extra: list[tuple[int, str]] = []
    for d in drawn:
        page, text, rect = _read(d)
        pool = [p for p in by_key.get((page, key(text)), []) if not p.matched]
        if not pool:
            extra.append((page, text))
            continue
        cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
        hit = min(pool, key=lambda p: (p.truth.cx - cx) ** 2 + (p.truth.cy - cy) ** 2)
        hit.drawn_rect = rect
        hit.drawn_text = text
        hit.drawn_color = getattr(d, "text_color", None)
        hit.drawn_font = getattr(d, "font_name", "") or ""
        hit.drawn_size = getattr(d, "font_size", 0.0) or 0.0
    return Score(truth_count=len(truth), drawn_count=len(drawn), pairs=pairs, extra=extra)


def _read(d) -> tuple[int, str, tuple[float, float, float, float]]:
    """(page, text, rect) from a Placement, an Annotation, or a plain tuple."""
    box = getattr(d, "rect", None) or getattr(d, "bbox", None)
    rect = tuple(box.as_tuple()) if hasattr(box, "as_tuple") else tuple(box)[:4]
    return int(d.page), d.text, rect


def report(s: Score, top: int = 12) -> str:
    """A human-readable scorecard, for iterating at the shell."""
    d = s.to_dict()
    lines = [
        f"truth {d['truth']}  drawn {d['drawn']}  matched {d['matched']}",
        f"recall {d['recall']:.3f}  precision {d['precision']:.3f}",
        f"placement: median {d['median_distance_pt']}pt "
        f"(dx {d['median_dx_pt']}, dy {d['median_dy_pt']})  "
        f"<=12pt {d['within_12pt']:.2f}  <=36pt {d['within_36pt']:.2f}  "
        f"<=72pt {d['within_72pt']:.2f}",
        f"style: colour {d['color_match']:.2f}  font {d['font_match']:.2f}  "
        f"size {d['size_match']:.2f}",
    ]
    if s.missing:
        lines.append(f"missing ({len(s.missing)}), first {top}:")
        lines += [f"  p{t.page:<3d} {t.text[:70]}" for t in s.missing[:top]]
    if s.extra:
        lines.append(f"drawn with no counterpart ({len(s.extra)}), first {top}:")
        lines += [f"  p{p:<3d} {t[:70]}" for p, t in s.extra[:top]]
    return "\n".join(lines)
