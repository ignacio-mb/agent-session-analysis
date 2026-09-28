# Metabase instances

A tiny local web app for spinning up named, throwaway Metabase instances in Docker, for testing the robot data engineer (`~/dev/mba`: `mb`, `mba`, the rde skill).

It is optional: nothing else in this repository needs it, or Docker, or Bun. What it gives is the same starting point for every test of the rde skill: a fresh Metabase on the same Postgres data, which comparing skill versions prompt by prompt assumes.

```bash
bun server.ts   # or, from the repository: make lab
```

Then open http://localhost:4000. There is nothing to install: Bun serves the page, talks to the Docker Engine API over its unix socket, and talks to Postgres with `Bun.SQL`. The first instance takes about 5 extra minutes, more on a slow download, to build the Postgres image (see [The analytics database](#the-analytics-database)).

## What an instance is

Creating an instance named `foo` gives you:

- **Metabase** at `http://foo.localhost:<port>`, in container `mbo-foo`, published on 127.0.0.1 at the first free port from 3200. Each instance has its own hostname because browsers keep cookies per host, not per port: signing in to one instance never signs you out of another. `*.localhost` resolves to your machine in browsers, curl, Node and Bun. It runs on an application database in the instance's Postgres, below, and starts with no example content (`MB_LOAD_SAMPLE_CONTENT=false`). Names are lowercase letters, digits and `-`, like a hostname.
- **Postgres** in container `mbo-foo.postgres`, on that port + 10000 (3200 → 13200), holding three databases. The page shows their URLs:
  - `sample`, the Sample Database (from `metabase/qa-databases:postgres-sample-15`, the image `rde init` uses): `postgres://metabase:metasample123@localhost:13200/sample`;
  - `analytics`, two real datasets, one per schema: `dba` (dba.stackexchange.com) and `flight_delays` (US flights of 2015): `postgres://metabase:metasample123@localhost:13200/analytics`;
  - `metabase_app`, the application database Metabase runs on: `postgres://metabase:metasample123@localhost:13200/metabase_app`.
