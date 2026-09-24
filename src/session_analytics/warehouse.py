"""A Postgres warehouse of every Claude Code session: the analytics as plain tables and views.

    session-analytics warehouse --up --load       start the local Postgres, analyze every transcript, load it
    make warehouse                                the same

Each table holds one kind of fact (a session, a turn, an API request, a tool call, a CLI call, a skill run, a
check result, a skill file a run was shown, a question and its options…), and each fact belongs to exactly one
session: transcripts are read own-only, so the history a resumed session copies stays with the session it came
from. The views answer the tuning questions directly: versions compared, check pass rates, question topics,
CLI error rates, daily usage. Tables are dropped and recreated on every load; the transcripts are the source of
truth and the warehouse a disposable copy of what they say.

Loading needs no Python driver: CSVs are streamed into `psql` inside the Postgres container (`docker exec -i`),
or into a local `psql` when one is installed and a DSN is given.
"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import locate, semantics, util
from .analyze import analyze, categorize_error, cli_calls, primary_program
from .export import default_root
from .parse import parse_session
from .pricing import Pricing
from .redact import Redactor
from .rollup import parse_since

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "docker-compose.yml"
CONTAINER, DATABASE, USER, PORT = "convo-analysis-pg", "claude_sessions", "convo", 55432

TEXT, INT, BIG, NUM, TS, BOOL = "text", "integer", "bigint", "numeric", "timestamptz", "boolean"

# name: (comment, [(column, type, comment or None)], primary key)
TABLES = {
    "sessions": ("One row per Claude Code session (own events only).", [
        ("session_id", TEXT, None), ("project", TEXT, "Project directory name"), ("title", TEXT, None),
        ("start_at", TS, None), ("end_at", TS, None), ("wall_ms", BIG, "End minus start"),
        ("active_ms", BIG, "Time Claude or a tool was working (turn durations)"), ("turns", INT, None), ("prompts", INT, None),
        ("api_requests", INT, None), ("tool_calls", INT, None), ("tool_errors", INT, None), ("tool_denials", INT, None),
        ("subagents", INT, None), ("skills_invoked", INT, None), ("skill_runs", INT, None),
        ("questions", INT, "Questions Claude asked (AskUserQuestion and prose)"), ("questions_prose", INT, None),
        ("input_tokens", BIG, None), ("output_tokens", BIG, None), ("cache_read_tokens", BIG, None),
        ("cache_write_tokens", BIG, None), ("cache_hit_ratio", NUM, None),
        ("cost_usd", NUM, "Estimated at list prices, API-equivalent"), ("reported_cost_usd", NUM, "Claude Code's own figure"),
        ("peak_context_tokens", BIG, None), ("compactions", INT, None), ("interruptions", INT, None),
        ("files_modified", INT, None), ("lines_added", INT, None), ("lines_removed", INT, None), ("commits", INT, None),
        ("pull_requests", INT, None), ("models", TEXT, None), ("claude_code_version", TEXT, None), ("entrypoint", TEXT, None),
        ("git_branch", TEXT, None), ("cwd", TEXT, None), ("transcript", TEXT, None)], ["session_id"]),
    "turns": ("One row per turn: a prompt and everything Claude did before handing back.", [
        ("session_id", TEXT, None), ("turn", INT, None), ("start_at", TS, None), ("end_at", TS, None),
        ("duration_ms", BIG, None), ("trigger", TEXT, "prompt | command | task_notification | bash"),
        ("prompt", TEXT, "What was asked (redacted, truncated)"), ("prompt_chars", INT, None), ("command", TEXT, None),
        ("requests", INT, None), ("tool_calls", INT, None), ("tool_errors", INT, None), ("cost_usd", NUM, None),
        ("input_tokens", BIG, None), ("output_tokens", BIG, None), ("cache_read_tokens", BIG, None),
        ("cache_write_tokens", BIG, None), ("max_context_tokens", BIG, None), ("skills_invoked", TEXT, None),
        ("interrupted", BOOL, None), ("compacted", BOOL, None), ("permission_mode", TEXT, None),
        ("stop_reason", TEXT, None)], ["session_id", "turn"]),
    "api_requests": ("One row per Claude API request (streamed lines merged).", [
        ("session_id", TEXT, None), ("request_no", INT, None), ("at", TS, None), ("turn", INT, None),
        ("scope", TEXT, "main or subagent"), ("agent_id", TEXT, None), ("model", TEXT, None), ("stop_reason", TEXT, None),
        ("input_tokens", BIG, None), ("output_tokens", BIG, None), ("cache_read_tokens", BIG, None),
        ("cache_write_5m_tokens", BIG, None), ("cache_write_1h_tokens", BIG, None), ("thinking_tokens", BIG, None),
        ("context_tokens", BIG, "Tokens sent: input + cache read + cache write"), ("cost_usd", NUM, None),
        ("latency_ms", BIG, "To the first content block"), ("duration_ms", BIG, None),
        ("skill", TEXT, "Claude Code's attribution"), ("tools", TEXT, "Tools this response called"), ("effort", TEXT, None),
        ("cache_miss_reason", TEXT, None)], ["session_id", "request_no"]),
    "tool_calls": ("One row per tool call.", [
        ("session_id", TEXT, None), ("tool_use_id", TEXT, None), ("at", TS, None), ("turn", INT, None), ("scope", TEXT, None),
        ("agent_id", TEXT, None), ("tool", TEXT, None), ("category", TEXT, None),
        ("status", TEXT, "ok | error | denied | interrupted | pending"), ("duration_ms", BIG, None),
        ("error_category", TEXT, None), ("program", TEXT, "For Bash: the program the command is about"),
        ("run_id", TEXT, "The skill run the call belongs to"), ("skill", TEXT, "Claude Code's attribution"),
        ("input", TEXT, "Input summary (redacted, truncated)"), ("error", TEXT, None), ("result_chars", INT, None),
        ("batch_size", INT, "Calls issued in parallel with it")], ["session_id", "tool_use_id"]),
    "cli_calls": ("One row per program invocation inside a Bash command, by signature (`mb transform create`).", [
        ("session_id", TEXT, None), ("tool_use_id", TEXT, None), ("seq", INT, "Position in the command"),
        ("at", TS, None), ("run_id", TEXT, None), ("program", TEXT, None), ("signature", TEXT, None),
        ("is_help", BOOL, "A --help lookup"), ("status", TEXT, "Status of the whole Bash call"),
        ("error_category", TEXT, None)], ["session_id", "tool_use_id", "seq"]),
    "skill_invocations": ("One row per skill invocation.", [
        ("session_id", TEXT, None), ("invocation_no", INT, None), ("at", TS, None), ("turn", INT, None), ("skill", TEXT, None),
        ("canonical", TEXT, None), ("mode", TEXT, "model (Skill tool) | user (/slash) | harness"), ("via", TEXT, None),
        ("scope", TEXT, None), ("success", BOOL, None), ("status", TEXT, None), ("args", TEXT, None),
        ("content_chars", INT, "Size of the SKILL.md body injected")], ["session_id", "invocation_no"]),
    "skill_runs": ("One row per skill run: an invocation plus the follow-up turns it steered.", [
        ("run_id", TEXT, None), ("session_id", TEXT, None), ("skill", TEXT, None), ("mode", TEXT, None),
        ("version", TEXT, "Git commit that ran (or a label when unknown)"), ("version_status", TEXT, None),
        ("version_date", TS, None), ("version_subject", TEXT, None), ("start_at", TS, None), ("end_at", TS, None),
        ("duration_ms", BIG, None), ("active_ms", BIG, None), ("turns", INT, None), ("follow_up_turns", INT, None),
        ("requests", INT, None), ("attributed_requests", INT, None), ("tool_calls", INT, None), ("tool_errors", INT, None),
        ("error_rate", NUM, None), ("cli_calls", INT, None), ("help_lookups", INT, None), ("retries_after_error", INT, None),
        ("cost_usd", NUM, None), ("attributed_cost_usd", NUM, None), ("input_tokens", BIG, None), ("output_tokens", BIG, None),
        ("context_peak", BIG, None), ("questions_asked", INT, "Through AskUserQuestion"), ("question_rounds", INT, None),
        ("prose_questions", INT, None), ("recommended_offered", INT, None), ("recommended_taken", INT, None),
        ("recommended_rate", NUM, None), ("typed_answers", INT, None), ("unanswered_questions", INT, None),
        ("question_wait_p50_ms", BIG, None), ("questions_flagged", INT, None), ("questions_before_create", INT, None),
        ("docs_read", INT, "The skill's own files shown, beyond SKILL.md"), ("docs_total", INT, None),
        ("cli_docs_read", INT, None), ("doc_tokens", BIG, None), ("doc_rereads", INT, None), ("doc_listings", INT, None),
        ("checks_passed", INT, None), ("checks_failed", INT, None), ("objects_created", INT, None),
        ("end_reason", TEXT, None), ("prompt", TEXT, None),
        ("prompt_key", TEXT, "The prompt's opening words, lowercased: runs of one prompt share it"),
        ("args", TEXT, None)], ["run_id"]),
    "skill_run_checks": ("One row per run and declared check (checks/<skill>.json).", [
        ("run_id", TEXT, None), ("session_id", TEXT, None), ("skill", TEXT, None), ("version", TEXT, None), ("check_id", TEXT, None),
        ("description", TEXT, None), ("status", TEXT, "pass | fail | n/a | error"), ("detail", TEXT, None)],
        ["run_id", "check_id"]),
    "skill_run_files": ("One row per run and skill document it touched, measured by what Claude was shown.", [
        ("run_id", TEXT, None), ("session_id", TEXT, None), ("skill", TEXT, None), ("version", TEXT, None),
        ("owner", TEXT, "The skill, another skill, or a CLI (mb)"), ("path", TEXT, None), ("kind", TEXT, None),
        ("read_order", INT, "0 is SKILL.md's injection"), ("how", TEXT, None), ("coverage", NUM, "Share of lines shown"),
        ("lines_seen", INT, None), ("total_lines", INT, None), ("accesses", INT, None), ("reads", INT, None),
        ("searches", INT, None), ("rereads", INT, None), ("tokens", INT, None), ("first_read_ms", BIG, None),
        ("found_by", TEXT, None), ("named_by", TEXT, None), ("version_check", TEXT, None), ("sections", TEXT, None)],
        ["run_id", "owner", "path"]),
    "questions": ("One row per question Claude put to the user: AskUserQuestion, prose, or a printed checkpoint.", [
        ("qid", TEXT, None), ("session_id", TEXT, None), ("run_id", TEXT, None), ("skill", TEXT, None), ("version", TEXT, None),
        ("asked_at", TS, None), ("since_start_ms", BIG, None), ("turn", INT, None),
        ("channel", TEXT, "ask | prose | checkpoint"), ("form", TEXT, None),
        ("topic", TEXT, "The skill's own interview topic (checks/<skill>.json)"), ("topic_label", TEXT, None),
        ("de_topic", TEXT, "Data-engineering topic (semantics/questions.json)"), ("de_topic_label", TEXT, None),
        ("layer", TEXT, "Where in the data stack the question sits"), ("layer_label", TEXT, None),
        ("semantics_by", TEXT, "What decided topic/layer: header, question or fallback"),
        ("header", TEXT, None), ("question", TEXT, None), ("options", INT, None), ("multi", BOOL, None),
        ("recommended_label", TEXT, None), ("outcome", TEXT, None), ("answer", TEXT, None), ("typed", TEXT, None),
        ("reply", TEXT, None), ("notes", TEXT, None), ("feedback", TEXT, None), ("wait_ms", BIG, None), ("batch_size", INT, None),
        ("flags", TEXT, None), ("flag_count", INT, None), ("before_create", BOOL, None), ("reask_of", TEXT, None)],
        ["qid"]),
    "de_topics": ("The data-engineering topics questions are mapped to, in display order.", [
        ("id", TEXT, None), ("label", TEXT, None), ("description", TEXT, None), ("sort_order", INT, None)], ["id"]),
    "de_layers": ("The data-stack layers questions are mapped to, in display order.", [
        ("id", TEXT, None), ("label", TEXT, None), ("description", TEXT, None), ("sort_order", INT, None)], ["id"]),
    "question_options": ("One row per option offered with a question.", [
        ("qid", TEXT, None), ("session_id", TEXT, None), ("option_no", INT, None), ("label", TEXT, None),
        ("description", TEXT, None), ("recommended", BOOL, None), ("chosen", BOOL, None)], ["qid", "option_no"]),
    "subagents": ("One row per subagent or workflow agent.", [
        ("session_id", TEXT, None), ("agent_id", TEXT, None), ("kind", TEXT, None), ("agent_type", TEXT, None),
        ("description", TEXT, None), ("start_at", TS, None), ("duration_ms", BIG, None), ("requests", INT, None),
        ("tool_calls", INT, None), ("tool_errors", INT, None), ("input_tokens", BIG, None), ("output_tokens", BIG, None),
        ("cost_usd", NUM, None), ("models", TEXT, None)], ["session_id", "agent_id"]),
    "files_touched": ("One row per file a session read or changed.", [
        ("session_id", TEXT, None), ("path", TEXT, None), ("reads", INT, None), ("edits", INT, None), ("writes", INT, None),
        ("creates", INT, None), ("lines_added", INT, None), ("lines_removed", INT, None), ("errors", INT, None)],
        ["session_id", "path"]),
    "warehouse_load": ("The load that produced these tables: when, and from how many transcripts.", [
        ("loaded_at", TS, None), ("transcripts", INT, None), ("sessions", INT, None), ("since", TEXT, None),
        ("generator_version", TEXT, None)], ["loaded_at"]),
    "tool_errors": ("One row per failed tool call, with what it said.", [
        ("session_id", TEXT, None), ("error_no", INT, None), ("at", TS, None), ("turn", INT, None), ("tool", TEXT, None),
        ("scope", TEXT, None), ("category", TEXT, None), ("input", TEXT, None), ("message", TEXT, None)],
        ["session_id", "error_no"]),
}

VIEWS = """
CREATE VIEW v_daily AS
SELECT start_at::date AS day, count(*) AS sessions, sum(turns) AS turns, sum(api_requests) AS api_requests,
       sum(tool_calls) AS tool_calls, sum(tool_errors) AS tool_errors, round(sum(cost_usd), 2) AS cost_usd,
       sum(output_tokens) AS output_tokens, round(sum(active_ms) / 3600000.0, 2) AS active_hours,
       sum(skill_runs) AS skill_runs, sum(questions) AS questions
