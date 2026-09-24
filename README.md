# convo-analysis

Understand what happened in a Claude Code session: which skills were invoked and **how**, every tool call,
aggregated counts, tokens, cost, subagents, workflows, files, git, errors, hooks, timing and context —
exported as JSON, CSV, Markdown and an interactive HTML dashboard.

It reads the transcripts Claude Code already writes to `~/.claude/projects/`, so there is nothing to enable and
it works on past sessions too. Pure Python 3.9+ standard library: the skill runs with the system `python3`.

```
/session-export                    # in Claude Code: export this session and summarise it
/session-export latest             # or: an id, an id prefix, a transcript path
/session-export rollup --since 7d  # every session in this project over the last week
```

## Install

```bash
./install.sh
```

That symlinks `skills/session-export` into `~/.claude/skills/` (or `$CLAUDE_CONFIG_DIR/skills/`), so this
checkout stays the source of truth. Start a new Claude Code session and run `/session-export`, or just ask
"export this session" / "what skills did you use?". `./install.sh --uninstall` removes the link.

The CLI works without installing anything:

```bash
python3 skills/session-export/scripts/session_export.py export latest
```

or put `session-analytics` on your PATH with `uv tool install --editable .`.

## What you get

Each export writes a folder, by default `~/claude-session-exports/<project>/<start>_<id>/`:

| File | What it is |
|---|---|
| `report.html` | Self-contained dashboard (works offline): timeline, skills, tools, cost, turns, agents, files, shell, errors, context. Light and dark. |
| `summary.md` | The short version the skill reads back to you. |
| `report.md` | Every section as Markdown tables. |
| `session.json` | The full analytics document (schema `convo-analysis/v1`). |
| `csv/` | `turns`, `requests`, `tool_calls`, `skills`, `files`, `subagents`, `errors` — one row per thing. |

### Skills — what was invoked, and how

A skill reaches a session three ways, and the transcript records each differently:

| Mode | Meaning | How it is recognised |
|---|---|---|
| `model` | Claude decided to use it (Skill tool) | `Skill` tool call → "Launching skill" result → the SKILL.md body injected with `sourceToolUseID` |
| `user` | You typed `/skill-name args` | a `<command-name>/x</command-name>` message followed by the injected body |
| `harness` | Claude Code loaded it itself | an injected `<command-name>x</command-name>` without the slash |

For each invocation: arguments, whether it loaded, the `allowed-tools` it granted, where it came from (user,
project, plugin, bundled — from its base directory), the size of the body it put into context, and the prompt
of the turn it ran in. Per skill, Claude Code's own `attributionSkill` on every response gives the requests,
tool calls, tokens and **cost attributed to that skill**. It also lists skills that were available but unused,
built-in slash commands (`/model`, `/compact`…), and skills re-injected after compaction.

### Everything else

- **Tools** — calls per tool and category, ok / error / denied / interrupted, duration percentiles, parallel
  batches, MCP servers, the most common tool-to-tool sequences, what each tool acted on (files, programs,
  domains), and every call as a row with its input and error.
- **Tokens and cost** — per model and per scope (main thread, subagents, workflows): input, output, thinking,
  cache reads, 5-minute and 1-hour cache writes, hit ratio, cache-miss reasons, peak and per-request context.
  Cost at list prices by component, model, skill, agent and turn — and Claude Code's own `cost-state`
  figures when the transcript has them, reconciled against the estimate.
- **Turns** — trigger (prompt, slash command, background-task notification), duration, requests, tool mix,
  skills, cost, interruptions, compactions.
- **Subagents and workflows** — each agent's type, task, duration, requests, tools, errors, cost and final
  context; workflow runs with phases, agent outcomes and totals.
- **Files, shell, git, web** — files read and changed with lines added/removed; programs and subcommands run,
  failures, the slowest commands; commits, pushes and PRs; fetched domains and searches.
- **You** — prompts and their length, interruptions, rejected tool calls, queued messages, think time
  between turns, questions Claude asked you and your answers.
