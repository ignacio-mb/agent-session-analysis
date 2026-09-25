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
    ["Support files", t.support_files === null || t.support_files === undefined ? "—" : F.num(t.support_files), "created, not named by the skill"],
  ];
  return h("div", { class: "kpis" }, tiles.map(([l, v, s]) =>
    h("div", { class: "kpi" }, h("div", { class: "label" }, l), h("div", { class: "value" }, v), h("div", { class: "sub" }, s))));
}

/* ------------------------------------------------------------------ versions */

const METRICS = [
  ["cost_usd", "Cost per run", F.usd], ["tool_calls", "Tool calls per run", F.num], ["tool_errors", "Tool errors per run", F.num],
  ["question_calls", "Questions asked per run", F.num], ["help_lookups", "--help lookups per run", F.num], ["active_ms", "Active time per run", F.dur],
  ["docs_read", "Skill files read per run", F.num], ["doc_tokens", "≈ Tokens of skill docs per run", F.tok], ["cli_docs_read", "CLI docs read per run", F.num],
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
        { key: "objects_created", label: "Created", num: true }, { key: "support_files", label: "Support files", num: true },
        { key: "inline_scripts", label: "Inline", num: true }, { key: "turn_count", label: "Turns", num: true },
        { key: "duration_ms", label: "Duration", num: true, fmt: F.dur }, { key: "checks_failed", label: "Checks ✗", num: true },
        { key: "status", label: "Match" }] })),
    ...charts, ...changes);
}

/* ------------------------------------------------------------------ interview */

const IV_METRICS = [
  ["questions_asked", "Questions asked per run", F.num], ["prose_questions", "Asked in prose per run", F.num],
  ["recommended_rate", "Recommended option taken, per run", F.pct], ["typed_answers", "Typed answers per run", F.num],
  ["question_wait_p50_ms", "Median wait for an answer, per run", F.dur], ["questions_flagged", "Questions flagged per run", F.num],
];

function topicCell(st, v) {
  if (!st || !st.runs_asking) return h("span", { class: "vcell absent", title: `no run of ${vShort(v)} asked this` }, `0/${v.runs}`);
  const bar = h("span", { class: "bar", "aria-hidden": "true" }, h("i", { style: `width:${(100 * st.runs_asking) / (v.runs || 1)}%` }));
  const tip = [`${st.runs_asking} of ${v.runs} runs asked it (${st.asked} through AskUserQuestion, ${st.prose} in prose)`];
  if (st.offered) tip.push(`recommended option taken ${st.recommended}/${st.offered}`);
  if (st.typed) tip.push(`${st.typed} typed answers`);
  tip.push("outcomes: " + Object.entries(st.outcomes || {}).map(([k, n]) => `${k} ×${n}`).join(", "));
  return h("span", { class: "vcell", title: tip.join("\n") }, `${st.runs_asking}/${v.runs}`, bar);
}

