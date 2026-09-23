"""Markdown views of the analytics dict: a full report and a short summary."""

from __future__ import annotations

from .util import fmt_duration as dur
from .util import fmt_pct as pct
from .util import fmt_tokens as tok
from .util import fmt_usd as usd
from .util import local_str, one_line, parse_ts


def _cell(v):
    if v is None or v == "":
        return "—"
    s = str(v).replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    return s


def table(headers, rows, align=None):
    if not rows:
        return "_none_\n"
    align = align or ["---"] * len(headers)
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join(align) + " |"]
    for r in rows:
        out.append("| " + " | ".join(_cell(c) for c in r) + " |")
    return "\n".join(out) + "\n"


def _t(iso):
    return local_str(parse_ts(iso), "%m-%d %H:%M:%S") if iso else "—"


def _num(n):
    return "—" if n is None else f"{n:,}"


def _plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def _kv(d, n=None, fmt=None):
    items = list(d.items())[: n or None]
    return ", ".join(f"{k} ×{fmt(v) if fmt else v}" for k, v in items) or "—"


def headline(a):
    t, tim = a["totals"], a["timing"]
    rep = a.get("reported")
    cost = usd(t["estimated_cost_usd"])
    if rep and rep.get("total_cost_usd") is not None:
        cost += f" (Claude Code reported {usd(rep['total_cost_usd'])})"
    main = a["tokens"]["by_scope"].get("main", {}).get("requests", 0)
    return [
        ("Turns / prompts", f"{t['turns']} / {t['prompts']}"),
        ("API requests", f"{t['api_requests']:,} (main {main:,} · subagents {t['api_requests'] - main:,})"),
        ("Tool calls", f"{t['tool_calls']:,} across {t['distinct_tools']} tools "
                       f"({t['tool_errors']} errors, {t['tool_denials']} denied)"),
        ("Skills", f"{t['skills_invoked']} invocations of {t['distinct_skills']} skills"
                   f" · {t['slash_commands']} built-in slash commands"),
        ("Subagents / workflows", f"{t['subagents']} subagents · {t['workflow_runs']} workflow runs "
                                  f"({t['workflow_agents']} workflow agents)"),
        ("Tokens", f"{tok(t['total_tokens'])} total · {tok(t['output_tokens'])} output · "
                   f"{tok(t['cache_read_tokens'])} cache read · cache hit {pct(t['cache_hit_ratio'])}"),
        ("Estimated cost", cost),
        ("Peak context", tok(t["peak_context_tokens"])),
        ("Time", f"{dur(tim['active_ms'])} active of {dur(tim['wall_ms'])} wall · model {dur(tim['model_ms'])}"
                 f" · tools {dur(tim['tool_wall_ms'])}"),
        ("Files", f"{t['files_read']} read · {t['files_modified']} modified · "
                  f"+{_num(t['lines_added'])} / −{_num(t['lines_removed'])} lines"),
        ("Shell / web", f"{t['shell_commands']} shell commands · {t['web_fetches']} fetches · "
                        f"{t['web_searches']} searches"),
        ("Git", f"{t['commits']} commits · {t['pull_requests']} PRs"),
        ("Interruptions / compactions / API errors",
         f"{t['interruptions']} / {t['compactions']} / {t['api_errors']}"),
    ]


def _meta_line(a):
    s = a["session"]
    branches = ", ".join(b["branch"] for b in s["git_branches"]) or "—"
    models = ", ".join(f"{m['name'] or m['model']} ({m['requests']})" for m in s["models"]) or "—"
    live = " · **live snapshot**" if s["live"] else ""
    return (f"`{s['id']}` · **{s['project_name']}** · branch {branches} · "
            f"{_t(s['start'])} → {_t(s['end'])} ({dur(s['wall_ms'])}){live}\n\n"
            f"Claude Code {', '.join(s['claude_code_versions']) or '—'} via "
            f"{', '.join(s['entrypoints']) or '—'} · models: {models}")


