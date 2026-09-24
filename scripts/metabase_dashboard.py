"""Build a skill's evaluation dashboard in Metabase on the session warehouse (warehouse.py, clickhouse.py).

    python3 scripts/metabase_dashboard.py --test [--clickhouse] [--skill rde]
    python3 scripts/metabase_dashboard.py --sync --profile <mb profile> --database <id> --collection <id> [--skill rde]

One dashboard per skill, for developing it: every card is that skill's runs, compared version by version and prompt
by prompt, since the way to judge a change to a skill is to run the same prompt on a fresh Metabase with each version.
Tabs: Skill versions, Interview, Question topics, Skill files & CLI, Overview, Session (one session in depth);
filters: Person (the ClickHouse warehouse's: whose sessions), Skill version, Prompt (the run's `prompt_key`: the
prompt's opening words, shared by repeats of one prompt), Session, Data-engineering topic and Layer.

--test runs every card's SQL against the warehouse: the local Postgres (docker exec psql), or with --clickhouse the
ClickHouse in ~/.config/convo-analysis/.env. --sync creates or updates, in the collection: the dashboard, its cards
(native SQL on the warehouse's tables and views; a new card is created inside the dashboard), and the semantic layer
the Question topics tab reads. Everything is found by name, so a second --sync updates in place and keeps every id;
text cards added by hand stay where they are, and the tab's cards start below them. The SQL dialect follows the
Metabase database's engine (postgres or clickhouse; --ch-database names the ClickHouse database, default sessions).
Afterwards every card is run once through Metabase (--no-verify skips).

It needs the Metabase CLI (`mb`) logged in to the target Metabase (--profile) and the warehouse already a database
there (--database: its id in `mb db list`). Pass the profile explicitly so nothing lands on a Metabase you did not
mean.

The semantic layer is a model, "Interview questions" (v_interview_questions: one row per question, with the
data-engineering topic and layer it is about, from semantics/questions.json, and friendly outcome columns), and
metrics on it; ask new questions of the model in the query builder rather than writing SQL against the tables.
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from session_analytics import checks as checks_mod  # noqa: E402 - the checks the Checks, run by run grid shows
from session_analytics import semantics  # noqa: E402 - the topics and layers the questions are mapped to

PROFILE = None
DB = None
WORK = Path(tempfile.mkdtemp(prefix="skill-dashboard-"))
NETWORK_TRIES = 4

# The skill, as an SQL string literal; filled in when a card is built (lit).
SKILL = ":skill"
VERSION, PROMPT, TOPIC, PERSON, SESSION = "{{version}}", "{{prompt}}", "{{de_topic}}", "{{person}}", "{{session}}"
# What each filter is called in a native card's template tags and on the dashboard, in the dashboard's order. Person is
# the shared ClickHouse warehouse's column (whose sessions); the Postgres warehouse is one person's and has none.
FILTER_NAMES = {"person": "Person", "version": "Skill version", "prompt": "Prompt", "session": "Session",
                "de_topic": "Data-engineering topic", "layer": "Layer"}
RUN_FILTERS, VERSION_CHARTS, SESSION_FILTERS = ("version", "prompt"), ("prompt",), ("version", "prompt", "session")
BY_VERSION = "ORDER BY min(r.version_date) NULLS LAST, r.version"
NO_PROMPT = "'(no prompt)'"
MB = "c.program = 'mb' AND NOT coalesce(c.is_help, false)"
EMPTY = "('no preference', 'declined', 'unanswered')"
OFFERED = ("q.channel = 'ask' AND q.recommended_label IS NOT NULL AND NOT q.multi "
           "AND q.outcome NOT IN ('declined', 'unanswered', 'interrupted', 'error')")


def run_filters(a="r", version=True, person=False):
    """Optional clauses narrowing skill_runs (alias `a`) to the Skill version, Prompt and (ClickHouse) Person filters."""
    return ((f"[[AND {a}.version = {VERSION}]] " if version else "") + f"[[AND {a}.prompt_key = {PROMPT}]]"
            + (f" [[AND {a}.person = {PERSON}]]" if person else ""))


def question_filters(runs, a="q", version=True, person=False):
    """The same for a table with run_id, session_id and version (questions, …): the prompt is the run's."""
    return ((f"[[AND {a}.version = {VERSION}]] " if version else "")
            + f"[[AND ({a}.run_id, {a}.session_id) IN (SELECT run_id, session_id FROM {runs} WHERE prompt_key = {PROMPT})]]"
            + (f" [[AND {a}.person = {PERSON}]]" if person else ""))


def session_label(a, dialect="postgres"):
    """How a session is named in the Session filter: start, the id's first 8 characters (as in run ids), title."""
    if dialect == "clickhouse":
        return (f"concat(formatDateTime({a}.start_at, '%Y-%m-%d %H:%i'), ' · ', left({a}.session_id, 8), ' · ', "
                f"leftUTF8(ifNull(coalesce({a}.title, {a}.project), ''), 60))")
    return (f"to_char({a}.start_at, 'YYYY-MM-DD HH24:MI') || ' · ' || left({a}.session_id, 8) || ' · ' "
            f"|| left(coalesce({a}.title, {a}.project, ''), 60)")


def in_sessions(col, dialect="postgres", db=None):
    """`col` (a session id) is a session with a run of the skill the filters keep, and the Session filter's, if set."""
    ch = dialect == "clickhouse"
    runs, sessions = (f"{db}.skill_runs", f"{db}.sessions") if ch else ("skill_runs", "sessions")
    return (f"{col} IN (SELECT sr.session_id FROM {runs} AS sr WHERE sr.skill = {SKILL} {run_filters('sr', person=ch)}) "
            f"[[AND {col} IN (SELECT ss.session_id FROM {sessions} AS ss WHERE {session_label('ss', dialect)} = {SESSION})]]")


RF, QF = run_filters(), question_filters("skill_runs")

# One row per metric, each run a value: (label, column expression on skill_runs).
PER_RUN = [
    ("Estimated cost (USD)", "cost_usd"), ("Cost Claude Code attributes to the skill (USD)", "attributed_cost_usd"),
    ("Active minutes", "active_ms / 60000.0"), ("Wall-clock minutes", "duration_ms / 60000.0"),
    ("Turns", "turns"), ("Follow-up turns", "follow_up_turns"), ("Claude API requests", "requests"),
    ("Tool calls", "tool_calls"), ("Failed tool calls", "tool_errors"), ("Retries after an error", "retries_after_error"),
    ("CLI calls", "cli_calls"), ("--help lookups", "help_lookups"),
    ("Questions asked (AskUserQuestion)", "questions_asked"), ("Question rounds", "question_rounds"),
    ("Questions asked in prose", "prose_questions"), ("Typed answers", "typed_answers"),
    ("Skill docs read", "docs_read"), ("Doc tokens", "doc_tokens"), ("Peak context tokens", "context_peak"),
    ("Objects created", "objects_created"), ("Checks passed", "checks_passed"), ("Checks failed", "checks_failed"),
]
PER_RUN_SQL = (f"WITH s AS (SELECT r.* FROM skill_runs r WHERE r.skill = {SKILL} {RF}),\nm AS (\n  "
               + "\n  UNION ALL ".join(f"SELECT {i} AS ord, '{label}' AS metric, ({expr})::numeric AS v FROM s" if i == 1
                                     else f"SELECT {i}, '{label}', {expr} FROM s"
                                     for i, (label, expr) in enumerate(PER_RUN, 1))
               + "\n)\nSELECT ord, metric, round(avg(v), 2) AS average,\n"
                 "       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY v))::numeric, 2) AS median,\n"
                 "       round(min(v), 2) AS minimum, round(max(v), 2) AS maximum\n"
                 "FROM m GROUP BY ord, metric ORDER BY ord")

# What a run did, from its mb calls (help lookups aside): (column, the signatures that count).
STEPS = [
    ("built_a_transform", ("mb transform create",)),
    ("wrote_transform_tests", ("mb transform-test create",)),
    ("ran_transform_tests", ("mb transform-test run",)),
    ("ran_a_transform", ("mb transform run", "mb transform-job run")),
    ("defined_measures_or_segments", ("mb measure create", "mb segment create")),
    ("published_to_the_library", ("mb library publish",)),
    ("built_a_dashboard", ("mb dashboard create",)),
    ("wrote_a_document", ("mb document create",)),
]


def _in(sigs):
    return ", ".join(f"'{s}'" for s in sigs)


STEPS_SQL = ("WITH steps AS (SELECT c.run_id, c.session_id,\n       "
             + ",\n       ".join(f"bool_or(c.signature IN ({_in(sigs)})) AS {col}" for col, sigs in STEPS)
             + f"\n  FROM cli_calls c WHERE {MB} GROUP BY c.run_id, c.session_id)\n"
             f"SELECT r.version, max(r.version_subject) AS subject, count(*) AS runs,\n       "
             + ",\n       ".join(f"round(count(*) FILTER (WHERE s.{col})::numeric / count(*), 3) AS {col}" for col, _ in STEPS)
             + f"\nFROM skill_runs r LEFT JOIN steps s ON s.run_id = r.run_id AND s.session_id = r.session_id\n"
             f"WHERE r.skill = {SKILL} {run_filters(version=False)} GROUP BY r.version {BY_VERSION}")

USD = {"number_style": "currency", "currency": "USD"}


def pct(*cols, decimals=0):
    return {f'["name","{c}"]': {"number_style": "percent", "decimals": decimals} for c in cols}


# A model's usage over the API requests `a`.
MODEL_USAGE = ("sum(a.input_tokens) AS input_tokens, sum(a.output_tokens) AS output_tokens, "
               "sum(a.cache_read_tokens) AS cache_read_tokens, "
               "sum(coalesce(a.cache_write_5m_tokens, 0) + coalesce(a.cache_write_1h_tokens, 0)) AS cache_write_tokens, "
               "sum(a.thinking_tokens) AS thinking_tokens, round(sum(a.cost_usd), 2) AS cost_usd")
# An API request belongs to the run whose span it falls in.
IN_RUNS = ("FROM api_requests a JOIN skill_runs r ON r.session_id = a.session_id "
           f"WHERE a.at >= r.start_at AND a.at <= r.end_at AND r.skill = {SKILL}")

# The runs of the Prompt filter, the versions ranked newest first among them, and each check's results per version.
CHECKS_BY_VERSION = (f"WITH runs AS (SELECT r.run_id, r.version, r.version_date FROM skill_runs r "
                     f"WHERE r.skill = {SKILL} {run_filters(version=False)}),\n"
                     f"v AS (SELECT version, row_number() OVER (ORDER BY min(version_date) DESC NULLS LAST, version DESC) "
                     f"AS rn FROM runs GROUP BY version),\n"
                     f"c AS (SELECT k.check_id, k.version, max(k.description) AS description, "
                     f"count(*) FILTER (WHERE k.status = 'pass') AS passed, count(*) FILTER (WHERE k.status = 'fail') AS failed,\n"
                     f"             string_agg(k.detail, ' · ' ORDER BY k.run_id) FILTER (WHERE k.status = 'fail') AS details\n"
                     f"      FROM skill_run_checks k JOIN runs USING (run_id) WHERE k.skill = {SKILL} "
                     f"GROUP BY k.check_id, k.version)\n")
PASS_RATE = "round(c.passed::numeric / nullif(c.passed + c.failed, 0), 3)"

