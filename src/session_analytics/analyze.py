"""Compute every analytic a session's records support, as one JSON-serialisable dict.

The dict is the product: session.json is this dict, the Markdown and HTML reports
and the CSVs are views of it. Sections are documented in docs/metrics.md.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shlex
import time
from collections import Counter, defaultdict
from urllib.parse import urlparse

from . import __version__, questions, skillfiles, skillruns, util
from .parse import BUILTIN_COMMANDS, mcp_parts, tool_category
from .pricing import COMPONENTS
from .redact import Redactor
from .util import strip_heredocs

SCHEMA = "convo-analysis/v1"
IDLE_GAP_MS = 30 * 60 * 1000
LIVE_WINDOW_MS = 10 * 60 * 1000

SHELL_KEYWORDS = {"if", "then", "else", "elif", "fi", "for", "do", "done", "while", "until", "in", "case",
                  "esac", "function", "{", "}", "(", ")", "!"}
SHELL_WRAPPERS = {"sudo", "time", "nohup", "env", "command", "exec", "builtin", "nice"}
MULTI_LEVEL = {"git", "gh", "uv", "docker", "make", "npm", "npx", "pnpm", "yarn", "kubectl", "terraform", "aws",
               "gcloud", "cargo", "go", "pip", "pip3", "brew", "helm", "poetry", "bun", "deno", "mb", "dq",
               "ingest", "job", "claude", "systemctl", "launchctl", "psql", "clickhouse", "docker-compose"}
ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
PROG_RE = re.compile(r"^[A-Za-z0-9_.+@-]+$")  # a program name, not a stray awk/jq program or quoted string

ERROR_CATEGORIES = [
    ("sibling_cancelled", re.compile(r"Cancelled: parallel tool call|sibling tool call", re.I)),
    ("blocked_by_harness", re.compile(r"(?:^|>)\s*Blocked:", re.I)),
    ("worktree_isolation", re.compile(r"is isolated in the worktree", re.I)),
    ("schema_mismatch", re.compile(r"does not match required schema", re.I)),
    ("input_validation", re.compile(r"InputValidationError|Invalid tool parameters|invalid_type|is not valid|"
                                    r"takes a process id|must be one of", re.I)),
    ("script_error", re.compile(r"Invalid workflow script|Script parse error|SyntaxError", re.I)),
    ("file_not_read_first", re.compile(r"has not been read yet|must read .* before|Read it first", re.I)),
    ("edit_no_match", re.compile(r"String to replace not found|No changes to make|Found \d+ matches of the string", re.I)),
    ("file_changed_since_read", re.compile(r"modified since read|has been modified since", re.I)),
    ("unknown_skill_or_tool", re.compile(r"Unknown skill|No such tool|Unknown tool", re.I)),
    ("tool_precondition", re.compile(r"No site is open|shows a local file|couldn't open file|navigate` first|"
                                     r"requires a prior", re.I)),
    ("permission", re.compile(r"Permission denied|EACCES|Operation not permitted|not permitted|"
                              r"denied by your permission settings", re.I)),
    ("file_not_found", re.compile(r"does not exist|No such file|ENOENT|File not found", re.I)),
    ("timeout", re.compile(r"timed out|timeout", re.I)),
    ("network", re.compile(r"ECONNREFUSED|ENOTFOUND|getaddrinfo|Connection refused|ECONNRESET|socket hang up", re.I)),
    ("http", re.compile(r"\b(?:status(?: code)?|HTTP)\s*:?\s*[45]\d\d\b|\b[45]\d\d (?:Not Found|Forbidden|Unauthorized|"
                        r"Internal Server Error|Bad Request)|Too many redirects", re.I)),
    ("jq_error", re.compile(r"\bjq: error")),
    ("python_exception", re.compile(r"Traceback \(most recent call last\)")),
    ("shell_syntax", re.compile(r"parse error near|syntax error near unexpected|unexpected EOF while looking")),
    ("exit_code", re.compile(r"^Exit code -?\d+")),
]


HOME = os.path.expanduser("~")


def _cwd_rel(path, cwd):
    """Paths inside the project are shown relative to it; elsewhere under $HOME as ~/..."""
    if not path or not isinstance(path, str):
        return path
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        return path[len(cwd.rstrip("/")) + 1:]
    if path.startswith(HOME + "/"):
        return "~" + path[len(HOME):]
    return path


def _dir_group(path):
    """Top-level directory for grouping: `src` for project paths, `~/dev/other` or `/tmp/x` outside it."""
    parts = [p for p in (path or "").split("/") if p]
    if not parts:
        return "."
    if path.startswith("~") or path.startswith("/"):
        head = "~" if path.startswith("~") else ""
        rest = parts[1:] if head else parts
        return ((head + "/") if head else "/") + "/".join(rest[:2]) if len(rest) > 2 else os.path.dirname(path) or "/"
    return parts[0] if len(parts) > 1 else "."


def _ext(path):
    base = os.path.basename(path or "")
    if base.startswith(".") and base.count(".") == 1:
        return base
    _, ext = os.path.splitext(base)
    return ext.lower() or "(none)"


def _domain(url):
    try:
        return urlparse(url or "").netloc or "(invalid)"
    except ValueError:
        return "(invalid)"


NAVIGATION = {"cd", "pushd", "popd", "export", "source", ".", "set", "unset", "alias", "true", ":"}


def split_segments(cmd):
    """Split a command line on && || ; | and newlines, outside quotes; drop comments."""
    segs, buf, q, i = [], [], None, 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if q:
            buf.append(ch)
            if ch == "\\" and q == '"' and i + 1 < n:
                buf.append(cmd[i + 1])
                i += 2
                continue
            if ch == q:
                q = None
        elif ch in ("'", '"'):
            q = ch
            buf.append(ch)
        elif ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(cmd[i + 1])
            i += 2
            continue
        elif cmd.startswith("&&", i) or cmd.startswith("||", i):
            segs.append("".join(buf))
            buf = []
            i += 2
            continue
        elif ch in ";|\n":
            segs.append("".join(buf))
            buf = []
        elif ch == "#" and (not buf or buf[-1].isspace()):
            j = cmd.find("\n", i)
            i = n if j < 0 else j
            continue
        else:
            buf.append(ch)
        i += 1
    segs.append("".join(buf))
    return [x.strip() for x in segs if x.strip()]


def _shell_tokens(seg):
    try:
        return shlex.split(seg, posix=True)
    except ValueError:
        return seg.split()


@functools.lru_cache(maxsize=4096)
def _programs_cached(cmd):
    return tuple(_programs(cmd))


def programs_of(cmd):
    """Programs a shell command runs: [(program, subcommand-or-None)], one per pipeline segment."""
    return list(_programs_cached(cmd or ""))


def _programs(cmd):
    out = []
    for seg in split_segments(strip_heredocs(cmd or "")):
        toks = _shell_tokens(seg)
        if toks and toks[0] in ("for", "select", "case"):
            continue  # loop/case headers name variables and patterns, not programs
        i = 0
        while i < len(toks):
            t = toks[i]
            if ENV_ASSIGN.match(t) or t in SHELL_KEYWORDS or t in SHELL_WRAPPERS:
                i += 1
                continue
            if t == "timeout" and i + 1 < len(toks):
                i += 2
                continue
            break
        if i >= len(toks):
            continue
        if toks[i] in ("[", "[["):
            out.append(("test", None))
            continue
        prog = os.path.basename(toks[i].strip("()'\"{}`$"))
        if not prog or prog[0] in "#<>-" or not PROG_RE.match(prog):
            continue
        sub = None
        args = toks[i + 1:]
        if prog == "git":
            # global options that take a value: git -C <dir> -c <k=v> status
            kept, j = [], 0
            while j < len(args):
                if args[j] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") and j + 1 < len(args):
                    j += 2
                    continue
                kept.append(args[j])
                j += 1
            args = kept
        rest = [x for x in args if not x.startswith("-")]
        if prog in ("python", "python3"):
            if "-m" in toks[i + 1:i + 3]:
                j = toks.index("-m", i + 1)
                sub = "-m " + toks[j + 1] if j + 1 < len(toks) else None
        elif prog == "uv" and rest[:1] == ["run"] and len(rest) > 1:
            sub = "run " + os.path.basename(rest[1])
        elif prog in MULTI_LEVEL and rest:
            sub = rest[0]
        out.append((prog, sub))
    return out


SIG_LEVELS = {"git": 1, "terraform": 1, "brew": 1, "pip": 1, "pip3": 1, "helm": 1, "make": 1, "cargo": 1,
              "go": 1, "gcloud": 3, "uv": 2, "npm": 2, "pnpm": 2, "yarn": 2}
SUBCOMMAND = re.compile(r"^[a-z][a-z0-9_-]*$")
GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}


@functools.lru_cache(maxsize=4096)
def _cli_cached(cmd):
    out = []
    for seg in split_segments(strip_heredocs(cmd)):
        toks = _shell_tokens(seg)
        if toks and toks[0] in ("for", "select", "case"):
            continue
        i = 0
        while i < len(toks) and (ENV_ASSIGN.match(toks[i]) or toks[i] in SHELL_KEYWORDS or toks[i] in SHELL_WRAPPERS):
            i += 1
        if i >= len(toks):
            continue
        prog = os.path.basename(toks[i].strip("()'\"{}`$"))
        if not prog or prog[0] in "#<>-" or toks[i] in ("[", "[[") or not PROG_RE.match(prog):
            continue
        levels = SIG_LEVELS.get(prog, 2 if prog in MULTI_LEVEL else 0)
        words, j = [], i + 1
        while j < len(toks) and len(words) < levels:
            t = toks[j]
            if prog == "git" and t in GIT_VALUE_OPTS:
                j += 2
                continue
            if t.startswith("-") and not words:
                j += 1
                continue
            if not SUBCOMMAND.match(t):
                break
            words.append(t)
            j += 1
        rest = toks[i + 1:]
        is_help = "--help" in rest or "-h" in rest or bool(words and words[-1] == "help")
        out.append((prog, " ".join([prog] + words), is_help, seg))
    return tuple(out)


def cli_calls(cmd):
    """Each CLI invocation in a shell command: [{program, signature, help, segment}].

    The signature keeps subcommands for CLIs that have them (`mb transform create`, `gh pr view`,
    `git commit`) and just the program otherwise (`cat`, `grep`).
    """
    return [{"program": p, "signature": sig, "help": h, "segment": seg} for p, sig, h, seg in _cli_cached(cmd or "")]


def primary_program(cmd):
    """The program a command is 'about': the first one that is not navigation (`cd x && pytest` -> pytest)."""
    progs = programs_of(cmd)
    for p, sub in progs:
        if p not in NAVIGATION:
            return p, sub
    return progs[0] if progs else (None, None)


def categorize_error(text):
    t = (text or "")[:1500]
    t = t.replace("<tool_use_error>", "").replace("</tool_use_error>", "").strip()
    for name, pat in ERROR_CATEGORIES:
        if pat.search(t):
            return name
    return "other"


def summarize_input(call):
    n, i = call.name, call.input or {}
    if n == "Bash":
        return i.get("command") or ""
    if n in ("Read", "Edit", "Write", "MultiEdit"):
        extra = ""
        if n == "Read" and (i.get("offset") or i.get("limit")):
            extra = f" [{i.get('offset') or 0}:+{i.get('limit') or ''}]"
        if n == "Edit" and i.get("replace_all"):
            extra = " (replace all)"
        return (i.get("file_path") or "") + extra
    if n == "NotebookEdit":
        return i.get("notebook_path") or ""
    if n == "Glob":
        return (i.get("pattern") or "") + (f"  in {i['path']}" if i.get("path") else "")
    if n == "Grep":
        s = f"/{i.get('pattern') or ''}/"
        if i.get("glob"):
            s += f" glob={i['glob']}"
        if i.get("path"):
            s += f" in {i['path']}"
        return s
    if n == "WebFetch":
        return i.get("url") or ""
    if n == "WebSearch":
        return i.get("query") or ""
    if n in ("Agent", "Task"):
        return f"{i.get('subagent_type') or 'agent'}: {i.get('description') or ''}"
    if n == "Skill":
        return f"{i.get('skill') or ''} {i.get('args') or ''}".strip()
    if n == "SlashCommand":
        return i.get("command") or ""
    if n == "ToolSearch":
        return i.get("query") or ""
    if n == "TaskCreate":
        return i.get("subject") or ""
    if n == "TaskUpdate":
        return f"#{i.get('taskId')} → {i.get('status') or ''}".strip()
    if n == "TodoWrite":
        return f"{len(i.get('todos') or [])} todos"
    if n == "AskUserQuestion":
        qs = i.get("questions") or []
        if qs and isinstance(qs[0], dict):
            return qs[0].get("question") or qs[0].get("header") or ""
        return ""
    if n == "Workflow":
        if i.get("description"):
            return i["description"]
        m = re.search(r"name:\s*['\"]([^'\"]+)", i.get("script") or "")
        return m.group(1) if m else (i.get("scriptPath") or "workflow")
    if n == "SendUserFile":
        return ", ".join(str(x) for x in (i.get("files") or []))
    if n == "Artifact":
        return f"{i.get('action') or 'publish'} {i.get('file_path') or i.get('url') or ''}".strip()
    if n == "ExitPlanMode":
        return f"plan ({len(i.get('plan') or '')} chars)"
    if n in ("TaskStop", "TaskOutput", "KillShell"):
        return str(i.get("task_id") or i.get("shell_id") or "")
    try:
        return json.dumps(i, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(i)


def target_of(call):
    """What a call acted on, for grouping: a path, a program, a domain, a query, a skill."""
    n, f, i = call.name, call.facts, call.input or {}
    if n == "Bash":
        return primary_program(i.get("command"))[0] or ""
    if n in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        return f.get("path") or i.get("file_path") or i.get("notebook_path") or ""
    if n == "WebFetch":
        return _domain(f.get("url") or i.get("url"))
    if n == "WebSearch":
        return i.get("query") or ""
    if n in ("Agent", "Task"):
        return i.get("subagent_type") or "agent"
    if n == "Skill":
        return i.get("skill") or ""
    if n in ("Glob", "Grep"):
        return i.get("pattern") or ""
    if n.startswith("mcp__"):
        return mcp_parts(n)[1]
    return ""


def skill_source(base_dir, name, home):
    if name and ":" in name:
        return "plugin"
    if not base_dir:
        return "unknown"
    b = base_dir
    if "bundled-skills" in b:
        return "bundled"
    if "/plugins/" in b:
        return "plugin"
    if b.startswith(os.path.join(home, ".claude", "skills")):
        return "user"
    if "/.claude/skills/" in b or b.endswith("/.claude/skills"):
        return "project"
    if "/Library/Application Support/" in b:
        return "app"
    return "other"


class _Ctx:
    def __init__(self, session, pricing, redactor, full, current_id, now_ms, checks=(), skill_sources=()):
        self.checks = tuple(checks or ())
        self.skill_sources = tuple(skill_sources or ())
        self.s = session
        self.pricing = pricing
        self.R = redactor
        self.full = full
        self.current_id = current_id
        self.now_ms = now_ms
        self.home = os.path.expanduser("~")
        self.cwd = session.cwds.most_common(1)[0][0] if session.cwds else None
        self.lim_prompt = None if full else 400
        self.lim_input = 4000 if full else 300
        self.lim_result = 4000 if full else 400

    def text(self, s, limit):
        """Redact first, then collapse and truncate (truncating first could split a secret past the pattern)."""
        return util.one_line(self.R(s or ""), limit)

    def block(self, s, limit):
        return util.clip(self.R(s or ""), limit)


def usage_totals(reqs):
    t = {"requests": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cache_write_5m": 0,
         "cache_write_1h": 0, "thinking": 0, "web_search": 0, "web_fetch": 0, "cost": 0.0, "unpriced_requests": 0}
    comp = Counter()
    for r in reqs:
        if r.model == "<synthetic>":
            continue
        t["requests"] += 1
        t["input"] += r.input_tokens
        t["output"] += r.output_tokens
        t["cache_read"] += r.cache_read_tokens
        t["cache_write"] += r.cache_write_tokens
        t["cache_write_5m"] += r.cache_write_5m_tokens
        t["cache_write_1h"] += r.cache_write_1h_tokens
        t["thinking"] += r.thinking_tokens
        t["web_search"] += r.web_search_requests
        t["web_fetch"] += r.web_fetch_requests
        if r.cost is None:
            t["unpriced_requests"] += 1
        else:
            t["cost"] += r.cost["total"]
            for c in COMPONENTS:
                comp[c] += r.cost[c]
    t["total_tokens"] = t["input"] + t["output"] + t["cache_read"] + t["cache_write"]
    t["cache_hit_ratio"] = util.ratio(t["cache_read"], t["input"] + t["cache_read"] + t["cache_write"])
    t["cost_components"] = {c: comp.get(c, 0.0) for c in COMPONENTS}
    return t


def analyze(session, pricing, redactor=None, full=False, current_id=None, now_ms=None, checks=(), skill_sources=()):
    s = session
    R = redactor if redactor is not None else Redactor(True)
    ctx = _Ctx(s, pricing, R, full, current_id, now_ms if now_ms is not None else time.time() * 1000,
               checks=checks, skill_sources=skill_sources)

    for r in s.requests.values():
        unsplit = max(0, r.cache_write_tokens - r.cache_write_5m_tokens - r.cache_write_1h_tokens)
        r.cost = pricing.cost(r.model, r.input_tokens, r.output_tokens, r.cache_read_tokens,
                              r.cache_write_5m_tokens, r.cache_write_1h_tokens, unsplit, r.web_search_requests,
                              r.speed)

    reqs = sorted((r for r in s.requests.values() if r.model != "<synthetic>"),
                  key=lambda r: (r.ts_first or 0))
    calls = sorted(s.tool_calls.values(), key=lambda c: (c.ts_call or c.ts_result or 0))

    out = {"schema": SCHEMA, "generator": {"name": "convo-analysis", "version": __version__,
                                           "generated_at": util.iso(ctx.now_ms), "full_content": bool(full),
                                           "redaction": bool(getattr(R, "enabled", True))}}
    out["session"] = _session(ctx, reqs)
    out["lineage"] = _lineage(ctx, reqs, calls)
    out["tokens"] = _tokens(ctx, reqs)
    out["cost"] = _cost(ctx, reqs)
    out["reported"] = _reported(ctx, reqs)
    out["timing"] = _timing(ctx, reqs, calls)
    out["turns"] = _turns(ctx, reqs, calls)
    out["requests"] = _requests(ctx, reqs)
    out["tools"] = _tools(ctx, calls)
    out["skills"] = _skills(ctx, reqs, calls)
    out["subagents"] = _subagents(ctx, reqs, calls)
    out["workflows"] = _workflows(ctx, reqs, calls)
    out["files"] = _files(ctx, calls)
    out["shell"] = _shell(ctx, calls)
    out["git"] = _git(ctx, calls)
    out["web"] = _web(ctx, calls, reqs)
    out["planning"] = _planning(ctx, calls)
    out["user"] = _user(ctx, calls, out["timing"])
    out["errors"] = _errors(ctx, calls, reqs)
    out["hooks"] = _hooks(ctx)
    out["context"] = _context(ctx, reqs)
    out["outputs"] = _outputs(ctx, calls, out["git"])
    out["timeline"] = _timeline(ctx, reqs, calls, out)
    docs = skillfiles.DocSet.for_session(ctx.s)
    out["trace"] = _trace(ctx, reqs, calls, docs)
    raw_qs = questions.raw_questions(ctx.s, reqs, calls, ctx.text)
    out["skill_runs"] = skillruns.build_runs(ctx, reqs, calls, out["trace"], check_files=ctx.checks,
                                             sources_extra=ctx.skill_sources, docs=docs, raw_qs=raw_qs)
    out["interview"] = _interview(ctx, raw_qs, out["skill_runs"])
    out["schema_coverage"] = _schema(ctx)
    out["totals"] = _totals(out)
    out["insights"] = _insights(out)
    out["generator"]["redacted_fields"] = getattr(R, "hits", None)
    return out


# ---------------------------------------------------------------------- session


def _session(ctx, reqs):
    s = ctx.s
    first_prompt = next((t.text for t in s.turns if t.trigger == "prompt" and t.text), "")
    title = (s.titles.get("custom") or s.titles.get("ai") or s.titles.get("agent_name")
             or s.titles.get("summary") or util.one_line(first_prompt, 80) or s.session_id)
    by_model = Counter(r.model for r in reqs)
    models = [{"model": m, "name": s.model_identities.get(m) or s.model_identities.get(m.split("[")[0]),
               "requests": n} for m, n in by_model.most_common()]
    wall = (s.last_ts - s.first_ts) if (s.first_ts is not None and s.last_ts is not None) else None
    live = bool(ctx.current_id and ctx.current_id == s.session_id) or (
        s.last_ts is not None and ctx.now_ms - s.last_ts < LIVE_WINDOW_MS and s.live_turn is not None)
    env = s.environment or {}
    wt = s.worktree or {}
    return {
        "id": s.session_id,
        "title": ctx.text(title, 160),
        "titles": {k: ctx.text(v, 300) for k, v in s.titles.items()},
        "transcript": str(s.main_path),
        "project_dir": s.project_dir,
        "cwd": ctx.cwd,
        "cwds": [c for c, _ in s.cwds.most_common()],
        "project_name": os.path.basename(ctx.cwd.rstrip("/")) if ctx.cwd else s.project_dir,
        "relocated_to": [r for r in s.relocated if r],
        "git_branches": [{"branch": b, "first_seen": util.iso(ts)} for b, ts in s.branches.items()],
        "claude_code_versions": [v for v, _ in s.versions.most_common()],
        "entrypoints": dict(s.entrypoints),
        "user_types": dict(s.user_types),
        "slugs": [x for x, _ in s.slugs.most_common()],
        "models": models,
        "start": util.iso(s.first_ts),
        "end": util.iso(s.last_ts),
        "start_ms": s.first_ts,
        "end_ms": s.last_ts,
        "wall_ms": wall,
        "live": live,
        "permission_modes": [{"at": util.iso(ts), "mode": m} for ts, m in s.permission_modes],
        "modes": [{"at": util.iso(ts), "mode": m} for ts, m in s.modes],
        "effort_levels": dict(Counter(r.effort for r in reqs if r.effort)),
        "worktree": {k: wt.get(k) for k in ("worktreeName", "worktreeBranch", "worktreePath", "originalBranch",
                                            "originalCwd") if wt.get(k)} or None,
        "continued_in": [c for c in s.continued_in if c],
        "remote_control_bridge": s.bridge_events > 0,
        "environment": {k: env.get(k) for k in ("platform", "osVersion", "shell", "isGitRepo", "isWorktree",
                                                "workingDirectory", "additionalWorkingDirectories")
                        if k in env} or None,
        "has_git_status_context": s.has_git_status,
    }


def _lineage(ctx, reqs, calls):
    """How much of this transcript was copied in from earlier sessions (resume / continue / fork)."""
    s = ctx.s
    own = [r for r in reqs if not r.inherited]
    inh = [r for r in reqs if r.inherited]
    uo, ui = usage_totals(own), usage_totals(inh)
    return {
        "continues": [{"session_id": sid, "events": n, "first": util.iso(s.lineage_span.get(sid, [None, None])[0]),
                       "last": util.iso(s.lineage_span.get(sid, [None, None])[1])} for sid, n in s.lineage.most_common()],
        "own_only": s.own_only,
        "inherited_events_skipped": s.inherited_events_skipped,
        "own": {"requests": uo["requests"], "cost_usd": round(uo["cost"], 6), "output_tokens": uo["output"],
                "total_tokens": uo["total_tokens"], "tool_calls": sum(1 for c in calls if not c.inherited),
                "turns": sum(1 for t in s.turns if not t.inherited)},
        "inherited": {"requests": ui["requests"], "cost_usd": round(ui["cost"], 6), "output_tokens": ui["output"],
                      "total_tokens": ui["total_tokens"], "tool_calls": sum(1 for c in calls if c.inherited),
                      "turns": sum(1 for t in s.turns if t.inherited),
                      "skill_invocations": sum(1 for i in s.skills if i.inherited)},
        "note": "A resumed or continued session's transcript begins with a copy of the earlier conversation; those "
                "lines keep the earlier session's id. Everything here includes them unless exported with "
                "--own-only; `own` is what happened in this session itself.",
    }


# ---------------------------------------------------------------------- tokens & cost


def _scope_of(r):
    return "main" if r.scope == "main" else ("workflow" if r.scope == "workflow" else "subagent")


def _tokens(ctx, reqs):
    by_model = defaultdict(list)
    by_scope = defaultdict(list)
    for r in reqs:
        by_model[r.model].append(r)
        by_scope[_scope_of(r)].append(r)
    overall = usage_totals(reqs)
    main = [r for r in reqs if r.scope == "main"]
    ctx_vals = [r.context_tokens for r in main]
    miss = defaultdict(lambda: {"requests": 0, "missed_tokens": 0})
    for r in reqs:
        if r.cache_miss_reason:
            miss[r.cache_miss_reason]["requests"] += 1
            miss[r.cache_miss_reason]["missed_tokens"] += r.cache_missed_tokens
    peak = max(main, key=lambda r: r.context_tokens) if main else None
    return {
        "overall": overall,
        "by_model": {m: usage_totals(v) for m, v in sorted(by_model.items(), key=lambda kv: -len(kv[1]))},
        "by_scope": {k: usage_totals(v) for k, v in by_scope.items()},
        "cache": {
            "hit_ratio": overall["cache_hit_ratio"],
            "read": overall["cache_read"],
            "write_5m": overall["cache_write_5m"],
            "write_1h": overall["cache_write_1h"],
            "uncached_input": overall["input"],
            "miss_reasons": dict(miss),
            "requests_with_cache_read": sum(1 for r in reqs if r.cache_read_tokens),
            "requests_without_cache_read": sum(1 for r in reqs if not r.cache_read_tokens),
        },
        "context": {
            "peak_tokens": peak.context_tokens if peak else None,
            "peak_at": util.iso(peak.ts_first) if peak else None,
            "peak_model": peak.model if peak else None,
            "mean_tokens": (sum(ctx_vals) / len(ctx_vals)) if ctx_vals else None,
            "last_tokens": main[-1].context_tokens if main else None,
        },
        "output": {
            "total": overall["output"],
            "thinking_tokens": overall["thinking"],
            "thinking_share": util.ratio(overall["thinking"], overall["output"]),
            "per_request": util.describe([r.output_tokens for r in reqs]),
        },
        "service_tiers": dict(Counter(r.service_tier for r in reqs if r.service_tier)),
        "speeds": dict(Counter(r.speed for r in reqs if r.speed)),
        "inference_geos": dict(Counter(r.inference_geo for r in reqs if r.inference_geo)),
    }


def _cost(ctx, reqs):
    total = usage_totals(reqs)
    by_model = {m: round(v["cost"], 6) for m, v in
                ((m, usage_totals([r for r in reqs if r.model == m])) for m in {r.model for r in reqs})}
    by_scope = defaultdict(float)
    by_skill = defaultdict(float)
    by_agent = defaultdict(float)
    by_mcp = defaultdict(float)
    by_turn = defaultdict(float)
    for r in reqs:
        c = r.cost["total"] if r.cost else 0.0
        by_scope[_scope_of(r)] += c
        by_skill[r.attribution_skill or "(no skill)"] += c
        by_agent[r.attribution_agent or ("main thread" if r.scope == "main" else r.scope)] += c
        if r.attribution_mcp_server:
            by_mcp[r.attribution_mcp_server] += c
        by_turn[r.turn] += c
    unknown = sorted({r.model for r in reqs if r.cost is None})
    series, cum = [], 0.0
    for r in reqs:
        cum += r.cost["total"] if r.cost else 0.0
        series.append([r.ts_last or r.ts_first, round(cum, 6)])
    top_turns = sorted(((t, c) for t, c in by_turn.items() if t is not None), key=lambda kv: -kv[1])[:15]
    top_requests = sorted(reqs, key=lambda r: -(r.cost["total"] if r.cost else 0))[:10]
    return {
        "estimated_usd": round(total["cost"], 6),
        "components": {k: round(v, 6) for k, v in total["cost_components"].items()},
        "by_model": dict(sorted(by_model.items(), key=lambda kv: -kv[1])),
        "by_scope": {k: round(v, 6) for k, v in by_scope.items()},
        "by_skill": {k: round(v, 6) for k, v in sorted(by_skill.items(), key=lambda kv: -kv[1])},
        "by_agent": {k: round(v, 6) for k, v in sorted(by_agent.items(), key=lambda kv: -kv[1])},
        "by_mcp_server": {k: round(v, 6) for k, v in sorted(by_mcp.items(), key=lambda kv: -kv[1])},
        "top_turns": [{"turn": t, "usd": round(c, 6)} for t, c in top_turns],
        "top_requests": [{"ts": util.iso(r.ts_first), "turn": r.turn, "model": r.model, "scope": r.scope,
                          "usd": round(r.cost["total"], 6) if r.cost else None, "context": r.context_tokens,
                          "output": r.output_tokens} for r in top_requests],
        "cumulative": series,
        "unpriced_models": unknown,
        "unpriced_requests": total["unpriced_requests"],
        "pricing": ctx.pricing.describe(),
        "notes": [
            "List-price estimate from the tokens recorded in the transcript; subscription plans are not billed "
            "per token, so read this as the API-equivalent cost.",
            "Cache writes are priced by TTL (5m = 1.25x input, 1h = 2x input); thinking is billed as output.",
            "Claude Code also makes calls it does not write to the transcript (session titles, compaction "
            "summaries, suggestions); compare with `reported` when present.",
        ],
    }


def _reported(ctx, reqs):
    """Claude Code's own accounting, from the last `cost-state` event (written when a session idles/exits)."""
    s = ctx.s
    if not s.cost_states:
        return None
    cs = s.cost_states[-1]
    by_model = {}
    for model, mu in (cs.get("modelUsage") or {}).items():
        if not isinstance(mu, dict):
            continue
        by_model[model] = {
            "input": mu.get("inputTokens"), "output": mu.get("outputTokens"),
            "cache_read": mu.get("cacheReadInputTokens"), "cache_write": mu.get("cacheCreationInputTokens"),
            "thinking": mu.get("thinkingTokens"), "web_search": mu.get("webSearchRequests"),
            "cost_usd": mu.get("costUSD"),
        }
    est = usage_totals(reqs)["cost"]
    reported = cs.get("totalCostUSD")
    recon = []
    for model, mu in by_model.items():
        mine = usage_totals([r for r in reqs if r.model.split("[")[0] == model.split("[")[0]])
        recon.append({
            "model": model,
            "reported_cost": mu.get("cost_usd"), "transcript_cost": round(mine["cost"], 6),
            "reported_input": mu.get("input"), "transcript_input": mine["input"],
            "reported_output": mu.get("output"), "transcript_output": mine["output"],
            "reported_cache_read": mu.get("cache_read"), "transcript_cache_read": mine["cache_read"],
            "reported_cache_write": mu.get("cache_write"), "transcript_cache_write": mine["cache_write"],
        })
    start = cs.get("startTime")
    return {
        "source": "last cost-state event in the transcript",
        "scope_note": "Claude Code's counters for the process that wrote this event (since `start`); a resumed "
                      "session may restart them, so they can undercount a long, resumed session.",
        "events": len(s.cost_states),
        "total_cost_usd": reported,
        "total_duration_ms": cs.get("totalDuration"),
        "api_duration_ms": cs.get("totalAPIDuration"),
        "api_duration_without_retries_ms": cs.get("totalAPIDurationWithoutRetries"),
        "tool_duration_ms": cs.get("totalToolDuration"),
        "lines_added": cs.get("totalLinesAdded"),
        "lines_removed": cs.get("totalLinesRemoved"),
        "start": util.iso(float(start)) if isinstance(start, (int, float)) else None,
        "has_unknown_model_cost": cs.get("hasUnknownModelCost"),
        "by_model": by_model,
        "reconciliation": {
            "transcript_estimate_usd": round(est, 6),
            "delta_usd": round(reported - est, 6) if isinstance(reported, (int, float)) else None,
            "by_model": recon,
            "note": "A positive delta is spend on calls not written to the transcript (titles, compaction, "
                    "suggestions) or a cost-state written before the latest activity.",
        },
    }


