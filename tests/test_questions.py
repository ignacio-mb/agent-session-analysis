"""The user interview: questions through AskUserQuestion and in prose, their topics, outcomes and flags."""

import json
from datetime import datetime, timezone

import pytest
from conftest import Transcript, U, text, tool_use

from session_analytics import questions as qmod
from session_analytics.analyze import analyze
from session_analytics.parse import parse_session
from session_analytics.pricing import Pricing
from session_analytics.skillreport import run_skill_report

INTERVIEW = {
    "prose": "avoid",
    "jargon": ["grain", "fact table"],
    "topics": [
        {"id": "sign-off", "label": "Sign-off", "match": "sign.?off|who (decides|owns)", "once": True, "must_ask": True},
        {"id": "placement", "label": "Where tables live", "match": "schema|land in"},
        {"id": "sources", "label": "Which sources", "match": "sources?"},
        {"id": "definition", "label": "A definition", "match": "counts? as|revenue", "evidence": True},
    ],
}
CHECKS = {"skill": "demo", "interview": INTERVIEW, "checks": [
    {"id": "sign-off-once", "type": "count", "match": {"topic": "^sign-off$"}, "max": 1},
    {"id": "plain-language", "type": "never", "match": {"tool": "^AskUserQuestion$", "flag": "^jargon"}},
    {"id": "recommendation-first", "type": "never",
     "match": {"tool": "^AskUserQuestion$", "flag": "no recommendation|recommendation not first"}},
]}
SIGNOFF = "Who signs off on what a number means?"
SCHEMA = "Which schema should the new tables land in?"
REVENUE = "What counts as revenue here? The grain is one row per order."
CHECKPOINT = ("Profiled.\n\n[CHECKPOINT]\nDecision: which date reports treat as now\nContext: the data stops on 2020-04-19\n"
              "Options:\n  A. Pin to the data\n  B. Pin to today\nRecommendation: A, the data has no later rows\n"
              "Action required: answer the question that follows.\n")


def opts(*labels):
    return [{"label": x, "description": f"about {x}"} for x in labels]


def ask(tr, tid, qs, answers, prompt, *, status="ok", annotations=None, feedback=None, advance=45.0, **kw):
    tr.assistant(f"m-{tid}", [text("I measured 1,204 orders across 3 tables."), tool_use(tid, "AskUserQuestion", questions=qs)],
                 U(), attributionSkill="demo", **kw)
    if status == "pending":
        return
    if status == "denied":
        tr.tool_result(tid, "The user doesn't want to proceed with this tool use. The tool use was rejected. To tell you "
                       f"how to proceed, the user said:\n{feedback}", prompt, is_error=True, advance=advance)
        return
    result = {"questions": qs, "answers": answers}
    if annotations:
        result["annotations"] = annotations
    tr.tool_result(tid, "The user answered: …", prompt, result=result, advance=advance)


@pytest.fixture
def checks_file(tmp_path):
    p = tmp_path / "demo.json"
    p.write_text(json.dumps(CHECKS))
    return str(p)


@pytest.fixture
def skill_dir(tmp_path):
    d = tmp_path / "skills" / "demo"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: demo\n---\n# demo\nInterview, then build.\n")
    return d