# (key, tab, name, display, sql, visualization_settings, filters, (col, row, w, h))
CARDS = [
    # ---------------------------------------------------------------- Skill versions
    ("versions", "Skill versions", "Versions compared, prompt by prompt", "table",
     f"SELECT coalesce(r.prompt_key, {NO_PROMPT}) AS prompt, r.version, max(r.version_subject) AS subject, "
     f"min(r.version_date) AS version_date, count(*) AS runs, "
     f"percentile_cont(0.5) WITHIN GROUP (ORDER BY r.cost_usd) AS median_cost_usd, "
     f"round((percentile_cont(0.5) WITHIN GROUP (ORDER BY r.active_ms) / 60000.0)::numeric, 1) AS median_active_minutes, "
     f"percentile_cont(0.5) WITHIN GROUP (ORDER BY r.tool_calls) AS median_tool_calls, "
     f"percentile_cont(0.5) WITHIN GROUP (ORDER BY r.tool_errors) AS median_tool_errors, "
     f"percentile_cont(0.5) WITHIN GROUP (ORDER BY r.help_lookups) AS median_help_lookups, "
     f"round(avg(r.questions_asked), 1) AS avg_questions_asked, round(avg(r.prose_questions), 1) AS avg_prose_questions, "
     f"round(sum(r.recommended_taken)::numeric / nullif(sum(r.recommended_offered), 0), 3) AS recommended_rate, "
     f"percentile_cont(0.5) WITHIN GROUP (ORDER BY r.objects_created) AS median_objects_created, "
     f"round(sum(r.checks_passed)::numeric / nullif(sum(r.checks_passed + r.checks_failed), 0), 3) AS checks_pass_rate "
     f"FROM skill_runs r WHERE r.skill = {SKILL} {RF} GROUP BY 1, r.version "
     f"ORDER BY 1, min(r.version_date) NULLS LAST, r.version",
     {"column_settings": {'["name","median_cost_usd"]': USD, **pct("recommended_rate", "checks_pass_rate")}},
     RUN_FILTERS, (0, 0, 24, 7)),
    ("vcost", "Skill versions", "Median cost per run, by version", "bar",
     f"SELECT r.version, percentile_cont(0.5) WITHIN GROUP (ORDER BY r.cost_usd) AS median_cost_usd "
     f"FROM skill_runs r WHERE r.skill = {SKILL} {run_filters(version=False)} GROUP BY r.version {BY_VERSION}",
     {"graph.dimensions": ["version"], "graph.metrics": ["median_cost_usd"],
      "column_settings": {'["name","median_cost_usd"]': USD}}, VERSION_CHARTS, (0, 7, 12, 7)),
    ("vtools", "Skill versions", "Median failed tool calls and --help lookups per run, by version", "bar",
     f"SELECT r.version, percentile_cont(0.5) WITHIN GROUP (ORDER BY r.tool_errors) AS median_tool_errors, "
     f"percentile_cont(0.5) WITHIN GROUP (ORDER BY r.help_lookups) AS median_help_lookups "
     f"FROM skill_runs r WHERE r.skill = {SKILL} {run_filters(version=False)} GROUP BY r.version {BY_VERSION}",
     {"graph.dimensions": ["version"], "graph.metrics": ["median_tool_errors", "median_help_lookups"],
      "graph.y_axis.auto_split": False}, VERSION_CHARTS, (12, 7, 12, 7)),
    ("steps", "Skill versions", "Steps the runs took, by version: share of runs", "table", STEPS_SQL,
     {"column_settings": pct(*(col for col, _ in STEPS))}, VERSION_CHARTS, (0, 14, 24, 6)),
    ("checkrates", "Skill versions", "Checks: the latest version against the one before", "table",
     CHECKS_BY_VERSION
     + f"SELECT c.check_id, max(c.version) FILTER (WHERE v.rn = 2) AS previous_version, "
     f"max({PASS_RATE}) FILTER (WHERE v.rn = 2) AS previous_pass_rate, "
     f"max(c.passed || '/' || (c.passed + c.failed)) FILTER (WHERE v.rn = 2) AS previous_runs, "
     f"max(c.version) FILTER (WHERE v.rn = 1) AS latest_version, max({PASS_RATE}) FILTER (WHERE v.rn = 1) AS latest_pass_rate, "
     f"max(c.passed || '/' || (c.passed + c.failed)) FILTER (WHERE v.rn = 1) AS latest_runs, "
     f"max({PASS_RATE}) FILTER (WHERE v.rn = 1) - max({PASS_RATE}) FILTER (WHERE v.rn = 2) AS change, "
     f"max(c.description) AS description FROM c JOIN v USING (version) WHERE v.rn <= 2 GROUP BY c.check_id "
     f"ORDER BY latest_pass_rate NULLS LAST, change NULLS LAST, c.check_id",
     {"column_settings": pct("previous_pass_rate", "latest_pass_rate", "change")}, VERSION_CHARTS, (0, 20, 24, 11)),
    ("failing", "Skill versions", "Checks failing on the latest version", "table",
     CHECKS_BY_VERSION
     + f"SELECT c.check_id, c.description, c.passed, c.failed, {PASS_RATE} AS pass_rate, c.details "
     f"FROM c JOIN v USING (version) WHERE v.rn = 1 AND c.failed > 0 ORDER BY pass_rate, c.check_id",
     {"column_settings": pct("pass_rate")}, VERSION_CHARTS, (0, 31, 24, 6)),
    # ---------------------------------------------------------------- Interview
    ("qasked", "Interview", "Questions asked (AskUserQuestion)", "scalar",
     f"SELECT count(*) AS asked FROM questions q WHERE q.skill = {SKILL} AND q.channel = 'ask' {QF}",
     {"scalar.field": "asked"}, RUN_FILTERS, (0, 0, 6, 3)),
    ("qrate", "Interview", "Recommended option taken", "scalar",
     f"SELECT count(*) FILTER (WHERE q.outcome = 'recommended')::numeric / nullif(count(*) FILTER (WHERE {OFFERED}), 0) "
     f"AS recommended_rate FROM questions q WHERE q.skill = {SKILL} {QF}",
     {"scalar.field": "recommended_rate", "column_settings": pct("recommended_rate")}, RUN_FILTERS, (6, 0, 6, 3)),
    ("qprose", "Interview", "Asked in prose instead", "scalar",
     f"SELECT count(*) AS in_prose FROM questions q WHERE q.skill = {SKILL} AND q.channel <> 'ask' {QF}",
     {"scalar.field": "in_prose"}, RUN_FILTERS, (12, 0, 6, 3)),
    ("qwait", "Interview", "Median wait for an answer (seconds)", "scalar",
     f"SELECT round((percentile_cont(0.5) WITHIN GROUP (ORDER BY q.wait_ms) / 1000.0)::numeric) AS median_wait_s "
     f"FROM questions q WHERE q.skill = {SKILL} AND q.channel = 'ask' AND q.outcome NOT IN ('unanswered', 'declined') {QF}",
     {"scalar.field": "median_wait_s"}, RUN_FILTERS, (18, 0, 6, 3)),
    ("qtopics", "Interview", "What came back, by topic", "row",
     f"SELECT i.interview_topic AS topic_label, i.outcome_group AS outcome, count(*) AS questions "
     f"FROM v_interview_questions i WHERE i.skill = {SKILL} {question_filters('skill_runs', 'i')} "
     f"GROUP BY 1, 2 ORDER BY 1, 2",
     {"graph.dimensions": ["topic_label", "outcome"], "graph.metrics": ["questions"], "stackable.stack_type": "stacked"},
     RUN_FILTERS, (0, 3, 12, 10)),
    ("qchannel", "Interview", "Questions per run, on average, by version", "bar",
     f"SELECT r.version, round(avg(r.questions_asked), 1) AS askuserquestion, round(avg(r.prose_questions), 1) AS in_prose "
     f"FROM skill_runs r WHERE r.skill = {SKILL} {run_filters(version=False)} GROUP BY r.version {BY_VERSION}",
     {"graph.dimensions": ["version"], "graph.metrics": ["askuserquestion", "in_prose"], "stackable.stack_type": "stacked",
      "graph.show_values": True, "series_settings": {"askuserquestion": {"title": "AskUserQuestion"},
                                                      "in_prose": {"title": "in prose"}}},
     VERSION_CHARTS, (12, 3, 12, 10)),
    ("qtopictable", "Interview", "Topics by version", "table",
     f"SELECT q.version, max(q.topic_label) AS topic_label, count(*) FILTER (WHERE q.channel = 'ask') AS asked, "
     f"count(*) FILTER (WHERE q.channel <> 'ask') AS in_prose, count(DISTINCT q.run_id) AS runs, "
     f"count(*) FILTER (WHERE q.outcome = 'recommended') AS recommended_taken, "
     f"count(*) FILTER (WHERE {OFFERED}) AS recommended_offered, count(*) FILTER (WHERE q.typed IS NOT NULL) AS typed, "
     f"count(*) FILTER (WHERE q.outcome IN {EMPTY}) AS came_back_empty, "
     f"count(*) FILTER (WHERE q.reask_of IS NOT NULL) AS asked_again, "
     f"round((percentile_cont(0.5) WITHIN GROUP (ORDER BY q.wait_ms) FILTER (WHERE q.channel = 'ask') / 1000.0)::numeric) "
     f"AS median_wait_s FROM questions q WHERE q.skill = {SKILL} {QF} GROUP BY q.version, q.topic "
     f"ORDER BY q.version, asked DESC, topic_label", {}, RUN_FILTERS, (0, 13, 24, 9)),
    ("qtyped", "Interview", "Where the options fell short: answers typed instead of picked", "table",
     f"SELECT t.asked_at, t.version, t.topic_label, t.question, t.options_offered, t.typed FROM v_typed_answers t "
     f"WHERE t.skill = {SKILL} [[AND t.version = {VERSION}]] "
     f"[[AND t.run_id IN (SELECT run_id FROM skill_runs WHERE prompt_key = {PROMPT})]] ORDER BY t.asked_at DESC",
     {}, RUN_FILTERS, (0, 22, 14, 7)),
    ("qflags", "Interview", "Questions against the skill's rules", "row",
     f"SELECT trim(f) AS flag, count(*) AS questions FROM questions q, unnest(string_to_array(q.flags, ';')) AS f "
     f"WHERE q.skill = {SKILL} AND trim(f) <> '' {QF} GROUP BY trim(f) ORDER BY 2 DESC, 1",
     {"graph.dimensions": ["flag"], "graph.metrics": ["questions"]}, RUN_FILTERS, (14, 22, 10, 7)),
    ("qall", "Interview", "Every question", "table",
     f"SELECT q.asked_at, q.version, q.run_id, q.topic_label, q.header, q.question, q.outcome, "
     f"coalesce(q.typed, q.answer, q.reply, q.feedback) AS answer, round(q.wait_ms / 1000.0) AS wait_s, q.flags "
     f"FROM questions q WHERE q.skill = {SKILL} {QF} ORDER BY q.asked_at DESC, q.qid", {}, RUN_FILTERS, (0, 29, 24, 12)),
    # ---------------------------------------------------------------- Skill files & CLI
    ("files", "Skill files & CLI", "The skill's files: runs shown each one, by version", "table",
     f"WITH picked AS (SELECT r.run_id, r.version FROM skill_runs r WHERE r.skill = {SKILL} {RF}),\n"
     f"nv AS (SELECT version, count(*) AS runs FROM picked GROUP BY version)\n"
     f"SELECT f.path, f.version, count(DISTINCT f.run_id) FILTER (WHERE f.lines_seen > 0) AS runs_shown, max(nv.runs) AS runs, "
     f"count(*) FILTER (WHERE f.how IN ('full', 'injected', 'injected + re-read')) AS whole, "
     f"count(*) FILTER (WHERE f.how IN ('partial', 'hits')) AS in_part, "
     f"percentile_cont(0.5) WITHIN GROUP (ORDER BY f.coverage) AS median_coverage, "
     f"percentile_cont(0.5) WITHIN GROUP (ORDER BY f.read_order) AS median_order "
     f"FROM skill_run_files f JOIN picked USING (run_id) JOIN nv ON nv.version = f.version "
     f"WHERE f.skill = {SKILL} AND f.owner = {SKILL} GROUP BY f.path, f.version ORDER BY f.path, f.version",
     {"column_settings": pct("median_coverage")}, RUN_FILTERS, (0, 0, 12, 12)),
    ("clidocs", "Skill files & CLI", "Other docs the runs read (bundled CLI skills)", "table",
     f"WITH picked AS (SELECT r.run_id, r.version FROM skill_runs r WHERE r.skill = {SKILL} {RF}),\n"
     f"nv AS (SELECT version, count(*) AS runs FROM picked GROUP BY version)\n"
     f"SELECT f.owner || ':' || f.path AS doc, f.version, count(DISTINCT f.run_id) FILTER (WHERE f.lines_seen > 0) "
     f"AS runs_shown, max(nv.runs) AS runs, "
     f"count(*) FILTER (WHERE f.how IN ('full', 'injected', 'injected + re-read')) AS whole, "
     f"count(*) FILTER (WHERE f.how IN ('partial', 'hits')) AS in_part, "
     f"percentile_cont(0.5) WITHIN GROUP (ORDER BY f.coverage) AS median_coverage "
     f"FROM skill_run_files f JOIN picked USING (run_id) JOIN nv ON nv.version = f.version "
     f"WHERE f.skill = {SKILL} AND f.owner <> {SKILL} GROUP BY 1, f.version ORDER BY runs_shown DESC, doc, f.version",
     {"column_settings": pct("median_coverage")}, RUN_FILTERS, (12, 0, 12, 12)),
    ("cli", "Skill files & CLI", "CLI commands in the skill's runs: uses, failures, --help", "table",
     f"SELECT c.signature, count(*) AS uses, count(*) FILTER (WHERE c.status = 'error') AS failed, "
     f"round(count(*) FILTER (WHERE c.status = 'error')::numeric / count(*), 3) AS failure_rate, "
     f"count(*) FILTER (WHERE c.is_help) AS help_lookups, count(DISTINCT c.run_id) AS runs "
     f"FROM cli_calls c JOIN skill_runs r USING (run_id, session_id) WHERE r.skill = {SKILL} AND c.signature LIKE '% %' "
     f"{RF} GROUP BY c.signature HAVING count(*) >= 2 ORDER BY uses DESC, c.signature LIMIT 40",
     {"column_settings": pct("failure_rate", decimals=1)}, RUN_FILTERS, (0, 12, 12, 12)),
    ("toolerrors", "Skill files & CLI", "Failed tool calls in the skill's runs", "table",
     f"SELECT t.at, r.version, t.run_id, t.tool, t.program, t.error_category, t.input, left(t.error, 300) AS error "
     f"FROM tool_calls t JOIN skill_runs r USING (run_id, session_id) WHERE r.skill = {SKILL} AND t.status = 'error' "
     f"{RF} ORDER BY t.at DESC, t.tool_use_id LIMIT 200", {}, RUN_FILTERS, (12, 12, 12, 12)),
    # ---------------------------------------------------------------- Overview: the skill's runs
    ("runs", "Overview", "Runs", "scalar",
     f"SELECT count(*) AS runs FROM skill_runs r WHERE r.skill = {SKILL} {RF}",
     {"scalar.field": "runs"}, RUN_FILTERS, (0, 0, 5, 3)),
    ("cost", "Overview", "Estimated cost of the runs", "scalar",
     f"SELECT round(sum(r.cost_usd), 2) AS cost_usd FROM skill_runs r WHERE r.skill = {SKILL} {RF}",
     {"scalar.field": "cost_usd", "column_settings": {'["name","cost_usd"]': USD}}, RUN_FILTERS, (5, 0, 5, 3)),
    ("hours", "Overview", "Active hours in the runs", "scalar",
     f"SELECT round(sum(r.active_ms) / 3600000.0, 1) AS active_hours FROM skill_runs r WHERE r.skill = {SKILL} {RF}",
     {"scalar.field": "active_hours"}, RUN_FILTERS, (10, 0, 5, 3)),
    ("err", "Overview", "Tool calls that failed in the runs", "scalar",
     f"SELECT sum(r.tool_errors)::numeric / nullif(sum(r.tool_calls), 0) AS error_rate FROM skill_runs r "
     f"WHERE r.skill = {SKILL} {RF}",
     {"scalar.field": "error_rate", "column_settings": pct("error_rate", decimals=1)}, RUN_FILTERS, (15, 0, 5, 3)),
    ("loaded", "Overview", "Data as of", "scalar",
     "SELECT max(loaded_at) AS loaded_at FROM warehouse_load", {"scalar.field": "loaded_at"}, (), (20, 0, 4, 3)),
    ("perrun", "Overview", "Per run: average, median and range", "table", PER_RUN_SQL,
     {"table.columns": [{"name": "ord", "enabled": False}] + [{"name": c, "enabled": True} for c in (
         "metric", "average", "median", "minimum", "maximum")]}, RUN_FILTERS, (0, 3, 12, 13)),
    ("costrun", "Overview", "Estimated cost of each run", "bar",
     f"SELECT to_char(r.start_at, 'MM-DD HH24:MI') || ' · ' || left(r.run_id, 8) AS run, r.version, "
     f"round(r.cost_usd, 2) AS cost_usd FROM skill_runs r WHERE r.skill = {SKILL} {RF} ORDER BY r.start_at",
     {"graph.dimensions": ["run", "version"], "graph.metrics": ["cost_usd"], "stackable.stack_type": "stacked",
      "column_settings": {'["name","cost_usd"]': USD}}, RUN_FILTERS, (12, 3, 12, 13)),
    ("costprompt", "Overview", "Cost by prompt", "row",
     f"SELECT coalesce(r.prompt_key, {NO_PROMPT}) AS prompt, round(sum(r.cost_usd), 2) AS cost_usd FROM skill_runs r "
     f"WHERE r.skill = {SKILL} {RF} GROUP BY 1 ORDER BY cost_usd DESC, prompt LIMIT 12",
     {"graph.dimensions": ["prompt"], "graph.metrics": ["cost_usd"], "column_settings": {'["name","cost_usd"]': USD}},
     RUN_FILTERS, (0, 16, 12, 8)),
    ("modelversion", "Overview", "Cost by model, per version", "bar",
     f"SELECT r.version, a.model, round(sum(a.cost_usd), 2) AS cost_usd {IN_RUNS} "
     f"{run_filters(version=False)} GROUP BY r.version, a.model ORDER BY min(r.version_date) NULLS LAST, r.version, a.model",
     {"graph.dimensions": ["version", "model"], "graph.metrics": ["cost_usd"], "stackable.stack_type": "stacked",
      "column_settings": {'["name","cost_usd"]': USD}}, VERSION_CHARTS, (12, 16, 12, 8)),
    ("models", "Overview", "Models in the runs: requests, tokens and cost", "table",
     f"SELECT a.model, count(*) AS requests, count(DISTINCT r.run_id) AS runs, {MODEL_USAGE}, "
     f"round(sum(a.cost_usd) / nullif(sum(sum(a.cost_usd)) OVER (), 0), 3) AS share_of_cost, "
     f"round((percentile_cont(0.5) WITHIN GROUP (ORDER BY a.latency_ms))::numeric) AS p50_latency_ms "
     f"{IN_RUNS} {RF} GROUP BY a.model ORDER BY cost_usd DESC NULLS LAST, a.model",
     {"column_settings": {'["name","cost_usd"]': USD, **pct("share_of_cost")}}, RUN_FILTERS, (0, 24, 24, 7)),
    ("tools", "Overview", "Tools in the runs: calls, failures and speed", "table",
     f"SELECT t.tool, count(*) AS calls, count(*) FILTER (WHERE t.status = 'error') AS errors, "
     f"round(count(*) FILTER (WHERE t.status = 'error')::numeric / count(*), 3) AS error_rate, "
     f"count(*) FILTER (WHERE t.status = 'denied') AS denied, "
     f"round((percentile_cont(0.5) WITHIN GROUP (ORDER BY t.duration_ms))::numeric) AS p50_ms "
     f"FROM tool_calls t JOIN skill_runs r USING (run_id, session_id) WHERE r.skill = {SKILL} {RF} "
     f"GROUP BY t.tool, t.category ORDER BY calls DESC, t.tool, t.category LIMIT 25",
     {"column_settings": pct("error_rate", decimals=1)}, RUN_FILTERS, (0, 31, 24, 9)),
    ("everyrun", "Overview", "Every run", "table",
     f"SELECT r.start_at, r.version, coalesce(r.prompt_key, {NO_PROMPT}) AS prompt, "
     f"coalesce(substring(r.prompt from 'https?://([^/[:space:]]+)'), substring(r.prompt from '(localhost:[0-9]+)')) "
     f"AS instance, r.mode, round(r.active_ms / 60000.0, 1) AS active_minutes, r.cost_usd, r.tool_calls, r.tool_errors, "
     f"r.help_lookups, r.questions_asked, r.prose_questions, r.checks_passed, r.checks_failed, r.objects_created, "
     f"r.end_reason, r.run_id, {session_label('s')} AS session FROM skill_runs r "
     f"LEFT JOIN sessions s ON s.session_id = r.session_id WHERE r.skill = {SKILL} {RF} ORDER BY r.start_at DESC",
     {"column_settings": {'["name","cost_usd"]': USD}}, RUN_FILTERS, (0, 40, 24, 12)),
    # ---------------------------------------------------------------- Session: one session, in depth
    ("sessions", "Session", "Sessions with the skill's runs: pick one in the Session filter, or click it", "table",
     f"SELECT {session_label('s')} AS session, s.start_at, s.project, s.title, "
     f"round(s.active_ms / 60000.0, 1) AS active_minutes, s.cost_usd, s.turns, s.prompts, s.api_requests, s.tool_calls, "
     f"s.tool_errors, s.subagents, s.compactions, count(*) AS runs, string_agg(DISTINCT r.version, ', ') AS versions "
     f"FROM sessions s JOIN skill_runs r ON r.session_id = s.session_id WHERE r.skill = {SKILL} {RF} "
     f"[[AND s.session_id IN (SELECT ss.session_id FROM sessions ss WHERE {session_label('ss')} = {SESSION})]] "
     f"GROUP BY s.session_id, s.start_at, s.project, s.title, s.active_ms, s.cost_usd, s.turns, s.prompts, "
     f"s.api_requests, s.tool_calls, s.tool_errors, s.subagents, s.compactions ORDER BY s.start_at DESC",
     {"column_settings": {'["name","cost_usd"]': USD}}, SESSION_FILTERS, (0, 0, 24, 7)),
    ("smodels", "Session", "Models in the session: requests, tokens and cost", "table",
     f"SELECT a.model, a.scope, count(*) AS requests, {MODEL_USAGE}, "
     f"round((percentile_cont(0.5) WITHIN GROUP (ORDER BY a.latency_ms))::numeric) AS p50_latency_ms "
     f"FROM api_requests a WHERE {in_sessions('a.session_id')} GROUP BY a.model, a.scope "
     f"ORDER BY cost_usd DESC NULLS LAST, a.model, a.scope",
     {"column_settings": {'["name","cost_usd"]': USD}}, SESSION_FILTERS, (0, 7, 12, 8)),
    ("sturncost", "Session", "Cost of each turn, by model", "bar",
     f"SELECT a.turn, a.model, round(sum(a.cost_usd), 2) AS cost_usd FROM api_requests a "
     f"WHERE {in_sessions('a.session_id')} AND a.turn IS NOT NULL GROUP BY a.turn, a.model ORDER BY a.turn, a.model",
     {"graph.dimensions": ["turn", "model"], "graph.metrics": ["cost_usd"], "stackable.stack_type": "stacked",
      "graph.x_axis.scale": "ordinal", "column_settings": {'["name","cost_usd"]': USD}}, SESSION_FILTERS, (12, 7, 12, 8)),
    ("sturns", "Session", "Turns: what each prompt asked, cost and did", "table",
     f"SELECT t.turn, t.start_at, t.trigger, left(coalesce(t.prompt, t.command), 200) AS prompt, "
     f"round(t.duration_ms / 1000.0) AS seconds, t.requests, t.tool_calls, t.tool_errors, round(t.cost_usd, 2) AS cost_usd, "
     f"t.max_context_tokens, t.skills_invoked, t.interrupted, t.compacted "
     f"FROM turns t WHERE {in_sessions('t.session_id')} ORDER BY t.start_at, t.turn LIMIT 1000",
     {"column_settings": {'["name","cost_usd"]': USD}}, SESSION_FILTERS, (0, 15, 24, 10)),
    ("sruns", "Session", "Skill runs in the session", "table",
     f"SELECT r.start_at, r.run_id, r.skill, r.version, r.mode, round(r.active_ms / 60000.0, 1) AS active_minutes, "
     f"r.cost_usd, r.tool_calls, r.tool_errors, r.questions_asked, r.checks_passed, r.checks_failed, r.objects_created, "
     f"r.end_reason FROM skill_runs r WHERE {in_sessions('r.session_id')} ORDER BY r.start_at, r.run_id",
     {"column_settings": {'["name","cost_usd"]': USD}}, SESSION_FILTERS, (0, 25, 24, 6)),
    ("squestions", "Session", "Questions in the session", "table",
     f"SELECT q.asked_at, q.skill, q.channel, q.topic_label, q.header, q.question, q.outcome, "
     f"coalesce(q.typed, q.answer, q.reply, q.feedback) AS answer, round(q.wait_ms / 1000.0) AS wait_s, q.flags "
     f"FROM questions q WHERE {in_sessions('q.session_id')} ORDER BY q.asked_at, q.qid",
     {}, SESSION_FILTERS, (0, 31, 24, 8)),
    ("ssubagents", "Session", "Subagents in the session", "table",
     f"SELECT g.start_at, g.kind, g.agent_type, g.description, round(g.duration_ms / 1000.0) AS seconds, g.requests, "
     f"g.tool_calls, g.tool_errors, round(g.cost_usd, 2) AS cost_usd, g.models "
     f"FROM subagents g WHERE {in_sessions('g.session_id')} ORDER BY g.start_at, g.agent_id",
     {"column_settings": {'["name","cost_usd"]': USD}}, SESSION_FILTERS, (0, 39, 24, 6)),
    ("stools", "Session", "Every tool call in the session", "table",
     f"SELECT t.at, t.turn, t.scope, t.tool, t.program, t.status, round(t.duration_ms / 1000.0, 1) AS seconds, t.run_id, "
     f"t.input, left(t.error, 300) AS error FROM tool_calls t WHERE {in_sessions('t.session_id')} "
     f"ORDER BY t.at, t.tool_use_id LIMIT 2000", {}, SESSION_FILTERS, (0, 45, 24, 12)),
]
# The cards whose columns list the filters' values: Person, Skill version and Prompt from Every run, Session from the
# Session tab's list.
VALUES_CARDS = {"person": "everyrun", "version": "everyrun", "prompt": "everyrun", "session": "sessions"}


