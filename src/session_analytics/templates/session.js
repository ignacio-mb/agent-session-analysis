"use strict";
/* Session dashboard. Reads the analytics dict embedded as JSON and renders it. */

const D = JSON.parse(document.getElementById("session-data").textContent);
Tip.init();

const S = D.session, T = D.totals;
const TOOL_ROWS = D.tools.rows;
const cssv = (n) => `var(${n})`;

const LANE_GROUPS = [
  ["Shell", ["shell"]],
  ["Edits", ["edits"]],
  ["Read & search", ["files", "search"]],
  ["Web", ["web"]],
  ["Agents & workflows", ["agents"]],
  ["Skills & tool search", ["skills"]],
  ["Planning & questions", ["planning"]],
  ["Outputs", ["outputs", "workspace"]],
  ["MCP tools", ["mcp"]],
  ["Other tools", ["other"]],
];

const MARKERS = {
  skill: { shape: "diamond", label: "Skill invoked" },
  compaction: { shape: "down", label: "Compaction" },
  commit: { shape: "circle", label: "Commit" },
  pr: { shape: "square", label: "Pull request" },
  interrupt: { shape: "cross", label: "Interrupted" },
  api_error: { shape: "triangle", label: "API error", critical: true },
};

/* ------------------------------------------------------------------ header */

function buildHeader() {
  const branches = S.git_branches.map((b) => b.branch).join(", ");
  const models = S.models.map((m) => `${m.name || m.model} (${m.requests})`).join(", ");
  const meta = h("div", { class: "meta" },
    h("span", { class: "id", title: "Session id" }, S.id),
    h("span", null, S.project_name + (branches ? ` · ${branches}` : "")),
    h("span", null, `${F.datetime(S.start)} → ${F.datetime(S.end)} (${F.dur(S.wall_ms)})`),
    h("span", null, `Claude Code ${S.claude_code_versions.join(", ") || "—"} · ${Object.keys(S.entrypoints).join(", ") || "—"}`),
    models ? h("span", null, models) : null);
  const badges = h("div", { class: "meta", style: "margin-top:6px" },
    S.live ? h("span", { class: "badge live" }, "Live snapshot") : null,
    D.generator.redaction ? h("span", { class: "badge" }, "Secrets redacted") : h("span", { class: "badge" }, "Unredacted"),
    D.generator.full_content ? h("span", { class: "badge" }, "Full content") : null,
    S.worktree ? h("span", { class: "badge" }, "Worktree " + (S.worktree.worktreeBranch || "")) : null);
  return h("header", { class: "top" }, h("div", { style: "min-width:0;flex:1" }, h("h1", null, S.title), meta, badges), themeToggle());
}

function buildKpis() {
  const rep = D.reported;
  const tiles = [
    ["Estimated cost", F.usd(T.estimated_cost_usd), rep && rep.total_cost_usd !== null ? `Claude Code reported ${F.usd(rep.total_cost_usd)}` : "API list-price equivalent"],
    ["Tokens", F.tok(T.total_tokens), `${F.tok(T.output_tokens)} output · ${F.pct(T.cache_hit_ratio)} from cache`],
    ["Turns", F.num(T.turns), `${T.prompts} prompts · ${T.interruptions} interrupted`],
    ["API requests", F.num(T.api_requests), `${F.num(T.main_requests)} main thread`],
    ["Tool calls", F.num(T.tool_calls), `${T.distinct_tools} tools · ${T.tool_errors} errors`],
    ["Skills invoked", F.num(T.skills_invoked), `${T.distinct_skills} distinct · ${T.slash_commands} commands`],
    ["Subagents", F.num(T.subagents + T.workflow_agents), `${T.workflow_runs} workflow runs`],
    ["Active time", F.dur(T.active_ms), `of ${F.dur(T.wall_ms)} wall clock`],
    ["Files changed", F.num(T.files_modified), `+${F.num(T.lines_added)} / −${F.num(T.lines_removed)} lines`],
    ["Peak context", F.tok(T.peak_context_tokens), `${T.compactions} compactions`],
  ];
  return h("div", { class: "kpis" }, tiles.map(([l, v, s]) =>
    h("div", { class: "kpi" }, h("div", { class: "label" }, l), h("div", { class: "value" }, v), h("div", { class: "sub" }, s))));
}

function buildInsights() {
  if (!D.insights.length) return null;
  return h("ul", { class: "insights" }, D.insights.map((i) => h("li", { class: i.level }, i.text)));
}

/* ------------------------------------------------------------------ timeline */

function timelineData(compact) {
  const t0 = D.timeline.start_ms, t1 = D.timeline.end_ms;
  const lanes = [];
  const turns = D.turns.rows.filter((t) => t.start_ms);
  lanes.push({
    label: "Turns", kind: "turns", items: turns.map((t) => ({
      t0: t.start_ms, t1: t.end_ms || t.start_ms, cls: t.index % 2 ? "turn-b" : "turn-a", text: String(t.index),
      tip: () => ({
        title: `Turn ${t.index} · ${t.trigger}${t.in_progress ? " · in progress" : ""}`,
        rows: [[F.dur(t.duration_ms), "duration"], [F.usd(t.cost_usd), "cost"], [String(t.tool_calls + t.subagent_tool_calls), "tool calls"]],
        body: t.prompt || (t.command ? "/" + t.command : ""),
      }),
    })),
  });
  const reqs = D.requests.rows.filter((r) => r.start_ms || r.ts_ms);
  const reqItem = (r) => ({
    t0: r.start_ms || r.ts_ms, t1: r.end_ms || r.ts_ms, cls: r.scope === "main" ? "span" : "span sub",
    tip: () => ({
      title: `${r.model} · ${r.scope}${r.agent_id ? " " + r.agent_id.slice(0, 8) : ""}`,
      rows: [[F.dur(r.duration_ms), "request"], [F.tok(r.context), "context"], [F.tok(r.output), "output"], [F.usd(r.cost_usd), "cost"]],
      body: r.tools.length ? "tools: " + r.tools.join(", ") : (r.stop_reason || ""),
    }),
  });
  lanes.push({ label: "Claude · main", items: reqs.filter((r) => r.scope === "main").map(reqItem) });
  if (!compact) lanes.push({ label: "Claude · agents", items: reqs.filter((r) => r.scope !== "main").map(reqItem) });
  const toolItem = (c) => ({
    t0: c.ts_ms, t1: c.end_ms || c.ts_ms, cls: c.status === "error" ? "span err" : c.scope === "main" ? "span" : "span sub",
    tip: () => ({
      title: `${c.name} · ${c.status}${c.turn !== null ? " · turn " + c.turn : ""}`,
      rows: [[F.dur(c.duration_ms), "duration"], [c.scope, "scope"]],
      body: c.input + (c.error ? "\n\n" + c.error : ""),
    }),
  });
  const mainTools = TOOL_ROWS.filter((c) => c.ts_ms && c.scope === "main");
  if (compact) lanes.push({ label: "Tools · main", items: mainTools.map(toolItem) });
  else {
    for (const [label, cats] of LANE_GROUPS) {
      const items = mainTools.filter((c) => cats.includes(c.category)).map(toolItem);
      if (items.length) lanes.push({ label, items });
    }
    const subTools = TOOL_ROWS.filter((c) => c.ts_ms && c.scope !== "main");
    if (subTools.length) lanes.push({ label: "Tools · agents", items: subTools.map(toolItem) });
  }
  const agents = D.subagents.rows.filter((a) => a.start_ms);
  if (agents.length) lanes.push({
    label: "Subagents", items: agents.map((a) => ({
      t0: a.start_ms, t1: a.end_ms || a.start_ms, cls: "span",
      tip: () => ({
        title: `${a.type || a.kind} · ${a.agent_id.slice(0, 10)}`,
        rows: [[F.dur(a.duration_ms), "duration"], [String(a.requests), "requests"], [String(a.tool_calls), "tool calls"], [F.usd(a.cost_usd), "cost"]],
        body: a.description || a.task_prompt || "",
      }),
    })),
  });
  const marks = D.timeline.markers.filter((m) => MARKERS[m.kind]);
  if (marks.length) lanes.push({
    label: "Events", kind: "marks", items: marks.map((m) => ({
      t0: m.t, kind: m.kind, tip: () => ({ title: F.datetime(m.t), rows: [[m.label, ""]] }),
    })),
  });
  return { t0, t1, lanes };
}

