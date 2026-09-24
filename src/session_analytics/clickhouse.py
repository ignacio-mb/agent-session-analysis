"""Load the session warehouse into ClickHouse, over its HTTP interface (no driver: urllib).

    session-analytics warehouse --clickhouse       the connection string: CLICKHOUSE_URL in this checkout's .env
    make clickhouse                                the same, then the check against the raw transcripts

The same tables as the Postgres warehouse (warehouse.TABLES, same rows) and the same views, written in ClickHouse
SQL. The target database must already exist: this never creates or drops a database. Each table is loaded beside
the live one (`<table>__load`), its row count checked, and swapped in with EXCHANGE TABLES, so a dashboard that
reads during a load sees the old rows or the new ones, never an empty table.

Everything this writes carries a comment starting with MARK. A table or view of the same name without it
belongs to something else, and stops the load before anything is written; objects with other names are never
touched. The connection string is a secret: nothing here prints the password, and errors name only the host.

The connection string is read from this checkout's .env (or --env-file), never from the process environment or
the current directory: a CLICKHOUSE_URL exported for another project, or the .env of the repository a session
ran in, must not be able to point this load at someone else's cluster.
"""

from __future__ import annotations

import gzip
import json
import math
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import util
from .warehouse import BIG, BOOL, INT, NUM, TABLES, TEXT, TS, VIEW_COMMENTS, _cell

MARK = "convo-analysis"
DEFAULT_DATABASE = "sessions"
ENV_KEYS = ("CLICKHOUSE_URL", "CLICKHOUSE_PASSWORD", "CLICKHOUSE_DATABASE")
CHUNK_BYTES = 8 << 20  # uncompressed JSONEachRow per INSERT
TYPES = {TEXT: "String", INT: "Int64", BIG: "Int64", NUM: "Float64", TS: "DateTime64(3, 'UTC')", BOOL: "Bool"}
INSERT_SETTINGS = {"date_time_input_format": "best_effort", "input_format_null_as_default": "1"}


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


def default_env_file():
    """The checkout's .env (beside .env.example)."""
    return Path(__file__).resolve().parents[2] / ".env"


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
        (TLS with an s-scheme, ?secure=true, or port 8443/443). Only the HTTP interface is spoken, so native-protocol
        ports (9000, 9440) are refused with the port to use instead."""
        if not url:
            raise ClickHouseError("no CLICKHOUSE_URL: copy .env.example to .env (make env) and fill it in")
        u = urllib.parse.urlsplit(url.strip())
        scheme = u.scheme.lower()
        if scheme not in ("http", "https", "clickhouse", "clickhouses", "clickhousedb", "clickhouse+http",
                          "clickhouse+https", "ch"):
            raise ClickHouseError(f"CLICKHOUSE_URL: unsupported scheme {u.scheme!r}; "
                                  f"use https://user:password@host:8443/db")
        if not u.hostname:
            raise ClickHouseError("CLICKHOUSE_URL has no host")
        query = urllib.parse.parse_qs(u.query)
        secure = (query.get("secure") or query.get("ssl") or [""])[0].lower() in ("1", "true", "yes")
        port = u.port
        tls = scheme in ("https", "clickhouses", "clickhouse+https") or secure or port in (8443, 443)
        if port in (9000, 9440):
            raise ClickHouseError(f"CLICKHOUSE_URL: port {port} is ClickHouse's native protocol; this loader speaks "
                                  f"HTTP — use {8443 if port == 9440 else 8123}")
        port = port or (8443 if tls else 8123)
        user = urllib.parse.unquote(u.username) if u.username else "default"
        pw = password if password is not None else urllib.parse.unquote(u.password or "")
        db = (u.path or "").strip("/") or database or (query.get("database") or [""])[0] or DEFAULT_DATABASE
        return cls(u.hostname, port, tls, user, pw, urllib.parse.unquote(db))

    @property
    def base(self):
        return f"{'https' if self.tls else 'http'}://{self.host}:{self.port}/"

    def __repr__(self):
        return f"{self.user}@{self.host}:{self.port}/{self.database}"


def target_from_settings(env_file=None):
    s, path = settings(env_file)
    if not s["CLICKHOUSE_URL"]:
        raise ClickHouseError(f"CLICKHOUSE_URL is not set in {path}: copy .env.example to .env (make env) and fill in "
                              f"the connection string")
    return Target.from_url(s["CLICKHOUSE_URL"], password=s["CLICKHOUSE_PASSWORD"], database=s["CLICKHOUSE_DATABASE"])


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

    def rows(self, sql, params=None):
        """A SELECT's rows, as dicts (JSONEachRow)."""
        out = self.run(sql.rstrip().rstrip(";") + " FORMAT JSONEachRow", params=params,
                       settings={"output_format_json_quote_64bit_integers": "0"})
        return [json.loads(line) for line in out.splitlines() if line.strip()]