# ---------------------------------------------------------------------- timing


def _turn_duration(t):
    if t.reported_duration_ms is not None:
        return float(t.reported_duration_ms), "reported"
    if t.ts_start is not None and t.ts_end is not None:
        return t.ts_end - t.ts_start, "computed"
    return None, None


def _timing(ctx, reqs, calls):
    s = ctx.s
    main_reqs = [r for r in reqs if r.scope == "main"]
    main_calls = [c for c in calls if c.scope == "main"]
    durations = [_turn_duration(t)[0] for t in s.turns]
    active = sum(d for d in durations if d)
    model_ms = sum(r.duration_ms or 0 for r in main_reqs)
    tool_wall = util.merged_span_ms([(c.ts_call, c.ts_result) for c in main_calls])
    tool_sum = sum(c.duration_ms or 0 for c in main_calls)
    think, gaps = [], []
    for prev, cur in zip(s.turns, s.turns[1:]):
        if prev.ts_end is None or cur.ts_start is None:
            continue
        gap = cur.ts_start - prev.ts_end
        if cur.trigger in ("prompt", "command", "bash") and cur.origin != "task-notification":
            think.append(gap)
        if gap >= IDLE_GAP_MS:
            gaps.append({"after_turn": prev.index, "from": util.iso(prev.ts_end), "to": util.iso(cur.ts_start),
                         "ms": gap})
    wall = (s.last_ts - s.first_ts) if (s.first_ts is not None and s.last_ts is not None) else None
    return {
        "wall_ms": wall,
        "active_ms": active,
        "active_share": util.ratio(active, wall),
        "model_ms": model_ms,
        "tool_wall_ms": tool_wall,
        "tool_sum_ms": tool_sum,
        "other_active_ms": max(0.0, active - model_ms - tool_wall) if active else None,
        "user_think_ms": util.describe(think),
        "idle_gaps": gaps,
        "turn_duration_ms": util.describe([d for d in durations if d is not None]),
        "request_latency_ms": util.describe([r.latency_ms for r in main_reqs]),
        "request_duration_ms": util.describe([r.duration_ms for r in main_reqs]),
        "tool_duration_ms": util.describe([c.duration_ms for c in main_calls]),
        "notes": [
            "Request latency is the time from the event before a response to its first content block "
            "(thinking included); duration runs to its last block.",
            "Tool durations include any wait for a permission prompt.",
        ],
    }