def check_grid(dialect="postgres", db=None, skill="rde"):
    """(key, tab, name, display, sql, vis, filters, pos) of Checks, run by run: a column per check of the skill's
    checks file (checks/<skill>.json), ✓ passed, ✗ failed, – not applicable."""
    ids = [c["id"] for c in checks_mod.load(skill=skill)]
    marks = [f'["name","{_ident(i)}"]' for i in ids]
    vis = {"column_settings": {m: {"column_title": i} for m, i in zip(marks, ids)},
           "table.column_formatting": [
               {"id": n, "columns": [_ident(i) for i in ids], "type": "single", "operator": "=", "value": v,
                "color": color, "highlight_row": False} for n, (v, color) in enumerate((("✗", "#ED6E6E"), ("✓", "#84BB4C")))]}
    if dialect == "clickhouse":
        cells = ",\n       ".join(
            f"nullIf(maxIf(multiIf(k.status = 'pass', '✓', k.status = 'fail', '✗', '–'), k.check_id = '{i}'), '') "
            f"AS {_ident(i)}" for i in ids)
        sql = (f"SELECT r.start_at AS start_at, r.version AS version, ifNull(r.prompt_key, {NO_PROMPT}) AS prompt, "
               f"r.run_id AS run_id, r.checks_passed AS checks_passed, r.checks_failed AS checks_failed,\n       {cells}\n"
               f"FROM {db}.skill_runs AS r LEFT JOIN {db}.skill_run_checks AS k "
               f"ON k.run_id = r.run_id AND k.session_id = r.session_id\n"
               f"WHERE r.skill = {SKILL} {run_filters(person=True)}\n"
               f"GROUP BY r.run_id, r.session_id, r.start_at, r.version, r.prompt_key, r.checks_passed, r.checks_failed\n"
               f"ORDER BY start_at DESC")
    else:
        cells = ",\n       ".join(
            f"max(CASE k.status WHEN 'pass' THEN '✓' WHEN 'fail' THEN '✗' ELSE '–' END) "
            f"FILTER (WHERE k.check_id = '{i}') AS {_ident(i)}" for i in ids)
        sql = (f"SELECT r.start_at, r.version, coalesce(r.prompt_key, {NO_PROMPT}) AS prompt, r.run_id, r.checks_passed, "
               f"r.checks_failed,\n       {cells}\n"
               f"FROM skill_runs r LEFT JOIN skill_run_checks k ON k.run_id = r.run_id\n"
               f"WHERE r.skill = {SKILL} {RF}\n"
               f"GROUP BY r.run_id, r.start_at, r.version, r.prompt_key, r.checks_passed, r.checks_failed\n"
               f"ORDER BY r.start_at DESC")
    return ("checkgrid", "Skill versions", "Checks, run by run", "table", sql, vis, RUN_FILTERS, (0, 37, 24, 11))


