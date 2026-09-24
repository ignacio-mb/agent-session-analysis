"""Load the session warehouse into ClickHouse, over its HTTP interface (no driver: urllib).

    session-analytics warehouse --clickhouse       every session on this machine (make clickhouse, with --check)
    session-analytics warehouse --clickhouse=auto --session-queue DIR      the SessionEnd hook: the ended session(s)

The same tables as the Postgres warehouse (warehouse.TABLES, same rows) and the same views, written in ClickHouse
SQL — shared: many people sync into one database, each from their own machines. Every row carries `source` (which
machine and Claude config directory it came from: a hash, see source_id) and `person` (whose sessions), and the
tables are partitioned by source. Only sessions that ran one of CLICKHOUSE_SKILLS (default: rde) are shared — a
session is shared whole, every turn of it, once the skill ran in it.

sync() is the one way rows change: for the sessions just read, it writes those that qualify and takes out those
that no longer do; it also takes out any shared session whose own warehouse rows show it does not qualify (what an
older version shared); every other session — one not read this time, because its transcript is gone or it is
outside --since — stays. Per table, this source's partition is rebuilt in a staging table (its rows minus those of
the sessions touched, plus their new rows), the counts checked, and swapped in with ALTER TABLE … REPLACE PARTITION,
atomically: a dashboard reading meanwhile sees the old partition or the new one, and no other source's partition is
touched. The taxonomy tables (de_topics, de_layers) are the same for everyone and rewritten only when this version's
differ. Syncs on one machine hold a lock from reading the transcripts to the last write. Only the ids of sessions
written or taken out are sent; a hook pass whose sessions never ran the skill, and were never shared (by the local
record in ~/.config/convo-analysis/shared), makes no request. The target database must already exist: this never
creates or drops one.

Everything this writes carries a comment starting with MARK. A table or view of the same name without it
belongs to something else, and stops the load before anything is written; objects with other names are never
touched. The connection string is a secret: nothing here prints the password, and errors name only the host.

The connection string is read from ~/.config/convo-analysis/.env (or --env-file) — outside any checkout or plugin
directory, so an update never loses it — never from the process environment or the current directory: a
CLICKHOUSE_URL exported for another project, or the .env of the repository a session ran in, must not be able to
point this load at someone else's cluster.
"""

from __future__ import annotations

import contextlib
import getpass
import gzip
import hashlib
import json
import math
import os
import re
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import __version__, util
from .warehouse import (
    BIG,
    BOOL,
    DEFAULT_SKILLS,
    INT,
    NUM,
    TABLES,
    TEXT,
    TS,
    VIEW_COMMENTS,
    _cell,
    only_sessions,
    qualifies,
)

MARK = "convo-analysis"
DEFAULT_DATABASE = "sessions"
ENV_KEYS = ("CLICKHOUSE_URL", "CLICKHOUSE_PASSWORD", "CLICKHOUSE_DATABASE", "CLICKHOUSE_PERSON", "CLICKHOUSE_SKILLS")
GLOBAL = ("de_topics", "de_layers")  # the taxonomy: the same for everyone, replaced whole
CHUNK_BYTES = 8 << 20  # uncompressed JSONEachRow per INSERT
TYPES = {TEXT: "String", INT: "Int64", BIG: "Int64", NUM: "Float64", TS: "DateTime64(3, 'UTC')", BOOL: "Bool"}
INSERT_SETTINGS = {"date_time_input_format": "best_effort", "input_format_null_as_default": "1"}
# A read that a write follows (read-modify-write of a partition): on ClickHouse Cloud each HTTP request can land on a
# different replica, and without this one could still be catching up on the previous load's parts.
CONSISTENT = {"select_sequential_consistency": "1"}


class ClickHouseError(RuntimeError):
    pass


# ---------------------------------------------------------------- the connection string
def read_env(path):
    """KEY=VALUE lines of a .env file (comments, blank lines and `export ` ignored; quotes stripped)."""
    out = {}
    p = Path(path)
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[len("export "):].strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k] = v
    return out


def config_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "convo-analysis"


def checkout_env_file():
    """Where the connection lived before ~/.config: the checkout's .env. Still read, when the user one is missing."""
    return Path(__file__).resolve().parents[2] / ".env"


def default_env_file():
    """~/.config/convo-analysis/.env: a plugin update replaces the plugin's directory, and a checkout is not where a
    secret should live. A checkout's .env is still read when that one is missing."""
    user = config_dir() / ".env"
    return user if user.is_file() or not checkout_env_file().is_file() else checkout_env_file()


def init_env():
    """Create ~/.config/convo-analysis/.env from .env.example (never over an existing one). Returns (path, created)."""
    dest = config_dir() / ".env"
    if dest.exists():
        return dest, False
    dest.parent.mkdir(parents=True, exist_ok=True)
    template = Path(__file__).resolve().parents[2] / ".env.example"
    text = template.read_text(encoding="utf-8") if template.is_file() else "CLICKHOUSE_URL=\n"
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # it will hold a password: owner-only
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return dest, True


def settings(env_file=None):
    """CLICKHOUSE_URL, CLICKHOUSE_PASSWORD and CLICKHOUSE_DATABASE from the env file (only)."""
    path = Path(env_file).expanduser() if env_file else default_env_file()
    values = read_env(path)
    return {k: values.get(k) or None for k in ENV_KEYS}, path