FROM sessions GROUP BY 1;

CREATE VIEW v_skill_versions AS
SELECT skill, version, min(version_date) AS version_date, max(version_subject) AS subject, count(*) AS runs,
       count(DISTINCT session_id) AS sessions, min(start_at) AS first_run, max(start_at) AS last_run,
       round(sum(cost_usd), 2) AS cost_usd,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY cost_usd) AS median_cost_usd,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY tool_calls) AS median_tool_calls,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY tool_errors) AS median_tool_errors,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY cli_calls) AS median_cli_calls,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY help_lookups) AS median_help_lookups,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY active_ms) / 60000.0 AS median_active_minutes,
       sum(questions_asked) AS questions_asked, sum(prose_questions) AS prose_questions,
       round(avg(questions_asked), 1) AS avg_questions_asked, round(avg(prose_questions), 1) AS avg_prose_questions,
       round(avg(question_rounds), 1) AS avg_question_rounds,
       sum(recommended_taken) AS recommended_taken, sum(recommended_offered) AS recommended_offered,
       round(sum(recommended_taken)::numeric / nullif(sum(recommended_offered), 0), 3) AS recommended_rate,
       sum(typed_answers) AS typed_answers,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY docs_read) AS median_docs_read,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY doc_tokens) AS median_doc_tokens,
       sum(checks_passed) AS checks_passed, sum(checks_failed) AS checks_failed
