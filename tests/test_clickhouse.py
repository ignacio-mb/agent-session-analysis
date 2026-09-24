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
    ddl = clickhouse.table_ddl("sessions", "questions", as_name="questions__load")
    assert ddl.startswith("CREATE TABLE `sessions`.`questions__load` (")
    assert "`qid` String" in ddl and "`asked_at` Nullable(DateTime64(3, 'UTC'))" in ddl
    assert "`multi` Nullable(Bool)" in ddl and "ORDER BY (`qid`)" in ddl
    assert f"COMMENT '{clickhouse.MARK} · " in ddl and "COMMENT 'ask | prose | checkpoint'" in ddl
    assert clickhouse.lit("it's a \\ path") == "'it\\'s a \\\\ path'"
    for name, (_, _cols, pk) in warehouse.TABLES.items():
        assert f"ORDER BY ({', '.join(f'`{c}`' for c in pk)})" in clickhouse.table_ddl("db", name)


def test_rows_match_what_postgres_would_hold():
    jv = clickhouse.json_value
    assert jv("", warehouse.TEXT) is None and jv(None, warehouse.INT) is None  # empty text is NULL, as in COPY csv
    assert jv(3.0, warehouse.INT) == 3 and jv(True, warehouse.BIG) == 1 and jv(float("nan"), warehouse.NUM) is None
    assert jv(1.5, warehouse.NUM) == 1.5 and jv(0, warehouse.BOOL) is False and jv(3.0, warehouse.TEXT) == "3"
    assert jv(0, warehouse.TS) == "1970-01-01T00:00:00.000Z" and jv(["a", "b"], warehouse.TEXT) == '["a", "b"]'


def test_rows_are_sent_in_chunks(monkeypatch):
    monkeypatch.setattr(clickhouse, "CHUNK_BYTES", 200)
    rows = [{"id": f"t{i}", "label": "x" * 50, "description": None, "sort_order": i} for i in range(10)]
    chunks = list(clickhouse.json_lines("de_topics", rows))
    assert len(chunks) > 1 and sum(c.count(b"\n") + 1 for c in chunks) == 10
    assert b'"description":null' in chunks[0] and b'"sort_order":0' in chunks[0]


def test_views_are_the_postgres_views_qualified():
    assert list(clickhouse.VIEWS) == list(warehouse.VIEW_COMMENTS)
    for name, sql in clickhouse.VIEWS.items():
        tables = re.findall(r"\b(?:FROM|(?<!ARRAY )JOIN)\s+([\w.{}]+)", sql)
        assert tables and all(t.startswith("{db}.") or t in ("v", "asked") for t in tables), (name, tables)
        assert "::" not in sql and "percentile_cont" not in sql and "LATERAL" not in sql, name
        ddl = clickhouse.view_ddl("sessions", name)
        assert ddl.startswith(f"CREATE OR REPLACE VIEW `sessions`.`{name}` AS") and clickhouse.MARK in ddl


class FakeClickHouse:
    """Records statements; answers the few SELECTs the load makes."""

    def __init__(self, tables=(), databases=("sessions",), short=None):
        self.tables = dict(tables)  # name -> comment
        self.databases, self.short, self.sql = databases, short, []
        self.inserted = {}

    def __call__(self, target, timeout=300):
        self.t = target
        return self

    def rows(self, sql, params=None):
        if "system.databases" in sql:
            return [{"name": params["db"]}] if params["db"] in self.databases else []
        if "system.tables" in sql:
            return [{"name": n, "engine": "MergeTree", "comment": c} for n, c in self.tables.items()]
        m = re.search(r"count\(\) AS n FROM `sessions`\.`(\w+)__load`", sql)
        n = self.inserted.get(m.group(1), 0)
        return [{"n": n - 1 if m.group(1) == self.short else n}]

    def run(self, sql, data=None, params=None, settings=None, compress=False):
        self.sql.append(sql.split("\n")[0])
        m = re.match(r"INSERT INTO `sessions`\.`(\w+)__load`", sql)
        if m:
            self.inserted[m.group(1)] = self.inserted.get(m.group(1), 0) + data.count(b"\n") + 1
        return ""


def _tables():
    t = {k: [] for k in warehouse.TABLES}
    t["de_topics"] = [{"id": "privacy", "label": "Privacy", "description": None, "sort_order": 1}]
    t["warehouse_load"] = [{"loaded_at": "2026-09-24T12:00:00.000Z", "transcripts": 1, "sessions": 0}]
    return t


def test_load_swaps_each_table_in_then_creates_the_views(monkeypatch):
    fake = FakeClickHouse(tables={"de_topics": f"{clickhouse.MARK} · old", "unrelated": ""})
    monkeypatch.setattr(clickhouse, "Client", fake)
    counts = clickhouse.load(_tables(), Target.from_url("https://u:p@h:8443/sessions"), log=None)
    assert counts["de_topics"] == 1 and counts["warehouse_load"] == 1 and counts["questions"] == 0
    s = fake.sql
    assert s[:2] == ["DROP TABLE IF EXISTS `sessions`.`sessions__load` SYNC", "CREATE TABLE `sessions`.`sessions__load` ("]
    assert "EXCHANGE TABLES `sessions`.`de_topics__load` AND `sessions`.`de_topics`" in s  # it existed: swapped
    assert "RENAME TABLE `sessions`.`questions__load` TO `sessions`.`questions`" in s  # new: renamed in
    assert not any("unrelated" in x for x in s)  # never touches what is not its own
    first_view = next(i for i, x in enumerate(s) if x.startswith("CREATE OR REPLACE VIEW"))
    assert all(not x.startswith(("CREATE TABLE", "EXCHANGE", "RENAME")) for x in s[first_view:])
    assert sum(x.startswith("CREATE OR REPLACE VIEW") for x in s) == len(clickhouse.VIEWS)


def test_load_refuses_what_it_did_not_create_and_a_short_insert(monkeypatch):
    t = Target.from_url("https://u:p@h:8443/sessions")
    monkeypatch.setattr(clickhouse, "Client", FakeClickHouse(tables={"sessions": "someone else's"}))
    with pytest.raises(ClickHouseError, match="not created by convo-analysis"):
        clickhouse.load(_tables(), t, log=None)
    monkeypatch.setattr(clickhouse, "Client", FakeClickHouse(databases=()))
    with pytest.raises(ClickHouseError, match="does not exist"):
        clickhouse.load(_tables(), t, log=None)
    fake = FakeClickHouse(tables={"de_topics": f"{clickhouse.MARK} · old"}, short="de_topics")
    monkeypatch.setattr(clickhouse, "Client", fake)
    with pytest.raises(ClickHouseError, match="1 sent; the live table is untouched"):
        clickhouse.load(_tables(), t, log=None)
    assert not any(x.startswith("EXCHANGE TABLES `sessions`.`de_topics__load`") for x in fake.sql)