class Target:
    """Where to load: parsed from a connection string. Its repr never shows the password."""

    def __init__(self, host, port, tls, user, password, database):
        self.host, self.port, self.tls = host, port, tls
        self.user, self.password, self.database = user, password, database

    @classmethod
    def from_url(cls, url, password=None, database=None):
        """https://user:password@host:8443/database — also http://, clickhouse://, clickhouses://, clickhousedb://
        (TLS with an s-scheme, ?secure=true / ?ssl=true, or port 8443/443), and the JDBC string the ClickHouse Cloud
        console hands out (jdbc:clickhouse://host:8443?user=…&password=…&ssl=true). Only the HTTP interface is
        spoken, so native-protocol ports (9000, 9440) are refused with the port to use instead."""
        if not url:
            raise ClickHouseError("no CLICKHOUSE_URL: copy .env.example to .env (make env) and fill it in")
        raw = url.strip()
        if raw.lower().startswith("jdbc:"):
            raw = raw[len("jdbc:"):]
            # jdbc:clickhouse:https://… and jdbc:ch:https://… put the protocol after the driver's name
            inner = re.match(r"(?i)(?:clickhouse|ch):(https?://.*)", raw)
            raw = inner.group(1) if inner else raw
        u = urllib.parse.urlsplit(raw)
        scheme = u.scheme.lower()
        if scheme not in ("http", "https", "clickhouse", "clickhouses", "clickhousedb", "clickhouse+http",
                          "clickhouse+https", "ch"):
            raise ClickHouseError(f"CLICKHOUSE_URL: unsupported scheme {u.scheme!r}; "
                                  f"use https://user:password@host:8443/db")
        if not u.hostname:
            raise ClickHouseError("CLICKHOUSE_URL has no host")
        # not parse_qs: a '+' in a JDBC password is a plus, not a space
        query = {k.lower(): urllib.parse.unquote(v) for k, _, v in (p.partition("=") for p in u.query.split("&") if p)}
        secure = (query.get("secure") or query.get("ssl") or "").lower() in ("1", "true", "yes")
        port = u.port
        tls = scheme in ("https", "clickhouses", "clickhouse+https") or secure or port in (8443, 443)
        if port in (9000, 9440):
            raise ClickHouseError(f"CLICKHOUSE_URL: port {port} is ClickHouse's native protocol; this loader speaks "
                                  f"HTTP — use {8443 if port == 9440 else 8123}")
        port = port or (8443 if tls else 8123)
        user = urllib.parse.unquote(u.username) if u.username else query.get("user") or "default"
        pw = password if password is not None else (urllib.parse.unquote(u.password) if u.password is not None
                                                    else query.get("password", ""))
        db = urllib.parse.unquote((u.path or "").strip("/")) or database or query.get("database") or DEFAULT_DATABASE
        return cls(u.hostname, port, tls, user, pw, db)

    @property
    def base(self):
        return f"{'https' if self.tls else 'http'}://{self.host}:{self.port}/"

    def __repr__(self):
        return f"{self.user}@{self.host}:{self.port}/{self.database}"


def skills_setting(env_file=None, override=None):
    """Which sessions go to the shared warehouse: those that invoked one of these skills (CLICKHOUSE_SKILLS, comma
    separated; default rde), or every session with `*`."""
    raw = override if override is not None else settings(env_file)[0]["CLICKHOUSE_SKILLS"]
    names = {x.strip() for x in (raw or "").split(",") if x.strip()}
    return tuple(sorted(names)) or DEFAULT_SKILLS


def target_from_settings(env_file=None):
    s, path = settings(env_file)
    if not s["CLICKHOUSE_URL"]:
        raise ClickHouseError(f"CLICKHOUSE_URL is not set in {path}: copy .env.example to .env (make env) and fill in "
                              f"the connection string")
    return Target.from_url(s["CLICKHOUSE_URL"], password=s["CLICKHOUSE_PASSWORD"], database=s["CLICKHOUSE_DATABASE"])


# ---------------------------------------------------------------- whose rows
class Identity:
    """Who is loading: `source` is what a load replaces, `person` a label on every row, `machine` the host name.
    (Not `owner`: skill_run_files already has one — the skill or CLI a document belongs to.)"""

    def __init__(self, source, person, machine):
        self.source, self.person, self.machine = source, person, machine

    def columns(self, table):
        if table in GLOBAL:
            return {}
        extra = {"source": self.source, "person": self.person}
        if table == "warehouse_load":
            extra["machine"] = self.machine
        return extra

    def __repr__(self):
        return f"{self.person} (source {self.source}, {self.machine})"


def _machine_id():
    """A stable id for this machine: the hardware UUID on macOS, /etc/machine-id on Linux, else one kept in ~/.config."""
    try:
        out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True, text=True,
                             timeout=5).stdout
        m = re.search(r'"IOPlatformUUID" = "([^"]+)"', out)
        if m:
            return m.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            v = Path(path).read_text().strip()
        except OSError:
            continue
        if v:
            return v
    f = config_dir() / "machine-id"
    try:
        return f.read_text().strip()
    except OSError:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(uuid.uuid4().hex + "\n")
        return f.read_text().strip()


def source_id(claude_dir):
    """This machine's sessions from this Claude config directory: the partition a load replaces. Derived rather than
    stored, so it survives a wiped config and a renamed person; hashed, so the hardware id never leaves the machine.
    A second machine, or a second config directory, is a second source — their sessions never overlap."""
    return hashlib.sha256(f"{_machine_id()}|{Path(claude_dir).expanduser().resolve()}".encode()).hexdigest()[:16]


