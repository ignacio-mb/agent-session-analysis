"""The files a skill run wrote: the shell parsed for writes, runs and reads, what a script drives, and a run's working
files against what the skill names — through the checks and into the warehouse."""

import json
from datetime import datetime, timezone

import pytest
from conftest import Transcript, U, text, tool_use

from session_analytics import warehouse
from session_analytics.analyze import analyze, cli_calls, unwrap_substitution
from session_analytics.parse import parse_session
from session_analytics.pricing import Pricing
from session_analytics.workfiles import api_calls, drives, kind_of, location, parse_command

MEMORY = "/home/me/.claude/projects/-work-proj/memory/gotchas.md"
FILES = {"expected": [{"id": "state", "label": "STATE.md", "match": r"(^|/)\.scratch/STATE\.md$"},
                      {"id": "body", "label": "A JSON body", "match": r"(^|/)\.scratch/(.+/)?[^/]+\.json$"}]}
TEMP_CHECK = {"id": "working-files-in-scratch", "type": "never", "match": {"location": "^temp$"}}


def test_what_a_shell_command_writes_runs_and_reads():
    out = parse_command(
        "cd sub && cat > a.json <<'JSON'\n{}\nJSON\n"
        "mb x get 1 >> log.txt 2>&1; tee -a t.txt < a.json > /dev/null\n"
        "cp a.json b.json ./keep/ && mv a.json c.json && touch d.md && curl -s -o e.csv https://x/api/y -o /dev/null\n"
        "D=/tmp/w; echo hi > $D/f.txt; echo x > $UNSET/g.txt\n"
        "source ./probe.sh && ./run.sh && python3 -c 'print(1)' && bash -c \"a\nb\"", "/p")
    assert [(w["path"], w["via"]) for w in out["writes"]] == [
        ("/p/sub/a.json", "heredoc"), ("/p/sub/log.txt", "append"), ("/p/sub/t.txt", "tee"),
        ("/p/sub/keep/a.json", "copy"), ("/p/sub/keep/b.json", "copy"), ("/p/sub/c.json", "move"),
        ("/p/sub/d.md", "touch"), ("/p/sub/e.csv", "download"), ("/tmp/w/f.txt", "redirect")]
    assert out["writes"][0]["content"] == "{}" and out["writes"][2]["append"]
    assert out["runs"] == ["/p/sub/probe.sh", "/p/sub/run.sh"]
    assert out["inline"] == ["print(1)", "a\nb"]
    assert {("/p/sub/a.json", sig) for sig in ("tee", "cp", "mv")} <= set(out["refs"])


def test_a_heredoc_on_an_interpreter_is_a_program_and_a_file_it_reads_is_used():
    out = parse_command("python3 - <<'PY'\nimport json\nprint(1)\nPY\nmb card create --file=.scratch/c.json --json", "/p")
    assert out["writes"] == [] and out["inline"] == ["import json\nprint(1)"]
    assert out["refs"] == [("/p/.scratch/c.json", "mb card create")]


def test_a_command_inside_a_substitution_is_the_command():
    # ID=$(mb card create …) is `mb card create`, for the CLI tables and checks as much as for what read a file
    assert [c["signature"] for c in cli_calls("ID=$(mb transform create --file t.json --json | jq -r .id)")] == [
        "mb transform create", "jq"]
    assert [c["signature"] for c in cli_calls("for i in $(mb card list --json | jq -r '.data[].id'); do echo $i; done")] \
        == ["mb card list", "jq", "echo"]
    assert unwrap_substitution('D="$(mb skills path x)"') == "mb skills path x" and unwrap_substitution("D=/x") == "D=/x"
    out = parse_command("ID=$(mb card create --file .scratch/c.json --json | jq .id); N=$(python3 .scratch/n.py)", "/p")
    assert ("/p/.scratch/c.json", "mb card create") in out["refs"] and out["runs"] == ["/p/.scratch/n.py"]


