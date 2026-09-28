"""The instance snapshot, against a fake `mb`: which profile a run used, which objects are the run's, the checks on
them, the source tables' shape, and the file the loader reads."""

import json

from session_analytics import instance, util

START = 1_790_000_000_000
END = START + 30 * 60_000


def at(minutes):
    return util.iso(START + minutes * 60_000)


def page(items):
    return {"returned": len(items), "offset": 0, "total": len(items), "has_more": False, "next_offset": None,
            "data": items}


class FakeMb:
    """An instance: what each `mb` command prints. `down` makes every call fail; `missing` names nouns it lacks."""

    def __init__(self, down=False, missing=(), questions=40):
        self.down, self.missing, self.questions, self.calls = down, set(missing), questions, []
        native = {"lib/type": "mbql/query", "database": 2,
                  "stages": [{"lib/type": "mbql.stage/native", "native": "SELECT count(*) FROM orders"}]}
        listing = {"lib/type": "mbql/query", "database": 2,
                   "stages": [{"lib/type": "mbql.stage/mbql", "source-table": 20, "limit": 50}]}
        by_metric = {"lib/type": "mbql/query", "database": 2,
                     "stages": [{"lib/type": "mbql.stage/mbql", "source-table": 20,
                                 "aggregation": [["metric", {}, 31]]}]}
        self.lists = {
            "card": [
                {"id": 1, "name": "Revenue by month", "type": "question", "display": "line", "database_id": 2,
                 "dataset_query": by_metric, "description": "Recognised revenue", "created_at": at(5),
                 "updated_at": at(6)},
                {"id": 2, "name": "_probe orders", "type": "question", "display": "table", "database_id": 2,
                 "dataset_query": native, "created_at": at(7), "updated_at": at(7)},
                {"id": 3, "name": "Old question", "type": "question", "database_id": 2, "dataset_query": native,
                 "created_at": at(-600), "updated_at": at(-500)},
                {"id": 6, "name": "Billing page test: orders listed", "type": "question", "database_id": 2,
                 "dataset_query": listing, "description": "The A/B test's orders", "created_at": at(9),
                 "updated_at": at(9)},
                {"id": 4, "name": "Edited earlier question", "type": "question", "database_id": 2,
                 "dataset_query": native, "created_at": at(-600), "updated_at": at(20)},
                {"id": 5, "name": "Archived", "type": "question", "archived": True, "created_at": at(8),
                 "updated_at": at(8)},
                {"id": 31, "name": "Revenue", "type": "metric", "database_id": 2, "dataset_query": by_metric,
                 "description": "Sum of paid invoices", "created_at": at(4), "updated_at": at(4)}],
            "transform": [
                {"id": 11, "name": "mart_orders", "description": "One row per order",
                 "source": {"type": "query", "query": native},
                 "target": {"type": "table", "database": 2, "schema": "analytics", "name": "mart_orders"},
                 "last_run": {"status": "succeeded"}, "table_dependencies": [{"table": 20}, {"transform": 12}],
                 "created_at": at(2), "updated_at": at(3)},
                {"id": 12, "name": "stg_orders", "source": {"type": "query", "query": native},
                 "target": {"type": "table", "database": 2, "schema": "analytics", "name": "stg_orders"},
                 "last_run": {"status": "failed"}, "created_at": at(1), "updated_at": at(1)}],
            "transform-test": [{"id": 41, "name": "mart_orders cases", "transform_id": 11, "created_at": at(3),
                                "updated_at": at(3)}],
            "dashboard": [{"id": 51, "name": "CEO", "description": "Monthly review", "created_at": at(10),
                           "updated_at": at(12)}],
            "measure": [],
            "segment": [{"id": 61, "name": "Paying", "definition": {"filters": []}, "table_id": 20,
                         "created_at": at(9), "updated_at": at(9)}],
            "document": [],
        }
        self.dashboards = {51: {"id": 51, "tabs": [{"id": 1}], "parameters": [{"id": "p"}], "dashcards": [
            {"card_id": 1, "parameter_mappings": [{"parameter_id": "p"}]},
            {"card_id": 2, "parameter_mappings": []},
            {"card_id": None, "visualization_settings": {"virtual_card": {"display": "text"}, "text": "as of"}}]}}
        self.queries = {1: {"status": "completed", "row_count": 12}, 2: {"status": "completed", "row_count": 0},
                        6: {"status": "completed", "row_count": 0},
                        4: {"status": "failed", "error": "Column not found"}, 31: {"status": "completed",
                                                                                     "row_count": 1}}
        self.tables = [
            {"id": 20, "db_id": 2, "schema": "public", "name": "orders", "active": True, "created_at": at(-900),
             "estimated_row_count": 5000},
            {"id": 21, "db_id": 2, "schema": "public", "name": "customers", "active": True, "created_at": at(-900)},
            {"id": 22, "db_id": 2, "schema": "analytics", "name": "mart_orders", "active": True, "transform_id": 11,
             "created_at": at(4)},
            {"id": 23, "db_id": 2, "schema": "analytics", "name": "_rde_smoke", "active": True, "created_at": at(1)},
            {"id": 24, "db_id": 2, "schema": "public", "name": "gone", "active": False, "created_at": at(-900)}]
        self.fields = {
            20: [{"base_type": "type/Integer", "semantic_type": "type/PK", "fingerprint": {"global": {"nil%": 0}}},
                 {"base_type": "type/Integer", "semantic_type": "type/FK", "fk_target_field_id": 9,
                  "fingerprint": {"global": {"nil%": 0.1}}},
                 {"base_type": "type/Text", "coercion_strategy": "Coercion/ISO8601->DateTime",
                  "fingerprint": {"global": {"nil%": 0.6}, "type": {"type/Text": {"percent-json": 0}}}},
                 {"base_type": "type/Text", "fingerprint": {"global": {"nil%": 1.0},
                                                            "type": {"type/Text": {"percent-json": 0.9}}}},
                 {"base_type": "type/DateTimeWithLocalTZ", "fingerprint": {"global": {"nil%": 0}}},
                 {"base_type": "type/JSON", "fingerprint": None}],
            21: [{"base_type": "type/BigInteger", "semantic_type": "type/PK"},
                 {"base_type": "type/Boolean"}]}

    def __call__(self, args):
        self.calls.append(args)
        if self.down:
            raise instance.MbError("mb search: fetch failed: connect ECONNREFUSED 127.0.0.1:3200")
        noun, verb = args[0], args[1]
        if noun == "search":
            return json.dumps({"total": self.questions, "returned": 1, "data": []})
        if noun == "auth":
            return json.dumps(page([{"profile": "shop", "url": "http://localhost:3200"},
                                    {"profile": "stats", "url": "https://stats.example.com"}]))
        if noun in self.missing:
            raise instance.MbError(f"mb {noun} list: 404 Not Found")
        if verb == "list" and noun == "table":
            db = int(args[args.index("--db-id") + 1])
            return json.dumps(page([t for t in self.tables if t["db_id"] == db]))
        if verb == "list":
            return json.dumps(page(self.lists[noun]))
        if noun == "dashboard" and verb == "get":
            return json.dumps(self.dashboards[int(args[2])])
        if noun == "card" and verb == "query":
            return json.dumps(self.queries[int(args[2])])
        if noun == "table" and verb == "get":
            return json.dumps({"id": int(args[2]), "fields": self.fields.get(int(args[2]), [])})
        if noun == "query":
            return json.dumps({"status": "completed", "data": {"rows": [[77]]}})
        raise AssertionError(args)


