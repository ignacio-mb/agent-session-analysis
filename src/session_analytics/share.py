"""Share sessions as a file, for whoever holds the team's ClickHouse connection to load.

    session-analytics share                       this machine's sessions that ran rde, as one file to send
    session-analytics warehouse --import FILE     (the one holding the connection) load such files

Someone without the ClickHouse connection string shares a file instead of syncing. It holds the rows a direct sync
would write (warehouse.build, with secret-looking strings masked) of the sessions in which one of the skills ran,
and who and which machine they come from: JSON, one row per line, so they can read exactly what they send. Only
those sessions go in: a session that never ran the skill leaves neither its rows nor its id.

The import loads each file with the same sync as a direct load, under the sender's source: a newer file from the
same machine updates their sessions, and no one else's rows change. A file older than the last one imported from
that machine is skipped, so sending an old file again rolls nothing back. The shared taxonomy is the importer's, as
in a direct load. The skill versions are labelled again on the importer's side: a tester who installed the skill
without its git history can't tell which commit ran, so each run carries what its version is resolved from (the
fingerprint of the injected SKILL.md, and the skill files it read), resolved against the importer's checkout.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import time
from pathlib import Path

from . import __version__, clickhouse, locate, semantics, util, versions, warehouse
from .export import _slug, default_root
from .rollup import parse_since

FORMAT = "agent-session-analysis/share/1"
SOURCE_RE = re.compile(r"^[0-9a-f]{16}$")
# Tables that carry a run's version, next to its run_id: labelled again together.
VERSIONED = [n for n, (_, cols, _) in warehouse.TABLES.items() if {"run_id", "version"} <= {c for c, _, _ in cols}]


class ShareError(RuntimeError):
    pass


# ---------------------------------------------------------------- sessions left out, remembered on this machine
def _excluded_path():
    return clickhouse.config_dir() / "share.json"


def read_excluded():
    """Sessions the user left out with --exclude: never in a share file again until --include names them."""
    try:
        return set(json.loads(_excluded_path().read_text()).get("excluded") or ())
    except (OSError, ValueError, AttributeError):
        return set()


def update_excluded(exclude=(), include=(), claude_dir=None):
    """Add --exclude'd sessions and drop --include'd ones (ids, prefixes, `current`, or transcript paths). A session
    whose transcript is gone can still be named by its full id. Returns the set now left out."""
    out = read_excluded()
    if not exclude and not include:
        return out
    for spec in exclude:
        out.add(_session_id(spec, claude_dir))
    for spec in include:
        hits = {x for x in out if x.startswith(spec)}
        if len(hits) > 1:
            raise locate.SessionNotFound(f"'{spec}' matches {len(hits)} sessions left out; use more characters")
        out -= hits or {_session_id(spec, claude_dir)}
    path = _excluded_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"excluded": sorted(out)}, indent=1) + "\n")
    return out


def _session_id(spec, claude_dir):
    try:
        return locate.resolve(spec, cdir=claude_dir).stem
    except locate.SessionNotFound:
        if locate.UUID_RE.match(spec):
            return spec.lower()
        raise


# ---------------------------------------------------------------- the file
def _pattern(skills):
    """What the transcripts of a session that ran one of `skills` must contain: the Skill tool called with it
    ("skill":"rde", or a plugin's "skill":"agent-skills:rde"), or it as a slash or harness command
    (<command-name>/rde</command-name>). None for every session (`*`)."""
    if "*" in skills:
        return None
    names = b"|".join(re.escape(n).encode() for n in sorted({s.rsplit(":", 1)[-1] for s in skills}))
    return re.compile(rb'"skill":\s*"(?:[^"\\:]+:)?(?:' + names + rb')"'
                      rb"|<command-name>/?(?:[^<:]+:)?(?:" + names + rb")</command-name>")


def _mentions(transcript, pattern):
    """Whether a session's transcripts — the main one, and its subagents' and workflows' — match `pattern`: a quick
    look that spares parsing the sessions that cannot have run the skill. Unreadable counts as a yes, so the parse
    decides."""
    side = locate.side_dir(transcript)
    for f in [Path(transcript)] + (sorted(side.rglob("*.jsonl")) if side.is_dir() else []):
        try:
            data = f.read_bytes()
        except OSError:
            return True
        if pattern.search(data):
            return True
    return False


def build(claude_dir=None, since="all", skills=warehouse.DEFAULT_SKILLS, env_file=None, log=None, now_ms=None):
    """The share document of this machine's sessions that ran one of `skills`, minus those left out. Returns
    (document, {"failed": transcripts that did not read, "left_out": sessions that qualified but were left out})."""
    now = time.time() * 1000 if now_ms is None else now_ms
    cdir = locate.claude_dir(claude_dir)
    cutoff = parse_since(since, now)
    files = [f for f in locate.iter_transcripts(cdir) if f.stat().st_mtime * 1000 >= cutoff]
    pattern = _pattern(skills)
    picked = files if pattern is None else [f for f in files if _mentions(f, pattern)]
    if log:
        log(f"Reading {len(picked)} of {len(files)} transcripts (the others never invoke {', '.join(skills)})…")
    tables, meta = warehouse.build(cdir, since=since, redact=True, log=log, now_ms=now,
                                   transcripts=[str(f) for f in picked])
    excluded = read_excluded()
    qualifying = warehouse.sessions_with_skill(tables, skills)
    keep = qualifying - excluded
    rows = warehouse.only_sessions(tables, keep, skills)
    ident = clickhouse.identity(cdir, env_file)
    doc = {
        "format": FORMAT, "generator_version": __version__, "created_at": util.iso(now), "person": ident.person,
        "source": ident.source, "machine": ident.machine, "skills": sorted(skills), "since": since,
        "withdrawn": sorted(excluded),
        "versions": {rid: v for rid, v in sorted(meta["versions"].items()) if v["session_id"] in keep},
        "tables": {k: v for k, v in rows.items() if k not in clickhouse.GLOBAL},
    }
    return doc, {"failed": meta["failed"], "left_out": sorted(qualifying & excluded)}


def _dumps(v):
    return json.dumps(v, ensure_ascii=False, default=str)


def render(doc):
    """The document as JSON with one row per line: the header first, then each table's rows."""
    head = [f" {_dumps(k)}: {_dumps(v)}" for k, v in doc.items() if k not in ("versions", "tables")]
    runs = ",\n".join(f"  {_dumps(k)}: {_dumps(v)}" for k, v in doc["versions"].items())
    tables = ",\n".join(f"  {_dumps(name)}: [" + ("\n" + ",\n".join(f"   {_dumps(r)}" for r in rows) + "\n  "
                                                   if rows else "") + "]"
                        for name, rows in doc["tables"].items())
    body = head + [' "versions": {' + (f"\n{runs}\n " if runs else "") + "}",
                   ' "tables": {' + (f"\n{tables}\n " if tables else "") + "}"]
    return "{\n" + ",\n".join(body) + "\n}\n"


def default_path(doc, compress=False, now_ms=None):
    """~/claude-session-exports/_share/sessions-<person>-<time>.json: the name says whose, and when."""
    stamp = util.local_str(time.time() * 1000 if now_ms is None else now_ms, "%Y%m%d-%H%M")
    who = _slug((doc.get("person") or "someone").split("@")[0], 30)
    return default_root() / "_share" / f"sessions-{who}-{stamp}.json{'.gz' if compress else ''}"


def write(doc, path, compress=False):
    """Write the file (owner-only: it holds prompt previews and file paths). Returns its size in bytes."""
    data = render(doc).encode("utf-8")
    if compress:
        data = gzip.compress(data)
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    tmp.replace(path)
    return len(data)


def read(path):
    """A share file (plain or gzipped), checked: its format, whose it is, and a version this one can load."""
    try:
        data = Path(path).read_bytes()
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        doc = json.loads(data.decode("utf-8"))
    except (OSError, EOFError, ValueError) as exc:  # gzip and UTF-8 errors are ValueError (or EOFError)
        raise ShareError(f"not a share file: {exc}") from exc
    fmt = doc.get("format") if isinstance(doc, dict) else None
    if fmt != FORMAT:
        raise ShareError(f"not a share file (format {fmt!r}, expected {FORMAT})")
    if not SOURCE_RE.match(str(doc.get("source") or "")) or not doc.get("person"):
        raise ShareError("the file does not say whose sessions it holds (person, source)")
    if not isinstance(doc.get("tables"), dict) or not isinstance(doc["tables"].get("sessions"), list):
        raise ShareError("the file holds no sessions table")
    made = doc.get("generator_version")
    if clickhouse._numbers(made) > clickhouse._numbers(__version__):
        raise ShareError(f"made with {made}, newer than this {__version__}: update this checkout or plugin first")
    return doc


def expand(paths):
    """Files to import: each path given, and the share files in a folder given (.json, .json.gz)."""
    out = []
    for p in paths:
        p = Path(p).expanduser()
        if p.is_dir():
            out += sorted(f for f in p.iterdir() if f.is_file() and f.name.endswith((".json", ".json.gz")))
        else:
            out.append(p)
    return out


# ---------------------------------------------------------------- the import
def relabel(tables, runs, sources_extra=()):
    """Label each run the sender could not place with the commit that ran, against this machine's git checkout of
    the skill (found under ~/dev and friends, or `sources_extra`), and put the label on every row of the run. A run
    this machine can't place either keeps the sender's label. Returns how many runs got a commit."""
    found, sources = {}, {}
    for rid, v in runs.items():
        key = v.get("skill")
        if v.get("status") == "commit" or not v.get("fingerprint") or not key:
            continue
        if key not in sources:
            sources[key] = versions.find_sources(key, explicit=list(sources_extra))
        got = versions.resolve(v["fingerprint"], v.get("invoked_ms"), sources[key], v.get("read_hashes") or {})
        if got.get("status") == "commit":
            found[rid] = got
    for name in VERSIONED:
        for r in tables.get(name, ()):
            got = found.get(r.get("run_id"))
            if got is None:
                continue
            r["version"] = got["commit"]
            if name == "skill_runs":
                r.update(version_status="commit", version_date=got.get("date"), version_subject=got.get("subject"))
    return len(found)


def import_doc(doc, target, skills, sources_extra=(), rescope=False, log=None):
    """Load one share file into ClickHouse under its sender's source (see clickhouse.sync). The sender's list of
    sessions left out is theirs to say: those are taken out, and stay out until a later file includes them. Returns
    sync's result, with `relabelled` (runs given a commit here) and `skipped` (the file was older than the last one
    imported from that machine)."""
    ident = clickhouse.Identity(doc["source"], doc["person"], doc.get("machine") or "")
    cache = clickhouse.read_cache(target, ident.source) or {}
    last = cache.get("share_created_at")
    if last and (doc.get("created_at") or "") < last:
        return {"skipped": last}
    tables = {k: [dict(r) for r in doc["tables"].get(k) or ()] for k in warehouse.TABLES if k not in clickhouse.GLOBAL}
    tables["de_topics"], tables["de_layers"] = semantics.load().dimensions()  # the importer's, as in a direct load
    relabelled = relabel(tables, doc.get("versions") or {}, sources_extra)
    clickhouse.write_cache(target, ident.source, withdrawn=sorted(set(doc.get("withdrawn") or ())))
    res = clickhouse.sync(tables, target, ident, {r.get("session_id") for r in tables["sessions"]}, skills,
                          since=doc.get("since") or "all", rescope=rescope, log=log,
                          generator=doc.get("generator_version"))
    clickhouse.write_cache(target, ident.source, share_created_at=doc.get("created_at"))
    return dict(res, relabelled=relabelled, skipped=None)
