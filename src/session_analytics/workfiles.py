"""The files a skill run wrote — its working files — and the ones the skill never asked for.

A run writes files with the Write, Edit and NotebookEdit tools and through the shell: a heredoc into a file
(`cat > ./.scratch/t.json <<'JSON'`), a redirect (`mb dashboard get 9 --json > d.json`, `>>`), `tee`, `cp`, `mv`,
`touch`, `curl -o`, `wget -O`; Claude Code also reports the files a command created among those it diffed. Each file
is placed (`project`, `temp`, `memory` — Claude Code's own —, `home`, `other`), typed by its extension (`script`,
`sql`, `json`, `data`, `doc`, `env`, `other`; a file the run executed is a script), and matched against the working
files the skill names (the "files" section of checks/<skill>.json). A file the run created that the skill does not
name, and that is not Claude Code's memory, is a support file: made to do what the skill did not.

Per file: how the run used it — executed or sourced (`runs`), read by a command (`used_by`: `mb transform create`,
`jq`, `Read`) — and for code, what it drives: the CLI commands in its text (`drives`: `mb card create`) and the HTTP
API paths it calls (`api`: `/api/dashboard`). Programs handed to an interpreter without a file (`python3 - <<'PY'`,
`python3 -c`) are counted apart, when they are two lines or more.

A shell write to a path the session had neither read nor written before counts as creating it: a transcript does not
say whether the file existed. Paths behind variables the command does not set, and globs, are skipped.
"""

from __future__ import annotations

import os
import re
from collections import Counter

from .analyze import (
    ENV_ASSIGN,
    MULTI_LEVEL,
    SHELL_KEYWORDS,
    SHELL_WRAPPERS,
    SIG_LEVELS,
    _shell_tokens,
    cli_calls,
    split_segments,
    unwrap_substitution,
)

