"""A Postgres warehouse of every Claude Code session: the analytics as plain tables and views.

    session-analytics warehouse --up --load       start the local Postgres, analyze every transcript, load it
    make warehouse                                the same

Each table holds one kind of fact (a session, a turn, an API request, a tool call, a CLI call, a skill run, a
check result, a skill file a run was shown, a file a run wrote, a question and its options…), and each fact belongs
to exactly one
session: transcripts are read own-only, so the history a resumed session copies stays with the session it came
from. The views answer the tuning questions directly: versions compared, check pass rates, question topics,
CLI error rates, daily usage. Tables are dropped and recreated on every load; the transcripts are the source of
truth and the warehouse a disposable copy of what they say.

Loading needs no Python driver: CSVs are streamed into `psql` inside the Postgres container (`docker exec -i`),
or into a local `psql` when one is installed and a DSN is given.
"""

from __future__ import annotations

import contextlib
import csv
import json
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import instance, locate, semantics, skillruns, util
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
        ("session_id", TEXT, "Claude Code's session id"), ("project", TEXT, "Project directory name"),
        ("title", TEXT, "The session's title (its summary, or its first prompt)"),
        ("start_at", TS, "First event of the session"), ("end_at", TS, "Last event of the session"),
        ("wall_ms", BIG, "End minus start"),
        ("active_ms", BIG, "Time Claude or a tool was working (turn durations)"),
        ("turns", INT, "Turns: a prompt, command, notification or ! command and everything done before handing back"),
        ("prompts", INT, "Prompts the user typed"),
        ("api_requests", INT, "Claude API requests, subagents included"),
        ("tool_calls", INT, "Tool calls, subagents included"),
        ("tool_errors", INT, "Tool calls that returned an error"),
        ("tool_denials", INT, "Tool calls the user or a permission rule refused"),
        ("subagents", INT, "Subagents and workflow agents launched"),
        ("skills_invoked", INT, "Skill invocations: by Claude, by the user as /slash commands, or injected"),
        ("skill_runs", INT, "Skill runs (see skill_runs)"),
        ("questions", INT, "Questions Claude asked (AskUserQuestion and prose)"),
        ("questions_prose", INT, "Of those, questions asked in the text of a reply rather than through "
                                 "AskUserQuestion"),
        ("input_tokens", BIG, "Uncached input tokens"), ("output_tokens", BIG, "Output tokens, thinking included"),
        ("cache_read_tokens", BIG, "Input tokens read from the prompt cache"),
        ("cache_write_tokens", BIG, "Input tokens written to the prompt cache"),
        ("cache_hit_ratio", NUM, "Cache read / (input + cache read + cache write)"),
        ("cost_usd", NUM, "Estimated at list prices, API-equivalent"),
        ("reported_cost_usd", NUM, "Claude Code's own figure"),
        ("peak_context_tokens", BIG, "Largest context sent in one request (input + cache read + cache write)"),
        ("compactions", INT, "Times the conversation was compacted"),
        ("interruptions", INT, "Times the user interrupted Claude"),
        ("files_modified", INT, "Files edited or written"), ("lines_added", INT, "Lines added by edits and writes"),
        ("lines_removed", INT, "Lines removed by edits and writes"), ("commits", INT, "git commits made"),
        ("pull_requests", INT, "Pull requests created or updated"), ("models", TEXT, "Models used, comma-separated"),
        ("claude_code_version", TEXT, "Claude Code version (the most used, if several)"),
        ("entrypoint", TEXT, "How Claude Code was started: cli, desktop, sdk…"),
        ("git_branch", TEXT, "git branch (the most used, if several)"), ("cwd", TEXT, "Working directory"),
        ("transcript", TEXT, "Path of the transcript file"),
        ("prompt_key", TEXT, "The first prompt's opening words, lowercased, as skill_runs.prompt_key: a session "
                             "and a skill run given the same prompt share it"),
        ("baseline", BOOL, "The first prompt opened with `baseline:`: a direct agent given, without the skill, a "
                           "prompt the skill is compared on; shared with the skill's sessions")], ["session_id"]),
    "turns": ("One row per turn: a prompt and everything Claude did before handing back.", [
        ("session_id", TEXT, "The session (sessions.session_id)"),
        ("turn", INT, "Turn number within the session, from 0"), ("start_at", TS, "When the turn started"),
        ("end_at", TS, "When Claude handed back"),
        ("duration_ms", BIG, "How long the turn took"),
        ("trigger", TEXT, "prompt | command | task_notification | bash"),
        ("prompt", TEXT, "What was asked (redacted, truncated)"), ("prompt_chars", INT, "Length of the prompt"),
        ("command", TEXT, "The /slash command, when the turn is one"),
        ("requests", INT, "Claude API requests"), ("tool_calls", INT, "Tool calls"),
        ("tool_errors", INT, "Tool calls that returned an error"), ("cost_usd", NUM, "Estimated at list prices"),
        ("input_tokens", BIG, "Uncached input tokens"), ("output_tokens", BIG, "Output tokens"),
        ("cache_read_tokens", BIG, "Input tokens read from the prompt cache"),
        ("cache_write_tokens", BIG, "Input tokens written to the prompt cache"),
        ("max_context_tokens", BIG, "Largest context sent in one of the turn's requests"),
        ("skills_invoked", TEXT, "Skills invoked during the turn, comma-separated"),
        ("interrupted", BOOL, "The user interrupted the turn"), ("compacted", BOOL, "The conversation was compacted"),
        ("permission_mode", TEXT, "Permission mode the prompt was sent in: default, plan, acceptEdits, auto…"),
        ("stop_reason", TEXT, "Why the turn's last API response stopped: end_turn, tool_use, max_tokens…")],
        ["session_id", "turn"]),
    "api_requests": ("One row per Claude API request (streamed lines merged).", [
        ("session_id", TEXT, "The session (sessions.session_id)"),
        ("request_no", INT, "Request number within the session"), ("at", TS, "When the response started"),
        ("turn", INT, "The turn it belongs to"),
        ("scope", TEXT, "main or subagent"), ("agent_id", TEXT, "The subagent that sent it (subagents.agent_id)"),
        ("model", TEXT, "Model that answered"), ("stop_reason", TEXT, "end_turn | tool_use | max_tokens | …"),
        ("input_tokens", BIG, "Uncached input tokens"), ("output_tokens", BIG, "Output tokens, thinking included"),
        ("cache_read_tokens", BIG, "Input tokens read from the prompt cache"),
        ("cache_write_5m_tokens", BIG, "Input tokens written to the 5-minute prompt cache"),
        ("cache_write_1h_tokens", BIG, "Input tokens written to the 1-hour prompt cache"),
        ("thinking_tokens", BIG, "Output tokens spent thinking (estimated)"),
        ("context_tokens", BIG, "Tokens sent: input + cache read + cache write"),
        ("cost_usd", NUM, "Estimated at list prices"),
        ("latency_ms", BIG, "To the first content block"), ("duration_ms", BIG, "First to last streamed line"),
        ("skill", TEXT, "Claude Code's attribution"), ("tools", TEXT, "Tools this response called"),
        ("effort", TEXT, "Reasoning effort the request ran at"),
        ("cache_miss_reason", TEXT, "Why Claude Code says the prompt cache missed, when it did")],
        ["session_id", "request_no"]),
    "tool_calls": ("One row per tool call.", [
        ("session_id", TEXT, "The session (sessions.session_id)"), ("tool_use_id", TEXT, "The call's id"),
        ("at", TS, "When the call was made"), ("turn", INT, "The turn it belongs to"),
        ("scope", TEXT, "main, subagent or workflow"),
        ("agent_id", TEXT, "The subagent that made it"),
        ("tool", TEXT, "Tool name (mcp__<server>__<tool> for MCP tools)"),
        ("category", TEXT, "files | edits | search | shell | web | agents | skills | planning | outputs | mcp | other"),
        ("status", TEXT, "ok | error | denied | interrupted | pending"), ("duration_ms", BIG, "Call to result"),
        ("error_category", TEXT, "What kind of error: file_not_found, input_validation, permission, timeout…"),
        ("program", TEXT, "For Bash: the program the command is about"),
        ("run_id", TEXT, "The skill run the call belongs to"), ("skill", TEXT, "Claude Code's attribution"),
        ("input", TEXT, "Input summary (redacted, truncated)"), ("error", TEXT, "The error it returned (truncated)"),
        ("result_chars", INT, "Size of the result Claude was shown"),
        ("batch_size", INT, "Calls issued in parallel with it")], ["session_id", "tool_use_id"]),
    "cli_calls": ("One row per program invocation inside a Bash command, by signature (`mb transform create`).", [
        ("session_id", TEXT, "The session (sessions.session_id)"),
        ("tool_use_id", TEXT, "The Bash call it ran in (tool_calls.tool_use_id)"),
        ("seq", INT, "Position in the command"),
        ("at", TS, "When the Bash call was made"), ("run_id", TEXT, "The skill run the call belongs to"),
        ("program", TEXT, "The program: mb, git, jq…"),
        ("signature", TEXT, "Program and subcommands, without arguments: `mb transform create`, `git commit`"),
        ("is_help", BOOL, "A --help lookup"), ("status", TEXT, "Status of the whole Bash call"),
        ("error_category", TEXT, "What kind of error the Bash call returned")], ["session_id", "tool_use_id", "seq"]),
    "skill_invocations": ("One row per skill invocation.", [
        ("session_id", TEXT, "The session (sessions.session_id)"),
        ("invocation_no", INT, "Invocation number in the session"), ("at", TS, "When the skill was invoked"),
        ("turn", INT, "The turn it was invoked in"), ("skill", TEXT, "Skill name as invoked"),
        ("canonical", TEXT, "Skill name as Claude Code resolved it"),
        ("mode", TEXT, "model (Skill tool) | user (/slash) | harness"),
        ("via", TEXT, "Skill | SlashCommand (tools Claude called) | slash (typed by the user) | harness"),
        ("scope", TEXT, "main, subagent or workflow"), ("success", BOOL, "The skill loaded"),
        ("status", TEXT, "ok | error | denied | interrupted | forked"), ("args", TEXT, "Arguments it was invoked with"),
        ("content_chars", INT, "Size of the SKILL.md body injected")], ["session_id", "invocation_no"]),
    "skill_runs": ("One row per skill run: an invocation plus the follow-up turns it steered.", [
        ("run_id", TEXT, "<first 8 characters of the session id>:<run number in the session>"),
        ("session_id", TEXT, "The session (sessions.session_id)"), ("skill", TEXT, "The skill that ran"),
        ("mode", TEXT, "model (Skill tool) | user (/slash) | harness"),
        ("version", TEXT, "Git commit that ran (or a label when unknown)"),
        ("version_status", TEXT, "How the version was found: commit | working-tree | installed | unknown"),
        ("version_date", TS, "Date of the version's commit"),
        ("version_subject", TEXT, "Subject of the version's commit"), ("start_at", TS, "When the skill was invoked"),
        ("end_at", TS, "Last request or tool result of the run"),
        ("duration_ms", BIG, "Start to the run's last event"),
        ("active_ms", BIG, "Time Claude or a tool was working in the run's turns"),
        ("turns", INT, "Turns the run spans"),
        ("follow_up_turns", INT, "Of those, turns where no request was attributed to the skill"),
        ("requests", INT, "Claude API requests in the run, subagents included"),
        ("attributed_requests", INT, "Of those, requests Claude Code attributed to the skill"),
        ("tool_calls", INT, "Tool calls in the run, subagents included"),
        ("tool_errors", INT, "Tool calls that returned an error"),
        ("error_rate", NUM, "tool_errors / tool_calls"),
        ("cli_calls", INT, "Bash calls running a CLI subcommand (`mb …`, `gh …`), main thread"),
        ("help_lookups", INT, "--help lookups of a CLI subcommand"),
        ("retries_after_error", INT, "Bash calls that ran a subcommand again right after it failed"),
        ("cost_usd", NUM, "Estimated at list prices, all the run's requests"),
        ("attributed_cost_usd", NUM, "Estimated cost of the requests attributed to the skill"),
        ("input_tokens", BIG, "Uncached input tokens"), ("output_tokens", BIG, "Output tokens"),
        ("context_peak", BIG, "Largest context sent in one main-thread request"),
        ("questions_asked", INT, "Through AskUserQuestion"),
        ("question_rounds", INT, "AskUserQuestion calls (one can ask several questions)"),
        ("prose_questions", INT, "Questions asked in the text of a reply"),
        ("recommended_offered", INT, "Answered single-choice questions that offered a recommended option"),
        ("recommended_taken", INT, "Of those, questions where the recommended option was picked"),
        ("recommended_rate", NUM, "recommended_taken / recommended_offered"),
        ("typed_answers", INT, "Questions answered by typing rather than picking an option"),
        ("unanswered_questions", INT, "AskUserQuestion questions declined or left unanswered"),
        ("question_wait_p50_ms", BIG, "Median wait for an answer to AskUserQuestion"),
        ("questions_flagged", INT, "Questions that broke one of the skill's rules (see questions.flags)"),
        ("questions_before_create", INT, "AskUserQuestion questions asked before the first CLI `create`"),
        ("docs_read", INT, "The skill's own files shown, beyond SKILL.md"),
        ("docs_total", INT, "Files the skill ships"),
        ("cli_docs_read", INT, "Documents of other skills or CLIs the run touched"),
        ("doc_tokens", BIG, "Estimated tokens of skill documents shown (characters / 4)"),
        ("doc_rereads", INT, "Times a skill document was read again"),
        ("doc_listings", INT, "Listings of a skill directory"),
        ("checks_passed", INT, "Declared checks (checks/<skill>.json) the run passed; n/a counts in neither"),
        ("checks_failed", INT, "Declared checks the run failed; one failure when the checks file would not load"),
        ("objects_created", INT, "Objects a CLI `create` reported creating (Metabase transforms, cards…)"),
        ("files_written", INT, "Distinct files the run wrote: Write, Edit, or the shell (heredoc, redirect, tee, cp…)"),
        ("files_created", INT, "Of them, the files the run created"),
        ("support_files", INT, "Files the run created that the skill does not name (checks/<skill>.json \"files\") and "
                               "that are not Claude Code's memory: made to do what the skill did not; empty when the "
                               "skill names no files"),
        ("support_scripts", INT, "Of the support files, scripts: code the agent wrote, or a file it ran"),
        ("support_script_runs", INT, "Times the run executed its own scripts"),
        ("inline_scripts", INT, "Programs of two lines or more handed to an interpreter without a file "
                                "(python3 - <<'PY', python3 -c)"),
        ("inline_script_lines", INT, "Lines in those programs"),
        ("temp_files", INT, "Files the run created in a system temp directory"),
        ("memory_notes", INT, "Claude Code memory files the run wrote"),
        ("end_reason", TEXT, "What ended the run: next skill: <name> | session end | session still live | agent end "
                             "| harness-injected skill"),
        ("prompt", TEXT, "What the user asked when the run started (redacted, truncated)"),
        ("prompt_key", TEXT, "The prompt's opening words, lowercased: runs of one prompt share it"),
        ("args", TEXT, "Arguments the skill was invoked with")], ["run_id"]),
    "skill_run_checks": ("One row per run and declared check (checks/<skill>.json).", [
        ("run_id", TEXT, "The run (skill_runs.run_id)"), ("session_id", TEXT, "The session (sessions.session_id)"),
        ("skill", TEXT, "The skill that ran"), ("version", TEXT, "The version that ran (skill_runs.version)"),
        ("check_id", TEXT, "The check's id in the checks file"),
        ("description", TEXT, "What the check expects"), ("status", TEXT, "pass | fail | n/a | error"),
        ("detail", TEXT, "Why: the event that broke it, or the count")],
        ["run_id", "check_id"]),
    "skill_run_files": ("One row per run and skill document it touched, measured by what Claude was shown.", [
        ("run_id", TEXT, "The run (skill_runs.run_id)"), ("session_id", TEXT, "The session (sessions.session_id)"),
        ("skill", TEXT, "The skill that ran"), ("version", TEXT, "The version that ran (skill_runs.version)"),
        ("owner", TEXT, "The skill, another skill, or a CLI (mb)"), ("path", TEXT, "Path within the owner's directory"),
        ("kind", TEXT, "The file's directory in the skill (playbooks, references, . for the top), or its owner"),
        ("read_order", INT, "0 is SKILL.md's injection"),
        ("how", TEXT, "full | partial | hits | injected | injected + re-read | not shown | no hits | missing"),
        ("coverage", NUM, "Share of lines shown"),
        ("lines_seen", INT, "Distinct lines shown"), ("total_lines", INT, "Lines in the file"),
        ("accesses", INT, "Reads, searches and listings of it"), ("reads", INT, "Reads of it"),
        ("searches", INT, "Searches that looked in it"), ("rereads", INT, "Reads after the first"),
        ("tokens", INT, "Estimated tokens shown (characters / 4)"),
        ("first_read_ms", BIG, "From the run's start to the first access"),
        ("found_by", TEXT, "How Claude got to it: named | listing | search | resolved path | unprompted"),
        ("named_by", TEXT, "Documents shown earlier in the run that name it"),
        ("version_check", TEXT, "Whether the text shown is the run's version: match | differs | changed since | a "
                                "commit"),
        ("sections", TEXT, "Headings of the sections shown, when not the whole file")],
        ["run_id", "owner", "path"]),
    "skill_run_working_files": (
        "One row per run and file it wrote — with Write, Edit, or the shell (a heredoc into a file, a redirect, tee, "
        "cp, curl -o) — and whether the skill names it (checks/<skill>.json \"files\"): a file the run created that "
        "the skill does not name, and that is not Claude Code's memory, is a support file.", [
            ("run_id", TEXT, "The run (skill_runs.run_id)"), ("session_id", TEXT, "The session (sessions.session_id)"),
            ("skill", TEXT, "The skill that ran"), ("version", TEXT, "The version that ran (skill_runs.version)"),
            ("path", TEXT, "Relative to the project, ~ for home, else absolute"), ("name", TEXT, "File name"),
            ("ext", TEXT, "File extension"),
            ("kind", TEXT, "script | sql | json | data | doc | env | other (a file the run ran is a script)"),
            ("location", TEXT, "project | temp (a system temp directory) | memory (Claude Code's) | home | other"),
            ("expected", TEXT, "Which of the working files the skill names it is, when it names it"),
            ("expected_label", TEXT, "Display name of the working file it is"),
            ("created", BOOL, "The run created it: a Write that created it, or a shell write to a path the session had "
                              "not read or written before"),
            ("support", BOOL, "Created by the run, not named by the skill, not Claude Code's memory: made to do what "
                              "the skill did not; empty when the skill names no files"),
            ("via", TEXT, "How the run first wrote it: Write, Edit, heredoc, redirect, append, tee, copy, move, touch, "
                          "download, shell"),
            ("first_at", TS, "When the run first wrote it"),
            ("since_start_ms", BIG, "From the run's start to its first write"),
            ("turn", INT, "The turn it was first written in"), ("scope", TEXT, "main, subagent or workflow"),
            ("writes", INT, "Whole writes of it (Write, or the shell)"),
            ("edits", INT, "Edits of it"),
            ("lines", INT, "At its last whole write, when the text is in the transcript"),
            ("runs", INT, "Times the run executed or sourced it"),
            ("used_by", TEXT, "What read it afterwards: mb transform create, jq, Read…"),
            ("drives", TEXT, "For a script: the CLI commands in its text (mb card create…)"),
            ("api", TEXT, "For a script: the HTTP API paths it calls (/api/card…), or http")],
        ["run_id", "path"]),
    "questions": ("One row per question Claude put to the user: AskUserQuestion, prose, or a printed checkpoint.", [
        ("qid", TEXT, "<first 8 characters of the session id>:<question id>"),
        ("session_id", TEXT, "The session (sessions.session_id)"), ("run_id", TEXT, "The skill run it was asked in"),
        ("skill", TEXT, "The skill of that run"), ("version", TEXT, "The version of that run"),
        ("asked_at", TS, "When it was asked"), ("since_start_ms", BIG, "From the run's start to the question"),
        ("turn", INT, "The turn it was asked in"),
        ("channel", TEXT, "ask | prose | checkpoint"),
        ("form", TEXT, "choice | confirm | multi (AskUserQuestion) | prose | checkpoint | offer"),
        ("topic", TEXT, "The skill's own interview topic (checks/<skill>.json)"),
        ("topic_label", TEXT, "The interview topic's display name"),
        ("de_topic", TEXT, "Data-engineering topic (semantics/questions.json)"),
        ("de_topic_label", TEXT, "The data-engineering topic's display name"),
        ("layer", TEXT, "Where in the data stack the question sits"), ("layer_label", TEXT, "The layer's display name"),
        ("semantics_by", TEXT, "What decided topic/layer: header, question or fallback"),
        ("header", TEXT, "AskUserQuestion's short header"), ("question", TEXT, "The question (redacted, truncated)"),
        ("options", INT, "Options offered"), ("multi", BOOL, "Several options could be picked"),
        ("recommended_label", TEXT, "The option marked recommended"),
        ("outcome", TEXT, "recommended | other option | picked | typed | typed + picked | no preference | declined | "
                          "unanswered | interrupted | error; prose: replied | accepted | turned down | unanswered"),
        ("answer", TEXT, "The answer AskUserQuestion returned"), ("typed", TEXT, "Text typed instead of an option"),
        ("reply", TEXT, "For prose and checkpoints: the next prompt"),
        ("notes", TEXT, "Notes the user added to the answer"), ("feedback", TEXT, "What the user said when declining"),
        ("wait_ms", BIG, "From the question to the answer, or to the next prompt"),
        ("batch_size", INT, "Questions in the same AskUserQuestion call"),
        ("flags", TEXT, "The skill's rules it broke, semicolon-separated"), ("flag_count", INT, "Number of flags"),
        ("before_create", BOOL, "Asked before the run's first CLI `create`"),
        ("reask_of", TEXT, "The earlier question it repeats (questions.qid)")],
        ["qid"]),
    "de_topics": ("The data-engineering topics questions are mapped to, in display order.", [
        ("id", TEXT, "Topic id (questions.de_topic)"), ("label", TEXT, "Display name"),
        ("description", TEXT, "What questions on this topic are about"), ("sort_order", INT, "Display order")], ["id"]),
    "de_layers": ("The data-stack layers questions are mapped to, in display order.", [
        ("id", TEXT, "Layer id (questions.layer)"), ("label", TEXT, "Display name"),
        ("description", TEXT, "What the layer covers"), ("sort_order", INT, "Display order")], ["id"]),
    "question_options": ("One row per option offered with a question.", [
        ("qid", TEXT, "The question (questions.qid)"), ("session_id", TEXT, "The session (sessions.session_id)"),
        ("option_no", INT, "Position among the options, from 0"), ("label", TEXT, "The option's label"),
        ("description", TEXT, "The option's description"), ("recommended", BOOL, "Marked recommended"),
        ("chosen", BOOL, "The user picked it")], ["qid", "option_no"]),
    "subagents": ("One row per subagent or workflow agent.", [
        ("session_id", TEXT, "The session (sessions.session_id)"), ("agent_id", TEXT, "The agent's id"),
        ("kind", TEXT, "subagent or workflow"), ("agent_type", TEXT, "Agent type: general-purpose, Explore…"),
        ("description", TEXT, "The task it was given (short)"), ("start_at", TS, "Its first event"),
        ("duration_ms", BIG, "First to last event"), ("requests", INT, "Claude API requests"),
        ("tool_calls", INT, "Tool calls"), ("tool_errors", INT, "Tool calls that returned an error"),
        ("input_tokens", BIG, "Uncached input tokens"), ("output_tokens", BIG, "Output tokens"),
        ("cost_usd", NUM, "Estimated at list prices"),
        ("models", TEXT, "Models used, comma-separated")], ["session_id", "agent_id"]),
    "files_touched": ("One row per file a session read or changed.", [
        ("session_id", TEXT, "The session (sessions.session_id)"), ("path", TEXT, "File path"), ("reads", INT, "Reads"),
        ("edits", INT, "Edits"), ("writes", INT, "Whole-file writes"),
        ("creates", INT, "Writes that created the file"), ("lines_added", INT, "Lines added"),
        ("lines_removed", INT, "Lines removed"), ("errors", INT, "Calls on it that returned an error")],
        ["session_id", "path"]),
    "run_instances": ("One row per skill run and Metabase instance it used: the snapshot taken when its session "
                      "ended (instance.py).", [
        ("session_id", TEXT, "The session (sessions.session_id)"), ("run_id", TEXT, "The run (skill_runs.run_id)"),
        ("host", TEXT, "The instance: host and port, never a path or a credential"),
        ("profile", TEXT, "The mb CLI profile the run used"), ("captured_at", TS, "When the snapshot was taken"),
        ("reachable", BOOL, "The instance answered; when not, nothing below it was captured"),
        ("error", TEXT, "Why it did not answer"),
        ("skipped", TEXT, "What the instance could not list (a feature it lacks), with why"),
        ("objects_created", INT, "Objects the run created (run_artifacts, in_run = created)"),
        ("objects_changed", INT, "Objects created earlier that the run changed"),
        ("source_tables", INT, "Source tables profiled (run_source_tables)")], ["session_id", "run_id", "host"]),
    "run_artifacts": ("One row per Metabase object a skill run created or changed: its definition, and whether it "
                      "works, as the instance held it when the session ended.", [
        ("session_id", TEXT, "The session (sessions.session_id)"), ("run_id", TEXT, "The run (skill_runs.run_id)"),
        ("host", TEXT, "The instance (run_instances.host)"),
        ("kind", TEXT, "question | model | metric | transform | transform_test | dashboard | measure | segment | "
                       "document"),
        ("object_id", BIG, "Its id in that instance"), ("name", TEXT, "Its name"),
        ("in_run", TEXT, "created: the run made it; changed: it existed and the run changed it"),
        ("created_at", TS, "When it was created"), ("updated_at", TS, "When it was last changed"),
        ("collection_id", BIG, "Its collection"), ("description", TEXT, "Its description (truncated)"),
        ("query_kind", TEXT, "native (SQL) | mbql (query builder, metrics and measures by id)"),
        ("definition", TEXT, "Its SQL, or its MBQL as JSON (truncated)"), ("display", TEXT, "Questions: the chart"),
        ("database_id", BIG, "The database it reads or writes"),
        ("dashboard_id", BIG, "A question saved inside a dashboard: that dashboard"),
        ("target_table", TEXT, "Transforms: the table it writes, schema.table"),
        ("last_run_status", TEXT, "Transforms: how its last run ended"),
        ("transform_tests", INT, "Transforms: transform tests the run wrote for it"),
        ("tabs", INT, "Dashboards: tabs"), ("dashboard_filters", INT, "Dashboards: filters"),
        ("dashcards", INT, "Dashboards: cards, text included"), ("card_dashcards", INT, "Dashboards: cards of a question"),
        ("text_dashcards", INT, "Dashboards: text and heading cards"),
        ("unmapped_dashcards", INT, "Dashboards with filters: question cards wired to none of them"),
        ("on_dashboards", INT, "Questions: the run's dashboards showing it"),
        ("uses_metric", BOOL, "Questions: aggregates a metric or measure by id"),
        ("run_status", TEXT, "Questions, models, metrics: how running it ended (completed | failed)"),
        ("row_count", BIG, "Rows it returned"), ("run_error", TEXT, "What running it said, when it failed")],
        ["session_id", "run_id", "host", "kind", "object_id"]),
    "run_artifact_checks": ("One row per object a skill run made and check on it (instance.CHECKS): does it run, "
                            "is it described, tested, wired, reused.", [
        ("session_id", TEXT, "The session (sessions.session_id)"), ("run_id", TEXT, "The run (skill_runs.run_id)"),
        ("host", TEXT, "The instance (run_instances.host)"), ("kind", TEXT, "The object's kind (run_artifacts.kind)"),
        ("object_id", BIG, "The object (run_artifacts.object_id)"), ("check_id", TEXT, "The check"),
        ("status", TEXT, "pass | fail"), ("detail", TEXT, "Why it failed")],
        ["session_id", "run_id", "host", "kind", "object_id", "check_id"]),
    "run_source_tables": ("One row per source table a skill run built on, profiled from Metabase's metadata: its "
                          "size and shape, never a value it holds.", [
        ("session_id", TEXT, "The session (sessions.session_id)"), ("run_id", TEXT, "The run (skill_runs.run_id)"),
        ("host", TEXT, "The instance (run_instances.host)"), ("table_id", BIG, "The table's id in that instance"),
        ("db_id", BIG, "Its database"), ("schema_name", TEXT, "Its schema"), ("table_name", TEXT, "Its name"),
        ("rows", BIG, "Rows (Metabase's estimate, else counted)"), ("columns", INT, "Columns"),
        ("pk_columns", INT, "Primary-key columns"), ("fk_columns", INT, "Foreign-key columns"),
        ("numeric_columns", INT, "Number columns"), ("temporal_columns", INT, "Date and time columns"),
        ("text_columns", INT, "Text columns"), ("boolean_columns", INT, "True/false columns"),
        ("json_columns", INT, "JSON, array and dictionary columns"),
        ("text_json_columns", INT, "Text columns that mostly hold JSON"),
        ("coerced_columns", INT, "Columns Metabase reads as another type (a date kept as text or a number)"),
        ("empty_columns", INT, "Columns with no value at all"),
        ("mostly_empty_columns", INT, "Columns empty in half the rows or more"),
        ("max_null_share", NUM, "The largest share of empty values in one column")],
        ["session_id", "run_id", "host", "table_id"]),
    "warehouse_load": ("The load that produced these tables: when, and from how many transcripts.", [
        ("loaded_at", TS, "When the load ran"), ("transcripts", INT, "Transcripts read"),
        ("sessions", INT, "Sessions loaded"), ("since", TEXT, "How far back transcripts were read (--since)"),
        ("generator_version", TEXT, "Version of session-analytics that loaded it"),
        ("skills", TEXT, "Which sessions it holds: those that invoked these skills, or * for every session")],
        ["loaded_at"]),
    "tool_errors": ("One row per failed tool call, with what it said.", [
        ("session_id", TEXT, "The session (sessions.session_id)"), ("error_no", INT, "Error number in the session"),
        ("at", TS, "When the call was made"), ("turn", INT, "The turn it happened in"), ("tool", TEXT, "Tool name"),
        ("scope", TEXT, "main, subagent or workflow"),
        ("category", TEXT, "What kind of error: file_not_found, input_validation, permission, timeout…"),
        ("input", TEXT, "Input summary (redacted, truncated)"), ("message", TEXT, "What the tool said (truncated)")],
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


# A session whose first prompt opens with `baseline:` is a direct agent given, without the skill, a prompt the skill is
# compared on: shared with the skill's sessions, keyed by its prompt without the marker.
BASELINE_RE = re.compile(r"^\s*baseline\s*:\s*", re.I)


def _first_prompt(turns):
    """The prompt that opened the session: its first typed prompt, or `/command args`."""
    first = next((t for t in turns if t.get("trigger") in ("prompt", "command")), None)
    if first is None:
        return None
    if first.get("trigger") == "command":
        return f"{first.get('command') or ''} {first.get('command_args') or ''}"
    return first.get("prompt")


def _first_prompt_key(turns):
    """The key of the prompt that opened the session, as a skill run keys its own (a baseline's marker left out): a
    session given a run's prompt without the skill is compared with that run."""
    return skillruns.prompt_key(BASELINE_RE.sub("", _first_prompt(turns) or ""))


def is_baseline(turns):
    return bool(BASELINE_RE.match(_first_prompt(turns) or ""))


def baseline_run(a, s):
    """A baseline session as one run, `<id>:0`, for its instance snapshot: the whole session, every tool call in it, and
    the prompt that opened it (a direct agent may never name an mb profile: its instance is the one the prompt
    names). None when the session has no start."""
    start = util.parse_ts(a["session"].get("start"))
    if start is None:
        return None
    return {"run_id": f"{s.session_id[:8]}:0", "session_id": s.session_id, "baseline": True, "start_ms": start,
            "end_ms": util.parse_ts(a["session"].get("end")) or start, "prompt": _first_prompt(a["turns"]["rows"]),
            "steps": list(range(len(a["trace"]["steps"])))}


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
        "git_branch": _top(se.get("git_branches")), "cwd": se.get("cwd"), "transcript": se.get("transcript"),
        "prompt_key": _first_prompt_key(a["turns"]["rows"]), "baseline": is_baseline(a["turns"]["rows"])})
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
            "objects_created": r.get("objects_created"), "files_written": len(r.get("files_written") or ()),
            "files_created": r.get("files_created"), "support_files": r.get("support_files"),
            "support_scripts": r.get("support_scripts"), "support_script_runs": r.get("support_script_runs"),
            "inline_scripts": r.get("inline_scripts"), "inline_script_lines": r.get("inline_script_lines"),
            "temp_files": r.get("temp_files"), "memory_notes": r.get("memory_notes"),
            "end_reason": r.get("end_reason"), "prompt": r.get("prompt"),
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
        for w in r.get("working_files") or ():
            rows["skill_run_working_files"].append({
                "run_id": r["run_id"], "session_id": sid, "skill": r["skill"], "version": ver, "path": w["path"],
                "name": w.get("name"), "ext": w.get("ext"), "kind": w.get("kind"), "location": w.get("location"),
                "expected": w.get("expected"), "expected_label": w.get("expected_label"), "created": w.get("created"),
                "support": w.get("support"), "via": w.get("via"), "first_at": _iso(w.get("first_t")),
                "since_start_ms": w.get("dt"), "turn": w.get("turn"), "scope": w.get("scope"),
                "writes": w.get("writes"), "edits": w.get("edits"), "lines": w.get("lines"), "runs": w.get("runs"),
                "used_by": _join(w.get("used_by")) or None, "drives": _join(w.get("drives")) or None,
                "api": _join(w.get("api")) or None})
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


