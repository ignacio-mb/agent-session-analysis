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

    wh = sub.add_parser("warehouse", help="load every session into a local Postgres, as tables and views")
    wh.add_argument("--since", default="all", help="all (default), 90d, 7d, or a date (YYYY-MM-DD)")
    wh.add_argument("--project", help="only sessions of this project directory (default: every project)")
    wh.add_argument("--out", help="where to write the CSVs and SQL (default: ~/claude-session-exports/_warehouse/<time>/)")
    wh.add_argument("--up", action="store_true", help="start the Postgres in docker-compose.yml first")
    wh.add_argument("--load", action="store_true", help="load the tables into Postgres (default: only write files)")
    wh.add_argument("--check", action="store_true",
                    help="recount every session from its raw transcript and compare with the warehouse (after --load)")
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


def cmd_warehouse(args):
    from . import reconcile, warehouse
    log = (lambda *_: None) if args.json else (lambda m: print(m, file=sys.stderr))
    if args.check and not args.load:
        try:
            res = reconcile.check(warehouse.psql_command(container=args.container, dsn=args.dsn), args.claude_dir)
        except (RuntimeError, OSError) as exc:
            print(f"session-analytics: warehouse --check: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(res, indent=1, default=str) if args.json else reconcile.render(res))
        return 0 if res["ok"] else 1
    try:
        res = warehouse.run_warehouse(args.claude_dir, args.project, args.since, args.out, do_load=args.load,
                                      start=args.up, container=args.container, dsn=args.dsn,
                                      redact=not args.no_redact, pricing=Pricing(args.pricing), log=log)
    except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        print(f"session-analytics: warehouse: {detail}".strip(), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(res, indent=1, default=str))
        return 0
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
    print(f"\nFiles: {res['out_dir']} (schema.sql, views.sql, one CSV per table)")
    if args.check and res["loaded"]:
        chk = reconcile.check(warehouse.psql_command(container=args.container, dsn=args.dsn), args.claude_dir)
        print("\n" + reconcile.render(chk))
        return 0 if chk["ok"] else 1
    return 0


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
