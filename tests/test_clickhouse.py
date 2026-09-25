"""The ClickHouse load, offline: the connection string, the DDL, the rows, and the load's order of operations against
a fake server (a live one: `make clickhouse-dev`, then scripts/metabase_dashboard.py --test --clickhouse)."""

import hashlib
import json
import os
import re
import time

import pytest

from session_analytics import clickhouse, semantics, warehouse
from session_analytics.clickhouse import ClickHouseError, Target


def test_connection_strings():
    t = Target.from_url("https://reader:s3cr%40t@abc.eu-west-1.aws.clickhouse.cloud:8443/sessions")
    assert (t.host, t.port, t.tls, t.user, t.password, t.database) == \
        ("abc.eu-west-1.aws.clickhouse.cloud", 8443, True, "reader", "s3cr@t", "sessions")
    assert "s3cr" not in repr(t) and t.base == "https://abc.eu-west-1.aws.clickhouse.cloud:8443/"
    t = Target.from_url("clickhouse://default:pw@host.example?secure=true")
    assert (t.port, t.tls, t.database) == (8443, True, "sessions")
    t = Target.from_url("http://convo:convo@127.0.0.1:18123", database="elsewhere")
    assert (t.port, t.tls, t.database) == (18123, False, "elsewhere")
    t = Target.from_url("https://u@host:8443/db", password="p/w#1")  # CLICKHOUSE_PASSWORD: no encoding needed
    assert t.password == "p/w#1"
    # the JDBC string the ClickHouse Cloud console hands out
    t = Target.from_url("jdbc:clickhouse://abc.aws.clickhouse.cloud:8443?user=default&password=a+b%26c&ssl=true")
    assert (t.host, t.port, t.tls, t.user, t.password, t.database) == \
        ("abc.aws.clickhouse.cloud", 8443, True, "default", "a+b&c", "sessions")
    t = Target.from_url("jdbc:ch:https://abc.aws.clickhouse.cloud:8443/other?user=u", password="from-the-env-file")
    assert (t.tls, t.user, t.password, t.database) == (True, "u", "from-the-env-file", "other")
    with pytest.raises(ClickHouseError, match="use 8443"):
        Target.from_url("clickhouse://u:p@host:9440/sessions")
    with pytest.raises(ClickHouseError, match="scheme"):
        Target.from_url("postgres://u:p@host/sessions")