def _git_email():
    try:
        return subprocess.run(["git", "config", "--global", "user.email"], capture_output=True, text=True,
                              timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def identity(claude_dir, env_file=None):
    """CLICKHOUSE_PERSON from the env file, else the git email, else user@host; the source from the machine."""
    s, _ = settings(env_file)
    host = socket.gethostname()
    return Identity(source_id(claude_dir), s["CLICKHOUSE_PERSON"] or _git_email() or f"{getpass.getuser()}@{host}",
                    host)


# ---------------------------------------------------------------- HTTP
class Client:
    def __init__(self, target, timeout=300):
        self.t = target
        self.timeout = timeout
        self._ssl = ssl.create_default_context() if target.tls else None

    def run(self, sql, data=None, params=None, settings=None, compress=False):
        """Run one statement. With `data`, the SQL travels in the URL and `data` is the body (an INSERT's rows)."""
        q = {"database": self.t.database, "wait_end_of_query": "1"}
        q.update(settings or {})
        q.update({f"param_{k}": v for k, v in (params or {}).items()})
        if data is None:
            body = sql.encode("utf-8")
        else:
            q["query"] = sql
            body = data
        headers = {"X-ClickHouse-User": self.t.user, "X-ClickHouse-Key": self.t.password,
                   "Content-Type": "text/plain; charset=utf-8"}
        if compress:
            body = gzip.compress(body, compresslevel=5)
            headers["Content-Encoding"] = "gzip"
        req = urllib.request.Request(self.t.base + "?" + urllib.parse.urlencode(q), data=body, headers=headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl) as res:
                return res.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace").strip()
            raise ClickHouseError(f"ClickHouse ({self.t.host}): {text[:1500]}") from None
        except urllib.error.URLError as exc:
            raise ClickHouseError(f"cannot reach ClickHouse at {self.t.host}:{self.t.port}: {exc.reason}") from None

    def rows(self, sql, params=None, settings=None):
        """A SELECT's rows, as dicts (JSONEachRow)."""
        out = self.run(sql.rstrip().rstrip(";") + " FORMAT JSONEachRow", params=params,
                       settings={"output_format_json_quote_64bit_integers": "0", **(settings or {})})
        return [json.loads(line) for line in out.splitlines() if line.strip()]


# ---------------------------------------------------------------- DDL
def ident(name):
    return "`" + name.replace("`", "``") + "`"


def lit(s):
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


def qualified(db, name):
    return f"{ident(db)}.{ident(name)}"


def columns(name):
    """(column, ClickHouse type, comment) of a table as ClickHouse holds it: the loader's columns first, then the
    warehouse's, primary-key columns required and the rest Nullable."""
    _, cols, pk = TABLES[name]
    out = []
    if name not in GLOBAL:
        out += [("source", "LowCardinality(String)", "The machine and Claude config directory that loaded the row (a "
                                                     "hash): each load replaces its own source's rows and no one else's"),
                ("person", "LowCardinality(String)", "Whose sessions: CLICKHOUSE_PERSON, else the loading machine's git email")]
        if name == "warehouse_load":
            out.append(("machine", "LowCardinality(String)", "Host name of the loading machine"))
    return out + [(c, TYPES[t] if c in pk else f"Nullable({TYPES[t]})", d) for c, t, d in cols]


def table_ddl(db, name, as_name=None):
    """CREATE TABLE for one warehouse table, partitioned by source (the taxonomy tables are not)."""
    comment, _, pk = TABLES[name]
    body = ",\n".join(f"  {ident(c)} {t}" + (f" COMMENT {lit(d)}" if d else "") for c, t, d in columns(name))
    part = "" if name in GLOBAL else "PARTITION BY source\n"
    return (f"CREATE TABLE {qualified(db, as_name or name)} (\n{body}\n)\nENGINE = MergeTree\n{part}"
            f"ORDER BY ({', '.join(ident(c) for c in pk)})\nCOMMENT {lit(f'{MARK} {__version__} · {comment}')}")


def view_ddl(db, name):
    return (f"CREATE OR REPLACE VIEW {qualified(db, name)} AS\n{VIEWS[name].format(db=ident(db)).strip()}\n"
            f"COMMENT {lit(f'{MARK} · {VIEW_COMMENTS[name]}')}")


# ---------------------------------------------------------------- rows
def json_value(v, typ):
    """A warehouse value as ClickHouse reads it from JSONEachRow; empty text is NULL, as in the Postgres load."""
    if v is None:
        return None
    if typ == BOOL:
        return bool(v)
    if typ in (INT, BIG):
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, int):
            return v
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return int(round(f)) if math.isfinite(f) else None
    if typ == NUM:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) else None
    if typ == TS and isinstance(v, (int, float)) and not isinstance(v, bool):
        return util.iso(v)
    s = _cell(v)
    return s if s != "" else None


def json_lines(name, rows, extra=None):
    """JSONEachRow chunks of at most CHUNK_BYTES; `extra` (the loader's columns) goes on every row."""
    cols = [(c, t) for c, t, _ in TABLES[name][1]]
    buf, size = [], 0
    for r in rows:
        line = json.dumps({**(extra or {}), **{c: json_value(r.get(c), t) for c, t in cols}}, ensure_ascii=False,
                          separators=(",", ":"))
        buf.append(line)
        size += len(line) + 1
        if size >= CHUNK_BYTES:
            yield "\n".join(buf).encode("utf-8")
            buf, size = [], 0
    if buf:
        yield "\n".join(buf).encode("utf-8")


# ---------------------------------------------------------------- load
def preflight(client, db):
    """The database exists, and nothing this would replace belongs to anyone else. Returns {table: {columns}}."""
    if not client.rows("SELECT name FROM system.databases WHERE name = {db:String}", {"db": db}):
        raise ClickHouseError(f"database {db} does not exist on {client.t.host}: create it first "
                              f"(CREATE DATABASE {ident(db)}) — this loader never creates one")
    existing = {r["name"]: r for r in client.rows(
        "SELECT name, engine, comment FROM system.tables WHERE database = {db:String}", {"db": db})}
    ours = set(TABLES) | set(VIEWS) | {f"{t}__rebuild" for t in TABLES}
    foreign = sorted(n for n in ours & set(existing) if not (existing[n]["comment"] or "").startswith(MARK))
    if foreign:
        raise ClickHouseError(f"{db} already has {', '.join(foreign)}, not created by {MARK} (no '{MARK}' comment): "
                              f"refusing to replace them — load into a database of its own")
    cols = {}
    for r in client.rows("SELECT table, name FROM system.columns WHERE database = {db:String}", {"db": db}):
        cols.setdefault(r["table"], set()).add(r["name"])
    return {n: cols.get(n, set()) for n in existing if n in TABLES}


def _insert(client, db, table, name, rows, extra):
    for chunk in json_lines(name, rows, extra):
        client.run(f"INSERT INTO {qualified(db, table)} FORMAT JSONEachRow", data=chunk, settings=INSERT_SETTINGS,
                   compress=True)
    got = int(client.rows(f"SELECT count() AS n FROM {qualified(db, table)}", settings=CONSISTENT)[0]["n"])
    if got != len(rows):
        raise ClickHouseError(f"{db}.{table}: {got} rows arrived of {len(rows)} sent; the live table is untouched")
    return got