HOME = os.path.expanduser("~")
TEMP_RE = re.compile(r"^(?:/private)?/(?:tmp|var/folders)/")
MEMORY_RE = re.compile(r"/\.claude/projects/[^/]+/memory/")
# util.HEREDOC_RE, but not the tail of a here-string (<<<word)
HEREDOC_RE = re.compile(r"(?<!<)<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
KINDS = {
    "script": {".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".r", ".php", ".lua",
               ".jq", ".awk"},
    "sql": {".sql"},
    "json": {".json", ".jsonl", ".ndjson"},
    "data": {".csv", ".tsv", ".txt", ".yaml", ".yml", ".xml", ".log", ".parquet", ".xlsx"},
    "doc": {".md", ".markdown", ".html", ".rst", ".desc"},
    "env": {".env", ".envrc"},
}
SHELL_EXT = {".sh", ".bash", ".zsh"}
INTERPRETERS = {"python", "bash", "sh", "zsh", "node", "ruby", "perl", "deno", "bun", "tsx", "ts-node", "Rscript"}
INLINE_FLAGS = {"-c", "-e", "--eval"}
REDIR_TOKEN = re.compile(r"\d*(?:>>?|>\||<<?<?|<<-)|&>>?")  # a bare operator: its target is the next token
REDIR_GLUED = re.compile(r"(?:\d*>>?|\d*>\||&>>?|<<-?|<)\S")  # >file, 2>/dev/null, <<PY, <file
VAR_RE = re.compile(r"\$\{(\w+)\}|\$(\w+)")
# A consumer named in used_by: a program and its subcommands; anything else (a file read in $(cat …), an awk program)
# is the shell's.
CONSUMER_RE = re.compile(r"[A-Za-z_][\w+-]*(?:\.\d+)*(?: [a-z][\w-]*){0,2}")
CANDIDATE_SPLIT = re.compile(r"[\s=,@<>()`'\"]+")
# What a script's text drives: CLIs with subcommands (mb card create), and curl/wget. In a string of code, not the
# CLIs whose names are everyday words ("go up", "make sure").
CALLABLE = MULTI_LEVEL | {"curl", "wget"}
NAMED_CLIS = CALLABLE - {"go", "make", "job", "ingest", "claude", "dq"}
LIST_CALL_RE = re.compile(r"""(['"])([a-z][\w.-]*)\1((?:\s*,\s*(['"])[a-z][\w-]*\4){1,2})""")
LIST_VERB_RE = re.compile(r"""(['"])([a-z][\w.-]*)\1\s*,\s*[A-Za-z_]\w*\s*,\s*(['"])([a-z][\w-]*)\3""")
STRING_RE = re.compile(r"""(['"])((?:\\.|(?!\1)[^\\\n])*)\1""")
WORDS_CALL_RE = re.compile(r"(?<![\w./-])([a-z][\w.-]*)((?:[ \t]+[a-z][\w-]*){0,2})")
API_RE = re.compile(r"/api/([a-z][\w-]*)")
HTTP_RE = re.compile(r"\brequests\.(?:get|post|put|patch|delete|request|Session)\b|\burllib\.request\b|"
                     r"\bhttp\.client\b|\bhttpx\.|\bfetch\(|\baxios\b|\bcurl\b|\bwget\b")
MIN_INLINE_LINES = 2


def ext_of(path):
    base = os.path.basename(path or "")
    if base.startswith(".") and base.count(".") == 1:
        return base.lower()
    return os.path.splitext(base)[1].lower()


def kind_of(path, executed=False):
    """script | sql | json | data | doc | env | other, by extension; a file the run executed is a script."""
    if executed:
        return "script"
    ext = ext_of(path)
    return next((k for k, exts in KINDS.items() if ext in exts), "other")


def location(path, cwd):
    """project | temp | memory | home | other, for an absolute path."""
    if MEMORY_RE.search(path):
        return "memory"
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        return "project"
    if TEMP_RE.match(path):
        return "temp"
    if path.startswith(HOME + "/"):
        return "home"
    return "other"


def display(path, cwd):
    """Relative to the project, ~ for home, else absolute."""
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        return path[len(cwd.rstrip("/")) + 1:]
    return "~" + path[len(HOME):] if path.startswith(HOME + "/") else path


def resolve(raw, cwd, env=None):
    """An absolute, normalised path; None behind a variable the command does not set, a glob, or no directory."""
    if not raw:
        return None
    p = VAR_RE.sub(lambda m: (env or {}).get(m.group(1) or m.group(2), "$" + (m.group(1) or m.group(2)))
                   if (m.group(1) or m.group(2)) != "HOME" else HOME, raw)
    if p == "~" or p.startswith("~/"):
        p = HOME + p[1:]
    if any(ch in p for ch in "$`*?[]{}") or "\n" in p:
        return None
    if not p.startswith("/"):
        if not cwd:
            return None
        p = os.path.join(cwd, p)
    return os.path.normpath(p)


# ---------------------------------------------------------------- one shell command
def _word(s, i):
    """The shell word at s[i], quotes removed, and the index after it."""
    out, q, n = [], None, len(s)
    while i < n:
        ch = s[i]
        if q:
            if ch == q:
                q = None
            elif ch == "\\" and q == '"' and i + 1 < n:
                out.append(s[i + 1])
                i += 2
                continue
            else:
                out.append(ch)
        elif ch in "'\"":
            q = ch
        elif ch.isspace() or ch in ";|&<>()":
            break
        elif ch == "\\" and i + 1 < n:
            out.append(s[i + 1])
            i += 2
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out), i


def redirects(seg):
    """[(target, append)] for each output redirect of a segment (> >> >| &> 2>), outside quotes. Descriptors (>&2,
    2>&1), process substitution and /dev/* are not files."""
    out, q, i, n = [], None, 0, len(seg)
    while i < n:
        ch = seg[i]
        if q:
            if ch == "\\" and q == '"':
                i += 2
                continue
            if ch == q:
                q = None
            i += 1
            continue
        if ch in "'\"":
            q = ch
        elif ch == "\\":
            i += 2
            continue
        elif ch == ">" and not (i and seg[i - 1] in "<=-") and not seg.startswith(">(", i):
            j, append = i + 1, False
            if j < n and seg[j] == ">":
                append, j = True, j + 1
            elif j < n and seg[j] == "|":
                j += 1
            if j < n and seg[j] == "&":
                i = j + 1
                continue
            while j < n and seg[j] in " \t":
                j += 1
            target, j = _word(seg, j)
            if target and target != "-" and not target.startswith("/dev/"):
                out.append((target, append))
            i = max(j, i + 1)
            continue
        i += 1
    return out


def _argv(toks):
    """(program, args) of a segment's tokens: leading assignments, keywords and wrappers skipped, redirections
    dropped; (None, assignments) for a segment that only sets variables."""
    i, assigned = 0, []
    while i < len(toks) and (ENV_ASSIGN.match(toks[i]) or toks[i] in SHELL_KEYWORDS or toks[i] in SHELL_WRAPPERS):
        if ENV_ASSIGN.match(toks[i]):
            assigned.append(toks[i])
        i += 1
    if i < len(toks) and toks[i] == "timeout":
        i += 2
    words, skip = [], False
    for t in toks[i:]:
        if skip:
            skip = False
        elif REDIR_TOKEN.fullmatch(t):
            skip = True
        elif not REDIR_GLUED.match(t):
            words.append(t)
    if not words:
        return None, assigned
    return words[0], words[1:]


