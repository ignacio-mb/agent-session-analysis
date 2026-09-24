"""Build the "Claude Code sessions" Metabase dashboard on the session warehouse (see warehouse.py).

    python3 scripts/metabase_dashboard.py --test
    python3 scripts/metabase_dashboard.py --build --profile <mb profile> --database <id>

--test runs every card's SQL against the local warehouse (docker exec psql). --build creates a collection, the
cards (native SQL on the warehouse's views) and a dashboard with five tabs — Overview, Skill versions, Interview,
Question topics, Skill files & CLI — and a Skill filter. It needs the Metabase CLI (`mb`) logged in to the target
Metabase (--profile) and the warehouse already added there as a database (--database: its id in `mb db list`).
Nothing is created without --build; pass the profile explicitly so nothing lands on a Metabase you did not mean.

The Question topics tab reads a model, "Interview questions": one row per question, with the data-engineering topic
and layer it is about (semantics/questions.json) and friendly outcome columns. The model is the semantic layer —
ask new questions of it in the query builder rather than writing SQL against the tables.
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
     "GROUP BY project ORDER BY cost_usd DESC LIMIT 12",
     {"graph.dimensions": ["project"], "graph.metrics": ["cost_usd"],
      "column_settings": {'["name","cost_usd"]': {"number_style": "currency", "currency": "USD"}}}, False, (0, 14, 12, 8)),
    ("tools", "Overview", "Tools: calls, failures and speed", "table",
     "SELECT tool, calls, errors, error_rate, denied, round(p50_ms::numeric) AS p50_ms FROM v_tools ORDER BY calls DESC LIMIT 25",
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
     f"FROM cli_calls c JOIN skill_runs r USING (run_id) WHERE r.skill = {SKILL} AND c.signature LIKE '% %' "
     f"GROUP BY c.signature HAVING count(*) >= 2 ORDER BY uses DESC LIMIT 40",
     {"column_settings": {'["name","failure_rate"]': {"number_style": "percent", "decimals": 1}}}, True, (0, 12, 12, 12)),
    ("toolerrors", "Skill files & CLI", "Failed tool calls in the skill's runs", "table",
     f"SELECT t.at, r.version, t.run_id, t.tool, t.program, t.error_category, t.input, left(t.error, 300) AS error "
     f"FROM tool_calls t JOIN skill_runs r USING (run_id) WHERE r.skill = {SKILL} AND t.status = 'error' "
     f"ORDER BY t.at DESC LIMIT 200", {}, True, (12, 12, 12, 12)),
]
TABS = ["Overview", "Skill versions", "Interview", "Skill files & CLI"]

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


def crosstab_sql():
    """Topic x layer, one column per layer in stack order (from semantics/questions.json at build time)."""
    _, layers = semantics.load().dimensions()
    cells = ",\n       ".join(f"count(q.qid) FILTER (WHERE q.layer = '{lay['id']}') AS {_ident(lay['id'])}" for lay in layers)
    return (f"SELECT t.label AS de_topic,\n       {cells},\n       count(q.qid) AS all_layers\n"
            f"FROM de_topics t LEFT JOIN questions q ON q.de_topic = t.id AND q.skill = {SKILL}\n"
            f"GROUP BY t.label, t.sort_order ORDER BY t.sort_order")


def _ident(s):
    return re.sub(r"\W", "_", s)


def topic_cards():
    """(key, name, display, query, visualization_settings, filters, (col, row, w, h)) for the Question topics tab.
    A query is ("sql", text) or ("mbql", stage without its source); filters: the dashboard filters the card takes."""
    _, layers = semantics.load().dimensions()
    layer_ids = [_ident(lay["id"]) for lay in layers]
    metric = lambda key: ("metric", key)  # noqa: E731 - resolved to the metric's id when the card is built
    return [
        ("tq_total", "Questions put to the user", "scalar", ("mbql", {"aggregation": [metric("questions")]}),
         {"scalar.field": "questions"}, ("skill", "de_topic", "layer"), (0, 0, 6, 3)),
        ("tq_stack", "About a data-stack layer (not cross-cutting)", "scalar",
         ("sql", f"SELECT count(*) FILTER (WHERE layer <> 'cross-cutting')::numeric / nullif(count(*), 0) AS on_the_stack "
                 f"FROM questions WHERE skill = {SKILL}"),
         {"scalar.field": "on_the_stack", "column_settings": {'["name","on_the_stack"]': PERCENT}}, ("skill",), (6, 0, 6, 3)),
        ("tq_never", "Layers the interview never asks about", "scalar",
         ("sql", f"SELECT coalesce(string_agg(l.label, ', ' ORDER BY l.sort_order), 'none') AS never_asked FROM de_layers l "
                 f"WHERE l.id <> 'cross-cutting' AND NOT EXISTS (SELECT 1 FROM questions q WHERE q.layer = l.id "
                 f"AND q.skill = {SKILL})"),
         {"scalar.field": "never_asked"}, ("skill",), (12, 0, 6, 3)),
        ("tq_fallback", "Placed by the fallback, not a rule", "scalar",
         ("sql", f"SELECT count(*) FILTER (WHERE semantics_by LIKE '%fallback%' OR de_topic = 'other')::numeric "
                 f"/ nullif(count(*), 0) AS by_fallback FROM questions WHERE skill = {SKILL}"),
         {"scalar.field": "by_fallback", "column_settings": {'["name","by_fallback"]': PERCENT}}, ("skill",), (18, 0, 6, 3)),
        ("tq_matrix", "Where the questions land: data-engineering topic × layer", "table", ("sql", crosstab_sql()),
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
         ("sql", f"WITH runs AS (SELECT version, max(version_date) AS version_date, count(*) AS runs FROM skill_runs "
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


def psql(sql):
    res = subprocess.run(["docker", "exec", "-i", "convo-analysis-pg", "psql", "-U", "convo", "-d", "claude_sessions",
                          "-v", "ON_ERROR_STOP=1", "-A", "-F", "\t", "-c", sql], capture_output=True, text=True)
    return res.returncode, (res.stdout if res.returncode == 0 else res.stderr).strip()


def _test_sql(sql):
    """The card's SQL as Metabase runs it with the Skill filter on rde and every optional filter off."""
    return re.sub(r"\[\[.*?\]\]", "", sql, flags=re.S).replace(SKILL, "'rde'")


