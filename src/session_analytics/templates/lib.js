"use strict";
/* Tiny component library for the reports. No dependencies; every piece of
   session-derived text is inserted with textContent (it is untrusted data:
   prompts, commands, tool output). */

const SVGNS = "http://www.w3.org/2000/svg";

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  setAttrs(el, attrs);
  append(el, children);
  return el;
}

function sv(tag, attrs, ...children) {
  const el = document.createElementNS(SVGNS, tag);
  setAttrs(el, attrs);
  append(el, children);
  return el;
}

function setAttrs(el, attrs) {
  if (!attrs) return;
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.setAttribute("class", v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(typeof c === "object" ? c : document.createTextNode(String(c)));
  }
}

/* ------------------------------------------------------------------ formatting */

const F = {
  num(n) { return n === null || n === undefined ? "—" : Number(n).toLocaleString(); },
  tok(n) {
    if (n === null || n === undefined) return "—";
    const a = Math.abs(n);
    if (a >= 1e9) return (n / 1e9).toFixed(a >= 1e10 ? 1 : 2) + "B";
    if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e7 ? 1 : 2) + "M";
    if (a >= 1e3) return (n / 1e3).toFixed(a >= 1e4 ? 1 : 2) + "k";
    return String(Math.round(n));
  },
  usd(x) {
    if (x === null || x === undefined) return "—";
    if (x === 0) return "$0";
    if (Math.abs(x) < 0.01) return "$" + x.toFixed(4);
    return "$" + x.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  },
  pct(x) { return x === null || x === undefined ? "—" : (x * 100).toFixed(1) + "%"; },
  dur(ms) {
    if (ms === null || ms === undefined || isNaN(ms)) return "—";
    if (ms < 1000) return Math.round(ms) + " ms";
    let s = ms / 1000;
    if (s < 60) return s.toFixed(1) + " s";
    s = Math.round(s);
    let m = Math.floor(s / 60); s %= 60;
    if (m < 60) return `${m}m ${String(s).padStart(2, "0")}s`;
    let hh = Math.floor(m / 60); m %= 60;
    if (hh < 24) return `${hh}h ${String(m).padStart(2, "0")}m`;
    const d = Math.floor(hh / 24); hh %= 24;
    return `${d}d ${String(hh).padStart(2, "0")}h`;
  },
  ms(v) { return typeof v === "number" ? v : (v ? Date.parse(v) : null); },
  every(ms) {
    const units = [[86400e3, "day"], [3600e3, "hour"], [60e3, "minute"]];
    for (const [u, name] of units) if (ms >= u && ms % u === 0) { const n = ms / u; return n === 1 ? name : `${n} ${name}s`; }
    return F.dur(ms);
  },
  time(v) {
    const ms = F.ms(v);
    if (ms === null || isNaN(ms)) return "—";
    return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  },
  datetime(v) {
    const ms = F.ms(v);
    if (ms === null || isNaN(ms)) return "—";
    const d = new Date(ms);
    return d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " +
      d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  },
  short(s, n) {
    if (s === null || s === undefined) return "";
    s = String(s).replace(/\s+/g, " ").trim();
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  },
};

/* ------------------------------------------------------------------ tooltip */

const Tip = {
  el: null,
  init() {
    this.el = h("div", { class: "tip", role: "tooltip" });
    document.body.appendChild(this.el);
  },
  /* rows: [[value, label, color?], ...]; body: optional monospace text */
  show(evt, title, rows, body) {
    const el = this.el;
    el.replaceChildren();
    if (title) el.appendChild(h("div", { class: "tip-title" }, title));
    for (const r of rows || []) {
      const row = h("div", { class: "tip-row" });
      if (r[2]) row.appendChild(h("span", { class: "tip-key", style: `background:${r[2]}` }));
      row.appendChild(h("strong", null, r[0]));
      if (r[1]) row.appendChild(h("span", { class: "tip-label" }, r[1]));
      el.appendChild(row);
    }
    if (body) el.appendChild(h("div", { class: "tip-body" }, body));
    el.style.display = "block";
    let x, y;
    if (evt && evt.clientX !== undefined && evt.type !== "focus") { x = evt.clientX; y = evt.clientY; }
    else if (evt && evt.target && evt.target.getBoundingClientRect) {
      const r = evt.target.getBoundingClientRect(); x = r.left + r.width / 2; y = r.top;
    } else { x = 0; y = 0; }
    const tw = el.offsetWidth, th = el.offsetHeight;
    let left = x + 14, top = y + 14;
    if (left + tw > window.innerWidth - 8) left = Math.max(8, x - tw - 14);
    if (top + th > window.innerHeight - 8) top = Math.max(8, y - th - 14);
    el.style.left = left + "px";
    el.style.top = top + "px";
  },
  hide() { if (this.el) this.el.style.display = "none"; },
  bind(node, fn) {
    const show = (e) => { const t = fn(e); if (t) Tip.show(e, t.title, t.rows, t.body); };
    node.addEventListener("pointermove", show);
    node.addEventListener("focus", show);
    node.addEventListener("pointerleave", () => Tip.hide());
    node.addEventListener("blur", () => Tip.hide());
  },
};

/* ------------------------------------------------------------------ responsive chart hosts */