def _replace_own_rows(client, db, name, rows, ident_, existing):
    """This source's rows of one table, swapped in with REPLACE PARTITION; other sources' partitions are not touched.
    A table from before per-source loads (no `source` column: it held only its last loader's rows) is rebuilt."""
    live = name
    cols = existing.get(name)
    rebuild = cols is not None and "source" not in cols
    if cols is None:
        client.run(table_ddl(db, name).replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1))
    elif rebuild:
        live = f"{name}__rebuild"
        client.run(f"DROP TABLE IF EXISTS {qualified(db, live)} SYNC")
        client.run(table_ddl(db, name, as_name=live))
    else:
        _add_missing_columns(client, db, name, cols)
    stage = f"{name}__load_{ident_.source}"
    client.run(f"DROP TABLE IF EXISTS {qualified(db, stage)} SYNC")
    client.run(f"CREATE TABLE {qualified(db, stage)} AS {qualified(db, live)}")  # same structure: REPLACE needs it
    try:
        got = _insert(client, db, stage, name, rows, ident_.columns(name))
        if got:
            client.run(f"ALTER TABLE {qualified(db, live)} REPLACE PARTITION {lit(ident_.source)} "
                       f"FROM {qualified(db, stage)}")
        else:  # REPLACE refuses an empty source partition: nothing of ours left, so drop what there was
            client.run(f"ALTER TABLE {qualified(db, live)} DROP PARTITION {lit(ident_.source)}")
    finally:
        client.run(f"DROP TABLE IF EXISTS {qualified(db, stage)} SYNC")
    if rebuild:
        client.run(f"EXCHANGE TABLES {qualified(db, live)} AND {qualified(db, name)}")
        client.run(f"DROP TABLE {qualified(db, live)} SYNC")
    return got


def _replace_whole(client, db, name, rows, ident_, existing):
    """A taxonomy table: the same for everyone, so the whole table is swapped (EXCHANGE, or RENAME the first time)."""
    stage = f"{name}__load_{ident_.source}"
    client.run(f"DROP TABLE IF EXISTS {qualified(db, stage)} SYNC")
    client.run(table_ddl(db, name, as_name=stage))
    try:
        got = _insert(client, db, stage, name, rows, {})
        if name in existing:
            client.run(f"EXCHANGE TABLES {qualified(db, stage)} AND {qualified(db, name)}")
        else:
            client.run(f"RENAME TABLE {qualified(db, stage)} TO {qualified(db, name)}")
    finally:
        client.run(f"DROP TABLE IF EXISTS {qualified(db, stage)} SYNC")
    return got


_lock_depth = 0


@contextlib.contextmanager
def machine_lock():
    """One ClickHouse sync at a time on this machine: a sync reads this source's partition and writes it back, and
    run_warehouse holds it from reading the transcripts to the last write, so the hook and a manual run never
    interleave. Re-entrant within a process. flock where there is one (released even when the process dies), else
    an exclusive lock file that goes stale after 30 minutes."""
    global _lock_depth
    if _lock_depth:
        _lock_depth += 1
        try:
            yield
        finally:
            _lock_depth -= 1
        return
    path = config_dir() / "clickhouse.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:
        fcntl = None
    if fcntl is not None:
        fh = open(path, "a")
        fcntl.flock(fh, fcntl.LOCK_EX)
    else:
        import time as _t
        while True:
            try:
                fd = os.open(f"{path}.held", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                try:
                    if _t.time() - os.path.getmtime(f"{path}.held") > 1800:
                        os.unlink(f"{path}.held")
                        continue
                except OSError:
                    pass
                _t.sleep(1)
    _lock_depth = 1
    try:
        yield
    finally:
        _lock_depth = 0
        if fcntl is not None:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
        else:
            with contextlib.suppress(OSError):
                os.unlink(f"{path}.held")


def load(tables, target, ident_, log=print):
    """Replace this source's whole partition with `tables` — only to rebuild tables from before per-source loads
    (sync does everything else). Returns {table: rows}."""
    client = Client(target)
    db = target.database
    with machine_lock():
        existing = preflight(client, db)
        counts = {}
        for name in TABLES:
            rows = tables.get(name, ())
            swap = _replace_whole if name in GLOBAL else _replace_own_rows
            counts[name] = swap(client, db, name, rows, ident_, existing)
            if log and len(rows) >= 10000:
                log(f"  {name}: {counts[name]:,} rows")
        for name in VIEWS:
            client.run(view_ddl(db, name))
    return counts


def _array(values):
    """An Array(String) query parameter."""
    return "[" + ",".join("'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'" for v in values) + "]"


def _add_missing_columns(client, db, name, cols):
    for c, t, d in columns(name):  # a newer version's columns: added, never dropped
        if c not in cols:
            client.run(f"ALTER TABLE {qualified(db, name)} ADD COLUMN IF NOT EXISTS {ident(c)} {t}"
                       + (f" COMMENT {lit(d)}" if d else ""))


def _count(client, db, table, where="", params=None):
    return int(client.rows(f"SELECT count() AS n FROM {qualified(db, table)} {where}", params, CONSISTENT)[0]["n"])


def _merge_sessions(client, db, name, rows, ident_, existing, ids):
    """This source's partition of one table again: its rows minus those of the sessions in `ids`, plus `rows` (their
    new rows), swapped in with REPLACE PARTITION — this machine's other sessions and every other source's rows are
    untouched. The ids go in the statement's body (a take-out after an upgrade can name thousands: too long for a
    URL). Every count is read consistently, and the swap itself is counted afterwards."""
    if name in existing:
        _add_missing_columns(client, db, name, existing[name])
    else:
        client.run(table_ddl(db, name).replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1))
    stage = f"{name}__load_{ident_.source}"
    src = {"src": ident_.source}
    mine = "WHERE source = {src:String}"
    keep = mine + " AND NOT has(" + _array(sorted(ids)) + ", session_id)"
    client.run(f"DROP TABLE IF EXISTS {qualified(db, stage)} SYNC")
    client.run(f"CREATE TABLE {qualified(db, stage)} AS {qualified(db, name)}")
    try:
        client.run(f"INSERT INTO {qualified(db, stage)} SELECT * FROM {qualified(db, name)} {keep}", params=src,
                   settings=CONSISTENT)
        kept = _count(client, db, stage)
        live = _count(client, db, name, keep, src)
        if kept != live:  # the copy missed rows the live table has: never swap a short partition in
            raise ClickHouseError(f"{db}.{stage}: copied {kept} rows of {live}; the live table is untouched")
        for chunk in json_lines(name, rows, ident_.columns(name)):
            client.run(f"INSERT INTO {qualified(db, stage)} FORMAT JSONEachRow", data=chunk, settings=INSERT_SETTINGS,
                       compress=True)
        got = _count(client, db, stage)
        if got != kept + len(rows):
            raise ClickHouseError(f"{db}.{stage}: {got} rows, expected {kept} kept + {len(rows)} new; the live table "
                                  f"is untouched")
        swap = (f"ALTER TABLE {qualified(db, name)} REPLACE PARTITION {lit(ident_.source)} FROM {qualified(db, stage)}"
                if got else f"ALTER TABLE {qualified(db, name)} DROP PARTITION {lit(ident_.source)}")
        for _attempt in range(2):  # the staging table is still whole: a swap that came up short is done again
            client.run(swap)
            if _count(client, db, name, mine, src) == got:
                break
        else:
            raise ClickHouseError(f"{db}.{name}: this source's partition does not hold the {got} rows swapped in")
    finally:
        client.run(f"DROP TABLE IF EXISTS {qualified(db, stage)} SYNC")
    return len(rows)