# ---------------------------------------------------------------- The semantic layer on the questions
# A model (one row per question, with its data-engineering topic and layer) and metrics defined on it, so a question
# asked in the query builder counts, rates and waits the same way the dashboard does. The Question topics tab is
# built from them; its MBQL cards drill through to the questions behind a number.
MODEL_NAME = "Interview questions"
MODEL_SQL = "SELECT i.*, r.prompt_key FROM v_interview_questions i LEFT JOIN skill_runs r ON r.run_id = i.run_id"
MODEL_DESCRIPTION = ("One row per question Claude put to the user — AskUserQuestion, in prose, or a printed checkpoint — "
                     "with the data-engineering topic and layer it is about, what came back, and the prompt of the run "
                     "that asked it. The topics and layers "
                     "are rules in semantics/questions.json (convo-analysis); the rows are reloaded when a session ends.")
# column -> (display name, description, semantic type)
MODEL_COLUMNS = {
    "qid": ("Question ID", None, "type/PK"),
    "asked_at": ("Asked at", None, "type/CreationTimestamp"),
    "skill": ("Skill", "The skill whose run asked it; empty outside a skill run.", "type/Category"),
    "version": ("Skill version", "The skill's git commit that ran.", "type/Category"),
    "version_date": ("Version date", "When that commit was made.", None),
    "version_subject": ("Version subject", "That commit's subject line.", None),
    "prompt_key": ("Prompt", "The opening words of the prompt the run started from: repeats of one prompt share it, "
                             "so versions compare on the same task.", "type/Category"),
    "run_id": ("Skill run", "The skill run that asked it.", None),
    "session_id": ("Session", None, None),
    "project": ("Project", None, "type/Category"),
    "person": ("Person", "Whose sessions (ClickHouse warehouse only): CLICKHOUSE_PERSON on the loading machine, else its "
                        "git email.", "type/Category"),
    "channel": ("Asked via", "AskUserQuestion, in prose (a question at the end of a reply), or a printed checkpoint.",
                "type/Category"),
    "interview_topic": ("Interview topic", "The skill's own topic for the question (checks/<skill>.json).", "type/Category"),
    "de_topic": ("Data-engineering topic", "What kind of concern the question is about.", "type/Category"),
    "de_topic_order": ("Topic order", "Display order of the topic.", None),
    "layer": ("Layer", "Where in the data stack the question sits; cross-cutting when it spans the stack.", "type/Category"),
    "layer_order": ("Layer order", "Display order of the layer, presentation first.", None),
    "classified_by": ("Placed by", "What decided topic / layer: a rule on the header, a rule on the question, or the "
                                   "fallback from the interview topic.", "type/Category"),
    "header": ("Header", "The question's short label.", None),
    "question": ("Question", None, None),
    "outcome": ("Outcome", None, "type/Category"),
    "outcome_group": ("What came back", None, "type/Category"),
    "recommendation_offered": ("Recommendation offered", "A single-choice AskUserQuestion with a recommended option "
                                                         "that came back with an answer.", None),
    "took_recommendation": ("Took the recommendation", None, None),
    "typed_answer": ("Typed an answer", "Answered by typing instead of picking: the options fell short.", None),
    "came_back_empty": ("Came back empty", "No preference, declined, or never answered.", None),
    "answer": ("Answer", "What the user typed, picked or replied.", None),
    "wait_s": ("Wait (seconds)", "From an AskUserQuestion to its answer; empty when it was not answered.", None),
    "flags": ("Flags", "The skill's interview rules the question broke, separated by ;.", None),
}
# A dashboard filter -> the model column it narrows on the Question topics tab's MBQL cards.
MODEL_FILTER_COLUMNS = {"person": "person", "version": "version", "prompt": "prompt_key", "de_topic": "de_topic",
                        "layer": "layer"}


def col(name, base="type/Text"):
    return ["field", {"base-type": base}, name]


def is_true(name):
    return ["=", {}, col(name, "type/Boolean"), True]


ASKED = ["=", {}, col("channel"), "AskUserQuestion"]
# (key, name, description, output column, aggregation)
METRICS = [
    ("questions", "Questions", "Every question put to the user: AskUserQuestion, in prose, or a printed checkpoint.",
     ["count", {}]),
    ("asked", "Questions asked with AskUserQuestion", "Questions put through AskUserQuestion, with options to pick.",
     ["count-where", {}, ASKED]),
    ("recommended_rate", "Recommended option taken",
     "Of the single-choice AskUserQuestion questions that offered a recommended option and came back with an answer, "
     "the share where the user took it.",
     ["/", {}, ["count-where", {}, is_true("took_recommendation")], ["count-where", {}, is_true("recommendation_offered")]]),
    ("typed_rate", "Typed-answer rate",
     "Share of AskUserQuestion questions answered by typing instead of picking: the options fell short.",
     ["/", {}, ["count-where", {}, is_true("typed_answer")], ["count-where", {}, ASKED]]),
    ("empty_rate", "Came back empty", "Share of questions that got no answer: no preference, declined, or never answered.",
     ["/", {}, ["count-where", {}, is_true("came_back_empty")], ["count", {}]]),
    ("median_wait_s", "Median wait for an answer", "Median seconds from an AskUserQuestion to its answer.",
     ["median", {}, col("wait_s", "type/Decimal")]),
]
TOPIC_TAB = "Question topics"
SESSION_TAB = "Session"
ALL_TABS = ["Skill versions", "Interview", TOPIC_TAB, "Skill files & CLI", "Overview", SESSION_TAB]


def description(skill):
    return (f"How each version of the {skill} skill behaves, prompt by prompt: cost, steps taken, checks, the questions "
            f"it asks and what they are about, the files it reads, the CLI calls that failed. Pick a Prompt to compare "
            f"versions on the same task. Source: the convo-analysis session warehouse.")


def crosstab_sql(dialect="postgres", db=None):
    """Topic x layer, one column per layer in stack order (from semantics/questions.json at build time)."""
    _, layers = semantics.load().dimensions()
    if dialect == "clickhouse":
        cells = ",\n       ".join(f"countIf(q.layer = '{lay['id']}') AS {_ident(lay['id'])}" for lay in layers)
        # a topic no question matched joins a row whose key is '' (not NULL): count the matches only
        return (f"SELECT t.label AS de_topic,\n       {cells},\n       countIf(q.qid != '') AS all_layers\n"
                f"FROM {db}.de_topics AS t LEFT JOIN (SELECT x.qid AS qid, x.de_topic AS de_topic, x.layer AS layer "
                f"FROM {db}.questions AS x WHERE x.skill = {SKILL} {question_filters(f'{db}.skill_runs', 'x', person=True)}) AS q "
                f"ON q.de_topic = t.id\n"
                f"GROUP BY t.label, t.sort_order ORDER BY t.sort_order")
    cells = ",\n       ".join(f"count(q.qid) FILTER (WHERE q.layer = '{lay['id']}') AS {_ident(lay['id'])}" for lay in layers)
    return (f"SELECT t.label AS de_topic,\n       {cells},\n       count(q.qid) AS all_layers\n"
            f"FROM de_topics t LEFT JOIN (SELECT x.qid, x.de_topic, x.layer FROM questions x WHERE x.skill = {SKILL} "
            f"{question_filters('skill_runs', 'x')}) q ON q.de_topic = t.id\n"
            f"GROUP BY t.label, t.sort_order ORDER BY t.sort_order")


def layer_rows(dialect="postgres"):
    """The layers as an inline table `l` (id, label, sort_order), from semantics/questions.json at build time like
    the matrix's columns, so the cards do not depend on which loader's de_layers the shared warehouse holds last."""
    _, layers = semantics.load().dimensions()
    rows = ", ".join(f"({lit(x['id'])}, {lit(x['label'])}, {x['sort_order']})" for x in layers)
    if dialect == "clickhouse":
        return f"values('id String, label String, sort_order Int64', {rows}) AS l"
    return f"(VALUES {rows}) AS l (id, label, sort_order)"


def model_sql_clickhouse(db):
    return (f"SELECT i.*, r.prompt_key AS prompt_key FROM {db}.v_interview_questions AS i "
            f"LEFT JOIN {db}.skill_runs AS r ON r.run_id = i.run_id AND r.session_id = i.session_id")


def _ident(s):
    return re.sub(r"\W", "_", s)