function timelineChart(width, compact, zoom) {
  const { t0, t1, lanes } = timelineData(compact);
  const labelW = 132, laneH = 16, gap = 8, axisH = 22, padR = 14;
  const plotW = Math.max(120, (width - labelW - padR) * zoom);
  const H = axisH + lanes.length * (laneH + gap) + 4;
  const span = Math.max(1, t1 - t0);
  const x = (t) => ((t - t0) / span) * plotW;

  const labels = sv("svg", { class: "chart", width: labelW, height: H, "aria-hidden": "true" });
  lanes.forEach((ln, i) => {
    labels.appendChild(sv("text", { class: "lane-label", x: 0, y: axisH + i * (laneH + gap) + laneH - 4 }, ln.label));
  });
  const svg = sv("svg", { class: "chart", width: plotW + padR, height: H, role: "img", "aria-label": "Session timeline" });
  const { ticks, step } = timeTicks(t0, t1, Math.max(2, Math.floor(plotW / 95)));
  const grid = sv("g", { class: "grid" });
  for (const t of ticks) {
    grid.appendChild(sv("line", { x1: x(t), x2: x(t), y1: axisH - 4, y2: H }));
    svg.appendChild(sv("text", { x: x(t), y: 12, "text-anchor": "middle" }, fmtTick(t, step)));
  }
  svg.appendChild(grid);
  lanes.forEach((ln, i) => {
    const y = axisH + i * (laneH + gap);
    svg.appendChild(sv("rect", { class: "lane-bg", x: 0, y, width: plotW, height: laneH, rx: 3 }));
    for (const it of ln.items) {
      if (ln.kind === "marks") {
        const mk = MARKERS[it.kind];
        const node = markShape(mk.shape, x(it.t0), y + laneH / 2, 5, mk.critical ? cssv("--critical") : cssv("--ink-2"));
        node.__tip = it.tip;
        svg.appendChild(node);
        continue;
      }
      const turns = ln.kind === "turns";
      const x0 = x(it.t0), w = Math.max(turns ? 2 : 1.5, x(it.t1) - x0);
      const node = sv("rect", { class: it.cls, x: x0, y: y + (turns ? 0 : 3), width: w, height: turns ? laneH : laneH - 6, rx: 2 });
      node.__tip = it.tip;
      svg.appendChild(node);
      // Label a turn with its number only where the number fits inside the span.
      if (turns && it.text && w > it.text.length * 6 + 8) {
        svg.appendChild(sv("text", { class: "turn-label", x: x0 + 4, y: y + laneH - 4 }, it.text));
      }
    }
  });
  svg.addEventListener("pointermove", (e) => {
    const fn = e.target.__tip;
    if (fn) { const t = fn(); Tip.show(e, t.title, t.rows, t.body); } else Tip.hide();
  });
  svg.addEventListener("pointerleave", () => Tip.hide());
  const scroller = h("div", { class: "chart-scroll", style: "flex:1;min-width:0" }, svg);
  return h("div", { style: "display:flex" }, labels, scroller);
}

function timelineLegend() {
  const kinds = new Set(D.timeline.markers.map((m) => m.kind));
  const items = [
    { label: "Activity (request, tool call, agent)", color: cssv("--s1") },
    { label: "Tool error", color: cssv("--critical") },
  ];
  for (const [k, mk] of Object.entries(MARKERS)) if (kinds.has(k)) items.push({ label: mk.label, shape: "mark", mark: mk.shape, color: mk.critical ? cssv("--critical") : cssv("--ink-2") });
  return legend(items);
}

/* ------------------------------------------------------------------ shared pieces */

function toolTip(t) {
  return {
    title: `${t.name} · ${t.category}`,
    rows: [[F.num(t.calls), "calls"], [F.num(t.ok || 0), "ok"], [F.num(t.error || 0), "errors"],
      [F.num(t.denied || 0), "denied"], [F.dur(t.duration_ms.p50), "p50"], [F.dur(t.duration_ms.p90), "p90"],
      [`${t.main} / ${t.subagent}`, "main / agents"]],
  };
}

function toolBarList(limit) {
  return barList(D.tools.by_tool, {
    label: (t) => t.name, value: (t) => t.calls, limit,
    extra: (t) => t.error ? h("span", { class: "err-flag" }, `${t.error} err`) : `p50 ${F.dur(t.duration_ms.p50)}`,
    tip: toolTip,
  });
}

const COST_SEGS = [
  { key: "input", label: "Input", color: cssv("--s1") },
  { key: "cache_write", label: "Cache write", color: cssv("--s2") },
  { key: "cache_read", label: "Cache read", color: cssv("--s3") },
  { key: "output", label: "Output (incl. thinking)", color: cssv("--s4") },
];

function costByModelRows() {
  const rows = [];
  for (const [model, u] of Object.entries(D.tokens.by_model)) {
    const c = u.cost_components || {};
    rows.push({ label: model, values: { input: c.input || 0, cache_write: (c.cache_write_5m || 0) + (c.cache_write_1h || 0), cache_read: c.cache_read || 0, output: c.output || 0, web: c.web_search || 0 } });
  }
  return rows.sort((a, b) => Object.values(b.values).reduce((x, y) => x + y, 0) - Object.values(a.values).reduce((x, y) => x + y, 0));
}

function costCard(span) {
  const rows = costByModelRows();
  const segs = COST_SEGS.slice();
  if (rows.some((r) => r.values.web)) segs.push({ key: "web", label: "Web search", color: cssv("--s5") });
  return chartCard({
    title: "Where the money went", sub: "Estimated cost by model and component (list prices)", span,
    empty: rows.length ? null : "No priced requests.",
    chart: () => stackedBars(rows, segs, { fmt: F.usd }),
    table: () => dataTable({
      search: false, rows: rows.map((r) => Object.assign({ model: r.label }, r.values)),
      columns: [{ key: "model", label: "Model" }].concat(segs.map((s) => ({ key: s.key, label: s.label, num: true, fmt: F.usd }))),
    }),
  });
}

function activityCard(span) {
  const b = D.timeline.buckets;
  if (!b || !b.rows) return card({ title: "Activity over time", span }, h("div", { class: "empty" }, "Not enough time span."));
  return chartCard({
    title: "Activity over time", sub: `Tool calls per ${F.every(b.size_ms)} (main thread and agents)`, span,
    chart: (w) => columnsChart(w, b.rows, {
      value: (r) => r.tool_calls, t0: D.timeline.start_ms, bucket: b.size_ms,
      tip: (r) => ({ title: `${F.time(r.t)} – ${F.time(r.t + b.size_ms)}`, rows: [[F.num(r.tool_calls), "tool calls"], [F.num(r.errors), "errors"], [F.num(r.requests), "main requests"], [F.num(r.subagent_requests), "agent requests"], [F.tok(r.output), "output tokens"], [F.usd(r.cost), "cost"]] }),
    }),
    table: () => dataTable({
      search: false, limit: 500, rows: b.rows.filter((r) => r.requests || r.tool_calls || r.subagent_requests),
      columns: [{ key: "t", label: "Bucket start", fmt: F.datetime }, { key: "tool_calls", label: "Tool calls", num: true },
        { key: "errors", label: "Errors", num: true }, { key: "requests", label: "Main requests", num: true },
        { key: "subagent_requests", label: "Agent requests", num: true }, { key: "output", label: "Output tokens", num: true, fmt: F.tok },
        { key: "cost", label: "Cost", num: true, fmt: F.usd }],
    }),
  });
}

function cumulativeCostCard(span) {
  const pts = D.cost.cumulative.filter((p) => p[0]);
  return chartCard({
    title: "Cumulative cost", sub: "Estimated spend as the session progressed", span,
    empty: pts.length ? null : "No priced requests.",
    chart: (w) => lineChart(w, pts, { fmtY: F.usd, t0: D.timeline.start_ms, t1: D.timeline.end_ms,
      tip: (p) => ({ title: F.datetime(p[0]), rows: [[F.usd(p[1]), "spent so far"]] }) }),
    table: () => dataTable({ search: false, limit: 300, rows: pts.map((p) => ({ t: p[0], usd: p[1] })),
      columns: [{ key: "t", label: "Time", fmt: F.datetime }, { key: "usd", label: "Cumulative", num: true, fmt: F.usd }] }),
  });
}

function contextCard(span, height) {
  const pts = D.context.series.filter((p) => p[0]);
  const markers = D.context.compactions.filter((c) => c.ts).map((c) => ({ t: Date.parse(c.ts), label: `Compaction (${c.trigger || "?"}) ${F.tok(c.pre_tokens)} → ${F.tok(c.post_tokens)}` }));
  return chartCard({
    title: "Context size per request", sub: "Input + cache read + cache write tokens sent on each main-thread request", span,
    empty: pts.length ? null : "No main-thread requests.",
    chart: (w) => lineChart(w, pts, { fmtY: F.tok, height, markers, t0: D.timeline.start_ms, t1: D.timeline.end_ms,
      tip: (p) => ({ title: F.datetime(p[0]), rows: [[F.tok(p[1]), "context"], [F.tok(p[2]), "output"]] }) }),
    table: () => dataTable({ search: false, limit: 300, rows: pts.map((p) => ({ t: p[0], ctx: p[1], out: p[2] })),
      columns: [{ key: "t", label: "Time", fmt: F.datetime }, { key: "ctx", label: "Context", num: true, fmt: F.tok }, { key: "out", label: "Output", num: true, fmt: F.tok }] }),
  });
}

function skillsCompact() {
  const inv = D.skills.invocations;
  if (!inv.length) return h("div", { class: "empty" }, `No skills invoked (${D.skills.available_count} available).`);
  return dataTable({
    search: false, limit: 12, rows: inv,
    columns: [{ key: "turn", label: "Turn", num: true }, { key: "canonical", label: "Skill", render: (r) => r.canonical || r.name },
      { key: "mode", label: "Invoked by", render: (r) => modeBadge(r.mode) },
      { key: "success", label: "Result", render: (r) => statusBadge(r.success === false ? "error" : "ok") }],
  });
}

/* ------------------------------------------------------------------ tabs */

