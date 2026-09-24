"""Skill files: which documents of a skill entered Claude's context during a run, and how much of each.

A skill steers a run through its files: SKILL.md routes to a playbook, the playbook names references, and a
skill like rde also sends Claude to the docs its CLI bundles (`mb skills path <name>`, then one section).
For each run this answers which of those files were read, which lines, by what command, in what order, what
pointed Claude at each one, and whether the text Claude saw is the text of the version the run is labelled
with (an installed copy can lag the repository it was copied from).

Evidence, most exact first:
  - the invocation injects SKILL.md's body, whole;
  - the Read tool reports the first line it returned and how many;
  - a shell or Grep call's output is matched line by line against the file's text, so `sed -n '/## Tabs/,/^## /p'`,
    `grep -n -A12 x`, `head -40` and `cat` are all measured by what Claude was shown rather than by what the
    command meant to print.

Which files a command touched comes from its arguments: variables it assigns (`D=$(mb skills path x | jq …)`),
`cd`, `for f in …` loops, globs and directories (expanded against the skill's file list). Files are named
`owner:path`: the skill's own (`rde:references/state.md`), another installed skill's, or the docs a CLI bundles
(`mb:dashboard/SKILL.md`, from `…/node_modules/@metabase/cli/skill-data/dashboard/SKILL.md`).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
from collections import Counter
from pathlib import Path

from . import versions
from .util import strip_heredocs

HOME = os.path.expanduser("~")
CLI_PACKAGES = {"@metabase/cli": "mb"}   # npm package -> the program that serves its bundled skill-data
VIRTUAL_RE = re.compile(r"^<cli:([\w.@-]+)>(?:/(.*))?$")
SKILL_DATA_RE = re.compile(r"^(.*?/skill-data)(?:/(.*))?$")
SKILLS_RE = re.compile(r"^(.*?/skills/([\w.-]+))(?:/(.*))?$")
SKILLS_CMD_RE = re.compile(r"(?:^|[\s;&|(])([\w./-]+)\s+skills\s+(path|get|list)\b([^|;&)\n]*)")
LEARN_RE = re.compile(r"(/[^\s\"',]*?/skill-data)/[\w.-]+")
GLOB_RE = re.compile(r"[*?\[]")
SUB_TOKEN = "@@SUB{}@@"
VAR_RE = re.compile(r"\$\{(\w+)[^}]*\}|\$(\w+)|@@SUB(\d+)@@")
ASSIGN_RE = re.compile(r"^([A-Za-z_]\w*)\+?=(.*)$", re.S)
REDIRECT_RE = re.compile(r"^(\d*)(&>>?|>>?&?|<<<|<&|<>|<)(.*)$")
HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
MAYBE_DOC_RE = re.compile(r"skill|playbook|reference|\$|`|\bcd\b|\bpushd\b", re.I)
NO_FILE_RE = re.compile(r"(?m)^(?:[\w.-]+: )+([^:\n]+): No such file or directory")

READ_FULL = {"cat", "less", "more", "bat", "batcat", "nl", "tac", "jq", "yq", "view"}
READ_PART = {"head", "tail", "sed"}
SEARCH = {"grep", "egrep", "fgrep", "rg", "ag", "ack", "awk", "gawk", "mawk"}
LISTING = {"ls", "find", "tree", "fd", "eza", "exa"}
STAT = {"wc", "stat", "file", "du", "md5", "md5sum", "shasum", "sha256sum"}
FILTERS = READ_PART | SEARCH | {"cut", "sort", "uniq", "tr", "column", "fold", "fmt", "jq", "wc", "nl", "cat"}
SKIP_WORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "(", "time", "nohup", "command",
              "builtin", "exec", "sudo", "nice", "env", "export", "local", "declare", "readonly", "typeset"}
END_WORDS = {"fi", "done", "esac", "}", ")", "case", "function"}
VALUE_OPTS = {
    "head": "nc", "tail": "ncb", "sed": "efl", "grep": "efABCmdD", "egrep": "efABCmdD", "fgrep": "efABCmdD",
    "rg": "efABCmgtTMj", "ag": "ABCGgm", "ack": "ABCm", "awk": "Fvf", "gawk": "Fvf", "mawk": "Fvf", "jq": "f",
    "yq": "f", "bat": "lrHm", "batcat": "lrHm", "tree": "LPIo", "nl": "bdfhilnsvw", "tac": "s", "ls": "I",
    "fd": "tedE",
}
LONG_VALUE = {"--include", "--exclude", "--exclude-dir", "--glob", "--type", "--max-count", "--context",
              "--after-context", "--before-context", "--regexp", "--file", "--lines", "--bytes", "--expression",
              "--line-range", "--language", "--indent", "--field-separator", "--ignore", "--max-depth"}
LONG_TWO_VALUES = {"--arg", "--argjson", "--slurpfile", "--rawfile"}
OUTPUT_PREFIXES = (
    re.compile(r"^\s*\d+(?:\t|→|\s│\s?)"),                  # cat -n, nl, Read-style numbering, bat
    re.compile(r"^\d+[:-]"),                                 # grep -n
    re.compile(r"^\S+?[:-]\d+[:-]"),                          # grep -n over several files: path:12: / path-12-
    re.compile(r"^[^\s:]+\.(?:md|markdown|txt|json|ya?ml|sql|py|sh|js|ts)[:-]"),  # several files without -n
)
JSON_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "/": "/"}


# ---------------------------------------------------------------------------- shell


def _match_paren(s, i):
    """Index of the `)` closing a `$(` whose body starts at i, or None."""
    depth, q, n = 1, None, len(s)
    while i < n:
        ch = s[i]
        if q:
            if ch == "\\" and q == '"':
                i += 2
                continue
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
        elif ch == "\\":
            i += 2
            continue
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def extract_substitutions(cmd):
    """Replace each `$( … )` and backtick substitution with a placeholder word: (text, [inner commands])."""
    out, subs, q, i, n = [], [], None, 0, len(cmd)
    while i < n:
        ch = cmd[i]
        if q == "'":
            out.append(ch)
            if ch == "'":
                q = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(cmd[i:i + 2])
            i += 2
            continue
        if ch == "'" and q is None:
            q = "'"
        elif ch == '"':
            q = None if q == '"' else '"'
        elif cmd.startswith("$(", i) and not cmd.startswith("$((", i):
            j = _match_paren(cmd, i + 2)
            if j is None:
                out.append(cmd[i:])
                break
            subs.append(cmd[i + 2:j])
            out.append(SUB_TOKEN.format(len(subs) - 1))
            i = j + 1
            continue
        elif ch == "`":
            j = cmd.find("`", i + 1)
            if j < 0:
                out.append(cmd[i:])
                break
            subs.append(cmd[i + 1:j])
            out.append(SUB_TOKEN.format(len(subs) - 1))
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out), subs


def split_statements(cmd):
    """[[stage, …], …]: statements split on ; && || & and newlines, their stages on |, outside quotes."""
    stmts, stages, buf = [], [], []
    q, i, n = None, 0, len(cmd)

    def cut_stage():
        s = "".join(buf).strip()
        buf.clear()
        if s:
            stages.append(s)

    def cut_statement():
        cut_stage()
        if stages:
            stmts.append(stages[:])
            stages.clear()

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
            i += 1
            continue
        if ch in "'\"":
            q = ch
            buf.append(ch)
        elif ch == "\\" and i + 1 < n:
            if cmd[i + 1] != "\n":
                buf.append(cmd[i:i + 2])
            i += 2
            continue
        elif cmd.startswith("&&", i) or cmd.startswith("||", i):
            cut_statement()
            i += 2
            continue
        elif ch == "|":
            cut_stage()
            if cmd.startswith("|&", i):
                i += 1
        elif ch == "&":
            if (i and cmd[i - 1] in "<>") or cmd.startswith("&>", i):
                buf.append(ch)
            else:
                cut_statement()
        elif ch in ";\n":
            cut_statement()
        elif ch == "#" and (not buf or buf[-1].isspace()):
            j = cmd.find("\n", i)
            i = n if j < 0 else j
            continue
        else:
            buf.append(ch)
        i += 1
    cut_statement()
    return stmts


def words(stage):
    """Shell words of one stage: [(text, quoted, single_quoted, starts_quoted)], quotes removed, escapes applied."""
    out, buf = [], []
    quoted = single = started = lead = False
    q, i, n = None, 0, len(stage)
    while i < n:
        ch = stage[i]
        if q == "'":
            if ch == "'":
                q = None
            else:
                buf.append(ch)
        elif q == '"':
            if ch == '"':
                q = None
            elif ch == "\\" and i + 1 < n and stage[i + 1] in '"\\$`':
                buf.append(stage[i + 1])
                i += 1
            else:
                buf.append(ch)
        elif ch in "'\"":
            if not started:
                lead = True
            q = ch
            quoted = started = True
            single = single or ch == "'"
        elif ch == "\\" and i + 1 < n:
            buf.append(stage[i + 1])
            started = True
            i += 1
        elif ch.isspace():
            if started:
                out.append(("".join(buf), quoted, single, lead))
                buf, quoted, single, started, lead = [], False, False, False, False
        else:
            buf.append(ch)
            started = True
        i += 1
    if started:
        out.append(("".join(buf), quoted, single, lead))
    return out


def _expand(text, env, subs):
    """Every value a word can take: variables (a `for` loop gives several) and substitutions. [] if unknown."""
    m = VAR_RE.search(text)
    if not m:
        return [text]
    if m.group(3) is not None:
        v = subs[int(m.group(3))] if int(m.group(3)) < len(subs) else None
        vals = [v] if v is not None else []
    else:
        vals = env.get(m.group(1) or m.group(2)) or []
    if not vals:
        return []
    tails = _expand(text[m.end():], env, subs)
    return [text[:m.start()] + v + t for v in vals[:25] for t in tails][:60]


def _absolute(p, cwd):
    if p == "~" or p.startswith("~/"):
        p = HOME + p[1:]
    if p.startswith("<cli:"):
        head, _, rest = p.partition(">")
        return head + ">" + ("/" + os.path.normpath(rest.lstrip("/")) if rest.strip("/") else "")
    if not p.startswith("/"):
        if not cwd:
            return None
        p = os.path.join(cwd, p)
    return os.path.normpath(p)


def _split_args(prog, args):
    """(positional words, [(option, value)]) for the programs that read files; option values consumed."""
    vals = VALUE_OPTS.get(prog, "")
    pos, opts, i, ended = [], [], 0, False
    while i < len(args):
        w = args[i]
        t = w[0]
        if ended or w[3] or not t.startswith("-") or t == "-" or len(t) == 1:
            pos.append(w)
            i += 1
            continue
        if t == "--":
            ended = True
            i += 1
            continue
        if t.startswith("--"):
            name, eq, val = t.partition("=")
            if not eq and name in LONG_VALUE and i + 1 < len(args):
                val = args[i + 1][0]
                i += 1
            elif not eq and name in LONG_TWO_VALUES:
                i += 2
            opts.append((name, val or None))
            i += 1
            continue
        body = t[1:]
        if body.isdigit() or (body[:1] == "+" and body[1:].isdigit()):
            opts.append(("-#", body))  # head -40, tail +5, grep -3
            i += 1
            continue
        j = 0
        while j < len(body):
            ch = body[j]
            if ch in vals:
                val = body[j + 1:]
                if not val and i + 1 < len(args):
                    val = args[i + 1][0]
                    i += 1
                opts.append(("-" + ch, val))
                break
            opts.append(("-" + ch, None))
            j += 1
        i += 1
    return pos, opts


def _quote(s):
    return s if re.match(r"^[\w./@%+=:,^-]+$", s or "") else shlex.quote(s)


def _short(s, limit=160):
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _cli_op(prog, args):
    """`<prog> skills get|path|list …`: a CLI serving its bundled skill docs. -> [(target, op, recursive)]"""
    if len(args) < 2 or args[0][0] != "skills" or args[1][0] not in ("get", "path", "list"):
        return None
    verb = args[1][0]
    rest = [w[0] for w in args[2:]]
    names = [n for a in rest if not a.startswith("-") for n in a.split(",") if n]
    root = f"<cli:{os.path.basename(prog)}>"
    if verb == "list":
        return [(root, "list", False)]
    if verb == "path":
        return [(f"{root}/{n}", "resolve", False) for n in names] or [(root, "resolve", False)]
    if "--all" in rest:
        names = ["*"]
    out = [(f"{root}/{n}/SKILL.md", "read", False) for n in names]
    if "--full" in rest:
        out += [(f"{root}/{n}", "read", True) for n in names]
    return out


def _substitution_value(inner, cwd):
    """What a `$( … )` stands for, when it names a place: `mb skills path x | jq …` -> <cli:mb>/x."""
    m = SKILLS_CMD_RE.search(inner)
    if m and m.group(2) == "path":
        names = [a for a in m.group(3).split() if not a.startswith("-")]
        return f"<cli:{os.path.basename(m.group(1))}>" + (f"/{names[0]}" if names else "")
    if inner.strip() in ("pwd", "pwd -P"):
        return cwd
    return None


_SHELL_CACHE = {}


def _shell_ops_cached(cmd, cwd):
    key = (cmd, cwd)
    if key not in _SHELL_CACHE:
        if len(_SHELL_CACHE) > 20000:
            _SHELL_CACHE.clear()
        _SHELL_CACHE[key] = shell_ops(cmd, cwd)
    return [dict(o) for o in _SHELL_CACHE[key]]


def shell_ops(cmd, cwd):
    """File operations in a shell command: [{path, op, via, detail, recursive, filter}].

    `path` is absolute or virtual (`<cli:mb>/dashboard/SKILL.md`) and may be a glob or a directory; `op` is
    read | search | list | resolve | stat.
    """
    text, subs = extract_substitutions(strip_heredocs(cmd or ""))
    env = {"HOME": [HOME]}
    state = {"cwd": cwd}
    values = [_substitution_value(s, cwd) for s in subs]
    ops = []
    for inner in subs:  # substitutions run commands too: `$(mb skills path core | jq …)` resolves a doc
        for stages in split_statements(inner):
            ops += _pipeline_ops(stages, env, [], dict(state))
    for stages in split_statements(text):
        ops += _pipeline_ops(stages, env, values, state)
    return ops


def _pipeline_ops(stages, env, subs, state):
    ops, first = [], None
    for stage in stages:
        ws = words(stage)
        # Redirections: `< file` feeds the program a file; output redirections are not reads.
        kept, inputs, i = [], [], 0
        while i < len(ws):
            t, _, _, lead = ws[i]
            m = REDIRECT_RE.match(t) if not lead else None
            if m and not t.startswith("<("):
                target = m.group(3)
                if not target and i + 1 < len(ws):
                    target = ws[i + 1][0]
                    i += 1
                if m.group(2) == "<" and target:
                    inputs.append((target, False, False, False))
                i += 1
                continue
            kept.append(ws[i])
            i += 1
        ws = kept
        j = 0
        while j < len(ws):
            t, _, single, lead = ws[j]
            am = ASSIGN_RE.match(t) if not lead else None
            if am:
                name, raw = am.group(1), am.group(2)
                env[name] = [raw] if single else _expand(raw, env, subs)
                j += 1
            elif t in SKIP_WORDS:
                j += 1
            else:
                break
        ws = ws[j:]
        if not ws or ws[0][0] in END_WORDS:
            continue
        head = ws[0][0]
        if head == "for" and len(ws) >= 3 and ws[2][0] == "in":
            env[ws[1][0]] = [v for w in ws[3:] for v in ([w[0]] if w[2] else _expand(w[0], env, subs))]
            continue
        prog = os.path.basename(head)
        args = ws[1:]
        if prog in ("cd", "pushd"):
            target = args[0][0] if args else "~"
            vals = [target] if args and args[0][2] else _expand(target, env, subs)
            if vals and vals[0] != "-":
                state["cwd"] = _absolute(vals[0], state["cwd"]) or state["cwd"]
            continue
        stage_ops = []
        cli = _cli_op(head, args)
        if cli is not None:
            for target, op, rec in cli:
                stage_ops.append({"path": target, "op": op, "via": f"{prog} skills {args[1][0]}", "recursive": rec,
                                  "detail": _short(f"{prog} " + " ".join(_quote(w[0]) for w in args))})
        else:
            stage_ops = _program_ops(prog, args, inputs)
        resolved = []
        for o in stage_ops:
            vals = [o["path"]] if o.pop("literal", False) else _expand(o["path"], env, subs)
            for v in vals:
                p = _absolute(v, state["cwd"])
                if p:
                    resolved.append(dict(o, path=p))
        if resolved:
            ops += resolved
            if first is None:
                first = resolved
        elif first is not None and prog in FILTERS:
            # A filter narrows what the reader printed: `cat f | grep x`, `sed -n '/a/,/b/p' f | head -60`.
            for o in first:
                o["detail"] = _short(o["detail"] + " | " + stage)
                if prog in SEARCH and o["op"] == "read":
                    o["op"] = "search"
    return ops


def _program_ops(prog, args, inputs):
    """Targets one program reads, with the op and a short description of how."""
    if prog in LISTING:
        if prog == "find":
            paths = []
            for w in args:
                if w[0].startswith(("-", "(", "!")):
                    break
                paths.append(w)
            paths = paths or [(".", False, False, False)]
        else:
            paths, _ = _split_args(prog, args)
            paths = paths or [(".", False, False, False)]
        return [{"path": w[0], "literal": w[2], "op": "list", "via": prog, "recursive": False,
                 "detail": _short(" ".join([prog] + [_quote(x[0]) for x in args if x[0].startswith("-")]))}
                for w in paths]
    if prog not in READ_FULL | READ_PART | SEARCH | STAT:
        return []
    pos, opts = _split_args(prog, args)
    names = {o for o, _ in opts}
    op = "read" if prog in READ_FULL | READ_PART else "search" if prog in SEARCH else "stat"
    script = []
    if prog == "sed":
        if "-i" in names:
            return []  # an in-place edit, not a read
        if "-e" not in names and "-f" not in names and pos:
            script, pos = [pos[0]], pos[1:]
    elif prog in ("grep", "egrep", "fgrep", "rg", "ag", "ack"):
        if "-e" not in names and "-f" not in names and pos:
            script, pos = [pos[0]], pos[1:]
        if "-l" in names or "-L" in names or "-c" in names or "--files-with-matches" in names or "--count" in names:
            op = "list"  # prints file names or counts, not text
    elif prog in ("awk", "gawk", "mawk", "jq", "yq"):
        if "-f" not in names and pos:
            script, pos = [pos[0]], pos[1:]
    recursive = prog in ("rg", "ag", "ack") or (prog in ("grep", "egrep", "fgrep") and
                                                bool(names & {"-r", "-R", "--recursive"}))
    targets = pos + inputs
    if not targets and recursive:
        targets = [(".", False, False, False)]
    parts = [prog]
    for o, v in opts:
        parts.append("-" + v if o == "-#" else o if v is None else f"{o} {_quote(v)}")
    detail = " ".join(parts + [_quote(w[0]) for w in script])
    glob_filter = next((v for o, v in opts if o in ("--include", "--glob", "-g") and v), None)
    return [{"path": w[0], "literal": w[2], "op": op, "via": prog, "recursive": recursive, "filter": glob_filter,
             "detail": _short(detail)} for w in targets]


# ---------------------------------------------------------------------------- where documents live


def _glob_match(pattern, rel):
    ps, rs = pattern.split("/"), rel.split("/")

    def m(i, j):
        if i == len(ps):
            return j == len(rs)
        if ps[i] == "**":
            return any(m(i + 1, k) for k in range(j, len(rs) + 1))
        return j < len(rs) and fnmatch.fnmatchcase(rs[j], ps[i]) and m(i + 1, j + 1)
    return m(0, 0)


class DocSet:
    """Where skill documents live: installed skills, a skill's source at the version a run used, and the docs
    CLIs bundle. Maps a path to (owner, relative path), lists an owner's files, returns a file's text."""

    def __init__(self, skill_dirs=None):
        self.roots = {}          # owner -> [directories]
        self.clis = {}           # skill-data directory -> the CLI's name
        self.pins = {}           # owner -> (SkillSource, commit): text and file list at a version
        self._cache = {}
        for owner, dirs in (skill_dirs or {}).items():
            for d in sorted(dirs):
                self._add_root(owner, d)

    @classmethod
    def for_session(cls, s):
        dirs = {}
        for inv in s.skills:
            if inv.base_dir:
                dirs.setdefault((inv.canonical or inv.name or "?").split(":")[-1], set()).add(
                    inv.base_dir.replace("~/", HOME + "/", 1) if inv.base_dir.startswith("~/") else inv.base_dir)
        docs = cls(dirs)
        for c in s.tool_calls.values():
            if c.name == "Bash":
                docs.learn(c)
        return docs

    def _add_root(self, owner, d):
        d = os.path.normpath(d)
        lst = self.roots.setdefault(owner, [])
        if d not in lst:
            lst.append(d)

    def learn(self, call):
        """`mb skills path x` printed where the CLI keeps its docs: remember that directory belongs to `mb`."""
        for m in SKILLS_CMD_RE.finditer(call.input.get("command") or ""):
            if m.group(2) != "path":
                continue
            for root in LEARN_RE.findall(call.output or call.result_preview or ""):
                name = os.path.basename(m.group(1))
                self.clis.setdefault(os.path.normpath(root), name)
                self._add_root(name, root)

    def pin(self, owner, source, commit):
        """Read `owner`'s files at `commit` of `source` (a versions.SkillSource), e.g. the version a run used."""
        self.pins[owner] = (source, commit)
        self._add_root(owner, str(source.dir))

    def _cli_name(self, root):
        root = os.path.normpath(root)
        if root in self.clis:
            return self.clis[root]
        pkg = None
        try:
            pkg = json.loads((Path(root).parent / "package.json").read_text(encoding="utf-8")).get("name")
        except (OSError, ValueError, AttributeError):
            m = re.search(r"node_modules/((?:@[^/]+/)?[^/]+)/skill-data$", root)
            pkg = m.group(1) if m else None
        if pkg:
            return CLI_PACKAGES.get(pkg, pkg.split("/")[-1])
        return os.path.basename(os.path.dirname(root)) or "cli"

    def locate(self, path):
        """(owner, relative path) for a skill document or directory, else None."""
        if not path:
            return None
        m = VIRTUAL_RE.match(path)
        if m:
            owner = m.group(1)
            if owner not in self.roots:
                root = _cli_root(owner)
                if root:
                    self._add_root(owner, root)
            return owner, (m.group(2) or "").strip("/")
        best = None
        for owner, dirs in self.roots.items():
            for d in dirs:
                if (path == d or path.startswith(d + "/")) and (best is None or len(d) > len(best[2])):
                    best = (owner, path[len(d) + 1:], d)
        if best:
            return best[0], best[1]
        m = SKILL_DATA_RE.match(path)
        if m:
            owner = self._cli_name(m.group(1))
            self._add_root(owner, m.group(1))
            return owner, (m.group(2) or "").strip("/")
        m = SKILLS_RE.match(path)
        if m and "/node_modules/" not in path:
            self._add_root(m.group(2), m.group(1))
            return m.group(2), (m.group(3) or "").strip("/")
        return None

    def files(self, owner):
        pin = self.pins.get(owner)
        key = ("files", owner, pin[1] if pin else None, id(pin[0]) if pin else None)
        if key not in self._cache:
            out = []
            if pin:
                out = pin[0].files_at(pin[1])
            if not out:
                for d in self.roots.get(owner, ()):
                    out = versions.list_dir(d)
                    if out:
                        break
            self._cache[key] = out
        return self._cache[key]

    def text(self, owner, rel):
        pin = self.pins.get(owner)
        key = ("text", owner, rel, pin[1] if pin else None, id(pin[0]) if pin else None)
        if key not in self._cache:
            t = pin[0].file_at(pin[1], rel) if pin else None
            if t is None:
                for d in self.roots.get(owner, ()):
                    p = Path(d) / rel
                    if p.is_file():
                        try:
                            t = p.read_text(encoding="utf-8", errors="replace")
                        except OSError:
                            t = None
                        break
            self._cache[key] = t
        return self._cache[key]

    def expand(self, raw):
        """One raw operation -> [op with owner and rel], globs and directories expanded; [] when not a skill's."""
        loc = self.locate(raw["path"])
        if loc is None:
            return []
        owner, rel = loc
        parts = rel.split("/")
        if ".." in parts or any(x in versions.SKIP_DIRS or (x.startswith(".") and x not in (".", "")) for x in parts):
            return []
        files = self.files(owner)
        if GLOB_RE.search(rel):
            hits = [f for f in files if _glob_match(rel, f)]
            return [dict(raw, owner=owner, rel=f, spread=rel) for f in hits] or \
                [dict(raw, owner=owner, rel=rel, missing=True)]
        is_dir = rel == "" or any(f.startswith(rel + "/") for f in files)
        if is_dir:
            if raw["op"] in ("list", "resolve", "stat"):
                return [dict(raw, owner=owner, rel=(rel + "/") if rel else "")]
            if raw.get("recursive"):
                sub = [f for f in files if not rel or f.startswith(rel + "/")]
                if raw.get("filter"):
                    sub = [f for f in sub if fnmatch.fnmatchcase(os.path.basename(f), raw["filter"])]
                return [dict(raw, owner=owner, rel=f, spread=(rel + "/") if rel else "") for f in sub]
            return []
        return [dict(raw, owner=owner, rel=rel, missing=rel not in files if files else None)]