def test_what_a_script_drives():
    py = ('import subprocess\nsubprocess.run(["mb", "card", "create", "--file", f])\n'
          'subprocess.run(["mb", kind, "update", str(i)])\nos.system("gh pr create --fill")\nnote = "go up, make sure"\n')
    assert drives(py, "x.py") == ["mb card create", "mb * update", "gh pr create"]
    sh = "mb dashboard get 3 --json > d.json\nfor i in 1 2; do mb card update $i --file c.json; done\ncurl -s $U/api/card\n"
    assert drives(sh, "x.sh") == ["mb dashboard get", "mb card update", "curl"]
    assert api_calls(sh) == ["/api/card"] and api_calls("requests.post(u)") == ["http"] and api_calls("x = 1") == []


def test_kinds_and_locations():
    assert [kind_of(p) for p in ("a.py", "b.SQL", "c.json", "ids.txt", "n.md", "x.env", ".env", "blob")] == [
        "script", "sql", "json", "data", "doc", "env", "env", "other"]
    assert kind_of("tool", executed=True) == "script"
    assert [location(p, "/work/proj") for p in ("/work/proj/.scratch/a", "/private/tmp/x", "/var/folders/q/y", MEMORY,
                                                 "/opt/z")] == ["project", "temp", "temp", "memory", "other"]


def bash(tr, tid, command):
    tr.assistant(f"m-{tid}", [tool_use(tid, "Bash", command=command)], U(), attributionSkill="demo")
    tr.tool_result(tid, "done", "p1", result={"stdout": "", "stderr": "", "interrupted": False})


def write(tr, tid, path, content):
    tr.assistant(f"m-{tid}", [tool_use(tid, "Write", file_path=path, content=content)], U(), attributionSkill="demo")
    tr.tool_result(tid, "File created", "p1", result={"type": "create", "filePath": path, "content": content,
                                                      "structuredPatch": []})


def files_session(claude_dir, skill_dir, sid="ffffffff-0000-0000-0000-000000000001"):
    tr = Transcript(claude_dir, session_id=sid, start=datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc))
    tr.prompt("build revenue reporting", "p1")
    tr.assistant("m-sk", [tool_use("sk", "Skill", skill="demo")], U())
    tr.tool_result("sk", "Launching skill: demo", "p1", result={"success": True, "commandName": "demo"})
    tr.meta(f"Base directory for this skill: {skill_dir}\n\n# demo", "p1", sourceToolUseID="sk")
    bash(tr, "b1", "cat ./.scratch/STATE.md 2>/dev/null; mkdir -p ./.scratch && cat > ./.scratch/STATE.md <<'EOF'\n"
                   "# STATE\nprofile: p\nEOF")
    bash(tr, "b2", "cat > .scratch/card.json <<'JSON'\n{\"name\": \"Revenue\"}\nJSON\n"
                   "mb card create --file .scratch/card.json --json")
    bash(tr, "b3", "cat > ./.scratch/build.py <<'PY'\nimport subprocess\nfor n in (1, 2):\n"
                   "    subprocess.run([\"mb\", \"dashboard\", \"update\", str(n)])\nrequests.post(url + '/api/dashboard/1')\n"
                   "PY\npython3 ./.scratch/build.py && python3 ./.scratch/build.py")
    bash(tr, "b4", "mb card list --json > /tmp/cards.json 2>&1")
    bash(tr, "b5", "python3 - <<'PY'\nimport json\nprint(len(json.load(open('/tmp/cards.json'))))\nPY")
    write(tr, "w1", MEMORY, "# gotchas\n")
    write(tr, "w2", "/work/proj/.scratch/ids.txt", "1\n2\n")
    tr.assistant("m-e1", [tool_use("e1", "Edit", file_path="/work/proj/.scratch/STATE.md", old_string="p",
                                   new_string="q")], U(), attributionSkill="demo")
    tr.tool_result("e1", "updated", "p1", result={"filePath": "/work/proj/.scratch/STATE.md", "structuredPatch": []})
    tr.assistant("m-end", [text("Built the card and the dashboard.")], U(), stop="end_turn", attributionSkill="demo")
    tr.system("turn_duration", durationMs=60000, messageCount=20)
    return tr.write()


