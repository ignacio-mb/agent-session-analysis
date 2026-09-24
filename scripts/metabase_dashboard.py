"""Build the "Claude Code sessions" Metabase dashboard on the session warehouse (see warehouse.py).

    python3 scripts/metabase_dashboard.py --test
    python3 scripts/metabase_dashboard.py --build --profile <mb profile> --database <id>

--test runs every card's SQL against the local warehouse (docker exec psql). --build creates a collection, the
cards (native SQL on the warehouse's views) and a dashboard with four tabs — Overview, Skill versions, Interview,
Skill files & CLI — and a Skill filter. It needs the Metabase CLI

USAGE mb <command> [options]

COMMANDS

                 auth    Authenticate against a Metabase instance
         db, database    Inspect and sync Metabase databases
                table    Manage Metabase tables
                field    Manage Metabase fields
               upload    Upload CSV files into Metabase
  content-translation    Download or replace the content translation dictionary
                 card    Manage Metabase cards (questions, models, metrics)
            dashboard    Manage Metabase dashboards
         subscription    Manage Metabase dashboard subscriptions (scheduled dashboard delivery)
                alert    Manage Metabase question alerts (scheduled card delivery on a send condition)
           collection    Manage Metabase collections
              library    Curate the Metabase Library — publish trusted tables to its Data collection
             document    Manage Metabase documents
            transform    Manage Metabase transforms
        transform-job    Manage Metabase transform jobs
        transform-tag    Manage Metabase transform tags
       transform-test    Manage transform tests — fixtures and expectations run against temp tables
              setting    Inspect and update Metabase settings
               search    Search Metabase content (cards, dashboards, collections, …)
             git-sync    Sync Metabase content with a git remote
                setup    Complete the initial Metabase setup wizard with a default user
              snippet    Manage Metabase native query snippets
              segment    Manage Metabase segments
              measure    Manage Metabase measures
             timeline    Manage Metabase timelines (event annotations for time-series charts)
       timeline-event    Manage events on Metabase timelines (list them with `mb timeline events`)
                  eid    Translate Metabase entity ids (string EIDs) to numeric ids
                query    Run an ad-hoc MBQL or native query
                 uuid    Mint random UUID v4 strings
              upgrade    Upgrade the Metabase CLI itself to the latest published release
               skills    Read CLI-bundled skills — always consult the matching skill before acting on a task; they are the source of truth for every workflow.

Use mb <command> --help for more information about a command.

AGENT SKILLS

  mb skills get core — auth, conventions, and per-resource footguns
  mb skills get data-workflow — guided end-to-end: raw data → clean tables → metrics → answers → dashboards
  mb skills list — every bundled skill

First time? Run `mb auth login` to connect to a Metabase instance.

Machine-readable command index: mb --help --json CLI logged in to the target Metabase (--profile) and the
warehouse already added there as a database (--database: its id in {"returned":1,"offset":0,"total":1,"has_more":false,"next_offset":null,"data":[{"id":1,"name":"Sample Database","engine":"postgres"}]}). Nothing is created without
--build; pass the profile explicitly so nothing lands on a Metabase you did not mean.
"""
import argparse
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

PROFILE = None
DB = None
WORK = Path(tempfile.mkdtemp(prefix="claude-sessions-dashboard-"))

SKILL = "{{skill}}"
BUILD_VERSION_ORDER = "ORDER BY version_date NULLS LAST"

OUTCOME_GROUP = """CASE WHEN channel <> 'ask' THEN 'in prose'
            WHEN outcome = 'recommended' THEN 'recommended option'
            WHEN outcome IN ('typed', 'typed + picked') THEN 'typed an answer'
            WHEN outcome IN ('other option', 'picked') THEN 'another option'
            WHEN outcome = 'no preference' THEN 'no preference'
            ELSE 'declined or unanswered' END"""