function interviewTab() {
  const IV = D.interview || {};
  const s = IV.summary || {};
  if (!s.total) return h("div", { class: "empty" }, "No run of this skill asked a question in this window.");
  const order = ((IV.taxonomy || {}).topics || []).map((t) => t.id).concat(["other"]);
  const empty = s.no_preference + s.declined + s.unanswered;
  const tiles = [
    ["Questions", F.num(s.total), `${s.asked} through AskUserQuestion · ${s.prose} in prose`],
    ["Runs that asked", `${RUNS.filter((r) => (r.questions_asked || 0) + (r.prose_questions || 0) > 0).length}/${RUNS.length}`,
      (() => { const xs = RUNS.map((r) => (r.questions_asked || 0) + (r.prose_questions || 0)).filter((n) => n > 0).sort((a, b) => a - b);
        if (!xs.length) return "none"; const m = Math.floor(xs.length / 2); return `median ${F.num(xs.length % 2 ? xs[m] : (xs[m - 1] + xs[m]) / 2)} questions in those`; })()],
    ["Recommended option taken", s.recommended_offered ? `${s.recommended_picked}/${s.recommended_offered}` : "—", s.recommended_offered ? F.pct(s.recommended_rate) : "none offered"],
    ["Typed answers", F.num(s.typed), "none of the options fit"],
    ["Came back empty", F.num(empty), `${s.no_preference} no preference · ${s.declined} declined · ${s.unanswered} unanswered`],
    ["Median wait", F.dur(s.wait_p50_ms), s.wait_max_ms ? `longest ${F.dur(s.wait_max_ms)}` : ""],
  ];
  const kpiRow = h("div", { class: "kpis" }, tiles.map(([l, v, sub]) => h("div", { class: "kpi" }, h("div", { class: "label" }, l), h("div", { class: "value" }, v), h("div", { class: "sub" }, sub))));
  const groups = VERS.map((v) => ({ label: vShort(v), version: v, items: RUNS.filter((r) => r.version_key === v.key) }));
  const strips = IV_METRICS.map(([key, title, fmt]) => chartCard({
    title, sub: "One dot per run · line = median", span: 4,
    chart: (w) => stripPlot(w, groups, { value: (r) => r[key], fmt,
      tip: (r, v) => ({ title: `${r.run_id} · ${r.commit || ""}`, rows: [[fmt(v), title.toLowerCase()]], body: r.args || r.prompt }) }),
    table: () => dataTable({ search: false, rows: RUNS, columns: [{ key: "run_id", label: "Run" }, { key: "commit", label: "Version" }, { key, label: title, num: true, fmt }] }) }));
  const flagOf = (c) => h("span", null, c.label, c.once ? h("span", { class: "chip", title: "the skill says: ask once" }, "once") : null,
    c.must_ask ? h("span", { class: "chip", title: "the skill requires this question" }, "must ask") : null);
  const matrix = dataTable({ rows: IV.catalog, limit: 60, search: false, columns: [{ key: "label", label: "Topic", cls: "wrap", render: flagOf }]
    .concat(VERS.map((v) => ({ key: "v_" + v.key, label: vShort(v), num: true, sort: (c) => ((c.per_version[v.key] || {}).runs_asking || 0) / (v.runs || 1),
      render: (c) => topicCell(c.per_version[v.key], v) })))
    .concat([{ key: "runs", label: "All runs", num: true }]) });
  const catalog = dataTable({ rows: IV.catalog, limit: 60, columns: [
    { key: "label", label: "Topic", cls: "wrap", render: flagOf },
    { key: "asked", label: "Asked", num: true }, { key: "prose", label: "In prose", num: true }, { key: "runs", label: "Runs", num: true },
    { key: "recommended_rate", label: "Recommended taken", num: true, fmt: (v, c) => (c.offered ? `${c.recommended}/${c.offered}` : "—") },
    { key: "typed", label: "Typed", num: true }, { key: "declined", label: "Empty", num: true, fmt: (v, c) => F.num(c.no_preference + c.declined + c.unanswered) },
    { key: "reasked", label: "Asked again", num: true }, { key: "wait_p50_ms", label: "Median wait", num: true, fmt: F.dur },
    { key: "answers", label: "Answers given", cls: "wrap", fmt: (o) => topList(o, 3, " · ") },
    { key: "headers", label: "Headers used", cls: "wrap", fmt: (o) => Object.keys(o || {}).join(", ") || "—" },
    { key: "examples", label: "Example question", cls: "wrap", fmt: (a) => (a && a[0]) || "—" }] });
  const typed = (IV.typed || []).length ? dataTable({ search: false, limit: 60, rows: IV.typed, columns: [
    { key: "run_id", label: "Run" }, { key: "topic_label", label: "Topic", cls: "wrap" }, { key: "question", label: "Question", cls: "wrap" },
    { key: "options", label: "Options offered", cls: "wrap", fmt: (a) => (a || []).join(" · ") }, { key: "typed", label: "What was typed", cls: "wrap" }] })
    : h("div", { class: "empty" }, "Every answer was one of the options offered.");
  const withQs = RUNS.filter((r) => IV.questions.some((q) => q.run_id === r.run_id));
  const pick = h("select", { "aria-label": "Run" }, withQs.slice().reverse().map((r) => h("option", { value: r.run_id }, `${r.run_id} · ${r.commit || "?"} · ${F.short(r.args || r.prompt, 40)}`)));
  const mapHost = h("div");
  const drawMap = () => {
    const rows = IV.questions.filter((q) => q.run_id === pick.value);
    const run = RUNS.find((r) => r.run_id === pick.value) || {};
    const d = (D.details[pick.value] || {}).interview || {};
    const markers = d.first_create_dt !== null && d.first_create_dt !== undefined ? [{ t: run.start_ms + d.first_create_dt, label: "first create" }] : [];
    mapHost.replaceChildren(chartCard({ title: `The interview of ${pick.value}, round by round`, span: 12, legendEl: interviewLegend(),
      sub: "One column per round, one lane per topic; the dashed line is the run's first create", chart: (w) => interviewMap(w, rows, { order, markers }),
      table: () => questionTable(rows) }));
    requestAnimationFrame(() => Charts.renderVisible());
  };
  pick.addEventListener("change", drawMap);
  if (withQs.length) setTimeout(drawMap, 0);
  return h("div", null, kpiRow, h("div", { class: "grid" },
    ...strips,
    card({ title: "Which topics each version asks about", sub: "Runs that asked the topic / runs of that version; hover a cell for what came back", span: 12 }, matrix),
    card({ title: "Question catalog", sub: `Every question grouped by the skill's ${(IV.taxonomy || {}).source === "skill" ? "own topics (checks file)" : "generic topics"}: how often, what people answered, how long it took`, span: 12 }, catalog),
    card({ title: "Where the options fell short", sub: "Answers typed instead of picked: what the options missed", span: 12 }, typed),
    card({ title: "One run's interview", span: 12, tools: pick }, mapHost),
    card({ title: "Every question", sub: "Across runs and versions; filter by topic, outcome or channel", span: 12 }, questionTable(IV.questions, { showRun: true, showVersion: true, limit: 200 }))));
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
      { key: "objects_created", label: "Created", num: true }, { key: "support_files", label: "Support files", num: true },
      { key: "docs_read", label: "Files read", num: true, fmt: (n, r) => `${F.num(n)}/${F.num(r.docs_total)}` },
      { key: "cli_docs_read", label: "CLI docs", num: true }, { key: "doc_tokens", label: "≈ Doc tokens", num: true, fmt: F.tok },
      { key: "cost_usd", label: "Cost", num: true, fmt: F.usd },
      { key: "duration_ms", label: "Duration", num: true, fmt: F.dur }, { key: "checks_failed", label: "Checks ✗", num: true }] });
  if (RUNS.length) setTimeout(() => open(RUNS[RUNS.length - 1].run_id), 0);
  return h("div", null, card({ title: "Runs", sub: "Pick a run to see its checks, the skill files it read, CLI calls, questions, objects, errors and the full trace", span: 12 }, table), panel);
}