FROM skill_runs GROUP BY skill, version;

CREATE VIEW v_check_rates AS
SELECT c.skill, c.version, c.check_id, max(c.description) AS description,
       count(*) FILTER (WHERE c.status = 'pass') AS passed, count(*) FILTER (WHERE c.status = 'fail') AS failed,
       count(*) FILTER (WHERE c.status NOT IN ('pass', 'fail')) AS not_applicable,
       round(count(*) FILTER (WHERE c.status = 'pass')::numeric
             / nullif(count(*) FILTER (WHERE c.status IN ('pass', 'fail')), 0), 3) AS pass_rate
FROM skill_run_checks c GROUP BY c.skill, c.version, c.check_id;

CREATE VIEW v_question_topics AS
SELECT skill, version, topic, max(topic_label) AS topic_label, count(*) AS questions,
       count(*) FILTER (WHERE channel = 'ask') AS asked, count(*) FILTER (WHERE channel <> 'ask') AS in_prose,
       count(DISTINCT run_id) AS runs,
       count(*) FILTER (WHERE outcome = 'recommended') AS recommended_taken,
       count(*) FILTER (WHERE channel = 'ask' AND recommended_label IS NOT NULL AND NOT multi
                        AND outcome NOT IN ('declined', 'unanswered', 'interrupted', 'error')) AS recommended_offered,
       count(*) FILTER (WHERE typed IS NOT NULL) AS typed,
       count(*) FILTER (WHERE outcome IN ('no preference', 'declined', 'unanswered')) AS came_back_empty,
       count(*) FILTER (WHERE reask_of IS NOT NULL) AS asked_again,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY wait_ms) FILTER (WHERE channel = 'ask') / 1000.0 AS median_wait_s