def _runner(prog):
    return "python" if re.fullmatch(r"python[\d.]*", prog or "") else prog


def _executed(prog, args):
    """The file a command runs or sources: python x.py, bash x.sh, source x.sh, node x.js, uv run x.py, ./x.sh."""
    p = _runner(os.path.basename(prog))
    if p in ("source", "."):
        return args[0] if args else None
    if p == "uv":
        return next((a for a in args[1:] if a.endswith(".py")), None) if args[:1] == ["run"] else None
    if p in ("deno", "bun") and args[:1] == ["run"]:
        args = args[1:]
    if p == "npx" and args[:1] and args[0] in ("tsx", "ts-node"):
        p, args = args[0], args[1:]
    if p in INTERPRETERS:
        for a in args:
            if a in INLINE_FLAGS or a in ("-m", "-"):
                return None
            if not a.startswith("-"):
                return a
        return None
    return prog if "/" in prog else None


def _inline(prog, args, stdin):
    """Code handed to an interpreter without a file: -c/-e code, or a heredoc on its stdin (python3 - <<'PY')."""
    if _runner(os.path.basename(prog)) not in INTERPRETERS:
        return None
    for k, a in enumerate(args):
        if a in INLINE_FLAGS:
            return args[k + 1] if k + 1 < len(args) else None
        if a == "-m" or (a != "-" and not a.startswith("-")):
            return None
    return stdin


def parse_command(cmd, cwd):
    """What one shell command does to files: {"writes": [{"path", "via", "content", "append"}], "runs": [path],
    "refs": [(path, signature)], "inline": [code]}. Paths are absolute; `cwd` is where the command starts, and a
    `cd` inside it moves on from there."""
    lines = (cmd or "").split("\n")
    kept, bodies, i = [], [], 0
    while i < len(lines):
        kept.append(lines[i])
        i += 1
        for m in HEREDOC_RE.finditer(kept[-1]):
            body = []
            while i < len(lines) and lines[i].strip() != m.group(2):
                body.append(lines[i])
                i += 1
            i += 1
            bodies.append("\n".join(body))
    out = {"writes": [], "runs": [], "refs": [], "inline": []}
    env, here = {}, cwd
    for seg in split_segments("\n".join(kept)):
        stdin = None
        for _ in HEREDOC_RE.finditer(seg):
            body = bodies.pop(0) if bodies else None
            stdin = body if stdin is None else stdin
        seg = unwrap_substitution(seg)
        toks = _shell_tokens(seg)
        prog, args = _argv(toks)
        if prog is None:
            env.update(a.split("=", 1) for a in args)
            continue
        if prog in ("export", "declare", "local", "readonly"):
            env.update(a.split("=", 1) for a in args if ENV_ASSIGN.match(a))
            continue
        if prog in ("cd", "pushd"):
            here = resolve(args[0], here, env) if args and args[0] != "-" else (HOME if not args else None)
            continue
        written = []
        body_prog = os.path.basename(prog) in ("cat", "tee")
        for target, append in redirects(seg):
            written.append({"path": resolve(target, here, env), "content": stdin if body_prog else None,
                            "via": "heredoc" if body_prog and stdin is not None else ("append" if append else "redirect"),
                            "append": append})
        base = os.path.basename(prog)
        plain = [a for a in args if not a.startswith("-")]
        if base == "tee":
            for a in plain:
                written.append({"path": resolve(a, here, env), "content": stdin, "append": "-a" in args,
                                "via": "heredoc" if stdin is not None else "tee"})
        elif base in ("cp", "mv") and len(plain) >= 2:
            *srcs, dest = plain
            into = dest.endswith("/") or len(srcs) > 1 or (not ext_of(dest) and bool(ext_of(srcs[0])))
            for src in srcs:
                target = os.path.join(dest, os.path.basename(src.rstrip("/"))) if into else dest
                written.append({"path": resolve(target, here, env), "content": None, "append": False,
                                "via": "copy" if base == "cp" else "move"})
        elif base == "touch":
            written += [{"path": resolve(a, here, env), "content": None, "append": False, "via": "touch"} for a in plain]
        elif base in ("curl", "wget"):
            flags = ("-o", "--output") if base == "curl" else ("-O", "--output-document")
            for k, a in enumerate(args):
                target = args[k + 1] if a in flags and k + 1 < len(args) else (
                    a.split("=", 1)[1] if a.startswith(flags[1] + "=") else None)
                if target and target != "-":
                    written.append({"path": resolve(target, here, env), "content": None, "append": False,
                                    "via": "download"})
        written = [w for w in written if w["path"] and not w["path"].startswith("/dev/")]
        out["writes"] += written
        ran = _executed(prog, args)
        ran_path = resolve(ran, here, env) if ran else None
        if ran_path:
            out["runs"].append(ran_path)
        code = _inline(prog, args, stdin)
        if code is not None:
            out["inline"].append(code)
        mine = {w["path"] for w in written} | {ran_path}
        sig = next((c["signature"] for c in cli_calls(seg)), base)
        sig = sig if CONSUMER_RE.fullmatch(sig or "") else "shell"
        for t in toks[1:] if toks else ():
            for cand in CANDIDATE_SPLIT.split(t):
                if ("/" in cand or ext_of(cand)) and (p := resolve(cand, here, env)) and p not in mine:
                    out["refs"].append((p, sig))
    return out


