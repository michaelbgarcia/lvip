# HOWTO — running the aCRF parser

Practical guide: setup, every command you can run, and what file each one
produces. For *how the parser works internally*, see [README.md](README.md)
— this doc is about running the pipeline, not its design.

## 1. Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Everything below assumes commands run from the repo root, using `.venv/bin/python`.

## 2. Get a PDF to work with

`data/` holds the CDISC SDTM Metadata Submission Guidelines example CRF, twice:

| file | what it is |
|---|---|
| `data/blankcrf_annotated.pdf` | the aCRF — 22 pages, 206 SDTM annotations |
| `data/blankcrf.pdf` | the same 22 pages with the markup removed |

That pairing is the point. The two files are the same CRF, so the annotated one
is a complete answer key for the blank one: ingest the annotated CRF as history,
stage the blank one, and every annotation that comes out can be compared with
the one the sponsor actually drew. Swap in your own aCRF/blank pair anywhere
below.

A synthetic fixture is also generated on the fly by the tests, for the cases
this one real CRF has no example of. You can write it out if you want to look at
it:

```bash
# writes data/sample_acrf.pdf (annotated) and prints its path
.venv/bin/python tests/sample_pdf.py
```

`build()` makes the annotated fixture and `build_second_study()` a second study,
used by the cross-form tests.

## 3. Run the test suite

```bash
.venv/bin/python -m pytest -q
```

Tests are one file per module (`tests/test_<module>.py`), plus
`tests/test_msg_fidelity.py`, which scores the whole pipeline against the MSG
pair. No network, no external services.

### Scoring a run

```bash
# the scorecard: recall, precision, placement error, colour/font/size
.venv/bin/python scripts/score_msg.py -v

# the same run, broken down by scope, page and cause
.venv/bin/python scripts/diagnose_msg.py --page 6
```

The number to move is `median_distance_pt`, with recall held up — placing three
annotations perfectly and losing two hundred is not an improvement. Every
threshold in the parser is a named constant at the top of its module, and this
is how you find out what changing one costs.

## 4. The CLI

Everything runs through `python -m acrf_parser`:

```bash
.venv/bin/python -m acrf_parser <pdf> [options]
```

| Flag | Effect |
|---|---|
| `pdf` | input CRF PDF (positional, required) |
| `-o/--outdir` | output directory (default `output/`) |
| `--db PATH` | write this parse into a SQLite knowledge base |
| `--staging` | write the staging XLSX (pair with `--corpus`) |
| `--corpus PATH` | knowledge base used to pre-fill staging + derive house style |
| `--import-staging XLSX` | read a returned workbook back and validate it |
| `--write-annotations` | (with `--import-staging`) draw approved rows onto the PDF |
| `--learn` | (with `--import-staging`) feed approved rows back into `--corpus` |
| `--no-template` | skip building the Phase 8 template |
| `--quiet` | suppress the JSON summaries printed to stdout |

Exit code on `--import-staging` runs is `1` if any row had a validation
error, `0` otherwise — safe to gate a CI/pipeline step on.

## 5. Pipeline walkthrough, with artifacts

There are two workflows: **parsing a study that's already annotated** (build
history), and **staging a blank CRF against that history, then reading the
review back** (the actual working loop). Both go through the same CLI.

### 5a. Parse an annotated study → build history

```bash
.venv/bin/python -m acrf_parser data/blankcrf_annotated.pdf -o output --db output/kb.sqlite
```

Runs phases 1–8 (extraction → layout → forms → fields → annotations →
linking → template) and writes:

| Artifact | Phase | What it is |
|---|---|---|
| `output/blankcrf_annotated.extract.json` | 1–6 | full parsed document: pages, forms, fields, annotations, links |
| `output/blankcrf_annotated.template.json` | 8 | coordinate-free template (form/field → variable), reusable across studies |
| `output/kb.sqlite` | 7 | SQLite knowledge base: forms, fields, annotations, links for this study |

