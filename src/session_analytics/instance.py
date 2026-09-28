"""What a skill run left in its Metabase: the objects it created or changed, checks on them, and the source tables it
built on.

The transcript holds what the agent did; the instance holds what it made. The instance is often gone soon after (a
local Metabase in Docker), so the SessionEnd hook captures it, once per run, into a file under
<export root>/_instances/<session id>/, and every load reads the file: a reload never needs the instance again.

Everything goes through the Metabase CLI (`mb`) with the profile the run used, so no credential is read here. Only a
local instance (the experimenter's own: localhost, *.localhost) is captured: listing a shared production Metabase's
content from a hook would load it for everyone. Of a source table only its size and Metabase's own column statistics
are kept (how many columns of each kind, keys, relationships, the share of empty values), never a value it holds.
"""
from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from . import util
from .export import default_root

SNAPSHOT_VERSION = 1
MB_TIMEOUT_S = 90
# An object belongs to the run when it was created or changed between the run's start and this long after its last
# event: a create's response can land after the call that logged it.
WINDOW_SLACK_MS = 60_000
CARD_QUERY_CAP = 200
SOURCE_TABLE_CAP = 100
TEXT_CAP = 4000
ERROR_CAP = 500
# Questions and dashboards are found by listing them all; past this many questions an instance is not listed whole.
LIST_CAP = 5000
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "host.docker.internal")

# How a command names the profile it runs against: --profile x, -p x, MB_PROFILE=x, or a PROFILE=x the commands pass on.
PROFILE_RE = re.compile(r"(?:--profile[= ]|(?<![\w-])-p |\bMB_PROFILE=|\bPROFILE=)[\"']?([\w.:/@-]+)")
METRIC_RE = re.compile(r'\["(?:metric|measure)",')
# A name a probe or a leftover gets: _x, test_x, tmp x, "Copy of x", x_smoke_y — not a business word ("Billing page test").
SCRATCH_RE = re.compile(r"^_|^(copy of|test|tmp|temp|scratch)([\s_:-]|$)|_(test|tmp|temp|smoke|probe|scratch)(_|$)", re.I)
AGGREGATE_SQL_RE = re.compile(r"\b(sum|count|avg|min|max|percentile_cont|median|quantile\w*)\s*\(", re.I)
TEXT_KINDS = ("text", "heading")

# kind -> (`mb` noun, the command's list args). A kind an instance lacks (transform tests without the feature,
# documents before they existed) is skipped.
KINDS = {
    "card": ("card", ()),
    "transform": ("transform", ()),
    "transform_test": ("transform-test", ()),
    "dashboard": ("dashboard", ()),
    "measure": ("measure", ()),
    "segment": ("segment", ()),
    "document": ("document", ()),
}


class MbError(RuntimeError):
    pass


def snapshot_dir(root=None):
    return Path(root) if root else default_root() / "_instances"


def snapshot_path(session_id, run_id, root=None):
    return snapshot_dir(root) / session_id / (run_id.replace(":", "_") + ".json")