def topic_cards(dialect="postgres", db=None):
    """(key, name, display, query, visualization_settings, filters, (col, row, w, h)) for the Question topics tab.
    A query is ("sql", text) or ("mbql", stage without its source); filters: the dashboard filters the card takes."""
    _, layers = semantics.load().dimensions()
    layer_ids = [_ident(lay["id"]) for lay in layers]
    metric = lambda key: ("metric", key)  # noqa: E731 - resolved to the metric's id when the card is built
    ch = clickhouse_sql(db) if dialect == "clickhouse" else {}
    native = lambda key, pg: ("sql", ch.get(key, pg) if ch else pg)  # noqa: E731
    everything = ("version", "prompt", "de_topic", "layer")
    return [
        ("tq_total", "Questions put to the user", "scalar", ("mbql", {"aggregation": [metric("questions")]}),
         {"scalar.field": "questions"}, everything, (0, 0, 6, 3)),
        ("tq_stack", "About a data-stack layer (not cross-cutting)", "scalar",
         native("tq_stack", f"SELECT count(*) FILTER (WHERE q.layer <> 'cross-cutting')::numeric / nullif(count(*), 0) "
                            f"AS on_the_stack FROM questions q WHERE q.skill = {SKILL} {QF}"),
         {"scalar.field": "on_the_stack", "column_settings": pct("on_the_stack")}, RUN_FILTERS, (6, 0, 6, 3)),
        ("tq_never", "Layers the interview never asks about", "scalar",
         native("tq_never", f"SELECT coalesce(string_agg(l.label, ', ' ORDER BY l.sort_order), 'none') AS never_asked "
                            f"FROM {layer_rows()} WHERE l.id <> 'cross-cutting' AND NOT EXISTS (SELECT 1 FROM questions q "
                            f"WHERE q.layer = l.id AND q.skill = {SKILL} {QF})"),
         {"scalar.field": "never_asked"}, RUN_FILTERS, (12, 0, 6, 3)),
        ("tq_fallback", "Placed by the fallback, not a rule", "scalar",
         native("tq_fallback", f"SELECT count(*) FILTER (WHERE q.semantics_by LIKE '%fallback%' OR q.de_topic = 'other')"
                               f"::numeric / nullif(count(*), 0) AS by_fallback FROM questions q "
                               f"WHERE q.skill = {SKILL} {QF}"),
         {"scalar.field": "by_fallback", "column_settings": pct("by_fallback")}, RUN_FILTERS, (18, 0, 6, 3)),
        ("tq_matrix", "Where the questions land: data-engineering topic × layer", "table",
         native("tq_matrix", crosstab_sql()),
         {"column_settings": {'["name","de_topic"]': {"column_title": "Data-engineering topic"},
                              '["name","all_layers"]': {"column_title": "All layers"},
                              **{f'["name","{i}"]': {"column_title": lay["label"]} for i, lay in zip(layer_ids, layers)}},
          "table.column_formatting": [{"id": 0, "columns": layer_ids, "type": "range", "colors": ["#FFFFFF", "#509EE3"],
                                       "min_type": "all", "max_type": "all", "min_value": 0, "max_value": 100,
                                       "operator": "=", "value": "", "color": "#509EE3", "highlight_row": False}]},
         RUN_FILTERS, (0, 3, 24, 10)),
        ("tq_outcomes", "What came back, by data-engineering topic", "row",
         ("mbql", {"aggregation": [metric("questions")], "breakout": [col("de_topic"), col("outcome_group")]}),
         {"graph.dimensions": ["de_topic", "outcome_group"], "graph.metrics": ["questions"],
          "stackable.stack_type": "stacked"}, everything, (0, 13, 12, 10)),
        ("tq_scorecard", "Each topic: how the questions went", "table",
         ("mbql", {"aggregation": [metric(k) for k, *_ in METRICS], "breakout": [col("de_topic")],
                   "order-by": [("desc-aggregation", "questions")]}),
         {"column_settings": {**pct("recommended_rate", "typed_rate", "empty_rate"),
                              '["name","median_wait_s"]': {"decimals": 0}}},
         everything, (12, 13, 12, 10)),
        ("tq_versions", "Questions per run, by layer and version", "bar",
         native("tq_versions",
                f"WITH runs AS (SELECT r.version, max(r.version_date) AS version_date, count(*) AS runs FROM skill_runs r "
                f"WHERE r.skill = {SKILL} {run_filters(version=False)} GROUP BY r.version)\n"
                f"SELECT r.version, l.label AS layer, round(count(q.qid)::numeric / r.runs, 2) AS questions_per_run\n"
                f"FROM runs r CROSS JOIN {layer_rows()}\n"
                f"LEFT JOIN (SELECT x.qid, x.version, x.layer FROM questions x WHERE x.skill = {SKILL} "
                f"[[AND x.de_topic_label = {TOPIC}]] {question_filters('skill_runs', 'x', version=False)}) q "
                f"ON q.version = r.version AND q.layer = l.id\n"
                f"GROUP BY r.version, r.version_date, r.runs, l.label, l.sort_order\n"
                f"ORDER BY r.version_date NULLS LAST, l.sort_order"),
         {"graph.dimensions": ["version", "layer"], "graph.metrics": ["questions_per_run"], "stackable.stack_type": "stacked",
          "graph.series_order": [{"key": lay["label"], "name": lay["label"], "enabled": True} for lay in layers]},
         ("prompt", "de_topic"), (0, 23, 24, 9)),
        ("tq_rows", "The questions, with their topic and layer", "table",
         ("mbql", {"fields": [col(c) for c in ("asked_at", "version", "de_topic", "layer", "interview_topic", "header",
                                               "question", "outcome_group", "answer", "wait_s", "classified_by")],
                   "order-by": [("desc", "asked_at")]}),
         {}, everything, (0, 32, 24, 12)),
    ]