# (key, tab, name, display, sql, visualization_settings, uses_skill, (col, row, w, h))
CARDS = [
    # ---------------------------------------------------------------- Overview
    ("sessions30", "Overview", "Sessions, last 30 days", "scalar",
     "SELECT count(*) AS sessions FROM sessions WHERE start_at >= now() - interval '30 days'",
     {"scalar.field": "sessions"}, False, (0, 0, 6, 3)),
    ("cost30", "Overview", "Estimated cost, last 30 days", "scalar",
     "SELECT round(sum(cost_usd), 2) AS cost_usd FROM sessions WHERE start_at >= now() - interval '30 days'",
     {"scalar.field": "cost_usd", "column_settings": {'["name","cost_usd"]': {"number_style": "currency", "currency": "USD"}}},
     False, (6, 0, 6, 3)),
    ("hours30", "Overview", "Active hours, last 30 days", "scalar",
     "SELECT round(sum(active_ms) / 3600000.0, 1) AS active_hours FROM sessions WHERE start_at >= now() - interval '30 days'",
     {"scalar.field": "active_hours"}, False, (12, 0, 6, 3)),
    ("err30", "Overview", "Tool calls that failed, last 30 days", "scalar",
     "SELECT sum(tool_errors)::numeric / nullif(sum(tool_calls), 0) AS error_rate FROM sessions "
     "WHERE start_at >= now() - interval '30 days'",
     {"scalar.field": "error_rate", "column_settings": {'["name","error_rate"]': {"number_style": "percent", "decimals": 1}}},
     False, (18, 0, 6, 3)),
    ("costday", "Overview", "Estimated cost per day", "bar",
     "SELECT day, cost_usd FROM v_daily WHERE day >= current_date - 60 ORDER BY day",
     {"graph.dimensions": ["day"], "graph.metrics": ["cost_usd"],
      "column_settings": {'["name","cost_usd"]': {"number_style": "currency", "currency": "USD"}}}, False, (0, 3, 12, 7)),
    ("costproject", "Overview", "Cost by project, last 30 days", "row",
     "SELECT project, round(sum(cost_usd), 2) AS cost_usd FROM sessions WHERE start_at >= now() - interval '30 days' "
     "GROUP BY project ORDER BY cost_usd DESC LIMIT 12",
     {"graph.dimensions": ["project"], "graph.metrics": ["cost_usd"],
      "column_settings": {'["name","cost_usd"]': {"number_style": "currency", "currency": "USD"}}}, False, (12, 3, 12, 7)),
    ("tools", "Overview", "Tools: calls, failures and speed", "table",
     "SELECT tool, calls, errors, error_rate, denied, round(p50_ms::numeric) AS p50_ms FROM v_tools ORDER BY calls DESC LIMIT 25",
     {"column_settings": {'["name","error_rate"]': {"number_style": "percent", "decimals": 1}}}, False, (0, 10, 12, 9)),
    ("models", "Overview", "Requests and cost by model", "table",
     "SELECT model, requests, sessions, cost_usd, round(p50_latency_ms::numeric) AS p50_latency_ms FROM v_models "
     "ORDER BY cost_usd DESC NULLS LAST",
     {"column_settings": {'["name","cost_usd"]': {"number_style": "currency", "currency": "USD"}}}, False, (12, 10, 12, 9)),
    # ---------------------------------------------------------------- Skill versions
    ("versions", "Skill versions", "Versions compared", "table",
     f"SELECT version, subject, version_date, runs, median_cost_usd, median_tool_calls, median_tool_errors, "
     f"round(median_active_minutes::numeric, 1) AS median_active_minutes, median_help_lookups, questions_asked, "
     f"prose_questions, recommended_rate, typed_answers, median_docs_read, checks_failed "
     f"FROM v_skill_versions WHERE skill = {SKILL} {BUILD_VERSION_ORDER}",
     {"column_settings": {'["name","median_cost_usd"]': {"number_style": "currency", "currency": "USD"},
                          '["name","recommended_rate"]': {"number_style": "percent", "decimals": 0}}}, True, (0, 0, 24, 6)),
    ("vcost", "Skill versions", "Median cost per run, by version", "bar",
     f"SELECT version, median_cost_usd FROM v_skill_versions WHERE skill = {SKILL} {BUILD_VERSION_ORDER}",
     {"graph.dimensions": ["version"], "graph.metrics": ["median_cost_usd"],
      "column_settings": {'["name","median_cost_usd"]': {"number_style": "currency", "currency": "USD"}}}, True, (0, 6, 12, 7)),
    ("vtools", "Skill versions", "Median failed tool calls and --help lookups per run, by version", "bar",
     f"SELECT version, median_tool_errors, median_help_lookups FROM v_skill_versions "
     f"WHERE skill = {SKILL} {BUILD_VERSION_ORDER}",
     {"graph.dimensions": ["version"], "graph.metrics": ["median_tool_errors", "median_help_lookups"],
      "graph.y_axis.auto_split": False}, True, (12, 6, 12, 7)),
    ("checkrates", "Skill versions", "Checks: the latest version against the one before", "table",
     f"WITH v AS (SELECT version, row_number() OVER (ORDER BY version_date DESC NULLS LAST) AS rn "
     f"FROM v_skill_versions WHERE skill = {SKILL}) "
     f"SELECT c.check_id, max(c.version) FILTER (WHERE v.rn = 2) AS previous_version, "
     f"max(c.pass_rate) FILTER (WHERE v.rn = 2) AS previous_pass_rate, "
     f"max(c.passed || '/' || (c.passed + c.failed)) FILTER (WHERE v.rn = 2) AS previous_runs, "
     f"max(c.version) FILTER (WHERE v.rn = 1) AS latest_version, max(c.pass_rate) FILTER (WHERE v.rn = 1) AS latest_pass_rate, "
     f"max(c.passed || '/' || (c.passed + c.failed)) FILTER (WHERE v.rn = 1) AS latest_runs, "
     f"max(c.pass_rate) FILTER (WHERE v.rn = 1) - max(c.pass_rate) FILTER (WHERE v.rn = 2) AS change, "
     f"max(c.description) AS description FROM v_check_rates c JOIN v USING (version) "
     f"WHERE c.skill = {SKILL} AND v.rn <= 2 GROUP BY c.check_id "
     f"ORDER BY latest_pass_rate NULLS LAST, change NULLS LAST, c.check_id",
     {"column_settings": {'["name","previous_pass_rate"]': {"number_style": "percent", "decimals": 0},
                          '["name","latest_pass_rate"]': {"number_style": "percent", "decimals": 0},
                          '["name","change"]': {"number_style": "percent", "decimals": 0}}}, True, (0, 13, 24, 11)),
    ("failing", "Skill versions", "Checks failing on the latest version", "table",
     f"WITH latest AS (SELECT version FROM v_skill_versions WHERE skill = {SKILL} ORDER BY version_date DESC NULLS LAST LIMIT 1) "
     f"SELECT c.check_id, c.description, c.passed, c.failed, c.pass_rate FROM v_check_rates c JOIN latest USING (version) "
     f"WHERE c.skill = {SKILL} AND c.failed > 0 ORDER BY c.pass_rate, c.check_id",
     {"column_settings": {'["name","pass_rate"]': {"number_style": "percent", "decimals": 0}}}, True, (0, 24, 24, 6)),
    # ---------------------------------------------------------------- Interview
    ("qasked", "Interview", "Questions asked (AskUserQuestion)", "scalar",
     f"SELECT count(*) AS asked FROM questions WHERE skill = {SKILL} AND channel = 'ask'",
     {"scalar.field": "asked"}, True, (0, 0, 6, 3)),
    ("qrate", "Interview", "Recommended option taken", "scalar",
     f"SELECT count(*) FILTER (WHERE outcome = 'recommended')::numeric / nullif(count(*) FILTER (WHERE channel = 'ask' "
     f"AND recommended_label IS NOT NULL AND NOT multi AND outcome NOT IN ('declined', 'unanswered', 'interrupted', 'error')), 0) "
     f"AS recommended_rate FROM questions WHERE skill = {SKILL}",
     {"scalar.field": "recommended_rate", "column_settings": {'["name","recommended_rate"]': {"number_style": "percent", "decimals": 0}}},
     True, (6, 0, 6, 3)),
    ("qprose", "Interview", "Asked in prose instead", "scalar",
     f"SELECT count(*) AS in_prose FROM questions WHERE skill = {SKILL} AND channel <> 'ask'",
     {"scalar.field": "in_prose"}, True, (12, 0, 6, 3)),
    ("qwait", "Interview", "Median wait for an answer (seconds)", "scalar",
     f"SELECT round((percentile_cont(0.5) WITHIN GROUP (ORDER BY wait_ms) / 1000.0)::numeric) AS median_wait_s "
     f"FROM questions WHERE skill = {SKILL} AND channel = 'ask' AND outcome NOT IN ('unanswered', 'declined')",
     {"scalar.field": "median_wait_s"}, True, (18, 0, 6, 3)),
    ("qtopics", "Interview", "What came back, by topic", "row",
     f"SELECT topic_label, {OUTCOME_GROUP} AS outcome, count(*) AS questions FROM questions WHERE skill = {SKILL} "
     f"GROUP BY 1, 2 ORDER BY 1",
     {"graph.dimensions": ["topic_label", "outcome"], "graph.metrics": ["questions"], "stackable.stack_type": "stacked"},
     True, (0, 3, 12, 10)),
    ("qchannel", "Interview", "Questions per version: AskUserQuestion or prose", "bar",
     f"SELECT q.version, CASE WHEN q.channel = 'ask' THEN 'AskUserQuestion' ELSE 'in prose' END AS channel, count(*) AS questions "
     f"FROM questions q JOIN (SELECT version, version_date FROM v_skill_versions WHERE skill = {SKILL}) v USING (version) "
     f"WHERE q.skill = {SKILL} GROUP BY 1, 2 ORDER BY min(v.version_date), 2",
     {"graph.dimensions": ["version", "channel"], "graph.metrics": ["questions"], "stackable.stack_type": "stacked"},
     True, (12, 3, 12, 10)),
    ("qtopictable", "Interview", "Topics by version", "table",
     f"SELECT version, topic_label, asked, in_prose, runs, recommended_taken, recommended_offered, typed, came_back_empty, "
     f"asked_again, round(median_wait_s::numeric) AS median_wait_s FROM v_question_topics WHERE skill = {SKILL} "
     f"ORDER BY version, asked DESC", {}, True, (0, 13, 24, 9)),
    ("qtyped", "Interview", "Where the options fell short: answers typed instead of picked", "table",
     f"SELECT asked_at, version, topic_label, question, options_offered, typed FROM v_typed_answers WHERE skill = {SKILL} "
     f"ORDER BY asked_at DESC", {}, True, (0, 22, 14, 7)),
    ("qflags", "Interview", "Questions against the skill's rules", "row",
     f"SELECT flag, sum(questions) AS questions FROM v_question_flags WHERE skill = {SKILL} GROUP BY flag ORDER BY 2 DESC",
     {"graph.dimensions": ["flag"], "graph.metrics": ["questions"]}, True, (14, 22, 10, 7)),
    ("qall", "Interview", "Every question", "table",
     f"SELECT asked_at, version, run_id, topic_label, header, question, outcome, "
     f"coalesce(typed, answer, reply, feedback) AS answer, round(wait_ms / 1000.0) AS wait_s, flags "
     f"FROM questions WHERE skill = {SKILL} ORDER BY asked_at DESC", {}, True, (0, 29, 24, 12)),
    # ---------------------------------------------------------------- Skill files & CLI
    ("files", "Skill files & CLI", "The skill's files: runs shown each one, by version", "table",
     f"SELECT path, version, runs_shown, runs, whole, in_part, median_coverage, median_order FROM v_skill_files "
     f"WHERE skill = {SKILL} AND owner = {SKILL} ORDER BY path, version",
     {"column_settings": {'["name","median_coverage"]': {"number_style": "percent", "decimals": 0}}}, True, (0, 0, 12, 12)),
    ("clidocs", "Skill files & CLI", "Other docs the runs read (bundled CLI skills)", "table",
     f"SELECT owner || ':' || path AS doc, version, runs_shown, runs, whole, in_part, median_coverage FROM v_skill_files "
     f"WHERE skill = {SKILL} AND owner <> {SKILL} ORDER BY runs_shown DESC, doc",
     {"column_settings": {'["name","median_coverage"]': {"number_style": "percent", "decimals": 0}}}, True, (12, 0, 12, 12)),
    ("cli", "Skill files & CLI", "CLI commands in the skill's runs: uses, failures, --help", "table",
     f"SELECT c.signature, count(*) AS uses, count(*) FILTER (WHERE c.status = 'error') AS failed, "
     f"round(count(*) FILTER (WHERE c.status = 'error')::numeric / count(*), 3) AS failure_rate, "
     f"count(*) FILTER (WHERE c.is_help) AS help_lookups, count(DISTINCT c.run_id) AS runs "
     f"FROM cli_calls c JOIN skill_runs r USING (run_id) WHERE r.skill = {SKILL} AND c.signature LIKE '% %' "
     f"GROUP BY c.signature HAVING count(*) >= 2 ORDER BY uses DESC LIMIT 40",
     {"column_settings": {'["name","failure_rate"]': {"number_style": "percent", "decimals": 1}}}, True, (0, 12, 12, 12)),
    ("toolerrors", "Skill files & CLI", "Failed tool calls in the skill's runs", "table",
     f"SELECT t.at, r.version, t.run_id, t.tool, t.program, t.error_category, t.input, left(t.error, 300) AS error "
     f"FROM tool_calls t JOIN skill_runs r USING (run_id) WHERE r.skill = {SKILL} AND t.status = 'error' "
     f"ORDER BY t.at DESC LIMIT 200", {}, True, (12, 12, 12, 12)),
]
TABS = ["Overview", "Skill versions", "Interview", "Skill files & CLI"]


