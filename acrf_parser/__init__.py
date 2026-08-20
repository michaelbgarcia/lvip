"""Deterministic aCRF (annotated CRF) parser - no AI, no OCR, no embeddings."""
from .models import Annotation, BBox, Document, Page, TextBlock, TextLine, Word
from .extract import ACRFParser, parse_pdf, dump_json, summarize
from .normalize import clean, normalize

__version__ = "0.1.0"
__all__ = [
    "ACRFParser", "parse_pdf", "dump_json", "summarize",
    "Document", "Page", "TextBlock", "TextLine", "Word", "Annotation", "BBox",
    "clean", "normalize",
]
