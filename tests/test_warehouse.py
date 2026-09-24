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