def test_the_connection_comes_from_the_env_file_only(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# ClickHouse\nexport CLICKHOUSE_URL='https://u:p@from-file:8443/sessions'\nCLICKHOUSE_DATABASE=\n")
    monkeypatch.setenv("CLICKHOUSE_URL", "https://u:p@from-environment:8443/other")
    assert clickhouse.target_from_settings(env).host == "from-file"
    env.write_text("CLICKHOUSE_URL=\n")
    with pytest.raises(ClickHouseError, match="not set"):
        clickhouse.target_from_settings(env)


def test_table_ddl():
    ddl = clickhouse.table_ddl("sessions", "questions", as_name="questions__rebuild")
    assert ddl.startswith("CREATE TABLE `sessions`.`questions__rebuild` (")
    assert "`source` LowCardinality(String)" in ddl and "`person` LowCardinality(String)" in ddl
    assert "`qid` String" in ddl and "`asked_at` Nullable(DateTime64(3, 'UTC'))" in ddl
    assert "`multi` Nullable(Bool)" in ddl and "PARTITION BY source\nORDER BY (`qid`)" in ddl
    assert f"COMMENT '{clickhouse.MARK} {clickhouse.__version__} · " in ddl and "COMMENT 'ask | prose | checkpoint'" in ddl
    topics = clickhouse.table_ddl("sessions", "de_topics")  # the taxonomy: the same for everyone, not partitioned
    assert "source" not in topics and "PARTITION BY" not in topics
    assert "`machine`" in clickhouse.table_ddl("db", "warehouse_load")
    assert clickhouse.lit("it's a \\ path") == "'it\\'s a \\\\ path'"
    for name, (_, _cols, pk) in warehouse.TABLES.items():
        assert f"ORDER BY ({', '.join(f'`{c}`' for c in pk)})" in clickhouse.table_ddl("db", name)


def test_rows_match_what_postgres_would_hold():
    jv = clickhouse.json_value
    assert jv("", warehouse.TEXT) is None and jv(None, warehouse.INT) is None  # empty text is NULL, as in COPY csv
    assert jv(3.0, warehouse.INT) == 3 and jv(True, warehouse.BIG) == 1 and jv(float("nan"), warehouse.NUM) is None
    assert jv(1.5, warehouse.NUM) == 1.5 and jv(0, warehouse.BOOL) is False and jv(3.0, warehouse.TEXT) == "3"
    assert jv(0, warehouse.TS) == "1970-01-01T00:00:00.000Z" and jv(["a", "b"], warehouse.TEXT) == '["a", "b"]'


def test_rows_are_sent_in_chunks_with_the_loaders_columns(monkeypatch):
    monkeypatch.setattr(clickhouse, "CHUNK_BYTES", 200)
    rows = [{"id": f"t{i}", "label": "x" * 50, "description": None, "sort_order": i} for i in range(10)]
    chunks = list(clickhouse.json_lines("de_topics", rows))
    assert len(chunks) > 1 and sum(c.count(b"\n") + 1 for c in chunks) == 10
    assert b'"description":null' in chunks[0] and b'"sort_order":0' in chunks[0]
    me = clickhouse.Identity("abc123", "ana@example.com", "laptop")
    (chunk,) = clickhouse.json_lines("sessions", [{"session_id": "s1"}], me.columns("sessions"))
    assert chunk.startswith(b'{"source":"abc123","person":"ana@example.com","session_id":"s1"')
    assert me.columns("de_topics") == {} and me.columns("warehouse_load")["machine"] == "laptop"


def test_views_are_the_postgres_views_qualified():
    assert list(clickhouse.VIEWS) == list(warehouse.VIEW_COMMENTS)
    for name, sql in clickhouse.VIEWS.items():
        tables = re.findall(r"\b(?:FROM|(?<!ARRAY )JOIN)\s+([\w.{}]+)", sql)
        assert tables and all(t.startswith("{db}.") or t in ("v", "asked") for t in tables), (name, tables)
        assert "::" not in sql and "percentile_cont" not in sql and "LATERAL" not in sql, name
        # run and question ids repeat across machines: a join on one also matches the session
        for m in re.finditer(r"ON (\w+)\.(run_id|qid) = (\w+)\.\2(.*)", sql):
            assert "session_id" in m.group(4), (name, m.group(0))
        ddl = clickhouse.view_ddl("sessions", name)
        assert ddl.startswith(f"CREATE OR REPLACE VIEW `sessions`.`{name}` AS") and clickhouse.MARK in ddl
    assert "q.person AS person" in clickhouse.VIEWS["v_interview_questions"]


class FakeClickHouse:
    """A tiny ClickHouse over real rows: tables with columns and per-source partitions of row dicts, and views with
    their comments, answering the handful of statement shapes clickhouse.py sends, and logging every statement.
    Constructing it as a Client counts as a connection (`connections`)."""

    def __init__(self, tables=None, databases=("sessions",), short=None, short_copy=None):
        # name -> {"comment", "columns", "parts": {source: [row, ...]}}
        self.tables = tables or {}
        self.views = {}  # name -> comment
        self.databases, self.short, self.short_copy = databases, short, short_copy
        self.sql, self.connections = [], 0

    def __call__(self, target, timeout=300):
        self.t = target
        self.connections += 1
        return self

    @staticmethod
    def table(comment, columns, parts=None):
        return {"comment": comment, "columns": set(columns), "parts": {k: list(v) for k, v in (parts or {}).items()}}

    def rows_of(self, table, source=None):
        parts = self.tables[table]["parts"]
        return [r for src, rows in parts.items() if source in (None, src) for r in rows]

    def held(self, table):
        """{source: rows} of a table."""
        return {src: len(rows) for src, rows in self.tables[table]["parts"].items() if rows}

    def sessions(self, table, source):
        return {r.get("session_id") for r in self.rows_of(table, source)}

    @staticmethod
    def ids_in(sql):
        m = re.search(r"NOT has\((\[.*?\]), session_id\)", sql, re.S)
        return set(re.findall(r"'([^']*)'", m.group(1))) if m else set()

    def rows(self, sql, params=None, settings=None):
        params = params or {}
        if "system.databases" in sql:
            return [{"name": params["db"]}] if params["db"] in self.databases else []
        if "system.tables" in sql:
            return ([{"name": n, "engine": "MergeTree", "comment": t["comment"]} for n, t in self.tables.items()]
                    + [{"name": n, "engine": "View", "comment": c} for n, c in self.views.items()])
        if "system.columns" in sql:
            return [{"table": n, "name": c} for n, t in self.tables.items() for c in t["columns"]]
        src = params.get("src")
        if "UNION ALL" in sql:
            names = re.findall(r"FROM `sessions`\.`(\w+)` WHERE source", sql)
            ids = {r.get("session_id") for n in names for r in self.rows_of(n, src)}
            return [{"session_id": sid} for sid in sorted(i for i in ids if i)]
        m = re.search(r"FROM `sessions`\.`(\w+)`", sql)
        name = m.group(1)
        rows = self.rows_of(name, src) if src else self.rows_of(name)
        if "NOT has(" in sql:
            rows = [r for r in rows if r.get("session_id") not in self.ids_in(sql)]
        if sql.startswith("SELECT count()"):
            n = len(rows)
            if self.short and name.startswith(f"{self.short}__load") and n:
                n -= 1
            return [{"n": n}]
        if sql.startswith("SELECT DISTINCT session_id"):
            return [{"session_id": sid} for sid in sorted({r.get("session_id") for r in rows})]
        if sql.startswith("SELECT * "):
            return [dict(r) for r in rows]
        cols = [c.strip(" `") for c in re.match(r"SELECT (.*?) FROM", sql).group(1).split(",")]
        return [{c: r.get(c) for c in cols} for r in rows]

    def run(self, sql, data=None, params=None, settings=None, compress=False):
        self.sql.append(sql.split("\n")[0])
        params = params or {}
        name = lambda x: x.split("`.`")[1].rstrip("`")  # noqa: E731
        comment = re.search(r"COMMENT '((?:[^'\\]|\\.)*)'\s*$", sql)
        if m := re.match(r"CREATE TABLE (?:IF NOT EXISTS )?(`\S+`)( AS (`\S+`))?", sql):
            n = name(m.group(1))
            if n not in self.tables:
                if m.group(2):
                    like = self.tables[name(m.group(3))]
                    self.tables[n] = self.table(like["comment"], like["columns"])
                else:
                    self.tables[n] = self.table(comment.group(1) if comment else clickhouse.MARK,
                                                set(re.findall(r"^  `(\w+)`", sql, re.M)))
        elif m := re.match(r"CREATE OR REPLACE VIEW (`\S+`)", sql):
            self.views[name(m.group(1))] = comment.group(1)
        elif m := re.match(r"INSERT INTO (`\S+`) SELECT \* FROM (`\S+`) WHERE source", sql):
            ids = self.ids_in(sql)
            kept = [dict(r) for r in self.tables[name(m.group(2))]["parts"].get(params["src"], [])
                    if r.get("session_id") not in ids]
            if self.short_copy == name(m.group(2)) and kept:
                kept = kept[:-1]  # a copy that read a stale replica
            self.tables[name(m.group(1))]["parts"].setdefault(params["src"], []).extend(kept)
        elif m := re.match(r"INSERT INTO (`\S+`)", sql):
            parts = self.tables[name(m.group(1))]["parts"]
            for line in data.decode().splitlines():
                row = json.loads(line)
                parts.setdefault(row.get("source", ""), []).append(row)
        elif m := re.match(r"ALTER TABLE (`\S+`) REPLACE PARTITION '(\w+)' FROM (`\S+`)", sql):
            src_rows = self.tables[name(m.group(3))]["parts"][m.group(2)]
            self.tables[name(m.group(1))]["parts"][m.group(2)] = [dict(r) for r in src_rows]
        elif m := re.match(r"ALTER TABLE (`\S+`) DROP PARTITION '(\w+)'", sql):
            self.tables[name(m.group(1))]["parts"].pop(m.group(2), None)
        elif m := re.match(r"ALTER TABLE (`\S+`) ADD COLUMN IF NOT EXISTS `(\w+)`", sql):
            self.tables[name(m.group(1))]["columns"].add(m.group(2))
        elif m := re.match(r"EXCHANGE TABLES (`\S+`) AND (`\S+`)", sql):
            a, b = name(m.group(1)), name(m.group(2))
            self.tables[a], self.tables[b] = self.tables[b], self.tables[a]
        elif m := re.match(r"RENAME TABLE (`\S+`) TO (`\S+`)", sql):
            self.tables[name(m.group(2))] = self.tables.pop(name(m.group(1)))
        elif m := re.match(r"DROP TABLE (?:IF EXISTS )?(`\S+`)", sql):
            self.tables.pop(name(m.group(1)), None)
        return ""


def _tables(sessions=None, questions=None, skill="rde", success=True):
    """Warehouse rows for sessions {id: questions}; each invoked `skill` (None: no skill at all)."""
    if sessions is None:
        sessions = {"s": questions} if questions is not None else {"s1": 2}
    t = {k: [] for k in warehouse.TABLES}
    t["de_topics"] = [{"id": "privacy", "label": "Privacy", "description": None, "sort_order": 1}]
    for sid, n in sessions.items():
        t["sessions"].append({"session_id": sid})
        t["questions"] += [{"qid": f"{sid}:q{i}", "session_id": sid} for i in range(n)]
        if skill:
            t["skill_invocations"].append({"session_id": sid, "invocation_no": 0, "skill": skill, "success": success,
                                           "status": "ok" if success else "error"})
    t["warehouse_load"] = [{"loaded_at": "2026-09-24T12:00:00.000Z", "transcripts": 1, "sessions": len(sessions)}]
    return t


def _merge(*parts):
    out = {k: [] for k in warehouse.TABLES}
    for p in parts:
        for k, rows in p.items():
            if k in ("de_topics", "de_layers", "warehouse_load"):
                out[k] = rows
            else:
                out[k] += rows
    return out


ANA = clickhouse.Identity("aaaa", "ana@example.com", "ana-laptop")
BO = clickhouse.Identity("bbbb", "bo@example.com", "bo-desktop")
URL = "https://u:p@h:8443/sessions"
RDE = ("rde",)


def sync(tables, ident_, read=None, full=False):
    read = {r["session_id"] for r in tables["sessions"]} if read is None else read
    return clickhouse.sync(tables, Target.from_url(URL), ident_, read, RDE, full=full, log=None)


def test_each_machine_changes_only_its_own_rows(monkeypatch):
    ch = FakeClickHouse()
    monkeypatch.setattr(clickhouse, "Client", ch)
    sync(_tables({"a1": 2}), ANA, full=True)
    sync(_tables({"b1": 3}), BO, full=True)
    assert ch.held("questions") == {"aaaa": 2, "bbbb": 3}
    sync(_tables({"a1": 5}), ANA, full=True)  # Ana again: her rows replaced, Bo's kept
    assert ch.held("questions") == {"aaaa": 5, "bbbb": 3}
    assert "ALTER TABLE `sessions`.`questions` REPLACE PARTITION 'aaaa' FROM `sessions`.`questions__load_aaaa`" in ch.sql
    assert ch.held("de_topics") == {"": 1}  # the taxonomy: the same for everyone
    assert not any("__load" in n for n in ch.tables)  # staging never outlives a sync
    assert sum(x.startswith("CREATE OR REPLACE VIEW") for x in ch.sql) >= len(clickhouse.VIEWS)
    first_sessions = next(i for i, x in enumerate(ch.sql) if x.startswith("ALTER TABLE `sessions`.`sessions` REPLACE"))
    assert all(not x.startswith("ALTER TABLE") or "warehouse_load" in x or "de_" in x
               for x in ch.sql[first_sessions + 1:first_sessions + 3])  # `sessions` is written after the rest


def test_the_hook_updates_one_session_and_leaves_every_other_alone(monkeypatch):
    ch = FakeClickHouse()
    monkeypatch.setattr(clickhouse, "Client", ch)
    sync(_tables({"a1": 2, "a2": 1}), ANA, full=True)
    sync(_tables({"b1": 3}), BO, full=True)
    res = sync(_tables({"a2": 4}), ANA)  # a2 ended again: updated
    assert res["written"] == ["a2"] and res["removed"] == [] and res["held"] == 2
    assert ch.sessions("questions", "aaaa") == {"a1", "a2"} and len(ch.rows_of("questions", "aaaa")) == 6
    assert ch.held("questions")["bbbb"] == 3
    sync(_tables({"a3": 1}), ANA)  # a new session: added
    assert ch.sessions("sessions", "aaaa") == {"a1", "a2", "a3"}
    res = sync(_tables({"a1": 1}, skill=None), ANA)  # a1 read again, no longer runs rde: taken out
    assert res["removed"] == ["a1"] and ch.sessions("sessions", "aaaa") == {"a2", "a3"}
    assert ch.held("sessions")["bbbb"] == 1
    (row,) = ch.rows_of("warehouse_load", "aaaa")
    assert (row["sessions"], row["transcripts"], row["skills"], row["since"]) == (2, 2, "rde", "all")


def test_a_session_not_read_this_time_keeps_its_history(monkeypatch):
    ch = FakeClickHouse()
    monkeypatch.setattr(clickhouse, "Client", ch)
    sync(_tables({"a1": 2, "a2": 1}), ANA, full=True)
    # a1's transcript is gone (Claude Code prunes old ones), or outside --since: a full sync must not take it out
    res = sync(_tables({"a2": 1}), ANA, full=True)
    assert res["removed"] == [] and ch.sessions("sessions", "aaaa") == {"a1", "a2"}


def test_what_an_older_version_shared_is_taken_out(monkeypatch):
    ch = FakeClickHouse()
    monkeypatch.setattr(clickhouse, "Client", ch)
    # as the previous version did: every session shared, rde or not, no scope recorded
    everything = _merge(_tables({"a1": 1}), _tables({"x1": 2}, skill="dataviz"), _tables({"x2": 1}, skill=None))
    clickhouse.load(everything, Target.from_url(URL), ANA, log=None)
    assert ch.sessions("sessions", "aaaa") == {"a1", "x1", "x2"}
    res = sync(_tables({"a1": 1}), ANA)  # the hook, after the upgrade: one rde session ends
    assert sorted(res["stale"]) == ["x1", "x2"] and ch.sessions("sessions", "aaaa") == {"a1"}
    assert ch.sessions("questions", "aaaa") == {"a1"} and ch.sessions("skill_invocations", "aaaa") == {"a1"}
    (row,) = ch.rows_of("warehouse_load", "aaaa")
    assert row["skills"] == "rde" and row["sessions"] == 1  # the marker is written once the partition matches it


def test_a_rejected_or_failed_skill_call_does_not_share_the_session(monkeypatch):
    ch = FakeClickHouse()
    monkeypatch.setattr(clickhouse, "Client", ch)
    res = sync(_tables({"a1": 3}, success=False), ANA)
    assert res["written"] == [] and "sessions" not in ch.tables


def test_a_session_that_never_ran_rde_costs_no_request(monkeypatch):
    ch = FakeClickHouse()
    monkeypatch.setattr(clickhouse, "Client", ch)
    t = Target.from_url(URL)
    assert clickhouse.needs_sync(t, ANA, {"x1"}, set(), RDE)  # no record yet: ask what is shared (source hash only)
    sync(_tables({"x1": 1}, skill=None), ANA)
    assert ch.connections == 1 and not any(x.startswith(("INSERT", "ALTER", "CREATE")) for x in ch.sql)
    assert "sessions" not in ch.tables and "warehouse_load" not in ch.tables  # nothing written, not even a load row
    assert not clickhouse.needs_sync(t, ANA, {"x2"}, set(), RDE)  # now known: nothing shared, scope unchanged
    sync(_tables({"a1": 1}), ANA)
    assert clickhouse.needs_sync(t, ANA, {"a1"}, set(), RDE)  # a shared session read again: it may have to go
    # a changed scope alone is no reason to call: that waits for a pass with something to share, or --rescope
    assert not clickhouse.needs_sync(t, ANA, {"x2"}, set(), ("dataviz",))


def test_tables_from_before_per_source_loads(monkeypatch):
    old = {"qid", "session_id"}  # no `source`: it held only the last loader's rows
    ch = FakeClickHouse(tables={"questions": FakeClickHouse.table(f"{clickhouse.MARK} · old", old, {"": [{"qid": "q"}]}),
                                "unrelated": FakeClickHouse.table("", {"x"})})
    monkeypatch.setattr(clickhouse, "Client", ch)
    with pytest.raises(ClickHouseError, match="run a full load first"):
        sync(_tables({"a1": 1}), ANA)  # the hook: not with a partial view of the machine
    sync(_tables({"a1": 2}), ANA, full=True)
    assert ch.held("questions") == {"aaaa": 2} and "source" in ch.tables["questions"]["columns"]
    assert "EXCHANGE TABLES `sessions`.`questions__rebuild` AND `sessions`.`questions`" in ch.sql
    assert "questions__rebuild" not in ch.tables and "unrelated" in ch.tables
    assert not any("unrelated" in x for x in ch.sql)  # never touches what is not its own
    ch.tables["questions"]["columns"].discard("reask_of")  # a newer version's column: added, nothing dropped
    sync(_tables({"a1": 2}), ANA)
    assert "ALTER TABLE `sessions`.`questions` ADD COLUMN IF NOT EXISTS `reask_of` Nullable(String)" in ch.sql


def test_what_it_did_not_create_a_short_copy_and_a_short_insert_are_refused(monkeypatch):
    monkeypatch.setattr(clickhouse, "Client", FakeClickHouse(tables={"sessions": FakeClickHouse.table("x", {"a"})}))
    with pytest.raises(ClickHouseError, match="not created by convo-analysis"):
        sync(_tables(), ANA)
    monkeypatch.setattr(clickhouse, "Client", FakeClickHouse(databases=()))
    with pytest.raises(ClickHouseError, match="does not exist"):
        sync(_tables(), ANA)
    ch = FakeClickHouse(short="questions")
    monkeypatch.setattr(clickhouse, "Client", ch)
    with pytest.raises(ClickHouseError, match="the live table is untouched"):
        sync(_tables({"a1": 2}), ANA)
    assert not any("REPLACE PARTITION 'aaaa' FROM `sessions`.`questions__load" in x for x in ch.sql)
    ch = FakeClickHouse()
    monkeypatch.setattr(clickhouse, "Client", ch)
    sync(_tables({"a1": 2, "a2": 3}), ANA)
    ch.short_copy = "questions"  # the copy of the live partition came back short (a replica still catching up)
    with pytest.raises(ClickHouseError, match="copied"):
        sync(_tables({"a1": 1}), ANA)
    assert len(ch.rows_of("questions", "aaaa")) == 5 and "questions__load_aaaa" not in ch.tables


def test_forget(monkeypatch):
    ch = FakeClickHouse()
    monkeypatch.setattr(clickhouse, "Client", ch)
    sync(_tables({"a1": 1, "a2": 2}), ANA)
    sync(_tables({"b1": 1}), BO)
    assert clickhouse.forget(Target.from_url(URL), ANA, ["a2", "zz"]) == (["a2"], ["zz"])
    assert ch.sessions("sessions", "aaaa") == {"a1"} and ch.held("sessions")["bbbb"] == 1
    (row,) = ch.rows_of("warehouse_load", "aaaa")
    assert row["sessions"] == 1  # the load row follows what is left
    sync(_tables({"a1": 1, "a2": 5}), ANA, full=True)  # a withdrawn session is not shared again by a later sync
    assert ch.sessions("sessions", "aaaa") == {"a1"}
    assert clickhouse.forget(Target.from_url(URL), ANA) == (["a1"], [])
    assert "aaaa" not in ch.held("sessions") and ch.held("sessions") == {"bbbb": 1}
    assert "aaaa" not in ch.held("warehouse_load")  # nothing held: no row saying who loaded
    sync(_tables({"a1": 1, "a3": 1}), ANA)  # a new session is shared; the forgotten ones stay out
    assert ch.sessions("sessions", "aaaa") == {"a3"}
    clickhouse.reshare(Target.from_url(URL), "aaaa", ["a1"])  # named again: shared again
    sync(_tables({"a1": 1}), ANA)
    assert ch.sessions("sessions", "aaaa") == {"a1", "a3"}


STAMP = f"{clickhouse.MARK} {clickhouse.__version__} #"
TESTS_LAYER = {"id": "tests", "label": "Tests", "description": None, "sort_order": 0}


def _shared_writes(ch, since):
    """The statements from `since` on that wrote a taxonomy table or a view."""
    return [x for x in ch.sql[since:] if x.startswith("CREATE OR REPLACE VIEW") or "`de_" in x]


def test_the_views_and_the_taxonomy_are_rewritten_only_when_they_changed(monkeypatch):
    ch = FakeClickHouse()
    monkeypatch.setattr(clickhouse, "Client", ch)
    sync(_tables({"a1": 1}), ANA)
    assert set(ch.views) == set(clickhouse.VIEWS) and all(c.startswith(STAMP) for c in ch.views.values())
    assert ch.tables["de_topics"]["comment"].startswith(STAMP)
    before = len(ch.sql)
    sync(_tables({"a1": 2}), ANA)
    assert _shared_writes(ch, before) == []
    # this version with other content — an edit not yet committed — is written over
    ch.views["v_daily"] = f"{STAMP}0123456789ab · edited in a checkout"
    before = len(ch.sql)
    sync(_tables({"a1": 3}), ANA)
    assert _shared_writes(ch, before) == ["CREATE OR REPLACE VIEW `sessions`.`v_daily` AS"]
    assert ch.views["v_daily"].startswith(STAMP) and "edited" not in ch.views["v_daily"]


def test_only_the_sessions_that_ran_the_skill_are_shared():
    t = _merge(_tables({"r1": 1, "r2": 2}), _tables({"x1": 5}, skill="dataviz"),
               _tables({"p1": 1}, skill="agent-skills:rde"), _tables({"n1": 1}, skill="rde", success=False))
    assert warehouse.sessions_with_skill(t, RDE) == {"r1", "r2", "p1"}
    assert warehouse.sessions_with_skill(t, ("*",)) == {"r1", "r2", "x1", "p1", "n1"}
    assert warehouse.sessions_with_skill(t, ("agent-skills:rde",)) == {"p1"}  # a plugin prefix: that plugin only
    only = warehouse.only_sessions(t, {"r1"}, RDE)
    assert [r["session_id"] for r in only["questions"]] == ["r1"] and only["de_topics"] == t["de_topics"]
    assert only["warehouse_load"][0]["sessions"] == 1 and only["warehouse_load"][0]["skills"] == "rde"
    assert only["warehouse_load"][0]["transcripts"] == 1  # not how many transcripts the machine has
    assert clickhouse.skills_setting(override="rde, dataviz") == ("dataviz", "rde")
    assert clickhouse.skills_setting(override="") == ("rde",)


def test_who_is_loading(tmp_path, monkeypatch):
    monkeypatch.setattr(clickhouse, "_machine_id", lambda: "machine-1")
    one, two = tmp_path / "claude-a", tmp_path / "claude-b"
    assert clickhouse.source_id(one) == clickhouse.source_id(one) != clickhouse.source_id(two)
    assert len(clickhouse.source_id(one)) == 16 and "machine-1" not in clickhouse.source_id(one)
    env = tmp_path / "c.env"
    env.write_text("CLICKHOUSE_URL=https://u:p@h:8443/sessions\nCLICKHOUSE_PERSON=ana@example.com\n")
    me = clickhouse.identity(one, env)
    assert me.person == "ana@example.com" and me.source == clickhouse.source_id(one)
    env.write_text("CLICKHOUSE_URL=https://u:p@h:8443/sessions\n")  # a new name keeps the same source
    monkeypatch.setattr(clickhouse, "_git_email", lambda: "ana@work.example")
    assert clickhouse.identity(one, env).person == "ana@work.example" and clickhouse.identity(one, env).source == me.source


def test_the_env_file_lives_in_the_users_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    checkout = tmp_path / "checkout.env"
    monkeypatch.setattr(clickhouse, "checkout_env_file", lambda: checkout)
    user = tmp_path / "config" / "convo-analysis" / ".env"
    assert clickhouse.default_env_file() == user  # neither exists: the user's is the one to create
    checkout.write_text("CLICKHOUSE_URL=https://u:p@old:8443/sessions\n")
    assert clickhouse.default_env_file() == checkout  # a checkout set up before ~/.config still works
    path, created = clickhouse.init_env()
    assert (path, created) == (user, True) and oct(user.stat().st_mode & 0o777) == "0o600"
    assert "CLICKHOUSE_URL=" in user.read_text() and clickhouse.default_env_file() == user
    user.write_text("CLICKHOUSE_URL=https://u:p@mine:8443/sessions\n")
    assert clickhouse.init_env() == (user, False) and "mine" in user.read_text()  # never over an existing one


def test_the_hook_does_nothing_until_something_is_set_up(tmp_path, monkeypatch, capsys):
    from session_analytics import cli
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(clickhouse, "checkout_env_file", lambda: tmp_path / "none.env")
    monkeypatch.setattr(warehouse, "running", lambda container=None: False)
    monkeypatch.setattr(warehouse, "build", lambda *a, **k: pytest.fail("parsed transcripts with nothing to load"))
    assert cli.main(["warehouse", "--load=auto", "--clickhouse=auto", "--claude-dir", str(tmp_path)]) == 0
    assert "Nothing to load" in capsys.readouterr().err


def _hook_setup(tmp_path, monkeypatch, fake):
    """A machine with one session (the interview fixture, which invokes the skill `demo`), queued by the hook."""
    from test_questions import interview_session
    claude = tmp_path / "claude"
    claude.mkdir()
    skill = tmp_path / "skill"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("# demo\n")
    transcript = interview_session(claude, skill)
    queue = tmp_path / "queue"
    queue.mkdir()
    (queue / transcript.stem).write_text(f"{transcript}\n")
    env = tmp_path / "ch.env"
    env.write_text("CLICKHOUSE_URL=https://u:p@h:8443/sessions\nCLICKHOUSE_PERSON=ana@example.com\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(clickhouse, "_machine_id", lambda: "machine-1")
    monkeypatch.setattr(clickhouse, "Client", fake)
    monkeypatch.setattr(warehouse, "running", lambda container=None: False)
    args = ["warehouse", "--clickhouse=auto", "--session-queue", str(queue), "--env-file", str(env),
            "--claude-dir", str(claude), "--out", str(tmp_path / "out")]
    return transcript, queue, args


def test_the_hook_exports_the_session_that_ended_and_clears_it_from_the_queue(tmp_path, monkeypatch):
    from session_analytics import cli
    ch = FakeClickHouse()
    transcript, queue, args = _hook_setup(tmp_path, monkeypatch, ch)
    assert cli.main(args + ["--skills", "demo"]) == 0
    source = clickhouse.source_id(tmp_path / "claude")
    assert ch.sessions("sessions", source) == {transcript.stem} and not list(queue.iterdir())
    assert ch.sessions("questions", source) == {transcript.stem}


def test_a_session_that_did_not_invoke_the_skill_is_not_shared(tmp_path, monkeypatch):
    from session_analytics import cli
    ch = FakeClickHouse()
    _transcript, queue, args = _hook_setup(tmp_path, monkeypatch, ch)
    assert cli.main(args) == 0  # default skills: rde — the fixture only invoked `demo`
    assert "sessions" not in ch.tables and not list(queue.iterdir())  # nothing written at all; the entry is done


def test_a_failed_load_keeps_the_session_queued(tmp_path, monkeypatch):
    from session_analytics import cli
    _transcript, queue, args = _hook_setup(tmp_path, monkeypatch, FakeClickHouse(databases=()))
    assert cli.main(args + ["--skills", "demo"]) == 1
    assert len(list(queue.iterdir())) == 1  # the next session end retries it


def test_the_check_looks_only_at_the_shared_sessions(tmp_path, monkeypatch):
    from test_questions import interview_session

    from session_analytics import reconcile
    claude = tmp_path / "claude"
    claude.mkdir()
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# demo\n")
    interview_session(claude, skill)
    monkeypatch.setattr(reconcile, "warehouse_counts", lambda source: ({}, None))
    assert len(reconcile.check(None, claude)["missing"]) == 1  # the warehouse lacks it
    res = reconcile.check(None, claude, only=set())  # but it was never meant to be there
    assert res["missing"] == [] and res["checked"] == 0


def test_a_transcript_that_does_not_read_is_left_alone_and_stays_queued(tmp_path, monkeypatch):
    from session_analytics import cli
    ch = FakeClickHouse()
    transcript, queue, args = _hook_setup(tmp_path, monkeypatch, ch)
    assert cli.main(args + ["--skills", "demo"]) == 0  # shared once
    source = clickhouse.source_id(tmp_path / "claude")
    assert ch.sessions("sessions", source) == {transcript.stem}
    (queue / transcript.stem).write_text(f"{transcript}\n")  # it ends again, and this time it cannot be read
    real = warehouse.parse_session
    monkeypatch.setattr(warehouse, "parse_session", lambda *a, **k: (_ for _ in ()).throw(ValueError("mid-write")))
    assert cli.main(args + ["--skills", "demo"]) == 0
    assert ch.sessions("sessions", source) == {transcript.stem}  # not taken out
    assert [q.name for q in queue.iterdir()] == [transcript.stem]  # retried next time
    monkeypatch.setattr(warehouse, "parse_session", real)
    assert cli.main(args + ["--skills", "demo"]) == 0 and not list(queue.iterdir())


def test_the_queue_ignores_junk_and_keeps_an_entry_rewritten_meanwhile(tmp_path, monkeypatch):
    from session_analytics import cli
    ch = FakeClickHouse()
    transcript, queue, args = _hook_setup(tmp_path, monkeypatch, ch)
    (queue / ".DS_Store").write_bytes(b"\x00\x05\x16\x07\xff\xfe binary")  # Finder was here
    (queue / "notes.txt").write_text("hello")
    real = warehouse.run_warehouse

    def ended_again(*a, **k):  # the session ends once more while its export runs
        res = real(*a, **k)
        entry = queue / transcript.stem
        entry.write_text(f"{transcript}\n")
        os.utime(entry, ns=(entry.stat().st_mtime_ns + 10**9,) * 2)
        return res
    monkeypatch.setattr(warehouse, "run_warehouse", ended_again)
    assert cli.main(args + ["--skills", "demo"]) == 0
    assert sorted(q.name for q in queue.iterdir()) == [".DS_Store", transcript.stem, "notes.txt"]


def test_an_unusable_connection_string_keeps_the_queue(tmp_path, monkeypatch):
    from session_analytics import cli
    _transcript, queue, args = _hook_setup(tmp_path, monkeypatch, FakeClickHouse())
    env = tmp_path / "ch.env"
    env.write_text("CLICKHOUSE_URL=postgres://nope@host/db\n")
    assert cli.main(args) == 1 and len(list(queue.iterdir())) == 1


def test_a_queued_session_without_its_transcript_is_left_alone(tmp_path, monkeypatch):
    from session_analytics import cli
    ch = FakeClickHouse()
    transcript, queue, args = _hook_setup(tmp_path, monkeypatch, ch)
    assert cli.main(args + ["--skills", "demo"]) == 0  # shared
    source = clickhouse.source_id(tmp_path / "claude")
    moved = transcript.with_name("moved.jsonl.bak")
    transcript.rename(moved)
    (queue / transcript.stem).write_text(f"{transcript}\n")
    assert cli.main(args + ["--skills", "demo"]) == 0
    assert ch.sessions("sessions", source) == {transcript.stem} and not list(queue.iterdir())


def test_a_second_claude_config_dir_goes_under_its_own_source(tmp_path, monkeypatch):
    from test_questions import interview_session

    from session_analytics import cli
    ch = FakeClickHouse()
    _transcript, queue, args = _hook_setup(tmp_path, monkeypatch, ch)
    other = tmp_path / "claude-work"
    other.mkdir()
    theirs = interview_session(other, tmp_path / "skill", sid="eeeeeeee-0000-0000-0000-00000000000b")
    (queue / theirs.stem).write_text(f"{theirs}\n")
    assert cli.main(args + ["--skills", "demo"]) == 0
    mine, work = clickhouse.source_id(tmp_path / "claude"), clickhouse.source_id(other)
    assert ch.sessions("sessions", work) == {theirs.stem} and theirs.stem not in ch.sessions("sessions", mine)


def test_forget_one_session(tmp_path, monkeypatch, capsys):
    from session_analytics import cli
    ch = FakeClickHouse()
    transcript, _queue, args = _hook_setup(tmp_path, monkeypatch, ch)
    assert cli.main(args + ["--skills", "demo"]) == 0
    source = clickhouse.source_id(tmp_path / "claude")
    env, claude = args[args.index("--env-file") + 1], args[args.index("--claude-dir") + 1]
    assert cli.main(["warehouse", "--clickhouse-forget", "--session", transcript.stem, "--env-file", env,
                     "--claude-dir", claude]) == 0
    assert ch.sessions("sessions", source) == set() and "Took 1 session(s)" in capsys.readouterr().out


def test_the_hook_catches_up_a_session_whose_end_it_missed(tmp_path, monkeypatch):
    from test_questions import interview_session

    from session_analytics import cli
    ch = FakeClickHouse()
    transcript, queue, args = _hook_setup(tmp_path, monkeypatch, ch)
    assert cli.main(args + ["--skills", "demo"]) == 0  # the first pass records when it ran
    missed = interview_session(tmp_path / "claude", tmp_path / "skill", sid="eeeeeeee-0000-0000-0000-00000000000c")
    source = clickhouse.source_id(tmp_path / "claude")
    assert not list(queue.iterdir())  # its SessionEnd never fired: nothing queued
    assert cli.main(args + ["--skills", "demo"]) == 0
    assert missed.stem not in ch.sessions("sessions", source)  # written to just now: maybe still going, left alone
    idle = time.time() - 6 * 60
    os.utime(missed, (idle, idle))  # six minutes later, it has gone quiet
    before = len(ch.sql)
    assert cli.main(args + ["--skills", "demo"]) == 0
    assert missed.stem in ch.sessions("sessions", source)
    synced = clickhouse.read_cache(Target.from_url(URL), source)["synced"]
    assert set(synced) == {transcript.stem, missed.stem} and len(ch.sql) > before
    before = len(ch.sql)
    assert cli.main(args + ["--skills", "demo"]) == 0  # nothing new: no request at all
    assert len(ch.sql) == before


def test_a_skill_call_still_waiting_at_the_prompt_does_not_count():
    pending = _tables({"a1": 1})
    pending["skill_invocations"][0].update(success=None, status=None)  # no tool_result yet
    assert warehouse.sessions_with_skill(pending, RDE) == set()


def test_rows_an_interrupted_write_left_behind_are_found_and_taken_out(monkeypatch):
    ch = FakeClickHouse()
    monkeypatch.setattr(clickhouse, "Client", ch)
    sync(_tables({"a1": 2, "a2": 1}), ANA)
    ch.tables["sessions"]["parts"]["aaaa"] = [r for r in ch.rows_of("sessions", "aaaa") if r["session_id"] != "a2"]
    ch.tables["skill_invocations"]["parts"]["aaaa"] = [r for r in ch.rows_of("skill_invocations", "aaaa")
                                                       if r["session_id"] != "a2"]  # a2: only its questions remain
    res = sync(_tables({"a1": 2}), ANA)
    assert "a2" in res["stale"] and ch.sessions("questions", "aaaa") == {"a1"}


def test_ids_travel_in_the_body_not_the_url(monkeypatch):
    ch = FakeClickHouse()
    sent = []
    real_run = ch.run

    def run(sql, data=None, params=None, settings=None, compress=False):
        sent.append(params or {})
        return real_run(sql, data, params, settings, compress)
    ch.run = run
    monkeypatch.setattr(clickhouse, "Client", ch)
    sync(_tables({f"s{i:04d}": 1 for i in range(300)}), ANA)
    sync(_tables({}, skill=None), ANA, read={f"s{i:04d}" for i in range(300)})  # all 300 taken out
    assert not any("ids" in p for p in sent) and max(len(str(p)) for p in sent) < 200


def test_a_changed_scope_waits_for_rescope(monkeypatch):
    ch = FakeClickHouse()
    monkeypatch.setattr(clickhouse, "Client", ch)
    sync(_tables({"a1": 1, "a2": 1}), ANA)
    typo = clickhouse.sync(_tables({}), Target.from_url(URL), ANA, set(), ("rdee",), log=None)
    assert typo["scope_pending"] == {"from": "rde", "to": "rdee", "would_take_out": 2} and typo["removed"] == []
    assert ch.sessions("sessions", "aaaa") == {"a1", "a2"}  # nothing lost to a typo
    done = clickhouse.sync(_tables({}), Target.from_url(URL), ANA, set(), ("dataviz",), rescope=True, log=None)
    assert sorted(done["removed"]) == ["a1", "a2"] and "aaaa" not in ch.held("sessions")


def test_an_older_version_leaves_what_a_newer_one_shared(monkeypatch):
    ch = FakeClickHouse()
    monkeypatch.setattr(clickhouse, "Client", ch)
    sync(_tables({"a1": 1}), ANA)
    newer = f"{clickhouse.MARK} 99.0.0 #0123456789ab · written by a newer version"
    ch.tables["de_layers"] = FakeClickHouse.table(newer, {"id", "label", "description", "sort_order"},
                                                  {"": [TESTS_LAYER]})
    ch.views["v_interview_questions"] = newer  # with a column this version's lacks
    before = len(ch.sql)
    res = sync(_tables({"b1": 3}), BO)  # this version: no tests layer, the older view
    assert ch.held("questions") == {"aaaa": 1, "bbbb": 3}  # its own rows load as ever
    assert res["newer"] == {"de_layers": "99.0.0", "v_interview_questions": "99.0.0"}
    assert ch.rows_of("de_layers") == [TESTS_LAYER] and ch.views["v_interview_questions"] == newer
    assert _shared_writes(ch, before) == []  # everything else it shares was this version's already
    clickhouse.load(_tables({"b1": 3}), Target.from_url(URL), BO, log=None)  # nor does a full rebuild replace them
    assert ch.rows_of("de_layers") == [TESTS_LAYER] and ch.views["v_interview_questions"] == newer


def test_a_newer_version_puts_back_what_an_older_one_shared(monkeypatch):
    ch = FakeClickHouse()
    monkeypatch.setattr(clickhouse, "Client", ch)
    current = _tables({"a1": 1})
    current["de_layers"] = [TESTS_LAYER]
    sync(current, ANA)
    # as an older checkout left them: its taxonomy without the tests layer, its views unversioned
    ch.tables["de_layers"] = FakeClickHouse.table(f"{clickhouse.MARK} 0.1.0 · old", {"id", "label"}, {})
    ch.views = {n: f"{clickhouse.MARK} · {d}" for n, d in warehouse.VIEW_COMMENTS.items()}
    res = sync(current, ANA)
    assert res["newer"] == {} and ch.rows_of("de_layers") == [TESTS_LAYER]
    assert ch.tables["de_layers"]["comment"].startswith(STAMP)
    assert all(c.startswith(STAMP) for c in ch.views.values())


def test_the_hook_log_says_this_machine_should_update(tmp_path, monkeypatch, capsys):
    from session_analytics import cli
    ch = FakeClickHouse()
    transcript, queue, args = _hook_setup(tmp_path, monkeypatch, ch)
    assert cli.main(args + ["--skills", "demo"]) == 0
    ch.views["v_daily"] = f"{clickhouse.MARK} 99.0.0 #0123456789ab · written by a newer version"
    (queue / transcript.stem).write_text(f"{transcript}\n")  # the session ends again
    capsys.readouterr()
    assert cli.main(args + ["--skills", "demo"]) == 0
    out = capsys.readouterr().out
    assert f"as convo-analysis 99.0.0 wrote them, newer than this machine's {clickhouse.__version__}" in out
    assert "update convo-analysis here" in out


# What every source shares, by the version that writes it: two checkouts on one version must write the same views and
# taxonomy, or each load takes away the other's (only a newer version's are left alone). A change to a view, its
# comment or semantics/questions.json bumps __version__ and adds its fingerprint here; a released one never changes.
SHARED = {"0.4.0": "841661404a96"}


def test_a_change_to_what_every_source_shares_bumps_the_version():
    topics, layers = semantics.load().dimensions()
    digests = clickhouse.shared_digests({"de_topics": topics, "de_layers": layers})
    got = hashlib.sha256(json.dumps(digests, sort_keys=True).encode()).hexdigest()[:12]
    assert SHARED.get(clickhouse.__version__) == got, \
        f"the views or the taxonomy changed: bump __version__ and add its fingerprint to SHARED ({got})"


def test_forget_a_session_whose_transcript_is_gone_and_share_it_again(tmp_path, monkeypatch):
    from session_analytics import cli
    ch = FakeClickHouse()
    transcript, queue, args = _hook_setup(tmp_path, monkeypatch, ch)
    assert cli.main(args + ["--skills", "demo"]) == 0
    source = clickhouse.source_id(tmp_path / "claude")
    env, claude = args[args.index("--env-file") + 1], args[args.index("--claude-dir") + 1]
    kept = transcript.read_text()
    transcript.unlink()  # Claude Code pruned it; the shared rows stay by design
    assert cli.main(["warehouse", "--clickhouse-forget", "--session", transcript.stem, "--env-file", env,
                     "--claude-dir", claude]) == 0
    assert ch.sessions("sessions", source) == set()
    transcript.write_text(kept)
    (queue / transcript.stem).write_text(f"{transcript}\n")
    assert cli.main(args + ["--skills", "demo"]) == 0  # it ends again: withdrawn, so it stays out
    assert ch.sessions("sessions", source) == set()
    assert cli.main(["warehouse", "--clickhouse", "--session", str(transcript), "--skills", "demo", "--env-file", env,
                     "--claude-dir", claude, "--out", str(tmp_path / "out2")]) == 0  # named: shared again
    assert ch.sessions("sessions", source) == {transcript.stem}
