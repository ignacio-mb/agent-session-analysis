"""The Postgres warehouse: rows per table, the bundle on disk, and the SQL (loading itself needs Docker)."""

import csv

from test_questions import checks_file, interview_session, skill_dir  # noqa: F401 - shared fixtures

from session_analytics import warehouse
from session_analytics.analyze import analyze
from session_analytics.parse import parse_session
from session_analytics.pricing import Pricing


def test_session_rows_cover_every_table(claude_dir, skill_dir, checks_file):  # noqa: F811
    p = interview_session(claude_dir, skill_dir)
    s = parse_session(p, own_only=True)
    rows = warehouse.session_rows(analyze(s, Pricing(), checks=[checks_file]), s)
    (sess,) = rows["sessions"]
    assert sess["session_id"] == s.session_id and sess["questions"] == 9 and sess["skill_runs"] == 1
    (run,) = rows["skill_runs"]
    assert run["questions_asked"] == 7 and run["recommended_taken"] == 1 and run["recommended_offered"] == 3
    assert {r["check_id"] for r in rows["skill_run_checks"]} == {"sign-off-once", "plain-language", "recommendation-first"}
    assert len(rows["questions"]) == 9 and all(q["qid"].startswith(s.session_id[:8] + ":") for q in rows["questions"])
    assert sum(1 for o in rows["question_options"] if o["chosen"]) >= 3
    create = [c for c in rows["cli_calls"] if c["signature"] == "mb transform create"]
    assert create and create[0]["run_id"] == run["run_id"]
    assert all(t["run_id"] == run["run_id"] for t in rows["tool_calls"] if t["tool"] == "AskUserQuestion")
    for name, (_, cols, pk) in warehouse.TABLES.items():
        names = {c for c, _, _ in cols}
        for r in rows[name]:
            assert set(r) <= names, (name, set(r) - names)
            assert all(r.get(k) is not None for k in pk), (name, pk)


def test_a_session_is_keyed_by_its_opening_prompt_as_a_run_is():
    typed = [{"trigger": "task_notification", "prompt": "done"},
             {"trigger": "prompt", "prompt": "I want to use @/tmp/a.csv Sample database data, https://x.io/y to build"},
             {"trigger": "prompt", "prompt": "now a dashboard"}]
    assert warehouse._first_prompt_key(typed) == "i want to use sample database data to"
    command = [{"trigger": "command", "prompt": None, "command": "/rde", "command_args": "I want to use Sample data"}]
    assert warehouse._first_prompt_key(command) == "i want to use sample data"
    assert warehouse._first_prompt_key([{"trigger": "bash", "prompt": "ls"}]) is None


def test_bundle_and_sql(claude_dir, skill_dir, checks_file, tmp_path):  # noqa: F811
    interview_session(claude_dir, skill_dir)
    tables, meta = warehouse.build(claude_dir=claude_dir, since="all")
    assert meta["transcripts"] == 1 and not meta["failed"]
    counts = warehouse.write_bundle(tables, tmp_path / "wh")
    assert counts["sessions"] == 1 and counts["questions"] == 9
    with open(tmp_path / "wh" / "questions.csv", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0].keys() == {c for c, _, _ in warehouse.TABLES["questions"][1]}
    assert {r["outcome"] for r in rows} >= {"recommended", "typed", "declined", "unanswered"}
    sql = warehouse.schema_sql()
    assert sql.count("CREATE TABLE") == len(warehouse.TABLES) and "COMMENT ON TABLE questions" in sql
    assert warehouse.views_sql().count("CREATE VIEW") == len(warehouse.VIEW_COMMENTS)
    assert warehouse._cell(True) == "true" and warehouse._cell(None) == "" and warehouse._cell(3.0) == "3"


