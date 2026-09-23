"""Analytics over the synthetic session: exact totals, costs, attribution and views."""

import json

import pytest

from session_analytics.analyze import analyze
from session_analytics.parse import parse_session
from session_analytics.pricing import Pricing
from session_analytics.redact import Redactor

NOW = 1788300000000.0  # after the synthetic session, so it is not "live"


@pytest.fixture
def a(rich, monkeypatch):
    monkeypatch.setenv("HOME", "/home/me")  # skill source classification is relative to $HOME
    return analyze(parse_session(rich), Pricing(), redactor=Redactor(True), now_ms=NOW)


def test_totals(a):
    t = a["totals"]
    assert t["turns"] == 5 and t["prompts"] == 3
    assert t["api_requests"] == 11 and t["main_requests"] == 9
    assert t["tool_calls"] == 8 and t["tool_errors"] == 1 and t["tool_denials"] == 1
    assert t["skills_invoked"] == 3 and t["distinct_skills"] == 3 and t["slash_commands"] == 1
    assert t["subagents"] == 1
    assert (t["input_tokens"], t["output_tokens"]) == (450, 455)
    assert (t["cache_read_tokens"], t["cache_write_tokens"]) == (130100, 23250)
    assert (t["lines_added"], t["lines_removed"]) == (2, 1)
    assert (t["commits"], t["pull_requests"], t["compactions"], t["interruptions"], t["api_errors"]) == (1, 1, 1, 1, 1)
    assert json.dumps(a)  # the whole dict is JSON-serialisable


def test_cost_matches_hand_computed_list_prices(a):
    # Opus 5: $5 in / $25 out / $0.50 cache read / 1.25x (5m) and 2x (1h) input for cache writes.
    opus = 140 * 5e-6 + 355 * 25e-6 + 128900 * 0.5e-6 + 21250 * 10e-6 + 1000 * 6.25e-6
    # Haiku 4.5: $1 / $5 / $0.10 / 1.25x input.
    haiku = 310 * 1e-6 + 100 * 5e-6 + 1200 * 0.1e-6 + 1000 * 1.25e-6
    assert a["cost"]["estimated_usd"] == pytest.approx(opus + haiku, abs=1e-6)
    assert a["cost"]["by_model"]["claude-haiku-4-5-20251001"] == pytest.approx(haiku, abs=1e-6)
    assert a["cost"]["components"]["cache_write_1h"] == pytest.approx(21250 * 10e-6, abs=1e-9)
    assert a["lineage"]["inherited"]["cost_usd"] == pytest.approx(5 * 5e-6 + 20 * 25e-6 + 100 * 10e-6, abs=1e-9)


def test_skills_section(a):
    sk = a["skills"]
    modes = {i["name"]: (i["mode"], i["source"]) for i in sk["invocations"]}
    assert modes == {"alpha": ("model", "user"), "beta": ("user", "project"),
                     "workflow-authoring": ("harness", "unknown")}
    per = {p["skill"]: p for p in sk["per_skill"]}
    assert per["alpha"]["attributed_requests"] == 4 and per["alpha"]["attributed_tool_calls"] == 4
    assert per["alpha"]["tool_errors"] == 1
    assert per["alpha"]["cost_usd"] == pytest.approx(0.05155, abs=1e-6)
    assert per["beta"]["attributed_requests"] == 1 and per["beta"]["cost_usd"] == pytest.approx(0.015775, abs=1e-6)
    assert sk["unused_available"] == ["gamma"]
    assert sk["slash_commands"]["by_name"] == {"model": 1}
    assert sk["by_mode"] == {"model": 1, "user": 1, "harness": 1}


def test_tools_section(a):
    tl = a["tools"]
    by = {t["name"]: t for t in tl["by_tool"]}
    assert by["Bash"]["calls"] == 2 and by["Bash"]["error"] == 1
    assert by["Grep"]["subagent"] == 1
    assert tl["parallel_batches"]["max"] == 2
    assert tl["by_status"] == {"ok": 6, "error": 1, "denied": 1}
    assert tl["denials"] == {"user-rejected": 1}
    assert {"from": "Skill", "to": "Bash", "count": 1} in tl["transitions"]


def test_subagent_rows(a):
    row = a["subagents"]["rows"][0]
    assert (row["agent_id"], row["type"], row["launched_in_turn"]) == ("a1", "Explore", 2)
    assert row["requests"] == 2 and row["tool_calls"] == 1
    assert row["final_context_tokens"] == 10 + 1200 + 100 + 60
    assert row["reported_final_context_tokens"] == 1300


def test_files_shell_git_web(a):
    f = {r["path"]: r for r in a["files"]["rows"]}
    assert f["src/app.py"]["reads"] == 1 and f["src/app.py"]["edits"] == 1
    assert a["shell"]["primary_programs"] == {"uv": 1, "git": 1}
    assert a["shell"]["subcommands"]["git commit"] == 1
    assert a["git"]["commits"][0]["sha"] == "abc1234567"
    assert a["git"]["counts"]["pushes"] == 1
    assert a["web"]["domains"] == {"example.com": 1}


def test_errors_context_and_schema(a):
    assert a["errors"]["by_category"] == {"exit_code": 1}
    assert a["errors"]["api_error_statuses"] == {"529": 1}
    assert a["context"]["compactions"][0]["trigger"] == "auto"
    assert a["hooks"]["runs"] == 1
    assert a["schema_coverage"]["unknown"]["event_types"] == {"mystery-event": 1}
    assert a["reported"]["total_cost_usd"] == 1.23
    assert a["session"]["title"] == "Fix the failing test"
    assert a["session"]["live"] is False


def test_lineage_and_insight(a):
    lin = a["lineage"]
    assert lin["continues"][0]["session_id"].startswith("00000000")
    assert lin["inherited"]["requests"] == 1 and lin["own"]["requests"] == 10
    assert any("continues earlier session" in i["text"] for i in a["insights"])


def test_redaction_masks_secrets_in_exports(a):
    blob = json.dumps(a)
    assert "ghp_ABCDEFGHIJ" not in blob
    assert "abcdef123456" not in blob  # ?token= query value
    assert "‹redacted›" in json.dumps(a, ensure_ascii=False)


def test_unredacted_when_disabled(rich):
    a = analyze(parse_session(rich), Pricing(), redactor=Redactor(False), now_ms=NOW)
    assert "ghp_ABCDEFGHIJ" in json.dumps(a)


def test_own_only_analysis(rich):
    a = analyze(parse_session(rich, own_only=True), Pricing(), now_ms=NOW)
    assert a["totals"]["turns"] == 4 and a["lineage"]["inherited"]["requests"] == 0
    assert a["lineage"]["inherited_events_skipped"] == 2
