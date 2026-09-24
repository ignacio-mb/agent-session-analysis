"""Turn a Claude Code session on disk into records the analytics can count.

A session is the main transcript plus everything in its side directory:
subagent transcripts (with .meta.json), workflow agent transcripts and
journals, workflow run summaries, and persisted tool outputs.

The JSONL format is internal to Claude Code and changes between releases, so
every handler here is defensive: an unknown event type, attachment type or
system subtype is counted in `schema` and never raises. Facts worth knowing
about the format (all verified against real transcripts):

* One API response is written as several lines, one per content block, sharing
  `message.id` and `requestId`. Only `output_tokens` grows across those lines
  (the last is final), so usage is merged with max() per field.
* `promptId` on user events groups a turn: the prompt, its tool results and the
  meta messages injected for it all share one.
* A skill is invoked three ways, recorded differently:
    - by the model: `Skill` tool_use -> "Launching skill" result -> an isMeta
      user message with `sourceToolUseID` carrying the SKILL.md body;
    - by the user: a `<command-name>/x</command-name>` message -> isMeta body;
    - by the harness: an isMeta `<command-name>x</command-name>` (no slash).
  Assistant lines also carry `attributionSkill`, which attributes the model's
  activity to the skill it is executing.
* `toolUseResult` on the user line holds the structured result of a tool call
  (structuredPatch for edits, gitOperation for git commands, agent totals...).
"""

from __future__ import annotations

import bisect
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import util
from .locate import side_dir

KNOWN_EVENT_TYPES = frozenset({
    "user", "assistant", "attachment", "system", "summary", "custom-title", "ai-title", "agent-name",
    "last-prompt", "pr-link", "bridge-session", "queue-operation", "mode", "permission-mode",
    "worktree-state", "file-history-snapshot", "file-history-delta", "relocated", "frame-link",
    "cost-state", "artifact-autoreact-ledger", "artifact-comment-monitor", "continued-in", "atis-latch",
    "progress", "tag", "started", "result", "launched", "failed",
})
KNOWN_SYSTEM_SUBTYPES = frozenset({
    "stop_hook_summary", "api_error", "turn_duration", "away_summary", "compact_boundary",
    "local_command", "informational", "model_refusal_fallback", "microcompact_boundary",
})
KNOWN_ATTACHMENT_TYPES = frozenset({
    "total_tokens_reminder", "batching_reminder_sent", "skill_listing", "prompt_snapshot",
    "deferred_tools_delta", "remote_session_change", "mcp_instructions_delta", "date", "session_context",
    "edited_text_file", "environment", "structured_output", "instructions", "model", "task_reminder",
    "auto_mode", "queued_command", "agent_listing_delta", "nested_memory", "ultra_effort_enter", "file",
    "deferred_tools_record", "hook_success", "plan_mode", "command_permissions", "dynamic_skill",
    "read_truncation_notice", "compact_file_reference", "silent_turn_reminder", "plan_mode_exit",
    "directory", "date_change", "plan_file_reference", "opened_file_in_ide", "hook_additional_context",
    "thinking_stripped", "selected_lines_in_ide", "hook_system_message", "auto_mode_exit",
    "ultra_effort_exit", "invoked_skills", "thinking_drop", "hook_error", "hook_blocking_error",
    "hook_non_blocking_error", "hook_cancelled", "todo_reminder", "todo",
})

# Slash commands that are part of Claude Code itself rather than skills. Only a
# fallback: a command followed by an injected skill body is a skill regardless.
BUILTIN_COMMANDS = frozenset({
    "add-dir", "agents", "bashes", "bug", "clear", "compact", "config", "context", "cost", "doctor",
    "effort", "exit", "export", "extra-usage", "fast", "feedback", "help", "hooks", "ide", "insights",
    "install-github-app", "install-slack-app", "login", "logout", "mcp", "memory", "migrate-installer",
    "model", "output-style", "permissions", "plan", "plugin", "plugins", "pr-comments", "privacy-settings",
    "release-notes", "remote-control", "rename", "resume", "rewind", "sandbox", "stats", "status",
    "statusline", "tasks", "terminal-setup", "theme", "todos", "upgrade", "usage", "vim",
    "auto-mode-setup", "continue", "keybindings", "workflows", "artifacts", "chrome", "voice",
})

TOOL_CATEGORIES = {
    "Read": "files", "NotebookRead": "files", "Edit": "edits", "MultiEdit": "edits", "Write": "edits",
    "NotebookEdit": "edits", "Glob": "search", "Grep": "search", "LS": "search",
    "Bash": "shell", "BashOutput": "shell", "KillShell": "shell", "KillBash": "shell", "Monitor": "shell",
    "TaskOutput": "shell", "TaskStop": "shell",
    "WebFetch": "web", "WebSearch": "web",
    "Agent": "agents", "Task": "agents", "Workflow": "agents", "SendMessage": "agents", "ListAgents": "agents",
    "Skill": "skills", "SlashCommand": "skills", "ToolSearch": "skills",
    "TodoWrite": "planning", "TaskCreate": "planning", "TaskUpdate": "planning", "TaskList": "planning",
    "TaskGet": "planning", "EnterPlanMode": "planning", "ExitPlanMode": "planning",
    "AskUserQuestion": "planning",
    "SendUserFile": "outputs", "Artifact": "outputs", "ArtifactComments": "outputs", "ArtifactData": "outputs",
    "ReportFindings": "outputs", "StructuredOutput": "outputs", "PushNotification": "outputs",
    "EnterWorktree": "workspace", "ExitWorktree": "workspace", "CronCreate": "workspace",
    "CronDelete": "workspace", "CronList": "workspace", "ScheduleWakeup": "workspace",
    "RemoteTrigger": "workspace",
}

TURN_STARTERS = {"prompt": "prompt", "command": "command", "task_notification": "task_notification",
                 "bash_input": "bash"}

COMMAND_NAME_RE = re.compile(r"<command-name>\s*([^<]*?)\s*</command-name>")
COMMAND_ARGS_RE = re.compile(r"<command-args>([\s\S]*?)</command-args>")
LOCAL_STDOUT_RE = re.compile(r"<local-command-(?:stdout|stderr)>([\s\S]*?)</local-command-(?:stdout|stderr)>")
BASE_DIR_RE = re.compile(r"Base directory for this skill:\s*(\S+)")
BASE_DIR_LINE_RE = re.compile(r"^\s*Base directory for this skill: [^\n]*\n+")
TOKENS_LEFT_RE = re.compile(r"(\d[\d,]*)\s+tokens?\s+left")
EXIT_CODE_RE = re.compile(r"^Exit code (-?\d+)")


def tool_category(name):
    if name.startswith("mcp__"):
        return "mcp"
    return TOOL_CATEGORIES.get(name, "other")


def mcp_parts(name):
    """mcp__<server>__<tool> -> (server, tool); servers may contain underscores."""
    if not name.startswith("mcp__"):
        return None, None
    rest = name[5:]
    if "__" in rest:
        server, tool = rest.split("__", 1)
        return server, tool
    return rest, ""


@dataclass
class SourceFile:
    path: str
    scope: str                      # main | subagent | workflow
    agent_id: str = None
    workflow_run: str = None
    meta: dict = field(default_factory=dict)
    lines: int = 0
    bad_lines: int = 0
    size: int = 0
    first_ts: float = None
    last_ts: float = None
    first_prompt: str = None
    prompts: int = 0


