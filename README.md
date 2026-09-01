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
# parse a real aCRF: writes <stem>.extract.json + <stem>.template.json,
# and a knowledge base
.venv/bin/python -m acrf_parser data/blankcrf_annotated.pdf -o output --db output/kb.sqlite

# score the whole pipeline against a real aCRF and its own blank
.venv/bin/python scripts/score_msg.py -v
```

## Calibration

`data/` holds the CDISC SDTM Metadata Submission Guidelines example CRF twice:
`blankcrf_annotated.pdf` with its 206 SDTM annotations and `blankcrf.pdf`
without them. Same 22 pages either way, so the annotated one is a complete
**answer key** for the blank one, and the whole loop can be scored end to end
with no human labelling in it:

```
annotated CRF -> corpus -> stage the blank -> approve -> draw -> compare
```

| | before calibrating | now |
|---|---|---|
| statements reproduced (recall) | 0.379 | **1.000** |
| drawn that the sponsor also drew (precision) | 0.459 | **0.986** |
| median placement error | 174.5 pt | **0.0 pt** |
| within 12 pt of the original | 5% | **94%** |
| within 72 pt | 31% | **99%** |
| text colour / font / size reproduced | 0.99 / 1.00 / 0.89 | **1.00 / 1.00 / 1.00** |

`tests/msg.py` builds the answer key (with raw PyMuPDF, never through this
parser - a key extracted by the code under test only proves the code agrees with
itself), `tests/msg_pipeline.py` runs the loop, `tests/test_msg_fidelity.py`
asserts floors just under the measured numbers, and `scripts/diagnose_msg.py`
breaks a run down by scope, page and cause.

The synthetic fixture in `tests/sample_pdf.py` is still used, for the cases this
one real CRF has no example of: a study that colour-codes by SDTM domain, two
studies that disagree about what "Start Date" means, a page named by nothing but
its domain-header annotation.

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

build_kb(doc, "kb.sqlite")                             # one study
build_kb(parse_pdf("other_study.pdf"), "kb.sqlite")    # a second one accumulates

with KnowledgeBase("kb.sqlite") as kb:
    kb.variable_for("Medical History", "Start Date")   # 'MHSTDTC'
    kb.variable_for("Adverse Events", "Start Date")    # 'AESTDTC' - never the same row
    kb.variable_for("Demographics", "Start Date")      # None - the form is half the key
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
| [acrf_parser/models.py](acrf_parser/models.py) | `BBox`, `Word`, `TextLine`, `TextGroup`, `Annotation`, `Rule`, `Control`, `Field`, `FormAnchor`, `Page`, `Document` |
| [acrf_parser/extract.py](acrf_parser/extract.py) | **Phase 1** — `ACRFParser.parse_pdf()` |
| [acrf_parser/layout.py](acrf_parser/layout.py) | layout pass — regions, columns, wrapped-line grouping, roles |
| [acrf_parser/forms.py](acrf_parser/forms.py) | **Phase 2** — form identity, continuation pages, cross references |
| [acrf_parser/fields.py](acrf_parser/fields.py) | **Phase 3** — `Field` records, label cleanup, options, controls |
| [acrf_parser/annotations.py](acrf_parser/annotations.py) | **Phases 4–5** — statement splitting and classification |
| [acrf_parser/linking.py](acrf_parser/linking.py) | **Phase 6** — scored, row-aware field↔annotation links |
| [acrf_parser/anchors.py](acrf_parser/anchors.py) | **Phase 6b** — form-level markup, and the per-page anchor it is written back to |
| [acrf_parser/kb.py](acrf_parser/kb.py) | **Phase 7** — SQLite knowledge base |
| [acrf_parser/template.py](acrf_parser/template.py) | **Phase 8** — template creation and application |
| [acrf_parser/style.py](acrf_parser/style.py) | house style — text colour, font and placement per annotation type, box fill per SDTM domain, measured from a corpus |
| [acrf_parser/prefill.py](acrf_parser/prefill.py) | deterministic pre-fill — five scored tiers, offline, no model |
| [acrf_parser/staging.py](acrf_parser/staging.py) | **Phase 9a/b** — the staging workbook |
| [acrf_parser/importer.py](acrf_parser/importer.py) | **Phase 9c** — reading the workbook back, validated per row |
| [acrf_parser/writer.py](acrf_parser/writer.py) | **Phase 10** — drawing approved annotations onto the PDF |
| [acrf_parser/normalize.py](acrf_parser/normalize.py) | shared text normalization |
| [acrf_parser/cli.py](acrf_parser/cli.py) | `python -m acrf_parser` |
| [tests/sample_pdf.py](tests/sample_pdf.py) | synthetic aCRF fixture generator |
| [tests/msg.py](tests/msg.py) | the MSG answer key, and how a run is scored against it |
| [tests/msg_pipeline.py](tests/msg_pipeline.py) | one end-to-end pass over the MSG pair |
| [scripts/score_msg.py](scripts/score_msg.py) | the scorecard |
| [scripts/diagnose_msg.py](scripts/diagnose_msg.py) | the same run, broken down by scope, page and cause |

## Phases

- [x] **1 — PDF extraction.** Pages, dimensions, text, blocks, lines, words, annotation objects + coordinates, ruled lines and response controls.
- [x] **Layout pass.** Regions, column bands, wrapped-line grouping, question/response roles.
- [x] **2 — Form detection.** `Form`, continuation pages, `See Page X`.
- [x] **3 — Field extraction.** Raw + normalized text, item numbering, options, controls.
- [x] **4 — Annotation extraction.** Ids, form association, multi-statement splitting.
- [x] **5 — Annotation classification.** DOMAIN_HEADER, VARIABLE, CONSTANT_ASSIGNMENT, CONDITIONAL_VARIABLE, SUPP_QUALIFIER, NOT_SUBMITTED, CROSS_REFERENCE, DERIVATION_RULE, NOTE.
- [x] **6 — Field↔annotation linking.** `link_score`, row-aware, never pure nearest-neighbour.
- [x] **6b — Form-level markup.** Domain headers and form-level constants, which belong to no field: recognised by type and by sitting above the page's first field, and given a per-page anchor so they can be staged, reviewed, drawn and learned like anything else.
- [x] **7 — Knowledge base.** SQLite: forms, fields, annotations, links.
- [x] **8 — Template creation.** Coordinate-free templates, and applying one to a blank CRF.
- [x] **House style.** `/DA` appearance capture, and colour/font/placement conventions derived from a corpus. Fill is measured per SDTM domain as well as per type — a study that draws `DSTERM` yellow and `RFICDTC` blue is colour-coding by domain, which type alone cannot express — and is left blank and flagged rather than defaulted where a statement's domain cannot be read off its text.
- [x] **9a — Deterministic pre-fill.** Five scored tiers against the corpus, offline.
- [x] **9b — Staging XLSX export.** Work sheet (one row per annotation, field-level and form-level), locked geometry, house style, domain fill palette, instructions.
- [x] **9c — Staging XLSX import.** Per-row validation, review copy written back.
- [x] **10 — PDF annotation writing.** Approved rows drawn, clamped, de-collided, re-readable.
- [x] **11 — The return edge.** Approved rows fed back, with trust and rejections.

## The blank-CRF workflow

```bash
# 1. ingest history once (per annotated study)
.venv/bin/python -m acrf_parser study_2023.pdf --db corpus.sqlite --quiet

