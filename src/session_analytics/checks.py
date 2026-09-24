"""Expectations about how a skill behaves, evaluated against each of its runs.

A checks file is JSON: {"skill": "rde", "checks": [{"id": ..., "desc": ..., "type": ..., ...}]}.
The events checked are the run's tool calls, in order (main thread only unless "scope": "all").

Types
  first         the run's first event matches `match`
  before        the first `a` comes before the first `b`; n/a when `b` never happens
  count         `min` <= matching events <= `max`; "distinct": true counts distinct skill files instead
  count_before  events matching `match` before the first `until` are within [`min`, `max`]; n/a without `until`
  never         no event matches `match`
  every         every event matching `match` also matches `require`; with "segments": true each shell
                pipeline segment (`a && b | c`) is tested on its own

Matchers (every key given must hold; values are regular expressions unless noted)
  tool      the tool name                     bash      the shell command (or segment)
  resource  a skill file the call read, relative to the skill directory ("playbooks/x.md")
  input     the input summary shown in reports
  status    ok | error | denied | interrupted (exact)
  file, op, how   one skill document the call touched (see skillfiles.py), all three on the same document:
            file  "<owner>:<path>", e.g. "rde:references/state.md" or "mb:dashboard/SKILL.md"
            op    read | search | list | resolve | stat
            how   full | partial | hits | not shown | no hits | missing | listed | resolved
"""

from __future__ import annotations

import json
import re
from pathlib import Path

VALID_TYPES = {"first", "before", "count", "count_before", "never", "every"}
FILE_KEYS = ("file", "op", "how")
PACKAGE_CHECKS = Path(__file__).resolve().parents[2] / "checks"


def load(paths=(), skill=None):
    """Checks for `skill`: explicit files first, else <repo>/checks/<skill>.json when it exists."""
    files = [Path(p).expanduser() for p in paths]
    if not files and skill:
        candidate = PACKAGE_CHECKS / f"{skill}.json"
        if candidate.is_file():
            files = [candidate]
    checks = []
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        if skill and data.get("skill") and data["skill"] != skill:
            continue
        for c in data.get("checks", []):
            if c.get("type") not in VALID_TYPES:
                raise ValueError(f"{f}: check {c.get('id')!r} has unknown type {c.get('type')!r}")
            checks.append(dict(c, source=str(f)))
    return checks


def _matches(event, m, text_override=None):
    if not m:
        return False
    for key, pattern in m.items():
        if key == "tool":
            if not re.search(pattern, event["tool"]):
                return False
        elif key == "bash":
            text = text_override if text_override is not None else event.get("command")
            if not text or not re.search(pattern, text):
                return False
        elif key == "resource":
            if not any(re.search(pattern, r) for r in event.get("resources", ())):
                return False
        elif key == "input":
            if not re.search(pattern, event.get("input") or ""):
                return False
        elif key == "status":
            if event.get("status") != pattern:
                return False
        elif key in FILE_KEYS:
            continue
        else:
            raise ValueError(f"unknown matcher key {key!r}")
    fm = {k: m[k] for k in FILE_KEYS if k in m}
    if fm and not _file_hits(event, fm):
        return False
    return True


def _file_hits(event, fm):
    """The skill documents of one event that satisfy every file/op/how pattern given."""
    return [f for f in event.get("files", ()) if all(re.search(p, f.get(k) or "") for k, p in fm.items())]


def _first(events, m):
    return next((i for i, e in enumerate(events) if _matches(e, m)), None)


def _first_pos(events, m):
    """(event index, offset in its command) of the first match; offsets order two matches in one command."""
    i = _first(events, m)
    if i is None:
        return None
    pos = 0
    if "bash" in m and events[i].get("command"):
        hit = re.search(m["bash"], events[i]["command"])
        pos = hit.start() if hit else 0
    return (i, pos)


def evaluate(check, events):
    """-> {id, desc, status: pass|fail|n/a, detail}"""
    t = check["type"]
    out = {"id": check.get("id"), "desc": check.get("desc"), "type": t}
    try:
        if t == "first":
            if not events:
                return dict(out, status="n/a", detail="no tool calls")
            ok = _matches(events[0], check["match"])
            return dict(out, status="pass" if ok else "fail",
                        detail=None if ok else f"first call was {events[0]['tool']}: {events[0]['input'][:80]}")
        if t == "before":
            pa, pb = _first_pos(events, check["a"]), _first_pos(events, check["b"])
            if pb is None:
                return dict(out, status="n/a", detail="`b` never happened")
            if pa is None:
                return dict(out, status="fail", detail=f"`b` at call {pb[0] + 1} with no `a` before it")
            ok = pa < pb
            return dict(out, status="pass" if ok else "fail", detail=f"`a` at call {pa[0] + 1}, `b` at call {pb[0] + 1}")
        if t == "count":
            hits = [e for e in events if _matches(e, check["match"])]
            fm = {k: check["match"][k] for k in FILE_KEYS if k in check["match"]}
            if check.get("distinct") and fm:
                n = len({f["file"] for e in hits for f in _file_hits(e, fm)})
            elif check.get("distinct"):
                pat = check["match"].get("resource")
                n = len({r for e in hits for r in e.get("resources", ()) if not pat or re.search(pat, r)})
            else:
                n = len(hits)
            ok = check.get("min", 0) <= n <= check.get("max", float("inf"))
            return dict(out, status="pass" if ok else "fail", detail=f"{n} matching")
        if t == "count_before":
            iu = _first(events, check["until"])
            if iu is None:
                return dict(out, status="n/a", detail="`until` never happened")
            n = sum(1 for e in events[:iu] if _matches(e, check["match"]))
            ok = check.get("min", 0) <= n <= check.get("max", float("inf"))
            return dict(out, status="pass" if ok else "fail", detail=f"{n} before call {iu + 1}")
        if t == "never":
            hits = [e for e in events if _matches(e, check["match"])]
            fm = {k: check["match"][k] for k in FILE_KEYS if k in check["match"]}
            example = None
            if hits and fm:
                example = ", ".join(sorted({f["file"] for e in hits for f in _file_hits(e, fm)})[:3])
            elif hits:
                example = (hits[0].get("command") or hits[0].get("input") or "")[:100]
            return dict(out, status="pass" if not hits else "fail",
                        detail=f"{len(hits)} matching" + (f", e.g. {example}" if example else "") if hits else None)
        if t == "every":
            seen, bad, example = 0, 0, None
            for e in events:
                units = e.get("segments") if check.get("segments") and e.get("segments") else [None]
                for seg in units:
                    if _matches(e, check["match"], seg):
                        seen += 1
                        if not _matches(e, check["require"], seg):
                            bad += 1
                            example = example or (seg or e.get("command") or e.get("input") or "")[:120]
            if not seen:
                return dict(out, status="n/a", detail="nothing to check")
            return dict(out, status="pass" if not bad else "fail",
                        detail=f"{bad} of {seen} violate" + (f", e.g. `{example}`" if example else ""))
    except (KeyError, re.error, ValueError) as exc:
        return dict(out, status="error", detail=f"bad check: {exc}")
    return dict(out, status="error", detail="unknown type")