# ---------------------------------------------------------------------- turns & requests


def _turns(ctx, reqs, calls):
    s = ctx.s
    reqs_by_turn = defaultdict(list)
    for r in reqs:
        reqs_by_turn[r.turn].append(r)
    calls_by_turn = defaultdict(list)
    for c in calls:
        calls_by_turn[c.turn].append(c)
    skills_by_turn = defaultdict(list)
    for inv in s.skills:
        skills_by_turn[inv.turn].append(inv.canonical or inv.name)
    live = s.live_turn
    rows = []
    for t in s.turns:
        rs = reqs_by_turn.get(t.index, [])
        main_rs = [r for r in rs if r.scope == "main"]
        cs = calls_by_turn.get(t.index, [])
        main_cs = [c for c in cs if c.scope == "main"]
        u = usage_totals(rs)
        dur, src = _turn_duration(t)
        text = t.text or ""
        rows.append({
            "index": t.index,
            "start": util.iso(t.ts_start), "end": util.iso(t.ts_end),
            "start_ms": t.ts_start, "end_ms": t.ts_end,
            "duration_ms": dur, "duration_source": src,
            "trigger": t.trigger,
            "origin": t.origin,
            "prompt_source": t.prompt_source,
            "permission_mode": t.permission_mode,
            "prompt": ctx.text(text, ctx.lim_prompt),
            "prompt_chars": len(text),
            "prompt_words": len(text.split()),
            "images": t.images,
            "command": t.command,
            "command_args": ctx.text(t.command_args, 200) if t.command_args else None,
            "requests": len(main_rs),
            "subagent_requests": len(rs) - len(main_rs),
            "tool_calls": len(main_cs),
            "subagent_tool_calls": len(cs) - len(main_cs),
            "tools": dict(Counter(c.name for c in main_cs).most_common()),
            "tool_errors": sum(1 for c in cs if c.status == "error"),
            "tool_denials": sum(1 for c in cs if c.status == "denied"),
            "skills_invoked": skills_by_turn.get(t.index, []),
            "skills_attributed": sorted({r.attribution_skill for r in rs if r.attribution_skill}),
            "agents_launched": sum(1 for c in main_cs if c.name in ("Agent", "Task", "Workflow")),
            "models": sorted({r.model for r in rs}),
            "input_tokens": u["input"], "output_tokens": u["output"], "cache_read_tokens": u["cache_read"],
            "cache_write_tokens": u["cache_write"], "thinking_tokens": u["thinking"],
            "cost_usd": round(u["cost"], 6),
            "max_context_tokens": max((r.context_tokens for r in main_rs), default=None),
            "stop_reason": main_rs[-1].stop_reason if main_rs else None,
            "interrupted": t.interrupted,
            "compacted": t.compacted,
            "queued_prompts_absorbed": t.queued_absorbed,
            "reported_message_count": t.reported_message_count,
            "in_progress": t is live,
            "inherited": t.inherited,
        })
    triggers = Counter(t.trigger for t in s.turns)
    return {
        "count": len(s.turns),
        "by_trigger": dict(triggers),
        "duration_ms": util.describe([r["duration_ms"] for r in rows]),
        "tool_calls_per_turn": util.describe([r["tool_calls"] for r in rows]),
        "requests_per_turn": util.describe([r["requests"] for r in rows]),
        "cost_per_turn": util.describe([r["cost_usd"] for r in rows]),
        "interrupted": sum(1 for t in s.turns if t.interrupted),
        "rows": rows,
    }


