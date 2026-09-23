"use strict";
/* Multi-session rollup dashboard. */

const D = JSON.parse(document.getElementById("session-data").textContent);
Tip.init();
const T = D.totals;

function header() {
  const sc = D.scope;
  return h("header", { class: "top" },
    h("div", { style: "min-width:0;flex:1" },
      h("h1", null, `Claude Code sessions — ${sc.label}`),
      h("div", { class: "meta" },
        h("span", null, `Since ${sc.since}${sc.cutoff ? " (" + F.datetime(sc.cutoff) + ")" : ""} → ${F.datetime(sc.until)}`),
        h("span", null, `${sc.transcripts_scanned} transcripts scanned`),
        h("span", null, `Claude Code ${D.claude_code_versions.slice(-3).join(", ")}`)),
      h("div", { class: "meta", style: "margin-top:6px" },
        h("span", { class: "badge" }, D.generator.redaction ? "Secrets redacted" : "Unredacted"),
        T.duplicate_requests_removed ? h("span", { class: "badge" }, `${F.num(T.duplicate_requests_removed)} duplicate requests removed`) : null)),
    themeToggle());
}

function kpis() {
  const tiles = [
    ["Estimated cost", F.usd(T.cost_usd), "API list-price equivalent"],
    ["Sessions", F.num(T.sessions || 0), `${F.num(T.turns || 0)} turns · ${F.num(T.prompts || 0)} prompts`],
    ["API requests", F.num(T.requests || 0), `${F.tok(T.output_tokens || 0)} output tokens`],
    ["Tool calls", F.num(T.tool_calls || 0), `${F.num(T.tool_errors || 0)} errors`],
    ["Skills invoked", F.num(T.skill_invocations || 0), `${T.distinct_skills || 0} distinct`],
    ["Subagents", F.num((T.subagents || 0) + (T.workflow_agents || 0)), `${F.num(T.workflow_agents || 0)} in workflows`],
    ["Active time", F.dur(T.active_ms || 0), "sum of turn durations"],
    ["Cache hit", F.pct(T.cache_hit_ratio), `${F.tok(T.cache_read_tokens || 0)} tokens read from cache`],
    ["Lines changed", `+${F.num(T.lines_added || 0)}`, `−${F.num(T.lines_removed || 0)} · ${F.num(T.commits || 0)} commits`],
  ];
  return h("div", { class: "kpis" }, tiles.map(([l, v, s]) =>
    h("div", { class: "kpi" }, h("div", { class: "label" }, l), h("div", { class: "value" }, v), h("div", { class: "sub" }, s))));
}

function dayColumns(key, fmt, title, sub) {
  const rows = D.by_day;
  return chartCard({ title, sub, span: 6, empty: rows.length ? null : "No activity in this window.",
    chart: (w) => columnsChart(w, rows.map((d) => Object.assign({ t: Date.parse(d.day + "T00:00:00") }, d)), {
      value: (r) => r[key], fmtY: fmt, t0: Date.parse(rows[0].day + "T00:00:00"), bucket: 86400e3,
      tip: (r) => ({ title: r.day, rows: [[F.usd(r.cost_usd), "cost"], [F.num(r.requests), "requests"], [F.num(r.tool_calls), "tool calls"], [F.num(r.prompts), "prompts"], [F.num(r.sessions), "sessions"], [F.dur(r.active_ms), "active"]] }),
    }),
    table: () => dataTable({ search: false, rows, limit: 400, sortKey: "day", sortDir: "asc", columns: [
      { key: "day", label: "Day" }, { key: "sessions", label: "Sessions", num: true }, { key: "prompts", label: "Prompts", num: true },
      { key: "requests", label: "Requests", num: true }, { key: "tool_calls", label: "Tool calls", num: true }, { key: "tool_errors", label: "Errors", num: true },
      { key: "output_tokens", label: "Output", num: true, fmt: F.tok }, { key: "cost_usd", label: "Cost", num: true, fmt: F.usd }, { key: "active_ms", label: "Active", num: true, fmt: F.dur }] }),
  });
}

