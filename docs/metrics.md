# Metrics reference

Every export is one JSON document, `session.json` (schema `convo-analysis/v1`). The Markdown report, the
HTML dashboard and the CSVs are views of it. This page lists its sections and fields.

Conventions:

- Timestamps are ISO-8601 UTC strings (`start`, `ts`) or epoch milliseconds (`*_ms`). Durations are ms.
- A **request** is one API response, merged across the lines it was streamed into.
- **scope** is `main` (your conversation), `subagent` (an Agent/Task subagent), `workflow` (an agent inside a
  Workflow run), or `sidechain` (subagent lines kept in the main file by older Claude Code versions).
- **inherited** marks records copied into this transcript from an earlier session when it was resumed or
  continued (see [`lineage`](#lineage)).
- **Stats objects** (`duration_ms`, `think_time_ms`, …) have `count, sum, min, p50, p90, p99, max, mean`.
- Free text (prompts, commands, inputs, errors, answers) is redacted and truncated unless the export used
  `--no-redact` / `--full`.

## `generator`

`name`, `version`, `generated_at`, `full_content` (was `--full` used), `redaction` (on/off),
`redacted_fields` (how many strings the redactor changed).

## `session`

| Field | Meaning |
|---|---|
| `id`, `transcript`, `project_dir` | Session id, transcript path, the encoded project directory it sits in |
| `title`, `titles` | Best title (your custom title, else Claude Code's AI title, agent name, summary, first prompt); every title variant seen |
| `cwd`, `cwds`, `project_name`, `relocated_to` | Working directory (most common), all of them, its basename, directories the session was relocated to |
| `git_branches` | Each branch seen, with when it was first seen |
| `claude_code_versions`, `entrypoints`, `user_types` | Claude Code versions, entrypoints (`cli`, `claude-desktop`, …) with line counts |
| `models` | Models used, with marketing name (from the transcript) and request count |
| `start`, `end`, `wall_ms` | First and last event of the main transcript |
| `live` | The session is still running (it is the current one, or its last turn is unfinished and recent) |
| `permission_modes`, `modes` | Changes of permission mode (`default`, `plan`, `auto`, `acceptEdits`…) over time |
| `effort_levels` | Requests per effort level (`medium`, `high`, `xhigh`, `max`) |
| `worktree` | Worktree name, branch, path and origin when the session ran in one |
| `continued_in` | Session ids this one was continued into |
| `remote_control_bridge` | The session was bridged to a remote client |
| `environment`, `has_git_status_context` | Platform, OS, shell, git repo / worktree flags, extra working directories; whether git status was injected |

## `lineage`

A resumed or continued session's transcript starts with a copy of the earlier conversation. Those lines keep
the earlier session's id, which is how they are recognised.

| Field | Meaning |
|---|---|
| `continues` | Earlier sessions whose history is in this transcript: id, lines, first/last timestamp |
| `own`, `inherited` | Requests, cost, output and total tokens, tool calls, turns (and skill invocations) of each part |
| `own_only`, `inherited_events_skipped` | Whether `--own-only` dropped the copied lines, and how many |

## `totals`

The headline numbers: `turns`, `prompts`, `api_requests`, `main_requests`, `tool_calls`, `distinct_tools`,
`tool_errors`, `tool_denials`, `skills_invoked`, `distinct_skills`, `slash_commands`, `subagents`,
`workflow_runs`, `workflow_agents`, token totals (`input_tokens`, `output_tokens`, `cache_read_tokens`,
`cache_write_tokens`, `total_tokens`), `cache_hit_ratio`, `estimated_cost_usd`, `own_cost_usd`,
`inherited_cost_usd`, `inherited_requests`, `reported_cost_usd`, `files_read`, `files_modified`,
`lines_added`, `lines_removed`, `shell_commands`, `commits`, `pull_requests`, `web_fetches`, `web_searches`,
`compactions`, `interruptions`, `api_errors`, `wall_ms`, `active_ms`, `peak_context_tokens`.

## `insights`

Short observations worth reading first: `{level: info|warn, text}`.

## `tokens`

| Field | Meaning |
|---|---|
| `overall`, `by_model`, `by_scope` | Usage totals: `requests`, `input`, `output`, `thinking`, `cache_read`, `cache_write` (+ `_5m`, `_1h`), `web_search`, `web_fetch`, `total_tokens`, `cache_hit_ratio`, `cost`, `cost_components`, `unpriced_requests` |
| `cache` | `hit_ratio` (cache reads ÷ all input-side tokens), reads, writes by TTL, uncached input, `miss_reasons` (Claude Code's diagnosis per request: `messages_changed`, `system_changed`, `model_changed`, `tools_changed`, `previous_message_not_found`…, with missed tokens), requests with and without a cache read |
| `context` | Context sent per main-thread request (input + cache read + cache write): `peak_tokens`, `peak_at`, `peak_model`, `mean_tokens`, `last_tokens` |
| `output` | Output total, thinking tokens and share, per-request stats |
| `service_tiers`, `speeds`, `inference_geos` | Requests per value |

## `cost`

List-price estimate of the API-equivalent spend.

| Field | Meaning |
|---|---|
| `estimated_usd`, `components` | Total, split into input, output, cache read, cache write (5m / 1h), web search |
| `by_model`, `by_scope`, `by_skill`, `by_agent`, `by_mcp_server` | The same total split by model, scope, attributed skill (`(no skill)` for the rest), attributed agent type, and attributed MCP server |
| `top_turns`, `top_requests` | The most expensive turns and requests |
| `cumulative` | `[timestamp_ms, running total]` per request, for the cost-over-time chart |
| `unpriced_models`, `unpriced_requests` | Models with no price in the table, excluded from the total |
| `pricing` | The rate table used (per million tokens), cache multipliers, web search price, overrides |

Rates live in `pricing.py`. Override or extend them with `--pricing prices.json` or `SESSION_ANALYTICS_PRICING`.

## `reported`

Claude Code's own accounting from the last `cost-state` event, or `null` when it has not written one (it does
so when a session idles or exits): `total_cost_usd`, `total_duration_ms`, `api_duration_ms`,
`api_duration_without_retries_ms`, `tool_duration_ms`, `lines_added`, `lines_removed`, `start`, `by_model`
(tokens and cost per model), and `reconciliation` (reported vs transcript estimate, per model). The counters
cover the process that wrote the event, so a resumed session can undercount.

## `timing`

| Field | Meaning |
|---|---|
| `wall_ms`, `active_ms`, `active_share` | First to last event; the sum of turn durations (Claude Code's own `turn_duration` when recorded) |
| `model_ms` | Main-thread time waiting on the model: each request from the event before it to its last block |
| `tool_wall_ms`, `tool_sum_ms` | Main-thread tool time with overlaps merged, and summed |
| `other_active_ms` | Active time that is neither (hooks, permission prompts, overhead) |
| `user_think_ms` | Gaps between the end of a turn and your next prompt |
| `idle_gaps` | Gaps of 30 minutes or more between turns |
| `turn_duration_ms`, `request_latency_ms`, `request_duration_ms`, `tool_duration_ms` | Distributions. Latency runs to the first content block (thinking included); duration runs to the last |

## `turns`

`count`, `by_trigger`, distributions of `duration_ms`, `tool_calls_per_turn`, `requests_per_turn` and
`cost_per_turn`, the number `interrupted`, and `rows`, one per turn:

| Field | Meaning |
|---|---|
| `index`, `start`, `end`, `duration_ms`, `duration_source` | Position and timing (`reported` by Claude Code or `computed`) |
| `trigger` | `prompt`, `command` (a slash command), `task_notification` (a background task finished), `bash` (`!` shell mode) |
| `origin`, `prompt_source`, `permission_mode` | Who sent it (`human`, `task-notification`), how (`typed`, `queued`, `sdk`…), permission mode at the time |
| `prompt`, `prompt_chars`, `prompt_words`, `images`, `command`, `command_args` | What was asked |
| `requests`, `subagent_requests`, `tool_calls`, `subagent_tool_calls`, `tools` | Work done; `tools` is the main-thread tool mix |
| `tool_errors`, `tool_denials`, `skills_invoked`, `skills_attributed`, `agents_launched`, `models` | What happened in it |
| token fields, `cost_usd`, `max_context_tokens`, `stop_reason` | Usage |
| `interrupted`, `compacted`, `queued_prompts_absorbed`, `reported_message_count`, `in_progress`, `inherited` | Flags |

## `requests`

`count`, `synthetic_error_messages` (client-side API error placeholders), `stop_reasons`, `content_blocks`
(thinking / text / tool_use counts), `text_chars`, `thinking_chars`, `tool_uses`, `effort`, `per_turn_effort`,
`advisor_models`, `attribution` (requests per attributed skill, agent, plugin, MCP server and MCP tool),
`multi_iteration`, `context_edits`, `refusals`, and `rows`:

`i, ts, start_ms, end_ms, turn, scope, agent_id, model, stop_reason, input, output, cache_read,
cache_write_5m, cache_write_1h, thinking, context, cost_usd, latency_ms, duration_ms, tools, blocks, skill,
agent, plugin, mcp_server, effort, cache_miss_reason, speed, inherited`.

## `tools`

| Field | Meaning |
|---|---|
| `total_calls`, `distinct_tools`, `by_status` | Status is `ok`, `error`, `denied` (a permission rule, auto mode, or you rejected it), `interrupted`, `pending` (no result yet) |
| `by_tool` | Per tool: category, MCP server, calls by status and scope, `parallel`, result and input characters, first/last use, `error_rate`, `duration_ms` |
| `by_category` | `files`, `edits`, `search`, `shell`, `web`, `agents`, `skills`, `planning`, `outputs`, `workspace`, `mcp`, `other` |
| `mcp_servers` | Calls, errors and tools per MCP server |
| `parallel_batches` | How many tool calls each model response made at once |
| `transitions` | The most common consecutive tool pairs on the main thread |
| `denials`, `deferred_tools_loaded` | Denials by kind; tools loaded through ToolSearch |
| `top_targets` | Per tool, what it acted on most: files, programs, domains, queries, patterns |
| `rows` | Every call: `id, ts, end_ms, turn, scope, agent_id, name, category, status, denial_kind, duration_ms, batch_size, input, description` (Bash's stated reason), `target, result_chars, error, result` (with `--full`), `skill, agent, inherited` |

## `skills`

| Field | Meaning |
|---|---|
| `available`, `available_count` | Skills Claude Code listed to the model, plus ones discovered in subdirectories |
| `invocations` | Every load: `name`, `canonical` (Claude Code's name, e.g. `plugin:skill`), `mode` (`model` = Claude via the Skill tool, `user` = you typed /name, `harness` = Claude Code injected it), `via`, `ts`, `turn`, `scope`, `args`, `success`, `status` (`ok`, `forked` into a subagent, or the failure), `error`, `allowed_tools` it granted, `base_dir`, `source` (`user`, `project`, `plugin`, `bundled`, `app`), `content_chars` (size of the injected body), `forked_agent_id`, `turn_prompt` |
| `per_skill` | Per skill: invocations by mode, turns, source, and the requests, tool calls (by tool), tool errors, tokens and cost Claude Code attributed to it through `attributionSkill` |
| `invoked_distinct`, `invocations_total`, `by_mode` | Counts |
| `unused_available`, `failed` | Listed but never used; invocations that failed (unknown skill…) |
| `restored_after_compaction`, `dynamically_discovered`, `plugins` | Skills re-injected after compaction; discovered while working; requests per plugin |
| `slash_commands` | Built-in commands you typed (`/model`, `/compact`…) with args and output |
| `command_permissions` | Tools pre-approved by a skill's or command's `allowed-tools` |

Attribution persists until the turn ends, so a skill invoked early in a turn is credited with the rest of it.

## `subagents`

`count`, `workflow_agents`, `launched` (Agent/Task calls), `by_type`, `agent_types_available`,
`launches_without_transcript`, and `rows`, one per agent transcript:

`agent_id, kind, workflow_run, type, name, description, phase, workflow_status, spawn_depth, worktree,
launched_by_tool_use, launched_in_turn, background, status, forked_skill, start, end, duration_ms,
reported_duration_ms, final_context_tokens, reported_final_context_tokens` (Claude Code's `totalTokens`, which
is the agent's final context size, not a sum), `reported_tool_uses, reported_tool_stats, models, requests`,
token fields, `cost_usd, tool_calls, tools, tool_errors, skills, task_prompt, task_prompt_chars, transcript,
lines`.

## `workflows`

`count`, `launched`, and `rows` per run: `run, run_id, name, status, start, duration_ms, reported_agents,
reported_total_tokens, reported_tool_calls, default_model, phases, summary, launched_in_turn,
launch_tool_use, agent_transcripts, journal` (agent outcomes), plus requests, tokens, cost, tool calls and
errors of its agents.

## `files`

`unique_read`, `unique_modified`, `unique_created`, `lines_added` / `lines_removed` (from Edit, Write and
MultiEdit patches — what Claude Code's own counter tracks), `bash_lines_added` / `bash_lines_removed`
(files changed by shell commands, diffed by Claude Code), `lines_read`, `by_extension_modified`,
`by_extension_read`, `by_directory_modified`, `checkpoints` (file-history snapshots and tracked files),
`changed_outside_edit_tools` (files Claude had read that changed on disk by another route: you, a
formatter, a shell command), `mentioned_by_user` (@-mentions), `opened_in_ide`, `ide_selections`,
`persisted_tool_outputs` (large outputs Claude Code saved to disk), and `rows` per file:
`path, reads, lines_read, edits, writes, creates, bash_edits, lines_added, lines_removed, bash_lines_added,
bash_lines_removed, errors, first, last, scopes`.

## `shell`

Bash commands: `commands`, `errors`, `denied`, `interrupted`, `background`, `timed_out`, `sandbox_disabled`,
`custom_timeout`, `with_description`, `stdout_chars`, `stderr_chars`, `persisted_outputs`, `exit_codes`,
`return_code_interpretations`, `programs` (every program in every pipeline segment), `primary_programs`
(the one each command was about, after `cd`/`export`), `subcommands` (`git commit`, `uv run pytest`…),
`duration_ms`, `slowest`, `failing`. Heredoc bodies and quoted strings are not mistaken for commands.

## `git`

`commits` (sha, branch, kind), `pushes`, `pull_requests` (action, number, url), `branch_operations` — all
from the `gitOperation` Claude Code records on Bash results — plus `pr_links`, `git_subcommands`,
`gh_subcommands`, and `counts`.

## `web`

`fetches`, `fetch_errors`, `domains`, `status_codes`, `bytes`, `fetch_duration_ms`, `fetch_rows`, `searches`,
`search_rows` (query, result count, seconds), and `server_side` search/fetch requests from API usage.

## `planning`

`tasks_created`, `task_updates`, `task_transitions`, `tasks_completed`, `task_subjects`, `todo_writes`,
`last_todo_status`, `plan_mode` (entered, presented, approved, rejected, plan files), `questions_asked`, and
`questions` (every AskUserQuestion with its options and your answers).

## `user`

`prompts`, `prompt_sources`, `origins`, `prompt_chars`, `prompt_words`, `images_pasted`,
`slash_commands_typed`, `slash_commands`, `shell_mode_inputs`, `interruptions`,
`interruptions_during_tool_use`, `tool_denials`, `rejected_by_user`, `queue` (messages you queued while
Claude worked, and whether they were absorbed mid-turn), `task_notification_turns`, `away_summaries`,
`think_time_ms`, `permission_modes_on_prompts`.

## `errors`

`tool_errors`, `tool_error_rate`, `by_category`, `by_tool`, `rows` (tool, category, input, message),
`denied`, `interrupted`, `pending`, `api_errors`, `api_error_statuses`, `api_error_rows` (status or connection
code, retry attempt, source), `api_error_messages`, `rate_limit_events`, `refusal_fallbacks`, `notices`.

Error categories: `exit_code`, `file_not_found`, `edit_no_match`, `file_not_read_first`,
`file_changed_since_read`, `input_validation`, `schema_mismatch`, `permission`, `timeout`, `network`, `http`,
`unknown_skill_or_tool`, `tool_precondition`, `blocked_by_harness`, `worktree_isolation`, `script_error`,
`sibling_cancelled`, `other`.

## `hooks`

`runs`, `by_event`, `by_name`, `by_kind`, `non_zero_exit`, `duration_ms`, `commands`, and `stop_hooks`
(summaries, hooks run, errors, stops prevented, durations, commands).

## `context`

| Field | Meaning |
|---|---|
| `series` | `[timestamp_ms, context_tokens, output_tokens]` per main-thread request |
| `compactions`, `compactions_by_trigger` | Each compaction: trigger (`auto`/`manual`), tokens before and after, how long it took |
| `token_budget_left` | `[timestamp_ms, tokens]`: the budget countdown Claude Code shows the model (desktop sessions) |
| `deferred_tools_announced`, `deferred_tools_removed` | Tools offered for on-demand loading |
| `mcp_servers` | Servers with instructions, and pending / failed / needs-auth ones |
| `instruction_files`, `nested_memory_files` | CLAUDE.md and memory files loaded, with type and size |
| `system_prompt` | Snapshots, size, number of tools, and tool names from the last snapshot |
| `skills_listed`, `agent_types_listed` | What Claude Code offered |
| `attachments`, `attachments_by_scope` | Everything Claude Code injected around prompts, by type |
| `auto_mode`, `ultra_effort`, `environment_changes`, `thinking_stripped`, `thinking_dropped`, `context_edits` | Mode toggles and context-management events |

## `outputs`

`artifacts` published, `files_sent_to_user`, `pull_requests`, `commits`, `plans` presented,
`findings_reports`, `frames`, `away_summaries` ("while you were away" recaps).

## `timeline`

`start_ms`, `end_ms`, `buckets` (activity per time bucket: requests, agent requests, tool calls, errors, output
tokens, cost), and `markers` (skills, compactions, API errors, commits, PRs, interruptions). The dashboard
draws spans from `turns.rows`, `requests.rows`, `tools.rows` and `subagents.rows`.

## `schema_coverage`

`sources` (every file read: scope, agent, lines, unparseable lines, bytes), totals, `event_types` per scope,
`system_subtypes`, `attachment_types`, `progress_types`, `unknown` (types this version does not recognise),
`unmatched_tool_results`, `meta_messages`, `local_command_outputs`, `task_notifications`,
`artifact_monitor_events`, `remote_bridge_events`.

## Rollup (`rollup.json`)

Schema `convo-analysis/rollup-v1`: `scope` (project, window), `totals` (requests counted once across
transcripts, only inside the window; `duplicate_requests_removed`), `sessions` (one row each, with
`window_*` figures), `by_day`, `by_project`, `by_model`, `tools`, `skills` (invocations by mode, sessions,
attributed requests, tool calls and cost), `slash_commands`, `agent_types`, `error_categories`, `programs`,
`mcp_servers`, `heatmap` (requests by weekday × hour), `entrypoints`, `claude_code_versions`,
`cost_components`, `insights`.

## `trace`

`steps`: every prompt, skill invocation, Claude API request, tool call and notable event, in time order.
Each step has `i`, `t` (epoch ms), `k` (`prompt`, `skill`, `request`, `tool`, `event`), `turn`, `scope`,
`agent`, and by kind:

- `prompt`: `trigger`, `text`
- `skill`: `name`, `mode`, `via`, `args`, `ok`, `version` (fingerprint)
- `request`: `model`, `in`, `out`, `cr` (cache read), `cw` (cache write), `ctx` (context sent), `think`
  (thinking characters), `text` (what the model said), `tools`, `usd`, `lat` (latency to first block), `dur`,
  `stop`, `skill` (attributed), `miss` (cache-miss reason)
- `tool`: `name`, `id`, `status`, `dur`, `input`, `result`, `sigs` (CLI signatures), `prog` (main program),
  `res` (skill documents touched, `"<op> <owner>:<path>"`, e.g. `read rde:references/state.md`,
  `search mb:dashboard/SKILL.md`; see `skill_runs.skill_files`), `skill`, `batch`, `denial`
- `event`: `what` (`compaction`, `api_error`, `interrupt`), `text`

## `skill_runs`

One entry per skill run (an invocation plus the follow-up turns it steered, until another skill takes over):

| Field | Meaning |
|---|---|
| `run_id`, `skill`, `canonical`, `mode`, `via`, `scope`, `agent_id`, `inherited` | Which run, and how it was invoked |
| `args`, `prompt` | What the run was asked |
| `version` | `{status: commit|working-tree|installed|unknown, commit, sha, date, subject, source, label}`; `fingerprint` is the hash of the injected SKILL.md body |
| `start_ms`, `end_ms`, `end_reason`, `duration_ms`, `active_ms` | Span, and why it ended |
| `turns`, `turn_count`, `follow_up_turns`, `nested_skills`, `failed_invocations` | Turns covered (each `attributed` or follow-up) and skills invoked inside it |
| `requests`, `attributed_requests`, token fields, `cost_usd`, `attributed_cost_usd`, `cache_hit_ratio` | Claude API usage |
| `context_start`, `context_end`, `context_peak`, `latency_p50_ms`, `models` | Context and latency |
| `tool_calls`, `main_tool_calls`, `tools`, `tool_errors`, `error_rate`, `error_categories`, `errors`, `denials`, `interrupted`, `subagents` | Tool usage and failures |
| `cli`, `cli_calls`, `help_lookups`, `retries_after_error` | CLI subcommands (`mb transform create`) with calls, errors and `--help` lookups |
| `skill_files` | Every skill document the run touched and how much of it Claude was shown (below) |
| `resources_read`, `playbooks`, `references` | The skill's own files read or searched, in order of first access |
| `docs_read`, `docs_total`, `docs_never`, `cli_docs_read`, `doc_tokens`, `doc_rereads`, `doc_listings`, `doc_unprompted`, `doc_version_mismatches` | Summary numbers from `skill_files` for tables and medians |
| `changes_seen` | What the commit that ran changed against the previous commit touching the skill, per file, and whether this run was shown those lines |
| `expected_by_playbooks`, `missing_expected`, `not_named_by_playbooks` | Files the playbooks' "Read first:" lines name, which of them were never read, and which were read without being named |
| `questions`, `questions_asked`, `question_calls` | AskUserQuestion calls, with your answers |
| `objects`, `objects_created`, `files_written` | Objects the CLI reported (`{id, name, type, verb}`), files written |
| `final_message`, `last_stop_reason` | The run's hand-back |
| `checks`, `checks_passed`, `checks_failed` | `{id, desc, status: pass|fail|n/a|error, detail}` per declared check |
| `steps` | Indices into `trace.steps` |

### `skill_runs[].skill_files`

Which documents entered the run's context, measured by what Claude was shown rather than by what a command
meant to print. Owners are the run's skill (`rde`), other installed skills, and CLIs that bundle skill docs
(`mb:dashboard/SKILL.md`, from `…/@metabase/cli/skill-data/`, found through `mb skills path|get <name>`,
variables such as `D=$(mb skills path x | jq -r …)`, or the path itself).

- **Evidence**: SKILL.md's body is injected whole by the invocation; the Read tool reports the lines it
  returned; a Bash or Grep output is matched line by line against the file's text at the version the run is
  labelled with (distinctive lines exactly, blank and repeated lines between two shown ones filled in), so
  `sed -n '/## Tabs/,/^## /p'`, `grep -n -A12`, `head -40` and `cat` are all measured the same way.
- `accesses`: one row per tool call and document, in order: `t`, `dt` (ms since the invocation), `call`,
  `owner`, `path`, `kind` (directory, or the owner for other owners' docs), `op` (`inject`, `read`, `search`,
  `list`, `resolve`, `stat`), `via` (`Read`, `cat`, `sed`, `grep`, `mb skills get`…), `detail` (the command
  without its file arguments), `status`, `lines` (`[[first, last], …]` shown), `seen`, `total`, `coverage`,
  `how` (`injected`, `full`, `partial`, `hits`, `not shown`, `no hits`, `missing`, `listed`, `resolved`,
  `searched (n with hits)` for a search over a directory), `sections` (Markdown headings the shown lines fall
  under, for partial reads), `chars`, `version`, `first`, `named_by`, `found_by`.
- `files`: one row per document: `order` (0 is SKILL.md's injection), `first_dt`, `accesses`, `reads`,
  `searches`, `via`, `lines` (union shown), `coverage`, `how`, `sections`, `rereads` (reads after the whole
  file had been shown), `chars` / `tokens` (≈ chars ÷ 4, re-reads counted again), `unique_chars`,
  `named_by` (documents shown earlier whose shown lines name this one: a path, a relative Markdown link, or
  `mb skills path <name>`), `found_by` (`named`, `listing`, `search`, `resolved path`, `unprompted`),
  `version`, `missing`.
- `version` (per access and file): `match` (the text shown is the pinned version's), `older <commit>` /
  `newer <commit>` (another commit's), `uncommitted` (the source's working tree), `installed copy` (a copy on
  disk that matches no commit), `differs` (a whole-file read printed text matching no version found),
  `changed since` / `as installed now` for other owners' docs, checked against what is installed today.
  Only whole-file reads can be checked.
- `inventory`: `files` of the skill at that version (`git ls-tree`, or the directory), `never` (shown to
  this run: none of their lines).
- `totals`: `own_shown`, `own_inventory`, `own_full`, `own_partial`, `other_files`, `other_owners`, `reads`,
  `searches`, `listings`, `resolves`, `rereads`, `missing`, `version_mismatches`, `body_chars`, `doc_chars`,
  `doc_tokens`, `unique_doc_chars`, `unprompted`.

`changes_seen.files[]`: `path`, `added`, `removed`, `ranges` (lines the change added or rewrote, in the new
version's numbering), `changed_lines`, `seen_lines`, `frontmatter_lines`, `deletions`, and `status`: `seen`,
`partly seen`, `not in the lines read`, `file not read`, `deletions only`, or `frontmatter only` (SKILL.md's
frontmatter decides when the skill triggers and is never injected).

`skill_files.csv` flattens `files` across runs.

## `interview`

Every question Claude put to you, from `questions.py`. Questions inside a skill run are named by that skill's
own topics (`checks/<skill>.json`, `"interview"`); the rest by generic ones (permission, scope, definition,
setup, data handling, preference).

- **Channels** (`kind`): `ask` (AskUserQuestion), `prose` (a sentence ending in "?" in the reply that ends a
  turn; answered by the next prompt), `checkpoint` (a printed `[CHECKPOINT]` block with no AskUserQuestion
  behind it). Prose offers ("Want me to …?") get the topic `offer`.
- `questions[]`: `qid`, `t`, `dt`, `turn`, `run_id`, `skill`, `call`, `batch_size`, `batch_index`, `header`,
  `question`, `options` (`label`, `description`, `preview`, `recommended` — "(Recommended)" in the label —
  and `chosen`), `multi`, `form` (`confirm`, `choice`, `multi`, `offer`, `prose`, `checkpoint`),
  `recommended_index`, `recommended_label`, `status`, `outcome` (`recommended`, `other option`, `picked` when
  no recommendation was offered, `typed`, `typed + picked`, `no preference`, `declined`, `unanswered`,
  `interrupted`, `error`; for prose `accepted`, `turned down`, `replied`, `unanswered`), `answer`, `typed`
  (text typed instead of an option), `notes` (your notes on an answer), `feedback` (what you said when
  declining), `reply` (the next prompt, for prose), `wait_ms`, `topic`, `topic_label`, `taxonomy`,
  `before_create` (asked before the run's first `… create`), `checkpoint_block`, `after_error`, `reask_of`,
  `words`, `has_numbers`, and `flags`: `no recommendation`, `recommendation not first`, `fewer than two
  options`, `no measured numbers` (a topic marked `evidence` with no digits in the question or its reply),
  `jargon: …`, `code in the question`, `asked again` (a topic marked `once`), `like an earlier question`,
  `after an error`, `asked in prose` (when the skill says `"prose": "avoid"`), `checkpoint with no
  AskUserQuestion`.
- Totals: `total`, `asked`, `calls` (rounds), `prose`, `checkpoints_unasked`, `answered`,
  `recommended_offered`, `recommended_picked`, `recommended_rate`, `typed`, `no_preference`, `declined`,
  `unanswered`, `prose_unanswered`, `flagged`, `reasked`, `wait_p50_ms` (per round), `wait_max_ms`,
  `wait_total_ms`, `prose_wait_p50_ms`, `outcomes`, `flags`, `topics[]` (per topic: `asked`, `prose`,
  `outcomes`, `offered`, `recommended`, `recommended_rate`, `wait_p50_ms`, `typed`, `answers`, `flags`),
  `runs[]` (runs with questions: start and first create, for the interview map).

Each skill run carries its own `interview` (the same fields, plus `taxonomy` and `first_create_dt`) and the
flat fields `questions_asked`, `question_calls`, `prose_questions`, `recommended_rate`, `typed_answers`,
`unanswered_questions`, `question_wait_p50_ms`, `questions_reasked`, `questions_flagged`,
`questions_before_create`, `first_question_dt`. Checks can match a call's questions with `topic` and `flag`.
`questions.csv` has one row per question.

## `shell.signatures`

Per CLI signature (`mb card create`, `git commit`, `gh pr view`…): `calls` (Bash calls using it), `uses`
(occurrences), `errors`, `error_rate`, `help_lookups`, `p50_ms`, `max_ms`. `shell.help_lookups` is the total.

## Skill report (`skill.json`)

Schema `convo-analysis/skill-v1`: `skill`, `scope`, `totals`, `checks` (ids and descriptions), `versions`
(per version: `label`, `commit`, `date`, `subject`, `runs`, `median` and `mean` of the run metrics, `checks`
pass/fail/n.a. and `rate`, `resources` read, `cli` per signature with `per_run`, `error_categories`,
`inventory` (the skill's files at that version), `files` (per `owner:path`: `touched`, `shown`, `full`,
`partial`, `not_shown`, median `coverage` / `order` / `first_dt` / `tokens`, `accesses`, `rereads`, `via`,
`found_by`, `named_by`, `sections`, `mismatches`), `never` (files no run of the version was shown), and
`changes` = commits and files changed since the previous version with runs, plus `exposure`: per changed
file, the lines it gained and how many runs saw all, some or none of them), `files` (one row per file across
versions, with `per_version` stats, `in_version` and `changed`), `runs` (one row per run),
`details.<run_id>` (skill_files, changes_seen, cli, questions, objects, final message, errors, checks, turns,
actions, steps), `failures` (tool errors grouped by what the message says), `insights`. `csv/skill_files.csv`
has one row per run and file. `interview`: the skill's `taxonomy`, a `summary`, the `catalog` (per topic:
`asked`, `prose`, `runs`, `offered`, `recommended`, `recommended_rate`, `typed`, `no_preference`,
`declined`, `unanswered`, `reasked`, `wait_p50_ms`, `answers`, `headers`, `examples`, `once`, `must_ask`,
and `per_version`), `questions` (every question of every run, with `version_key` and `commit`) and `typed`
(answers typed instead of picked, with the options offered); each version also has `interview` totals.
`csv/questions.csv` has one row per question.

## Warehouse (Postgres)

`session-analytics warehouse --up --load` writes and loads one table per kind of fact; `schema.sql` and
`views.sql` sit beside the CSVs it loads, and every table and column that needs it carries a `COMMENT`.
Keys: `sessions.session_id`; `turns (session_id, turn)`; `api_requests (session_id, request_no)`;
`tool_calls (session_id, tool_use_id)` with `run_id` and `program`; `cli_calls (session_id, tool_use_id, seq)`
with `signature` and `is_help`; `skill_invocations (session_id, invocation_no)`; `skill_runs.run_id` with
`version`; `skill_run_checks (run_id, check_id)`; `skill_run_files (run_id, owner, path)`; `questions.qid`
(`<session8>:<qid>`); `question_options (qid, option_no)`; `subagents (session_id, agent_id)`;
`files_touched (session_id, path)`; `tool_errors (session_id, error_no)`. Views: `v_daily`, `v_skill_versions`,
`v_check_rates`, `v_question_topics`, `v_question_outcomes`, `v_typed_answers`, `v_question_flags`,
`v_skill_files`, `v_cli_signatures`, `v_tools`, `v_models`.

