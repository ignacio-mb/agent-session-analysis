"""Build the "Claude Code sessions" Metabase dashboard on the session warehouse (warehouse.py, clickhouse.py).

    python3 scripts/metabase_dashboard.py --test [--clickhouse]
    python3 scripts/metabase_dashboard.py --sync --profile <mb profile> --database <id> --collection <id>

--test runs every card's SQL against the warehouse: the local Postgres (docker exec psql), or with --clickhouse the
ClickHouse in the checkout's .env. --sync creates or updates, in the collection: the dashboard (five tabs —
Overview, Skill versions, Interview, Question topics, Skill files & CLI — and Skill, Data-engineering topic and
Layer filters), its cards (native SQL on the warehouse's views; a new card is created inside the dashboard), and the
semantic layer the Question topics tab reads. Everything is found by name, so a second --sync updates in place and
keeps every id. The SQL dialect follows the Metabase database's engine (postgres or clickhouse; --ch-database names
the ClickHouse database, default sessions). Afterwards every card is run once through Metabase (--no-verify skips).

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
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from session_analytics import semantics  # noqa: E402 - the topics and layers the questions are mapped to

PROFILE = None
DB = None
WORK = Path(tempfile.mkdtemp(prefix="claude-sessions-dashboard-"))

SKILL = "{{skill}}"
BUILD_VERSION_ORDER = "ORDER BY version_date NULLS LAST"

# Every conversation (a session with at least one API request): average and median of each metric, all time and
# the last 30 days, one row per metric.
AVG_PER_CONVERSATION = """WITH s AS (SELECT sessions.*, start_at >= now() - interval '30 days' AS recent,
       (SELECT count(*) FROM questions q WHERE q.session_id = sessions.session_id AND q.channel = 'ask') AS asked
  FROM sessions WHERE api_requests > 0),
m AS (
  SELECT 1 AS ord, 'Estimated cost (USD)' AS metric, cost_usd::numeric AS v, recent FROM s
  UNION ALL SELECT 2, 'Turns', turns, recent FROM s
  UNION ALL SELECT 3, 'Prompts you typed', prompts, recent FROM s
  UNION ALL SELECT 4, 'Claude API requests', api_requests, recent FROM s
  UNION ALL SELECT 5, 'Tool calls', tool_calls, recent FROM s
  UNION ALL SELECT 6, 'Failed tool calls', tool_errors, recent FROM s
  UNION ALL SELECT 7, 'Active minutes', active_ms / 60000.0, recent FROM s
  UNION ALL SELECT 8, 'Wall-clock minutes', wall_ms / 60000.0, recent FROM s
  UNION ALL SELECT 9, 'Output tokens', output_tokens, recent FROM s
  UNION ALL SELECT 10, 'Input tokens incl. cache', input_tokens + cache_read_tokens + cache_write_tokens, recent FROM s
  UNION ALL SELECT 11, 'Peak context tokens', peak_context_tokens, recent FROM s
  UNION ALL SELECT 12, 'Subagents', subagents, recent FROM s
  UNION ALL SELECT 13, 'Skills invoked', skills_invoked, recent FROM s
  UNION ALL SELECT 14, 'Skill runs', skill_runs, recent FROM s
  UNION ALL SELECT 15, 'Questions asked (AskUserQuestion)', asked, recent FROM s
  UNION ALL SELECT 16, 'Questions asked in prose', questions_prose, recent FROM s
  UNION ALL SELECT 17, 'Files modified', files_modified, recent FROM s
  UNION ALL SELECT 18, 'Lines added', lines_added, recent FROM s
  UNION ALL SELECT 19, 'Commits', commits, recent FROM s
  UNION ALL SELECT 20, 'Compactions', compactions, recent FROM s
  UNION ALL SELECT 21, 'Interruptions', interruptions, recent FROM s
)
SELECT ord, metric,
       round(avg(v), 2) AS average,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY v))::numeric, 2) AS median,
       round(avg(v) FILTER (WHERE recent), 2) AS average_last_30_days,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY v) FILTER (WHERE recent))::numeric, 2) AS median_last_30_days