# ---------------------------------------------------------------- DDL
def ident(name):
    return "`" + name.replace("`", "``") + "`"


def lit(s):
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


def qualified(db, name):
    return f"{ident(db)}.{ident(name)}"


def table_ddl(db, name, as_name=None):
    """CREATE TABLE for one warehouse table: primary-key columns required, the rest Nullable, ordered by the key."""
    comment, cols, pk = TABLES[name]
    body = []
    for c, t, d in cols:
        typ = TYPES[t] if c in pk else f"Nullable({TYPES[t]})"
        body.append(f"  {ident(c)} {typ}" + (f" COMMENT {lit(d)}" if d else ""))
    return (f"CREATE TABLE {qualified(db, as_name or name)} (\n" + ",\n".join(body) + "\n)\nENGINE = MergeTree\n"
            f"ORDER BY ({', '.join(ident(c) for c in pk)})\nCOMMENT {lit(f'{MARK} · {comment}')}")


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


def json_lines(name, rows):
    """JSONEachRow chunks of at most CHUNK_BYTES."""
    cols = [(c, t) for c, t, _ in TABLES[name][1]]
    buf, size = [], 0
    for r in rows:
        line = json.dumps({c: json_value(r.get(c), t) for c, t in cols}, ensure_ascii=False, separators=(",", ":"))
        buf.append(line)
        size += len(line) + 1
        if size >= CHUNK_BYTES:
            yield "\n".join(buf).encode("utf-8")
            buf, size = [], 0
    if buf:
        yield "\n".join(buf).encode("utf-8")


# ---------------------------------------------------------------- load
def preflight(client, db):
    """The database exists, and nothing this would replace belongs to anyone else. Returns {name: engine}."""
    if not client.rows("SELECT name FROM system.databases WHERE name = {db:String}", {"db": db}):
        raise ClickHouseError(f"database {db} does not exist on {client.t.host}: create it first "
                              f"(CREATE DATABASE {ident(db)}) — this loader never creates one")
    existing = {r["name"]: r for r in client.rows(
        "SELECT name, engine, comment FROM system.tables WHERE database = {db:String}", {"db": db})}
    ours = set(TABLES) | set(VIEWS) | {f"{t}__load" for t in TABLES}
    foreign = sorted(n for n in ours & set(existing) if not (existing[n]["comment"] or "").startswith(MARK))
    if foreign:
        raise ClickHouseError(f"{db} already has {', '.join(foreign)}, not created by {MARK} (no '{MARK}' comment): "
                              f"refusing to replace them — load into a database of its own")
    return {n: r["engine"] for n, r in existing.items()}


def load(tables, target, log=print):
    """Load every table (swap in), then the views. Returns {table: rows}."""
    client = Client(target)
    db = target.database
    existing = preflight(client, db)
    counts = {}
    for name in TABLES:
        rows = tables.get(name, ())
        tmp = f"{name}__load"
        client.run(f"DROP TABLE IF EXISTS {qualified(db, tmp)} SYNC")
        client.run(table_ddl(db, name, as_name=tmp))
        for chunk in json_lines(name, rows):
            client.run(f"INSERT INTO {qualified(db, tmp)} FORMAT JSONEachRow", data=chunk, settings=INSERT_SETTINGS,
                       compress=True)
        got = int(client.rows(f"SELECT count() AS n FROM {qualified(db, tmp)}")[0]["n"])
        if got != len(rows):
            raise ClickHouseError(f"{db}.{tmp}: {got} rows arrived of {len(rows)} sent; the live table is untouched")
        if name in existing:
            client.run(f"EXCHANGE TABLES {qualified(db, tmp)} AND {qualified(db, name)}")
            client.run(f"DROP TABLE {qualified(db, tmp)} SYNC")
        else:
            client.run(f"RENAME TABLE {qualified(db, tmp)} TO {qualified(db, name)}")
        counts[name] = got
        if log and len(rows) >= 10000:
            log(f"  {name}: {got:,} rows")
    for name in VIEWS:
        client.run(view_ddl(db, name))
    return counts


# ---------------------------------------------------------------- the views, in ClickHouse SQL
# Rounding goes through Decimal: Postgres rounds numerics half away from zero, ClickHouse rounds a Float64 half to
# even, and the two warehouses are meant to agree to the digit. accurateCastOrNull, not toDecimal64: a division by
# nullIf(0) is still computed under the NULL, and the infinity there would fail the cast.
# Same names, columns and meaning as warehouse.VIEWS. Differences that matter: percentile_cont is
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
       q.session_id AS session_id, s.project AS project,
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
LEFT JOIN {db}.skill_runs AS r ON r.run_id = q.run_id
LEFT JOIN {db}.sessions AS s ON s.session_id = q.session_id""",
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
FROM {db}.questions AS q LEFT JOIN {db}.question_options AS o ON o.qid = q.qid
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