RUN = {"run_id": "abcd1234:1", "session_id": "abcd1234-0000", "start_ms": START, "end_ms": END,
       "prompt": "build a dashboard on localhost:3200"}


def capture(fake):
    return instance.capture(RUN, "shop", "http://localhost:3200/", instance.Mb("shop", fake), END + 5_000)


def test_the_profile_is_the_one_the_commands_name_else_the_prompts_instance():
    known = {"shop": "http://localhost:3200", "toy-store2": "http://toy-store2.localhost:3202", "stats": "https://s.io"}
    commands = ["export PROFILE=toy-store2; mb card list --profile $PROFILE", "mb query -p toy-store2 --file q.json",
                "mb auth list --profile stats", "ls --profile nope"]
    assert instance.profiles_of(commands, "", known) == ["toy-store2", "stats"]
    assert instance.profiles_of(["mb --version"], "I have data in http://localhost:3200/browse", known) == ["shop"]
    assert instance.profiles_of(["mb --version"], "no instance named", known) == []


def test_the_runs_objects_are_those_created_or_changed_in_its_window():
    snap = capture(FakeMb())
    assert (snap["reachable"], snap["error"], snap["skipped"], snap["host"]) == (True, None, {}, "localhost:3200")
    got = {(a["kind"], a["id"]): a["in_run"] for a in snap["artifacts"]}
    assert got == {("question", 1): "created", ("question", 2): "created", ("question", 4): "changed",
                   ("question", 6): "created",
                   ("metric", 31): "created", ("transform", 11): "created", ("transform", 12): "created",
                   ("transform_test", 41): "created", ("dashboard", 51): "created", ("segment", 61): "created"}