- **Errors, hooks, context** — tool errors by category, API retries and rate limits, hook runs, compactions,
  the token budget over time, instruction files, MCP servers, what Claude Code injected around your prompts.
- **Coverage** — every event type in the transcript, and any this version does not recognise.

`docs/metrics.md` describes every field.

### Developing a skill

For anyone building a skill (it was built around RDE), `skill` follows one skill across every session:

```bash
session-analytics skill rde --since 30d            # every rde run, grouped by the version that ran
session-analytics compare b3734789:1 d085f38b:1    # two runs side by side, with a diff of what each did
```

- **Runs, not turns.** Claude Code attributes work to a skill only until the turn ends, but the skill keeps
  steering your follow-up turns, so a run lasts until another skill takes over or the session ends. Both
  views are kept: `attributed` and the whole run.
- **Versions.** Each run is labelled with the git commit of the skill that ran, by matching the SKILL.md body
  Claude Code injected against every commit of the skill's source (found under `~/dev/*/skills/<name>`, or
  `--source`). The report shows what changed in git between consecutive versions.
- **Which files the skill fed Claude.** Every document of the skill a run touched, and every doc it sent Claude
  to in the CLI's bundled skills (`mb:dashboard/SKILL.md`, reached through `mb skills path|get`): which lines
  Claude was actually shown (measured on the command's output, so `sed -n '/## Tabs/,/^## /p'`, `grep -A12`,
  `head` and `cat` all count), the sections those lines fall under, the order, what named each file (SKILL.md,
  a playbook's link, `mb skills path <name>`) or whether it was found by listing or grepping, and whether the
  text shown is the version that ran — an installed copy that lags the repository shows up as
  `older <commit>`. Files no run reads are listed per version.
- **Did a change reach the runs?** For every file a version changed, the lines it added, and how many of that
  version's runs were shown them. A rule added to a reference no run opens, or below the part a `head -40`
  prints, cannot have changed anything.
- **The interview.** Every question a run put to you — through AskUserQuestion, in prose at the end of a
  turn, or as a printed `[CHECKPOINT]` with no question behind it — named by the skill's own topics
  (`checks/rde.json` carries RDE's: sign-off, where the work lands, freshness, the decision memo, definitions,
  publishing…). For each: the options offered, whether you took the recommended one, picked another, typed
  your own answer, had no preference, declined or never answered, how long you took, and flags against the
  skill's rules for questions (a recommendation, first; measured numbers behind a decision; plain language;
  asked once). Across runs: which topics each version asks, where the recommendation misses, what people type
  when no option fits, and what could be decided and shown instead of asked.
- **What a run did.** Every CLI call by subcommand (`mb transform create`), `--help` lookups, retries after a
  failure; the objects the CLI reported creating; cost, context and the final hand-back; and a step-by-step
  trace of every Claude API request and tool call.
- **Checks.** `checks/<skill>.json` declares what the skill should do, and every run is checked against it.
  `checks/rde.json` encodes RDE's own rules: state first, `mb --version` and `mb auth list` before work,
  a playbook before building, ask before creating anything, `--json`/`--profile` on every `mb` call, bodies
  from `.scratch` files, update rather than delete and recreate, read one section of a bundled `mb` skill
  rather than all of it. Check types: `first`, `before`, `count`, `count_before`, `never`, `every`; matchers
  cover commands, tools, and the documents read (`"file": "^mb:", "how": "^full$"`) — see
  `src/session_analytics/checks.py`.

The skill report has per-version strip plots (one dot per run), check pass rates by version, a run × check
matrix, an **Interview** tab (questions per version, the topic × version matrix, a question catalog with the
answers people gave, where the options fell short, an interview map per run), drill-down into any run, a
**Skill files** tab (files × versions, how each file was read, which
changes the runs saw, files never shown, paths tried that do not exist), CLI calls and grouped failures, and a
compare view. Single-session exports gain a **Skill runs** tab (with each run's files, drawn as strips of the
lines shown), an **Interview** tab and a **Trace** tab.

### Several sessions

```bash
session-analytics rollup --since 7d          # this project
session-analytics rollup --since 30d --all   # every project
session-analytics list                       # recent sessions, to pick one
```