def run_cli(args, timeout=MB_TIMEOUT_S):
    """`mb <args>`'s stdout; MbError when it fails or cannot run."""
    try:
        res = subprocess.run(["mb", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MbError(f"mb {' '.join(args[:2])}: {exc}") from exc
    if res.returncode != 0 or not res.stdout.strip():
        raise MbError(f"mb {' '.join(args[:2])}: {_failure(res.stdout, res.stderr)[:ERROR_CAP]}")
    return res.stdout


def _failure(stdout, stderr):
    """What a failed `mb` call said: its JSON error's message, else its stderr without the version-skew notice."""
    try:
        err = json.loads(stdout).get("error") or {}
        if err.get("message"):
            return err["message"]
    except (ValueError, AttributeError):
        pass  # not the CLI's JSON error: fall back to what it printed
    lines = [ln for ln in (stderr or stdout).splitlines() if ln.strip() and not ln.startswith("Could not parse the Metabase version")]
    return " ".join(lines).strip()


class Mb:
    """The CLI on one profile, parsed: `mb(*args)` one JSON value, `mb.all(*args)` every item of a list command."""

    def __init__(self, profile, runner=run_cli):
        self.profile, self.runner = profile, runner

    def __call__(self, *args):
        out = self.runner([*args, "--profile", self.profile, "--json", "--max-bytes", "0"])
        try:
            return json.loads(out)
        except ValueError as exc:
            raise MbError(f"mb {' '.join(args[:2])}: not JSON: {out[:200]}") from exc

    def all(self, *args):
        items, offset = [], 0
        while True:
            page = self(*args, "--offset", str(offset))
            if isinstance(page, list):
                return page
            items += page.get("data") or []
            if not page.get("has_more") or page.get("next_offset") is None:
                return items
            offset = page["next_offset"]


def known_profiles(runner=run_cli):
    """{profile: url} of every profile the CLI knows."""
    out = runner(["auth", "list", "--json", "--fields", "profile,url", "--max-bytes", "0"])
    try:
        rows = json.loads(out)
    except ValueError as exc:
        raise MbError(f"mb auth list: not JSON: {out[:200]}") from exc
    rows = rows.get("data") if isinstance(rows, dict) else rows
    return {r["profile"]: r.get("url") for r in rows or () if r.get("profile")}


def is_local(url):
    host = urlparse(url or "").hostname or ""
    return host in LOCAL_HOSTS or host.endswith(".localhost")


def host_of(url):
    """host[:port] of a URL: what the warehouse says about the instance, never a path or a credential."""
    p = urlparse(url or "")
    return p.hostname + (f":{p.port}" if p.port else "") if p.hostname else None


def profiles_of(commands, prompt, known):
    """The profiles a run used, most used first: named in its commands, or, when none is, the one whose instance the
    prompt names."""
    counts = Counter(m.group(1) for c in commands for m in PROFILE_RE.finditer(c or "") if m.group(1) in known)
    if counts:
        return [p for p, _ in counts.most_common()]
    return [p for p, url in known.items() if host_of(url) and host_of(url) in (prompt or "")][:1]


def run_commands(a, s, run):
    """The shell commands of a run's tool calls."""
    steps = a["trace"]["steps"]
    out = []
    for i in run.get("steps") or ():
        st = steps[i]
        c = s.tool_calls.get(st.get("id")) if st.get("k") == "tool" else None
        if c is not None and c.name == "Bash":
            out.append(c.input.get("command") or "")
    return out


def _ms(ts):
    """Milliseconds of an ISO timestamp (Metabase's), or None."""
    return util.parse_ts(ts) if ts else None


def _in_run(obj, start, end):
    created, updated = _ms(obj.get("created_at")), _ms(obj.get("updated_at"))
    if created is not None and start <= created <= end:
        return "created"
    if updated is not None and start <= updated <= end:
        return "changed"
    return None


def _cap(text, n=TEXT_CAP):
    return None if text is None else str(text)[:n]


def _query(dq):
    """(query_kind, definition) of a dataset query: the SQL of a native one, the MBQL of another."""
    if not dq:
        return None, None
    stage = (dq.get("stages") or [{}])[0]
    if stage.get("lib/type") == "mbql.stage/native":
        return "native", _cap(stage.get("native"))
    if dq.get("type") == "native":
        return "native", _cap((dq.get("native") or {}).get("query"))
    return "mbql", _cap(json.dumps(dq, sort_keys=True))


def _card(c):
    kind, definition = _query(c.get("dataset_query"))
    return {"kind": c.get("type") if c.get("type") in ("model", "metric") else "question", "display": c.get("display"),
            "query_kind": kind, "definition": definition, "database_id": c.get("database_id"),
            "dashboard_id": c.get("dashboard_id"),
            "uses_metric": bool(definition and kind == "mbql" and METRIC_RE.search(definition))}


def _transform(t):
    src = t.get("source") or {}
    kind, definition = _query(src.get("query")) if src.get("query") else (src.get("type"), _cap(json.dumps(src)))
    target = t.get("target") or {}
    return {"kind": "transform", "query_kind": kind, "definition": definition, "database_id": target.get("database"),
            "target_table": ".".join(x for x in (target.get("schema"), target.get("name")) if x) or None,
            "last_run_status": (t.get("last_run") or {}).get("status"),
            "depends_on_tables": [d["table"] for d in t.get("table_dependencies") or () if "table" in d]}


def _dashboard(d):
    cards = d.get("dashcards") or []
    has_filters = bool(d.get("parameters"))

    def is_text(dc):
        return dc.get("card_id") is None and ((dc.get("visualization_settings") or {}).get("virtual_card") or {}).get(
            "display") in TEXT_KINDS
    return {"kind": "dashboard", "tabs": len(d.get("tabs") or ()), "dashboard_filters": len(d.get("parameters") or ()),
            "dashcards": len(cards), "card_dashcards": sum(1 for dc in cards if dc.get("card_id") is not None),
            "text_dashcards": sum(1 for dc in cards if is_text(dc)),
            "unmapped_dashcards": sum(1 for dc in cards if dc.get("card_id") is not None and has_filters
                                      and not dc.get("parameter_mappings")),
            "card_ids": sorted({dc["card_id"] for dc in cards if dc.get("card_id") is not None})}


def _common(kind, obj, in_run):
    return {"kind": kind, "id": obj.get("id"), "name": obj.get("name"), "in_run": in_run,
            "created_at": obj.get("created_at"), "updated_at": obj.get("updated_at"),
            "collection_id": obj.get("collection_id"), "description": _cap(obj.get("description"))}


def artifacts(mb, start, end, skipped):
    """Every object of the instance created or changed in [start, end] (ms), in the shape the loader reads; a kind
    the instance could not list goes into `skipped` with why."""
    out = []
    listed = {}
    # the search index's count: cheap, and the first call, so an instance that does not answer fails here
    questions = mb("search", "--models", "card,dataset,metric", "--limit", "1").get("total") or 0
    for kind, (noun, args) in KINDS.items():
        if kind in ("card", "dashboard") and questions > LIST_CAP:
            skipped[kind] = f"{questions} questions: more than {LIST_CAP}, not listed"
            continue
        try:
            listed[kind] = mb.all(noun, "list", *args, "--full")
        except MbError as exc:
            skipped[kind] = str(exc)[:ERROR_CAP]
    for kind, items in listed.items():
        for obj in items:
            if obj.get("archived"):
                continue
            w = _in_run(obj, start, end)
            if w is None:
                continue
            a = _common(kind, obj, w)
            if kind == "card":
                a.update(_card(obj))
            elif kind == "transform":
                a.update(_transform(obj))
            elif kind == "transform_test":
                a.update({"transform_id": obj.get("transform_id")})
            elif kind == "dashboard":
                try:
                    a.update(_dashboard(mb("dashboard", "get", str(obj["id"]), "--full")))
                except MbError as exc:
                    skipped[f"dashboard {obj['id']}"] = str(exc)[:ERROR_CAP]
            elif kind in ("measure", "segment"):
                a.update({"definition": _cap(json.dumps(obj.get("definition"), sort_keys=True)),
                          "table_id": obj.get("table_id"), "query_kind": "mbql"})
            out.append(a)
    for a in [x for x in out if x["kind"] in ("question", "model", "metric")][:CARD_QUERY_CAP]:
        try:
            res = mb("card", "query", str(a["id"]), "--limit", "1")
            a["run"] = {"status": res.get("status"), "row_count": res.get("row_count"),
                        "error": _cap(res.get("error"), ERROR_CAP)}
        except MbError as exc:
            a["run"] = {"status": "failed", "row_count": None, "error": _cap(str(exc), ERROR_CAP)}
    return out


def _field_kind(f):
    base = f.get("base_type") or ""
    if base in ("type/JSON", "type/SerializedJSON", "type/Structured", "type/Dictionary", "type/Array"):
        return "json"
    for kind, marks in (("boolean", ("Boolean",)), ("temporal", ("Date", "Time")),
                        ("numeric", ("Integer", "Float", "Decimal", "Number")), ("text", ("Text",))):
        if any(m in base for m in marks):
            return kind
    return "other"


def profile_table(t, fields, rows):
    """A source table as counts: never a value from it."""
    kinds = Counter(_field_kind(f) for f in fields)
    nulls = [((f.get("fingerprint") or {}).get("global") or {}).get("nil%") for f in fields]
    nulls = [n for n in nulls if n is not None]

    def text_json(f):
        return (((f.get("fingerprint") or {}).get("type") or {}).get("type/Text") or {}).get("percent-json") or 0
    return {"table_id": t.get("id"), "db_id": t.get("db_id"), "schema": t.get("schema"), "name": t.get("name"),
            "rows": rows, "columns": len(fields),
            "pk_columns": sum(1 for f in fields if f.get("semantic_type") == "type/PK"),
            "fk_columns": sum(1 for f in fields if f.get("semantic_type") == "type/FK" or f.get("fk_target_field_id")),
            "numeric_columns": kinds["numeric"], "temporal_columns": kinds["temporal"], "text_columns": kinds["text"],
            "boolean_columns": kinds["boolean"], "json_columns": kinds["json"],
            "text_json_columns": sum(1 for f in fields if _field_kind(f) == "text" and text_json(f) > 0.5),
            "coerced_columns": sum(1 for f in fields if f.get("coercion_strategy")),
            "empty_columns": sum(1 for n in nulls if n >= 1), "mostly_empty_columns": sum(1 for n in nulls if n >= 0.5),
            "max_null_share": max(nulls) if nulls else None}


def _count_rows(mb, db_id, table_id):
    q = {"lib/type": "mbql/query", "database": db_id,
         "stages": [{"lib/type": "mbql.stage/mbql", "source-table": table_id,
                     "aggregation": [["count", {"lib/uuid": "00000000-0000-4000-8000-000000000001"}]]}]}
    res = mb("query", "--body", json.dumps(q))
    rows = (res.get("data") or {}).get("rows") or []
    return rows[0][0] if res.get("status") == "completed" and rows and rows[0] else None


def source_tables(mb, arts, start, end, skipped):
    """The tables the run built on, in the databases its transforms and questions read: every active table no
    transform writes, and none the run itself created next to its transforms' output (a smoke test's, a probe's)."""
    dbs = {a.get("database_id") for a in arts if a.get("database_id") is not None}
    out_schemas = {(a.get("database_id"), (a.get("target_table") or "").split(".")[0])
                   for a in arts if a.get("kind") == "transform" and a.get("target_table")}
    picked = []
    for db in sorted(dbs):
        try:
            tables = mb.all("table", "list", "--db-id", str(db), "--full")
        except MbError as exc:
            skipped[f"database {db}"] = str(exc)[:ERROR_CAP]
            continue
        for t in tables:
            if not t.get("active", True) or t.get("transform_id") is not None or t.get("transform"):
                continue
            created = _ms(t.get("created_at"))
            if (db, t.get("schema")) in out_schemas and created is not None and start <= created <= end:
                continue
            picked.append(t)
    out = []
    for t in picked[:SOURCE_TABLE_CAP]:
        try:
            fields = mb("table", "get", str(t["id"]), "--include", "fields", "--full").get("fields") or []
        except MbError as exc:
            skipped[f"table {t['id']}"] = str(exc)[:ERROR_CAP]
            continue
        rows = t.get("estimated_row_count")
        if rows is None:
            try:
                rows = _count_rows(mb, t.get("db_id"), t["id"])
            except MbError:
                rows = None
        out.append(profile_table(t, fields, rows))
    return out


def capture(run, profile, url, mb, now_ms):
    """One run's snapshot of one instance. An instance that does not answer gives reachable: false and the error."""
    start, end = run["start_ms"], (run.get("end_ms") or run["start_ms"]) + WINDOW_SLACK_MS
    snap = {"version": SNAPSHOT_VERSION, "run_id": run["run_id"], "session_id": run["session_id"], "profile": profile,
            "host": host_of(url), "captured_at": util.iso(now_ms), "window": [util.iso(start), util.iso(end)],
            "window_start_ms": start, "window_end_ms": end,
            "reachable": False, "error": None, "skipped": {}, "artifacts": [], "source_tables": []}
    if not is_local(url):
        snap["error"] = f"not captured: {snap['host']} is not a local instance"
        return snap
    try:
        arts = artifacts(mb, start, end, snap["skipped"])
        snap["reachable"] = True
        snap["artifacts"] = arts
        snap["source_tables"] = source_tables(mb, arts, start, end, snap["skipped"])
    except MbError as exc:
        snap["error"] = str(exc)[:ERROR_CAP]
    return snap


def _current(path, run):
    """The snapshot on disk answered and covers the whole run (a resumed session can take a run further)."""
    try:
        snap = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    end = (run.get("end_ms") or run["start_ms"]) + WINDOW_SLACK_MS
    return bool(snap.get("reachable")) and (snap.get("window_end_ms") or 0) >= end


def capture_session(a, s, now_ms, root=None, runner=run_cli, log=None, wanted=None):
    """Capture every run of the session (`wanted(run)`: of the skills in scope) that used an mb profile and has no
    current snapshot (one that answered and covers the run to its end): [paths written]."""
    todo = [(run, snapshot_path(s.session_id, run["run_id"], root)) for run in a.get("skill_runs") or ()
            if wanted is None or wanted(run)]
    todo = [(run, path) for run, path in todo if not _current(path, run)]
    if not todo:
        return []
    known = known_profiles(runner)
    written = []
    for run, path in todo:
        profiles = profiles_of(run_commands(a, s, run), run.get("prompt"), known)
        if not profiles:
            continue
        profile = profiles[0]
        snap = capture(run, profile, known[profile], Mb(profile, runner), now_ms)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, indent=1))
        tmp.replace(path)
        written.append(path)
        if log:
            state = f"{len(snap['artifacts'])} objects, {len(snap['source_tables'])} source tables" if snap["reachable"] \
                else f"unreachable: {snap['error']}"
            log(f"  instance {snap['host']} for run {run['run_id']}: {state}")
    return written


