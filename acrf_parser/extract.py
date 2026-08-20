"""Phase 1 - PDF extraction.

Pulls every textual and annotation object out of an aCRF PDF. No interpretation
happens here: later phases consume the Document this produces.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pymupdf

from . import annotations as annots
from . import fields as flds
from . import forms, layout, linking
from .models import (Annotation, BBox, Control, Document, Page, Rule, TextBlock,
                     TextLine, Word)
from .normalize import clean

_BOLD_FLAG = 1 << 4     # span flags bit for bold
_RULE_MAX_THICK = 2.0   # a filled rect this thin is a ruled line, not a box
_CTRL_MIN_SIDE = 4.0    # smaller than this is decoration, not an input control


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
        out.rules, out.controls = self._graphics(page)
        self._tag_annotation_text(out)
        return out                   # layout pass runs document-wide in parse_pdf

    def _graphics(self, page: pymupdf.Page) -> tuple[list[Rule], list[Control]]:
        """Split vector drawings into separators (rules) and response controls.

        Rules stop line grouping running across section boundaries; controls are
        the evidence that a column holds responses rather than questions.
        """
        rules: list[Rule] = []
        controls: list[Control] = []
        w, h = page.rect.width or 1.0, page.rect.height or 1.0
        for d in page.get_drawings():
            box = BBox.of(d["rect"])
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
        for a in page.annots():
            info = a.info or {}
            content = clean(info.get("content", ""))
            text = content or self._annot_appearance_text(a)
            annots.append(Annotation(
                page=page.number + 1,
                text=text,
                bbox=BBox.of(a.rect),
                subtype=a.type[1] if a.type else "",
                author=clean(info.get("title", "")),
                content=content,
                title=clean(info.get("title", "")),
                color=self._annot_color(a),
                xref=a.xref,
            ))
        return annots

    @staticmethod
    def _annot_appearance_text(a: pymupdf.Annot) -> str:
        """Text drawn inside the annotation's own appearance stream (not page text)."""
        try:
            return clean(a.get_text("text"))
        except Exception:
            return ""

    @staticmethod
    def _annot_color(a: pymupdf.Annot) -> tuple[float, ...] | None:
        colors = a.colors or {}
        c = colors.get("stroke") or colors.get("fill")
        return tuple(round(float(v), 3) for v in c) if c else None


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