_CLI_ROOTS = {}


def _cli_root(prog):
    """Where an installed CLI keeps its bundled skill docs: <package>/skill-data, found from the binary."""
    if prog not in _CLI_ROOTS:
        root = None
        exe = shutil.which(prog)
        if exe:
            p = Path(os.path.realpath(exe))
            for parent in list(p.parents)[:5]:
                if (parent / "skill-data").is_dir():
                    root = str(parent / "skill-data")
                    break
        _CLI_ROOTS[prog] = root
    return _CLI_ROOTS[prog]


def call_ops(call, docs):
    """The skill files one tool call touched: [{owner, rel, op, via, detail, …}], in the order it touched them."""
    if call.name == "Read":
        p = call.input.get("file_path")
        detail = "Read"
        if call.input.get("offset") or call.input.get("limit"):
            detail = f"Read offset {call.input.get('offset') or 1} limit {call.input.get('limit') or '—'}"
        raw = [{"path": os.path.normpath(p), "op": "read", "via": "Read", "detail": detail}] if p else []
    elif call.name == "Grep":
        base = call.input.get("path") or call.cwd
        mode = call.input.get("output_mode") or "files_with_matches"
        detail = _short("Grep " + _quote(call.input.get("pattern") or "") +
                        (f" --glob {call.input['glob']}" if call.input.get("glob") else "") + f" ({mode})")
        raw = [{"path": _absolute(base, call.cwd), "op": "search" if mode == "content" else "list", "via": "Grep",
                "recursive": True, "filter": call.input.get("glob"), "detail": detail}] if base else []
    elif call.name == "Glob":
        base = call.input.get("path") or call.cwd
        raw = [{"path": _absolute(base, call.cwd), "op": "list", "via": "Glob",
                "detail": _short("Glob " + (call.input.get("pattern") or ""))}] if base else []
    elif call.name == "Bash":
        cmd = call.input.get("command") or ""
        # Most commands name nothing that could be a skill document; skip parsing those. Relative paths count
        # only when the shell already sits in a skill directory.
        if not MAYBE_DOC_RE.search(cmd) and not (call.cwd and docs.locate(call.cwd)):
            return []
        raw = _shell_ops_cached(cmd, call.cwd)
    else:
        return []
    out, seen = [], set()
    for r in raw:
        if not r.get("path"):
            continue
        for o in docs.expand(r):
            k = (o["owner"], o["rel"], o["op"])
            if k not in seen:
                seen.add(k)
                out.append(o)
    return out


