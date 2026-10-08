"""Builds the whole true world, stage by stage. With a cache directory, each stage's result is pickled so a change in a
late stage doesn't re-run the early ones (delete the cache after changing an early stage)."""

from __future__ import annotations

import pickle
import time
from pathlib import Path

from . import demand, facilities, inventory, ops, p3, quality, supply
from .master import World, build_master

STAGES = [
    ("master", lambda w: build_master(w)),
    ("demand", lambda w: (demand.build_prices(w), demand.build_p3_customers(w), demand.build_demand(w))),
    ("ops", lambda w: ops.run_ops(w)),
    ("supply", lambda w: supply.run_supply(w)),
    ("quality", lambda w: quality.run_quality(w)),
    ("facilities", lambda w: facilities.run_facilities(w)),
    ("inventory", lambda w: inventory.run_inventory(w)),
    ("p3", lambda w: p3.run_p3(w)),
]


def build_world(cache: Path | None = None, log=print) -> World:
    w = World()
    names = [s for s, _ in STAGES]
    start = 0
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
        for i in range(len(STAGES) - 1, -1, -1):
            f = cache / f"{names[i]}.pkl"
            if f.exists():
                w = pickle.loads(f.read_bytes())
                start = i + 1
                log(f"  cached: {names[i]}")
                break
    for name, fn in STAGES[start:]:
        t = time.time()
        fn(w)
        log(f"  {name}: {time.time() - t:.0f}s")
        if cache:
            (cache / f"{name}.pkl").write_bytes(pickle.dumps(w))
    return w
