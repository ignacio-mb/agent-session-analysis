"""Which of the standard datasets an rde test session was run on, and each skill run in it: Stripe, Airline Flight
Delays, Toy Store, DBA Stack Exchange, Contrast, Metabase's Sample Database — or none.

The rules live in semantics/datasets.json: per dataset, regular expressions for its name and its distinctive schemas,
tables, columns and transform prefixes (`match`), and for the mb profiles and instance hosts named after it
(`instances`). fill() works from the warehouse rows alone, so a session gets the same dataset when it is built, when
a share file of it is imported, and when its rows are read back from ClickHouse to be labelled again.

A run is placed by the first of these that settles it:
  prompt       the prompt it started from and the arguments it was invoked with name exactly one dataset
  snapshot     what it built in its Metabase (names, descriptions, SQL, target tables) names exactly one
  tool calls   one dataset is named by at least `min_calls` of its tool calls, and `margin` times as many as the
               runner-up (a run that probes several lab instances names several)
  instance     its mb profile or instance host is named after exactly one (toy-store2, dba), and neither its prompt
               nor its session's title names another: on a lab instance that holds several, the name is only a hint
  session      else, its session's
A session: its runs' own datasets, when they agree; else its title (Claude Code's summary of it, or its first
prompt); else the snapshots and instances of its runs, and all the session's tool calls (in a skill run or not), taken
together; its runs' datasets by majority; else none. A baseline session (a direct agent given an rde run's prompt) is
one run, `<id>:0`, whose prompt is the session's first and whose tool calls are those no skill run made, so it is
placed as the run it copies. The query string of a URL in a prompt or title never counts: a dashboard's link carries
the values of its filters (`?dataset=Stripe`), not what the session was about.

Every lab instance holds Metabase's Sample Database as well, whatever it is named after: a run on it is placed on
it by what it says, never by its instance's name. The source tables a run built on never count: they list whole databases, and one database can hold several datasets.
`dataset_by` records what decided, so the mapping can be audited run by run.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

DEFAULT = Path(__file__).resolve().parents[2] / "semantics" / "datasets.json"
_CACHE = {}

# The columns fill() reads, per table: all a store of the rows (a build, a share file, ClickHouse) has to give back.
READS = {
    "sessions": ("session_id", "title", "baseline"),
    "turns": ("session_id", "turn", "trigger", "prompt"),
    "skill_runs": ("session_id", "run_id", "prompt", "args", "start_at"),
    "tool_calls": ("session_id", "run_id", "input"),
    "run_artifacts": ("session_id", "run_id", "name", "description", "definition", "target_table"),
    "run_instances": ("session_id", "run_id", "host", "profile"),
}
ARTIFACT_TEXT = ("name", "description", "definition", "target_table")
QUERY = re.compile(r"\?[^\s?=]*=\S*")  # a URL's query string: a pasted dashboard link's filter values


def _prose(text):
    """`text` without the query strings of the URLs in it (its paths stay: /schema/flight_delays names a dataset)."""
    return QUERY.sub(" ", text) if isinstance(text, str) else text


def _alternation(patterns):
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.I) if patterns else None


class Datasets:
    def __init__(self, path=None):
        spec = json.loads(Path(path or DEFAULT).read_text(encoding="utf-8"))
        self.min_calls, self.margin = spec["min_calls"], spec["margin"]
        self.labels = [d["label"] for d in spec["datasets"]]
        self.match = {d["label"]: _alternation(d.get("match") or ()) for d in spec["datasets"]}
        self.instances = {d["label"]: _alternation(d.get("instances") or ()) for d in spec["datasets"]}

    def named(self, *texts):
        """The datasets any of `texts` names, in the file's order."""
        texts = [t for t in texts if isinstance(t, str) and t]
        return [x for x in self.labels if self.match[x] and any(self.match[x].search(t) for t in texts)]

    def dominant(self, texts):
        """(dataset, by) when one dataset is named by at least min_calls of `texts`, and margin times as many as the
        runner-up; else (None, None)."""
        n = Counter(x for t in texts for x in self.named(t))
        ranked = sorted(n.items(), key=lambda kv: (-kv[1], self.labels.index(kv[0])))
        if not ranked:
            return None, None
        top, most = ranked[0]
        if most < self.min_calls or most < self.margin * (ranked[1][1] if len(ranked) > 1 else 0):
            return None, None
        return top, "tool calls: " + ", ".join(f"{x} {c}" for x, c in ranked)

    def hosted(self, instances):
        """The datasets an instance is named after: its mb profile, or the first label of its host (toy-store2 of
        toy-store2.localhost:3202)."""
        names = set()
        for r in instances:
            names.add(str(r.get("profile") or "").strip())
            names.add(str(r.get("host") or "").split(":")[0].split(".")[0].strip())
        names.discard("")
        return [x for x in self.labels if self.instances[x] and any(self.instances[x].fullmatch(n) for n in names)]

    def hosted_unless(self, instances, told):
        """The one dataset `instances` are named after, unless the texts of the session (`told`: the datasets its
        title, or the run's prompt, names) name only others; else None."""
        got = self.hosted(instances)
        return got[0] if len(got) == 1 and (not told or got[0] in told) else None

    def run(self, prompt, args, artifacts, calls, instances, title=None):
        """(dataset, by) from one run's own evidence (its session's `title` only keeps a misleading instance name from
        deciding), or (None, None)."""
        said = self.named(_prose(prompt), _prose(args))
        for got, by in ((said, "prompt"),
                        (self.named(*(a.get(c) for a in artifacts for c in ARTIFACT_TEXT)), "snapshot")):
            if len(got) == 1:
                return got[0], by
        got, by = self.dominant(c.get("input") for c in calls)
        if got:
            return got, by
        got = self.hosted_unless(instances, set(said) | set(self.named(_prose(title))))
        return (got, "instance") if got else (None, None)

    def session(self, title, own, artifacts, calls, instances):
        """(dataset, by) of a session from its title, its runs' own datasets (`own`, in run order: None where a run
        settled nothing), and the snapshots and instances of its runs and all its tool calls taken together; or
        (None, None)."""
        placed = [x for x in own if x]
        if len(set(placed)) == 1:
            return placed[0], "runs"
        titled = self.named(_prose(title))
        for got, by in ((titled, "title"),
                        (self.named(*(a.get(c) for a in artifacts for c in ARTIFACT_TEXT)), "snapshot")):
            if len(got) == 1:
                return got[0], by
        got, by = self.dominant(c.get("input") for c in calls)
        if got:
            return got, by
        got = self.hosted_unless(instances, set(titled))
        if got:
            return got, "instance"
        if placed:  # the runs disagree: the most runs' (ties: the earliest run's), the split recorded
            ranked = Counter(placed).most_common()
            return ranked[0][0], "runs: " + ", ".join(f"{x} {n}" for x, n in ranked)
        return None, None