# ---------------------------------------------------------------- the same cards in ClickHouse SQL
# For the warehouse clickhouse.py loads (same tables and views, in database `db`). Columns are qualified wherever an
# output alias reuses a column's name, so the SQL means the same on either of ClickHouse's analyzers. Run and
# question ids repeat across machines, so run joins also match the session.
def clickhouse_sql(db="sessions"):
    """{card key: ClickHouse SQL} for every native card, CARDS and the Question topics tab alike."""
    d = db
    rf, rp = run_filters(person=True), run_filters(version=False, person=True)
    qf = question_filters(f"{d}.skill_runs", person=True)
    by_version = "ORDER BY min(r.version_date) ASC NULLS LAST, version"
    offered = OFFERED
    median = lambda x: f"quantileExactInclusive(0.5)({x})"  # noqa: E731
    dec = lambda x, n: f"round(accurateCastOrNull({x}, 'Decimal64(9)'), {n})"  # noqa: E731 - rounds as Postgres does
    steps = ("WITH steps AS (SELECT c.run_id AS run_id, c.session_id AS session_id,\n       "
             + ",\n       ".join(f"max(c.signature IN ({_in(sigs)})) AS {col}" for col, sigs in STEPS)
             + f"\n  FROM {d}.cli_calls AS c WHERE c.program = 'mb' AND NOT ifNull(c.is_help, false) "
             f"GROUP BY c.run_id, c.session_id)\n"
             f"SELECT r.version AS version, max(r.version_subject) AS subject, count() AS runs,\n       "
             + ",\n       ".join(f"{dec(f'countIf(s.{col} = 1) / count()', 3)} AS {col}" for col, _ in STEPS)
             + f"\nFROM {d}.skill_runs AS r LEFT JOIN steps AS s ON s.run_id = r.run_id AND s.session_id = r.session_id\n"
             f"WHERE r.skill = {SKILL} {rp} GROUP BY r.version {by_version}")
    pass_rate = dec("c.passed / nullIf(c.passed + c.failed, 0)", 3)
    ratio = lambda rn: f"nullIf(maxIf(concat(toString(c.passed), '/', toString(c.passed + c.failed)), v.rn = {rn}), '')"  # noqa: E731
    checks = (f"WITH runs AS (SELECT r.run_id AS run_id, r.session_id AS session_id, r.version AS version, "
              f"r.version_date AS version_date FROM {d}.skill_runs AS r WHERE r.skill = {SKILL} {rp}),\n"
              f"vd AS (SELECT u.version AS version, min(u.version_date) AS first_date FROM runs AS u GROUP BY u.version),\n"
              f"v AS (SELECT vd.version AS version, row_number() OVER (ORDER BY vd.first_date DESC NULLS LAST, "
              f"vd.version DESC) AS rn FROM vd),\n"
              f"c AS (SELECT k.check_id AS check_id, k.version AS version, max(k.description) AS description, "
              f"countIf(k.status = 'pass') AS passed, countIf(k.status = 'fail') AS failed,\n"
              f"             nullIf(arrayStringConcat(groupArrayIf(k.detail, k.status = 'fail'), ' · '), '') AS details\n"
              f"      FROM {d}.skill_run_checks AS k INNER JOIN runs AS u ON u.run_id = k.run_id "
              f"AND u.session_id = k.session_id WHERE k.skill = {SKILL} GROUP BY k.check_id, k.version)\n")
    picked = (f"WITH picked AS (SELECT r.run_id AS run_id, r.session_id AS session_id, r.version AS version "
              f"FROM {d}.skill_runs AS r WHERE r.skill = {SKILL} {rf}),\n"
              f"nv AS (SELECT p.version AS version, count() AS runs FROM picked AS p GROUP BY p.version)\n")
    files_from = (f"FROM {d}.skill_run_files AS f INNER JOIN picked AS p ON p.run_id = f.run_id AND p.session_id = f.session_id "
                  f"INNER JOIN nv ON nv.version = f.version ")
    shown = "uniqExactIf((f.run_id, f.session_id), f.lines_seen > 0) AS runs_shown"
    whole = ("countIf(f.how IN ('full', 'injected', 'injected + re-read')) AS whole, "
             "countIf(f.how IN ('partial', 'hits')) AS in_part")
    runs_join = f"INNER JOIN {d}.skill_runs AS r ON r.run_id = {{a}}.run_id AND r.session_id = {{a}}.session_id"
    wait = dec("quantileExactInclusiveIf(0.5)(q.wait_ms, q.channel = 'ask') / 1000.0", 0)
    error_rate = dec("countIf(t.status = 'error') / count()", 3)
    usage = ("sum(a.input_tokens) AS input_tokens, sum(a.output_tokens) AS output_tokens, "
             "sum(a.cache_read_tokens) AS cache_read_tokens, "
             "sum(ifNull(a.cache_write_5m_tokens, 0) + ifNull(a.cache_write_1h_tokens, 0)) AS cache_write_tokens, "
             f"sum(a.thinking_tokens) AS thinking_tokens, {dec('sum(a.cost_usd)', 2)} AS cost_usd")
    in_runs = (f"FROM {d}.api_requests AS a INNER JOIN {d}.skill_runs AS r "
               f"ON r.session_id = a.session_id AND r.source = a.source "
               f"WHERE a.at >= r.start_at AND a.at <= r.end_at AND r.skill = {SKILL}")
    per_run = ",\n  ".join(f"({i}, '{label}', toFloat64({expr}))" for i, (label, expr) in enumerate(PER_RUN, 1))
    return {
        # Skill versions
        "versions": f"SELECT ifNull(r.prompt_key, {NO_PROMPT}) AS prompt, r.version AS version, "
                    f"max(r.version_subject) AS subject, min(r.version_date) AS version_date, count() AS runs, "
                    f"{median('r.cost_usd')} AS median_cost_usd, "
                    f"{dec(median('r.active_ms') + ' / 60000.0', 1)} AS median_active_minutes, "
                    f"{median('r.tool_calls')} AS median_tool_calls, {median('r.tool_errors')} AS median_tool_errors, "
                    f"{median('r.help_lookups')} AS median_help_lookups, "
                    f"{dec('avg(r.questions_asked)', 1)} AS avg_questions_asked, "
                    f"{dec('avg(r.prose_questions)', 1)} AS avg_prose_questions, "
                    f"{dec('sum(r.recommended_taken) / nullIf(sum(r.recommended_offered), 0)', 3)} AS recommended_rate, "
                    f"{median('r.objects_created')} AS median_objects_created, "
                    f"{dec('sum(r.checks_passed) / nullIf(sum(r.checks_passed + r.checks_failed), 0)', 3)} "
                    f"AS checks_pass_rate FROM {d}.skill_runs AS r WHERE r.skill = {SKILL} {rf} "
                    f"GROUP BY ifNull(r.prompt_key, {NO_PROMPT}), r.version "
                    f"ORDER BY prompt, version_date ASC NULLS LAST, version",
        "vcost": f"SELECT r.version AS version, {median('r.cost_usd')} AS median_cost_usd FROM {d}.skill_runs AS r "
                 f"WHERE r.skill = {SKILL} {rp} GROUP BY r.version {by_version}",
        "vtools": f"SELECT r.version AS version, {median('r.tool_errors')} AS median_tool_errors, "
                  f"{median('r.help_lookups')} AS median_help_lookups FROM {d}.skill_runs AS r "
                  f"WHERE r.skill = {SKILL} {rp} GROUP BY r.version {by_version}",
        "steps": steps,
        "checkrates": checks
        + f"SELECT c.check_id AS check_id, maxIf(c.version, v.rn = 2) AS previous_version, "
          f"maxIf({pass_rate}, v.rn = 2) AS previous_pass_rate, {ratio(2)} AS previous_runs, "
          f"maxIf(c.version, v.rn = 1) AS latest_version, maxIf({pass_rate}, v.rn = 1) AS latest_pass_rate, "
          f"{ratio(1)} AS latest_runs, maxIf({pass_rate}, v.rn = 1) - maxIf({pass_rate}, v.rn = 2) AS change, "
          f"max(c.description) AS description FROM c INNER JOIN v ON v.version = c.version WHERE v.rn <= 2 "
          f"GROUP BY c.check_id ORDER BY latest_pass_rate ASC NULLS LAST, change ASC NULLS LAST, check_id",
        "failing": checks
        + f"SELECT c.check_id AS check_id, c.description AS description, c.passed AS passed, c.failed AS failed, "
          f"{pass_rate} AS pass_rate, c.details AS details FROM c INNER JOIN v ON v.version = c.version "
          f"WHERE v.rn = 1 AND c.failed > 0 ORDER BY pass_rate, check_id",
        # Interview
        "qasked": f"SELECT count() AS asked FROM {d}.questions AS q WHERE q.skill = {SKILL} AND q.channel = 'ask' {qf}",
        "qrate": f"SELECT countIf(q.outcome = 'recommended') / nullIf(countIf({offered}), 0) AS recommended_rate "
                 f"FROM {d}.questions AS q WHERE q.skill = {SKILL} {qf}",
        "qprose": f"SELECT count() AS in_prose FROM {d}.questions AS q WHERE q.skill = {SKILL} AND q.channel != 'ask' {qf}",
        "qwait": f"SELECT {dec(median('q.wait_ms') + ' / 1000.0', 0)} AS median_wait_s FROM {d}.questions AS q "
                 f"WHERE q.skill = {SKILL} AND q.channel = 'ask' AND q.outcome NOT IN ('unanswered', 'declined') {qf}",
        "qtopics": f"SELECT i.interview_topic AS topic_label, i.outcome_group AS outcome, count() AS questions "
                   f"FROM {d}.v_interview_questions AS i WHERE i.skill = {SKILL} "
                   f"{question_filters(f'{d}.skill_runs', 'i', person=True)} "
                   f"GROUP BY i.interview_topic, i.outcome_group ORDER BY topic_label, outcome",
        "qchannel": f"SELECT r.version AS version, {dec('avg(r.questions_asked)', 1)} AS askuserquestion, "
                    f"{dec('avg(r.prose_questions)', 1)} AS in_prose FROM {d}.skill_runs AS r "
                    f"WHERE r.skill = {SKILL} {rp} GROUP BY r.version {by_version}",
        "qtopictable": f"SELECT q.version AS version, max(q.topic_label) AS topic_label, "
                       f"countIf(q.channel = 'ask') AS asked, countIf(q.channel != 'ask') AS in_prose, "
                       f"uniqExact(q.run_id, q.session_id) AS runs, countIf(q.outcome = 'recommended') AS recommended_taken, "
                       f"countIf({offered}) AS recommended_offered, countIf(q.typed IS NOT NULL) AS typed, "
                       f"countIf(q.outcome IN {EMPTY}) AS came_back_empty, "
                       f"countIf(q.reask_of IS NOT NULL) AS asked_again, "
                       f"{wait} AS median_wait_s FROM {d}.questions AS q WHERE q.skill = {SKILL} {qf} "
                       f"GROUP BY q.version, q.topic ORDER BY version, asked DESC, topic_label",
        "qtyped": f"SELECT t.asked_at AS asked_at, t.version AS version, t.topic_label AS topic_label, "
                  f"t.question AS question, t.options_offered AS options_offered, t.typed AS typed "
                  f"FROM {d}.v_typed_answers AS t WHERE t.skill = {SKILL} [[AND t.version = {VERSION}]] "
                  f"[[AND t.run_id IN (SELECT run_id FROM {d}.skill_runs WHERE prompt_key = {PROMPT})]] "
                  f"[[AND t.run_id IN (SELECT run_id FROM {d}.skill_runs WHERE person = {PERSON})]] "
                  f"ORDER BY asked_at DESC",
        "qflags": f"SELECT trimBoth(f) AS flag, count() AS questions FROM {d}.questions AS q "
                  f"ARRAY JOIN splitByChar(';', ifNull(q.flags, '')) AS f "
                  f"WHERE q.skill = {SKILL} AND trimBoth(f) != '' {qf} GROUP BY flag ORDER BY questions DESC, flag",
        "qall": f"SELECT q.asked_at AS asked_at, q.version AS version, q.run_id AS run_id, q.topic_label AS topic_label, "
                f"q.header AS header, q.question AS question, q.outcome AS outcome, "
                f"coalesce(q.typed, q.answer, q.reply, q.feedback) AS answer, round(q.wait_ms / 1000.0) AS wait_s, "
                f"q.flags AS flags FROM {d}.questions AS q WHERE q.skill = {SKILL} {qf} ORDER BY asked_at DESC, q.qid",
        # Skill files & CLI
        "files": picked + f"SELECT f.path AS path, f.version AS version, {shown}, max(nv.runs) AS runs, {whole}, "
                          f"{median('f.coverage')} AS median_coverage, {median('f.read_order')} AS median_order "
                          f"{files_from}WHERE f.skill = {SKILL} AND f.owner = {SKILL} GROUP BY f.path, f.version "
                          f"ORDER BY path, version",
        "clidocs": picked + f"SELECT concat(f.owner, ':', f.path) AS doc, f.version AS version, {shown}, "
                            f"max(nv.runs) AS runs, {whole}, {median('f.coverage')} AS median_coverage "
                            f"{files_from}WHERE f.skill = {SKILL} AND f.owner != {SKILL} "
                            f"GROUP BY concat(f.owner, ':', f.path), f.version ORDER BY runs_shown DESC, doc, version",
        "cli": f"SELECT c.signature AS signature, count() AS uses, countIf(c.status = 'error') AS failed, "
               f"round(countIf(c.status = 'error') / count(), 3) AS failure_rate, countIf(c.is_help) AS help_lookups, "
               f"uniqExact(c.session_id, c.run_id) AS runs FROM {d}.cli_calls AS c {runs_join.format(a='c')} "
               f"WHERE r.skill = {SKILL} AND c.signature LIKE '% %' {rf} "
               f"GROUP BY c.signature HAVING count() >= 2 ORDER BY uses DESC, signature LIMIT 40",
        "toolerrors": f"SELECT t.at AS at, r.version AS version, t.run_id AS run_id, t.tool AS tool, t.program AS program, "
                      f"t.error_category AS error_category, t.input AS input, leftUTF8(t.error, 300) AS error "
                      f"FROM {d}.tool_calls AS t {runs_join.format(a='t')} "
                      f"WHERE r.skill = {SKILL} AND t.status = 'error' {rf} ORDER BY t.at DESC, t.tool_use_id LIMIT 200",
        # Overview
        "runs": f"SELECT count() AS runs FROM {d}.skill_runs AS r WHERE r.skill = {SKILL} {rf}",
        "cost": f"SELECT round(sum(r.cost_usd), 2) AS cost_usd FROM {d}.skill_runs AS r WHERE r.skill = {SKILL} {rf}",
        "hours": f"SELECT round(sum(r.active_ms) / 3600000.0, 1) AS active_hours FROM {d}.skill_runs AS r "
                 f"WHERE r.skill = {SKILL} {rf}",
        "err": f"SELECT sum(r.tool_errors) / nullIf(sum(r.tool_calls), 0) AS error_rate FROM {d}.skill_runs AS r "
               f"WHERE r.skill = {SKILL} {rf}",
        "loaded": f"SELECT max(w.loaded_at) AS loaded_at FROM {d}.warehouse_load AS w",
        "perrun": f"WITH s AS (SELECT r.* FROM {d}.skill_runs AS r WHERE r.skill = {SKILL} {rf})\n"
                  f"SELECT m.1 AS ord, m.2 AS metric, round(avg(m.3), 2) AS average, "
                  f"round({median('m.3')}, 2) AS median, round(min(m.3), 2) AS minimum, round(max(m.3), 2) AS maximum\n"
                  f"FROM s ARRAY JOIN [\n  {per_run}] AS m\nGROUP BY ord, metric ORDER BY ord",
        "costrun": f"SELECT concat(formatDateTime(r.start_at, '%m-%d %H:%i'), ' · ', left(r.run_id, 8)) AS run, "
                   f"r.version AS version, round(r.cost_usd, 2) AS cost_usd FROM {d}.skill_runs AS r "
                   f"WHERE r.skill = {SKILL} {rf} ORDER BY r.start_at",
        "costprompt": f"SELECT ifNull(r.prompt_key, {NO_PROMPT}) AS prompt, round(sum(r.cost_usd), 2) AS cost_usd "
                      f"FROM {d}.skill_runs AS r WHERE r.skill = {SKILL} {rf} GROUP BY ifNull(r.prompt_key, {NO_PROMPT}) "
                      f"ORDER BY cost_usd DESC, prompt LIMIT 12",
        "modelversion": f"SELECT r.version AS version, a.model AS model, {dec('sum(a.cost_usd)', 2)} AS cost_usd "
                        f"{in_runs} {rp} GROUP BY r.version, a.model "
                        f"ORDER BY min(r.version_date) ASC NULLS LAST, version, model",
        "models": f"SELECT a.model AS model, count() AS requests, uniqExact(r.run_id, r.session_id) AS runs, {usage}, "
                  f"{dec('sum(a.cost_usd) / nullIf(sum(sum(a.cost_usd)) OVER (), 0)', 3)} AS share_of_cost, "
                  f"{dec(median('a.latency_ms'), 0)} AS p50_latency_ms "
                  f"{in_runs} {rf} GROUP BY a.model ORDER BY cost_usd DESC NULLS LAST, model",
        "tools": f"SELECT t.tool AS tool, count() AS calls, countIf(t.status = 'error') AS errors, "
                 f"{error_rate} AS error_rate, "
                 f"countIf(t.status = 'denied') AS denied, {dec(median('t.duration_ms'), 0)} AS p50_ms "
                 f"FROM {d}.tool_calls AS t {runs_join.format(a='t')} WHERE r.skill = {SKILL} {rf} "
                 f"GROUP BY t.tool, t.category ORDER BY calls DESC, tool, t.category LIMIT 25",
        "everyrun": f"SELECT r.start_at AS start_at, r.version AS version, ifNull(r.prompt_key, {NO_PROMPT}) AS prompt, "
                    f"coalesce(nullIf(extract(ifNull(r.prompt, ''), 'https?://([^/[:space:]]+)'), ''), "
                    f"nullIf(extract(ifNull(r.prompt, ''), '(localhost:[0-9]+)'), '')) AS instance, r.mode AS mode, "
                    f"{dec('r.active_ms / 60000.0', 1)} AS active_minutes, r.cost_usd AS cost_usd, "
                    f"r.tool_calls AS tool_calls, r.tool_errors AS tool_errors, r.help_lookups AS help_lookups, "
                    f"r.questions_asked AS questions_asked, r.prose_questions AS prose_questions, "
                    f"r.checks_passed AS checks_passed, r.checks_failed AS checks_failed, "
                    f"r.objects_created AS objects_created, r.end_reason AS end_reason, r.run_id AS run_id, "
                    f"{session_label('s', 'clickhouse')} AS session, r.person AS person FROM {d}.skill_runs AS r "
                    f"LEFT JOIN {d}.sessions AS s ON s.session_id = r.session_id AND s.source = r.source "
                    f"WHERE r.skill = {SKILL} {rf} ORDER BY start_at DESC",
        # Session
        "sessions": f"SELECT {session_label('s', 'clickhouse')} AS session, s.person AS person, s.start_at AS start_at, "
                    f"s.project AS project, s.title AS title, {dec('s.active_ms / 60000.0', 1)} AS active_minutes, "
                    f"s.cost_usd AS cost_usd, s.turns AS turns, s.prompts AS prompts, s.api_requests AS api_requests, "
                    f"s.tool_calls AS tool_calls, s.tool_errors AS tool_errors, s.subagents AS subagents, "
                    f"s.compactions AS compactions, count() AS runs, "
                    f"arrayStringConcat(arraySort(groupUniqArray(r.version)), ', ') AS versions "
                    f"FROM {d}.sessions AS s INNER JOIN {d}.skill_runs AS r "
                    f"ON r.session_id = s.session_id AND r.source = s.source WHERE r.skill = {SKILL} {rf} "
                    f"[[AND s.session_id IN (SELECT ss.session_id FROM {d}.sessions AS ss "
                    f"WHERE {session_label('ss', 'clickhouse')} = {SESSION})]] "
                    f"GROUP BY s.session_id, s.person, s.start_at, s.project, s.title, s.active_ms, s.cost_usd, s.turns, "
                    f"s.prompts, s.api_requests, s.tool_calls, s.tool_errors, s.subagents, s.compactions "
                    f"ORDER BY start_at DESC",
        "smodels": f"SELECT a.model AS model, a.scope AS scope, count() AS requests, {usage}, "
                   f"{dec(median('a.latency_ms'), 0)} AS p50_latency_ms FROM {d}.api_requests AS a "
                   f"WHERE {in_sessions('a.session_id', 'clickhouse', d)} GROUP BY a.model, a.scope "
                   f"ORDER BY cost_usd DESC NULLS LAST, model, scope",
        "sturncost": f"SELECT a.turn AS turn, a.model AS model, {dec('sum(a.cost_usd)', 2)} AS cost_usd "
                     f"FROM {d}.api_requests AS a WHERE {in_sessions('a.session_id', 'clickhouse', d)} "
                     f"AND a.turn IS NOT NULL GROUP BY a.turn, a.model ORDER BY turn, model",
        "sturns": f"SELECT t.turn AS turn, t.start_at AS start_at, t.trigger AS trigger, "
                  f"leftUTF8(coalesce(t.prompt, t.command), 200) AS prompt, {dec('t.duration_ms / 1000.0', 0)} AS seconds, "
                  f"t.requests AS requests, t.tool_calls AS tool_calls, t.tool_errors AS tool_errors, "
                  f"{dec('t.cost_usd', 2)} AS cost_usd, t.max_context_tokens AS max_context_tokens, "
                  f"t.skills_invoked AS skills_invoked, t.interrupted AS interrupted, t.compacted AS compacted "
                  f"FROM {d}.turns AS t WHERE {in_sessions('t.session_id', 'clickhouse', d)} "
                  f"ORDER BY start_at, turn LIMIT 1000",
        "sruns": f"SELECT r.start_at AS start_at, r.run_id AS run_id, r.skill AS skill, r.version AS version, "
                 f"r.mode AS mode, {dec('r.active_ms / 60000.0', 1)} AS active_minutes, r.cost_usd AS cost_usd, "
                 f"r.tool_calls AS tool_calls, r.tool_errors AS tool_errors, r.questions_asked AS questions_asked, "
                 f"r.checks_passed AS checks_passed, r.checks_failed AS checks_failed, "
                 f"r.objects_created AS objects_created, r.end_reason AS end_reason FROM {d}.skill_runs AS r "
                 f"WHERE {in_sessions('r.session_id', 'clickhouse', d)} ORDER BY start_at, run_id",
        "squestions": f"SELECT q.asked_at AS asked_at, q.skill AS skill, q.channel AS channel, q.topic_label AS topic_label, "
                      f"q.header AS header, q.question AS question, q.outcome AS outcome, "
                      f"coalesce(q.typed, q.answer, q.reply, q.feedback) AS answer, round(q.wait_ms / 1000.0) AS wait_s, "
                      f"q.flags AS flags FROM {d}.questions AS q WHERE {in_sessions('q.session_id', 'clickhouse', d)} "
                      f"ORDER BY asked_at, q.qid",
        "ssubagents": f"SELECT g.start_at AS start_at, g.kind AS kind, g.agent_type AS agent_type, "
                      f"g.description AS description, {dec('g.duration_ms / 1000.0', 0)} AS seconds, g.requests AS requests, "
                      f"g.tool_calls AS tool_calls, g.tool_errors AS tool_errors, {dec('g.cost_usd', 2)} AS cost_usd, "
                      f"g.models AS models FROM {d}.subagents AS g WHERE {in_sessions('g.session_id', 'clickhouse', d)} "
                      f"ORDER BY start_at, g.agent_id",
        "stools": f"SELECT t.at AS at, t.turn AS turn, t.scope AS scope, t.tool AS tool, t.program AS program, "
                  f"t.status AS status, {dec('t.duration_ms / 1000.0', 1)} AS seconds, t.run_id AS run_id, "
                  f"t.input AS input, leftUTF8(t.error, 300) AS error FROM {d}.tool_calls AS t "
                  f"WHERE {in_sessions('t.session_id', 'clickhouse', d)} ORDER BY at, t.tool_use_id LIMIT 2000",
        # Question topics
        "tq_stack": f"SELECT countIf(q.layer != 'cross-cutting') / nullIf(count(), 0) AS on_the_stack "
                    f"FROM {d}.questions AS q WHERE q.skill = {SKILL} {qf}",
        "tq_never": f"SELECT if(count() = 0, 'none', arrayStringConcat(arrayMap(x -> x.2, "
                    f"arraySort(groupArray((l.sort_order, ifNull(l.label, ''))))), ', ')) AS never_asked "
                    f"FROM {layer_rows('clickhouse')} WHERE l.id != 'cross-cutting' AND l.id NOT IN "
                    f"(SELECT q.layer FROM {d}.questions AS q WHERE q.skill = {SKILL} AND q.layer IS NOT NULL {qf})",
        "tq_fallback": f"SELECT countIf(q.semantics_by LIKE '%fallback%' OR q.de_topic = 'other') / nullIf(count(), 0) "
                       f"AS by_fallback FROM {d}.questions AS q WHERE q.skill = {SKILL} {qf}",
        "tq_matrix": crosstab_sql("clickhouse", d),
        "tq_versions": f"WITH runs AS (SELECT r.version AS version, max(r.version_date) AS version_date, "
                       f"count() AS runs FROM {d}.skill_runs AS r WHERE r.skill = {SKILL} {rp} GROUP BY r.version)\n"
                       f"SELECT u.version AS version, l.label AS layer, "
                       f"round(countIf(q.qid != '') / u.runs, 2) AS questions_per_run\n"
                       f"FROM runs AS u CROSS JOIN {layer_rows('clickhouse')}\n"
                       f"LEFT JOIN (SELECT x.qid AS qid, x.version AS version, x.layer AS layer FROM {d}.questions AS x "
                       f"WHERE x.skill = {SKILL} [[AND x.de_topic_label = {TOPIC}]] "
                       f"{question_filters(f'{d}.skill_runs', 'x', version=False, person=True)}) AS q "
                       f"ON q.version = u.version AND q.layer = l.id\n"
                       f"GROUP BY u.version, u.version_date, u.runs, l.label, l.sort_order\n"
                       f"ORDER BY u.version_date ASC NULLS LAST, l.sort_order",
    }


