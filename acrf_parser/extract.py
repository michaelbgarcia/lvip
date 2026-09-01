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
_FILL_MIN_COVER = 0.5   # a fill smaller than half the box is decoration, not background


class ACRFParser:
    """Deterministic PDF extractor. One instance per PDF file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        self.document: Document | None = None

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
        rect = page.rect
        out = Page(
            number=page.number + 1,          # 1-based, matches "See Page 7"
            width=round(rect.width, 2),
            height=round(rect.height, 2),
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
        w, h = page.rect.width or 1.0, page.rect.height or 1.0
        annot_rects = [a.bbox for a in annotations]
        for d in page.get_drawings():
            box = BBox.of(d["rect"])
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
            controls.append(Control(page.number + 1, BBox.of(wd.rect), "WIDGET",
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
            tb = TextBlock(text="", bbox=BBox.of(b["bbox"]), page=page.number + 1,
                           block_no=b["number"])
            for ln, line in enumerate(b.get("lines", [])):
                spans = line.get("spans", [])
                text = clean("".join(s.get("text", "") for s in spans))
                if not text:
                    continue
                lead = max(spans, key=lambda s: len(s.get("text", "")), default={})
                tl = TextLine(
                    text=text, bbox=BBox.of(line["bbox"]), page=page.number + 1,
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
            Word(text=w[4], bbox=BBox.of(w[:4]), page=page.number + 1,
                 block_no=int(w[5]), line_no=int(w[6]), word_no=int(w[7]))
            for w in page.get_text("words", sort=True)
        ]

    def _annotations(self, page: pymupdf.Page) -> list[Annotation]:
        """PDF annotation objects: SDTM markup lives here as /Contents or appearance text."""
        annots: list[Annotation] = []
        fallback = self._acroform_da(page.parent)
        for a in page.annots():
            info = a.info or {}
            content = clean(info.get("content", ""))
            text = content or self._annot_appearance_text(a)
            style = parse_da(self._annot_da(page.parent, a.xref) or fallback)
            fill, fill_source = self._annot_fill(page.parent, a)
            annots.append(Annotation(
                page=page.number + 1,
                text=text,
                bbox=BBox.of(a.rect),
                subtype=a.type[1] if a.type else "",
                author=clean(info.get("title", "")),
                content=content,
                title=clean(info.get("title", "")),
                color=self._annot_color(a),
                fill_color=fill,
                fill_source=fill_source,
                text_color=style["text_color"],
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
