"""The ClickHouse sync against a real server — opt-in: CONVO_CLICKHOUSE_TEST_URL=http://user:password@host:8123, a
server where a scratch database may be created and dropped (`make clickhouse-dev-test` runs it on the throwaway
local one). What FakeClickHouse cannot show: REPLACE PARTITION, has() on the session id, INSERT … SELECT * lining up
with the staging table after a column was added, and the counts ClickHouse itself reports."""

import os
import uuid

import pytest
from test_clickhouse import ANA, BO, RDE, _merge, _tables

from session_analytics import clickhouse

URL = os.environ.get("CONVO_CLICKHOUSE_TEST_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set CONVO_CLICKHOUSE_TEST_URL to run against a real ClickHouse")


@pytest.fixture
def target():
    admin = clickhouse.Client(clickhouse.Target.from_url(URL.rstrip("/") + "/default"))
    db = f"convo_test_{uuid.uuid4().hex[:10]}"
    admin.run(f"CREATE DATABASE {db}")
    try:
        yield clickhouse.Target.from_url(URL.rstrip("/") + f"/{db}")
    finally:
        admin.run(f"DROP DATABASE IF EXISTS {db} SYNC")


def held(target, table, source):
    c = clickhouse.Client(target)
    return {r["session_id"]: int(r["n"]) for r in c.rows(
        f"SELECT session_id, count() AS n FROM `{target.database}`.`{table}` WHERE source = {{s:String}} "
        f"GROUP BY session_id", {"s": source})}


def sync(target, tables, ident_, read=None, full=False):
    read = {r["session_id"] for r in tables["sessions"]} if read is None else read
    return clickhouse.sync(tables, target, ident_, read, RDE, full=full, log=None)


def test_sync_on_a_real_clickhouse(target):
    sync(target, _tables({"a1": 2, "a2": 1}), ANA, full=True)
    sync(target, _tables({"b1": 3}), BO, full=True)
    assert held(target, "questions", "aaaa") == {"a1": 2, "a2": 1} and held(target, "questions", "bbbb") == {"b1": 3}
    res = sync(target, _tables({"a2": 4}), ANA)  # one session ended again
    assert res["written"] == ["a2"] and held(target, "questions", "aaaa") == {"a1": 2, "a2": 4}
    assert held(target, "questions", "bbbb") == {"b1": 3}
    res = sync(target, _tables({"a1": 1}, skill=None), ANA)  # read again, no longer runs rde
    assert res["removed"] == ["a1"] and set(held(target, "sessions", "aaaa")) == {"a2"}
    sync(target, _tables({"a2": 4}), ANA, full=True)  # a full sync that does not read a1: nothing else changes
    assert set(held(target, "sessions", "aaaa")) == {"a2"} and set(held(target, "sessions", "bbbb")) == {"b1"}
    assert clickhouse.forget(target, BO) == (["b1"], []) and held(target, "sessions", "bbbb") == {}
    c = clickhouse.Client(target)
    assert not [r for r in c.rows(f"SELECT name FROM system.tables WHERE database = '{target.database}'")
                if "__load" in r["name"]]


def test_what_an_older_version_shared_is_taken_out_on_a_real_clickhouse(target):
    everything = _merge(_tables({"a1": 1}), _tables({"x1": 2}, skill="dataviz"), _tables({"x2": 1}, skill=None))
    clickhouse.load(everything, target, ANA, log=None)  # as before rde-only sharing: every session, no scope
    res = sync(target, _tables({"a1": 1}), ANA)
    assert sorted(res["stale"]) == ["x1", "x2"] and set(held(target, "questions", "aaaa")) == {"a1"}


def test_a_column_added_by_a_newer_version_on_a_real_clickhouse(target):
    sync(target, _tables({"a1": 2}), ANA)
    clickhouse.Client(target).run(f"ALTER TABLE `{target.database}`.`questions` DROP COLUMN reask_of")
    sync(target, _tables({"a2": 1}), ANA)  # the column comes back; the copy of a1's rows still lines up
    assert held(target, "questions", "aaaa") == {"a1": 2, "a2": 1}