function heatmap() {
  const hm = D.heatmap;
  const max = Math.max(1, ...hm.requests.flat());
  return chartCard({ title: "When you work", sub: "API requests by weekday and local hour (darker = more)", span: 12,
    chart: (w) => {
      const labelW = 40, top = 18, rowH = 20, gap = 2;
      const cw = Math.max(8, (w - labelW - 8) / 24);
      const H = top + 7 * (rowH + gap) + 4;
      const svg = sv("svg", { class: "chart", width: w, height: H, role: "img", "aria-label": "Requests by weekday and hour" });
      for (let hr = 0; hr < 24; hr += 3) svg.appendChild(sv("text", { x: labelW + hr * cw + cw / 2, y: 11, "text-anchor": "middle" }, String(hr).padStart(2, "0")));
      hm.requests.forEach((row, d) => {
        svg.appendChild(sv("text", { x: 0, y: top + d * (rowH + gap) + 14 }, hm.weekdays[d]));
        row.forEach((v, hr) => {
          const cell = sv("rect", { x: labelW + hr * cw + 1, y: top + d * (rowH + gap), width: Math.max(1, cw - 2), height: rowH, rx: 3,
            fill: v ? "var(--s1)" : "var(--surface-2)", "fill-opacity": v ? (0.15 + 0.85 * (v / max)).toFixed(3) : 1 });
          Tip.bind(cell, () => ({ title: `${hm.weekdays[d]} ${String(hr).padStart(2, "0")}:00–${String(hr + 1).padStart(2, "0")}:00`, rows: [[F.num(v), "requests"]] }));
          svg.appendChild(cell);
        });
      });
      return svg;
    },
    table: () => dataTable({ search: false, limit: 200, rows: hm.requests.flatMap((row, d) => row.map((v, hr) => ({ day: hm.weekdays[d], hour: hr, requests: v }))).filter((r) => r.requests),
      columns: [{ key: "day", label: "Weekday" }, { key: "hour", label: "Hour", num: true }, { key: "requests", label: "Requests", num: true }] }) });
}

function overview() {
  const models = Object.entries(D.by_model).map(([m, u]) => ({ label: m, values: { input: u.cost_components.input, cache_write: u.cost_components.cache_write_5m + u.cost_components.cache_write_1h, cache_read: u.cost_components.cache_read, output: u.cost_components.output } }));
  const segs = [{ key: "input", label: "Input", color: "var(--s1)" }, { key: "cache_write", label: "Cache write", color: "var(--s2)" },
    { key: "cache_read", label: "Cache read", color: "var(--s3)" }, { key: "output", label: "Output", color: "var(--s4)" }];
  const projects = Object.entries(D.by_project).map(([k, v]) => Object.assign({ project: k }, v));
  return h("div", { class: "grid" },
    dayColumns("cost_usd", F.usd, "Cost per day", "Estimated spend of requests made that day"),
    dayColumns("tool_calls", F.num, "Tool calls per day", "Main thread and agents"),
    heatmap(),
    chartCard({ title: "Cost by model", span: 6, empty: models.length ? null : "No requests.",
      chart: () => stackedBars(models, segs, { fmt: F.usd }),
      table: () => dataTable({ search: false, rows: Object.entries(D.by_model).map(([m, u]) => Object.assign({ model: m }, u)), columns: [
        { key: "model", label: "Model" }, { key: "requests", label: "Requests", num: true }, { key: "output", label: "Output", num: true, fmt: F.tok },
        { key: "cache_read", label: "Cache read", num: true, fmt: F.tok }, { key: "cache_hit_ratio", label: "Hit", num: true, fmt: F.pct }, { key: "cost", label: "Cost", num: true, fmt: F.usd }] }) }),
    chartCard({ title: "Cost by project", span: 6, empty: projects.length ? null : "No projects.",
      chart: () => barList(projects, { label: (p) => p.project, value: (p) => p.cost, fmt: F.usd,
        tip: (p) => ({ title: p.project, rows: [[F.usd(p.cost), "cost"], [F.num(p.sessions), "sessions"], [F.num(p.turns), "turns"], [F.num(p.tool_calls), "tool calls"]] }) }),
      table: () => dataTable({ search: false, rows: projects, columns: [{ key: "project", label: "Project" }, { key: "sessions", label: "Sessions", num: true },
        { key: "turns", label: "Turns", num: true }, { key: "requests", label: "Requests", num: true }, { key: "tool_calls", label: "Tool calls", num: true }, { key: "cost", label: "Cost", num: true, fmt: F.usd }] }) }));
}

function sessions() {
  return h("div", { class: "grid" }, card({ title: "Sessions", sub: "Window columns count only activity inside the window, each request once", span: 12 },
    dataTable({ rows: D.sessions, limit: 100, sortKey: "start_ms", selects: [{ key: "project", label: "projects" }], columns: [
      { key: "start_ms", label: "Started", fmt: F.datetime }, { key: "project", label: "Project" }, { key: "title", label: "Title", cls: "wrap" },
      { key: "window_turns", label: "Turns", num: true }, { key: "window_requests", label: "Requests", num: true }, { key: "window_tool_calls", label: "Tools", num: true },
      { key: "skills", label: "Skills", cls: "wrap", fmt: (v) => v.join(", ") || "—" }, { key: "subagents", label: "Agents", num: true },
      { key: "window_active_ms", label: "Active", num: true, fmt: F.dur }, { key: "window_cost_usd", label: "Cost", num: true, fmt: F.usd },
      { key: "lines_added", label: "+Lines", num: true }, { key: "commits", label: "Commits", num: true },
      { key: "continues", label: "Continues", fmt: (v) => v.map((x) => x.slice(0, 8)).join(", ") || "—" },
      { key: "id", label: "Id", cls: "code", fmt: (v) => v.slice(0, 8) }] })));
}

