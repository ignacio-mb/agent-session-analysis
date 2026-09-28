"""Share files: a tester writes their sessions that ran a skill to one file, with no ClickHouse access, and whoever
holds the connection imports it — under the tester's source, with the runs labelled against the importer's checkout."""

from datetime import datetime, timezone

import pytest
from conftest import Transcript, U, text
from test_clickhouse import FakeClickHouse
from test_questions import interview_session
from test_skills import BODY_V1, FRONT, PLAYBOOK, git, run_session

from session_analytics import __version__, cli, clickhouse, share

SID = "eeeeeeee-0000-0000-0000-000000000001"
OTHER = "ffffffff-0000-0000-0000-000000000002"


def plain_session(claude, sid=OTHER):
    """A session that never ran a skill."""
    tr = Transcript(claude, session_id=sid, start=datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc))
    tr.prompt("what does this query do", "p1")
    tr.assistant(f"{sid}-m1", [text("It counts orders per day.")], U(), stop="end_turn")
    return tr.write()


def as_sender(monkeypatch, tmp_path):
    monkeypatch.setattr(clickhouse, "_machine_id", lambda: "machine-1")
    monkeypatch.setattr(clickhouse, "_git_email", lambda: "ana@example.com")
    monkeypatch.setattr(clickhouse, "checkout_env_file", lambda: tmp_path / "none.env")


def as_importer(monkeypatch, tmp_path, fake):
    """Another machine: its own id and email, and the ClickHouse connection. Returns the warehouse arguments."""
    monkeypatch.setattr(clickhouse, "_machine_id", lambda: "machine-2")
    monkeypatch.setattr(clickhouse, "_git_email", lambda: "ig@example.com")
    monkeypatch.setattr(clickhouse, "Client", fake)
    env = tmp_path / "ch.env"
    env.write_text("CLICKHOUSE_URL=https://u:p@h:8443/sessions\n")
    return ["warehouse", "--env-file", str(env), "--skills", "demo"]


@pytest.fixture
def sender(tmp_path, monkeypatch):
    """A tester's machine: one session that ran the skill `demo` (the interview fixture), one that ran nothing."""
    claude = tmp_path / "claude"
    skill = tmp_path / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\n---\n# demo\nInterview, then build.\n")
    as_sender(monkeypatch, tmp_path)
    return {"claude": claude, "skilled": interview_session(claude, skill), "plain": plain_session(claude)}


def share_file(claude, out, *extra):
    assert cli.main(["share", "--skills", "demo", "--claude-dir", str(claude), "--out", str(out), *extra]) == 0
    return share.read(out)


def test_a_share_file_holds_the_sessions_that_ran_the_skill_and_who_sent_them(sender, tmp_path):
    out = tmp_path / "share.json"
    doc = share_file(sender["claude"], out)
    assert doc["format"] == share.FORMAT and doc["generator_version"] == __version__
    assert (doc["person"], doc["source"]) == ("ana@example.com", clickhouse.source_id(sender["claude"]))
    assert [r["session_id"] for r in doc["tables"]["sessions"]] == [SID]
    assert {r["session_id"] for r in doc["tables"]["questions"]} == {SID}
    assert OTHER not in out.read_text()  # a session that never ran the skill leaves not even its id
    assert "de_topics" not in doc["tables"] and doc["withdrawn"] == []
    (run,) = doc["versions"].values()
    assert run["session_id"] == SID and run["skill"] == "demo" and run["fingerprint"]
    rows = sum(len(v) for v in doc["tables"].values())
    assert sum(1 for line in out.read_text().splitlines() if line.startswith("   {")) == rows  # a row per line
    assert oct(out.stat().st_mode & 0o777) == "0o600"