def _per_source(existing):
    return [n for n in TABLES if n not in GLOBAL and n != "warehouse_load" and "source" in existing.get(n, ())]


def _held_ids(client, db, existing, source):
    """Every session this source has rows of, in any table: rows a write interrupted part-way left behind count too."""
    tables = _per_source(existing)
    if not tables:
        return set()
    union = " UNION ALL ".join(f"SELECT session_id FROM {qualified(db, n)} WHERE source = {{src:String}}"
                               for n in tables)
    return {r["session_id"] for r in client.rows(f"SELECT DISTINCT session_id FROM ({union})", {"src": source},
                                                 CONSISTENT) if r["session_id"]}


def _shared(client, db, existing, source):
    """What this source holds now: {session: [its skill invocations]}, and the scope its last sync recorded."""
    held = {sid: [] for sid in _held_ids(client, db, existing, source)}
    if held and "source" in existing.get("skill_invocations", ()):
        for r in client.rows(f"SELECT session_id, skill, canonical, success, status FROM "
                             f"{qualified(db, 'skill_invocations')} WHERE source = {{src:String}}", {"src": source},
                             CONSISTENT):
            if r["session_id"] in held:
                held[r["session_id"]].append(r)
    scope = None
    if "skills" in existing.get("warehouse_load", ()):
        got = client.rows(f"SELECT skills FROM {qualified(db, 'warehouse_load')} WHERE source = {{src:String}}",
                          {"src": source}, CONSISTENT)
        scope = got[0]["skills"] if got else None
    return held, scope


