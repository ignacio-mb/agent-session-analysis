// Render every tab, chart and table twin of a generated report in Node, against a minimal DOM stub.
// Catches runtime errors (a misspelled field, a missing section) without a browser:
//   node scripts/render_check.js <report.html | rollup.html>
"use strict";
const fs = require("fs");
const vm = require("vm");

class Text { constructor(t) { this.t = String(t); } get textContent() { return this.t; } }
class El {
  constructor(tag) { this.tagName = tag; this.attrs = {}; this.children = []; this.style = {}; this.dataset = {}; this.hidden = false; this._text = ""; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k.startsWith("data-")) this.dataset[k.slice(5)] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  appendChild(c) { this.children.push(c); return c; }
  append(...cs) { cs.forEach((c) => this.appendChild(typeof c === "object" ? c : new Text(c))); }
  insertBefore(c) { this.children.unshift(c); return c; }
  replaceChildren(...cs) { this.children = []; this._text = ""; this.append(...cs); }
  addEventListener() {}
  querySelectorAll() { return []; }
  querySelector() { return null; }
  remove() {}
  click() {}
  get classList() { return { toggle() {}, add() {}, remove() {}, contains() { return false; } }; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  set textContent(v) { this.children = []; this._text = String(v); }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  get isConnected() { return true; }
  get offsetParent() { return {}; }
  get clientWidth() { return 640; }
  get offsetWidth() { return 200; }
  get offsetHeight() { return 40; }
  getBoundingClientRect() { return { left: 0, top: 0, width: 640, height: 100 }; }
}

const page = fs.readFileSync(process.argv[2], "utf8");
const data = page.match(/<script id="session-data" type="application\/json">([\s\S]*?)<\/script>/)[1];
const scripts = [...page.matchAll(/<script>\n([\s\S]*?)\n<\/script>/g)].map((m) => m[1]);
const nodes = { "session-data": Object.assign(new El("script"), { _text: data }), app: new El("div") };

Object.assign(globalThis, {
  document: { createElement: (t) => new El(t), createElementNS: (ns, t) => new El(t), createTextNode: (t) => new Text(t),
    getElementById: (id) => nodes[id], body: new El("body"), documentElement: new El("html") },
  window: { addEventListener() {}, innerWidth: 1280, innerHeight: 800 },
  requestAnimationFrame: (f) => f(), location: { hash: "" }, history: { replaceState() {} },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
});

let charts = 0, tables = 0;
vm.runInThisContext(scripts[0], { filename: "lib.js" });
// Build every chart and its table twin as soon as a card is created.
const realChartCard = globalThis.chartCard;
globalThis.chartCard = function (opts) {
  if (!opts.empty) { opts.chart(640); charts++; if (opts.table) { opts.table(); tables++; } }
  return realChartCard(opts);
};
vm.runInThisContext(scripts[1], { filename: "app.js" });
const builders = ["buildOverview", "buildTimeline", "buildSkills", "buildTools", "buildCost", "buildTurns", "buildAgents",
  "buildFiles", "buildShell", "buildErrors", "buildContext", "buildRaw", "overview", "sessions", "skillsTab", "toolsTab"]
  .filter((n) => typeof globalThis[n] === "function");
for (const n of builders) globalThis[n]();
for (const host of Charts.hosts) host._render(640);
if (typeof timelineChart === "function") { timelineChart(900, true, 1); timelineChart(900, false, 4); }
console.log(`ok: ${builders.length} tabs, ${charts} charts, ${tables} table views, ${Charts.hosts.length} chart hosts rendered`);