def test_what_the_snapshot_says_about_each_kind():
    arts = {a["id"]: a for a in capture(FakeMb())["artifacts"]}
    assert (arts[1]["query_kind"], arts[1]["uses_metric"], arts[1]["run"]) == (
        "mbql", True, {"status": "completed", "row_count": 12, "error": None})
    assert (arts[2]["query_kind"], arts[2]["definition"], arts[2]["uses_metric"]) == (
        "native", "SELECT count(*) FROM orders", False)
    assert arts[4]["run"] == {"status": "failed", "row_count": None, "error": "Column not found"}
    assert {k: arts[11][k] for k in ("target_table", "last_run_status", "depends_on_tables", "database_id")} == {
        "target_table": "analytics.mart_orders", "last_run_status": "succeeded", "depends_on_tables": [20],
        "database_id": 2}
    assert {k: arts[51][k] for k in ("tabs", "dashboard_filters", "dashcards", "card_dashcards", "text_dashcards",
                                     "unmapped_dashcards", "card_ids")} == {
        "tabs": 1, "dashboard_filters": 1, "dashcards": 3, "card_dashcards": 2, "text_dashcards": 1,
        "unmapped_dashcards": 1, "card_ids": [1, 2]}


def test_source_tables_are_the_ones_no_transform_writes_and_the_run_did_not_leave():
    tables = {t["name"]: t for t in capture(FakeMb())["source_tables"]}
    assert sorted(tables) == ["customers", "orders"]
    assert tables["orders"] == {
        "table_id": 20, "db_id": 2, "schema": "public", "name": "orders", "rows": 5000, "columns": 6, "pk_columns": 1,
        "fk_columns": 1, "numeric_columns": 2, "temporal_columns": 1, "text_columns": 2, "boolean_columns": 0,
        "json_columns": 1, "text_json_columns": 1, "coerced_columns": 1, "empty_columns": 1,
        "mostly_empty_columns": 2, "max_null_share": 1.0}
    assert (tables["customers"]["rows"], tables["customers"]["max_null_share"]) == (77, None)


def test_an_instance_that_does_not_answer_is_recorded_as_such():
    snap = capture(FakeMb(down=True))
    assert (snap["reachable"], snap["artifacts"], snap["source_tables"]) == (False, [], [])
    assert "ECONNREFUSED" in snap["error"]


def test_only_a_local_instance_is_captured():
    fake = FakeMb()
    snap = instance.capture(RUN, "stats", "https://stats.example.com", instance.Mb("stats", fake), END)
    assert (snap["reachable"], snap["error"], fake.calls) == (
        False, "not captured: stats.example.com is not a local instance", [])
    assert [instance.is_local(u) for u in ("http://toy.localhost:3202", "http://127.0.0.1:3000",
                                            "http://host.docker.internal:3000", "https://localhost.example.com")] == [
        True, True, True, False]


def test_an_instance_with_too_many_questions_is_not_listed_whole():
    snap = capture(FakeMb(questions=instance.LIST_CAP + 1))
    assert snap["reachable"] is True and set(snap["skipped"]) == {"card", "dashboard"}
    assert {a["kind"] for a in snap["artifacts"]} == {"transform", "transform_test", "segment"}


