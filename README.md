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

### Several sessions

```bash
session-analytics rollup --since 7d          # this project
session-analytics rollup --since 30d --all   # every project
session-analytics list                       # recent sessions, to pick one
```

The rollup adds cost and activity per day, a weekday × hour heatmap, cost by project and model, and tools and
skills across sessions. A resumed or continued session starts with a copy of the earlier conversation, so
the rollup counts every API request exactly once.

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