def test_only_transcripts_that_invoke_the_skill_are_parsed(sender):
    pattern = share._pattern(("demo",))
    assert share._mentions(sender["skilled"], pattern) and not share._mentions(sender["plain"], pattern)
    for line in ('{"text":"the demo skill: see checks/demo.json, \\"demo\\""}', '{"skill":"demos"}'):
        assert not pattern.search(line.encode())  # talking about the skill is not running it
    for line in ('{"input":{"skill":"demo"}}', '{"input": {"skill": "agent-skills:demo"}}',
                 "<command-name>/demo</command-name>", "<command-name>demo</command-name>"):
        assert pattern.search(line.encode())
    side = sender["plain"].with_suffix("") / "subagents"  # a run inside a subagent counts
    side.mkdir(parents=True)
    (side / "agent-1.jsonl").write_text('{"input":{"skill":"agent-skills:demo"}}\n')
    assert share._mentions(sender["plain"], pattern)
    assert share._pattern(("*",)) is None


def test_a_session_left_out_stays_out_until_included_again(sender, tmp_path):
    doc = share_file(sender["claude"], tmp_path / "a.json", "--exclude", SID[:8])
    assert doc["tables"]["sessions"] == [] and doc["withdrawn"] == [SID]
    assert share_file(sender["claude"], tmp_path / "b.json")["withdrawn"] == [SID]  # remembered
    doc = share_file(sender["claude"], tmp_path / "c.json", "--include", SID[:4])
    assert [r["session_id"] for r in doc["tables"]["sessions"]] == [SID] and doc["withdrawn"] == []


def test_an_import_loads_the_file_under_its_senders_source(sender, tmp_path, monkeypatch, capsys):
    out = tmp_path / "share.json"
    doc = share_file(sender["claude"], out)
    ch = FakeClickHouse()
    args = as_importer(monkeypatch, tmp_path, ch)
    capsys.readouterr()
    assert cli.main(args + ["--import", str(out)]) == 0
    src = doc["source"]
    assert ch.sessions("sessions", src) == {SID} and set(ch.held("sessions")) == {src}  # not the importer's source
    assert {r["person"] for r in ch.rows_of("questions")} == {"ana@example.com"}
    (load,) = ch.rows_of("warehouse_load", src)
    assert load["generator_version"] == __version__ and load["sessions"] == 1
    assert ch.rows_of("de_topics")  # the importer's taxonomy, as in a direct load
    assert "ana@example.com (" in capsys.readouterr().out
    before = len(ch.rows_of("questions"))
    assert cli.main(args + ["--import", str(tmp_path)]) == 0  # the same file again, from its folder: no change
    assert len(ch.rows_of("questions")) == before and ch.sessions("sessions", src) == {SID}
    share.write(dict(doc, created_at="2020-01-01T00:00:00.000Z", tables=dict(doc["tables"], questions=[])),
                tmp_path / "old.json")
    capsys.readouterr()
    assert cli.main(args + ["--import", str(tmp_path / "old.json")]) == 0  # an older file rolls nothing back
    assert "skipped" in capsys.readouterr().out and len(ch.rows_of("questions")) == before


def test_a_session_the_sender_left_out_is_taken_out_and_stays_out(sender, tmp_path, monkeypatch):
    src = share_file(sender["claude"], tmp_path / "1.json")["source"]
    share_file(sender["claude"], tmp_path / "2.json", "--exclude", SID)
    share_file(sender["claude"], tmp_path / "3.json", "--include", SID)
    ch = FakeClickHouse()
    args = as_importer(monkeypatch, tmp_path, ch)
    assert cli.main(args + ["--import", str(tmp_path / "1.json")]) == 0
    assert ch.sessions("sessions", src) == {SID}
    assert cli.main(args + ["--import", str(tmp_path / "2.json")]) == 0
    assert src not in ch.held("sessions") and src not in ch.held("questions")
    assert cli.main(args + ["--import", str(tmp_path / "3.json")]) == 0
    assert ch.sessions("sessions", src) == {SID}