def build(claude_dir=None, project=None, since="all", limit=5000, redact=True, pricing=None, now_ms=None, log=None,
          transcripts=None, capture=None, instances_root=None, capture_skills=None):
    """Analyze every transcript in scope — or only `transcripts` (main transcript paths): ({table: rows}, meta). For
    the sessions in `capture` (ids), first snapshot what their runs of `capture_skills` built in Metabase
    (instance.py); every session's snapshots on disk load with it."""
    from . import __version__
    now = now_ms if now_ms is not None else time.time() * 1000
    cutoff = parse_since(since, now)
    cdir = locate.claude_dir(claude_dir)
    if transcripts is not None:
        files = [Path(t) for t in transcripts if Path(t).is_file()]
    else:
        files = [f for f in locate.iter_transcripts(cdir, project=project) if f.stat().st_mtime * 1000 >= cutoff][:limit]
    pricing = pricing or Pricing()
    R = Redactor(redact)
    tables = {k: [] for k in TABLES}
    seen_keys = {k: set() for k in TABLES}
    failed = []
    read = set()  # the sessions whose transcripts read cleanly, empty ones included
    versions = {}  # run_id: what its version was resolved from (share files carry it, to be labelled elsewhere)
    for n, f in enumerate(files, 1):
        try:
            s = parse_session(f, own_only=True)
            if not s.requests and not s.turns:
                read.add(s.session_id)
                continue
            a = analyze(s, pricing, redactor=R, now_ms=now)
            if capture is not None and s.session_id in capture:
                base = baseline_run(a, s) if is_baseline(a["turns"]["rows"]) else None
                runs = dict(a, skill_runs=[*(a.get("skill_runs") or ()), base]) if base else a
                try:
                    instance.capture_session(runs, s, now, root=instances_root, log=log,
                                             wanted=lambda run: run.get("baseline")
                                             or _captures(run, capture_skills or DEFAULT_SKILLS))
                except instance.MbError as exc:  # no CLI, or it cannot list its profiles: load what there is
                    if log:
                        log(f"  no instance snapshot for {s.session_id[:8]}: {exc}")
            rows = session_rows(a, s)
            rows.update(instance.session_rows(s.session_id, R, instances_root))
            for k, rs in rows.items():
                pk = TABLES[k][2]
                for r in rs:
                    key = tuple(r.get(c) for c in pk)
                    if key in seen_keys[k]:
                        continue  # a session file seen twice (a relocated copy): keep the first
                    seen_keys[k].add(key)
                    tables[k].append(r)
            for r in a.get("skill_runs") or ():
                v = r.get("version") or {}
                versions.setdefault(r["run_id"], {
                    "session_id": s.session_id, "skill": r["skill"], "fingerprint": r.get("fingerprint"),
                    "invoked_ms": r.get("invoked_ms"), "read_hashes": r.get("read_hashes") or {},
                    "status": v.get("status"), "label": v.get("label")})
            read.add(s.session_id)
        except Exception as exc:  # one unreadable transcript must not sink the warehouse
            failed.append({"transcript": str(f), "error": f"{type(exc).__name__}: {exc}"})
        if log and n % 25 == 0:
            log(f"  {n}/{len(files)} transcripts")
    tables["de_topics"], tables["de_layers"] = semantics.load().dimensions()
    tables["warehouse_load"] = [{"loaded_at": util.iso(now), "transcripts": len(files),
                                  "sessions": len(tables["sessions"]), "since": since, "generator_version": __version__,
                                  "skills": "*"}]
    return tables, {"transcripts": len(files), "failed": failed, "cutoff": cutoff, "now": now, "read": read,
                    "files": files, "versions": versions}


