"""One skill across many sessions: every run, grouped by the version that ran, for skill development.

    session-analytics skill rde --since 30d --all

Runs come from skillruns.build_runs (one per invocation, including the follow-up turns it steered).
Versions come from versions.resolve (git commit of the skill source). For each version: how many runs,
median cost / tool calls / errors / questions / duration, check pass rates, which skill files were read,
which CLI commands ran and failed, and what changed in git since the previous version.
"""

from __future__ import annotations

import csv
import difflib
import json
import re
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from . import __version__, locate, render_html, util, versions
from .analyze import analyze
from .export import _slug, default_root
from .parse import parse_session
from .pricing import Pricing
from .redact import Redactor
from .rollup import parse_since
from .skillruns import skill_key

SCHEMA = "convo-analysis/skill-v1"
RUN_METRICS = ("cost_usd", "requests", "tool_calls", "tool_errors", "error_rate", "cli_calls", "help_lookups",
               "retries_after_error", "question_calls", "objects_created", "duration_ms", "active_ms", "turn_count",
               "follow_up_turns", "context_peak", "output_tokens", "checks_failed")


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
            res = [r.split(":", 1)[1] for r in st.get("res") or () if r.split(":", 1)[0] == run["skill"]]
            if name == "Bash":
                mine += [f"$ {sig}" for sig in st.get("sigs") or ()]
                mine += [f"read {r}" for r in res]
                if not mine and st.get("prog"):
                    mine.append(f"$ {st['prog']}")
            elif res:
                mine += [f"read {r}" for r in res]
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
    needles = [n.encode() for n in (f'/skills/{key}/', f'"skill":"{key}"', f'<command-name>/{key}<',
                                   f'<command-name>{key}<', f'"attributionSkill":"{key}"', f'"commandName":"{key}"',
                                   f':{key}"')]
    pricing = pricing or Pricing()
    R = Redactor(redact)
    runs, scanned = [], 0
    for f in files:
        try:
            data = f.read_bytes()
        except OSError:
            continue
        if not any(n in data for n in needles):
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


def build_report(name, runs, meta, since, project):
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
            row["changes"] = {"from": prev.get("commit"), "commits": src.log_between(prev["sha"], row["sha"]),
                              "files": src.diffstat(prev["sha"], row["sha"])}
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

    run_rows = []
    for r in runs:
        run_rows.append({k: r.get(k) for k in (
            "run_id", "skill", "session_id", "session_title", "project", "mode", "via", "start_ms", "end_reason", "args",
            "prompt", "turn_count", "follow_up_turns", "requests", "attributed_requests", "tool_calls", "tool_errors",
            "error_rate", "cli_calls", "help_lookups", "retries_after_error", "question_calls", "questions_asked",
            "objects_created", "cost_usd", "attributed_cost_usd", "duration_ms", "active_ms", "context_start",
            "context_end", "context_peak", "output_tokens", "cache_hit_ratio", "playbooks", "references", "missing_expected",
            "not_named_by_playbooks", "checks_passed", "checks_failed", "denials", "interrupted", "subagents", "models")}
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
        "runs": run_rows,
        "failures": failure_rows,
        "details": {r["run_id"]: {k: r.get(k) for k in (
            "resources", "cli", "questions", "objects", "files_written", "final_message", "errors", "checks", "turns",
            "nested_skills", "failed_invocations", "actions", "steps", "version", "expected_by_playbooks",
            "tools", "error_categories", "transcript")} for r in runs},
    }
    report["insights"] = _insights(report)
    return report


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
                         ("question_calls", "questions"), ("help_lookups", "help lookups"), ("duration_ms", "duration")):
            x, y = a["median"].get(m), b["median"].get(m)
            if x and y and (y / x >= 1.5 or y / x <= 0.67):
                fmt = util.fmt_usd if m == "cost_usd" else (util.fmt_duration if m == "duration_ms" else str)
                notes.append(f"Median {label} per run moved {fmt(x)} → {fmt(y)} from {a['commit'] or a['label']} to "
                             f"{b['commit'] or b['label']} (n={a['runs']} → {b['runs']}).")
    worst = sorted(((c["id"], sum(v["checks"].get(c["id"], {}).get("fail", 0) for v in vs)) for c in rep["checks"]),
                   key=lambda x: -x[1])
    if worst and worst[0][1]:
        notes.append("Most failed checks: " + ", ".join(f"`{i}` ×{n}" for i, n in worst[:4] if n) + ".")
    if rep["failures"]:
        f = rep["failures"][0]
        notes.append(f"Most frequent tool failure: {f['signature']} (×{f['count']} in {len(f['runs'])} runs).")
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
    rep = build_report(name, runs, meta, since, project)
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
        _csv(d / "versions.csv", [{k: v for k, v in row.items() if k not in ("resources", "cli", "checks")}
                                  for row in rep["versions"]])
        _csv(d / "checks.csv", [dict(run_id=r["run_id"], version=r["commit"] or r["version"], **r["checks"])
                                for r in rep["runs"]])
        _csv(d / "failures.csv", rep["failures"])
        paths["csv"] = str(d)
    summary = render_markdown(rep).split("## Checks by version")[0].split("## Runs")[0].rstrip() + "\n"
    if paths:
        summary += "\n## Files written\n" + "\n".join(f"- {k}: {v}" for k, v in paths.items()) + "\n"
    (out / "summary.md").write_text(summary, encoding="utf-8")
    paths["summary"] = str(out / "summary.md")
    return {"out_dir": str(out), "paths": paths, "report": rep, "runs": runs, "summary": summary}
