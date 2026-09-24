"""Skill runs: what a skill did from the moment it was invoked, for developing and troubleshooting skills.

Claude Code attributes responses to a skill only until the turn ends (`attributionSkill`), but a skill's
instructions stay in context and keep steering the follow-up turns. A run therefore starts at an invocation
and lasts until another skill is invoked in a later turn, or the session ends. Skills invoked in the same
turn by the model, and skills Claude Code injects itself, are nested in the run rather than ending it.
Every metric keeps both views: `attributed` (Claude Code's attribution) and the whole run.

Each run records the version of the skill that ran (see versions.py), every skill document it read and how
much of each (see skillfiles.py) against what the playbooks say to read first, every CLI call by signature
(`mb transform create`), help lookups, questions asked and answered, objects the CLI reported creating,
cost and context, the final hand-back, and the checks declared for the skill (see checks.py).
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict

from . import checks as checks_mod
from . import skillfiles, util, versions

READ_FIRST_RE = re.compile(r"^\s*\**Read first\**\s*:(.*)$", re.I | re.M)
LINK_RE = re.compile(r"\]\(([^)\s]+\.md)\)|`([\w./-]+\.md)`")
OBJ_RE = re.compile(r"\{[^{}\n]{0,800}\}")
MUTATING = {"create", "update", "delete", "delete-table", "archive", "run", "publish", "export", "import", "move"}
HOME = os.path.expanduser("~")


def skill_key(name):
    """`plugin:rde` and `rde` are the same skill for grouping."""
    return (name or "?").split(":")[-1]


def read_first(text, playbook_rel):
    """Files a playbook names on its "Read first:" line, resolved relative to the skill directory."""
    if not text:
        return []
    out = []
    base = os.path.dirname(playbook_rel)
    for line in READ_FIRST_RE.findall(text):
        for m in LINK_RE.finditer(line):
            target = m.group(1) or m.group(2)
            if "/" not in target and m.group(2):
                continue  # a bare `x.md` mention without a path is ambiguous
            out.append(os.path.normpath(os.path.join(base, target)))
    return sorted(set(out))


def created_objects(call, cli_calls):
    """Objects a CLI call reported touching: JSON objects with an id and a name in its output."""
    if call.name != "Bash" or call.status != "ok":
        return []
    sigs = [c["signature"] for c in cli_calls(call.input.get("command")) if not c["help"]]
    mut = sorted({sg for sg in sigs if len(sg.split()) >= 2 and sg.split()[-1] in MUTATING})
    if not mut:
        return []
    verbs = {sg.split()[-1] for sg in mut}
    nouns = {sg.split()[1] for sg in mut if len(sg.split()) >= 3}
    verb = verbs.pop() if len(verbs) == 1 else "mixed"
    noun = nouns.pop() if len(nouns) == 1 else "mixed"
    objs = []
    for m in OBJ_RE.finditer(call.result_preview or ""):
        try:
            d = json.loads(m.group(0))
        except ValueError:
            continue
        if isinstance(d, dict) and "id" in d and (d.get("name") or d.get("display_name")):
            objs.append({"id": d["id"], "name": d.get("name") or d.get("display_name"), "type": noun, "verb": verb,
                         "signature": mut[0] if len(mut) == 1 else ", ".join(mut)})
    return objs


def _segment(s):
    """Runs of a session: [{inv, nested, start, end, end_reason, agent}].

    Skills Claude Code injects itself (harness mode, e.g. workflow-authoring when the Workflow tool is used)
    are infrastructure: they nest in whatever run is active and own a run only when no invoked skill does.
    """
    invs = sorted((i for i in s.skills if i.ts is not None and i.success is not False), key=lambda i: i.ts)
    main = [i for i in invs if i.scope == "main"]
    groups, cur = [], None
    for inv in (i for i in main if i.mode != "harness"):
        if cur is not None and inv.turn == cur["inv"].turn and inv.mode != "user":
            cur["nested"].append(inv)
            continue
        if cur is not None:
            cur["end"] = inv.ts
            cur["end_reason"] = f"next skill: {inv.canonical or inv.name}"
        cur = {"inv": inv, "nested": [], "start": inv.ts, "end": None, "end_reason": None, "agent": None}
        groups.append(cur)
    if cur is not None:
        cur["end_reason"] = "session still live" if s.live_turn is not None else "session end"
    orphans = []
    for inv in (i for i in main if i.mode == "harness"):
        home = next((g for g in groups if g["start"] <= inv.ts and (g["end"] is None or inv.ts < g["end"])), None)
        if home is None:  # injected before the first invoked skill: nest it in the run of that turn, if any
            home = next((g for g in groups if g["inv"].turn == inv.turn), None)
        if home is not None:
            home["nested"].append(inv)
        else:
            orphans.append(inv)
    for inv in orphans:
        groups.append({"inv": inv, "nested": [], "start": inv.ts, "end": None, "end_reason": "harness-injected skill",
                       "agent": None})
    for inv in invs:
        if inv.scope != "main":
            groups.append({"inv": inv, "nested": [], "start": inv.ts, "end": None, "end_reason": "agent end",
                           "agent": inv.agent_id})
    return groups


def _pin(docs, key, version, sources):
    """Point the doc set at the files of the version a run used; returns that version's source, if any."""
    src = next((x for x in sources if str(x.dir) == version.get("source")), None)
    if src is None:
        docs.pins.pop(key, None)
        return None
    docs.pin(key, src, version["sha"] if version.get("status") == "commit" else "WORKTREE")
    return src


