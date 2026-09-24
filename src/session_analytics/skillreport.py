"""One skill across many sessions: every run, grouped by the version that ran, for skill development.

    session-analytics skill rde --since 30d --all

Runs come from skillruns.build_runs (one per invocation, including the follow-up turns it steered).
Versions come from versions.resolve (git commit of the skill source). For each version: how many runs,
median cost / tool calls / errors / questions / duration, check pass rates, which CLI commands ran and
failed, what changed in git since the previous version, and for every file of the skill (and every bundled
CLI doc it sent Claude to) how the runs used it: read whole or in part, which sections, in what order, what
named it, and whether the lines a change added were ever shown to a run (skillfiles.exposure).
"""

from __future__ import annotations

import csv
import difflib
import json
import os
import re
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from . import __version__, locate, render_html, skillfiles, util, versions
from . import questions as questions_mod
from .analyze import analyze
from .export import _slug, default_root
from .parse import parse_session
from .pricing import Pricing
from .redact import Redactor
from .render_csv import skill_file_rows
from .rollup import parse_since
from .skillruns import skill_key, taxonomy_for

SCHEMA = "convo-analysis/skill-v1"
RUN_METRICS = ("cost_usd", "requests", "tool_calls", "tool_errors", "error_rate", "cli_calls", "help_lookups",
               "retries_after_error", "question_calls", "objects_created", "duration_ms", "active_ms", "turn_count",
               "follow_up_turns", "context_peak", "output_tokens", "checks_failed", "docs_read", "cli_docs_read",
               "doc_tokens", "doc_listings", "doc_rereads", "questions_asked", "prose_questions", "recommended_rate",
               "typed_answers", "unanswered_questions", "question_wait_p50_ms", "questions_flagged",
               "questions_before_create")
FULL = ("full", "injected", "injected + re-read")


def actions(run, steps):
    """A run as a list of comparable actions: CLI commands, skill files read, questions, edits, prompts."""
    out = []
    for st in steps:
        k = st.get("k")
        mine = []
        if k == "prompt" and st["t"] > run["start_ms"]:
            mine.append("you: prompt")
        elif k == "skill" and st["t"] > run["start_ms"]:
            mine.append(f"skill {st.get('name')}")
        elif k == "tool" and st.get("scope") == "main" and st.get("id") != run.get("tool_use_id"):
            name = st.get("name")
            res = []
            for entry in st.get("res") or ():
                op, _, ref = entry.partition(" ")
                owner, _, path = ref.partition(":")
                if op in ("read", "search", "list"):
                    res.append(f"{op} " + ((path or "./") if owner == run["skill"] else ref))
            if name == "Bash":
                mine += [f"$ {sig}" for sig in st.get("sigs") or ()]
                mine += res
                if not mine and st.get("prog"):
                    mine.append(f"$ {st['prog']}")
            elif res:
                mine += res
            elif name == "AskUserQuestion":
                mine.append("ask the user")
            elif name in ("Edit", "Write", "MultiEdit"):
                mine.append(f"{name} {Path(str(st.get('input') or '')).name}")
            else:
                mine.append(name)
            if mine and st.get("status") in ("error", "denied"):
                mine[-1] += f"  ✗ {st['status']}"
        out += [(st["i"], a) for a in mine]
    collapsed = []
    for i, a in out:
        if collapsed and collapsed[-1][1] == a:
            collapsed[-1] = (collapsed[-1][0], a, collapsed[-1][2] + 1)
        else:
            collapsed.append((i, a, 1))
    return [{"step": i, "action": a, "times": n} for i, a, n in collapsed]


def _median(vals):
    """True median (the middle two averaged): versions often have two or three runs, where nearest-rank misleads."""
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def collect_runs(name, claude_dir=None, project=None, since="30d", limit=500, redact=True, pricing=None,
                 check_files=(), sources=(), now_ms=None):
    key = skill_key(name)
    now = now_ms if now_ms is not None else time.time() * 1000
    cutoff = parse_since(since, now)
    cdir = locate.claude_dir(claude_dir)
    files = [f for f in locate.iter_transcripts(cdir, project=project) if f.stat().st_mtime * 1000 >= cutoff][:limit]
    # Cheap pre-filter before a full parse: the ways a skill's name appears when it was actually used.
    k = re.escape(key).encode()
    marker = re.compile(rb'/skills/' + k + rb'/|"(?:skill|commandName|attributionSkill)":\s*"(?:[^"]*:)?' + k +
                        rb'"|<command-name>/?' + k + rb'<')
    pricing = pricing or Pricing()
    R = Redactor(redact)
    runs, scanned = [], 0
    for f in files:
        try:
            data = f.read_bytes()
        except OSError:
            continue
        if not marker.search(data):
            continue
        scanned += 1
        s = parse_session(f)
        if not any(skill_key(i.canonical or i.name) == key for i in s.skills):
            continue
        a = analyze(s, pricing, redactor=R, now_ms=now, checks=check_files, skill_sources=sources)
        steps = a["trace"]["steps"]
        for r in a["skill_runs"]:
            if r["skill"] != key or r["inherited"] or (r["start_ms"] or 0) < cutoff:
                continue
            run_steps = [steps[i] for i in r["steps"]]
            r = dict(r, steps=run_steps, session_title=a["session"]["title"], project=a["session"]["project_name"],
                     transcript=a["session"]["transcript"])
            r["actions"] = actions(r, run_steps)
            runs.append(r)
    runs.sort(key=lambda r: r["start_ms"] or 0)
    return runs, {"transcripts": len(files), "mentioning_skill": scanned, "cutoff": cutoff, "now": now}