function buildOverview() {
  const lg = timelineLegend();
  return h("div", { class: "grid" },
    chartCard({ title: "Session timeline", sub: "Turns, model requests, tool calls, subagents and events over time — hover for details", span: 12, legendEl: lg,
      chart: (w) => timelineChart(w, true, 1), table: () => toolCallsTable() }),
    costCard(6),
    chartCard({ title: "Attributed spend by skill", sub: "Cost of the requests Claude Code attributed to each skill", span: 6,
      empty: Object.keys(D.cost.by_skill).length ? null : "No requests.",
      chart: () => barList(Object.entries(D.cost.by_skill).map(([k, v]) => ({ k, v })), { label: (x) => x.k, value: (x) => x.v, fmt: F.usd }),
      table: () => dataTable({ search: false, rows: Object.entries(D.cost.by_skill).map(([k, v]) => ({ skill: k, usd: v })),
        columns: [{ key: "skill", label: "Skill" }, { key: "usd", label: "Cost", num: true, fmt: F.usd }] }) }),
    activityCard(6), cumulativeCostCard(6), contextCard(12, 170),
    card({ title: "Top tools", span: 6 }, toolBarList(12)),
    card({ title: "Skills invoked", span: 6, note: D.skills.notes[0] }, skillsCompact()));
}

function buildTimeline() {
  let zoom = 1;
  const host = h("div", { class: "chart-host" });
  Charts.register(host, (w) => timelineChart(w, false, zoom));
  const seg = h("div", { class: "seg", role: "group", "aria-label": "Zoom" });
  for (const z of [1, 2, 4, 8, 16]) {
    const b = h("button", { type: "button", "aria-pressed": String(z === 1) }, z + "×");
    b.addEventListener("click", () => { zoom = z; seg.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x === b))); Charts.renderVisible(true); });
    seg.appendChild(b);
  }
  return h("div", { class: "grid" },
    card({ title: "Session timeline", sub: "One lane per kind of activity. Main-thread tools by category; agents' own requests and tools in their lanes. Scroll horizontally when zoomed.", span: 12, tools: seg },
      timelineLegend(), host),
    card({ title: "All tool calls", sub: "The timeline's table view", span: 12 }, toolCallsTable()));
}

function buildSkills() {
  const sk = D.skills;
  const perSkillRows = sk.per_skill;
  return h("div", { class: "grid" },
    card({ title: "Summary", span: 12 },
      kv([["Skills available", F.num(sk.available_count)], ["Distinct skills invoked", F.num(sk.invoked_distinct)],
        ["Invocations", `${sk.invocations_total} (${Object.entries(sk.by_mode).map(([k, v]) => `${k} ×${v}`).join(", ") || "none"})`],
        ["Failed", String(sk.failed.length)], ["Plugins attributed", Object.keys(sk.plugins).join(", ") || "—"]]),
      h("div", { class: "note" }, sk.notes.join(" "))),
    card({ title: "Invocations", sub: "Every time a skill was loaded, how, and what it was asked", span: 12 },
      dataTable({ rows: sk.invocations, limit: 200, sortKey: "ts_ms", sortDir: "asc", selects: [{ key: "mode", label: "modes" }, { key: "source", label: "sources" }],
        columns: [
          { key: "ts_ms", label: "When", fmt: F.datetime }, { key: "turn", label: "Turn", num: true },
          { key: "canonical", label: "Skill", render: (r) => r.canonical || r.name },
          { key: "mode", label: "Invoked by", render: (r) => modeBadge(r.mode) }, { key: "via", label: "Via" },
          { key: "args", label: "Args", cls: "wrap" }, { key: "source", label: "Source" },
          { key: "content_chars", label: "Body chars", num: true, fmt: F.tok },
          { key: "status", label: "Result", render: (r) => statusBadge(r.success === false ? "error" : "ok") },
          { key: "allowed_tools", label: "Allowed tools", fmt: (v) => (v || []).join(", ") || "—", cls: "wrap" },
          { key: "turn_prompt", label: "Turn prompt", cls: "wrap" }, { key: "error", label: "Error", cls: "wrap" },
          { key: "scope", label: "Scope" }] })),
    chartCard({ title: "Attributed spend by skill", span: 6, empty: perSkillRows.length ? null : "No skill activity.",
      chart: () => barList(perSkillRows, { label: (p) => p.skill, value: (p) => p.cost_usd, fmt: F.usd,
        tip: (p) => ({ title: p.skill, rows: [[F.usd(p.cost_usd), "cost"], [F.num(p.attributed_requests), "requests"], [F.num(p.attributed_tool_calls), "tool calls"], [F.tok(p.output_tokens), "output tokens"], [F.num(p.invocations), "invocations"]] }) }),
      table: () => dataTable({ search: false, rows: perSkillRows, columns: [{ key: "skill", label: "Skill" }, { key: "cost_usd", label: "Cost", num: true, fmt: F.usd }] }) }),
    chartCard({ title: "Tool calls made under each skill", span: 6, empty: perSkillRows.some((p) => p.attributed_tool_calls) ? null : "No attributed tool calls.",
      chart: () => barList(perSkillRows.filter((p) => p.attributed_tool_calls), { label: (p) => p.skill, value: (p) => p.attributed_tool_calls,
        extra: (p) => p.tool_errors ? h("span", { class: "err-flag" }, `${p.tool_errors} err`) : "",
        tip: (p) => ({ title: p.skill, rows: Object.entries(p.tools).map(([k, v]) => [F.num(v), k]) }) }),
      table: () => dataTable({ search: false, rows: perSkillRows, columns: [{ key: "skill", label: "Skill" }, { key: "attributed_tool_calls", label: "Tool calls", num: true }, { key: "tools", label: "Tools", fmt: (v) => Object.entries(v).map(([k, n]) => `${k} ×${n}`).join(", "), cls: "wrap" }] }) }),
    card({ title: "Per skill", sub: "Invocations plus the activity Claude Code attributed to each skill (attribution lasts until the turn ends)", span: 12 },
      dataTable({ rows: perSkillRows, search: false, sortKey: "cost_usd",
        columns: [{ key: "skill", label: "Skill" }, { key: "source", label: "Source" }, { key: "invocations", label: "Invocations", num: true },
          { key: "by_mode", label: "Modes", fmt: (v) => Object.entries(v).map(([k, n]) => `${k} ×${n}`).join(", ") || "—" },
          { key: "turns", label: "Turns", num: true, fmt: (v) => String(v.length), sort: (r) => r.turns.length },
          { key: "attributed_requests", label: "Requests", num: true }, { key: "attributed_tool_calls", label: "Tool calls", num: true },
          { key: "tool_errors", label: "Tool errors", num: true }, { key: "output_tokens", label: "Output", num: true, fmt: F.tok },
          { key: "cache_read_tokens", label: "Cache read", num: true, fmt: F.tok }, { key: "cost_usd", label: "Cost", num: true, fmt: F.usd },
          { key: "tools", label: "Top tools", cls: "wrap", fmt: (v) => Object.entries(v).slice(0, 6).map(([k, n]) => `${k} ×${n}`).join(", ") || "—" },
          { key: "first", label: "First", fmt: F.datetime }, { key: "last", label: "Last", fmt: F.datetime }] })),
    card({ title: "Built-in slash commands", span: 6 },
      sk.slash_commands.rows.length ? dataTable({ search: false, rows: sk.slash_commands.rows,
        columns: [{ key: "ts", label: "When", fmt: F.datetime }, { key: "turn", label: "Turn", num: true }, { key: "name", label: "Command", fmt: (v) => "/" + v },
          { key: "args", label: "Args", cls: "wrap" }, { key: "output", label: "Output", cls: "wrap" }] }) : h("div", { class: "empty" }, "None typed.")),
    card({ title: `Available but not used (${sk.unused_available.length})`, span: 6 },
      sk.unused_available.length ? h("div", { class: "pills" }, sk.unused_available.map((n) => h("span", { class: "pill" }, n))) : h("div", { class: "empty" }, "Every listed skill was used, or none were listed.")),
    sk.restored_after_compaction.length || sk.dynamically_discovered.length ? card({ title: "Re-injected and discovered skills", span: 12 },
      kv([["Re-injected after compaction", sk.restored_after_compaction.map((r) => `${r.name} (${F.time(r.ts)})`).join(", ") || "—"],
        ["Discovered in subdirectories", sk.dynamically_discovered.map((d) => `${d.names.join(", ")} (${d.dir})`).join("; ") || "—"]])) : null);
}

function toolCallsTable() {
  return dataTable({
    rows: TOOL_ROWS, limit: 150, sortKey: "ts_ms", sortDir: "asc",
    selects: [{ key: "name", label: "tools" }, { key: "status", label: "statuses" }, { key: "scope", label: "scopes" }, { key: "category", label: "categories" }],
    columns: [
      { key: "i", label: "#", num: true }, { key: "ts_ms", label: "When", fmt: F.time }, { key: "turn", label: "Turn", num: true },
      { key: "scope", label: "Scope" }, { key: "name", label: "Tool" }, { key: "status", label: "Status", render: (r) => statusBadge(r.status) },
      { key: "duration_ms", label: "Duration", num: true, fmt: F.dur }, { key: "batch_size", label: "Batch", num: true },
      { key: "input", label: "Input", cls: "code" }, { key: "description", label: "Why (Bash)", cls: "wrap" },
      { key: "result_chars", label: "Result chars", num: true, fmt: F.tok }, { key: "error", label: "Error", cls: "code" },
      { key: "skill", label: "Skill" }, { key: "agent_id", label: "Agent", fmt: (v) => (v ? v.slice(0, 10) : "—") }],
  });
}