def load_snapshots(session_id, root=None):
    """The session's snapshots on disk, run by run."""
    d = snapshot_dir(root) / session_id
    out = []
    for p in sorted(d.glob("*.json")) if d.is_dir() else ():
        try:
            out.append(json.loads(p.read_text()))
        except ValueError:
            continue  # a half-written capture is retried by the next one; it holds nothing to load
    return out


# (id, the kinds it applies to, what it checks). Derived at load time from the snapshot, so a check can change
# without the instance.
CHECKS = [
    ("runs", ("question", "model", "metric"), "Runs: the query completes"),
    ("returns-rows", ("question", "model", "metric"),
     "Returns rows (questions: those on a dashboard; an exception question elsewhere is empty by design)"),
    ("described", ("question", "model", "metric", "transform", "dashboard", "measure", "segment"),
     "Has a description"),
    ("transform-ran", ("transform",), "The transform's last run succeeded"),
    ("transform-tested", ("transform",), "The transform has a transform test"),
    ("on-a-dashboard", ("question",), "The question is on one of the run's dashboards"),
    ("uses-a-metric", ("question",),
     "A question that aggregates does it with a metric or measure by id instead of re-deriving the number"),
    ("filters-reach-cards", ("dashboard",), "Every card on the dashboard is wired to a filter"),
    ("has-a-note", ("dashboard",), "The dashboard has a text card (as-of, trust label, definitions)"),
    ("no-scratch-name", ("question", "model", "metric", "transform", "dashboard"),
     "Its name is not a probe's or a leftover's (_x, test, tmp, smoke, scratch, copy of)"),
]


