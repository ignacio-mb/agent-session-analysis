---
name: session-export
description: Export and analyze a Claude Code session from its transcript — which skills were invoked and how (by Claude through the Skill tool, by the user as /slash commands, or injected by Claude Code), every tool call with timings and errors, aggregated counts, tokens, cost, cache use, subagents, workflows, files changed, git activity, hooks, context and compactions — as JSON, CSV, Markdown and an interactive HTML dashboard. Also rolls up many sessions. Use when the user says "export this session", "session analytics", "what skills/tools did you use", "how much did this session cost", "analyze session <id>", "what did I do in Claude Code this week", or asks for usage across sessions.
argument-hint: "[current | latest | <session-id or prefix> | <path.jsonl>] [--own-only] [--full] [--no-redact]   ·   rollup [--since 7d] [--all]   ·   list"
---

# Session export

Turns a Claude Code transcript (`~/.claude/projects/<project>/<session-id>.jsonl`, plus its subagent and
workflow transcripts) into analytics. Everything runs locally with the system `python3` (3.9+, no packages).

Every command below runs the launcher at `${CLAUDE_SKILL_DIR}/scripts/session_export.py`. If
`${CLAUDE_SKILL_DIR}` appears unsubstituted, use the "Base directory for this skill" shown above in its place.

Arguments the user gave: `$ARGUMENTS`

## 1. Pick the command from what the user asked

| The user wants | Run |
|---|---|
| this session (the default, also when no arguments were given) | `python3 "${CLAUDE_SKILL_DIR}/scripts/session_export.py" export --current-session "${CLAUDE_SESSION_ID}"` |
| another session: an id, a prefix, `latest`, or a transcript path | `python3 "${CLAUDE_SKILL_DIR}/scripts/session_export.py" export <that reference>` |
| to find a session first ("my sessions", "yesterday's session") | `python3 "${CLAUDE_SKILL_DIR}/scripts/session_export.py" list` (`--all` for every project), then export the one they mean |
| several sessions: "this week", "last 30 days", "across projects" | `python3 "${CLAUDE_SKILL_DIR}/scripts/session_export.py" rollup --since 7d` (`--all` for every project; `--since` takes 24h, 7d, 2w, or a date) |
| how one skill behaves across sessions and versions ("how is rde doing", "did my change to <skill> work", "which version of <skill> fails more", "which files of <skill> get read") | `python3 "${CLAUDE_SKILL_DIR}/scripts/session_export.py" skill <name> --since 30d` |
| everything in a database, to query with SQL or explore in Metabase ("load my sessions into Postgres", "a warehouse") | `python3 "${CLAUDE_SKILL_DIR}/scripts/session_export.py" warehouse --up --load` (needs Docker; `--since 90d` to narrow). Then give the connection: `postgresql://convo@127.0.0.1:55432/claude_sessions`, or `host.docker.internal:55432` from a Metabase in Docker |
| two runs of a skill side by side ("compare run X with run Y") | `python3 "${CLAUDE_SKILL_DIR}/scripts/session_export.py" compare <session>:<n> <session>:<n>` (run ids come from the reports) |

Pass through any flags the user asked for:

- `--own-only` — the session was resumed or continued, and they want only what happened in it. The summary
  warns when a transcript starts with history copied from an earlier session; offer this flag then.
- `--full` — include full prompts, commands and tool results rather than previews.
- `--no-redact` — only when they explicitly ask; exports mask secret-looking strings by default.
- `--out <dir>` — a specific destination. The default is `~/claude-session-exports/<project>/<start>_<id>/`,
  stable per session, so exporting again overwrites the previous snapshot.
- `--format json,md,html,csv` — a subset of outputs.

If `${CLAUDE_SESSION_ID}` was not substituted, drop `--current-session`: the launcher falls back to
`$CLAUDE_CODE_SESSION_ID`, then to the newest transcript of the current project.

## 2. Report back

The command prints a Markdown summary that ends with the paths it wrote. Answer from that summary — do not
open `report.md` or `session.json` just to repeat them. Lead with what the user asked about, then:

- **Skills**: each one invoked, and *how* — by Claude (Skill tool), by the user (/slash), or by Claude Code
  (harness) — plus the spend and tool calls Claude Code attributed to it.