_BANNER = re.compile(r"^\s*(=+|-+|#+)[^\n]*$")
_ERRORISH = re.compile(r"error|fail|not found|denied|invalid|cannot|can't|unknown|missing|refused|timed? ?out|"
                       r"exception|traceback|no such|unexpected|parse", re.I)


def failure_key(message):
    """A failure's message reduced to what repeats across runs: the first error-looking line, numbers blanked."""
    lines = [ln.strip() for ln in (message or "").splitlines() if ln.strip()]
    lines = [ln for ln in lines if not _BANNER.match(ln) and not ln.startswith("Exit code ")] or lines
    line = next((ln for ln in lines if _ERRORISH.search(ln)), lines[0] if lines else "")
    line = re.sub(r"\(at <stdin>:\d+\)", "", line)
    line = re.sub(r'"[^"]{12,}"', '"…"', line)
    line = re.sub(r"\d+", "N", line)
    return util.one_line(line, 90)


def _version_key(r):
    v = r["version"]
    return v.get("sha") or v.get("fingerprint") or "unknown"


def build_report(name, runs, meta, since, project, check_files=()):
    key = skill_key(name)
    by_version = defaultdict(list)
    for r in runs:
        by_version[_version_key(r)].append(r)

    def order(item):
        vk, rs = item
        v = rs[0]["version"]
        return (util.parse_ts(v.get("date")) or min(x["start_ms"] or 0 for x in rs))

    check_ids = list(dict.fromkeys(c["id"] for r in runs for c in r["checks"]))
    check_desc = {c["id"]: c.get("desc") for r in runs for c in r["checks"]}
    version_rows, prev = [], None
    for vk, rs in sorted(by_version.items(), key=order):
        v = rs[0]["version"]
        row = {"key": vk, "label": v.get("label"), "status": v.get("status"), "commit": v.get("commit"),
               "sha": v.get("sha"), "date": v.get("date"), "subject": v.get("subject"), "source": v.get("source"),
               "fingerprint": v.get("fingerprint"), "runs": len(rs), "run_ids": [r["run_id"] for r in rs],
               "first_run": util.iso(min(r["start_ms"] for r in rs)), "last_run": util.iso(max(r["start_ms"] for r in rs)),
               "median": {m: _median([r.get(m) for r in rs]) for m in RUN_METRICS},
               "mean": {m: _mean([r.get(m) for r in rs]) for m in RUN_METRICS},
               "total_cost_usd": round(sum(r["cost_usd"] for r in rs), 6)}
        rates = {}
        for cid in check_ids:
            st = Counter(next((c["status"] for c in r["checks"] if c["id"] == cid), "n/a") for r in rs)
            applicable = st["pass"] + st["fail"]
            rates[cid] = {"pass": st["pass"], "fail": st["fail"], "na": st["n/a"] + st.get("error", 0),
                          "rate": util.ratio(st["pass"], applicable)}
        row["checks"] = rates
        row["resources"] = dict(Counter(p for r in rs for p in r["resources_read"]).most_common())
        cli = defaultdict(lambda: {"calls": 0, "errors": 0, "help": 0})
        for r in rs:
            for c in r["cli"]:
                for k in ("calls", "errors", "help"):
                    cli[c["signature"]][k] += c[k]
        row["cli"] = {k: dict(v, per_run=round(v["calls"] / len(rs), 2)) for k, v in
                      sorted(cli.items(), key=lambda kv: -kv[1]["calls"])}
        row["error_categories"] = dict(Counter(c for r in rs for c, n in r["error_categories"].items()
                                               for _ in range(n)).most_common())
        row["changes"] = None
        if prev and prev.get("sha") and row.get("sha") and row["source"]:
            src = versions.SkillSource(row["source"])
            hunks = src.diff_hunks(prev["sha"], row["sha"])
            row["changes"] = {"from": prev.get("commit"), "commits": src.log_between(prev["sha"], row["sha"]),
                              "files": src.diffstat(prev["sha"], row["sha"]),
                              "exposure": _exposure(hunks, rs, key, src.file_at(row["sha"], "SKILL.md"))}
        # Every file of the skill at this version, and every other document the runs touched.
        row["inventory"] = list(rs[0]["skill_files"]["inventory"]["files"])
        keys = [f"{key}:{f}" for f in row["inventory"]]
        keys += [fk for r in rs for fk in (f"{f['owner']}:{f['path']}" for f in r["skill_files"]["files"])
                 if fk not in keys]
        row["files"] = {fk: _file_stats(rs, fk) for fk in dict.fromkeys(keys)}
        row["never"] = [f for f in row["inventory"] if not row["files"][f"{key}:{f}"]["shown"]]
        version_rows.append(row)
        prev = row

    failures = defaultdict(lambda: {"count": 0, "runs": set(), "example": None, "tool": None, "category": None})
    for r in runs:
        for e in r["errors"]:
            sig = f"{e['category']} · {e['tool']} · {failure_key(e['message'])}"
            f = failures[sig]
            f["count"] += 1
            f["runs"].add(r["run_id"])
            f["tool"], f["category"] = e["tool"], e["category"]
            f["example"] = f["example"] or {"run": r["run_id"], "input": e["input"], "message": e["message"]}
    failure_rows = [{"signature": k, "count": v["count"], "runs": sorted(v["runs"]), "tool": v["tool"],
                     "category": v["category"], "example": v["example"]}
                    for k, v in sorted(failures.items(), key=lambda kv: -kv[1]["count"])]

    file_rows = _file_rows(version_rows, key)
    run_rows = []
    for r in runs:
        run_rows.append({k: r.get(k) for k in (
            "run_id", "skill", "session_id", "session_title", "project", "mode", "via", "start_ms", "end_reason", "args",
            "prompt", "turn_count", "follow_up_turns", "requests", "attributed_requests", "tool_calls", "tool_errors",
            "error_rate", "cli_calls", "help_lookups", "retries_after_error", "question_calls", "questions_asked",
            "objects_created", "cost_usd", "attributed_cost_usd", "duration_ms", "active_ms", "context_start",
            "context_end", "context_peak", "output_tokens", "cache_hit_ratio", "playbooks", "references", "missing_expected",
            "not_named_by_playbooks", "checks_passed", "checks_failed", "denials", "interrupted", "subagents", "models",
            "docs_read", "docs_total", "docs_never", "cli_docs_read", "doc_tokens", "doc_rereads", "doc_listings",
            "doc_unprompted", "doc_version_mismatches", "prose_questions", "recommended_rate", "typed_answers",
            "unanswered_questions", "question_wait_p50_ms", "questions_reasked", "questions_flagged",
            "questions_before_create", "first_question_dt")}
                        | {"version": r["version"].get("label"), "version_key": _version_key(r),
                           "commit": r["version"].get("commit"),
                           "checks": {c["id"]: c["status"] for c in r["checks"]}})

    report = {
        "schema": SCHEMA,
        "generator": {"name": "convo-analysis", "version": __version__, "generated_at": util.iso(meta["now"])},
        "skill": key,
        "scope": {"project": project, "label": Path(project).name if project else "all projects", "since": since,
                  "cutoff": util.iso(meta["cutoff"]) if meta["cutoff"] else None, "transcripts": meta["transcripts"],
                  "transcripts_using_skill": meta["mentioning_skill"]},
        "totals": {"runs": len(runs), "sessions": len({r["session_id"] for r in runs}), "versions": len(version_rows),
                   "cost_usd": round(sum(r["cost_usd"] for r in runs), 6),
                   "tool_calls": sum(r["tool_calls"] for r in runs), "tool_errors": sum(r["tool_errors"] for r in runs),
                   "cli_calls": sum(r["cli_calls"] for r in runs), "question_calls": sum(r["question_calls"] for r in runs),
                   "objects_created": sum(r["objects_created"] for r in runs),
                   "median_cost_usd": _median([r["cost_usd"] for r in runs]),
                   "median_tool_calls": _median([r["tool_calls"] for r in runs])},
        "checks": [{"id": cid, "desc": check_desc.get(cid)} for cid in check_ids],
        "versions": version_rows,
        "files": file_rows,
        "runs": run_rows,
        "failures": failure_rows,
        "details": {r["run_id"]: {k: r.get(k) for k in (
            "skill_files", "changes_seen", "cli", "interview", "objects", "files_written", "final_message", "errors",
            "checks", "turns", "nested_skills", "failed_invocations", "actions", "steps", "version",
            "expected_by_playbooks", "tools", "error_categories", "transcript")} for r in runs},
    }
    report["interview"] = _interview(runs, version_rows, taxonomy_for(key, check_files))
    report["insights"] = _insights(report)
    return report