DEFAULT_SKILLS = ("rde",)  # the shared warehouse's scope when nothing else is set: sessions that ran the rde skill


def _skill_matches(name, skills):
    """An invocation's name against the wanted skills: `rde` matches rde and any plugin's `…:rde`; a wanted name with
    a plugin prefix (`agent-skills:rde`) matches only that."""
    if not name:
        return False
    return any(name == w or (":" not in w and name.rsplit(":", 1)[-1] == w) for w in skills)


def _captures(run, skills):
    """A run whose instance gets a snapshot: one of the skills the warehouse shares (`*`: every skill)."""
    return "*" in skills or _skill_matches(run.get("skill") or "", skills)


def qualifies(invocations, skills):
    """Whether a session's skill invocations put it in scope: one of `skills` ran — the call completed and succeeded.
    A Skill call the user rejected, one that failed, and one still waiting at the permission prompt (no result yet:
    success is NULL) never ran the skill. `*` takes every session."""
    if "*" in skills:
        return True
    return any(inv.get("success") is True and (inv.get("status") or "ok") in ("ok", "forked")
               and (_skill_matches(inv.get("skill"), skills) or _skill_matches(inv.get("canonical"), skills))
               for inv in invocations)


def sessions_with_skill(tables, skills):
    """The sessions that ran one of `skills` (invoked by the model, the user or Claude Code), and the baselines they
    are compared with (a session opened with `baseline:`); `*`: every session."""
    by_session = {}
    for r in tables["skill_invocations"]:
        by_session.setdefault(r["session_id"], []).append(r)
    return {r["session_id"] for r in tables["sessions"]
            if r.get("baseline") or qualifies(by_session.get(r["session_id"], ()), skills)}


