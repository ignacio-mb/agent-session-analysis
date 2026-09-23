"""Aggregate analytics across many sessions.

Resumed and continued sessions start with a copy of the earlier conversation, so
the same API response can sit in several transcripts (14% of requests on the
machine this was built on). Every request is therefore counted once, owned by the
session that did not inherit it (else the earliest one holding it), and only when
it falls inside the requested time window.
"""

from __future__ import annotations

import csv
import json
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from . import __version__, locate, render_html, util
from .analyze import analyze, categorize_error, primary_program, usage_totals
from .export import _slug, default_root
from .parse import parse_session
from .pricing import COMPONENTS, Pricing
from .redact import Redactor

SCHEMA = "convo-analysis/rollup-v1"
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def parse_since(since, now_ms):
    if not since or since == "all":
        return 0.0
    m = re.fullmatch(r"(\d+)\s*([mhdw])", since.strip().lower())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return now_ms - n * {"m": 60e3, "h": 3600e3, "d": 86400e3, "w": 7 * 86400e3}[unit]
    try:
        return datetime.strptime(since.strip(), "%Y-%m-%d").timestamp() * 1000
    except ValueError:
        raise ValueError(f"--since must look like 7d, 24h, 2w or YYYY-MM-DD, not {since!r}") from None


def session_row(a):
    s, t = a["session"], a["totals"]
    return {
        "id": s["id"], "title": s["title"], "project": s["project_name"], "cwd": s["cwd"],
        "start_ms": s["start_ms"], "end_ms": s["end_ms"], "wall_ms": s["wall_ms"], "active_ms": t["active_ms"],
        "turns": t["turns"], "prompts": t["prompts"], "requests": t["api_requests"], "tool_calls": t["tool_calls"],
        "tool_errors": t["tool_errors"], "skill_invocations": t["skills_invoked"],
        "skills": sorted({i["canonical"] or i["name"] for i in a["skills"]["invocations"]}),
        "subagents": t["subagents"] + t["workflow_agents"], "cost_usd": t["estimated_cost_usd"],
        "own_cost_usd": t["own_cost_usd"], "inherited_cost_usd": t["inherited_cost_usd"],
        "output_tokens": t["output_tokens"], "total_tokens": t["total_tokens"], "cache_hit_ratio": t["cache_hit_ratio"],
        "models": [m["model"] for m in s["models"]], "lines_added": t["lines_added"],
        "lines_removed": t["lines_removed"], "commits": t["commits"], "pull_requests": t["pull_requests"],
        "compactions": t["compactions"], "interruptions": t["interruptions"], "api_errors": t["api_errors"],
        "continues": [c["session_id"] for c in a["lineage"]["continues"]], "live": s["live"],
    }


def quick_rows(files, pricing):
    rows = []
    for f in files:
        try:
            a = analyze(parse_session(f), pricing, redactor=Redactor(True))
        except Exception as exc:  # one unreadable transcript must not hide the rest
            rows.append({"id": Path(f).stem, "title": f"(unreadable: {type(exc).__name__})", "start_ms": None,
                         "wall_ms": None, "turns": 0, "tool_calls": 0, "cost_usd": None})
            continue
        rows.append(session_row(a))
    return rows


def _day(ms):
    return datetime.fromtimestamp(ms / 1000.0).strftime("%Y-%m-%d")


