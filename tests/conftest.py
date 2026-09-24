"""A builder for synthetic transcripts that mirror Claude Code's on-disk format.

The shapes here were taken from real transcripts:
one line per content block with growing output_tokens, promptId on user events,
toolUseResult beside tool results, skill bodies as isMeta messages, subagent
files in <session>/subagents/ with a .meta.json beside them.
"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

SID = "11111111-2222-3333-4444-555555555555"
OLD_SID = "00000000-aaaa-bbbb-cccc-000000000000"
CWD = "/work/proj"


def U(inp=10, out=100, cr=1000, cw5=0, cw1=500, think=0, web=0):
    return {
        "input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
        "cache_creation_input_tokens": cw5 + cw1,
        "cache_creation": {"ephemeral_5m_input_tokens": cw5, "ephemeral_1h_input_tokens": cw1},
        "output_tokens_details": {"thinking_tokens": think},
        "server_tool_use": {"web_search_requests": web, "web_fetch_requests": 0},
        "service_tier": "standard", "speed": "standard", "inference_geo": "not_available",
    }


class Transcript:
    def __init__(self, claude_dir, session_id=SID, cwd=CWD, start=None):
        self.claude_dir = Path(claude_dir)
        self.sid = session_id
        self.cwd = cwd
        self.proj = self.claude_dir / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", cwd)
        self.proj.mkdir(parents=True, exist_ok=True)
        self.lines = []
        self.t = start or datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)
        self.n = 0

    # ------------------------------------------------------------ primitives
    def ts(self, advance=1.0):
        self.t += timedelta(seconds=advance)
        return self.t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{self.t.microsecond // 1000:03d}Z"

    def base(self, type_, advance=1.0, session_id=None, **kw):
        self.n += 1
        ev = {"parentUuid": None, "isSidechain": False, "userType": "external", "cwd": self.cwd,
              "sessionId": session_id or self.sid, "version": "2.1.280", "gitBranch": "main",
              "entrypoint": "cli", "type": type_, "uuid": f"u-{self.n}", "timestamp": self.ts(advance)}
        ev.update(kw)
        return ev

    def add(self, ev):
        self.lines.append(json.dumps(ev))
        return ev

    def raw(self, obj):
        self.lines.append(json.dumps(obj))

    def bad_line(self):
        self.lines.append("{not json")

    # ------------------------------------------------------------ events
    def prompt(self, text, prompt_id, advance=5.0, session_id=None, content=None, **kw):
        return self.add(self.base("user", advance, session_id=session_id,
                                  message={"role": "user", "content": content if content is not None else text},
                                  promptId=prompt_id, promptSource="typed", origin={"kind": "human"},
                                  permissionMode="auto", **kw))

    def assistant(self, msg_id, blocks, usage, model="claude-opus-5", stop="tool_use", advance=2.0,
                  session_id=None, **kw):
        for i, b in enumerate(blocks):
            u = dict(usage)
            last = i == len(blocks) - 1
            if not last:
                u["output_tokens"] = max(1, usage["output_tokens"] // 3)
            self.add(self.base("assistant", advance if i == 0 else 0.2, session_id=session_id,
                               message={"model": model, "id": msg_id, "type": "message", "role": "assistant",
                                        "content": [b], "stop_reason": stop if last else None,
                                        "stop_sequence": None, "usage": u},
                               requestId=f"req_{msg_id}", **kw))

    def tool_result(self, tool_id, content, prompt_id, is_error=False, result=None, advance=1.0,
                    session_id=None, **kw):
        ev = self.base("user", advance, session_id=session_id,
                       message={"role": "user", "content": [{"tool_use_id": tool_id, "type": "tool_result",
                                                             "content": content, "is_error": is_error}]},
                       promptId=prompt_id, **kw)
        if result is not None:
            ev["toolUseResult"] = result
        return self.add(ev)

    def meta(self, text, prompt_id, advance=0.1, **kw):
        return self.add(self.base("user", advance, message={"role": "user", "content": [{"type": "text", "text": text}]},
                                  isMeta=True, promptId=prompt_id, **kw))

    def system(self, subtype, advance=0.5, **kw):
        return self.add(self.base("system", advance, subtype=subtype, **kw))

    def attachment(self, att, advance=0.1):
        return self.add(self.base("attachment", advance, attachment=att))

    def write(self):
        path = self.proj / f"{self.sid}.jsonl"
        path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        return path

    def side(self):
        d = self.proj / self.sid
        d.mkdir(parents=True, exist_ok=True)
        return d


def tool_use(tid, name, **inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def text(t):
    return {"type": "text", "text": t}


def thinking(t="…"):
    return {"type": "thinking", "thinking": t, "signature": "sig"}


def build_rich_session(claude_dir):
    """One session exercising every path the analytics read. Returns the main transcript path."""
    tr = Transcript(claude_dir)

    # Inherited history: copied from an earlier session on resume (stamped with that session's id).
    tr.prompt("earlier question", "p-old", session_id=OLD_SID)
    tr.assistant("msg_old", [text("earlier answer")], U(inp=5, out=20, cr=0, cw1=100), stop="end_turn",
                 session_id=OLD_SID)

    # --- turn 0: model invokes skill `alpha`, then works under it
    tr.prompt("Please fix the failing test. token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab", "p1")
    tr.attachment({"type": "skill_listing", "content": "…", "skillCount": 4, "isInitial": True,
                   "names": ["alpha", "beta", "gamma", "workflow-authoring"]})
    tr.attachment({"type": "model", "identity": {"modelId": "claude-opus-5", "marketingName": "Opus 5"}, "text": ""})
    tr.attachment({"type": "hook_success", "hookName": "UserPromptSubmit", "hookEvent": "UserPromptSubmit",
                   "toolUseID": "x", "content": "", "stdout": "", "stderr": "", "exitCode": 0,
                   "command": "echo hi", "durationMs": 12})
    tr.assistant("msg_1", [thinking(), tool_use("tu_skill", "Skill", skill="alpha", args="fix it")],
                 U(inp=100, out=60, cr=0, cw1=20000, think=40))
    tr.tool_result("tu_skill", "Launching skill: alpha", "p1",
                   result={"success": True, "commandName": "alpha", "allowedTools": ["Bash(pytest *)"]})
    tr.meta("Base directory for this skill: /home/me/.claude/skills/alpha\n\n# Alpha\nDo the thing.", "p1",
            sourceToolUseID="tu_skill")
    tr.assistant("msg_2", [tool_use("tu_bash", "Bash", command="cd /work/proj && uv run pytest -q", description="Run tests"),
                           tool_use("tu_read", "Read", file_path="/work/proj/src/app.py")],
                 U(inp=5, out=80, cr=20000, cw1=300), attributionSkill="alpha")
    tr.tool_result("tu_bash", "Exit code 1\nFAILED tests/test_app.py::test_x", "p1", is_error=True, advance=3.0,
                   result={"stdout": "", "stderr": "FAILED", "interrupted": False, "isImage": False})
    tr.tool_result("tu_read", "1\tprint('hi')", "p1",
                   result={"type": "text", "file": {"filePath": "/work/proj/src/app.py", "content": "x",
                                                    "numLines": 10, "startLine": 1, "totalLines": 10}})
    tr.assistant("msg_3", [tool_use("tu_edit", "Edit", file_path="/work/proj/src/app.py", old_string="a",
                                    new_string="b\nc", replace_all=False)],
                 U(inp=5, out=40, cr=20300, cw1=200), attributionSkill="alpha")
    tr.tool_result("tu_edit", "The file has been updated.", "p1",
                   result={"filePath": "/work/proj/src/app.py", "oldString": "a", "newString": "b\nc",
                           "originalFile": "a\n", "structuredPatch": [{"oldStart": 1, "oldLines": 1, "newStart": 1,
                                                                        "newLines": 2, "lines": ["-a", "+b", "+c"]}],
                           "userModified": False, "replaceAll": False})
    tr.assistant("msg_4", [tool_use("tu_git", "Bash", command="git commit -m fix && git push")],
                 U(inp=5, out=30, cr=20500, cw1=100), attributionSkill="alpha")
    tr.tool_result("tu_git", "[main abc123] fix", "p1",
                   result={"stdout": "ok", "stderr": "", "interrupted": False, "isImage": False,
                           "gitOperation": {"commit": {"branch": "main", "kind": "commit", "sha": "abc1234567"},
                                            "push": {"branch": "main"}}})
    tr.assistant("msg_5", [text("Fixed.")], U(inp=5, out=20, cr=20600, cw1=50), stop="end_turn",
                 attributionSkill="alpha")
    tr.system("turn_duration", durationMs=60000, messageCount=12)

    # --- turn 1: the user types /beta; it launches an Explore subagent; the user interrupts
    tr.add(tr.base("user", 30.0, message={"role": "user", "content":
                                          "<command-message>beta</command-message>\n<command-name>/beta</command-name>\n"
                                          "<command-args>arg1</command-args>"}, promptId="p2"))
    tr.meta("Base directory for this skill: /work/proj/.claude/skills/beta\n\n# Beta", "p2")
    tr.assistant("msg_6", [tool_use("tu_agent", "Agent", subagent_type="Explore", description="look around",
                                    prompt="Find the config")],
                 U(inp=5, out=50, cr=21000, cw1=400), attributionSkill="beta")
    tr.tool_result("tu_agent", [{"type": "text", "text": "found it"}], "p2", advance=20.0,
                   result={"status": "completed", "agentId": "a1", "agentType": "Explore",
                           "resolvedModel": "claude-haiku-4-5-20251001", "totalDurationMs": 18000,
                           "totalTokens": 1300, "totalToolUseCount": 1,
                           "toolStats": {"bashCount": 0, "readCount": 0, "searchCount": 1, "editFileCount": 0,
                                         "linesAdded": 0, "linesRemoved": 0, "otherToolCount": 0}})
    tr.add(tr.base("user", 1.0, message={"role": "user", "content": [{"type": "text",
                                                                       "text": "[Request interrupted by user]"}]},
                   promptId="p2"))

    # --- turn 2: a built-in slash command
    tr.add(tr.base("user", 10.0, message={"role": "user", "content": "<command-name>/model</command-name>\n"
                                          "<command-message>model</command-message>\n<command-args></command-args>"},
                   promptId="p3"))
    tr.add(tr.base("user", 0.2, message={"role": "user", "content":
                                         "<local-command-stdout>Set model to Opus 5</local-command-stdout>"},
                   promptId="p3"))

    # --- turn 3: a denied tool, an API retry, a compaction, the harness loading a skill
    tr.prompt("Now fetch the docs", "p4", advance=40.0)
    tr.meta("<command-message>workflow-authoring</command-message>\n<command-name>workflow-authoring</command-name>",
            "p4")
    tr.meta("# Workflow authoring reference\n…", "p4")
    tr.system("api_error", level="error", error={"status": 529, "message": "Overloaded", "isNetworkDown": False},
              retryInMs=500, retryAttempt=1, maxRetries=10, source="request_retry")
    tr.assistant("msg_7", [tool_use("tu_fetch", "WebFetch", url="https://example.com/docs?token=abcdef123456",
                                    prompt="read")], U(inp=5, out=30, cr=21500, cw1=100))
    tr.tool_result("tu_fetch", "The user doesn't want to proceed with this tool use.", "p4", is_error=True,
                   toolDenialKind="user-rejected")
    tr.system("compact_boundary", level="info", content="Conversation compacted",
              compactMetadata={"trigger": "auto", "preTokens": 150000, "postTokens": 20000, "durationMs": 9000})
    tr.add(tr.base("user", 0.5, message={"role": "user", "content": "This session is being continued…"},
                   isCompactSummary=True, promptId="p4"))
    tr.assistant("msg_8", [text("Could not fetch.")], U(inp=5, out=25, cr=5000, cw5=1000, cw1=0), stop="end_turn")
    tr.system("turn_duration", durationMs=20000, messageCount=6)

    # --- bookkeeping events
    tr.raw({"type": "custom-title", "customTitle": "Fix the failing test", "sessionId": SID})
    tr.raw({"type": "pr-link", "sessionId": SID, "prNumber": 7, "prUrl": "https://github.com/o/r/pull/7",
            "prRepository": "o/r", "timestamp": tr.ts(0.1)})
    tr.raw({"type": "pr-link", "sessionId": SID, "prNumber": 7, "prUrl": "https://github.com/o/r/pull/7",
            "prRepository": "o/r", "timestamp": tr.ts(0.1)})
    tr.raw({"type": "queue-operation", "operation": "enqueue", "timestamp": tr.ts(0.1), "sessionId": SID,
            "content": "x"})
    tr.raw({"type": "mystery-event", "sessionId": SID})
    tr.bad_line()
    tr.raw({"type": "cost-state", "sessionId": SID, "totalCostUSD": 1.23, "totalAPIDuration": 50000,
            "totalAPIDurationWithoutRetries": 45000, "totalToolDuration": 9000, "totalLinesAdded": 2,
            "totalLinesRemoved": 1, "totalDuration": 200000, "startTime": 1788256800000,
            "modelUsage": {"claude-opus-5": {"inputTokens": 150, "outputTokens": 355, "cacheReadInputTokens": 150000,
                                             "cacheCreationInputTokens": 22000, "thinkingTokens": 40,
                                             "webSearchRequests": 0, "costUSD": 1.2}},
            "hasUnknownModelCost": False})
    main = tr.write()

    # --- the Explore subagent's own transcript
    sd = tr.side() / "subagents"
    sd.mkdir(parents=True, exist_ok=True)
    # Runs while turn 2's Agent call is in flight (10:01:02 → 10:01:22).
    sub = Transcript(claude_dir, start=datetime(2026, 9, 1, 10, 1, 3, tzinfo=timezone.utc))
    sub.add(sub.base("user", 1.0, message={"role": "user", "content": "Find the config"}, isSidechain=True, agentId="a1"))
    sub.assistant("msg_s1", [tool_use("tu_grep", "Grep", pattern="config", path="/work/proj")],
                  U(inp=300, out=40, cr=0, cw5=900, cw1=0), model="claude-haiku-4-5-20251001", agentId="a1",
                  isSidechain=True)
    sub.tool_result("tu_grep", "Found 2 files", "ps", result={"mode": "files_with_matches", "numFiles": 2,
                                                             "filenames": ["a", "b"]}, isSidechain=True, agentId="a1")
    sub.assistant("msg_s2", [text("found it")], U(inp=10, out=60, cr=1200, cw5=100, cw1=0),
                  model="claude-haiku-4-5-20251001", stop="end_turn", agentId="a1", isSidechain=True)
    (sd / "agent-a1.jsonl").write_text("\n".join(sub.lines) + "\n", encoding="utf-8")
    (sd / "agent-a1.meta.json").write_text(json.dumps({"agentType": "Explore", "spawnDepth": 1,
                                                       "description": "look around"}), encoding="utf-8")
    return main


@pytest.fixture
def claude_dir(tmp_path):
    d = tmp_path / "claude"
    d.mkdir()
    return d


@pytest.fixture
def rich(claude_dir):
    return build_rich_session(claude_dir)


@pytest.fixture(autouse=True)
def _private_config(tmp_path_factory, monkeypatch):
    """Every test gets its own ~/.config/convo-analysis: never the real machine lock, env file or shared-sessions cache
    (a real hook load holds the lock; flock would make the test wait on it)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("config")))
