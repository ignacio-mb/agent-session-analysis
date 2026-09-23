"""End to end: locating sessions, the export files, the rollup, the schema scan."""

import json
import re
from datetime import datetime, timezone

import pytest
from conftest import SID, Transcript, U, text

from session_analytics import cli, locate
from session_analytics.rollup import run_rollup
from session_analytics.schema_scan import scan


def test_resolve_references(rich, claude_dir):
    assert locate.resolve(SID, cdir=claude_dir) == rich
    assert locate.resolve(SID[:8], cdir=claude_dir) == rich
    assert locate.resolve("latest", cdir=claude_dir, cwd="/work/proj") == rich
    assert locate.resolve("current", cdir=claude_dir, current=SID) == rich
    assert locate.resolve(str(rich), cdir=claude_dir) == rich
    with pytest.raises(locate.SessionNotFound):
        locate.resolve("ffffffff", cdir=claude_dir)


def test_claude_config_dir_env(rich, claude_dir, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    assert locate.resolve(SID[:8]) == rich


def test_export_writes_every_format(rich, claude_dir, tmp_path, capsys):
    out = tmp_path / "out"
    code = cli.main(["export", SID, "--claude-dir", str(claude_dir), "--out", str(out)])
    assert code == 0
    printed = capsys.readouterr().out
    assert "alpha" in printed and "by Claude (Skill tool)" in printed and "by you (/slash)" in printed
    for name in ("report.html", "report.md", "session.json", "summary.md"):
        assert (out / name).is_file(), name
    for name in ("turns", "requests", "tool_calls", "skills", "files", "subagents", "errors"):
        assert (out / "csv" / f"{name}.csv").is_file(), name
    data = json.loads((out / "session.json").read_text())
    assert data["schema"] == "convo-analysis/v1"
    assert (out / "csv" / "tool_calls.csv").read_text().count("\n") == 9  # header + 8 calls


def test_html_embeds_data_safely(rich, claude_dir, tmp_path):
    out = tmp_path / "out"
    cli.main(["export", SID, "--claude-dir", str(claude_dir), "--out", str(out), "--format", "html", "--quiet"])
    page = (out / "report.html").read_text()
    m = re.search(r'<script id="session-data" type="application/json">(.*?)</script>', page, re.S)
    assert m, "data block missing"
    blob = m.group(1)
    assert "<" not in blob and ">" not in blob  # escaped, so no tag can close the block early
    data = json.loads(blob)
    assert data["session"]["id"] == SID
    assert not re.search(r"\{\{(CSS|LIB|APP|DATA|TITLE|VERSION)\}\}", page)  # every placeholder filled


def test_json_output_and_own_only_flag(rich, claude_dir, tmp_path, capsys):
    cli.main(["export", SID, "--claude-dir", str(claude_dir), "--out", str(tmp_path / "o"), "--json",
              "--own-only", "--format", "json"])
    res = json.loads(capsys.readouterr().out)
    assert res["totals"]["turns"] == 4
    assert res["paths"]["json"].endswith("session.json")


def test_rollup_counts_copied_history_once(claude_dir, tmp_path):
    a_id, b_id = "aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002"
    start = datetime(2026, 9, 1, 9, 0, 0, tzinfo=timezone.utc)
    a = Transcript(claude_dir, session_id=a_id, start=start)
    a.prompt("original", "pa")
    a.assistant("msg_a", [text("answer")], U(inp=10, out=100, cr=0, cw1=1000), stop="end_turn")
    a.write()
    b = Transcript(claude_dir, session_id=b_id, start=start)
    b.lines = list(a.lines)  # resumed: B starts with A's lines, still stamped with A's id
    b.t = a.t
    b.prompt("continue", "pb")
    b.assistant("msg_b", [text("more")], U(inp=10, out=50, cr=1000, cw1=100), stop="end_turn")
    b.write()
    res = run_rollup(claude_dir=claude_dir, project=None, since="all", out_dir=tmp_path / "r",
                     formats=["json", "md", "html", "csv"], now_ms=datetime(2026, 9, 2, tzinfo=timezone.utc).timestamp() * 1000)
    t = res["rollup"]["totals"]
    assert t["requests"] == 2 and t["duplicate_requests_removed"] == 1
    assert t["output_tokens"] == 150
    rows = {r["id"]: r for r in res["rollup"]["sessions"]}
    assert rows[a_id]["window_requests"] == 1 and rows[b_id]["window_requests"] == 1
    assert (tmp_path / "r" / "rollup.html").is_file() and (tmp_path / "r" / "csv" / "sessions.csv").is_file()


def test_rollup_window_excludes_old_activity(claude_dir, tmp_path):
    old = Transcript(claude_dir, session_id="cccccccc-0000-0000-0000-000000000003",
                     start=datetime(2026, 1, 1, tzinfo=timezone.utc))
    old.prompt("old", "po")
    old.assistant("msg_c", [text("x")], U(), stop="end_turn")
    old.write()
    res = run_rollup(claude_dir=claude_dir, since="2026-06-01", out_dir=tmp_path / "r", formats=["json"],
                     now_ms=datetime(2026, 9, 2, tzinfo=timezone.utc).timestamp() * 1000)
    assert res["rollup"]["totals"].get("requests", 0) == 0


def test_schema_scan_flags_unknown_types(rich):
    report = scan([rich])
    assert report["unknown"]["event_types"] == {"mystery-event": 1}
    assert report["bad_lines"] == 1
    assert report["tool_names"]["Bash"] == 2


def test_list_and_pricing_commands(rich, claude_dir, capsys):
    assert cli.main(["list", "--all", "--claude-dir", str(claude_dir), "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["id"] == SID and rows[0]["title"] == "Fix the failing test"
    assert cli.main(["pricing"]) == 0
    assert "claude-opus-5" in capsys.readouterr().out
