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
# generate a synthetic test aCRF (4 pages, 15 annotations)
.venv/bin/python tests/sample_pdf.py

# extract text, annotations and layout to JSON
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
| [acrf_parser/models.py](acrf_parser/models.py) | `BBox`, `Word`, `TextLine`, `TextGroup`, `Annotation`, `Rule`, `Control`, `Page`, `Document` |
| [acrf_parser/extract.py](acrf_parser/extract.py) | **Phase 1** — `ACRFParser.parse_pdf()` |
| [acrf_parser/layout.py](acrf_parser/layout.py) | layout pass — regions, columns, wrapped-line grouping, roles |
| [acrf_parser/normalize.py](acrf_parser/normalize.py) | shared text normalization |
| [acrf_parser/cli.py](acrf_parser/cli.py) | `python -m acrf_parser` |
| [tests/sample_pdf.py](tests/sample_pdf.py) | synthetic aCRF fixture generator |

## Phases

- [x] **1 — PDF extraction.** Pages, dimensions, text, blocks, lines, words, annotation objects + coordinates, ruled lines and response controls.
- [x] **Layout pass.** Regions, column bands, wrapped-line grouping, question/response roles.
- [ ] 2 — Form detection (`Form`, continuation pages, `See Page X`)
- [ ] 3 — Field extraction (raw + normalized text)
- [ ] 4 — Annotation extraction
- [ ] 5 — Annotation classification (DOMAIN_HEADER, VARIABLE, CONSTANT_ASSIGNMENT, SUPP_QUALIFIER, NOT_SUBMITTED, CROSS_REFERENCE, DERIVATION_RULE, NOTE)
- [ ] 6 — Field↔annotation linking (`link_score`, row-aware, never pure nearest-neighbour)
- [ ] 7 — Knowledge base (SQLite: forms, fields, annotations, links)
- [ ] 8 — Template creation

## Design notes

**Wrapped text is regrouped before field extraction.** PyMuPDF emits one `TextLine` per
*rendered* line, so one CRF question arrives as six fields:

```
Please record / protocol / version on / which subject / is currently / enrolled:
```

[layout.py](acrf_parser/layout.py) merges them back into a single `TextGroup`, keeping the
constituent lines so every merge is auditable. The merge test is a conjunction — same
region, same column band, no ruled separator crossing, line gap ≤ `0.5 × font size`, same
font size and weight, left/right/centre aligned, no `:`/`?` terminator on the previous line,
and no answer control of its own. Line pitch is the workhorse: wrapped lines sit ~1.15× font
size apart, separately drawn fields ~2×. When MuPDF already grouped lines into one block,
that grouping is trusted and only pitch is re-checked.

**MuPDF block numbers are never the grouping key.** They are unreliable in both directions:
on a right-aligned criteria page, MuPDF splits one criterion across blocks `[2, 3]` (its
block detection keys on left-edge continuity, and ragged-left text has a jumping left edge),
while elsewhere it merges freely. Grouping is geometric, so a block split costs nothing —
`test_grouping_ignores_muPDF_block_splits` pins that behaviour.

**Where pitch cannot help, controls mark the boundaries.** On a criteria page every line is
the same distance apart, within items *and* between them, so pitch says "merge everything".
What separates the items is the checkbox beside each one: a control row-aligned with a line,
in its band and within an inch, means that line *starts* a field. Each control claims only
the topmost line it sits beside, so a tall checkbox level with a whole wrapped label anchors
line 1 rather than splitting the label. Numbering (`1.`, `a)`, `•`) is the second, redundant
signal — the fixture page groups correctly with either one disabled.

**A trailing colon ends a label, unless a list continues.** `...currently enrolled:` closes
a question, but `2. Have any of the following:` runs on into `Hypo/hyper-thyroidism;`. A
following line that ends in `;` or `,` is treated as a list continuation; without that
exception criterion 2 splits into two fragments.

**Columns are detected, not assumed.** `detect_columns` finds the widest whitespace gutter
that most body lines are actually *paired across* — the pairing test is what stops a local
gap between short labels reading as a column boundary. Two bands get the CRF prior
(questions left, responses right); wider grids (log forms) fall back to per-band evidence;
no clean gutter means single column. Every group is then tagged `region`, `column`, `role`
(`QUESTION` / `RESPONSE_OPTION` / `SECTION_HEADER` / `PAGE_HEADER` / `FOOTER`) with a
`role_confidence` and a `role_evidence` list. Nothing is ever filtered out — Phase 6 consumes
`role` as a linking feature, and every call can be explained from its evidence.

Two traps this handles, both real:
- An annotation parked *in the gutter* (`SUPPDS.QVAL when QNAM = "PROTVER"`) would hide the
  column split, so detection runs on `content_lines` only.
- A field label repeating across the pages of one form (`Condition` on both Medical History
  pages) is not a running header, so repeated text is only promoted to `HEADER`/`FOOTER`
  inside the page margin and after 3+ repeats. Phase 2 can strengthen this once form
  boundaries are known.

Thresholds live as named constants at the top of [layout.py](acrf_parser/layout.py)
(`GAP_RATIO`, `GUTTER_MIN_PT`, `CONTROL_NEAR_PT`, …) and are tuned against the synthetic
fixture — recalibrate them against a real study PDF before trusting new studies.


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
