# Developing a skill

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

To test the skill against the same fresh Metabase and data every time, see the [lab](../lab/README.md).