- **Setup already done:**
  - the admin from `.env` (see [Settings](#settings)), in light mode (Metabase's default follows your OS);
  - both databases connected, as **Sample Database** and **Analytics**, each read through a read-only role (`metabase_readonly`), with a **writable connection** as the owner (`metabase`) for transforms, uploads and actions. Tables the owner creates later stay readable through the main connection, even in new schemas.
  - the application database connected too, as **Metabase Application Database**, through the read-only role only: no writable connection, since a write there can break Metabase. Its tables (`report_card`, `report_dashboard`, `collection`, `core_user`, `audit_log`, `query_execution`, …) are Metabase's own content and history, queryable like any other data. The All Users group gets the default access to it, as to any new database.
  - an admin API key.
- **Copy env**, which copies `MB_URL`/`MB_API_KEY` (for `mb`) and `MBA_URL`/`MBA_API_KEY` (for `mba`).
- **Copy mb login**, which copies a command that saves an `mb` profile named after the instance.

**Reset** wipes the Postgres, application database included, and sets everything up again on the same name and ports. **Delete** removes both containers. **Stop** and **Start** keep them; Start brings Postgres up before Metabase, which exits without its application database. A Metabase container without the `mbo.appdb` label runs on H2, inside the container, until you Reset it.

The image and environment match `rde init`: `metabase/metabase-dev:transform-tests-ee` by default, the staging token store, `MB_WAREHOUSE_ALLOWED_NETWORKS=allow-all`, and `host.docker.internal` pointing at your machine. Any image works: type one or pick a local one. Missing images are pulled.

## The analytics database

`analytics` holds two real datasets, each complete and in a schema of its own, so one connection sees both and each dataset's tables stay together. Every table and column has a description, which Metabase shows.

### `dba`: Stack Exchange

[dba.stackexchange.com](https://dba.stackexchange.com), the Database Administrators Q&A site, as of Stack Exchange's own [data dump](https://archive.org/details/stackexchange) of 2024-04-06: a tech company's real product data, complete, from the site's launch in January 2011 to March 2024, plus a few hundred posts migrated from Stack Overflow back to 2008. The content is by the site's users, licensed CC BY-SA 2.5, 3.0 or 4.0 depending on when it was written (`content_license` says which).

| Table | Rows | |
|---|---:|---|
| `votes` | 911,783 | upvotes, downvotes, accepted answers, bounties, close and delete votes; anonymous and by day |
| `post_history` | 833,657 | every revision and moderation event on a post, with the text of each version |
| `badges` | 429,421 | badges awarded to users |
| `comments` | 347,838 | comments on questions and answers |
| `post_tags` | 278,266 | which tags each question has |
| `users` | 248,141 | every account, with reputation and profile |
| `posts` | 243,410 | 103,026 questions, 138,650 answers, and tag wikis |
| `post_links` | 20,194 | links between questions, and duplicates |
| `tags` | 1,242 | |
| `post_types`, `vote_types`, `post_history_types` | 15, 15, 35 | the kinds of post, vote and history event |

Every row and value of the dump is there. [`db/stackexchange.sql`](db/stackexchange.sql) says exactly what changed on the way in: names, types, and `post_tags` and the lookups (named from Stack Exchange's [schema documentation](https://meta.stackexchange.com/q/2677)). The dump leaves out deleted posts but keeps rows that point at them, such as 111,161 votes, so those foreign keys are declared `NOT VALID`: Metabase still sees them, and joins drop those rows.

### `flight_delays`: US flights of 2015

Every domestic flight of 14 US airlines in 2015, with its delays and their causes, cancellations and diversions: the US Department of Transportation's on-time reports, as Maven Analytics' [Data Playground](https://mavenanalytics.io/data-playground) publishes them ("Airline Flight Delays"). US government data, in the public domain.

| Table | Rows | |
|---|---:|---|
| `flights` | 5,819,079 | one row per scheduled flight: times, delays, taxi and air time, distance, and why it was late or cancelled |
| `airports` | 322 | name, city, state and coordinates |
| `airlines` | 14 | |
| `cancellation_codes` | 4 | airline, weather, National Air System, security |

Every row and value of the files is there, as they are: [`db/flight_delays.sql`](db/flight_delays.sql) only lowercases the names, gives the values their types (clock times stay `hhmm` text, local to the airport) and numbers the flights in file order as `id`. Exporting the tables back to CSV gives the files byte for byte. The data has the quirks it came with: October's 486,165 flights name their airports by the DOT's five-digit numeric ids instead of IATA codes, so the airport foreign keys are declared `NOT VALID` and joins drop October; and three airports have no coordinates.

**The image.** Every instance's Postgres runs `mbo-postgres:<hash>`, built from [`db/`](db/) the first time an instance needs it: it downloads both datasets (the dump from archive.org, 319 MB, and the flights from Maven Analytics, 195 MB, each checked against its SHA-1), streams them into Postgres, and bakes the data into the image, so later instances start with it at once. The tag is a hash of `db/`: change anything there, restart the app, and the next create or Reset builds a new image. Instances keep the image they were created with: those created before the analytics database hold Stack Exchange as a database of its own, `stackexchange`, connected as **DBA Stack Exchange**, until you Reset them; those created before `db/` existed hold only the Sample Database.

**Disk.** The image is 4.2 GB, 2.6 GB of it the data. The build leaves about 5 GB of untagged cache, mostly the downloads and the loaded data, which makes rebuilds fast; `docker image prune` removes it. Each instance's Postgres copies the files of the tables it reads, up to 2.6 GB.

## Settings

Copy `.env.example` to `.env` next to `server.ts`, fill it in, and start the app from this directory (`make lab`, from the repository, does):

```
LAB_ADMIN_EMAIL=yourname@metabase.com
LAB_ADMIN_PASSWORD=metabot1
MB_PREMIUM_EMBEDDING_TOKEN=...
```

`LAB_ADMIN_EMAIL` / `LAB_ADMIN_PASSWORD` are the admin of each new instance; without them, `yourname@metabase.com` / `metabot1`. The token turns on the premium features: the writable connection, transforms, the library and remote sync. Instances created after that get both. `RDE_LICENSE_TOKEN` works too. Without a token that has the `writable-connection` feature, the databases get no writable connection: their main connection uses the owner so writes still work, and the page shows a note.

## State

Docker is the source of truth: an instance is the pair of containers labeled `mbo.name` / `mbo.db`, and the Postgres container's `mbo.databases` label lists the databases it holds. The app keeps no database of its own. The only file it writes is `state.json`, which holds each instance's API key (Metabase can't show a key again) and its setup note, by container id.

Optional environment variables: `PORT` (default 4000), `FIRST_PORT` (default 3200), and `DOCKER_HOST` (a `unix://` socket, detected automatically).
