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

## 3. Show the dashboard

`report.html` (or `rollup.html`) is a self-contained, offline page: timeline, tool and skill breakdowns, cost
and context charts, sortable tables, light and dark themes.

- If a tool that sends a file to the user is available (`SendUserFile`), send the HTML with display
  `render`.
- Otherwise give the path, and offer to open it: re-run with `--open`, or `open <path>` on macOS.

The exports contain the user's prompts, commands and file paths. Keep them local; do not upload or publish
them anywhere unless the user asks.

## 4. Follow-up questions

Answer from `session.json` (or the CSVs in `csv/`) with a short `python3 -c` or `jq` query instead of
re-running the export. Useful keys: `totals`, `insights`, `skills.invocations`, `skills.per_skill`,
`tools.by_tool`, `tools.rows`, `turns.rows`, `requests.rows`, `subagents.rows`, `cost`, `reported`,
`lineage`, `errors`, `timing`, `files.rows`, `git`. `docs/metrics.md` in the project describes every field.

After a Claude Code upgrade, `python3 "${CLAUDE_SKILL_DIR}/scripts/session_export.py" schema` lists transcript event types this version does
not recognise yet.
