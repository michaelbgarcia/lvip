#!/usr/bin/env python3
"""Score one pipeline run against the MSG answer key.

    python3 scripts/score_msg.py            # the scorecard
    python3 scripts/score_msg.py -v         # plus per-page detail
    python3 scripts/score_msg.py --keep out # keep the workbook and PDF

The number to move is `median_distance_pt` with recall held up: placing three
annotations perfectly and losing two hundred is not an improvement.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import msg
from tests.msg_pipeline import run


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--keep", type=Path, help="write intermediates here instead of a temp dir")
    args = ap.parse_args()

    tmp = args.keep or Path(tempfile.mkdtemp(prefix="msg-"))
    r = run(tmp)
    truth = msg.ground_truth()
    # Scored off the written PDF rather than off the write report: the report
    # says where the writer meant to put each box, the PDF says what a reader
    # finds - and only the PDF carries the rendered colour, font and size.
    s = msg.score(r.reread(), truth)

    if args.json:
        print(json.dumps(s.to_dict(), indent=1))
        return 0

    print(msg.report(s, top=25 if args.verbose else 8))
    print()
    print(f"parse: {len(list(r.annotated.iter_annotations()))} annotations, "
          f"{len(list(r.annotated.iter_fields()))} fields, "
          f"{len(r.annotated.forms)} forms in the annotated CRF")
    print(f"blank: {len(list(r.blank.iter_fields()))} fields, "
          f"{len(r.blank.forms)} forms")
    print(f"staging: {len(r.imported.rows)} rows, "
          f"{len(r.imported.approved())} approved, "
          f"{len(r.written.placements)} drawn, "
          f"{len(r.written.adjusted)} moved off their anchor")

    if args.verbose:
        per_page = defaultdict(lambda: [0, 0])
        for p in s.pairs:
            per_page[p.truth.page][1] += 1
            per_page[p.truth.page][0] += bool(p.matched)
        print("\nper page (matched/truth):")
        for page in sorted(per_page):
            hit, total = per_page[page]
            bar = "#" * hit + "." * (total - hit)
            print(f"  p{page:<3d} {hit:3d}/{total:<3d} {bar}")
    print(f"\nintermediates: {tmp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
