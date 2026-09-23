"""The parser against a synthetic session whose every number is known."""

import pytest
from conftest import OLD_SID, SID

from session_analytics.parse import parse_session


def test_streamed_lines_merge_into_one_request(rich):
    s = parse_session(rich)
    real = [r for r in s.requests.values() if r.model != "<synthetic>"]
    assert len(real) == 11  # 9 main (1 inherited) + 2 subagent
    msg1 = next(r for r in real if r.message_id == "msg_1")
    assert msg1.lines == 2
    assert msg1.output_tokens == 60  # the last (largest) streamed value, not a sum
    assert msg1.cache_write_1h_tokens == 20000
    assert msg1.thinking_tokens == 40
    assert msg1.stop_reason == "tool_use"
    assert msg1.blocks == {"thinking": 1, "tool_use": 1}


def test_turns_follow_prompt_ids_and_triggers(rich):
    s = parse_session(rich)
    assert [t.trigger for t in s.turns] == ["prompt", "prompt", "command", "command", "prompt"]
    assert s.turns[0].inherited and not s.turns[1].inherited
    assert s.turns[1].reported_duration_ms == 60000 and s.turns[1].ended
    assert s.turns[2].command == "/beta" and s.turns[2].command_args == "arg1"
    assert s.turns[2].interrupted
    assert s.turns[4].compacted
    # Tool results and meta messages share their prompt's id and do not open turns.
    assert {c.turn for c in s.tool_calls.values() if c.scope == "main"} == {1, 2, 4}


def test_three_ways_a_skill_is_invoked(rich):
    s = parse_session(rich)
    by_name = {i.name: i for i in s.skills}
    assert set(by_name) == {"alpha", "beta", "workflow-authoring"}
    alpha = by_name["alpha"]
    assert (alpha.mode, alpha.via, alpha.args, alpha.success) == ("model", "Skill", "fix it", True)
    assert alpha.allowed_tools == ["Bash(pytest *)"]
    assert alpha.base_dir == "/home/me/.claude/skills/alpha"
    assert alpha.content_chars > 20
    beta = by_name["beta"]
    assert (beta.mode, beta.via, beta.args, beta.turn) == ("user", "slash", "arg1", 2)
    assert beta.base_dir == "/work/proj/.claude/skills/beta"
    assert by_name["workflow-authoring"].mode == "harness"
    # /model is a built-in command, not a skill.
    cmds = {c["name"]: c for c in s.commands}
    assert cmds["model"]["is_skill"] is False
    assert cmds["model"]["output"] == "Set model to Opus 5"


def test_tool_calls_pair_with_results(rich):
    s = parse_session(rich)
    calls = s.tool_calls
    assert len(calls) == 8
    assert calls["tu_bash"].status == "error" and calls["tu_bash"].facts["exit_code"] == 1
    assert calls["tu_fetch"].status == "denied" and calls["tu_fetch"].denial_kind == "user-rejected"
    assert calls["tu_bash"].batch_size == 2 and calls["tu_read"].batch_size == 2
    assert calls["tu_edit"].facts["added"] == 2 and calls["tu_edit"].facts["removed"] == 1
    assert calls["tu_read"].facts["num_lines"] == 10
    assert calls["tu_git"].facts["git"]["commit"]["sha"] == "abc1234567"
    assert calls["tu_agent"].facts["agent_id"] == "a1"
    assert calls["tu_bash"].attribution_skill == "alpha"
    assert calls["tu_agent"].attribution_skill == "beta"
    assert calls["tu_bash"].duration_ms == pytest.approx(3200)  # from its tool_use line to its result
    assert s.orphan_results == 0


def test_subagent_transcript_is_loaded_and_scoped(rich):
    s = parse_session(rich)
    src = next(x for x in s.sources if x.scope == "subagent")
    assert src.agent_id == "a1" and src.meta["agentType"] == "Explore"
    assert src.first_prompt == "Find the config"
    sub = [r for r in s.requests.values() if r.scope == "subagent"]
    assert {r.model for r in sub} == {"claude-haiku-4-5-20251001"}
    assert s.tool_calls["tu_grep"].scope == "subagent" and s.tool_calls["tu_grep"].turn == 2


def test_system_and_attachment_events(rich):
    s = parse_session(rich)
    assert len(s.compactions) == 1 and s.compactions[0]["pre_tokens"] == 150000
    assert s.api_errors[0]["status"] == 529
    assert len(s.hook_runs) == 1 and s.hook_runs[0]["event"] == "UserPromptSubmit"
    assert s.skill_listing == {"alpha", "beta", "gamma", "workflow-authoring"}
    assert s.model_identities == {"claude-opus-5": "Opus 5"}
    assert len(s.pr_links) == 1
    assert s.titles["custom"] == "Fix the failing test"
    assert s.cost_states[-1]["totalCostUSD"] == 1.23
    assert s.unknown_event_types == {"mystery-event": 1}
    assert sum(x.bad_lines for x in s.sources) == 1


def test_inherited_history_is_tagged_or_dropped(rich):
    s = parse_session(rich)
    assert s.lineage == {OLD_SID: 2}
    inherited = [r for r in s.requests.values() if r.inherited]
    assert [r.message_id for r in inherited] == ["msg_old"]
    own = parse_session(rich, own_only=True)
    assert own.inherited_events_skipped == 2
    assert len(own.turns) == 4 and own.turns[0].text.startswith("Please fix")
    assert not any(r.inherited for r in own.requests.values())
    assert own.session_id == SID
