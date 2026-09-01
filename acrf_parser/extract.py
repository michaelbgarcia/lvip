"""Phase 1 - PDF extraction.

Pulls every textual and annotation object out of an aCRF PDF. No interpretation
happens here: later phases consume the Document this produces.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pymupdf

from . import anchors, annotations as annots
from . import fields as flds
from . import forms, layout, linking
from .models import (Annotation, BBox, Control, Document, Page, Rule, TextBlock,
                     TextLine, Word)
from .normalize import clean

_BOLD_FLAG = 1 << 4     # span flags bit for bold
_RULE_MAX_THICK = 2.0   # a filled rect this thin is a ruled line, not a box
_CTRL_MIN_SIDE = 4.0    # smaller than this is decoration, not an input control
_ANNOT_COVER = 0.9      # a drawing this far inside an annotation rect is its own box

# /DA ("default appearance") operators. A FreeText annotation's colour and font
# live here, not in /C or /IC - `annot.colors` comes back empty for them, which
# is why the styling looked absent until this was parsed.
_DA_FONT = re.compile(r"/([^\s/]+)\s+([\d.]+)\s+Tf")
_DA_RGB = re.compile(r"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+rg\b", re.I)
_DA_GRAY = re.compile(r"(?<![\d.])([\d.]+)\s+g\b")
_DA_CMYK = re.compile(r"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+k\b", re.I)

# Appearance-stream scanning (see _appearance_fill). Content-stream operators
# are postfix: operands accumulate, then one token consumes them.
_NUMBER = re.compile(r"^[-+]?(?:\d+\.?\d*|\.\d+)$")
_FILL_OPS = {"f", "F", "f*", "b", "b*", "B", "B*"}   # ops that paint an interior
_PATH_END_OPS = {"n", "S", "s", "W", "W*"}           # path discarded or clipped/stroked
_SHOW_TEXT_OPS = {"Tj", "TJ", "'", '"'}              # ops that paint glyphs
_FILL_MIN_COVER = 0.5   # a fill smaller than half the box is decoration, not background

# Annotation subtypes that are review apparatus rather than markup on the page.
# A `Text` annotation is a sticky note - the little icon a reviewer leaves in
# Acrobat - and `Popup` is the balloon that opens from one. Neither renders as
# part of the form, and the MSG example CRF carries four ("Accepted set by Me",
# "MigrationConfirmed set by Me") left behind by whoever signed it off. Read as
# markup they became four annotations of a form, four staging rows to review and
# four boxes drawn onto the next study's CRF.
_NOT_MARKUP = frozenset({"Text", "Popup", "Link", "FileAttachment", "Sound",
                         "Movie", "Screen", "PrinterMark", "TrapNet", "Watermark"})
# Subtypes whose entire content is the text they carry, so an empty one is
# empty. A Square or a Highlight says what it says by where it is.
_TEXT_BEARING = frozenset({"FreeText"})


def page_size(page: pymupdf.Page) -> tuple[float, float]:
    """The page's size as a reader sees it - `page.rect`, rotation applied.

    Everything in this parser works in *display* coordinates (see
    `display_matrix`), so this is the size those coordinates are expressed in: a
    landscape CRF page is 792 wide and 612 tall, whatever its media box says.
    """
    rect = page.rect
    return round(rect.width, 2), round(rect.height, 2)


def display_matrix(page: pymupdf.Page) -> pymupdf.Matrix | None:
    """The transform from PDF page space into reading order, or None if identity.

    A landscape CRF page is usually a portrait page carrying `/Rotate 90`, and
    PyMuPDF gives out two different coordinate systems for it. `page.rect` is
    what a reader displays - 792x612 - but every coordinate the library hands
    back or takes in is in the *unrotated* page space: `get_text` puts words at
    y=750 on a page `rect` calls 612 tall, `annot.rect` agrees with those words,
    and `add_freetext_annot` places a box by them too.

    That is not a cosmetic difference, because this whole parser reasons in
    reading order. "Same row" is vertical overlap, columns are found by cutting
    on vertical whitespace, a title lives in the top third of the page, a
    wrapped line is the one below its predecessor. On a quarter-turned page in
    raw coordinates, *rows run down the x axis*: three of the MSG CRF's 22 pages
    are like that, and on them every one of those tests asks its question about
    the wrong axis. It is why "Reason for Discontinuation" came back as three
    unrelated fragments and why those pages yielded no fields at all.

    Rotating once, here, is what keeps the rest of the pipeline free of it: no
    module below Phase 1 has to know a page can be turned. `Page.rotation` still
    records the quarter turn, and `writer` inverts this matrix on the way out
    because `add_freetext_annot` wants page space back.
    """
    return page.rotation_matrix if page.rotation % 360 else None


class ACRFParser:
    """Deterministic PDF extractor. One instance per PDF file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        self.document: Document | None = None
        # Set per page. Every box this class produces goes through `_box`, so a
        # quarter-turned page is rotated into reading order exactly once and no
        # later phase has to know it was turned - see `display_matrix`.
        self._matrix: pymupdf.Matrix | None = None

    def _box(self, rect) -> BBox:
        """One extracted rectangle, in display coordinates."""
        if self._matrix is None:
            return BBox.of(rect)
        return BBox.of(pymupdf.Rect(rect) * self._matrix)

    # ---- main entry point -------------------------------------------------
    def parse_pdf(self) -> Document:
        """Open the PDF and extract text + annotations for every page."""
        with pymupdf.open(self.path) as doc:
            if doc.needs_pass:
                raise ValueError(f"{self.path.name} is password protected")
            pages = [self._parse_page(p) for p in doc]
            layout.analyze_document(pages)      # needs every page: running headers
            self.document = Document(
                path=str(self.path),
                page_count=doc.page_count,
                metadata={k: v for k, v in (doc.metadata or {}).items() if v},
                pages=pages,
            )
            self.document.forms = forms.detect_forms(pages)   # Phase 2
            flds.extract_fields(pages)                        # Phase 3
            annots.extract_annotations(pages)                 # Phases 4-5
            linking.link_document(self.document)              # Phase 6
            anchors.resolve_form_level(self.document)         # Phase 6b
        return self.document

    # ---- per-page work ----------------------------------------------------
    def _parse_page(self, page: pymupdf.Page) -> Page:
        self._matrix = display_matrix(page)
        w, h = page_size(page)
        out = Page(
            number=page.number + 1,          # 1-based, matches "See Page 7"
            width=w,
            height=h,
            rotation=page.rotation,
            text=page.get_text("text"),
        )
        out.blocks, out.lines = self._blocks_and_lines(page)
        out.words = self._words(page)
        out.annotations = self._annotations(page)
        out.rules, out.controls = self._graphics(page, out.annotations)
        self._tag_annotation_text(out)
        return out                   # layout pass runs document-wide in parse_pdf

    def _graphics(self, page: pymupdf.Page,
                  annotations: list[Annotation]) -> tuple[list[Rule], list[Control]]:
        """Split vector drawings into separators (rules) and response controls.

        Rules stop line grouping running across section boundaries; controls are
        the evidence that a column holds responses rather than questions.

        An annotation's own background and border are vector drawings too - a
        study that fills its markup boxes would otherwise hand every annotation
        back as an answer box, and a page of markup would read as a page of
        response controls. So drawings sitting inside an annotation rect are its
        appearance, not the form's.
        """
        rules: list[Rule] = []
        controls: list[Control] = []
        pw, ph = page_size(page)
        w, h = pw or 1.0, ph or 1.0
        annot_rects = [a.bbox for a in annotations]
        for d in page.get_drawings():
            box = self._box(d["rect"])
            if any(box.inside_frac(r) >= _ANNOT_COVER for r in annot_rects):
                continue
            kinds = {it[0] for it in d.get("items", [])}
            if box.height <= _RULE_MAX_THICK and box.width > _CTRL_MIN_SIDE:
                rules.append(Rule(page.number + 1, box, "H", round(box.width / w, 4)))
            elif box.width <= _RULE_MAX_THICK and box.height > _CTRL_MIN_SIDE:
                rules.append(Rule(page.number + 1, box, "V", round(box.height / h, 4)))
            elif box.width >= _CTRL_MIN_SIDE and box.height >= _CTRL_MIN_SIDE:
                kind = "CIRCLE" if "c" in kinds else "BOX"
                controls.append(Control(page.number + 1, box, kind))
        for wd in page.widgets():    # AcroForm fields; annots() excludes these
            controls.append(Control(page.number + 1, self._box(wd.rect), "WIDGET",
                                    widget_type=str(wd.field_type_string or ""),
                                    field_name=str(wd.field_name or "")))
        return rules, controls

    @staticmethod
    def _tag_annotation_text(page: Page, min_frac: float = 0.6) -> None:
        """Flag text objects that are really annotation markup.

        PyMuPDF renders annotation appearance streams into the page text layer,
        so `BRTHDTC` shows up both as an Annotation and as a TextLine. Anything
        mostly inside an annotation rect is markup, not a CRF field label.
        """
        rects = [a.bbox for a in page.annotations]
        if not rects:
            return
        for obj in (*page.words, *page.lines):
            obj.from_annotation = any(obj.bbox.inside_frac(r) >= min_frac for r in rects)
        for b in page.blocks:
            b.from_annotation = bool(b.lines) and all(l.from_annotation for l in b.lines)

    def _blocks_and_lines(self, page: pymupdf.Page) -> tuple[list[TextBlock], list[TextLine]]:
        """Text blocks with their lines; font size/bold kept for header detection."""
        blocks: list[TextBlock] = []
        lines: list[TextLine] = []
        data = page.get_text("dict", sort=True)
        for b in data.get("blocks", []):
            if b.get("type") != 0:           # 0 = text, 1 = image
                continue
            tb = TextBlock(text="", bbox=self._box(b["bbox"]), page=page.number + 1,
                           block_no=b["number"])
            for ln, line in enumerate(b.get("lines", [])):
                spans = line.get("spans", [])
                text = clean("".join(s.get("text", "") for s in spans))
                if not text:
                    continue
                lead = max(spans, key=lambda s: len(s.get("text", "")), default={})
                tl = TextLine(
                    text=text, bbox=self._box(line["bbox"]), page=page.number + 1,
                    block_no=b["number"], line_no=ln,
                    size=round(lead.get("size", 0.0), 2),
                    font=lead.get("font", ""),
                    bold=bool(lead.get("flags", 0) & _BOLD_FLAG) or "bold" in lead.get("font", "").lower(),
                )
                tb.lines.append(tl)
                lines.append(tl)
            tb.text = "\n".join(l.text for l in tb.lines)
            if tb.text:
                blocks.append(tb)
        return blocks, lines

    def _words(self, page: pymupdf.Page) -> list[Word]:
        """Word-level layout - the finest grain available for row/column logic."""
        return [
            Word(text=w[4], bbox=self._box(w[:4]), page=page.number + 1,
                 block_no=int(w[5]), line_no=int(w[6]), word_no=int(w[7]))
            for w in page.get_text("words", sort=True)
        ]

    def _annotations(self, page: pymupdf.Page) -> list[Annotation]:
        """PDF annotation objects: SDTM markup lives here as /Contents or appearance text."""
        annots: list[Annotation] = []
        fallback = self._acroform_da(page.parent)
        for a in page.annots():
            subtype = a.type[1] if a.type else ""
            if subtype in _NOT_MARKUP:
                continue                 # a reviewer's sticky note, not the form's
            info = a.info or {}
            content = clean(info.get("content", ""))
            text = content or self._annot_appearance_text(a)
            painted = self._appearance_text_color(page.parent, a.xref)
            if not text and subtype in _TEXT_BEARING:
                # A FreeText box with no text in it, in /Contents or in its own
                # appearance stream. Text is the whole of what a FreeText says,
                # so an empty one cannot be classified, linked, staged or drawn -
                # every later phase already treats it as nothing - and carrying
                # it only tells a reviewer the file holds more markup than it
                # does. The MSG example CRF has three.
                #
                # Scoped to the text-bearing subtypes on purpose: a Square or a
                # Highlight means something by *where* it is, and is expected to
                # carry no text at all.
                continue
            style = parse_da(self._annot_da(page.parent, a.xref) or fallback)
            fill, fill_source = self._annot_fill(page.parent, a)
            annots.append(Annotation(
                page=page.number + 1,
                text=text,
                bbox=self._box(a.rect),
                subtype=subtype,
                author=clean(info.get("title", "")),
                content=content,
                title=clean(info.get("title", "")),
                color=self._annot_color(a),
                fill_color=fill,
                fill_source=fill_source,
                # What the page actually shows, falling back to what /DA claims.
                # The two disagree more often than they should - see
                # `_appearance_text_color`.
                text_color=painted or style["text_color"],
                font_name=style["font_name"],
                font_size=style["font_size"],
                xref=a.xref,
            ))
        return annots

    @staticmethod
    def _annot_da(pdf: pymupdf.Document, xref: int) -> str:
        """The annotation's own /DA string, if it carries one."""
        try:
            kind, value = pdf.xref_get_key(xref, "DA")
        except Exception:
            return ""
        return value if kind == "string" else ""

    @staticmethod
    def _acroform_da(pdf: pymupdf.Document) -> str:
        """Document-level default appearance.

        An annotation with no /DA of its own inherits the AcroForm default, so a
        study that sets its house style once at the document level would
        otherwise read as having no styling at all.
        """
        try:
            kind, value = pdf.xref_get_key(pdf.pdf_catalog(), "AcroForm")
            if kind == "xref":
                kind, value = pdf.xref_get_key(int(value.split()[0]), "DA")
                return value if kind == "string" else ""
        except Exception:
            pass
        return ""

    @staticmethod
    def _annot_appearance_text(a: pymupdf.Annot) -> str:
        """Text drawn inside the annotation's own appearance stream (not page text)."""
        try:
            return clean(a.get_text("text"))
        except Exception:
            return ""

    @staticmethod
    def _annot_color(a: pymupdf.Annot) -> tuple[float, ...] | None:
        """The annotation's /C entry - its border/background colour key."""
        c = (a.colors or {}).get("stroke")
        return tuple(round(float(v), 3) for v in c) if c else None

    @classmethod
    def _annot_fill(cls, pdf: pymupdf.Document, a: pymupdf.Annot) -> tuple[tuple[float, ...] | None, str]:
        """The box's background colour, and where it was found.

        Three places hold it, and a study may use any of them:

        * `/IC` (interior colour) - the spec's answer, but many tools omit it;
        * the appearance stream, which is what a reader actually paints and so
          is the only reliable source for a highlight drawn as `1 1 0 rg ... re f`;
        * `/C`, which for a FreeText annotation *is* the background (the text
          colour lives in /DA), so a red-on-yellow box with only /C set is
          yellow, not red - reading /C as "the colour" is what makes fill and
          text colour look identical.

        Returns `(rgb, "IC" | "APPEARANCE" | "C")`, or `(None, "")`.
        """
        ic = (a.colors or {}).get("fill")
        if ic:
            return tuple(round(float(v), 3) for v in ic), "IC"
        painted = cls._appearance_fill(pdf, a)
        if painted:
            return painted, "APPEARANCE"
        stroke = (a.colors or {}).get("stroke")
        if stroke and (a.type[1] if a.type else "") == "FreeText":
            return tuple(round(float(v), 3) for v in stroke), "C"
        return None, ""

    @classmethod
    def _appearance_fill(cls, pdf: pymupdf.Document, a: pymupdf.Annot) -> tuple[float, ...] | None:
        """Background colour painted by the annotation's own appearance stream.

        Interprets just enough of the content stream to answer "what colour is
        the largest filled rectangle?": non-stroking colour operators (`rg`/`g`/
        `k`), rectangle paths, and the fill operators. A fill covering less than
        `_FILL_MIN_COVER` of the box is ignored - that is an underline or a
        glyph, not the box's background.
        """
        stream = cls._appearance_stream(pdf, a.xref)
        if not stream:
            return None
        box_area = abs(a.rect.width * a.rect.height) or 1.0
        color: tuple[float, ...] | None = None
        best: tuple[tuple[float, ...] | None, float] = (None, 0.0)
        area = 0.0                       # area of rectangles in the current path
        ops: list[float] = []
        for tok in stream.split():
            if _NUMBER.match(tok):
                ops.append(float(tok))
                continue
            if tok == "rg" and len(ops) >= 3:
                color = tuple(round(v, 3) for v in ops[-3:])
            elif tok == "g" and ops:
                v = round(ops[-1], 3)
                color = (v, v, v)
            elif tok == "k" and len(ops) >= 4:
                c, m, y, k = ops[-4:]
                color = tuple(round((1 - v) * (1 - k), 3) for v in (c, m, y))
            elif tok == "re" and len(ops) >= 4:
                area += abs(ops[-2] * ops[-1])
            elif tok in _FILL_OPS:
                if color is not None and area > best[1]:
                    best = (color, area)
                area = 0.0
            elif tok in _PATH_END_OPS:
                area = 0.0
            ops = []
        return best[0] if best[1] / box_area >= _FILL_MIN_COVER else None

    @classmethod
    def _appearance_text_color(cls, pdf: pymupdf.Document, xref: int) -> tuple[float, ...] | None:
        """The colour the annotation's glyphs are actually painted in.

        `/DA` is supposed to say this, and on a real aCRF it frequently does not.
        Every annotation on the CDISC MSG example CRF carries
        `0 0 0 rg /Arial,BoldItalic 12 Tf`, and every variable on it is drawn in
        red - the appearance stream sets `1 0 0 rg` after the /DA has had its
        say, and the appearance stream is what a reader paints. Believing /DA
        there means learning a house style of black text from a corpus that is
        not black, and then drawing the next study's aCRF in the wrong colour.

        So this reads the non-stroking colour in force at the first text-showing
        operator, which is by construction the colour the text comes out. The
        two rules together - appearance first, /DA second - are the same
        precedence `_annot_fill` already applies to the background, and for the
        same reason.
        """
        stream = cls._appearance_stream(pdf, xref)
        if not stream:
            return None
        color: tuple[float, ...] | None = None
        ops: list[float] = []
        for tok in stream.split():
            if _NUMBER.match(tok):
                ops.append(float(tok))
                continue
            if tok == "rg" and len(ops) >= 3:
                color = tuple(round(v, 3) for v in ops[-3:])
            elif tok == "g" and ops:
                v = round(ops[-1], 3)
                color = (v, v, v)
            elif tok == "k" and len(ops) >= 4:
                c, m, y, k = ops[-4:]
                color = tuple(round((1 - v) * (1 - k), 3) for v in (c, m, y))
            elif tok in _SHOW_TEXT_OPS:
                return color
            ops = []
        return None

    @staticmethod
    def _appearance_stream(pdf: pymupdf.Document, xref: int) -> str:
        """Decoded /AP /N content stream, or "" if the annotation has none."""
        try:
            kind, value = pdf.xref_get_key(xref, "AP/N")
            if kind != "xref":
                return ""
            return pdf.xref_stream(int(value.split()[0])).decode("latin-1", "replace")
        except Exception:
            return ""