def aggregates(a):
    """The object's query computes a number: an MBQL stage with an aggregation, or SQL calling an aggregate."""
    definition = a.get("definition") or ""
    if a.get("query_kind") == "mbql":
        return '"aggregation"' in definition or bool(METRIC_RE.search(definition))
    return bool(AGGREGATE_SQL_RE.search(definition))


def check_rows(arts):
    """[(artifact, check_id, status, detail)] for every check that applies."""
    on_dash = {cid for a in arts if a["kind"] == "dashboard" for cid in a.get("card_ids") or ()}
    tested = Counter(a.get("transform_id") for a in arts if a["kind"] == "transform_test")
    out = []
    for a in arts:
        run = a.get("run") or {}
        shown = a.get("id") in on_dash or a.get("dashboard_id") is not None
        empty_matters = a["kind"] != "question" or shown
        verdicts = {
            "runs": (run.get("status") == "completed", run.get("error")),
            "returns-rows": ((run.get("row_count") or 0) > 0, None)
            if run.get("status") == "completed" and empty_matters else None,
            "described": (bool((a.get("description") or "").strip()), None),
            "transform-ran": (a.get("last_run_status") == "succeeded", a.get("last_run_status")),
            "transform-tested": (tested[a.get("id")] > 0, None),
            "on-a-dashboard": (shown, None),
            "uses-a-metric": (bool(a.get("uses_metric")), a.get("query_kind")) if aggregates(a) else None,
            "filters-reach-cards": ((a.get("unmapped_dashcards") or 0) == 0,
                                    f"{a.get('unmapped_dashcards')} of {a.get('card_dashcards')} cards unwired")
            if a.get("dashboard_filters") else None,
            "has-a-note": ((a.get("text_dashcards") or 0) > 0, None),
            "no-scratch-name": (not SCRATCH_RE.search(a.get("name") or ""), None),
        }
        for cid, kinds, _desc in CHECKS:
            v = verdicts[cid]
            if a["kind"] in kinds and v is not None:
                ok, detail = v
                out.append((a, cid, "pass" if ok else "fail", None if ok else detail))
    return out