def test_a_kind_the_instance_lacks_is_skipped_not_fatal():
    snap = capture(FakeMb(missing={"transform-test", "document"}))
    assert snap["reachable"] is True and set(snap["skipped"]) == {"transform_test", "document"}
    assert "404 Not Found" in snap["skipped"]["transform_test"]
    assert not any(a["kind"] == "transform_test" for a in snap["artifacts"])


def test_the_checks_on_each_object():
    got = {(a["id"], cid): (status, detail) for a, cid, status, detail in instance.check_rows(capture(FakeMb())["artifacts"])}
    assert got[(1, "runs")] == ("pass", None) and got[(1, "uses-a-metric")] == ("pass", None)
    assert got[(1, "on-a-dashboard")] == ("pass", None) and got[(1, "no-scratch-name")] == ("pass", None)
    assert got[(2, "returns-rows")] == ("fail", None) and got[(2, "no-scratch-name")] == ("fail", None)
    assert got[(2, "uses-a-metric")] == ("fail", "native") and got[(2, "described")] == ("fail", None)
    assert got[(4, "runs")] == ("fail", "Column not found") and (4, "returns-rows") not in got
    assert got[(4, "on-a-dashboard")] == ("fail", None)
    assert got[(11, "transform-ran")] == ("pass", None) and got[(11, "transform-tested")] == ("pass", None)
    assert got[(12, "transform-ran")] == ("fail", "failed") and got[(12, "transform-tested")] == ("fail", None)
    assert got[(51, "filters-reach-cards")] == ("fail", "1 of 2 cards unwired")
    assert got[(51, "has-a-note")] == ("pass", None) and got[(51, "described")] == ("pass", None)
    assert (61, "no-scratch-name") not in got and got[(61, "described")] == ("fail", None)
    # a business word is no scratch name; a list computes nothing; an empty question off the dashboards is no defect
    assert got[(6, "no-scratch-name")] == ("pass", None) and (6, "uses-a-metric") not in got
    assert (6, "returns-rows") not in got and got[(6, "on-a-dashboard")] == ("fail", None)


class Call:
    def __init__(self, command):
        self.name, self.input = "Bash", {"command": command}


class Session:
    session_id = RUN["session_id"]
    tool_calls = {"t1": Call("export PROFILE=shop; mb card list --profile $PROFILE"), "t2": Call("ls")}


ANALYSIS = {"skill_runs": [dict(RUN, steps=[0, 1])],
            "trace": {"steps": [{"k": "tool", "id": "t1"}, {"k": "tool", "id": "t2"}]}}


def test_a_capture_is_written_once_and_loads_as_rows(tmp_path):
    fake = FakeMb()
    (path,) = instance.capture_session(ANALYSIS, Session(), END + 5_000, root=tmp_path, runner=fake)
    assert path == instance.snapshot_path(RUN["session_id"], RUN["run_id"], tmp_path) and path.name == "abcd1234_1.json"
    assert instance.capture_session(ANALYSIS, Session(), END + 9_000, root=tmp_path, runner=fake) == []
    rows = instance.session_rows(RUN["session_id"], lambda t: t, tmp_path)
    (inst,) = rows["run_instances"]
    assert inst == {"session_id": RUN["session_id"], "run_id": RUN["run_id"], "host": "localhost:3200",
                    "profile": "shop", "captured_at": util.iso(END + 5_000), "reachable": True, "error": None,
                    "skipped": None, "objects_created": 9, "objects_changed": 1, "source_tables": 2}
    by_id = {r["object_id"]: r for r in rows["run_artifacts"]}
    assert (by_id[11]["transform_tests"], by_id[1]["on_dashboards"], by_id[2]["on_dashboards"]) == (1, 1, 1)
    assert (by_id[31]["on_dashboards"], by_id[4]["run_status"], by_id[4]["run_error"]) == (None, "failed",
                                                                                          "Column not found")
    assert {(r["table_name"], r["schema_name"], r["rows"]) for r in rows["run_source_tables"]} == {
        ("orders", "public", 5000), ("customers", "public", 77)}
    assert len(rows["run_artifact_checks"]) == len(instance.check_rows(json.loads(path.read_text())["artifacts"]))