def interview_session(claude_dir, skill_dir, sid="eeeeeeee-0000-0000-0000-000000000001", day=3):
    tr = Transcript(claude_dir, session_id=sid, start=datetime(2026, 9, day, 9, 0, tzinfo=timezone.utc))
    tr.prompt("build revenue reporting", "p1")
    tr.assistant("m-sk", [tool_use("sk", "Skill", skill="demo")], U())
    tr.tool_result("sk", "Launching skill: demo", "p1", result={"success": True, "commandName": "demo"})
    tr.meta(f"Base directory for this skill: {skill_dir}\n\n# demo\nInterview, then build.", "p1", sourceToolUseID="sk")
    gate = [
        {"question": SIGNOFF, "header": "Sign-off", "multiSelect": False, "options": opts("Me: decide and show me (Recommended)", "Check everything", "Just go")},
        {"question": SCHEMA, "header": "Schema", "multiSelect": False, "options": opts("analytics (Recommended)", "Different name")},
        {"question": REVENUE, "header": "Revenue", "multiSelect": False, "options": opts("Net sales", "Gross")},
    ]
    ask(tr, "a1", gate, {SIGNOFF: "Me: decide and show me (Recommended)", SCHEMA: "rde_3 schema", REVENUE: "Gross"}, "p1",
        annotations={SIGNOFF: {"notes": "batch the definitions"}})
    multi = [{"question": "Which sources should I include?", "header": "Sources", "multiSelect": True,
              "options": opts("orders", "refunds", "events")}]
    ask(tr, "a2", multi, {"Which sources should I include?": ["orders", "my custom feed"]}, "p1")
    tr.assistant("m-create", [tool_use("c1", "Bash", command="mb transform create --file .scratch/t.json --json")], U(),
                 attributionSkill="demo")
    tr.tool_result("c1", '{"id": 7, "name": "stg_orders"}', "p1", result={"stdout": '{"id": 7, "name": "stg_orders"}'})
    tr.assistant("m-fail", [tool_use("c2", "Bash", command="mb transform run 7 --json")], U(), attributionSkill="demo")
    tr.tool_result("c2", "Exit code 1\nerror: target schema missing", "p1", is_error=True)
    again = [{"question": "Who decides what revenue counts as?", "header": "Sign-off", "multiSelect": False,
              "options": opts("You (Recommended)", "Finance")}]
    ask(tr, "a3", again, {"Who decides what revenue counts as?": "Finance"}, "p1")
    ask(tr, "a4", [{"question": "Publish now?", "header": "Publish", "multiSelect": False, "options": opts("Yes (Recommended)", "No")}],
        None, "p1", status="denied", feedback="stop asking, just build it")
    tr.assistant("m-end1", [text("Built stg_orders. Want me to add a dashboard?")], U(), stop="end_turn", attributionSkill="demo")
    tr.system("turn_duration", durationMs=90000, messageCount=20)
    tr.prompt("yes please", "p2", advance=120.0)
    tr.assistant("m-end2", [text(CHECKPOINT)], U(), stop="end_turn")
    tr.system("turn_duration", durationMs=5000, messageCount=2)
    tr.prompt("A", "p3", advance=30.0)
    ask(tr, "a5", [{"question": "Keep the working files?", "header": "Files", "multiSelect": False,
                    "options": opts("Keep (Recommended)", "Skip")}], None, "p3", status="pending")
    return tr.write()