# ---------------------------------------------------------------------------- what Claude was shown


def _json_strings(v):
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _json_strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from _json_strings(x)


def output_index(text):
    """Every line of an output, stripped, with and without the prefixes cat -n / grep -n / Read add."""
    keys = set()
    if not text:
        return keys
    blobs = [text]
    s = text.lstrip()
    if s[:1] in "{[":
        try:
            blobs += list(_json_strings(json.loads(s)))
        except ValueError:
            # A JSON envelope cut at a size cap: unescape what is there.
            blobs.append(re.sub(r'\\([nt"\\/])', lambda m: JSON_ESCAPES[m.group(1)], s))
    for blob in blobs:
        for line in blob.split("\n"):
            k = line.strip()
            if not k:
                continue
            keys.add(k)
            for rx in OUTPUT_PREFIXES:
                m = rx.match(line)
                if m:
                    keys.add(line[m.end():].strip())
    return keys


def _profile(text):
    """Per line of a document: its stripped text and whether it can identify itself in an output."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    stripped = [ln.strip() for ln in lines]
    counts = Counter(stripped)
    distinct = [len(k) >= 6 and counts[k] == 1 and any(c.isalnum() for c in k) for k in stripped]
    return lines, stripped, distinct


def seen_in_output(profile, keys):
    """1-based numbers of the document's lines an output showed.

    Distinctive lines (unique in the file, long enough to mean something) are matched exactly; blank lines,
    fences and repeated table rules between two shown lines count when the gap is short and nothing
    distinctive in it is missing.
    """
    _, stripped, distinct = profile
    seen = {i + 1 for i, d in enumerate(distinct) if d and stripped[i] in keys}
    order = sorted(seen)
    for a, b in zip(order, order[1:]):
        if 1 < b - a <= 6 and all(not distinct[j - 1] or stripped[j - 1] in keys for j in range(a + 1, b)):
            seen.update(range(a + 1, b))
    return seen


def ranges(nums):
    out = []
    for n in sorted(nums):
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return out


def sections(lines, seen):
    """The Markdown headings a set of shown lines falls under, in file order."""
    heads, cur, fence = [], None, False
    names = []
    for ln in lines:
        if ln.lstrip().startswith(("```", "~~~")):
            fence = not fence
        m = None if fence else HEADING_RE.match(ln)
        if m:
            cur = m.group(2)
        heads.append(cur)
    for i in sorted(seen):
        h = heads[i - 1] if 0 < i <= len(heads) else None
        if h and h not in names:
            names.append(h)
    return names


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------- one run


def _mention_patterns(owner, rel, src_owner, src_rel):
    pats = []
    if src_owner == owner:
        cands = {rel}
        base = os.path.dirname(src_rel)
        if base:
            cands.add(os.path.relpath(rel, base))
        for c in cands:
            pats.append(re.compile(r"(?:^|(?<=[\s(\[`'\"<:,]))(?:\.{1,2}/)*" + re.escape(c) + r"(?![\w/-])"))
    if rel.count("/") >= 1 and rel.endswith("/SKILL.md") and owner != src_owner:
        name = re.escape(rel.split("/")[0])
        pats.append(re.compile(r"skills\s+(?:path|get)\s+(?:[\w.-]+,)*" + name + r"(?![\w-])"))
        pats.append(re.compile(r"skills\s+path.*`" + name + "`"))
    return pats


def _named_by(loaded, owner, rel, docs):
    """Documents shown earlier in the run whose shown lines name this file."""
    out = []
    for src_owner, src_rel, shown in loaded:
        if (src_owner, src_rel) == (owner, rel) or not shown:
            continue
        pats = _mention_patterns(owner, rel, src_owner, src_rel)
        if not pats:
            continue
        text = docs.text(src_owner, src_rel)
        if not text:
            continue
        lines = text.replace("${CLAUDE_SKILL_DIR}/", "").replace("$CLAUDE_SKILL_DIR/", "").split("\n")
        if any(p.search(lines[i - 1]) for i in shown if 0 < i <= len(lines) for p in pats):
            label = src_rel if src_owner == owner else f"{src_owner}:{src_rel}"
            if label not in out:
                out.append(label)
    return out


def _provenance(docs, owner, rel, key, source, pin_sha, check):
    """Where text that is not the pinned version's came from: (label, text) or (None, None).

    `older <commit>` / `newer <commit>` (another commit of the source), `uncommitted` (the source's working
    tree), `installed copy` (a copy on disk, such as ~/.claude/skills/<name>, that matches no commit).
    """
    if owner == key and source is not None and source.repo:
        commits = source.commits()
        pin_date = next((c["date_ms"] for c in commits if c["sha"] == pin_sha), None)
        for c in commits:
            if c["sha"] == pin_sha:
                continue
            t = source.file_at(c["sha"], rel)
            if t is not None and check(t):
                word = "newer" if pin_date is not None and (c["date_ms"] or 0) > pin_date else "older"
                return f"{word} {c['short']}", t
        wt = source.file_at("WORKTREE", rel)
        if wt is not None and wt != docs.text(owner, rel) and check(wt):
            return "uncommitted", wt
    for d in docs.roots.get(owner, ()):
        if source is not None and os.path.normpath(d) == os.path.normpath(str(source.dir)):
            continue
        f = Path(d) / rel
        if f.is_file():
            try:
                t = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if check(t):
                return ("installed copy" if owner == key else "as installed now"), t
    return None, None


def _dcov(prof, shown):
    """Share of a document's distinctive lines that were shown (1.0 when it has none and something was)."""
    dn = sum(prof[2])
    if not dn:
        return 1.0 if shown else 0.0
    return sum(1 for i in shown if 0 < i <= len(prof[2]) and prof[2][i - 1]) / dn


