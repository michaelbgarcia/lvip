"""Command line entry point: python -m acrf_parser <pdf> [-o outdir]."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .anchors import summarize_form_level
from .extract import ACRFParser, dump_json, summarize
from .importer import read_staging, write_review_copy
from .writer import write_annotations
from .kb import KnowledgeBase, build_kb, ingest_approved
from .prefill import PrefillIndex
from .staging import build_staging, summarize_staging, write_staging
from .style import derive_house_style, derive_house_style_from_kb
from .template import build_template, save_template, summarize_template


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="acrf_parser", description="Parse an annotated CRF PDF")
    ap.add_argument("pdf", type=Path, help="input aCRF PDF")
    ap.add_argument("-o", "--outdir", type=Path, default=Path("output"), help="output directory")
    ap.add_argument("--db", type=Path, help="write this parse into a SQLite knowledge base")
    ap.add_argument("--staging", action="store_true",
                    help="write the staging XLSX for review (use with --corpus)")
    ap.add_argument("--corpus", type=Path,
                    help="knowledge base of previously annotated studies, used to "
                         "pre-fill the staging sheet and derive house style")
    ap.add_argument("--import-staging", type=Path, metavar="XLSX",
                    help="validate a returned staging workbook against this PDF "
                         "and write a review copy naming what to fix")
    ap.add_argument("--write-annotations", action="store_true",
                    help="with --import-staging: draw the approved rows onto a "
                         "copy of the PDF")
    ap.add_argument("--learn", action="store_true",
                    help="with --import-staging: feed the approved rows back into "
                         "--corpus so the next study starts from them")
    ap.add_argument("--no-template", action="store_true", help="skip the Phase 8 template")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    doc = ACRFParser(args.pdf).parse_pdf()

    if args.import_staging:
        return _import(args, doc, ap)
    written = [dump_json(doc, args.outdir / f"{args.pdf.stem}.extract.json")]
    template = None
    if not args.no_template:
        template = build_template(doc)
        written.append(save_template(template, args.outdir / f"{args.pdf.stem}.template.json"))
    if args.db:
        written.append(build_kb(doc, args.db))

    staged = None
    if args.staging:
        index, house = _corpus(args.corpus, doc)
        written.append(write_staging(doc, args.outdir / f"{args.pdf.stem}.staging.xlsx",
                                     index=index, house=house))
        staged = summarize_staging(build_staging(doc, index, house))

    if not args.quiet:
        print(json.dumps(summarize(doc), indent=2))
        print(json.dumps(summarize_form_level(doc), indent=2))
        if template:
            print(json.dumps(summarize_template(template), indent=2))
        if staged:
            print(json.dumps(staged, indent=2))
        for path in written:
            print(f"-> {path}")
    return 0


def _import(args, doc, ap) -> int:
    """Read a returned workbook back, and optionally draw it onto the PDF.

    Exit code reflects whether the sheet was clean, so this can gate a pipeline.
    """
    report = read_staging(args.import_staging, doc)
    review = write_review_copy(
        report, args.import_staging,
        args.outdir / f"{args.import_staging.stem}.reviewed.xlsx")
    written = [review]
    wrote = None
    if args.write_annotations:
        wrote = write_annotations(args.pdf, report.rows,
                                  args.outdir / f"{args.pdf.stem}.annotated.pdf")
        written.append(Path(wrote.path))
    if args.learn:
        # Explicit, never implicit. Importing is also how you *check* a sheet
        # before it is final, and a knowledge base that quietly learned from a
        # trial run would be serving those rows back at full confidence with no
        # record of where they came from. Learning is hard to undo; ask for it.
        if not args.corpus:
            ap.error("--learn needs --corpus to say which knowledge base to update")
        written.append(ingest_approved(report, doc, args.corpus,
                                       source=f"{args.pdf.stem} reviewed"))
    if not args.quiet:
        print(json.dumps(report.to_dict(), indent=2))
        for issue in report.errors[:20]:
            print(f"  ERROR {issue.row_id or '-':7} {issue.column}: {issue.message}")
        if wrote:
            print(json.dumps(wrote.to_dict(), indent=2))
        if args.learn:
            print(f"learned {len(report.approved())} approved and "
                  f"{len(report.rejected())} rejected row(s)")
        for path in written:
            print(f"-> {path}")
    return 0 if not report.errors else 1


def _corpus(path: Path | None, doc):
    """Pre-fill index and house style from previously annotated studies.

    With no corpus the workbook is still produced - every row simply arrives as
    NEEDS_MAPPING, which is the true state of a first study.
    """
    if not path:
        return PrefillIndex(), derive_house_style([])
    with KnowledgeBase(path) as kb:
        return PrefillIndex.from_kb(kb), derive_house_style_from_kb(kb)