def run_rollup(claude_dir=None, project=None, since="30d", limit=500, out_dir=None, formats=("json", "md", "html", "csv"),
               redact=True, pricing=None, now_ms=None):
    pricing = pricing or Pricing()
    now = now_ms if now_ms is not None else time.time() * 1000
    cutoff = parse_since(since, now)
    cdir = locate.claude_dir(claude_dir)
    files = [f for f in locate.iter_transcripts(cdir, project=project) if f.stat().st_mtime * 1000 >= cutoff][:limit]
    R = Redactor(redact)
    parsed = []
    for f in files:
        try:
            s = parse_session(f)
        except Exception as exc:
            print(f"(skipping {f.name}: {type(exc).__name__}: {exc})")
            continue
        parsed.append((s, analyze(s, pricing, redactor=R, now_ms=now)))
    parsed.sort(key=lambda x: x[0].first_ts or 0)

    claims = defaultdict(list)
    for s, _ in parsed:
        for r in s.requests.values():
            claims[r.key].append((s.session_id, r.inherited))
    owner = {}
    for k, lst in claims.items():
        own = [sid for sid, inh in lst if not inh]
        owner[k] = own[0] if own else lst[0][0]
    duplicates = sum(len(v) - 1 for v in claims.values() if len(v) > 1)

    in_window = lambda ts: ts is not None and ts >= cutoff  # noqa: E731
    by_model = defaultdict(list)
    by_day = defaultdict(lambda: {"sessions": set(), "requests": 0, "tool_calls": 0, "cost": 0.0, "output": 0,
                                  "prompts": 0, "active_ms": 0.0, "tool_errors": 0})
    heat = [[0] * 24 for _ in range(7)]
    by_project = defaultdict(lambda: {"sessions": 0, "requests": 0, "tool_calls": 0, "cost": 0.0, "turns": 0})
    tools = defaultdict(lambda: {"calls": 0, "errors": 0, "denied": 0, "sessions": set(), "_d": []})
    skills = defaultdict(lambda: {"invocations": 0, "by_mode": Counter(), "sessions": set(), "cost": 0.0,
                                  "requests": 0, "tool_calls": 0})
    commands, programs, mcp, err_cats = Counter(), Counter(), Counter(), Counter()
    agent_types = defaultdict(lambda: {"agents": 0, "requests": 0, "cost": 0.0})
    entry, versions = Counter(), Counter()
    rows = []
    totals = Counter()
    active_by_session = {}

    for s, a in parsed:
        sid = s.session_id
        own_reqs = [r for r in s.requests.values() if r.model != "<synthetic>" and owner.get(r.key) == sid
                    and in_window(r.ts_first)]
        own_calls = [c for c in s.tool_calls.values() if in_window(c.ts_call or c.ts_result) and
                     (c.request_key is None or owner.get(c.request_key) == sid)]
        own_turns = [t for t in s.turns if not t.inherited and in_window(t.ts_start)]
        own_skills = [i for i in s.skills if not i.inherited and in_window(i.ts)]
        if not (own_reqs or own_turns or own_calls):
            continue
        u = usage_totals(own_reqs)
        proj = a["session"]["project_name"]
        active = sum((t.reported_duration_ms if t.reported_duration_ms is not None else
                      ((t.ts_end or t.ts_start) - t.ts_start)) for t in own_turns if t.ts_start is not None)
        active_by_session[sid] = active
        row = session_row(a)
        row.update({"window_requests": u["requests"], "window_cost_usd": round(u["cost"], 6),
                    "window_output_tokens": u["output"], "window_tool_calls": len(own_calls),
                    "window_turns": len(own_turns), "window_active_ms": active})
        rows.append(row)
        totals["sessions"] += 1
        totals["turns"] += len(own_turns)
        totals["prompts"] += sum(1 for t in own_turns if t.trigger == "prompt" and t.origin != "task-notification")
        totals["requests"] += u["requests"]
        totals["tool_calls"] += len(own_calls)
        totals["tool_errors"] += sum(1 for c in own_calls if c.status == "error")
        totals["skill_invocations"] += len(own_skills)
        totals["active_ms"] += active
        for k in ("input", "output", "cache_read", "cache_write", "thinking"):
            totals[k + "_tokens"] += u[k]
        totals["cost_usd"] += u["cost"]
        for c in own_calls:
            f = c.facts
            if c.status == "ok" and c.name in ("Edit", "Write", "MultiEdit"):
                totals["lines_added"] += f.get("added") or 0
                totals["lines_removed"] += f.get("removed") or 0
            g = f.get("git") if c.name == "Bash" else None
            if isinstance(g, dict):
                totals["commits"] += 1 if "commit" in g else 0
                totals["pr_operations"] += 1 if "pr" in g else 0
        owned_sources = {r.source for r in own_reqs}
        for idx, src in enumerate(s.sources):
            if src.scope != "main" and idx in owned_sources:
                totals["subagents" if src.scope == "subagent" else "workflow_agents"] += 1
                t = (src.meta or {}).get("agentType") or ("workflow-agent" if src.scope == "workflow" else "(unknown)")
                agent_types[t]["agents"] += 1
        by_project[proj]["sessions"] += 1
        by_project[proj]["requests"] += u["requests"]
        by_project[proj]["tool_calls"] += len(own_calls)
        by_project[proj]["cost"] += u["cost"]
        by_project[proj]["turns"] += len(own_turns)
        entry.update(s.entrypoints.keys())
        versions.update(s.versions.keys())
        for r in own_reqs:
            by_model[r.model].append(r)
            d = by_day[_day(r.ts_first)]
            d["sessions"].add(sid)
            d["requests"] += 1
            d["cost"] += r.cost["total"] if r.cost else 0.0
            d["output"] += r.output_tokens
            lt = datetime.fromtimestamp(r.ts_first / 1000.0)
            heat[lt.weekday()][lt.hour] += 1
            if r.attribution_skill:
                sk = skills[r.attribution_skill]
                sk["cost"] += r.cost["total"] if r.cost else 0.0
                sk["requests"] += 1
            if r.scope != "main":
                t = r.attribution_agent or "(unknown)"
                agent_types[t]["requests"] += 1
                agent_types[t]["cost"] += r.cost["total"] if r.cost else 0.0
        for t in own_turns:
            if t.trigger == "prompt" and t.origin != "task-notification":
                by_day[_day(t.ts_start)]["prompts"] += 1
        for c in own_calls:
            ts = c.ts_call or c.ts_result
            d = by_day[_day(ts)]
            d["tool_calls"] += 1
            d["sessions"].add(sid)
            tl = tools[c.name]
            tl["calls"] += 1
            tl["sessions"].add(sid)
            if c.status == "error":
                tl["errors"] += 1
                d["tool_errors"] += 1
                err_cats[categorize_error(c.result_preview)] += 1
            if c.status == "denied":
                tl["denied"] += 1
            if c.duration_ms is not None:
                tl["_d"].append(c.duration_ms)
            if c.attribution_skill:
                skills[c.attribution_skill]["tool_calls"] += 1
            if c.name == "Bash":
                p0, _ = primary_program(c.input.get("command"))
                if p0:
                    programs[p0] += 1
            if c.name.startswith("mcp__"):
                mcp[c.name.split("__")[1]] += 1
        for t in own_turns:
            d = by_day[_day(t.ts_start)]
            d["active_ms"] += (t.reported_duration_ms if t.reported_duration_ms is not None else
                               ((t.ts_end or t.ts_start) - t.ts_start))
        for inv in own_skills:
            key = inv.canonical or inv.name
            sk = skills[key]
            sk["invocations"] += 1
            sk["by_mode"][inv.mode] += 1
            sk["sessions"].add(sid)
        for cmd in s.commands:
            if not cmd.get("is_skill") and not cmd.get("inherited") and in_window(cmd.get("ts")) \
                    and cmd.get("invoked_by") == "user":
                commands[cmd["name"]] += 1

    model_rows = {m: usage_totals(v) for m, v in sorted(by_model.items(), key=lambda kv: -len(kv[1]))}
    for v in model_rows.values():
        v["cost"] = round(v["cost"], 6)
    day_rows = [{"day": k, "sessions": len(v["sessions"]), "requests": v["requests"], "tool_calls": v["tool_calls"],
                 "tool_errors": v["tool_errors"], "prompts": v["prompts"], "output_tokens": v["output"],
                 "cost_usd": round(v["cost"], 6), "active_ms": v["active_ms"]} for k, v in sorted(by_day.items())]
    tool_rows = []
    for name, v in sorted(tools.items(), key=lambda kv: -kv[1]["calls"]):
        tool_rows.append({"name": name, "calls": v["calls"], "errors": v["errors"], "denied": v["denied"],
                          "sessions": len(v["sessions"]), "error_rate": util.ratio(v["errors"], v["calls"]),
                          "p50_ms": util.percentile(v["_d"], 50), "p90_ms": util.percentile(v["_d"], 90)})
    skill_rows = []
    for name, v in sorted(skills.items(), key=lambda kv: (-kv[1]["cost"], -kv[1]["invocations"])):
        skill_rows.append({"skill": name, "invocations": v["invocations"], "by_mode": dict(v["by_mode"]),
                           "sessions": len(v["sessions"]), "attributed_requests": v["requests"],
                           "attributed_tool_calls": v["tool_calls"], "attributed_cost_usd": round(v["cost"], 6)})
    rows.sort(key=lambda r: -(r["start_ms"] or 0))
    tot = dict(totals)
    tot["cost_usd"] = round(tot.get("cost_usd", 0.0), 6)
    tot["cache_hit_ratio"] = util.ratio(tot.get("cache_read_tokens", 0), tot.get("input_tokens", 0)
                                        + tot.get("cache_read_tokens", 0) + tot.get("cache_write_tokens", 0))
    tot["distinct_skills"] = sum(1 for r in skill_rows if r["invocations"])
    tot["duplicate_requests_removed"] = duplicates
    result = {
        "schema": SCHEMA,
        "generator": {"name": "convo-analysis", "version": __version__, "generated_at": util.iso(now),
                      "redaction": redact},
        "scope": {"project": project, "label": os.path.basename(str(project).rstrip("/")) if project else "all projects",
                  "since": since, "cutoff": util.iso(cutoff) if cutoff else None, "until": util.iso(now),
                  "transcripts_scanned": len(files)},
        "totals": tot,
        "sessions": rows,
        "by_day": day_rows,
        "by_project": {k: dict(v, cost=round(v["cost"], 6)) for k, v in sorted(by_project.items(), key=lambda kv: -kv[1]["cost"])},
        "by_model": model_rows,
        "tools": tool_rows,
        "skills": skill_rows,
        "slash_commands": dict(commands.most_common()),
        "agent_types": {k: dict(v, cost=round(v["cost"], 6)) for k, v in sorted(agent_types.items(), key=lambda kv: -kv[1]["cost"])},
        "error_categories": dict(err_cats.most_common()),
        "programs": dict(programs.most_common(30)),
        "mcp_servers": dict(mcp.most_common()),
        "heatmap": {"weekdays": WEEKDAYS, "requests": heat},
        "entrypoints": dict(entry), "claude_code_versions": sorted(versions),
        "cost_components": {c: round(sum(r.cost[c] for rs in by_model.values() for r in rs if r.cost), 6) for c in COMPONENTS},
        "notes": [
            f"Requests are de-duplicated across transcripts ({duplicates} copies from resumed/continued sessions removed) "
            "and counted only inside the window.",
            "Costs are list-price estimates of the API-equivalent spend.",
        ],
    }
    result["insights"] = _insights(result)

    out = Path(out_dir) if out_dir else (default_root() / "_rollups" /
                                         f"{_slug(result['scope']['label'])}_{since}_{datetime.now().strftime('%Y-%m-%d')}")
    out.mkdir(parents=True, exist_ok=True)
    paths = {}
    if "html" in formats:
        paths["dashboard"] = render_html.write(result, out / "rollup.html", app="rollup.js",
                                               title=f"Claude sessions — {result['scope']['label']} — last {since}")
    if "md" in formats:
        (out / "rollup.md").write_text(render_markdown(result), encoding="utf-8")
        paths["report"] = str(out / "rollup.md")
    if "json" in formats:
        with open(out / "rollup.json", "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=1, default=str)
        paths["json"] = str(out / "rollup.json")
    if "csv" in formats:
        csv_dir = out / "csv"
        csv_dir.mkdir(exist_ok=True)
        _csv(csv_dir / "sessions.csv", rows)
        _csv(csv_dir / "daily.csv", day_rows)
        _csv(csv_dir / "tools.csv", tool_rows)
        _csv(csv_dir / "skills.csv", skill_rows)
        _csv(csv_dir / "models.csv", [dict(model=m, **{k: v for k, v in u.items() if k != "cost_components"})
                                      for m, u in model_rows.items()])
        paths["csv"] = str(csv_dir)
    summary = render_summary(result, paths)
    (out / "summary.md").write_text(summary, encoding="utf-8")
    paths["summary"] = str(out / "summary.md")
    return {"out_dir": str(out), "paths": paths, "rollup": result, "summary": summary}


def _csv(path, rows):
    if not rows:
        Path(path).write_text("", encoding="utf-8")
        return
    cols = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow([json.dumps(r.get(c), sort_keys=True) if isinstance(r.get(c), (list, dict))
                        else ("" if r.get(c) is None else r.get(c)) for c in cols])


def _insights(r):
    t = r["totals"]
    notes = []
    if t.get("sessions"):
        notes.append(f"{t['sessions']} sessions, {t.get('turns', 0)} turns, {t.get('requests', 0):,} API requests, "
                     f"{util.fmt_usd(t.get('cost_usd'))} estimated.")
    if r["by_day"]:
        busiest = max(r["by_day"], key=lambda d: d["cost_usd"])
        notes.append(f"Busiest day: {busiest['day']} ({util.fmt_usd(busiest['cost_usd'])}, {busiest['requests']} requests).")
    if r["skills"]:
        s0 = r["skills"][0]
        notes.append(f"Most expensive skill: {s0['skill']} ({util.fmt_usd(s0['attributed_cost_usd'])} attributed, "
                     f"{s0['invocations']} invocations in {s0['sessions']} sessions).")
    if r["tools"]:
        worst = max((x for x in r["tools"] if x["calls"] >= 20), key=lambda x: x["error_rate"] or 0, default=None)
        if worst and worst["errors"]:
            notes.append(f"Highest tool error rate: {worst['name']} {util.fmt_pct(worst['error_rate'])} "
                         f"({worst['errors']} of {worst['calls']}).")
    if t.get("duplicate_requests_removed"):
        notes.append(f"{t['duplicate_requests_removed']} duplicated requests from resumed sessions were counted once.")
    return notes


def render_summary(r, paths=None):
    t, sc = r["totals"], r["scope"]
    lines = [f"# Claude Code sessions — {sc['label']} since {sc['since']}", "",
             f"- **Sessions:** {t.get('sessions', 0)} · turns {t.get('turns', 0)} · prompts {t.get('prompts', 0)}",
             f"- **API requests:** {t.get('requests', 0):,} · tool calls {t.get('tool_calls', 0):,} "
             f"({t.get('tool_errors', 0)} errors) · skills {t.get('skill_invocations', 0)} invocations",
             f"- **Tokens:** {util.fmt_tokens(t.get('output_tokens', 0))} output · "
             f"{util.fmt_tokens(t.get('cache_read_tokens', 0))} cache read · hit {util.fmt_pct(t.get('cache_hit_ratio'))}",
             f"- **Estimated cost:** {util.fmt_usd(t.get('cost_usd'))} · active {util.fmt_duration(t.get('active_ms'))}",
             f"- **Code:** +{t.get('lines_added', 0):,} / −{t.get('lines_removed', 0):,} lines · {t.get('commits', 0)} commits",
             ""]
    lines += ["## Insights"] + [f"- {n}" for n in r["insights"]] + [""]
    if r["skills"]:
        lines += ["## Skills", ""] + [f"- {s['skill']}: {s['invocations']} invocation{'' if s['invocations'] == 1 else 's'}, "
                                      f"{util.fmt_usd(s['attributed_cost_usd'])}"
                                      for s in r["skills"][:10]] + [""]
    if r["tools"]:
        lines += ["## Tools", "", ", ".join(f"{x['name']} ×{x['calls']}" for x in r["tools"][:12]), ""]
    if paths:
        lines += ["## Files written"] + [f"- {k}: {v}" for k, v in paths.items()]
    return "\n".join(lines) + "\n"


def render_markdown(r):
    from .render_md import table
    out = [render_summary(r).rstrip(), ""]
    out += ["## Sessions", "", table(
        ["Started", "Project", "Title", "Turns", "Requests", "Tools", "Skills", "Cost (window)", "Id"],
        [(util.local_str(x["start_ms"], "%m-%d %H:%M"), x["project"], util.one_line(x["title"], 60), x["window_turns"],
          x["window_requests"], x["window_tool_calls"], ", ".join(x["skills"][:4]), util.fmt_usd(x["window_cost_usd"]),
          x["id"][:8]) for x in r["sessions"]])]
    out += ["## By day", "", table(["Day", "Sessions", "Prompts", "Requests", "Tool calls", "Errors", "Output", "Cost", "Active"],
                                   [(d["day"], d["sessions"], d["prompts"], d["requests"], d["tool_calls"], d["tool_errors"],
                                     util.fmt_tokens(d["output_tokens"]), util.fmt_usd(d["cost_usd"]),
                                     util.fmt_duration(d["active_ms"])) for d in r["by_day"]])]
    out += ["## By model", "", table(["Model", "Requests", "Output", "Cache read", "Hit", "Cost"],
                                     [(m, u["requests"], util.fmt_tokens(u["output"]), util.fmt_tokens(u["cache_read"]),
                                       util.fmt_pct(u["cache_hit_ratio"]), util.fmt_usd(u["cost"]))
                                      for m, u in r["by_model"].items()])]
    out += ["## Skills", "", table(["Skill", "Invocations", "Modes", "Sessions", "Requests", "Tool calls", "Cost"],
                                   [(s["skill"], s["invocations"], ", ".join(f"{k} ×{v}" for k, v in s["by_mode"].items()),
                                     s["sessions"], s["attributed_requests"], s["attributed_tool_calls"],
                                     util.fmt_usd(s["attributed_cost_usd"])) for s in r["skills"]])]
    out += ["## Tools", "", table(["Tool", "Calls", "Errors", "Error rate", "Sessions", "p50", "p90"],
                                  [(x["name"], x["calls"], x["errors"], util.fmt_pct(x["error_rate"]), x["sessions"],
                                    util.fmt_duration(x["p50_ms"]), util.fmt_duration(x["p90_ms"])) for x in r["tools"]])]
    out += ["## By project", "", table(["Project", "Sessions", "Turns", "Requests", "Tool calls", "Cost"],
                                       [(k, v["sessions"], v["turns"], v["requests"], v["tool_calls"], util.fmt_usd(v["cost"]))
                                        for k, v in r["by_project"].items()])]
    if r["slash_commands"]:
        out += ["## Slash commands", "", ", ".join(f"/{k} ×{v}" for k, v in r["slash_commands"].items()), ""]
    if r["error_categories"]:
        out += ["## Tool error categories", "", ", ".join(f"{k} ×{v}" for k, v in r["error_categories"].items()), ""]
    out += ["_" + " ".join(r["notes"]) + "_", ""]
    return "\n".join(out)