def _interview(runs, version_rows, taxonomy):
    """Every question of every run, by topic and by version: what the skill asks, and what comes back."""
    vkey = {r["run_id"]: _version_key(r) for r in runs}
    commit = {r["run_id"]: r["version"].get("commit") or r["version"].get("label") for r in runs}
    rows = [dict(q, version_key=vkey[r["run_id"]], commit=commit[r["run_id"]], session_title=r.get("session_title"))
            for r in runs for q in r["interview"]["questions"]]
    order = {t: i for i, t in enumerate(taxonomy.order())}
    catalog = {}
    for q in rows:
        c = catalog.setdefault(q["topic"], {"topic": q["topic"], "label": q["topic_label"], "rows": []})
        c["rows"].append(q)
    cat_rows = []
    for c in catalog.values():
        qs = c.pop("rows")
        summ = questions_mod.summarize(qs)
        topic = next((t for t in summ["topics"] if t["topic"] == c["topic"]), {})
        per_version = {}
        for v in version_rows:
            vq = [q for q in qs if q["version_key"] == v["key"]]
            vs = questions_mod.summarize(vq) if vq else None
            per_version[v["key"]] = {"runs": v["runs"], "runs_asking": len({q["run_id"] for q in vq}),
                                     "asked": vs["asked"] if vs else 0, "prose": vs["prose"] if vs else 0,
                                     "recommended": vs["recommended_picked"] if vs else 0,
                                     "offered": vs["recommended_offered"] if vs else 0,
                                     "typed": vs["typed"] if vs else 0, "outcomes": vs["outcomes"] if vs else {}}
        headers = Counter(q["header"] for q in qs if q.get("header"))
        examples = list(dict.fromkeys(q["question"] for q in qs if q.get("question")))[:4]
        cat_rows.append(dict(c, asked=summ["asked"], prose=summ["prose"], checkpoints=summ["checkpoints_unasked"],
                             runs=len({q["run_id"] for q in qs}), offered=summ["recommended_offered"],
                             recommended=summ["recommended_picked"], recommended_rate=summ["recommended_rate"],
                             typed=summ["typed"], no_preference=summ["no_preference"],
                             declined=summ["declined"], unanswered=summ["unanswered"] + summ["prose_unanswered"],
                             reasked=summ["reasked"], wait_p50_ms=summ["wait_p50_ms"], outcomes=summ["outcomes"],
                             answers=topic.get("answers") or {}, flags=summ["flags"], headers=dict(headers.most_common(6)),
                             examples=examples, per_version=per_version,
                             once=next((t["once"] for t in taxonomy.topics if t["id"] == c["topic"]), False),
                             must_ask=next((t["must_ask"] for t in taxonomy.topics if t["id"] == c["topic"]), False)))
    cat_rows.sort(key=lambda c: (order.get(c["topic"], 99), -(c["asked"] + c["prose"])))
    for v in version_rows:
        vq = [q for q in rows if q["version_key"] == v["key"]]
        v["interview"] = questions_mod.summarize(vq)
        del v["interview"]["topics"]
    typed = [{"run_id": q["run_id"], "commit": q["commit"], "topic": q["topic"], "topic_label": q["topic_label"],
              "header": q["header"], "question": q["question"], "typed": q["typed"],
              "options": [o["label"] for o in q["options"]]} for q in rows if q["typed"]]
    return {"taxonomy": {"source": taxonomy.source, "prose": "avoid" if taxonomy.prose_is_a_miss else None,
                         "jargon": taxonomy.jargon_words,
                         "topics": [{"id": t["id"], "label": t["label"], "once": t["once"], "evidence": t["evidence"],
                                     "must_ask": t["must_ask"]} for t in taxonomy.topics]},
            "summary": questions_mod.summarize(rows), "catalog": cat_rows, "questions": rows, "typed": typed}


