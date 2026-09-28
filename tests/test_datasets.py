"""The dataset each session and skill run was for (semantics/datasets.json), placed from the warehouse rows alone.
The cases are the shared warehouse's own: a Toy Store loaded into a Postgres database named stackexchange, a flights
instance that also holds the Stack Exchange tables, a Stripe run whose prompt says nothing, a Contrast run that
mentions Stripe once, and sessions that name a dataset only in passing."""

import json

from session_analytics import datasets, warehouse


def tables(**rows):
    t = {k: [] for k in warehouse.TABLES}
    t.update(rows)
    return t


def calls(sid, run_id, *inputs):
    return [{"session_id": sid, "tool_use_id": f"t{i}", "run_id": run_id, "input": x} for i, x in enumerate(inputs)]


def test_a_run_is_placed_by_its_prompt_even_on_a_database_named_after_another_dataset():
    # 59e71cc2: the toy store loaded into the lab's Postgres database `stackexchange`, which its tool calls name often
    sid = "59e71cc2-3d8c"
    t = tables(sessions=[{"session_id": sid, "title": "Data import"}],
               skill_runs=[{"session_id": sid, "run_id": "59e71cc2:1", "prompt": "now create a semantic layer for me",
                            "args": 'Instance: http://toy-store2.localhost:3202 (Metabase database id 2, Postgres db '
                                    '"stackexchange", schema public, now holding the Maven Fuzzy Factory toy store)'}],
               tool_calls=calls(sid, "59e71cc2:1", *["psql -d stackexchange -c 'select count(*) from orders'"] * 40))
    got = datasets.fill(t)
    (s,), (r,) = t["sessions"], t["skill_runs"]
    assert (r["dataset"], r["dataset_by"]) == ("Toy Store", "prompt")
    assert (s["dataset"], s["dataset_by"]) == ("Toy Store", "runs")
    assert got == {"sessions": {"Toy Store": 1}, "skill_runs": {"Toy Store": 1}}


def test_the_source_tables_of_a_run_never_count():
    # 139db903: its instance's database also holds the 12 Stack Exchange tables, and run_source_tables lists them all
    sid, rid = "139db903-b94b", "139db903:1"
    se = ["posts", "users", "votes", "comments", "badges", "tags", "post_history", "post_links", "post_tags",
          "post_types", "vote_types", "post_history_types"]
    t = tables(sessions=[{"session_id": sid, "title": "Reports"}],
               skill_runs=[{"session_id": sid, "run_id": rid, "prompt": "create meaningful tables and reports"}],
               run_artifacts=[{"session_id": sid, "run_id": rid, "kind": "transform", "name": "stg_bts_flight",
                               "definition": "-- Sources: flight_delays.flights, flight_delays.airlines\nselect …",
                               "target_table": "analytics.stg_bts_flight"}],
               run_source_tables=[{"session_id": sid, "run_id": rid, "schema_name": "public", "table_name": n}
                                  for n in se] + [{"session_id": sid, "run_id": rid, "schema_name": "flight_delays",
                                                   "table_name": "flights"}],
               run_instances=[{"session_id": sid, "run_id": rid, "host": "airline-flight-delays.localhost:3203",
                               "profile": "airline-flight-delays"}])
    datasets.fill(t)
    assert (t["skill_runs"][0]["dataset"], t["skill_runs"][0]["dataset_by"]) == ("Airline Flight Delays", "snapshot")
    t["run_artifacts"] = []  # no snapshot: the instance is named after the dataset
    datasets.fill(t)
    assert (t["skill_runs"][0]["dataset"], t["skill_runs"][0]["dataset_by"]) == ("Airline Flight Delays", "instance")


