"""Deterministic aCRF (annotated CRF) parser - no AI, no OCR, no embeddings."""
from . import annotations, fields, forms, kb, layout, linking, template
from .models import (Annotation, AnnotationPart, BBox, ColumnBand, Control, CrossReference,
                     Document, Field, Form, Link, Page, ResponseOption, Rule,
                     TextBlock, TextGroup, TextLine, Word)
from .extract import ACRFParser, parse_pdf, dump_json, summarize
from .kb import KnowledgeBase, build_kb
from .linking import unlinked_annotations, unlinked_fields
from .template import apply_template, build_template, load_template, save_template
from .normalize import clean, normalize

__version__ = "0.2.0"
__all__ = [
    "ACRFParser", "parse_pdf", "dump_json", "summarize",
    "layout", "forms", "fields", "annotations", "linking", "kb", "template",
    "Document", "Page", "TextBlock", "TextLine", "TextGroup", "Word", "Annotation",
    "AnnotationPart", "Rule", "Control", "ColumnBand", "BBox", "CrossReference",
    "Form", "Field", "ResponseOption", "Link",
    "KnowledgeBase", "build_kb", "unlinked_annotations", "unlinked_fields",
    "build_template", "apply_template", "save_template", "load_template",
    "clean", "normalize",
]