def _row(c, start, owner, rel, key, o):
    t = getattr(c, "ts_call", None)
    return {"t": t, "dt": (t - start) if t is not None and start is not None else None,
            "call": getattr(c, "id", None), "scope": c.scope, "owner": owner, "path": rel,
            "kind": _kind(owner, rel, key), "op": o["op"], "via": o["via"], "detail": o.get("detail"),
            "status": getattr(c, "status", None), "lines": None,
            "seen": None, "total": None, "coverage": None, "how": None, "chars": None, "sections": [],
            "version": None, "first": False, "named_by": None, "found_by": None, "missing": o.get("missing") or None}


def run_files(calls, inv, key, docs, source=None, pin_sha=None, body_matched=None):
    """Every skill document a run touched, in order, and a per-file summary.

    `calls` are the run's tool calls in order; `inv` the invocation (its SKILL.md body is the first document
    loaded); `docs` a DocSet pinned to the version the run is labelled with; `source`/`pin_sha` let text that
    does not match that version be traced to what it does match; `body_matched` says whether the injected
    SKILL.md body matched that version (the run's version status).
    """
    start = inv.ts
    accesses, profiles, loaded, dirs_seen = [], {}, [], []

    def profile(owner, rel):
        k = (owner, rel)
        if k not in profiles:
            t = docs.text(owner, rel)
            profiles[k] = _profile(t) if t is not None else None
        return profiles[k]

    body = profile(key, "SKILL.md")
    if body is not None:
        lines = body[0]
        fm = versions.frontmatter_lines("\n".join(lines))
        shown = set(range(fm + 1, len(lines) + 1))
        accesses.append(dict(
            _row(inv, start, key, "SKILL.md", key, {"op": "inject", "detail": "the body Claude Code injects "
                                                   "(frontmatter excluded)",
                                                   "via": {"model": "Skill tool", "user": "/" + key}.get(inv.mode,
                                                                                                        "harness")}),
            t=start, dt=0, call=inv.tool_use_id, status="ok", lines=ranges(shown), seen=len(shown), total=len(lines),
            coverage=round(len(shown) / len(lines), 3) if lines else None, how="injected", chars=inv.content_chars,
            version="match" if body_matched else None, first=True, named_by=[], found_by="invocation"))
        loaded.append((key, "SKILL.md", shown))
    firsts = {(key, "SKILL.md")} if body is not None else set()
    for c in calls:
        ops = call_ops(c, docs)
        if not ops:
            continue
        keys = output_index(c.output) if c.name in ("Bash", "Grep") else None
        absent = {os.path.basename(m.strip()) for m in NO_FILE_RE.findall(c.output or "")}
        spread_hits = {}
        rows = []
        for o in ops:
            owner, rel, op = o["owner"], o["rel"], o["op"]
            row = _row(c, start, owner, rel, key, o)
            if op in ("list", "resolve", "stat") or rel.endswith("/") or rel == "":
                row["how"] = {"list": "listed", "resolve": "resolved", "stat": "size"}.get(op, op)
                dirs_seen.append((owner, rel, op))
                rows.append(row)
                continue
            prof = profile(owner, rel)
            shown, total, full = set(), (len(prof[0]) if prof else None), False
            if c.name == "Read":
                f = c.facts
                s0, n = f.get("start_line") or 1, f.get("num_lines")
                shown = set(range(s0, s0 + n)) if n else set()
                total = f.get("total_lines") or total
                row["chars"] = f.get("content_chars")
                full = bool(n) and total is not None and s0 == 1 and n >= total
                if prof is not None and f.get("content_sha"):
                    def same(t, _s0=s0, _n=n or 0, _sha_=f["content_sha"]):
                        ls = t.split("\n")
                        if ls and ls[-1] == "":
                            ls = ls[:-1]
                        part = "\n".join(ls[_s0 - 1:_s0 - 1 + _n])
                        return _sha_ in (_sha(part), _sha(part + "\n"), _sha(t))
                    if same(docs.text(owner, rel)):
                        row["version"] = "match"
                    else:
                        label, _ = _provenance(docs, owner, rel, key, source, pin_sha, same)
                        row["version"] = label or ("differs" if owner == key else "changed since")
            elif prof is not None and keys is not None:
                shown = seen_in_output(prof, keys)
                dc = _dcov(prof, shown)
                full = dc >= 0.95
                plain = o["via"] in READ_FULL and " | " not in (o.get("detail") or "") and op == "read"
                if full:
                    row["version"] = "match"
                elif plain and keys:
                    # A whole-file read that did not show this version's text: find the text it did show.
                    def same_out(t, _keys=keys):
                        pr = _profile(t)
                        return _dcov(pr, seen_in_output(pr, _keys)) >= 0.95
                    label, alt = _provenance(docs, owner, rel, key, source, pin_sha, same_out)
                    if label:
                        prof = _profile(alt)
                        shown, total, full = seen_in_output(prof, keys), len(prof[0]), True
                        row["version"] = label
                    elif os.path.basename(rel) in absent:
                        row["missing"] = True
                    elif c.status == "ok" and sum(1 for x in ops if x["op"] == "read") == 1:
                        # The only file printed, yet matching no version we can find: a copy changed since.
                        row["version"] = "differs" if owner == key else "changed since"
            if o.get("spread") is not None and op == "search":
                spread_hits.setdefault(o["spread"], 0)
                if not shown:
                    continue
                spread_hits[o["spread"]] += 1
            row["lines"] = ranges(shown)
            row["seen"] = len(shown)
            row["total"] = total
            row["coverage"] = round(len(shown) / total, 3) if total else None
            if prof is not None and row["chars"] is None:
                row["chars"] = sum(len(prof[0][i - 1]) + 1 for i in shown if 0 < i <= len(prof[0]))
            if shown:
                row["how"] = "full" if full else "hits" if op == "search" else "partial"
            else:
                row["how"] = "missing" if row["missing"] else "no hits" if op == "search" else "not shown"
            if shown and not full and prof is not None:
                row["sections"] = sections(prof[0], shown)[:8]
            if (owner, rel) not in firsts:
                firsts.add((owner, rel))
                row["first"] = True
                row["named_by"] = _named_by(loaded, owner, rel, docs)
                row["found_by"] = "named" if row["named_by"] else _found_by(dirs_seen, owner, rel, o)
            if shown:
                loaded.append((owner, rel, shown))
            rows.append(row)
        for spread, hits in spread_hits.items():
            first = next(o for o in ops if o.get("spread") == spread)
            row = _row(c, start, first["owner"], spread, key, first)
            row["how"] = f"searched ({hits} with hits)"
            rows.append(row)
            dirs_seen.append((first["owner"], spread, "search"))
        accesses += rows
    return _summarize(accesses, key, docs)


