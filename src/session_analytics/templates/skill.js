"use strict";
/* Skill report: every run of one skill across sessions, grouped by the version that ran. */

const D = JSON.parse(document.getElementById("session-data").textContent);
Tip.init();
const RUNS = D.runs;
const VERS = D.versions;

function fullRun(id) {
  const row = RUNS.find((r) => r.run_id === id);
  const d = D.details[id] || {};
  return Object.assign({}, row, d, { version: d.version || {}, checks: d.checks || [] });
}

function vShort(v) {
  return v.commit || (v.label || "?").slice(0, 16);
}

function header() {
  const sc = D.scope, t = D.totals;
  const first = RUNS.length ? F.datetime(RUNS[0].start_ms) : "—", last = RUNS.length ? F.datetime(RUNS[RUNS.length - 1].start_ms) : "—";
  return h("header", { class: "top" },
    h("div", { style: "min-width:0;flex:1" },
      h("h1", null, `${D.skill} — skill report`),
      h("div", { class: "meta" },
        h("span", null, `${t.runs} runs in ${t.sessions} sessions`),
        h("span", null, `${VERS.length} versions`),
        h("span", null, `${sc.label}, since ${sc.since}`),
        h("span", null, `${first} → ${last}`),
        h("span", null, `${sc.transcripts_using_skill} of ${sc.transcripts} transcripts in the window used it`))),
    themeToggle());
}

function kpis() {
  const t = D.totals;
  const tiles = [
    ["Runs", F.num(t.runs), `${t.sessions} sessions`],
    ["Versions", F.num(VERS.length), VERS.length ? `latest ${vShort(VERS[VERS.length - 1])}` : ""],
    ["Median cost / run", F.usd(t.median_cost_usd), `${F.usd(t.cost_usd)} total`],
    ["Median tool calls / run", F.num(t.median_tool_calls), `${F.num(t.tool_calls)} total`],
    ["Tool errors", F.num(t.tool_errors), `${F.pct(t.tool_calls ? t.tool_errors / t.tool_calls : null)} of calls`],
    ["CLI calls", F.num(t.cli_calls), "subcommands like `mb card create`"],
    ["Questions asked", F.num(t.question_calls), "AskUserQuestion calls"],
    ["Objects created", F.num(t.objects_created), "reported by CLI output"],
  ];
  return h("div", { class: "kpis" }, tiles.map(([l, v, s]) =>
    h("div", { class: "kpi" }, h("div", { class: "label" }, l), h("div", { class: "value" }, v), h("div", { class: "sub" }, s))));
}

/* ------------------------------------------------------------------ versions */

const METRICS = [
  ["cost_usd", "Cost per run", F.usd], ["tool_calls", "Tool calls per run", F.num], ["tool_errors", "Tool errors per run", F.num],
  ["question_calls", "Questions asked per run", F.num], ["help_lookups", "--help lookups per run", F.num], ["active_ms", "Active time per run", F.dur],
];

function versionsTab() {
  const groups = VERS.map((v) => ({ label: vShort(v), version: v, items: RUNS.filter((r) => r.version_key === v.key) }));
  const charts = METRICS.map(([key, title, fmt]) => chartCard({
    title, sub: "One dot per run · line = median", span: 4, empty: RUNS.length ? null : "No runs.",
    chart: (w) => stripPlot(w, groups, { value: (r) => r[key], fmt,
      tip: (r, v) => ({ title: `${r.run_id} · ${vShort(groups.find((g) => g.items.includes(r)).version)}`, rows: [[fmt(v), title.toLowerCase()]], body: r.args || r.prompt }) }),
    table: () => dataTable({ search: false, rows: RUNS, columns: [{ key: "run_id", label: "Run" }, { key: "commit", label: "Version" }, { key: key, label: title, num: true, fmt }] }),
  }));
  const changes = VERS.filter((v) => v.changes && (v.changes.commits.length || v.changes.files.length)).map((v) =>
    card({ title: `${v.changes.from} → ${v.commit}`, sub: v.subject, span: 6 },
      h("ul", { class: "insights" }, v.changes.commits.map((c) => h("li", null, `${c.short} ${c.subject}`))),
      dataTable({ search: false, limit: 40, rows: v.changes.files, columns: [{ key: "path", label: "File", cls: "code" },
        { key: "added", label: "+", num: true }, { key: "removed", label: "−", num: true }] })));
  return h("div", { class: "grid" },
    card({ title: "Versions", sub: "Medians per run; the version is the git commit whose SKILL.md matches what Claude Code injected", span: 12 },
      dataTable({ search: false, rows: VERS.map((v) => Object.assign({}, v, v.median)), columns: [
        { key: "label", label: "Version", cls: "wrap" }, { key: "date", label: "Committed", fmt: F.datetime }, { key: "runs", label: "Runs", num: true },
        { key: "cost_usd", label: "Cost", num: true, fmt: F.usd }, { key: "tool_calls", label: "Tools", num: true }, { key: "tool_errors", label: "Errors", num: true },
        { key: "cli_calls", label: "CLI", num: true }, { key: "help_lookups", label: "Help", num: true }, { key: "question_calls", label: "Asked", num: true },
        { key: "objects_created", label: "Created", num: true }, { key: "turn_count", label: "Turns", num: true },
        { key: "duration_ms", label: "Duration", num: true, fmt: F.dur }, { key: "checks_failed", label: "Checks ✗", num: true },
        { key: "status", label: "Match" }] })),
    ...charts, ...changes);
}

