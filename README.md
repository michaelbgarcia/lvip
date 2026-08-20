# aCRF Parser

Deterministic parser for annotated Case Report Forms. Extracts the
`(form, field_text) → annotation` relationship from aCRF PDFs.

**No AI, no LLMs, no embeddings, no OCR.** Regex, layout analysis and
confidence scoring only — every result is explainable.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Use

```bash
# generate a synthetic test aCRF (3 forms, 13 annotations)
.venv/bin/python tests/sample_pdf.py

# Phase 1: extract text + annotations to JSON
.venv/bin/python -m acrf_parser data/sample_acrf.pdf -o output
```

```python
from acrf_parser import parse_pdf

doc = parse_pdf("data/sample_acrf.pdf")
page = doc.page(1)
page.content_text                       # page text WITHOUT annotation markup
[a.text for a in page.annotations]      # ['DM=Demographics', 'BRTHDTC', ...]
```

## Layout

| Path | Purpose |
|---|---|
| [acrf_parser/models.py](acrf_parser/models.py) | `BBox`, `Word`, `TextLine`, `TextBlock`, `Annotation`, `Page`, `Document` |
| [acrf_parser/extract.py](acrf_parser/extract.py) | **Phase 1** — `ACRFParser.parse_pdf()` |
| [acrf_parser/normalize.py](acrf_parser/normalize.py) | shared text normalization |
| [acrf_parser/cli.py](acrf_parser/cli.py) | `python -m acrf_parser` |
| [tests/sample_pdf.py](tests/sample_pdf.py) | synthetic aCRF fixture generator |

## Phases

- [x] **1 — PDF extraction.** Pages, dimensions, text, blocks, lines, words, annotation objects + coordinates.
- [ ] 2 — Form detection (`Form`, continuation pages, `See Page X`)
- [ ] 3 — Field extraction (raw + normalized text)
- [ ] 4 — Annotation extraction
- [ ] 5 — Annotation classification (DOMAIN_HEADER, VARIABLE, CONSTANT_ASSIGNMENT, SUPP_QUALIFIER, NOT_SUBMITTED, CROSS_REFERENCE, DERIVATION_RULE, NOTE)
- [ ] 6 — Field↔annotation linking (`link_score`, row-aware, never pure nearest-neighbour)
- [ ] 7 — Knowledge base (SQLite: forms, fields, annotations, links)
- [ ] 8 — Template creation

## Design notes

**Annotation text is stripped from the page text layer.** PyMuPDF ≥1.24 renders
annotation appearance streams into `page.get_text()`, so `BRTHDTC` appears both
as an `Annotation` and as a `TextLine`. Any text object ≥60% inside an
annotation rect is flagged `from_annotation`; Phase 3 reads `page.content_lines`
so markup is never mistaken for a field label.

**Coordinates.** Absolute points are kept only for linking. Anything persisted
to a template uses `BBox.relative(w, h)` (`rel_x_pct`, `rel_y_pct`) or a
relative label (`right_of_field`), so templates survive layout changes.

**Primary key is `(form_name, field_text)`**, never `field_text` alone — "Start
Date" maps to `MHSTDTC`, `CMSTDTC` or `AESTDTC` depending on the form.

## Tests

```bash
.venv/bin/python -m pytest -q
```