function buildTools() {
  const tl = D.tools;
  const cats = Object.entries(tl.by_category).map(([k, v]) => Object.assign({ category: k }, v));
  const mcp = Object.entries(tl.mcp_servers).map(([k, v]) => Object.assign({ server: k }, v));
  const targets = Object.entries(tl.top_targets).filter(([k]) => ["Bash", "Read", "Edit", "Write", "Grep", "Glob", "WebFetch", "Agent"].includes(k));
  return h("div", { class: "grid" },
    chartCard({ title: "Calls by tool", sub: "Main thread and agents; errors flagged", span: 7,
      chart: () => toolBarList(30),
      table: () => dataTable({ search: false, rows: tl.by_tool, columns: [
        { key: "name", label: "Tool" }, { key: "category", label: "Category" }, { key: "calls", label: "Calls", num: true },
        { key: "main", label: "Main", num: true }, { key: "subagent", label: "Agents", num: true }, { key: "error", label: "Errors", num: true },
        { key: "denied", label: "Denied", num: true }, { key: "interrupted", label: "Interrupted", num: true },
        { key: "p50", label: "p50", num: true, sort: (r) => r.duration_ms.p50, render: (r) => F.dur(r.duration_ms.p50) },
        { key: "p90", label: "p90", num: true, sort: (r) => r.duration_ms.p90, render: (r) => F.dur(r.duration_ms.p90) },
        { key: "max", label: "Max", num: true, sort: (r) => r.duration_ms.max, render: (r) => F.dur(r.duration_ms.max) },
        { key: "parallel", label: "In parallel batches", num: true }, { key: "result_chars", label: "Result chars", num: true, fmt: F.tok }] }) }),
    card({ title: "By category", span: 5 },
      dataTable({ search: false, rows: cats, columns: [{ key: "category", label: "Category" }, { key: "calls", label: "Calls", num: true },
        { key: "errors", label: "Errors", num: true }, { key: "tools", label: "Tools", num: true }] }),
      h("div", { style: "margin-top:12px" }, kv([
        ["Status", Object.entries(tl.by_status).map(([k, v]) => `${k} ×${v}`).join(", ")],
        ["Parallel calls", `${tl.parallel_batches.parallel_calls} (largest batch ${tl.parallel_batches.max})`],
        ["Batch sizes", Object.entries(tl.parallel_batches.distribution).map(([k, v]) => `${k}: ${v}`).join(", ") || "—"],
        ["Denials", Object.entries(tl.denials).map(([k, v]) => `${k} ×${v}`).join(", ") || "none"]]))),
    mcp.length ? card({ title: "MCP servers", span: 6 }, dataTable({ search: false, rows: mcp, columns: [{ key: "server", label: "Server" },
      { key: "calls", label: "Calls", num: true }, { key: "errors", label: "Errors", num: true },
      { key: "tools", label: "Tools", cls: "wrap", fmt: (v) => Object.entries(v).map(([k, n]) => `${k} ×${n}`).join(", ") }] })) : null,
    card({ title: "Most common sequences", sub: "Consecutive main-thread tool calls", span: mcp.length ? 6 : 6 },
      dataTable({ search: false, limit: 15, rows: tl.transitions, columns: [{ key: "from", label: "From" }, { key: "to", label: "Then" }, { key: "count", label: "Times", num: true }] })),
    card({ title: "Deferred tools loaded via ToolSearch", span: 6 }, pills(tl.deferred_tools_loaded)),
    targets.length ? card({ title: "Top targets", sub: "What each tool acted on most", span: 12 },
      h("div", { class: "grid" }, targets.map(([name, list]) => h("div", { class: "span-6" }, h("h2", { style: "font-size:13px;margin:4px 0 6px" }, name),
        barList(list, { label: (x) => x.target, value: (x) => x.count, limit: 8 }))))) : null,
    card({ title: "All tool calls", span: 12 }, toolCallsTable()));
}

function buildCost() {
  const tk = D.tokens, co = D.cost, rep = D.reported;
  const models = Object.entries(tk.by_model).map(([m, u]) => Object.assign({ model: m }, u));
  const scope = (obj) => Object.entries(obj).map(([k, v]) => ({ k, v }));
  const misses = Object.entries(tk.cache.miss_reasons).map(([k, v]) => Object.assign({ reason: k }, v));
  const turnRows = co.top_turns.map((t) => Object.assign({}, t, { prompt: (D.turns.rows[t.turn] || {}).prompt }));
  return h("div", { class: "grid" },
    card({ title: "Tokens by model", span: 12 },
      dataTable({ search: false, rows: models, columns: [
        { key: "model", label: "Model" }, { key: "requests", label: "Requests", num: true }, { key: "input", label: "Input", num: true, fmt: F.tok },
        { key: "output", label: "Output", num: true, fmt: F.tok }, { key: "thinking", label: "Thinking", num: true, fmt: F.tok },
        { key: "cache_read", label: "Cache read", num: true, fmt: F.tok }, { key: "cache_write_5m", label: "Write 5m", num: true, fmt: F.tok },
        { key: "cache_write_1h", label: "Write 1h", num: true, fmt: F.tok }, { key: "cache_hit_ratio", label: "Hit ratio", num: true, fmt: F.pct },
        { key: "web_search", label: "Web searches", num: true }, { key: "cost", label: "Cost", num: true, fmt: F.usd }] })),
    costCard(6),
    card({ title: "Cost split", span: 6 },
      h("div", { class: "grid" },
        h("div", { class: "span-6" }, h("h2", { style: "font-size:13px;margin:0 0 6px" }, "By scope"), barList(scope(co.by_scope), { label: (x) => x.k, value: (x) => x.v, fmt: F.usd })),
        h("div", { class: "span-6" }, h("h2", { style: "font-size:13px;margin:0 0 6px" }, "By agent"), barList(scope(co.by_agent), { label: (x) => x.k, value: (x) => x.v, fmt: F.usd, limit: 8 })),
        Object.keys(co.by_mcp_server).length ? h("div", { class: "span-12" }, h("h2", { style: "font-size:13px;margin:6px 0 6px" }, "By MCP server (attributed)"), barList(scope(co.by_mcp_server), { label: (x) => x.k, value: (x) => x.v, fmt: F.usd })) : null)),
    card({ title: "Prompt cache", span: 6 },
      kv([["Hit ratio", F.pct(tk.cache.hit_ratio)], ["Cache reads", F.tok(tk.cache.read)], ["Cache writes (5m / 1h)", `${F.tok(tk.cache.write_5m)} / ${F.tok(tk.cache.write_1h)}`],
        ["Uncached input", F.tok(tk.cache.uncached_input)], ["Requests with a cache read", `${tk.cache.requests_with_cache_read} of ${tk.cache.requests_with_cache_read + tk.cache.requests_without_cache_read}`],
        ["Thinking share of output", F.pct(tk.output.thinking_share)], ["Service tiers", Object.keys(tk.service_tiers).join(", ") || "—"], ["Speed", Object.keys(tk.speeds).join(", ") || "—"]]),
      misses.length ? h("div", { style: "margin-top:12px" }, dataTable({ search: false, rows: misses, sortKey: "missed_tokens",
        columns: [{ key: "reason", label: "Cache miss reason" }, { key: "requests", label: "Requests", num: true }, { key: "missed_tokens", label: "Missed tokens", num: true, fmt: F.tok }] })) : null),
    rep ? card({ title: "Claude Code's own accounting", sub: rep.scope_note, span: 6 },
      kv([["Reported cost", F.usd(rep.total_cost_usd)], ["Transcript estimate", F.usd(rep.reconciliation.transcript_estimate_usd)],
        ["Difference", F.usd(rep.reconciliation.delta_usd)], ["API time", F.dur(rep.api_duration_ms)], ["API time without retries", F.dur(rep.api_duration_without_retries_ms)],
        ["Tool time", F.dur(rep.tool_duration_ms)], ["Lines", `+${F.num(rep.lines_added)} / −${F.num(rep.lines_removed)}`], ["Counting since", F.datetime(rep.start)]]),
      h("div", { style: "margin-top:12px" }, dataTable({ search: false, rows: rep.reconciliation.by_model, columns: [
        { key: "model", label: "Model" }, { key: "reported_cost", label: "Reported", num: true, fmt: F.usd }, { key: "transcript_cost", label: "Transcript", num: true, fmt: F.usd },
        { key: "reported_output", label: "Out (rep.)", num: true, fmt: F.tok }, { key: "transcript_output", label: "Out (tx)", num: true, fmt: F.tok }] })),
      h("div", { class: "note" }, rep.reconciliation.note))
      : card({ title: "Claude Code's own accounting", span: 6 }, h("div", { class: "empty" }, "No cost-state event yet — Claude Code writes one when a session idles or exits.")),
    chartCard({ title: "Most expensive turns", span: 6, empty: turnRows.length ? null : "No turns.",
      chart: () => barList(turnRows, { label: (t) => `#${t.turn} ${F.short(t.prompt, 40)}`, value: (t) => t.usd, fmt: F.usd,
        tip: (t) => ({ title: `Turn ${t.turn}`, rows: [[F.usd(t.usd), "cost"]], body: t.prompt || "" }) }),
      table: () => dataTable({ search: false, rows: turnRows, columns: [{ key: "turn", label: "Turn", num: true }, { key: "usd", label: "Cost", num: true, fmt: F.usd }, { key: "prompt", label: "Prompt", cls: "wrap" }] }) }),
    card({ title: "API requests", sub: "One row per model response (streamed lines merged)", span: 12 },
      dataTable({ rows: D.requests.rows, limit: 100, sortKey: "ts_ms", sortDir: "asc", selects: [{ key: "model", label: "models" }, { key: "scope", label: "scopes" }, { key: "stop_reason", label: "stop reasons" }, { key: "skill", label: "skills" }],
        columns: [{ key: "i", label: "#", num: true }, { key: "ts_ms", label: "When", fmt: F.time }, { key: "turn", label: "Turn", num: true }, { key: "scope", label: "Scope" },
          { key: "model", label: "Model" }, { key: "stop_reason", label: "Stop" }, { key: "input", label: "Input", num: true, fmt: F.tok }, { key: "cache_read", label: "Cache read", num: true, fmt: F.tok },
          { key: "cache_write_1h", label: "Write 1h", num: true, fmt: F.tok }, { key: "cache_write_5m", label: "Write 5m", num: true, fmt: F.tok }, { key: "output", label: "Output", num: true, fmt: F.tok },
          { key: "thinking", label: "Thinking", num: true, fmt: F.tok }, { key: "context", label: "Context", num: true, fmt: F.tok }, { key: "cost_usd", label: "Cost", num: true, fmt: F.usd },
          { key: "latency_ms", label: "Latency", num: true, fmt: F.dur }, { key: "duration_ms", label: "Duration", num: true, fmt: F.dur },
          { key: "tools", label: "Tools", cls: "wrap", fmt: (v) => v.join(", ") || "—" }, { key: "skill", label: "Skill" }, { key: "agent", label: "Agent" },
          { key: "effort", label: "Effort" }, { key: "cache_miss_reason", label: "Cache miss" }] })),
    card({ title: "Pricing used", sub: co.pricing.unit + " · cache writes " + co.pricing.cache_write_5m_multiplier + "× (5m) / " + co.pricing.cache_write_1h_multiplier + "× (1h) input", span: 12, note: co.notes.join(" ") },
      dataTable({ search: false, rows: co.pricing.rates, limit: 50, columns: [{ key: "model_prefix", label: "Model prefix" }, { key: "input", label: "Input", num: true },
        { key: "output", label: "Output", num: true }, { key: "cache_read", label: "Cache read", num: true }, { key: "cache_write_5m", label: "Write 5m", num: true },
        { key: "cache_write_1h", label: "Write 1h", num: true }, { key: "source", label: "Source" }] })));
}

