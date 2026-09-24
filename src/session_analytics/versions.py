"""Which version of a skill ran: map an invocation's fingerprint to a git commit of the skill's source.

The fingerprint is a hash of the SKILL.md body Claude Code injected (see parse.skill_body). The same hash is
computed for SKILL.md at every commit that touched the skill's directory, and for the working tree, so a run
can be labelled with the commit it ran. Several commits can share one SKILL.md (only a playbook changed);
the content hashes of the files the run actually read break that tie.

Sources are found automatically (the installed skill directory if it is a git checkout, and
~/dev|~/src|~/code|~/projects|~/work/*/skills/<name>) or given explicitly with --source / the
SESSION_ANALYTICS_SKILL_SOURCES environment variable (paths separated by the OS path separator).
"""

from __future__ import annotations

import glob
import hashlib
import os
import re
import subprocess
from pathlib import Path

from .parse import fingerprint_body
from .util import parse_ts

SEARCH_ROOTS = ("~/dev", "~/src", "~/code", "~/projects", "~/work", "~/repos")


def md_body(md):
    """SKILL.md as Claude Code injects it: the frontmatter removed, surrounding whitespace stripped."""
    if md.startswith("---"):
        end = md.find("\n---", 3)
        if end >= 0:
            md = md[end + 4:]
    return md.strip()


def _git(repo, *args):
    try:
        out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


class SkillSource:
    """A skill directory, and the git history behind it when it is in a repository."""

    def __init__(self, skill_dir):
        self.dir = Path(skill_dir).expanduser().resolve()
        top = _git(self.dir, "rev-parse", "--show-toplevel")
        self.repo = Path(top.strip()) if top else None
        self.rel = str(self.dir.relative_to(self.repo)) if self.repo else None
        self._commits = None
        self._files = {}

    def __repr__(self):
        return f"SkillSource({self.dir})"

    def commits(self):
        """Commits touching the skill directory, newest first, each with the fingerprint of its SKILL.md."""
        if self._commits is not None:
            return self._commits
        self._commits = []
        if not self.repo:
            return self._commits
        log = _git(self.repo, "log", "--format=%H%x1f%h%x1f%aI%x1f%s", "--", self.rel) or ""
        for line in log.splitlines():
            parts = line.split("\x1f")
            if len(parts) != 4:
                continue
            sha, short, date, subject = parts
            md = self.file_at(sha, "SKILL.md")
            self._commits.append({
                "sha": sha, "short": short, "date": date, "date_ms": parse_ts(date), "subject": subject,
                "fingerprint": fingerprint_body(md_body(md)) if md is not None else None,
            })
        return self._commits

    def file_at(self, commit, rel_path):
        key = (commit, rel_path)
        if key not in self._files:
            if commit == "WORKTREE":
                p = self.dir / rel_path
                self._files[key] = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None
            else:
                self._files[key] = _git(self.repo, "show", f"{commit}:{self.rel}/{rel_path}") if self.repo else None
        return self._files[key]

    def files_at(self, commit):
        """Every file of the skill at `commit` ("WORKTREE" for the directory as it is now), relative paths."""
        key = ("files", commit)
        if key not in self._files:
            if commit == "WORKTREE" or not self.repo:
                self._files[key] = list_dir(self.dir)
            else:
                out = _git(self.repo, "ls-tree", "-r", "--name-only", commit, "--", self.rel) or ""
                prefix = self.rel.rstrip("/") + "/"
                self._files[key] = sorted(line[len(prefix):] for line in out.splitlines() if line.startswith(prefix))
        return self._files[key]

    def diff_hunks(self, older, newer):
        """Lines each file gained between two commits, in the newer file's numbering.

        {path: {"ranges": [[first, last], ...], "added": n, "removed": n, "deletions": [line, ...]}}, where
        `deletions` are the places a hunk only removed text (nothing new there for a run to read).
        """
        if not self.repo:
            return {}
        out = _git(self.repo, "diff", "-U0", "--no-color", "--no-ext-diff", older, newer, "--", self.rel) or ""
        files, cur = {}, None
        prefix = self.rel.rstrip("/") + "/"
        for line in out.splitlines():
            if line.startswith("+++ "):
                path = line[4:].strip()
                path = path[2:] if path.startswith("b/") else path
                cur = None if path == "/dev/null" else files.setdefault(
                    path[len(prefix):] if path.startswith(prefix) else path,
                    {"ranges": [], "added": 0, "removed": 0, "deletions": []})
            elif line.startswith("@@") and cur is not None:
                m = HUNK_RE.match(line)
                if not m:
                    continue
                removed = int(m.group(2)) if m.group(2) is not None else 1
                start, count = int(m.group(3)), int(m.group(4)) if m.group(4) is not None else 1
                cur["removed"] += removed
                cur["added"] += count
                if count:
                    cur["ranges"].append([start, start + count - 1])
                else:
                    cur["deletions"].append(start)
        return files

    def working_tree(self):
        md = self.file_at("WORKTREE", "SKILL.md")
        dirty = bool(self.repo and (_git(self.repo, "status", "--porcelain", "--", self.rel) or "").strip())
        return {"fingerprint": fingerprint_body(md_body(md)) if md is not None else None, "dirty": dirty}

    def diffstat(self, older, newer):
        if not self.repo:
            return []
        out = _git(self.repo, "diff", "--numstat", older, newer, "--", self.rel) or ""
        rows = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                a, r, path = parts
                rows.append({"path": os.path.relpath(path, self.rel) if self.rel else path,
                             "added": int(a) if a.isdigit() else None, "removed": int(r) if r.isdigit() else None})
        return rows

    def log_between(self, older, newer):
        if not self.repo:
            return []
        out = _git(self.repo, "log", "--format=%h%x1f%aI%x1f%s", f"{older}..{newer}", "--", self.rel) or ""
        return [dict(zip(("short", "date", "subject"), line.split("\x1f"))) for line in out.splitlines() if line]


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", ".pytest_cache"}