def _changes_seen(src, version, sf, key, docs):
    """What the commit that ran changed in the skill (against the previous commit touching it), and whether
    this run was shown those lines."""
    if src is None or not src.repo or version.get("status") != "commit":
        return None
    commits = src.commits()
    idx = next((i for i, c in enumerate(commits) if c["sha"] == version["sha"]), None)
    if idx is None or idx + 1 >= len(commits):
        return None
    prev = commits[idx + 1]
    files = skillfiles.exposure(src.diff_hunks(prev["sha"], version["sha"]), sf["files"], key,
                                docs.text(key, "SKILL.md"))
    return {"from": prev["short"], "to": version.get("commit"), "subject": version.get("subject"), "files": files}


def build_runs(ctx, reqs, calls, trace, check_files=(), sources_extra=(), docs=None):
    from .analyze import categorize_error, cli_calls, summarize_input, usage_totals

    s = ctx.s
    docs = docs or skillfiles.DocSet.for_session(s)
    skill_dirs = defaultdict(set)
    for inv in s.skills:
        if inv.base_dir:
            skill_dirs[skill_key(inv.canonical or inv.name)].add(os.path.expanduser(inv.base_dir))
    source_cache, checks_cache = {}, {}
    failed = [i for i in s.skills if i.success is False]
    turns_by_index = {t.index: t for t in s.turns}
    step_ts = [(st["t"], st.get("scope"), st.get("agent"), st["i"]) for st in trace["steps"]]
    runs = []
    for n, g in enumerate(_segment(s), 1):
        inv, start, end, agent = g["inv"], g["start"], g["end"], g["agent"]
        key = skill_key(inv.canonical or inv.name)

        def inside(ts, a_id=None, _start=start, _end=end, _agent=agent):
            if ts is None or ts < _start or (_end is not None and ts >= _end):
                return False
            return _agent is None or a_id == _agent

        rq = [r for r in reqs if inside(r.ts_first, r.agent_id)]
        cl = [c for c in calls if inside(c.ts_call if c.ts_call is not None else c.ts_result, c.agent_id)
              and c.id != inv.tool_use_id]
        main_rq = [r for r in rq if r.scope == ("main" if agent is None else r.scope)]
        main_cl = [c for c in cl if agent is not None or c.scope == "main"]

        # The version first: the Read tool's whole-file reads of the skill's own files break ties between
        # commits that share a SKILL.md. Then every skill document the run touched, measured at that version.
        read_hashes = {}
        for c in cl:
            if c.name == "Read" and c.facts.get("content_sha") and not c.facts.get("partial"):
                loc = docs.locate(os.path.normpath(c.input.get("file_path") or "/"))
                if loc and loc[0] == key and loc[1]:
                    read_hashes[loc[1]] = c.facts["content_sha"]
        if key not in source_cache:
            source_cache[key] = versions.find_sources(key, base_dirs=sorted(skill_dirs.get(key, ())),
                                                      explicit=sources_extra)
        sources = source_cache[key]
        version = versions.resolve(inv.fingerprint, inv.ts, sources, read_hashes)
        src = _pin(docs, key, version, sources)
        sf = skillfiles.run_files(cl, inv, key, docs, source=src, pin_sha=version.get("sha"),
                                  body_matched=version.get("status") != "unknown")
        changes_seen = _changes_seen(src, version, sf, key, docs)
        own = [f for f in sf["files"] if f["owner"] == key and f["how"] != "injected"]
        distinct = [f["path"] for f in own]
        playbooks = [p for p in distinct if p.startswith("playbooks/")]
        expected = set()
        for pb in playbooks:
            expected.update(read_first(docs.text(key, pb), pb))
        read_set = set(distinct)
        by_call = defaultdict(list)
        for a in sf["accesses"]:
            if a["op"] != "inject":
                by_call[a["call"]].append(a)

        # CLI usage (the layer a skill like rde drives), help lookups, retries after a failure.
        sig_stats = defaultdict(lambda: {"calls": 0, "errors": 0, "help": 0})
        retries, prev_failed_sigs = 0, None
        for c in (x for x in main_cl if x.name == "Bash"):
            cls = cli_calls(c.input.get("command"))
            sigs = {x["signature"] for x in cls if " " in x["signature"]}
            for x in cls:
                if " " in x["signature"] and x["help"]:
                    sig_stats[x["signature"]]["help"] += 1
            for sg in sigs:
                sig_stats[sg]["calls"] += 1
                if c.status == "error":
                    sig_stats[sg]["errors"] += 1
            if prev_failed_sigs and sigs & prev_failed_sigs:
                retries += 1
            prev_failed_sigs = sigs if c.status == "error" else None

        errors = [c for c in cl if c.status == "error"]
        questions = []
        for c in main_cl:
            if c.name == "AskUserQuestion":
                ans = c.facts.get("answers") or {}
                for q in c.facts.get("questions") or []:
                    questions.append({"t": c.ts_call, "status": c.status, "header": q.get("header"),
                                      "question": ctx.text(q.get("question"), 300),
                                      "answer": ctx.text(ans.get(q.get("question")), 300) if ans else None})
        objects = []
        for c in main_cl:
            for o in created_objects(c, cli_calls):
                o["t"] = c.ts_call
                o["name"] = ctx.text(str(o["name"]), 120)
                objects.append(o)
        written = list(dict.fromkeys(_rel(c.facts.get("path") or c.input.get("file_path"), ctx.cwd)
                                     for c in main_cl if c.name in ("Write", "Edit", "MultiEdit") and c.status == "ok"))

        turn_ids = sorted({r.turn for r in main_rq if r.turn is not None} | ({inv.turn} if inv.turn is not None else set()))
        turn_rows = []
        for ti in turn_ids:
            t = turns_by_index.get(ti)
            if t is None:
                continue
            turn_rows.append({"index": ti, "trigger": t.trigger, "prompt": ctx.text(t.text or ("/" + (t.command or "")), 240),
                              "attributed": any(r.turn == ti and skill_key(r.attribution_skill or "") == key for r in main_rq),
                              "interrupted": t.interrupted, "duration_ms": (t.reported_duration_ms if t.reported_duration_ms
                                                                            is not None else ((t.ts_end or t.ts_start) - t.ts_start))})
        u_all = usage_totals(rq)
        u_attr = usage_totals([r for r in rq if skill_key(r.attribution_skill or "") == key])
        last_text = next((r for r in sorted(main_rq, key=lambda r: r.ts_first or 0, reverse=True) if r.text_preview), None)
        ts_all = [x for x in [r.ts_last for r in rq] + [c.ts_result for c in cl] if x is not None]
        events = [{"tool": c.name, "command": c.input.get("command") if c.name == "Bash" else None,
                   "segments": [x["segment"] for x in cli_calls(c.input.get("command"))] if c.name == "Bash" else None,
                   "resources": [a["path"] for a in by_call.get(c.id, ()) if a["owner"] == key
                                 and a["op"] in ("read", "search") and a["path"] and not a["path"].endswith("/")],
                   "files": [{"file": f"{a['owner']}:{a['path']}", "op": a["op"], "how": a["how"] or ""}
                             for a in by_call.get(c.id, ())],
                   "input": summarize_input(c), "status": c.status} for c in main_cl]
        if key not in checks_cache:
            try:
                checks_cache[key] = checks_mod.load(check_files, skill=key)
            except (OSError, ValueError) as exc:
                checks_cache[key] = [{"id": "checks-file", "desc": f"could not load checks: {exc}", "type": "never",
                                      "match": {}}]
        check_rows = [checks_mod.evaluate(ch, events) for ch in checks_cache[key]]
        first_ctx = next((r.context_tokens for r in main_rq), None)
        runs.append({
            "run_id": f"{s.session_id[:8]}:{n}",
            "session_id": s.session_id,
            "tool_use_id": inv.tool_use_id,
            "skill": key, "canonical": inv.canonical or inv.name, "mode": inv.mode, "via": inv.via,
            "scope": inv.scope, "agent_id": agent, "inherited": inv.inherited,
            "args": ctx.text(inv.args, 400) if inv.args else None,
            "prompt": ctx.text(_prompt_of(turns_by_index.get(inv.turn), inv), 400),
            "start_ms": start, "end_ms": max(ts_all) if ts_all else start, "window_end_ms": end,
            "end_reason": g["end_reason"],
            "version": version, "fingerprint": inv.fingerprint, "base_dir": inv.base_dir,
            "body_chars": inv.content_chars,
            "nested_skills": [{"name": x.canonical or x.name, "mode": x.mode, "t": x.ts} for x in g["nested"]],
            "failed_invocations": [{"name": x.name, "t": x.ts, "error": ctx.text(x.error, 160)} for x in failed
                                   if inside(x.ts, x.agent_id)],
            "turns": turn_rows, "turn_count": len(turn_rows),
            "follow_up_turns": sum(1 for t in turn_rows if not t["attributed"]),
            "active_ms": sum(t["duration_ms"] or 0 for t in turn_rows),
            "duration_ms": (max(ts_all) - start) if ts_all else 0,
            "requests": u_all["requests"], "attributed_requests": u_attr["requests"],
            "input_tokens": u_all["input"], "output_tokens": u_all["output"], "thinking_tokens": u_all["thinking"],
            "cache_read_tokens": u_all["cache_read"], "cache_write_tokens": u_all["cache_write"],
            "cache_hit_ratio": u_all["cache_hit_ratio"],
            "cost_usd": round(u_all["cost"], 6), "attributed_cost_usd": round(u_attr["cost"], 6),
            "context_start": first_ctx, "context_end": main_rq[-1].context_tokens if main_rq else None,
            "context_peak": max((r.context_tokens for r in main_rq), default=None),
            "latency_p50_ms": util.percentile([r.latency_ms for r in main_rq], 50),
            "models": sorted({r.model for r in rq}),
            "tool_calls": len(cl), "main_tool_calls": len(main_cl),
            "tools": dict(Counter(c.name for c in cl).most_common()),
            "tool_errors": len(errors), "error_rate": util.ratio(len(errors), len(cl)),
            "error_categories": dict(Counter(categorize_error(c.result_preview) for c in errors).most_common()),
            "errors": [{"t": c.ts_call, "tool": c.name, "input": ctx.text(summarize_input(c), 200),
                        "message": ctx.block(c.result_preview, 600), "category": categorize_error(c.result_preview)}
                       for c in errors[:30]],
            "denials": sum(1 for c in cl if c.status == "denied"),
            "interrupted": sum(1 for c in cl if c.status == "interrupted") + sum(1 for t in turn_rows if t["interrupted"]),
            "subagents": sum(1 for c in main_cl if c.name in ("Agent", "Task")),
            "cli": [dict(signature=k, **v) for k, v in sorted(sig_stats.items(), key=lambda kv: -kv[1]["calls"])],
            "cli_calls": sum(v["calls"] for v in sig_stats.values()),
            "help_lookups": sum(v["help"] for v in sig_stats.values()),
            "retries_after_error": retries,
            "skill_files": sf, "resources_read": distinct, "playbooks": playbooks,
            "docs_read": sum(1 for f in own if f["seen"]), "docs_total": sf["totals"]["own_inventory"],
            "docs_never": len(sf["inventory"]["never"]), "cli_docs_read": sf["totals"]["other_files"],
            "doc_tokens": sf["totals"]["doc_tokens"], "doc_rereads": sf["totals"]["rereads"],
            "doc_listings": sf["totals"]["listings"], "doc_unprompted": sf["totals"]["unprompted"],
            "doc_version_mismatches": sf["totals"]["version_mismatches"], "changes_seen": changes_seen,
            "references": [p for p in distinct if p.startswith("references/")],
            "expected_by_playbooks": sorted(expected),
            "missing_expected": sorted(expected - read_set),
            "not_named_by_playbooks": sorted(p for p in read_set - expected if p.startswith("references/")),
            "questions": questions, "questions_asked": len(questions),
            "question_calls": sum(1 for c in main_cl if c.name == "AskUserQuestion"),
            "objects": objects, "objects_created": sum(1 for o in objects if o["verb"] == "create"),
            "files_written": written,
            "final_message": ctx.text(last_text.text_preview, 1200) if last_text else None,
            "last_stop_reason": main_rq[-1].stop_reason if main_rq else None,
            "checks": check_rows,
            "checks_failed": sum(1 for c in check_rows if c["status"] == "fail"),
            "checks_passed": sum(1 for c in check_rows if c["status"] == "pass"),
            "steps": [i for t, scope, a_id, i in step_ts if inside(t, a_id)],
        })
    return runs


def _prompt_of(turn, inv):
    """What the user asked when the run started: the turn's prompt, or `/skill args` for a slash command."""
    if turn is None:
        return ""
    if turn.trigger == "command":
        return f"{turn.command or '/' + inv.name} {turn.command_args or ''}".strip()
    return turn.text or ""


def _rel(path, cwd):
    if not path:
        return path
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        return path[len(cwd.rstrip("/")) + 1:]
    return "~" + path[len(HOME):] if path.startswith(HOME + "/") else path