Run it again on a second study with `--db output/kb.sqlite` and the KB
*accumulates* — that's the corpus later steps read from.

Skip `--no-template` if you don't need the template file; skip `--db` if you
just want to inspect one PDF's extraction.

### 5b. Stage a blank CRF against history

```bash
.venv/bin/python -m acrf_parser blank_acrf.pdf -o output --staging --corpus output/kb.sqlite
```

Runs extraction (phases 1–6) plus **9a** (deterministic pre-fill, five scored
tiers) and **9b** (staging workbook build), reading `house style` (text colour,
box fill, font, placement conventions) from the corpus too.

| Artifact | Phase | What it is |
|---|---|---|
| `output/blank_acrf.extract.json` | 1–6 | this blank CRF's own parse |
| `output/blank_acrf.template.json` | 8 | (unless `--no-template`) |
| `output/blank_acrf.staging.xlsx` | 9a/9b | **the workbook a reviewer/agent works in** — one row per *annotation*, pre-filled where history reaches, `NEEDS_MAPPING` where it doesn't |

Open the staging XLSX: rows are pre-filled with a tier (`EXACT_KEY`,
`CROSS_FORM_CONSENSUS`, `DOMAIN_PATTERN`, `FUZZY_SAME_FORM`, or
`NEEDS_MAPPING`) plus the score/source study for anything fuzzy. Only
`EXACT_KEY` rows arrive pre-approved; everything else needs a human or agent
decision. Geometry lives on a separate, locked `Geometry` sheet — don't hand-edit it.
`DomainFills` carries the study's measured fill palette per SDTM domain.

**One row is one annotation.** A field with several annotations has several rows,
sharing a `row_id` and numbered 1, 2, 3… in `annot_seq`. Where history has seen a
field carry a set — `DSTERM`, `DSDECOD=INFORMED CONSENT OBTAINED`, `RFICDTC`,
`DSSTDTC` on one consent date — the whole set is exported. To add one yourself,
copy the row, paste it directly beneath, and put the next free number in
`annot_seq` (leave it blank and the importer numbers it, with a warning). Keep
`row_id` exactly as it is — that is what says which field the annotation belongs
to. Write one statement per row rather than several in one cell: each row keeps
its own type, colour and placement, and they are drawn side by side in
`annot_seq` order. `(row_id, annot_seq)` must be unique, and a field must keep at
least one row.

**Some rows belong to the form, not to a field.** The markup across the top of a
page — the domain header(s) and any form-level constants — belongs to no printed
question, so it gets its own rows: `scope` = `FORM`, `row_id` = `p<page>h`, and
`field_text` reading `[form header] <form name>`. A page usually carries more
than one: two domain headers side by side (`DS=Disposition`, `DM=Demographics`)
plus constants such as `DSCAT = PROTOCOL MILESTONE`. They work exactly like a
field's rows — copy and bump `annot_seq` to add another — and are drawn left to
right across the header band in `annot_seq` order. Unlike a field, a page may
legitimately end up with none; say so by setting the row `REJECTED` with a
reason rather than deleting it, so the corpus learns from the decision.

**Fills follow the domain, not the annotation type.** A study that colour-codes
draws `DSTERM` and `RFICDTC` in different colours even though both are plain
`VARIABLE` markup on the same field. The `DomainFills` sheet is the measured
palette; `fill_basis` on each row says where that row's colour came from. Where
it reads *"corpus varies fill by domain and this statement's domain is
unresolved"*, `fill_rgb` is deliberately blank and needs a decision — DM's own
variables carry no prefix, so nothing in `RFICDTC` says DM. Take the colour from
`DomainFills`; never copy the majority colour, which is a claim about which
domain the statement is.

With no `--corpus`, the workbook still gets written — every row just comes
back `NEEDS_MAPPING`, which is the honest state of a first study.