def list_dir(root):
    """Files under `root`, relative, skipping VCS and cache directories."""
    root = Path(root)
    out = []
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for f in filenames:
            if not f.startswith("."):
                out.append(os.path.relpath(os.path.join(dirpath, f), root))
    return sorted(out)


def frontmatter_lines(text):
    """How many leading lines of a SKILL.md are YAML frontmatter (Claude Code injects only what follows)."""
    if not text or not text.startswith("---"):
        return 0
    lines = text.split("\n")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return i + 1
    return 0


def _skill_dirs_under(path, name):
    p = Path(path).expanduser()
    if (p / "SKILL.md").is_file():
        return [p]
    return [c for c in (p / "skills" / name, p / name) if (c / "SKILL.md").is_file()]


_SOURCES = {}
_FOUND = {}


def find_sources(name, base_dirs=(), explicit=()):
    """Candidate source directories for skill `name`, git checkouts first. Cached per process."""
    cache_key = (name, tuple(sorted(base_dirs)), tuple(explicit), os.environ.get("SESSION_ANALYTICS_SKILL_SOURCES", ""))
    if cache_key in _FOUND:
        return _FOUND[cache_key]
    seen, out = set(), []

    def add(d):
        try:
            r = Path(d).expanduser().resolve()
        except OSError:
            return
        if r in seen or not (r / "SKILL.md").is_file():
            return
        seen.add(r)
        if r not in _SOURCES:
            _SOURCES[r] = SkillSource(r)
        out.append(_SOURCES[r])

    env = os.environ.get("SESSION_ANALYTICS_SKILL_SOURCES", "")
    for d in list(explicit) + [x for x in env.split(os.pathsep) if x]:
        for c in _skill_dirs_under(d, name):
            add(c)
    for b in base_dirs:
        if b:
            add(b)
    for root in SEARCH_ROOTS:
        for hit in glob.glob(os.path.join(os.path.expanduser(root), "*", "skills", name, "SKILL.md")) + \
                glob.glob(os.path.join(os.path.expanduser(root), "*", name, "SKILL.md")):
            add(os.path.dirname(hit))
    out.sort(key=lambda s: s.repo is None)
    _FOUND[cache_key] = out
    return out


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12] if text is not None else None


def resolve(fingerprint, run_ms, sources, read_hashes=None):
    """The version a run used: {status, label, commit, date, subject, source, ...}.

    status is `commit` (a commit's SKILL.md matches), `working-tree` (matches uncommitted edits),
    `installed` (matches a non-git copy), or `unknown`.
    """
    if not fingerprint:
        return {"status": "unknown", "label": "unknown (no skill body in transcript)", "fingerprint": None}
    read_hashes = read_hashes or {}
    for src in sources:
        matches = [c for c in src.commits() if c["fingerprint"] == fingerprint]
        if matches:
            def score(c, _src=src):
                hits = sum(1 for rel, h in read_hashes.items() if _sha(_src.file_at(c["sha"], rel)) == h)
                on_time = c["date_ms"] is not None and run_ms is not None and c["date_ms"] <= run_ms + 60_000
                return (hits, on_time, c["date_ms"] or 0)
            best = max(matches, key=score)
            return {"status": "commit", "fingerprint": fingerprint, "commit": best["short"], "sha": best["sha"],
                    "date": best["date"], "subject": best["subject"], "source": str(src.dir),
                    "repo": str(src.repo), "candidates": len(matches),
                    "label": f"{best['short']} · {best['subject']}"}
        if src.repo:
            wt = src.working_tree()
            if wt["fingerprint"] == fingerprint:
                return {"status": "working-tree", "fingerprint": fingerprint, "source": str(src.dir),
                        "repo": str(src.repo), "label": "uncommitted working tree", "dirty": wt["dirty"]}
    for src in sources:
        if not src.repo:
            md = src.file_at("WORKTREE", "SKILL.md")
            if md is not None and fingerprint_body(md_body(md)) == fingerprint:
                return {"status": "installed", "fingerprint": fingerprint, "source": str(src.dir),
                        "label": f"installed copy ({src.dir.name}, not in git)"}
    return {"status": "unknown", "fingerprint": fingerprint, "label": f"unknown version {fingerprint[:8]}"}