def test_outcomes_topics_flags_and_waits(claude_dir, skill_dir, checks_file):
    a = analyze(parse_session(interview_session(claude_dir, skill_dir)), Pricing(), checks=[checks_file])
    iv = a["interview"]
    rows = {q["question"]: q for q in iv["questions"]}
    s = rows[SIGNOFF]
    assert (s["topic"], s["outcome"], s["notes"], s["form"], s["run_id"]) == ("sign-off", "recommended", "batch the definitions",
                                                                             "choice", "eeeeeeee:1")
    assert s["wait_ms"] == 45000 and s["before_create"] is True and s["options"][0]["chosen"]
    sc = rows[SCHEMA]
    assert (sc["topic"], sc["outcome"], sc["typed"]) == ("placement", "typed", "rde_3 schema")
    rv = rows[REVENUE]
    assert rv["outcome"] == "picked" and "no recommendation" in rv["flags"] and "jargon: grain" in rv["flags"]
    assert "no measured numbers" not in rv["flags"]  # the same reply measured 1,204 orders
    mu = rows["Which sources should I include?"]
    assert (mu["form"], mu["outcome"], mu["typed"]) == ("multi", "typed + picked", "my custom feed")
    re_ask = rows["Who decides what revenue counts as?"]
    assert re_ask["topic"] == "sign-off" and "asked again" in re_ask["flags"] and "after an error" in re_ask["flags"]
    assert re_ask["outcome"] == "other option" and re_ask["before_create"] is False
    pub = rows["Publish now?"]
    assert (pub["outcome"], pub["feedback"], pub["form"]) == ("declined", "stop asking, just build it", "confirm")
    offer = rows["Want me to add a dashboard?"]
    assert (offer["kind"], offer["topic"], offer["outcome"], offer["reply"]) == ("prose", "offer", "accepted", "yes please")
    assert "asked in prose" in offer["flags"] and offer["wait_ms"] == 120500  # the turn_duration event adds 0.5 s
    cp = rows["which date reports treat as now"]
    assert cp["kind"] == "checkpoint" and cp["reply"] == "A" and cp["recommended_label"] == "A. Pin to the data"
    assert "checkpoint with no AskUserQuestion" in cp["flags"]
    assert rows["Keep the working files?"]["outcome"] == "unanswered"
    assert (iv["total"], iv["asked"], iv["calls"], iv["prose"], iv["checkpoints_unasked"]) == (9, 7, 5, 1, 1)
    assert (iv["recommended_picked"], iv["recommended_offered"], iv["typed"], iv["declined"], iv["unanswered"]) == (1, 3, 2, 1, 1)
    (run,) = a["skill_runs"]
    assert run["questions_asked"] == 7 and run["prose_questions"] == 1 and run["typed_answers"] == 2
    assert run["questions_before_create"] == 4 and run["interview"]["taxonomy"] == "skill"
    status = {c["id"]: c["status"] for c in run["checks"]}
    assert status == {"sign-off-once": "fail", "plain-language": "fail", "recommendation-first": "fail"}


def test_skill_report_interview(claude_dir, skill_dir, checks_file, tmp_path):
    interview_session(claude_dir, skill_dir)
    res = run_skill_report("demo", claude_dir=claude_dir, since="all", out_dir=tmp_path / "rep", check_files=[checks_file],
                           sources=[str(skill_dir)], now_ms=datetime(2026, 9, 6, tzinfo=timezone.utc).timestamp() * 1000)
    rep = res["report"]
    iv = rep["interview"]
    assert iv["taxonomy"]["source"] == "skill" and [c["topic"] for c in iv["catalog"]][:2] == ["sign-off", "placement"]
    signoff = next(c for c in iv["catalog"] if c["topic"] == "sign-off")
    assert (signoff["asked"], signoff["reasked"], signoff["must_ask"], signoff["once"]) == (2, 1, True, True)
    assert [t["typed"] for t in iv["typed"]] == ["rde_3 schema", "my custom feed"]
    notes = " ".join(rep["insights"])
    assert "typed instead of picked" in notes and "in prose" in notes and "Asked again within a run" in notes
    v = rep["versions"][0]["interview"]
    assert v["asked"] == 7 and v["prose"] == 1
    assert (tmp_path / "rep" / "csv" / "questions.csv").read_text().count("\n") >= 10
    assert "## Interview" in (tmp_path / "rep" / "skill.md").read_text()


def test_prose_questions_and_outcomes():
    txt = ("Done — the card is live (e.g. at /q/7). Want me to add a filter? Or should I stop here?\n\n"
           "| Metric | Why? |\n|---|---|\n```\nwhy?\n```\n> quoted question?\n- Should I also publish it?")
    assert qmod.prose_questions(txt) == ["Want me to add a filter?", "Or should I stop here?", "Should I also publish it?"]
    o = [{"label": "A (Recommended)", "recommended": True}, {"label": "B", "recommended": False}]
    assert qmod._outcome("ok", "A (Recommended)", o, False)[0] == "recommended"
    assert qmod._outcome("ok", "B", o, False)[0] == "other option"
    assert qmod._outcome("ok", "[No preference]", o, False)[0] == "no preference"
    assert qmod._outcome("ok", ["A (Recommended)", "mine"], o, True)[:3] == ("typed + picked", ["A (Recommended)"], "mine")
    assert qmod._outcome("pending", None, o, False)[0] == "unanswered"