def _found_by(dirs_seen, owner, rel, op):
    if op.get("spread") is not None:
        return "search"
    for d_owner, d_rel, d_op in reversed(dirs_seen):
        d = d_rel.rstrip("/")
        if d_owner == owner and (not d or rel.startswith(d + "/") or rel == d or GLOB_RE.search(d)):
            return {"list": "listing", "search": "search", "resolve": "resolved path"}.get(d_op, d_op)
    return "unprompted"


def _kind(owner, rel, key):
    if owner != key:
        return owner
    d = os.path.dirname(rel.rstrip("/"))
    return d or "."


def _summarize(accesses, key, docs):
    files = {}
    for a in accesses:
        if a["op"] in ("list", "resolve", "stat") or a["path"].endswith("/") or a["path"] == "" or \
                (a["how"] or "").startswith("searched"):
            continue
        k = (a["owner"], a["path"])
        f = files.get(k)
        if f is None:
            f = files[k] = {"owner": a["owner"], "path": a["path"], "kind": a["kind"], "order": len(files),
                            "first_t": a["t"], "first_dt": a["dt"], "accesses": 0, "reads": 0, "searches": 0,
                            "via": Counter(), "shown": set(), "total": a["total"], "chars": 0, "rereads": 0,
                            "named_by": a["named_by"] or [], "found_by": a["found_by"], "versions": Counter(),
                            "missing": a.get("missing"), "full": False, "injected": False}
        if a["op"] == "inject":
            f["injected"] = True
        elif a["op"] == "read" and f["full"]:
            f["rereads"] += 1
        f["accesses"] += 1
        f["reads" if a["op"] in ("read", "inject") else "searches"] += 1
        f["via"][a["via"]] += 1
        f["chars"] += a["chars"] or 0
        f["total"] = f["total"] or a["total"]
        for lo, hi in a["lines"] or ():
            f["shown"].update(range(lo, hi + 1))
        f["full"] = f["full"] or a["how"] in ("full", "injected")
        if a["version"]:
            f["versions"][a["version"]] += 1
    out = []
    for (owner, rel), f in files.items():
        shown = f.pop("shown")
        text = docs.text(owner, rel)
        prof = _profile(text) if text is not None else None
        full = f.pop("full") or (prof is not None and bool(shown) and _dcov(prof, shown) >= 0.95)
        injected = f.pop("injected")
        if injected:
            f["how"] = "injected" if f["accesses"] == 1 else "injected + re-read"
        elif shown:
            f["how"] = "full" if full else "partial" if f["reads"] else "hits"
        else:
            f["how"] = "missing" if f["missing"] else "not shown" if f["reads"] else "no hits"
        f["unique_chars"] = sum(len(prof[0][i - 1]) + 1 for i in shown if 0 < i <= len(prof[0])) if prof else None
        f["lines"] = ranges(shown)
        f["seen"] = len(shown)
        f["coverage"] = round(len(shown) / f["total"], 3) if f["total"] else None
        f["sections"] = sections(prof[0], shown)[:10] if (prof is not None and shown and not full) else []
        f["via"] = dict(f["via"].most_common())
        mism = [v for v in f["versions"] if v != "match"]
        f["version"] = mism[0] if mism else ("match" if f["versions"] else None)
        f.pop("versions")
        f["tokens"] = round(f["chars"] / 4) if f["chars"] else 0
        out.append(f)
    own = [f for f in out if f["owner"] == key]
    inventory = docs.files(key)
    touched = {f["path"] for f in own if f["how"] not in ("missing", "no hits", "not shown")}
    body = next((a for a in accesses if a["op"] == "inject"), None)
    totals = {
        "files": len(out), "own_files": len(own), "own_inventory": len(inventory),
        "own_shown": sum(1 for f in own if f["path"] in touched),
        "own_full": sum(1 for f in own if f["how"] in ("full", "injected", "injected + re-read")),
        "own_partial": sum(1 for f in own if f["how"] in ("partial", "hits")),
        "other_files": len(out) - len(own), "other_owners": sorted({f["owner"] for f in out if f["owner"] != key}),
        "reads": sum(1 for a in accesses if a["op"] == "read"),
        "searches": sum(1 for a in accesses if a["op"] == "search" and not (a["how"] or "").startswith("searched")),
        "listings": sum(1 for a in accesses if a["op"] == "list"),
        "resolves": sum(1 for a in accesses if a["op"] == "resolve"),
        "rereads": sum(f["rereads"] for f in out),
        "missing": sum(1 for f in out if f["how"] == "missing"),
        "version_mismatches": sum(1 for f in out if f["version"] not in (None, "match", "as installed now")),
        "body_chars": body["chars"] if body else None,
        "doc_chars": sum(a["chars"] or 0 for a in accesses if a["op"] != "inject"),
        "unique_doc_chars": sum(f["unique_chars"] or 0 for f in out if f["how"] != "injected"),
        "unprompted": sum(1 for f in out if f["found_by"] == "unprompted"),
    }
    totals["doc_tokens"] = round(totals["doc_chars"] / 4)
    return {"accesses": accesses, "files": out,
            "inventory": {"owner": key, "files": inventory, "never": [p for p in inventory if p not in touched]},
            "totals": totals}