FROM questions GROUP BY skill, version, topic;

CREATE VIEW v_question_semantics AS
SELECT q.skill, q.version, q.de_topic, t.label AS de_topic_label, t.sort_order AS de_topic_order,
       q.layer, l.label AS layer_label, l.sort_order AS layer_order,
       count(*) AS questions, count(*) FILTER (WHERE q.channel = 'ask') AS asked,
       count(*) FILTER (WHERE q.channel <> 'ask') AS in_prose, count(DISTINCT q.run_id) AS runs,
       count(*) FILTER (WHERE q.outcome = 'recommended') AS recommended_taken,
       count(*) FILTER (WHERE q.channel = 'ask' AND q.recommended_label IS NOT NULL AND NOT q.multi
                        AND q.outcome NOT IN ('declined', 'unanswered', 'interrupted', 'error')) AS recommended_offered,
       count(*) FILTER (WHERE q.typed IS NOT NULL) AS typed,
       count(*) FILTER (WHERE q.outcome IN ('no preference', 'declined', 'unanswered')) AS came_back_empty,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY q.wait_ms) FILTER (WHERE q.channel = 'ask') / 1000.0 AS median_wait_s
FROM questions q LEFT JOIN de_topics t ON t.id = q.de_topic LEFT JOIN de_layers l ON l.id = q.layer
GROUP BY q.skill, q.version, q.de_topic, t.label, t.sort_order, q.layer, l.label, l.sort_order;

CREATE VIEW v_interview_questions AS
SELECT q.qid, q.asked_at, q.skill, q.version, r.version_date, r.version_subject, q.run_id, q.session_id, s.project,
       CASE q.channel WHEN 'ask' THEN 'AskUserQuestion' WHEN 'prose' THEN 'In prose' ELSE 'Checkpoint' END AS channel,
       q.topic_label AS interview_topic,
       coalesce(t.label, q.de_topic_label) AS de_topic, coalesce(t.sort_order, 99) AS de_topic_order,
       coalesce(l.label, q.layer_label) AS layer, coalesce(l.sort_order, 99) AS layer_order,
       q.semantics_by AS classified_by, q.header, q.question, q.outcome,
       CASE WHEN q.channel <> 'ask' THEN 'in prose'
            WHEN q.outcome = 'recommended' THEN 'recommended option'
            WHEN q.outcome IN ('typed', 'typed + picked') THEN 'typed an answer'
            WHEN q.outcome IN ('other option', 'picked') THEN 'another option'
            WHEN q.outcome = 'no preference' THEN 'no preference'
            ELSE 'declined or unanswered' END AS outcome_group,
       o.offered AS recommendation_offered, o.offered AND q.outcome = 'recommended' AS took_recommendation,
       q.typed IS NOT NULL AS typed_answer,
       q.outcome IN ('no preference', 'declined', 'unanswered') AS came_back_empty,
       coalesce(q.typed, q.answer, q.reply, q.feedback) AS answer,
       CASE WHEN q.channel = 'ask' AND q.outcome NOT IN ('unanswered', 'declined')
            THEN round(q.wait_ms / 1000.0, 1) END AS wait_s,
       q.flags
FROM questions q
-- A recommendation counts as offered on a single-choice AskUserQuestion that came back with an answer.
CROSS JOIN LATERAL (SELECT coalesce(q.channel = 'ask' AND q.recommended_label IS NOT NULL AND NOT q.multi
                                    AND q.outcome NOT IN ('declined', 'unanswered', 'interrupted', 'error'),
                                    false) AS offered) o
LEFT JOIN de_topics t ON t.id = q.de_topic
LEFT JOIN de_layers l ON l.id = q.layer
LEFT JOIN skill_runs r ON r.run_id = q.run_id
LEFT JOIN sessions s ON s.session_id = q.session_id;

CREATE VIEW v_question_outcomes AS
SELECT skill, version, channel, outcome, count(*) AS questions, count(DISTINCT run_id) AS runs
FROM questions GROUP BY skill, version, channel, outcome;

CREATE VIEW v_typed_answers AS
SELECT q.skill, q.version, q.run_id, q.asked_at, q.topic_label, q.header, q.question, q.typed,
       string_agg(o.label, ' / ' ORDER BY o.option_no) AS options_offered
FROM questions q LEFT JOIN question_options o USING (qid)
WHERE q.typed IS NOT NULL
GROUP BY q.skill, q.version, q.run_id, q.asked_at, q.topic_label, q.header, q.question, q.typed;

CREATE VIEW v_question_flags AS
SELECT q.skill, q.version, trim(f) AS flag, count(*) AS questions, count(DISTINCT q.run_id) AS runs
FROM questions q, unnest(string_to_array(q.flags, ';')) AS f
WHERE q.flags IS NOT NULL AND q.flags <> ''
GROUP BY q.skill, q.version, trim(f);

CREATE VIEW v_skill_files AS
SELECT f.skill, f.version, f.owner, f.path, v.runs,
       count(DISTINCT f.run_id) FILTER (WHERE f.lines_seen > 0) AS runs_shown,
       count(*) FILTER (WHERE f.how IN ('full', 'injected', 'injected + re-read')) AS whole,
       count(*) FILTER (WHERE f.how IN ('partial', 'hits')) AS in_part,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY f.coverage) AS median_coverage,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY f.read_order) AS median_order,
       sum(f.tokens) AS tokens
FROM skill_run_files f
JOIN (SELECT skill, version, count(*) AS runs FROM skill_runs GROUP BY skill, version) v USING (skill, version)
GROUP BY f.skill, f.version, f.owner, f.path, v.runs;