def summary(a, paths=None):
    """The short version printed by the CLI and read by the skill."""
    s = a["session"]
    lines = [f"# {s['title']}", "", _meta_line(a), ""]
    lines += [f"- **{k}:** {v}" for k, v in headline(a)]
    if a["insights"]:
        lines += ["", "## Insights"]
        lines += [f"- {'⚠️ ' if i['level'] == 'warn' else ''}{i['text']}" for i in a["insights"]]
    sk = a["skills"]
    if sk["invocations"]:
        lines += ["", "## Skills invoked"]
        for inv in sk["invocations"][:20]:
            how = {"model": "by Claude (Skill tool)", "user": "by you (/slash)", "harness": "by Claude Code"}.get(
                inv["mode"], inv["mode"])
            res = "" if inv["success"] is not False else " — **failed**"
            args = f" `{one_line(inv['args'], 60)}`" if inv["args"] else ""
            lines.append(f"- turn {inv['turn']}: **{inv['canonical'] or inv['name']}** {how}{args}"
                         f" [{inv['source']}]{res}")
        attributed = [p for p in sk["per_skill"] if p["attributed_requests"]]
        if attributed:
            lines.append("- attributed spend: " + ", ".join(
                f"{p['skill']} {usd(p['cost_usd'])} ({_plural(p['attributed_tool_calls'], 'tool call')})"
                for p in attributed[:6]))
    tools = a["tools"]["by_tool"][:10]
    if tools:
        lines += ["", "## Top tools"]
        lines.append(", ".join(f"{t['name']} ×{t['calls']}" + (f" ({t['error']} err)" if t["error"] else "")
                               for t in tools))
    if paths:
        lines += ["", "## Files written"]
        lines += [f"- {k}: {v}" for k, v in paths.items()]
    return "\n".join(lines) + "\n"


