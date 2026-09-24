"""Flat CSV tables from the analytics dict, for spreadsheets and SQL engines."""

from __future__ import annotations

import csv
import json
import os

TABLES = {
    "turns.csv": ("turns", ["index", "start", "end", "duration_ms", "duration_source", "trigger", "origin",
                            "prompt_source", "permission_mode", "prompt", "prompt_chars", "prompt_words", "images",
                            "command", "command_args", "requests", "subagent_requests", "tool_calls",
                            "subagent_tool_calls", "tools", "tool_errors", "tool_denials", "skills_invoked",
                            "skills_attributed", "agents_launched", "models", "input_tokens", "output_tokens",
                            "cache_read_tokens", "cache_write_tokens", "thinking_tokens", "cost_usd",
                            "max_context_tokens", "stop_reason", "interrupted", "compacted",
                            "queued_prompts_absorbed", "in_progress"]),
    "requests.csv": ("requests", ["i", "ts", "turn", "scope", "agent_id", "model", "stop_reason", "input", "output",
                                  "cache_read", "cache_write_5m", "cache_write_1h", "thinking", "context",
                                  "cost_usd", "latency_ms", "duration_ms", "tools", "blocks", "skill", "agent",
                                  "plugin", "mcp_server", "effort", "cache_miss_reason", "speed"]),
    "tool_calls.csv": ("tools", ["i", "id", "ts", "turn", "scope", "agent_id", "name", "category", "status",
                                 "denial_kind", "duration_ms", "batch_size", "input", "description", "target",
                                 "result_chars", "error", "result", "skill", "agent"]),
    "skills.csv": ("skills", ["i", "ts", "turn", "name", "canonical", "mode", "via", "scope", "agent_id", "args",
                              "success", "status", "error", "allowed_tools", "source", "base_dir", "content_chars",
                              "forked_agent_id", "turn_prompt"]),
    "files.csv": ("files", ["path", "reads", "lines_read", "edits", "writes", "creates", "bash_edits",
                            "lines_added", "lines_removed", "bash_lines_added", "bash_lines_removed", "errors",
                            "first", "last", "scopes"]),
    "subagents.csv": ("subagents", ["agent_id", "kind", "workflow_run", "type", "name", "description", "phase",
                                    "launched_in_turn", "background", "status", "forked_skill", "start", "end",
                                    "duration_ms", "models", "requests", "input_tokens", "output_tokens",
                                    "cache_read_tokens", "cache_write_tokens", "cost_usd", "tool_calls", "tools",
                                    "tool_errors", "skills", "final_context_tokens", "reported_final_context_tokens",
                                    "reported_tool_uses", "task_prompt", "transcript"]),
    "errors.csv": ("errors", ["ts", "turn", "tool", "scope", "category", "input", "message"]),
    "questions.csv": ("questions", ["qid", "run_id", "skill", "t", "dt", "turn", "kind", "form", "topic", "topic_label",
                                    "header", "question", "options", "multi", "recommended_label", "outcome", "answer",
                                    "typed", "reply", "notes", "feedback", "wait_ms", "batch_size", "flags",
                                    "before_create", "reask_of"]),
    "skill_files.csv": ("skill_files", ["run_id", "skill", "version", "owner", "path", "kind", "order", "how",
                                        "coverage", "seen", "total", "lines", "sections", "first_dt", "accesses",
                                        "reads", "searches", "rereads", "via", "named_by", "found_by", "version_check",
                                        "tokens"]),
}


def _rows(a, key):
    if key == "turns":
        return a["turns"]["rows"]
    if key == "requests":
        return a["requests"]["rows"]
    if key == "tools":
        return a["tools"]["rows"]
    if key == "skills":
        return a["skills"]["invocations"]
    if key == "files":
        return a["files"]["rows"]
    if key == "subagents":
        return a["subagents"]["rows"]
    if key == "errors":
        return a["errors"]["rows"]
    if key == "questions":
        return (a.get("interview") or {}).get("questions") or []
    if key == "skill_files":
        return skill_file_rows(a.get("skill_runs") or [])
    return []


def skill_file_rows(runs):
    """One row per skill run and file it touched (skillfiles.run_files()["files"])."""
    rows = []
    for r in runs:
        for f in (r.get("skill_files") or {}).get("files") or ():
            rows.append(dict(f, run_id=r["run_id"], skill=r["skill"], version_check=f.get("version"),
                             version=(r.get("version") or {}).get("commit") or (r.get("version") or {}).get("label")))
    return rows


def _flat(v):
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    if v is None:
        return ""
    return v


def write_all(a, out_dir):
    written = {}
    for name, (key, cols) in TABLES.items():
        path = os.path.join(out_dir, name)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            for r in _rows(a, key):
                w.writerow([_flat(r.get(c)) for c in cols])
        written[name] = path
    return written