GLOBAL_TABLES = ("de_topics", "de_layers", "warehouse_load")


def only_sessions(tables, ids, skills="*"):
    """The same tables holding only the sessions in `ids`; the taxonomy stays, and warehouse_load says what it holds."""
    out = {k: (rows if k in GLOBAL_TABLES else [r for r in rows if r.get("session_id") in ids])
           for k, rows in tables.items()}
    # what it holds, not how much else the machine has: the count of all transcripts would say that
    out["warehouse_load"] = [dict(r, transcripts=len(out["sessions"]), sessions=len(out["sessions"]),
                                  skills=",".join(sorted(skills)) or "*") for r in tables.get("warehouse_load", ())]
    return out


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
    try:
        res = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", container], capture_output=True,
                             text=True, timeout=15)
    except subprocess.TimeoutExpired:  # a Docker that does not answer is not a running Postgres
        return False
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
                  clickhouse_identity=None, sessions=None, skills=DEFAULT_SKILLS, rescope=False, write_files=True,
                  capture=True):
    """Analyze once; write the files; load Postgres (do_load: every session) and/or sync ClickHouse
    (clickhouse_target, as clickhouse_identity: see clickhouse.sync — sessions that ran one of `skills` are shared,
    and only that source's rows change). `sessions` (main transcript paths: the SessionEnd hook) limits what is read
    for ClickHouse to those; without, every transcript in scope is read. Those sessions' skill runs get a snapshot of
    what they built in Metabase (capture; once per run, when the instance answers). A target that fails does not stop
    the other: its error is in `errors`."""
    out = Path(out_dir) if out_dir else default_root() / "_warehouse" / datetime.now().strftime("%Y-%m-%d_%H%M")
    if start:
        log("Starting Postgres (docker compose up -d --wait)…")
        up()
    ch = None
    if clickhouse_target is not None:
        from . import clickhouse as ch
    # From reading the transcripts to the last ClickHouse write: a sync that read an older transcript than the one
    # another process just shared would put the older rows back.
    with ch.machine_lock() if ch else contextlib.nullcontext():
        log("Analyzing transcripts…")
        # Postgres always gets every session; ClickHouse alone, per session, needs only those transcripts read.
        only = sessions if sessions is not None and not do_load else None
        snap = {Path(t).stem for t in sessions} if capture and sessions else None
        tables, meta = build(claude_dir, project, since, redact=redact, pricing=pricing, log=log, transcripts=only,
                             capture=snap, capture_skills=skills)
        counts = write_bundle(tables, out) if write_files else {k: len(v) for k, v in tables.items()}
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
        if ch is not None:
            full = sessions is None
            requested = None if full else {Path(t).stem for t in sessions}
            read = set(meta["read"]) if full else set(meta["read"]) & requested
            # Asked for but not read: never "no longer qualifies" — a transcript that failed to read stays queued, one
            # that does not exist is dropped; either way its shared rows are left as they are.
            failed = {Path(f["transcript"]).stem for f in meta["failed"]}
            unread = set() if full else requested - read
            qualifying = sessions_with_skill(tables, skills) & read
            scope = ",".join(sorted(skills))
            info = {"target": repr(clickhouse_target), "identity": repr(clickhouse_identity), "skills": scope,
                    "mode": "all" if full else "sessions", "read": len(read), "qualifying": len(qualifying),
                    "unread": sorted(unread & failed), "missing": sorted(unread - failed)}
            try:
                if not full and not ch.needs_sync(clickhouse_target, clickhouse_identity, read, qualifying, skills):
                    res["clickhouse"] = dict(info, written=[], removed=[], stale=[], held=None, counts={}, quiet=True)
                else:
                    log(f"Syncing ClickHouse ({clickhouse_target!r}) as {clickhouse_identity!r}: {len(read)} session(s) "
                        f"read, {len(qualifying)} ran {scope}…")
                    res["clickhouse"] = dict(info, **ch.sync(tables, clickhouse_target, clickhouse_identity, read, skills,
                                                            since=since if full else "hook", full=full,
                                                            rescope=rescope, log=log))
            except (ch.ClickHouseError, OSError) as exc:
                res["errors"]["clickhouse"] = str(exc)
    return res