def test_a_silent_prompt_falls_to_the_tool_calls_and_pre_flight_is_not_a_flight():
    # 0190a1f7: "rde init" with new connections; the only data it touched was Stripe's
    sid, rid = "0190a1f7-25ab", "0190a1f7:1"
    inputs = ["mb table list --schema raw_stripe_dlt_spike --profile rde"] * 12 + ["run the pre-flight checks"] * 2
    t = tables(sessions=[{"session_id": sid, "title": "rde init with new connections"}],
               skill_runs=[{"session_id": sid, "run_id": rid, "prompt": 'i want to run "rde init"', "args": "init"}],
               tool_calls=calls(sid, rid, *inputs))
    datasets.fill(t)
    assert (t["skill_runs"][0]["dataset"], t["skill_runs"][0]["dataset_by"]) == ("Stripe", "tool calls: Stripe 12")
    assert (t["sessions"][0]["dataset"], t["sessions"][0]["dataset_by"]) == ("Stripe", "runs")
    # too few calls, or not far enough ahead of the runner-up: not settled
    rules = datasets.load()
    assert rules.dominant(["raw_stripe_dlt_spike"] * 9) == (None, None)
    assert rules.dominant(["raw_stripe_dlt_spike"] * 12 + ["select * from flight_delays.flights"] * 5) == (None, None)
    assert rules.dominant(["raw_stripe_dlt_spike"] * 12 + ["select * from flight_delays.flights"] * 2) == \
        ("Stripe", "tool calls: Stripe 12, Airline Flight Delays 2")
    # a run on one schema of a lab database that holds two, which checks the Sample Database is not it
    assert rules.dominant(["select * from dba.posts"] * 25 + ["select * from flight_delays.flights"] * 5
                          + ["the Sample Database has orders too"] * 4) == \
        ("DBA Stack Exchange", "tool calls: DBA Stack Exchange 25, Airline Flight Delays 5, Sample Database 4")


def test_a_run_that_probes_several_lab_instances_is_on_none_of_them():
    # 3b130560: rde building the sessions dashboard's Dimensions tab on metabase.example.com, timing mb on toy-store2 and
    # reading the lab's flight_delays.sql and Stack Exchange files on the way
    sid, rid = "3b130560-cf65", "3b130560:1"
    t = tables(sessions=[{"session_id": sid, "title": "Dimensions tab for code sessions dashboard"}],
               skill_runs=[{"session_id": sid, "run_id": rid, "prompt": "given the dashboard 9819-claude-code-sessions",
                            "args": "Add a Dimensions tab. Data: Metabase Analytics database, schema `sessions`."}],
               tool_calls=calls(sid, rid, *["mb transform list --profile toy-store2 --json"] * 11,
                                *["sed -n 1,40p lab/db/flight_delays.sql"] * 4, "git grep -n 'DBA Stack Exchange'"),
               run_instances=[{"session_id": sid, "run_id": rid, "host": "metabase.example.com", "profile": "team"}])
    datasets.fill(t)
    assert (t["skill_runs"][0]["dataset"], t["sessions"][0]["dataset"]) == (None, None)


def test_a_contrast_run_that_mentions_stripe_once_and_the_runs_around_it():
    # a2ef1324: a metabase-cli run looked at Stripe, two /rde calls said nothing, the third built on raw_contrast
    sid = "a2ef1324-8bec"
    t = tables(sessions=[{"session_id": sid, "title": "db sync-schema"}],
               skill_runs=[{"session_id": sid, "run_id": "a2ef1324:2", "prompt": "can you see the tables in my "
                                                                                   "connections?"},
                           {"session_id": sid, "run_id": "a2ef1324:4", "prompt": "/rde"},
                           {"session_id": sid, "run_id": "a2ef1324:5",
                            "prompt": "use /rde skill to build me a complete data flow using raw_contrast db",
                            "args": "using the raw_contrast database in the ClickHouse connection (profile rde)"}],
               tool_calls=calls(sid, "a2ef1324:2", *["mb table list --schema raw_stripe_dlt_spike"] * 6)
               + calls(sid, "a2ef1324:5", "only raw_stripe_dlt_spike is visible to this user",
                       *["mb query 'select * from raw_contrast.event'"] * 42))
    datasets.fill(t)
    runs = {r["run_id"]: (r["dataset"], r["dataset_by"]) for r in t["skill_runs"]}
    assert runs == {"a2ef1324:2": ("Contrast", "session"), "a2ef1324:4": ("Contrast", "session"),
                    "a2ef1324:5": ("Contrast", "prompt")}
    assert (t["sessions"][0]["dataset"], t["sessions"][0]["dataset_by"]) == ("Contrast", "runs")


