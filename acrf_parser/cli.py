"""Command line entry point: python -m acrf_parser <pdf> [-o outdir]."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .extract import ACRFParser, dump_json, summarize


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="acrf_parser", description="Parse an annotated CRF PDF")
    ap.add_argument("pdf", type=Path, help="input aCRF PDF")
    ap.add_argument("-o", "--outdir", type=Path, default=Path("output"), help="output directory")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    doc = ACRFParser(args.pdf).parse_pdf()
    out = dump_json(doc, args.outdir / f"{args.pdf.stem}.extract.json")
    if not args.quiet:
        print(json.dumps(summarize(doc), indent=2))
        print(f"-> {out}")
    return 0