CREATE VIEW v_cli_signatures AS
SELECT program, signature, count(*) AS uses, count(DISTINCT tool_use_id) AS bash_calls,
       count(DISTINCT session_id) AS sessions, count(DISTINCT run_id) AS runs,
       count(*) FILTER (WHERE status = 'error') AS errors,
       round(count(*) FILTER (WHERE status = 'error')::numeric / count(*), 3) AS error_rate,
       count(*) FILTER (WHERE is_help) AS help_lookups
FROM cli_calls GROUP BY program, signature;

CREATE VIEW v_tools AS
SELECT tool, category, count(*) AS calls, count(DISTINCT session_id) AS sessions,
       count(*) FILTER (WHERE status = 'error') AS errors, count(*) FILTER (WHERE status = 'denied') AS denied,
       round(count(*) FILTER (WHERE status = 'error')::numeric / count(*), 3) AS error_rate,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50_ms
FROM tool_calls GROUP BY tool, category;

CREATE VIEW v_models AS
SELECT model, count(*) AS requests, count(DISTINCT session_id) AS sessions, sum(input_tokens) AS input_tokens,
       sum(output_tokens) AS output_tokens, sum(cache_read_tokens) AS cache_read_tokens,
       round(sum(cost_usd), 2) AS cost_usd,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50_latency_ms