# ---- convenience ----------------------------------------------------------
def parse_pdf(path: str | Path) -> Document:
    """Parse one aCRF PDF and return its Document."""
    return ACRFParser(path).parse_pdf()


def dump_json(doc: Document, path: str | Path, indent: int = 2) -> Path:
    """Write the raw extraction to JSON (Phase 1 output artifact)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc.to_dict(), indent=indent, ensure_ascii=False))
    return path


def summarize(doc: Document) -> dict[str, Any]:
    """Quick counts for sanity-checking an extraction run."""
    return {
        "file": Path(doc.path).name,
        "forms": forms.summarize_forms(doc.forms),
        **flds.summarize_fields(list(doc.iter_fields())),
        **annots.summarize_annotations(list(doc.iter_annotations())),
        **linking.summarize_links(doc),
        "pages": doc.page_count,
        "blocks": sum(len(p.blocks) for p in doc.pages),
        "lines": sum(len(p.lines) for p in doc.pages),
        "words": sum(len(p.words) for p in doc.pages),
        "annotations": sum(len(p.annotations) for p in doc.pages),
        "content_lines": sum(len(p.content_lines) for p in doc.pages),
        "groups": sum(len(p.groups) for p in doc.pages),
        "wrapped_groups": sum(1 for p in doc.pages for g in p.groups if g.line_count > 1),
        "columns_per_page": [len(p.column_bands) for p in doc.pages],
        "rules": sum(len(p.rules) for p in doc.pages),
        "controls": sum(len(p.controls) for p in doc.pages),
        "pages_without_text": [p.number for p in doc.pages if not p.text.strip()],
        "pages_without_annotations": [p.number for p in doc.pages if not p.annotations],
    }


def parse_da(da: str) -> dict[str, Any]:
    """Parse a PDF /DA (default appearance) string into colour, font and size.

    `'0.85 0.1 0.1 rg /Helv 8.0 Tf'` -> red text, Helvetica, 8pt. Grayscale (`g`)
    and CMYK (`k`) are converted to RGB so downstream comparison is uniform -
    house style is derived by counting identical values, and two spellings of
    the same colour would read as disagreement.

    A font size of 0 is the PDF convention for auto-size, and is reported as 0.0
    rather than guessed at.
    """
    out: dict[str, Any] = {"text_color": None, "font_name": "", "font_size": 0.0}
    if not da:
        return out
    font = _DA_FONT.search(da)
    if font:
        out["font_name"] = font.group(1)
        out["font_size"] = round(float(font.group(2)), 2)
    rgb = _DA_RGB.search(da)
    gray = _DA_GRAY.search(da)
    cmyk = _DA_CMYK.search(da)
    if rgb:
        out["text_color"] = tuple(round(float(v), 3) for v in rgb.groups())
    elif cmyk:
        c, m, y, k = (float(v) for v in cmyk.groups())
        out["text_color"] = tuple(round((1 - v) * (1 - k), 3) for v in (c, m, y))
    elif gray:
        v = round(float(gray.group(1)), 3)
        out["text_color"] = (v, v, v)
    return out