@dataclass
class Request:
    """One API response, merged across the lines it was streamed into."""
    key: str
    message_id: str
    request_id: str
    scope: str
    agent_id: str
    source: int
    model: str = ""
    ts_start: float = None          # previous event in the file: when the request was sent
    ts_first: float = None          # first content block written
    ts_last: float = None           # last content block written
    lines: int = 0
    stop_reason: str = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    thinking_tokens: int = 0
    web_search_requests: int = 0
    web_fetch_requests: int = 0
    service_tier: str = None
    speed: str = None
    inference_geo: str = None
    effort: str = None
    per_turn_effort: str = None
    attribution_skill: str = None
    attribution_agent: str = None
    attribution_plugin: str = None
    attribution_mcp_server: str = None
    attribution_mcp_tool: str = None
    advisor_model: str = None
    blocks: Counter = field(default_factory=Counter)
    text_chars: int = 0
    thinking_chars: int = 0
    text_preview: str = ""          # the start of what the model said in this response
    text_full: str = ""             # all of it (capped), for the questions it asks in prose
    tool_use_ids: list = field(default_factory=list)
    cache_miss_reason: str = None
    cache_missed_tokens: int = 0
    is_api_error: bool = False
    api_error_status: int = None
    error_text: str = None
    quota: dict = None
    refusal: dict = None
    context_edits: int = 0
    iterations: int = 0
    turn: int = None
    inherited: bool = False         # copied from an earlier session on resume/continue
    cost: dict = None

    @property
    def context_tokens(self):
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def total_tokens(self):
        return self.context_tokens + self.output_tokens

    @property
    def latency_ms(self):
        if self.ts_start is None or self.ts_first is None:
            return None
        return max(0.0, self.ts_first - self.ts_start)

    @property
    def duration_ms(self):
        if self.ts_start is None or self.ts_last is None:
            return None
        return max(0.0, self.ts_last - self.ts_start)

    def absorb_usage(self, u):
        if not isinstance(u, dict):
            return
        self.input_tokens = max(self.input_tokens, _int(u.get("input_tokens")))
        self.output_tokens = max(self.output_tokens, _int(u.get("output_tokens")))
        self.cache_read_tokens = max(self.cache_read_tokens, _int(u.get("cache_read_input_tokens")))
        self.cache_write_tokens = max(self.cache_write_tokens, _int(u.get("cache_creation_input_tokens")))
        cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
        self.cache_write_5m_tokens = max(self.cache_write_5m_tokens, _int(cc.get("ephemeral_5m_input_tokens")))
        self.cache_write_1h_tokens = max(self.cache_write_1h_tokens, _int(cc.get("ephemeral_1h_input_tokens")))
        otd = u.get("output_tokens_details") if isinstance(u.get("output_tokens_details"), dict) else {}
        self.thinking_tokens = max(self.thinking_tokens, _int(otd.get("thinking_tokens")))
        stu = u.get("server_tool_use") if isinstance(u.get("server_tool_use"), dict) else {}
        self.web_search_requests = max(self.web_search_requests, _int(stu.get("web_search_requests")))
        self.web_fetch_requests = max(self.web_fetch_requests, _int(stu.get("web_fetch_requests")))
        for attr in ("service_tier", "speed", "inference_geo"):
            if u.get(attr):
                setattr(self, attr, u[attr])
        if isinstance(u.get("iterations"), list):
            self.iterations = max(self.iterations, len(u["iterations"]))


TEXT_CAP = 60_000

# Tools whose whole output is kept: skillfiles matches it line by line against skill documents.
OUTPUT_KEPT = {"Bash", "Grep", "Glob"}
OUTPUT_CAP = 200_000


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict
    scope: str
    agent_id: str
    source: int
    request_key: str
    message_id: str
    ts_call: float
    turn: int = None
    ts_result: float = None
    status: str = "pending"         # ok | error | denied | interrupted | pending
    is_error: bool = False
    denial_kind: str = None
    result_chars: int = 0
    result_images: int = 0
    result_preview: str = ""
    facts: dict = field(default_factory=dict)
    batch_size: int = 1
    batch_index: int = 0
    attribution_skill: str = None
    attribution_agent: str = None
    inherited: bool = False
    cwd: str = None                 # the shell's working directory when the call was made
    output: str = None              # full result text of shell and search calls, for skillfiles

    @property
    def duration_ms(self):
        if self.ts_call is None or self.ts_result is None:
            return None
        return max(0.0, self.ts_result - self.ts_call)

    @property
    def category(self):
        return tool_category(self.name)


@dataclass
class Turn:
    index: int
    prompt_id: str
    trigger: str                    # prompt | command | task_notification | bash
    ts_start: float
    ts_end: float = None
    text: str = ""
    prompt_source: str = None
    origin: str = None
    permission_mode: str = None
    images: int = 0
    command: str = None
    command_args: str = None
    reported_duration_ms: float = None
    reported_message_count: int = None
    interrupted: bool = False
    compacted: bool = False
    queued_absorbed: int = 0
    ended: bool = False
    inherited: bool = False


@dataclass
class SkillInvocation:
    name: str
    mode: str                       # model | user | harness
    ts: float
    turn: int
    scope: str
    agent_id: str
    args: str = None
    tool_use_id: str = None
    via: str = None                 # Skill | SlashCommand | slash | harness
    success: bool = None
    error: str = None
    canonical: str = None
    allowed_tools: list = None
    base_dir: str = None
    content_chars: int = None
    forked_agent_id: str = None
    status: str = None
    inherited: bool = False
    fingerprint: str = None         # hash of the injected SKILL.md body, normalised; identifies the version


def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


class _FileCtx:
    __slots__ = ("idx", "src", "last_ts", "pending_command")

    def __init__(self, idx, src):
        self.idx = idx
        self.src = src
        self.last_ts = None
        self.pending_command = None

    def main(self, ev):
        return self.src.scope == "main" and not ev.get("isSidechain")

    def scope(self, ev):
        if self.src.scope == "main" and ev.get("isSidechain"):
            return "sidechain"
        return self.src.scope

    def agent(self, ev):
        return self.src.agent_id or ev.get("agentId")


