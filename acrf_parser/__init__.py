"""Deterministic aCRF (annotated CRF) parser - no AI, no OCR, no embeddings."""
from . import (annotations, fields, forms, importer, kb, layout, linking,
               prefill, staging, style, template, writer)
from .models import (Annotation, AnnotationPart, BBox, ColumnBand, Control, CrossReference,
                     Document, Field, Form, Link, Page, ResponseOption, Rule,
                     TextBlock, TextGroup, TextLine, Word)
from .extract import ACRFParser, parse_pdf, parse_da, dump_json, summarize
from .kb import KnowledgeBase, build_kb, ingest_approved
from .linking import unlinked_annotations, unlinked_fields
from .importer import ImportReport, ImportedRow, read_staging, write_review_copy
from .prefill import PrefillIndex, prefill_document
from .writer import WriteReport, write_annotations
from .staging import build_staging, write_staging
from .style import (HouseStyle, StyleRule, derive_house_style,
                    derive_house_style_from_kb)
from .template import apply_template, build_template, load_template, save_template
from .normalize import clean, normalize

__version__ = "0.2.0"
__all__ = [
    "ACRFParser", "parse_pdf", "dump_json", "summarize",
    "layout", "forms", "fields", "annotations", "linking", "kb", "style",
    "template", "prefill", "staging", "importer", "writer",
    "Document", "Page", "TextBlock", "TextLine", "TextGroup", "Word", "Annotation",
    "AnnotationPart", "Rule", "Control", "ColumnBand", "BBox", "CrossReference",
    "Form", "Field", "ResponseOption", "Link",
    "KnowledgeBase", "build_kb", "ingest_approved", "unlinked_annotations", "unlinked_fields",
    "build_template", "apply_template", "save_template", "load_template",
    "derive_house_style", "derive_house_style_from_kb", "HouseStyle", "StyleRule",
    "parse_da", "PrefillIndex", "prefill_document", "build_staging", "write_staging",
    "read_staging", "write_review_copy", "ImportReport", "ImportedRow",
    "write_annotations", "WriteReport",
    "clean", "normalize",
]
