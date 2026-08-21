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

There's no real aCRF checked in — `data/sample_acrf.pdf` is a synthetic
5-page, 17-annotation fixture. Regenerate it (or build a second, matching
"blank" study) with:

```bash
# writes data/sample_acrf.pdf (annotated) and prints its path
.venv/bin/python tests/sample_pdf.py
```

Look at [tests/sample_pdf.py](tests/sample_pdf.py) — `build()` makes the
annotated fixture, `build_second_study()` makes a second study used by the
cross-form tests. For a real run, swap in your own aCRF/blank-CRF PDF path
anywhere below.

## 3. Run the test suite

```bash
.venv/bin/python -m pytest -q
```

Tests are one file per module (`tests/test_<module>.py`), all reading the
synthetic fixture — no network, no external services.

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
.venv/bin/python -m acrf_parser data/sample_acrf.pdf -o output --db output/kb.sqlite
```

Runs phases 1–8 (extraction → layout → forms → fields → annotations →
linking → template) and writes:

| Artifact | Phase | What it is |
|---|---|---|
| `output/sample_acrf.extract.json` | 1–6 | full parsed document: pages, forms, fields, annotations, links |
| `output/sample_acrf.template.json` | 8 | coordinate-free template (form/field → variable), reusable across studies |
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
tiers) and **9b** (staging workbook build), reading `house style` (colour /
font / placement conventions) from the corpus too.

| Artifact | Phase | What it is |
|---|---|---|
| `output/blank_acrf.extract.json` | 1–6 | this blank CRF's own parse |
| `output/blank_acrf.template.json` | 8 | (unless `--no-template`) |
| `output/blank_acrf.staging.xlsx` | 9a/9b | **the workbook a reviewer/agent works in** — one row per field, pre-filled where history reaches, `NEEDS_MAPPING` where it doesn't |

Open the staging XLSX: rows are pre-filled with a tier (`EXACT_KEY`,
`CROSS_FORM_CONSENSUS`, `DOMAIN_PATTERN`, `FUZZY_SAME_FORM`, or
`NEEDS_MAPPING`) plus the score/source study for anything fuzzy. Only
`EXACT_KEY` rows arrive pre-approved; everything else needs a human or agent
decision. Geometry lives on a separate, locked `Geometry` sheet — don't hand-edit it.

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
| `output/blank_acrf.annotated.pdf` | 10 | (with `--write-annotations`) the CRF with approved rows drawn on, colour/font/placement from house style, collisions nudged and off-page boxes pulled back |
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
# fresh fixture
.venv/bin/python tests/sample_pdf.py

# 1. ingest the annotated study into a corpus
.venv/bin/python -m acrf_parser data/sample_acrf.pdf -o output --db output/kb.sqlite

# 2. stage a blank CRF (here, reusing the same PDF for demo purposes —
#    normally this is a different, unannotated study)
.venv/bin/python -m acrf_parser data/sample_acrf.pdf -o output \
    --staging --corpus output/kb.sqlite

# 3. hand-edit output/sample_acrf.staging.xlsx, save as reviewed.xlsx, then:
.venv/bin/python -m acrf_parser data/sample_acrf.pdf -o output \
    --import-staging reviewed.xlsx --corpus output/kb.sqlite \
    --write-annotations --learn
```

## 7. Working from Python instead of the CLI

Every CLI flag maps to a library call — useful for scripting or inspecting
intermediate state without going through files:

```python
from acrf_parser import parse_pdf, build_kb, KnowledgeBase, build_template, apply_template

doc = parse_pdf("data/sample_acrf.pdf")     # phases 1-6, in memory
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

Everything under `output/` and `data/sample_acrf.pdf` /
`data/sample_second_study.pdf` is regenerable — safe to delete and re-run
the commands above. Nothing in `acrf_parser/` or `tests/` is generated.