def test_an_unreachable_capture_is_tried_again(tmp_path):
    down = FakeMb(down=True)
    down_auth = lambda args: FakeMb()(args) if args[0] == "auth" else down(args)  # noqa: E731
    (path,) = instance.capture_session(ANALYSIS, Session(), END, root=tmp_path, runner=down_auth)
    assert json.loads(path.read_text())["reachable"] is False
    assert instance.capture_session(ANALYSIS, Session(), END, root=tmp_path, runner=FakeMb()) == [path]
    assert json.loads(path.read_text())["reachable"] is True


def test_a_run_a_resumed_session_took_further_is_captured_again(tmp_path):
    (path,) = instance.capture_session(ANALYSIS, Session(), END, root=tmp_path, runner=FakeMb())
    longer = dict(ANALYSIS, skill_runs=[dict(RUN, steps=[0, 1], end_ms=END + 3_600_000)])
    assert instance.capture_session(longer, Session(), END + 3_700_000, root=tmp_path, runner=FakeMb()) == [path]
    assert json.loads(path.read_text())["window_end_ms"] == END + 3_600_000 + instance.WINDOW_SLACK_MS


def test_a_run_that_named_no_profile_is_not_captured(tmp_path):
    class Quiet(Session):
        tool_calls = {"t1": Call("ls"), "t2": Call("pwd")}
    quiet = dict(ANALYSIS, skill_runs=[dict(RUN, steps=[0, 1], prompt="no instance here")])
    assert instance.capture_session(quiet, Quiet(), END, root=tmp_path, runner=FakeMb()) == []
    assert instance.session_rows(RUN["session_id"], lambda t: t, tmp_path) == {
        "run_instances": [], "run_artifacts": [], "run_artifact_checks": [], "run_source_tables": []}


def test_an_object_two_overlapping_runs_saw_is_the_later_runs():
    early = {"run_id": "s:1", "host": "h", "window_start_ms": START, "artifacts": [
        {"kind": "question", "id": 1, "in_run": "created", "created_at": at(5)},
        {"kind": "question", "id": 2, "in_run": "created", "created_at": at(20)},
        {"kind": "question", "id": 3, "in_run": "changed", "updated_at": at(25)}]}
    late = {"run_id": "s:2", "host": "h", "window_start_ms": START + 15 * 60_000, "artifacts": [
        {"kind": "question", "id": 2, "in_run": "created", "created_at": at(20)},
        {"kind": "question", "id": 3, "in_run": "changed", "updated_at": at(25)}]}
    other = {"run_id": "s:3", "host": "elsewhere", "window_start_ms": START + 16 * 60_000, "artifacts": [
        {"kind": "question", "id": 1, "in_run": "created", "created_at": at(5)}]}
    got = {r: [a["id"] for a in arts] for r, arts in instance.owned([early, late, other]).items()}
    assert got == {"s:1": [1], "s:2": [2, 3], "s:3": [1]}


def test_only_runs_of_the_shared_skills_are_captured(tmp_path):
    from session_analytics import warehouse
    mixed = dict(ANALYSIS, skill_runs=[dict(RUN, steps=[0, 1], skill="rde"),
                                       dict(RUN, run_id="abcd1234:2", steps=[0, 1], skill="workflow-authoring")])
    wanted = lambda run: warehouse._captures(run, ("rde",))  # noqa: E731
    (path,) = instance.capture_session(mixed, Session(), END, root=tmp_path, runner=FakeMb(), wanted=wanted)
    assert path.name == "abcd1234_1.json"
    assert warehouse._captures({"skill": "workflow-authoring"}, ("*",)) is True


def test_a_failed_mb_call_says_what_went_wrong_without_the_version_notice():
    notice = "Could not parse the Metabase version; assuming a head build past v64."
    refused = '{"ok":false,"error":{"category":"network","message":"Connection refused by localhost:3201"}}'
    assert instance._failure(refused, notice) == "Connection refused by localhost:3201"
    assert instance._failure("", f"{notice}\nUnknown command: transform-test") == "Unknown command: transform-test"