def _requests(ctx, reqs):
    rows = []
    for i, r in enumerate(reqs):
        rows.append({
            "i": i, "ts": util.iso(r.ts_first), "ts_ms": r.ts_first, "start_ms": r.ts_start, "end_ms": r.ts_last,
            "turn": r.turn, "scope": r.scope,
            "agent_id": r.agent_id, "model": r.model, "stop_reason": r.stop_reason,
            "input": r.input_tokens, "output": r.output_tokens, "cache_read": r.cache_read_tokens,
            "cache_write_5m": r.cache_write_5m_tokens, "cache_write_1h": r.cache_write_1h_tokens,
            "thinking": r.thinking_tokens, "context": r.context_tokens,
            "cost_usd": round(r.cost["total"], 6) if r.cost else None,
            "latency_ms": r.latency_ms, "duration_ms": r.duration_ms,
            "tools": [ctx.s.tool_calls[t].name for t in r.tool_use_ids if t in ctx.s.tool_calls],
            "blocks": dict(r.blocks), "skill": r.attribution_skill, "agent": r.attribution_agent,
            "plugin": r.attribution_plugin, "mcp_server": r.attribution_mcp_server, "effort": r.effort,
            "cache_miss_reason": r.cache_miss_reason, "speed": r.speed, "inherited": r.inherited,
        })
    blocks = Counter()
    for r in reqs:
        blocks.update(r.blocks)
    synthetic = [r for r in ctx.s.requests.values() if r.model == "<synthetic>"]
    return {
        "count": len(reqs),
        "synthetic_error_messages": len(synthetic),
        "stop_reasons": dict(Counter(r.stop_reason or "(none)" for r in reqs)),
        "content_blocks": dict(blocks),
        "text_chars": sum(r.text_chars for r in reqs),
        "thinking_chars": sum(r.thinking_chars for r in reqs),
        "tool_uses": sum(len(r.tool_use_ids) for r in reqs),
        "effort": dict(Counter(r.effort for r in reqs if r.effort)),
        "per_turn_effort": dict(Counter(r.per_turn_effort for r in reqs if r.per_turn_effort)),
        "advisor_models": dict(Counter(r.advisor_model for r in reqs if r.advisor_model)),
        "attribution": {
            "skill": dict(Counter(r.attribution_skill for r in reqs if r.attribution_skill)),
            "agent": dict(Counter(r.attribution_agent for r in reqs if r.attribution_agent)),
            "plugin": dict(Counter(r.attribution_plugin for r in reqs if r.attribution_plugin)),
            "mcp_server": dict(Counter(r.attribution_mcp_server for r in reqs if r.attribution_mcp_server)),
            "mcp_tool": dict(Counter(r.attribution_mcp_tool for r in reqs if r.attribution_mcp_tool)),
        },
        "multi_iteration": sum(1 for r in reqs if r.iterations > 1),
        "context_edits": sum(r.context_edits for r in reqs),
        "refusals": [{"ts": util.iso(r.ts_first), "model": r.model, "details": r.refusal} for r in reqs
                     if r.stop_reason == "refusal"],
        "rows": rows,
    }


# ---------------------------------------------------------------------- tools


def _tools(ctx, calls):
    by_name = {}
    for c in calls:
        d = by_name.get(c.name)
        if d is None:
            server, tool = mcp_parts(c.name)
            d = by_name[c.name] = {
                "name": c.name, "category": tool_category(c.name), "mcp_server": server, "calls": 0,
                "ok": 0, "error": 0, "denied": 0, "interrupted": 0, "pending": 0, "main": 0, "subagent": 0,
                "parallel": 0, "result_chars": 0, "input_chars": 0, "_durations": [], "first": c.ts_call,
                "last": c.ts_call,
            }
        d["calls"] += 1
        d[c.status] = d.get(c.status, 0) + 1
        d["main" if c.scope == "main" else "subagent"] += 1
        if c.batch_size > 1:
            d["parallel"] += 1
        d["result_chars"] += c.result_chars
        d["input_chars"] += len(summarize_input(c))
        if c.duration_ms is not None:
            d["_durations"].append(c.duration_ms)
        if c.ts_call is not None:
            d["first"] = c.ts_call if d["first"] is None else min(d["first"], c.ts_call)
            d["last"] = c.ts_call if d["last"] is None else max(d["last"], c.ts_call)
    summary = []
    for d in sorted(by_name.values(), key=lambda x: -x["calls"]):
        durs = d.pop("_durations")
        d["error_rate"] = util.ratio(d["error"], d["calls"])
        d["duration_ms"] = util.describe(durs)
        d["first"] = util.iso(d["first"])
        d["last"] = util.iso(d["last"])
        summary.append(d)

    cats = defaultdict(lambda: {"calls": 0, "errors": 0, "tools": 0})
    for d in summary:
        cats[d["category"]]["calls"] += d["calls"]
        cats[d["category"]]["errors"] += d["error"]
        cats[d["category"]]["tools"] += 1

    mcp = defaultdict(lambda: {"calls": 0, "errors": 0, "tools": Counter()})
    for c in calls:
        server, tool = mcp_parts(c.name)
        if server:
            mcp[server]["calls"] += 1
            mcp[server]["errors"] += 1 if c.status == "error" else 0
            mcp[server]["tools"][tool] += 1
    mcp_out = {k: {"calls": v["calls"], "errors": v["errors"], "tools": dict(v["tools"].most_common())}
               for k, v in sorted(mcp.items(), key=lambda kv: -kv[1]["calls"])}

    batches = Counter()
    seen = set()
    for c in calls:
        key = (c.source, c.message_id)
        if c.message_id and key not in seen:
            seen.add(key)
            batches[c.batch_size] += 1

    bigrams = Counter()
    main_seq = [c.name for c in calls if c.scope == "main"]
    for a, b in zip(main_seq, main_seq[1:]):
        bigrams[(a, b)] += 1

    loaded = Counter()
    for c in calls:
        if c.name == "ToolSearch":
            loaded.update(c.facts.get("matches") or c.facts.get("loaded_tools") or [])

    targets = defaultdict(Counter)
    for c in calls:
        tg = target_of(c)
        if tg:
            targets[c.name][_cwd_rel(tg, ctx.cwd)] += 1

    rows = []
    for i, c in enumerate(calls):
        rows.append({
            "i": i, "id": c.id, "ts": util.iso(c.ts_call), "ts_ms": c.ts_call, "end_ms": c.ts_result,
            "turn": c.turn, "scope": c.scope, "agent_id": c.agent_id, "name": c.name, "category": c.category,
            "status": c.status, "denial_kind": c.denial_kind, "duration_ms": c.duration_ms,
            "batch_size": c.batch_size, "input": ctx.text(summarize_input(c), ctx.lim_input),
            "description": ctx.text(c.input.get("description"), 200) if c.name == "Bash" else None,
            "target": ctx.text(_cwd_rel(target_of(c), ctx.cwd), 200),
            "result_chars": c.result_chars,
            "error": ctx.text(c.result_preview, ctx.lim_result) if c.status in ("error", "denied") else None,
            "result": ctx.text(c.result_preview, ctx.lim_result) if ctx.full and c.status == "ok" else None,
            "skill": c.attribution_skill,
            "agent": c.attribution_agent,
            "inherited": c.inherited,
        })
    return {
        "total_calls": len(calls),
        "distinct_tools": len(summary),
        "by_status": dict(Counter(c.status for c in calls)),
        "by_tool": summary,
        "by_category": dict(sorted(cats.items(), key=lambda kv: -kv[1]["calls"])),
        "mcp_servers": mcp_out,
        "parallel_batches": {"distribution": {str(k): v for k, v in sorted(batches.items())},
                             "max": max(batches) if batches else 0,
                             "parallel_calls": sum(1 for c in calls if c.batch_size > 1)},
        "transitions": [{"from": a, "to": b, "count": n} for (a, b), n in bigrams.most_common(30)],
        "denials": dict(Counter(c.denial_kind for c in calls if c.denial_kind)),
        "deferred_tools_loaded": dict(loaded.most_common()),
        "top_targets": {name: [{"target": ctx.text(t, 200), "count": n} for t, n in cnt.most_common(15)]
                        for name, cnt in targets.items()},
        "rows": rows,
    }


# ---------------------------------------------------------------------- skills


def _skills(ctx, reqs, calls):
    s = ctx.s
    turns = {t.index: t for t in s.turns}
    invocations = []
    for i, inv in enumerate(sorted(s.skills, key=lambda x: x.ts or 0)):
        t = turns.get(inv.turn)
        invocations.append({
            "i": i, "name": inv.name, "canonical": inv.canonical, "mode": inv.mode, "via": inv.via,
            "ts": util.iso(inv.ts), "ts_ms": inv.ts, "turn": inv.turn, "scope": inv.scope,
            "agent_id": inv.agent_id, "args": ctx.text(inv.args, 300) if inv.args else None,
            "success": inv.success, "status": inv.status, "error": ctx.text(inv.error, 300) if inv.error else None,
            "allowed_tools": inv.allowed_tools, "base_dir": inv.base_dir,
            "source": skill_source(inv.base_dir, inv.canonical or inv.name, ctx.home),
            "content_chars": inv.content_chars, "forked_agent_id": inv.forked_agent_id,
            "turn_prompt": ctx.text(t.text, 200) if t is not None and t.text else None,
            "inherited": inv.inherited,
        })

    # One key per skill: the canonical name when Claude Code reported one, joined by alias.
    alias = {}
    for inv in s.skills:
        key = inv.canonical or inv.name
        for a in {inv.name, key, key.split(":")[-1]}:
            alias.setdefault(a, key)
    def key_of(name):
        if not name:
            return None
        return alias.get(name) or alias.get(name.split(":")[-1]) or name

    agg = {}

    def slot(k):
        if k not in agg:
            agg[k] = {"skill": k, "invocations": 0, "by_mode": Counter(), "first": None, "last": None,
                      "turns": set(), "source": None, "requests": 0, "tool_calls": 0, "tools": Counter(),
                      "tool_errors": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                      "cost": 0.0}
        return agg[k]

    for inv, row in zip(sorted(s.skills, key=lambda x: x.ts or 0), invocations):
        a = slot(key_of(inv.canonical or inv.name))
        a["invocations"] += 1
        a["by_mode"][inv.mode] += 1
        if inv.turn is not None:
            a["turns"].add(inv.turn)
        if inv.ts is not None:
            a["first"] = inv.ts if a["first"] is None else min(a["first"], inv.ts)
            a["last"] = inv.ts if a["last"] is None else max(a["last"], inv.ts)
        if row["source"] != "unknown":
            a["source"] = row["source"]
    for r in reqs:
        k = key_of(r.attribution_skill)
        if not k:
            continue
        a = slot(k)
        a["requests"] += 1
        a["input"] += r.input_tokens
        a["output"] += r.output_tokens
        a["cache_read"] += r.cache_read_tokens
        a["cache_write"] += r.cache_write_tokens
        a["cost"] += r.cost["total"] if r.cost else 0.0
        if r.turn is not None:
            a["turns"].add(r.turn)
    for c in calls:
        k = key_of(c.attribution_skill)
        if not k:
            continue
        a = slot(k)
        a["tool_calls"] += 1
        a["tools"][c.name] += 1
        if c.status == "error":
            a["tool_errors"] += 1
    per_skill = []
    for a in sorted(agg.values(), key=lambda x: (-x["cost"], -x["invocations"])):
        per_skill.append({
            "skill": a["skill"], "invocations": a["invocations"], "by_mode": dict(a["by_mode"]),
            "first": util.iso(a["first"]), "last": util.iso(a["last"]), "turns": sorted(a["turns"]),
            "source": a["source"], "attributed_requests": a["requests"], "attributed_tool_calls": a["tool_calls"],
            "tools": dict(a["tools"].most_common(12)), "tool_errors": a["tool_errors"],
            "input_tokens": a["input"], "output_tokens": a["output"], "cache_read_tokens": a["cache_read"],
            "cache_write_tokens": a["cache_write"], "cost_usd": round(a["cost"], 6),
        })

    available = sorted(s.known_skill_names())
    used = set()
    for inv in s.skills:
        used.update({inv.name, (inv.canonical or inv.name), (inv.canonical or inv.name).split(":")[-1]})
    commands = [c for c in s.commands if not c.get("is_skill")]
    return {
        "available": available,
        "available_count": max(len(available), s.skill_listing_count),
        "invoked_distinct": len({key_of(inv.canonical or inv.name) for inv in s.skills}),
        "invocations_total": len(s.skills),
        "by_mode": dict(Counter(inv.mode for inv in s.skills)),
        "invocations": invocations,
        "per_skill": per_skill,
        "unused_available": [n for n in available if n not in used and n.split(":")[-1] not in used],
        "failed": [row for row in invocations if row["success"] is False],
        "restored_after_compaction": [{"ts": util.iso(r["ts"]), "name": r["name"], "chars": r["chars"]}
                                      for r in s.restored_skills],
        "dynamically_discovered": [{"ts": util.iso(d["ts"]), "dir": d["dir"], "names": d["names"]}
                                   for d in s.dynamic_skills],
        "plugins": dict(Counter(r.attribution_plugin for r in reqs if r.attribution_plugin)),
        "slash_commands": {
            "count": len(commands),
            "by_name": dict(Counter(c["name"] for c in commands).most_common()),
            "rows": [{"name": c["name"], "args": ctx.text(c["args"], 200) if c["args"] else None,
                      "ts": util.iso(c["ts"]), "turn": c["turn"], "builtin": c["name"] in BUILTIN_COMMANDS,
                      "output": ctx.text(c.get("output"), 200) if c.get("output") else None}
                     for c in commands],
        },
        "command_permissions": [{"ts": util.iso(p["ts"]), "turn": p["turn"], "tools": p["tools"]}
                                for p in s.command_permissions],
        "notes": [
            "mode=model: the model called the Skill tool; mode=user: you typed /<skill>; "
            "mode=harness: Claude Code injected the skill itself.",
            "Attributed requests/tokens/cost come from Claude Code's own `attributionSkill` on each response.",
        ],
    }


