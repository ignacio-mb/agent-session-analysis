"""Find Claude Code's data directory and resolve a session reference to a transcript.

Layout (observed; the format is internal to Claude Code and can change):

    <claude_dir>/projects/<encoded-cwd>/<session-id>.jsonl         main transcript
    <claude_dir>/projects/<encoded-cwd>/<session-id>/subagents/     agent-<id>.jsonl + .meta.json
    <claude_dir>/projects/<encoded-cwd>/<session-id>/subagents/workflows/wf_<run>/  workflow agents
    <claude_dir>/projects/<encoded-cwd>/<session-id>/workflows/wf_<run>.json        workflow summaries
    <claude_dir>/projects/<encoded-cwd>/<session-id>/tool-results/  large tool outputs saved to disk

<claude_dir> is $CLAUDE_CONFIG_DIR when set, else ~/.claude. The cwd is encoded by
replacing every non-alphanumeric character with "-" (paths over 200 characters
are truncated and suffixed with a hash).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
SESSION_ENV_VARS = ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")


class SessionNotFound(Exception):
    pass


def claude_dir(override=None):
    if override:
        return Path(os.path.expanduser(override))
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return Path(os.path.expanduser(env))
    return Path.home() / ".claude"


def projects_dir(cdir):
    return Path(cdir) / "projects"


def encode_project(cwd):
    return re.sub(r"[^a-zA-Z0-9]", "-", str(cwd))


def project_dirs_for(cwd, cdir):
    """Project directories that hold transcripts for `cwd` (exact, or the truncated long-path form)."""
    root = projects_dir(cdir)
    enc = encode_project(os.path.abspath(os.path.expanduser(str(cwd))))
    exact = root / enc
    if exact.is_dir():
        return [exact]
    if len(enc) > 200 and root.is_dir():
        return [p for p in root.iterdir() if p.is_dir() and p.name.startswith(enc[:200])]
    return []


def iter_transcripts(cdir, project=None):
    """Main transcripts (not subagent files), newest first. `project` is a cwd or None for all."""
    root = projects_dir(cdir)
    if not root.is_dir():
        return []
    if project:
        dirs = project_dirs_for(project, cdir)
    else:
        dirs = [p for p in root.iterdir() if p.is_dir()]
    files = []
    for d in dirs:
        for f in d.glob("*.jsonl"):
            if f.is_file():
                files.append(f)
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def current_session_id(explicit=None):
    if explicit and explicit not in ("${CLAUDE_SESSION_ID}", "$CLAUDE_SESSION_ID"):
        return explicit
    for var in SESSION_ENV_VARS:
        v = os.environ.get(var)
        if v:
            return v
    return None


def resolve(spec, cdir=None, cwd=None, current=None):
    """Resolve 'current' | 'latest' | <uuid> | <uuid prefix> | <path.jsonl> to a transcript Path."""
    cdir = claude_dir(cdir)
    cwd = cwd or os.getcwd()
    spec = (spec or "current").strip()

    as_path = Path(os.path.expanduser(spec))
    if spec.endswith(".jsonl") or os.sep in spec:
        if as_path.is_file():
            return as_path
        raise SessionNotFound(f"no transcript at {as_path}")

    if spec == "current":
        sid = current_session_id(current)
        if sid:
            hit = _by_id(sid, cdir)
            if hit:
                return hit
        # No id available (or not yet flushed): newest transcript for this project.
        spec = "latest"

    if spec == "latest":
        files = iter_transcripts(cdir, project=cwd) or iter_transcripts(cdir)
        if not files:
            raise SessionNotFound(f"no transcripts under {projects_dir(cdir)}")
        return files[0]

    hit = _by_id(spec, cdir)
    if hit:
        return hit
    matches = [p for p in iter_transcripts(cdir) if p.stem.startswith(spec)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        listed = ", ".join(p.stem for p in matches[:8])
        raise SessionNotFound(f"'{spec}' matches {len(matches)} sessions ({listed}…); use more characters")
    raise SessionNotFound(f"no session matching '{spec}' under {projects_dir(cdir)}")


def _by_id(sid, cdir):
    root = projects_dir(cdir)
    if not root.is_dir():
        return None
    for d in root.iterdir():
        p = d / f"{sid}.jsonl"
        if p.is_file():
            return p
    return None


def side_dir(main_path):
    """<dir>/<session-id>/ holding subagents, workflows, tool-results, custom-title.json."""
    p = Path(main_path)
    return p.with_suffix("")