- **Tools**: the counts, and anything notable: errors, denials, the slowest command.
- **Tokens and cost**: the estimate (list prices, API-equivalent), the cache hit rate, and Claude Code's own
  reported figure when the summary has one.
- The insights marked ⚠️.

Keep it short; the files hold the detail. For a live session (the current one), say it is a snapshot
that includes this export.

### Skill development

`skill <name>` finds every run of the skill (a run is the invocation plus the follow-up turns it steered) and
labels each with the git commit of the skill that ran, by matching the injected SKILL.md against the skill's
source repo (found under ~/dev and similar, or `--source <dir>`). It evaluates the checks in
`checks/<name>.json` of the convo-analysis repo (or `--checks <file>`) on every run. Report per version: the
median cost, tool calls, errors, questions and help lookups, the check pass rates, the skill files read, the
CLI commands and their failures, and the git changes since the previous version. Lead with what changed
between the latest two versions, and name the checks that moved. For "did my change work?", compare the
check pass rates and medians of the versions before and after the commit, and say how many runs each has:
one or two runs per version is anecdote, not a trend.

For "which files were read" and "did the runs even see my change": every run records each document of the
skill (and each bundled CLI doc, e.g. `mb:dashboard/SKILL.md`) it was shown — whole or in part, which
sections, in what order, and what named it — measured on the tool output, so partial reads by `sed`, `grep`
or `head` count only the lines they printed. The insights say which changed files the runs of a version did
not see, which files no run was shown, and when the text read was not the version that ran (a stale installed
copy reads as `older <commit>`). A change a run never saw cannot explain a difference in that run.

For "how is the interview going" or "are the questions good": every question a run asked is recorded with its
topic (the skill's own, from `checks/<name>.json` "interview"), the options, what came back (the recommended
option, another one, a typed answer, no preference, declined, unanswered), the wait, and flags against the
skill's rules for questions. Report the recommended-taken rate by topic (always taken: could be decided and
shown instead, unless the skill requires asking; rarely taken: the default is wrong), the typed answers (what
the options missed), questions asked again or in prose, and how the numbers moved between versions.

## 3. Show the dashboard

`report.html` (or `rollup.html`, `skill.html`) is a self-contained, offline page: timeline, step-by-step trace
of every Claude API request and tool call, skill runs with their checks, tool and skill breakdowns, cost and
context charts, sortable tables, light and dark themes. `skill.html` adds per-version strip plots, the checks
matrix, an Interview tab (questions by topic and version, the answers people gave, where the options fell
short), a Skill files tab (files × versions, how each was read, which changes the runs saw) and a compare
view.

- If a tool that sends a file to the user is available (`SendUserFile`), send the HTML with display
  `render`.
- Otherwise give the path, and offer to open it: re-run with `--open`, or `open <path>` on macOS.

The exports contain the user's prompts, commands and file paths. Keep them local; do not upload or publish
them anywhere unless the user asks.

## 4. Follow-up questions

Answer from `session.json` (or the CSVs in `csv/`) with a short `python3 -c` or `jq` query instead of
re-running the export. Useful keys: `totals`, `insights`, `skill_runs` (per run: version, checks,
`skill_files` with `files` / `accesses` / `inventory`, `changes_seen`, `interview`, cli, objects, errors,
final_message), `interview` (every question: topic, options, outcome, wait, flags), `trace.steps`, `skills.invocations`, `skills.per_skill`,
`tools.by_tool`, `tools.rows`, `shell.signatures`, `turns.rows`, `requests.rows`, `subagents.rows`, `cost`,
`reported`, `lineage`, `errors`, `timing`, `files.rows`, `git`. For a skill report, `skill.json` has
`versions` (with `files`, `never`, `changes.exposure` and `interview`), `files`, `interview` (`catalog`,
`questions`, `typed`), `runs`, `details.<run_id>` and `failures`; `csv/skill_files.csv` and
`csv/questions.csv` have one row per run and file, and per question. `docs/metrics.md` in the project describes every field.

After a Claude Code upgrade, `python3 "${CLAUDE_SKILL_DIR}/scripts/session_export.py" schema` lists transcript event types this version does
not recognise yet.