# ---------------------------------------------------------------- Metabase
def mb(*args, body=None):
    cmd = ["mb", *args, "--profile", PROFILE, "--json", "--max-bytes", "0"]
    if body is not None:
        f = WORK / f"body-{uuid.uuid4().hex[:8]}.json"
        f.write_text(json.dumps(body))
        cmd += ["--file", str(f)]
    for attempt in range(NETWORK_TRIES):
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0 or '"category":"network"' not in res.stdout + res.stderr:
            break
        time.sleep(2 ** attempt)  # the Metabase was unreachable: every call here is safe to repeat
    if res.returncode != 0:
        raise SystemExit(f"mb {' '.join(args)} failed:\n{res.stderr or res.stdout}")
    out = json.loads(res.stdout)
    return out.get("data", out) if isinstance(out, dict) and "id" not in out else out


def lit(s):
    return "'" + s.replace("'", "''") + "'"


def native_query(sql, skill):
    """A native card's query: the skill as a literal, a template tag per filter the SQL names."""
    tags = {f: {"id": str(uuid.uuid4()), "name": f, "display-name": label, "type": "text"}
            for f, label in FILTER_NAMES.items() if f"{{{{{f}}}}}" in sql}
    return {"lib/type": "mbql/query", "database": DB,
            "stages": [{"lib/type": "mbql.stage/native", "native": sql.replace(SKILL, lit(skill)),
                        "template-tags": tags}]}


def _id(res):
    return res.get("id") or res["data"]["id"]


def upsert_card(existing, body, skip_validate=False, create_extra=None):
    """Update the card of that name and kind (keeping its id, its dashcards and whatever links to it), or create it.
    `create_extra` only applies to a new card (dashboard_id: a question that lives in its dashboard)."""
    kind = {"model": "dataset", "metric": "metric"}.get(body.get("type"), "card")
    cid = existing.get((kind, body["name"]))
    flags = ["--skip-validate"] if skip_validate else []
    if cid:
        mb("card", "update", str(cid), *flags, body=body)
        print(f"{body.get('type', 'card')} {cid} updated: {body['name']}")
        return cid
    cid = _id(mb("card", "create", *flags, body=dict(body, **(create_extra or {}))))
    existing[(kind, body["name"])] = cid
    print(f"{body.get('type', 'card')} {cid} created: {body['name']}")
    return cid


def collection_items(col_id):
    return {(i["model"], i["name"]): i["id"] for i in mb("collection", "items", str(col_id), "--limit", "1000")
            if isinstance(i, dict) and not i.get("archived")}


def _resolve(node, types, metric_ids, agg_uuids):
    """Fill in a topic card's MBQL: model columns get their real base type, ("metric", key) the metric clause, and
    the order-by shorthands their clauses."""
    if isinstance(node, tuple):
        op, key = node
        if op == "metric":
            u = agg_uuids.setdefault(key, str(uuid.uuid4()))
            name = next(n for k, n, *_ in METRICS if k == key)
            return ["metric", {"lib/uuid": u, "name": key, "display-name": name}, metric_ids[key]]
        if op == "desc-aggregation":
            return ["desc", {}, ["aggregation", {}, agg_uuids[key]]]
        return [op, {}, _resolve(col(key), types, metric_ids, agg_uuids)]
    if isinstance(node, list):
        if len(node) == 3 and node[0] == "field" and isinstance(node[2], str):
            return ["field", {**node[1], "base-type": types[node[2]]}, node[2]]
        return [_resolve(x, types, metric_ids, agg_uuids) for x in node]
    if isinstance(node, dict):
        # aggregations first, so an order-by can point at their lib/uuid
        keys = sorted(node, key=lambda k: k != "aggregation")
        return {k: _resolve(node[k], types, metric_ids, agg_uuids) for k in keys}
    return node


def topic_stage(q, skill, types, metric_ids):
    """A Question topics MBQL card's stage on the model: its query, narrowed to the skill's questions."""
    stage = _resolve(q, types, metric_ids, {})
    return {**stage, "filters": [["=", {}, ["field", {"base-type": types["skill"]}, "skill"], skill]]}


def _parameters(values_from, person):
    """The dashboard filters, in FILTER_NAMES order: values from a card's column of the same name (values_from: filter
    -> card id), or the taxonomy's labels. Person only where the warehouse has the column (ClickHouse)."""
    topics, layers = semantics.load().dimensions()
    static = {"de_topic": [t["label"] for t in topics], "layer": [lay["label"] for lay in layers]}
    out = []
    for f, label in FILTER_NAMES.items():
        if f == "person" and not person:
            continue
        source = ({"values_source_type": "card", "values_source_config": {
                      "card_id": values_from[f], "value_field": ["field", f, {"base-type": "type/Text"}]}}
                  if f in values_from else
                  {"values_source_type": "static-list", "values_source_config": {"values": static[f]}})
        out.append({"id": f, "name": label, "slug": f, "type": "string/=", "isMultiSelect": False, **source})
    return out


def _sets(column, parameter):
    return {"id": parameter, "source": {"type": "column", "id": column, "name": column},
            "target": {"type": "parameter", "id": parameter}}


def crossfilter(column, parameter):
    """Dashcard settings: clicking a cell of `column` sets the filter `parameter` to it."""
    return {"column_settings": {f'["name","{column}"]': {"click_behavior": {
        "type": "crossfilter", "parameterMapping": {parameter: _sets(column, parameter)}}}}}


def open_session(dashboard_id, tab_id):
    """Dashcard settings: clicking a run's session opens the Session tab on it."""
    return {"column_settings": {'["name","session"]': {"click_behavior": {
        "type": "link", "linkType": "dashboard", "targetId": dashboard_id, "tabId": tab_id,
        "parameterMapping": {"session": _sets("session", "session")}}}}}


def _kept_text_cards(dash):
    """Dashcards without a card (text, headings, links) someone added by hand, and per tab how many rows they take
    at its top, so the tab's cards start below them."""
    kept = [dc for dc in dash.get("dashcards") or () if not dc.get("card_id")]
    top = {}
    for dc in sorted(kept, key=lambda d: d["row"]):
        t = dc.get("dashboard_tab_id")
        if dc["row"] <= top.get(t, 0):
            top[t] = max(top.get(t, 0), dc["row"] + dc["size_y"])
    return kept, top