function skillsTab() {
  const rows = D.skills;
  return h("div", { class: "grid" },
    chartCard({ title: "Attributed cost by skill", span: 6, empty: rows.length ? null : "No skills.",
      chart: () => barList(rows.filter((s) => s.attributed_cost_usd), { label: (s) => s.skill, value: (s) => s.attributed_cost_usd, fmt: F.usd,
        tip: (s) => ({ title: s.skill, rows: [[F.usd(s.attributed_cost_usd), "cost"], [F.num(s.invocations), "invocations"], [F.num(s.sessions), "sessions"], [F.num(s.attributed_tool_calls), "tool calls"]] }) }),
      table: () => dataTable({ search: false, rows, columns: [{ key: "skill", label: "Skill" }, { key: "attributed_cost_usd", label: "Cost", num: true, fmt: F.usd }] }) }),
    chartCard({ title: "Invocations by skill", span: 6, empty: rows.some((s) => s.invocations) ? null : "No invocations.",
      chart: () => barList(rows.filter((s) => s.invocations).sort((a, b) => b.invocations - a.invocations), { label: (s) => s.skill, value: (s) => s.invocations,
        extra: (s) => Object.entries(s.by_mode).map(([k, v]) => `${k} ${v}`).join(" · ") }),
      table: () => dataTable({ search: false, rows, columns: [{ key: "skill", label: "Skill" }, { key: "invocations", label: "Invocations", num: true }] }) }),
    card({ title: "Skills", span: 12 }, dataTable({ rows, search: false, sortKey: "attributed_cost_usd", columns: [
      { key: "skill", label: "Skill" }, { key: "invocations", label: "Invocations", num: true },
      { key: "by_mode", label: "Invoked by", fmt: (v) => Object.entries(v).map(([k, n]) => `${k} ×${n}`).join(", ") || "—" },
      { key: "sessions", label: "Sessions", num: true }, { key: "attributed_requests", label: "Requests", num: true },
      { key: "attributed_tool_calls", label: "Tool calls", num: true }, { key: "attributed_cost_usd", label: "Cost", num: true, fmt: F.usd }] })),
    card({ title: "Slash commands you typed", span: 12 }, pills(Object.fromEntries(Object.entries(D.slash_commands).map(([k, v]) => ["/" + k, v])))));
}

function toolsTab() {
  const rows = D.tools;
  const agents = Object.entries(D.agent_types).map(([k, v]) => Object.assign({ type: k }, v));
  return h("div", { class: "grid" },
    chartCard({ title: "Calls by tool", span: 7, empty: rows.length ? null : "No tool calls.",
      chart: () => barList(rows, { label: (t) => t.name, value: (t) => t.calls,
        extra: (t) => t.errors ? h("span", { class: "err-flag" }, `${t.errors} err`) : `p50 ${F.dur(t.p50_ms)}` }),
      table: () => dataTable({ search: false, rows, columns: [{ key: "name", label: "Tool" }, { key: "calls", label: "Calls", num: true },
        { key: "errors", label: "Errors", num: true }, { key: "error_rate", label: "Error rate", num: true, fmt: F.pct }, { key: "denied", label: "Denied", num: true },
        { key: "sessions", label: "Sessions", num: true }, { key: "p50_ms", label: "p50", num: true, fmt: F.dur }, { key: "p90_ms", label: "p90", num: true, fmt: F.dur }] }) }),
    card({ title: "Programs run in the shell", span: 5 }, barList(Object.entries(D.programs).map(([k, v]) => ({ k, v })), { label: (x) => x.k, value: (x) => x.v, limit: 20 })),
    card({ title: "Agent types", span: 6 }, agents.length ? dataTable({ search: false, rows: agents, columns: [{ key: "type", label: "Type" },
      { key: "agents", label: "Agents", num: true }, { key: "requests", label: "Requests", num: true }, { key: "cost", label: "Cost", num: true, fmt: F.usd }] }) : h("div", { class: "empty" }, "No subagents.")),
    card({ title: "Tool error categories", span: 6 }, pills(D.error_categories)),
    card({ title: "MCP servers", span: 6 }, pills(D.mcp_servers)),
    card({ title: "Entry points", span: 6 }, pills(D.entrypoints)));
}

(function mount() {
  const app = document.getElementById("app");
  const t = tabs([
    { id: "overview", label: "Overview", build: overview },
    { id: "sessions", label: "Sessions", count: D.sessions.length, build: sessions },
    { id: "skills", label: "Skills", count: D.skills.length, build: skillsTab },
    { id: "tools", label: "Tools", count: D.tools.length, build: toolsTab },
  ]);
  app.append(header(), kpis(), D.insights.length ? h("ul", { class: "insights" }, D.insights.map((i) => h("li", null, i))) : "",
    t.nav, ...t.sections, h("footer", { class: "foot" }, D.notes.join(" ")));
  t.start();
})();
