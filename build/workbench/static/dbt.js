/* Workbench · dbt Studio (/dbt/) — lineage graph, SQL editor with preview /
   compile / run, a no-code model builder and docs & tests forms that write
   the dbt YAML. Vanilla JS on BSP + WB; dbt runs as Workbench jobs. */
(function () {
  "use strict";
  const { h, icon } = BSP;
  const api = WB.api;
  const BASE = "/dbt";
  const shell = WB.shell("dbt");
  const main = shell.main;
  main.classList.add("dbt-main");

  // nav entry (the shared shell knows the other Workbench pages)
  const nav = shell.top.querySelector(".wb-nav");
  if (nav && !nav.querySelector('a[href="/dbt/"]')) {
    nav.appendChild(h("a", { href: "/dbt/", class: "active", "aria-current": "page" }, icon("stacks", 16), h("span", { class: "lbl" }, "dbt Studio")));
  }

  const LAYERS = ["source", "seed", "staging", "snapshot", "intermediate", "marts", "other", "exposure"];
  const LAYER_LABEL = { source: "Sources", seed: "Seeds", staging: "Staging", snapshot: "Snapshots", intermediate: "Intermediate", marts: "Marts", other: "Other", exposure: "Exposures" };
  const LAYER_RANK = { source: 0, seed: 0, staging: 1, snapshot: 1, intermediate: 2, other: 2, marts: 3, exposure: 4, analysis: 4 };
  const LAYER_INFO = {
    staging: ["Staging", "Land a source table 1:1 — rename and type columns, no business logic."],
    intermediate: ["Intermediate", "Clean, filter, de-duplicate and join into business entities."],
    marts: ["Marts", "Summaries for dashboards and notebooks — group and aggregate."],
  };
  const PREFIX = { staging: "stg_", intermediate: "int_", marts: "mart_" };
  const NAME_RE = /^[a-z][a-z0-9_]*$/;

  const state = {
    projects: [], target: "postgres", pid: null, view: "lineage", model: null,
    graph: null, graphLoading: null, parsing: null, parseFailed: false, lastAutoParse: 0,
    hidden: new Set(), selected: null, search: "",
    dirty: false,
  };

  const P = () => encodeURIComponent(state.pid);
  const A = (path, q) => `${BASE}/api${path}${path.includes("?") ? "&" : "?"}p=${P()}${q ? "&" + q : ""}`;
  const project = () => state.projects.find((p) => p.id === state.pid);

  // ── routing ────────────────────────────────────────────────────────
  function readHash() {
    const parts = (location.hash || "").replace(/^#\/?/, "").split("/").map(decodeURIComponent);
    const [pid, view, model] = parts;
    return { pid: pid || null, view: ["lineage", "models", "activity", "model"].includes(view) ? view : "lineage", model: model || null };
  }
  function go(view, model, replace) {
    const hash = `#/${encodeURIComponent(state.pid)}/${view}${model ? "/" + encodeURIComponent(model) : ""}`;
    if (replace) history.replaceState(null, "", hash); else if (location.hash !== hash) history.pushState(null, "", hash);
    route();
  }
  window.addEventListener("popstate", () => route());
  window.addEventListener("beforeunload", (e) => { if (state.dirty) { e.preventDefault(); e.returnValue = ""; } });

  async function leaveOk() {
    if (!state.dirty) return true;
    const ok = await BSP.confirm({ title: "Discard unsaved changes?", body: "The SQL you edited has not been saved.", okText: "Discard", danger: true });
    if (ok) state.dirty = false;
    return ok;
  }

  // ── code-first editing: the same project folder opens in VS Code (/code/),
  // which has dbt Power User, SQL tools and a terminal with `dbt` ready.
  // Edits made there show up here (lineage goes stale → re-parse).
  function openInVSCode(relPath) {
    const p = project();
    if (!p) return;
    window.open(`/code/?folder=${encodeURIComponent(p.path)}`, "_blank", "noopener");
    if (relPath) BSP.copy(relPath, "File path (Ctrl/⌘ + P in VS Code, then paste)");
  }

  // ── page frame ─────────────────────────────────────────────────────
  const projSel = h("select", { class: "select", "aria-label": "dbt project", onchange: async (e) => {
    if (!(await leaveOk())) { projSel.value = state.pid; return; }
    state.pid = e.target.value; state.graph = null; state.selected = null; state.parseFailed = false; state.lastAutoParse = 0;
    go("lineage");
  } });
  const targetChip = h("span", { class: "chip outline", title: "dbt target (DBT_TARGET)" }, icon("database", 12), h("span", { class: "dbt-target" }, "postgres"));
  const gcpLine = h("div", { class: "caption", style: { marginTop: "6px" } });
  const moreBtn = h("button", { class: "btn ghost icon sm", title: "More", "aria-label": "More actions" }, icon("more", 16));
  moreBtn.onclick = () => BSP.menu(moreBtn, [
    { label: "Refresh lineage (dbt parse)", icon: "refresh", onClick: () => parse(true) },
    { label: "Load seeds (dbt seed)", icon: "file-table", onClick: () => runDbt({ action: "seed" }).then(refreshGraph) },
    { label: "Run all tests", icon: "shield", onClick: () => runDbt({ action: "test" }).then(refreshGraph) },
    { label: "Generate docs", icon: "document", onClick: () => runDbt({ action: "docs" }) },
  ]);
  const head = h("div", { class: "wb-head" },
    h("div", null, h("div", { class: "eyebrow" }, "Workbench"), h("h1", null, "dbt Studio"),
      h("div", { class: "lede" }, "See how data flows from sources to staging, intermediate and marts. Preview, build and test models, create new ones without code, and document them — dbt's YAML is written for you."), gcpLine),
    h("div", { class: "dbt-head-actions" }, projSel, targetChip,
      h("button", { class: "btn secondary sm", onclick: () => addSourceDialog() }, icon("database", 14), "Add source"),
      h("button", { class: "btn primary sm", onclick: () => wizard() }, icon("wand", 14), "New model"),
      h("button", { class: "btn secondary sm", title: "dbt build — seeds, models and tests", onclick: () => runDbt({ action: "build" }).then(refreshGraph) }, icon("play", 14), "Build all"),
      h("button", { class: "btn ghost sm", title: "Prefer code? Open this project in VS Code — dbt Power User, SQL tools and a terminal with dbt are ready. Changes show up here automatically.",
        onclick: () => openInVSCode() }, icon("open-in-new", 14), "Edit in VS Code"),
      moreBtn));
  const tabsEl = h("div", { class: "tabs", role: "tablist" });
  const body = h("div");
  main.append(head, tabsEl, body);

  const TABS = [["lineage", "Lineage", "stacks"], ["models", "Models", "file-table"], ["activity", "Activity", "history"]];
  function renderTabs() {
    tabsEl.replaceChildren(...[...TABS.map(([id, label, ic]) => h("button", {
      role: "tab", "aria-selected": String(state.view === id), class: state.view === id ? "active" : "",
      onclick: async () => { if (await leaveOk()) go(id); },
    }, icon(ic, 15), label)),
    state.view === "model" && state.model ? h("button", { role: "tab", class: "active", "aria-selected": "true" }, icon("file-code", 15), state.model) : null].filter(Boolean));
  }

  // ── data ───────────────────────────────────────────────────────────
  async function loadProjects() {
    const r = await api(`${BASE}/api/projects`);
    state.projects = r.projects; state.target = r.target;
    targetChip.querySelector(".dbt-target").textContent = r.target;
    targetChip.title = `dbt target (DBT_TARGET) · BigQuery target's Google identity: ${r.gcp_identity || "GCP_SA_JSON / default credentials"}`;
    gcpLine.replaceChildren(icon("cloud", 12), " Google identity (bigquery target): ", h("b", null, r.gcp_identity || "GCP_SA_JSON / default credentials"),
      " (", h("a", { href: "/studio/#identities", target: "_blank", rel: "noopener" }, "change"), ")");
    projSel.replaceChildren(...r.projects.map((p) => h("option", { value: p.id }, p.id === p.name ? p.id : `${p.id} (${p.name})`)));
    return r;
  }

  async function loadGraph(force) {
    if (state.graph && !force) return state.graph;
    if (state.graphLoading) return state.graphLoading;
    const pid = state.pid;
    state.graphLoading = api(A("/graph")).then((g) => {
      if (pid !== state.pid) return null;
      state.graph = g;
      g.byId = new Map(g.nodes.map((n) => [n.id, n]));
      g.children = new Map(g.nodes.map((n) => [n.id, []]));
      for (const e of g.edges) g.children.get(e.from) && g.children.get(e.from).push(e.to);
      return g;
    }).finally(() => { state.graphLoading = null; });
    return state.graphLoading;
  }
  async function refreshGraph() {
    await loadGraph(true);
    if (state.graph && (state.graph.stale || !state.graph.has_manifest)) await parse(false);
    if (state.view !== "model") route();
    else if (modelView.refreshStatus) modelView.refreshStatus();
  }

  async function runDbt(req, opts) {
    const job = await api(A("/run"), { body: req });
    return WB.watchJob(job, { base: BASE, hint: false, ...(opts || {}) });
  }

  // user=false: automatic refresh (at most every 20 s, never again after a failure)
  async function parse(user) {
    if (state.parsing) return state.parsing;
    if (user === false && (state.parseFailed || Date.now() - state.lastAutoParse < 20000)) return null;
    if (user === false) state.lastAutoParse = Date.now();
    state.parsing = (async () => {
      const s = await runDbt({ action: "parse" });
      state.parseFailed = s.status !== "succeeded";
      state.lastParseJob = s;
      await loadGraph(true).catch(() => null);
      if (state.parseFailed) BSP.toast("dbt could not read the project — open the job log for the error", "error");
      return s;
    })().finally(() => { state.parsing = null; });
    const s = await state.parsing;
    if (state.view === "lineage" || state.view === "models") route();
    return s;
  }

  const modelsOnly = () => (state.graph ? state.graph.nodes.filter((n) => n.type === "model" || n.type === "seed" || n.type === "snapshot") : []);
  const sourcesOnly = () => (state.graph ? state.graph.nodes.filter((n) => n.type === "source") : []);

  // ── status helpers ─────────────────────────────────────────────────
  function runClass(st) {
    if (!st) return "";
    const s = st.status;
    if (s === "success" || s === "pass") return "ok";
    if (s === "warn") return "warn";
    if (s === "error" || s === "fail" || s === "runtime error") return "fail";
    return "";
  }
  function testClass(t) {
    if (!t || !t.total) return "";
    if (t.fail || t.error) return "fail";
    if (t.warn) return "warn";
    if (t.pass && !t.never && !t.skipped) return "ok";
    return t.pass ? "ok" : "";
  }
  function runBadge(n) {
    const st = n && n.status;
    if (!st) return h("span", { class: "badge neutral", title: "Not run from here yet" }, h("span", { class: "dot" }), "Not run");
    const c = runClass(st);
    const label = { ok: "Built", warn: "Warn", fail: "Failed" }[c] || st.status;
    return h("span", { class: `badge ${c === "ok" ? "ok" : c === "fail" ? "error" : c === "warn" ? "warning" : "neutral"}`, title: `${st.command || ""} ${st.at ? "· " + new Date(st.at).toLocaleString() : ""}${st.message ? "\n" + st.message : ""}` },
      h("span", { class: "dot" }), label, st.at ? h("span", { style: { fontWeight: 400 } }, "· " + BSP.fmtAgo(st.at)) : null);
  }
  function testBadge(n) {
    const t = n && n.tests;
    if (!t || !t.total) return h("span", { class: "badge neutral", title: "No tests yet — add some under Docs & tests" }, "No tests");
    const c = testClass(t);
    const txt = t.fail || t.error ? `${t.fail + t.error} failing` : t.warn ? `${t.warn} warning` : t.pass ? `${t.pass}/${t.total} passing` : `${t.total} not run`;
    return h("span", { class: `badge ${c === "ok" ? "ok" : c === "fail" ? "error" : c === "warn" ? "warning" : "neutral"}`, title: `${t.total} tests` }, icon("shield", 12), txt);
  }
  const layerChip = (layer) => h("span", { class: "chip xs outline" }, h("span", { class: `ldot ${layer}` }), LAYER_LABEL[layer] || layer);

  // ── router ─────────────────────────────────────────────────────────
  let cleanup = [];
  async function route() {
    cleanup.forEach((f) => f()); cleanup = [];
    const r = readHash();
    if (r.pid && r.pid !== state.pid && state.projects.some((p) => p.id === r.pid)) {
      state.pid = r.pid; state.graph = null; state.selected = null; state.parseFailed = false; state.lastAutoParse = 0;
    }
    if (!state.pid && state.projects.length) state.pid = state.projects[0].id;
    projSel.value = state.pid || "";
    state.view = r.view; state.model = r.model;
    renderTabs();
    body.replaceChildren(); body.className = "fade-in";
    if (!state.projects.length) {
      body.append(h("div", { class: "card" }, WB.empty({ ic: "folder", title: "No dbt projects in your workspace",
        body: "dbt Studio looks for folders with a dbt_project.yml under ~/work. The appliance ships dbt_project (bsp_edw: staging → intermediate → marts) — it is mounted at ~/work/dbt_project, or create a project in VS Code." })));
      return;
    }
    if (state.view === "lineage") renderLineage();
    else if (state.view === "models") renderModels();
    else if (state.view === "activity") renderActivity();
    else if (state.view === "model" && state.model) modelView.render(state.model);
    else go("lineage", null, true);
  }

  // ═══════════════════════════ LINEAGE ═══════════════════════════
  const NODE_W = 210, NODE_H = 38, COL_GAP = 96, ROW_GAP = 14;

  function layout(nodes, g) {
    const ids = new Set(nodes.map((n) => n.id));
    const rank = new Map();
    const visiting = new Set();
    function r(n) {
      if (rank.has(n.id)) return rank.get(n.id);
      if (visiting.has(n.id)) return LAYER_RANK[n.layer] ?? 2;
      visiting.add(n.id);
      let v = LAYER_RANK[n.layer] ?? 2;
      for (const p of n.parents) if (ids.has(p)) v = Math.max(v, r(g.byId.get(p)) + 1);
      visiting.delete(n.id);
      rank.set(n.id, v);
      return v;
    }
    nodes.forEach(r);
    const used = [...new Set(rank.values())].sort((a, b) => a - b);
    const colOf = new Map(used.map((v, i) => [v, i]));
    const cols = used.map(() => []);
    for (const n of nodes) cols[colOf.get(rank.get(n.id))].push(n);
    cols.forEach((c) => c.sort((a, b) => (a.layer + a.name).localeCompare(b.layer + b.name)));
    const pos = new Map();
    const reindex = () => cols.forEach((c) => c.forEach((n, i) => pos.set(n.id, i)));
    reindex();
    const bary = (n, rel) => { const xs = rel.filter((x) => pos.has(x) && ids.has(x)).map((x) => pos.get(x)); return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : pos.get(n.id); };
    for (let it = 0; it < 6; it++) {
      const fwd = it % 2 === 0;
      const order = fwd ? cols.map((_, i) => i) : cols.map((_, i) => cols.length - 1 - i);
      for (const ci of order) {
        cols[ci].sort((a, b) => (fwd ? bary(a, a.parents) - bary(b, b.parents) : bary(a, g.children.get(a.id) || []) - bary(b, g.children.get(b.id) || [])));
        cols[ci].forEach((n, i) => pos.set(n.id, i));
      }
    }
    const maxRows = Math.max(1, ...cols.map((c) => c.length));
    const xy = new Map();
    cols.forEach((c, ci) => {
      const off = ((maxRows - c.length) * (NODE_H + ROW_GAP)) / 2;
      c.forEach((n, i) => xy.set(n.id, { x: 40 + ci * (NODE_W + COL_GAP), y: 50 + off + i * (NODE_H + ROW_GAP) }));
    });
    const width = 80 + cols.length * (NODE_W + COL_GAP) - COL_GAP;
    const height = 90 + maxRows * (NODE_H + ROW_GAP);
    const headers = cols.map((c, ci) => {
      const ls = [...new Set(c.map((n) => n.layer))];
      return { x: 40 + ci * (NODE_W + COL_GAP), label: ls.map((l) => LAYER_LABEL[l] || l).join(" · ") };
    });
    return { xy, width, height, headers };
  }

  function lineageOf(g, id) {
    const up = new Set(), down = new Set();
    const walk = (x, set, next) => { for (const y of next(x) || []) if (!set.has(y)) { set.add(y); walk(y, set, next); } };
    walk(id, up, (x) => (g.byId.get(x) || {}).parents);
    walk(id, down, (x) => g.children.get(x));
    return { up, down };
  }

  function renderLineage() {
    const search = h("div", { class: "search" }, icon("search", 14),
      h("input", { class: "input", placeholder: "Find a model, source or column…", value: state.search, "aria-label": "Find in lineage",
        oninput: BSP.debounce((e) => { state.search = e.target.value.trim().toLowerCase(); draw(); }, 150) }));
    const legend = h("div", { class: "row wrap", style: { gap: "6px" } });
    const statusLine = h("span", { class: "caption" });
    const refreshBtn = h("button", { class: "btn ghost sm", title: "Re-read the project (dbt parse)", onclick: () => parse(true) }, icon("refresh", 14), "Refresh");
    const toolbar = h("div", { class: "wb-toolbar", style: { marginBottom: "12px" } }, search, legend, h("span", { class: "spacer" }), statusLine, refreshBtn);
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("role", "img"); svg.setAttribute("aria-label", "Lineage graph");
    const overlay = h("div", { class: "overlay" }, h("div", { class: "row" }, h("span", { class: "wb-jobs-pill show", style: { cursor: "default" } }, h("span", { class: "spin" }), h("span", null, "Reading project…"))));
    const zoom = h("div", { class: "zoom" },
      h("button", { class: "btn ghost icon sm", title: "Zoom in", "aria-label": "Zoom in", onclick: () => zoomBy(0.8) }, icon("add", 14)),
      h("button", { class: "btn ghost icon sm", title: "Fit", "aria-label": "Fit to screen", onclick: () => fit() }, icon("apps", 14)),
      h("button", { class: "btn ghost icon sm", title: "Zoom out", "aria-label": "Zoom out", onclick: () => zoomBy(1.25) }, h("span", { style: { fontWeight: 700, width: "14px", textAlign: "center" } }, "−")));
    const canvas = h("div", { class: "dbt-canvas" }, svg, zoom, overlay);
    const insp = h("div", { class: "card tight dbt-insp" });
    const wrap = h("div", { class: "dbt-lineage noinsp" }, canvas, insp);
    body.append(toolbar, wrap);

    let vb = null, lay = null;
    function setVB() { if (vb) svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`); }
    function fit() {
      if (!lay) return;
      const r = canvas.getBoundingClientRect();
      const ar = (r.width || 800) / (r.height || 500);
      let w = Math.max(lay.width, 600), hgt = Math.max(lay.height, 300);
      if (w / hgt > ar) hgt = w / ar; else w = hgt * ar;
      vb = { x: (lay.width - w) / 2, y: (lay.height - hgt) / 2, w, h: hgt };
      setVB();
    }
    function zoomBy(f, cx, cy) {
      if (!vb) return;
      const nw = Math.min(Math.max(vb.w * f, 200), 20000), k = nw / vb.w;
      cx = cx ?? vb.x + vb.w / 2; cy = cy ?? vb.y + vb.h / 2;
      vb = { x: cx - (cx - vb.x) * k, y: cy - (cy - vb.y) * k, w: nw, h: vb.h * k };
      setVB();
    }
    function toSvg(ev) {
      const r = svg.getBoundingClientRect();
      return { x: vb.x + ((ev.clientX - r.left) / r.width) * vb.w, y: vb.y + ((ev.clientY - r.top) / r.height) * vb.h };
    }
    canvas.addEventListener("wheel", (e) => { if (!vb) return; e.preventDefault(); const p = toSvg(e); zoomBy(e.deltaY > 0 ? 1.12 : 0.89, p.x, p.y); }, { passive: false });
    let drag = null;
    canvas.addEventListener("mousedown", (e) => { if (e.button !== 0 || e.target.closest(".g-node, .zoom") || !vb) return; drag = { x: e.clientX, y: e.clientY, vb: { ...vb }, moved: false }; canvas.classList.add("panning"); });
    const onMove = (e) => {
      if (!drag) return;
      const r = svg.getBoundingClientRect();
      const dx = ((e.clientX - drag.x) / r.width) * drag.vb.w, dy = ((e.clientY - drag.y) / r.height) * drag.vb.h;
      if (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) > 3) drag.moved = true;
      vb = { ...drag.vb, x: drag.vb.x - dx, y: drag.vb.y - dy }; setVB();
    };
    const onUp = () => { if (drag && !drag.moved) { state.selected = null; draw(); } drag = null; canvas.classList.remove("panning"); };
    window.addEventListener("mousemove", onMove); window.addEventListener("mouseup", onUp);
    cleanup.push(() => { window.removeEventListener("mousemove", onMove); window.removeEventListener("mouseup", onUp); });

    function renderLegend(g) {
      const present = new Set(g.nodes.map((n) => n.layer));
      legend.replaceChildren(...LAYERS.filter((l) => present.has(l)).map((l) => h("span", {
        class: `chip outline lchip${state.hidden.has(l) ? " off" : ""}`, role: "button", tabindex: "0", title: state.hidden.has(l) ? "Show" : "Hide",
        onclick: () => { state.hidden.has(l) ? state.hidden.delete(l) : state.hidden.add(l); renderLegend(g); draw(true); },
      }, h("span", { class: `ldot ${l}` }), LAYER_LABEL[l], h("span", { class: "caption" }, String(g.nodes.filter((n) => n.layer === l).length)))));
    }

    function draw(relayout) {
      const g = state.graph;
      if (!g) return;
      const nodes = g.nodes.filter((n) => !state.hidden.has(n.layer));
      if (relayout || !lay) { lay = layout(nodes, g); if (relayout || !vb) fit(); }
      const sel = state.selected && g.byId.get(state.selected) ? state.selected : null;
      const lin = sel ? lineageOf(g, sel) : null;
      const inLin = (id) => !lin || id === sel || lin.up.has(id) || lin.down.has(id);
      const q = state.search;
      const hit = (n) => q && (n.name.toLowerCase().includes(q) || (n.description || "").toLowerCase().includes(q) || n.columns.some((c) => (c.name || "").toLowerCase().includes(q)));
      const NS = "http://www.w3.org/2000/svg";
      const el = (tag, attrs, ...kids) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs || {})) if (v != null) e.setAttribute(k, v); kids.forEach((k) => k && e.appendChild(typeof k === "string" ? document.createTextNode(k) : k)); return e; };
      const frag = document.createDocumentFragment();
      for (const hd of lay.headers) frag.appendChild(el("text", { class: "g-col", x: hd.x + 2, y: 30 }, hd.label));
      for (const e of g.edges) {
        const a = lay.xy.get(e.from), b = lay.xy.get(e.to);
        if (!a || !b) continue;
        const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2, x2 = b.x, y2 = b.y + NODE_H / 2, c = Math.max(40, (x2 - x1) / 2);
        const on = sel && inLin(e.from) && inLin(e.to);
        frag.appendChild(el("path", { class: `g-edge${on ? " hl" : sel || q ? " dim" : ""}`, d: `M${x1},${y1} C${x1 + c},${y1} ${x2 - c},${y2} ${x2},${y2}` }));
      }
      const colour = { source: "var(--dbt-source)", seed: "var(--dbt-seed)", staging: "var(--dbt-staging)", intermediate: "var(--dbt-intermediate)", marts: "var(--dbt-marts)", snapshot: "var(--dbt-snapshot)", exposure: "var(--dbt-exposure)", other: "var(--dbt-other)" };
      for (const n of nodes) {
        const p = lay.xy.get(n.id);
        if (!p) continue;
        const dim = (sel && !inLin(n.id)) || (q && !hit(n));
        const name = n.type === "source" ? `${n.source_name}.${n.name}` : n.name;
        const label = name.length > 27 ? name.slice(0, 26) + "…" : name;
        const sub = n.type === "source" ? "source" : n.type === "exposure" ? "exposure" : `${n.type}${n.materialized ? " · " + n.materialized : ""}`;
        const rc = runClass(n.status), tc = testClass(n.tests);
        const st = rc === "fail" || tc === "fail" ? "var(--dbt-fail)" : rc === "warn" || tc === "warn" ? "var(--dbt-warn)" : rc === "ok" ? "var(--dbt-ok)" : null;
        const grp = el("g", { class: `g-node${n.id === sel ? " sel" : ""}${dim ? " dim" : ""}${q && hit(n) ? " hit" : ""}`, transform: `translate(${p.x},${p.y})`, tabindex: "0", role: "button", "aria-label": `${name} (${LAYER_LABEL[n.layer] || n.layer})` },
          el("title", null, `${name}\n${LAYER_LABEL[n.layer] || n.layer} ${sub}${n.description ? "\n" + n.description : ""}`),
          el("rect", { class: "box", width: NODE_W, height: NODE_H, rx: 6 }),
          el("rect", { x: 0, y: 0, width: 6, height: NODE_H, rx: 3, fill: colour[n.layer] || colour.other }),
          el("text", { x: 16, y: 16 }, label),
          el("text", { class: "sub", x: 16, y: 30 }, sub),
          st ? el("circle", { cx: NODE_W - 14, cy: NODE_H / 2, r: 5, fill: st }) : null,
          n.tests && n.tests.total ? el("text", { class: "sub", x: NODE_W - 24, y: NODE_H / 2 + 4, "text-anchor": "end" }, `${n.tests.total}✓`) : null);
        grp.addEventListener("click", (ev) => { ev.stopPropagation(); state.selected = n.id; draw(); });
        grp.addEventListener("dblclick", () => openNode(n));
        grp.addEventListener("keydown", (ev) => { if (ev.key === "Enter") openNode(n); if (ev.key === " ") { ev.preventDefault(); state.selected = n.id; draw(); } });
        frag.appendChild(grp);
      }
      svg.replaceChildren(frag);
      renderInspector(sel ? g.byId.get(sel) : null);
    }

    function renderInspector(n) {
      wrap.classList.toggle("noinsp", !n);
      if (!n) { insp.replaceChildren(); return; }
      const g = state.graph;
      const isModel = n.type === "model";
      const name = n.type === "source" ? `${n.source_name}.${n.name}` : n.name;
      const chips = (ids) => h("div", { class: "chiplist" }, ids.length ? ids.map((id) => { const x = g.byId.get(id); return x ? h("span", { class: "chip xs outline", onclick: () => { state.selected = id; draw(); } }, h("span", { class: `ldot ${x.layer}` }), x.type === "source" ? `${x.source_name}.${x.name}` : x.name) : null; }) : h("span", { class: "caption" }, "none"));
      insp.replaceChildren(h("div", null,
        h("div", { class: "row between" }, layerChip(n.layer), h("button", { class: "btn ghost icon sm", "aria-label": "Close", onclick: () => { state.selected = null; draw(); } }, icon("close", 14))),
        h("h3", { style: { margin: "8px 0 4px" } }, name),
        n.description ? h("p", { class: "muted", style: { fontSize: "13px" } }, n.description) : h("p", { class: "caption" }, isModel ? "No description yet — add one under Docs & tests." : ""),
        n.type !== "exposure" && n.type !== "source" ? h("div", { class: "row wrap", style: { margin: "10px 0" } }, runBadge(n), testBadge(n)) : null,
        h("dl", { class: "kv" },
          h("dt", null, "Type"), h("dd", null, n.type + (n.materialized ? ` · ${n.materialized}` : "")),
          n.relation ? [h("dt", null, "Warehouse"), h("dd", { class: "mono", style: { wordBreak: "break-all" } }, n.relation.replace(/"/g, ""))] : null,
          n.path ? [h("dt", null, "File"), h("dd", { class: "mono", style: { wordBreak: "break-all" } }, n.path)] : null),
        h("div", { class: "row wrap", style: { gap: "6px", marginBottom: "12px" } },
          isModel ? h("button", { class: "btn primary sm", onclick: () => openNode(n) }, icon("edit", 14), "Open") : null,
          n.type === "source" || n.type === "seed" || isModel ? h("button", { class: "btn secondary sm", onclick: () => quickPreview(n) }, icon("visibility", 14), "Preview") : null,
          isModel || n.type === "seed" || n.type === "snapshot" ? h("button", { class: "btn ghost sm", onclick: () => runDbt({ action: "build", select: n.name }).then(refreshGraph) }, icon("play", 14), "Build") : null,
          isModel ? h("button", { class: "btn ghost sm", onclick: () => runDbt({ action: "test", select: n.name }).then(refreshGraph) }, icon("shield", 14), "Test") : null,
          n.type === "source" || isModel || n.type === "seed" ? h("button", { class: "btn ghost sm", title: "New model that starts from this one", onclick: () => wizard(n) }, icon("wand", 14), "Build on this") : null),
        n.columns.length ? [h("div", { class: "label", style: { marginBottom: "6px" } }, `Documented columns (${n.columns.length})`),
          h("div", { class: "cols" }, n.columns.map((c) => h("div", null, h("span", { class: "mono" }, c.name), h("span", { class: "d", title: c.description }, c.description))))] : null,
        n.test_list && n.test_list.length ? [h("div", { class: "label", style: { margin: "12px 0 6px" } }, "Tests"),
          h("div", { class: "stack", style: { gap: "4px" } }, n.test_list.map((t) => h("div", { class: "row", style: { fontSize: "12.5px" } },
            h("span", { class: `sdot ${t.status === "pass" ? "ok" : t.status === "warn" ? "warn" : t.status === "fail" || t.status === "error" ? "fail" : ""}` }),
            h("span", null, t.kind), t.column ? h("span", { class: "mono caption" }, t.column) : null,
            t.failures ? h("span", { class: "caption" }, `${t.failures} rows`) : null)))] : null,
        h("div", { class: "label", style: { margin: "12px 0 6px" } }, "Upstream"), chips(n.parents),
        h("div", { class: "label", style: { margin: "12px 0 6px" } }, "Downstream"), chips(g.children.get(n.id) || [])));
    }

    async function load() {
      try {
        const g = await loadGraph();
        if (!g) return;
        statusLine.textContent = g.has_manifest ? `${g.nodes.filter((n) => n.type === "model").length} models · ${sourcesOnly().length} sources${g.generated_at ? " · read " + BSP.fmtAgo(g.generated_at) : ""}` : "";
        if (!g.has_manifest) {
          overlay.style.display = "";
          if (state.parseFailed) {
            overlay.replaceChildren(h("div", { style: { maxWidth: "560px" } }, errBox("dbt could not read this project", "Fix the error shown in the log (often a YAML or Jinja typo), then refresh.", state.lastParseJob),
              h("div", { class: "row", style: { marginTop: "10px", justifyContent: "center" } }, h("button", { class: "btn primary sm", onclick: () => parse(true) }, icon("refresh", 14), "Refresh lineage"))));
          } else parse(false);
          return;
        }
        overlay.style.display = "none";
        renderLegend(g);
        draw(true);
        if (g.stale && !state.parsing) parse(false);
      } catch (e) {
        overlay.replaceChildren(h("div", { class: "alert error", style: { maxWidth: "520px" } }, icon("error", 18), h("div", null, h("div", { class: "title" }, "Could not read the project"), e.message)));
      }
    }
    if (state.parsing) state.parsing.then(() => { if (overlay.isConnected) load(); });
    load();
    const ro = new ResizeObserver(() => { if (lay && vb) fit(); });
    ro.observe(canvas);
    cleanup.push(() => ro.disconnect());
  }

  function openNode(n) {
    if (n.type === "model") go("model", n.name);
    else if (n.type === "source" || n.type === "seed") quickPreview(n);
  }

  async function quickPreview(n) {
    const req = n.type === "source" ? { action: "show", inline_sql: `select * from {{ source('${n.source_name}', '${n.name}') }}`, limit: 100 }
      : { action: "show", select: n.name, limit: 100 };
    const title = n.type === "source" ? `${n.source_name}.${n.name}` : n.name;
    const holder = h("div", null, WB.skeletonRows(6));
    BSP.drawer({ title: `Preview · ${title}`, subtitle: "First 100 rows", wide: true, body: holder });
    const out = await previewInto(holder, req);
    return out;
  }

  // ═══════════════════════════ RESULTS ═══════════════════════════
  // years, ids and codes read better without thousands separators
  const PLAIN = /(^|_)(year|yr|yyyy|id|key|code|no|number)$/i;
  function fmtCell(v, col) {
    if (v === null || v === undefined) return h("span", { class: "caption", style: { fontStyle: "italic" } }, "null");
    if (typeof v === "number" && Number.isInteger(v) && PLAIN.test(col || "")) return String(v);
    if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString() : v.toLocaleString(undefined, { maximumFractionDigits: 4 });
    if (typeof v === "boolean") return v ? "true" : "false";
    return String(v);
  }
  function resultsTable(out) {
    const cols = out.columns || [];
    const rows = out.rows || [];
    if (!cols.length) return h("div", { class: "empty" }, h("h4", null, "No rows"), "The query ran but returned nothing.");
    const numeric = new Set(cols.filter((c) => rows.some((r) => typeof r[c] === "number")));
    const t = BSP.table({ columns: cols.map((c) => ({ key: c, label: c, num: numeric.has(c), sort: true, render: (r) => fmtCell(r[c], c) })), rows, pageSize: 50 });
    return h("div", { class: "dbt-results" }, h("div", { class: "caption", style: { marginBottom: "6px" } }, `${rows.length} row${rows.length === 1 ? "" : "s"} · ${cols.length} columns${out.node ? " · " + out.node : ""}`), t.el);
  }
  async function previewInto(holder, req) {
    holder.replaceChildren(WB.skeletonRows(6));
    let s;
    try { s = await runDbt(req); } catch (e) { holder.replaceChildren(errBox("Could not start the preview", e.message)); return null; }
    if (s.status !== "succeeded") {
      holder.replaceChildren(errBox("Preview failed", "dbt reported an error — the job log (bottom right) has the details.", s));
      return null;
    }
    try {
      const out = await api(`${BASE}/api/output/${s.id}`);
      holder.replaceChildren(out.text ? h("pre", { class: "codeblock sql" }, out.text) : resultsTable(out));
      return out;
    } catch (e) { holder.replaceChildren(errBox("No preview output", e.message)); return null; }
  }
  function errBox(title, msg, job) {
    return h("div", { class: "alert error" }, icon("error", 18), h("div", { style: { flex: 1 } }, h("div", { class: "title" }, title), msg,
      job ? h("div", { style: { marginTop: "8px" } }, h("button", { class: "btn secondary sm", onclick: () => WB.jobDrawer(BASE, job) }, icon("notes", 14), "Show log")) : null));
  }

  // ═══════════════════════════ SQL EDITOR ═══════════════════════════
  const KW = new Set(("select from where and or not in is null as with join left right inner outer full cross on group by order having limit offset union all distinct case when then else end " +
    "cast over partition row rows between preceding following unbounded current asc desc nulls first last true false like ilike exists any interval date timestamp insert update delete into values create table view qualify using natural lateral filter within").split(" "));
  function highlight(src) {
    const re = /(\{#[\s\S]*?#\}|\{\{[\s\S]*?\}\}|\{%[\s\S]*?%\}|--[^\n]*|\/\*[\s\S]*?\*\/|'(?:[^']|'')*'|"[^"\n]*"|\b\d+(?:\.\d+)?\b|\b[A-Za-z_][A-Za-z0-9_]*\b)/g;
    let out = "", last = 0, m;
    while ((m = re.exec(src))) {
      out += BSP.esc(src.slice(last, m.index));
      const t = m[0];
      let cls = null;
      if (t.startsWith("{")) cls = "jj";
      else if (t.startsWith("--") || t.startsWith("/*")) cls = "cm";
      else if (t.startsWith("'")) cls = "st";
      else if (/^\d/.test(t)) cls = "nu";
      else if (KW.has(t.toLowerCase())) cls = "kw";
      else if (src[re.lastIndex] === "(") cls = "fn";
      out += cls ? `<span class="tk-${cls}">${BSP.esc(t)}</span>` : BSP.esc(t);
      last = re.lastIndex;
    }
    return out + BSP.esc(src.slice(last));
  }
  function sqlEditor(value, opts) {
    opts = opts || {};
    const gutterIn = h("div", { class: "in" });
    const gutter = h("div", { class: "ed-gutter", "aria-hidden": "true" }, gutterIn);
    const hl = h("pre", { class: "ed-hl", "aria-hidden": "true" });
    const ta = h("textarea", { class: "ed-ta", spellcheck: "false", autocapitalize: "off", autocomplete: "off", wrap: "off", "aria-label": opts.label || "SQL", readonly: opts.readonly || null });
    ta.value = value || "";
    const el = h("div", { class: `ed${opts.short ? " short" : ""}` }, gutter, h("div", { class: "ed-wrap" }, hl, ta));
    let lines = -1;
    function paint() {
      hl.innerHTML = highlight(ta.value) + "\n";
      const n = ta.value.split("\n").length;
      if (n !== lines) { lines = n; gutterIn.textContent = Array.from({ length: n }, (_, i) => i + 1).join("\n"); }
      sync();
    }
    function sync() { hl.scrollTop = ta.scrollTop; hl.scrollLeft = ta.scrollLeft; gutterIn.style.transform = `translateY(${-ta.scrollTop}px)`; }
    ta.addEventListener("input", () => { paint(); opts.onChange && opts.onChange(ta.value); });
    ta.addEventListener("scroll", sync);
    ta.addEventListener("keydown", (e) => {
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key.toLowerCase() === "s") { e.preventDefault(); opts.onSave && opts.onSave(); return; }
      if (mod && e.key === "Enter") { e.preventDefault(); opts.onRun && opts.onRun(); return; }
      if (e.key === "Tab" && !opts.readonly) {
        e.preventDefault();
        const s = ta.selectionStart, en = ta.selectionEnd;
        if (s === en && !e.shiftKey) { ta.setRangeText("    ", s, en, "end"); }
        else {
          const ls = ta.value.lastIndexOf("\n", s - 1) + 1;
          const block = ta.value.slice(ls, en);
          const next = e.shiftKey ? block.replace(/^ {1,4}/gm, "") : block.replace(/^/gm, "    ");
          ta.setRangeText(next, ls, en, "select");
        }
        ta.dispatchEvent(new Event("input"));
      }
    });
    paint();
    return { el, ta, get value() { return ta.value; }, set value(v) { ta.value = v; paint(); }, focus() { ta.focus(); } };
  }
  function sqlBlock(text) {
    const pre = h("pre", { class: "codeblock sql", html: highlight(text || "") });
    return h("div", { style: { position: "relative" } }, pre,
      h("button", { class: "btn ghost icon sm", style: { position: "absolute", top: "6px", right: "6px" }, title: "Copy", "aria-label": "Copy SQL", onclick: () => BSP.copy(text, "SQL") }, icon("copy", 14)));
  }

  // ═══════════════════════════ MODEL VIEW ═══════════════════════════
  const modelView = {};
  modelView.render = async function (name) {
    const holder = h("div", null, WB.skeletonRows(8));
    body.append(holder);
    let m;
    try {
      [m] = await Promise.all([api(A("/model", `name=${encodeURIComponent(name)}`)), loadGraph().catch(() => null)]);
    } catch (e) {
      holder.replaceChildren(h("div", { class: "card" }, WB.empty({ ic: "error", title: `Could not open ${name}`, body: e.message,
        actions: [h("button", { class: "btn secondary", onclick: () => go("lineage") }, icon("arrow-left", 14), "Back to lineage")] })));
      return;
    }
    const nodeOf = () => (state.graph ? state.graph.nodes.find((n) => n.type === "model" && n.name === name) : null);
    let mtime = m.mtime, saved = m.sql;
    state.dirty = false;
    const dirtyDot = h("span", { class: "dirty-dot", title: "Unsaved changes", style: { display: "none" } });
    const saveBtn = h("button", { class: "btn primary sm", disabled: true, title: "Save (Ctrl/⌘ + S)", onclick: () => save() }, icon("check", 14), "Save");
    const setDirty = (d) => { state.dirty = d; dirtyDot.style.display = d ? "" : "none"; saveBtn.disabled = !d; };
    const ed = sqlEditor(m.sql, { label: `SQL for ${name}`, onChange: (v) => setDirty(v !== saved), onSave: () => save(), onRun: () => preview() });
    const up = h("input", { type: "checkbox" }), down = h("input", { type: "checkbox" });
    const badges = h("div", { class: "row wrap" });
    modelView.refreshStatus = () => { const n = nodeOf(); badges.replaceChildren(runBadge(n), testBadge(n)); };
    modelView.refreshStatus();

    async function save() {
      if (!state.dirty) return true;
      try {
        const r = await BSP.busy(saveBtn, () => api(A("/model/sql", `name=${encodeURIComponent(name)}`), { method: "PUT", body: { sql: ed.value, mtime } }));
        mtime = r.mtime; saved = ed.value;
        setDirty(false);
        BSP.toast(`Saved ${r.path}`, "success");
        state.graph && (state.graph.stale = true);
        return true;
      } catch (e) {
        if (e.status === 409) {
          const ok = await BSP.confirm({ title: "File changed on disk", body: `${m.path} was changed elsewhere (VS Code or another tab) since you opened it. Overwrite it with your version?`, okText: "Overwrite", danger: true });
          if (ok) { mtime = null; return save(); }
        } else BSP.toast(e.message, "error");
        return false;
      }
    }
    const sel = () => ({ select: name, upstream: up.checked, downstream: down.checked });
    async function act(action, extra) {
      if (state.dirty && !(await save())) return;
      await runDbt({ action, ...sel(), ...(extra || {}) });
      await refreshGraph();
    }
    async function preview() {
      showTab("results");
      const req = state.dirty ? { action: "show", inline_sql: ed.value, limit: 100 } : { action: "show", select: name, limit: 100 };
      await previewInto(panes.results, req);
    }
    async function compile() {
      showTab("compiled");
      panes.compiled.replaceChildren(WB.skeletonRows(4));
      const req = state.dirty ? { action: "compile", inline_sql: ed.value } : { action: "compile", select: name };
      const s = await runDbt(req);
      if (s.status !== "succeeded") { panes.compiled.replaceChildren(errBox("Compile failed", "See the job log for the Jinja/SQL error.", s)); return; }
      try { const out = await api(`${BASE}/api/output/${s.id}`); panes.compiled.replaceChildren(sqlBlock(out.compiled || "")); }
      catch (e) { panes.compiled.replaceChildren(errBox("No compiled SQL", e.message)); }
    }

    const head = h("div", { class: "dbt-model-head" },
      h("div", null,
        h("div", { class: "row", style: { marginBottom: "6px" } },
          h("button", { class: "btn ghost sm", onclick: async () => { if (await leaveOk()) go("lineage"); } }, icon("arrow-left", 14), "Lineage"),
          layerChip(m.layer), h("span", { class: "mono caption" }, m.path)),
        h("h2", null, icon("file-code", 20), name, dirtyDot),
        h("div", { style: { marginTop: "8px" } }, badges)),
      h("div", { class: "stack", style: { alignItems: "flex-end" } },
        h("div", { class: "dbt-actions" }, saveBtn,
          h("button", { class: "btn secondary sm", title: "Show the first 100 rows (Ctrl/⌘ + Enter)", onclick: () => preview() }, icon("visibility", 14), "Preview"),
          h("button", { class: "btn ghost sm", title: "Show the SQL dbt will run", onclick: () => compile() }, icon("file-code", 14), "Compile"),
          h("button", { class: "btn ghost sm", title: "Open the project in VS Code and copy this file's path (Ctrl/⌘ + P, paste)", onclick: () => openInVSCode(m.path) }, icon("open-in-new", 14), "VS Code"),
          h("span", { class: "sep" }),
          h("button", { class: "btn ghost sm", title: "Create/refresh the table or view", onclick: () => act("run") }, icon("play", 14), "Run"),
          h("button", { class: "btn secondary sm", title: "Run + test", onclick: () => act("build") }, icon("zap", 14), "Build"),
          h("button", { class: "btn ghost sm", onclick: () => act("test") }, icon("shield", 14), "Test")),
        h("div", { class: "dbt-actions" },
          h("label", { class: "checkbox", title: "Also run the models this one reads from" }, up, "with upstream"),
          h("label", { class: "checkbox", title: "Also run the models that read from this one" }, down, "with downstream"),
          h("button", { class: "btn ghost sm", title: "Rebuild incremental models from scratch", onclick: () => act("build", { full_refresh: true }) }, "Full refresh"))));

    const subTabs = h("div", { class: "tabs", role: "tablist", style: { marginTop: "16px" } });
    const panes = {
      results: h("div", null, h("div", { class: "empty" }, h("div", { class: "ic" }, icon("visibility", 22)), h("h4", null, "Preview the data"), "Press ", h("b", null, "Preview"), " (or ", h("span", { class: "kbd" }, "Ctrl"), " + ", h("span", { class: "kbd" }, "Enter"), ") to see the first 100 rows. Unsaved edits are previewed as-is.")),
      compiled: h("div", null, h("div", { class: "empty" }, h("h4", null, "Compiled SQL"), "Press ", h("b", null, "Compile"), " to see the plain SQL that dbt runs in the warehouse.")),
      docs: h("div"),
      lineage: h("div"),
    };
    const SUB = [["docs", "Docs & tests", "notes"], ["results", "Results", "file-table"], ["compiled", "Compiled SQL", "file-code"], ["lineage", "Upstream & downstream", "stacks"]];
    let current = "docs";
    function showTab(id) {
      current = id;
      subTabs.replaceChildren(...SUB.map(([k, label, ic]) => h("button", { role: "tab", class: current === k ? "active" : "", "aria-selected": String(current === k), onclick: () => showTab(k) }, icon(ic, 14), label)));
      for (const [k, el] of Object.entries(panes)) el.style.display = k === id ? "" : "none";
    }
    holder.replaceChildren(head, ed.el, subTabs, ...Object.values(panes));
    showTab("docs");
    ed.focus();
    docsForm(panes.docs, name, m);
    renderNeighbours();
    cleanup.push(() => { modelView.refreshStatus = null; });

    function renderNeighbours() {
      const n = nodeOf(), g = state.graph;
      if (!n || !g) { panes.lineage.replaceChildren(h("p", { class: "caption" }, "Refresh the lineage to see this model's neighbours.")); return; }
      const chip = (id) => { const x = g.byId.get(id); return x ? h("span", { class: "chip outline", onclick: () => (x.type === "model" ? leaveOk().then((ok) => ok && go("model", x.name)) : quickPreview(x)) }, h("span", { class: `ldot ${x.layer}` }), x.type === "source" ? `${x.source_name}.${x.name}` : x.name) : null; };
      panes.lineage.replaceChildren(h("div", { class: "card" },
        h("div", { class: "label", style: { marginBottom: "8px" } }, "Reads from"), h("div", { class: "chiplist" }, n.parents.length ? n.parents.map(chip) : h("span", { class: "caption" }, "nothing")),
        h("div", { class: "label", style: { margin: "16px 0 8px" } }, "Feeds"), h("div", { class: "chiplist" }, (g.children.get(n.id) || []).length ? g.children.get(n.id).map(chip) : h("span", { class: "caption" }, "nothing yet"))));
    }
  };

  // ═══════════════════════════ DOCS & TESTS FORM ═══════════════════════════
  async function docsForm(el, name, m) {
    el.replaceChildren(WB.skeletonRows(5));
    let colsInfo = { columns: [], live: false };
    try { colsInfo = await api(A("/columns", `ref=${encodeURIComponent(name)}`)); } catch (_) { /* not built / not parsed yet */ }
    const props = m.props;
    const docCols = new Map(props.columns.map((c) => [c.name, c]));
    const order = [...colsInfo.columns.map((c) => c.name), ...props.columns.map((c) => c.name)].filter((v, i, a) => a.indexOf(v) === i);
    const types = new Map(colsInfo.columns.map((c) => [c.name, c.type]));
    const models = modelsOnly().map((n) => n.name).filter((n) => n !== name).sort();

    const desc = h("textarea", { class: "textarea", rows: 3, placeholder: "What is this model, who uses it, and what does one row mean?" });
    desc.value = props.description || "";
    const inheritLabel = m.materialized && !props.materialized ? `Folder default (${m.materialized})` : "Folder default";
    const mat = h("select", { class: "select" }, h("option", { value: "" }, inheritLabel), ...["view", "table", "incremental", "ephemeral"].map((x) => h("option", { value: x }, x)));
    mat.value = props.materialized || "";
    const uk = h("input", { class: "input mono", placeholder: "e.g. claim_id", value: props.unique_key || "", list: "dbt-cols-" + name });
    const ukField = h("div", { class: "field" }, h("label", null, "Unique key (incremental)"), uk, h("div", { class: "hint" }, "New rows replace old rows with the same key."));
    const syncUk = () => { ukField.style.display = mat.value === "incremental" ? "" : "none"; };
    mat.onchange = syncUk; syncUk();
    const tags = h("input", { class: "input", placeholder: "billing, finance", value: props.tags.join(", ") });
    const dl = h("datalist", { id: "dbt-cols-" + name }, order.map((c) => h("option", { value: c })));

    const rows = [];
    const tbody = h("tbody");
    function colRow(cname) {
      const c = docCols.get(cname) || { name: cname, description: "", tests: {}, other_tests: [] };
      const t = c.tests || {};
      const d = h("input", { class: "input", placeholder: "Describe this column", value: c.description || "" });
      const uq = h("input", { type: "checkbox", checked: !!t.unique || null, "aria-label": `${cname} unique` });
      const nn = h("input", { type: "checkbox", checked: !!t.not_null || null, "aria-label": `${cname} not null` });
      const av = h("input", { type: "checkbox", checked: !!t.accepted_values || null });
      const avVals = h("input", { class: "input", placeholder: "Open, Closed", value: t.accepted_values ? t.accepted_values.values.join(", ") : "" });
      const avWarn = h("input", { type: "checkbox", checked: t.accepted_values && t.accepted_values.severity === "warn" || null });
      const avSub = h("div", { class: "sub" }, avVals, h("label", { class: "checkbox", title: "Report as a warning instead of failing the build" }, avWarn, "warn only"));
      const rl = h("input", { type: "checkbox", checked: !!t.relationships || null });
      const rlModel = h("select", { class: "select" }, h("option", { value: "" }, "model…"), models.map((x) => h("option", { value: x }, x)));
      const fieldList = h("datalist", { id: `dbt-rel-${name}-${cname}` });
      const rlField = h("input", { class: "input mono", placeholder: "column", list: fieldList.id, value: t.relationships ? t.relationships.field : cname });
      const rlWarn = h("input", { type: "checkbox", checked: t.relationships && t.relationships.severity === "warn" || null });
      if (t.relationships) { if (!models.includes(t.relationships.to)) rlModel.appendChild(h("option", { value: t.relationships.to }, t.relationships.to)); rlModel.value = t.relationships.to; }
      rlModel.onchange = async () => {
        fieldList.replaceChildren();
        if (!rlModel.value) return;
        try { const r = await api(A("/columns", `ref=${encodeURIComponent(rlModel.value)}`)); fieldList.replaceChildren(...r.columns.map((x) => h("option", { value: x.name }))); } catch (_) { /* free text */ }
      };
      const rlSub = h("div", { class: "sub" }, rlModel, rlField, fieldList, h("label", { class: "checkbox" }, rlWarn, "warn only"));
      const sync = () => { avSub.style.display = av.checked ? "" : "none"; rlSub.style.display = rl.checked ? "" : "none"; };
      av.onchange = sync; rl.onchange = sync; sync();
      rows.push({
        name: cname,
        get() {
          const tests = {};
          if (uq.checked) tests.unique = { severity: (t.unique && t.unique.severity) || "error" };
          if (nn.checked) tests.not_null = { severity: (t.not_null && t.not_null.severity) || "error" };
          if (av.checked) tests.accepted_values = { values: avVals.value.split(",").map((x) => x.trim()).filter(Boolean), severity: avWarn.checked ? "warn" : "error" };
          if (rl.checked) tests.relationships = { to: rlModel.value, field: rlField.value.trim(), severity: rlWarn.checked ? "warn" : "error" };
          return { name: cname, description: d.value, tests };
        },
        validate() {
          if (av.checked && !avVals.value.trim()) return `${cname}: list the accepted values`;
          if (rl.checked && (!rlModel.value || !rlField.value.trim())) return `${cname}: pick the model and column it must exist in`;
          return null;
        },
      });
      return h("tr", null,
        h("td", null, h("span", { class: "nm" }, cname), types.get(cname) ? h("span", { class: "ty" }, types.get(cname)) : !colsInfo.live ? null : h("span", { class: "ty" }, "not in warehouse"),
          c.other_tests && c.other_tests.length ? h("div", { class: "ty", title: "Kept as they are" }, "+ " + c.other_tests.join(", ")) : null),
        h("td", { style: { minWidth: "240px" } }, d),
        h("td", { class: "c" }, uq), h("td", { class: "c" }, nn),
        h("td", null, h("label", { class: "checkbox" }, av, "allowed values"), avSub),
        h("td", null, h("label", { class: "checkbox" }, rl, "must exist in"), rlSub));
    }
    order.forEach((c) => tbody.appendChild(colRow(c)));
    const addCol = h("input", { class: "input mono", placeholder: "column_name", style: { width: "180px" } });
    const addBtn = h("button", { class: "btn ghost sm", onclick: () => {
      const v = addCol.value.trim();
      if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(v)) return BSP.toast("Column names: letters, digits and _", "error");
      if (rows.some((r) => r.name === v)) return BSP.toast(`${v} is already listed`, "info");
      tbody.appendChild(colRow(v)); addCol.value = "";
    } }, icon("add", 14), "Add column");

    const fileNote = h("span", { class: "caption" }, "Saved to ", h("code", null, props.file));
    const saveBtn = h("button", { class: "btn primary", onclick: async () => {
      const problems = rows.map((r) => r.validate()).filter(Boolean);
      if (problems.length) return BSP.toast(problems[0], "error");
      const payload = {
        description: desc.value, materialized: mat.value || null, unique_key: mat.value === "incremental" ? uk.value.trim() || null : null,
        tags: tags.value.split(",").map((x) => x.trim()).filter(Boolean), columns: rows.map((r) => r.get()),
      };
      try {
        const r = await BSP.busy(saveBtn, () => api(A("/model/props", `name=${encodeURIComponent(name)}`), { method: "PUT", body: payload }));
        m.props = r.props;
        fileNote.replaceChildren("Saved to ", h("code", null, r.props.file));
        BSP.toast(`Docs & tests saved to ${r.props.file}`, "success");
        if (r.warning) BSP.toast(r.warning, "info");
        state.graph && (state.graph.stale = true);
        loadGraph(true).catch(() => {});
      } catch (e) { BSP.toast(e.message, "error"); }
    } }, icon("check", 16), "Save docs & tests");
    const testBtn = h("button", { class: "btn secondary", onclick: async () => { await runDbt({ action: "test", select: name }); await refreshGraph(); } }, icon("shield", 16), "Run tests");

    el.replaceChildren(h("div", { class: "card" },
      h("div", { class: "dbt-form-grid" },
        h("div", { class: "field" }, h("label", null, "Description"), desc),
        h("div", null, h("div", { class: "field" }, h("label", null, "Materialization"), mat, h("div", { class: "hint" }, "view = always fresh, table = fast to query, incremental = append/merge new rows.")), ukField),
        h("div", { class: "field" }, h("label", null, "Tags"), tags, h("div", { class: "hint" }, "Comma-separated. Select by tag: dbt build -s tag:billing"))),
      dl,
      h("div", { class: "row between wrap", style: { margin: "6px 0 10px" } },
        h("h4", null, "Columns & tests"),
        h("span", { class: "caption" }, colsInfo.live ? "Columns read from the warehouse." : "Build the model to list its columns from the warehouse; documented columns are shown.")),
      order.length ? null : h("div", { class: "alert info", style: { marginBottom: "10px" } }, icon("info", 18), h("div", null, "No columns known yet. Build the model (", h("b", null, "Build"), " above) or add a column by name below.")),
      h("div", { class: "table-wrap", style: { maxHeight: "520px" } }, h("table", { class: "coltable" },
        h("thead", null, h("tr", null, h("th", null, "Column"), h("th", null, "Description"), h("th", { class: "c", title: "No duplicates" }, "Unique"), h("th", { class: "c", title: "Never empty" }, "Not null"), h("th", null, "Accepted values"), h("th", null, "Relationship"))),
        tbody)),
      h("div", { class: "row wrap", style: { marginTop: "10px" } }, addCol, addBtn),
      h("hr", { class: "divider", style: { margin: "16px 0" } }),
      h("div", { class: "row wrap" }, saveBtn, testBtn, h("span", { class: "spacer", style: { flex: 1 } }), fileNote)));
  }

  // ═══════════════════════════ MODELS TABLE ═══════════════════════════
  async function renderModels() {
    const card = h("div", { class: "card" }, WB.skeletonRows(8));
    body.append(card);
    let g;
    try { g = await loadGraph(); } catch (e) { card.replaceChildren(errBox("Could not read the project", e.message)); return; }
    if (!g) return;
    if (!g.has_manifest) { card.replaceChildren(WB.empty({ ic: "refresh", title: "Reading the project…", body: "dbt parse is running; this list fills in when it finishes." })); parse(false); return; }
    const q = h("input", { class: "input", placeholder: "Filter models…", "aria-label": "Filter models" });
    const rows = g.nodes.filter((n) => n.type !== "exposure").sort((a, b) => (LAYERS.indexOf(a.layer) - LAYERS.indexOf(b.layer)) || a.name.localeCompare(b.name));
    const t = BSP.table({
      rowKey: "id", pageSize: 100,
      columns: [
        { key: "name", label: "Name", sort: true, render: (n) => h("span", { class: "row", style: { gap: "8px" } }, h("span", { class: `ldot ${n.layer}` }), h("b", { class: "mono" }, n.type === "source" ? `${n.source_name}.${n.name}` : n.name)) },
        { key: "layer", label: "Layer", sort: (n) => LAYERS.indexOf(n.layer), render: (n) => LAYER_LABEL[n.layer] || n.layer },
        { key: "materialized", label: "Type", sort: true, render: (n) => n.type === "model" ? n.materialized : n.type },
        { key: "status", label: "Last run", sort: (n) => (n.status ? n.status.status : ""), render: (n) => (n.type === "source" ? "" : runBadge(n)) },
        { key: "tests", label: "Tests", sort: (n) => (n.tests ? n.tests.total : 0), render: (n) => testBadge(n) },
        { key: "description", label: "Description", render: (n) => h("span", { class: "caption", title: n.description }, (n.description || "").slice(0, 90)) },
      ],
      rows,
      onRowClick: (n) => openNode(n),
    });
    q.oninput = BSP.debounce(() => { const v = q.value.trim().toLowerCase(); t.update(rows.filter((n) => !v || n.name.toLowerCase().includes(v) || (n.description || "").toLowerCase().includes(v) || n.layer.includes(v))); }, 120);
    card.replaceChildren(h("div", { class: "wb-toolbar", style: { marginBottom: "12px" } }, h("div", { class: "search" }, icon("search", 14), q), h("span", { class: "spacer" }),
      h("span", { class: "caption" }, `${rows.length} nodes`)), t.el);
  }

  // ═══════════════════════════ ACTIVITY ═══════════════════════════
  async function renderActivity() {
    const card = h("div", { class: "card" }, WB.skeletonRows(6));
    body.append(card);
    async function load() {
      const r = await api(`${BASE}/api/jobs?limit=100`);
      const jobs = r.jobs.filter((j) => !j.meta || !j.meta.project || j.meta.project === state.pid);
      const t = BSP.table({
        rowKey: "id",
        columns: [
          { key: "title", label: "Command", render: (j) => h("b", null, j.title) },
          { key: "status", label: "Status", render: (j) => WB.statusBadge(j.status) },
          { key: "created", label: "Started", sort: true, render: (j) => (j.started || j.created ? new Date((j.started || j.created) * 1000).toLocaleString() : "") },
          { key: "dur", label: "Took", num: true, render: (j) => (j.finished && j.started ? WB.fmtDur(j.finished - j.started) : "") },
          { key: "error", label: "", render: (j) => h("span", { class: "caption" }, j.error || "") },
        ],
        rows: jobs, pageSize: 50,
        onRowClick: (j) => (j.status === "running" || j.status === "queued" ? WB.watchJob(j, { base: BASE }) : WB.jobDrawer(BASE, j)),
        empty: WB.empty({ ic: "history", title: "Nothing run yet", body: "Builds, tests, previews and parses for this project show up here." }),
      });
      card.replaceChildren(h("div", { class: "card-head" }, h("h3", null, "dbt activity", h("span", { class: "count" }, String(jobs.length))),
        h("button", { class: "btn ghost sm", onclick: () => load() }, icon("refresh", 14), "Refresh")), t.el);
    }
    try { await load(); } catch (e) { card.replaceChildren(errBox("Could not load activity", e.message)); }
  }

  // ═══════════════════════════ NEW MODEL WIZARD ═══════════════════════════
  async function wizard(startNode) {
    try { await loadGraph(); } catch (e) { BSP.toast(e.message, "error"); return; }
    const g = state.graph;
    if (!g || !g.has_manifest) { BSP.toast("Reading the project first — try again in a moment", "info"); parse(false); return; }
    const spec = { layer: "intermediate", name: "", description: "", materialized: "", from: null, joins: [], columns: [], filters: [], group_by: [], aggregates: [] };
    const colCache = new Map();
    const relKey = (r) => (r.kind === "ref" ? `ref:${r.model}` : `source:${r.source}.${r.table}`);
    const relLabel = (r) => (r.kind === "ref" ? r.model : `${r.source}.${r.table}`);
    const options = [
      ...sourcesOnly().sort((a, b) => (a.source_name + a.name).localeCompare(b.source_name + b.name)).map((n) => ({ group: "Sources (raw tables)", rel: { kind: "source", source: n.source_name, table: n.name } })),
      ...modelsOnly().sort((a, b) => (LAYERS.indexOf(a.layer) - LAYERS.indexOf(b.layer)) || a.name.localeCompare(b.name)).map((n) => ({ group: `${LAYER_LABEL[n.layer] || n.layer} models`, rel: { kind: "ref", model: n.name } })),
    ];
    function relSelect(value, onchange, placeholder) {
      const s = h("select", { class: "select grow" }, h("option", { value: "" }, placeholder || "Choose…"));
      const groups = {};
      for (const o of options) (groups[o.group] = groups[o.group] || h("optgroup", { label: o.group })).appendChild(h("option", { value: relKey(o.rel) }, relLabel(o.rel)));
      Object.values(groups).forEach((gr) => s.appendChild(gr));
      if (value) s.value = relKey(value);
      s.onchange = () => { const o = options.find((x) => relKey(x.rel) === s.value); onchange(o ? { ...o.rel } : null); };
      return s;
    }
    async function columnsOf(rel) {
      if (!rel) return [];
      const k = relKey(rel);
      if (!colCache.has(k)) {
        const q = rel.kind === "ref" ? `ref=${encodeURIComponent(rel.model)}` : `source=${encodeURIComponent(rel.source)}&table=${encodeURIComponent(rel.table)}`;
        colCache.set(k, api(A("/columns", q)).catch((e) => ({ columns: [], error: e.message })));
      }
      return colCache.get(k);
    }
    if (startNode) {
      spec.from = startNode.type === "source" ? { kind: "source", source: startNode.source_name, table: startNode.name } : { kind: "ref", model: startNode.name };
      spec.layer = startNode.layer === "source" || startNode.layer === "seed" ? "staging" : startNode.layer === "staging" ? "intermediate" : "marts";
    }

    // 1. layer, name, description
    const nameIn = h("input", { class: "input mono", placeholder: "int_open_tickets", "aria-label": "Model name" });
    const nameErr = h("div", { class: "error" });
    const descIn = h("input", { class: "input", placeholder: "Open tickets with their customer and service area" });
    const matSel = h("select", { class: "select" }, h("option", { value: "" }, "Folder default"), ["view", "table"].map((x) => h("option", { value: x }, x)));
    const layerPick = h("div", { class: "layer-pick" });
    let nameTouched = false;
    function suggestName() {
      if (nameTouched) return;
      const base = spec.from ? (spec.from.kind === "ref" ? spec.from.model : spec.from.table).replace(/^(brz|stg|int|mart|fct|dim)_/, "") : "";
      nameIn.value = base ? PREFIX[spec.layer] + base + (spec.layer === "marts" ? "_summary" : "") : "";
      spec.name = nameIn.value; checkName();
    }
    function checkName() {
      const v = nameIn.value.trim();
      const exists = state.graph.nodes.some((n) => n.type === "model" && n.name === v);
      nameErr.textContent = !v ? "" : !NAME_RE.test(v) ? "Lower-case letters, digits and _ ; start with a letter." : exists ? `A model called ${v} already exists.` : "";
      nameIn.classList.toggle("invalid", !!nameErr.textContent);
    }
    nameIn.oninput = () => { nameTouched = true; spec.name = nameIn.value.trim(); checkName(); refreshSql(); };
    descIn.oninput = () => { spec.description = descIn.value; refreshSql(); };
    matSel.onchange = () => { spec.materialized = matSel.value; refreshSql(); };
    function renderLayers() {
      layerPick.replaceChildren(...Object.entries(LAYER_INFO).map(([k, [title, blurb]]) => h("button", { type: "button", class: `tile${spec.layer === k ? " selected" : ""}`,
        onclick: () => { spec.layer = k; renderLayers(); suggestName(); refreshSql(); } },
        h("div", { class: "tile-title" }, h("span", { class: `ldot ${k}` }), title), h("div", { class: "tile-blurb" }, blurb))));
    }
    renderLayers();

    // 2. start from
    const fromHolder = h("div", { class: "wz-row" });
    function renderFrom() { fromHolder.replaceChildren(relSelect(spec.from, (r) => { spec.from = r; spec.columns = []; spec.filters = []; spec.group_by = []; spec.aggregates = []; suggestName(); renderAll(); }, "Pick a source table or model…")); }

    // 3. joins
    const joinsHolder = h("div");
    async function renderJoins() {
      joinsHolder.replaceChildren();
      for (const [i, j] of spec.joins.entries()) {
        const alias = `j${i + 1}`;
        const leftCols = await allColumns(i);
        const rightCols = (await columnsOf(j.rel)).columns || [];
        const typeSel = h("select", { class: "select" }, [["left", "keep all rows (left join)"], ["inner", "only matches (inner join)"]].map(([v, l]) => h("option", { value: v }, l)));
        typeSel.value = j.type; typeSel.onchange = () => { j.type = typeSel.value; refreshSql(); };
        const lSel = h("select", { class: "select" }, h("option", { value: "" }, "key…"), leftCols.map((c) => h("option", { value: `${c.rel}.${c.name}` }, `${c.rel === "base" ? "" : c.rel + "."}${c.name}`)));
        lSel.value = j.left_rel && j.left ? `${j.left_rel}.${j.left}` : "";
        lSel.onchange = () => { const [r, ...c] = lSel.value.split("."); j.left_rel = r; j.left = c.join("."); if (!j.right && rightCols.some((x) => x.name === j.left)) { j.right = j.left; rSel.value = j.right; } refreshSql(); };
        const rSel = h("select", { class: "select" }, h("option", { value: "" }, "key…"), rightCols.map((c) => h("option", { value: c.name }, c.name)));
        rSel.value = j.right || ""; rSel.onchange = () => { j.right = rSel.value; refreshSql(); };
        joinsHolder.appendChild(h("div", { class: "wz-row" },
          h("span", { class: "chip xs primary" }, alias), relSelect(j.rel, (r) => { j.rel = r; j.right = ""; renderAll(); }, "Table to join…"), typeSel,
          h("span", { class: "caption" }, "where"), lSel, h("span", null, "="), rSel,
          h("button", { class: "btn ghost icon sm", "aria-label": "Remove join", onclick: () => { spec.joins.splice(i, 1); spec.columns = spec.columns.filter((c) => c.rel === "base"); renderAll(); } }, icon("delete", 14))));
      }
      joinsHolder.appendChild(h("button", { class: "btn ghost sm", disabled: !spec.from || null, onclick: () => { spec.joins.push({ rel: null, type: "left", left_rel: "base", left: "", right: "" }); renderJoins(); } }, icon("add", 14), "Join another table"));
    }
    async function allColumns(uptoJoins) {
      const out = [];
      const rels = [["base", spec.from], ...spec.joins.slice(0, uptoJoins === undefined ? spec.joins.length : uptoJoins).map((j, i) => [`j${i + 1}`, j.rel])];
      for (const [alias, rel] of rels) {
        if (!rel) continue;
        const r = await columnsOf(rel);
        for (const c of r.columns || []) out.push({ rel: alias, name: c.name, type: c.type });
      }
      return out;
    }

    // 4. columns (or group-by columns when summarising)
    const summarise = h("input", { type: "checkbox" });
    const colsHolder = h("div");
    const colsTitle = h("span", null, "Columns");
    async function renderColumns() {
      colsTitle.textContent = summarise.checked ? "Group by" : "Columns";
      if (!spec.from) { colsHolder.replaceChildren(h("p", { class: "caption" }, "Pick where to start from first.")); return; }
      const cols = await allColumns();
      const warn = [];
      for (const [alias, rel] of [["base", spec.from], ...spec.joins.map((j, i) => [`j${i + 1}`, j.rel])]) {
        if (!rel) continue;
        const r = await columnsOf(rel);
        if (!(r.columns || []).length) warn.push(h("div", { class: "alert warning", style: { marginBottom: "8px" } }, icon("warning", 18),
          h("div", { style: { flex: 1 } }, h("b", null, relLabel(rel)), ` has no known columns${r.error ? " (" + r.error + ")" : ""}. Build it first so its columns can be listed.`,
            rel.kind === "ref" ? h("div", { style: { marginTop: "6px" } }, h("button", { class: "btn secondary sm", onclick: async () => { await runDbt({ action: "build", select: rel.model }); colCache.delete(relKey(rel)); renderAll(); } }, icon("play", 14), `Build ${rel.model}`)) : null)));
      }
      const list = summarise.checked ? spec.group_by : spec.columns;
      const find = (c) => list.find((x) => x.rel === c.rel && x.column === c.name);
      const items = cols.map((c) => {
        const cur = find(c);
        const cb = h("input", { type: "checkbox", checked: !!cur || null, "aria-label": `${c.rel} ${c.name}` });
        const rn = h("input", { class: "input mono", placeholder: "rename", value: cur && cur.alias ? cur.alias : "", style: { display: cur ? "" : "none" } });
        cb.onchange = () => {
          if (cb.checked) list.push({ rel: c.rel, column: c.name, alias: "" }); else list.splice(list.indexOf(find(c)), 1);
          rn.style.display = cb.checked ? "" : "none"; refreshSql();
        };
        rn.oninput = () => { const x = find(c); if (x) { x.alias = rn.value.trim(); refreshSql(); } };
        return h("label", { class: "wz-col", title: c.type || "" }, cb, h("span", { class: "nm" }, c.name, c.rel !== "base" ? h("span", { class: "rel" }, ` ${c.rel}`) : null), rn);
      });
      colsHolder.replaceChildren(...warn,
        h("div", { class: "row", style: { marginBottom: "6px" } },
          h("button", { class: "btn ghost sm", onclick: () => { list.splice(0, list.length, ...cols.map((c) => ({ rel: c.rel, column: c.name, alias: "" }))); renderColumns(); refreshSql(); } }, "Select all"),
          h("button", { class: "btn ghost sm", onclick: () => { list.splice(0, list.length); renderColumns(); refreshSql(); } }, "Clear"),
          h("span", { class: "caption" }, summarise.checked ? "One output row per combination of these columns." : "Nothing ticked = keep every column.")),
        h("div", { class: "wz-cols" }, items));
    }

    // 5. filters
    const filtersHolder = h("div");
    const OPS = [["=", "is"], ["!=", "is not"], [">", ">"], [">=", "≥"], ["<", "<"], ["<=", "≤"], ["in", "is one of"], ["not in", "is not one of"], ["like", "matches (% wildcard)"], ["is null", "is empty"], ["is not null", "is not empty"]];
    async function renderFilters() {
      const cols = await allColumns();
      filtersHolder.replaceChildren(...spec.filters.map((f, i) => {
        const cs = h("select", { class: "select" }, h("option", { value: "" }, "column…"), cols.map((c) => h("option", { value: `${c.rel}.${c.name}` }, `${c.rel === "base" ? "" : c.rel + "."}${c.name}`)));
        cs.value = f.column ? `${f.rel}.${f.column}` : "";
        cs.onchange = () => { const [r, ...c] = cs.value.split("."); f.rel = r; f.column = c.join("."); refreshSql(); };
        const os = h("select", { class: "select" }, OPS.map(([v, l]) => h("option", { value: v }, l)));
        os.value = f.op;
        const val = h("input", { class: "input grow", placeholder: "value (comma-separated for lists)", value: f.value || "" });
        const syncV = () => { val.style.display = f.op.includes("null") ? "none" : ""; };
        os.onchange = () => { f.op = os.value; syncV(); refreshSql(); };
        val.oninput = () => { f.value = val.value; refreshSql(); };
        syncV();
        return h("div", { class: "wz-row" }, cs, os, val, h("button", { class: "btn ghost icon sm", "aria-label": "Remove filter", onclick: () => { spec.filters.splice(i, 1); renderFilters(); refreshSql(); } }, icon("delete", 14)));
      }), h("button", { class: "btn ghost sm", disabled: !spec.from || null, onclick: () => { spec.filters.push({ rel: "base", column: "", op: "=", value: "" }); renderFilters(); } }, icon("add", 14), "Add filter"));
    }

    // 6. aggregates
    const aggHolder = h("div");
    const FNS = [["sum", "Total (sum)"], ["avg", "Average"], ["count", "Count rows"], ["count_distinct", "Count distinct"], ["min", "Minimum"], ["max", "Maximum"]];
    async function renderAggs() {
      if (!summarise.checked) { aggHolder.replaceChildren(h("p", { class: "caption" }, "Switch on to total, average or count by the group-by columns (typical for marts).")); return; }
      const cols = await allColumns();
      aggHolder.replaceChildren(...spec.aggregates.map((a, i) => {
        const fs = h("select", { class: "select" }, FNS.map(([v, l]) => h("option", { value: v }, l)));
        fs.value = a.fn;
        const cs = h("select", { class: "select" }, h("option", { value: "" }, a.fn === "count" ? "all rows" : "column…"), cols.map((c) => h("option", { value: `${c.rel}.${c.name}` }, `${c.rel === "base" ? "" : c.rel + "."}${c.name}`)));
        cs.value = a.column ? `${a.rel}.${a.column}` : "";
        const al = h("input", { class: "input mono", placeholder: "output name", value: a.alias || "" });
        fs.onchange = () => { a.fn = fs.value; renderAggs(); refreshSql(); };
        cs.onchange = () => { const [r, ...c] = cs.value.split("."); a.rel = r; a.column = c.join("."); if (!a.alias && a.column) { a.alias = `${a.fn === "sum" ? "total" : a.fn}_${a.column}`; al.value = a.alias; } refreshSql(); };
        al.oninput = () => { a.alias = al.value.trim(); refreshSql(); };
        return h("div", { class: "wz-row" }, fs, h("span", { class: "caption" }, "of"), cs, h("span", { class: "caption" }, "as"), al,
          h("button", { class: "btn ghost icon sm", "aria-label": "Remove measure", onclick: () => { spec.aggregates.splice(i, 1); renderAggs(); refreshSql(); } }, icon("delete", 14)));
      }), h("button", { class: "btn ghost sm", onclick: () => { spec.aggregates.push({ fn: "sum", rel: "base", column: "", alias: "" }); renderAggs(); } }, icon("add", 14), "Add measure"));
    }
    summarise.onchange = () => { if (summarise.checked && !spec.aggregates.length) spec.aggregates.push({ fn: "count", rel: "base", column: "", alias: "row_count" }); renderColumns(); renderAggs(); refreshSql(); };

    // SQL preview
    const sqlHolder = h("div", null, h("p", { class: "caption" }, "The SQL appears here as you go."));
    const dataHolder = h("div", { style: { marginTop: "10px" } });
    function cleanSpec() {
      const s = JSON.parse(JSON.stringify(spec));
      s.joins = s.joins.filter((j) => j.rel).map((j) => ({ ...j.rel, type: j.type, left_rel: j.left_rel || "base", left: j.left, right: j.right }));
      s.filters = s.filters.filter((f) => f.column);
      s.aggregates = s.aggregates.filter((a) => a.fn === "count" || a.column);
      if (!summarise.checked) { s.group_by = []; s.aggregates = []; } else s.columns = [];
      s.materialized = s.materialized || null;
      s.name = s.name || "new_model";
      return s;
    }
    let lastSql = "";
    const refreshSql = BSP.debounce(async () => {
      if (!spec.from) return;
      try {
        const r = await api(A("/wizard/sql"), { body: { spec: cleanSpec() } });
        lastSql = r.sql;
        sqlHolder.replaceChildren(sqlBlock(r.sql));
      } catch (e) { sqlHolder.replaceChildren(h("div", { class: "alert warning" }, icon("warning", 18), h("div", null, e.message))); lastSql = ""; }
    }, 250);

    async function renderAll() { renderFrom(); await renderJoins(); await renderColumns(); await renderFilters(); await renderAggs(); refreshSql(); }

    const previewBtn = h("button", { class: "btn secondary", onclick: async () => {
      if (!lastSql) return BSP.toast("Finish the steps above first", "info");
      const sql = lastSql.split("\n").filter((l) => !/^\{\{\s*config\(/.test(l)).join("\n");
      await previewInto(dataHolder, { action: "show", inline_sql: sql, limit: 100 });
    } }, icon("visibility", 16), "Preview data");
    const createBtn = h("button", { class: "btn primary", onclick: async () => {
      checkName();
      if (!spec.from) return BSP.toast("Pick where the model starts from", "error");
      if (!spec.name || nameErr.textContent) return BSP.toast(nameErr.textContent || "Give the model a name", "error");
      try {
        const r = await BSP.busy(createBtn, () => api(A("/wizard"), { body: { spec: cleanSpec() } }));
        BSP.toast(`Created ${r.path}`, "success");
        d.close();
        state.graph && (state.graph.stale = true);
        go("model", r.name);
        parse(false);
      } catch (e) { BSP.toast(e.message, "error"); }
    } }, icon("check", 16), "Create model");

    const sec = (n, title, content, extra) => h("div", { class: "wz-sec" }, h("h4", null, h("span", { class: "n" }, String(n)), title, extra ? h("span", { class: "opt" }, extra) : null), content);
    const d = BSP.drawer({
      title: "New model", subtitle: "Point and click — dbt Studio writes the SQL (with ref() and source()) and the YAML.", wide: true,
      body: h("div", null,
        sec(1, "What are you building?", h("div", null, layerPick, h("div", { class: "wz-grid" },
          h("div", { class: "field" }, h("label", null, "Model name"), nameIn, nameErr),
          h("div", { class: "field" }, h("label", null, "Description"), descIn),
          h("div", { class: "field" }, h("label", null, "Materialization"), matSel)))),
        sec(2, "Start from", fromHolder),
        sec(3, "Join other tables", joinsHolder, "optional"),
        sec(4, colsTitle, colsHolder),
        sec(5, "Keep only rows where…", filtersHolder, "optional"),
        sec(6, "Summarise", aggHolder, h("label", { class: "switch" }, summarise, h("span", { class: "track" }), "Group & aggregate")),
        sec(7, "Generated SQL", h("div", null, sqlHolder, dataHolder))),
      foot: [h("span", { class: "caption", style: { marginRight: "auto" } }, "Saved as models/<layer>/<name>.sql — edit it later like any model."), previewBtn, createBtn],
    });
    d.el.classList.add("dbt-wide");
    suggestName();
    renderAll();
  }

  // ═══════════════════════════ ADD SOURCE ═══════════════════════════
  async function addSourceDialog() {
    const bodyEl = h("div", null, WB.skeletonRows(4));
    const ok = h("button", { class: "btn primary", disabled: true }, icon("check", 16), "Add to project");
    const cancel = h("button", { class: "btn ghost" }, "Cancel");
    const m = BSP.modal({ title: "Add source tables", wide: true, body: bodyEl, foot: [cancel, ok] });
    cancel.onclick = () => m.close();
    let schemas;
    try { schemas = (await api(`${BASE}/api/warehouse/tables`)).schemas; }
    catch (e) { bodyEl.replaceChildren(errBox("Could not list warehouse tables", e.message)); return; }
    const declared = new Set(sourcesOnly().map((n) => `${n.schema}.${n.name}`));
    const managed = new Set(modelsOnly().map((n) => n.schema));
    const names = Object.keys(schemas).filter((s) => !managed.has(s) && !["staging", "intermediate", "marts", "snapshots"].includes(s));
    if (!names.length) { bodyEl.replaceChildren(WB.empty({ ic: "database", title: "No tables in the warehouse yet", body: "Upload a file on the Import & Export page (it lands in the landing schema), or load the sample data." })); return; }
    const schemaSel = h("select", { class: "select" }, names.map((s) => h("option", { value: s }, `${s} (${schemas[s].length})`)));
    schemaSel.value = names.includes("landing") ? "landing" : names[0];
    const srcName = h("input", { class: "input mono" });
    const srcDesc = h("input", { class: "input", placeholder: "Where this data comes from" });
    const list = h("div", { class: "wz-cols", style: { maxHeight: "320px" } });
    const picked = new Set();
    const existingSrc = () => sourcesOnly().find((n) => n.schema === schemaSel.value);
    function renderList() {
      picked.clear();
      const ex = existingSrc();
      srcName.value = ex ? ex.source_name : schemaSel.value;
      list.replaceChildren(...schemas[schemaSel.value].map((t) => {
        const has = declared.has(`${schemaSel.value}.${t.name}`);
        const cb = h("input", { type: "checkbox", disabled: has || null, checked: has || null });
        cb.onchange = () => { cb.checked ? picked.add(t.name) : picked.delete(t.name); ok.disabled = !picked.size; };
        return h("label", { class: "wz-col", title: has ? "Already a source" : t.type }, cb, h("span", { class: "nm" }, t.name), h("span", { class: "rel" }, has ? "added" : t.type));
      }));
      ok.disabled = true;
    }
    schemaSel.onchange = renderList;
    renderList();
    bodyEl.replaceChildren(
      h("p", { class: "muted", style: { marginBottom: "12px" } }, "Declare warehouse tables as dbt sources so models can read them with ", h("code", null, "{{ source() }}"), " and they appear in the lineage. Uploads from Import & Export land in the ", h("code", null, "landing"), " schema."),
      h("div", { class: "wz-grid" },
        h("div", { class: "field" }, h("label", null, "Warehouse schema"), schemaSel),
        h("div", { class: "field" }, h("label", null, "Source name"), srcName, h("div", { class: "hint" }, "Used in source('<name>', '<table>').")),
        h("div", { class: "field" }, h("label", null, "Description"), srcDesc)),
      h("div", { class: "label", style: { marginBottom: "6px" } }, "Tables"), list);
    ok.onclick = async () => {
      const name = srcName.value.trim();
      if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) return BSP.toast("Source name: letters, digits and _", "error");
      try {
        const r = await BSP.busy(ok, () => api(A("/sources"), { body: { source: name, schema: schemaSel.value, tables: [...picked], description: srcDesc.value } }));
        BSP.toast(`Added ${r.added.length} table${r.added.length === 1 ? "" : "s"} to source ${r.source} (${r.file})`, "success");
        m.close();
        await parse(false);
      } catch (e) { BSP.toast(e.message, "error"); }
    };
  }

  // ── boot ───────────────────────────────────────────────────────────
  WB.shortcut("n", () => wizard());
  (async function boot() {
    try { await loadProjects(); }
    catch (e) { body.replaceChildren(errBox("Could not load dbt projects", e.message)); return; }
    const r = readHash();
    state.pid = r.pid && state.projects.some((p) => p.id === r.pid) ? r.pid : (state.projects[0] || {}).id || null;
    if (state.pid && !location.hash) history.replaceState(null, "", `#/${encodeURIComponent(state.pid)}/lineage`);
    WB.resume(BASE, ["dbt"], () => {});
    route();
  })();
})();