def sync(col_id, name, skill, dialect, ch_db, verify=True):
    """The whole dashboard in collection `col_id`: created, or updated in place. The model and metrics live in the
    collection; a card that does not exist yet is created inside the dashboard (a dashboard question), so the
    collection lists only what someone would open on its own."""
    existing = collection_items(col_id)
    did = existing.get(("dashboard", name))
    if did is None:
        did = _id(mb("dashboard", "create", body={"name": name, "collection_id": col_id, "description": description(skill)}))
        print(f"dashboard {did} created: {name}")
    dash = mb("dashboard", "get", str(did), "--full")
    for dc in dash.get("dashcards") or ():
        c = dc.get("card") or {}
        if c.get("id"):
            existing.setdefault(({"model": "dataset", "metric": "metric"}.get(c.get("type"), "card"), c["name"]), c["id"])
    in_dashboard = {"dashboard_id": did}
    ch = clickhouse_sql(ch_db) if dialect == "clickhouse" else {}

    # the model and its column metadata
    model_sql = model_sql_clickhouse(ch_db) if ch else MODEL_SQL
    model_id = upsert_card(existing, {
        "name": MODEL_NAME, "type": "model", "display": "table", "description": MODEL_DESCRIPTION,
        "collection_id": col_id, "visualization_settings": {},
        "dataset_query": {"lib/type": "mbql/query", "database": DB,
                          "stages": [{"lib/type": "mbql.stage/native", "native": model_sql, "template-tags": {}}]}})
    meta = mb("card", "get", str(model_id), "--full").get("result_metadata") or []
    if not meta:
        raise SystemExit(f"model {model_id} has no result_metadata: run it once in Metabase, then re-run this")
    if not {c["name"] for c in meta} >= set(MODEL_FILTER_COLUMNS.values()):
        # the view gained a column since the model was saved, and saving the same query keeps the old metadata:
        # take what a fresh run reports
        meta = mb("card", "query", str(model_id), "--limit", "1", "--full")["results_metadata"]["columns"]
    for c in meta:
        display, desc, semantic_type = MODEL_COLUMNS.get(c["name"], (None, None, None))
        c.update({k: v for k, v in (("display_name", display), ("description", desc),
                                    ("semantic_type", semantic_type)) if v})
    mb("card", "update", str(model_id), body={"result_metadata": meta})
    types = {c["name"]: c["base_type"] for c in meta}

    metric_ids = {}
    for key, mname, desc, agg in METRICS:
        metric_ids[key] = upsert_card(existing, {
            "name": mname, "type": "metric", "display": "scalar", "description": desc, "collection_id": col_id,
            "visualization_settings": {},
            "dataset_query": {"lib/type": "mbql/query", "database": DB,
                              "stages": [{"lib/type": "mbql.stage/mbql", "source-card": model_id,
                                          "aggregation": [_resolve(agg, types, {}, {})]}]}})

    # every card: (key, card id, tab, position, parameter mappings, dashcard visualization settings)
    placed = []
    for key, tab, cname, display, sql, vis, _filters, pos in [*CARDS, check_grid(dialect, ch_db, skill)]:
        sql = sql if key == "checkgrid" else ch.get(key, sql)
        cid = upsert_card(existing, {"name": cname, "display": display, "visualization_settings": vis,
                                     "collection_id": col_id, "dataset_query": native_query(sql, skill)},
                          create_extra=in_dashboard)
        maps = [{"parameter_id": f, "card_id": cid, "target": ["variable", ["template-tag", f]]}
                for f in FILTER_NAMES if f"{{{{{f}}}}}" in sql]
        # Clicking a session in the Session tab's list picks it.
        placed.append((key, cid, tab, pos, maps, crossfilter("session", "session") if key == "sessions" else {}))
    for key, cname, display, (kind, q), vis, filters, pos in topic_cards(dialect, ch_db):
        if kind == "sql":
            dq = native_query(q, skill)
        else:
            dq = {"lib/type": "mbql/query", "database": DB,
                  "stages": [{"lib/type": "mbql.stage/mbql", "source-card": model_id,
                              **topic_stage(q, skill, types, metric_ids)}]}
        # The CLI's bundled MBQL schema wants a metric's entity id in ["metric", {}, id]; the server takes only its
        # numeric id. So a card that uses a metric skips the CLI's pre-flight check; the server still checks it.
        cid = upsert_card(existing, {"name": cname, "display": display, "visualization_settings": vis,
                                     "collection_id": col_id, "dataset_query": dq},
                          skip_validate=kind == "mbql", create_extra=in_dashboard)
        if kind == "sql":
            maps = [{"parameter_id": f, "card_id": cid, "target": ["variable", ["template-tag", f]]}
                    for f in FILTER_NAMES if f"{{{{{f}}}}}" in q]
        else:
            model_filters = [*filters, *(["person"] if "person" in types else [])]
            maps = [{"parameter_id": f, "card_id": cid, "target": ["dimension", [
                "field", MODEL_FILTER_COLUMNS[f], {"base-type": types[MODEL_FILTER_COLUMNS[f]]}], {"stage-number": 0}]}
                for f in model_filters]
        # Clicking a topic in the matrix sets the topic filter: the cards below narrow to it.
        placed.append((key, cid, TOPIC_TAB, pos, maps, crossfilter("de_topic", "de_topic") if key == "tq_matrix" else {}))

    # the layout: re-read, since a new dashboard question arrives with a dashcard of its own
    card_of = {key: cid for key, cid, *_ in placed}
    params = _parameters({f: card_of[k] for f, k in VALUES_CARDS.items()}, person=dialect == "clickhouse")
    while True:
        dash = mb("dashboard", "get", str(did), "--full")
        tab_id = {t["name"]: t["id"] for t in dash.get("tabs") or ()}
        # a run's session opens the Session tab, once that tab has an id
        link = open_session(did, tab_id[SESSION_TAB]) if SESSION_TAB in tab_id else {}
        tabs = [{"id": tab_id.get(t, -(i + 1)), "name": t, "position": i} for i, t in enumerate(ALL_TABS)]
        tab_id = {t["name"]: t["id"] for t in tabs}
        dashcard_of = {dc["card_id"]: dc["id"] for dc in dash.get("dashcards") or () if dc.get("card_id")}
        kept, top = _kept_text_cards(dash)
        kept = [dc for dc in kept if dc.get("dashboard_tab_id") in tab_id.values()]
        dashcards = [{"id": dashcard_of.get(cid, -n), "card_id": cid, "dashboard_tab_id": tab_id[tab], "col": x,
                      "row": y + top.get(tab_id[tab], 0), "size_x": w, "size_y": h, "parameter_mappings": maps,
                      "visualization_settings": link if key == "everyrun" else vis, "series": []}
                     for n, (key, cid, tab, (x, y, w, h), maps, vis) in enumerate(placed, 1)]
        dashcards += [{k: dc[k] for k in ("id", "card_id", "dashboard_tab_id", "col", "row", "size_x", "size_y",
                                          "visualization_settings")} | {"parameter_mappings": [], "series": []}
                      for dc in kept]
        mb("dashboard", "update", str(did), body={"description": description(skill), "tabs": tabs,
                                                  "dashcards": dashcards, "parameters": params})
        if link:
            break
    print(f"dashboard {did}: {len(dashcards)} cards on {len(tabs)} tabs ({len(kept)} text cards kept); "
          f"model {model_id}, metrics {', '.join(str(i) for i in metric_ids.values())}")
    if verify:
        bad = 0
        for cid in [model_id, *metric_ids.values(), *(p[1] for p in placed)]:
            res = subprocess.run(["mb", "card", "query", str(cid), "--profile", PROFILE, "--json", "--limit", "1"],
                                 capture_output=True, text=True)
            try:
                out = json.loads(res.stdout)
            except ValueError:
                out = {"ok": False, "error": res.stderr or res.stdout}
            if res.returncode != 0 or out.get("ok") is False or out.get("status") not in (None, "completed"):
                bad += 1
                print(f"  FAIL card {cid}: {str(out.get('error') or out)[:300]}")
        print(f"verified: {len(placed) + len(metric_ids) + 1 - bad} cards ran, {bad} failed")
        return bad
    return 0


# ---------------------------------------------------------------- --test: every card's SQL, against the warehouse
def psql(sql):
    res = subprocess.run(["docker", "exec", "-i", "convo-analysis-pg", "psql", "-U", "convo", "-d", "claude_sessions",
                          "-v", "ON_ERROR_STOP=1", "-A", "-F", "\t", "-c", sql], capture_output=True, text=True)
    return res.returncode, (res.stdout if res.returncode == 0 else res.stderr).strip()


def _test_sql(sql, skill):
    """The card's SQL as Metabase runs it with every filter off."""
    return re.sub(r"\[\[.*?\]\]", "", sql, flags=re.S).replace(SKILL, lit(skill))


def test(dialect="postgres", env_file=None, skill="rde"):
    if dialect == "clickhouse":
        from session_analytics import clickhouse
        client = clickhouse.Client(clickhouse.target_from_settings(env_file))
        db = client.t.database
        ch = clickhouse_sql(db)
        sqls = [(key, name, ch[key]) for key, _tab, name, *_ in CARDS]
        sqls.append(("checkgrid", "Checks, run by run", check_grid("clickhouse", db, skill)[4]))
        sqls.append(("model", MODEL_NAME, f"SELECT * FROM ({model_sql_clickhouse(db)}) AS m WHERE m.skill = {SKILL}"))
        sqls += [(key, name, q[1]) for key, name, _display, q, *_ in topic_cards("clickhouse", db) if q[0] == "sql"]

        def run(sql):
            try:
                rows = client.rows(sql)
            except clickhouse.ClickHouseError as exc:
                return 1, str(exc)
            head = "\t".join(rows[0]) if rows else ""
            return 0, "\n".join([head] + ["\t".join(str(v) for v in r.values()) for r in rows] + ["(end)"])
    else:
        sqls = [(key, name, sql) for key, _tab, name, _display, sql, *_ in CARDS]
        sqls.append(("checkgrid", "Checks, run by run", check_grid("postgres", None, skill)[4]))
        sqls.append(("model", MODEL_NAME, f"SELECT * FROM ({MODEL_SQL}) m WHERE m.skill = {SKILL}"))
        sqls += [(key, name, q[1]) for key, name, _display, q, *_ in topic_cards() if q[0] == "sql"]
        run = psql
    bad = 0
    for key, name, sql in sqls:
        code, out = run(_test_sql(sql, skill))
        lines = out.splitlines()
        rows = max(0, len(lines) - 2) if code == 0 else 0
        status = "ok " if code == 0 and rows else ("EMPTY" if code == 0 else "FAIL")
        bad += status == "FAIL"
        print(f"{status} {key:<12} {rows:>4} rows  {name}")
        if code != 0:
            print("      ", out[:400])
        elif rows:
            print("      ", lines[0][:150], "|", lines[1][:150])
    return bad


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--test", action="store_true", help="run every card's SQL against the warehouse")
    ap.add_argument("--clickhouse", action="store_true", help="with --test: the ClickHouse warehouse (.env)")
    ap.add_argument("--env-file", help="with --test --clickhouse: read CLICKHOUSE_URL from this file")
    ap.add_argument("--sync", action="store_true", help="create or update the dashboard, model and metrics in Metabase")
    ap.add_argument("--skill", default="rde", help="the skill the dashboard evaluates (default rde)")
    ap.add_argument("--profile", help="mb CLI profile of the target Metabase (required with --sync)")
    ap.add_argument("--database", type=int, help="the warehouse's database id in that Metabase (required with --sync)")
    ap.add_argument("--collection", type=int, help="the collection the dashboard lives in (required with --sync)")
    ap.add_argument("--name", help="the dashboard's name, found by it in the collection (default: <skill> skill evaluation)")
    ap.add_argument("--ch-database", default="sessions", help="ClickHouse database holding the tables (default sessions)")
    ap.add_argument("--no-verify", action="store_true", help="with --sync: skip running every card afterwards")
    args = ap.parse_args()
    if not re.fullmatch(r"[\w.:-]+", args.skill):
        ap.error("--skill must be a skill name")
    if args.test:
        sys.exit(1 if test("clickhouse" if args.clickhouse else "postgres", args.env_file, args.skill) else 0)
    if args.sync:
        if not (args.profile and args.database and args.collection):
            ap.error("--sync needs --profile, --database and --collection")
        PROFILE, DB = args.profile, args.database
        engine = mb("database", "get", str(DB)).get("engine")
        if engine not in ("postgres", "clickhouse"):
            ap.error(f"database {DB} is {engine!r}: the cards are written for postgres and clickhouse")
        if engine == "clickhouse" and not re.fullmatch(r"\w+", args.ch_database):
            ap.error("--ch-database must be a plain identifier")
        name = args.name or f"{args.skill} skill evaluation"
        sys.exit(1 if sync(args.collection, name, args.skill, engine, args.ch_database, verify=not args.no_verify) else 0)
    ap.print_help()
