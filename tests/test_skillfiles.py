"""Which skill documents a run was shown: shell parsing, output matching, provenance, exposure, the report."""

import json
from datetime import datetime, timezone

import pytest
from conftest import Transcript, U, text, tool_use
from test_skills import git, injected

from session_analytics import checks as checks_mod
from session_analytics import skillfiles as sf
from session_analytics.analyze import analyze
from session_analytics.parse import parse_session
from session_analytics.pricing import Pricing
from session_analytics.skillreport import run_skill_report
from session_analytics.skillruns import read_first

FRONT = "---\nname: docs\ndescription: a skill with documents\n---\n"
BODY_V1 = "# docs\n\nRoute: read `playbooks/build.md`. Mechanics: `mb skills path dashboard`, one section.\n"
BODY_V2 = BODY_V1 + "\nAsk before creating anything.\n"
PLAYBOOK = "# Build\n\nRead first: [`guide.md`](../references/guide.md).\n\n1. Create the card.\n"
GUIDE_V1 = ("# Guide\n\n## Alpha section\n\nalpha line one is here\nalpha line two is here\n\n## Beta section\n\n"
            "beta line one is here\nbeta line two is here\n\n## Gamma section\n\ngamma line one is here\n")
GUIDE_V2 = GUIDE_V1 + "gamma line two is NEW here\n"
MB_DASH = ("# Dashboard\n\n## Layout\n\nthe grid is 24 columns wide\ncards snap to the grid rows\n\n## Wiring\n\n"
           "filters map to card parameters\nlinked filters cascade values\n")