def mb(*args, body=None):
    cmd = ["mb", "--profile", PROFILE, *args, "--json"]
    if body is not None:
        f = WORK / f"body-{uuid.uuid4().hex[:8]}.json"
        f.write_text(json.dumps(body))
        cmd += ["--file", str(f)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise SystemExit(f"mb {' '.join(args)} failed:\n{res.stderr or res.stdout}")
    return json.loads(res.stdout)


def psql(sql):
    res = subprocess.run(["docker", "exec", "-i", "convo-analysis-pg", "psql", "-U", "convo", "-d", "claude_sessions",
                          "-v", "ON_ERROR_STOP=1", "-A", "-F", "\t", "-c", sql], capture_output=True, text=True)
    return res.returncode, (res.stdout if res.returncode == 0 else res.stderr).strip()


def test():
    bad = 0
    for key, _tab, name, _display, sql, _vis, _uses_skill, _ in CARDS:
        code, out = psql(sql.replace(SKILL, "'rde'"))
        lines = out.splitlines()
        rows = max(0, len(lines) - 2) if code == 0 else 0
        status = "ok " if code == 0 and rows else ("EMPTY" if code == 0 else "FAIL")
        bad += status != "ok "
        print(f"{status} {key:<12} {rows:>4} rows  {name}")
        if code != 0:
            print("      ", out[:400])
        elif rows:
            print("      ", lines[0][:150], "|", lines[1][:150])
    return bad


def card_body(name, display, sql, vis, uses_skill, collection_id):
    tags = {}
    if uses_skill:
        tags["skill"] = {"id": str(uuid.uuid4()), "name": "skill", "display-name": "Skill", "type": "text",
                         "required": True, "default": "rde"}
    return {"name": name, "display": display, "visualization_settings": vis, "collection_id": collection_id,
            "dataset_query": {"lib/type": "mbql/query", "database": DB,
                              "stages": [{"lib/type": "mbql.stage/native", "native": sql, "template-tags": tags}]}}


def build():
    skills = [line for line in psql("SELECT DISTINCT skill FROM skill_runs ORDER BY 1")[1].splitlines()[1:-1] if line]
    col = mb("collection", "create", body={"name": "Claude Code sessions",
                                          "description": "Session analytics from the convo-analysis warehouse (Postgres)."})
    col_id = col.get("id") or col["data"]["id"]
    ids = {}
    for key, _tab, name, display, sql, vis, uses_skill, _ in CARDS:
        c = mb("card", "create", body=card_body(name, display, sql, vis, uses_skill, col_id))
        ids[key] = c.get("id") or c["data"]["id"]
        print("card", ids[key], name)
    tabs = [{"id": -(i + 1), "name": t, "position": i} for i, t in enumerate(TABS)]
    tab_id = {t: -(i + 1) for i, t in enumerate(TABS)}
    dashcards = []
    for n, (key, tab, _name, _display, _sql, _vis, uses_skill, (x, y, w, h)) in enumerate(CARDS, 1):
        dc = {"id": -n, "card_id": ids[key], "dashboard_tab_id": tab_id[tab], "col": x, "row": y, "size_x": w, "size_y": h,
              "parameter_mappings": []}
        if uses_skill:
            dc["parameter_mappings"] = [{"parameter_id": "skill", "card_id": ids[key],
                                         "target": ["variable", ["template-tag", "skill"]]}]
        dashcards.append(dc)
    body = {"name": "Claude Code sessions", "collection_id": col_id,
            "description": "How Claude Code sessions go, and how each version of a skill behaves: cost, tools, checks, "
                           "the questions it asks, the files it reads. Source: the convo-analysis warehouse.",
            "tabs": tabs, "dashcards": dashcards,
            "parameters": [{"id": "skill", "name": "Skill", "slug": "skill", "type": "string/=", "default": ["rde"],
                            "values_source_type": "static-list", "values_source_config": {"values": skills}}]}
    d = mb("dashboard", "create", body=body)
    did = d.get("id") or d["data"]["id"]
    print("dashboard", did)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--test", action="store_true", help="run every card's SQL against the local warehouse")
    ap.add_argument("--build", action="store_true", help="create the collection, cards and dashboard in Metabase")
    ap.add_argument("--profile", help="mb CLI profile of the target Metabase (required with --build)")
    ap.add_argument("--database", type=int, help="the warehouse's database id in that Metabase (required with --build)")
    args = ap.parse_args()
    if args.test:
        sys.exit(1 if test() else 0)
    if args.build:
        if not args.profile or not args.database:
            ap.error("--build needs --profile and --database")
        PROFILE, DB = args.profile, args.database
        build()
    else:
        ap.print_help()
