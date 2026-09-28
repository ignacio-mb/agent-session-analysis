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

## Already installed? Update to 0.8.0

1. **Update the skill**, then quit and reopen Claude Code (skills and hooks load when a session starts):

   ```bash
   claude plugin marketplace update agent-session-analysis && claude plugin update session-export@agent-session-analysis
   git checkout main && git pull            # instead, if you installed from a checkout
   ```

   `claude plugin list` should show 0.8.0. Keep one install: the plugin, or a checkout with its hook, not both.

2. **Share your sessions again, once**, so the rows you shared before get the new columns (each session's and
   run's dataset; from 0.6.0 or older, their prompt keys and column descriptions too). In Claude Code:
   `/session-export update the shared warehouse`, or `make clickhouse` from a checkout. Without the connection
   string, `/session-export share` makes a new file to send. Sessions you took out stay out.

3. **Keep `mb` logged in to your local Metabase.** When an rde session ends, the hook also records what the run
   built in its Metabase and the source tables it used (counts only, never values). It reaches the instance through
   the `mb` CLI profile the run used, or the URL in its prompt, and only a local one (`localhost`, `*.localhost`).
   A session whose instance is already gone just has no snapshot. For a lab instance, **Copy mb login** gives the
   command.

4. **To compare rde with a direct agent**, open a fresh session without the skill and give it the rde run's prompt
   with `baseline:` in front. It is shared like an rde session and matched to that run.

5. **The lab** (if you run it): after pulling, restart it (`make lab`) and **Reset** each instance to get the
   `analytics` database (Stack Exchange and US flights of 2015). The first create or Reset builds the new Postgres
   image, which downloads about 500 MB and takes a few minutes. Instances you don't reset keep their old data.

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
make clickhouse-datasets                         # label every shared session and run with its dataset again
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
