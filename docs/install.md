# Install and update

The skill reads the transcripts Claude Code already writes to `~/.claude/projects/`, so there is nothing to enable
and it works on past sessions too. Pure Python 3.9+ standard library: the skill runs with the system `python3`.

## As a plugin

From any Claude Code session:

```
/plugin marketplace add ignacio-mb/agent-session-analysis
/plugin install session-export@agent-session-analysis
```

The plugin also installs the SessionEnd hook that keeps a warehouse up to date (`hooks/hooks.json`; see
[warehouse.md](warehouse.md#keeping-it-fresh)). Until a warehouse is set up, the hook does nothing.

## From a checkout

```bash
./install.sh
```

That symlinks `skills/session-export` into `~/.claude/skills/` (or `$CLAUDE_CONFIG_DIR/skills/`), so this
checkout stays the source of truth. Start a new Claude Code session and run `/session-export`, or just ask
"export this session" / "what skills did you use?". `./install.sh --uninstall` removes the link. A checkout does
not install the hook: [warehouse.md](warehouse.md#keeping-it-fresh) shows how to add it.

The CLI works without installing anything:

```bash
python3 skills/session-export/scripts/session_export.py export latest
```

or put `session-analytics` on your PATH with `uv tool install --editable .`.

## Updating

Run 0.4.0 or later. A copy older than 0.4.0 overwrites the team warehouse's shared views and taxonomy with outdated
ones every time one of your sessions ends.

**Installed as a plugin** (the usual way):

```bash
claude plugin marketplace update agent-session-analysis
claude plugin update session-export@agent-session-analysis
```

**From a checkout** (`./install.sh` links the skill to the checkout, so pulling is all it takes):

```bash
git checkout main && git pull
```

**Then quit and reopen Claude Code**: skills and hooks load when a session starts. Check the version with
`claude plugin list` (plugin) or `PYTHONPATH=src python3 -m session_analytics --version` (checkout).

Keep one install, not both: with the plugin and a checkout's hook, every session end loads twice and the older
copy undoes the newer one.
