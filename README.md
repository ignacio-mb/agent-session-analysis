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

As a Claude Code plugin, from any session:

```
/plugin marketplace add ignacio-mb/agent-session-analysis
/plugin install session-export@agent-session-analysis
```

Or from a checkout:

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
| `csv/` | `turns`, `requests`, `tool_calls`, `skills`, `files`, `subagents`, `errors`, `questions`, `skill_files`, `working_files` — one row per thing. |

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
- **What the questions are about.** Each question is also mapped to a data-engineering topic (privacy,
  ownership, time, quality, delivery, business logic, sources, modeling, platform, operations, workflow,
  requirements) and a layer of the stack (presentation, semantic, modeling, staging, source, platform, or
  cross-cutting), by rules in `semantics/questions.json` tried on the question's header, then its text, then a
  fallback from the skill's own topic. So an interview can be read as coverage: which layers it asks about,
  which it never does, and where its recommendations and options work.
- **What a run did.** Every CLI call by subcommand (`mb transform create`, also inside `ID=$(…)`), `--help`
  lookups, retries after a failure; the objects the CLI reported creating; cost, context and the final hand-back;
  and a step-by-step trace of every Claude API request and tool call.
- **What the agent made on its own.** Every file a run wrote — with Write or Edit, or through the shell (a heredoc
  into a file, a redirect, `tee`, `cp`, `curl -o`) — against the working files the skill asks for (`"files"` in
  `checks/<skill>.json`; RDE's: STATE.md, `probe.sh`, a model's `.sql` and `.desc`, the JSON bodies `mb` reads with
  `--file`, CSV extracts, all in `./.scratch`). A file the run created that the skill does not name, and that is not
  Claude Code's memory, is a **support file**: made to do what the skill did not. For each, where it is (project,
  temp directory, memory), what kind (script, data, JSON…), how often the run ran it, what read it, and for a script
  what it drives: the CLI commands in its text (`mb dashboard update`) and the HTTP API paths it calls. Programs
  handed to an interpreter without a file (`python3 - <<'PY'`) are counted too. A skill that keeps needing the
  same script is missing a step, or the CLI a command.
- **Checks.** `checks/<skill>.json` declares what the skill should do, and every run is checked against it.
  `checks/rde.json` encodes RDE's own rules: state first, `mb --version` and `mb auth list` before work,
  a playbook before building, ask before creating anything, `--json`/`--profile` on every `mb` call, bodies
  from `.scratch` files, update rather than delete and recreate, read one section of a bundled `mb` skill
  rather than all of it, working files in `./.scratch` and never a system temp directory. Check types: `first`,
  `before`, `count`, `count_before`, `never`, `every`; matchers cover commands, tools, the documents read
  (`"file": "^mb:", "how": "^full$"`) and the files a call created (`"location": "^temp$"`) — see
  `src/session_analytics/checks.py`.