FROM m GROUP BY ord, metric ORDER BY ord"""

# (key, tab, name, display, sql, visualization_settings, uses_skill, (col, row, w, h))
CARDS = [
    # ---------------------------------------------------------------- Overview
    ("sessions30", "Overview", "Sessions, last 30 days", "scalar",
     "SELECT count(*) AS sessions FROM sessions WHERE start_at >= now() - interval '30 days'",
     {"scalar.field": "sessions"}, False, (0, 0, 5, 3)),
    ("cost30", "Overview", "Estimated cost, last 30 days", "scalar",
     "SELECT round(sum(cost_usd), 2) AS cost_usd FROM sessions WHERE start_at >= now() - interval '30 days'",
     {"scalar.field": "cost_usd", "column_settings": {'["name","cost_usd"]': {"number_style": "currency", "currency": "USD"}}},
     False, (5, 0, 5, 3)),
    ("hours30", "Overview", "Active hours, last 30 days", "scalar",
     "SELECT round(sum(active_ms) / 3600000.0, 1) AS active_hours FROM sessions WHERE start_at >= now() - interval '30 days'",
     {"scalar.field": "active_hours"}, False, (10, 0, 5, 3)),
    ("err30", "Overview", "Tool calls that failed, last 30 days", "scalar",
     "SELECT sum(tool_errors)::numeric / nullif(sum(tool_calls), 0) AS error_rate FROM sessions "
     "WHERE start_at >= now() - interval '30 days'",
     {"scalar.field": "error_rate", "column_settings": {'["name","error_rate"]': {"number_style": "percent", "decimals": 1}}},
     False, (15, 0, 5, 3)),
    ("loaded", "Overview", "Data as of", "scalar",
     "SELECT max(loaded_at) AS loaded_at FROM warehouse_load", {"scalar.field": "loaded_at"}, False, (20, 0, 4, 3)),
    ("perconv", "Overview", "Average per conversation", "table",
     AVG_PER_CONVERSATION,
     {"table.columns": [{"name": "ord", "enabled": False}] + [{"name": c, "enabled": True} for c in (
         "metric", "average", "median", "average_last_30_days", "median_last_30_days")]}, False, (0, 3, 12, 11)),
    ("costday", "Overview", "Estimated cost per day", "bar",
     "SELECT day, cost_usd FROM v_daily WHERE day >= current_date - 60 ORDER BY day",
     {"graph.dimensions": ["day"], "graph.metrics": ["cost_usd"],
      "column_settings": {'["name","cost_usd"]': {"number_style": "currency", "currency": "USD"}}}, False, (12, 3, 12, 11)),
    ("costproject", "Overview", "Cost by project, last 30 days", "row",
     "SELECT project, round(sum(cost_usd), 2) AS cost_usd FROM sessions WHERE start_at >= now() - interval '30 days' "
     "GROUP BY project ORDER BY cost_usd DESC, project LIMIT 12",
     {"graph.dimensions": ["project"], "graph.metrics": ["cost_usd"],
      "column_settings": {'["name","cost_usd"]': {"number_style": "currency", "currency": "USD"}}}, False, (0, 14, 12, 8)),
    ("tools", "Overview", "Tools: calls, failures and speed", "table",
     "SELECT tool, calls, errors, error_rate, denied, round(p50_ms::numeric) AS p50_ms FROM v_tools ORDER BY calls DESC, tool, category LIMIT 25",
     {"column_settings": {'["name","error_rate"]': {"number_style": "percent", "decimals": 1}}}, False, (0, 22, 24, 9)),
    ("models", "Overview", "Requests and cost by model", "table",
     "SELECT model, requests, sessions, cost_usd, round(p50_latency_ms::numeric) AS p50_latency_ms FROM v_models "
     "ORDER BY cost_usd DESC NULLS LAST",
     {"column_settings": {'["name","cost_usd"]': {"number_style": "currency", "currency": "USD"}}}, False, (12, 14, 12, 8)),
    # ---------------------------------------------------------------- Skill versions
    ("versions", "Skill versions", "Versions compared", "table",
     f"SELECT version, subject, version_date, runs, median_cost_usd, median_tool_calls, median_tool_errors, "
     f"round(median_active_minutes::numeric, 1) AS median_active_minutes, median_help_lookups, avg_questions_asked, "
     f"avg_prose_questions, avg_question_rounds, recommended_rate, typed_answers, median_docs_read, checks_failed "
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
     f"SELECT interview_topic AS topic_label, outcome_group AS outcome, count(*) AS questions FROM v_interview_questions "
     f"WHERE skill = {SKILL} GROUP BY 1, 2 ORDER BY 1",
     {"graph.dimensions": ["topic_label", "outcome"], "graph.metrics": ["questions"], "stackable.stack_type": "stacked"},
     True, (0, 3, 12, 10)),
    ("qchannel", "Interview", "Questions per run, on average, by version", "bar",
     f"SELECT version, avg_questions_asked AS askuserquestion, avg_prose_questions AS in_prose FROM v_skill_versions "
     f"WHERE skill = {SKILL} {BUILD_VERSION_ORDER}",
     {"graph.dimensions": ["version"], "graph.metrics": ["askuserquestion", "in_prose"], "stackable.stack_type": "stacked",
      "graph.show_values": True, "series_settings": {"askuserquestion": {"title": "AskUserQuestion"},
                                                      "in_prose": {"title": "in prose"}}},
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
     f"FROM cli_calls c JOIN skill_runs r USING (run_id, session_id) WHERE r.skill = {SKILL} AND c.signature LIKE '% %' "
     f"GROUP BY c.signature HAVING count(*) >= 2 ORDER BY uses DESC, c.signature LIMIT 40",
     {"column_settings": {'["name","failure_rate"]': {"number_style": "percent", "decimals": 1}}}, True, (0, 12, 12, 12)),
    ("toolerrors", "Skill files & CLI", "Failed tool calls in the skill's runs", "table",
     f"SELECT t.at, r.version, t.run_id, t.tool, t.program, t.error_category, t.input, left(t.error, 300) AS error "
     f"FROM tool_calls t JOIN skill_runs r USING (run_id, session_id) WHERE r.skill = {SKILL} AND t.status = 'error' "
     f"ORDER BY t.at DESC, t.tool_use_id LIMIT 200", {}, True, (12, 12, 12, 12)),
]

# ---------------------------------------------------------------- The semantic layer on the questions
# A model (one row per question, with its data-engineering topic and layer) and metrics defined on it, so a question
# asked in the query builder counts, rates and waits the same way the dashboard does. The Question topics tab is
# built from them; its MBQL cards drill through to the questions behind a number.
MODEL_NAME = "Interview questions"
MODEL_SQL = "SELECT * FROM v_interview_questions"
MODEL_DESCRIPTION = ("One row per question Claude put to the user — AskUserQuestion, in prose, or a printed checkpoint — "
                     "with the data-engineering topic and layer it is about and what came back. The topics and layers "
                     "are rules in semantics/questions.json (convo-analysis); the rows are reloaded when a session ends.")
# column -> (display name, description, semantic type)
MODEL_COLUMNS = {
    "qid": ("Question ID", None, "type/PK"),
    "asked_at": ("Asked at", None, "type/CreationTimestamp"),
    "skill": ("Skill", "The skill whose run asked it; empty outside a skill run.", "type/Category"),
    "version": ("Skill version", "The skill's git commit that ran.", "type/Category"),
    "version_date": ("Version date", "When that commit was made.", None),
    "version_subject": ("Version subject", "That commit's subject line.", None),
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
PERCENT = {"number_style": "percent", "decimals": 0}
TOPIC_TAB = "Question topics"


DESCRIPTION = ("How Claude Code sessions go, and how each version of a skill behaves: cost, tools, checks, the questions "
               "it asks and what they are about, the files it reads. Source: the convo-analysis session warehouse.")
ALL_TABS = ["Overview", "Skill versions", "Interview", TOPIC_TAB, "Skill files & CLI"]


def crosstab_sql(dialect="postgres", db=None):
    """Topic x layer, one column per layer in stack order (from semantics/questions.json at build time)."""
    _, layers = semantics.load().dimensions()
    if dialect == "clickhouse":
        cells = ",\n       ".join(f"countIf(q.layer = '{lay['id']}') AS {_ident(lay['id'])}" for lay in layers)
        # a topic no question matched joins a row whose key is '' (not NULL): count the matches only
        return (f"SELECT t.label AS de_topic,\n       {cells},\n       countIf(q.qid != '') AS all_layers\n"
                f"FROM {db}.de_topics AS t LEFT JOIN {db}.questions AS q ON q.de_topic = t.id AND q.skill = {SKILL}\n"
                f"GROUP BY t.label, t.sort_order ORDER BY t.sort_order")
    cells = ",\n       ".join(f"count(q.qid) FILTER (WHERE q.layer = '{lay['id']}') AS {_ident(lay['id'])}" for lay in layers)
    return (f"SELECT t.label AS de_topic,\n       {cells},\n       count(q.qid) AS all_layers\n"
            f"FROM de_topics t LEFT JOIN questions q ON q.de_topic = t.id AND q.skill = {SKILL}\n"
            f"GROUP BY t.label, t.sort_order ORDER BY t.sort_order")


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
    return [
        ("tq_total", "Questions put to the user", "scalar", ("mbql", {"aggregation": [metric("questions")]}),
         {"scalar.field": "questions"}, ("skill", "de_topic", "layer"), (0, 0, 6, 3)),
        ("tq_stack", "About a data-stack layer (not cross-cutting)", "scalar",
         native("tq_stack", f"SELECT count(*) FILTER (WHERE layer <> 'cross-cutting')::numeric / nullif(count(*), 0) "
                            f"AS on_the_stack FROM questions WHERE skill = {SKILL}"),
         {"scalar.field": "on_the_stack", "column_settings": {'["name","on_the_stack"]': PERCENT}}, ("skill",), (6, 0, 6, 3)),
        ("tq_never", "Layers the interview never asks about", "scalar",
         native("tq_never", f"SELECT coalesce(string_agg(l.label, ', ' ORDER BY l.sort_order), 'none') AS never_asked "
                            f"FROM de_layers l WHERE l.id <> 'cross-cutting' AND NOT EXISTS (SELECT 1 FROM questions q "
                            f"WHERE q.layer = l.id AND q.skill = {SKILL})"),
         {"scalar.field": "never_asked"}, ("skill",), (12, 0, 6, 3)),
        ("tq_fallback", "Placed by the fallback, not a rule", "scalar",
         native("tq_fallback", f"SELECT count(*) FILTER (WHERE semantics_by LIKE '%fallback%' OR de_topic = 'other')"
                               f"::numeric / nullif(count(*), 0) AS by_fallback FROM questions WHERE skill = {SKILL}"),
         {"scalar.field": "by_fallback", "column_settings": {'["name","by_fallback"]': PERCENT}}, ("skill",), (18, 0, 6, 3)),
        ("tq_matrix", "Where the questions land: data-engineering topic × layer", "table",
         native("tq_matrix", crosstab_sql()),
         {"column_settings": {'["name","de_topic"]': {"column_title": "Data-engineering topic"},
                              '["name","all_layers"]': {"column_title": "All layers"},
                              **{f'["name","{i}"]': {"column_title": lay["label"]} for i, lay in zip(layer_ids, layers)}},
          "table.column_formatting": [{"id": 0, "columns": layer_ids, "type": "range", "colors": ["#FFFFFF", "#509EE3"],
                                       "min_type": "all", "max_type": "all", "min_value": 0, "max_value": 100,
                                       "operator": "=", "value": "", "color": "#509EE3", "highlight_row": False}]},
         ("skill",), (0, 3, 24, 10)),
        ("tq_outcomes", "What came back, by data-engineering topic", "row",
         ("mbql", {"aggregation": [metric("questions")], "breakout": [col("de_topic"), col("outcome_group")]}),
         {"graph.dimensions": ["de_topic", "outcome_group"], "graph.metrics": ["questions"],
          "stackable.stack_type": "stacked"}, ("skill", "de_topic", "layer"), (0, 13, 12, 10)),
        ("tq_scorecard", "Each topic: how the questions went", "table",
         ("mbql", {"aggregation": [metric(k) for k, *_ in METRICS], "breakout": [col("de_topic")],
                   "order-by": [("desc-aggregation", "questions")]}),
         {"column_settings": {'["name","recommended_rate"]': PERCENT, '["name","typed_rate"]': PERCENT,
                              '["name","empty_rate"]': PERCENT, '["name","median_wait_s"]': {"decimals": 0}}},
         ("skill", "de_topic", "layer"), (12, 13, 12, 10)),
        ("tq_versions", "Questions per run, by layer and version", "bar",
         native("tq_versions",
                f"WITH runs AS (SELECT version, max(version_date) AS version_date, count(*) AS runs FROM skill_runs "
                f"WHERE skill = {SKILL} GROUP BY version)\n"
                f"SELECT r.version, l.label AS layer, round(count(q.qid)::numeric / r.runs, 2) AS questions_per_run\n"
                f"FROM runs r CROSS JOIN de_layers l\n"
                f"LEFT JOIN questions q ON q.skill = {SKILL} AND q.version = r.version AND q.layer = l.id "
                f"[[AND q.de_topic_label = {{{{de_topic}}}}]]\n"
                f"GROUP BY r.version, r.version_date, r.runs, l.label, l.sort_order\n"
                f"ORDER BY r.version_date NULLS LAST, l.sort_order"),
         {"graph.dimensions": ["version", "layer"], "graph.metrics": ["questions_per_run"], "stackable.stack_type": "stacked",
          "graph.series_order": [{"key": lay["label"], "name": lay["label"], "enabled": True} for lay in layers]},
         ("skill", "de_topic"), (0, 23, 24, 9)),
        ("tq_rows", "The questions, with their topic and layer", "table",
         ("mbql", {"fields": [col(c) for c in ("asked_at", "version", "de_topic", "layer", "interview_topic", "header",
                                               "question", "outcome_group", "answer", "wait_s", "classified_by")],
                   "order-by": [("desc", "asked_at")]}),
         {}, ("skill", "de_topic", "layer"), (0, 32, 24, 12)),
    ]


# ---------------------------------------------------------------- the same cards in ClickHouse SQL
# For the warehouse clickhouse.py loads (same tables and views, in database `db`). Columns are qualified wherever an
# output alias reuses a column's name, so the SQL means the same on either of ClickHouse's analyzers.
AVG_PER_CONVERSATION_CH = """WITH asked AS (SELECT x.session_id AS session_id, countIf(x.channel = 'ask') AS n
                 FROM {db}.questions AS x GROUP BY x.session_id),
