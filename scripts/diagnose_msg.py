#!/usr/bin/env python3
"""Break the MSG score down by scope, page and cause.

The scorecard says how far off the run is; this says which decision put it
there. Placement error decomposes into three separable causes, and they want
different fixes:

* the annotation was anchored to the wrong thing (a form-level box drawn in the
  page's header band when the sponsor drew it halfway down);
* the anchor was right and the collision search moved it;
* the anchor was right, nothing moved it, and the house style's offset is just
  not where the sponsor puts markup.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import msg
from tests.msg_pipeline import run


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=Path)
    ap.add_argument("--page", type=int, help="dump every pairing on one page")
    args = ap.parse_args()
    tmp = args.keep or Path(tempfile.mkdtemp(prefix="msg-"))
    r = run(tmp)
    s = msg.score(r.reread())

    # Where the writer put each box, keyed the way the scorer keys truth.
    by_key = defaultdict(list)
    for p in r.placements:
        by_key[(p.page, msg.key(p.text))].append(p)

    buckets: dict[str, list[float]] = defaultdict(list)
    moves = Counter()
    for pair in s.matched:
        pool = by_key.get((pair.truth.page, pair.truth.key))
        place = pool[0] if pool else None
        scope = place.scope if place else "?"
        buckets[scope].append(pair.distance)
        if place and place.adjustments:
            buckets[f"{scope}+moved"].append(pair.distance)
            for a in place.adjustments:
                moves[a.split(" to ")[-1].split(";")[0]] += 1
        elif place:
            buckets[f"{scope}+asis"].append(pair.distance)

    print(msg.report(s, top=0).splitlines()[0])
    print(msg.report(s, top=0).splitlines()[2])
    print("\nplacement error by scope (n, median pt, p90 pt):")
    for name in sorted(buckets):
        v = sorted(buckets[name])
        p90 = v[min(len(v) - 1, int(len(v) * 0.9))]
        print(f"  {name:18s} n={len(v):4d}  median={statistics.median(v):7.1f}  p90={p90:7.1f}")

    print("\nwhy boxes moved:")
    for why, n in moves.most_common(10):
        print(f"  {n:4d}  {why}")

    print("\nworst pages (median error over matched):")
    per_page = defaultdict(list)
    for pair in s.matched:
        per_page[pair.truth.page].append(pair.distance)
    for page, v in sorted(per_page.items(), key=lambda kv: -statistics.median(kv[1]))[:8]:
        print(f"  p{page:<3d} n={len(v):3d}  median={statistics.median(v):7.1f}")

    if args.page:
        print(f"\npage {args.page} pairings (truth -> drawn):")
        for pair in s.pairs:
            if pair.truth.page != args.page:
                continue
            if pair.matched:
                print(f"  {pair.truth.text[:40]:42s} "
                      f"truth=({pair.truth.cx:6.1f},{pair.truth.cy:6.1f}) "
                      f"drawn=({pair.drawn_rect[0]:6.1f},{pair.drawn_rect[1]:6.1f}) "
                      f"d={pair.distance:6.1f}")
            else:
                print(f"  {pair.truth.text[:40]:42s} MISSING")
    print(f"\nintermediates: {tmp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