const Charts = {
  hosts: [],
  register(host, render) {
    host._render = render;
    host._w = 0;
    this.hosts.push(host);
  },
  renderVisible(force) {
    for (const host of this.hosts) {
      if (!host.isConnected || host.offsetParent === null) continue;
      const w = Math.floor(host.clientWidth);
      if (!w || (!force && w === host._w)) continue;
      host._w = w;
      host.replaceChildren();
      try { host.appendChild(host._render(w)); }
      catch (e) { host.appendChild(h("div", { class: "empty" }, "Chart failed to render: " + e.message)); }
    }
  },
};
window.addEventListener("resize", (() => {
  let t; return () => { clearTimeout(t); t = setTimeout(() => Charts.renderVisible(), 120); };
})());

/* ------------------------------------------------------------------ cards */

function card({ title, sub, span = 12, tools, note }, ...content) {
  const head = h("div", { class: "head" }, h("h2", null, title), tools ? h("div", { class: "tools" }, tools) : null);
  return h("div", { class: `card span-${span}` }, head, sub ? h("div", { class: "sub" }, sub) : null, content,
    note ? h("div", { class: "note" }, note) : null);
}

/* A chart with its accessible table twin behind a toggle. */
function chartCard({ title, sub, span = 12, chart, table, note, legendEl, empty }) {
  if (empty) return card({ title, sub, span }, h("div", { class: "empty" }, empty));
  const host = h("div", { class: "chart-host" });
  Charts.register(host, chart);
  const tableHost = h("div", { hidden: true });
  let built = false;
  const btn = h("button", { class: "linkbtn", type: "button", "aria-pressed": "false" }, "Table");
  btn.addEventListener("click", () => {
    const showTable = tableHost.hidden;
    if (showTable && !built) { tableHost.appendChild(table()); built = true; }
    tableHost.hidden = !showTable;
    host.hidden = showTable;
    if (legendEl) legendEl.hidden = showTable;
    btn.textContent = showTable ? "Chart" : "Table";
    btn.setAttribute("aria-pressed", String(showTable));
    if (!showTable) Charts.renderVisible(true);
  });
  return card({ title, sub, span, tools: table ? btn : null, note }, legendEl || null, host, tableHost);
}

function legend(items) {
  return h("div", { class: "legend" }, items.map((it) => {
    const sw = sv("svg", { width: 14, height: 10, "aria-hidden": "true" });
    if (it.shape === "line") sw.appendChild(sv("line", { x1: 0, x2: 14, y1: 5, y2: 5, stroke: it.color, "stroke-width": 2 }));
    else if (it.shape === "mark") sw.appendChild(markShape(it.mark, 7, 5, 4, it.color));
    else sw.appendChild(sv("rect", { x: 2, y: 1, width: 10, height: 8, rx: 2, fill: it.color }));
    return h("span", { class: "k" }, sw, it.label);
  }));
}