s AS (SELECT se.*, se.start_at >= now() - INTERVAL 30 DAY AS recent, ifNull(a.n, 0) AS asked_n
      FROM {db}.sessions AS se LEFT JOIN asked AS a ON a.session_id = se.session_id WHERE se.api_requests > 0)
SELECT m.1 AS ord, m.2 AS metric,
       round(avg(m.3), 2) AS average,
       round(quantileExactInclusive(0.5)(m.3), 2) AS median,
       round(avgIf(m.3, recent), 2) AS average_last_30_days,
       round(quantileExactInclusiveIf(0.5)(m.3, recent), 2) AS median_last_30_days
FROM s ARRAY JOIN [
  (1, 'Estimated cost (USD)', toFloat64(cost_usd)), (2, 'Turns', toFloat64(turns)),
  (3, 'Prompts you typed', toFloat64(prompts)), (4, 'Claude API requests', toFloat64(api_requests)),
  (5, 'Tool calls', toFloat64(tool_calls)), (6, 'Failed tool calls', toFloat64(tool_errors)),
  (7, 'Active minutes', active_ms / 60000.0), (8, 'Wall-clock minutes', wall_ms / 60000.0),
  (9, 'Output tokens', toFloat64(output_tokens)),
  (10, 'Input tokens incl. cache', toFloat64(input_tokens + cache_read_tokens + cache_write_tokens)),
  (11, 'Peak context tokens', toFloat64(peak_context_tokens)), (12, 'Subagents', toFloat64(subagents)),
  (13, 'Skills invoked', toFloat64(skills_invoked)), (14, 'Skill runs', toFloat64(skill_runs)),
  (15, 'Questions asked (AskUserQuestion)', toFloat64(asked_n)),
  (16, 'Questions asked in prose', toFloat64(questions_prose)), (17, 'Files modified', toFloat64(files_modified)),
  (18, 'Lines added', toFloat64(lines_added)), (19, 'Commits', toFloat64(commits)),
  (20, 'Compactions', toFloat64(compactions)), (21, 'Interruptions', toFloat64(interruptions))] AS m