def _n(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def _interview_insights(rep):
    """What the interview says about tuning the skill's questions."""
    iv = rep.get("interview") or {}
    s, cat = iv.get("summary") or {}, iv.get("catalog") or []
    if not s.get("total"):
        return []
    notes = [f"{s['total']} questions across {rep['totals']['runs']} runs: {s['asked']} through AskUserQuestion in "
             f"{s['calls']} rounds" + (f", {s['prose']} in prose" if s["prose"] else "") +
             (f"; the recommended option was picked for {s['recommended_picked']}/{s['recommended_offered']} "
              f"({util.fmt_pct(s['recommended_rate'])})" if s["recommended_offered"] else "") + "."]
    always = [c for c in cat if c["offered"] >= 3 and c["recommended"] == c["offered"] and not c["must_ask"]]
    if always:
        notes.append("Always answered with the recommended option: " + ", ".join(
            f"{c['label']} ({c['recommended']}/{c['offered']} in {c['runs']} runs)" for c in always[:3]) +
            ". If these decisions are reversible, they could be decided and shown instead of asked.")
    vs = [v for v in rep["versions"] if (v.get("interview") or {}).get("total", 0) >= 3]  # enough to compare
    if len(vs) >= 2:
        a, b = vs[-2]["interview"], vs[-1]["interview"]
        if abs(util.ratio(a["prose"], a["total"]) - util.ratio(b["prose"], b["total"])) >= 0.25:
            notes.append(f"Questions asked in prose: {a['prose']}/{a['total']} on {vs[-2]['commit'] or vs[-2]['label']} → "
                         f"{b['prose']}/{b['total']} on {vs[-1]['commit'] or vs[-1]['label']}.")
        ra, rb = a["recommended_rate"], b["recommended_rate"]
        if ra is not None and rb is not None and abs(ra - rb) >= 0.2:
            notes.append(f"Recommended option picked: {util.fmt_pct(ra)} on {vs[-2]['commit'] or vs[-2]['label']} → "
                         f"{util.fmt_pct(rb)} on {vs[-1]['commit'] or vs[-1]['label']}.")
    missed = [c for c in cat if c["offered"] >= 2 and (c["recommended_rate"] or 0) <= 0.5]
    for c in missed[:2]:
        answers = ", ".join(f"“{a}” ×{n}" for a, n in list(c["answers"].items())[:3])
        notes.append(f"The recommendation missed for {c['label']}: picked {c['recommended']}/{c['offered']}; "
                     f"answers given: {answers}.")
    if iv.get("typed"):
        ex = iv["typed"][:3]
        notes.append(f"{_n(len(iv['typed']), 'answer')} typed instead of picked (the options did not fit): " + "; ".join(
            f"“{util.one_line(t['typed'], 60)}” for “{util.one_line(t['question'], 70)}”" for t in ex) + ".")
    lost = s["no_preference"] + s["declined"] + s["unanswered"]
    if lost:
        notes.append(f"{_n(lost, 'question')} came back empty: {s['no_preference']} with no preference, {s['declined']} "
                     f"declined, {s['unanswered']} unanswered.")
    reasked = [c for c in cat if c["reasked"]]
    if reasked:
        notes.append("Asked again within a run: " + ", ".join(f"{c['label']} ×{c['reasked']}" for c in reasked[:4]) + ".")
    fl = s.get("flags") or {}
    if fl.get("asked in prose") or fl.get("checkpoint with no AskUserQuestion"):
        ex = next((q for q in iv["questions"] if "asked in prose" in q["flags"]), None)
        notes.append(f"{_n(fl.get('asked in prose', 0), 'question')} asked in prose and "
                     f"{_n(fl.get('checkpoint with no AskUserQuestion', 0), 'checkpoint block')} printed with no "
                     f"AskUserQuestion behind them" +
                     (f" (e.g. “{util.one_line(ex['question'], 90)}”)" if ex else "") + ".")
    rules = {k: fl[k] for k in ("no recommendation", "recommendation not first", "fewer than two options",
                                "no measured numbers", "jargon", "code in the question", "after an error") if fl.get(k)}
    if rules:
        notes.append("Questions against the skill's own rules: " + ", ".join(f"{k} ×{n}" for k, n in rules.items()) + ".")
    if s.get("wait_p50_ms"):
        slow = max((q for q in iv["questions"] if q["kind"] == "ask" and q["wait_ms"]), key=lambda q: q["wait_ms"])
        notes.append(f"Median wait for an answer {util.fmt_duration(s['wait_p50_ms'])}; the longest "
                     f"{util.fmt_duration(slow['wait_ms'])}, for “{util.one_line(slow['question'], 80)}”.")
    return notes


def _file_stats(rs, fkey):
    """How the runs of one version used one file (`owner:path`)."""
    touched = []
    for r in rs:
        f = next((x for x in r["skill_files"]["files"] if f"{x['owner']}:{x['path']}" == fkey), None)
        if f is not None:
            touched.append(f)
    shown = [f for f in touched if f["seen"]]
    return {
        "runs": len(rs), "touched": len(touched), "shown": len(shown),
        "full": sum(1 for f in shown if f["how"] in FULL),
        "partial": sum(1 for f in shown if f["how"] in ("partial", "hits")),
        "not_shown": len(touched) - len(shown),
        "coverage": _median([f["coverage"] for f in shown]),
        "order": _median([f["order"] for f in touched]),
        "first_dt": _median([f["first_dt"] for f in touched]),
        "tokens": _median([f["tokens"] for f in shown]),
        "accesses": sum(f["accesses"] for f in touched),
        "rereads": sum(f["rereads"] for f in touched),
        "via": dict(Counter(v for f in touched for v, n in f["via"].items() for _ in range(n)).most_common()),
        "found_by": dict(Counter(f["found_by"] for f in touched if f["found_by"]).most_common()),
        "named_by": dict(Counter(n for f in touched for n in f["named_by"]).most_common(6)),
        "sections": dict(Counter(sec for f in shown for sec in f["sections"]).most_common(8)),
        "mismatches": sorted({f["version"] for f in touched if f["version"] not in (None, "match", "as installed now")}),
    }


def _exposure(hunks, rs, key, skill_md_text):
    """Per changed file: what the change added, and how many of this version's runs were shown it."""
    rows = {}
    for r in rs:
        for e in skillfiles.exposure(hunks, r["skill_files"]["files"], key, skill_md_text):
            row = rows.setdefault(e["path"], {k: e[k] for k in ("path", "added", "removed", "ranges", "changed_lines",
                                                                 "frontmatter_lines", "deletions")}
                                  | {"statuses": Counter(), "runs": {}})
            row["statuses"][e["status"]] += 1
            row["runs"][r["run_id"]] = {"status": e["status"], "seen": e["seen_lines"], "changed": e["changed_lines"]}
    out = []
    for row in rows.values():
        st = row.pop("statuses")
        row["seen"] = st["seen"]
        row["partly"] = st["partly seen"]
        row["not_seen"] = st["not in the lines read"] + st["file not read"]
        row["not_observable"] = st["deletions only"] + st["frontmatter only"]
        out.append(row)
    return sorted(out, key=lambda x: (-(x["not_seen"] + x["partly"]), x["path"]))


def _file_rows(version_rows, key):
    """One row per file across versions: the skill's own files first, then the docs of other owners."""
    keys = list(dict.fromkeys(fk for v in version_rows for fk in v["files"]))
    rows = []
    for fk in keys:
        owner, _, path = fk.partition(":")
        per = {}
        for v in version_rows:
            st = v["files"].get(fk)
            changed = next((c for c in ((v.get("changes") or {}).get("files") or ()) if c["path"] == path), None) \
                if owner == key else None
            per[v["key"]] = dict(st or {"runs": v["runs"], "touched": 0, "shown": 0},
                                 in_version=(path in v["inventory"]) if owner == key else None,
                                 changed={"added": changed["added"], "removed": changed["removed"]} if changed else None)
        rows.append({"file": fk, "owner": owner, "path": path, "own": owner == key,
                     "kind": (os.path.dirname(path) or ".") if owner == key else owner,
                     "runs_shown": sum(x.get("shown", 0) for x in per.values()),
                     "runs": sum(x.get("runs", 0) for x in per.values()), "per_version": per})
    rows.sort(key=lambda r: (not r["own"], r["owner"], r["path"]))
    return rows


def _insights(rep):
    notes = []
    vs = rep["versions"]
    if not rep["runs"]:
        return [f"No {rep['skill']} runs in this window."]
    notes.append(f"{rep['totals']['runs']} runs of {rep['skill']} in {rep['totals']['sessions']} sessions across "
                 f"{len(vs)} version(s); median run {util.fmt_usd(rep['totals']['median_cost_usd'])}, "
                 f"{rep['totals']['median_tool_calls']} tool calls.")
    if len(vs) >= 2:
        a, b = vs[-2], vs[-1]
        for cid in (c["id"] for c in rep["checks"]):
            # Latest version where the check applied, against the most recent earlier one where it applied too.
            applied = [v for v in vs if v["checks"].get(cid, {}).get("rate") is not None]
            if len(applied) < 2:
                continue
            va, vb = applied[-2], applied[-1]
            ra, rb = va["checks"][cid], vb["checks"][cid]
            if abs(rb["rate"] - ra["rate"]) >= 0.25:
                notes.append(f"Check `{cid}`: {ra['pass']}/{ra['pass'] + ra['fail']} runs passed on "
                             f"{va['commit'] or va['label']} → {rb['pass']}/{rb['pass'] + rb['fail']} on "
                             f"{vb['commit'] or vb['label']}.")
        for m, label in (("cost_usd", "cost"), ("tool_calls", "tool calls"), ("tool_errors", "tool errors"),
                         ("question_calls", "questions"), ("help_lookups", "help lookups"), ("duration_ms", "duration"),
                         ("docs_read", "skill files read"), ("doc_tokens", "≈ tokens of skill docs read"),
                         ("questions_asked", "questions asked")):
            x, y = a["median"].get(m), b["median"].get(m)
            if x and y and (y / x >= 1.5 or y / x <= 0.67):
                fmt = util.fmt_usd if m == "cost_usd" else (util.fmt_duration if m == "duration_ms" else
                                                            util.fmt_tokens if m == "doc_tokens" else str)
                notes.append(f"Median {label} per run moved {fmt(x)} → {fmt(y)} from {a['commit'] or a['label']} to "
                             f"{b['commit'] or b['label']} (n={a['runs']} → {b['runs']}).")
    notes += _file_insights(rep)
    notes += _interview_insights(rep)
    worst = sorted(((c["id"], sum(v["checks"].get(c["id"], {}).get("fail", 0) for v in vs)) for c in rep["checks"]),
                   key=lambda x: -x[1])
    if worst and worst[0][1]:
        notes.append("Most failed checks: " + ", ".join(f"`{i}` ×{n}" for i, n in worst[:4] if n) + ".")
    if rep["failures"]:
        f = rep["failures"][0]
        notes.append(f"Most frequent tool failure: {f['signature']} (×{f['count']} in {len(f['runs'])} runs).")
    return notes


def _file_insights(rep):
    """What the skill-file analysis says worth reading first: changes no run saw, files no run reads, text
    that was not the version a run is labelled with, and files reached without anything naming them."""
    notes, vs = [], rep["versions"]
    for v in vs:
        ch = v.get("changes") or {}
        # The root README is for people browsing the repository; Claude Code never loads it.
        missed = [e for e in ch.get("exposure") or () if e["changed_lines"] and (e["not_seen"] or e["partly"])
                  and e["path"] != "README.md"]
        if missed:
            parts = [f"{e['path']} (+{e['added']}: seen by {e['seen']}/{v['runs']} runs"
                     + (f", in part by {e['partly']}" if e["partly"] else "") + ")" for e in missed[:3]]
            more = f" and {len(missed) - 3} more" if len(missed) > 3 else ""
            notes.append(f"{ch.get('from')} → {v['commit']} changed files its runs did not fully see: "
                         + "; ".join(parts) + more + ".")
    if vs:
        last = vs[-1]
        never = [p for p in last.get("never") or () if p != "README.md"]
        if never and last["runs"] >= 2:
            notes.append(f"{len(never)} of {len(last['inventory'])} files were shown to no run of "
                         f"{last['commit'] or last['label']} ({last['runs']} runs): " + ", ".join(never[:6])
                         + (" …" if len(never) > 6 else "") + ".")
    off = Counter()
    for fr in rep["files"]:
        for st in fr["per_version"].values():
            for label in st.get("mismatches") or ():
                off[(fr["file"], label)] += 1
    if off:
        (fk, label), n = off.most_common(1)[0]
        notes.append(f"Runs read text that is not the version they ran: {fk} matched `{label}` "
                     f"({sum(off.values())} file reads in all). Is the installed copy in step with the repository?")
    reached = Counter()
    for fr in rep["files"]:
        in_skill = any(st.get("in_version") for st in fr["per_version"].values())
        if fr["own"] and in_skill and fr["path"] != "README.md":
            for st in fr["per_version"].values():
                for how, n in (st.get("found_by") or {}).items():
                    if how in ("listing", "search", "unprompted"):
                        reached[fr["path"]] += n
    if reached:
        notes.append("Skill files reached without an earlier doc naming them (by listing, grep, or from memory): "
                     + ", ".join(f"{p} ×{n}" for p, n in reached.most_common(4)) + ".")
    return notes


def load_run(ref, claude_dir=None, redact=True, pricing=None, check_files=(), sources=()):
    """A run by reference `<session id or prefix>:<n>` (as shown in reports), with steps and actions."""
    if ":" not in ref:
        raise ValueError(f"run reference must look like <session>:<n>, e.g. b3734789:1 (got {ref!r})")
    sess, _, n = ref.rpartition(":")
    path = locate.resolve(sess, cdir=claude_dir)
    s = parse_session(path)
    a = analyze(s, pricing or Pricing(), redactor=Redactor(redact), checks=check_files, skill_sources=sources)
    want = f"{s.session_id[:8]}:{n}"
    for r in a["skill_runs"]:
        if r["run_id"] == want:
            steps = [a["trace"]["steps"][i] for i in r["steps"]]
            r = dict(r, steps=steps, session_title=a["session"]["title"], project=a["session"]["project_name"])
            r["actions"] = actions(r, steps)
            return r
    ids = ", ".join(r["run_id"] for r in a["skill_runs"]) or "none"
    raise ValueError(f"no run {want} in that session (runs: {ids})")


def compare_runs(a, b):
    """Side by side metrics, checks and an action-sequence diff of two runs (as produced by collect_runs)."""
    rows = [(m, a.get(m), b.get(m)) for m in RUN_METRICS]
    checks = []
    for cid in dict.fromkeys([c["id"] for c in a["checks"]] + [c["id"] for c in b["checks"]]):
        ca = next((c["status"] for c in a["checks"] if c["id"] == cid), "—")
        cb = next((c["status"] for c in b["checks"] if c["id"] == cid), "—")
        checks.append((cid, ca, cb))
    seq_a = [x["action"] + (f" ×{x['times']}" if x["times"] > 1 else "") for x in a["actions"]]
    seq_b = [x["action"] + (f" ×{x['times']}" if x["times"] > 1 else "") for x in b["actions"]]
    diff = list(difflib.unified_diff(seq_a, seq_b, fromfile=a["run_id"], tofile=b["run_id"], lineterm="", n=2))
    return {"metrics": rows, "checks": checks, "diff": diff,
            "resources": (sorted(set(a["resources_read"]) - set(b["resources_read"])),
                          sorted(set(b["resources_read"]) - set(a["resources_read"])))}


def render_compare(a, b, cmp):
    from .render_md import table

    def fmt(m, v):
        if v is None:
            return "—"
        if m == "cost_usd":
            return util.fmt_usd(v)
        if m.endswith("_ms"):
            return util.fmt_duration(v)
        if m == "error_rate":
            return util.fmt_pct(v)
        return f"{v:,}" if isinstance(v, int) else f"{v:.2f}" if isinstance(v, float) else str(v)
    lines = [f"# {a['run_id']} vs {b['run_id']}", "",
             f"- **A** {a['run_id']}: {a['version'].get('label')} · {util.local_str(a['start_ms'])} · {util.one_line(a['prompt'] or a['args'], 90)}",
             f"- **B** {b['run_id']}: {b['version'].get('label')} · {util.local_str(b['start_ms'])} · {util.one_line(b['prompt'] or b['args'], 90)}",
             "", "## Metrics", "", table(["Metric", "A", "B"], [(m, fmt(m, x), fmt(m, y)) for m, x, y in cmp["metrics"]]),
             "## Checks", "", table(["Check", "A", "B"], [(c, x, y) for c, x, y in cmp["checks"] if x != y] or
                                     [("(all the same)", "", "")]),
             "## Skill files", "", f"- Only A read: {', '.join(cmp['resources'][0]) or '—'}",
             f"- Only B read: {', '.join(cmp['resources'][1]) or '—'}", "",
             "## What each run did (unified diff of actions)", "", "```diff", *cmp["diff"], "```", ""]
    return "\n".join(lines)


def render_markdown(rep):
    from .render_md import table
    t = rep["totals"]
    lines = [f"# Skill report — {rep['skill']}", "",
             f"{t['runs']} runs · {t['sessions']} sessions · {t['versions']} versions · {rep['scope']['label']} since "
             f"{rep['scope']['since']} · {util.fmt_usd(t['cost_usd'])} total", "", "## Insights", ""]
    lines += [f"- {n}" for n in rep["insights"]] + [""]
    lines += ["## Versions", "", table(
        ["Version", "Date", "Runs", "Median cost", "Median tools", "Median errors", "Median questions",
         "Median help", "Median duration"],
        [(v["label"], (v["date"] or "")[:10], v["runs"], util.fmt_usd(v["median"]["cost_usd"]), v["median"]["tool_calls"],
          v["median"]["tool_errors"], v["median"]["question_calls"], v["median"]["help_lookups"],
          util.fmt_duration(v["median"]["duration_ms"])) for v in rep["versions"]])]
    for v in rep["versions"]:
        if v.get("changes") and (v["changes"]["commits"] or v["changes"]["files"]):
            lines += [f"### Changes {v['changes']['from']} → {v['commit']}", ""]
            lines += [f"- `{c['short']}` {c['subject']}" for c in v["changes"]["commits"]]
            lines += [f"- {f['path']} (+{f['added']} −{f['removed']})" for f in v["changes"]["files"]] + [""]
    if rep["checks"]:
        lines += ["## Checks by version", "", table(
            ["Check"] + [v["commit"] or v["label"][:14] for v in rep["versions"]],
            [(c["id"],) + tuple(f"{v['checks'][c['id']]['pass']}/{v['checks'][c['id']]['pass'] + v['checks'][c['id']]['fail']}"
                                if v["checks"][c["id"]]["pass"] + v["checks"][c["id"]]["fail"] else "n/a"
                                for v in rep["versions"]) for c in rep["checks"]])]
    lines += _files_markdown(rep)
    lines += _interview_markdown(rep)
    lines += ["## Runs", "", table(
        ["Run", "Started", "Version", "How", "Turns", "Tools", "Errors", "mb/CLI", "Help", "Asked", "Created",
         "Cost", "Checks ✗", "Prompt"],
        [(r["run_id"], util.local_str(r["start_ms"], "%m-%d %H:%M"), r["commit"] or (r["version"] or "")[:12], r["mode"],
          r["turn_count"], r["tool_calls"], r["tool_errors"], r["cli_calls"], r["help_lookups"], r["question_calls"],
          r["objects_created"], util.fmt_usd(r["cost_usd"]), r["checks_failed"], util.one_line(r["args"] or r["prompt"], 60))
         for r in rep["runs"]])]
    if rep["failures"]:
        lines += ["## Failures", "", table(["Count", "Runs", "Failure"],
                                           [(f["count"], len(f["runs"]), f["signature"]) for f in rep["failures"][:25]])]
    return "\n".join(lines) + "\n"


def _files_markdown(rep):
    from .render_md import table
    vs, files = rep["versions"], rep.get("files") or []
    if not files or not vs:
        return []

    def cell(st):
        if st.get("in_version") is False:
            return "—"
        c = f"{st.get('shown', 0)}/{st.get('runs', 0)}"
        return c + (" Δ" if st.get("changed") else "")
    shown = [f for f in files if f["runs_shown"] or any(x.get("in_version") for x in f["per_version"].values())]
    out = ["## Skill files", "",
           "Runs shown at least one line of the file / runs of that version; Δ the version changed the file.", "",
           table(["File"] + [v["commit"] or v["label"][:14] for v in vs] + ["All"],
                 [(f["path"] if f["own"] else f"{f['owner']}:{f['path']}",) +
                  tuple(cell(f["per_version"].get(v["key"], {})) for v in vs) + (f"{f['runs_shown']}/{f['runs']}",)
                  for f in shown])]
    for v in vs:
        ex = (v.get("changes") or {}).get("exposure") or []
        if ex:
            out += [f"### Did the runs of {v['commit']} see what {v['changes']['from']} → {v['commit']} changed?", "",
                    table(["File", "+", "−", "Changed lines", "Saw all", "Saw some", "Saw none"],
                          [(e["path"], e["added"], e["removed"], ", ".join(f"{a}–{b}" if a != b else str(a)
                                                                           for a, b in e["ranges"][:5]),
                            f"{e['seen']}/{v['runs']}", e["partly"], e["not_seen"]) for e in ex])]
    out += ["### Never shown, by version", ""]
    out += [f"- {v['commit'] or v['label']} ({v['runs']} runs): " + (", ".join(v.get("never") or ()) or "none")
            for v in vs] + [""]
    return out


def _interview_markdown(rep):
    from .render_md import table
    iv = rep.get("interview") or {}
    if not (iv.get("summary") or {}).get("total"):
        return []
    vs = rep["versions"]
    out = ["## Interview", "", table(
        ["Version", "Runs", "Questions", "In prose", "Recommended taken", "Typed", "Empty", "Median wait"],
        [(v["commit"] or v["label"][:14], v["runs"], v["interview"]["asked"], v["interview"]["prose"],
          f"{v['interview']['recommended_picked']}/{v['interview']['recommended_offered']}" if v["interview"]["recommended_offered"] else "—",
          v["interview"]["typed"], v["interview"]["no_preference"] + v["interview"]["declined"] + v["interview"]["unanswered"],
          util.fmt_duration(v["interview"]["wait_p50_ms"]) if v["interview"]["wait_p50_ms"] is not None else "—")
         for v in vs]),
        "### Question catalog", "", table(
        ["Topic", "Asked", "In prose", "Runs", "Recommended taken", "Typed", "Asked again", "Median wait", "Answers given"],
        [(c["label"], c["asked"], c["prose"], c["runs"], f"{c['recommended']}/{c['offered']}" if c["offered"] else "—",
          c["typed"], c["reasked"], util.fmt_duration(c["wait_p50_ms"]) if c["wait_p50_ms"] is not None else "—",
          "; ".join(f"{k} ×{n}" for k, n in list(c["answers"].items())[:3])) for c in iv["catalog"]])]
    if iv.get("typed"):
        out += ["### Where the options fell short", ""]
        out += [f"- {t['run_id']} ({t['topic_label']}): “{util.one_line(t['question'], 100)}” — offered "
                f"{' / '.join(t['options'])}; typed “{t['typed']}”" for t in iv["typed"]] + [""]
    return out


def _csv(path, rows):
    if not rows:
        Path(path).write_text("", encoding="utf-8")
        return
    cols = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow([json.dumps(r.get(c), sort_keys=True, default=str) if isinstance(r.get(c), (list, dict))
                        else ("" if r.get(c) is None else r.get(c)) for c in cols])


def run_skill_report(name, claude_dir=None, project=None, since="30d", limit=500, out_dir=None,
                     formats=("json", "md", "html", "csv"), redact=True, pricing=None, check_files=(), sources=(),
                     now_ms=None):
    runs, meta = collect_runs(name, claude_dir, project, since, limit, redact, pricing, check_files, sources, now_ms)
    rep = build_report(name, runs, meta, since, project, check_files=check_files)
    out = Path(out_dir) if out_dir else (default_root() / "_skills" /
                                         f"{_slug(rep['skill'])}_{_slug(rep['scope']['label'])}_{since}_"
                                         f"{datetime.now().strftime('%Y-%m-%d')}")
    out.mkdir(parents=True, exist_ok=True)
    paths = {}
    if "html" in formats:
        paths["dashboard"] = render_html.write(rep, out / "skill.html", app="skill.js",
                                               title=f"{rep['skill']} — skill report")
    if "md" in formats:
        (out / "skill.md").write_text(render_markdown(rep), encoding="utf-8")
        paths["report"] = str(out / "skill.md")
    if "json" in formats:
        with open(out / "skill.json", "w", encoding="utf-8") as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=1, default=str)
        paths["json"] = str(out / "skill.json")
    if "csv" in formats:
        d = out / "csv"
        d.mkdir(exist_ok=True)
        _csv(d / "runs.csv", rep["runs"])
        _csv(d / "versions.csv", [{k: v for k, v in row.items() if k not in ("resources", "cli", "checks", "files",
                                                                               "inventory", "changes")}
                                  for row in rep["versions"]])
        _csv(d / "checks.csv", [dict(run_id=r["run_id"], version=r["commit"] or r["version"], **r["checks"])
                                for r in rep["runs"]])
        _csv(d / "failures.csv", rep["failures"])
        _csv(d / "skill_files.csv", skill_file_rows(runs))
        _csv(d / "questions.csv", [{k: q.get(k) for k in ("qid", "run_id", "commit", "t", "dt", "kind", "form", "topic",
                                                          "topic_label", "header", "question", "options", "multi",
                                                          "recommended_label", "outcome", "answer", "typed", "reply",
                                                          "notes", "feedback", "wait_ms", "flags", "before_create",
                                                          "reask_of")}
                                   for q in rep["interview"]["questions"]])
        paths["csv"] = str(d)
    summary = render_markdown(rep).split("## Checks by version")[0].split("## Runs")[0].rstrip() + "\n"
    if paths:
        summary += "\n## Files written\n" + "\n".join(f"- {k}: {v}" for k, v in paths.items()) + "\n"
    (out / "summary.md").write_text(summary, encoding="utf-8")
    paths["summary"] = str(out / "summary.md")
    return {"out_dir": str(out), "paths": paths, "report": rep, "runs": runs, "summary": summary}
