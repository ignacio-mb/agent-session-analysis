"""Generates the GATR dataset: python3 generate.py [--cache DIR] [--skip-true]

Writes the recorded tables and their load script into the lab's Postgres build context (lab/db/gatr/, lab/db/gatr.sql),
the official data dictionary (lab/gatr/docs/), and the answer key (lab/gatr/answer_key/: corruptions.md, true_world/,
and the facts the stories were planted with). Deterministic: the same code and seed give the same bytes."""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import time
from pathlib import Path

# Python randomises string hashing per process, which changes the order sets of product keys iterate in, and so which
# random draw each batch gets: pin it, or two runs of the same code give different data.
if os.environ.get("PYTHONHASHSEED") != "0":
    os.execve(sys.executable, [sys.executable, *sys.argv], {**os.environ, "PYTHONHASHSEED": "0"})

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from gatr import dictionary, export, stories_doc  # noqa: E402
from gatr.recorded import build_recorded  # noqa: E402
from gatr.world import build_world  # noqa: E402

LAB = HERE.parents[1]
DATA = LAB / "db" / "gatr"
SQL = LAB / "db" / "gatr.sql"
DOCS = LAB / "gatr" / "docs"
KEY = LAB / "gatr" / "answer_key"


def jsonable(x):
    import datetime as dt

    import numpy as np
    if isinstance(x, dict):
        return {str(k): jsonable(v) for k, v in x.items() if not str(k).startswith("_")}
    if isinstance(x, (list, tuple, set)):
        return [jsonable(v) for v in (sorted(x) if isinstance(x, set) else x)]
    if isinstance(x, (dt.date, dt.datetime)):
        return x.isoformat()
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, np.floating):
        return float(x)
    return x


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, help="pickle each stage of the true world here, and reuse it")
    ap.add_argument("--skip-true", action="store_true", help="don't rewrite answer_key/true_world")
    args = ap.parse_args()
    t0 = time.time()
    print("true world")
    w = build_world(args.cache)
    print("recorded layer")
    R = build_recorded(w)
    if args.cache:
        (args.cache / "recorded.pkl").write_bytes(pickle.dumps(R.out))
    print("export")
    stats = export.export_recorded(R.out, DATA, SQL)
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "data_dictionary.md").write_text(dictionary.render_markdown(), encoding="utf-8")
    KEY.mkdir(parents=True, exist_ok=True)
    export.write_corruptions(R.reg, KEY / "corruptions.md")
    stories_doc.render(w, KEY / "hidden_stories.md")
    (KEY / "story_facts.json").write_text(json.dumps(jsonable(w.story), indent=1, ensure_ascii=False) + "\n",
                                          encoding="utf-8")
    (KEY / "table_stats.json").write_text(json.dumps(stats, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    if not args.skip_true:
        export.export_true(w, KEY / "true_world")
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