def _version(text):
    m = re.search(rf"{MARK} (\d+(?:\.\d+)*)", text or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def _taxonomy_is_current(client, db, name, rows):
    """Whether a taxonomy table can be left as it is: it holds these rows already, or a newer version wrote it (one
    machine on an older version must not take a newer taxonomy away from everyone)."""
    got = client.rows("SELECT comment FROM system.tables WHERE database = {db:String} AND name = {t:String}",
                      {"db": db, "t": name})
    if not got:
        return False
    if _version(got[0]["comment"]) > _version(f"{MARK} {__version__}"):
        return True
    cols = [(c, t) for c, t, _ in TABLES[name][1]]
    have = client.rows(f"SELECT {', '.join(ident(c) for c, _ in cols)} FROM {qualified(db, name)}", settings=CONSISTENT)
    want = [{c: json_value(r.get(c), t) for c, t in cols} for r in rows]
    key = lambda r: json.dumps(r, sort_keys=True, default=str)  # noqa: E731
    return sorted(map(key, have)) == sorted(map(key, want))


def _write_load_row(client, db, ident_, row, held, scope, since):
    """warehouse_load: one row per source saying what it holds; none once it holds nothing (no trace of who loaded)."""
    rows = [dict(row or {}, loaded_at=util.iso(time.time() * 1000), transcripts=len(held), sessions=len(held),
                 since=since, generator_version=__version__, skills=scope)] if held else []
    return _replace_own_rows(client, db, "warehouse_load", rows, ident_, preflight(client, db))


# ---------------------------------------------------------------- what this machine has shared, kept locally
def _cache_path(target, source):
    key = hashlib.sha256(f"{target.host}|{target.port}|{target.database}|{source}".encode()).hexdigest()[:16]
    return config_dir() / "shared" / f"{key}.json"


def read_cache(target, source):
    """This machine's record for one source on one target: `ids` shared, `withdrawn` (taken out with
    --clickhouse-forget: never shared again unless named), `skills`, `swept_ms`, `synced` ({id: transcript mtime})."""
    try:
        return json.loads(_cache_path(target, source).read_text())
    except (OSError, ValueError):
        return None


def write_cache(target, source, **fields):
    path = _cache_path(target, source)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = dict(read_cache(target, source) or {}, **fields)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
    tmp.replace(path)


def reshare(target, source, session_ids):
    """Named explicitly again (--clickhouse --session): no longer withdrawn."""
    cache = read_cache(target, source) or {}
    write_cache(target, source, withdrawn=sorted(set(cache.get("withdrawn") or ()) - set(session_ids)))


def needs_sync(target, ident_, read_ids, qualifying, skills):
    """Whether a hook pass has anything to say to ClickHouse. Not when none of the sessions it read qualifies (or those
    that do were withdrawn) and, by this machine's record, none of them was shared: a session that never ran rde then
    costs no request — its id, and the time it ended, never leave the machine. With no record yet (the first pass
    after installing, or a wiped ~/.config), one request learns what this machine has shared (only the source hash
    is sent)."""
    cache = read_cache(target, ident_.source)
    if cache is None or "ids" not in cache:
        return True
    if set(qualifying) - set(cache.get("withdrawn") or ()):
        return True
    return bool(set(read_ids) & set(cache.get("ids") or ()))


def sync(tables, target, ident_, read_ids, skills, since="all", full=False, rescope=False, log=print):
    """Bring this source's rows in line with the transcripts just read (`read_ids`: sessions, `tables`: their rows).
      - a session read that ran one of `skills` is written (added, or its rows replaced) — unless it was withdrawn;
      - a session read that did not is taken out, if it was there;
      - a shared session whose own rows in the warehouse show it does not qualify is taken out, read or not (what an
        older version shared), and so is a withdrawn one (put back by an older version's hook);
      - every other session stays: one whose transcript Claude Code has since deleted, or that was outside --since.
    When CLICKHOUSE_SKILLS changed since the last sync, nothing is taken out for the new scope's sake until
    `rescope` (a typo there would otherwise delete history for good): `scope_pending` says how many would go.
    Only the ids of sessions written or taken out are sent."""
    client = Client(target)
    db = target.database
    scope = ",".join(sorted(skills))
    read_ids = set(read_ids)
    cache = read_cache(target, ident_.source) or {}
    withdrawn = set(cache.get("withdrawn") or ())
    with machine_lock():
        existing = preflight(client, db)
        legacy = sorted(n for n, cols in existing.items() if n not in GLOBAL and "source" not in cols)
        if legacy:
            if not full:
                raise ClickHouseError(f"{', '.join(legacy)} predate per-source loads: run a full load first "
                                      f"(make clickhouse)")
            if log:
                log(f"Rebuilding {', '.join(legacy)} (from before per-source loads)…")
            keep = (_qualifying(tables, skills) & read_ids) - withdrawn
            rebuilt = only_sessions(tables, keep, skills)
            if not keep:
                rebuilt["warehouse_load"] = []  # a machine with nothing to share leaves no row saying who it is
            load(rebuilt, target, ident_, log=log)
            existing = preflight(client, db)
        held, recorded = _shared(client, db, existing, ident_.source)
        write = (_qualifying(tables, skills) & read_ids) - withdrawn
        out_of_scope = {sid for sid, invs in held.items() if sid not in read_ids and not qualifies(invs, skills)}
        no_longer = (read_ids - write) & set(held)
        scope_changed = recorded is not None and recorded != scope
        pending = None
        if scope_changed and not rescope:
            # judged by a new scope: wait for --rescope; only what the old scope also rejects (or withdrawn) goes
            old = tuple(x for x in recorded.split(",") if x)
            pending = {"from": recorded, "to": scope,
                       "would_take_out": len(out_of_scope | no_longer)}
            out_of_scope = {sid for sid in out_of_scope if not qualifies(held[sid], old)}
            no_longer = {sid for sid in no_longer if sid in withdrawn or not qualifies(
                [r for r in tables.get("skill_invocations", ()) if r.get("session_id") == sid], old)}
        remove = no_longer | out_of_scope | (withdrawn & set(held))
        touch = write | remove
        counts = {}
        if touch:
            rows = only_sessions(tables, write, skills)
            # `sessions` last: a take-out interrupted part-way still shows the session as shared, so a retry finds it
            order = [n for n in TABLES if n not in GLOBAL and n not in ("sessions", "warehouse_load")] + ["sessions"]
            for name in order:
                counts[name] = _merge_sessions(client, db, name, rows.get(name, ()), ident_, existing, touch)
            for name in GLOBAL:
                if not _taxonomy_is_current(client, db, name, tables.get(name, ())):
                    counts[name] = _replace_whole(client, db, name, tables.get(name, ()), ident_, existing)
        mark = recorded if pending else scope
        if touch or (held and recorded != mark):
            now_held = sorted(_held_ids(client, db, preflight(client, db), ident_.source))
            counts["warehouse_load"] = _write_load_row(client, db, ident_, (tables.get("warehouse_load") or [{}])[0],
                                                       now_held, mark, since)
            for name in VIEWS:
                client.run(view_ddl(db, name))
        else:
            now_held = sorted(held)  # nothing to write — and with nothing shared, nothing is written at all
        write_cache(target, ident_.source, skills=scope, ids=now_held)
    return {"written": sorted(write), "removed": sorted(remove), "stale": sorted(out_of_scope), "held": len(now_held),
            "counts": counts, "scope_pending": pending,
            "withheld": sorted((_qualifying(tables, skills) & read_ids) & withdrawn)}


def _qualifying(tables, skills):
    from .warehouse import sessions_with_skill
    return sessions_with_skill(tables, skills)


def forget(target, ident_, session_ids=None):
    """Take this source's rows out — all of them, or only `session_ids`' (a session whose transcript is gone included)
    — and remember them as withdrawn, so no later sync (the hook, its catch-up, make clickhouse) shares them again
    until one is named with --clickhouse --session. The other sources' rows stay. Returns (taken out, not held)."""
    client = Client(target)
    db = target.database
    cache = read_cache(target, ident_.source) or {}
    with machine_lock():
        existing = preflight(client, db)
        held, recorded = _shared(client, db, existing, ident_.source)
        wanted = set(held) if session_ids is None else set(session_ids)
        gone = wanted & set(held)
        if gone:
            # `sessions` last, as in sync: interrupted, the rest still shows the session as held
            for name in [n for n in _per_source(existing) if n != "sessions"] + \
                    [n for n in _per_source(existing) if n == "sessions"]:
                if session_ids is None:
                    client.run(f"ALTER TABLE {qualified(db, name)} DROP PARTITION {lit(ident_.source)}")
                else:
                    _merge_sessions(client, db, name, (), ident_, existing, gone)
            now_held = sorted(_held_ids(client, db, existing, ident_.source))
            load_row = (client.rows(f"SELECT * FROM {qualified(db, 'warehouse_load')} WHERE source = {{src:String}}",
                                    {"src": ident_.source}, CONSISTENT) or [{}])[0] \
                if "warehouse_load" in existing else {}
            load_row = {k: v for k, v in load_row.items() if k not in ("source", "person", "machine")}
            _write_load_row(client, db, ident_, load_row, now_held, recorded, "forget")
        else:
            now_held = sorted(held)
        write_cache(target, ident_.source, ids=now_held,
                    withdrawn=sorted(set(cache.get("withdrawn") or ()) | wanted))
    return sorted(gone), sorted(wanted - gone)


# ---------------------------------------------------------------- the views, in ClickHouse SQL
# Rounding goes through Decimal: Postgres rounds numerics half away from zero, ClickHouse rounds a Float64 half to
# even, and the two warehouses are meant to agree to the digit. accurateCastOrNull, not toDecimal64: a division by
# nullIf(0) is still computed under the NULL, and the infinity there would fail the cast.
# Same names, columns and meaning as warehouse.VIEWS, over every source's rows (v_interview_questions adds `person`).
# Run and question ids are `<first 8 of the session id>:<n>`, unique on one machine but not across many, so joins on
# them also match the session. Other differences that matter: percentile_cont is
# quantileExactInclusive (the same interpolation), `x::numeric` would be Decimal(10, 0) here so division stays
# Float64, and a LEFT JOIN miss leaves a key column '' rather than NULL (every non-key column is Nullable, so those
# still come back NULL).
VIEWS = {
    "v_daily": """
SELECT toDate(s.start_at) AS day, count() AS sessions, sum(s.turns) AS turns, sum(s.api_requests) AS api_requests,
       sum(s.tool_calls) AS tool_calls, sum(s.tool_errors) AS tool_errors,
       round(accurateCastOrNull(sum(s.cost_usd), 'Decimal64(9)'), 2) AS cost_usd,
       sum(s.output_tokens) AS output_tokens,
       round(accurateCastOrNull(sum(s.active_ms) / 3600000.0, 'Decimal64(9)'), 2) AS active_hours,
       sum(s.skill_runs) AS skill_runs, sum(s.questions) AS questions
FROM {db}.sessions AS s GROUP BY day""",
    "v_skill_versions": """
SELECT r.skill AS skill, r.version AS version, min(r.version_date) AS version_date,
       max(r.version_subject) AS subject, count() AS runs, uniqExact(r.session_id) AS sessions,
       min(r.start_at) AS first_run, max(r.start_at) AS last_run,
       round(accurateCastOrNull(sum(r.cost_usd), 'Decimal64(9)'), 2) AS cost_usd,
       quantileExactInclusive(0.5)(r.cost_usd) AS median_cost_usd,
       quantileExactInclusive(0.5)(r.tool_calls) AS median_tool_calls,
       quantileExactInclusive(0.5)(r.tool_errors) AS median_tool_errors,
       quantileExactInclusive(0.5)(r.cli_calls) AS median_cli_calls,
       quantileExactInclusive(0.5)(r.help_lookups) AS median_help_lookups,
       quantileExactInclusive(0.5)(r.active_ms) / 60000.0 AS median_active_minutes,
       sum(r.questions_asked) AS questions_asked, sum(r.prose_questions) AS prose_questions,
       round(accurateCastOrNull(avg(r.questions_asked), 'Decimal64(9)'), 1) AS avg_questions_asked,
       round(accurateCastOrNull(avg(r.prose_questions), 'Decimal64(9)'), 1) AS avg_prose_questions,
       round(accurateCastOrNull(avg(r.question_rounds), 'Decimal64(9)'), 1) AS avg_question_rounds,
       sum(r.recommended_taken) AS recommended_taken, sum(r.recommended_offered) AS recommended_offered,
       round(accurateCastOrNull(sum(r.recommended_taken) / nullIf(sum(r.recommended_offered), 0),
                                'Decimal64(9)'), 3) AS recommended_rate,
       sum(r.typed_answers) AS typed_answers,
       quantileExactInclusive(0.5)(r.docs_read) AS median_docs_read,
       quantileExactInclusive(0.5)(r.doc_tokens) AS median_doc_tokens,
       sum(r.checks_passed) AS checks_passed, sum(r.checks_failed) AS checks_failed
FROM {db}.skill_runs AS r GROUP BY r.skill, r.version""",
    "v_check_rates": """
SELECT c.skill AS skill, c.version AS version, c.check_id AS check_id, max(c.description) AS description,
       countIf(c.status = 'pass') AS passed, countIf(c.status = 'fail') AS failed,
       countIf(c.status NOT IN ('pass', 'fail')) AS not_applicable,
       round(accurateCastOrNull(countIf(c.status = 'pass') / nullIf(countIf(c.status IN ('pass', 'fail')), 0),
                                'Decimal64(9)'), 3) AS pass_rate
FROM {db}.skill_run_checks AS c GROUP BY c.skill, c.version, c.check_id""",
    "v_question_topics": """
SELECT q.skill AS skill, q.version AS version, q.topic AS topic, max(q.topic_label) AS topic_label,
       count() AS questions, countIf(q.channel = 'ask') AS asked, countIf(q.channel != 'ask') AS in_prose,
       uniqExact(q.run_id) AS runs,
       countIf(q.outcome = 'recommended') AS recommended_taken,
       countIf(q.channel = 'ask' AND q.recommended_label IS NOT NULL AND NOT q.multi
               AND q.outcome NOT IN ('declined', 'unanswered', 'interrupted', 'error')) AS recommended_offered,
       countIf(q.typed IS NOT NULL) AS typed,
       countIf(q.outcome IN ('no preference', 'declined', 'unanswered')) AS came_back_empty,
       countIf(q.reask_of IS NOT NULL) AS asked_again,
       quantileExactInclusiveIf(0.5)(q.wait_ms, q.channel = 'ask') / 1000.0 AS median_wait_s
FROM {db}.questions AS q GROUP BY q.skill, q.version, q.topic""",
    "v_question_semantics": """
SELECT q.skill AS skill, q.version AS version, q.de_topic AS de_topic, t.label AS de_topic_label,
       t.sort_order AS de_topic_order, q.layer AS layer, l.label AS layer_label, l.sort_order AS layer_order,
       count() AS questions, countIf(q.channel = 'ask') AS asked, countIf(q.channel != 'ask') AS in_prose,
       uniqExact(q.run_id) AS runs,
       countIf(q.outcome = 'recommended') AS recommended_taken,
       countIf(q.channel = 'ask' AND q.recommended_label IS NOT NULL AND NOT q.multi
               AND q.outcome NOT IN ('declined', 'unanswered', 'interrupted', 'error')) AS recommended_offered,
       countIf(q.typed IS NOT NULL) AS typed,
       countIf(q.outcome IN ('no preference', 'declined', 'unanswered')) AS came_back_empty,
       quantileExactInclusiveIf(0.5)(q.wait_ms, q.channel = 'ask') / 1000.0 AS median_wait_s
FROM {db}.questions AS q
LEFT JOIN {db}.de_topics AS t ON t.id = q.de_topic
LEFT JOIN {db}.de_layers AS l ON l.id = q.layer
GROUP BY q.skill, q.version, q.de_topic, t.label, t.sort_order, q.layer, l.label, l.sort_order""",
    "v_interview_questions": """
SELECT q.qid AS qid, q.asked_at AS asked_at, q.skill AS skill, q.version AS version,
       r.version_date AS version_date, r.version_subject AS version_subject, q.run_id AS run_id,
       q.session_id AS session_id, s.project AS project, q.person AS person,
       CASE q.channel WHEN 'ask' THEN 'AskUserQuestion' WHEN 'prose' THEN 'In prose' ELSE 'Checkpoint' END
           AS channel,
       q.topic_label AS interview_topic,
       coalesce(t.label, q.de_topic_label) AS de_topic, coalesce(t.sort_order, 99) AS de_topic_order,
       coalesce(l.label, q.layer_label) AS layer, coalesce(l.sort_order, 99) AS layer_order,
       q.semantics_by AS classified_by, q.header AS header, q.question AS question, q.outcome AS outcome,
       CASE WHEN q.channel != 'ask' THEN 'in prose'
            WHEN q.outcome = 'recommended' THEN 'recommended option'
            WHEN q.outcome IN ('typed', 'typed + picked') THEN 'typed an answer'
            WHEN q.outcome IN ('other option', 'picked') THEN 'another option'
            WHEN q.outcome = 'no preference' THEN 'no preference'
            ELSE 'declined or unanswered' END AS outcome_group,
       -- a recommendation counts as offered on a single-choice AskUserQuestion that came back with an answer
       CAST(coalesce(q.channel = 'ask' AND q.recommended_label IS NOT NULL AND NOT q.multi
                     AND q.outcome NOT IN ('declined', 'unanswered', 'interrupted', 'error'), 0) AS Bool)
           AS recommendation_offered,
       CAST(recommendation_offered AND coalesce(q.outcome = 'recommended', 0) AS Bool) AS took_recommendation,
       CAST(q.typed IS NOT NULL AS Bool) AS typed_answer,
       CAST(coalesce(q.outcome IN ('no preference', 'declined', 'unanswered'), 0) AS Bool) AS came_back_empty,
       coalesce(q.typed, q.answer, q.reply, q.feedback) AS answer,
       if(q.channel = 'ask' AND q.outcome NOT IN ('unanswered', 'declined'),
          round(accurateCastOrNull(q.wait_ms / 1000.0, 'Decimal64(9)'), 1), NULL) AS wait_s,
       q.flags AS flags
FROM {db}.questions AS q
LEFT JOIN {db}.de_topics AS t ON t.id = q.de_topic
LEFT JOIN {db}.de_layers AS l ON l.id = q.layer
LEFT JOIN {db}.skill_runs AS r ON r.run_id = q.run_id AND r.session_id = q.session_id
LEFT JOIN {db}.sessions AS s ON s.session_id = q.session_id AND s.source = q.source""",
    "v_question_outcomes": """
SELECT q.skill AS skill, q.version AS version, q.channel AS channel, q.outcome AS outcome, count() AS questions,
       uniqExact(q.run_id) AS runs
FROM {db}.questions AS q GROUP BY q.skill, q.version, q.channel, q.outcome""",
    "v_typed_answers": """
SELECT q.skill AS skill, q.version AS version, q.run_id AS run_id, q.asked_at AS asked_at,
       q.topic_label AS topic_label, q.header AS header, q.question AS question, q.typed AS typed,
       nullIf(arrayStringConcat(arrayMap(x -> x.2, arraySort(groupArrayIf((o.option_no, ifNull(o.label, '')),
                                                                          o.qid != ''))), ' / '), '')
           AS options_offered
FROM {db}.questions AS q LEFT JOIN {db}.question_options AS o ON o.qid = q.qid AND o.session_id = q.session_id
WHERE q.typed IS NOT NULL
GROUP BY q.skill, q.version, q.run_id, q.asked_at, q.topic_label, q.header, q.question, q.typed""",
    "v_question_flags": """
SELECT q.skill AS skill, q.version AS version, trimBoth(f) AS flag, count() AS questions,
       uniqExact(q.run_id) AS runs
FROM {db}.questions AS q ARRAY JOIN splitByChar(';', ifNull(q.flags, '')) AS f
WHERE trimBoth(f) != ''
GROUP BY q.skill, q.version, flag""",
    "v_skill_files": """
SELECT f.skill AS skill, f.version AS version, f.owner AS owner, f.path AS path, v.runs AS runs,
       uniqExactIf(f.run_id, f.lines_seen > 0) AS runs_shown,
       countIf(f.how IN ('full', 'injected', 'injected + re-read')) AS whole,
       countIf(f.how IN ('partial', 'hits')) AS in_part,
       quantileExactInclusive(0.5)(f.coverage) AS median_coverage,
       quantileExactInclusive(0.5)(f.read_order) AS median_order,
       sum(f.tokens) AS tokens
FROM {db}.skill_run_files AS f
INNER JOIN (SELECT r.skill AS skill, r.version AS version, count() AS runs FROM {db}.skill_runs AS r
            GROUP BY r.skill, r.version) AS v ON v.skill = f.skill AND v.version = f.version
GROUP BY f.skill, f.version, f.owner, f.path, v.runs""",
    "v_cli_signatures": """
SELECT c.program AS program, c.signature AS signature, count() AS uses, uniqExact(c.tool_use_id) AS bash_calls,
       uniqExact(c.session_id) AS sessions, uniqExact(c.run_id) AS runs,
       countIf(c.status = 'error') AS errors,
       round(accurateCastOrNull(countIf(c.status = 'error') / count(), 'Decimal64(9)'), 3) AS error_rate,
       countIf(c.is_help) AS help_lookups
FROM {db}.cli_calls AS c GROUP BY c.program, c.signature""",
    "v_tools": """
SELECT t.tool AS tool, t.category AS category, count() AS calls, uniqExact(t.session_id) AS sessions,
       countIf(t.status = 'error') AS errors, countIf(t.status = 'denied') AS denied,
       round(accurateCastOrNull(countIf(t.status = 'error') / count(), 'Decimal64(9)'), 3) AS error_rate,
       quantileExactInclusive(0.5)(t.duration_ms) AS p50_ms
FROM {db}.tool_calls AS t GROUP BY t.tool, t.category""",
    "v_models": """
SELECT a.model AS model, count() AS requests, uniqExact(a.session_id) AS sessions,
       sum(a.input_tokens) AS input_tokens, sum(a.output_tokens) AS output_tokens,
       sum(a.cache_read_tokens) AS cache_read_tokens,
       round(accurateCastOrNull(sum(a.cost_usd), 'Decimal64(9)'), 2) AS cost_usd,
       quantileExactInclusive(0.5)(a.latency_ms) AS p50_latency_ms
FROM {db}.api_requests AS a GROUP BY a.model""",
}
