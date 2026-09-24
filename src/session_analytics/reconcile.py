"""Check the warehouse against the raw transcripts it was loaded from.

    session-analytics warehouse --check        (make warehouse-check; make warehouse runs it after every load)

Every session is recounted straight from its JSONL with plain `json` — none of the parser's code — and compared
with the warehouse rows: API requests, input/output/cache-read tokens, tool calls, failed tool calls,
AskUserQuestion questions, Skill tool calls. The recount follows the exporter's rules, so a difference is a bug:
the main file plus <session>/**/*.jsonl (subagents, workflow agents; not journal.jsonl); main-file lines stamped
with another session id are history a resumed session copied in, and are skipped; `<synthetic>` messages are not
API requests; the lines a streamed response was written in are merged by (message id, request id), keeping the
largest usage figures. Transcripts written after the load (a live session) are reported apart, not compared.
"""

from __future__ import annotations

import json
import subprocess

from . import locate

FIELDS = ("api_requests", "output_tokens", "input_tokens", "cache_read_tokens", "tool_calls", "tool_errors",
          "ask_questions", "skill_calls")
_SQL = """SELECT s.session_id,
  (SELECT count(*) FROM api_requests a WHERE a.session_id = s.session_id),
  (SELECT coalesce(sum(output_tokens), 0) FROM api_requests a WHERE a.session_id = s.session_id),
  (SELECT coalesce(sum(input_tokens), 0) FROM api_requests a WHERE a.session_id = s.session_id),
  (SELECT coalesce(sum(cache_read_tokens), 0) FROM api_requests a WHERE a.session_id = s.session_id),
  (SELECT count(*) FROM tool_calls t WHERE t.session_id = s.session_id AND t.tool <> '(unmatched)'),
  (SELECT count(*) FROM tool_calls t WHERE t.session_id = s.session_id AND t.tool <> '(unmatched)'
     AND t.status IN ('error', 'denied', 'interrupted')),
  (SELECT count(*) FROM questions q WHERE q.session_id = s.session_id AND q.channel = 'ask'),
  (SELECT count(*) FROM tool_calls t WHERE t.session_id = s.session_id AND t.tool = 'Skill')
FROM sessions s"""


def raw_counts(main):
    """The counts the warehouse should hold for one session, from its transcript files alone."""
    sid = main.stem
    side = main.parent / sid
    files = [main] + (sorted(f for f in side.rglob("*.jsonl") if f.name != "journal.jsonl") if side.is_dir() else [])
    reqs, uses, results = {}, {}, {}
    for f in files:
        is_main = f == main
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            t = ev.get("type")
            if is_main and t in ("user", "assistant", "system", "attachment") and ev.get("sessionId") not in (None, sid):
                continue
            msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
            if t == "assistant":
                if msg.get("model") == "<synthetic>":
                    continue
                u = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
                r = reqs.setdefault((msg.get("id"), ev.get("requestId")), {"out": 0, "in": 0, "cr": 0})
                r["out"] = max(r["out"], u.get("output_tokens") or 0)
                r["in"] = max(r["in"], u.get("input_tokens") or 0)
                r["cr"] = max(r["cr"], u.get("cache_read_input_tokens") or 0)
                for b in msg.get("content") or ():
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        uses[b.get("id")] = (b.get("name"), b.get("input") if isinstance(b.get("input"), dict) else {})
            elif t == "user" and isinstance(msg.get("content"), list):
                for b in msg["content"]:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        results[b.get("tool_use_id")] = bool(b.get("is_error"))
    return sid, {
        "api_requests": len(reqs), "output_tokens": sum(r["out"] for r in reqs.values()),
        "input_tokens": sum(r["in"] for r in reqs.values()), "cache_read_tokens": sum(r["cr"] for r in reqs.values()),
        "tool_calls": len(uses), "tool_errors": sum(1 for i in uses if results.get(i)),
        "ask_questions": sum(len(inp.get("questions") or ()) for name, inp in uses.values() if name == "AskUserQuestion"),
        "skill_calls": sum(1 for name, _ in uses.values() if name == "Skill"),
    }


def _psql(psql, sql):
    res = subprocess.run(psql + ["-A", "-t", "-F", "\t", "-c", sql], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip())
    return res.stdout


def warehouse_counts(psql):
    rows = {}
    for line in _psql(psql, _SQL).splitlines():
        parts = line.split("\t")
        if len(parts) == len(FIELDS) + 1:
            rows[parts[0]] = dict(zip(FIELDS, (int(x) for x in parts[1:])))
    loaded = _psql(psql, "SELECT extract(epoch FROM max(loaded_at)) * 1000 FROM warehouse_load").strip()
    return rows, (float(loaded) if loaded else None)


def check(psql, claude_dir=None):
    """Every session in scope, recounted from its transcript and compared with the warehouse."""
    wh, loaded_ms = warehouse_counts(psql)
    out = {"loaded_ms": loaded_ms, "checked": 0, "matched": 0, "live": [], "differ": [], "missing": [],
           "raw": dict.fromkeys(FIELDS, 0), "warehouse": dict.fromkeys(FIELDS, 0)}
    for p in locate.iter_transcripts(locate.claude_dir(claude_dir)):
        sid, raw = raw_counts(p)
        if sid not in wh:
            if raw["api_requests"] or raw["tool_calls"]:
                out["missing"].append({"session_id": sid, "transcript": str(p), "raw": raw})
            continue
        out["checked"] += 1
        if loaded_ms is not None and p.stat().st_mtime * 1000 > loaded_ms:
            out["live"].append(sid)  # written after the load: not comparable
            continue
        for k in FIELDS:
            out["raw"][k] += raw[k]
            out["warehouse"][k] += wh[sid][k]
        bad = {k: {"raw": raw[k], "warehouse": wh[sid][k]} for k in FIELDS if raw[k] != wh[sid][k]}
        if bad:
            out["differ"].append({"session_id": sid, "transcript": str(p), "counts": bad})
        else:
            out["matched"] += 1
    out["ok"] = not out["differ"]
    return out


def render(res):
    lines = [f"# Warehouse check\n\n{res['checked']} sessions recounted from their raw JSONL: {res['matched']} match on "
             f"every count, {len(res['differ'])} differ, {len(res['live'])} written since the load (live, not "
             f"compared), {len(res['missing'])} with activity the warehouse does not have.\n",
             "| Count | Raw JSONL | Warehouse |", "|---|---:|---:|"]
    for k in FIELDS:
        mark = "" if res["raw"][k] == res["warehouse"][k] else " ≠"
        lines.append(f"| {k.replace('_', ' ')} | {res['raw'][k]:,} | {res['warehouse'][k]:,}{mark} |")
    for d in res["differ"][:15]:
        lines.append(f"\n- {d['session_id']}: " + ", ".join(f"{k} raw {v['raw']:,} ≠ warehouse {v['warehouse']:,}"
                                                          for k, v in d["counts"].items()))
    for m in res["missing"][:10]:
        lines.append(f"\n- not loaded: {m['session_id']} ({m['transcript']})")
    return "\n".join(lines) + "\n"