def report(a):
    s = a["session"]
    out = [f"# Session report — {s['title']}", "", _meta_line(a), ""]

    out += ["## At a glance", "", table(["Metric", "Value"], headline(a))]
    if a["insights"]:
        out += ["## Insights", ""]
        out += [f"- {'⚠️ ' if i['level'] == 'warn' else ''}{i['text']}" for i in a["insights"]]
        out.append("")

    # ---------------- skills
    sk = a["skills"]
    out += ["## Skills", "",
            f"{sk['invocations_total']} invocations of {sk['invoked_distinct']} skills "
            f"({_kv(sk['by_mode'])}); {sk['available_count']} skills were available.", ""]
    out += ["### Invocations", "", table(
        ["#", "When", "Turn", "Skill", "How", "Via", "Args", "Source", "Body", "Result"],
        [(i["i"], _t(i["ts"]), i["turn"], i["canonical"] or i["name"], i["mode"], i["via"],
          one_line(i["args"], 60), i["source"], tok(i["content_chars"]) + " chars" if i["content_chars"] else "—",
          "ok" if i["success"] else (i["status"] or "failed") + (f": {one_line(i['error'], 60)}" if i["error"] else ""))
         for i in sk["invocations"]])]
    out += ["### Per skill (activity Claude Code attributed to it)", "", table(
        ["Skill", "Invocations", "Modes", "Turns", "Requests", "Tool calls", "Tool errors", "Output tok", "Cost",
         "Top tools"],
        [(p["skill"], p["invocations"], _kv(p["by_mode"]), len(p["turns"]), p["attributed_requests"],
          p["attributed_tool_calls"], p["tool_errors"], tok(p["output_tokens"]), usd(p["cost_usd"]),
          _kv(p["tools"], 5)) for p in sk["per_skill"]])]
    if sk["failed"]:
        out += ["### Failed invocations", "", table(["Skill", "Turn", "Error"],
                                                    [(f["name"], f["turn"], one_line(f["error"], 120))
                                                     for f in sk["failed"]])]
    if sk["slash_commands"]["rows"]:
        out += ["### Built-in slash commands", "", table(
            ["When", "Turn", "Command", "Args", "Output"],
            [(_t(c["ts"]), c["turn"], "/" + c["name"], one_line(c["args"], 60), one_line(c["output"], 80))
             for c in sk["slash_commands"]["rows"]])]
    if sk["unused_available"]:
        out += [f"### Available but not used ({len(sk['unused_available'])})", "",
                ", ".join(f"`{n}`" for n in sk["unused_available"]), ""]
    if sk["restored_after_compaction"]:
        out += ["### Re-injected after compaction", "",
                ", ".join(f"{r['name']} ({_t(r['ts'])})" for r in sk["restored_after_compaction"]), ""]

    # ---------------- tools
    tl = a["tools"]
    out += ["## Tools", "",
            f"{tl['total_calls']:,} calls to {tl['distinct_tools']} tools · status: {_kv(tl['by_status'])} · "
            f"parallel calls: {tl['parallel_batches']['parallel_calls']} (largest batch "
            f"{tl['parallel_batches']['max']})", ""]
    out.append(table(
        ["Tool", "Category", "Calls", "Main", "Sub", "Errors", "Denied", "Interrupted", "p50", "p90", "Max",
         "Result chars"],
        [(x["name"], x["category"], x["calls"], x["main"], x["subagent"], x["error"], x["denied"],
          x["interrupted"], dur(x["duration_ms"]["p50"]), dur(x["duration_ms"]["p90"]),
          dur(x["duration_ms"]["max"]), tok(x["result_chars"])) for x in tl["by_tool"]]))
    out += ["### By category", "", table(["Category", "Calls", "Errors", "Tools"],
                                         [(k, v["calls"], v["errors"], v["tools"])
                                          for k, v in tl["by_category"].items()])]
    if tl["mcp_servers"]:
        out += ["### MCP servers", "", table(["Server", "Calls", "Errors", "Tools"],
                                             [(k, v["calls"], v["errors"], _kv(v["tools"], 8))
                                              for k, v in tl["mcp_servers"].items()])]
    if tl["transitions"]:
        out += ["### Most common sequences (main thread)", "",
                table(["From", "To", "Count"], [(x["from"], x["to"], x["count"]) for x in tl["transitions"][:15]])]
    if tl["deferred_tools_loaded"]:
        out += ["### Deferred tools loaded via ToolSearch", "", _kv(tl["deferred_tools_loaded"]), ""]
    for name in ("Bash", "Read", "Edit", "Grep", "WebFetch"):
        tt = tl["top_targets"].get(name)
        if tt:
            out += [f"### Top {name} targets", "", table(["Target", "Count"],
                                                         [(one_line(x["target"], 100), x["count"]) for x in tt[:10]])]

    # ---------------- tokens & cost
    tk, co = a["tokens"], a["cost"]
    out += ["## Tokens & cost", "", table(
        ["Model", "Requests", "Input", "Output", "Thinking", "Cache read", "Cache write 5m", "Cache write 1h",
         "Hit ratio", "Cost"],
        [(m, v["requests"], tok(v["input"]), tok(v["output"]), tok(v["thinking"]), tok(v["cache_read"]),
          tok(v["cache_write_5m"]), tok(v["cache_write_1h"]), pct(v["cache_hit_ratio"]), usd(v["cost"]))
         for m, v in tk["by_model"].items()])]
    out += ["- **Cost components:** " + ", ".join(f"{k.replace('_', ' ')} {usd(v)}" for k, v in co["components"].items() if v),
            "- **By scope:** " + ", ".join(f"{k} {usd(v)}" for k, v in co["by_scope"].items()),
            "- **By skill (attributed):** " + ", ".join(f"{k} {usd(v)}" for k, v in list(co["by_skill"].items())[:8]),
            "- **By agent:** " + ", ".join(f"{k} {usd(v)}" for k, v in list(co["by_agent"].items())[:8]),
            f"- **Context:** peak {tok(tk['context']['peak_tokens'])} at {_t(tk['context']['peak_at'])}, "
            f"mean {tok(tk['context']['mean_tokens'])}",
            f"- **Output:** {tok(tk['output']['total'])} tokens, {pct(tk['output']['thinking_share'])} thinking",
            ""]
    if tk["cache"]["miss_reasons"]:
        out += ["### Cache misses", "", table(["Reason", "Requests", "Missed tokens"],
                                              [(k, v["requests"], tok(v["missed_tokens"]))
                                               for k, v in tk["cache"]["miss_reasons"].items()])]
    if co["unpriced_models"]:
        out += [f"_No price for: {', '.join(co['unpriced_models'])} ({co['unpriced_requests']} requests excluded)._", ""]
    rep = a.get("reported")
    if rep:
        rc = rep["reconciliation"]
        out += ["### Claude Code's own accounting (cost-state)", "",
                f"Reported {usd(rep['total_cost_usd'])} vs transcript estimate {usd(rc['transcript_estimate_usd'])} "
                f"(delta {usd(rc['delta_usd'])}). API time {dur(rep['api_duration_ms'])}, tool time "
                f"{dur(rep['tool_duration_ms'])}, lines +{_num(rep['lines_added'])}/−{_num(rep['lines_removed'])}. "
                f"_{rep['scope_note']}_", "",
                table(["Model", "Reported cost", "Transcript cost", "Reported output", "Transcript output",
                       "Reported cache read", "Transcript cache read"],
                      [(r["model"], usd(r["reported_cost"]), usd(r["transcript_cost"]), _num(r["reported_output"]),
                        _num(r["transcript_output"]), _num(r["reported_cache_read"]), _num(r["transcript_cache_read"]))
                       for r in rc["by_model"]])]
    out += ["_" + " ".join(co["notes"]) + "_", ""]

    # ---------------- timing
    tim = a["timing"]
    out += ["## Timing", "",
            f"- Wall {dur(tim['wall_ms'])}, active {dur(tim['active_ms'])} ({pct(tim['active_share'])})",
            f"- Main thread: model {dur(tim['model_ms'])}, tools {dur(tim['tool_wall_ms'])} wall "
            f"({dur(tim['tool_sum_ms'])} summed), other {dur(tim['other_active_ms'])}",
            f"- Turn duration p50 {dur(tim['turn_duration_ms']['p50'])}, p90 {dur(tim['turn_duration_ms']['p90'])}, "
            f"max {dur(tim['turn_duration_ms']['max'])}",
            f"- Request latency p50 {dur(tim['request_latency_ms']['p50'])}, p90 "
            f"{dur(tim['request_latency_ms']['p90'])}; duration p50 {dur(tim['request_duration_ms']['p50'])}",
            f"- Your think time between turns p50 {dur(tim['user_think_ms']['p50'])}, p90 "
            f"{dur(tim['user_think_ms']['p90'])}",
            f"- Idle gaps over 30 min: {len(tim['idle_gaps'])}", ""]

    # ---------------- turns
    tu = a["turns"]
    out += ["## Turns", "", f"{tu['count']} turns ({_kv(tu['by_trigger'])}); {tu['interrupted']} interrupted.", ""]
    out.append(table(
        ["#", "Start", "Duration", "Trigger", "Prompt", "Req", "Tools", "Err", "Skills", "Output", "Cost", "Flags"],
        [(r["index"], _t(r["start"]), dur(r["duration_ms"]), r["trigger"],
          one_line(r["prompt"] or ("/" + r["command"] if r["command"] else ""), 90), r["requests"],
          r["tool_calls"] + r["subagent_tool_calls"], r["tool_errors"],
          ", ".join(r["skills_invoked"] or r["skills_attributed"]), tok(r["output_tokens"]), usd(r["cost_usd"]),
          " ".join(f for f, on in (("interrupted", r["interrupted"]), ("compacted", r["compacted"]),
                                   ("in-progress", r["in_progress"])) if on))
         for r in tu["rows"]]))

    # ---------------- subagents & workflows
    sa = a["subagents"]
    if sa["rows"] or sa["launches_without_transcript"]:
        out += ["## Subagents", "", f"{sa['count']} subagents, {sa['workflow_agents']} workflow agents.", ""]
        out.append(table(
            ["Agent", "Kind", "Type", "Description", "Turn", "Duration", "Requests", "Tools", "Errors",
             "Final context", "Cost"],
            [(r["agent_id"][:12], r["kind"], r["type"], one_line(r["description"], 60), r["launched_in_turn"],
              dur(r["duration_ms"]), r["requests"], r["tool_calls"], r["tool_errors"],
              tok(r["final_context_tokens"]), usd(r["cost_usd"])) for r in sa["rows"]]))
        if sa["by_type"]:
            out += ["### By agent type", "", table(["Type", "Agents", "Requests", "Tool calls", "Cost"],
                                                   [(k, v["agents"], v["requests"], v["tool_calls"], usd(v["cost_usd"]))
                                                    for k, v in sa["by_type"].items()])]
    wf = a["workflows"]
    if wf["rows"]:
        out += ["## Workflows", "", table(
            ["Run", "Name", "Status", "Turn", "Duration", "Agents", "Requests", "Tool calls", "Cost", "Phases"],
            [(r["run"], r["name"], r["status"], r["launched_in_turn"], dur(r["duration_ms"]),
              r["reported_agents"] or r["agent_transcripts"], r["requests"], r["tool_calls"], usd(r["cost_usd"]),
              ", ".join(p for p in r["phases"] if p)) for r in wf["rows"]])]

    # ---------------- files
    fi = a["files"]
    out += ["## Files", "",
            f"{fi['unique_read']} read ({_num(fi['lines_read'])} lines) · {fi['unique_modified']} modified · "
            f"{fi['unique_created']} created · edit tools +{_num(fi['lines_added'])}/−{_num(fi['lines_removed'])}"
            f" · via shell +{_num(fi['bash_lines_added'])}/−{_num(fi['bash_lines_removed'])}", "",
            f"- Modified by extension: {_kv(fi['by_extension_modified'], 12)}",
            f"- Modified by directory: {_kv(fi['by_directory_modified'], 12)}",
            f"- Checkpoints: {fi['checkpoints']['snapshots']} snapshots, {fi['checkpoints']['tracked_files']} files tracked",
            ""]
    out.append(table(["File", "Reads", "Edits", "Writes", "Shell edits", "+", "−", "Errors"],
                     [(f["path"], f["reads"], f["edits"], f["writes"], f["bash_edits"],
                       f["lines_added"] + f["bash_lines_added"], f["lines_removed"] + f["bash_lines_removed"],
                       f["errors"]) for f in fi["rows"][:60]]))
    if fi["changed_outside_edit_tools"]:
        out += [f"- Changed on disk other than by Claude's edit tools (you, a formatter, or a shell command): "
                f"{_kv(fi['changed_outside_edit_tools'], 10)}", ""]
    if fi["mentioned_by_user"]:
        out += [f"- @-mentioned by you: {_kv(fi['mentioned_by_user'], 10)}", ""]

    # ---------------- shell, git, web
    sh = a["shell"]
    if sh["commands"]:
        out += ["## Shell", "",
                f"{sh['commands']} commands · {sh['errors']} failed · {sh['background']} background · "
                f"{sh['timed_out']} timed out · {sh['interrupted']} interrupted · {sh['sandbox_disabled']} sandbox "
                f"overrides", "",
                f"- Programs (primary): {_kv(sh['primary_programs'], 15)}",
                f"- Subcommands: {_kv(sh['subcommands'], 15)}",
                f"- Exit codes: {_kv(sh['exit_codes'])}", ""]
        if sh["slowest"]:
            out += ["### Slowest commands", "", table(["Duration", "Turn", "Status", "Command"],
                                                      [(dur(x["duration_ms"]), x["turn"], x["status"],
                                                        one_line(x["command"], 110)) for x in sh["slowest"][:8]])]
        if sh["failing"]:
            out += ["### Failing commands", "", table(["Turn", "Exit", "Command", "Error"],
                                                      [(x["turn"], x["exit_code"], one_line(x["command"], 70),
                                                        one_line(x["error"], 90)) for x in sh["failing"][:12]])]
    g = a["git"]
    if any(g["counts"].values()) or g["git_subcommands"]:
        out += ["## Git", "",
                f"- git: {_kv(g['git_subcommands'], 15)}", f"- gh: {_kv(g['gh_subcommands'], 10)}", ""]
        if g["commits"]:
            out.append(table(["When", "Turn", "SHA", "Branch", "Kind"],
                             [(_t(c["ts"]), c["turn"], (c["sha"] or "")[:10], c["branch"], c["kind"]) for c in g["commits"]]))
        if g["pr_links"] or g["pull_requests"]:
            out.append(table(["PR", "Action", "When"],
                             [(p["url"], p.get("action") or "linked", _t(p.get("ts") or p.get("first_seen")))
                              for p in (g["pull_requests"] or g["pr_links"])]))
    w = a["web"]
    if w["fetches"] or w["searches"]:
        out += ["## Web", "", f"- {w['fetches']} fetches ({w['fetch_errors']} failed), {tok(w['bytes'])} bytes; "
                              f"domains: {_kv(w['domains'], 12)}",
                f"- {w['searches']} searches: " + "; ".join(one_line(x["query"], 60) for x in w["search_rows"][:10]),
                ""]

    # ---------------- planning, user
    pl = a["planning"]
    if pl["tasks_created"] or pl["todo_writes"] or pl["questions_asked"] or pl["plan_mode"]["plans_presented"]:
        out += ["## Planning & questions", "",
                f"- Tasks: {pl['tasks_created']} created, {pl['task_updates']} updates, {pl['tasks_completed']} completed"
                f"; TodoWrite ×{pl['todo_writes']}",
                f"- Plan mode: entered {pl['plan_mode']['entered']}, plans presented {pl['plan_mode']['plans_presented']}"
                f" (approved {pl['plan_mode']['plans_approved']})",
                f"- Questions asked: {pl['questions_asked']}", ""]
        for q in pl["questions"][:10]:
            for qq in q["questions"]:
                ans = (q["answers"] or {}).get(qq["question"]) if q["answers"] else None
                out.append(f"  - turn {q['turn']}: _{qq['question']}_ → {ans or '—'}")
        out.append("")
    us = a["user"]
    out += ["## You", "",
            f"- {us['prompts']} prompts (sources: {_kv(us['prompt_sources'])}), median {us['prompt_words']['p50'] or 0} words",
            f"- {us['slash_commands_typed']} slash commands typed ({_kv(us['slash_commands'], 10)})",
            f"- {us['interruptions']} interruptions, {us['rejected_by_user']} tool calls rejected, "
            f"{us['images_pasted']} images pasted",
            f"- Queue: {_kv(us['queue']['operations'])}; absorbed mid-turn: "
            f"{us['queue']['reasons'].get('absorbed_mid_turn', 0)}",
            f"- Think time between turns: p50 {dur(us['think_time_ms']['p50'])}", ""]

    # ---------------- errors, hooks
    er = a["errors"]
    out += ["## Errors", "",
            f"{er['tool_errors']} tool errors ({pct(er['tool_error_rate'])}) · {er['denied']} denied · "
            f"{er['interrupted']} interrupted · {er['api_errors']} API errors/retries", "",
            f"- By category: {_kv(er['by_category'])}", f"- By tool: {_kv(er['by_tool'])}",
            f"- API errors: {_kv(er['api_error_statuses'])}", ""]
    if er["rows"]:
        out.append(table(["When", "Turn", "Tool", "Category", "Input", "Message"],
                         [(_t(r["ts"]), r["turn"], r["tool"], r["category"], one_line(r["input"], 50),
                           one_line(r["message"], 100)) for r in er["rows"][:40]]))
    hk = a["hooks"]
    if hk["runs"] or hk["stop_hooks"]["summaries"]:
        out += ["## Hooks", "",
                f"- {hk['runs']} hook runs ({_kv(hk['by_event'])}), {hk['non_zero_exit']} non-zero exits, "
                f"p50 {dur(hk['duration_ms']['p50'])}",
                f"- Stop hooks: {hk['stop_hooks']['summaries']} summaries, {hk['stop_hooks']['hooks_run']} runs, "
                f"{hk['stop_hooks']['errors']} errors, {hk['stop_hooks']['prevented_continuation']} prevented stop", ""]

    # ---------------- context
    cx = a["context"]
    out += ["## Context", "",
            f"- Compactions: {len(cx['compactions'])} ({_kv(cx['compactions_by_trigger'])})",
            f"- System prompt: {tok(cx['system_prompt']['last_chars'])} chars, "
            f"{cx['system_prompt']['tools_available'] or '—'} tools in the last snapshot",
            f"- Skills listed: {cx['skills_listed']}; agent types: {', '.join(cx['agent_types_listed']) or '—'}",
            "- Instruction files: " + (", ".join(f"{i['path']} ({i['type']})" for i in cx["instruction_files"]) or "—"),
            f"- MCP servers with instructions: {', '.join(cx['mcp_servers']['with_instructions']) or '—'}"
            + (f"; failed: {', '.join(cx['mcp_servers']['failed'])}" if cx["mcp_servers"]["failed"] else "")
            + (f"; needs auth: {', '.join(cx['mcp_servers']['needs_auth'])}" if cx["mcp_servers"]["needs_auth"] else ""),
            f"- Deferred tools announced: {len(cx['deferred_tools_announced'])}", ""]
    if cx["compactions"]:
        out.append(table(["When", "Turn", "Trigger", "Before", "After", "Took"],
                         [(_t(c["ts"]), c["turn"], c["trigger"], tok(c["pre_tokens"]), tok(c["post_tokens"]),
                           dur(c["duration_ms"])) for c in cx["compactions"]]))

    # ---------------- outputs
    ou = a["outputs"]
    if ou["artifacts"] or ou["files_sent_to_user"] or ou["plans"]:
        out += ["## Outputs", ""]
        out += [f"- Artifact: {x['title'] or '—'} {x['url'] or ''} (v{x['version']})" for x in ou["artifacts"]]
        out += [f"- Sent to you: {', '.join(x['files'])}" for x in ou["files_sent_to_user"]]
        out += [f"- Plan: {x['path'] or '—'} ({x['chars']} chars, {x['status']})" for x in ou["plans"]]
        out.append("")

    # ---------------- coverage
    sc = a["schema_coverage"]
    out += ["## Transcript coverage", "",
            f"{sc['files']} files, {sc['lines']:,} lines ({sc['bad_lines']} unparseable), {tok(sc['bytes'])} bytes.",
            "", "- Event types (main): " + _kv(sc["event_types"].get("main", {}), 30),
            "- System subtypes: " + _kv(sc["system_subtypes"]),
            "- Attachment types: " + _kv(sc["attachment_types"], 40),
            "- Unrecognised: " + (", ".join(f"{k}: {list(v)}" for k, v in sc["unknown"].items() if v) or "none"),
            ""]
    g = a["generator"]
    out += [f"_Generated {g['generated_at']} by convo-analysis {g['version']} · "
            f"redaction {'on' if g['redaction'] else 'off'} · full content {'on' if g['full_content'] else 'off'}_", ""]
    return "\n".join(out)