# ---------------------------------------------------------------------- subagents & workflows


def _subagents(ctx, reqs, calls):
    s = ctx.s
    reqs_by_src = defaultdict(list)
    for r in reqs:
        reqs_by_src[r.source].append(r)
    calls_by_src = defaultdict(list)
    for c in calls:
        calls_by_src[c.source].append(c)
    launch_by_agent = {}
    launch_by_tool = {}
    for c in calls:
        if c.name in ("Agent", "Task"):
            launch_by_tool[c.id] = c
            if c.facts.get("agent_id"):
                launch_by_agent[c.facts["agent_id"]] = c
    forked = {inv.forked_agent_id: inv for inv in s.skills if inv.forked_agent_id}
    rows = []
    linked = set()
    for idx, src in enumerate(s.sources):
        if src.scope == "main":
            continue
        rs, cs = reqs_by_src.get(idx, []), calls_by_src.get(idx, [])
        u = usage_totals(rs)
        meta = src.meta or {}
        launch = launch_by_agent.get(src.agent_id) or launch_by_tool.get(meta.get("toolUseId"))
        if launch is not None:
            linked.add(launch.id)
        f = launch.facts if launch is not None else {}
        wf_agent = s.workflow_agents.get(src.workflow_run, {}).get(src.agent_id, {}) if src.workflow_run else {}
        dur = (src.last_ts - src.first_ts) if (src.first_ts and src.last_ts) else None
        last = max(rs, key=lambda r: r.ts_first or 0) if rs else None
        rows.append({
            "agent_id": src.agent_id, "kind": src.scope, "workflow_run": src.workflow_run,
            "type": meta.get("agentType") or f.get("agent_type") or ("workflow-agent" if src.workflow_run else None),
            "name": meta.get("name"),
            "description": ctx.text(meta.get("description") or f.get("description") or wf_agent.get("label"), 200),
            "phase": meta.get("workflowPhase") or wf_agent.get("phase"),
            "workflow_status": wf_agent.get("status"),
            "spawn_depth": meta.get("spawnDepth"),
            "worktree": meta.get("worktreePath"),
            "launched_by_tool_use": launch.id if launch is not None else meta.get("toolUseId"),
            "launched_in_turn": launch.turn if launch is not None else None,
            "background": f.get("background"),
            "status": f.get("status"),
            "forked_skill": (forked[src.agent_id].canonical or forked[src.agent_id].name)
            if src.agent_id in forked else None,
            "start": util.iso(src.first_ts), "end": util.iso(src.last_ts),
            "start_ms": src.first_ts, "end_ms": src.last_ts, "duration_ms": dur,
            "reported_duration_ms": f.get("total_duration_ms"),
            # Claude Code's `totalTokens` for an agent is its final context size (last request's
            # context + output), not a sum; `final_context_tokens` recomputes exactly that.
            "reported_final_context_tokens": f.get("total_tokens"),
            "final_context_tokens": (last.context_tokens + last.output_tokens) if last else None,
            "reported_tool_uses": f.get("total_tool_uses"),
            "reported_tool_stats": f.get("tool_stats"),
            "models": sorted({r.model for r in rs}),
            "requests": u["requests"], "input_tokens": u["input"], "output_tokens": u["output"],
            "cache_read_tokens": u["cache_read"], "cache_write_tokens": u["cache_write"],
            "cost_usd": round(u["cost"], 6),
            "tool_calls": len(cs), "tools": dict(Counter(c.name for c in cs).most_common()),
            "tool_errors": sum(1 for c in cs if c.status == "error"),
            "skills": sorted({inv.canonical or inv.name for inv in s.skills if inv.agent_id == src.agent_id}),
            "task_prompt": ctx.text(src.first_prompt, 300) if src.first_prompt else None,
            "task_prompt_chars": len(src.first_prompt or ""),
            "transcript": os.path.relpath(src.path, os.path.dirname(str(s.main_path))),
            "lines": src.lines,
        })
    unlinked = []
    for c in calls:
        if c.name in ("Agent", "Task") and c.id not in linked:
            unlinked.append({"tool_use_id": c.id, "ts": util.iso(c.ts_call), "turn": c.turn,
                             "type": c.facts.get("agent_type") or c.input.get("subagent_type"),
                             "description": ctx.text(c.input.get("description"), 200),
                             "status": c.facts.get("status"), "agent_id": c.facts.get("agent_id"),
                             "reported_final_context_tokens": c.facts.get("total_tokens"),
                             "reported_duration_ms": c.facts.get("total_duration_ms")})
    by_type = defaultdict(lambda: {"agents": 0, "requests": 0, "tool_calls": 0, "cost_usd": 0.0,
                                   "output_tokens": 0, "duration_ms": 0.0})
    for r in rows:
        if r["kind"] != "subagent":
            continue
        b = by_type[r["type"] or "(unknown)"]
        b["agents"] += 1
        b["requests"] += r["requests"]
        b["tool_calls"] += r["tool_calls"]
        b["cost_usd"] += r["cost_usd"]
        b["output_tokens"] += r["output_tokens"]
        b["duration_ms"] += r["duration_ms"] or 0
    return {
        "count": sum(1 for r in rows if r["kind"] == "subagent"),
        "workflow_agents": sum(1 for r in rows if r["kind"] == "workflow"),
        "launched": sum(1 for c in calls if c.name in ("Agent", "Task")),
        "by_type": {k: dict(v, cost_usd=round(v["cost_usd"], 6)) for k, v in
                    sorted(by_type.items(), key=lambda kv: -kv[1]["cost_usd"])},
        "rows": sorted(rows, key=lambda r: r["start_ms"] or 0),
        "launches_without_transcript": unlinked,
        "agent_types_available": sorted(ctx.s.agent_types_listed),
    }


def _workflows(ctx, reqs, calls):
    s = ctx.s
    reqs_by_run = defaultdict(list)
    src_run = {i: src.workflow_run for i, src in enumerate(s.sources) if src.workflow_run}
    for r in reqs:
        if r.source in src_run:
            reqs_by_run[src_run[r.source]].append(r)
    calls_by_run = defaultdict(list)
    for c in calls:
        if c.source in src_run:
            calls_by_run[src_run[c.source]].append(c)
    launches = [c for c in calls if c.name == "Workflow"]

    def match_launch(stem, run_id):
        for c in launches:
            rid = c.facts.get("run_id") or ""
            if rid and (rid == stem or rid == run_id or stem.startswith(rid) or rid.startswith(stem)):
                return c
        return None

    runs = set(s.workflow_runs) | set(src_run.values()) | set(s.workflow_agents)
    rows = []
    for stem in sorted(runs):
        info = s.workflow_runs.get(stem, {})
        rs, cs = reqs_by_run.get(stem, []), calls_by_run.get(stem, [])
        u = usage_totals(rs)
        launch = match_launch(stem, info.get("run_id"))
        agents = s.workflow_agents.get(stem, {})
        rows.append({
            "run": stem, "run_id": info.get("run_id") or stem,
            "name": info.get("name") or (launch.facts.get("workflow_name") if launch else None),
            "status": info.get("status") or (launch.facts.get("status") if launch else None),
            "start": util.iso(info.get("start_ms")), "start_ms": info.get("start_ms"),
            "duration_ms": info.get("duration_ms"),
            "reported_agents": info.get("agent_count"), "reported_total_tokens": info.get("total_tokens"),
            "reported_tool_calls": info.get("total_tool_calls"), "default_model": info.get("default_model"),
            "phases": info.get("phases") or [],
            "summary": ctx.text(info.get("summary") or (launch.facts.get("summary") if launch else None), 400),
            "launched_in_turn": launch.turn if launch else None,
            "launch_tool_use": launch.id if launch else None,
            "agent_transcripts": len({src.agent_id for src in s.sources if src.workflow_run == stem}),
            "journal": dict(Counter(a.get("status") for a in agents.values())),
            "requests": u["requests"], "input_tokens": u["input"], "output_tokens": u["output"],
            "cache_read_tokens": u["cache_read"], "cache_write_tokens": u["cache_write"],
            "cost_usd": round(u["cost"], 6), "tool_calls": len(cs),
            "tools": dict(Counter(c.name for c in cs).most_common(12)),
            "tool_errors": sum(1 for c in cs if c.status == "error"),
        })
    return {"count": len(rows), "launched": len(launches), "rows": rows}


# ---------------------------------------------------------------------- files, shell, git, web


def _files(ctx, calls):
    s = ctx.s
    files = {}

    def slot(path):
        rel = _cwd_rel(path, ctx.cwd)
        if rel not in files:
            files[rel] = {"path": rel, "reads": 0, "lines_read": 0, "edits": 0, "writes": 0, "creates": 0,
                          "bash_edits": 0, "lines_added": 0, "lines_removed": 0, "bash_lines_added": 0,
                          "bash_lines_removed": 0, "errors": 0, "first": None, "last": None, "scopes": set()}
        return files[rel]

    for c in calls:
        path = None
        if c.name in ("Read", "Edit", "MultiEdit", "Write", "NotebookEdit"):
            path = c.facts.get("path") or c.input.get("file_path") or c.input.get("notebook_path")
        if path:
            f = slot(path)
            if c.status != "ok":
                f["errors"] += 1
            elif c.name == "Read":
                f["reads"] += 1
                f["lines_read"] += c.facts.get("num_lines") or 0
            elif c.name == "Write":
                f["writes"] += 1
                if c.facts.get("write_kind") == "create":
                    f["creates"] += 1
                f["lines_added"] += c.facts.get("added") or 0
                f["lines_removed"] += c.facts.get("removed") or 0
            else:
                f["edits"] += 1
                f["lines_added"] += c.facts.get("added") or 0
                f["lines_removed"] += c.facts.get("removed") or 0
            f["scopes"].add(c.scope)
            ts = c.ts_call
            if ts is not None:
                f["first"] = ts if f["first"] is None else min(f["first"], ts)
                f["last"] = ts if f["last"] is None else max(f["last"], ts)
        if c.name == "Bash" and c.facts.get("edit_diff"):
            for fd in c.facts["edit_diff"]:
                if not fd.get("path"):
                    continue
                f = slot(fd["path"])
                f["bash_edits"] += 1
                if fd.get("created"):
                    f["creates"] += 1
                f["bash_lines_added"] += fd.get("added") or 0
                f["bash_lines_removed"] += fd.get("removed") or 0
                f["scopes"].add(c.scope)
    rows = []
    for f in sorted(files.values(), key=lambda x: -(x["lines_added"] + x["lines_removed"] + x["bash_lines_added"]
                                                     + x["bash_lines_removed"] + x["reads"])):
        f["scopes"] = sorted(f["scopes"])
        f["first"], f["last"] = util.iso(f["first"]), util.iso(f["last"])
        rows.append(f)
    modified = [f for f in rows if f["edits"] or f["writes"] or f["bash_edits"]]
    ext_mod = Counter(_ext(f["path"]) for f in modified)
    ext_read = Counter(_ext(f["path"]) for f in rows if f["reads"])
    dirs = Counter(_dir_group(f["path"]) for f in modified)
    main_added = sum(f["lines_added"] for f in rows)
    main_removed = sum(f["lines_removed"] for f in rows)
    return {
        "note": "lines_added/lines_removed count Edit/Write/MultiEdit patches (what Claude Code's own counter "
                "tracks); bash_lines_* are files changed by shell commands (sed -i, scripts) that Claude Code "
                "diffed after the fact.",
        "unique_read": sum(1 for f in rows if f["reads"]),
        "unique_modified": len(modified),
        "unique_created": sum(1 for f in rows if f["creates"]),
        "lines_added": main_added,
        "lines_removed": main_removed,
        "bash_lines_added": sum(f["bash_lines_added"] for f in rows),
        "bash_lines_removed": sum(f["bash_lines_removed"] for f in rows),
        "lines_read": sum(f["lines_read"] for f in rows),
        "by_extension_modified": dict(ext_mod.most_common()),
        "by_extension_read": dict(ext_read.most_common()),
        "by_directory_modified": dict(dirs.most_common(20)),
        "rows": rows,
        "checkpoints": {"snapshots": s.file_snapshots, "deltas": s.file_deltas,
                        "tracked_files": len(s.tracked_files),
                        "max_version": max(s.tracked_files.values()) if s.tracked_files else 0},
        # Claude Code flags a file Claude had read when it changes on disk by any other route than its
        # edit tools: you in an editor, a formatter, or a shell command Claude ran.
        "changed_outside_edit_tools": {_cwd_rel(k, ctx.cwd): v for k, v in s.user_edited_files.most_common()},
        "mentioned_by_user": {_cwd_rel(k, ctx.cwd): v for k, v in s.mentioned_files.most_common()},
        "opened_in_ide": {_cwd_rel(k, ctx.cwd): v for k, v in s.ide_files.most_common()},
        "ide_selections": s.ide_selections,
        "persisted_tool_outputs": s.persisted_outputs,
    }