function markShape(kind, x, y, r, color) {
  const attrs = { fill: color };
  if (kind === "diamond") return sv("path", Object.assign({ d: `M${x},${y - r}L${x + r},${y}L${x},${y + r}L${x - r},${y}Z` }, attrs));
  if (kind === "triangle") return sv("path", Object.assign({ d: `M${x},${y - r}L${x + r},${y + r * 0.8}L${x - r},${y + r * 0.8}Z` }, attrs));
  if (kind === "down") return sv("path", Object.assign({ d: `M${x - r},${y - r * 0.8}L${x + r},${y - r * 0.8}L${x},${y + r}Z` }, attrs));
  if (kind === "square") return sv("rect", Object.assign({ x: x - r * 0.8, y: y - r * 0.8, width: r * 1.6, height: r * 1.6, rx: 1 }, attrs));
  if (kind === "cross") return sv("path", { d: `M${x - r},${y - r}L${x + r},${y + r}M${x + r},${y - r}L${x - r},${y + r}`, stroke: color, "stroke-width": 2 });
  return sv("circle", Object.assign({ cx: x, cy: y, r: r * 0.85 }, attrs));
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/* ------------------------------------------------------------------ scales & ticks */

function niceStep(raw) {
  if (raw <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const f = raw / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
}

function niceTicks(max, count = 4) {
  if (!max || max <= 0) return [0, 1];
  const step = niceStep(max / count);
  const top = Math.ceil(max / step) * step;
  const out = [];
  for (let v = 0; v <= top + step / 2; v += step) out.push(v);
  return out;
}

const TIME_STEPS = [60e3, 2 * 60e3, 5 * 60e3, 10 * 60e3, 15 * 60e3, 30 * 60e3, 3600e3, 2 * 3600e3, 3 * 3600e3,
  6 * 3600e3, 12 * 3600e3, 86400e3, 2 * 86400e3, 7 * 86400e3];

function timeTicks(t0, t1, maxTicks) {
  const span = t1 - t0;
  let step = TIME_STEPS[TIME_STEPS.length - 1];
  for (const s of TIME_STEPS) { if (span / s <= maxTicks) { step = s; break; } }
  const tz = new Date(t0).getTimezoneOffset() * 60e3;
  let t = Math.ceil((t0 - tz) / step) * step + tz;
  const out = [];
  for (; t <= t1; t += step) out.push(t);
  return { ticks: out, step };
}

function fmtTick(t, step) {
  const d = new Date(t);
  if (step >= 86400e3) return d.toLocaleDateString([], { month: "short", day: "numeric" });
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function roundedTop(x, y, w, hgt, r) {
  r = Math.min(r, w / 2, hgt);
  if (hgt <= 0) return "";
  return `M${x},${y + hgt}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + hgt}Z`;
}

/* ------------------------------------------------------------------ bar list */

function barList(items, { label, value, fmt = F.num, tip, extra, max, limit = 30 }) {
  if (!items.length) return h("div", { class: "empty" }, "Nothing recorded.");
  const shown = items.slice(0, limit);
  const m = max || Math.max(...shown.map(value), 0) || 1;
  const wrap = h("div", { class: "barlist" });
  for (const it of shown) {
    const v = value(it) || 0;
    const row = h("div", { class: "bl-row" + (extra ? " has-extra" : ""), tabindex: "0" },
      h("div", { class: "bl-label", title: label(it) }, label(it)),
      h("div", { class: "bl-track" }, h("div", { class: "bl-bar", style: `width:${Math.max(0.4, (v / m) * 100)}%` })),
      h("div", { class: "bl-value" }, fmt(v)),
      extra ? h("div", { class: "bl-extra" }, extra(it)) : null);
    if (tip) Tip.bind(row, () => tip(it));
    wrap.appendChild(row);
  }
  if (items.length > limit) wrap.appendChild(h("div", { class: "note" }, `+${items.length - limit} more in the table view`));
  return wrap;
}

/* ------------------------------------------------------------------ stacked horizontal bars */

function stackedBars(rows, segs, { fmt = F.num, rowTip } = {}) {
  const totals = rows.map((r) => segs.reduce((a, s) => a + (r.values[s.key] || 0), 0));
  const max = Math.max(...totals, 0) || 1;
  const wrap = h("div", null, h("div", { class: "stack-legend" },
    segs.map((s) => h("span", null, h("i", { style: `background:${s.color}` }), s.label))));
  rows.forEach((r, i) => {
    const bar = h("div", { class: "stack-bar", style: `width:${Math.max(1, (totals[i] / max) * 100)}%` });
    for (const s of segs) {
      const v = r.values[s.key] || 0;
      if (!v) continue;
      const seg = h("div", { class: "stack-seg", tabindex: "0", style: `flex:${v} 1 0;background:${s.color}`, "data-label": fmt(v) });
      Tip.bind(seg, () => ({ title: r.label, rows: [[fmt(v), s.label, s.color], [F.pct(v / totals[i]), "of row"]] }));
      bar.appendChild(seg);
    }
    const row = h("div", { class: "stack-row" }, h("div", { class: "bl-label", title: r.label }, r.label),
      h("div", null, bar), h("div", { class: "bl-value" }, fmt(totals[i])));
    wrap.appendChild(row);
  });
  // Label a segment only where the text fits with padding; otherwise the tooltip and table carry it.
  requestAnimationFrame(() => wrap.querySelectorAll(".stack-seg").forEach((seg) => {
    const text = seg.getAttribute("data-label");
    if (seg.clientWidth > text.length * 6.6 + 12) seg.textContent = text;
  }));
  return wrap;
}

/* ------------------------------------------------------------------ columns over time */

function columnsChart(width, rows, { value, fmtY = F.num, tip, height = 170, t0, bucket }) {
  const m = { l: 48, r: 12, t: 10, b: 24 };
  const W = width, H = height, pw = W - m.l - m.r, ph = H - m.t - m.b;
  const svg = sv("svg", { class: "chart", width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img" });
  const vals = rows.map(value);
  const ticks = niceTicks(Math.max(...vals, 0), 3);
  const top = ticks[ticks.length - 1] || 1;
  const y = (v) => m.t + ph - (v / top) * ph;
  const g = sv("g", { class: "grid" });
  for (const tk of ticks) {
    g.appendChild(sv("line", { x1: m.l, x2: W - m.r, y1: y(tk), y2: y(tk) }));
    svg.appendChild(sv("text", { x: m.l - 6, y: y(tk) + 3.5, "text-anchor": "end" }, fmtY(tk)));
  }
  svg.insertBefore(g, svg.firstChild);
  const n = rows.length || 1, bw = pw / n, barW = Math.max(1, Math.min(24, bw - 2));
  rows.forEach((r, i) => {
    const v = vals[i] || 0;
    const x = m.l + i * bw + (bw - barW) / 2;
    if (v > 0) svg.appendChild(sv("path", { class: "col", d: roundedTop(x, y(v), barW, m.t + ph - y(v), 4) }));
    const hit = sv("rect", { x: m.l + i * bw, y: m.t, width: bw, height: ph, fill: "transparent", tabindex: "-1" });
    Tip.bind(hit, () => tip(r, i));
    svg.appendChild(hit);
  });
  svg.appendChild(sv("line", { class: "axis", x1: m.l, x2: W - m.r, y1: m.t + ph, y2: m.t + ph, stroke: "var(--axis)" }));
  if (t0 !== undefined && bucket) {
    const t1 = t0 + bucket * n;
    const { ticks: tt, step } = timeTicks(t0, t1, Math.max(2, Math.floor(pw / 90)));
    for (const t of tt) {
      const x = m.l + ((t - t0) / (t1 - t0)) * pw;
      svg.appendChild(sv("text", { x, y: H - 6, "text-anchor": "middle" }, fmtTick(t, step)));
    }
  }
  return svg;
}

/* ------------------------------------------------------------------ line chart with crosshair */

function lineChart(width, points, { fmtY = F.num, tip, height = 190, markers = [], t0, t1, stepped = false } = {}) {
  const m = { l: 52, r: 16, t: 14, b: 24 };
  const W = width, H = height, pw = W - m.l - m.r, ph = H - m.t - m.b;
  const svg = sv("svg", { class: "chart", width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img" });
  if (!points.length) return svg;
  const xs0 = t0 !== undefined ? t0 : points[0][0];
  const xs1 = t1 !== undefined ? t1 : points[points.length - 1][0];
  const span = Math.max(1, xs1 - xs0);
  const x = (t) => m.l + ((t - xs0) / span) * pw;
  const ymax = Math.max(...points.map((p) => p[1]), 0);
  const ticks = niceTicks(ymax, 3);
  const top = ticks[ticks.length - 1] || 1;
  const y = (v) => m.t + ph - (v / top) * ph;
  const g = sv("g", { class: "grid" });
  for (const tk of ticks) {
    g.appendChild(sv("line", { x1: m.l, x2: W - m.r, y1: y(tk), y2: y(tk) }));
    svg.appendChild(sv("text", { x: m.l - 6, y: y(tk) + 3.5, "text-anchor": "end" }, fmtY(tk)));
  }
  svg.appendChild(g);
  const { ticks: tt, step } = timeTicks(xs0, xs1, Math.max(2, Math.floor(pw / 90)));
  for (const t of tt) svg.appendChild(sv("text", { x: x(t), y: H - 6, "text-anchor": "middle" }, fmtTick(t, step)));
  let d = "";
  points.forEach((p, i) => {
    if (i === 0) d += `M${x(p[0])},${y(p[1])}`;
    else if (stepped) d += `H${x(p[0])}V${y(p[1])}`;
    else d += `L${x(p[0])},${y(p[1])}`;
  });
  const last = points[points.length - 1];
  svg.appendChild(sv("path", { class: "area", d: `${d}L${x(last[0])},${m.t + ph}L${x(points[0][0])},${m.t + ph}Z` }));
  svg.appendChild(sv("path", { class: "line", d }));
  svg.appendChild(sv("line", { x1: m.l, x2: W - m.r, y1: m.t + ph, y2: m.t + ph, stroke: "var(--axis)" }));
  for (const mk of markers) {
    const mx = x(mk.t);
    svg.appendChild(sv("line", { class: "marker-line", x1: mx, x2: mx, y1: m.t, y2: m.t + ph }));
    const shape = markShape(mk.shape || "down", mx, m.t - 2, 5, mk.critical ? "var(--critical)" : "var(--ink-2)");
    shape.setAttribute("tabindex", "0");
    Tip.bind(shape, () => ({ title: F.datetime(mk.t), rows: [[mk.label, ""]] }));
    svg.appendChild(shape);
  }
  // Selective direct label: the last value.
  svg.appendChild(sv("circle", { class: "dot", cx: x(last[0]), cy: y(last[1]), r: 4 }));
  const cross = sv("line", { class: "crosshair", x1: 0, x2: 0, y1: m.t, y2: m.t + ph, visibility: "hidden" });
  const dot = sv("circle", { class: "dot", r: 4, visibility: "hidden" });
  svg.appendChild(cross); svg.appendChild(dot);
  const overlay = sv("rect", { x: m.l, y: m.t, width: pw, height: ph, fill: "transparent" });
  overlay.addEventListener("pointermove", (e) => {
    const rect = svg.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * W;
    const t = xs0 + ((px - m.l) / pw) * span;
    let lo = 0, hi = points.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (points[mid][0] < t) lo = mid + 1; else hi = mid; }
    let i = lo;
    if (i > 0 && Math.abs(points[i - 1][0] - t) < Math.abs(points[i][0] - t)) i -= 1;
    const p = points[i];
    cross.setAttribute("x1", x(p[0])); cross.setAttribute("x2", x(p[0])); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", x(p[0])); dot.setAttribute("cy", y(p[1])); dot.setAttribute("visibility", "visible");
    const t2 = tip ? tip(p, i) : { title: F.datetime(p[0]), rows: [[fmtY(p[1]), ""]] };
    Tip.show(e, t2.title, t2.rows, t2.body);
  });
  overlay.addEventListener("pointerleave", () => {
    cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); Tip.hide();
  });
  svg.appendChild(overlay);
  return svg;
}

/* ------------------------------------------------------------------ data table */

/* columns: [{key, label, num, fmt(v,row), render(row)->Node, cls, sort(row)->value}] */
function dataTable({ columns, rows, limit = 100, search = true, sortKey = null, sortDir = "desc", selects = [], placeholder = "Filter…" }) {
  const state = { q: "", key: sortKey, dir: sortDir, show: limit, sel: {} };
  const textCache = new WeakMap();
  const rowText = (r) => {
    let t = textCache.get(r);
    if (t === undefined) {
      t = columns.map((c) => {
        const v = r[c.key];
        return v === null || v === undefined ? "" : typeof v === "object" ? JSON.stringify(v) : String(v);
      }).join(" \u0001 ").toLowerCase();
      textCache.set(r, t);
    }
    return t;
  };
  const root = h("div");
  const tools = h("div", { class: "tbl-tools" });
  const count = h("span", { class: "tbl-count" });
  if (search) {
    const inp = h("input", { type: "search", placeholder, "aria-label": "Filter rows" });
    inp.addEventListener("input", () => { state.q = inp.value.trim().toLowerCase(); state.show = limit; draw(); });
    tools.appendChild(inp);
  }
  for (const s of selects) {
    const opts = ["", ...Array.from(new Set(rows.map((r) => r[s.key]).filter((v) => v !== null && v !== undefined && v !== ""))).sort()];
    if (opts.length <= 2) continue;
    const sel = h("select", { "aria-label": s.label }, opts.map((o) => h("option", { value: o }, o === "" ? `All ${s.label}` : String(o))));
    sel.addEventListener("change", () => { state.sel[s.key] = sel.value; state.show = limit; draw(); });
    tools.appendChild(sel);
  }
  tools.appendChild(count);
  const wrap = h("div", { class: "tbl-wrap" });
  const more = h("div", { class: "more" });
  root.append(tools, wrap, more);

  function sortVal(r) {
    const c = columns.find((x) => x.key === state.key);
    const v = c && c.sort ? c.sort(r) : r[state.key];
    return v;
  }

  function draw() {
    let data = rows;
    if (state.q) data = data.filter((r) => rowText(r).includes(state.q));
    for (const [k, v] of Object.entries(state.sel)) if (v) data = data.filter((r) => String(r[k]) === v);
    if (state.key) {
      const dir = state.dir === "asc" ? 1 : -1;
      data = data.slice().sort((a, b) => {
        const va = sortVal(a), vb = sortVal(b);
        if (va === vb) return 0;
        if (va === null || va === undefined) return 1;
        if (vb === null || vb === undefined) return -1;
        return (va > vb ? 1 : -1) * dir;
      });
    }
    const vis = data.slice(0, state.show);
    const thead = h("thead", null, h("tr", null, columns.map((c) => {
      const th = h("th", { class: c.num ? "num" : null, scope: "col" });
      if (state.key === c.key) th.setAttribute("aria-sort", state.dir === "asc" ? "ascending" : "descending");
      const b = h("button", { type: "button" }, c.label);
      b.addEventListener("click", () => {
        if (state.key === c.key) state.dir = state.dir === "asc" ? "desc" : "asc";
        else { state.key = c.key; state.dir = c.num ? "desc" : "asc"; }
        draw();
      });
      th.appendChild(b);
      return th;
    })));
    const tbody = h("tbody", null, vis.map((r) => h("tr", null, columns.map((c) => {
      const td = h("td", { class: [c.num ? "num" : "", c.cls || ""].join(" ").trim() || null });
      if (c.render) { const n = c.render(r); if (n !== null && n !== undefined) append(td, [n]); }
      else {
        const v = r[c.key];
        const text = c.fmt ? c.fmt(v, r) : v === null || v === undefined ? "—" :
          typeof v === "object" ? JSON.stringify(v) : String(v);
        if ((c.cls === "code" || c.cls === "wrap") && text.length > 260) {
          // Long commands and prompts stay a few lines tall until clicked.
          const box = h("div", { class: "clamp", title: "Click to expand" }, text);
          box.addEventListener("click", () => box.classList.add("open"));
          td.appendChild(box);
        } else td.textContent = text;
      }
      return td;
    }))));
    wrap.replaceChildren(h("table", { class: "data" }, thead, tbody));
    count.textContent = data.length === rows.length ? `${rows.length.toLocaleString()} rows` :
      `${data.length.toLocaleString()} of ${rows.length.toLocaleString()} rows`;
    more.replaceChildren();
    if (data.length > state.show) {
      const b = h("button", { class: "linkbtn", type: "button" }, `Show all ${data.length.toLocaleString()}`);
      b.addEventListener("click", () => { state.show = Infinity; draw(); });
      more.appendChild(b);
    }
  }
  draw();
  return root;
}

function statusBadge(status) {
  return h("span", { class: "status " + (status || "pending") }, status || "pending");
}

function modeBadge(mode) {
  const labels = { model: "Claude (Skill tool)", user: "you (/slash)", harness: "Claude Code" };
  return h("span", { class: "mode " + mode, title: mode }, labels[mode] || mode);
}

function pills(obj, fmt = F.num, limit = 30) {
  const entries = Object.entries(obj || {});
  if (!entries.length) return h("div", { class: "empty" }, "None.");
  return h("div", { class: "pills" }, entries.slice(0, limit).map(([k, v]) => h("span", { class: "pill" }, k + " ", h("b", null, fmt(v)))));
}

function kv(pairs) {
  return h("dl", { class: "kv" }, pairs.filter((p) => p).map(([k, v]) => [h("dt", null, k), h("dd", null, v === null || v === undefined || v === "" ? "—" : v)]));
}

function themeToggle() {
  const root = document.documentElement;
  let saved = null;
  try { saved = localStorage.getItem("csa-theme"); } catch (e) { /* storage may be blocked */ }
  if (saved === "light" || saved === "dark") root.setAttribute("data-theme", saved);
  const seg = h("div", { class: "seg", role: "group", "aria-label": "Theme" });
  const set = (v) => {
    if (v === "auto") root.removeAttribute("data-theme"); else root.setAttribute("data-theme", v);
    try { if (v === "auto") localStorage.removeItem("csa-theme"); else localStorage.setItem("csa-theme", v); } catch (e) { /* ignore */ }
    seg.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === v)));
    Charts.renderVisible(true);
  };
  for (const v of ["auto", "light", "dark"]) {
    const b = h("button", { type: "button", "data-v": v, "aria-pressed": String((saved || "auto") === v) }, v[0].toUpperCase() + v.slice(1));
    b.addEventListener("click", () => set(v));
    seg.appendChild(b);
  }
  return seg;
}

function tabs(defs) {
  /* defs: [{id, label, count, build: () => Node}] -> {nav, sections} */
  const nav = h("nav", { class: "tabs", role: "tablist" });
  const sections = [];
  let current = null;
  const built = new Set();
  function select(id, push) {
    for (const d of defs) {
      const on = d.id === id;
      d.btn.setAttribute("aria-selected", String(on));
      d.sec.classList.toggle("active", on);
      if (on && !built.has(id)) { d.sec.appendChild(d.build()); built.add(id); }
    }
    current = id;
    if (push) { try { history.replaceState(null, "", "#" + id); } catch (e) { /* file:// may refuse */ } }
    requestAnimationFrame(() => Charts.renderVisible());
  }
  for (const d of defs) {
    d.btn = h("button", { role: "tab", type: "button", "aria-controls": "tab-" + d.id }, d.label,
      d.count !== undefined && d.count !== null ? h("span", { class: "count" }, String(d.count)) : null);
    d.btn.addEventListener("click", () => select(d.id, true));
    d.sec = h("section", { class: "tab", id: "tab-" + d.id, role: "tabpanel" });
    nav.appendChild(d.btn);
    sections.push(d.sec);
  }
  const initial = (location.hash || "").slice(1);
  return { nav, sections, start: () => select(defs.some((d) => d.id === initial) ? initial : defs[0].id, false) };
}

function downloadLink(label, filename, getText, type = "application/json") {
  const a = h("button", { class: "linkbtn", type: "button" }, label);
  a.addEventListener("click", () => {
    const blob = new Blob([getText()], { type });
    const url = URL.createObjectURL(blob);
    const link = h("a", { href: url, download: filename });
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  return a;
}

/* ------------------------------------------------------------------ status marks (symbol + text, never colour alone) */

const STATUS_MARK = { pass: ["✓", "good"], ok: ["✓", "good"], fail: ["✗", "critical"], error: ["✗", "critical"],
  denied: ["⊘", "serious"], interrupted: ["‖", "warning"], "n/a": ["–", "muted"], pending: ["…", "muted"] };

function statusMark(status, withText = true) {
  const [sym, tone] = STATUS_MARK[status] || ["?", "muted"];
  return h("span", { class: `smark ${tone}`, title: status }, h("b", null, sym), withText ? " " + status : null);
}

/* ------------------------------------------------------------------ strip plot: one dot per run, grouped */

function stripPlot(width, groups, { value, fmt = F.num, tip, height = 170 } = {}) {
  /* groups: [{label, items: [run...]}]; one hue — the x position carries identity */
  const m = { l: 56, r: 10, t: 10, b: 34 };
  const W = width, H = height, pw = W - m.l - m.r, ph = H - m.t - m.b;
  const svg = sv("svg", { class: "chart", width: W, height: H, role: "img" });
  const all = groups.flatMap((g) => g.items.map(value)).filter((v) => v !== null && v !== undefined);
  const ticks = niceTicks(Math.max(...all, 0), 3);
  const top = ticks[ticks.length - 1] || 1;
  const y = (v) => m.t + ph - (v / top) * ph;
  const grid = sv("g", { class: "grid" });
  for (const tk of ticks) {
    grid.appendChild(sv("line", { x1: m.l, x2: W - m.r, y1: y(tk), y2: y(tk) }));
    svg.appendChild(sv("text", { x: m.l - 6, y: y(tk) + 3.5, "text-anchor": "end" }, fmt(tk)));
  }
  svg.insertBefore(grid, svg.firstChild);
  const bw = pw / Math.max(1, groups.length);
  groups.forEach((g, gi) => {
    const cx = m.l + gi * bw + bw / 2;
    svg.appendChild(sv("text", { x: cx, y: H - 18, "text-anchor": "middle" }, F.short(g.label, Math.max(6, Math.floor(bw / 7)))));
    svg.appendChild(sv("text", { x: cx, y: H - 5, "text-anchor": "middle" }, `n=${g.items.length}`));
    const vals = g.items.map(value).filter((v) => v !== null && v !== undefined).sort((a, b) => a - b);
    if (vals.length) {
      const mid = vals.length >> 1;
      const med = vals.length % 2 ? vals[mid] : (vals[mid - 1] + vals[mid]) / 2;
      svg.appendChild(sv("line", { x1: cx - Math.min(28, bw / 3), x2: cx + Math.min(28, bw / 3), y1: y(med), y2: y(med),
        stroke: "var(--ink)", "stroke-width": 2, "stroke-linecap": "round" }));
    }
    g.items.forEach((it, i) => {
      const v = value(it);
      if (v === null || v === undefined) return;
      const jitter = g.items.length > 1 ? ((i % 7) - 3) * Math.min(6, bw / 16) : 0;
      const dot = sv("circle", { class: "dot", cx: cx + jitter, cy: y(v), r: 5, tabindex: "0" });
      Tip.bind(dot, () => (tip ? tip(it, v) : { title: g.label, rows: [[fmt(v), ""]] }));
      svg.appendChild(dot);
    });
  });
  svg.appendChild(sv("line", { x1: m.l, x2: W - m.r, y1: m.t + ph, y2: m.t + ph, stroke: "var(--axis)" }));
  return svg;
}

/* ------------------------------------------------------------------ trace view */

const KIND_LABEL = { prompt: "you", request: "claude", tool: "tool", skill: "skill", event: "event" };

function traceView(steps, { limit = 400, showScope = true } = {}) {
  const state = { q: "", kind: "", status: "", errorsOnly: false, show: limit };
  const root = h("div", { class: "trace" });
  const tools = h("div", { class: "tbl-tools" });
  const q = h("input", { type: "search", placeholder: "Filter steps…", "aria-label": "Filter steps" });
  q.addEventListener("input", () => { state.q = q.value.trim().toLowerCase(); state.show = limit; draw(); });
  const kind = h("select", { "aria-label": "Step kind" }, h("option", { value: "" }, "All steps"),
    ["prompt", "request", "tool", "skill", "event"].map((k) => h("option", { value: k }, k === "request" ? "Claude API requests" : k === "prompt" ? "your prompts" : k + "s")));
  kind.addEventListener("change", () => { state.kind = kind.value; state.show = limit; draw(); });
  const errs = h("label", { class: "tbl-count" }, h("input", { type: "checkbox" }), " problems only");
  errs.firstChild.addEventListener("change", (e) => { state.errorsOnly = e.target.checked; state.show = limit; draw(); });
  const count = h("span", { class: "tbl-count" });
  tools.append(q, kind, errs, count);
  const list = h("div", { class: "trace-list" });
  const more = h("div", { class: "more" });
  root.append(tools, list, more);

  const text = (st) => [st.text, st.input, st.result, st.name, st.model, (st.sigs || []).join(" "), (st.res || []).join(" "), st.what, st.args]
    .filter(Boolean).join(" ").toLowerCase();
  const problem = (st) => ["error", "denied", "interrupted"].includes(st.status) || st.what === "api_error" || st.what === "interrupt" || st.ok === false;

  function row(st) {
    const k = st.k;
    const main = h("div", { class: "tr-main" });
    const meta = h("div", { class: "tr-meta" });
    let detail = null;
    if (k === "prompt") {
      main.append(h("span", { class: "tr-text" }, st.text || "(empty)"));
      meta.append(h("span", { class: "chip" }, st.trigger));
    } else if (k === "request") {
      main.append(h("span", { class: "tr-text" }, st.text || (st.tools && st.tools.length ? "→ " + st.tools.join(", ") : `thinking ${F.num(st.think || 0)} chars`)));
      meta.append(h("span", { class: "chip" }, (st.model || "").replace("claude-", "")), h("span", { class: "chip" }, `ctx ${F.tok(st.ctx)}`),
        h("span", { class: "chip" }, `out ${F.tok(st.out)}`), h("span", { class: "chip" }, F.dur(st.dur)), h("span", { class: "chip" }, F.usd(st.usd)));
      if (st.miss) meta.append(h("span", { class: "chip" }, `cache miss: ${st.miss}`));
      if (st.text && st.text.length > 160) detail = st.text;
    } else if (k === "tool") {
      main.append(statusMark(st.status || "pending", false), h("b", null, " " + st.name + " "), h("span", { class: "tr-text mono" }, st.input || ""));
      meta.append(h("span", { class: "chip" }, F.dur(st.dur)));
      for (const sgn of st.sigs || []) meta.append(h("span", { class: "chip strong" }, sgn));
      for (const r of st.res || []) meta.append(h("span", { class: "chip" }, "reads " + r.split(":").slice(1).join(":")));
      if (st.batch > 1) meta.append(h("span", { class: "chip" }, `parallel ×${st.batch}`));
      if (st.denial) meta.append(h("span", { class: "chip" }, st.denial));
      detail = [st.input, st.result ? "→ " + st.result : ""].filter(Boolean).join("\n\n");
    } else if (k === "skill") {
      main.append(h("b", null, st.name), " ", modeBadge(st.mode), st.args ? h("span", { class: "tr-text" }, " " + st.args) : null);
      if (st.version) meta.append(h("span", { class: "chip" }, "v " + st.version.slice(0, 8)));
      if (st.ok === false) meta.append(statusMark("fail"));
    } else {
      main.append(h("span", { class: "tr-text" }, st.text || st.what));
      meta.append(h("span", { class: "chip" }, st.what));
    }
    if (showScope && st.scope && st.scope !== "main") meta.append(h("span", { class: "chip" }, `${st.scope} ${(st.agent || "").slice(0, 8)}`));
    if (st.inherited) meta.append(h("span", { class: "chip" }, "inherited"));
    const body = h("div", { class: "tr-body" }, main, meta);
    const r = h("div", { class: `tr-row k-${k}` + (problem(st) ? " problem" : ""), tabindex: "0" },
      h("span", { class: "tr-time mono" }, F.time(st.t)), h("span", { class: `tr-kind k-${k}` }, KIND_LABEL[k] || k), body);
    if (detail) {
      const box = h("pre", { class: "tr-detail", hidden: true }, detail);
      body.appendChild(box);
      r.addEventListener("click", () => { box.hidden = !box.hidden; });
      r.classList.add("expandable");
    }
    return r;
  }

  function draw() {
    let data = steps;
    if (state.kind) data = data.filter((s) => s.k === state.kind);
    if (state.errorsOnly) data = data.filter(problem);
    if (state.q) data = data.filter((s) => text(s).includes(state.q));
    list.replaceChildren(...data.slice(0, state.show).map(row));
    count.textContent = `${data.length.toLocaleString()} of ${steps.length.toLocaleString()} steps`;
    more.replaceChildren();
    if (data.length > state.show) {
      const b = h("button", { class: "linkbtn", type: "button" }, `Show ${Math.min(limit, data.length - state.show)} more`);
      b.addEventListener("click", () => { state.show += limit; draw(); });
      more.appendChild(b);
    }
  }
  draw();
  return root;
}

/* ------------------------------------------------------------------ one skill run, in detail */

function runDetail(run, steps) {
  const v = run.version || {};
  const checks = run.checks || [];
  const head = kv([
    ["Run", `${run.run_id} · ${run.skill} invoked by ${run.mode === "model" ? "Claude (Skill tool)" : run.mode === "user" ? "you (/slash)" : "Claude Code"}`],
    ["Version", v.status === "commit" ? `${v.commit} · ${v.subject} (${F.datetime(v.date)})` : v.label],
    ["Asked", run.prompt || run.args || "—"],
    ["Ran", `${F.datetime(run.start_ms)} · ${F.dur(run.duration_ms)} · ${run.turn_count} turns (${run.follow_up_turns} follow-up) · ended: ${run.end_reason}`],
    ["Work", `${run.requests} Claude requests (${run.attributed_requests} attributed) · ${run.tool_calls} tool calls · ${run.tool_errors} errors · ${run.cli_calls} CLI calls · ${run.help_lookups} help lookups · ${run.retries_after_error} retries after an error`],
    ["Cost", `${F.usd(run.cost_usd)} · context ${F.tok(run.context_start)} → ${F.tok(run.context_end)} (peak ${F.tok(run.context_peak)}) · cache hit ${F.pct(run.cache_hit_ratio)}`],
    ["Questions", `${run.question_calls} AskUserQuestion calls (${run.questions_asked} questions)`],
    ["Created", run.objects_created ? `${run.objects_created} objects` : "nothing reported"],
  ]);
  const checkList = checks.length ? h("div", { class: "checks" }, checks.map((c) =>
    h("div", { class: "check-row" }, statusMark(c.status), h("span", null, " " + (c.desc || c.id)), c.detail ? h("span", { class: "muted" }, " — " + c.detail) : null)))
    : h("div", { class: "empty" }, "No checks defined for this skill (add checks/<skill>.json).");
  const resources = (run.resources || []).length ? dataTable({ search: false, limit: 60, rows: run.resources, columns: [
    { key: "t", label: "When", fmt: F.time }, { key: "path", label: "Skill file", cls: "code" }, { key: "kind", label: "Kind" },
    { key: "via", label: "Via" }, { key: "chars", label: "Chars", num: true, fmt: F.tok }, { key: "status", label: "Status", render: (r) => statusMark(r.status, false) }] })
    : h("div", { class: "empty" }, "The run read none of the skill's own files.");
  const expected = h("div", { class: "note" },
    (run.missing_expected || []).length ? `Named by a playbook's "Read first" but never read: ${run.missing_expected.join(", ")}. ` : "",
    (run.not_named_by_playbooks || []).length ? `Read without a playbook naming it: ${run.not_named_by_playbooks.join(", ")}.` : "");
  const cli = (run.cli || []).length ? dataTable({ search: false, limit: 40, rows: run.cli, columns: [
    { key: "signature", label: "Command", cls: "code" }, { key: "calls", label: "Calls", num: true }, { key: "errors", label: "Errors", num: true },
    { key: "help", label: "--help", num: true }] }) : h("div", { class: "empty" }, "No CLI subcommands.");
  const questions = (run.questions || []).length ? dataTable({ search: false, rows: run.questions, columns: [
    { key: "t", label: "When", fmt: F.time }, { key: "header", label: "Topic" }, { key: "question", label: "Question", cls: "wrap" },
    { key: "answer", label: "Answer", cls: "wrap" }] }) : h("div", { class: "empty" }, "No questions asked.");
  const objects = (run.objects || []).length ? dataTable({ search: false, rows: run.objects, columns: [
    { key: "t", label: "When", fmt: F.time }, { key: "verb", label: "Verb" }, { key: "type", label: "Type" }, { key: "id", label: "Id" },
    { key: "name", label: "Name", cls: "wrap" }] }) : h("div", { class: "empty" }, "No objects reported by CLI output.");
  const errors = (run.errors || []).length ? dataTable({ search: false, rows: run.errors, columns: [
    { key: "t", label: "When", fmt: F.time }, { key: "tool", label: "Tool" }, { key: "category", label: "Category" },
    { key: "input", label: "Input", cls: "code" }, { key: "message", label: "Message", cls: "code" }] }) : h("div", { class: "empty" }, "No errors.");
  const actionsList = (run.actions || []).length ? h("ol", { class: "actions" }, run.actions.map((a) =>
    h("li", { class: a.action.includes("✗") ? "problem" : null }, a.action + (a.times > 1 ? `  ×${a.times}` : "")))) : null;
  return h("div", { class: "grid" },
    card({ title: "Run", span: 7 }, head, run.final_message ? h("details", { class: "raw", open: true }, h("summary", null, "Final hand-back"), h("pre", { class: "tr-detail" }, run.final_message)) : null),
    card({ title: `Checks (${run.checks_passed || 0} passed, ${run.checks_failed || 0} failed)`, span: 5 }, checkList),
    card({ title: "Skill files read", sub: "In order, with how they were read", span: 6 }, resources, expected),
    card({ title: "CLI commands", span: 6 }, cli),
    card({ title: "Questions asked", span: 6 }, questions),
    card({ title: "Objects the CLI reported", span: 6 }, objects),
    card({ title: "Errors", span: 12 }, errors),
    actionsList ? card({ title: "What it did", sub: "Actions in order (repeats collapsed) — the sequence the compare view diffs", span: 12 }, actionsList) : null,
    card({ title: "Trace", sub: "Every prompt, Claude API request and tool call in the run — click a row for its input and output", span: 12 }, traceView(steps)));
}
