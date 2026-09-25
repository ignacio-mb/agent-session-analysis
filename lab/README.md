# Metabase instances

A tiny local web app for spinning up named, throwaway Metabase instances in Docker, for testing the robot data engineer (`~/dev/mba`: `mb`, `mba`, the rde skill).

```bash
bun server.ts
```

Then open http://localhost:4000. There is nothing to install: Bun serves the page, talks to the Docker Engine API over its unix socket, and talks to Postgres with `Bun.SQL`. The first instance takes about 3 extra minutes, to build the Postgres image (see [The Stack Exchange database](#the-stack-exchange-database)).

## What an instance is

Creating an instance named `foo` gives you:

- **Metabase** at `http://foo.localhost:<port>`, in container `mbo-foo`, published on 127.0.0.1 at the first free port from 3200. Each instance has its own hostname because browsers keep cookies per host, not per port: signing in to one instance never signs you out of another. `*.localhost` resolves to your machine in browsers, curl, Node and Bun. It starts with a fresh H2 application database and no example content (`MB_LOAD_SAMPLE_CONTENT=false`). Names are lowercase letters, digits and `-`, like a hostname.
- **Postgres** in container `mbo-foo.postgres`, on that port + 10000 (3200 → 13200), holding two databases. The page shows their URLs:
  - `sample`, the Sample Database (from `metabase/qa-databases:postgres-sample-15`, the image `rde init` uses): `postgres://metabase:metasample123@localhost:13200/sample`;
  - `stackexchange`, real data from dba.stackexchange.com: `postgres://metabase:metasample123@localhost:13200/stackexchange`.
- **Setup already done:**
  - the admin from `.env` (see [Settings](#settings)), in light mode (Metabase's default follows your OS);
  - both databases connected, as **Sample Database** and **DBA Stack Exchange**, each read through a read-only role (`metabase_readonly`), with a **writable connection** as the owner (`metabase`) for transforms, uploads and actions. Tables the owner creates later stay readable through the main connection, even in new schemas.
  - an admin API key.
- **Copy env**, which copies `MB_URL`/`MB_API_KEY` (for `mb`) and `MBA_URL`/`MBA_API_KEY` (for `mba`).
- **Copy mb login**, which copies a command that saves an `mb` profile named after the instance.

**Reset** wipes the application database and the Postgres, and sets everything up again on the same name and ports. **Delete** removes both containers. **Stop** and **Start** keep them.

The image and environment match `rde init`: `metabase/metabase-dev:transform-tests-ee` by default, the staging token store, `MB_WAREHOUSE_ALLOWED_NETWORKS=allow-all`, and `host.docker.internal` pointing at your machine. Any image works: type one or pick a local one. Missing images are pulled.

## The Stack Exchange database

`stackexchange` is [dba.stackexchange.com](https://dba.stackexchange.com), the Database Administrators Q&A site, as of Stack Exchange's own [data dump](https://archive.org/details/stackexchange) of 2024-04-06: a tech company's real product data from January 2011 to March 2024, with a few older posts migrated from Stack Overflow. The content is by the site's users, licensed [CC BY-SA](https://creativecommons.org/licenses/by-sa/4.0/). Every table and column has a description, which Metabase shows.

| Table | Rows | |
|---|---:|---|
| `votes` | 800,622 | upvotes, downvotes, accepted answers, bounties, close and delete votes; anonymous and by day |
| `post_history` | 833,635 | every revision and moderation event on a post |
| `badges` | 429,421 | badges awarded to users |
| `comments` | 347,838 | comments on questions and answers |
| `post_tags` | 278,266 | which tags each question has |
| `users` | 248,141 | every account, with reputation and profile |
| `posts` | 243,410 | 103,026 questions, 138,650 answers, and tag wikis |
| `post_links` | 19,904 | links between questions, and duplicates |
| `tags` | 1,242 | |
| `post_types`, `vote_types`, `post_history_types` | 15, 15, 35 | the kinds of post, vote and history event |

All values come from the dump. [`db/stackexchange.sql`](db/stackexchange.sql) says exactly what changed on the way in: names, types, `post_tags` and the lookups (named from Stack Exchange's [schema documentation](https://meta.stackexchange.com/q/2677)), no text for body revisions in `post_history`, and no rows pointing at deleted posts, so every foreign key holds.

**The image.** Every instance's Postgres runs `mbo-postgres:<hash>`, built from [`db/`](db/) the first time an instance needs it: it downloads the dump from archive.org (319 MB, checked against its SHA-1), streams it into Postgres, and bakes the data into the image, so later instances start with it at once. The tag is a hash of `db/`: change anything there and the next create or Reset builds a new image. Instances keep the image they were created with; ones created before `db/` existed hold only the Sample Database until you Reset them.

**Disk.** The image is 1.9 GB, 0.9 GB of it the data. The build leaves 1.3 GB of untagged cache that makes rebuilds fast; `docker image prune` removes it. Each instance's Postgres copies the files of the tables it reads, up to 0.9 GB.

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