@pytest.fixture
def docs_env(tmp_path):
    """A skill in git (v1, then v2 changing SKILL.md and the guide), an installed copy that still has the
    v1 guide, and a CLI package bundling a dashboard skill."""
    repo = tmp_path / "agent-skills"
    src = repo / "skills" / "docs"
    (src / "playbooks").mkdir(parents=True)
    (src / "references").mkdir()
    git(tmp_path, "init", "-q", str(repo))
    (src / "SKILL.md").write_text(FRONT + BODY_V1)
    (src / "playbooks" / "build.md").write_text(PLAYBOOK)
    (src / "references" / "guide.md").write_text(GUIDE_V1)
    (src / "references" / "unused.md").write_text("# Unused\n\nnobody reads this file\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "v1")
    (src / "SKILL.md").write_text(FRONT + BODY_V2)
    (src / "references" / "guide.md").write_text(GUIDE_V2)
    git(repo, "commit", "-q", "-am", "v2: gamma")
    v1 = git(repo, "rev-parse", "--short", "HEAD~1").strip()

    installed = tmp_path / "installed" / "docs"  # copied before the guide changed: a stale install
    (installed / "playbooks").mkdir(parents=True)
    (installed / "references").mkdir()
    (installed / "SKILL.md").write_text(FRONT + BODY_V2)
    (installed / "playbooks" / "build.md").write_text(PLAYBOOK)
    (installed / "references" / "guide.md").write_text(GUIDE_V1)
    (installed / "references" / "unused.md").write_text("# Unused\n\nnobody reads this file\n")

    pkg = tmp_path / "node_modules" / "@metabase" / "cli"
    (pkg / "skill-data" / "dashboard").mkdir(parents=True)
    (pkg / "package.json").write_text(json.dumps({"name": "@metabase/cli"}))
    (pkg / "skill-data" / "dashboard" / "SKILL.md").write_text(MB_DASH)
    return {"src": src, "installed": installed, "cli": pkg / "skill-data", "v1": v1, "tmp": tmp_path}


def bash(tr, tid, command, output, prompt="p1", is_error=False, **kw):
    tr.assistant(f"m-{tid}", [tool_use(tid, "Bash", command=command)], U(), attributionSkill="docs", **kw)
    tr.tool_result(tid, output, prompt, is_error=is_error, result={"stdout": output, "stderr": "", "interrupted": False})


def docs_session(claude_dir, env, sid="dddddddd-0000-0000-0000-000000000001", day=2):
    inst, cli = env["installed"], env["cli"]
    tr = Transcript(claude_dir, session_id=sid, cwd=str(env["tmp"] / "work"),
                    start=datetime(2026, 9, day, 10, 0, tzinfo=timezone.utc))
    tr.prompt("build a board", "p1")
    tr.assistant("m-sk", [tool_use("sk", "Skill", skill="docs")], U())
    tr.tool_result("sk", "Launching skill: docs", "p1", result={"success": True, "commandName": "docs"})
    tr.meta(injected(inst, BODY_V2), "p1", sourceToolUseID="sk")
    bash(tr, "b1", f"cat {inst}/playbooks/build.md", PLAYBOOK)
    lines = GUIDE_V2.split("\n")
    tr.assistant("m-r1", [tool_use("r1", "Read", file_path=f"{inst}/references/guide.md", offset=3, limit=4)], U(),
                 attributionSkill="docs")
    part = "\n".join(lines[2:6])
    tr.tool_result("r1", part, "p1", result={"type": "text", "file": {
        "filePath": f"{inst}/references/guide.md", "content": part, "numLines": 4, "startLine": 3, "totalLines": 15}})
    bash(tr, "b2", "mb skills path dashboard", json.dumps({"data": [{"name": "dashboard", "dir": f"{cli}/dashboard"}]}))
    bash(tr, "b3", "D=$(mb skills path dashboard | jq -r '.data[0].dir'); sed -n '/## Layout/,/^## /p' \"$D/SKILL.md\"",
         "## Layout\n\nthe grid is 24 columns wide\ncards snap to the grid rows\n\n## Wiring")
    bash(tr, "b4", f"cd {inst} && grep -n beta references/guide.md", "10:beta line one is here\n11:beta line two is here")
    bash(tr, "b5", f"ls {inst}/references", "guide.md\nunused.md")
    bash(tr, "b6", f"cat {inst}/references/guide.md", GUIDE_V1)
    bash(tr, "b7", f"cat {inst}/references/missing.md", f"cat: {inst}/references/missing.md: No such file or directory",
         is_error=True)
    tr.assistant("m-end", [text("Built the board.")], U(), stop="end_turn", attributionSkill="docs")
    tr.system("turn_duration", durationMs=30000, messageCount=20)
    return tr.write()


def test_shell_ops_follow_variables_substitutions_cd_and_loops():
    ops = sf.shell_ops("C=$(mb skills path core | jq -r '.data[0].dir'); ls -R $C; grep -n -i -A3 'model' $C/SKILL.md | head",
                       "/w")
    assert [(o["op"], o["path"]) for o in ops] == [("resolve", "<cli:mb>/core"), ("list", "<cli:mb>/core"),
                                                   ("search", "<cli:mb>/core/SKILL.md")]
    assert ops[2]["detail"] == "grep -n -i -A 3 model | head"
    ops = sf.shell_ops("cd /s/rde && cat references/a.md; echo ======; sed -n 1,40p playbooks/b.md | head -5", "/w")
    assert [(o["op"], o["path"], o["via"]) for o in ops] == [("read", "/s/rde/references/a.md", "cat"),
                                                             ("read", "/s/rde/playbooks/b.md", "sed")]
    ops = sf.shell_ops('for f in ~/s/rde/playbooks/*.md; do head -3 "$f"; done', "/w")
    assert ops[0]["path"].endswith("/s/rde/playbooks/*.md") and ops[0]["via"] == "head"
    ops = sf.shell_ops("mb skills get core,dashboard --full | head -100", "/w")
    assert [(o["op"], o["path"]) for o in ops] == [("read", "<cli:mb>/core/SKILL.md"), ("read", "<cli:mb>/dashboard/SKILL.md"),
                                                   ("read", "<cli:mb>/core"), ("read", "<cli:mb>/dashboard")]
    assert sf.shell_ops("awk -F'\\t' '{print $1}' /tmp/x.tsv 2>/dev/null | sort", "/w")[0]["path"] == "/tmp/x.tsv"
    assert sf.shell_ops("jq -r '.data[0].dir' out.json", "/w")[0] == {
        "path": "/w/out.json", "op": "read", "via": "jq", "recursive": False, "filter": None, "detail": "jq -r '.data[0].dir'"}
    assert sf.shell_ops("sed -i '' 's/a/b/' /s/rde/SKILL.md", "/w") == []  # an edit, not a read
    assert sf.shell_ops("grep -rl tab /s/rde", "/w")[0]["op"] == "list"  # file names only


def test_output_matching_measures_what_was_shown():
    prof = sf._profile(GUIDE_V2)
    keys = sf.output_index("6:alpha line two is here\n9-\n10:beta line one is here\n")
    assert sorted(sf.seen_in_output(prof, keys)) == [6, 10]
    keys = sf.output_index(json.dumps({"data": [{"content": GUIDE_V2}]}))  # `mb skills get --json`
    assert sf._dcov(prof, sf.seen_in_output(prof, keys)) == 1.0
    assert sf.sections(prof[0], {9, 10}) == ["Beta section"]
    assert sf.ranges([1, 2, 3, 7, 9, 10]) == [[1, 3], [7, 7], [9, 10]]


def test_docset_names_cli_docs_after_the_program(docs_env, tmp_path):
    docs = sf.DocSet({"docs": {str(docs_env["installed"])}})
    assert docs.locate(f"{docs_env['installed']}/references/guide.md") == ("docs", "references/guide.md")
    assert docs.locate(f"{docs_env['cli']}/dashboard/SKILL.md") == ("mb", "dashboard/SKILL.md")  # from package.json
    assert docs.locate(f"{docs_env['src']}/playbooks/build.md") == ("docs", "playbooks/build.md")
    assert docs.locate("/elsewhere/.claude/skills/other/x.md") == ("other", "x.md")
    assert docs.locate("/work/proj/README.md") is None
    assert docs.expand({"path": f"{docs_env['installed']}/.git/config", "op": "read"}) == []


def test_run_files_measures_every_document(claude_dir, docs_env):
    a = analyze(parse_session(docs_session(claude_dir, docs_env)), Pricing(), skill_sources=[str(docs_env["src"])])
    (run,) = a["skill_runs"]
    assert run["version"]["subject"] == "v2: gamma"
    files = {(f["owner"], f["path"]): f for f in run["skill_files"]["files"]}
    body = files[("docs", "SKILL.md")]
    assert body["how"] == "injected" and body["order"] == 0 and body["version"] == "match"
    pb = files[("docs", "playbooks/build.md")]
    assert (pb["how"], pb["named_by"], pb["version"]) == ("full", ["SKILL.md"], "match")
    guide = files[("docs", "references/guide.md")]
    # Read 3-6, grep 9-10, then `cat` of the installed copy, which is still v1: every line but the new one.
    assert guide["named_by"] == ["playbooks/build.md"] and guide["accesses"] == 3
    assert guide["version"] == f"older {docs_env['v1']}"
    assert guide["lines"] == [[1, 15]]
    dash = files[("mb", "dashboard/SKILL.md")]
    assert (dash["how"], dash["sections"], dash["named_by"]) == ("partial", ["Layout", "Wiring"], ["docs:SKILL.md"])
    assert files[("docs", "references/missing.md")]["how"] == "missing"
    acc = [(x["path"], x["op"], x["how"]) for x in run["skill_files"]["accesses"]]
    assert ("references/guide.md", "read", "partial") in acc and ("references/guide.md", "search", "hits") in acc
    assert ("references/", "list", "listed") in acc and ("dashboard/", "resolve", "resolved") in acc
    assert run["skill_files"]["inventory"]["never"] == ["references/unused.md"]
    t = run["skill_files"]["totals"]
    assert (t["own_shown"], t["own_inventory"], t["other_files"], t["missing"]) == (3, 4, 1, 1)
    assert run["docs_read"] == 2 and run["cli_docs_read"] == 1 and run["doc_version_mismatches"] == 1
    # What v2 changed: SKILL.md (injected, so seen) and the guide's new last line, which the stale copy lacks.
    changes = {c["path"]: c["status"] for c in run["changes_seen"]["files"]}
    assert changes == {"SKILL.md": "seen", "references/guide.md": "not in the lines read"}
    steps = [a["trace"]["steps"][i] for i in run["steps"]]
    assert any("search docs:references/guide.md" in (st.get("res") or ()) for st in steps)


def test_file_checks_and_report(claude_dir, docs_env, tmp_path):
    docs_session(claude_dir, docs_env)
    cfile = tmp_path / "docs.json"
    cfile.write_text(json.dumps({"skill": "docs", "checks": [
        {"id": "one-section", "type": "never", "match": {"file": "^mb:[^/]+/SKILL\\.md$", "op": "read", "how": "^full$"}},
        {"id": "no-guesses", "type": "never", "match": {"how": "^missing$"}},
        {"id": "two-refs", "type": "count", "distinct": True, "match": {"file": "^docs:references/"}, "min": 2}]}))
    res = run_skill_report("docs", claude_dir=claude_dir, since="all", out_dir=tmp_path / "rep", check_files=[str(cfile)],
                           sources=[str(docs_env["src"])], now_ms=datetime(2026, 9, 5, tzinfo=timezone.utc).timestamp() * 1000)
    (run,) = res["runs"]
    status = {c["id"]: (c["status"], c.get("detail")) for c in run["checks"]}
    assert status["one-section"] == ("pass", None)
    assert status["no-guesses"] == ("fail", "1 matching, e.g. docs:references/missing.md")
    assert status["two-refs"][0] == "pass"
    rep = res["report"]
    (v,) = rep["versions"]
    assert v["never"] == ["references/unused.md"]
    rows = {f["file"]: f for f in rep["files"]}
    assert rows["docs:references/guide.md"]["per_version"][v["key"]]["mismatches"] == [f"older {docs_env['v1']}"]
    assert rows["mb:dashboard/SKILL.md"]["per_version"][v["key"]]["sections"] == {"Layout": 1, "Wiring": 1}
    assert any("not the version they ran" in n for n in rep["insights"])
    assert (tmp_path / "rep" / "csv" / "skill_files.csv").read_text().count("\n") == 1 + len(run["skill_files"]["files"])
    assert "## Skill files" in (tmp_path / "rep" / "skill.md").read_text()


def test_read_first_resolves_relative_links():
    assert read_first(PLAYBOOK, "playbooks/build.md") == ["references/guide.md"]
    assert checks_mod.FILE_KEYS == ("file", "op", "how")