function buildTurns() {
  const rows = D.turns.rows;
  return h("div", { class: "grid" },
    chartCard({ title: "Cost per turn", span: 12, empty: rows.length ? null : "No turns.",
      chart: (w) => columnsChart(w, rows, { value: (r) => r.cost_usd || 0, fmtY: F.usd, height: 160,
        tip: (r) => ({ title: `Turn ${r.index} · ${r.trigger}`, rows: [[F.usd(r.cost_usd), "cost"], [F.dur(r.duration_ms), "duration"], [F.num(r.tool_calls + r.subagent_tool_calls), "tool calls"], [F.tok(r.output_tokens), "output"]], body: r.prompt || (r.command ? "/" + r.command : "") }) }),
      table: () => dataTable({ search: false, rows, columns: [{ key: "index", label: "Turn", num: true }, { key: "cost_usd", label: "Cost", num: true, fmt: F.usd }] }) }),
    card({ title: "Turns", sub: `${D.turns.count} turns · ${Object.entries(D.turns.by_trigger).map(([k, v]) => `${k} ×${v}`).join(", ")} · median ${F.dur(D.turns.duration_ms.p50)}`, span: 12 },
      dataTable({ rows, limit: 200, sortKey: "index", sortDir: "asc", selects: [{ key: "trigger", label: "triggers" }],
        columns: [{ key: "index", label: "#", num: true }, { key: "start_ms", label: "Start", fmt: F.datetime }, { key: "duration_ms", label: "Duration", num: true, fmt: F.dur },
          { key: "trigger", label: "Trigger" }, { key: "prompt", label: "Prompt", cls: "wrap", render: (r) => r.prompt || (r.command ? "/" + r.command + " " + (r.command_args || "") : "—") },
          { key: "requests", label: "Requests", num: true }, { key: "tool_calls", label: "Tools", num: true }, { key: "subagent_tool_calls", label: "Agent tools", num: true },
          { key: "tool_errors", label: "Errors", num: true }, { key: "skills_invoked", label: "Skills", fmt: (v, r) => (v.length ? v : r.skills_attributed).join(", ") || "—" },
          { key: "tools", label: "Tool mix", cls: "wrap", fmt: (v) => Object.entries(v).map(([k, n]) => `${k} ×${n}`).join(", ") || "—" },
          { key: "output_tokens", label: "Output", num: true, fmt: F.tok }, { key: "max_context_tokens", label: "Max context", num: true, fmt: F.tok },
          { key: "cost_usd", label: "Cost", num: true, fmt: F.usd },
          { key: "flags", label: "Flags", render: (r) => [r.interrupted ? "interrupted" : "", r.compacted ? "compacted" : "", r.in_progress ? "in progress" : "", r.queued_prompts_absorbed ? `+${r.queued_prompts_absorbed} queued` : ""].filter(Boolean).join(", ") || "—" }] })));
}

function buildAgents() {
  const sa = D.subagents, wf = D.workflows;
  const types = Object.entries(sa.by_type).map(([k, v]) => Object.assign({ type: k }, v));
  return h("div", { class: "grid" },
    card({ title: "Subagents", sub: `${sa.count} subagents, ${sa.workflow_agents} workflow agents, ${sa.launched} Agent tool launches`, span: 12 },
      sa.rows.length ? dataTable({ rows: sa.rows, limit: 100, sortKey: "start_ms", sortDir: "asc", selects: [{ key: "kind", label: "kinds" }, { key: "type", label: "types" }],
        columns: [{ key: "agent_id", label: "Agent", fmt: (v) => v.slice(0, 12) }, { key: "kind", label: "Kind" }, { key: "type", label: "Type" },
          { key: "description", label: "Description", cls: "wrap" }, { key: "launched_in_turn", label: "Turn", num: true }, { key: "start_ms", label: "Start", fmt: F.time },
          { key: "duration_ms", label: "Duration", num: true, fmt: F.dur }, { key: "requests", label: "Requests", num: true }, { key: "tool_calls", label: "Tools", num: true },
          { key: "tool_errors", label: "Errors", num: true }, { key: "final_context_tokens", label: "Final context", num: true, fmt: F.tok },
          { key: "output_tokens", label: "Output", num: true, fmt: F.tok }, { key: "cost_usd", label: "Cost", num: true, fmt: F.usd },
          { key: "models", label: "Models", fmt: (v) => v.join(", ") }, { key: "skills", label: "Skills", fmt: (v) => v.join(", ") || "—" },
          { key: "tools", label: "Tool mix", cls: "wrap", fmt: (v) => Object.entries(v).map(([k, n]) => `${k} ×${n}`).join(", ") || "—" },
          { key: "task_prompt", label: "Task prompt", cls: "wrap" }] }) : h("div", { class: "empty" }, "No subagents in this session.")),
    types.length ? chartCard({ title: "Cost by agent type", span: 6,
      chart: () => barList(types, { label: (t) => t.type, value: (t) => t.cost_usd, fmt: F.usd,
        tip: (t) => ({ title: t.type, rows: [[F.num(t.agents), "agents"], [F.num(t.requests), "requests"], [F.num(t.tool_calls), "tool calls"], [F.usd(t.cost_usd), "cost"]] }) }),
      table: () => dataTable({ search: false, rows: types, columns: [{ key: "type", label: "Type" }, { key: "agents", label: "Agents", num: true }, { key: "requests", label: "Requests", num: true }, { key: "tool_calls", label: "Tool calls", num: true }, { key: "cost_usd", label: "Cost", num: true, fmt: F.usd }] }) }) : null,
    sa.agent_types_available.length ? card({ title: "Agent types available", span: 6 }, h("div", { class: "pills" }, sa.agent_types_available.map((t) => h("span", { class: "pill" }, t)))) : null,
    card({ title: "Workflows", span: 12 },
      wf.rows.length ? dataTable({ rows: wf.rows, search: false, columns: [{ key: "run", label: "Run" }, { key: "name", label: "Name" }, { key: "status", label: "Status" },
        { key: "launched_in_turn", label: "Turn", num: true }, { key: "duration_ms", label: "Duration", num: true, fmt: F.dur },
        { key: "reported_agents", label: "Agents", num: true }, { key: "requests", label: "Requests", num: true }, { key: "tool_calls", label: "Tool calls", num: true },
        { key: "cost_usd", label: "Cost", num: true, fmt: F.usd }, { key: "reported_total_tokens", label: "Reported tokens", num: true, fmt: F.tok },
        { key: "phases", label: "Phases", fmt: (v) => v.join(" → "), cls: "wrap" }, { key: "journal", label: "Agent outcomes", fmt: (v) => Object.entries(v).map(([k, n]) => `${k} ×${n}`).join(", ") || "—" },
        { key: "summary", label: "Summary", cls: "wrap" }] }) : h("div", { class: "empty" }, "No workflow runs.")),
    sa.launches_without_transcript.length ? card({ title: "Agent launches without a transcript", sub: "Background agents still running, or transcripts removed", span: 12 },
      dataTable({ search: false, rows: sa.launches_without_transcript, columns: [{ key: "ts", label: "When", fmt: F.datetime }, { key: "turn", label: "Turn", num: true },
        { key: "type", label: "Type" }, { key: "description", label: "Description", cls: "wrap" }, { key: "status", label: "Status" }] })) : null);
}

