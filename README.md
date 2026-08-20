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
# generate a synthetic test aCRF (5 pages, 17 annotations)
.venv/bin/python tests/sample_pdf.py

# parse: writes <stem>.extract.json + <stem>.template.json, and a knowledge base
.venv/bin/python -m acrf_parser data/sample_acrf.pdf -o output --db output/kb.sqlite
```

```python
from acrf_parser import parse_pdf

doc = parse_pdf("data/sample_acrf.pdf")          # runs every phase

[f.name for f in doc.forms]                      # ['Demographics', 'Medical History', ...]
doc.form("Medical History").pages                # [2, 3] - continuation page folded in

page = doc.page(1)
page.content_text                                # page text WITHOUT annotation markup
[f.text for f in page.fields]                    # ['Date of birth', 'Age', 'Sex', 'Race']

dob = doc.field("p1f0")
[doc.annotation(l.annotation_id).text for l in doc.links_for(dob.id)]   # ['BRTHDTC']
```

The relation, persisted and queried back:

```python
from acrf_parser import build_kb, KnowledgeBase

build_kb(doc, "kb.sqlite")                       # accumulates across studies
with KnowledgeBase("kb.sqlite") as kb:
    kb.variable_for("Medical History", "Start Date")   # 'MHSTDTC'
    kb.variable_for("Adverse Events", "Start Date")    # 'AESTDTC' - never the same row
    kb.disagreements()                                 # keys whose pages disagree
```

And re-applied to the *next* study's unannotated CRF:

```python
from acrf_parser import build_template, apply_template

template = build_template(doc)
for m in apply_template(template, parse_pdf("blank_crf.pdf")):
    m.method, m.confidence, [a["variable"] for a in m.annotations]
    # ('EXACT_KEY', 0.95, ['BRTHDTC'])