def owned(snaps):
    """Each snapshot's artifacts, every object kept by one run only: of the runs whose windows hold its last touch on
    the same instance, the one that started last (runs of one session can overlap)."""
    starts = {(x.get("host"), x["run_id"]): x.get("window_start_ms") or 0 for x in snaps}
    holders = {}
    for x in snaps:
        for a in x.get("artifacts") or ():
            holders.setdefault((x.get("host"), a["kind"], a.get("id")), []).append(x["run_id"])
    out = {}
    for x in snaps:
        host, keep = x.get("host"), []
        for a in x.get("artifacts") or ():
            touched = _ms(a.get("created_at") if a.get("in_run") == "created" else a.get("updated_at")) or 0
            runs = holders[(host, a["kind"], a.get("id"))]
            fits = [r for r in runs if starts[(host, r)] <= touched] or runs
            if max(fits, key=lambda r: starts[(host, r)]) == x["run_id"]:
                keep.append(a)
        out[x["run_id"]] = keep
    return out


def session_rows(session_id, redact, root=None):
    """{table: rows} for the instance tables, from the session's snapshots; `redact` is applied to free text."""
    rows = {"run_instances": [], "run_artifacts": [], "run_artifact_checks": [], "run_source_tables": []}
    snaps = load_snapshots(session_id, root)
    mine = owned(snaps)
    for snap in snaps:
        key = {"session_id": session_id, "run_id": snap["run_id"], "host": snap.get("host")}
        arts = mine[snap["run_id"]]
        rows["run_instances"].append(dict(key, profile=snap.get("profile"), captured_at=snap.get("captured_at"),
                                          reachable=snap.get("reachable"), error=redact(snap.get("error")),
                                          skipped=redact("; ".join(f"{k}: {v}" for k, v in
                                                                         (snap.get("skipped") or {}).items()) or None),
                                          objects_created=sum(1 for a in arts if a.get("in_run") == "created"),
                                          objects_changed=sum(1 for a in arts if a.get("in_run") == "changed"),
                                          source_tables=len(snap.get("source_tables") or ())))
        on_dash = Counter(cid for a in arts if a["kind"] == "dashboard" for cid in a.get("card_ids") or ())
        tests = Counter(a.get("transform_id") for a in arts if a["kind"] == "transform_test")
        for a in arts:
            run = a.get("run") or {}
            rows["run_artifacts"].append(dict(
                key, kind=a["kind"], object_id=a.get("id"), name=redact(a.get("name")), in_run=a.get("in_run"),
                created_at=a.get("created_at"), updated_at=a.get("updated_at"), collection_id=a.get("collection_id"),
                description=redact(a.get("description")), query_kind=a.get("query_kind"),
                definition=redact(a.get("definition")), display=a.get("display"),
                database_id=a.get("database_id"), dashboard_id=a.get("dashboard_id"),
                target_table=a.get("target_table"), last_run_status=a.get("last_run_status"),
                transform_tests=tests[a.get("id")] if a["kind"] == "transform" else None,
                tabs=a.get("tabs"), dashboard_filters=a.get("dashboard_filters"), dashcards=a.get("dashcards"),
                card_dashcards=a.get("card_dashcards"), text_dashcards=a.get("text_dashcards"),
                unmapped_dashcards=a.get("unmapped_dashcards"),
                on_dashboards=on_dash[a.get("id")] if a["kind"] == "question" else None,
                uses_metric=a.get("uses_metric"), run_status=run.get("status"), row_count=run.get("row_count"),
                run_error=redact(run.get("error"))))
        for a, cid, status, detail in check_rows(arts):
            rows["run_artifact_checks"].append(dict(key, kind=a["kind"], object_id=a.get("id"), check_id=cid,
                                                    status=status, detail=redact(_cap(detail, ERROR_CAP))))
        for t in snap.get("source_tables") or ():
            rows["run_source_tables"].append(dict(key, schema_name=t.get("schema"), table_name=t.get("name"),
                                                  **{k: t.get(k) for k in SOURCE_TABLE_COLUMNS}))
    return rows


SOURCE_TABLE_COLUMNS = ("table_id", "db_id", "rows", "columns", "pk_columns", "fk_columns",
                        "numeric_columns", "temporal_columns", "text_columns", "boolean_columns", "json_columns",
                        "text_json_columns", "coerced_columns", "empty_columns", "mostly_empty_columns",
                        "max_null_share")