def test_a_follow_up_run_takes_its_sessions_dataset():
    sid = "6e331b62-55cf"
    t = tables(sessions=[{"session_id": sid, "title": "Toy store database import and setup"}],
               skill_runs=[{"session_id": sid, "run_id": "6e331b62:2", "skill": "session-export",
                            "prompt": "export the session into clickhouse"}])
    datasets.fill(t)
    assert (t["skill_runs"][0]["dataset"], t["skill_runs"][0]["dataset_by"]) == ("Toy Store", "session")
    assert t["sessions"][0]["dataset_by"] == "title"


def test_a_baseline_is_one_run_whose_prompt_is_the_sessions_first():
    # a direct agent given an rde run's prompt: no skill_runs row, and a snapshot under <id>:0
    sid = "abcd1234-ffff"
    t = tables(sessions=[{"session_id": sid, "title": "Community dashboard", "baseline": True}],
               turns=[{"session_id": sid, "turn": 1, "trigger": "prompt", "prompt": "now add a chart"},
                      {"session_id": sid, "turn": 0, "trigger": "prompt",
                       "prompt": "baseline: i have a very cool dataset in localhost:3200, DBA Stack Exchange"}],
               run_instances=[{"session_id": sid, "run_id": "abcd1234:0", "host": "localhost:3200", "profile": None}])
    datasets.fill(t)
    assert (t["sessions"][0]["dataset"], t["sessions"][0]["dataset_by"]) == ("DBA Stack Exchange", "runs")
    # a silent prompt: its tool calls are the whole session's (no skill run made them), as the rde run's are its own,
    # and they outvote an instance named after another dataset
    t = tables(sessions=[{"session_id": sid, "title": "Semantic layer and dashboard", "baseline": True}],
               turns=[{"session_id": sid, "turn": 0, "trigger": "prompt",
                       "prompt": "Baseline: now create a semantic layer and a dashboard for me"}],
               tool_calls=calls(sid, None, *["mb query --profile dba 'select count(*) from flight_delays.flights'"] * 30),
               run_instances=[{"session_id": sid, "run_id": "abcd1234:0", "host": "dba.localhost:3200",
                               "profile": "dba"}])
    datasets.fill(t)
    assert (t["sessions"][0]["dataset"], t["sessions"][0]["dataset_by"]) == ("Airline Flight Delays", "runs")
    # without the flag (a row from before 0.7.0): its <id>:0 rows still make it one, placed by its instance
    t = tables(sessions=[{"session_id": sid, "title": "Reports"}],
               turns=[{"session_id": sid, "turn": 0, "trigger": "prompt", "prompt": "baseline: create reports"}],
               run_instances=[{"session_id": sid, "run_id": "abcd1234:0", "host": "toy-store2.localhost:3202",
                               "profile": "toy-store2"}])
    datasets.fill(t)
    assert (t["sessions"][0]["dataset"], t["sessions"][0]["dataset_by"]) == ("Toy Store", "runs")


def test_the_sample_database_on_an_instance_named_after_another_dataset_is_the_sample_database():
    # f086a80a, afb58d4c: rde on Metabase's Sample Database, on the lab's `dba` instance, or naming its marts mart_store_
    sid, rid = "f086a80a-92c2", "f086a80a:1"
    t = tables(sessions=[{"session_id": sid, "title": "Sample database reports and table structure"}],
               skill_runs=[{"session_id": sid, "run_id": rid, "prompt": "I want to use Sample database data to create "
                                                                          "meaningful reports, living in localhost:3200"}],
               tool_calls=calls(sid, rid, *["mb transform create --file .scratch/mart_store_fct_order.json"] * 12,
                                "cat .scratch/cfg_store.desc"),
               run_instances=[{"session_id": sid, "run_id": rid, "host": "localhost:3200", "profile": "dba"}])
    got = datasets.fill(t)
    assert (t["skill_runs"][0]["dataset"], t["skill_runs"][0]["dataset_by"]) == ("Sample Database", "prompt")
    assert (t["sessions"][0]["dataset"], t["sessions"][0]["dataset_by"]) == ("Sample Database", "runs")
    assert got == {"sessions": {"Sample Database": 1}, "skill_runs": {"Sample Database": 1}}
    t["skill_runs"][0]["prompt"] = "make reports"  # silent, and the instance is named dba: the title says which
    datasets.fill(t)
    assert (t["skill_runs"][0]["dataset"], t["skill_runs"][0]["dataset_by"]) == ("Sample Database", "session")
    assert (t["sessions"][0]["dataset"], t["sessions"][0]["dataset_by"]) == ("Sample Database", "title")
    t["sessions"][0]["title"] = "Reports"  # nothing says: the instance's name is all there is
    datasets.fill(t)
    assert (t["skill_runs"][0]["dataset"], t["skill_runs"][0]["dataset_by"]) == ("DBA Stack Exchange", "instance")


