"""Command line entry point: python -m acrf_parser <pdf> [-o outdir]."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .extract import ACRFParser, dump_json, summarize
from .kb import build_kb
from .template import build_template, save_template, summarize_template


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="acrf_parser", description="Parse an annotated CRF PDF")
    ap.add_argument("pdf", type=Path, help="input aCRF PDF")
    ap.add_argument("-o", "--outdir", type=Path, default=Path("output"), help="output directory")
    ap.add_argument("--db", type=Path, help="also write the parse into this SQLite knowledge base")
    ap.add_argument("--no-template", action="store_true", help="skip the Phase 8 template")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    doc = ACRFParser(args.pdf).parse_pdf()
    written = [dump_json(doc, args.outdir / f"{args.pdf.stem}.extract.json")]
    if not args.no_template:
        template = build_template(doc)
        written.append(save_template(template, args.outdir / f"{args.pdf.stem}.template.json"))
    if args.db:
        written.append(build_kb(doc, args.db))

    if not args.quiet:
        print(json.dumps(summarize(doc), indent=2))
        if not args.no_template:
            print(json.dumps(summarize_template(template), indent=2))
        for path in written:
            print(f"-> {path}")
    return 0