GROUP BY ord, metric ORDER BY ord"""


def clickhouse_sql(db="sessions"):
    """{card key: ClickHouse SQL} for every native card, CARDS and the Question topics tab alike."""
    d, since = db, "now() - INTERVAL 30 DAY"
    by_version = "ORDER BY version_date ASC NULLS LAST"
    offered = ("channel = 'ask' AND recommended_label IS NOT NULL AND NOT multi "
               "AND outcome NOT IN ('declined', 'unanswered', 'interrupted', 'error')")
    return {
        # Overview
        "sessions30": f"SELECT count() AS sessions FROM {d}.sessions WHERE start_at >= {since}",
        "cost30": f"SELECT round(sum(s.cost_usd), 2) AS cost_usd FROM {d}.sessions AS s WHERE s.start_at >= {since}",
        "hours30": f"SELECT round(sum(active_ms) / 3600000.0, 1) AS active_hours FROM {d}.sessions "
                   f"WHERE start_at >= {since}",
        "err30": f"SELECT sum(tool_errors) / nullIf(sum(tool_calls), 0) AS error_rate FROM {d}.sessions "
                 f"WHERE start_at >= {since}",
        "loaded": f"SELECT max(w.loaded_at) AS loaded_at FROM {d}.warehouse_load AS w",
        "perconv": AVG_PER_CONVERSATION_CH.format(db=d),
        "costday": f"SELECT day, cost_usd FROM {d}.v_daily WHERE day >= today() - 60 ORDER BY day",
        "costproject": f"SELECT s.project AS project, round(sum(s.cost_usd), 2) AS cost_usd FROM {d}.sessions AS s "
                       f"WHERE s.start_at >= {since} GROUP BY s.project ORDER BY cost_usd DESC, project LIMIT 12",
        "tools": f"SELECT tool, calls, errors, error_rate, denied, round(t.p50_ms) AS p50_ms FROM {d}.v_tools AS t "
                 f"ORDER BY calls DESC, tool, t.category LIMIT 25",
        "models": f"SELECT model, requests, sessions, cost_usd, round(m.p50_latency_ms) AS p50_latency_ms "
                  f"FROM {d}.v_models AS m ORDER BY cost_usd DESC NULLS LAST",
        # Skill versions
        "versions": f"SELECT version, subject, version_date, runs, median_cost_usd, median_tool_calls, median_tool_errors, "
                    f"round(v.median_active_minutes, 1) AS median_active_minutes, median_help_lookups, "
                    f"avg_questions_asked, avg_prose_questions, avg_question_rounds, recommended_rate, typed_answers, "
                    f"median_docs_read, checks_failed FROM {d}.v_skill_versions AS v WHERE skill = {SKILL} {by_version}",
        "vcost": f"SELECT version, median_cost_usd FROM {d}.v_skill_versions WHERE skill = {SKILL} {by_version}",
        "vtools": f"SELECT version, median_tool_errors, median_help_lookups FROM {d}.v_skill_versions "
                  f"WHERE skill = {SKILL} {by_version}",
        "checkrates": f"WITH v AS (SELECT version, row_number() OVER (ORDER BY version_date DESC NULLS LAST) AS rn "
                      f"FROM {d}.v_skill_versions WHERE skill = {SKILL}) "
                      f"SELECT c.check_id AS check_id, maxIf(c.version, v.rn = 2) AS previous_version, "
                      f"maxIf(c.pass_rate, v.rn = 2) AS previous_pass_rate, "
                      f"nullIf(maxIf(concat(toString(c.passed), '/', toString(c.passed + c.failed)), v.rn = 2), '') "
                      f"AS previous_runs, maxIf(c.version, v.rn = 1) AS latest_version, "
                      f"maxIf(c.pass_rate, v.rn = 1) AS latest_pass_rate, "
                      f"nullIf(maxIf(concat(toString(c.passed), '/', toString(c.passed + c.failed)), v.rn = 1), '') "
                      f"AS latest_runs, maxIf(c.pass_rate, v.rn = 1) - maxIf(c.pass_rate, v.rn = 2) AS change, "
                      f"max(c.description) AS description "
                      f"FROM {d}.v_check_rates AS c INNER JOIN v ON v.version = c.version "
                      f"WHERE c.skill = {SKILL} AND v.rn <= 2 GROUP BY c.check_id "
                      f"ORDER BY latest_pass_rate ASC NULLS LAST, change ASC NULLS LAST, check_id",
        "failing": f"SELECT check_id, description, passed, failed, pass_rate FROM {d}.v_check_rates "
                   f"WHERE skill = {SKILL} AND failed > 0 AND version IN (SELECT version FROM {d}.v_skill_versions "
                   f"WHERE skill = {SKILL} ORDER BY version_date DESC NULLS LAST LIMIT 1) ORDER BY pass_rate, check_id",
        # Interview
        "qasked": f"SELECT count() AS asked FROM {d}.questions WHERE skill = {SKILL} AND channel = 'ask'",
        "qrate": f"SELECT countIf(outcome = 'recommended') / nullIf(countIf({offered}), 0) AS recommended_rate "
                 f"FROM {d}.questions WHERE skill = {SKILL}",
        "qprose": f"SELECT count() AS in_prose FROM {d}.questions WHERE skill = {SKILL} AND channel != 'ask'",
        "qwait": f"SELECT round(quantileExactInclusive(0.5)(wait_ms) / 1000.0) AS median_wait_s FROM {d}.questions "
                 f"WHERE skill = {SKILL} AND channel = 'ask' AND outcome NOT IN ('unanswered', 'declined')",
        "qtopics": f"SELECT i.interview_topic AS topic_label, i.outcome_group AS outcome, count() AS questions "
                   f"FROM {d}.v_interview_questions AS i WHERE i.skill = {SKILL} "
                   f"GROUP BY i.interview_topic, i.outcome_group ORDER BY topic_label",
        "qchannel": f"SELECT version, avg_questions_asked AS askuserquestion, avg_prose_questions AS in_prose "
                    f"FROM {d}.v_skill_versions WHERE skill = {SKILL} {by_version}",
        "qtopictable": f"SELECT version, topic_label, asked, in_prose, runs, recommended_taken, recommended_offered, typed, "
                       f"came_back_empty, asked_again, round(t.median_wait_s) AS median_wait_s "
                       f"FROM {d}.v_question_topics AS t WHERE skill = {SKILL} ORDER BY version, asked DESC",
        "qtyped": f"SELECT asked_at, version, topic_label, question, options_offered, typed FROM {d}.v_typed_answers "
                  f"WHERE skill = {SKILL} ORDER BY asked_at DESC",
        "qflags": f"SELECT f.flag AS flag, sum(f.questions) AS questions FROM {d}.v_question_flags AS f "
                  f"WHERE f.skill = {SKILL} GROUP BY f.flag ORDER BY questions DESC",
        "qall": f"SELECT q.asked_at AS asked_at, q.version AS version, q.run_id AS run_id, q.topic_label AS topic_label, "
                f"q.header AS header, q.question AS question, q.outcome AS outcome, "
                f"coalesce(q.typed, q.answer, q.reply, q.feedback) AS answer, round(q.wait_ms / 1000.0) AS wait_s, "
                f"q.flags AS flags FROM {d}.questions AS q WHERE q.skill = {SKILL} ORDER BY q.asked_at DESC",
        # Skill files & CLI
        "files": f"SELECT path, version, runs_shown, runs, whole, in_part, median_coverage, median_order "
                 f"FROM {d}.v_skill_files WHERE skill = {SKILL} AND owner = {SKILL} ORDER BY path, version",
        "clidocs": f"SELECT concat(owner, ':', path) AS doc, version, runs_shown, runs, whole, in_part, median_coverage "
                   f"FROM {d}.v_skill_files WHERE skill = {SKILL} AND owner != {SKILL} ORDER BY runs_shown DESC, doc",
        "cli": f"SELECT c.signature AS signature, count() AS uses, countIf(c.status = 'error') AS failed, "
               f"round(countIf(c.status = 'error') / count(), 3) AS failure_rate, countIf(c.is_help) AS help_lookups, "
               f"uniqExact(c.session_id, c.run_id) AS runs FROM {d}.cli_calls AS c INNER JOIN {d}.skill_runs AS r "
               f"ON r.run_id = c.run_id AND r.session_id = c.session_id WHERE r.skill = {SKILL} "
               f"AND c.signature LIKE '% %' "
               f"GROUP BY c.signature HAVING count() >= 2 ORDER BY uses DESC, signature LIMIT 40",
        "toolerrors": f"SELECT t.at AS at, r.version AS version, t.run_id AS run_id, t.tool AS tool, t.program AS program, "
                      f"t.error_category AS error_category, t.input AS input, left(t.error, 300) AS error "
                      f"FROM {d}.tool_calls AS t INNER JOIN {d}.skill_runs AS r "
                      f"ON r.run_id = t.run_id AND r.session_id = t.session_id "
                      f"WHERE r.skill = {SKILL} AND t.status = 'error' ORDER BY t.at DESC, t.tool_use_id LIMIT 200",
        # Question topics
        "tq_stack": f"SELECT countIf(layer != 'cross-cutting') / nullIf(count(), 0) AS on_the_stack "
                    f"FROM {d}.questions WHERE skill = {SKILL}",
        "tq_never": f"SELECT if(count() = 0, 'none', arrayStringConcat(arrayMap(x -> x.2, "
                    f"arraySort(groupArray((l.sort_order, ifNull(l.label, ''))))), ', ')) AS never_asked "
                    f"FROM {d}.de_layers AS l WHERE l.id != 'cross-cutting' AND l.id NOT IN "
                    f"(SELECT layer FROM {d}.questions WHERE skill = {SKILL} AND layer IS NOT NULL)",
        "tq_fallback": f"SELECT countIf(semantics_by LIKE '%fallback%' OR de_topic = 'other') / nullIf(count(), 0) "
                       f"AS by_fallback FROM {d}.questions WHERE skill = {SKILL}",
        "tq_matrix": crosstab_sql("clickhouse", d),
        "tq_versions": f"WITH runs AS (SELECT sr.version AS version, max(sr.version_date) AS version_date, "
                       f"count() AS runs FROM {d}.skill_runs AS sr WHERE sr.skill = {SKILL} GROUP BY sr.version)\n"
                       f"SELECT r.version AS version, l.label AS layer, "
                       f"round(countIf(q.qid != '') / r.runs, 2) AS questions_per_run\n"
                       f"FROM runs AS r CROSS JOIN {d}.de_layers AS l\n"
                       f"LEFT JOIN {d}.questions AS q ON q.skill = {SKILL} AND q.version = r.version "
                       f"AND q.layer = l.id [[AND q.de_topic_label = {{{{de_topic}}}}]]\n"
                       f"GROUP BY r.version, r.version_date, r.runs, l.label, l.sort_order\n"
                       f"ORDER BY r.version_date ASC NULLS LAST, l.sort_order",
    }


# ---------------------------------------------------------------- Metabase
def mb(*args, body=None):
    cmd = ["mb", *args, "--profile", PROFILE, "--json", "--max-bytes", "0"]
    if body is not None:
        f = WORK / f"body-{uuid.uuid4().hex[:8]}.json"
        f.write_text(json.dumps(body))
        cmd += ["--file", str(f)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise SystemExit(f"mb {' '.join(args)} failed:\n{res.stderr or res.stdout}")
    out = json.loads(res.stdout)
    return out.get("data", out) if isinstance(out, dict) and "id" not in out else out


def native_query(sql):
    tags = {}
    if SKILL in sql:
        tags["skill"] = {"id": str(uuid.uuid4()), "name": "skill", "display-name": "Skill", "type": "text",
                         "required": True, "default": "rde"}
    if "{{de_topic}}" in sql:
        tags["de_topic"] = {"id": str(uuid.uuid4()), "name": "de_topic", "display-name": "Data-engineering topic",
                            "type": "text"}
    return {"lib/type": "mbql/query", "database": DB,
            "stages": [{"lib/type": "mbql.stage/native", "native": sql, "template-tags": tags}]}


def native_rows(sql):
    return mb("query", body=native_query(sql))["rows"]


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


def sync(col_id, name, dialect, ch_db, verify=True):
    """The whole dashboard in collection `col_id`: created, or updated in place. The model and metrics live in the
    collection; a card that does not exist yet is created inside the dashboard (a dashboard question), so the
    collection lists only what someone would open on its own."""
    existing = collection_items(col_id)
    did = existing.get(("dashboard", name))
    if did is None:
        did = _id(mb("dashboard", "create", body={"name": name, "collection_id": col_id, "description": DESCRIPTION}))
        print(f"dashboard {did} created: {name}")
    dash = mb("dashboard", "get", str(did), "--full")
    for dc in dash.get("dashcards") or ():
        c = dc.get("card") or {}
        if c.get("id"):
            existing.setdefault(({"model": "dataset", "metric": "metric"}.get(c.get("type"), "card"), c["name"]), c["id"])
    in_dashboard = {"dashboard_id": did}
    ch = clickhouse_sql(ch_db) if dialect == "clickhouse" else {}

    # the model and its column metadata
    model_sql = f"SELECT * FROM {ch_db}.v_interview_questions" if ch else MODEL_SQL
    model_id = upsert_card(existing, {
        "name": MODEL_NAME, "type": "model", "display": "table", "description": MODEL_DESCRIPTION,
        "collection_id": col_id, "visualization_settings": {},
        "dataset_query": {"lib/type": "mbql/query", "database": DB,
                          "stages": [{"lib/type": "mbql.stage/native", "native": model_sql, "template-tags": {}}]}})
    meta = mb("card", "get", str(model_id), "--full").get("result_metadata") or []
    if not meta:
        raise SystemExit(f"model {model_id} has no result_metadata: run it once in Metabase, then re-run this")
    for c in meta:
        display, description, semantic_type = MODEL_COLUMNS.get(c["name"], (None, None, None))
        c.update({k: v for k, v in (("display_name", display), ("description", description),
                                    ("semantic_type", semantic_type)) if v})
    mb("card", "update", str(model_id), body={"result_metadata": meta})
    types = {c["name"]: c["base_type"] for c in meta}

    metric_ids = {}
    for key, mname, description, agg in METRICS:
        metric_ids[key] = upsert_card(existing, {
            "name": mname, "type": "metric", "display": "scalar", "description": description, "collection_id": col_id,
            "visualization_settings": {},
            "dataset_query": {"lib/type": "mbql/query", "database": DB,
                              "stages": [{"lib/type": "mbql.stage/mbql", "source-card": model_id,
                                          "aggregation": [_resolve(agg, types, {}, {})]}]}})

    # every card: (card id, tab, position, parameter mappings, dashcard visualization settings)
    placed = []
    for key, tab, cname, display, sql, vis, uses_skill, pos in CARDS:
        cid = upsert_card(existing, card_body(cname, display, ch.get(key, sql), vis, uses_skill, col_id),
                          create_extra=in_dashboard)
        maps = [{"parameter_id": "skill", "card_id": cid, "target": ["variable", ["template-tag", "skill"]]}] \
            if uses_skill else []
        placed.append((cid, tab, pos, maps, {}))
    # Clicking a topic in the matrix sets the topic filter: the cards below narrow to it.
    matrix_click = {"column_settings": {'["name","de_topic"]': {"click_behavior": {
        "type": "crossfilter", "parameterMapping": {"de_topic": {
            "id": "de_topic", "source": {"type": "column", "id": "de_topic", "name": "de_topic"},
            "target": {"type": "parameter", "id": "de_topic"}}}}}}}
    for key, cname, display, (kind, q), vis, filters, pos in topic_cards(dialect, ch_db):
        if kind == "sql":
            dq = native_query(q)
        else:
            dq = {"lib/type": "mbql/query", "database": DB,
                  "stages": [{"lib/type": "mbql.stage/mbql", "source-card": model_id,
                              **_resolve(q, types, metric_ids, {})}]}
        # The CLI's bundled MBQL schema wants a metric's entity id in ["metric", {}, id]; the server takes only its
        # numeric id. So a card that uses a metric skips the CLI's pre-flight check; the server still checks it.
        cid = upsert_card(existing, {"name": cname, "display": display, "visualization_settings": vis,
                                     "collection_id": col_id, "dataset_query": dq},
                          skip_validate=kind == "mbql", create_extra=in_dashboard)
        maps = [{"parameter_id": f, "card_id": cid,
                 "target": ["variable", ["template-tag", f]] if kind == "sql"
                 else ["dimension", ["field", f, {"base-type": types[f]}], {"stage-number": 0}]} for f in filters]
        placed.append((cid, TOPIC_TAB, pos, maps, matrix_click if key == "tq_matrix" else {}))

    # the layout: re-read, since a new dashboard question arrives with a dashcard of its own
    dash = mb("dashboard", "get", str(did), "--full")
    tab_id = {t["name"]: t["id"] for t in dash.get("tabs") or ()}
    tabs = [{"id": tab_id.get(t, -(i + 1)), "name": t, "position": i} for i, t in enumerate(ALL_TABS)]
    tab_id = {t["name"]: t["id"] for t in tabs}
    dashcard_of = {dc["card_id"]: dc["id"] for dc in dash.get("dashcards") or () if dc.get("card_id")}
    dashcards = [{"id": dashcard_of.get(cid, -n), "card_id": cid, "dashboard_tab_id": tab_id[tab], "col": x, "row": y,
                  "size_x": w, "size_y": h, "parameter_mappings": maps, "visualization_settings": vis, "series": []}
                 for n, (cid, tab, (x, y, w, h), maps, vis) in enumerate(placed, 1)]
    skills_sql = (f"SELECT DISTINCT skill FROM {ch_db}.skill_runs WHERE skill IS NOT NULL ORDER BY skill" if ch
                  else "SELECT DISTINCT skill FROM skill_runs WHERE skill IS NOT NULL ORDER BY skill")
    skills = [r[0] for r in native_rows(skills_sql)]
    topics, layers = semantics.load().dimensions()
    params = [{"id": "skill", "name": "Skill", "slug": "skill", "type": "string/=", "default": ["rde"],
               "values_source_type": "static-list", "values_source_config": {"values": skills}},
              {"id": "de_topic", "name": "Data-engineering topic", "slug": "de_topic", "type": "string/=",
               "values_source_type": "static-list", "values_source_config": {"values": [t["label"] for t in topics]}},
              {"id": "layer", "name": "Layer", "slug": "layer", "type": "string/=",
               "values_source_type": "static-list", "values_source_config": {"values": [lay["label"] for lay in layers]}}]
    mb("dashboard", "update", str(did), body={"description": DESCRIPTION, "tabs": tabs, "dashcards": dashcards,
                                              "parameters": params})
    print(f"dashboard {did}: {len(dashcards)} cards on {len(tabs)} tabs; model {model_id}, "
          f"metrics {', '.join(str(i) for i in metric_ids.values())}")
    if verify:
        bad = 0
        for cid in [model_id, *metric_ids.values(), *(p[0] for p in placed)]:
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


def _test_sql(sql):
    """The card's SQL as Metabase runs it with the Skill filter on rde and every optional filter off."""
    return re.sub(r"\[\[.*?\]\]", "", sql, flags=re.S).replace(SKILL, "'rde'")