def test():
    sqls = [(key, name, sql) for key, _tab, name, _display, sql, _vis, _uses_skill, _ in CARDS]
    sqls.append(("model", MODEL_NAME, MODEL_SQL + " WHERE skill = {{skill}}"))
    sqls += [(key, name, q[1]) for key, name, _display, q, *_ in topic_cards() if q[0] == "sql"]
    bad = 0
    for key, name, sql in sqls:
        code, out = psql(_test_sql(sql))
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


def card_body(name, display, sql, vis, uses_skill, collection_id):
    return {"name": name, "display": display, "visualization_settings": vis, "collection_id": collection_id,
            "dataset_query": native_query(sql)}


def _id(res):
    return res.get("id") or res["data"]["id"]


def upsert_card(existing, body, skip_validate=False):
    """Create the card, or update the one of that name and kind already in the collection (keeps its id and links)."""
    kind = {"model": "dataset", "metric": "metric"}.get(body.get("type"), "card")
    cid = existing.get((kind, body["name"]))
    flags = ["--skip-validate"] if skip_validate else []
    if cid:
        mb("card", "update", str(cid), *flags, body=body)
        print(f"{body.get('type', 'card')} {cid} updated: {body['name']}")
        return cid
    cid = _id(mb("card", "create", *flags, body=body))
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


