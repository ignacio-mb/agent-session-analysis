"""session-analytics: export and analyze Claude Code sessions.

    session-analytics export [SESSION]      current | latest | <id or prefix> | <path.jsonl>
    session-analytics list                  recent sessions for this project (or --all)
    session-analytics rollup --since 7d     aggregate many sessions
    session-analytics skill rde             every run of a skill across sessions, by version, with checks
    session-analytics compare A B           two skill runs side by side (run ids like b3734789:1)
    session-analytics warehouse --up --load  every session into a local Postgres (tables + views)
    session-analytics schema [SESSION]      event-type inventory; flags what this version does not know
    session-analytics pricing               the price table used for estimates
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, locate
from .export import FORMATS, export_session, parse_formats
from .pricing import Pricing
from .util import fmt_duration, fmt_usd, local_str


def _common(p):
    p.add_argument("--claude-dir", help="Claude Code data dir (default: $CLAUDE_CONFIG_DIR or ~/.claude)")
    p.add_argument("--pricing", help="JSON file overriding/adding model prices (or $SESSION_ANALYTICS_PRICING)")


def build_parser():
    ap = argparse.ArgumentParser(prog="session-analytics", description="Export and analyze Claude Code sessions.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    e = sub.add_parser("export", help="export one session (default: the current one)")
    e.add_argument("session", nargs="?", default="current",
                   help="current (default) | latest | <session id or unique prefix> | <path to .jsonl>")
    e.add_argument("--current-session", help="id that 'current' means (the skill passes ${CLAUDE_SESSION_ID})")
    e.add_argument("--cwd", help="project directory used to find 'latest' (default: this directory)")
    e.add_argument("--out", help="output directory (default: ~/claude-session-exports/<project>/<start>_<id>/)")
    e.add_argument("--format", default="all", help=f"comma list of {', '.join(FORMATS)} (default: all)")
    e.add_argument("--full", action="store_true", help="include full prompts, commands and tool results")
    e.add_argument("--no-redact", action="store_true", help="do not mask secret-looking strings")
    e.add_argument("--own-only", action="store_true",
                   help="leave out history copied from an earlier session when this one was resumed/continued")
    e.add_argument("--checks", action="append", default=[], metavar="FILE",
                   help="skill checks to evaluate on each skill run (default: checks/<skill>.json in this repo)")
    e.add_argument("--source", action="append", default=[], metavar="DIR",
                   help="a skill's source directory or repo, to label runs with the git commit that ran")
    e.add_argument("--open", action="store_true", help="open the HTML dashboard when done")
    e.add_argument("--json", action="store_true", help="print a JSON result (paths, totals, insights) instead of Markdown")
    e.add_argument("--quiet", action="store_true", help="print only the output directory")
    _common(e)

    ls = sub.add_parser("list", help="list recent sessions")
    ls.add_argument("--project", help="project directory (default: this directory)")
    ls.add_argument("--all", action="store_true", help="every project")
    ls.add_argument("--limit", type=int, default=15)
    ls.add_argument("--json", action="store_true")
    _common(ls)

    r = sub.add_parser("rollup", help="aggregate analytics across many sessions")
    r.add_argument("--project", help="project directory (default: this directory)")
    r.add_argument("--all", action="store_true", help="every project")
    r.add_argument("--since", default="30d", help="7d, 24h, 2w, or a date (YYYY-MM-DD); default 30d")
    r.add_argument("--limit", type=int, default=500, help="at most this many sessions (newest first)")
    r.add_argument("--out", help="output directory (default: ~/claude-session-exports/_rollups/<scope>_<date>/)")
    r.add_argument("--format", default="all", help="comma list of json, md, html, csv (default: all)")
    r.add_argument("--no-redact", action="store_true")
    r.add_argument("--open", action="store_true")
    r.add_argument("--json", action="store_true")
    _common(r)

    sk = sub.add_parser("skill", help="every run of one skill across sessions, grouped by the version that ran")
    sk.add_argument("name", help="skill name, e.g. rde")
    sk.add_argument("--since", default="30d", help="7d, 24h, 2w, all, or a date (YYYY-MM-DD); default 30d")
    sk.add_argument("--project", help="only sessions of this project directory (default: every project)")
    sk.add_argument("--limit", type=int, default=1000, help="at most this many transcripts (newest first)")
    sk.add_argument("--checks", action="append", default=[], metavar="FILE",
                    help="checks to evaluate on each run (default: checks/<name>.json in this repo)")
    sk.add_argument("--source", action="append", default=[], metavar="DIR",
                    help="the skill's source directory or repo (default: found under ~/dev and friends)")
    sk.add_argument("--out", help="output directory (default: ~/claude-session-exports/_skills/...)")
    sk.add_argument("--format", default="all", help="comma list of json, md, html, csv (default: all)")
    sk.add_argument("--no-redact", action="store_true")
    sk.add_argument("--open", action="store_true")
    sk.add_argument("--json", action="store_true")
    _common(sk)

    cp = sub.add_parser("compare", help="compare two skill runs side by side (run ids like b3734789:1)")
    cp.add_argument("run_a")
    cp.add_argument("run_b")
    cp.add_argument("--checks", action="append", default=[], metavar="FILE")
    cp.add_argument("--source", action="append", default=[], metavar="DIR")
    cp.add_argument("--out", help="also write the comparison to this Markdown file")
    cp.add_argument("--no-redact", action="store_true")
    _common(cp)

    wh = sub.add_parser("warehouse", help="load every session into Postgres and/or ClickHouse, as tables and views")
    wh.add_argument("--since", default="all", help="all (default), 90d, 7d, or a date (YYYY-MM-DD)")
    wh.add_argument("--project", help="only sessions of this project directory (default: every project)")
    wh.add_argument("--out", help="where to write the CSVs and SQL (default: ~/claude-session-exports/_warehouse/<time>/)")
    wh.add_argument("--up", action="store_true", help="start the Postgres in docker-compose.yml first")
    wh.add_argument("--load", nargs="?", const="yes", choices=["yes", "auto"],
                    help="load the tables into the local Postgres (default: only write files); --load=auto only when "
                         "its container is running (the SessionEnd hook)")
    wh.add_argument("--clickhouse", nargs="?", const="yes", choices=["yes", "auto"],
                    help="replace this machine's rows in the shared ClickHouse: CLICKHOUSE_URL in "
                         "~/.config/convo-analysis/.env (--init-env); --clickhouse=auto skips it while that is empty")
    wh.add_argument("--clickhouse-forget", action="store_true",
                    help="take this machine's rows out of the ClickHouse warehouse (everyone else's stay)")
    wh.add_argument("--skills", help="ClickHouse gets the sessions that invoked one of these skills (comma separated; "
                                     "* for every session). Default: CLICKHOUSE_SKILLS in the env file, else rde")
    wh.add_argument("--session", action="append", metavar="TRANSCRIPT",
                    help="ClickHouse: update only this session (its main transcript .jsonl); repeatable")
    wh.add_argument("--session-queue", metavar="DIR",
                    help="ClickHouse: update only the sessions queued in DIR (one file per session, holding its "
                         "transcript path; the SessionEnd hook writes them), and clear what was handled")
    wh.add_argument("--rescope", action="store_true",
                    help="CLICKHOUSE_SKILLS changed: take out the shared sessions the new scope no longer covers (a "
                         "sync only reports them until then, so a typo there cannot delete history)")
    wh.add_argument("--init-env", action="store_true",
                    help="create ~/.config/convo-analysis/.env from .env.example, to fill in CLICKHOUSE_URL")
    wh.add_argument("--env-file", help="read CLICKHOUSE_URL from this file instead of ~/.config/convo-analysis/.env")
    wh.add_argument("--check", action="store_true",
                    help="recount every session from its raw transcript and compare with what was loaded "
                         "(alone: with the Postgres warehouse)")
    wh.add_argument("--container", default="convo-analysis-pg", help="the Postgres container to load through")
    wh.add_argument("--dsn", help="load with a local psql to this DSN instead of the container")
    wh.add_argument("--no-redact", action="store_true")
    wh.add_argument("--json", action="store_true")
    _common(wh)

    sc = sub.add_parser("schema", help="inventory event types; flag ones this version does not recognise")
    sc.add_argument("session", nargs="?", help="a session (default: every transcript)")
    sc.add_argument("--current-session")
    sc.add_argument("--json", action="store_true")
    _common(sc)

    pr = sub.add_parser("pricing", help="print the price table")
    pr.add_argument("--json", action="store_true")
    _common(pr)
    return ap


def _open(path):
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif os.name == "nt":
            os.startfile(path)  # noqa: S606 - opening a local file the user asked for
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError as exc:
        print(f"(could not open {path}: {exc})", file=sys.stderr)


def cmd_export(args):
    pricing = Pricing(args.pricing)
    try:
        formats = parse_formats(args.format)
        path = locate.resolve(args.session, cdir=args.claude_dir, cwd=args.cwd,
                              current=locate.current_session_id(args.current_session))
    except (ValueError, locate.SessionNotFound) as exc:
        print(f"session-analytics: {exc}", file=sys.stderr)
        return 2
    cur = locate.current_session_id(args.current_session)
    res = export_session(path, out_dir=args.out, formats=formats, full=args.full, redact=not args.no_redact,
                         pricing=pricing, current_id=cur, own_only=args.own_only, checks=args.checks,
                         skill_sources=args.source)
    if args.open and "dashboard" in res["paths"]:
        _open(res["paths"]["dashboard"])
    if args.quiet:
        print(res["out_dir"])
    elif args.json:
        a = res["analytics"]
        print(json.dumps({"out_dir": res["out_dir"], "paths": res["paths"], "session": {
            k: a["session"][k] for k in ("id", "title", "project_name", "start", "end", "live", "transcript")},
            "totals": a["totals"], "insights": a["insights"]}, indent=2, default=str))
    else:
        print(res["summary"])
    return 0


def cmd_list(args):
    from .rollup import quick_rows
    cdir = locate.claude_dir(args.claude_dir)
    project = None if args.all else (args.project or os.getcwd())
    files = locate.iter_transcripts(cdir, project=project)[: args.limit]
    if not files and project:
        print(f"No sessions for {project}; showing all projects.", file=sys.stderr)
        files = locate.iter_transcripts(cdir)[: args.limit]
    rows = quick_rows(files, Pricing(args.pricing))
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    print(f"{'started':<17} {'dur':>8} {'turns':>5} {'tools':>6} {'cost':>9}  {'id':<8}  title")
    for r in rows:
        print(f"{local_str(r['start_ms'], '%Y-%m-%d %H:%M'):<17} {fmt_duration(r['wall_ms']):>8} {r['turns']:>5} "
              f"{r['tool_calls']:>6} {fmt_usd(r['cost_usd']):>9}  {r['id'][:8]}  {r['title'][:70]}")
    return 0


def cmd_rollup(args):
    from .rollup import run_rollup
    try:
        formats = parse_formats(args.format)
    except ValueError as exc:
        print(f"session-analytics: {exc}", file=sys.stderr)
        return 2
    res = run_rollup(claude_dir=args.claude_dir, project=None if args.all else (args.project or os.getcwd()),
                     since=args.since, limit=args.limit, out_dir=args.out, formats=formats,
                     redact=not args.no_redact, pricing=Pricing(args.pricing))
    if args.open and "dashboard" in res["paths"]:
        _open(res["paths"]["dashboard"])
    if args.json:
        print(json.dumps({"out_dir": res["out_dir"], "paths": res["paths"], "totals": res["rollup"]["totals"]},
                         indent=2, default=str))
    else:
        print(res["summary"])
    return 0


def cmd_skill(args):
    from .skillreport import run_skill_report
    try:
        formats = parse_formats(args.format)
        res = run_skill_report(args.name, claude_dir=args.claude_dir, project=args.project, since=args.since,
                               limit=args.limit, out_dir=args.out, formats=formats, redact=not args.no_redact,
                               pricing=Pricing(args.pricing), check_files=args.checks, sources=args.source)
    except ValueError as exc:
        print(f"session-analytics: {exc}", file=sys.stderr)
        return 2
    if args.open and "dashboard" in res["paths"]:
        _open(res["paths"]["dashboard"])
    if args.json:
        rep = res["report"]
        print(json.dumps({"out_dir": res["out_dir"], "paths": res["paths"], "totals": rep["totals"],
                          "insights": rep["insights"],
                          "versions": [{k: v[k] for k in ("label", "runs", "median")} for v in rep["versions"]]},
                         indent=2, default=str))
    else:
        print(res["summary"])
    return 0


def cmd_compare(args):
    from .skillreport import compare_runs, load_run, render_compare
    pricing = Pricing(args.pricing)
    try:
        a = load_run(args.run_a, args.claude_dir, not args.no_redact, pricing, args.checks, args.source)
        b = load_run(args.run_b, args.claude_dir, not args.no_redact, pricing, args.checks, args.source)
    except (ValueError, locate.SessionNotFound) as exc:
        print(f"session-analytics: {exc}", file=sys.stderr)
        return 2
    text = render_compare(a, b, compare_runs(a, b))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(text)
    return 0


def _read_queue(qdir, log):
    """The hook's queue: {entry: (transcript path, mtime)}. Only entries named like a session id count; anything else
    there (a Finder .DS_Store) is ignored, and an entry that cannot be read, or names no transcript, is dropped."""
    out = {}
    d = Path(qdir)
    if not d.is_dir():
        return out
    for q in sorted(d.iterdir()):
        if not q.is_file() or not locate.UUID_RE.match(q.name):
            continue
        try:
            mtime = q.stat().st_mtime_ns
            t = q.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            t = ""
        if not t.endswith(".jsonl"):
            log(f"Dropped a queue entry that names no transcript: {q.name}")
            q.unlink(missing_ok=True)
            continue
        out[q] = (t, mtime)
    return out


def _done_with(queued, keep=()):
    """Delete the queue entries handled — but not one written again meanwhile: that session ended again, and its
    newer content still has to go out."""
    for q, (t, mtime) in queued.items():
        if Path(t).stem in keep:
            continue
        try:
            if q.stat().st_mtime_ns == mtime:
                q.unlink()
        except OSError:
            pass


def _claude_dir_of(transcript, default):
    """<claude dir>/projects/<project>/<session>.jsonl: the config directory a transcript belongs to, whose source
    its rows go under (sessions of a second CLAUDE_CONFIG_DIR share the hook's queue)."""
    p = Path(transcript)
    return p.parent.parent.parent if p.parent.parent.name == "projects" else Path(default)


def _print_clickhouse(ch):
    if ch.get("quiet"):
        if ch["read"]:
            print(f"\nClickHouse: nothing to share — the {ch['read']} session(s) read did not run {ch['skills']}, "
                  f"and none of them was shared; no request was made.")
        return
    what = []
    if ch["written"]:
        what.append(f"shared {len(ch['written'])} session(s) that ran {ch['skills']}")
    fresh_out = sorted(set(ch["removed"]) - set(ch["stale"]))
    if fresh_out:
        what.append(f"took out {len(fresh_out)} that no longer qualify")
    if ch["stale"]:
        what.append(f"took out {len(ch['stale'])} shared earlier that never ran {ch['skills']}")
    head = "Synced every session on this machine" if ch["mode"] == "all" else "Synced the ended session(s)"
    print(f"\n{head} to ClickHouse ({ch['target']}, as {ch['identity']}): {'; '.join(what) or 'nothing changed'}. "
          f"This source now holds {ch['held']} session(s); every other source's rows are untouched.")
    if ch["missing"]:
        print(f"No transcript for {len(ch['missing'])} queued session(s) (moved or deleted): left as they are.")
    if ch.get("withheld"):
        print(f"{len(ch['withheld'])} session(s) that ran {ch['skills']} were withdrawn earlier and stay out "
              f"(`--clickhouse --session <id>` shares one again).")
    if ch.get("scope_pending"):
        sp = ch["scope_pending"]
        print(f"CLICKHOUSE_SKILLS changed from {sp['from']} to {sp['to']}: {sp['would_take_out']} shared session(s) "
              f"the new scope does not cover stay until you run `warehouse --clickhouse --rescope`.")
    if ch.get("newer"):
        print(f"Left {len(ch['newer'])} shared view(s) and taxonomy table(s) as convo-analysis "
              f"{', '.join(sorted(set(ch['newer'].values())))} wrote them, newer than this machine's {__version__}: "
              f"update convo-analysis here (git pull, or update the session-export plugin).")


ACTIVE_MS = 5 * 60 * 1000  # a transcript written to within this is a session still going: the catch-up leaves it


def _forget_specs(specs, default_cdir, claude_dir):
    """--clickhouse-forget --session values as {config dir: [session ids]}. A session id is taken as it is, so one whose
    transcript Claude Code has deleted — kept shared by design — can still be taken out."""
    out = {}
    for spec in specs:
        try:
            t = locate.resolve(spec, cdir=claude_dir)
            out.setdefault(_claude_dir_of(t, default_cdir), []).append(t.stem)
        except locate.SessionNotFound:
            if not locate.UUID_RE.match(spec):
                raise
            out.setdefault(Path(default_cdir), []).append(spec)
    return out


def _sweep(target, source, cdir, queued_paths, started_ms, log):
    """The catch-up: transcripts of `cdir` changed since the last pass whose SessionEnd never came (the app was quit,
    the machine slept). Leaves out one already synced at its current state, and one still being written (its own end,
    or a later pass, picks it up). Returns (paths, {path: mtime}, the oldest mtime left for later or None)."""
    cache = clickhouse_cache(target, source)
    swept = cache.get("swept_ms")
    if not swept:
        return [], {}, None
    synced = cache.get("synced") or {}
    picked, mtimes, held_back = [], {}, None
    for p in locate.iter_transcripts(cdir):
        m = p.stat().st_mtime * 1000
        if m <= swept - 10 * 60 * 1000 or str(p) in queued_paths or m <= synced.get(p.stem, 0):
            continue
        if started_ms - m < ACTIVE_MS:
            held_back = m if held_back is None else min(held_back, m)
            continue
        picked.append(str(p))
        mtimes[str(p)] = m
    return picked, mtimes, held_back


def clickhouse_cache(target, source):
    from . import clickhouse
    return clickhouse.read_cache(target, source) or {}


def cmd_warehouse(args):
    from . import clickhouse, reconcile, warehouse
    log = (lambda *_: None) if args.json else (lambda m: print(m, file=sys.stderr))
    started_ms = time.time() * 1000
    if args.init_env:
        path, created = clickhouse.init_env()
        print(f"{'Created' if created else 'Already there:'} {path} — fill in CLICKHOUSE_URL (its comments say how).")
        return 0
    default_cdir = Path(locate.claude_dir(args.claude_dir))
    target, unusable = None, None
    if args.clickhouse or args.clickhouse_forget:
        conf, env_path = clickhouse.settings(args.env_file)
        if conf["CLICKHOUSE_URL"] or args.clickhouse == "yes" or args.clickhouse_forget:
            try:
                target = clickhouse.target_from_settings(args.env_file)
            except clickhouse.ClickHouseError as exc:
                # set but unusable: say so, keep the queue, and still reload a running Postgres (exit 1 at the end)
                print(f"session-analytics: warehouse --clickhouse: {exc}", file=sys.stderr)
                if args.clickhouse != "auto":
                    return 1
                unusable = str(exc)
        if target is not None and env_path == clickhouse.checkout_env_file():
            log(f"note: the connection is in {env_path}; move it to {clickhouse.config_dir() / '.env'}, where an "
                f"update of this checkout or plugin can't take it with it")
    skills = clickhouse.skills_setting(args.env_file, args.skills)
    if args.clickhouse_forget:
        try:
            by_dir = _forget_specs(args.session, default_cdir, args.claude_dir) if args.session else {default_cdir: None}
        except locate.SessionNotFound as exc:
            print(f"session-analytics: warehouse --clickhouse-forget: {exc}", file=sys.stderr)
            return 1
        for cdir, ids in by_dir.items():
            ident = clickhouse.identity(cdir, args.env_file)
            try:
                gone, absent = clickhouse.forget(target, ident, ids)
            except clickhouse.ClickHouseError as exc:
                print(f"session-analytics: warehouse --clickhouse-forget: {exc}", file=sys.stderr)
                return 1
            print(f"Took {len(gone)} session(s) of {ident!r} out of {target!r}"
                  + (f"; {len(absent)} named were not shared ({', '.join(absent)})" if absent else "")
                  + ". Every other source's rows stay. They are withdrawn: no later sync shares them again — "
                    "`--clickhouse --session <id>` shares one again. The SessionEnd hook still shares new sessions "
                    f"that run {','.join(skills)}; empty CLICKHOUSE_URL to stop that.")
        return 0
    explicit = []
    for spec in args.session or ():
        try:
            explicit.append(str(locate.resolve(spec, cdir=args.claude_dir)))
        except locate.SessionNotFound as exc:
            print(f"session-analytics: warehouse --session {spec}: {exc}", file=sys.stderr)
            return 1
    if explicit and target is not None:  # named to share: no longer withdrawn
        for t in explicit:
            clickhouse.reshare(target, clickhouse.source_id(_claude_dir_of(t, default_cdir)), [Path(t).stem])
    queued = _read_queue(args.session_queue, log) if args.session_queue else {}
    sessions = None
    if args.session or args.session_queue:
        sessions = list(dict.fromkeys(explicit + [t for t, _ in queued.values()]))
    do_load = args.load == "yes" or (args.load == "auto" and warehouse.running(args.container))
    if (args.load or args.clickhouse) and not do_load and target is None and not args.check:
        if unusable:
            return 1  # the queue stays: the sessions go out once CLICKHOUSE_URL is fixed
        _done_with(queued)  # nowhere to put them: a full load (make clickhouse) covers history later
        log("Nothing to load: no local Postgres running and no CLICKHOUSE_URL set.")  # the hook, before any parse
        return 0
    swept_paths, swept_mtimes, held_back = [], {}, None
    if args.session_queue and target is not None:
        swept_paths, swept_mtimes, held_back = _sweep(target, clickhouse.source_id(default_cdir), default_cdir,
                                                      set(sessions), started_ms, log)
        sessions = list(dict.fromkeys(sessions + swept_paths))
    source0 = clickhouse.source_id(default_cdir) if target is not None else None
    if args.session_queue and not sessions and not do_load and not args.check:
        log("Nothing queued.")
        if target is not None:
            clickhouse.write_cache(target, source0, swept_ms=min(started_ms, held_back or started_ms))
        return 0
    if args.check and not do_load and target is None:
        try:
            res = reconcile.check(warehouse.psql_command(container=args.container, dsn=args.dsn), args.claude_dir)
        except (RuntimeError, OSError) as exc:
            print(f"session-analytics: warehouse --check: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(res, indent=1, default=str) if args.json else reconcile.render(res))
        return 0 if res["ok"] else 1
    # One pass per Claude config directory the sessions belong to (each is its own source); Postgres, and the
    # files, with the first.
    groups = {default_cdir: None if sessions is None else []}
    for t in sessions or ():
        groups.setdefault(_claude_dir_of(t, default_cdir), []).append(t)
    mtimes = {}
    for t in sessions or ():
        try:
            mtimes[t] = Path(t).stat().st_mtime * 1000
        except OSError:
            pass
    runs = []
    for n, (cdir, group) in enumerate(groups.items()):
        if n and target is None:
            continue  # other config directories only matter to ClickHouse
        if group == [] and not (n == 0 and (do_load or args.check or len(groups) == 1)):
            continue  # no session of this config directory queued, and no Postgres load to do with it
        ident = clickhouse.identity(cdir, args.env_file) if target is not None else None
        try:
            res = warehouse.run_warehouse(str(cdir), args.project, args.since, args.out, do_load=do_load and n == 0,
                                          start=args.up and n == 0, container=args.container, dsn=args.dsn,
                                          redact=not args.no_redact, pricing=Pricing(args.pricing), log=log,
                                          clickhouse_target=target, clickhouse_identity=ident, sessions=group,
                                          skills=skills, rescope=args.rescope, write_files=n == 0)
        except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            print(f"session-analytics: warehouse: {detail}".strip(), file=sys.stderr)
            return 1
        runs.append((cdir, group, ident, res))
    for cdir, group, ident, res in runs:
        ch = res["clickhouse"]
        if unusable or res["errors"].get("clickhouse"):
            continue  # nothing handled: every entry stays queued for the next pass
        unread = set((ch or {}).get("unread") or ())
        mine = {q: v for q, v in queued.items() if group is not None and v[0] in group}
        _done_with(mine, keep=unread)
        queued_unread = sorted(unread & {Path(v[0]).stem for v in mine.values()})
        swept_unread = sorted(unread - set(queued_unread))
        if queued_unread:
            log(f"Could not read {len(queued_unread)} queued transcript(s); left queued, their shared rows untouched: "
                + ", ".join(queued_unread))
        if swept_unread:
            log(f"Could not read {len(swept_unread)} transcript(s) the catch-up found; the next pass tries again: "
                + ", ".join(swept_unread))
        if ch and not ch.get("quiet"):
            # what was synced, at the state it was read in: the catch-up leaves it alone until it changes again
            by_stem = {Path(t).stem: m for t, m in mtimes.items()}
            synced = dict(clickhouse_cache(target, ident.source).get("synced") or {})
            synced.update({sid: by_stem[sid] for sid in ch.get("written") or () if sid in by_stem})
            for sid in ch.get("removed") or ():
                synced.pop(sid, None)
            clickhouse.write_cache(target, ident.source, synced=synced)
        if target is not None and args.session_queue and cdir == default_cdir:
            oldest_unread = min((swept_mtimes.get(t) for t in swept_mtimes if Path(t).stem in swept_unread),
                                default=None)
            clickhouse.write_cache(target, ident.source, swept_ms=min(
                x for x in (started_ms, held_back, oldest_unread) if x is not None))
    checks = {}
    first = runs[0][3]
    if args.check and first["loaded"]:
        checks["Postgres"] = reconcile.check(warehouse.psql_command(container=args.container, dsn=args.dsn),
                                             args.claude_dir)
    if args.check and first["clickhouse"] and not first["clickhouse"].get("quiet"):
        try:
            checks["ClickHouse"] = reconcile.check((clickhouse.Client(target), runs[0][2].source), args.claude_dir,
                                                   only=set(first["clickhouse"]["written"]))
        except clickhouse.ClickHouseError as exc:
            first["errors"]["clickhouse check"] = str(exc)
    ok = not unusable and all(not r[3]["errors"] for r in runs) and all(c["ok"] for c in checks.values())
    if args.json:
        print(json.dumps(dict(first, runs=[r[3]["clickhouse"] for r in runs], checks=checks), indent=1, default=str))
        return 0 if ok else 1
    res = first
    c, conn = res["counts"], res["connection"]
    print(f"# Session warehouse\n\n{res['meta']['transcripts']} transcripts → {c['sessions']:,} sessions, "
          f"{c['turns']:,} turns, {c['api_requests']:,} API requests, {c['tool_calls']:,} tool calls, "
          f"{c['cli_calls']:,} CLI calls, {c['skill_runs']:,} skill runs, {c['questions']:,} questions.")
    if res["meta"]["failed"]:
        print(f"\n{len(res['meta']['failed'])} transcripts could not be read: "
              + ", ".join(f["transcript"] for f in res["meta"]["failed"][:5]))
    if res["loaded"]:
        print(f"\nLoaded into Postgres: postgresql://{conn['user']}@{conn['host']}:{conn['port']}/{conn['database']} "
              f"(from a Metabase in Docker: {conn['from_docker']['host']}:{conn['port']}). Tables: "
              + ", ".join(k for k, n in c.items()) + "; views: " + ", ".join(warehouse.VIEW_COMMENTS) + ".")
    for _cdir, _group, _ident, r in runs:
        if r["clickhouse"]:
            _print_clickhouse(r["clickhouse"])
    if args.out or res["out_dir"]:
        print(f"\nFiles: {res['out_dir']} (schema.sql, views.sql, one CSV per table)")
    for _cdir, _group, _ident, r in runs:
        for name, err in r["errors"].items():
            print(f"\n{name} failed: {err}", file=sys.stderr)
    for name, chk in checks.items():
        print("\n" + reconcile.render(chk, title=f"{name} check"))
    return 0 if ok else 1


def cmd_schema(args):
    from .schema_scan import scan
    cdir = locate.claude_dir(args.claude_dir)
    if args.session:
        files = [locate.resolve(args.session, cdir=args.claude_dir,
                                current=locate.current_session_id(args.current_session))]
    else:
        files = locate.iter_transcripts(cdir)
    report = scan(files)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0
    print(f"{report['sessions']} sessions, {report['files']} files, {report['lines']:,} lines "
          f"({report['bad_lines']} unparseable)\n")
    for key in ("event_types", "system_subtypes", "attachment_types", "tool_names"):
        print(f"## {key.replace('_', ' ')}")
        for name, n in list(report[key].items())[:60]:
            print(f"  {n:>8}  {name}")
        print()
    unknown = report["unknown"]
    print("## not recognised by this version")
    if not any(unknown.values()):
        print("  none")
    for k, v in unknown.items():
        for name, n in v.items():
            print(f"  {n:>8}  {k}: {name}")
    return 0


def cmd_pricing(args):
    d = Pricing(args.pricing).describe()
    if args.json:
        print(json.dumps(d, indent=2))
        return 0
    print(f"{d['unit']}; cache writes {d['cache_write_5m_multiplier']}x (5m) / {d['cache_write_1h_multiplier']}x (1h) "
          f"input; web search ${d['web_search_usd_per_request']}/request\n")
    print(f"{'model prefix':<22} {'input':>7} {'output':>7} {'c.read':>7} {'w.5m':>7} {'w.1h':>7}  source")
    for r in d["rates"]:
        print(f"{r['model_prefix']:<22} {r['input']:>7} {r['output']:>7} {r['cache_read']:>7} "
              f"{r['cache_write_5m']:>7} {r['cache_write_1h']:>7}  {r['source']}")
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not args.cmd:
        args = build_parser().parse_args(["export"] + list(argv or sys.argv[1:]))
    started = time.time()
    code = {"export": cmd_export, "list": cmd_list, "rollup": cmd_rollup, "skill": cmd_skill, "compare": cmd_compare,
            "schema": cmd_schema, "pricing": cmd_pricing, "warehouse": cmd_warehouse}[args.cmd](args)
    if os.environ.get("SESSION_ANALYTICS_TIMING"):
        print(f"({time.time() - started:.2f}s)", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