```

## Layout

| Path | Purpose |
|---|---|
| [acrf_parser/models.py](acrf_parser/models.py) | `BBox`, `Word`, `TextLine`, `TextGroup`, `Annotation`, `Rule`, `Control`, `Page`, `Document` |
| [acrf_parser/extract.py](acrf_parser/extract.py) | **Phase 1** — `ACRFParser.parse_pdf()` |
| [acrf_parser/layout.py](acrf_parser/layout.py) | layout pass — regions, columns, wrapped-line grouping, roles |
| [acrf_parser/forms.py](acrf_parser/forms.py) | **Phase 2** — form identity, continuation pages, cross references |
| [acrf_parser/fields.py](acrf_parser/fields.py) | **Phase 3** — `Field` records, label cleanup, options, controls |
| [acrf_parser/annotations.py](acrf_parser/annotations.py) | **Phases 4–5** — statement splitting and classification |
| [acrf_parser/linking.py](acrf_parser/linking.py) | **Phase 6** — scored, row-aware field↔annotation links |
| [acrf_parser/kb.py](acrf_parser/kb.py) | **Phase 7** — SQLite knowledge base |
| [acrf_parser/template.py](acrf_parser/template.py) | **Phase 8** — template creation and application |
| [acrf_parser/normalize.py](acrf_parser/normalize.py) | shared text normalization |
| [acrf_parser/cli.py](acrf_parser/cli.py) | `python -m acrf_parser` |
| [tests/sample_pdf.py](tests/sample_pdf.py) | synthetic aCRF fixture generator |

## Phases

- [x] **1 — PDF extraction.** Pages, dimensions, text, blocks, lines, words, annotation objects + coordinates, ruled lines and response controls.
- [x] **Layout pass.** Regions, column bands, wrapped-line grouping, question/response roles.
- [x] **2 — Form detection.** `Form`, continuation pages, `See Page X`.
- [x] **3 — Field extraction.** Raw + normalized text, item numbering, options, controls.
- [x] **4 — Annotation extraction.** Ids, form association, multi-statement splitting.
- [x] **5 — Annotation classification.** DOMAIN_HEADER, VARIABLE, CONSTANT_ASSIGNMENT, SUPP_QUALIFIER, NOT_SUBMITTED, CROSS_REFERENCE, DERIVATION_RULE, NOTE.
- [x] **6 — Field↔annotation linking.** `link_score`, row-aware, never pure nearest-neighbour.
- [x] **7 — Knowledge base.** SQLite: forms, fields, annotations, links.
- [x] **8 — Template creation.** Coordinate-free templates, and applying one to a blank CRF.

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

Every threshold is a named constant at the top of its module — `GAP_RATIO`,
`GUTTER_MIN_PT`, `CONTROL_NEAR_PT` in [layout.py](acrf_parser/layout.py); the
`CONF_*` ladder in [forms.py](acrf_parser/forms.py) and
[annotations.py](acrf_parser/annotations.py); `W_ROW`, `ROW_SLACK_PT`,
`MIN_LINK_SCORE` in [linking.py](acrf_parser/linking.py); `REL_TOL` in
[template.py](acrf_parser/template.py). All of them are tuned against the
synthetic fixture — recalibrate against a real study PDF before trusting new
studies.


**A page's form comes from four signals, in order of how much they can be trusted.**
A printed title (`Form: Medical History`) settles it; failing that, a bold heading
larger than the body; failing that, the domain-header annotation. The fixture's
Disposition page prints no title at all — `DS=Disposition` is the only thing that
names it, which is why markup is a form-detection signal and not only a Phase 5
input. Cross references and inheritance are a *second* pass, because `See Page 7`
on page 5 has to wait for page 7 to be named. `(continued)` is stripped from the
name so a continuation page lands on its form rather than beside it, and a
dangling `See Page 7` in a 5-page PDF stays `resolved=False` — a finding about
the file, not something to discard.

**A field keeps both its printed text and a key-able one.** `raw_text` is what
the page shows; `text` has the item number peeled into `item_number` and the
trailing colon removed, so renumbering a criteria list does not invalidate every
key on the page. Response options are not fields: `Original` / `Amendment 1-4`
are one question's codelist and attach to the question owning their row band —
with their bboxes, since an option can carry markup of its own.

**Answer controls are matched by row, not by radius.** A CRF puts its answer
boxes on a fixed left edge while labels vary in length, so `Age` sits 100pt from
its box and `Date of birth` 66pt from the identical box; any fixed distance takes
one and drops the other. What actually disqualifies a control is *another field's
label standing between it and this one*, plus the column gutter — the Disposition
question must not claim the radio circles that belong to its options.

**One annotation box is not always one statement.** Annotators routinely write
`AESTDTC AEENDTC` in a single box, and linking that as one thing maps two
variables to one field. Only a *pure* variable list splits: anything with prose,
an `=`, brackets or a `when` clause stays whole, so
`SUPPDS.QVAL when QNAM = "PROTVER"` survives intact.

**Classification order is the whole trick.** `SUPPDS.QVAL when QNAM = "PROTVER"`
contains an `=` and reads as a constant assignment unless SUPP is tested first.
The one genuinely ambiguous shape is `XX=YYYY` — `DS=Disposition` is a domain
header, `DS=COMPLETED` is a constant. A human-readable right side settles it; when
both sides are code-shaped, the tie-breakers are the CDISC domain list and page
position (a domain header is the first markup on its page), and the loser is
recorded in the evidence rather than thrown away.

**Linking is row-gated, scored and assigned — three separate defences against
nearest-neighbour.** Nearest-neighbour looks right on a clean page and is wrong
everywhere else: markup for the last field in a column is nearer to the next
section's heading than to its own label. So an annotation must *share a row* with
a field (or sit within `ROW_SLACK_PT` of one) to be a candidate at all; candidates
are then scored on independent features — row overlap, markup to the right,
domain agreement, proximity, response zone — and consumed strongest-first rather
than by argmax. An annotation links once; a field takes at most one annotation
*per type*, because a field legitimately carries a `VARIABLE` and a
`SUPP_QUALIFIER` at once but two bare variables on one label means one belongs to
the field below. Page 1 of the fixture has two annotations with no field beside
them; both come back unlinked, which is the correct answer and the test that pins
it. Losing candidates are kept with `rejected=True`, so a wrong link can be
argued with instead of just re-run.

**The knowledge base stores occurrences, not conclusions.** `Condition` is printed
on both pages of Medical History and annotated on only one. Both are rows; the
`field_map` view folds them into the single answer, and `variants > 1` surfaces
the case where a form's pages disagree instead of resolving it silently at write
time. Keying is `(form_name, field_text)` throughout — the two-study fixture
exists precisely so `Start Date → MHSTDTC` and `Start Date → AESTDTC` can be
shown not to blend.

**Templates hold no coordinates.** Geometry is page-relative (`rel_x_pct`) or a
word (`right_of_field`), and page numbers are offsets from the form's first page,
so Medical History being pages 2-3 here and 11-12 in the next study is not a
difference. `test_nothing_is_stored_in_points` walks the whole document to pin it.
One entry per key, occurrences merged, so the unannotated copy of `Condition`
cannot shadow the annotated one.

**Applying a template states its method rather than guessing.** `EXACT_KEY` is
form *and* field text; `POSITION` is the re-worded-label case, bounded by
`REL_TOL`; `TEXT_ONLY` means the text matched under a *different* form and is a
suggestion for a human, not an answer. The round-trip test strips every
annotation from the fixture and re-applies the template — 13 of 14 fields come
back by exact key. The fourteenth is the honest part: the Disposition page was
named only by `DS=Disposition`, so with the markup gone the page is inherited by
the form above it and its field can only match on text, under the wrong form, at
0.3. The method says so rather than reporting a confident answer.

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