The rollup adds cost and activity per day, a weekday × hour heatmap, cost by project and model, and tools and
skills across sessions. A resumed or continued session starts with a copy of the earlier conversation, so
the rollup counts every API request exactly once.

### A warehouse, for SQL and Metabase

```bash
make warehouse                                   # start Postgres (docker compose) and load every session
python3 -m session_analytics warehouse --up --load --since 90d
```

Every session goes into a local Postgres (`docker-compose.yml`, `127.0.0.1:55432`, database
`claude_sessions`, user `convo`, no password) as plain tables — `sessions`, `turns`, `api_requests`,
`tool_calls`, `cli_calls` (every program in every shell command, by signature), `skill_invocations`,
`skill_runs`, `skill_run_checks`, `skill_run_files`, `questions`, `question_options`, `subagents`,
`files_touched`, `tool_errors` — and views that answer the usual questions: `v_skill_versions` (each version
of a skill compared), `v_check_rates`, `v_question_topics`, `v_question_outcomes`, `v_typed_answers`,
`v_question_flags`, `v_skill_files`, `v_cli_signatures`, `v_tools`, `v_models`, `v_daily`. Tables and columns
carry comments, which Metabase shows as descriptions. A Metabase running in Docker reaches it at
`host.docker.internal:55432`. Each fact belongs to one session (transcripts are read own-only), and every load
drops and recreates the tables; `warehouse_load` records when, and the dashboard shows it as "Data as of".
`make warehouse` checks every load: each session is recounted straight from its raw JSONL (plain `json`, none of
the parser's code) and compared with the warehouse — API requests, tokens, tool calls, failures, questions, skill
calls — so a difference is a bug, not a rounding (`make warehouse-check` runs it alone; sessions written after the
load are reported apart). `make warehouse-psql` opens a shell.

Nothing refreshes it on its own: Claude Code only appends to its transcripts, and the warehouse (and every
dashboard on it) holds what the last `make warehouse` read.

`scripts/metabase_dashboard.py` builds a Metabase dashboard on it — Overview, Skill versions, Interview, Skill
files & CLI, with a Skill filter — once the warehouse is added to that Metabase as a database:
`python3 scripts/metabase_dashboard.py --test` checks every card's SQL against the warehouse, and
`--build --profile <mb profile> --database <id>` creates the collection, cards and dashboard. The cards are
native SQL on table and view names, so reloading the warehouse keeps them working.

## Accuracy

Checked against every transcript on the machine this was built on (180 sessions, 700 subagent files):

- Token totals equal Claude Code's own per-model counters exactly wherever the two cover the same calls, and
  the price table reproduces the `costUSD` Claude Code records to the cent. The remaining gap to its reported
  total is calls Claude Code does not write to the transcript (session titles, compaction summaries), so the
  estimate is a floor and the dashboard shows both figures.
- Lines added/removed by edit tools match Claude Code's counter exactly in 51 of 57 sessions. The others were
  resumed sessions, where Claude Code's counter restarts.
- Subagent tool-use counts match the totals the Agent tool reports.

Caveats: the transcript format is internal to Claude Code and changes between releases. The parser counts
what it does not recognise instead of failing, and `session-analytics schema` lists it. Costs are list prices
(the API-equivalent), not what a subscription bills. Tool durations include time spent waiting on a
permission prompt.

## Privacy

Exports contain your prompts, commands and file paths. Secret-looking strings (API keys, tokens, private keys,
passwords, credentials in URLs) are masked by default; `--no-redact` turns that off. Previews are truncated
unless you pass `--full`. Nothing is sent anywhere: the files stay where they are written.

## Development

```bash
make test        # pytest on the system Python (3.9) and on the uv default
make smoke       # parse and analyze every session on this machine; reports failures
make render-check  # render the newest session's dashboard in Node and fail on any JS error
make lint
```

The code is small and flat: `parse.py` turns transcripts into records, `analyze.py` turns records into the
analytics document, and `render_*.py` and `templates/` are views of that document.