/* ------------------------------------------------------------------ checks */

function checksTab() {
  if (!D.checks.length) return h("div", { class: "empty" }, "No checks are defined for this skill. Add checks/<skill>.json to the convo-analysis repo, or pass --checks.");
  const rateTable = h("table", { class: "data matrix" },
    h("thead", null, h("tr", null, h("th", null, "Check"), VERS.map((v) => h("th", { class: "num", title: v.label }, vShort(v))))),
    h("tbody", null, D.checks.map((c) => h("tr", null, h("td", null, h("b", null, c.id), h("div", { class: "muted" }, c.desc || "")),
      VERS.map((v) => {
        const r = v.checks[c.id] || { pass: 0, fail: 0 };
        const n = r.pass + r.fail;
        if (!n) return h("td", { class: "cell" }, statusMark("n/a", false));
        const tone = r.fail === 0 ? "pass" : r.pass === 0 ? "fail" : "interrupted";
        return h("td", { class: "cell" }, statusMark(tone, false), ` ${r.pass}/${n}`);
      })))));
  const runCols = VERS.flatMap((v) => RUNS.filter((r) => r.version_key === v.key).map((r) => ({ v, r })));
  const matrix = h("table", { class: "data matrix" },
    h("thead", null,
      h("tr", null, h("th", null, ""), VERS.map((v) => h("th", { colspan: String(RUNS.filter((r) => r.version_key === v.key).length), title: v.label }, vShort(v)))),
      h("tr", null, h("th", null, "Check"), runCols.map(({ r }) => h("th", { class: "num", title: r.args || r.prompt || "" }, r.run_id)))),
    h("tbody", null, D.checks.map((c) => h("tr", null, h("td", null, c.id),
      runCols.map(({ r }) => {
        const st = (r.checks || {})[c.id] || "n/a";
        const det = ((D.details[r.run_id] || {}).checks || []).find((x) => x.id === c.id);
        return h("td", { class: "cell", title: det && det.detail ? det.detail : st }, statusMark(st, false));
      })))));
  return h("div", { class: "grid" },
    card({ title: "Pass rate by version", sub: "passed / runs where the check applied", span: 12 }, h("div", { class: "tbl-wrap" }, rateTable)),
    card({ title: "Every run", sub: "Hover a cell for the evidence (call numbers, counts, an example)", span: 12 }, h("div", { class: "tbl-wrap" }, matrix)));
}

/* ------------------------------------------------------------------ runs */