class Session:
    """Everything recorded about one session, as records. Build with `parse_session()`."""

    def __init__(self, main_path, own_only=False):
        self.main_path = Path(main_path)
        self.session_id = self.main_path.stem
        # A resumed/continued transcript starts with a copy of the earlier session's history, each line
        # still stamped with that session's id. `own_only` drops those lines; otherwise they are tagged.
        self.own_only = own_only
        self.lineage = Counter()
        self.lineage_span = {}
        self.inherited_events_skipped = 0
        self._inherited = False
        self.project_dir = self.main_path.parent.name
        self.sources = []
        self.requests = {}
        self.tool_calls = {}
        self.turns = []
        self.skills = []
        self.commands = []
        self._skill_by_tool = {}
        self._turn = None

        self.compactions = []
        self.api_errors = []
        self.hook_runs = []
        self.stop_hooks = []
        self.notices = []
        self.away_summaries = []
        self.refusal_fallbacks = []
        self.interrupts = []
        self.queued_commands = []
        self.queue_ops = Counter()
        self.queue_reasons = Counter()

        self.titles = {}
        self.pr_links = {}
        self.permission_modes = []
        self.modes = []
        self.worktree = None
        self.relocated = []
        self.continued_in = []
        self.bridge_events = 0
        self.frame_links = []
        self.cost_states = []
        self.artifact_monitor_events = 0

        self.file_snapshots = 0
        self.file_deltas = 0
        self.tracked_files = {}

        self.event_types = Counter()
        self.system_subtypes = Counter()
        self.attachments = Counter()
        self.attachments_by_scope = defaultdict(Counter)
        self.progress_types = Counter()
        self.unknown_event_types = Counter()
        self.unknown_system_subtypes = Counter()
        self.unknown_attachment_types = Counter()

        self.skill_listing = set()
        self.skill_listing_count = 0
        self.dynamic_skills = []
        self.restored_skills = []
        self.deferred_added = Counter()
        self.deferred_removed = Counter()
        self.mcp_pending = set()
        self.mcp_failed = set()
        self.mcp_needs_auth = set()
        self.mcp_instruction_servers = set()
        self.agent_types_listed = set()
        self.instruction_files = {}
        self.memory_files = Counter()
        self.mentioned_files = Counter()
        self.user_edited_files = Counter()
        self.ide_files = Counter()
        self.ide_selections = 0
        self.environment = None
        self.environment_changes = []
        self.has_git_status = False
        self.model_identities = {}
        self.prompt_snapshots = []
        self.tokens_left = []
        self.plan_mode = Counter()
        self.plan_files = set()
        self.auto_mode = Counter()
        self.ultra_effort = Counter()
        self.command_permissions = []
        self.misc = Counter()

        self.versions = Counter()
        self.entrypoints = Counter()
        self.cwds = Counter()
        self.branches = {}
        self.user_types = Counter()
        self.slugs = Counter()
        self.first_ts = None
        self.last_ts = None
        self.images_pasted = 0
        self.local_command_outputs = 0
        self.bash_mode_inputs = 0
        self.task_notifications = 0
        self.meta_messages = 0
        self.orphan_results = 0

        self.workflow_runs = {}
        self.workflow_agents = defaultdict(dict)
        self.persisted_outputs = {"count": 0, "bytes": 0}

        self._handlers = {
            "user": self._on_user,
            "assistant": self._on_assistant,
            "attachment": self._on_attachment,
            "system": self._on_system,
            "summary": lambda ev, ts, ctx: self._title("summary", ev.get("summary")),
            "custom-title": lambda ev, ts, ctx: self._title("custom", ev.get("customTitle")),
            "ai-title": lambda ev, ts, ctx: self._title("ai", ev.get("aiTitle")),
            "agent-name": lambda ev, ts, ctx: self._title("agent_name", ev.get("agentName")),
            "last-prompt": lambda ev, ts, ctx: self._title("last_prompt", ev.get("lastPrompt")),
            "pr-link": self._on_pr_link,
            "bridge-session": self._on_bridge,
            "queue-operation": self._on_queue_op,
            "mode": lambda ev, ts, ctx: self._mode_change(self.modes, ts or ctx.last_ts, ev.get("mode")),
            "permission-mode": lambda ev, ts, ctx: self._mode_change(
                self.permission_modes, ts or ctx.last_ts, ev.get("permissionMode")),
            "worktree-state": self._on_worktree,
            "file-history-snapshot": self._on_file_snapshot,
            "file-history-delta": self._on_file_delta,
            "relocated": lambda ev, ts, ctx: self.relocated.append(ev.get("relocatedCwd")),
            "frame-link": self._on_frame_link,
            "cost-state": lambda ev, ts, ctx: self.cost_states.append(ev),
            "continued-in": lambda ev, ts, ctx: self.continued_in.append(ev.get("continuedInSessionId")),
            "artifact-autoreact-ledger": self._on_artifact_monitor,
            "artifact-comment-monitor": self._on_artifact_monitor,
            "progress": lambda ev, ts, ctx: self.progress_types.update(
                [(ev.get("data") or {}).get("type") or "<none>"]),
        }

    # ------------------------------------------------------------------ loading

    def load(self):
        self._load_file(self.main_path, "main")
        sd = side_dir(self.main_path)
        if sd.is_dir():
            self._load_side_dir(sd)
        self._finalize()
        return self

    def _load_side_dir(self, sd):
        title_file = sd / "custom-title.json"
        if title_file.is_file():
            try:
                self._title("custom", json.loads(title_file.read_text(encoding="utf-8")).get("customTitle"))
            except (ValueError, OSError, AttributeError):
                pass
        for f in sorted(sd.rglob("*.jsonl")):
            rel = f.relative_to(sd).parts
            run = next((p for p in rel if p.startswith("wf_")), None)
            if f.name == "journal.jsonl":
                self._load_journal(f, run)
                continue
            scope = "workflow" if "workflows" in rel else "subagent"
            agent_id = f.stem[len("agent-"):] if f.stem.startswith("agent-") else f.stem
            meta = {}
            meta_file = f.with_name(f.stem + ".meta.json")
            if meta_file.is_file():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    meta = {}
            self._load_file(f, scope, agent_id=agent_id, run=run, meta=meta if isinstance(meta, dict) else {})
        wf_dir = sd / "workflows"
        if wf_dir.is_dir():
            for wf in sorted(wf_dir.glob("*.json")):
                try:
                    d = json.loads(wf.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue
                if isinstance(d, dict):
                    self._on_workflow_summary(wf.stem, d)
        tr = sd / "tool-results"
        if tr.is_dir():
            for f in tr.iterdir():
                if f.is_file():
                    self.persisted_outputs["count"] += 1
                    self.persisted_outputs["bytes"] += f.stat().st_size

    def _load_file(self, path, scope, agent_id=None, run=None, meta=None):
        src = SourceFile(path=str(path), scope=scope, agent_id=agent_id, workflow_run=run, meta=meta or {})
        try:
            src.size = Path(path).stat().st_size
        except OSError:
            pass
        ctx = _FileCtx(len(self.sources), src)
        self.sources.append(src)
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                if not raw.strip():
                    continue
                src.lines += 1
                try:
                    ev = json.loads(raw)
                except ValueError:
                    src.bad_lines += 1
                    continue
                if not isinstance(ev, dict):
                    src.bad_lines += 1
                    continue
                self._dispatch(ev, ctx)

    def _load_journal(self, path, run):
        agents = self.workflow_agents[run or path.parent.name]
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                aid = ev.get("agentId")
                if not aid:
                    continue
                a = agents.setdefault(aid, {"agent_id": aid, "key": ev.get("key"), "label": None,
                                            "phase": None, "status": "started"})
                if ev.get("type") == "started":
                    a["label"] = ev.get("label") or a["label"]
                    a["phase"] = ev.get("phase") or a["phase"]
                elif ev.get("type") == "result":
                    a["status"] = "completed"
                elif ev.get("type") == "failed":
                    a["status"] = "failed"

    def _on_workflow_summary(self, stem, d):
        start = d.get("startTime")
        self.workflow_runs[stem] = {
            "run_id": d.get("runId") or stem,
            "name": d.get("workflowName"),
            "status": d.get("status"),
            "agent_count": d.get("agentCount"),
            "duration_ms": d.get("durationMs"),
            "total_tokens": d.get("totalTokens"),
            "total_tool_calls": d.get("totalToolCalls"),
            "default_model": d.get("defaultModel"),
            "start_ms": float(start) if isinstance(start, (int, float)) else util.parse_ts(d.get("timestamp")),
            "phases": [p.get("title") for p in (d.get("phases") or []) if isinstance(p, dict)],
            "summary": d.get("summary"),
            "log_lines": len(d.get("logs") or []),
            "task_id": d.get("taskId"),
            "script_path": d.get("scriptPath"),
        }

    # ------------------------------------------------------------------ dispatch

    def _dispatch(self, ev, ctx):
        t = ev.get("type") or "<none>"
        ts = util.parse_ts(ev.get("timestamp"))
        sid = ev.get("sessionId")
        self._inherited = bool(ctx.src.scope == "main" and sid and sid != self.session_id
                               and t in ("user", "assistant", "system", "attachment"))
        if self._inherited:
            self.lineage[sid] += 1
            span = self.lineage_span.setdefault(sid, [ts, ts])
            if ts is not None:
                span[0] = ts if span[0] is None else min(span[0], ts)
                span[1] = ts if span[1] is None else max(span[1], ts)
            if self.own_only:
                self.inherited_events_skipped += 1
                return
        self.event_types[(ctx.src.scope, t)] += 1
        if ts is not None:
            src = ctx.src
            src.first_ts = ts if src.first_ts is None else min(src.first_ts, ts)
            src.last_ts = ts if src.last_ts is None else max(src.last_ts, ts)
            if src.scope == "main":
                self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
                self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)
        if ctx.src.scope == "main":
            if ev.get("version"):
                self.versions[ev["version"]] += 1
            if ev.get("entrypoint"):
                self.entrypoints[ev["entrypoint"]] += 1
            if ev.get("cwd"):
                self.cwds[ev["cwd"]] += 1
            if ev.get("userType"):
                self.user_types[ev["userType"]] += 1
            if ev.get("slug"):
                self.slugs[ev["slug"]] += 1
            branch = ev.get("gitBranch")
            if branch and branch not in self.branches:
                self.branches[branch] = ts
        handler = self._handlers.get(t)
        if handler is not None:
            handler(ev, ts, ctx)
        elif t not in KNOWN_EVENT_TYPES:
            self.unknown_event_types[t] += 1
        if ts is not None:
            ctx.last_ts = ts

    # ------------------------------------------------------------------ turns

    def _touch_turn(self, ts):
        t = self._turn
        if t is not None and ts is not None:
            t.ts_end = ts if t.ts_end is None else max(t.ts_end, ts)

    def _maybe_new_turn(self, ev, ts, kind, text, images):
        pid = ev.get("promptId")
        cur = self._turn
        if cur is not None and pid is not None and pid == cur.prompt_id:
            self._touch_turn(ts)
            return
        origin = ev.get("origin")
        t = Turn(
            index=len(self.turns), prompt_id=pid, trigger=TURN_STARTERS[kind], ts_start=ts, ts_end=ts,
            text=util.clean_prompt(text) if kind == "prompt" else text, prompt_source=ev.get("promptSource"),
            origin=origin.get("kind") if isinstance(origin, dict) else None,
            permission_mode=ev.get("permissionMode"), images=images, inherited=self._inherited,
        )
        if kind == "command":
            name, args = _command_parts(text)
            t.command, t.command_args = name, args
        self.turns.append(t)
        self._turn = t

    def _current_turn(self, ctx, ev):
        if ctx.main(ev) and self._turn is not None:
            return self._turn.index
        return None

    # ------------------------------------------------------------------ user

    @staticmethod
    def _user_kind(ev, text):
        if ev.get("isCompactSummary"):
            return "compact_summary"
        s = text.lstrip()
        if s.startswith("[Request interrupted by user"):
            return "interrupt"
        if s.startswith("<command-") and "<command-name>" in s:
            return "harness_command" if ev.get("isMeta") else "command"
        if s.startswith("<local-command-"):
            return "local_command_output"
        if s.startswith("<bash-input>"):
            return "bash_input"
        if s.startswith("<bash-stdout>") or s.startswith("<bash-stderr>"):
            return "bash_output"
        if s.startswith("<task-notification>"):
            return "task_notification"
        if ev.get("isMeta") or s.startswith("Caveat: The messages below were generated"):
            return "meta"
        return "prompt"

    def _on_user(self, ev, ts, ctx):
        msg = ev.get("message") or {}
        content = msg.get("content")
        blocks = content if isinstance(content, list) else []
        main = ctx.main(ev)
        results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
        if results:
            tur = ev.get("toolUseResult")
            for b in results:
                self._on_tool_result(b, ev, ts, ctx, tur if len(results) == 1 else None)
            if main:
                self._touch_turn(ts)
            return

        text = util.text_of(content)
        images = sum(1 for b in blocks if isinstance(b, dict) and b.get("type") == "image")
        kind = self._user_kind(ev, text)

        if main:
            if kind in TURN_STARTERS:
                self._maybe_new_turn(ev, ts, kind, text, images)
            else:
                self._touch_turn(ts)
        elif kind == "prompt":
            ctx.src.prompts += 1
            if ctx.src.first_prompt is None:
                ctx.src.first_prompt = text
            return
        turn = self._current_turn(ctx, ev)

        if kind == "prompt":
            self.images_pasted += images
        elif kind in ("command", "harness_command"):
            self._on_command_text(ts, ctx, text, kind, turn)
        elif kind == "meta":
            self._on_meta_text(ev, ts, ctx, text, turn)
        elif kind == "local_command_output":
            self.local_command_outputs += 1
            if ctx.pending_command is not None:
                m = LOCAL_STDOUT_RE.search(text)
                ctx.pending_command["output"] = (m.group(1) if m else text).strip()
                ctx.pending_command["is_skill"] = False
                ctx.pending_command = None
        elif kind == "interrupt":
            self.interrupts.append({"ts": ts, "turn": turn, "scope": ctx.scope(ev),
                                    "for_tool_use": "for tool use" in text})
            if main and self._turn is not None:
                self._turn.interrupted = True
        elif kind == "bash_input":
            self.bash_mode_inputs += 1
        elif kind == "task_notification":
            self.task_notifications += 1
        elif kind == "compact_summary":
            if main and self._turn is not None:
                self._turn.compacted = True

    def _on_command_text(self, ts, ctx, text, kind, turn):
        name, args = _command_parts(text)
        rec = {
            "name": name.lstrip("/"), "raw": name, "args": args, "ts": ts, "turn": turn,
            "scope": ctx.src.scope, "agent_id": ctx.src.agent_id,
            "invoked_by": "user" if kind == "command" else "harness",
            "is_skill": None, "output": None, "skill_index": None, "inherited": self._inherited,
        }
        self.commands.append(rec)
        ctx.pending_command = rec

    def _on_meta_text(self, ev, ts, ctx, text, turn):
        self.meta_messages += 1
        base = BASE_DIR_RE.search(text)
        src_tool = ev.get("sourceToolUseID")
        if src_tool and src_tool in self._skill_by_tool:
            inv = self._skill_by_tool[src_tool]
            inv.content_chars = len(text)
            if base:
                inv.base_dir = base.group(1)
            inv.fingerprint = skill_fingerprint(text, inv.base_dir, self.session_id, inv.args)
            return
        pending = ctx.pending_command
        if pending is not None and not text.lstrip().startswith(("<local-command", "<command-")):
            self._promote(pending, text, base.group(1) if base else None)
            ctx.pending_command = None

    def _promote(self, cmd, text, base_dir):
        inv = SkillInvocation(
            name=cmd["name"], mode=cmd["invoked_by"], ts=cmd["ts"], turn=cmd["turn"], scope=cmd["scope"],
            agent_id=cmd["agent_id"], args=cmd["args"] or None,
            via="slash" if cmd["invoked_by"] == "user" else "harness",
            success=True, base_dir=base_dir, content_chars=len(text) if text is not None else None,
            status="ok", inherited=bool(cmd.get("inherited")),
            fingerprint=skill_fingerprint(text, base_dir, self.session_id, cmd["args"]) if text else None,
        )
        self.skills.append(inv)
        cmd["is_skill"] = True
        cmd["skill_index"] = len(self.skills) - 1

    # ------------------------------------------------------------------ assistant

    def _on_assistant(self, ev, ts, ctx):
        msg = ev.get("message") or {}
        mid = msg.get("id") or ev.get("uuid") or f"{ctx.idx}:{ctx.src.lines}"
        rid = ev.get("requestId")
        key = f"{mid}|{rid or ''}"
        main = ctx.main(ev)
        req = self.requests.get(key)
        if req is None:
            req = Request(key=key, message_id=mid, request_id=rid, scope=ctx.scope(ev), agent_id=ctx.agent(ev),
                          source=ctx.idx, ts_start=ctx.last_ts, ts_first=ts, inherited=self._inherited)
            if main and self._turn is not None:
                req.turn = self._turn.index
            self.requests[key] = req
        req.lines += 1
        if ts is not None:
            req.ts_first = ts if req.ts_first is None else min(req.ts_first, ts)
            req.ts_last = ts if req.ts_last is None else max(req.ts_last, ts)
        if msg.get("model"):
            req.model = msg["model"]
        if msg.get("stop_reason"):
            req.stop_reason = msg["stop_reason"]
        req.absorb_usage(msg.get("usage"))
        for attr, k in (("attribution_skill", "attributionSkill"), ("attribution_agent", "attributionAgent"),
                        ("attribution_plugin", "attributionPlugin"),
                        ("attribution_mcp_server", "attributionMcpServer"),
                        ("attribution_mcp_tool", "attributionMcpTool"), ("effort", "effort"),
                        ("per_turn_effort", "perTurnEffort"), ("advisor_model", "advisorModel")):
            v = ev.get(k)
            if v:
                setattr(req, attr, v)
        if ev.get("isApiErrorMessage"):
            req.is_api_error = True
            req.api_error_status = ev.get("apiErrorStatus") or req.api_error_status
            req.error_text = ev.get("error") or util.text_of(msg.get("content")) or req.error_text
        if isinstance(ev.get("quotaLimits"), dict):
            req.quota = ev["quotaLimits"]
        diag = msg.get("diagnostics")
        if isinstance(diag, dict) and isinstance(diag.get("cache_miss_reason"), dict):
            cmr = diag["cache_miss_reason"]
            req.cache_miss_reason = cmr.get("type") or req.cache_miss_reason
            req.cache_missed_tokens = max(req.cache_missed_tokens, _int(cmr.get("cache_missed_input_tokens")))
        if isinstance(msg.get("stop_details"), dict):
            req.refusal = msg["stop_details"]
        cm = msg.get("context_management")
        if isinstance(cm, dict) and isinstance(cm.get("applied_edits"), list):
            req.context_edits = max(req.context_edits, len(cm["applied_edits"]))
        for b in msg.get("content") or ():
            if not isinstance(b, dict):
                continue
            bt = b.get("type") or "<none>"
            req.blocks[bt] += 1
            if bt == "text":
                t = b.get("text") or ""
                req.text_chars += len(t)
                if len(req.text_preview) < 1200 and t.strip():
                    req.text_preview = (req.text_preview + "\n" + t).strip()[:1200]
                if t.strip() and t not in req.text_full and len(req.text_full) < TEXT_CAP:
                    req.text_full = (req.text_full + "\n\n" + t).strip()[:TEXT_CAP]
            elif bt == "thinking":
                req.thinking_chars += len(b.get("thinking") or "")
            elif bt == "tool_use":
                self._on_tool_use(b, ev, ts, ctx, req)
        if main:
            self._touch_turn(ts)
            ctx.pending_command = None

    def _on_tool_use(self, b, ev, ts, ctx, req):
        tid = b.get("id") or f"anon-{len(self.tool_calls)}"
        name = b.get("name") or "?"
        inp = b.get("input") if isinstance(b.get("input"), dict) else {}
        call = ToolCall(
            id=tid, name=name, input=inp, scope=req.scope, agent_id=req.agent_id, source=ctx.idx,
            request_key=req.key, message_id=req.message_id, ts_call=ts, turn=req.turn,
            attribution_skill=ev.get("attributionSkill") or req.attribution_skill,
            attribution_agent=ev.get("attributionAgent") or req.attribution_agent,
            inherited=req.inherited, cwd=ev.get("cwd"),
        )
        self.tool_calls[tid] = call
        req.tool_use_ids.append(tid)
        if name == "Skill":
            inv = SkillInvocation(name=str(inp.get("skill") or "?"), mode="model", ts=ts, turn=req.turn,
                                  scope=req.scope, agent_id=req.agent_id, args=inp.get("args"),
                                  tool_use_id=tid, via="Skill", inherited=req.inherited)
            self.skills.append(inv)
            self._skill_by_tool[tid] = inv
        elif name == "SlashCommand":
            cmd = str(inp.get("command") or "").strip()
            parts = cmd.split(None, 1)
            inv = SkillInvocation(name=(parts[0].lstrip("/") if parts else "?"), mode="model", ts=ts,
                                  turn=req.turn, scope=req.scope, agent_id=req.agent_id,
                                  args=parts[1] if len(parts) > 1 else None, tool_use_id=tid, via="SlashCommand",
                                  inherited=req.inherited)
            self.skills.append(inv)
            self._skill_by_tool[tid] = inv

    def _on_tool_result(self, b, ev, ts, ctx, tur):
        tid = b.get("tool_use_id")
        call = self.tool_calls.get(tid)
        if call is None:
            self.orphan_results += 1
            call = ToolCall(id=tid or f"orphan-{self.orphan_results}", name="(unmatched)", input={},
                            scope=ctx.scope(ev), agent_id=ctx.agent(ev), source=ctx.idx, request_key=None,
                            message_id=None, ts_call=None, turn=self._current_turn(ctx, ev))
            self.tool_calls[call.id] = call
        call.ts_result = ts
        content = b.get("content")
        call.result_chars, call.result_images, refs = util.result_stats(content)
        text = util.result_text(content)
        call.result_preview = text[:2000]
        if call.name in OUTPUT_KEPT:
            call.output = text[:OUTPUT_CAP]
        call.is_error = bool(b.get("is_error"))
        call.denial_kind = ev.get("toolDenialKind")
        if not call.denial_kind and call.is_error and text.startswith("The user doesn't want to proceed"):
            call.denial_kind = "user-rejected"
        interrupted = (isinstance(tur, dict) and tur.get("interrupted") is True) or \
            text.startswith("[Request interrupted by user")
        if call.denial_kind:
            call.status = "denied"
        elif interrupted:
            call.status = "interrupted"
        elif call.is_error:
            call.status = "error"
        else:
            call.status = "ok"
        if refs:
            call.facts["loaded_tools"] = refs
        try:
            self._facts(call, tur if isinstance(tur, dict) else {}, text)
        except Exception as exc:  # a malformed result must never sink the export
            call.facts["fact_error"] = f"{type(exc).__name__}: {exc}"

    def _facts(self, call, d, text):
        f = call.facts
        name = call.name
        inp = call.input
        if name == "Read":
            fl = d.get("file") if isinstance(d.get("file"), dict) else {}
            f["path"] = fl.get("filePath") or inp.get("file_path")
            f["kind"] = d.get("type")
            f["start_line"] = fl.get("startLine")
            f["num_lines"] = fl.get("numLines")
            f["total_lines"] = fl.get("totalLines")
            f["truncated"] = bool(fl.get("truncatedByTokenCap"))
            content = fl.get("content")
            if isinstance(content, str):
                f["content_chars"] = len(content)
                f["content_sha"] = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
                f["partial"] = bool(fl.get("totalLines") and fl.get("numLines") and fl["numLines"] < fl["totalLines"])
        elif name in ("Edit", "MultiEdit"):
            f["path"] = d.get("filePath") or inp.get("file_path")
            f["added"], f["removed"] = util.patch_counts(d.get("structuredPatch"))
            f["replace_all"] = bool(d.get("replaceAll") or inp.get("replace_all"))
            f["user_modified"] = bool(d.get("userModified"))
        elif name == "Write":
            f["path"] = d.get("filePath") or inp.get("file_path")
            f["write_kind"] = d.get("type")
            patch = d.get("structuredPatch")
            if patch:
                f["added"], f["removed"] = util.patch_counts(patch)
            elif call.status == "ok" and d.get("type") != "update":
                # A create carries no patch: every line of the new file is an addition.
                f["added"], f["removed"] = util.count_lines(d.get("content") or inp.get("content") or ""), 0
            else:
                f["added"], f["removed"] = 0, 0
            f["chars"] = len(inp.get("content") or "")
        elif name == "NotebookEdit":
            f["path"] = inp.get("notebook_path")
            f["edit_mode"] = inp.get("edit_mode")
        elif name == "Bash":
            f["stdout_chars"] = len(d.get("stdout") or "")
            f["stderr_chars"] = len(d.get("stderr") or "")
            f["stderr_preview"] = (d.get("stderr") or "")[:400]
            f["interrupted"] = bool(d.get("interrupted"))
            f["background_task_id"] = d.get("backgroundTaskId")
            f["timed_out_ms"] = d.get("timedOutAfterMs")
            f["persisted_bytes"] = d.get("persistedOutputSize")
            f["return_code_interpretation"] = d.get("returnCodeInterpretation")
            m = EXIT_CODE_RE.match(text or "")
            if m:
                f["exit_code"] = int(m.group(1))
            if isinstance(d.get("gitOperation"), dict):
                f["git"] = d["gitOperation"]
            ed = d.get("bashEditDiff")
            if isinstance(ed, dict):
                files = []
                for fi in ed.get("files") or ():
                    if isinstance(fi, dict):
                        a, r = util.patch_counts(fi.get("hunks"))
                        files.append({"path": fi.get("filePath"), "added": a, "removed": r,
                                      "created": bool(fi.get("created"))})
                f["edit_diff"] = files
                f["edit_diff_more"] = _int(ed.get("moreFiles"))
        elif name == "Grep":
            f["pattern"] = inp.get("pattern")
            f["mode"] = d.get("mode") or inp.get("output_mode")
            f["num_files"] = d.get("numFiles")
            f["num_lines"] = d.get("numLines")
        elif name == "Glob":
            f["pattern"] = inp.get("pattern")
            f["num_files"] = d.get("numFiles")
            f["truncated"] = d.get("truncated")
        elif name == "WebFetch":
            f["url"] = d.get("url") or inp.get("url")
            f["code"] = d.get("code")
            f["bytes"] = d.get("bytes")
            f["duration_ms"] = d.get("durationMs")
        elif name == "WebSearch":
            f["query"] = d.get("query") or inp.get("query")
            n = 0
            for r in d.get("results") or ():
                if isinstance(r, dict) and isinstance(r.get("content"), list):
                    n += len(r["content"])
            f["results"] = n
            f["duration_s"] = d.get("durationSeconds")
        elif name in ("Agent", "Task"):
            f["agent_id"] = d.get("agentId")
            f["status"] = d.get("status")
            f["agent_type"] = d.get("agentType") or inp.get("subagent_type")
            f["model"] = d.get("resolvedModel")
            f["total_duration_ms"] = d.get("totalDurationMs")
            f["total_tokens"] = d.get("totalTokens")
            f["total_tool_uses"] = d.get("totalToolUseCount")
            f["tool_stats"] = d.get("toolStats") if isinstance(d.get("toolStats"), dict) else None
            f["background"] = bool(inp.get("run_in_background")) or d.get("status") == "async_launched"
            f["description"] = inp.get("description") or d.get("description")
        elif name == "Workflow":
            f["run_id"] = d.get("runId")
            f["workflow_name"] = d.get("workflowName")
            f["status"] = d.get("status")
            f["task_id"] = d.get("taskId")
            f["summary"] = d.get("summary")
        elif name in ("Skill", "SlashCommand"):
            inv = self._skill_by_tool.get(call.id)
            if inv is not None:
                inv.success = call.status == "ok" and d.get("success", True) is not False
                inv.canonical = d.get("commandName")
                inv.allowed_tools = d.get("allowedTools") if isinstance(d.get("allowedTools"), list) else None
                inv.status = "ok" if inv.success else call.status
                if d.get("status") == "forked":
                    inv.status = "forked"
                    inv.forked_agent_id = d.get("agentId")
                if not inv.success:
                    inv.error = text[:400]
        elif name == "ToolSearch":
            f["query"] = inp.get("query")
            f["matches"] = d.get("matches") if isinstance(d.get("matches"), list) else f.get("loaded_tools", [])
            f["total_deferred"] = d.get("total_deferred_tools")
        elif name == "TaskCreate":
            task = d.get("task") if isinstance(d.get("task"), dict) else {}
            f["task_id"] = task.get("id")
            f["subject"] = inp.get("subject") or task.get("subject")
        elif name == "TaskUpdate":
            sc = d.get("statusChange") if isinstance(d.get("statusChange"), dict) else {}
            f["task_id"] = d.get("taskId") or inp.get("taskId")
            f["from"] = sc.get("from")
            f["to"] = sc.get("to") or inp.get("status")
        elif name == "TodoWrite":
            todos = d.get("newTodos") or inp.get("todos") or []
            f["todos"] = len(todos)
            f["todo_status"] = dict(Counter(t.get("status") for t in todos if isinstance(t, dict)))
        elif name == "AskUserQuestion":
            qs = d.get("questions") or inp.get("questions") or []
            f["questions"] = [{"header": q.get("header"), "question": q.get("question"),
                               "options": [{"label": o.get("label"), "description": o.get("description"),
                                            "preview": bool(o.get("preview"))}
                                           for o in q.get("options") or () if isinstance(o, dict)],
                               "multi": bool(q.get("multiSelect"))}
                              for q in qs if isinstance(q, dict)]
            # A multi-select answer is a list; typed text ("Other") arrives in place of an option label.
            f["answers"] = d.get("answers") if isinstance(d.get("answers"), dict) else None
            f["annotations"] = d.get("annotations") if isinstance(d.get("annotations"), dict) else None
        elif name == "ExitPlanMode":
            f["plan_chars"] = len(inp.get("plan") or d.get("plan") or "")
            f["plan_path"] = d.get("filePath") or inp.get("planFilePath")
        elif name == "EnterWorktree":
            f["path"] = d.get("worktreePath") or inp.get("path")
            f["branch"] = d.get("worktreeBranch")
        elif name == "Artifact":
            f["url"] = d.get("url")
            f["title"] = d.get("title")
            f["version"] = d.get("version")
            f["action"] = inp.get("action") or "publish"
        elif name == "SendUserFile":
            atts = d.get("attachments") if isinstance(d.get("attachments"), list) else []
            f["files"] = [a.get("path") for a in atts if isinstance(a, dict)] or list(inp.get("files") or [])
            f["display"] = d.get("display") or inp.get("display")
        elif name == "ReportFindings":
            f["count"] = d.get("count")
            f["level"] = d.get("level")

    # ------------------------------------------------------------------ system

    def _on_system(self, ev, ts, ctx):
        st = ev.get("subtype") or "<none>"
        self.system_subtypes[st] += 1
        main = ctx.main(ev)
        turn = self._current_turn(ctx, ev)
        if main:
            self._touch_turn(ts)
        if st == "turn_duration":
            if main and self._turn is not None:
                self._turn.reported_duration_ms = ev.get("durationMs")
                self._turn.reported_message_count = ev.get("messageCount")
                self._turn.ended = True
        elif st == "compact_boundary":
            md = ev.get("compactMetadata") if isinstance(ev.get("compactMetadata"), dict) else {}
            self.compactions.append({
                "ts": ts, "turn": turn, "scope": ctx.scope(ev),
                "trigger": md.get("trigger") or ev.get("trigger"),
                "pre_tokens": md.get("preTokens"), "post_tokens": md.get("postTokens"),
                "duration_ms": md.get("durationMs"), "dropped_tokens": md.get("cumulativeDroppedTokens"),
                "discovered_tools": len(md.get("preCompactDiscoveredTools") or []),
            })
        elif st == "api_error":
            err = ev.get("error") if isinstance(ev.get("error"), dict) else {}
            conn = err.get("connection") if isinstance(err.get("connection"), dict) else {}
            self.api_errors.append({
                "ts": ts, "turn": turn, "scope": ctx.scope(ev), "agent_id": ctx.agent(ev),
                "status": err.get("status"), "message": err.get("message") or err.get("formatted"),
                "network_down": bool(err.get("isNetworkDown")), "connection_code": conn.get("code"),
                "retry_attempt": ev.get("retryAttempt"), "max_retries": ev.get("maxRetries"),
                "retry_in_ms": ev.get("retryInMs"), "source": ev.get("source"),
            })
        elif st == "stop_hook_summary":
            infos = [i for i in (ev.get("hookInfos") or []) if isinstance(i, dict)]
            self.stop_hooks.append({
                "ts": ts, "turn": turn, "scope": ctx.scope(ev), "count": ev.get("hookCount") or len(infos),
                "commands": [i.get("command") for i in infos], "durations": [i.get("durationMs") for i in infos],
                "errors": len(ev.get("hookErrors") or []), "prevented": bool(ev.get("preventedContinuation")),
                "stop_reason": ev.get("stopReason"), "has_output": bool(ev.get("hasOutput")),
            })
        elif st == "away_summary":
            self.away_summaries.append({"ts": ts, "turn": turn, "text": ev.get("content")})
        elif st == "local_command":
            content = ev.get("content") or ""
            if "<command-name>" in content:
                self._on_command_text(ts, ctx, content, "command", turn)
                self.commands[-1]["via_system_event"] = True
            elif ctx.pending_command is not None:
                m = LOCAL_STDOUT_RE.search(content)
                ctx.pending_command["output"] = (m.group(1) if m else content).strip()
                ctx.pending_command["is_skill"] = False
                ctx.pending_command = None
            self.local_command_outputs += 1
        elif st == "informational":
            self.notices.append({"ts": ts, "turn": turn, "level": ev.get("level"), "text": ev.get("content")})
        elif st == "model_refusal_fallback":
            self.refusal_fallbacks.append({
                "ts": ts, "turn": turn, "original_model": ev.get("originalModel"),
                "fallback_model": ev.get("fallbackModel"), "category": ev.get("apiRefusalCategory"),
                "trigger": ev.get("trigger"), "scope": ev.get("scope"),
            })
        elif st not in KNOWN_SYSTEM_SUBTYPES:
            self.unknown_system_subtypes[st] += 1

    # ------------------------------------------------------------------ attachments

    def _on_attachment(self, ev, ts, ctx):
        a = ev.get("attachment") if isinstance(ev.get("attachment"), dict) else {}
        at = a.get("type") or "<none>"
        self.attachments[at] += 1
        self.attachments_by_scope[ctx.src.scope][at] += 1
        if at not in KNOWN_ATTACHMENT_TYPES:
            self.unknown_attachment_types[at] += 1
        main = ctx.main(ev)
        if main:
            self._touch_turn(ts)
        turn = self._current_turn(ctx, ev)
        if at == "skill_listing":
            names = [n for n in (a.get("names") or []) if isinstance(n, str)]
            self.skill_listing.update(names)
            self.skill_listing_count = max(self.skill_listing_count, _int(a.get("skillCount")) or len(names))
        elif at == "dynamic_skill":
            names = [n for n in (a.get("skillNames") or []) if isinstance(n, str)]
            self.dynamic_skills.append({"ts": ts, "dir": a.get("displayPath") or a.get("skillDir"), "names": names})
        elif at == "invoked_skills":
            for s in a.get("skills") or ():
                if isinstance(s, dict):
                    self.restored_skills.append({"ts": ts, "turn": turn, "name": s.get("name"),
                                                 "path": s.get("path"), "chars": len(s.get("content") or "")})
        elif at == "deferred_tools_delta":
            self.deferred_added.update(n for n in (a.get("addedNames") or []) if isinstance(n, str))
            self.deferred_removed.update(n for n in (a.get("removedNames") or []) if isinstance(n, str))
            for key, bucket in (("pendingMcpServers", self.mcp_pending), ("failedMcpServers", self.mcp_failed),
                                ("needsAuthMcpServers", self.mcp_needs_auth)):
                for s in a.get(key) or ():
                    bucket.add(s if isinstance(s, str) else json.dumps(s, sort_keys=True)[:80])
        elif at == "mcp_instructions_delta":
            self.mcp_instruction_servers.update(n for n in (a.get("addedNames") or []) if isinstance(n, str))
        elif at == "agent_listing_delta":
            self.agent_types_listed.update(n for n in (a.get("addedTypes") or []) if isinstance(n, str))
        elif at.startswith("hook_"):
            self.hook_runs.append({
                "ts": ts, "turn": turn, "scope": ctx.scope(ev), "kind": at, "event": a.get("hookEvent"),
                "name": a.get("hookName"), "exit_code": a.get("exitCode"), "duration_ms": a.get("durationMs"),
                "command": a.get("command"), "stdout_chars": len(a.get("stdout") or ""),
                "stderr_chars": len(a.get("stderr") or ""), "content_chars": len(str(a.get("content") or "")),
            })
        elif at == "queued_command":
            origin = a.get("origin")
            self.queued_commands.append({
                "ts": ts, "turn": turn, "mode": a.get("commandMode"),
                "origin": origin.get("kind") if isinstance(origin, dict) else None,
                "chars": len(a.get("prompt") or "") if isinstance(a.get("prompt"), str) else 0,
            })
            if main and self._turn is not None and a.get("commandMode") == "prompt":
                self._turn.queued_absorbed += 1
        elif at == "file":
            self.mentioned_files[a.get("displayPath") or a.get("filename") or "?"] += 1
        elif at == "edited_text_file":
            self.user_edited_files[a.get("displayPath") or a.get("filename") or "?"] += 1
        elif at == "opened_file_in_ide":
            self.ide_files[a.get("filename") or "?"] += 1
        elif at == "selected_lines_in_ide":
            self.ide_selections += 1
            self.ide_files[a.get("displayPath") or a.get("filename") or "?"] += 1
        elif at == "nested_memory":
            self.memory_files[a.get("displayPath") or a.get("path") or "?"] += 1
        elif at == "instructions":
            for fi in a.get("files") or ():
                if isinstance(fi, dict) and fi.get("path"):
                    self.instruction_files[fi["path"]] = {"type": fi.get("type"),
                                                          "chars": len(fi.get("content") or "")}
        elif at == "environment":
            if isinstance(a.get("snapshot"), dict):
                self.environment = a["snapshot"]
            for ch in a.get("changes") or ():
                if isinstance(ch, dict):
                    self.environment_changes.append({"ts": ts, "field": ch.get("field")})
        elif at == "session_context":
            c = a.get("context") if isinstance(a.get("context"), dict) else {}
            self.has_git_status = self.has_git_status or bool(c.get("gitStatus"))
        elif at == "model":
            ident = a.get("identity") if isinstance(a.get("identity"), dict) else {}
            if ident.get("modelId"):
                self.model_identities[ident["modelId"]] = ident.get("marketingName")
        elif at == "prompt_snapshot":
            sp = a.get("systemPrompt")
            chars = 0
            if isinstance(sp, str):
                chars = len(sp)
            elif isinstance(sp, list):
                for part in sp:
                    chars += len(part) if isinstance(part, str) else len(str((part or {}).get("text") or ""))
            tools = a.get("tools") if isinstance(a.get("tools"), list) else None
            self.prompt_snapshots.append({
                "ts": ts, "scope": ctx.scope(ev), "system_chars": chars,
                "tool_count": len(tools) if tools is not None else None,
                "tools": [t.get("name") for t in tools if isinstance(t, dict)] if tools else None,
            })
        elif at == "total_tokens_reminder":
            m = TOKENS_LEFT_RE.search(str(a.get("text") or ""))
            if m and main:
                self.tokens_left.append((ts, int(m.group(1).replace(",", ""))))
        elif at in ("plan_mode", "plan_mode_exit", "plan_file_reference"):
            self.plan_mode[at] += 1
            p = a.get("planFilePath")
            if p:
                self.plan_files.add(p)
        elif at in ("auto_mode", "auto_mode_exit"):
            self.auto_mode[at] += 1
        elif at in ("ultra_effort_enter", "ultra_effort_exit"):
            self.ultra_effort[at] += 1
        elif at == "command_permissions":
            tools = a.get("allowedTools")
            if isinstance(tools, list) and tools:
                self.command_permissions.append({"ts": ts, "turn": turn, "tools": tools})
        else:
            self.misc[at] += 1

    # ------------------------------------------------------------------ other events

    def _title(self, kind, value):
        if value and isinstance(value, str):
            self.titles[kind] = value

    def _on_pr_link(self, ev, ts, ctx):
        url = ev.get("prUrl")
        if url and url not in self.pr_links:
            self.pr_links[url] = {"url": url, "number": ev.get("prNumber"), "repository": ev.get("prRepository"),
                                  "ts": ts}

    def _on_bridge(self, ev, ts, ctx):
        self.bridge_events += 1

    def _on_queue_op(self, ev, ts, ctx):
        self.queue_ops[ev.get("operation") or "<none>"] += 1
        if ev.get("reason"):
            self.queue_reasons[ev["reason"]] += 1

    def _mode_change(self, series, ts, value):
        if value and (not series or series[-1][1] != value):
            series.append((ts, value))

    def _on_worktree(self, ev, ts, ctx):
        ws = ev.get("worktreeSession")
        if isinstance(ws, dict):
            self.worktree = ws

    def _on_file_snapshot(self, ev, ts, ctx):
        self.file_snapshots += 1
        snap = ev.get("snapshot") if isinstance(ev.get("snapshot"), dict) else {}
        for path, info in (snap.get("trackedFileBackups") or {}).items():
            v = _int((info or {}).get("version")) if isinstance(info, dict) else 0
            self.tracked_files[path] = max(self.tracked_files.get(path, 0), v)

    def _on_file_delta(self, ev, ts, ctx):
        self.file_deltas += 1
        path = ev.get("trackingPath")
        backup = ev.get("backup") if isinstance(ev.get("backup"), dict) else {}
        if path:
            self.tracked_files[path] = max(self.tracked_files.get(path, 0), _int(backup.get("version")))

    def _on_frame_link(self, ev, ts, ctx):
        if ev.get("frameUrl") or ev.get("title"):
            self.frame_links.append({"ts": ts, "title": ev.get("title"), "url": ev.get("frameUrl"),
                                     "path": ev.get("path")})

    def _on_artifact_monitor(self, ev, ts, ctx):
        self.artifact_monitor_events += 1

    # ------------------------------------------------------------------ finalize

    def _finalize(self):
        names = self.known_skill_names()
        for cmd in self.commands:
            if cmd["is_skill"] is None:
                if cmd["name"] in names and cmd["name"] not in BUILTIN_COMMANDS:
                    self._promote(cmd, None, None)
                else:
                    cmd["is_skill"] = False

        starts = [t.ts_start for t in self.turns if t.ts_start is not None]
        idx = [t.index for t in self.turns if t.ts_start is not None]

        def turn_at(ts):
            if ts is None or not starts:
                return None
            i = bisect.bisect_right(starts, ts) - 1
            return idx[i] if i >= 0 else None

        for r in self.requests.values():
            if r.turn is None:
                r.turn = turn_at(r.ts_first)
        for c in self.tool_calls.values():
            if c.turn is None:
                c.turn = turn_at(c.ts_call if c.ts_call is not None else c.ts_result)
        for s in self.skills:
            if s.turn is None:
                s.turn = turn_at(s.ts)
        for coll in (self.api_errors, self.hook_runs, self.stop_hooks, self.compactions, self.interrupts):
            for e in coll:
                if e.get("turn") is None:
                    e["turn"] = turn_at(e.get("ts"))

        by_msg = defaultdict(list)
        for c in self.tool_calls.values():
            if c.message_id:
                by_msg[(c.source, c.message_id)].append(c)
        for calls in by_msg.values():
            for i, c in enumerate(calls):
                c.batch_size = len(calls)
                c.batch_index = i

    # ------------------------------------------------------------------ helpers

    def known_skill_names(self):
        names = set(self.skill_listing)
        for d in self.dynamic_skills:
            names.update(d["names"])
        return names

    def requests_in(self, scope=None):
        return [r for r in self.requests.values() if scope is None or r.scope == scope]

    @property
    def live_turn(self):
        """The last turn, if it has not finished (the session is still running)."""
        if self.turns and not self.turns[-1].ended:
            return self.turns[-1]
        return None