# ---------------------------------------------------------------- what a script drives
def drives(text, path):
    """CLI commands a script's text runs (mb card create; `mb * update` when the object is a variable)."""
    sigs = []
    if ext_of(path) in SHELL_EXT:
        sigs += [c["signature"] for c in cli_calls(text) if c["program"] in CALLABLE and not c["help"]]
    else:
        for m in LIST_CALL_RE.finditer(text):
            if m.group(2) in CALLABLE:
                sigs.append(_signature(m.group(2), re.findall(r"[a-z][\w-]*", m.group(3))))
        for m in LIST_VERB_RE.finditer(text):
            if m.group(2) in MULTI_LEVEL:
                sigs.append(f"{m.group(2)} * {m.group(4)}")
        for s in STRING_RE.finditer(text):
            for m in WORDS_CALL_RE.finditer(s.group(2)):
                if m.group(1) in NAMED_CLIS:
                    sigs.append(_signature(m.group(1), m.group(2).split()))
    return [s for s in dict.fromkeys(sigs) if " " in s or s.split()[0] not in MULTI_LEVEL]


def _signature(prog, words):
    levels = SIG_LEVELS.get(prog, 2 if prog in MULTI_LEVEL else 0)
    return " ".join([prog] + words[:levels])


def api_calls(text):
    """HTTP API paths a script calls (/api/card), or ["http"] when it makes requests without one."""
    paths = sorted({f"/api/{m.group(1)}" for m in API_RE.finditer(text)})
    return paths or (["http"] if HTTP_RE.search(text) else [])