### 5c. Import the reviewed workbook

After someone (or an agent) fills in the `NEEDS_MAPPING` rows and
approves/rejects suggestions in the XLSX:

```bash
.venv/bin/python -m acrf_parser blank_acrf.pdf -o output \
    --import-staging reviewed.xlsx --corpus output/kb.sqlite \
    --write-annotations --learn
```

| Artifact | Phase | What it is |
|---|---|---|
| `output/reviewed.reviewed.xlsx` | 9c | review copy — every row's validation verdict, `import_issues` column naming what's wrong on bad rows |
| `output/blank_acrf.annotated.pdf` | 10 | (with `--write-annotations`) the CRF with approved rows drawn on as black-bordered boxes, colour/fill/font/placement from house style, collisions nudged and off-page boxes pulled back |
| `output/kb.sqlite` (updated) | 11 | (with `--learn`) approved rows fed back in; rejections stored too, so they're never re-suggested |

Command exits `1` if any row has a validation error — check
`reviewed.reviewed.xlsx`'s `import_issues` column, fix those rows in the
original XLSX, and re-run `--import-staging` (without `--write-annotations
--learn` yet) until it's clean.

`--write-annotations` and `--learn` are independent flags: run
`--import-staging` alone first just to validate a sheet; add
`--write-annotations` when you want the drawn PDF; add `--learn` only when
you're sure you want this batch to become part of the corpus (it's
effectively irreversible — a KB write is not automatically undone).

## 6. One-shot end-to-end example

```bash
# 1. ingest the annotated study into a corpus
.venv/bin/python -m acrf_parser data/blankcrf_annotated.pdf -o output --db output/kb.sqlite

# 2. stage the blank CRF against everything learned so far
.venv/bin/python -m acrf_parser data/blankcrf.pdf -o output \
    --staging --corpus output/kb.sqlite

# 3. hand-edit output/blankcrf.staging.xlsx, save as reviewed.xlsx, then:
.venv/bin/python -m acrf_parser data/blankcrf.pdf -o output \
    --import-staging reviewed.xlsx --corpus output/kb.sqlite \
    --write-annotations --learn
```

Steps 1-3 with the review step done mechanically are exactly what
`scripts/score_msg.py` runs, so if you want to see the finished output without
opening Excel:

```bash
.venv/bin/python scripts/score_msg.py --keep output/msg_run
# output/msg_run/staging.xlsx, approved.xlsx and written.pdf
```

## 7. Working from Python instead of the CLI

Every CLI flag maps to a library call — useful for scripting or inspecting
intermediate state without going through files:

```python
from acrf_parser import parse_pdf, build_kb, KnowledgeBase, build_template, apply_template

doc = parse_pdf("data/blankcrf_annotated.pdf")     # phases 1-6, in memory
build_kb(doc, "output/kb.sqlite")           # phase 7
template = build_template(doc)              # phase 8
```

```python
with KnowledgeBase("output/kb.sqlite") as kb:
    kb.variable_for("Medical History", "Start Date")   # 'MHSTDTC'
    kb.disagreements()                                  # keys whose pages disagree
```

Inspecting a parsed document directly (no files at all):

```python
doc.forms                        # list[Form]
doc.form("Medical History").pages
doc.page(1).fields               # list[Field]
doc.field("p1f0")
doc.links_for("p1f0")            # annotations linked to this field
```

## 8. Where things live (module ↔ phase map)

See the **Layout** table in [README.md](README.md#layout) for the full
module-to-phase mapping (`extract.py` = phase 1, `forms.py` = phase 2, etc.)
and the **Phases** checklist for what each of the 11 phases does.

## 9. Cleaning up generated artifacts

Everything under `output/` and `data/blankcrf_annotated.pdf` /
`data/sample_second_study.pdf` is regenerable — safe to delete and re-run
the commands above. Nothing in `acrf_parser/` or `tests/` is generated.