ARGS_MARKER = "\n\nARGUMENTS: "


def skill_body(text, base_dir=None, session_id=None, args=None):
    """An injected skill body turned back into the SKILL.md body it came from (frontmatter already removed).

    Claude Code injects "Base directory for this skill: <dir>" + the SKILL.md body, substitutes
    ${CLAUDE_SKILL_DIR}, ${CLAUDE_SESSION_ID} and $ARGUMENTS, and appends "ARGUMENTS: <args>" when the skill
    does not place them itself. All of that varies per run, so it is undone here.
    """
    if not text:
        return None
    body = BASE_DIR_LINE_RE.sub("", text, count=1)
    i = body.rfind(ARGS_MARKER)
    if i >= 0:
        body = body[:i]
    if base_dir:
        body = body.replace(base_dir.rstrip("/"), "${CLAUDE_SKILL_DIR}")
    if session_id:
        body = body.replace(session_id, "${CLAUDE_SESSION_ID}")
    if args and isinstance(args, str) and len(args) >= 8:
        body = body.replace(args, "$ARGUMENTS")
    return body.strip()


def fingerprint_body(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12] if body is not None else None


def skill_fingerprint(text, base_dir=None, session_id=None, args=None):
    """Hash identifying the version of a skill from its injected body; see skill_body()."""
    return fingerprint_body(skill_body(text, base_dir, session_id, args))


def _command_parts(text):
    m = COMMAND_NAME_RE.search(text or "")
    name = m.group(1).strip() if m else "?"
    a = COMMAND_ARGS_RE.search(text or "")
    return name, (a.group(1).strip() if a else "")


def parse_session(path, own_only=False):
    return Session(path, own_only=own_only).load()