function runsTab() {
  const panel = h("div");
  const open = (id) => {
    const run = fullRun(id);
    panel.replaceChildren(h("h2", { style: "font-size:15px;margin:18px 0 10px" }, `Run ${id}`), runDetail(run, run.steps || []));
    requestAnimationFrame(() => Charts.renderVisible());
  };
  const table = dataTable({ rows: RUNS, limit: 200, sortKey: "start_ms", sortDir: "desc", selects: [{ key: "commit", label: "versions" }, { key: "project", label: "projects" }],
    columns: [
      { key: "run_id", label: "Run", render: (r) => { const b = h("button", { class: "linkbtn", type: "button" }, r.run_id); b.addEventListener("click", () => open(r.run_id)); return b; } },
      { key: "start_ms", label: "Started", fmt: F.datetime }, { key: "commit", label: "Version", fmt: (v, r) => v || (r.version || "").slice(0, 14) },
      { key: "mode", label: "Invoked by", render: (r) => modeBadge(r.mode) }, { key: "project", label: "Project" },
      { key: "args", label: "Asked", cls: "wrap", fmt: (v, r) => v || r.prompt || "—" },
      { key: "turn_count", label: "Turns", num: true }, { key: "tool_calls", label: "Tools", num: true }, { key: "tool_errors", label: "Errors", num: true },
      { key: "cli_calls", label: "CLI", num: true }, { key: "help_lookups", label: "Help", num: true }, { key: "question_calls", label: "Asked ?", num: true },
      { key: "objects_created", label: "Created", num: true }, { key: "cost_usd", label: "Cost", num: true, fmt: F.usd },
      { key: "duration_ms", label: "Duration", num: true, fmt: F.dur }, { key: "checks_failed", label: "Checks ✗", num: true }] });
  if (RUNS.length) setTimeout(() => open(RUNS[RUNS.length - 1].run_id), 0);
  return h("div", null, card({ title: "Runs", sub: "Pick a run to see its checks, the skill files it read, CLI calls, questions, objects, errors and the full trace", span: 12 }, table), panel);
}

/* ------------------------------------------------------------------ skill files & CLI */

function filesTab() {
  const paths = Array.from(new Set(VERS.flatMap((v) => Object.keys(v.resources)))).sort();
  const rows = paths.map((p) => Object.assign({ path: p, kind: p.split("/")[0] }, Object.fromEntries(VERS.map((v) => [v.key, v.resources[p] || 0]))));
  return h("div", { class: "grid" }, card({ title: "Which skill files each version read", sub: "Runs that read the file / runs of that version", span: 12 },
    dataTable({ rows, limit: 200, selects: [{ key: "kind", label: "kinds" }], columns: [{ key: "path", label: "File", cls: "code" }, { key: "kind", label: "Kind" }]
      .concat(VERS.map((v) => ({ key: v.key, label: vShort(v), num: true, fmt: (n) => `${n}/${v.runs}` }))) })));
}

function cliTab() {
  const sigs = Array.from(new Set(VERS.flatMap((v) => Object.keys(v.cli))));
  const rows = sigs.map((sg) => {
    const row = { signature: sg, total: 0, errors: 0, help: 0 };
    for (const v of VERS) {
      const c = v.cli[sg];
      row[v.key] = c ? c.per_run : 0;
      row.total += c ? c.calls : 0; row.errors += c ? c.errors : 0; row.help += c ? c.help : 0;
    }
    return row;
  }).sort((a, b) => b.total - a.total);
  return h("div", { class: "grid" },
    card({ title: "CLI commands by version", sub: "Calls per run; totals across all runs", span: 12 },
      dataTable({ rows, limit: 60, sortKey: "total", columns: [{ key: "signature", label: "Command", cls: "code" }, { key: "total", label: "Calls", num: true },
        { key: "errors", label: "Errors", num: true }, { key: "help", label: "--help", num: true }]
        .concat(VERS.map((v) => ({ key: v.key, label: `${vShort(v)} /run`, num: true }))) })),
    card({ title: "Failures", sub: "Tool errors across runs, grouped by what the message says", span: 12 },
      D.failures.length ? dataTable({ rows: D.failures, limit: 60, sortKey: "count", columns: [
        { key: "count", label: "Times", num: true }, { key: "runs", label: "Runs", fmt: (v) => v.join(", ") },
        { key: "category", label: "Category" }, { key: "tool", label: "Tool" }, { key: "signature", label: "Failure", cls: "code" },
        { key: "example", label: "Example input", cls: "code", fmt: (v) => (v ? v.input : "—") }] }) : h("div", { class: "empty" }, "No tool errors.")));
}

/* ------------------------------------------------------------------ compare */

function lcsDiff(a, b) {
  const n = a.length, m = b.length;
  const dp = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push(["same", a[i]]); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) out.push(["del", a[i++]]);
    else out.push(["add", b[j++]]);
  }
  while (i < n) out.push(["del", a[i++]]);
  while (j < m) out.push(["add", b[j++]]);
  return out;
}

