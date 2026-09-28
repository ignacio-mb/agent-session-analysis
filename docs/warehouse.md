# Warehouse and sharing

Every session as tables and views, for SQL and Metabase: a local Postgres for one machine, a shared ClickHouse for
the team, and share files for testers without its connection string. What each of
them exposes, and to whom: [export.md](export.md#privacy).

## A local Postgres

```bash
make warehouse                                   # start Postgres (docker compose) and load every session
session-analytics warehouse --up --load --since 90d
```

Every session goes into a local Postgres (`docker-compose.yml`, `127.0.0.1:55432`, database
`claude_sessions`, user `convo`, no password) as plain tables — `sessions`, `turns`, `api_requests`,
`tool_calls`, `cli_calls` (every program in every shell command, by signature), `skill_invocations`,
`skill_runs`, `skill_run_checks`, `skill_run_files`, `skill_run_working_files` (every file a run wrote, and
whether the skill names it), `questions`, `question_options`, `subagents`,
`files_touched`, `tool_errors`, `run_instances`, `run_artifacts`, `run_artifact_checks`, `run_source_tables` (what
each skill run left in its Metabase, below), plus `de_topics` and `de_layers` (the data-engineering taxonomy); each
session and skill run also carries the standard dataset it ran on (below) — and views
that answer the usual questions: `v_skill_versions` (each version of a skill compared), `v_check_rates`,
`v_question_topics`, `v_question_semantics` (questions by data-engineering topic × layer, per version),
`v_interview_questions` (one row per question, ready to explore), `v_question_outcomes`, `v_typed_answers`,
`v_question_flags`, `v_skill_files`, `v_cli_signatures`, `v_tools`, `v_models`, `v_daily`. Tables and columns
carry comments, which Metabase shows as descriptions. A Metabase running in Docker reaches it at
`host.docker.internal:55432`. Each fact belongs to one session (transcripts are read own-only), and every load
drops and recreates the tables; `warehouse_load` records when.
`make warehouse` checks every load: each session is recounted straight from its raw JSONL (plain `json`, none of
the parser's code) and compared with the warehouse — API requests, tokens, tool calls, failures, questions, skill
calls — so a difference is a bug, not a rounding (`make warehouse-check` runs it alone; sessions written after the
load — the main transcript or any of its subagent and workflow transcripts — are reported apart).
`make warehouse-psql` opens a shell.

## Keeping it fresh

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

## A shared warehouse in ClickHouse

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
error, other skills' runs, subagents — before and after the rde run — and the snapshot of what its rde runs built in
their Metabase (names, descriptions and SQL; source tables as counts, never a value). A session whose first prompt
opens with `baseline:` goes too, with its snapshot: a direct agent given, without the skill, a prompt the skill is
compared on (give a fresh session an rde run's prompt with `baseline:` in front; the marker is left out of its
`prompt_key`, so the two match). Every other session that never ran rde stays on the machine: not its rows,
not its id. Once `CLICKHOUSE_URL` is set, the SessionEnd hook shares each qualifying
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
(`de_topics`, `de_layers`) and the views are the only shared objects: the same for everyone, each rewritten when a
sync's version of it differs, never over what a newer version wrote. The comment on each records the version that
wrote it and a hash of its content; a machine on an older version leaves them as they are and says to update, and
its own rows still load. A second machine, or a second Claude config directory, is a second source; a session copied
between machines is counted once per machine that syncs it. To stop sharing altogether, empty `CLICKHOUSE_URL`.

`CLICKHOUSE_URL` is the cluster's HTTPS endpoint with a user and password and the database at the end
(`https://<user>:<password>@<host>:8443/sessions`) — or the JDBC string the ClickHouse Cloud console gives,
as it is; `CLICKHOUSE_PASSWORD` takes a password a URL would need escaped. It lives in
`~/.config/convo-analysis/.env` (owner-only), outside any checkout or plugin directory, so an update never takes
it; a checkout's `.env` from before is still read, with a note to move it. Nothing reads it from the environment
or from the directory a session ran in, so another project's `CLICKHOUSE_URL` can't redirect the load. The loader
speaks ClickHouse's HTTP interface with the standard library — no driver to install.

The same tables and rows as the Postgres load, with the views rewritten in ClickHouse SQL (`clickhouse.py`) over
every source's rows; on the same transcripts every view returns the same numbers in both.
The database must already exist — nothing creates one. Everything written carries a `convo-analysis` comment; a
same-named table without it belongs to someone else and stops the load before anything is written, and nothing
else in the database is touched. A newer version's columns are added to the tables in place (never dropped), and a
table from before per-source loads is rebuilt once. `make clickhouse-dev-test` tries the whole path on a throwaway
local ClickHouse (docker compose, profile `clickhouse`; `make clickhouse-dev-down` removes it).

## Sharing a file instead of the connection string

The connection string above can create and drop tables, so it stays with whoever runs the warehouse. Everyone else
shares a file, and that person imports it:

```bash
session-analytics share                        # this machine's sessions that ran rde, as one file
session-analytics share --exclude <id>         # leave a session out, of this file and every later one
session-analytics warehouse --import <files>   # whoever holds the connection: load them (a folder works too)
```

(Through the plugin: ask Claude Code to "share my rde sessions", or run `session_export.py share`.)

`share` writes `~/claude-session-exports/_share/sessions-<name>-<time>.json`: the rows a sync would write for the
sessions in which rde ran (`--skills`, as `CLICKHOUSE_SKILLS`), with secret-looking strings masked and the person and
source they come from. It is JSON with one row per line, so the sender can read exactly what they send; `--gzip`
makes it about a tenth of the size. A session that never ran rde leaves neither its rows nor its id, and only the
transcripts that invoke the skill are parsed. Nothing is sent anywhere: the sender passes the file on themselves,
somewhere only the importer reads it.

`warehouse --import` loads each file, oldest first, with the same sync as a direct load, under the sender's source: a
newer file from the same machine updates their sessions, an older one is skipped so nothing rolls back, and no one
else's rows change. A session the sender left out is taken out, and stays out until a later file includes it. The
taxonomy is the importer's, as in a direct load. Each run is labelled again with the git commit that ran, against the
importer's checkout of the skill (`--source <dir>` when it is not under ~/dev): a tester who installed rde without
its git history can't label their runs, but the file carries what the label is resolved from — the fingerprint of
the SKILL.md that ran, and the skill files each run read.
The dataset each session and run was on is placed again on the importer's side too, from the rows in the file
(below): a file made before 0.8.0 gets it, and every file is placed by the importer's rules.

## Which dataset a session ran on

rde is tested on a few standard datasets — Stripe, Airline Flight Delays, Toy Store (Maven Fuzzy Factory), DBA Stack
Exchange, and Contrast — and `sessions.dataset` and `skill_runs.dataset` say which, so a dashboard can show it on its
cards and filter by it; NULL when nothing settles it. `dataset_by` says what decided, so each label can be checked.
Both are placed from the warehouse rows alone (`src/session_analytics/datasets.py`, rules in
`semantics/datasets.json`), so a session gets the same dataset when it is loaded, when a share file of it is
imported, and when its rows are read back from ClickHouse. A run is placed by the first of these that names exactly
one dataset:

- `prompt`: the prompt it started from, and the arguments it was invoked with;
- `snapshot`: what it built in its Metabase (names, descriptions, SQL, target tables);
- `tool calls`: at least 10 of its tool calls name the dataset, and three times as many as name the runner-up
  (`tool calls: Stripe 12, Airline Flight Delays 2` records the counts);
- `instance`: its mb profile or instance host is named after the dataset (`toy-store2`, `dba`,
  `airline-flight-delays`), and neither its prompt nor its session's title names another: a lab instance can hold
  more than the dataset it is named after;
- `session`: else, its session's.

A session takes its runs' own datasets when they agree (`runs`); else its title (Claude Code's summary of it, or its
first prompt); else the snapshots and instances of its runs, and all the session's tool calls (in a skill run or not),
taken together; else its runs' by majority (`runs: Stripe 2, Toy Store 1`). A baseline is placed as one run,
`<session>:0`, whose prompt is the session's first (the prompt of the rde run it copies) and whose tool calls are
those no skill run made. The rules match a dataset's name and its distinctive schemas, tables, columns and transform prefixes
(`raw_stripe_dlt_spike`, `flight_delays`, `website_sessions`, `post_history`, `raw_contrast`), never a word datasets
share, never the query string of a URL in a prompt or title (a pasted dashboard link carries its filters' values, such
as `?dataset=Stripe`; its path counts: `/schema/flight_delays`), and never the source tables a run built on: those
list whole databases, one lab database holds several datasets, and the lab's older instances load their dataset into
a Postgres database named `stackexchange`, whichever it is. Metabase's Sample Database, which every instance holds,
is a dataset of its own: a run on it is placed there by what it says, whatever its instance is named.

What is already in the shared ClickHouse — rows shared before these columns, or placed by rules since changed — is
placed again by whoever holds the connection:

```bash
make clickhouse-datasets            # every source's sessions and runs, from their rows
make clickhouse-datasets DRY_RUN=1  # only print what it would set
```

It reads every source's rows back, places them by the same rules, and rewrites only `dataset` and `dataset_by` of
`sessions` and `skill_runs`, one source's partition at a time, as a sync swaps it: a staging copy of the live
partition with those two columns set anew on the rows whose label changes (`INSERT … SELECT * REPLACE`), counted,
then `ALTER TABLE … REPLACE PARTITION`, and read back. Every other column is copied as it is, and a source whose
labels are already these is not written. It adds the columns first when they are missing, and holds the machine's
sync lock. That lock covers this machine only: a source that still syncs from its own machine
(`warehouse --clickhouse`, not a share file) can land a sync while its partition is being labelled. One that lands
after the copy is caught (the counts, or the labels read back, differ) and stops it: run it again. One that lands in
the instant between the last count and the swap is undone until that machine syncs the session again (its
transcript changes, or `make clickhouse` there); run it while those machines are idle. `--dry-run` (`DRY_RUN=1`) only reads, with every query read-only
(`readonly=2`), so the server refuses a write; every other mode of `warehouse` refuses it. A machine still on an older
version shares its sessions without a dataset; run it again once they have updated.

## What a run built: a snapshot of its Metabase

The transcript holds what the agent did; the Metabase it worked on holds what it made, and that instance is often a
local Docker container gone soon after. So when the SessionEnd hook (or `warehouse --session`) syncs a session, it
first takes a snapshot of the Metabase each rde run used (`src/session_analytics/instance.py`), once per run, into
`~/claude-session-exports/_instances/<session id>/<run id>.json`; every load, Postgres or ClickHouse, reads the file,
so a reload never needs the instance again.

- **Which instance**: the mb CLI profile the run's commands name (`--profile x`, `-p x`, `MB_PROFILE=x`, `PROFILE=x`),
  else the profile whose URL the prompt names. Everything goes through `mb` with that profile, so no credential is
  read here. Only a local instance is captured (localhost, 127.0.0.1, `*.localhost`, host.docker.internal): listing a
  shared production Metabase's content from a hook would load it for everyone. A remote one is recorded as not
  captured; one that does not answer is recorded as such and tried again on the next sync.
- **What the run built** (`run_artifacts`): every transform, transform test, question, model, metric, measure,
  segment, dashboard and document created or changed between the run's start and its last event, with its definition
  (SQL, or MBQL), and whether each question runs and how many rows it returns. An object two overlapping runs saw is
  the later run's. A kind the instance lacks (transform tests without the feature) is skipped and said so; an
  instance with more than 5,000 questions is not listed whole.
- **Checks on it** (`run_artifact_checks`, `instance.CHECKS`): it runs; it returns rows (questions on a dashboard);
  it has a description; the transform's last run succeeded and it has a transform test; the question is on a
  dashboard and, when it aggregates, does it with a metric or measure by id; the dashboard's filters reach every card
  and it has a text card; its name is not a probe's or a leftover's. The checks are worked out when the snapshot is
  loaded, so changing one needs no instance.
- **The data it built on** (`run_source_tables`): every active table no transform writes in the databases the run's
  transforms and questions read, as counts: rows, columns by kind, keys and relationships, JSON and coerced columns,
  empty and mostly-empty columns. Never a value.
- **A baseline session** (its first prompt opens with `baseline:`) is captured as one run, `<session>:0`, spanning
  the whole session; a direct agent may never name an mb profile, so its instance is the one its prompt names.
- `--no-capture` loads without taking snapshots.