@pytest.fixture
def skill_dir(tmp_path):
    d = tmp_path / "skills" / "demo"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: demo\n---\n# demo\n")
    return d


def checks_file(tmp_path, files=FILES):
    p = tmp_path / "demo.json"
    p.write_text(json.dumps({"skill": "demo", "checks": [TEMP_CHECK], **({"files": files} if files else {})}))
    return str(p)


def test_a_runs_working_files_against_what_the_skill_names(claude_dir, skill_dir, tmp_path):
    s = parse_session(files_session(claude_dir, skill_dir), own_only=True)
    (run,) = analyze(s, Pricing(), checks=[checks_file(tmp_path)])["skill_runs"]
    rows = {w["path"]: w for w in run["working_files"]}
    assert run["files_written"] == [".scratch/STATE.md", ".scratch/card.json", ".scratch/build.py", "/tmp/cards.json",
                                    MEMORY, ".scratch/ids.txt"]
    state = rows[".scratch/STATE.md"]
    assert (state["expected"], state["support"], state["created"], state["via"], state["writes"], state["edits"],
            state["lines"]) == ("state", False, True, "heredoc", 1, 1, 2)
    body = rows[".scratch/card.json"]
    assert (body["expected_label"], body["support"], body["kind"], body["used_by"]) == \
        ("A JSON body", False, "json", ["mb card create"])
    script = rows[".scratch/build.py"]
    assert (script["support"], script["kind"], script["runs"], script["lines"], script["drives"], script["api"]) == \
        (True, "script", 2, 4, ["mb dashboard update"], ["/api/dashboard"])
    temp = rows["/tmp/cards.json"]
    assert (temp["location"], temp["support"], temp["via"], temp["kind"]) == ("temp", True, "redirect", "json")
    assert (rows[MEMORY]["location"], rows[MEMORY]["support"]) == ("memory", False)
    assert (rows[".scratch/ids.txt"]["support"], rows[".scratch/ids.txt"]["kind"]) == (True, "data")
    assert {k: run[k] for k in ("files_created", "support_files", "support_scripts", "support_script_runs",
                                "inline_scripts", "inline_script_lines", "temp_files", "memory_notes")} == {
        "files_created": 6, "support_files": 3, "support_scripts": 1, "support_script_runs": 2, "inline_scripts": 1,
        "inline_script_lines": 2, "temp_files": 1, "memory_notes": 1}
    (check,) = run["checks"]
    assert check["status"] == "fail" and "/tmp/cards.json" in check["detail"]


def test_without_the_skill_naming_its_files_support_is_unknown(claude_dir, skill_dir, tmp_path):
    s = parse_session(files_session(claude_dir, skill_dir), own_only=True)
    (run,) = analyze(s, Pricing(), checks=[checks_file(tmp_path, files=None)])["skill_runs"]
    assert run["support_files"] is None and run["files_created"] == 6
    assert {w["support"] for w in run["working_files"] if w["location"] != "memory"} == {None}


def test_the_warehouse_has_a_row_per_file_and_the_runs_counts(claude_dir, skill_dir, tmp_path):
    s = parse_session(files_session(claude_dir, skill_dir), own_only=True)
    rows = warehouse.session_rows(analyze(s, Pricing(), checks=[checks_file(tmp_path)]), s)
    (run,) = rows["skill_runs"]
    assert (run["files_written"], run["files_created"], run["support_files"], run["inline_scripts"]) == (6, 6, 3, 1)
    files = {r["path"]: r for r in rows["skill_run_working_files"]}
    assert len(files) == 6 and all(r["run_id"] == run["run_id"] for r in files.values())
    assert files[".scratch/build.py"]["drives"] == "mb dashboard update" and files[".scratch/build.py"]["runs"] == 2
    assert files[".scratch/card.json"]["used_by"] == "mb card create" and files[".scratch/ids.txt"]["used_by"] is None
    _, cols, pk = warehouse.TABLES["skill_run_working_files"]
    names = {c for c, _, _ in cols}
    assert all(set(r) <= names and all(r[k] is not None for k in pk) for r in files.values())