def exposure(hunks, files, key, skill_md_text=None):
    """Whether a run was shown what a change to the skill added.

    `hunks` is versions.SkillSource.diff_hunks(older, newer); `files` a run's per-file summary (run_files()
    ["files"]), measured at `newer`. For each changed file: the lines the change added or rewrote (newer's
    numbering) and how many the run saw. SKILL.md's body is always seen, since the invocation injects it;
    its frontmatter never is (it decides when the skill triggers, not what it says).
    """
    shown = {}
    for f in files:
        if f["owner"] == key:
            s = shown.setdefault(f["path"], set())
            for lo, hi in f["lines"] or ():
                s.update(range(lo, hi + 1))
    fm = versions.frontmatter_lines(skill_md_text) if skill_md_text else 0
    out = []
    for path, h in sorted(hunks.items()):
        changed = set()
        for lo, hi in h["ranges"]:
            changed.update(range(lo, hi + 1))
        front = {i for i in changed if i <= fm} if path == "SKILL.md" else set()
        changed -= front
        seen = changed & shown.get(path, set())
        if not changed:
            status = "frontmatter only" if front else "deletions only"
        elif len(seen) == len(changed):
            status = "seen"
        elif seen:
            status = "partly seen"
        elif path in shown:
            status = "not in the lines read"
        else:
            status = "file not read"
        out.append({"path": path, "added": h["added"], "removed": h["removed"], "ranges": h["ranges"],
                    "changed_lines": len(changed), "seen_lines": len(seen), "frontmatter_lines": len(front),
                    "deletions": len(h["deletions"]), "status": status})
    return out