def _shell(ctx, calls):
    bash = [c for c in calls if c.name == "Bash"]
    programs, primary, subs = Counter(), Counter(), Counter()
    for c in bash:
        cmd = c.input.get("command")
        for p, sub in programs_of(cmd):
            programs[p] += 1
            if sub:
                subs[f"{p} {sub}"] += 1
        p0, _ = primary_program(cmd)
        if p0:
            primary[p0] += 1
    sigs = defaultdict(lambda: {"calls": 0, "uses": 0, "errors": 0, "help": 0, "_d": []})
    for c in bash:
        seen = set()
        for cl in cli_calls(c.input.get("command")):
            d = sigs[cl["signature"]]
            d["uses"] += 1
            if cl["help"]:
                d["help"] += 1
            if cl["signature"] not in seen:
                seen.add(cl["signature"])
                d["calls"] += 1
                if c.status == "error":
                    d["errors"] += 1
                if c.duration_ms is not None:
                    d["_d"].append(c.duration_ms)
    sig_rows = []
    for sig, d in sorted(sigs.items(), key=lambda kv: (-kv[1]["calls"], kv[0])):
        if " " not in sig and d["calls"] < 2:
            continue
        sig_rows.append({"signature": sig, "calls": d["calls"], "uses": d["uses"], "errors": d["errors"],
                         "error_rate": util.ratio(d["errors"], d["calls"]), "help_lookups": d["help"],
                         "p50_ms": util.percentile(d["_d"], 50), "max_ms": max(d["_d"]) if d["_d"] else None})
    exit_codes = Counter(c.facts.get("exit_code") for c in bash if c.facts.get("exit_code") is not None)
    slow = sorted((c for c in bash if c.duration_ms is not None), key=lambda c: -c.duration_ms)[:12]
    failing = [c for c in bash if c.status == "error"][:25]
    return {
        "commands": len(bash),
        "errors": sum(1 for c in bash if c.status == "error"),
        "denied": sum(1 for c in bash if c.status == "denied"),
        "interrupted": sum(1 for c in bash if c.status == "interrupted" or c.facts.get("interrupted")),
        "background": sum(1 for c in bash if c.input.get("run_in_background") or c.facts.get("background_task_id")),
        "timed_out": sum(1 for c in bash if c.facts.get("timed_out_ms")),
        "sandbox_disabled": sum(1 for c in bash if c.input.get("dangerouslyDisableSandbox")),
        "custom_timeout": sum(1 for c in bash if c.input.get("timeout")),
        "with_description": sum(1 for c in bash if c.input.get("description")),
        "stdout_chars": sum(c.facts.get("stdout_chars") or 0 for c in bash),
        "stderr_chars": sum(c.facts.get("stderr_chars") or 0 for c in bash),
        "persisted_outputs": sum(1 for c in bash if c.facts.get("persisted_bytes")),
        "exit_codes": {str(k): v for k, v in exit_codes.most_common()},
        "return_code_interpretations": dict(Counter(c.facts.get("return_code_interpretation") for c in bash
                                                    if c.facts.get("return_code_interpretation"))),
        "programs": dict(programs.most_common(40)),
        "primary_programs": dict(primary.most_common(25)),
        "subcommands": dict(subs.most_common(40)),
        "signatures": sig_rows[:200],
        "help_lookups": sum(d["help"] for d in sigs.values()),
        "duration_ms": util.describe([c.duration_ms for c in bash]),
        "slowest": [{"command": ctx.text(c.input.get("command"), 200), "duration_ms": c.duration_ms,
                     "status": c.status, "turn": c.turn, "ts": util.iso(c.ts_call)} for c in slow],
        "failing": [{"command": ctx.text(c.input.get("command"), 200), "exit_code": c.facts.get("exit_code"),
                     "error": ctx.text(c.result_preview, 240), "turn": c.turn, "ts": util.iso(c.ts_call)}
                    for c in failing],
    }


def _git(ctx, calls):
    s = ctx.s
    commits, pushes, prs, branches = [], [], [], []
    for c in calls:
        g = c.facts.get("git") if c.name == "Bash" else None
        if not isinstance(g, dict):
            continue
        ts = util.iso(c.ts_call)
        if isinstance(g.get("commit"), dict):
            commits.append({"ts": ts, "turn": c.turn, "sha": g["commit"].get("sha"),
                            "branch": g["commit"].get("branch"), "kind": g["commit"].get("kind")})
        if isinstance(g.get("push"), dict):
            pushes.append({"ts": ts, "turn": c.turn, "branch": g["push"].get("branch")})
        if isinstance(g.get("pr"), dict):
            prs.append({"ts": ts, "turn": c.turn, "action": g["pr"].get("action"), "number": g["pr"].get("number"),
                        "url": g["pr"].get("url")})
        if isinstance(g.get("branch"), dict):
            branches.append({"ts": ts, "turn": c.turn, "action": g["branch"].get("action"),
                             "ref": g["branch"].get("ref")})
    git_cmds, gh_cmds = Counter(), Counter()
    for c in calls:
        if c.name != "Bash":
            continue
        for p, sub in programs_of(c.input.get("command")):
            if p == "git" and sub:
                git_cmds[sub] += 1
            elif p == "gh" and sub:
                gh_cmds[sub] += 1
    links = sorted(s.pr_links.values(), key=lambda x: x.get("ts") or 0)
    return {
        "commits": commits, "pushes": pushes, "pull_requests": prs, "branch_operations": branches,
        "pr_links": [{"url": p["url"], "number": p["number"], "repository": p["repository"],
                      "first_seen": util.iso(p["ts"])} for p in links],
        "git_subcommands": dict(git_cmds.most_common()),
        "gh_subcommands": dict(gh_cmds.most_common()),
        "counts": {"commits": len(commits), "pushes": len(pushes),
                   "prs_created": sum(1 for p in prs if (p.get("action") or "").startswith("creat")),
                   "pr_operations": len(prs), "pr_links": len(links)},
    }


def _web(ctx, calls, reqs):
    fetches = [c for c in calls if c.name == "WebFetch"]
    searches = [c for c in calls if c.name == "WebSearch"]
    return {
        "fetches": len(fetches),
        "fetch_errors": sum(1 for c in fetches if c.status != "ok"),
        "domains": dict(Counter(_domain(c.facts.get("url") or c.input.get("url")) for c in fetches).most_common()),
        "status_codes": {str(k): v for k, v in Counter(c.facts.get("code") for c in fetches
                                                       if c.facts.get("code") is not None).most_common()},
        "bytes": sum(c.facts.get("bytes") or 0 for c in fetches),
        "fetch_duration_ms": util.describe([c.facts.get("duration_ms") or c.duration_ms for c in fetches]),
        "fetch_rows": [{"ts": util.iso(c.ts_call), "turn": c.turn, "url": ctx.text(c.facts.get("url")
                        or c.input.get("url"), 300), "code": c.facts.get("code"), "bytes": c.facts.get("bytes"),
                        "status": c.status, "duration_ms": c.facts.get("duration_ms") or c.duration_ms}
                       for c in fetches],
        "searches": len(searches),
        "search_rows": [{"ts": util.iso(c.ts_call), "turn": c.turn, "query": ctx.text(c.input.get("query"), 200),
                         "results": c.facts.get("results"), "duration_s": c.facts.get("duration_s"),
                         "status": c.status} for c in searches],
        "server_side": {"web_search_requests": sum(r.web_search_requests for r in reqs),
                        "web_fetch_requests": sum(r.web_fetch_requests for r in reqs)},
    }


def _interview(ctx, raw_qs, runs):
    """Every question of the session: those inside a skill run as that run classified them (the skill's own
    topics), the rest against the generic topics."""
    by_qid = {}
    for r in runs:
        for q in r["interview"]["questions"]:
            by_qid.setdefault(q["qid"], dict(q, skill=r["skill"]))
    rest = questions.classify([dict(q) for q in raw_qs if q["qid"] not in by_qid], questions.Taxonomy())
    rows = sorted(list(by_qid.values()) + [questions.public(q, run_id=None, skill=None) for q in rest],
                  key=lambda q: (q["t"] or 0, q["qid"]))
    s0 = ctx.s.first_ts
    for q in rows:
        q["dt"] = (q["t"] - s0) if q["t"] is not None and s0 is not None else None
    summary = questions.summarize(rows)
    return dict(summary, questions=rows,
                runs=[{"run_id": r["run_id"], "skill": r["skill"], "start_ms": r["start_ms"],
                       "first_create_dt": r["interview"]["first_create_dt"], "asked": r["interview"]["asked"],
                       "prose": r["interview"]["prose"]} for r in runs if r["interview"]["questions"]])


def _planning(ctx, calls):
    s = ctx.s
    created = [c for c in calls if c.name == "TaskCreate"]
    updates = [c for c in calls if c.name == "TaskUpdate"]
    todo = [c for c in calls if c.name == "TodoWrite"]
    transitions = Counter(f"{c.facts.get('from') or '?'} → {c.facts.get('to') or '?'}" for c in updates)
    last_todo = todo[-1].facts.get("todo_status") if todo else None
    questions = []
    for c in calls:
        if c.name != "AskUserQuestion":
            continue
        answers = c.facts.get("answers") or {}
        questions.append({
            "ts": util.iso(c.ts_call), "turn": c.turn, "status": c.status,
            "questions": [{"header": q.get("header"), "question": ctx.text(q.get("question"), 300),
                           "options": len(q.get("options") or ()), "multi_select": q.get("multi")}
                          for q in c.facts.get("questions") or []],
            "answers": {ctx.text(k, 200): ctx.text(v if isinstance(v, str) else str(v), 300)
                        for k, v in answers.items()} if answers else None,
        })
    plans = [c for c in calls if c.name == "ExitPlanMode"]
    return {
        "tasks_created": len(created),
        "task_updates": len(updates),
        "task_transitions": dict(transitions.most_common()),
        "tasks_completed": sum(1 for c in updates if c.facts.get("to") == "completed"),
        "task_subjects": [ctx.text(c.facts.get("subject") or c.input.get("subject"), 160) for c in created],
        "todo_writes": len(todo),
        "last_todo_status": last_todo,
        "plan_mode": {"entered": sum(1 for c in calls if c.name == "EnterPlanMode"),
                      "plans_presented": len(plans),
                      "plans_approved": sum(1 for c in plans if c.status == "ok"),
                      "plans_rejected": sum(1 for c in plans if c.status in ("error", "denied")),
                      "plan_files": sorted(s.plan_files), "reminders": dict(s.plan_mode)},
        "questions_asked": len(questions),
        "questions": questions,
    }


def _user(ctx, calls, timing):
    s = ctx.s
    prompts = [t for t in s.turns if t.trigger == "prompt"]
    human = [t for t in prompts if t.origin != "task-notification"]
    cmds = [c for c in s.commands if c.get("invoked_by") == "user"]
    return {
        "prompts": len(human),
        "prompt_sources": dict(Counter(t.prompt_source or "(unknown)" for t in human)),
        "origins": dict(Counter(t.origin or "(unknown)" for t in prompts)),
        "prompt_chars": util.describe([len(t.text or "") for t in human]),
        "prompt_words": util.describe([len((t.text or "").split()) for t in human]),
        "images_pasted": s.images_pasted,
        "slash_commands_typed": len(cmds),
        "slash_commands": dict(Counter(c["name"] for c in cmds).most_common()),
        "shell_mode_inputs": s.bash_mode_inputs,
        "interruptions": len(s.interrupts),
        "interruptions_during_tool_use": sum(1 for i in s.interrupts if i["for_tool_use"]),
        "tool_denials": dict(Counter(c.denial_kind for c in calls if c.denial_kind)),
        "rejected_by_user": sum(1 for c in calls if c.denial_kind == "user-rejected"),
        "queue": {"operations": dict(s.queue_ops), "reasons": dict(s.queue_reasons),
                  "queued_prompts": sum(1 for q in s.queued_commands if q["mode"] == "prompt"),
                  "queued_task_notifications": sum(1 for q in s.queued_commands if q["mode"] == "task-notification")},
        "task_notification_turns": sum(1 for t in s.turns if t.trigger == "task_notification"),
        "away_summaries": len(s.away_summaries),
        "think_time_ms": timing["user_think_ms"],
        "permission_modes_on_prompts": dict(Counter(t.permission_mode for t in human if t.permission_mode)),
    }