def test_an_import_labels_each_run_with_the_commit_that_ran(tmp_path, monkeypatch):
    # The tester installed the skill as a plain copy: nothing on their machine says which commit it is.
    installed = tmp_path / "installed" / "demo"
    (installed / "playbooks").mkdir(parents=True)
    (installed / "SKILL.md").write_text(FRONT + BODY_V1)
    (installed / "playbooks" / "build.md").write_text(PLAYBOOK)
    claude = tmp_path / "claude"
    run_session(claude, installed, SID, BODY_V1, ask=True)
    as_sender(monkeypatch, tmp_path)
    doc = share_file(claude, tmp_path / "s.json")
    assert [r["version_status"] for r in doc["tables"]["skill_runs"]] == ["installed"]
    # The importer has the skill's git history: v1, then v2.
    repo = tmp_path / "agent-skills"
    skill = repo / "skills" / "demo"
    (skill / "playbooks").mkdir(parents=True)
    git(repo.parent, "init", "-q", str(repo))
    (skill / "SKILL.md").write_text(FRONT + BODY_V1)
    (skill / "playbooks" / "build.md").write_text(PLAYBOOK)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "v1")
    (skill / "SKILL.md").write_text(FRONT + BODY_V1 + "\nAsk first.\n")
    git(repo, "commit", "-q", "-am", "v2")
    ch = FakeClickHouse()
    args = as_importer(monkeypatch, tmp_path, ch)
    assert cli.main(args + ["--import", str(tmp_path / "s.json"), "--source", str(repo)]) == 0
    (run,) = ch.rows_of("skill_runs")
    assert run["version_status"] == "commit" and run["version_subject"] == "v1"
    assert run["version"] == git(repo, "log", "-1", "--format=%H", "HEAD~1")[: len(run["version"])]
    assert {q["version"] for q in ch.rows_of("questions")} == {run["version"]}  # every row of the run


def test_what_is_not_a_share_file_or_comes_from_a_newer_version_is_refused(tmp_path):
    bad = tmp_path / "x.json"
    bad.write_text('{"hello": 1}')
    with pytest.raises(share.ShareError, match="not a share file"):
        share.read(bad)
    doc = {"format": share.FORMAT, "generator_version": "99.0.0", "person": "a@example.com", "source": "0" * 16,
           "versions": {}, "tables": {"sessions": []}}
    share.write(doc, tmp_path / "new.json")
    with pytest.raises(share.ShareError, match="newer than this"):
        share.read(tmp_path / "new.json")
    share.write(dict(doc, generator_version=__version__), tmp_path / "ok.json.gz", compress=True)
    assert share.read(tmp_path / "ok.json.gz")["person"] == "a@example.com"  # gzipped files read as well


def test_an_import_places_each_session_and_run_on_its_dataset(sender, tmp_path, monkeypatch, capsys):
    doc = share_file(sender["claude"], tmp_path / "share.json")
    assert {r["dataset"] for r in doc["tables"]["sessions"]} == {None}  # "build revenue reporting" names none
    # as a version before the columns wrote it: no dataset at all, and a run whose arguments name the toy store
    for name in ("sessions", "skill_runs"):
        for r in doc["tables"][name]:
            del r["dataset"], r["dataset_by"]
    doc["tables"]["skill_runs"][0]["args"] = "Instance: http://toy-store2.localhost:3202, the Maven Fuzzy Factory data"
    share.write(dict(doc, generator_version="0.7.0"), tmp_path / "old.json")
    ch = FakeClickHouse()
    args = as_importer(monkeypatch, tmp_path, ch)
    capsys.readouterr()
    assert cli.main(args[:-1] + ["other", "--import", str(tmp_path / "old.json")]) == 0  # out of scope: none written
    assert "by dataset" not in capsys.readouterr().out
    assert cli.main(args + ["--import", str(tmp_path / "old.json")]) == 0
    ((s,), (r,)) = ch.rows_of("sessions"), ch.rows_of("skill_runs")
    assert (s["dataset"], s["dataset_by"]) == ("Toy Store", "runs") and (r["dataset"], r["dataset_by"]) == \
        ("Toy Store", "prompt")
    assert "shared 1 session(s) that ran demo, by dataset: Toy Store 1" in capsys.readouterr().out