def load(path=None):
    key = str(path or DEFAULT)
    if key not in _CACHE:
        _CACHE[key] = Datasets(path)
    return _CACHE[key]


def _number(v):
    v = str(v if v is not None else "").rsplit(":", 1)[-1]
    return int(v) if v.isdigit() else 0


def _first_prompt(turns):
    """The prompt that opened the session, as warehouse._first_prompt picks it: its first typed prompt, or the first
    /command (a turn row holds the command's arguments in its prompt)."""
    first = min((t for t in turns if t.get("trigger") in ("prompt", "command")), key=lambda t: _number(t.get("turn")),
                default=None)
    return first.get("prompt") if first else None


def _baseline(session):
    return session.get("baseline") in (True, 1, "true", "1")


def fill(tables):
    """Set `dataset` and `dataset_by` on every sessions and skill_runs row of `tables` ({table: rows}), from those rows
    alone. A table that is missing (a share file from before 0.7.0 has no run_* tables) or a column (rows an older
    version wrote) is evidence missing, never an error. Returns {"sessions": Counter, "skill_runs": Counter} of the
    datasets set (None: none)."""
    rules = load()
    by = {}  # session_id: {table: rows}
    for name in ("turns", "skill_runs", "tool_calls", "run_artifacts", "run_instances"):
        for r in tables.get(name) or ():
            by.setdefault(r.get("session_id"), {}).setdefault(name, []).append(r)
    sessions = {}  # session_id: its rows (one, unless a store holds it twice: each gets the columns)
    for s in tables.get("sessions") or ():
        sessions.setdefault(s.get("session_id"), []).append(s)
    counts = {"sessions": Counter(), "skill_runs": Counter()}
    for sid in list(sessions) + [x for x in by if x not in sessions]:
        s, got = (sessions.get(sid) or [{}])[0], by.get(sid) or {}
        calls = got.get("tool_calls") or []
        artifacts, instances = got.get("run_artifacts") or [], got.get("run_instances") or []
        rows = {}
        for r in got.get("skill_runs") or ():
            rows.setdefault(r.get("run_id"), []).append(r)
        ids = set(rows) | {r.get("run_id") for r in artifacts + instances}
        if _baseline(s):
            ids.add(f"{str(sid)[:8]}:0")
        ids.discard(None)
        order = sorted(ids, key=lambda i: (str((rows.get(i) or [{}])[0].get("start_at") or ""), _number(i)))
        own = {}
        for rid in order:
            r = (rows.get(rid) or [None])[0]
            # a run with no skill_runs row is a baseline's (warehouse.baseline_run): its prompt is the session's
            # first, and its tool calls every one no skill run made
            prompt, args = (r.get("prompt"), r.get("args")) if r else (_first_prompt(got.get("turns") or ()), None)
            own[rid] = rules.run(prompt, args, [a for a in artifacts if a.get("run_id") == rid],
                                 [c for c in calls if c.get("run_id") == rid or (r is None and c.get("run_id") is None)],
                                 [i for i in instances if i.get("run_id") == rid], s.get("title"))
        dataset, dataset_by = rules.session(s.get("title"), [own[i][0] for i in order], artifacts, calls, instances)
        for row in sessions.get(sid) or ():
            row["dataset"], row["dataset_by"] = dataset, dataset_by
            counts["sessions"][row["dataset"]] += 1
        for rid, same in rows.items():
            x, how = own.get(rid) or (None, None)
            if x is None and dataset:
                x, how = dataset, "session"
            for r in same:
                r["dataset"], r["dataset_by"] = x, how
                counts["skill_runs"][r["dataset"]] += 1
    return counts
