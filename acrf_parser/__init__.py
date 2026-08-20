"""Deterministic aCRF (annotated CRF) parser - no AI, no OCR, no embeddings."""
from . import layout
from .models import (Annotation, BBox, ColumnBand, Control, Document, Page, Rule,
                     TextBlock, TextGroup, TextLine, Word)
from .extract import ACRFParser, parse_pdf, dump_json, summarize
from .normalize import clean, normalize

__version__ = "0.1.0"
__all__ = [
    "ACRFParser", "parse_pdf", "dump_json", "summarize", "layout",
    "Document", "Page", "TextBlock", "TextLine", "TextGroup", "Word", "Annotation",
    "Rule", "Control", "ColumnBand", "BBox",
    "clean", "normalize",
]