def semantic(dashboard_id, col_id):
    """The model, its metrics and the Question topics tab — created, or updated in place when they exist."""
    existing = collection_items(col_id)
    model_body = {"name": MODEL_NAME, "type": "model", "display": "table", "description": MODEL_DESCRIPTION,
                  "collection_id": col_id, "visualization_settings": {},
                  "dataset_query": {"lib/type": "mbql/query", "database": DB,
                                    "stages": [{"lib/type": "mbql.stage/native", "native": MODEL_SQL, "template-tags": {}}]}}
    model_id = upsert_card(existing, model_body)
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
    for key, name, description, agg in METRICS:
        body = {"name": name, "type": "metric", "display": "scalar", "description": description, "collection_id": col_id,
                "visualization_settings": {},
                "dataset_query": {"lib/type": "mbql/query", "database": DB,
                                  "stages": [{"lib/type": "mbql.stage/mbql", "source-card": model_id,
                                              "aggregation": [_resolve(agg, types, {}, {})]}]}}
        metric_ids[key] = upsert_card(existing, body)

    cards = []
    for key, name, display, (kind, q), vis, filters, pos in topic_cards():
        if kind == "sql":
            dq = native_query(q)
        else:
            stage = _resolve(q, types, metric_ids, {})
            dq = {"lib/type": "mbql/query", "database": DB,
                  "stages": [{"lib/type": "mbql.stage/mbql", "source-card": model_id, **stage}]}
        body = {"name": name, "display": display, "visualization_settings": vis, "collection_id": col_id,
                "dataset_query": dq}
        # The CLI's bundled MBQL schema wants a metric's entity id in ["metric", {}, id]; the server (1.63) takes
        # only its numeric id. So a card that uses a metric skips the CLI's pre-flight check; the server still checks.
        cards.append((key, upsert_card(existing, body, skip_validate=kind == "mbql"), kind, filters, pos))

    dash = mb("dashboard", "get", str(dashboard_id), "--full")
    tabs = [{"id": t["id"], "name": t["name"]} for t in sorted(dash["tabs"], key=lambda t: t["position"])]
    topic_tab = next((t["id"] for t in tabs if t["name"] == TOPIC_TAB), None)
    if topic_tab is None:
        topic_tab = -1
        after = next((i + 1 for i, t in enumerate(tabs) if t["name"] == "Interview"), len(tabs))
        tabs.insert(after, {"id": topic_tab, "name": TOPIC_TAB})
    for i, t in enumerate(tabs):
        t["position"] = i
    keep = ("id", "card_id", "dashboard_tab_id", "col", "row", "size_x", "size_y", "parameter_mappings",
            "visualization_settings", "inline_parameters")
    dashcards = [{**{k: dc[k] for k in keep if k in dc}, "series": [{"id": s["id"]} for s in dc.get("series") or ()]}
                 for dc in dash["dashcards"] if dc["dashboard_tab_id"] != topic_tab]
    # Clicking a topic in the matrix sets the topic filter: the cards below narrow to it.
    matrix_click = {"column_settings": {'["name","de_topic"]': {"click_behavior": {
        "type": "crossfilter", "parameterMapping": {"de_topic": {
            "id": "de_topic", "source": {"type": "column", "id": "de_topic", "name": "de_topic"},
            "target": {"type": "parameter", "id": "de_topic"}}}}}}}
    for n, (key, card_id, kind, filters, (x, y, w, h)) in enumerate(cards, 1):
        maps = []
        for f in filters:
            if kind == "sql":
                target = ["variable", ["template-tag", f]]
            else:
                target = ["dimension", ["field", f, {"base-type": types[f]}], {"stage-number": 0}]
            maps.append({"parameter_id": f, "card_id": card_id, "target": target})
        dashcards.append({"id": -n, "card_id": card_id, "dashboard_tab_id": topic_tab, "col": x, "row": y,
                          "size_x": w, "size_y": h, "parameter_mappings": maps, "series": [],
                          "visualization_settings": matrix_click if key == "tq_matrix" else {}})
    topics, layers = semantics.load().dimensions()
    params = [p for p in dash["parameters"] if p["id"] not in ("de_topic", "layer")]
    params += [{"id": "de_topic", "name": "Data-engineering topic", "slug": "de_topic", "type": "string/=",
                "values_source_type": "static-list", "values_source_config": {"values": [t["label"] for t in topics]}},
               {"id": "layer", "name": "Layer", "slug": "layer", "type": "string/=",
                "values_source_type": "static-list", "values_source_config": {"values": [lay["label"] for lay in layers]}}]
    mb("dashboard", "update", str(dashboard_id), body={"tabs": tabs, "dashcards": dashcards, "parameters": params})
    print(f"dashboard {dashboard_id}: tab {TOPIC_TAB!r} with {len(cards)} cards; model {model_id}, "
          f"metrics {', '.join(str(i) for i in metric_ids.values())}")


def build():
    skills = [line for line in psql("SELECT DISTINCT skill FROM skill_runs ORDER BY 1")[1].splitlines()[1:-1] if line]
    col_id = _id(mb("collection", "create", body={"name": "Claude Code sessions",
                                                  "description": "Session analytics from the convo-analysis warehouse (Postgres)."}))
    ids = {}
    for key, _tab, name, display, sql, vis, uses_skill, _ in CARDS:
        ids[key] = _id(mb("card", "create", body=card_body(name, display, sql, vis, uses_skill, col_id)))
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
                           "the questions it asks and what they are about, the files it reads. Source: the "
                           "convo-analysis warehouse.",
            "tabs": tabs, "dashcards": dashcards,
            "parameters": [{"id": "skill", "name": "Skill", "slug": "skill", "type": "string/=", "default": ["rde"],
                            "values_source_type": "static-list", "values_source_config": {"values": skills}}]}
    did = _id(mb("dashboard", "create", body=body))
    print("dashboard", did)
    semantic(did, col_id)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--test", action="store_true", help="run every card's SQL against the local warehouse")
    ap.add_argument("--build", action="store_true", help="create the collection, cards and dashboard in Metabase")
    ap.add_argument("--semantic", action="store_true",
                    help="create or update the model, metrics and Question topics tab on an existing dashboard")
    ap.add_argument("--profile", help="mb CLI profile of the target Metabase (required with --build/--semantic)")
    ap.add_argument("--database", type=int, help="the warehouse's database id in that Metabase (required with --build)")
    ap.add_argument("--dashboard", type=int, help="the dashboard to add the Question topics tab to (--semantic)")
    ap.add_argument("--collection", type=int, help="the collection the model and metrics go in (--semantic)")
    args = ap.parse_args()
    if args.test:
        sys.exit(1 if test() else 0)
    if args.build or args.semantic:
        if not args.profile or not args.database:
            ap.error("--build and --semantic need --profile and --database")
        if args.semantic and not (args.dashboard and args.collection):
            ap.error("--semantic needs --dashboard and --collection")
        PROFILE, DB = args.profile, args.database
        build() if args.build else semantic(args.dashboard, args.collection)
    else:
        ap.print_help()
