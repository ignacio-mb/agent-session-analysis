# agent-session-analysis

Export and analyze Claude Code sessions (skills, tools, tokens, cost, subagents, files, timing), and follow one
skill such as rde across sessions and versions.

## Install the skill

In Claude Code:

```
/plugin marketplace add ignacio-mb/agent-session-analysis
/plugin install session-export@agent-session-analysis
```

Or from a checkout:

```bash
git clone https://github.com/ignacio-mb/agent-session-analysis.git && cd agent-session-analysis
./install.sh
```

Then start a new Claude Code session and run `/session-export`.

## Or the whole stack: the skill plus throwaway Metabase instances with sample data

Needs Docker and [Bun](https://bun.sh).

```bash
git clone https://github.com/ignacio-mb/agent-session-analysis.git && cd agent-session-analysis
./install.sh                     # the skill (skip if you installed the plugin)
cp lab/.env.example lab/.env     # your email and a Metabase license token
make lab                         # then open http://localhost:4000
```

## Useful commands

In Claude Code (for people with Clickhouse Connect String access):

```
/session-export                     # this session
/session-export latest              # or an id, an id prefix, a transcript path
/session-export rollup --since 7d   # every session in this project over the last week

```

### For people running experiments locally
```
/session-export share               # your rde sessions as one file, for the team
```

From a checkout:

```bash
uv tool install --editable .                     # put session-analytics on your PATH
session-analytics export latest
session-analytics list                           # recent sessions, to pick one
session-analytics rollup --since 30d --all       # every project
session-analytics skill rde --since 30d          # every rde run, grouped by the version that ran
session-analytics compare b3734789:1 d085f38b:1  # two runs side by side
```

Warehouse and sharing:

```bash
make warehouse                                   # every session into a local Postgres (Docker), then check it
make env                                         # create ~/.config/convo-analysis/.env; fill in CLICKHOUSE_URL
make clickhouse                                  # this machine's rde sessions into the shared ClickHouse
make clickhouse-forget                           # take them out, and keep them out
session-analytics share                          # the same sessions as a file, no connection string needed
session-analytics warehouse --import <files>     # load files others shared
```

Update (then quit and reopen Claude Code):

```bash
claude plugin marketplace update agent-session-analysis && claude plugin update session-export@agent-session-analysis
git checkout main && git pull                    # a checkout
```

Development:

```bash
make test
make lint
make smoke                                       # parse every session on this machine
make render-check                                # render the newest dashboard in Node
```

## Docs

- [Install and update](docs/install.md)
- [What an export contains](docs/export.md), with accuracy and privacy
- [Developing a skill](docs/skill-development.md): runs, versions, files read, the interview, checks
- [Warehouse and sharing](docs/warehouse.md)
- [Every field](docs/metrics.md)
- [The lab](lab/README.md): the throwaway Metabase instances and their data
- [Development](docs/development.md)