function divBar(r, max) {
  const add = r.lines_added + r.bash_lines_added, rem = r.lines_removed + r.bash_lines_removed;
  const cell = h("div", { class: "div-cell", title: `+${add} / −${rem}` },
    h("div", { class: "div-neg" }, h("span", { style: `width:${max ? (rem / max) * 100 : 0}%` })),
    h("div", { class: "div-pos" }, h("span", { style: `width:${max ? (add / max) * 100 : 0}%` })));
  return h("div", { style: "display:flex;gap:8px;align-items:center" }, cell, h("span", { class: "mono", style: "white-space:nowrap" }, `+${add} −${rem}`));
}

function buildFiles() {
  const fi = D.files, g = D.git, ou = D.outputs;
  const max = Math.max(1, ...fi.rows.map((r) => Math.max(r.lines_added + r.bash_lines_added, r.lines_removed + r.bash_lines_removed)));
  const ext = Object.entries(fi.by_extension_modified).map(([k, v]) => ({ k, v }));
  return h("div", { class: "grid" },
    card({ title: "Summary", span: 5, note: fi.note },
      kv([["Files read", `${F.num(fi.unique_read)} (${F.num(fi.lines_read)} lines)`], ["Files modified", F.num(fi.unique_modified)], ["Files created", F.num(fi.unique_created)],
        ["Edit tools", `+${F.num(fi.lines_added)} / −${F.num(fi.lines_removed)}`], ["Shell edits", `+${F.num(fi.bash_lines_added)} / −${F.num(fi.bash_lines_removed)}`],
        ["Checkpoints", `${fi.checkpoints.snapshots} snapshots, ${fi.checkpoints.tracked_files} files tracked`],
        ["Changed outside the edit tools", Object.keys(fi.changed_outside_edit_tools).join(", ") || "—"], ["@-mentioned by you", Object.keys(fi.mentioned_by_user).join(", ") || "—"],
        ["Opened in IDE", Object.keys(fi.opened_in_ide).join(", ") || "—"], ["Large outputs saved to disk", `${fi.persisted_tool_outputs.count} (${F.tok(fi.persisted_tool_outputs.bytes)} bytes)`]])),
    chartCard({ title: "Modified files by extension", span: 7, empty: ext.length ? null : "No files modified.",
      chart: () => barList(ext, { label: (x) => x.k, value: (x) => x.v }),
      table: () => dataTable({ search: false, rows: ext, columns: [{ key: "k", label: "Extension" }, { key: "v", label: "Files", num: true }] }) }),
    card({ title: "Files", sub: "Lines removed (left) and added (right), edit tools plus shell edits", span: 12 },
      fi.rows.length ? dataTable({ rows: fi.rows, limit: 100, sortKey: "lines_added",
        columns: [{ key: "path", label: "File", cls: "code" }, { key: "reads", label: "Reads", num: true }, { key: "edits", label: "Edits", num: true },
          { key: "writes", label: "Writes", num: true }, { key: "bash_edits", label: "Shell edits", num: true },
          { key: "lines_added", label: "Change", render: (r) => divBar(r, max), sort: (r) => r.lines_added + r.bash_lines_added + r.lines_removed + r.bash_lines_removed },
          { key: "lines_read", label: "Lines read", num: true }, { key: "errors", label: "Errors", num: true }, { key: "last", label: "Last touched", fmt: F.time }] }) : h("div", { class: "empty" }, "No file activity.")),
    card({ title: "Git", span: 6 },
      kv([["Commits", String(g.counts.commits)], ["Pushes", String(g.counts.pushes)], ["PR operations", String(g.counts.pr_operations)], ["PRs linked", String(g.counts.pr_links)]]),
      h("div", { style: "margin-top:10px" }, pills(g.git_subcommands)),
      Object.keys(g.gh_subcommands).length ? h("div", { style: "margin-top:8px" }, pills(Object.fromEntries(Object.entries(g.gh_subcommands).map(([k, v]) => ["gh " + k, v])))) : null,
      g.commits.length ? h("div", { style: "margin-top:12px" }, dataTable({ search: false, rows: g.commits, columns: [{ key: "ts", label: "When", fmt: F.datetime }, { key: "turn", label: "Turn", num: true },
        { key: "sha", label: "SHA", fmt: (v) => (v || "").slice(0, 10) }, { key: "branch", label: "Branch" }, { key: "kind", label: "Kind" }] })) : null),
    card({ title: "Outputs", span: 6 },
      (g.pr_links.length || g.pull_requests.length) ? dataTable({ search: false, rows: g.pr_links.length ? g.pr_links : g.pull_requests,
        columns: [{ key: "url", label: "Pull request", render: (r) => h("a", { href: r.url, target: "_blank", rel: "noopener" }, r.url) }, { key: "action", label: "Action" }] }) : null,
      ou.artifacts.length ? dataTable({ search: false, rows: ou.artifacts, columns: [{ key: "title", label: "Artifact" }, { key: "version", label: "Version", num: true },
        { key: "url", label: "URL", render: (r) => r.url ? h("a", { href: r.url, target: "_blank", rel: "noopener" }, "open") : "—" }] }) : null,
      ou.files_sent_to_user.length ? kv(ou.files_sent_to_user.map((x) => [F.time(x.ts), x.files.join(", ")])) : null,
      ou.plans.length ? kv(ou.plans.map((p) => ["Plan", `${p.path || "—"} (${F.num(p.chars)} chars, ${p.status})`])) : null,
      !(g.pr_links.length || g.pull_requests.length || ou.artifacts.length || ou.files_sent_to_user.length || ou.plans.length) ? h("div", { class: "empty" }, "No PRs, artifacts, files sent or plans.") : null));
}

function buildShell() {
  const sh = D.shell, w = D.web;
  const progs = Object.entries(sh.primary_programs).map(([k, v]) => ({ k, v }));
  const domains = Object.entries(w.domains).map(([k, v]) => ({ k, v }));
  return h("div", { class: "grid" },
    chartCard({ title: "Programs run", sub: "The program each command was about (after cd/export)", span: 6, empty: progs.length ? null : "No shell commands.",
      chart: () => barList(progs, { label: (x) => x.k, value: (x) => x.v }),
      table: () => dataTable({ search: false, rows: Object.entries(sh.programs).map(([k, v]) => ({ k, v })), columns: [{ key: "k", label: "Program (every segment)" }, { key: "v", label: "Uses", num: true }] }) }),
    card({ title: "Shell summary", span: 6 },
      kv([["Commands", F.num(sh.commands)], ["Failed", F.num(sh.errors)], ["Denied", F.num(sh.denied)], ["Interrupted", F.num(sh.interrupted)],
        ["Background", F.num(sh.background)], ["Timed out", F.num(sh.timed_out)], ["Sandbox overrides", F.num(sh.sandbox_disabled)], ["Custom timeout", F.num(sh.custom_timeout)],
        ["With a description", F.num(sh.with_description)], ["Output", `${F.tok(sh.stdout_chars)} stdout · ${F.tok(sh.stderr_chars)} stderr chars`],
        ["Exit codes", Object.entries(sh.exit_codes).map(([k, v]) => `${k} ×${v}`).join(", ") || "—"],
        ["Duration", `p50 ${F.dur(sh.duration_ms.p50)} · p90 ${F.dur(sh.duration_ms.p90)} · max ${F.dur(sh.duration_ms.max)}`]]),
      h("div", { style: "margin-top:10px" }, pills(sh.subcommands))),
    card({ title: "Slowest commands", span: 12 },
      dataTable({ search: false, rows: sh.slowest, columns: [{ key: "duration_ms", label: "Duration", num: true, fmt: F.dur }, { key: "turn", label: "Turn", num: true },
        { key: "status", label: "Status", render: (r) => statusBadge(r.status) }, { key: "command", label: "Command", cls: "code" }] })),
    sh.failing.length ? card({ title: "Failing commands", span: 12 },
      dataTable({ search: false, rows: sh.failing, columns: [{ key: "turn", label: "Turn", num: true }, { key: "exit_code", label: "Exit", num: true },
        { key: "command", label: "Command", cls: "code" }, { key: "error", label: "Output", cls: "code" }] })) : null,
    chartCard({ title: "Web fetches by domain", sub: `${w.fetches} fetches · ${w.fetch_errors} failed · ${F.tok(w.bytes)} bytes`, span: 6, empty: domains.length ? null : "No web fetches.",
      chart: () => barList(domains, { label: (x) => x.k, value: (x) => x.v }),
      table: () => dataTable({ rows: w.fetch_rows, columns: [{ key: "ts", label: "When", fmt: F.time }, { key: "url", label: "URL", cls: "code" }, { key: "code", label: "HTTP", num: true },
        { key: "bytes", label: "Bytes", num: true, fmt: F.tok }, { key: "duration_ms", label: "Duration", num: true, fmt: F.dur }] }) }),
    card({ title: "Web searches", sub: `${w.searches} searches · server-side: ${w.server_side.web_search_requests} search, ${w.server_side.web_fetch_requests} fetch`, span: 6 },
      w.search_rows.length ? dataTable({ search: false, rows: w.search_rows, columns: [{ key: "ts", label: "When", fmt: F.time }, { key: "query", label: "Query", cls: "wrap" },
        { key: "results", label: "Results", num: true }, { key: "duration_s", label: "Seconds", num: true }] }) : h("div", { class: "empty" }, "No searches.")));
}

