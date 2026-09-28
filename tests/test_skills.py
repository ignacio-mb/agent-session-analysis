"""Skill runs, versions, checks and the cross-session skill report, against a real (temporary) git repo."""

import json
import os
import subprocess
from datetime import datetime, timezone

import pytest
from conftest import Transcript, U, text, tool_use

from session_analytics import checks as checks_mod
from session_analytics.analyze import analyze, cli_calls
from session_analytics.parse import parse_session, skill_fingerprint
from session_analytics.pricing import Pricing
from session_analytics.skillreport import compare_runs, render_compare, run_skill_report
from session_analytics.skillruns import prompt_key, read_first

FRONT = "---\nname: demo\ndescription: test skill\n---\n"
BODY_V1 = "# demo\n\nRead the playbook at ${CLAUDE_SKILL_DIR}/playbooks/build.md, then build.\n"
BODY_V2 = BODY_V1 + "\nAsk before creating anything.\n"
PLAYBOOK = "# Build\n\nRead first: [`naming.md`](../references/naming.md).\n\n1. Create the card.\n"


def git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout


@pytest.fixture
def skill_repo(tmp_path):
    repo = tmp_path / "agent-skills"
    skill = repo / "skills" / "demo"
    (skill / "playbooks").mkdir(parents=True)
    (skill / "references").mkdir()
    git(repo.parent, "init", "-q", str(repo))
    (skill / "SKILL.md").write_text(FRONT + BODY_V1)
    (skill / "playbooks" / "build.md").write_text(PLAYBOOK)
    (skill / "references" / "naming.md").write_text("# Naming\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "v1")
    (skill / "SKILL.md").write_text(FRONT + BODY_V2)
    git(repo, "commit", "-q", "-am", "v2: ask first")
    return skill


def injected(skill_dir, body, args=None):
    text_ = body.replace("${CLAUDE_SKILL_DIR}", str(skill_dir)).strip()
    return f"Base directory for this skill: {skill_dir}\n\n{text_}" + (f"\n\nARGUMENTS: {args}" if args else "")


def run_session(claude_dir, skill_dir, sid, body, *, ask, harness_first=False, follow_up=False, day=1):
    tr = Transcript(claude_dir, session_id=sid, start=datetime(2026, 9, day, 10, 0, tzinfo=timezone.utc))
    tr.prompt("build a revenue card", "p1")
    if harness_first:
        tr.meta("<command-message>workflow-authoring</command-message>\n<command-name>workflow-authoring</command-name>", "p1")
        tr.meta("# Workflow authoring reference", "p1")
    tr.assistant(f"{sid}-m1", [tool_use(f"{sid}-sk", "Skill", skill="demo", args="build it")], U())
    tr.tool_result(f"{sid}-sk", "Launching skill: demo", "p1", result={"success": True, "commandName": "demo"})
    tr.meta(injected(skill_dir, body, "build it"), "p1", sourceToolUseID=f"{sid}-sk")
    tr.assistant(f"{sid}-m2", [tool_use(f"{sid}-state", "Bash", command="cat ./.scratch/STATE.md"),
                                tool_use(f"{sid}-pb", "Read", file_path=f"{skill_dir}/playbooks/build.md")], U(), attributionSkill="demo")
    tr.tool_result(f"{sid}-state", "no state", "p1", result={"stdout": "", "stderr": "", "interrupted": False})
    tr.tool_result(f"{sid}-pb", PLAYBOOK, "p1", result={"type": "text", "file": {
        "filePath": f"{skill_dir}/playbooks/build.md", "content": PLAYBOOK, "numLines": 5, "startLine": 1, "totalLines": 5}})
    if ask:
        tr.assistant(f"{sid}-m3", [tool_use(f"{sid}-ask", "AskUserQuestion", questions=[{"question": "Go?", "header": "Gate",
                                                                                     "options": [], "multiSelect": False}])],
                     U(), attributionSkill="demo")
        tr.tool_result(f"{sid}-ask", "answered", "p1", result={"questions": [{"question": "Go?", "header": "Gate"}],
                                                               "answers": {"Go?": "Yes"}})
    tr.assistant(f"{sid}-m4", [tool_use(f"{sid}-create", "Bash",
                                        command="mb card create --file .scratch/c.json --json --profile p")],
                 U(), attributionSkill="demo")
    tr.tool_result(f"{sid}-create", '{"id":5,"name":"Revenue"}', "p1", result={"stdout": '{"id":5,"name":"Revenue"}'})
    tr.assistant(f"{sid}-m5", [text("Built the Revenue card.")], U(), stop="end_turn", attributionSkill="demo")
    tr.system("turn_duration", durationMs=30000, messageCount=9)
    if follow_up:
        tr.prompt("also add a dashboard", "p2", advance=60)
        tr.assistant(f"{sid}-m6", [tool_use(f"{sid}-dash", "Bash",
                                            command="mb dashboard create --body '{\"name\":\"Board\"}' --json --profile p")], U())
        tr.tool_result(f"{sid}-dash", '{"id":9,"name":"Board"}', "p2", result={"stdout": '{"id":9,"name":"Board"}'})
        tr.assistant(f"{sid}-m7", [text("Added the Board dashboard.")], U(), stop="end_turn")
        tr.system("turn_duration", durationMs=20000, messageCount=4)
    return tr.write()


CHECKS = {"skill": "demo", "checks": [
    {"id": "ask-first", "type": "before", "a": {"tool": "^AskUserQuestion$"}, "b": {"bash": r"mb \w+ create"}},
    {"id": "file-bodies", "type": "every", "segments": True, "match": {"bash": r"^\s*mb \w+ (create|update)"},
     "require": {"bash": "--file"}},
    {"id": "playbook", "type": "count", "match": {"resource": "^playbooks/"}, "distinct": True, "min": 1},
    {"id": "state-first", "type": "first", "match": {"bash": r"STATE\.md"}},
    {"id": "never-delete", "type": "never", "match": {"bash": r"mb \w+ delete"}},
    {"id": "one-ask", "type": "count_before", "match": {"tool": "^AskUserQuestion$"}, "until": {"bash": "create"},
     "min": 1, "max": 1},
]}


@pytest.fixture
def checks_file(tmp_path):
    p = tmp_path / "demo.json"
    p.write_text(json.dumps(CHECKS))
    return str(p)


def test_fingerprint_undoes_per_run_substitutions(skill_repo):
    a = skill_fingerprint(injected(skill_repo, BODY_V1, "x" * 12), str(skill_repo), "sess", "x" * 12)
    b = skill_fingerprint(injected("/elsewhere/demo", BODY_V1), "/elsewhere/demo", "other", None)
    assert a == b


def test_runs_map_to_commits_and_include_follow_ups(claude_dir, skill_repo, checks_file):
    p = run_session(claude_dir, skill_repo, "aaaaaaaa-0000-0000-0000-000000000001", BODY_V1, ask=False, follow_up=True)
    a = analyze(parse_session(p), Pricing(), checks=[checks_file])
    (run,) = a["skill_runs"]
    assert run["version"]["status"] == "commit" and run["version"]["subject"] == "v1"
    assert run["turn_count"] == 2 and run["follow_up_turns"] == 1
    assert run["attributed_requests"] == 3 and run["requests"] == 6  # the invoking request, then 3 attributed, then 2 follow-up
    assert [(o["type"], o["verb"], o["name"]) for o in run["objects"]] == [("card", "create", "Revenue"),
                                                                           ("dashboard", "create", "Board")]
    assert run["playbooks"] == ["playbooks/build.md"]
    assert run["expected_by_playbooks"] == ["references/naming.md"]
    assert run["missing_expected"] == ["references/naming.md"]
    assert {c["id"]: c["status"] for c in run["checks"]} == {
        "ask-first": "fail", "file-bodies": "fail", "playbook": "pass", "state-first": "pass", "never-delete": "pass",
        "one-ask": "fail"}
    assert run["final_message"] == "Added the Board dashboard."
    assert run["cli"][0]["signature"] in ("mb card create", "mb dashboard create")


def test_harness_skill_nests_instead_of_owning_the_run(claude_dir, skill_repo, checks_file):
    p = run_session(claude_dir, skill_repo, "bbbbbbbb-0000-0000-0000-000000000002", BODY_V2, ask=True, harness_first=True)
    a = analyze(parse_session(p), Pricing(), checks=[checks_file])
    (run,) = a["skill_runs"]
    assert run["skill"] == "demo" and run["version"]["subject"] == "v2: ask first"
    assert [n["name"] for n in run["nested_skills"]] == ["workflow-authoring"]
    assert run["question_calls"] == 1 and run["interview"]["questions"][0]["answer"] == "Yes"
    assert {c["id"]: c["status"] for c in run["checks"]}["ask-first"] == "pass"


def test_skill_report_groups_by_version(claude_dir, skill_repo, checks_file, tmp_path):
    run_session(claude_dir, skill_repo, "aaaaaaaa-0000-0000-0000-000000000001", BODY_V1, ask=False, follow_up=True, day=1)
    run_session(claude_dir, skill_repo, "bbbbbbbb-0000-0000-0000-000000000002", BODY_V2, ask=True, day=2)
    res = run_skill_report("demo", claude_dir=claude_dir, since="all", out_dir=tmp_path / "rep", check_files=[checks_file],
                           now_ms=datetime(2026, 9, 5, tzinfo=timezone.utc).timestamp() * 1000)
    rep = res["report"]
    assert [v["subject"] for v in rep["versions"]] == ["v1", "v2: ask first"]
    assert rep["versions"][0]["checks"]["ask-first"]["rate"] == 0 and rep["versions"][1]["checks"]["ask-first"]["rate"] == 1
    changes = rep["versions"][1]["changes"]
    assert [c["subject"] for c in changes["commits"]] == ["v2: ask first"]
    assert changes["files"] == [{"path": "SKILL.md", "added": 2, "removed": 0}]
    assert any("ask-first" in n for n in rep["insights"])
    assert (tmp_path / "rep" / "skill.html").is_file() and (tmp_path / "rep" / "csv" / "checks.csv").is_file()
    a, b = res["runs"]
    cmp = compare_runs(a, b)
    assert "+ask the user" in cmp["diff"]
    assert "ask-first" in render_compare(a, b, cmp)


def test_read_first_names_files_relative_to_the_playbook():
    assert read_first(PLAYBOOK, "playbooks/build.md") == ["references/naming.md"]


def test_cli_calls_signatures():
    sigs = [(c["signature"], c["help"]) for c in cli_calls('mb transform create --help | head; mb query "$P" --json')]
    assert sigs == [("mb transform create", True), ("head", False), ("mb query", False)]
    assert [c["signature"] for c in cli_calls("git -C r commit -m x && gh pr create")] == ["git commit", "gh pr create"]


def test_check_types_and_bad_checks(tmp_path):
    ev = [{"tool": "Bash", "command": "mb auth list && mb --version", "segments": ["mb auth list", "mb --version"],
           "resources": [], "input": "", "status": "ok"},
          {"tool": "Read", "command": None, "segments": None, "resources": ["playbooks/a.md"], "input": "", "status": "ok"}]
    before = {"id": "v", "type": "before", "a": {"bash": r"mb --version"}, "b": {"bash": r"mb auth"}}
    assert checks_mod.evaluate(before, ev)["status"] == "fail"  # same command, but auth list comes first
    assert checks_mod.evaluate({"id": "c", "type": "count", "match": {"tool": "Read"}, "max": 0}, ev)["status"] == "fail"
    assert checks_mod.evaluate({"id": "n", "type": "never", "match": {"status": "error"}}, ev)["status"] == "pass"
    assert checks_mod.evaluate({"id": "x", "type": "count", "match": {"nope": "x"}}, ev)["status"] == "error"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"skill": "demo", "checks": [{"id": "z", "type": "sometimes"}]}))
    with pytest.raises(ValueError):
        checks_mod.load([str(bad)], skill="demo")