def test_raw_recount_matches_what_the_warehouse_loads(claude_dir, skill_dir, checks_file):  # noqa: F811
    from session_analytics import reconcile
    p = interview_session(claude_dir, skill_dir)
    sid, raw = reconcile.raw_counts(p)
    s = parse_session(p, own_only=True)
    rows = warehouse.session_rows(analyze(s, Pricing(), checks=[checks_file]), s)
    assert sid == s.session_id
    assert raw["api_requests"] == len(rows["api_requests"])
    assert raw["output_tokens"] == sum(r["output_tokens"] for r in rows["api_requests"])
    assert raw["tool_calls"] == len(rows["tool_calls"])
    assert raw["tool_errors"] == sum(1 for r in rows["tool_calls"] if r["status"] in ("error", "denied", "interrupted"))
    assert raw["ask_questions"] == sum(1 for q in rows["questions"] if q["channel"] == "ask") == 7
    assert raw["skill_calls"] == 1
    tables, _ = warehouse.build(claude_dir=claude_dir, since="all")
    (load,) = tables["warehouse_load"]
    assert load["sessions"] == 1 and load["transcripts"] == 1


def test_a_session_is_live_when_any_of_its_files_was_written_after_the_load(claude_dir, skill_dir, checks_file,  # noqa: F811
                                                                            monkeypatch):
    # A workflow's agents keep writing <sid>/subagents/** after the main transcript goes quiet: a load taken
    # meanwhile has part of the session, and comparing it would report a difference that is not a bug.
    import os

    from session_analytics import reconcile
    p = interview_session(claude_dir, skill_dir)
    sid, raw = reconcile.raw_counts(p)
    old = raw["_newest_ms"] / 1000 - 3600
    os.utime(p, (old, old))
    stale = dict({k: raw[k] for k in reconcile.FIELDS}, api_requests=raw["api_requests"] - 1)
    monkeypatch.setattr(reconcile, "warehouse_counts", lambda psql: ({sid: stale}, (old + 60) * 1000))
    res = reconcile.check(None, claude_dir)
    assert [d["session_id"] for d in res["differ"]] == [sid] and not res["live"] and not res["ok"]
    agent = p.parent / sid / "subagents" / "workflows" / "wf_x" / "agent-a.jsonl"
    agent.parent.mkdir(parents=True)
    agent.write_text("")
    res = reconcile.check(None, claude_dir)
    assert res["live"] == [sid] and not res["differ"] and res["ok"]


def test_a_baseline_is_marked_and_keyed_as_the_prompt_it_was_given():
    turns = [{"trigger": "prompt", "prompt": "Baseline: I want to use Sample database data to build reports"}]
    assert warehouse.is_baseline(turns) is True
    assert warehouse._first_prompt_key(turns) == warehouse._first_prompt_key(
        [{"trigger": "prompt", "prompt": "I want to use Sample database data to build reports"}])
    assert warehouse.is_baseline([{"trigger": "prompt", "prompt": "a baseline: not at the start"}]) is False


def test_the_baselines_are_shared_with_the_skills_sessions():
    tables = {k: [] for k in warehouse.TABLES}
    tables["sessions"] = [{"session_id": "rde1"}, {"session_id": "base1", "baseline": True}, {"session_id": "other"}]
    tables["skill_invocations"] = [{"session_id": "rde1", "skill": "rde", "success": True, "status": "ok"}]
    assert warehouse.sessions_with_skill(tables, ("rde",)) == {"rde1", "base1"}


def test_a_baseline_session_is_one_run_for_its_snapshot():
    class S:
        session_id = "abcd1234-ffff"
    a = {"session": {"start": "2026-09-28T10:00:00.000Z", "end": "2026-09-28T11:00:00.000Z"},
         "turns": {"rows": [{"trigger": "prompt", "prompt": "baseline: build on localhost:3200"}]},
         "trace": {"steps": [{"k": "tool"}, {"k": "text"}, {"k": "tool"}]}}
    run = warehouse.baseline_run(a, S())
    assert run == {"run_id": "abcd1234:0", "session_id": "abcd1234-ffff", "baseline": True,
                   "start_ms": 1790589600000.0, "end_ms": 1790593200000.0,
                   "prompt": "baseline: build on localhost:3200", "steps": [0, 1, 2]}
    assert warehouse.baseline_run(dict(a, session={}), S()) is None