function buildErrors() {
  const er = D.errors, hk = D.hooks, pl = D.planning, us = D.user;
  const cats = Object.entries(er.by_category).map(([k, v]) => ({ k, v }));
  const hookRows = Object.entries(hk.by_name).map(([k, v]) => ({ name: k, runs: v }));
  return h("div", { class: "grid" },
    chartCard({ title: "Tool errors by category", sub: `${er.tool_errors} errors (${F.pct(er.tool_error_rate)} of calls) · ${er.denied} denied · ${er.interrupted} interrupted`, span: 6,
      empty: cats.length ? null : "No tool errors.",
      chart: () => barList(cats, { label: (x) => x.k.replace(/_/g, " "), value: (x) => x.v }),
      table: () => dataTable({ search: false, rows: cats, columns: [{ key: "k", label: "Category" }, { key: "v", label: "Errors", num: true }] }) }),
    card({ title: "By tool, and denials", span: 6 },
      h("h2", { style: "font-size:13px;margin:0 0 6px" }, "Errors by tool"), pills(er.by_tool),
      h("h2", { style: "font-size:13px;margin:12px 0 6px" }, "Permission denials"), pills(us.tool_denials),
      h("h2", { style: "font-size:13px;margin:12px 0 6px" }, "API errors"), pills(er.api_error_statuses)),
    card({ title: "Tool errors", span: 12 },
      er.rows.length ? dataTable({ rows: er.rows, limit: 60, selects: [{ key: "tool", label: "tools" }, { key: "category", label: "categories" }],
        columns: [{ key: "ts", label: "When", fmt: F.time }, { key: "turn", label: "Turn", num: true }, { key: "tool", label: "Tool" }, { key: "category", label: "Category" },
          { key: "input", label: "Input", cls: "code" }, { key: "message", label: "Message", cls: "code" }, { key: "scope", label: "Scope" }] }) : h("div", { class: "empty" }, "No tool errors.")),
    card({ title: "API errors and retries", span: 6 },
      er.api_error_rows.length ? dataTable({ search: false, rows: er.api_error_rows, columns: [{ key: "ts", label: "When", fmt: F.time }, { key: "status", label: "Status" },
        { key: "connection_code", label: "Connection" }, { key: "retry_attempt", label: "Attempt", num: true }, { key: "source", label: "Source" }, { key: "message", label: "Message", cls: "wrap" }] })
        : h("div", { class: "empty" }, "No API errors."),
      er.rate_limit_events.length ? h("div", { style: "margin-top:10px" }, dataTable({ search: false, rows: er.rate_limit_events, columns: [{ key: "ts", label: "When", fmt: F.time }, { key: "status", label: "Status" }, { key: "type", label: "Limit" }] })) : null,
      er.refusal_fallbacks.length ? kv(er.refusal_fallbacks.map((r) => ["Refusal fallback", `${r.from} → ${r.to} (${r.category || "?"})`])) : null),
    card({ title: "Hooks", span: 6 },
      kv([["Hook runs", `${hk.runs} (${Object.entries(hk.by_event).map(([k, v]) => `${k} ×${v}`).join(", ") || "none"})`], ["Non-zero exits", F.num(hk.non_zero_exit)],
        ["Duration", `p50 ${F.dur(hk.duration_ms.p50)} · max ${F.dur(hk.duration_ms.max)}`],
        ["Stop hooks", `${hk.stop_hooks.summaries} summaries · ${hk.stop_hooks.hooks_run} runs · ${hk.stop_hooks.errors} errors · ${hk.stop_hooks.prevented_continuation} prevented stop`]]),
      hookRows.length ? h("div", { style: "margin-top:10px" }, dataTable({ search: false, rows: hookRows, columns: [{ key: "name", label: "Hook" }, { key: "runs", label: "Runs", num: true }] })) : null),
    card({ title: "Questions Claude asked you", span: 12 },
      pl.questions.length ? dataTable({ search: false, rows: pl.questions.flatMap((q) => q.questions.map((qq) => ({ ts: q.ts, turn: q.turn, header: qq.header, question: qq.question, answer: q.answers ? q.answers[qq.question] : null, options: qq.options }))),
        columns: [{ key: "ts", label: "When", fmt: F.time }, { key: "turn", label: "Turn", num: true }, { key: "header", label: "Topic" }, { key: "question", label: "Question", cls: "wrap" },
          { key: "options", label: "Options", num: true }, { key: "answer", label: "Your answer", cls: "wrap" }] }) : h("div", { class: "empty" }, "No questions asked.")),
    card({ title: "Planning", span: 12 },
      kv([["Tasks", `${pl.tasks_created} created · ${pl.task_updates} updates · ${pl.tasks_completed} completed`], ["Transitions", Object.entries(pl.task_transitions).map(([k, v]) => `${k} ×${v}`).join(", ") || "—"],
        ["TodoWrite", `${pl.todo_writes} writes${pl.last_todo_status ? " · last: " + Object.entries(pl.last_todo_status).map(([k, v]) => `${k} ${v}`).join(", ") : ""}`],
        ["Plan mode", `entered ${pl.plan_mode.entered} · presented ${pl.plan_mode.plans_presented} · approved ${pl.plan_mode.plans_approved}`],
        ["Task subjects", pl.task_subjects.join(" · ") || "—"]])));
}

function buildContext() {
  const cx = D.context, us = D.user;
  const budget = cx.token_budget_left.filter((p) => p[0]);
  return h("div", { class: "grid" },
    contextCard(12, 220),
    budget.length ? chartCard({ title: "Token budget left", sub: "The countdown Claude Code shows the model", span: 12,
      chart: (w) => lineChart(w, budget, { fmtY: F.tok, stepped: true, t0: D.timeline.start_ms, t1: D.timeline.end_ms, tip: (p) => ({ title: F.datetime(p[0]), rows: [[F.tok(p[1]), "tokens left"]] }) }),
      table: () => dataTable({ search: false, limit: 300, rows: budget.map((p) => ({ t: p[0], left: p[1] })), columns: [{ key: "t", label: "Time", fmt: F.datetime }, { key: "left", label: "Tokens left", num: true, fmt: F.num }] }) }) : null,
    card({ title: "Compactions", span: 6 },
      cx.compactions.length ? dataTable({ search: false, rows: cx.compactions, columns: [{ key: "ts", label: "When", fmt: F.datetime }, { key: "turn", label: "Turn", num: true },
        { key: "trigger", label: "Trigger" }, { key: "pre_tokens", label: "Before", num: true, fmt: F.tok }, { key: "post_tokens", label: "After", num: true, fmt: F.tok },
        { key: "duration_ms", label: "Took", num: true, fmt: F.dur }] }) : h("div", { class: "empty" }, "No compactions.")),
    card({ title: "What was loaded into context", span: 6 },
      kv([["Instruction files", cx.instruction_files.map((i) => `${i.path} (${i.type}, ${F.tok(i.chars)} chars)`).join(", ") || "—"],
        ["Nested memory", Object.keys(cx.nested_memory_files).join(", ") || "—"],
        ["System prompt", `${F.tok(cx.system_prompt.last_chars)} chars · ${cx.system_prompt.snapshots} snapshots`],
        ["Tools available", cx.system_prompt.tools_available !== null ? String(cx.system_prompt.tools_available) : "—"],
        ["Skills listed", F.num(cx.skills_listed)], ["Agent types", cx.agent_types_listed.join(", ") || "—"],
        ["MCP servers with instructions", cx.mcp_servers.with_instructions.join(", ") || "—"],
        ["MCP failed / needs auth", [cx.mcp_servers.failed.join(", "), cx.mcp_servers.needs_auth.join(", ")].filter(Boolean).join(" · ") || "—"],
        ["Deferred tools announced", F.num(cx.deferred_tools_announced.length)], ["Environment changes", Object.entries(cx.environment_changes).map(([k, v]) => `${k} ×${v}`).join(", ") || "—"],
        ["Your think time", `p50 ${F.dur(us.think_time_ms.p50)} · p90 ${F.dur(us.think_time_ms.p90)}`]])),
    card({ title: "Context injections by type", sub: "Attachments Claude Code added around your prompts", span: 12 }, pills(cx.attachments)),
    cx.system_prompt.tool_names ? card({ title: "Tools in the last system prompt", span: 12 }, h("div", { class: "pills" }, cx.system_prompt.tool_names.map((t) => h("span", { class: "pill" }, t)))) : null);
}