def _errors(ctx, calls, reqs):
    s = ctx.s
    errs = [c for c in calls if c.status == "error"]
    rows = []
    for c in errs:
        rows.append({"ts": util.iso(c.ts_call), "turn": c.turn, "tool": c.name, "scope": c.scope,
                     "category": categorize_error(c.result_preview),
                     "input": ctx.text(summarize_input(c), 160), "message": ctx.text(c.result_preview, 300)})
    by_cat = Counter(r["category"] for r in rows)
    by_tool = Counter(r["tool"] for r in rows)
    api = [{"ts": util.iso(e["ts"]), "turn": e["turn"], "status": e["status"], "source": e["source"],
            "retry_attempt": e["retry_attempt"], "max_retries": e["max_retries"],
            "network_down": e["network_down"], "connection_code": e["connection_code"],
            "message": ctx.text(e["message"], 200)} for e in s.api_errors]
    synthetic = [r for r in s.requests.values() if r.is_api_error]
    quota = [{"ts": util.iso(r.ts_first), "status": (r.quota or {}).get("status"),
              "type": (r.quota or {}).get("rateLimitType"), "resets_at": (r.quota or {}).get("resetsAt")}
             for r in s.requests.values() if r.quota]
    return {
        "tool_errors": len(rows),
        "tool_error_rate": util.ratio(len(rows), len(calls)),
        "by_category": dict(by_cat.most_common()),
        "by_tool": dict(by_tool.most_common()),
        "rows": rows,
        "denied": sum(1 for c in calls if c.status == "denied"),
        "interrupted": sum(1 for c in calls if c.status == "interrupted"),
        "pending": sum(1 for c in calls if c.status == "pending"),
        "api_errors": len(api),
        "api_error_statuses": dict(Counter(str(e["status"] or e["connection_code"] or e["source"] or "unknown")
                                           for e in s.api_errors).most_common()),
        "api_error_rows": api,
        "api_error_messages": [{"ts": util.iso(r.ts_first), "status": r.api_error_status,
                                "message": ctx.text(r.error_text, 200)} for r in synthetic],
        "rate_limit_events": quota,
        "refusal_fallbacks": [{"ts": util.iso(e["ts"]), "from": e["original_model"], "to": e["fallback_model"],
                               "category": e["category"]} for e in s.refusal_fallbacks],
        "notices": [{"ts": util.iso(n["ts"]), "level": n["level"], "text": ctx.text(n["text"], 200)}
                    for n in s.notices],
    }


def _hooks(ctx):
    s = ctx.s
    runs = s.hook_runs
    stop = s.stop_hooks
    return {
        "runs": len(runs),
        "by_event": dict(Counter(r["event"] or "(none)" for r in runs).most_common()),
        "by_name": dict(Counter(r["name"] or "(none)" for r in runs).most_common()),
        "by_kind": dict(Counter(r["kind"] for r in runs).most_common()),
        "non_zero_exit": sum(1 for r in runs if r["exit_code"] not in (None, 0)),
        "duration_ms": util.describe([r["duration_ms"] for r in runs]),
        "commands": dict(Counter(ctx.text(r["command"], 160) for r in runs if r["command"]).most_common(20)),
        "stop_hooks": {
            "summaries": len(stop),
            "hooks_run": sum(h["count"] or 0 for h in stop),
            "errors": sum(h["errors"] for h in stop),
            "prevented_continuation": sum(1 for h in stop if h["prevented"]),
            "duration_ms": util.describe([d for h in stop for d in h["durations"] if d is not None]),
            "commands": dict(Counter(ctx.text(c, 160) for h in stop for c in h["commands"] if c).most_common(10)),
        },
    }


def _context(ctx, reqs):
    s = ctx.s
    main = [r for r in reqs if r.scope == "main"]
    series = [[r.ts_first, r.context_tokens, r.output_tokens] for r in main if r.ts_first is not None]
    snaps = s.prompt_snapshots
    last_tools = next((sn for sn in reversed(snaps) if sn.get("tools")), None)
    tl = s.tokens_left
    if len(tl) > 1500:
        step = len(tl) / 1500.0
        tl = [tl[int(i * step)] for i in range(1500)] + [tl[-1]]
    return {
        "series": series,
        "compactions": [{"ts": util.iso(c["ts"]), "turn": c["turn"], "trigger": c["trigger"],
                         "pre_tokens": c["pre_tokens"], "post_tokens": c["post_tokens"],
                         "duration_ms": c["duration_ms"], "dropped_tokens": c["dropped_tokens"],
                         "scope": c["scope"]} for c in s.compactions],
        "compactions_by_trigger": dict(Counter(c["trigger"] or "(unknown)" for c in s.compactions)),
        "token_budget_left": [[ts, n] for ts, n in tl],
        "deferred_tools_announced": sorted(s.deferred_added),
        "deferred_tools_removed": sorted(s.deferred_removed),
        "mcp_servers": {"with_instructions": sorted(s.mcp_instruction_servers),
                        "pending": sorted(s.mcp_pending), "failed": sorted(s.mcp_failed),
                        "needs_auth": sorted(s.mcp_needs_auth)},
        "instruction_files": [{"path": p, "type": v["type"], "chars": v["chars"]}
                              for p, v in sorted(s.instruction_files.items())],
        "nested_memory_files": dict(s.memory_files.most_common()),
        "system_prompt": {
            "snapshots": len(snaps),
            "last_chars": snaps[-1]["system_chars"] if snaps else None,
            "max_chars": max((sn["system_chars"] for sn in snaps), default=None),
            "tools_available": last_tools["tool_count"] if last_tools else None,
            "tool_names": last_tools["tools"] if last_tools else None,
        },
        "skills_listed": s.skill_listing_count,
        "agent_types_listed": sorted(s.agent_types_listed),
        "attachments": dict(s.attachments.most_common()),
        "attachments_by_scope": {k: dict(v.most_common()) for k, v in s.attachments_by_scope.items()},
        "auto_mode": dict(s.auto_mode),
        "ultra_effort": dict(s.ultra_effort),
        "environment_changes": dict(Counter(ch["field"] for ch in s.environment_changes)),
        "thinking_stripped": s.attachments.get("thinking_stripped", 0),
        "thinking_dropped": s.attachments.get("thinking_drop", 0),
        "context_edits": sum(r.context_edits for r in reqs),
    }


def _outputs(ctx, calls, git):
    s = ctx.s
    artifacts = [{"ts": util.iso(c.ts_call), "turn": c.turn, "title": c.facts.get("title"),
                  "url": c.facts.get("url"), "version": c.facts.get("version"), "action": c.facts.get("action")}
                 for c in calls if c.name == "Artifact" and c.status == "ok"]
    sent = [{"ts": util.iso(c.ts_call), "turn": c.turn, "files": [_cwd_rel(str(f), ctx.cwd)
            for f in (c.facts.get("files") or [])], "display": c.facts.get("display")}
            for c in calls if c.name == "SendUserFile" and c.status == "ok"]
    plans = [{"ts": util.iso(c.ts_call), "turn": c.turn, "chars": c.facts.get("plan_chars"),
              "path": c.facts.get("plan_path"), "status": c.status} for c in calls if c.name == "ExitPlanMode"]
    return {
        "artifacts": artifacts,
        "files_sent_to_user": sent,
        "pull_requests": git["pr_links"],
        "commits": git["commits"],
        "plans": plans,
        "findings_reports": sum(1 for c in calls if c.name == "ReportFindings"),
        "frames": [{"ts": util.iso(f["ts"]), "title": f["title"], "url": f["url"]} for f in s.frame_links],
        "away_summaries": [{"ts": util.iso(a["ts"]), "text": ctx.text(a["text"], 400)} for a in s.away_summaries],
    }


# ---------------------------------------------------------------------- timeline & schema


