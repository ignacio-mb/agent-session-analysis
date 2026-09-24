"""Check the warehouse against the raw transcripts it was loaded from.

    session-analytics warehouse --check        (make warehouse-check; make warehouse runs it after every load)

Every session is recounted straight from its JSONL with plain `json` — none of the parser's code — and compared
with the warehouse rows: API requests, input/output/cache-read tokens, tool calls, failed tool calls,
AskUserQuestion questions, Skill tool calls. The recount follows the exporter's rules, so a difference is a bug:
the main file plus <session>/**/*.jsonl (subagents, workflow agents; not journal.jsonl); main-file lines stamped
with another session id are history a resumed session copied in, and are skipped; `<synthetic>` messages are not
API requests; the lines a streamed response was written in are merged by (message id, request id), keeping the
largest usage figures. A session any of whose files (main, subagents, workflow agents) was written after the load
is live: reported apart, not compared.
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
    newest = 0.0
    for f in files:
        is_main = f == main
        try:
            newest = max(newest, f.stat().st_mtime)
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
        "_newest_ms": newest * 1000,
    }


def _psql(psql, sql):
    res = subprocess.run(psql + ["-A", "-t", "-F", "\t", "-c", sql], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip())
    return res.stdout


# The same counts from a ClickHouse load (clickhouse.py), for the sessions this machine loaded (its source): one
# pass per table instead of a subquery per session.
_CH_SQL = """SELECT s.session_id AS session_id,
  ifNull(a.requests, 0) AS api_requests, ifNull(a.output_tokens, 0) AS output_tokens,
  ifNull(a.input_tokens, 0) AS input_tokens, ifNull(a.cache_read_tokens, 0) AS cache_read_tokens,
  ifNull(t.calls, 0) AS tool_calls, ifNull(t.failed, 0) AS tool_errors, ifNull(q.asked, 0) AS ask_questions,
  ifNull(t.skill_calls, 0) AS skill_calls
FROM {db}.sessions AS s
LEFT JOIN (SELECT r.session_id AS session_id, count() AS requests, sum(r.output_tokens) AS output_tokens,
                  sum(r.input_tokens) AS input_tokens, sum(r.cache_read_tokens) AS cache_read_tokens
           FROM {db}.api_requests AS r WHERE r.source = {{source:String}} GROUP BY r.session_id) AS a
  ON a.session_id = s.session_id
LEFT JOIN (SELECT c.session_id AS session_id, countIf(c.tool != '(unmatched)') AS calls,
                  countIf(c.tool != '(unmatched)' AND c.status IN ('error', 'denied', 'interrupted')) AS failed,
                  countIf(c.tool = 'Skill') AS skill_calls
           FROM {db}.tool_calls AS c WHERE c.source = {{source:String}} GROUP BY c.session_id) AS t
  ON t.session_id = s.session_id
LEFT JOIN (SELECT x.session_id AS session_id, countIf(x.channel = 'ask') AS asked
           FROM {db}.questions AS x WHERE x.source = {{source:String}} GROUP BY x.session_id) AS q
  ON q.session_id = s.session_id
WHERE s.source = {{source:String}}"""


def warehouse_counts(source):
    """{session: {count: n}} and when the warehouse was loaded (ms), from Postgres (a psql command) or ClickHouse
    (a (clickhouse.Client, source id) pair: that machine's rows)."""
    if isinstance(source, tuple):
        from .clickhouse import ident
        client, src = source
        db, p = ident(client.t.database), {"source": src}
        rows = {r["session_id"]: {k: int(r[k] or 0) for k in FIELDS} for r in client.rows(_CH_SQL.format(db=db), p)}
        loaded = client.rows(f"SELECT toUnixTimestamp64Milli(max(loaded_at)) AS ms FROM {db}.warehouse_load "
                             f"WHERE source = {{source:String}}", p)
        return rows, (float(loaded[0]["ms"]) if loaded and rows else None)
    psql = source
    rows = {}
    for line in _psql(psql, _SQL).splitlines():
        parts = line.split("\t")
        if len(parts) == len(FIELDS) + 1:
            rows[parts[0]] = dict(zip(FIELDS, (int(x) for x in parts[1:])))
    loaded = _psql(psql, "SELECT extract(epoch FROM max(loaded_at)) * 1000 FROM warehouse_load").strip()
    return rows, (float(loaded) if loaded else None)


def check(source, claude_dir=None, only=None):
    """Every session in scope, recounted from its transcript and compared with the warehouse (a psql command, or a
    (clickhouse.Client, source id) pair). `only`: the session ids the warehouse is meant to hold (a ClickHouse load
    shares the sessions that invoked its skills); the others are not looked at."""
    wh, loaded_ms = warehouse_counts(source)
    out = {"loaded_ms": loaded_ms, "checked": 0, "matched": 0, "live": [], "differ": [], "missing": [],
           "raw": dict.fromkeys(FIELDS, 0), "warehouse": dict.fromkeys(FIELDS, 0)}
    for p in locate.iter_transcripts(locate.claude_dir(claude_dir)):
        if only is not None and p.stem not in only:
            continue
        sid, raw = raw_counts(p)
        if sid not in wh:
            if raw["api_requests"] or raw["tool_calls"]:
                out["missing"].append({"session_id": sid, "transcript": str(p), "raw": {k: raw[k] for k in FIELDS}})
            continue
        out["checked"] += 1
        if loaded_ms is not None and raw["_newest_ms"] > loaded_ms:
            # Written after the load (a live session, or its subagents and workflow agents): not comparable.
            out["live"].append(sid)
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


def render(res, title="Warehouse check"):
    lines = [f"# {title}\n\n{res['checked']} sessions recounted from their raw JSONL: {res['matched']} match on "
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