# 2. stage a new blank CRF against everything learned so far
.venv/bin/python -m acrf_parser blank_crf.pdf -o output --staging --corpus corpus.sqlite

# 3. read the reviewed workbook back, draw the approved rows onto the PDF, and
#    feed them into the corpus (exit 1 if any row is unwritable, so it can gate
#    a pipeline). --learn is opt-in: see "The return edge" below.
.venv/bin/python -m acrf_parser blank_crf.pdf -o output \
    --import-staging reviewed.xlsx --corpus corpus.sqlite \
    --write-annotations --learn
```

The knowledge base is read at step 2 and written at step 3. It does three jobs
on the way out — the mapping pre-fill, the house style that fills the formatting
columns, and resolving each form's SDTM domain — and on the way back it learns
what the reviewer decided.

Step 2 writes a workbook with one row per CRF field, pre-filled from history,
each row carrying the tier that filled it and why. What history could not reach
is marked `NEEDS_MAPPING` — that, and only that, is the agent's work.

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
`CONF_*` ladder and the title-scoring weights in
[forms.py](acrf_parser/forms.py); `W_ROW`, `ROW_SLACK_PT`, `MIN_LINK_SCORE` in
[linking.py](acrf_parser/linking.py); `MAX_LEARNED_OFFSET_*` in
[staging.py](acrf_parser/staging.py); `MAX_MOVE_PT` in
[writer.py](acrf_parser/writer.py); `REL_TOL` in
[template.py](acrf_parser/template.py). They are calibrated against the MSG
pair — see **Calibration** above — and `scripts/score_msg.py` is how you find
out what moving one costs.

**A page can be turned, and everything below Phase 1 is spared knowing it.**
Three of the MSG CRF's 22 pages carry `/Rotate 90`. PyMuPDF reports two
coordinate systems for such a page: `page.rect` is the landscape view a reader
sees, while `get_text`, `annot.rect` and `get_drawings` all answer in the
*unrotated* portrait space, where the rows run down the x axis. Every geometric
test in this parser is about reading order — "same row" is vertical overlap,
columns are cut on vertical whitespace, a wrapped line is the one below its
predecessor — so on those pages each of them was asking about the wrong axis,
which is why they yielded no fields at all. `extract.display_matrix` rotates
once, at extraction; `writer` inverts it on the way out because
`add_freetext_annot` wants page space back, and sets the annotation's own
`/Rotate` so the glyphs are laid out in reading order rather than sideways.

**A form title is scored, not taken from the top of the page.** It used to be
the topmost bold heading, and on a real CRF that is almost never the form: every
page of the MSG example opens with a bordered identification band — the study
("CDISC Study CDISC01"), the visit, the assessment date — printed bold above and
to the left of the form's own name. Taking the topmost heading named all 22
pages after the study and collapsed the whole CRF into three forms, which
destroys the `(form_name, field_text)` key everything downstream is built on.
What actually separates "DEMOGRAPHY" from "CDISC Study CDISC01" is not height on
the page but that a title is *centred*, *short*, *not printed on every other
page*, and alone on its line — a table's header row is bold, short and centred
too, one cell at a time. None is decisive alone, so `forms._title_score` scores
them together and keeps the reasons as evidence.


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

**`XXORRES when XXTESTCD = CODE` is a type, not prose.** Every SDTM findings
domain is annotated this way, because a findings observation is not identified
by its variable — `QSORRES` is *every* answer on a questionnaire — but by the
variable plus the test code that says which question it is. On the MSG CRF that
shape is seventy of two hundred and six annotations, a third of the file, and
all seventy read as unclassified `NOTE`: no variable parsed, no domain resolved,
so no fill colour, no domain check, and the least structured label in the
taxonomy shown to the reviewer for the most structured thing on the page.
`CONDITIONAL_VARIABLE` parses the variables, the condition variable and its
values, and is checked after SUPP — `RACEOTH when SUPPDM.QNAM = RACEOTH` has the
same shape and is the more specific case. It tolerates what annotators actually
type: `QSORRESwhen QSTESTCD = CSDD19` and `QSORRES when QSTESTCD =CSDD04` are
both in this file, and a parser that only reads tidy input reports a typo as
prose.

**Markup that reaches no field still gets a row.** It used to be dropped unless
it sat above the page's first field, on the reasoning that anything unlinked
among the fields "stays a finding". It did not stay a finding — it stayed
nowhere: not in the workbook, not in the knowledge base, not on the output PDF,
and not in front of the reviewer who was supposed to find it. On the MSG CRF
that silently lost 35 of 206 annotations. It belongs to the form now, for the
same reason the form-level layer exists at all, and *where* it sat is kept in
the evidence instead: markup above the first field is the page's header band,
and markup among them is markup the linker could not place. The second is worth
a second look; neither is worth discarding.

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
than by argmax. An annotation links once; a field takes up to `MAX_PER_TYPE`
annotations of a type, and every one after the first has to genuinely *share the
field's row*. The bound used to be one-per-type, on the reasoning that "two bare
variables on one label means one belongs to the field below" — but a consent date
annotated `DSTERM`, `DSDECOD=INFORMED CONSENT OBTAINED`, `RFICDTC` and `DSSTDTC`
is four statements on one label, three of them plain variables, and a cap of one
does not merely drop the extras: it teaches the corpus the field has one
annotation, so the next study is pre-filled with a quarter of the markup it
needs. What separates the two cases is the row, not the count — the neighbouring
field's markup got in through `ROW_SLACK_PT` and has no vertical overlap at all,
so the first annotation of a type may claim the slack and the rest may not. Page
1 of the fixture has two annotations with no field beside
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

**Pre-fill is deterministic and runs before any model does.** Five tiers, all
scored, all explainable, all offline —
[prefill.py](acrf_parser/prefill.py) needs nothing but stdlib and SQLite:

| tier | fires when |
|---|---|
| `EXACT_KEY` | `(form, field_text)` seen before — the answer, not a guess |
| `CROSS_FORM_CONSENSUS` | the label maps to one variable in *every* form that has used it |
| `DOMAIN_PATTERN` | SDTM's naming convention, learned from the corpus |
| `FUZZY_SAME_FORM` | same form, near-identical wording |
| `NEEDS_MAPPING` | nothing in history reaches it — the agent's real job |

`DOMAIN_PATTERN` is the one that generalises. Seeing `MHSTDTC` on a Medical
History form teaches that the label "start date" plays the role `STDTC`; seeing
`AESTDTC` confirms it across a second domain. A Concomitant Medications form is
then pre-filled `CMSTDTC` although CM appears nowhere in the corpus. The
convention belongs to SDTM; the corpus is only evidence of which labels map to
which role, and a suffix attested by a single domain is treated as coincidence.

`CROSS_FORM_CONSENSUS` is where the corpus rates its own reliability. "Sex" maps
to `SEX` on every form that has one, so the label alone is sufficient. "Start
Date" maps to `MHSTDTC`, `AESTDTC` and `CMSTDTC`, so the label alone is
*insufficient* — and that is discovered from the disagreement rather than
declared in advance. Where forms disagree, no cross-form suggestion is offered
and the reason is recorded.

**Only `EXACT_KEY` can auto-approve.** Every fuzzy tier lands as `NEEDS_REVIEW`
whatever it scored, carrying the source study, the source label and the score,
so a reviewer sees *"matched 'Start Date' from STUDY-XYZ Medical History at
0.86"* and not a bare variable name wearing the same clothes as a fact. A
suggestion that is indistinguishable from a certainty is the failure mode that
would make the whole pipeline untrustworthy.

The similarity metric blends sequence ratio, containment and Jaccard over lightly
stemmed tokens, because each alone fails a re-wording that really happens:
"Conditions"/"Condition" scores 0.0 on raw token sets, and "Start Date" inside
"Start Date of Condition" is punished by Jaccard for being short. The pair that
matters is `"start date"` vs `"stop date"` — semantically opposite, and at 0.55
it sits just under the 0.6 floor. That margin is thin, and
`test_opposites_stay_below_the_fuzzy_floor` exists to fail loudly if the floor
is ever raised without re-checking it.

**A row is one annotation, not one field.** CRF fields routinely carry several
statements, so the workbook's key is `(row_id, annot_seq)` — which field, and
which of its annotations. Pre-fill exports the whole set an exact key was seen
with; a reviewer adds the rest by copying a row and giving it the next free seq,
which is the one way the sheet lets rows be added. One statement per row is what
lets each annotation keep its own type, colour and placement, what lets the
importer name the one that is wrong, and what lets the writer draw them in the
reviewer's own order. Only `EXACT_KEY` contributes a set: a fuzzy tier is a guess
about which variable a label means, and multiplying a guess by four multiplies
the reviewer's work without adding evidence.

**The staging workbook is shaped by who reads it.** The agent gets one flat table
with self-describing column names — no merged cells, no nesting. The human gets
the match tier, score and source study *beside* the suggestion rather than in an
audit log. The importer gets geometry, which is noise to the other two readers
and must never be hand-edited, so it lives on a locked `Geometry` sheet keyed by
`(row_id, annot_seq)` — and because that sheet is locked, a sibling row a
reviewer added inherits its field's geometry rather than demanding they
hand-write page fractions. Sheet protection is deliberately not switched on: it would stop Copilot
writing at all. The locked flags record intent, and the importer is what enforces
it — validation on the way back in, not a UI lock on the way out.

**Validation is per row, not per workbook.** One unparseable colour in row 200
must not reject the other 199 — the reviewer would fix it, re-import, and meet
the next problem one round trip later. Every row carries its own verdict, the
good ones import, and the bad ones come back naming their own problem so they
can all be fixed in one pass. `write_review_copy` puts each message in an
`import_issues` column *on the row that needs fixing*, because the reviewer is
already in Excel and should not have to cross-reference a log by hand.

Severity is split for the same reason. `ERROR` blocks the row — without a
resolvable colour or geometry there is nothing to draw. `WARNING` lets it
through flagged, which is the only sane treatment for a signal that is right
most of the time.

`DOMAIN_MISMATCH` is the example worth keeping. The obvious test — "the variable
does not start with the form's domain" — is wrong: `DM`'s own variables carry no
prefix, so `BRTHDTC`, `AGE` and `RACE` each raise a warning on the one form they
belong to. Three false alarms in four teaches a reviewer to ignore the check,
which costs more than the check is worth. What actually indicates a mistake is a
variable wearing *another* domain's code with a real suffix behind it —
`DSTERM` on a Medical History form. The suffix-length test is what keeps `AGE`
("AG" + "E") and `SEX` ("SE" + "X") out, since both begin with letters that
happen to be CDISC domain codes.

Three silent failures the importer exists to catch:

- **A sorted or deleted row.** `row_id` is the only thing tying a spreadsheet row
  to a position on the PDF. Rows are matched by it and never by position, so
  reversing the sheet changes nothing. A field may *lose* an annotation — that is
  how one is removed — but a field with no rows left at all is an error, as is a
  duplicated `(row_id, annot_seq)`, which is two rows nothing can tell apart. A
  blank `annot_seq` on an added row is numbered automatically and flagged, since
  "the next free one" has exactly one answer but choosing silently for the
  reviewer does not.
- **An edited label.** If `field_text` no longer matches the field its `row_id`
  points at, the row has stopped describing what it claims to.
- **An unevaluated formula.** An agent that writes `=CONCAT(...)` into a file
  nobody opens in Excel leaves no cached value, and the cell reads as blank —
  which would otherwise import as "no decision" rather than as a problem.

The declared annotation type is checked against the text by running
[annotations.classify](acrf_parser/annotations.py) over it, so the workbook
cannot claim a type its own contents contradict — the importer validates with the
same rules the parser reads with.

**The return edge is ingested from the workbook, not from the PDF we just wrote.**
Re-parsing the output is the obvious way to close the loop and it quietly
corrupts the corpus. A blank CRF has no domain-header annotations, so a page
with no printed title is inherited by the form above it: the Disposition page
reads as Medical History, and re-parsing relearns that mistake and files
`DSTERM` under Medical History at 0.95 confidence — a machine error laundered
into ground truth. The sheet has a `form_name` column, so that column is what is
believed.

Which meant `form_name` had to become editable. It was locked as evidence, and
the one column that most needs human correction on a blank CRF was the one the
reviewer could not touch. A change to it is recorded as `FORM_RENAMED` rather
than accepted silently, because it changes the primary key.

**Links carry how they were established.** `HUMAN_APPROVED` outranks `GEOMETRIC`,
and `PrefillIndex` breaks ties on trust before score, so a reviewer's decision
cannot lose to a lucky geometric match. Occurrences are still all kept — after
the return edge the Disposition question has two, `DSTERM` from the reviewer and
`SUPPDS.QVAL` from the original study — and the approved one simply ranks first.

**Rejections are evidence too.** A reviewer who turns down `MHENRF=ONGOING` has
told you something the corpus had no other way to learn, and re-proposing it
next study burns their trust in every other row on the sheet. Rejected rows are
stored as `HUMAN_REJECTED` with the suggestion that was refused, and pre-fill
zeroes any candidate matching one, saying so in its evidence.

**Learning is opt-in.** `--learn` is a flag, not a side effect of importing,
because importing is also how you *check* a sheet before it is final. A
knowledge base that quietly learned from a trial run would serve those rows back
at full confidence with no record of where they came from, and KB writes are
hard to undo.

One honest limit: a form-name correction is per document. The knowledge is filed
under the corrected name, but re-staging *the same* blank CRF still parses that
page as Medical History, so the key does not match until a document identifies
the form correctly. `test_approved_work_raises_the_next_run` asserts 5 → 2
rather than 5 → 0 for exactly this reason.

**The pipeline is checked by reading its own output.** `write_annotations` draws
the approved rows onto a copy of the blank CRF; the test then parses that copy
with the same parser — form detection, field extraction, classification and
linking all running fresh — and requires every mapping the sheet specified to
come back attached to the field it was specified for. Nine of nine round trip on
the fixture, with colour, font and size intact. That is a stronger claim than any
assertion about the writer's internals: it says the thing we produced is the
thing we can read.

It also pins something easy to miss — annotations *we* wrote must be stripped
from the text layer on re-parse, or the next study's field extraction would read
our own markup back as CRF labels. `test_written_annotations_are_stripped_from_the_text_layer`
exists for that.

**Placement comes from the most specific evidence there is.** Three answers, in
order:

1. **The offset history recorded from this field.** Portable — it survives the
   form being re-flowed — and it is a claim about this exact statement.
2. **The page position history recorded**, where that offset is missing or
   implausible. Less portable, but still where somebody put this statement
   rather than an average of where they put statements like it. It is the *only*
   evidence for form-level markup, which has no field to be offset from at all.
3. **The house style**, the corpus median for annotations of this type — the
   right answer for a statement nobody has seen, and a poor one for a statement
   that was seen, on this form, on this field, in a spot someone chose.

`anchor_source` on the Geometry sheet records which answered. Getting this order
right is most of the difference between a median placement error of 174 points
and one of zero: the house style alone put every page's domain headers in a band
at the top left whatever the study actually did, which on the MSG Demography
page is most of the page's height away from where two of the three belong.

The recorded **width** matters as much as the position. Annotators set long
markup in a narrow column beside the field, not across the page — "Reason for
Discontinuation / 0 Ongoing / 1 Adverse Event / …" is six lines in 189 points —
and drawn as one line that is over 600 points wide, will not fit anywhere, and
the search abandons the position and clamps the box to the margin. So the box is
wrapped to the width history recorded, and its height comes from the wrap rather
than from history, so a reviewer who rewrites the text gets a taller box instead
of a clipped one.

A learned position also suspends the obstacle search. The form's own words are
obstacles to a *computed* position, which is a guess about where there is room;
a position a previous study actually drew and shipped is known to work on that
page, and moving it to satisfy a word-overlap test costs fidelity to buy
nothing. Other annotations still block, and the search that resolves that is
bounded by `MAX_MOVE_PT` — an annotation nudged an inch is one a reviewer can
still see belongs to its field, and one carried across the page has quietly
become a claim about a different part of the form.

**Appearance is measured per statement where it was measured at all.** Same
order, same reason: the house style's per-type mode is right for a statement
nobody has seen, and the MSG corpus sets domain headers at 18pt, most variables
at 12pt and a run of questionnaire markup at 10pt. Since the box is measured
from the text at that size, a wrong size is a wrong width and so a wrong
position. Fill keeps the domain axis for unseen statements — that is what
colour-coding by domain is for — but a statement the corpus drew wins for
itself: 188 of the MSG CRF's 206 boxes are cyan, so the domain mode reports cyan
for DS markup the sponsor drew yellow, on the very page the distinction exists
to capture.

**The appearance stream outranks `/DA`.** A FreeText annotation is supposed to
declare its colour and font in `/DA`, and on a real aCRF it frequently lies:
every annotation on the MSG CRF carries `0 0 0 rg /Arial,BoldItalic 12 Tf`, and
every variable on it is drawn in red, because the appearance stream sets
`1 0 0 rg` after `/DA` has had its say. The appearance stream is what a reader
paints, so that is what is read — otherwise the corpus learns a house style of
black text from a study that is not black, and the next aCRF comes out in a
colour nobody used. The same precedence already governed the background fill.

**Two things arithmetic cannot decide alone:**

- **A box that runs off the page.** Long markup placed right of a label near the
  right margin will not fit. It is pulled back inside the page and the row
  records the move, because an annotation half off the page is worse than one a
  reviewer was told about.
- **Two annotations in the same place.** Fields a fraction of a row apart collide.
  Rows are drawn top-to-bottom so a nudge always pushes into unclaimed space, the
  later one moves down, and it says so. Silently overprinted text is the failure
  a reviewer would only find by eye, on page 40.
- **A field's own set.** Several annotations of one field share one box, so they
  cannot each be anchored from the field or the first would take the spot and
  collision search would scatter the rest — reading order lost, and the second
  statement as likely to land two rows down as beside the first. Each is anchored
  onto the end of the previous one in `annot_seq` order instead, so a set reads
  left to right along the row it belongs to and spills onto a neighbouring row
  only when this one genuinely runs out. Two annotations of the *same* field are
  never de-duplicated against each other: they share a box, so the geometric test
  cannot separate them, and a repeated statement is the importer's
  `DUPLICATE_STATEMENT` warning for a person to settle.

Only `ImportedRow.ready` rows are drawn — validated *and* signed off, never one
without the other. A font the PDF cannot embed is substituted for a base-14 face
and the substitution is recorded rather than passed off as the real thing — in
`Placement.notes`, not `Placement.adjustments`, because it is not a placement
problem and there is nothing on the page for a reviewer to check. A corpus set
in a house font substitutes on every row, and reporting two hundred "adjusted"
annotations when thirty actually moved is the same as reporting none.

The face is then written into the appearance directly. PyMuPDF takes a
`fontname` for an annotation and writes `/Helv` regardless, so a corpus set in
bold italic came out in plain roman on every row — the first thing a reviewer
notices about a page, and the difference between output that looks like the
sponsor's work and output that looks like a draft.

**Formatting conventions are measured, not inferred.** A FreeText annotation's
colour and font are not in `/C` or `/IC` — `annot.colors` comes back empty for
them. They live in the `/DA` default-appearance string
(`0.85 0.1 0.1 rg /Helv 8.0 Tf`), which
[extract.parse_da](acrf_parser/extract.py) parses into RGB, font and size,
normalising grayscale and CMYK so two spellings of one colour do not read as
disagreement. [style.py](acrf_parser/style.py) then counts those over a corpus
to produce a `StyleRule` per annotation type.

Every rule carries `samples` and `agreement` — the share that matched the modal
value — because the useful output is not an average but a verdict on whether the
corpus agrees. The fixture renders domain headers at 8pt on three pages and 9pt
on two: agreement `0.50`, `settled=False`, escalated to a human. Averaging would
have produced 8.5pt, a size nobody chose and which no reviewer would recognise
as wrong. Placement is measured only where an annotation is linked to a field;
unlinked markup still contributes its colour and font, just not its position.

This exists so that a downstream agent is never asked to *calculate* placement
or colour. Those are facts about finished work, recoverable by counting, and an
LLM asked for them returns plausible coordinates that are subtly wrong — the one
class of error a human reviewing a spreadsheet cannot catch.

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