def _timeline(ctx, reqs, calls, out):
    s = ctx.s
    t0 = s.first_ts
    t1 = s.last_ts
    for r in reqs:
        if r.ts_last is not None:
            t1 = r.ts_last if t1 is None else max(t1, r.ts_last)
    buckets = []
    if t0 is not None and t1 is not None and t1 > t0:
        span = t1 - t0
        size = max(60_000.0, span / 240.0)
        size = float(int(size // 60_000) * 60_000) or 60_000.0
        n = int(span // size) + 1
        acc = [{"t": t0 + i * size, "requests": 0, "tool_calls": 0, "errors": 0, "output": 0, "cost": 0.0,
                "subagent_requests": 0} for i in range(n)]
        for r in reqs:
            if r.ts_first is None:
                continue
            b = acc[min(n - 1, max(0, int((r.ts_first - t0) // size)))]
            if r.scope == "main":
                b["requests"] += 1
            else:
                b["subagent_requests"] += 1
            b["output"] += r.output_tokens
            b["cost"] += r.cost["total"] if r.cost else 0.0
        for c in calls:
            if c.ts_call is None:
                continue
            b = acc[min(n - 1, max(0, int((c.ts_call - t0) // size)))]
            b["tool_calls"] += 1
            if c.status == "error":
                b["errors"] += 1
        buckets = {"size_ms": size, "rows": acc}
    markers = []
    for inv in s.skills:
        if inv.ts is not None:
            markers.append({"t": inv.ts, "kind": "skill", "label": f"{inv.canonical or inv.name} ({inv.mode})"})
    for c in s.compactions:
        if c["ts"] is not None:
            markers.append({"t": c["ts"], "kind": "compaction", "label": f"compaction ({c['trigger']})"})
    for e in s.api_errors:
        if e["ts"] is not None:
            markers.append({"t": e["ts"], "kind": "api_error", "label": f"API error {e['status'] or ''}".strip()})
    for c in calls:
        g = c.facts.get("git") if c.name == "Bash" else None
        if isinstance(g, dict) and c.ts_call is not None:
            if "commit" in g:
                markers.append({"t": c.ts_call, "kind": "commit", "label": "commit"})
            if "pr" in g:
                markers.append({"t": c.ts_call, "kind": "pr", "label": f"PR {(g.get('pr') or {}).get('action')}"})
    for i in s.interrupts:
        if i["ts"] is not None:
            markers.append({"t": i["ts"], "kind": "interrupt", "label": "interrupted"})
    markers.sort(key=lambda m: m["t"])
    # Spans are drawn from turns.rows, requests.rows, tools.rows and subagents.rows directly.
    return {"start_ms": t0, "end_ms": t1, "buckets": buckets, "markers": markers}


def _result_summary(ctx, c, limit):
    f = c.facts
    if c.name == "AskUserQuestion" and f.get("questions"):
        ans = f.get("answers") or {}
        return " · ".join(f"{q.get('question')} → {ans.get(q.get('question'), '(no answer)')}" for q in f["questions"])
    if c.name == "Read" and c.status == "ok":
        part = " (partial)" if f.get("partial") else ""
        return f"{f.get('num_lines') or '?'} lines{part}"
    if c.name in ("Edit", "Write", "MultiEdit") and c.status == "ok":
        return f"+{f.get('added') or 0} −{f.get('removed') or 0} lines"
    return c.result_preview


def _trace(ctx, reqs, calls, docs):
    """Every prompt, API request, tool call and notable event of the session, in time order.

    A tool step's `res` lists the skill documents it touched as "<op> <owner>:<path>" (see skillfiles)."""
    s = ctx.s
    lim = 2000 if ctx.full else 280
    rows = []
    for t in s.turns:
        if t.ts_start is None:
            continue
        rows.append((t.ts_start, 0, {"k": "prompt", "t": t.ts_start, "turn": t.index, "scope": "main", "agent": None,
                                     "trigger": t.trigger, "text": ctx.text(t.text or ("/" + (t.command or "")), lim),
                                     "inherited": t.inherited}))
    for inv in s.skills:
        if inv.ts is None:
            continue
        rows.append((inv.ts, 1, {"k": "skill", "t": inv.ts, "turn": inv.turn, "scope": inv.scope, "agent": inv.agent_id,
                                 "name": inv.canonical or inv.name, "mode": inv.mode, "via": inv.via,
                                 "args": ctx.text(inv.args, lim) if inv.args else None, "ok": inv.success,
                                 "version": inv.fingerprint, "inherited": inv.inherited}))
    for r in reqs:
        if r.ts_first is None:
            continue
        rows.append((r.ts_first, 2, {"k": "request", "t": r.ts_first, "turn": r.turn, "scope": r.scope,
                                     "agent": r.agent_id, "model": r.model, "in": r.input_tokens, "out": r.output_tokens,
                                     "cr": r.cache_read_tokens, "cw": r.cache_write_tokens, "ctx": r.context_tokens,
                                     "think": r.thinking_chars, "text": ctx.text(r.text_preview, lim) or None,
                                     "tools": [ctx.s.tool_calls[x].name for x in r.tool_use_ids if x in ctx.s.tool_calls],
                                     "usd": round(r.cost["total"], 6) if r.cost else None, "lat": r.latency_ms,
                                     "dur": r.duration_ms, "stop": r.stop_reason, "skill": r.attribution_skill,
                                     "miss": r.cache_miss_reason, "inherited": r.inherited}))
    for c in calls:
        ts = c.ts_call if c.ts_call is not None else c.ts_result
        if ts is None:
            continue
        res = []
        for o in skillfiles.call_ops(c, docs):
            # A search or read over a directory or glob is one step, named as it was written.
            target = o["rel"] if o.get("spread") is None else o["spread"]
            entry = f"{o['op']} {o['owner']}:{target or './'}"
            if entry not in res:
                res.append(entry)
        rows.append((ts, 3, {"k": "tool", "t": ts, "turn": c.turn, "scope": c.scope, "agent": c.agent_id,
                             "name": c.name, "id": c.id, "status": c.status, "dur": c.duration_ms,
                             "input": ctx.text(summarize_input(c), lim),
                             "result": ctx.block(_result_summary(ctx, c, lim), lim),
                             "sigs": sorted({x["signature"] + (" --help" if x["help"] else "")
                                             for x in cli_calls(c.input.get("command")) if " " in x["signature"]})
                             if c.name == "Bash" else None,
                             "prog": primary_program(c.input.get("command"))[0] if c.name == "Bash" else None,
                             "res": res or None, "skill": c.attribution_skill, "batch": c.batch_size,
                             "denial": c.denial_kind, "inherited": c.inherited}))
    for e in s.compactions:
        if e["ts"] is not None:
            rows.append((e["ts"], 4, {"k": "event", "t": e["ts"], "turn": e["turn"], "scope": e["scope"], "agent": None,
                                      "what": "compaction", "text": f"{e['trigger'] or '?'}: {util.fmt_tokens(e['pre_tokens'])}"
                                      f" → {util.fmt_tokens(e['post_tokens'])} tokens"}))
    for e in s.api_errors:
        if e["ts"] is not None:
            rows.append((e["ts"], 4, {"k": "event", "t": e["ts"], "turn": e["turn"], "scope": e["scope"],
                                      "agent": e.get("agent_id"), "what": "api_error",
                                      "text": ctx.text(f"{e['status'] or e['connection_code'] or ''} {e['message'] or ''}"
                                                       f" (attempt {e['retry_attempt']})", 200)}))
    for e in s.interrupts:
        if e["ts"] is not None:
            rows.append((e["ts"], 4, {"k": "event", "t": e["ts"], "turn": e["turn"], "scope": e["scope"], "agent": None,
                                      "what": "interrupt", "text": "interrupted by you" + (
                                          " during a tool call" if e["for_tool_use"] else "")}))
    rows.sort(key=lambda x: (x[0], x[1]))
    steps = []
    for i, (_, _, d) in enumerate(rows):
        d["i"] = i
        steps.append({k: v for k, v in d.items() if v is not None and v != [] and v is not False})
        steps[-1]["i"], steps[-1]["t"] = i, d["t"]
    return {"steps": steps, "count": len(steps), "note": "Previews are redacted and truncated; --full keeps up to 2000 characters."}


def _schema(ctx):
    s = ctx.s
    by_scope = defaultdict(dict)
    for (scope, t), n in s.event_types.items():
        by_scope[scope][t] = n
    base = os.path.dirname(str(s.main_path))
    return {
        "sources": [{"path": os.path.relpath(src.path, base), "scope": src.scope, "agent_id": src.agent_id,
                     "workflow_run": src.workflow_run, "lines": src.lines, "bad_lines": src.bad_lines,
                     "bytes": src.size} for src in s.sources],
        "files": len(s.sources),
        "lines": sum(src.lines for src in s.sources),
        "bad_lines": sum(src.bad_lines for src in s.sources),
        "bytes": sum(src.size for src in s.sources),
        "event_types": {k: dict(sorted(v.items(), key=lambda kv: -kv[1])) for k, v in by_scope.items()},
        "system_subtypes": dict(s.system_subtypes.most_common()),
        "attachment_types": dict(s.attachments.most_common()),
        "progress_types": dict(s.progress_types.most_common()),
        "unknown": {"event_types": dict(s.unknown_event_types), "system_subtypes": dict(s.unknown_system_subtypes),
                    "attachment_types": dict(s.unknown_attachment_types)},
        "unmatched_tool_results": s.orphan_results,
        "meta_messages": s.meta_messages,
        "local_command_outputs": s.local_command_outputs,
        "task_notifications": s.task_notifications,
        "artifact_monitor_events": s.artifact_monitor_events,
        "remote_bridge_events": s.bridge_events,
    }


# ---------------------------------------------------------------------- headline


def _totals(out):
    tok = out["tokens"]["overall"]
    rep = out.get("reported") or {}
    return {
        "turns": out["turns"]["count"],
        "prompts": out["user"]["prompts"],
        "api_requests": out["requests"]["count"],
        "main_requests": out["tokens"]["by_scope"].get("main", {}).get("requests", 0),
        "tool_calls": out["tools"]["total_calls"],
        "distinct_tools": out["tools"]["distinct_tools"],
        "tool_errors": out["errors"]["tool_errors"],
        "tool_denials": out["errors"]["denied"],
        "skills_invoked": out["skills"]["invocations_total"],
        "distinct_skills": out["skills"]["invoked_distinct"],
        "slash_commands": out["skills"]["slash_commands"]["count"],
        "subagents": out["subagents"]["count"],
        "workflow_runs": out["workflows"]["count"],
        "workflow_agents": out["subagents"]["workflow_agents"],
        "input_tokens": tok["input"],
        "output_tokens": tok["output"],
        "cache_read_tokens": tok["cache_read"],
        "cache_write_tokens": tok["cache_write"],
        "total_tokens": tok["total_tokens"],
        "cache_hit_ratio": tok["cache_hit_ratio"],
        "estimated_cost_usd": out["cost"]["estimated_usd"],
        "own_cost_usd": out["lineage"]["own"]["cost_usd"],
        "inherited_cost_usd": out["lineage"]["inherited"]["cost_usd"],
        "inherited_requests": out["lineage"]["inherited"]["requests"],
        "reported_cost_usd": rep.get("total_cost_usd") if rep else None,
        "files_read": out["files"]["unique_read"],
        "files_modified": out["files"]["unique_modified"],
        "lines_added": out["files"]["lines_added"],
        "lines_removed": out["files"]["lines_removed"],
        "shell_commands": out["shell"]["commands"],
        "commits": out["git"]["counts"]["commits"],
        "pull_requests": max(out["git"]["counts"]["prs_created"], out["git"]["counts"]["pr_links"]),
        "web_fetches": out["web"]["fetches"],
        "web_searches": out["web"]["searches"],
        "compactions": len(out["context"]["compactions"]),
        "interruptions": out["user"]["interruptions"],
        "api_errors": out["errors"]["api_errors"],
        "wall_ms": out["timing"]["wall_ms"],
        "active_ms": out["timing"]["active_ms"],
        "peak_context_tokens": out["tokens"]["context"]["peak_tokens"],
    }


def _insights(out):
    """Short, factual observations worth surfacing first."""
    notes = []
    tot = out["totals"]
    cost = out["cost"]
    if cost["estimated_usd"]:
        top_model = next(iter(cost["by_model"].items()), None)
        msg = f"Estimated cost {util.fmt_usd(cost['estimated_usd'])}"
        if top_model:
            msg += f", {util.fmt_pct(util.ratio(top_model[1], cost['estimated_usd']))} on {top_model[0]}"
        rep = out.get("reported")
        if rep and isinstance(rep.get("total_cost_usd"), (int, float)):
            msg += f"; Claude Code reported {util.fmt_usd(rep['total_cost_usd'])}"
        notes.append({"level": "info", "text": msg + "."})
    comp = cost["components"]
    if cost["estimated_usd"]:
        biggest = max(comp.items(), key=lambda kv: kv[1])
        notes.append({"level": "info", "text": f"Largest cost component: {biggest[0].replace('_', ' ')} "
                      f"({util.fmt_pct(util.ratio(biggest[1], cost['estimated_usd']))})."})
    if tot["cache_hit_ratio"] is not None:
        level = "warn" if tot["cache_hit_ratio"] < 0.7 and tot["api_requests"] > 5 else "info"
        notes.append({"level": level, "text": f"Prompt cache served {util.fmt_pct(tot['cache_hit_ratio'])} of input tokens."})
    misses = out["tokens"]["cache"]["miss_reasons"]
    if misses:
        top = max(misses.items(), key=lambda kv: kv[1]["missed_tokens"])
        if top[1]["missed_tokens"]:
            notes.append({"level": "info", "text": f"Most cache-miss tokens: {top[0]} "
                          f"({util.fmt_tokens(top[1]['missed_tokens'])} across {top[1]['requests']} requests)."})
    skills = out["skills"]["per_skill"]
    if skills:
        s0 = skills[0]
        if s0["cost_usd"]:
            n = s0["attributed_tool_calls"]
            notes.append({"level": "info", "text": f"Skill with most attributed spend: {s0['skill']} "
                          f"({util.fmt_usd(s0['cost_usd'])}, {n} tool call{'' if n == 1 else 's'})."})
    if out["skills"]["failed"]:
        notes.append({"level": "warn", "text": f"{len(out['skills']['failed'])} skill invocation(s) failed "
                      f"({', '.join(sorted({f['name'] for f in out['skills']['failed']}))})."})
    if tot["tool_errors"]:
        cats = out["errors"]["by_category"]
        top_cat = next(iter(cats.items()), None)
        notes.append({"level": "warn" if (out["errors"]["tool_error_rate"] or 0) > 0.1 else "info",
                      "text": f"{tot['tool_errors']} tool errors ({util.fmt_pct(out['errors']['tool_error_rate'])} of "
                              f"calls); most common: {top_cat[0]} ×{top_cat[1]}." if top_cat else ""})
    if tot["tool_denials"]:
        n = tot["tool_denials"]
        notes.append({"level": "info", "text": f"{n} tool call{' was' if n == 1 else 's were'} denied "
                      f"({', '.join(f'{k} ×{v}' for k, v in out['user']['tool_denials'].items())})."})
    turns = out["turns"]["rows"]
    if turns:
        costly = max(turns, key=lambda t: t["cost_usd"] or 0)
        if costly["cost_usd"]:
            notes.append({"level": "info", "text": f"Most expensive turn: #{costly['index']} "
                          f"({util.fmt_usd(costly['cost_usd'])}, {costly['tool_calls']} tool calls, "
                          f"{util.fmt_duration(costly['duration_ms'])})."})
    slow = out["shell"]["slowest"]
    if slow and slow[0]["duration_ms"] and slow[0]["duration_ms"] > 60_000:
        notes.append({"level": "info", "text": f"Slowest shell command: {util.fmt_duration(slow[0]['duration_ms'])} "
                      f"— `{util.one_line(slow[0]['command'], 70)}`."})
    if tot["compactions"]:
        notes.append({"level": "info", "text": f"Context was compacted {tot['compactions']} time(s)."})
    if tot["api_errors"]:
        notes.append({"level": "warn", "text": f"{tot['api_errors']} API errors/retries "
                      f"({', '.join(f'{k} ×{v}' for k, v in out['errors']['api_error_statuses'].items())})."})
    gaps = out["timing"]["idle_gaps"]
    if gaps:
        notes.append({"level": "info", "text": f"{len(gaps)} idle gap(s) over 30 min; active time "
                      f"{util.fmt_duration(out['timing']['active_ms'])} of {util.fmt_duration(out['timing']['wall_ms'])} wall."})
    unknown = out["schema_coverage"]["unknown"]
    if any(unknown.values()):
        notes.append({"level": "warn", "text": "Transcript contains event types this version does not recognise: "
                      + ", ".join(sorted(set().union(*[set(v) for v in unknown.values()]))) + "."})
    lin = out["lineage"]
    if lin["continues"] and lin["inherited"]["requests"]:
        src = ", ".join(c["session_id"][:8] for c in lin["continues"][:3])
        notes.append({"level": "warn", "text": f"This transcript continues earlier session(s) {src}: "
                      f"{lin['inherited']['requests']} of {lin['inherited']['requests'] + lin['own']['requests']} requests "
                      f"({util.fmt_usd(lin['inherited']['cost_usd'])}) were copied in from them; this session's own "
                      f"activity cost {util.fmt_usd(lin['own']['cost_usd'])}. Export with --own-only to exclude them."})
    elif lin["own_only"] and lin["inherited_events_skipped"]:
        notes.append({"level": "info", "text": f"Own activity only: {lin['inherited_events_skipped']} lines copied from "
                      "earlier sessions were left out."})
    if out["session"]["live"]:
        notes.append({"level": "info", "text": "Session is live: this is a snapshot, including the export itself."})
    return [n for n in notes if n["text"]]
