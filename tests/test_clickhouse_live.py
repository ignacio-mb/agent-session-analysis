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


def test_the_datasets_are_set_in_place_on_a_real_clickhouse(target):
    ana = _tables({"a1": 2, "a2": 1})
    ana["sessions"][0]["title"] = "Stripe star schema"
    ana["skill_runs"] = [{"run_id": "a1:1", "session_id": "a1", "skill": "rde", "cost_usd": 1.25,
                          "prompt": "it's raw_contrast's turn \\ now", "start_at": "2026-09-16T17:47:12.345Z"}]
    sync(target, ana, ANA)
    sync(target, _tables({"b1": 1}), BO)
    c, db = clickhouse.Client(target), target.database
    for name in clickhouse.DATASET_KEYS:  # as a version before the columns left the tables
        c.run(f"ALTER TABLE `{db}`.`{name}` DROP COLUMN dataset, DROP COLUMN dataset_by")

    def sums(name, cols):
        return c.rows(f"SELECT source, count() AS n, toString(sum(cityHash64(toString(tuple({cols}))))) AS h "
                      f"FROM `{db}`.`{name}` GROUP BY source ORDER BY source")
    others = {n: ", ".join(f"`{x}`" for x, _, _ in clickhouse.columns(n) if x not in ("dataset", "dataset_by"))
              for n in clickhouse.DATASET_KEYS}
    before = {n: sums(n, cols) for n, cols in others.items()}
    assert [r["changed"] for r in clickhouse.fill_datasets(target, dry_run=True, log=None)] == \
        [{"sessions": 1, "skill_runs": 1}, {"sessions": 0, "skill_runs": 0}]
    assert "dataset" not in {r["name"] for r in c.rows(f"SELECT name FROM system.columns WHERE database = '{db}' "
                                                        "AND table = 'sessions'")}  # the dry run added nothing
    clickhouse.fill_datasets(target, log=None)
    assert {n: sums(n, cols) for n, cols in others.items()} == before  # every other column as it was
    got = {r["session_id"]: (r["dataset"], r["dataset_by"]) for r in c.rows(
        f"SELECT session_id, dataset, dataset_by FROM `{db}`.`sessions`")}
    assert got == {"a1": ("Contrast", "runs"), "a2": (None, None), "b1": (None, None)}
    (run,) = c.rows(f"SELECT dataset, dataset_by, prompt FROM `{db}`.`skill_runs`")
    assert (run["dataset"], run["dataset_by"], run["prompt"]) == ("Contrast", "prompt", "it's raw_contrast's turn \\ now")
    assert not [r for r in c.rows(f"SELECT name FROM system.tables WHERE database = '{db}'") if "__datasets" in r["name"]]