# ---------------------------------------------------------------- a session, a run
class Ledger:
    """Every file write, execution and reference in a session, from its tool calls in time order: once per session,
    so a file a run creates is told apart from one an earlier run (or the user) made."""

    def __init__(self, calls, cwd):
        self.cwd = cwd
        self.by_call = {}
        written, read = set(), set()
        for c in sorted(calls, key=lambda c: c.ts_call or c.ts_result or 0):
            ev = self._call(c)
            if ev is None:
                continue
            for w in ev["writes"]:
                if w["path"] in written:
                    w["new"] = False
                elif w.get("new") is None:
                    w["new"] = w["path"] not in read
                written.add(w["path"])
            read |= ev["read"]
            self.by_call[c.id] = ev

    def _call(self, c):
        # A shell command that failed may have written its files before the part that failed; a tool call that
        # failed, or was denied, wrote nothing.
        if c.status != "ok" and not (c.name == "Bash" and c.status in ("error", "interrupted")):
            return None
        ev = {"writes": [], "edits": [], "runs": [], "refs": [], "inline": [], "read": set()}
        path = c.facts.get("path") or c.input.get("file_path") or c.input.get("notebook_path")
        path = os.path.normpath(path) if path and path.startswith("/") else None
        if c.name == "Write" and path:
            kind = c.facts.get("write_kind")
            ev["writes"].append({"path": path, "via": "Write", "content": c.input.get("content"), "append": False,
                                 "new": True if kind == "create" else (False if kind == "update" else None)})
        elif c.name in ("Edit", "MultiEdit", "NotebookEdit") and path:
            ev["edits"].append({"path": path, "via": "Edit"})
        elif c.name == "Read" and path:
            ev["read"].add(path)
            ev["refs"].append((path, "Read"))
        elif c.name == "Bash":
            parsed = parse_command(c.input.get("command"), c.cwd or self.cwd)
            ev.update({k: parsed[k] for k in ("runs", "refs", "inline")})
            ev["writes"] = [dict(w, new=None) for w in parsed["writes"]]
            # Claude Code's own diff of what the command changed: authoritative on a file it created
            for fd in c.facts.get("edit_diff") or ():
                p = os.path.normpath(fd["path"]) if (fd.get("path") or "").startswith("/") else None
                if not p:
                    continue
                mine = next((w for w in ev["writes"] if w["path"] == p), None)
                if mine is not None:
                    mine["new"] = True if fd.get("created") else mine["new"]
                elif fd.get("created"):
                    ev["writes"].append({"path": p, "via": "shell", "content": None, "append": False, "new": True})
                else:
                    ev["edits"].append({"path": p, "via": "shell edit"})
        else:
            return None
        return ev

    def run(self, calls, start, spec, cwd=None):
        """The files one run wrote, from its calls: (rows, totals, {call id: files it created}). `spec` is the skill's
        "files" section (None: the skill names no files, so which files are support files is unknown)."""
        cwd = cwd or self.cwd
        files, inline = {}, []
        for c in sorted(calls, key=lambda c: c.ts_call or c.ts_result or 0):
            ev = self.by_call.get(c.id)
            if ev is None:
                continue
            t = c.ts_call or c.ts_result
            for w, edit in [(w, False) for w in ev["writes"]] + [(e, True) for e in ev["edits"]]:
                f = files.get(w["path"])
                if f is None:
                    f = files[w["path"]] = {
                        "abs": w["path"], "via": w["via"], "created": bool(w.get("new")), "first_t": t,
                        "dt": (t - start) if t is not None and start is not None else None, "turn": c.turn,
                        "scope": c.scope, "writes": 0, "edits": 0, "lines": None, "runs": 0, "used_by": Counter(),
                        "calls": [], "_texts": []}
                if edit:
                    f["edits"] += 1
                else:
                    f["writes"] += 1
                    if w.get("content") is not None and not w.get("append"):
                        f["lines"] = len(w["content"].splitlines())
                    if w.get("content"):
                        f["_texts"].append(w["content"])
                if c.id not in f["calls"]:
                    f["calls"].append(c.id)
            for p in ev["runs"]:
                if p in files:
                    files[p]["runs"] += 1
            for p, sig in ev["refs"]:
                if p in files:
                    files[p]["used_by"][sig] += 1
            inline += [code for code in ev["inline"] if code and len(code.strip().splitlines()) >= MIN_INLINE_LINES]
        expected = [(e, re.compile(e["match"])) for e in (spec or {}).get("expected") or () if e.get("match")]
        rows = []
        for f in files.values():
            texts = f.pop("_texts")
            kind = kind_of(f["abs"], executed=f["runs"] > 0)
            path = display(f["abs"], cwd)
            where = location(f["abs"], cwd)
            named = next((e for e, rx in expected if rx.search(path)), None)
            if not f["created"] or where == "memory" or named:
                support = False
            else:
                support = True if spec is not None else None
            code = "\n".join(texts) if kind == "script" else ""
            rows.append(dict(
                f, path=path, name=os.path.basename(f["abs"]), ext=ext_of(f["abs"]) or None, kind=kind,
                location=where, expected=named["id"] if named else None,
                expected_label=(named.get("label") or named["id"]) if named else None, support=support,
                used_by=[s for s, _ in f["used_by"].most_common()],
                drives=drives(code, f["abs"]) if code else [], api=api_calls(code) if code else []))
        for r in rows:
            del r["abs"]
        rows.sort(key=lambda r: (r["first_t"] is None, r["first_t"] or 0, r["path"]))
        known = spec is not None
        support = [r for r in rows if r["support"]]
        scripts = [r for r in support if r["kind"] == "script"]
        totals = {
            "files_created": sum(1 for r in rows if r["created"]),
            "support_files": len(support) if known else None, "support_scripts": len(scripts) if known else None,
            "support_script_runs": sum(r["runs"] for r in scripts) if known else None,
            "support_kinds": dict(Counter(r["kind"] for r in support)) if known else None,
            "memory_notes": sum(1 for r in rows if r["location"] == "memory"),
            "temp_files": sum(1 for r in rows if r["created"] and r["location"] == "temp"),
            "inline_scripts": len(inline), "inline_script_lines": sum(len(x.strip().splitlines()) for x in inline),
        }
        created = {}
        for r in rows:
            if r["created"]:
                created.setdefault(r["calls"][0], []).append(
                    {"path": r["path"], "location": r["location"], "kind": r["kind"],
                     "support": "unknown" if r["support"] is None else str(r["support"]).lower()})
        for r in rows:
            del r["calls"]
        return rows, totals, created