function buildRaw() {
  const sc = D.schema_coverage;
  const events = [];
  for (const [scope, types] of Object.entries(sc.event_types)) for (const [t, n] of Object.entries(types)) events.push({ scope, type: t, count: n });
  const unknown = Object.entries(sc.unknown).filter(([, v]) => Object.keys(v).length);
  return h("div", { class: "grid" },
    card({ title: "Transcript files", sub: `${sc.files} files · ${F.num(sc.lines)} lines · ${sc.bad_lines} unparseable · ${F.tok(sc.bytes)} bytes`, span: 12,
      tools: downloadLink("Download session.json", `session-${S.id.slice(0, 8)}.json`, () => JSON.stringify(D, null, 2)) },
      dataTable({ rows: sc.sources, limit: 50, columns: [{ key: "path", label: "File", cls: "code" }, { key: "scope", label: "Scope" }, { key: "agent_id", label: "Agent" },
        { key: "workflow_run", label: "Workflow run" }, { key: "lines", label: "Lines", num: true }, { key: "bad_lines", label: "Bad", num: true }, { key: "bytes", label: "Bytes", num: true, fmt: F.tok }] })),
    card({ title: "Event types", span: 6 }, dataTable({ search: false, rows: events, limit: 60, sortKey: "count", columns: [{ key: "scope", label: "Scope" }, { key: "type", label: "Type" }, { key: "count", label: "Events", num: true }] })),
    card({ title: "System subtypes and attachments", span: 6 },
      h("h2", { style: "font-size:13px;margin:0 0 6px" }, "System subtypes"), pills(sc.system_subtypes),
      h("h2", { style: "font-size:13px;margin:12px 0 6px" }, "Attachment types"), pills(sc.attachment_types, F.num, 80),
      h("h2", { style: "font-size:13px;margin:12px 0 6px" }, "Not recognised by this version"),
      unknown.length ? kv(unknown.map(([k, v]) => [k, Object.entries(v).map(([a, b]) => `${a} ×${b}`).join(", ")])) : h("div", { class: "empty" }, "Everything was recognised.")),
    card({ title: "Generator", span: 12 },
      kv([["Schema", D.schema], ["Version", D.generator.version], ["Generated", F.datetime(D.generator.generated_at)], ["Redaction", D.generator.redaction ? `on (${D.generator.redacted_fields || 0} fields changed)` : "off"],
        ["Full content", D.generator.full_content ? "on" : "off"], ["Transcript", S.transcript], ["Unmatched tool results", String(sc.unmatched_tool_results)]]),
      h("details", { class: "raw", style: "margin-top:12px" }, h("summary", null, "Totals as JSON"), h("pre", { class: "json" }, JSON.stringify(D.totals, null, 2)))));
}


/* ------------------------------------------------------------------ skill runs & trace */

function buildInterview() {
  const iv = D.interview;
  if (!iv || !(iv.questions || []).length) return h("div", { class: "empty" }, "Claude asked you no questions in this session.");
  const rows = iv.questions;
  const order = Array.from(new Set(rows.map((q) => q.topic)));
  const empty = iv.no_preference + iv.declined + iv.unanswered;
  const tiles = [
    ["Questions", F.num(iv.total), `${iv.asked} through AskUserQuestion · ${iv.prose} in prose`],
    ["Rounds", F.num(iv.calls), "AskUserQuestion calls"],
    ["Recommended option taken", iv.recommended_offered ? `${iv.recommended_picked}/${iv.recommended_offered}` : "—",
      iv.recommended_offered ? `${F.pct(iv.recommended_rate)} of the questions that offered one` : "no question offered one"],
    ["Typed answers", F.num(iv.typed), "none of the options fit"],
    ["Came back empty", F.num(empty), `${iv.no_preference} no preference · ${iv.declined} declined · ${iv.unanswered} unanswered`],
    ["Median wait", F.dur(iv.wait_p50_ms), iv.wait_max_ms ? `longest ${F.dur(iv.wait_max_ms)}` : "for an answer"],
  ];
  const kpiRow = h("div", { class: "kpis" }, tiles.map(([l, v, sub]) => h("div", { class: "kpi" }, h("div", { class: "label" }, l), h("div", { class: "value" }, v), h("div", { class: "sub" }, sub))));
  return h("div", null, kpiRow, h("div", { class: "grid" },
    chartCard({ title: "The interview, round by round", sub: "One column per round (an AskUserQuestion call, or a reply asking in prose), one lane per topic; dashed lines mark where a skill run started and its first create",
      span: 12, legendEl: interviewLegend(), chart: (w) => interviewMap(w, rows, { order, markers: interviewMarkers(iv.runs) }),
      table: () => questionTable(rows, { showRun: true, limit: 200 }) }),
    card({ title: "By topic", sub: "What came back, per topic", span: 7 }, topicOutcomeBars(rows, order)),
    card({ title: "Against the skill's rules", sub: "Questions flagged: a missing recommendation, no measured numbers, jargon, asked again, in prose…", span: 5 }, flagBars(iv.flags)),
    card({ title: "Every question", sub: "In order; the options show what was offered (✓ the pick), the answer what came back", span: 12 }, questionTable(rows, { showRun: true, limit: 200 }))));
}

function buildSkillRuns() {
  const runs = D.skill_runs || [];
  if (!runs.length) return h("div", { class: "empty" }, "No skill was invoked in this session.");
  const steps = D.trace.steps;
  const panel = h("div");
  const open = (id) => {
    const run = runs.find((r) => r.run_id === id);
    panel.replaceChildren(h("h2", { style: "font-size:15px;margin:18px 0 10px" }, `Run ${id} · ${run.skill}`), runDetail(run, run.steps.map((i) => steps[i])));
    requestAnimationFrame(() => Charts.renderVisible());
  };
  const table = dataTable({ search: false, rows: runs, sortKey: "start_ms", sortDir: "asc", columns: [
    { key: "run_id", label: "Run", render: (r) => { const b = h("button", { class: "linkbtn", type: "button" }, r.run_id); b.addEventListener("click", () => open(r.run_id)); return b; } },
    { key: "skill", label: "Skill" }, { key: "mode", label: "Invoked by", render: (r) => modeBadge(r.mode) },
    { key: "version", label: "Version", fmt: (v) => (v.status === "commit" ? v.commit : v.label) },
    { key: "start_ms", label: "Started", fmt: F.time }, { key: "turn_count", label: "Turns", num: true },
    { key: "tool_calls", label: "Tools", num: true }, { key: "tool_errors", label: "Errors", num: true }, { key: "cli_calls", label: "CLI", num: true },
    { key: "question_calls", label: "Asked", num: true }, { key: "objects_created", label: "Created", num: true },
    { key: "support_files", label: "Support files", num: true },
    { key: "cost_usd", label: "Cost", num: true, fmt: F.usd }, { key: "checks_failed", label: "Checks ✗", num: true },
    { key: "end_reason", label: "Ended" }] });
  setTimeout(() => open(runs[0].run_id), 0);
  return h("div", null, card({ title: "Skill runs", sub: "A run lasts from the invocation until another skill takes over or the session ends, follow-up turns included", span: 12 }, table), panel);
}

function buildTrace() {
  return h("div", { class: "grid" }, card({ title: "Trace", sub: "Every prompt, Claude API request, tool call and notable event in order — click a row for its input and output", span: 12 },
    traceView(D.trace.steps)));
}

/* ------------------------------------------------------------------ mount */

(function mount() {
  const app = document.getElementById("app");
  const t = tabs([
    { id: "overview", label: "Overview", build: buildOverview },
    { id: "timeline", label: "Timeline", build: buildTimeline },
    { id: "trace", label: "Trace", count: D.trace.count, build: buildTrace },
    { id: "skills", label: "Skills", count: T.skills_invoked, build: buildSkills },
    { id: "runs", label: "Skill runs", count: (D.skill_runs || []).length, build: buildSkillRuns },
    { id: "interview", label: "Interview", count: (D.interview || {}).total || 0, build: buildInterview },
    { id: "tools", label: "Tools", count: T.tool_calls, build: buildTools },
    { id: "cost", label: "Tokens & cost", build: buildCost },
    { id: "turns", label: "Turns", count: T.turns, build: buildTurns },
    { id: "agents", label: "Agents", count: T.subagents + T.workflow_agents, build: buildAgents },
    { id: "files", label: "Files & git", count: T.files_modified, build: buildFiles },
    { id: "shell", label: "Shell & web", count: T.shell_commands, build: buildShell },
    { id: "errors", label: "Errors & questions", count: T.tool_errors + T.api_errors, build: buildErrors },
    { id: "context", label: "Context", build: buildContext },
    { id: "raw", label: "Raw", build: buildRaw },
  ]);
  app.append(buildHeader(), buildKpis(), buildInsights() || "", t.nav, ...t.sections,
    h("footer", { class: "foot" }, `convo-analysis ${D.generator.version} · generated ${F.datetime(D.generator.generated_at)} · costs are list-price estimates`));
  t.start();
})();