def test_an_instance_named_after_one_dataset_never_outvotes_a_title_that_names_another():
    # on the lab's newer instances every database holds both dba and flight_delays, whatever the instance is called
    sid, rid = "aaaa0005-x", "aaaa0005:1"
    t = tables(sessions=[{"session_id": sid, "title": "Flight delays tables and reports"}],
               skill_runs=[{"session_id": sid, "run_id": rid, "prompt": "/rde"}],
               tool_calls=calls(sid, rid, "mb table list --db 2 --schema flight_delays --profile dba"),
               run_instances=[{"session_id": sid, "run_id": rid, "host": "dba.localhost:3200", "profile": "dba"}])
    datasets.fill(t)
    assert (t["skill_runs"][0]["dataset"], t["skill_runs"][0]["dataset_by"]) == ("Airline Flight Delays", "session")
    assert (t["sessions"][0]["dataset"], t["sessions"][0]["dataset_by"]) == ("Airline Flight Delays", "title")


def test_a_dataset_named_in_passing_places_nothing():
    # 206f29cf: new connections "like for stripe and contrast", five Stripe files read as a template
    sid = "206f29cf-b41a"
    t = tables(sessions=[{"session_id": sid, "title": "UnifyGTM/AthenaHQ ClickHouse connection"}],
               turns=[{"session_id": sid, "turn": 0, "trigger": "prompt",
                       "prompt": "Can we create connection for UnifyGTM and AthenaHQ to clickhouse, feel free to use "
                                 "existing scaffolding like for stripe and contrast"}],
               tool_calls=calls(sid, None, *["git show 6af8e40 -- data_tools/stripe.py"] * 5,
                                "grep -n 'raw_contrast' README.md", "ls tests/"))
    assert datasets.fill(t) == {"sessions": {None: 1}, "skill_runs": {}}
    assert (t["sessions"][0]["dataset"], t["sessions"][0]["dataset_by"]) == (None, None)


def test_a_pasted_dashboard_link_names_a_dataset_only_in_its_path():
    # 3b130560: the prompt is a link to the sessions dashboard, whose filters hold a session's title and a dataset
    sid, rid = "3b130560-cf65", "3b130560:1"
    link = ("given the dashboard https://metabase.example.com/dashboard/9819-claude-code-sessions?de_topic=&dataset="
            "Airline+Flight+Delays&session=2026-09-25+21%3A31+%C2%B7+aad65ae1+%C2%B7+DBA+Stack+Exchange+dashboard"
            "&tab=4970-question-topics and the data in https://metabase.example.com/browse/databases/Analytics"
            "/schema/sessions create a new tab called \"Dimensions\"")
    t = tables(sessions=[{"session_id": sid, "title": "Dimensions tab for code sessions dashboard"}],
               skill_runs=[{"session_id": sid, "run_id": rid, "prompt": link, "args": "Add a Dimensions tab"}],
               tool_calls=calls(sid, rid, *["mb card list --json"] * 30))
    datasets.fill(t)
    assert (t["skill_runs"][0]["dataset"], t["sessions"][0]["dataset"]) == (None, None)
    t["skill_runs"][0]["args"] = "Add a Dimensions tab: localhost:3000/dashboard/9819?dataset=Stripe#theme=night"
    datasets.fill(t)
    assert (t["skill_runs"][0]["dataset"], t["sessions"][0]["dataset"]) == (None, None)
    t["skill_runs"][0]["prompt"] = ("create meaningful tables and reports "
                                    "http://dba.localhost:3200/browse/databases/2-analytics/schema/flight_delays?x=1")
    datasets.fill(t)
    assert (t["skill_runs"][0]["dataset"], t["skill_runs"][0]["dataset_by"]) == ("Airline Flight Delays", "prompt")


