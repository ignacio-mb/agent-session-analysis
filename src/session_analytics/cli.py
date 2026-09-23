"""session-analytics: export and analyze Claude Code sessions.

    session-analytics export [SESSION]      current | latest | <id or prefix> | <path.jsonl>
    session-analytics list                  recent sessions for this project (or --all)
    session-analytics rollup --since 7d     aggregate many sessions
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
                         pricing=pricing, current_id=cur, own_only=args.own_only)
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
    code = {"export": cmd_export, "list": cmd_list, "rollup": cmd_rollup, "schema": cmd_schema,
            "pricing": cmd_pricing}[args.cmd](args)
    if os.environ.get("SESSION_ANALYTICS_TIMING"):
        print(f"({time.time() - started:.2f}s)", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