def test(dialect="postgres", env_file=None):
    if dialect == "clickhouse":
        from session_analytics import clickhouse
        client = clickhouse.Client(clickhouse.target_from_settings(env_file))
        db = client.t.database
        ch = clickhouse_sql(db)
        sqls = [(key, name, ch[key]) for key, _tab, name, *_ in CARDS]
        sqls.append(("model", MODEL_NAME, f"SELECT * FROM {db}.v_interview_questions WHERE skill = {SKILL}"))
        sqls += [(key, name, q[1]) for key, name, _display, q, *_ in topic_cards("clickhouse", db) if q[0] == "sql"]

        def run(sql):
            try:
                rows = client.rows(sql)
            except clickhouse.ClickHouseError as exc:
                return 1, str(exc)
            head = "\t".join(rows[0]) if rows else ""
            return 0, "\n".join([head] + ["\t".join(str(v) for v in r.values()) for r in rows] + ["(end)"])
    else:
        sqls = [(key, name, sql) for key, _tab, name, _display, sql, _vis, _uses_skill, _ in CARDS]
        sqls.append(("model", MODEL_NAME, MODEL_SQL + " WHERE skill = {{skill}}"))
        sqls += [(key, name, q[1]) for key, name, _display, q, *_ in topic_cards() if q[0] == "sql"]
        run = psql
    bad = 0
    for key, name, sql in sqls:
        code, out = run(_test_sql(sql))
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
    return {"name": name, "display": display, "visualization_settings": vis, "collection_id": collection_id,
            "dataset_query": native_query(sql)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--test", action="store_true", help="run every card's SQL against the warehouse")
    ap.add_argument("--clickhouse", action="store_true", help="with --test: the ClickHouse warehouse (.env)")
    ap.add_argument("--env-file", help="with --test --clickhouse: read CLICKHOUSE_URL from this file")
    ap.add_argument("--sync", action="store_true", help="create or update the dashboard, model and metrics in Metabase")
    ap.add_argument("--profile", help="mb CLI profile of the target Metabase (required with --sync)")
    ap.add_argument("--database", type=int, help="the warehouse's database id in that Metabase (required with --sync)")
    ap.add_argument("--collection", type=int, help="the collection the dashboard lives in (required with --sync)")
    ap.add_argument("--name", default="Claude Code sessions", help="the dashboard's name (found by it in the collection)")
    ap.add_argument("--ch-database", default="sessions", help="ClickHouse database holding the tables (default sessions)")
    ap.add_argument("--no-verify", action="store_true", help="with --sync: skip running every card afterwards")
    args = ap.parse_args()
    if args.test:
        sys.exit(1 if test("clickhouse" if args.clickhouse else "postgres", args.env_file) else 0)
    if args.sync:
        if not (args.profile and args.database and args.collection):
            ap.error("--sync needs --profile, --database and --collection")
        PROFILE, DB = args.profile, args.database
        engine = mb("database", "get", str(DB)).get("engine")
        if engine not in ("postgres", "clickhouse"):
            ap.error(f"database {DB} is {engine!r}: the cards are written for postgres and clickhouse")
        if engine == "clickhouse" and not re.fullmatch(r"\w+", args.ch_database):
            ap.error("--ch-database must be a plain identifier")
        sys.exit(1 if sync(args.collection, args.name, engine, args.ch_database, verify=not args.no_verify) else 0)
    ap.print_help()