def test_runs_that_disagree_are_settled_by_the_session_else_by_majority():
    sid = "s1-000"
    runs = [{"session_id": sid, "run_id": f"s1:{i}", "prompt": p, "start_at": f"2026-09-2{i}T10:00:00.000Z"}
            for i, p in enumerate(["build on raw_stripe_dlt_spike", "the toy store dashboard",
                                   "raw_stripe_dlt_spike again"], 1)]
    t = tables(sessions=[{"session_id": sid, "title": "Stripe and more"}], skill_runs=runs)
    datasets.fill(t)
    assert (t["sessions"][0]["dataset"], t["sessions"][0]["dataset_by"]) == ("Stripe", "title")
    assert [r["dataset"] for r in t["skill_runs"]] == ["Stripe", "Toy Store", "Stripe"]  # each run keeps its own
    t["sessions"][0]["title"] = "Two datasets"
    datasets.fill(t)
    assert (t["sessions"][0]["dataset"], t["sessions"][0]["dataset_by"]) == ("Stripe", "runs: Stripe 2, Toy Store 1")


def test_missing_tables_and_columns_are_missing_evidence():
    # a share file from before 0.7.0 (no run_* tables), rows a 0.1.0 sync wrote (no args, no baseline)
    t = {"sessions": [{"session_id": "x1", "title": None}, {"session_id": "x2", "title": "Stripe star schema"}],
         "skill_runs": [{"session_id": "x1", "run_id": "x1:1"}, {"run_id": "orphan:1", "session_id": "gone"}],
         "tool_calls": [{"session_id": "x1", "input": None}]}
    got = datasets.fill(t)
    assert [s["dataset"] for s in t["sessions"]] == [None, "Stripe"]
    assert all(r["dataset"] is None and r["dataset_by"] is None for r in t["skill_runs"])
    assert got == {"sessions": {None: 1, "Stripe": 1}, "skill_runs": {None: 2}}
    assert datasets.fill({}) == {"sessions": {}, "skill_runs": {}}
    # a hand-edited file: a number for a host, and a session and a run held twice (each copy gets the columns)
    t = {"sessions": [{"session_id": "x3", "title": "Toy store"}, {"session_id": "x3", "title": "Toy store"}],
         "skill_runs": [{"session_id": "x3", "run_id": "x3:1"}, {"session_id": "x3", "run_id": "x3:1"}],
         "run_instances": [{"session_id": "x3", "run_id": "x3:1", "host": 3202, "profile": 7}]}
    assert datasets.fill(t) == {"sessions": {"Toy Store": 2}, "skill_runs": {"Toy Store": 2}}
    assert all(r["dataset_by"] in ("title", "session") for r in t["sessions"] + t["skill_runs"])


def test_the_rules_never_take_a_word_datasets_share(tmp_path):
    rules = datasets.load()
    shared = ("users posts orders products customer invoice subscription card flight flights stackexchange "
              "mb_store_ _store_ _dlt_id localhost:3200 owner_user_id pre-flight in-flight contrast")
    assert rules.named(shared) == [] and rules.named("stg_metabase_store_subscription int_metabase_store_contract") == []
    assert rules.named("Airline Flight Delays") == ["Airline Flight Delays"]
    assert rules.named("dba.stackexchange.com") == ["DBA Stack Exchange"]
    assert rules.named("Maven+Fuzzy+Factory.zip") == ["Toy Store"] and rules.named("dbt_models_stripe") == ["Stripe"]
    assert rules.hosted([{"profile": "toy-store2"}, {"host": "localhost:3200"}]) == ["Toy Store"]
    assert rules.hosted([{"profile": "rde", "host": "dba.localhost:3200"}]) == ["DBA Stack Exchange"]
    assert rules.hosted([{"profile": "dbadmin"}]) == []  # the whole name
    spec = json.loads(datasets.DEFAULT.read_text(encoding="utf-8"))
    assert [d["label"] for d in spec["datasets"]] == rules.labels and spec["min_calls"] == rules.min_calls
    other = tmp_path / "datasets.json"  # the file is the rules: edited, they change
    other.write_text(json.dumps(dict(spec, min_calls=3)))
    assert datasets.load(other).dominant(["raw_stripe"] * 3)[0] == "Stripe"