/* ------------------------------------------------------------------ skill files & CLI */

function vcell(st, v) {
  if (!st || st.in_version === false) return h("span", { class: "vcell absent", title: "not in this version" }, "—");
  const n = st.runs || v.runs, k = st.shown || 0;
  const bar = h("span", { class: "bar", "aria-hidden": "true" }, h("i", { style: `width:${n ? (100 * k) / n : 0}%` }));
  const tip = [`${k} of ${n} runs of ${vShort(v)} were shown this file`];
  if (st.touched > k) tip.push(`${st.touched - k} more touched it without being shown a line`);
  if (st.full) tip.push(`${st.full} whole, ${st.partial || 0} in part`);
  if (st.changed) tip.push(`changed in this version: +${st.changed.added} −${st.changed.removed}`);
  return h("span", { class: "vcell", title: tip.join("\n") }, `${k}/${n}`, bar, st.changed ? h("span", { class: "delta", title: "changed in this version" }, "Δ") : null);
}

function topList(o, n, sep = ", ") {
  const e = Object.entries(o || {});
  if (!e.length) return "—";
  return e.slice(0, n).map(([k, c]) => `${k} ×${c}`).join(sep) + (e.length > n ? `${sep}+${e.length - n} more` : "");
}

function filesTab() {
  const ALL = D.files || [];
  if (!ALL.length || !VERS.length) return h("div", { class: "empty" }, "No skill-file data.");
  // A path no version contains and no run was shown a line of is a wrong guess or an empty search, not a file.
  const tried = ALL.filter((f) => !f.runs_shown && !Object.values(f.per_version).some((x) => x.in_version));
  const FILES = ALL.filter((f) => !tried.includes(f));
  const latest = VERS[VERS.length - 1];
  const own = FILES.filter((f) => f.own);
  const med = (k) => { const xs = RUNS.map((r) => r[k]).filter((x) => x !== null && x !== undefined).sort((a, b) => a - b);
    if (!xs.length) return null; const m = Math.floor(xs.length / 2); return xs.length % 2 ? xs[m] : (xs[m - 1] + xs[m]) / 2; };
  const tiles = [
    [`Files in ${D.skill}`, F.num((latest.inventory || []).length), `at ${vShort(latest)}`],
    ["Shown to a run", F.num(own.filter((f) => f.runs_shown > 0).length), "in any version"],
    ["Never shown", F.num((latest.never || []).length), `no run of ${vShort(latest)} (${latest.runs})`],
    ["Median files read / run", F.num(med("docs_read")), "the skill's own, beyond SKILL.md"],
    ["Median ≈ tokens of docs / run", F.tok(med("doc_tokens")), "doc text Claude was shown"],
    ["Other docs shown", F.num(FILES.length - own.length), Array.from(new Set(FILES.filter((f) => !f.own).map((f) => f.owner))).join(", ") || "none"],
  ];
  const kpiRow = h("div", { class: "kpis" }, tiles.map(([l, v, sub]) => h("div", { class: "kpi" }, h("div", { class: "label" }, l), h("div", { class: "value" }, v), h("div", { class: "sub" }, sub))));

  const matrix = dataTable({ rows: FILES, limit: 300, sortKey: null, selects: [{ key: "kind", label: "kinds" }, { key: "owner", label: "owners" }],
    columns: [{ key: "path", label: "File", cls: "path", fmt: (v, r) => (r.own ? v : `${r.owner}:${v}`) }, { key: "kind", label: "Kind" }]
      .concat(VERS.map((v) => ({ key: "v_" + v.key, label: vShort(v), num: true, sort: (r) => ((r.per_version[v.key] || {}).shown || 0) / (v.runs || 1),
        render: (r) => vcell(r.per_version[v.key], v) })))
      .concat([{ key: "runs_shown", label: "All", num: true, fmt: (n, r) => `${n}/${r.runs}` }]) });

  const pick = h("select", { "aria-label": "Version" }, VERS.slice().reverse().map((v) => h("option", { value: v.key }, `${vShort(v)} · ${v.runs} runs`)));
  const statsHost = h("div");
  const drawStats = () => {
    const v = VERS.find((x) => x.key === pick.value) || latest;
    const rows = FILES.map((f) => Object.assign({ file: f.own ? f.path : `${f.owner}:${f.path}`, kind: f.kind, owner: f.owner }, f.per_version[v.key] || {}))
      .filter((r) => r.touched);
    statsHost.replaceChildren(dataTable({ rows, limit: 200, sortKey: "order", sortDir: "asc", selects: [{ key: "owner", label: "owners" }], columns: [
      { key: "file", label: "File", cls: "path" },
      { key: "shown", label: "Shown", num: true, fmt: (n, r) => `${n}/${r.runs}` },
      { key: "full", label: "Whole · part", num: true, fmt: (n, r) => `${n} · ${r.partial}` },
      { key: "coverage", label: "Median share", num: true, fmt: F.pct },
      { key: "order", label: "Median order", num: true, fmt: (x) => (x === null || x === undefined ? "—" : x === 0 ? "injected" : String(Math.round(x * 10) / 10)) },
      { key: "first_dt", label: "Median first read", num: true, fmt: F.since },
      { key: "tokens", label: "Median ≈ tokens", num: true, fmt: F.tok },
      { key: "rereads", label: "Re-reads", num: true },
      { key: "found_by", label: "Reached by", cls: "wrap", render: (r) => {
        const named = Object.keys(r.named_by || {}).length;
        const txt = named ? "named by " + topList(r.named_by, 2) : topList(r.found_by, 2);
        return h("span", { title: `reached by: ${topList(r.found_by, 9)}\nnamed by: ${topList(r.named_by, 9)}` }, txt);
      } },
      { key: "sections", label: "Sections seen", cls: "wrap", render: (r) => h("span", { title: topList(r.sections, 20, "\n") }, topList(r.sections, 3, " · ")) },
      { key: "mismatches", label: "Not the version", fmt: (a) => (a && a.length ? a.join(", ") : "—") }] }));
  };
  pick.addEventListener("change", drawStats);
  drawStats();

  const exposure = VERS.filter((v) => v.changes && (v.changes.exposure || []).length).map((v) => card({
    title: `Did the runs of ${vShort(v)} see what ${v.changes.from} → ${vShort(v)} changed?`,
    sub: "Lines each changed file gained, and how many runs were shown them. SKILL.md's body is always injected; its frontmatter never is.", span: 12 },
    dataTable({ search: false, limit: 80, rows: v.changes.exposure, columns: [
      { key: "path", label: "File", cls: "path" }, { key: "added", label: "+", num: true }, { key: "removed", label: "−", num: true },
      { key: "ranges", label: "Changed lines", fmt: (x) => F.ranges(x, 5) },
      { key: "seen", label: "Saw all", num: true, fmt: (n) => `${n}/${v.runs}` }, { key: "partly", label: "Saw some", num: true },
      { key: "not_seen", label: "Saw none", num: true },
      { key: "runs", label: "Per run", render: (r) => h("span", null, Object.entries(r.runs || {}).map(([id, x]) =>
        h("span", { class: "chip", title: `${x.status}: ${x.seen}/${x.changed} changed lines shown` }, `${id} ${x.changed ? `${x.seen}/${x.changed}` : x.status}`))) }] })));

  const never = card({ title: "Never shown, by version", sub: "Files of the skill that no run of that version was shown a line of", span: 6 },
    h("div", { class: "checks" }, VERS.map((v) => h("div", null, h("b", null, `${vShort(v)} (${v.runs} runs): `),
      (v.never || []).length ? h("span", { class: "mono" }, v.never.join(", ")) : h("span", { class: "muted" }, "every file was shown to some run")))));
  const triedCard = card({ title: "Paths tried that showed nothing", sub: "Paths no version of the skill has, globs that matched nothing, searches with no hits", span: 6 },
    tried.length ? dataTable({ search: false, limit: 50, rows: tried.map((f) => Object.assign({ label: f.own ? f.path : `${f.owner}:${f.path}`,
      touched: Object.values(f.per_version).reduce((a, x) => a + (x.touched || 0), 0),
      how: Array.from(new Set(Object.values(f.per_version).flatMap((x) => Object.keys(x.found_by || {})))).join(", ") }, f)), columns: [
      { key: "label", label: "Path", cls: "code" }, { key: "touched", label: "Runs", num: true }, { key: "how", label: "Reached by" }] })
      : h("div", { class: "empty" }, "None."));

  return h("div", null, kpiRow, h("div", { class: "grid" },
    card({ title: "Which files each version's runs were shown", sub: "Runs shown at least one line of the file / runs of that version · Δ the version changed the file · — not in that version", span: 12 }, matrix),
    card({ title: "How each file was read", sub: "Per version: whole or in part, the share of the file shown, when and in what order, and what pointed Claude to it", span: 12, tools: pick }, statsHost),
    ...exposure, never, triedCard));
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
        ["retries_after_error", F.num], ["question_calls", F.num], ["objects_created", F.num], ["support_files", F.num],
        ["inline_scripts", F.num], ["turn_count", F.num], ["duration_ms", F.dur],
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
    { id: "interview", label: "Interview", count: ((D.interview || {}).summary || {}).total || 0, build: interviewTab },
    { id: "runs", label: "Runs", count: RUNS.length, build: runsTab },
    { id: "files", label: "Skill files", build: filesTab },
    { id: "cli", label: "CLI & failures", count: D.failures.length, build: cliTab },
    { id: "compare", label: "Compare", build: compareTab },
  ]);
  app.append(header(), kpis(), D.insights.length ? h("ul", { class: "insights" }, D.insights.map((i) => h("li", null, i))) : "",
    t.nav, ...t.sections, h("footer", { class: "foot" }, `convo-analysis ${D.generator.version} · generated ${F.datetime(D.generator.generated_at)}`));
  t.start();
})();
