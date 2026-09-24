"""The ClickHouse load, offline: the connection string, the DDL, the rows, and the load's order of operations against
a fake server (a live one: `make clickhouse-dev`, then scripts/metabase_dashboard.py --test --clickhouse)."""

import re

import pytest

from session_analytics import clickhouse, warehouse
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
    assert f"COMMENT '{clickhouse.MARK} · " in ddl and "COMMENT 'ask | prose | checkpoint'" in ddl
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
    """A tiny ClickHouse: tables with columns and per-source partitions, and a log of the statements run."""

    def __init__(self, tables=None, databases=("sessions",), short=None):
        # name -> {"comment", "columns", "parts": {source: rows}}
        self.tables = tables or {}
        self.databases, self.short, self.sql = databases, short, []

    def __call__(self, target, timeout=300):
        self.t = target
        return self

    @staticmethod
    def table(comment, columns, parts=None):
        return {"comment": comment, "columns": set(columns), "parts": dict(parts or {})}

    def rows(self, sql, params=None):
        if "system.databases" in sql:
            return [{"name": params["db"]}] if params["db"] in self.databases else []
        if "system.tables" in sql:
            return [{"name": n, "engine": "MergeTree", "comment": t["comment"]} for n, t in self.tables.items()]
        if "system.columns" in sql:
            return [{"table": n, "name": c} for n, t in self.tables.items() for c in t["columns"]]
        m = re.search(r"count\(\) AS n FROM `sessions`\.`(\w+)`", sql)
        n = sum(self.tables[m.group(1)]["parts"].values())
        return [{"n": n - 1 if m.group(1).startswith(f"{self.short}__load") else n}]

    def run(self, sql, data=None, params=None, settings=None, compress=False):
        self.sql.append(sql.split("\n")[0])
        name = lambda x: x.split("`.`")[1].rstrip("`")  # noqa: E731
        if m := re.match(r"CREATE TABLE (?:IF NOT EXISTS )?(`\S+`)( AS (`\S+`))?", sql):
            n = name(m.group(1))
            if n not in self.tables:
                cols = self.tables[name(m.group(3))]["columns"] if m.group(2) else set(re.findall(r"^  `(\w+)`", sql, re.M))
                self.tables[n] = self.table(clickhouse.MARK, cols)
        elif m := re.match(r"INSERT INTO (`\S+`)", sql):
            rows = data.count(b"\n") + 1
            src = re.search(rb'"source":"(\w+)"', data)
            parts = self.tables[name(m.group(1))]["parts"]
            key = src.group(1).decode() if src else ""
            parts[key] = parts.get(key, 0) + rows
        elif m := re.match(r"ALTER TABLE (`\S+`) REPLACE PARTITION '(\w+)' FROM (`\S+`)", sql):
            self.tables[name(m.group(1))]["parts"][m.group(2)] = self.tables[name(m.group(3))]["parts"][m.group(2)]
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


def _tables(questions=2):
    t = {k: [] for k in warehouse.TABLES}
    t["de_topics"] = [{"id": "privacy", "label": "Privacy", "description": None, "sort_order": 1}]
    t["questions"] = [{"qid": f"q{i}", "session_id": "s"} for i in range(questions)]
    t["warehouse_load"] = [{"loaded_at": "2026-09-24T12:00:00.000Z", "transcripts": 1, "sessions": 0}]
    return t


ANA = clickhouse.Identity("aaaa", "ana@example.com", "ana-laptop")
BO = clickhouse.Identity("bbbb", "bo@example.com", "bo-desktop")
URL = "https://u:p@h:8443/sessions"


def test_each_load_replaces_its_own_rows_and_nobody_elses(monkeypatch):
    ch = FakeClickHouse()
    monkeypatch.setattr(clickhouse, "Client", ch)
    clickhouse.load(_tables(2), Target.from_url(URL), ANA, log=None)
    clickhouse.load(_tables(3), Target.from_url(URL), BO, log=None)
    assert ch.tables["questions"]["parts"] == {"aaaa": 2, "bbbb": 3}
    clickhouse.load(_tables(5), Target.from_url(URL), ANA, log=None)  # Ana again: her rows replaced, Bo's kept
    assert ch.tables["questions"]["parts"] == {"aaaa": 5, "bbbb": 3}
    assert "ALTER TABLE `sessions`.`questions` REPLACE PARTITION 'aaaa' FROM `sessions`.`questions__load_aaaa`" in ch.sql
    assert ch.tables["de_topics"]["parts"] == {"": 1}  # the taxonomy: the same for everyone, swapped whole
    clickhouse.load(_tables(0), Target.from_url(URL), BO, log=None)  # no questions left: drop, not REPLACE
    assert ch.tables["questions"]["parts"] == {"aaaa": 5}
    assert not any("__load" in n for n in ch.tables)  # staging never outlives a load
    first_view = next(i for i, x in enumerate(ch.sql) if x.startswith("CREATE OR REPLACE VIEW"))
    assert sum(x.startswith("CREATE OR REPLACE VIEW") for x in ch.sql[first_view:]) >= len(clickhouse.VIEWS)
    assert clickhouse.forget(Target.from_url(URL), ANA) and ch.tables["questions"]["parts"] == {}


def test_a_table_from_before_per_source_loads_is_rebuilt(monkeypatch):
    old = {"qid", "session_id"}  # no `source`: it held only the last loader's rows
    ch = FakeClickHouse(tables={"questions": FakeClickHouse.table(f"{clickhouse.MARK} · old", old, {"": 7}),
                                "unrelated": FakeClickHouse.table("", {"x"})})
    monkeypatch.setattr(clickhouse, "Client", ch)
    clickhouse.load(_tables(2), Target.from_url(URL), ANA, log=None)
    assert ch.tables["questions"]["parts"] == {"aaaa": 2} and "source" in ch.tables["questions"]["columns"]
    assert "EXCHANGE TABLES `sessions`.`questions__rebuild` AND `sessions`.`questions`" in ch.sql
    assert "questions__rebuild" not in ch.tables and "unrelated" in ch.tables
    assert not any("unrelated" in x for x in ch.sql)  # never touches what is not its own
    ch.tables["questions"]["columns"].discard("reask_of")  # a newer version's column: added, nothing dropped
    clickhouse.load(_tables(2), Target.from_url(URL), ANA, log=None)
    assert "ALTER TABLE `sessions`.`questions` ADD COLUMN IF NOT EXISTS `reask_of` Nullable(String)" in ch.sql


def test_load_refuses_what_it_did_not_create_and_a_short_insert(monkeypatch):
    t = Target.from_url(URL)
    monkeypatch.setattr(clickhouse, "Client", FakeClickHouse(tables={"sessions": FakeClickHouse.table("x", {"a"})}))
    with pytest.raises(ClickHouseError, match="not created by convo-analysis"):
        clickhouse.load(_tables(), t, ANA, log=None)
    monkeypatch.setattr(clickhouse, "Client", FakeClickHouse(databases=()))
    with pytest.raises(ClickHouseError, match="does not exist"):
        clickhouse.load(_tables(), t, ANA, log=None)
    ch = FakeClickHouse(short="questions")
    monkeypatch.setattr(clickhouse, "Client", ch)
    with pytest.raises(ClickHouseError, match="2 sent; the live table is untouched"):
        clickhouse.load(_tables(2), t, ANA, log=None)
    assert not any("REPLACE PARTITION 'aaaa' FROM `sessions`.`questions__load" in x for x in ch.sql)
    assert "questions__load_aaaa" not in ch.tables


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
