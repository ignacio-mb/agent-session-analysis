"""Parse and analyze every session on this machine; report failures and anything unrecognised.

    PYTHONPATH=src python3 scripts/smoke.py [--claude-dir DIR]
"""

import argparse
import json
import sys
import time
import traceback
from collections import Counter

from session_analytics.analyze import analyze
from session_analytics.locate import claude_dir, iter_transcripts
from session_analytics.parse import parse_session
from session_analytics.pricing import Pricing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--claude-dir")
    args = ap.parse_args()
    files = iter_transcripts(claude_dir(args.claude_dir))
    pricing = Pricing()
    failures, unknown, slow = [], Counter(), []
    started = time.time()
    for path in files:
        t = time.time()
        try:
            a = analyze(parse_session(path), pricing)
            json.dumps(a)
        except Exception:
            failures.append((path.name, traceback.format_exc().strip().splitlines()[-1]))
            continue
        for kind, names in a["schema_coverage"]["unknown"].items():
            for name, n in names.items():
                unknown[f"{kind}: {name}"] += n
        if time.time() - t > 3:
            slow.append((path.name, round(time.time() - t, 1)))
    print(f"{len(files) - len(failures)}/{len(files)} sessions analyzed in {time.time() - started:.1f}s")
    for name, err in failures:
        print(f"FAIL {name}: {err}")
    if unknown:
        print("Not recognised by this version:")
        for k, n in unknown.most_common():
            print(f"  {n:>6}  {k}")
    if slow:
        print("Slow:", ", ".join(f"{n} ({s}s)" for n, s in slow))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