FROM api_requests GROUP BY model;
"""

VIEW_COMMENTS = {
    "v_daily": "Sessions, work and cost per day.",
    "v_skill_versions": "Each skill's versions compared: runs, medians, questions, checks.",
    "v_check_rates": "Pass rate of each declared check, per skill version.",
    "v_question_topics": "The interview by topic and version: how often asked, recommended taken, typed, waits.",
    "v_question_semantics": "Questions by data-engineering topic and layer, per skill version: counts, outcomes, waits.",
    "v_interview_questions": "One row per question, ready to explore: the data-engineering topic and layer it is about, "
                             "what came back (outcome_group), whether the recommended option was offered and taken, "
                             "typed answers, the wait. The Metabase model \"Interview questions\" reads it.",
    "v_question_outcomes": "What came back from questions, per skill version and channel.",
    "v_typed_answers": "Answers typed instead of picked, with the options that were offered.",
    "v_question_flags": "Questions against the skill's rules, by flag.",
    "v_skill_files": "Which skill documents each version's runs were shown.",
    "v_cli_signatures": "CLI commands by signature: uses, errors, --help lookups.",
    "v_tools": "Tool calls by tool: errors, denials, p50 duration.",
    "v_models": "API requests, tokens and cost per model.",
}


def schema_sql():
    """DROP and CREATE every table and view, with comments Metabase shows as descriptions."""
    out = ["-- Generated by session-analytics warehouse. Dropped and recreated on every load.",
           "DROP VIEW IF EXISTS " + ", ".join(VIEW_COMMENTS) + " CASCADE;",
           "DROP TABLE IF EXISTS " + ", ".join(TABLES) + " CASCADE;"]
    for name, (comment, cols, pk) in TABLES.items():
        body = ",\n  ".join(f"{c} {t}" for c, t, _ in cols) + f",\n  PRIMARY KEY ({', '.join(pk)})"
        out.append(f"CREATE TABLE {name} (\n  {body}\n);")
        out.append(f"COMMENT ON TABLE {name} IS {_lit(comment)};")
        out += [f"COMMENT ON COLUMN {name}.{c} IS {_lit(d)};" for c, _, d in cols if d]
    return "\n".join(out) + "\n"


def views_sql():
    return VIEWS + "\n".join(f"COMMENT ON VIEW {v} IS {_lit(d)};" for v, d in VIEW_COMMENTS.items()) + "\n"


def _lit(s):
    return "'" + str(s).replace("'", "''") + "'"


def _iso(ms):
    return util.iso(ms) if ms is not None else None


def _top(v):
    """The main value of a {value: count} dict, a list, or a string."""
    if isinstance(v, dict):
        return max(v.items(), key=lambda kv: kv[1] if isinstance(kv[1], (int, float)) else 0)[0] if v else None
    if isinstance(v, (list, tuple)):
        first = v[0] if v else None
        return first.get("model") or first.get("name") if isinstance(first, dict) else first
    return v


def _version(run):
    v = run.get("version") or {}
    return v.get("commit") or v.get("label")


def session_rows(a, s):
    """{table: [row, ...]} for one analyzed session (`a`) and its parsed transcript (`s`)."""
    sid = s.session_id
    se, tot, iv = a["session"], a["totals"], a.get("interview") or {}
    runs = a.get("skill_runs") or []
    steps = a["trace"]["steps"]
    run_of = {}
    for r in runs:
        for i in r["steps"]:
            st = steps[i]
            if st.get("k") == "tool" and st.get("id"):
                run_of.setdefault(st["id"], r["run_id"])
    rows = {k: [] for k in TABLES}
    rows["sessions"].append({
        "session_id": sid, "project": se.get("project_name"), "title": se.get("title"), "start_at": se.get("start"),
        "end_at": se.get("end"), "wall_ms": se.get("wall_ms"), "active_ms": tot.get("active_ms"),
        "turns": tot.get("turns"), "prompts": tot.get("prompts"), "api_requests": tot.get("api_requests"),
        "tool_calls": tot.get("tool_calls"), "tool_errors": tot.get("tool_errors"),
        "tool_denials": tot.get("tool_denials"), "subagents": tot.get("subagents"),
        "skills_invoked": tot.get("skills_invoked"), "skill_runs": len(runs), "questions": iv.get("total", 0),
        "questions_prose": iv.get("prose", 0), "input_tokens": tot.get("input_tokens"),
        "output_tokens": tot.get("output_tokens"), "cache_read_tokens": tot.get("cache_read_tokens"),
        "cache_write_tokens": tot.get("cache_write_tokens"), "cache_hit_ratio": tot.get("cache_hit_ratio"),
        "cost_usd": tot.get("estimated_cost_usd"), "reported_cost_usd": tot.get("reported_cost_usd"),
        "peak_context_tokens": tot.get("peak_context_tokens"), "compactions": tot.get("compactions"),
        "interruptions": tot.get("interruptions"), "files_modified": tot.get("files_modified"),
        "lines_added": tot.get("lines_added"), "lines_removed": tot.get("lines_removed"),
        "commits": tot.get("commits"), "pull_requests": tot.get("pull_requests"),
        "models": ", ".join(m.get("model") or "" for m in se.get("models") or ()),
        "claude_code_version": _top(se.get("claude_code_versions")), "entrypoint": _top(se.get("entrypoints")),
        "git_branch": _top(se.get("git_branches")), "cwd": se.get("cwd"), "transcript": se.get("transcript")})
    for t in a["turns"]["rows"]:
        rows["turns"].append({
            "session_id": sid, "turn": t["index"], "start_at": t.get("start"), "end_at": t.get("end"),
            "duration_ms": t.get("duration_ms"), "trigger": t.get("trigger"), "prompt": t.get("prompt"),
            "prompt_chars": t.get("prompt_chars"), "command": t.get("command"), "requests": t.get("requests"),
            "tool_calls": t.get("tool_calls"), "tool_errors": t.get("tool_errors"), "cost_usd": t.get("cost_usd"),
            "input_tokens": t.get("input_tokens"), "output_tokens": t.get("output_tokens"),
            "cache_read_tokens": t.get("cache_read_tokens"), "cache_write_tokens": t.get("cache_write_tokens"),
            "max_context_tokens": t.get("max_context_tokens"), "skills_invoked": _join(t.get("skills_invoked")),
            "interrupted": t.get("interrupted"), "compacted": t.get("compacted"),
            "permission_mode": t.get("permission_mode"), "stop_reason": t.get("stop_reason")})
    for r in a["requests"]["rows"]:
        rows["api_requests"].append({
            "session_id": sid, "request_no": r["i"], "at": r.get("ts"), "turn": r.get("turn"), "scope": r.get("scope"),
            "agent_id": r.get("agent_id"), "model": r.get("model"), "stop_reason": r.get("stop_reason"),
            "input_tokens": r.get("input"), "output_tokens": r.get("output"), "cache_read_tokens": r.get("cache_read"),
            "cache_write_5m_tokens": r.get("cache_write_5m"), "cache_write_1h_tokens": r.get("cache_write_1h"),
            "thinking_tokens": r.get("thinking"), "context_tokens": r.get("context"), "cost_usd": r.get("cost_usd"),
            "latency_ms": r.get("latency_ms"), "duration_ms": r.get("duration_ms"), "skill": r.get("skill"),
            "tools": _join(r.get("tools")), "effort": r.get("effort"), "cache_miss_reason": r.get("cache_miss_reason")})
    calls = s.tool_calls
    for t in a["tools"]["rows"]:
        c = calls.get(t["id"])
        cmd = c.input.get("command") if c is not None and c.name == "Bash" else None
        err = categorize_error(c.result_preview) if c is not None and c.status == "error" else None
        rows["tool_calls"].append({
            "session_id": sid, "tool_use_id": t["id"], "at": t.get("ts"), "turn": t.get("turn"), "scope": t.get("scope"),
            "agent_id": t.get("agent_id"), "tool": t.get("name"), "category": t.get("category"),
            "status": t.get("status"), "duration_ms": t.get("duration_ms"), "error_category": err,
            "program": primary_program(cmd)[0] if cmd else None, "run_id": run_of.get(t["id"]),
            "skill": t.get("skill"), "input": t.get("input"), "error": t.get("error"),
            "result_chars": t.get("result_chars"), "batch_size": t.get("batch_size")})
        if cmd:
            for j, x in enumerate(cli_calls(cmd)):
                rows["cli_calls"].append({
                    "session_id": sid, "tool_use_id": t["id"], "seq": j, "at": t.get("ts"), "run_id": run_of.get(t["id"]),
                    "program": x["program"], "signature": x["signature"], "is_help": x["help"], "status": c.status,
                    "error_category": err})
    for inv in a["skills"]["invocations"]:
        rows["skill_invocations"].append({
            "session_id": sid, "invocation_no": inv["i"], "at": inv.get("ts"), "turn": inv.get("turn"),
            "skill": inv.get("name"), "canonical": inv.get("canonical"), "mode": inv.get("mode"), "via": inv.get("via"),
            "scope": inv.get("scope"), "success": inv.get("success"), "status": inv.get("status"),
            "args": inv.get("args"), "content_chars": inv.get("content_chars")})
    for r in runs:
        v = r.get("version") or {}
        ver = _version(r)
        rv = r.get("interview") or {}
        rows["skill_runs"].append({
            "run_id": r["run_id"], "session_id": sid, "skill": r["skill"], "mode": r.get("mode"), "version": ver,
            "version_status": v.get("status"), "version_date": v.get("date"), "version_subject": v.get("subject"),
            "start_at": _iso(r.get("start_ms")), "end_at": _iso(r.get("end_ms")), "duration_ms": r.get("duration_ms"),
            "active_ms": r.get("active_ms"), "turns": r.get("turn_count"), "follow_up_turns": r.get("follow_up_turns"),
            "requests": r.get("requests"), "attributed_requests": r.get("attributed_requests"),
            "tool_calls": r.get("tool_calls"), "tool_errors": r.get("tool_errors"), "error_rate": r.get("error_rate"),
            "cli_calls": r.get("cli_calls"), "help_lookups": r.get("help_lookups"),
            "retries_after_error": r.get("retries_after_error"), "cost_usd": r.get("cost_usd"),
            "attributed_cost_usd": r.get("attributed_cost_usd"), "input_tokens": r.get("input_tokens"),
            "output_tokens": r.get("output_tokens"), "context_peak": r.get("context_peak"),
            "questions_asked": r.get("questions_asked"), "question_rounds": r.get("question_calls"),
            "prose_questions": r.get("prose_questions"), "recommended_offered": rv.get("recommended_offered"),
            "recommended_taken": rv.get("recommended_picked"), "recommended_rate": r.get("recommended_rate"),
            "typed_answers": r.get("typed_answers"), "unanswered_questions": r.get("unanswered_questions"),
            "question_wait_p50_ms": r.get("question_wait_p50_ms"), "questions_flagged": r.get("questions_flagged"),
            "questions_before_create": r.get("questions_before_create"), "docs_read": r.get("docs_read"),
            "docs_total": r.get("docs_total"), "cli_docs_read": r.get("cli_docs_read"), "doc_tokens": r.get("doc_tokens"),
            "doc_rereads": r.get("doc_rereads"), "doc_listings": r.get("doc_listings"),
            "checks_passed": r.get("checks_passed"), "checks_failed": r.get("checks_failed"),
            "objects_created": r.get("objects_created"), "end_reason": r.get("end_reason"), "prompt": r.get("prompt"),
            "prompt_key": r.get("prompt_key"), "args": r.get("args")})
        for ch in r.get("checks") or ():
            rows["skill_run_checks"].append({
                "run_id": r["run_id"], "session_id": sid, "skill": r["skill"], "version": ver, "check_id": ch.get("id"),
                "description": ch.get("desc"), "status": ch.get("status"), "detail": ch.get("detail")})
        for f in (r.get("skill_files") or {}).get("files") or ():
            rows["skill_run_files"].append({
                "run_id": r["run_id"], "session_id": sid, "skill": r["skill"], "version": ver, "owner": f["owner"],
                "path": f["path"], "kind": f.get("kind"), "read_order": f.get("order"), "how": f.get("how"),
                "coverage": f.get("coverage"), "lines_seen": f.get("seen"), "total_lines": f.get("total"),
                "accesses": f.get("accesses"), "reads": f.get("reads"), "searches": f.get("searches"),
                "rereads": f.get("rereads"), "tokens": f.get("tokens"), "first_read_ms": f.get("first_dt"),
                "found_by": f.get("found_by"), "named_by": _join(f.get("named_by")),
                "version_check": f.get("version"), "sections": _join(f.get("sections"), " · ")})
    run_version = {r["run_id"]: _version(r) for r in runs}
    for q in iv.get("questions") or ():
        rows["questions"].append({
            "qid": f"{sid[:8]}:{q['qid']}", "session_id": sid, "run_id": q.get("run_id"), "skill": q.get("skill"),
            "version": run_version.get(q.get("run_id")), "asked_at": _iso(q.get("t")), "since_start_ms": q.get("dt"),
            "turn": q.get("turn"), "channel": q.get("kind"), "form": q.get("form"), "topic": q.get("topic"),
            "topic_label": q.get("topic_label"), "de_topic": q.get("de_topic"), "de_topic_label": q.get("de_topic_label"),
            "layer": q.get("layer"), "layer_label": q.get("layer_label"), "semantics_by": q.get("semantics_by"),
            "header": q.get("header"), "question": q.get("question"),
            "options": len(q.get("options") or ()), "multi": q.get("multi"),
            "recommended_label": q.get("recommended_label"), "outcome": q.get("outcome"), "answer": q.get("answer"),
            "typed": q.get("typed"), "reply": q.get("reply"), "notes": q.get("notes"), "feedback": q.get("feedback"),
            "wait_ms": q.get("wait_ms"), "batch_size": q.get("batch_size"), "flags": _join(q.get("flags"), "; "),
            "flag_count": len(q.get("flags") or ()), "before_create": q.get("before_create"),
            "reask_of": f"{sid[:8]}:{q['reask_of']}" if q.get("reask_of") else None})
        for j, o in enumerate(q.get("options") or ()):
            rows["question_options"].append({
                "qid": f"{sid[:8]}:{q['qid']}", "session_id": sid, "option_no": j, "label": o.get("label"),
                "description": o.get("description"), "recommended": o.get("recommended"), "chosen": o.get("chosen")})
    for g in a["subagents"]["rows"]:
        rows["subagents"].append({
            "session_id": sid, "agent_id": g.get("agent_id"), "kind": g.get("kind"), "agent_type": g.get("type"),
            "description": g.get("description"), "start_at": g.get("start"), "duration_ms": g.get("duration_ms"),
            "requests": g.get("requests"), "tool_calls": g.get("tool_calls"), "tool_errors": g.get("tool_errors"),
            "input_tokens": g.get("input_tokens"), "output_tokens": g.get("output_tokens"), "cost_usd": g.get("cost_usd"),
            "models": _join(g.get("models"))})
    for f in a["files"]["rows"]:
        rows["files_touched"].append({
            "session_id": sid, "path": f.get("path"), "reads": f.get("reads"), "edits": f.get("edits"),
            "writes": f.get("writes"), "creates": f.get("creates"), "lines_added": f.get("lines_added"),
            "lines_removed": f.get("lines_removed"), "errors": f.get("errors")})
    for j, e in enumerate(a["errors"]["rows"]):
        rows["tool_errors"].append({
            "session_id": sid, "error_no": j, "at": e.get("ts"), "turn": e.get("turn"), "tool": e.get("tool"),
            "scope": e.get("scope"), "category": e.get("category"), "input": e.get("input"), "message": e.get("message")})
    return rows


def _join(v, sep=", "):
    if v is None:
        return None
    if isinstance(v, dict):
        return sep.join(str(k) for k in v)
    if isinstance(v, (list, tuple)):
        return sep.join(str(x) for x in v if x is not None)
    return str(v)


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v).replace("\x00", "")


def build(claude_dir=None, project=None, since="all", limit=5000, redact=True, pricing=None, now_ms=None, log=None):
    from . import __version__
    """Analyze every transcript in scope: ({table: rows}, meta)."""
    now = now_ms if now_ms is not None else time.time() * 1000
    cutoff = parse_since(since, now)
    cdir = locate.claude_dir(claude_dir)
    files = [f for f in locate.iter_transcripts(cdir, project=project) if f.stat().st_mtime * 1000 >= cutoff][:limit]
    pricing = pricing or Pricing()
    R = Redactor(redact)
    tables = {k: [] for k in TABLES}
    seen_keys = {k: set() for k in TABLES}
    failed = []
    for n, f in enumerate(files, 1):
        try:
            s = parse_session(f, own_only=True)
            if not s.requests and not s.turns:
                continue
            a = analyze(s, pricing, redactor=R, now_ms=now)
            for k, rs in session_rows(a, s).items():
                pk = TABLES[k][2]
                for r in rs:
                    key = tuple(r.get(c) for c in pk)
                    if key in seen_keys[k]:
                        continue  # a session file seen twice (a relocated copy): keep the first
                    seen_keys[k].add(key)
                    tables[k].append(r)
        except Exception as exc:  # one unreadable transcript must not sink the warehouse
            failed.append({"transcript": str(f), "error": f"{type(exc).__name__}: {exc}"})
        if log and n % 25 == 0:
            log(f"  {n}/{len(files)} transcripts")
    tables["de_topics"], tables["de_layers"] = semantics.load().dimensions()
    tables["warehouse_load"] = [{"loaded_at": util.iso(now), "transcripts": len(files),
                                  "sessions": len(tables["sessions"]), "since": since, "generator_version": __version__}]
    return tables, {"transcripts": len(files), "failed": failed, "cutoff": cutoff, "now": now}


def write_bundle(tables, out_dir):
    """schema.sql, views.sql and one CSV per table, ready for `psql` (or any COPY)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "schema.sql").write_text(schema_sql(), encoding="utf-8")
    (out / "views.sql").write_text(views_sql(), encoding="utf-8")
    counts = {}
    for name, (_, cols, _) in TABLES.items():
        with open(out / f"{name}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([c for c, _, _ in cols])
            for r in tables.get(name, ()):
                w.writerow([_cell(r.get(c)) for c, _, _ in cols])
        counts[name] = len(tables.get(name, ()))
    return counts


def psql_command(container=CONTAINER, database=DATABASE, user=USER, dsn=None):
    """How to reach psql: a local client with a DSN, else the one inside the Postgres container."""
    if dsn and shutil.which("psql"):
        return ["psql", dsn]
    if shutil.which("docker"):
        return ["docker", "exec", "-i", container, "psql", "-U", user, "-d", database]
    raise RuntimeError("no psql: install one and pass --dsn, or run Postgres in Docker (make warehouse-up)")


def running(container=CONTAINER):
    """Whether the local Postgres container is up (for --load=auto: the hook loads it only when it is)."""
    if not shutil.which("docker"):
        return False
    res = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", container], capture_output=True, text=True)
    return res.returncode == 0 and res.stdout.strip() == "true"


def up(compose=COMPOSE, wait=True):
    """Start the local Postgres from the repo's docker-compose.yml."""
    cmd = ["docker", "compose", "-f", str(compose), "up", "-d"] + (["--wait"] if wait else [])
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def load(out_dir, psql):
    """Recreate the tables, stream every CSV in, then create the views."""
    out = Path(out_dir)

    def run(args, stdin_text=None, stdin_file=None):
        res = subprocess.run(psql + ["-v", "ON_ERROR_STOP=1", "-q"] + args, input=stdin_text,
                             stdin=stdin_file, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError((res.stderr or res.stdout).strip())
        return res
    run(["-f", "-"], stdin_text=(out / "schema.sql").read_text(encoding="utf-8"))
    for name, (_, cols, _) in TABLES.items():
        with open(out / f"{name}.csv", encoding="utf-8") as fh:
            run(["-c", f"\\copy {name} ({', '.join(c for c, _, _ in cols)}) FROM STDIN WITH (FORMAT csv, HEADER true)"],
                stdin_file=fh)
    run(["-f", "-"], stdin_text=(out / "views.sql").read_text(encoding="utf-8"))
    run(["-c", "ANALYZE;"])


def run_warehouse(claude_dir=None, project=None, since="all", out_dir=None, do_load=False, start=False,
                  container=CONTAINER, dsn=None, redact=True, pricing=None, log=print, clickhouse_target=None,
                  clickhouse_identity=None):
    """Analyze once; write the files; load Postgres (do_load) and/or ClickHouse (clickhouse_target, as
    clickhouse_identity: only that source's rows are replaced). A target that fails does not stop the other: its
    error is in `errors`."""
    out = Path(out_dir) if out_dir else default_root() / "_warehouse" / datetime.now().strftime("%Y-%m-%d_%H%M")
    if start:
        log("Starting Postgres (docker compose up -d --wait)…")
        up()
    log("Analyzing transcripts…")
    tables, meta = build(claude_dir, project, since, redact=redact, pricing=pricing, log=log)
    counts = write_bundle(tables, out)
    res = {"out_dir": str(out), "counts": counts, "meta": meta, "loaded": False, "clickhouse": None, "errors": {},
           "connection": {"host": "127.0.0.1", "port": PORT, "database": DATABASE, "user": USER,
                          "from_docker": {"host": "host.docker.internal", "port": PORT}}}
    if do_load:
        log("Loading Postgres…")
        try:
            load(out, psql_command(container=container, dsn=dsn))
            res["loaded"] = True
        except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
            res["errors"]["postgres"] = (getattr(exc, "stderr", None) or str(exc)).strip()
    if clickhouse_target is not None:
        from . import clickhouse
        log(f"Loading ClickHouse ({clickhouse_target!r}) as {clickhouse_identity!r}…")
        try:
            res["clickhouse"] = {"target": repr(clickhouse_target), "identity": repr(clickhouse_identity),
                                 "counts": clickhouse.load(tables, clickhouse_target, clickhouse_identity, log=log)}
        except (clickhouse.ClickHouseError, OSError) as exc:
            res["errors"]["clickhouse"] = str(exc)
    return res