function compareTab() {
  if (RUNS.length < 2) return h("div", { class: "empty" }, "Compare needs at least two runs.");
  const opt = (r) => h("option", { value: r.run_id }, `${r.run_id} · ${r.commit || "?"} · ${F.datetime(r.start_ms)} · ${F.short(r.args || r.prompt, 50)}`);
  const selA = h("select", { "aria-label": "Run A" }, RUNS.map(opt));
  const selB = h("select", { "aria-label": "Run B" }, RUNS.map(opt));
  const latest = RUNS[RUNS.length - 1];
  const earlier = RUNS.slice().reverse().find((r) => r.version_key !== latest.version_key) || RUNS[RUNS.length - 2];
  selA.value = earlier.run_id; selB.value = latest.run_id;
  const out = h("div");
  const draw = () => {
    const a = fullRun(selA.value), b = fullRun(selB.value);
    const rows = [["Version", a.commit || "?", b.commit || "?"], ["Asked", F.short(a.args || a.prompt, 80), F.short(b.args || b.prompt, 80)],
      ...[["cost_usd", F.usd], ["requests", F.num], ["tool_calls", F.num], ["tool_errors", F.num], ["cli_calls", F.num], ["help_lookups", F.num],
        ["retries_after_error", F.num], ["question_calls", F.num], ["objects_created", F.num], ["turn_count", F.num], ["duration_ms", F.dur],
        ["context_peak", F.tok], ["checks_failed", F.num]].map(([k, f]) => [k.replace(/_/g, " "), f(a[k]), f(b[k])])];
    const checkRows = (a.checks || []).map((c) => [c.id, c.status, ((b.checks || []).find((x) => x.id === c.id) || {}).status || "—"]).filter((r) => r[1] !== r[2]);
    const onlyA = (a.resources_read || []).filter((p) => !(b.resources_read || []).includes(p));
    const onlyB = (b.resources_read || []).filter((p) => !(a.resources_read || []).includes(p));
    const seq = (r) => (r.actions || []).map((x) => x.action + (x.times > 1 ? `  ×${x.times}` : ""));
    const diff = lcsDiff(seq(a), seq(b));
    out.replaceChildren(h("div", { class: "grid" },
      card({ title: "Side by side", span: 6 }, h("div", { class: "tbl-wrap" }, h("table", { class: "data" },
        h("thead", null, h("tr", null, h("th", null, ""), h("th", null, a.run_id), h("th", null, b.run_id))),
        h("tbody", null, rows.map(([k, x, y]) => h("tr", null, h("td", null, k), h("td", null, x), h("td", null, y))))))),
      card({ title: "Checks that differ", span: 6 }, checkRows.length ? h("div", { class: "checks" }, checkRows.map(([id, x, y]) =>
        h("div", { class: "check-row" }, h("b", null, id + " "), statusMark(x), " → ", statusMark(y)))) : h("div", { class: "empty" }, "Same results."),
        h("div", { class: "note" }, `Only ${a.run_id} read: ${onlyA.join(", ") || "—"}`), h("div", { class: "note" }, `Only ${b.run_id} read: ${onlyB.join(", ") || "—"}`)),
      card({ title: "What each run did", sub: `− only ${a.run_id} · + only ${b.run_id} · aligned where both did the same thing`, span: 12 },
        h("div", { class: "diff" }, diff.map(([kind, line]) => h("div", { class: kind }, line))))));
  };
  selA.addEventListener("change", draw); selB.addEventListener("change", draw);
  draw();
  return h("div", null, h("div", { class: "tbl-tools" }, h("span", { class: "tbl-count" }, "A"), selA, h("span", { class: "tbl-count" }, "B"), selB), out);
}

(function mount() {
  const app = document.getElementById("app");
  const t = tabs([
    { id: "versions", label: "Versions", count: VERS.length, build: versionsTab },
    { id: "checks", label: "Checks", count: D.checks.length, build: checksTab },
    { id: "runs", label: "Runs", count: RUNS.length, build: runsTab },
    { id: "files", label: "Skill files", build: filesTab },
    { id: "cli", label: "CLI & failures", count: D.failures.length, build: cliTab },
    { id: "compare", label: "Compare", build: compareTab },
  ]);
  app.append(header(), kpis(), D.insights.length ? h("ul", { class: "insights" }, D.insights.map((i) => h("li", null, i))) : "",
    t.nav, ...t.sections, h("footer", { class: "foot" }, `convo-analysis ${D.generator.version} · generated ${F.datetime(D.generator.generated_at)}`));
  t.start();
})();