The skill report has per-version strip plots (one dot per run), check pass rates by version, a run × check
matrix, an **Interview** tab (questions per version, the topic × version matrix, a question catalog with the
answers people gave, where the options fell short, an interview map per run), drill-down into any run, a
**Skill files** tab (files × versions, how each file was read, which
changes the runs saw, files never shown, paths tried that do not exist), CLI calls and grouped failures, and a
compare view. Single-session exports gain a **Skill runs** tab (with each run's files, drawn as strips of the
lines shown, and the files it wrote), an **Interview** tab and a **Trace** tab.

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
`skill_runs`, `skill_run_checks`, `skill_run_files`, `skill_run_working_files` (every file a run wrote, and
whether the skill names it), `questions`, `question_options`, `subagents`,
`files_touched`, `tool_errors`, plus `de_topics` and `de_layers` (the data-engineering taxonomy) — and views
that answer the usual questions: `v_skill_versions` (each version of a skill compared), `v_check_rates`,
`v_question_topics`, `v_question_semantics` (questions by data-engineering topic × layer, per version),
`v_interview_questions` (one row per question, ready to explore), `v_question_outcomes`, `v_typed_answers`,
`v_question_flags`, `v_skill_files`, `v_cli_signatures`, `v_tools`, `v_models`, `v_daily`. Tables and columns
carry comments, which Metabase shows as descriptions. A Metabase running in Docker reaches it at
`host.docker.internal:55432`. Each fact belongs to one session (transcripts are read own-only), and every load
drops and recreates the tables; `warehouse_load` records when, and the dashboard shows it as "Data as of".
`make warehouse` checks every load: each session is recounted straight from its raw JSONL (plain `json`, none of
the parser's code) and compared with the warehouse — API requests, tokens, tool calls, failures, questions, skill
calls — so a difference is a bug, not a rounding (`make warehouse-check` runs it alone; sessions written after the
load — the main transcript or any of its subagent and workflow transcripts — are reported apart).
`make warehouse-psql` opens a shell.

Nothing refreshes it on its own: Claude Code only appends to its transcripts, and the warehouse (and every
dashboard on it) holds what the last load read. `scripts/warehouse_hook.sh` is a SessionEnd hook that updates
whatever is set up — the local Postgres, every session, when its container is running (`--load=auto`); the shared
ClickHouse (below), only the session that ended and only if it invoked rde, when `CLICKHOUSE_URL` is set
(`--clickhouse=auto --session-queue`) — and, with neither, exits before reading a transcript.
It runs in the background (closing a session is never held up), one load at a time (a session that ends mid-load
gets one more pass after it), logs to `~/claude-session-exports/_warehouse/hook.log` and overwrites
`_warehouse/latest` instead of adding a folder per load. The plugin installs it (`hooks/hooks.json`); from a
checkout, add it to `~/.claude/settings.json` instead — not both, or every session end loads twice:

```json
{"hooks": {"SessionEnd": [{"hooks": [{"type": "command",
                                      "command": "/path/to/convo-analysis/scripts/warehouse_hook.sh"}]}]}}
```

### A shared warehouse in ClickHouse

```bash
make env                # creates ~/.config/convo-analysis/.env from .env.example: fill in CLICKHOUSE_URL
make clickhouse         # every rde session on this machine into it (its own rows only), then the check
make clickhouse-forget  # take this machine's sessions out, and keep them out (--session <id> for one)
```

(Without a checkout, through the plugin: `session_export.py warehouse --init-env`, `--clickhouse --check`,
`--clickhouse-forget`.)

**What is shared: the sessions that ran the rde skill, whole.** `CLICKHOUSE_SKILLS` (default `rde`; comma
separated; a plugin's `…:rde` counts, and `agent-skills:rde` means only that plugin's; `*` for every session). Only
a Skill call that completed counts: rejected, failed, or still waiting at the permission prompt, it did not run the
skill. Once rde ran in a session, all of that session goes: every turn and prompt preview, tool call, file path and
error, other skills' runs, subagents — before and after the rde run. Sessions that never ran rde stay on the
machine: not their rows, not their ids. Once `CLICKHOUSE_URL` is set, the SessionEnd hook shares each qualifying
session as it ends, automatically; one that never ran rde makes no request either — except the very first pass on a
machine (or after `~/.config` was wiped), which asks the cluster, with this machine's source hash only, what the
machine has shared before.

**How it stays right.** Every change goes through one sync. For the sessions it just read, it writes those that
qualify and takes out those that no longer do; it also takes out any shared session whose own rows in the warehouse
show it never ran rde (what an earlier version shared, rows a sync interrupted part-way left behind). Every other
session stays: one whose transcript Claude Code has since deleted (it prunes old ones), or that was outside
`--since`. The hook reads only the session that ended, plus any transcript that changed since its last pass and has
been quiet for five minutes (a session whose end it missed; one already synced at its current state is not read
again); `make clickhouse` reads them all. Syncs on one machine hold a lock from reading to the last write.
`--clickhouse-forget` takes sessions out and remembers them as withdrawn, so no later sync — the hook, its catch-up,
`make clickhouse` — shares them again; `--clickhouse --session <id>` shares one again. A session whose transcript is
gone can still be forgotten by its id. When `CLICKHOUSE_SKILLS` changes, the sessions the new scope no longer
covers are only reported until `--rescope` (a typo there would otherwise delete history that cannot come back).

**Nobody's sync touches anyone else's rows.** Many people sync into one database, each from their own machines.
Every row carries `source` — this machine and Claude config directory, as a hash: derived from the hardware id, so
it survives a wiped config, and never the id itself — and `person` (`CLICKHOUSE_PERSON`, else the git email). The
tables are partitioned by `source`: per table, a sync rebuilds its own partition in a staging table (its rows minus
the touched sessions', plus their new rows), checks the counts, and swaps it in with `ALTER TABLE … REPLACE
PARTITION`, atomically, so a dashboard reading meanwhile sees the old partition or the new one. The taxonomy tables
(`de_topics`, `de_layers`) are the only shared ones: the same for everyone, rewritten when a sync's version of them
differs — never by an older version than the one that wrote them. A second machine, or a second Claude config directory, is a second source; a session copied between
machines is counted once per machine that syncs it. To stop sharing altogether, empty `CLICKHOUSE_URL`.

`CLICKHOUSE_URL` is the cluster's HTTPS endpoint with a user and password and the database at the end
(`https://<user>:<password>@<host>:8443/sessions`) — or the JDBC string the ClickHouse Cloud console gives,
as it is; `CLICKHOUSE_PASSWORD` takes a password a URL would need escaped. It lives in
`~/.config/convo-analysis/.env` (owner-only), outside any checkout or plugin directory, so an update never takes
it; a checkout's `.env` from before is still read, with a note to move it. Nothing reads it from the environment
or from the directory a session ran in, so another project's `CLICKHOUSE_URL` can't redirect the load. The loader
speaks ClickHouse's HTTP interface with the standard library — no driver to install.

The same tables and rows as the Postgres load, with the views rewritten in ClickHouse SQL (`clickhouse.py`) over
every source's rows; on the same transcripts every view and every dashboard card returns the same numbers in both.
The database must already exist — nothing creates one. Everything written carries a `convo-analysis` comment; a
same-named table without it belongs to someone else and stops the load before anything is written, and nothing
else in the database is touched. A newer version's columns are added to the tables in place (never dropped), and a
table from before per-source loads is rebuilt once. `make clickhouse-dev-test` tries the whole path on a throwaway
local ClickHouse (docker compose, profile `clickhouse`; `make clickhouse-dev-down` removes it).

### A Metabase dashboard on either

`scripts/metabase_dashboard.py` builds one skill's evaluation dashboard in Metabase on the warehouse, once the
warehouse is a database in that Metabase. Every card is that skill's runs (`--skill`, default `rde`), compared
version by version and prompt by prompt: the way to judge a change to a skill is to run the same prompt on a fresh
Metabase with each version. Tabs: Skill versions, Interview, Question topics, Skill files & CLI, Working files,
Overview, Session;
filters: Person (ClickHouse: whose sessions), Skill version, Prompt, Session, Data-engineering topic and Layer.

```bash
python3 scripts/metabase_dashboard.py --test [--clickhouse] [--skill rde]   # every card's SQL against the warehouse
python3 scripts/metabase_dashboard.py --sync --profile <mb profile> --database <id> --collection <id> [--skill rde]
```

- **Skill versions**: versions compared prompt by prompt (cost, active minutes, tool calls and failures, --help
  lookups, questions, objects created, support files and programs run inline, check pass rate); median cost and
  failures by version; the steps the runs took (built a transform, wrote and ran transform tests, ran a transform,
  defined measures or segments, published to the Library, built a dashboard, wrote a document) as a share of each
  version's runs; the checks, the latest version against the one before and what fails on the latest; and every
  run's checks side by side.
- **Interview**, **Question topics**, **Skill files & CLI**: the questions the runs ask and what came back, what
  they are about (a topic × layer matrix with a Tests column for transform tests), the skill files and CLI docs
  the runs were shown, the CLI commands and the tool calls that failed.
- **Working files**: what the agent made that the skill does not name. The files the runs created and how many are
  support files, the share of runs that made any, programs run inline; support files per run by version and kind,
  and the code the runs wrote themselves per run (scripts saved to files, programs run inline); what those scripts
  drive (CLI commands and API paths, with the scripts' names); the working files the skill names and the share of
  each version's runs that wrote each; and every file the runs wrote, support files first.
- **Overview**: the runs' count, cost, active hours and failure rate, each metric per run (average, median,
  range), the cost of each run by version, cost by prompt, cost by model per version, each model's requests,
  tokens (input, output, cache read and write, thinking) and share of the cost, tools, and every run with the
  instance its prompt named; clicking a run's session opens the Session tab on it.
- **Session**: one session in depth. Pick it in the Session filter (start, the id's first 8 characters, title) or
  click it in the list of sessions with the skill's runs: its models and their usage, the cost of each turn by
  model, every turn, the skill runs, questions and subagents in it, and every tool call.

The Prompt filter is the run's `prompt_key` (the prompt's opening words, without the slash command and URLs), so
the same prompt pasted again for another instance still groups with the first. Pick one to compare versions on the
same task; the by-version charts then rank only that prompt's runs.

`--sync` creates the dashboard (`<skill> skill evaluation`, or `--name`) in the collection, or updates it in
place: everything is found by name, so ids, links and bookmarks survive, and text cards added by hand stay where
they are, with the tab's cards starting below them. The SQL dialect follows the Metabase database's engine
(Postgres or ClickHouse; `--ch-database` names the ClickHouse database, default `sessions`). New cards are created
inside the dashboard, so the collection lists only the dashboard, the model and the metrics. Every card is then run
once through Metabase and reported. The cards are native SQL on table and view names, so reloading the warehouse
keeps them working.

The Question topics tab sits on a semantic layer: a model, **Interview questions** (`v_interview_questions`:
one row per question, with its data-engineering topic and layer, the run's prompt, what came back, whether the
recommended option was offered and taken, the wait), and metrics on it — Questions, Questions asked with
AskUserQuestion, Recommended option taken, Typed-answer rate, Came back empty, Median wait for an answer — so a
question asked of the model in Metabase's query builder counts the same way the dashboard does. The tab: a topic ×
layer matrix (click a topic to filter the tab), what came back per topic, a scorecard per topic, questions per run
by layer and version, the layers never asked about, and every question with its topic and layer.

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
unless you pass `--full`. Nothing is sent anywhere unless you load a warehouse: the files stay where they are
written. A ClickHouse load (and a Metabase dashboard on it) puts prompt previews, questions and answers, command
summaries, error messages and file paths, with your name on them, wherever that cluster and that collection are
readable; `--clickhouse-forget` takes them out.

## Development

```bash
make test        # pytest on the system Python (3.9) and on the uv default
make smoke       # parse and analyze every session on this machine; reports failures
make render-check  # render the newest session's dashboard in Node and fail on any JS error
make lint
```

The code is small and flat: `parse.py` turns transcripts into records, `analyze.py` turns records into the
analytics document, and `render_*.py` and `templates/` are views of that document.