def test_rde_checks_file_is_valid():
    loaded = checks_mod.load(skill="rde")
    assert len(loaded) >= 10 and all(c["type"] in checks_mod.VALID_TYPES for c in loaded)


def test_prompt_key_groups_repeats_of_one_prompt():
    first = ("I want to use Sample database data to create meaningul reports. living in "
             "http://localhost:13004/browse/databases/1-sample-database. use /rde skill")
    again = "I want to use Sample database data to create meaningful reports. living in http://localhost:3100/."
    assert prompt_key(first) == prompt_key(again) == "i want to use sample database data to"
    assert prompt_key("/rde http://localhost:13004 build the revenue model") == "build the revenue model"
    assert prompt_key('@"/Users/x/Downloads/fct_arr_test.json" @notes.md /rde You are a data engineer. Build a '
                      "self-contained Stripe star schema") == "you are a data engineer build a self"
    assert prompt_key("/rde") is None and prompt_key(None) is None


def test_rde_tests_are_written_before_the_first_transform_run():
    check = next(c for c in checks_mod.load(skill="rde") if c["id"] == "tests-before-first-run")
    ev = lambda cmd: {"tool": "Bash", "command": cmd, "segments": [cmd], "resources": [], "input": "", "status": "ok"}  # noqa: E731
    create, test, run = (ev("mb transform create --file .scratch/t.json --json"),
                         ev("mb transform-test create --file .scratch/tt.json --json"), ev("mb transform run 7 --sync --json"))
    assert checks_mod.evaluate(check, [create, test, run])["status"] == "pass"
    assert checks_mod.evaluate(check, [create, run, test])["status"] == "fail"
    assert checks_mod.evaluate(check, [create, ev("mb transform-test create --help"), run])["status"] == "fail"
    assert checks_mod.evaluate(check, [create, test])["status"] == "n/a"
