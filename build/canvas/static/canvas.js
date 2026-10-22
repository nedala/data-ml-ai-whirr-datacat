/* DataPlatform Integration Canvas — vanilla JS on window.BSP (no CDN, no build step).
   Views: #/ (pipelines · connections · runs)  ·  #/p/<id> (editor)  ·  #/new  */
(function () {
  "use strict";
  const { h, icon, toast, api: mkApi, busy, confirm, prompt, drawer, modal, fmtAgo, fmtTime, debounce } = window.BSP;
  const BASE = location.pathname.replace(/[^/]*$/, "");
  const api = mkApi(BASE);
  const view = document.getElementById("view");
  const crumbs = document.getElementById("crumbs");
  const topActions = document.getElementById("top-actions");
  let PALETTE = null, TYPES = {}, CONN_TYPES = {}, CONNECTIONS = [], GCP = {};
  // identities the signed-in user may act as + each consumer's assigned default
  // (Studio → Cloud identities; names, e-mails and projects only — never a key or token)
  let MYIDS = { identities: [], defaults: {} };

  async function boot() {
    const p = await api("api/palette");
    PALETTE = p.nodes; CONN_TYPES = p.connection_types; GCP = p.gcp || {};
    try { MYIDS = await mkApi("")("/studio/api/gcp/my-identities"); } catch (_) { /* vault locked / not signed in: defaults only */ }
    MYIDS.identities = MYIDS.identities || []; MYIDS.defaults = MYIDS.defaults || {};
    PALETTE.forEach((t) => (TYPES[t.type] = t));
    window.addEventListener("hashchange", route);
    route();
  }

  function setCrumbs(...parts) {
    crumbs.replaceChildren(...parts.flatMap((p, i) => [i ? h("span", {}, "/") : null,
      typeof p === "string" ? h("span", { class: "cur" }, p) : p]).filter(Boolean));
  }

  function route() {
    if (editor && editor.dirty && !location.hash.startsWith("#/p/" + editor.doc.id)) {
      if (!window.confirm("Discard unsaved changes?")) { history.back(); return; }
    }
    editor = null;
    topActions.replaceChildren();
    const m = location.hash.match(/^#\/p\/([^/?]+)/);
    if (m) return openEditor(decodeURIComponent(m[1]));
    if (location.hash === "#/new") return openEditor(null);
    return home(new URLSearchParams(location.hash.split("?")[1] || "").get("tab") || "pipelines");
  }

  const catOf = (t) => (TYPES[t] || {}).category || "transform";
  const nodeIcon = (t) => h("span", { class: "ic " + catOf(t) }, icon((TYPES[t] || {}).icon || "apps", 15));

  function statusBadge(st) {
    if (!st) return h("span", { class: "badge neutral" }, "never run");
    const cls = { success: "ok", failed: "error", running: "starting" }[st] || "neutral";
    return h("span", { class: "badge " + cls }, h("span", { class: "dot" }), st);
  }

  // ── home ───────────────────────────────────────────────────────────────
  async function home(tab) {
    setCrumbs("Pipelines");
    const pane = h("div");
    const tabs = h("div", { class: "tabs" }, ...[["pipelines", "Pipelines", "stacks"], ["connections", "Connections", "link"], ["runs", "Run history", "history"]]
      .map(([id, label, ic]) => h("button", { class: tab === id ? "active" : "", onclick: () => (location.hash = "#/?tab=" + id) }, icon(ic, 14), label)));
    view.replaceChildren(h("div", { class: "cv-home" },
      h("div", { class: "bsp-page-head" },
        h("div", {}, h("div", { class: "eyebrow" }, "Data & Pipelines"), h("h1", { style: { margin: "4px 0 0" } }, "Integration canvas"),
          h("div", { class: "lede" }, "Connect SharePoint, Outlook, BigQuery, Atlan, Power BI, databases, object storage and any REST API to the warehouse, BigQuery or the lake — no code. Pipelines are YAML in the data folder and load with dlt.")),
        h("div", { class: "row" },
          h("button", { class: "btn secondary", onclick: importYaml }, icon("upload", 16), "Import YAML"),
          h("button", { class: "btn primary", onclick: () => (location.hash = "#/new") }, icon("add", 16), "New pipeline"))),
      tabs, pane));
    if (tab === "connections") return connectionsTab(pane);
    if (tab === "runs") return runsTab(pane);
    const d = await api("api/pipelines");
    if (!d.pipelines.length) {
      pane.append(h("div", { class: "card empty" }, "No pipelines yet. ",
        h("button", { class: "btn secondary sm", onclick: async () => { await api("api/examples/seed", { method: "POST" }); route(); } }, "Add the examples")));
      return;
    }
    pane.append(h("div", { class: "grid auto" }, ...d.pipelines.map(pipelineCard)));
  }

  function pipelineCard(p) {
    const flow = [...p.sources.map((t) => nodeIcon(t)), h("span", { class: "muted" }, icon("arrow-right", 14)), ...p.destinations.map((t) => nodeIcon(t))];
    const setup = p.needs_setup || [];
    const setupText = setup.map((m) => m.replace("connection:", "connection ")).join(", ");
    const runBtn = setup.length
      ? h("button", { class: "btn ghost sm", disabled: true, title: `Add ${setupText} in Studio → Credentials → Connection secrets` }, icon("play", 14), "Run")
      : h("button", { class: "btn secondary sm", onclick: (e) => { e.stopPropagation(); busy(runBtn, () => runPipeline(p.id).then(() => route())); } }, icon("play", 14), "Run");
    return h("div", { class: "card pl-card", onclick: () => (location.hash = "#/p/" + encodeURIComponent(p.id)) },
      h("div", { class: "row between" }, h("div", { class: "pl-flow" }, ...flow), p.example ? h("span", { class: "chip primary" }, "example") : null),
      h("div", {}, h("div", { style: { fontWeight: 600, fontSize: "15px" } }, p.name), h("div", { class: "caption", style: { marginTop: "4px" } }, p.description || "")),
      h("div", { class: "pl-meta" },
        setup.length ? h("a", { class: "chip warn", href: "/studio/#credentials", target: "_blank", rel: "noopener",
          title: `Missing: ${setupText}. Add in Studio → Credentials → Connection secrets`, onclick: (e) => e.stopPropagation() }, icon("warning", 12), "Needs setup")
          : p.running ? statusBadge("running") : statusBadge(p.last_run && p.last_run.status),
        p.last_run ? h("span", { class: "caption" }, fmtAgo(p.last_run.started_at)) : null,
        p.schedule && p.schedule.enabled ? h("span", { class: "chip outline", title: "next " + fmtTime(p.next_run) }, icon("clock", 12), p.schedule.cron) : null,
        h("span", { style: { marginLeft: "auto" } }, runBtn)));
  }

  async function runPipeline(id) {
    try { const r = await api(`api/pipelines/${encodeURIComponent(id)}/run`, { method: "POST" }); toast(`Run ${r.run.run_id} started`, "success"); return r.run; }
    catch (e) { toast(e.message, "error"); throw e; }
  }

  async function importYaml() {
    const ta = h("textarea", { class: "textarea mono", style: { minHeight: "320px" }, placeholder: "id: my-pipeline\nname: …\nnodes: …" });
    const m = modal({ title: "Import pipeline YAML", wide: true, body: h("div", {}, ta, h("div", { class: "caption", style: { marginTop: "6px" } }, "Exported canvas YAML (Export button in the editor) or hand-written.")),
      foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Cancel"), h("button", { class: "btn primary", onclick: async () => {
        try { const r = await api("api/import", { body: { yaml: ta.value } }); m.close(); location.hash = "#/p/" + r.pipeline.id; } catch (e) { toast(e.message, "error"); }
      } }, "Import")] });
  }

  async function runsTab(pane) {
    const d = await api("api/runs?limit=100");
    const t = BSP.table({ columns: [
      { key: "status", label: "Status", render: (r) => statusBadge(r.status) },
      { key: "pipeline_name", label: "Pipeline", sort: true, render: (r) => h("a", { href: "#/p/" + encodeURIComponent(r.pipeline_id) }, r.pipeline_name || r.pipeline_id) },
      { key: "trigger", label: "Trigger" },
      { key: "started_at", label: "Started", sort: true, render: (r) => fmtTime(r.started_at) },
      { key: "rows", label: "Rows loaded", num: true, render: (r) => (r.loaded || []).reduce((a, x) => a + x.rows, 0).toLocaleString() },
      { key: "error", label: "Detail", render: (r) => h("span", { class: "caption" }, (r.error || (r.loaded || []).map((x) => x.target).join(", ")).slice(0, 120)) },
    ], rows: d.runs, empty: h("div", { class: "empty" }, "No runs yet.") });
    pane.append(h("div", { class: "card" }, t.el));
  }

  // ── connections ────────────────────────────────────────────────────────
  async function loadConnections() { const d = await api("api/connections"); CONNECTIONS = d.connections; return CONNECTIONS; }

  async function connectionsTab(pane) {
    await loadConnections();
    const list = h("div", { class: "stack lg" });
    const draw = () => list.replaceChildren(...CONNECTIONS.map((c) => {
      const ty = CONN_TYPES[c.type] || {};
      const res = h("div", { class: "caption" });
      const test = h("button", { class: "btn secondary sm" }, icon("play", 14), "Test");
      test.onclick = () => busy(test, async () => { const r = await api("api/connections/test", { body: { name: c.name } }); res.replaceChildren(icon(r.ok ? "tick-circle" : "error", 13), " ", r.detail); res.classList.add("test-res"); res.style.color = r.ok ? "var(--bsp-text-success, #15803d)" : "var(--bsp-text-error)"; });
      return h("div", { class: "card conn-row" }, h("div", { class: "row" }, h("span", { class: "ic" }, icon(ty.icon || "database", 18)),
        h("div", { style: { flex: 1, minWidth: 0 } }, h("div", { style: { fontWeight: 600 } }, c.name, " ", h("span", { class: "chip outline" }, ty.label || c.type), c.builtin ? h("span", { class: "chip primary", style: { marginLeft: "6px" } }, "built-in") : null),
          h("div", { class: "caption" }, c.description || summarizeParams(c)), gcpIdentityLine(c), res),
        test, ty.kind === "storage" ? h("button", { class: "btn ghost sm", onclick: () => storageBrowser(c.name, "", () => {}) }, icon("folder-open", 14), "Browse") : null,
        c.builtin ? null : h("button", { class: "btn ghost sm", onclick: () => connectionForm(c, () => connectionsTab(clear(pane))) }, icon("edit", 14), "Edit"),
        c.builtin ? null : h("button", { class: "btn ghost icon sm", title: "Delete", onclick: async () => { if (await confirm({ title: `Delete connection ${c.name}?`, danger: true, okText: "Delete" })) { await api("api/connections/" + encodeURIComponent(c.name), { method: "DELETE" }); connectionsTab(clear(pane)); } } }, icon("delete", 14))));
    }));
    draw();
    pane.append(h("div", { class: "row between", style: { marginBottom: "12px" } },
      h("div", { class: "caption" }, "Reusable connections: name, type and non-secret settings. Secrets are vault references (vault:NAME) to Studio → Credentials — never stored in pipelines."),
      h("button", { class: "btn primary", onclick: () => connectionForm(null, () => connectionsTab(clear(pane))) }, icon("add", 16), "New connection")), list);
  }
  const clear = (el) => { el.replaceChildren(); return el; };
  const summarizeParams = (c) => Object.entries(c.params || {}).filter(([k]) => !k.endsWith("_ref")).map(([k, v]) => `${k}=${v}`).join(" · ");

  // "Acting as <principal> on <project>" — which Google principal a GCS / BigQuery connection uses
  const GCP_CONSUMER = { gcs: "gcs", bigquery: "bigquery_read" };
  const myId = (name) => MYIDS.identities.find((i) => i.name === name || i.id === name);
  function actingText(st) {
    const key = GCP_CONSUMER[st.type];
    if (!key) return null;
    const p = st.params || {};
    if (p.identity) {
      const i = myId(p.identity);
      const proj = p.project || (i && i.default_project) || "";
      return { who: (i && i.principal) || p.identity, proj, named: true, shared: !!i };
    }
    const d = MYIDS.defaults[key] || {};
    const proj = p.project || d.project || "";
    return { who: d.principal || d.name || (GCP.assigned || {})[key] || "GCP_SA_JSON / default credentials", proj, named: false, shared: true };
  }
  function gcpIdentityLine(st) {
    const a = actingText(st);
    if (!a) return null;
    return h("div", { class: "caption", style: { marginTop: "4px" } }, icon("shield", 12), " Acting as ", h("b", null, a.who),
      a.proj ? [" on ", h("code", null, a.proj)] : "", a.named ? "" : h("span", { class: "muted" }, " · assigned default"),
      a.named && !a.shared ? h("span", { style: { color: "var(--bsp-text-error)" } }, " · not shared with you — runs you start will be refused") : "",
      st.identity_user ? h("div", { class: "muted" }, `Scheduled runs act as ${st.identity_user} (who saved it) — Studio re-checks their access on every run.`) : "");
  }
  // the identity select lists only identities shared with the signed-in user (plus the saved one)
  function identityField(f, st) {
    const cur = (st.params || {}).identity || "";
    const opts = [f.options[0], ...MYIDS.identities.map((i) => [i.name, `${i.name}${i.principal ? ` (${i.principal})` : ""}`])];
    if (cur && !opts.some(([v]) => v === cur)) opts.push([cur, `${cur} (not shared with you)`]);
    return Object.assign({}, f, { options: opts });
  }

  function connectionForm(existing, done) {
    const st = existing ? JSON.parse(JSON.stringify(existing)) : { name: "", type: "postgres", description: "", params: {} };
    const body = h("div");
    const result = h("div", { class: "caption", style: { marginTop: "8px" } });
    const draw = () => {
      const ty = CONN_TYPES[st.type];
      body.replaceChildren(
        field("Name", h("input", { class: "input", value: st.name, disabled: !!existing || null, placeholder: "care-mssql", oninput: (e) => (st.name = e.target.value) })),
        field("Type", select(Object.keys(CONN_TYPES).map((k) => [k, CONN_TYPES[k].label]), st.type, (v) => { st.type = v; st.params = {}; draw(); }, !!existing)),
        field("Description", h("input", { class: "input", value: st.description || "", oninput: (e) => (st.description = e.target.value) })),
        h("div", { class: "divider", style: { margin: "8px 0 14px" } }),
        ...ty.fields.filter((f) => visible(f, st.params, ty.fields)).map((f) => renderField(f.identity ? identityField(f, st) : f, st.params, () => draw(), { conn: true })),
        gcpIdentityLine(st), result);
    };
    draw();
    const test = h("button", { class: "btn secondary" }, icon("play", 16), "Test connection");
    test.onclick = () => busy(test, async () => { const r = await api("api/connections/test", { body: st }); result.replaceChildren(icon(r.ok ? "tick-circle" : "error", 13), " ", r.detail); result.classList.add("test-res"); result.style.color = r.ok ? "var(--bsp-text-success, #15803d)" : "var(--bsp-text-error)"; });
    const d = drawer({ title: existing ? "Edit connection" : "New connection", body, foot: [test, h("span", { style: { flex: 1 } }),
      h("button", { class: "btn ghost", onclick: () => d.close() }, "Cancel"),
      h("button", { class: "btn primary", onclick: async () => { try { await api("api/connections", { body: st }); d.close(); toast("Connection saved", "success"); await loadConnections(); done && done(); } catch (e) { toast(e.message, "error"); } } }, "Save")] });
  }

  // ── form helpers ───────────────────────────────────────────────────────
  function field(label, control, hint) {
    return h("div", { class: "field" }, h("label", {}, label), control, hint ? h("div", { class: "hint" }, hint) : null);
  }
  function select(options, value, onchange, disabled) {
    const s = h("select", { class: "select", disabled: disabled || null, onchange: (e) => onchange(e.target.value) },
      ...options.map((o) => { const [v, l] = Array.isArray(o) ? o : [o, o]; return h("option", { value: v, selected: String(v) === String(value) || null }, l); }));
    return s;
  }
  function visible(f, cfg, fields) {
    if (f.show_if_set) return !!cfg[f.show_if_set];
    if (!f.show_if) return true;
    return Object.entries(f.show_if).every(([k, v]) => {
      const def = (fields.find((x) => x.key === k) || {}).default;
      const cur = cfg[k] === undefined ? def : cfg[k];
      return Array.isArray(v) ? v.includes(cur) : cur === v;
    });
  }
  const listToText = (v) => (Array.isArray(v) ? v.join(", ") : v || "");
  const textToList = (s) => s.split(/[,\n]/).map((x) => x.trim()).filter(Boolean);
  const mapToText = (m) => (m && typeof m === "object" ? Object.entries(m).map(([k, v]) => `${k} → ${v}`).join("\n") : m || "");
  function textToMap(s) {
    const out = {};
    s.split("\n").forEach((line) => { const m = line.match(/^\s*(.+?)\s*(?:→|->|:|=)\s*(.+?)\s*$/); if (m) out[m[1]] = m[2]; });
    return out;
  }

  function renderField(f, cfg, redraw, opts) {
    opts = opts || {};
    const v = cfg[f.key] === undefined ? f.default : cfg[f.key];
    const set = (val, re) => { if (val === "" || val === undefined) delete cfg[f.key]; else cfg[f.key] = val; opts.onchange && opts.onchange(); if (re) redraw(); };
    const label = f.label + (f.required ? " *" : "");
    let control;
    switch (f.type) {
      case "number": control = h("input", { class: "input", type: "number", value: v ?? "", oninput: (e) => set(e.target.value === "" ? "" : Number(e.target.value)) }); break;
      case "bool": control = h("label", { class: "switch" }, h("input", { type: "checkbox", checked: !!v || null, onchange: (e) => set(e.target.checked, true) }), h("span", { class: "track" }), h("span", {}, v ? "On" : "Off")); break;
      case "select": control = select(f.options, v, (x) => set(x, true)); break;
      case "code": control = h("textarea", { class: "textarea mono", placeholder: f.placeholder || "", oninput: (e) => set(e.target.value) }, v || ""); break;
      case "json": {
        const err = h("div", { class: "error" });
        control = h("div", {}, h("textarea", { class: "textarea mono", placeholder: "{}", oninput: (e) => { try { set(e.target.value.trim() ? JSON.parse(e.target.value) : ""); err.textContent = ""; } catch (x) { err.textContent = "invalid JSON"; } } }, v && typeof v === "object" ? JSON.stringify(v, null, 2) : v || ""), err);
        break;
      }
      case "map": control = h("textarea", { class: "textarea mono", placeholder: f.placeholder || "old → new", oninput: (e) => set(textToMap(e.target.value)) }, mapToText(v)); break;
      case "list": control = h("input", { class: "input", value: listToText(v), placeholder: f.placeholder || "a, b, c", oninput: (e) => set(textToList(e.target.value)) }); break;
      case "columns": {
        const id = "dl-" + Math.random().toString(36).slice(2);
        const cols = (opts.upstreamColumns || []);
        control = h("div", {}, h("input", { class: "input", list: id, value: listToText(v), placeholder: f.placeholder || (cols.length ? cols.slice(0, 3).join(", ") : "col_a, col_b"), oninput: (e) => set(textToList(e.target.value)) }),
          h("datalist", { id }, ...cols.map((c) => h("option", { value: c }))),
          cols.length ? h("div", { class: "row wrap", style: { marginTop: "6px", gap: "4px" } }, ...cols.slice(0, 40).map((c) => h("button", { class: "chip outline", type: "button", onclick: () => { const cur = Array.isArray(cfg[f.key]) ? cfg[f.key] : textToList(cfg[f.key] || ""); if (!cur.includes(c)) { set([...cur, c], true); } } }, c))) : null);
        break;
      }
      case "secret": {
        const bad = v && !String(v).startsWith("vault:");
        control = h("div", {}, h("input", { class: "input mono" + (bad ? " invalid" : ""), value: v || "", placeholder: f.placeholder || "vault:NAME", oninput: (e) => set(e.target.value.trim()) }),
          bad ? h("div", { class: "error" }, "Use a vault reference (vault:NAME), never the secret itself") : null);
        break;
      }
      case "upload": control = uploadControl(v, (name) => set(name, true), opts); break;
      case "connection": {
        const kinds = f.kinds || [];
        const opts2 = CONNECTIONS.filter((c) => { const k = (CONN_TYPES[c.type] || {}).kind; return kinds.includes(k) || kinds.includes(c.type); });
        control = h("div", { class: "row" }, select([["", "— choose —"], ...opts2.map((c) => [c.name, `${c.name} (${(CONN_TYPES[c.type] || {}).label || c.type})`])], v || "", (x) => set(x, true)),
          h("a", { class: "btn ghost sm", href: "#/?tab=connections", target: "_blank", title: "Manage connections" }, icon("settings", 14)));
        break;
      }
      case "db_schema": control = asyncPicker(cfg, f, set, () => api(`api/connections/${enc(cfg.connection || "warehouse")}/schemas`).then((d) => d.schemas.map((s) => ({ value: s, label: s })))); break;
      case "db_table": control = tablePicker(cfg, set); break;
      case "db_columns": control = columnPicker(cfg, set); break;
      case "bucket": control = asyncPicker(cfg, f, set, () => api(`api/connections/${enc(cfg.connection)}/browse`).then((d) => d.entries.map((e) => ({ value: e.name, label: e.name }))), true); break;
      case "storage_glob": control = globControl(cfg, f, v, set); break;
      default: control = h("input", { class: "input", value: v ?? "", placeholder: f.placeholder || "", oninput: (e) => set(e.target.value) });
    }
    return field(label, control, f.hint);
  }
  const enc = encodeURIComponent;

  function asyncPicker(cfg, f, set, loader, allowFree) {
    const v = cfg[f.key] || "";
    const inp = h("input", { class: "input", value: v, placeholder: f.placeholder || "", oninput: (e) => set(e.target.value) });
    const list = h("div", { class: "picker", hidden: true });
    const load = h("button", { class: "btn ghost sm", type: "button", title: "Browse" }, icon("search", 14));
    load.onclick = () => busy(load, async () => {
      if (!cfg.connection && f.type === "bucket") return toast("Choose a connection first", "error");
      try {
        const items = await loader();
        list.hidden = false;
        list.replaceChildren(...items.map((it) => h("div", { class: "opt" + (it.value === cfg[f.key] ? " on" : ""), onclick: () => { inp.value = it.value; set(it.value, true); } }, it.label)));
        if (!items.length) list.replaceChildren(h("div", { class: "opt" }, "Nothing found"));
      } catch (e) { toast(e.message, "error"); }
    });
    return h("div", {}, h("div", { class: "input-group" }, inp, load), list);
  }

  function tablePicker(cfg, set) {
    const inp = h("input", { class: "input", value: cfg.table || "", placeholder: "search tables…", oninput: (e) => { set(e.target.value); refresh(); } });
    const list = h("div", { class: "picker", hidden: true });
    const est = h("div", { class: "caption", style: { marginTop: "6px" } });
    const refresh = debounce(async () => {
      try {
        const d = await api(`api/connections/${enc(cfg.connection || "warehouse")}/tables?schema=${enc(cfg.schema || "")}&q=${enc(inp.value)}`);
        list.hidden = false;
        list.replaceChildren(...d.tables.slice(0, 200).map((t) => h("div", { class: "opt" + (t.name === cfg.table ? " on" : ""), onclick: () => { set(t.name, true); } }, icon(t.kind === "view" ? "document" : "database", 14), t.name, h("span", { class: "ty" }, t.kind))));
        if (!d.tables.length) list.replaceChildren(h("div", { class: "opt" }, "No tables match"));
      } catch (e) { list.hidden = false; list.replaceChildren(h("div", { class: "opt" }, e.message)); }
    }, 250);
    const browse = h("button", { class: "btn ghost sm", type: "button", title: "Browse tables" }, icon("search", 14));
    browse.onclick = refresh;
    const estBtn = h("button", { class: "btn ghost sm", type: "button" }, icon("chart", 14), "Row count");
    estBtn.onclick = () => busy(estBtn, async () => {
      if (!cfg.table) return;
      try { const r = await api(`api/connections/${enc(cfg.connection || "warehouse")}/estimate?schema=${enc(cfg.schema || "")}&table=${enc(cfg.table)}`); est.textContent = `≈ ${Number(r.rows).toLocaleString()} rows (${r.method})`; }
      catch (e) { est.textContent = e.message; }
    });
    return h("div", {}, h("div", { class: "input-group" }, inp, browse), list, h("div", { class: "row", style: { marginTop: "6px" } }, estBtn, est));
  }

  function columnPicker(cfg, set) {
    const box = h("div", { class: "picker", hidden: true });
    const sel = new Set(Array.isArray(cfg.columns) ? cfg.columns : textToList(cfg.columns || ""));
    const btn = h("button", { class: "btn secondary sm", type: "button" }, icon("filter", 14), sel.size ? `${sel.size} selected — edit` : "Choose columns · type mapping");
    btn.onclick = () => busy(btn, async () => {
      if (!cfg.table) return toast("Pick a table first", "error");
      try {
        const d = await api(`api/connections/${enc(cfg.connection || "warehouse")}/columns?schema=${enc(cfg.schema || "")}&table=${enc(cfg.table)}`);
        box.hidden = false;
        box.replaceChildren(h("div", { class: "opt", style: { fontWeight: 600 } }, "Column", h("span", { class: "ty" }, "source type → lands as")),
          ...d.columns.map((c) => h("label", { class: "opt" }, h("input", { type: "checkbox", checked: sel.has(c.name) || null, onchange: (e) => { e.target.checked ? sel.add(c.name) : sel.delete(c.name); set(sel.size ? [...sel] : ""); } }),
            c.name, c.primary_key ? h("span", { class: "chip primary" }, "PK") : null, h("span", { class: "ty" }, `${c.type} → ${c.load_type || ""}`))));
      } catch (e) { toast(e.message, "error"); }
    });
    return h("div", {}, btn, box);
  }

  function globControl(cfg, f, v, set) {
    const inp = h("input", { class: "input mono", value: v || "", placeholder: f.placeholder || "**/*.parquet", oninput: (e) => set(e.target.value) });
    const info = h("div", { class: "caption", style: { marginTop: "6px" } });
    const browse = h("button", { class: "btn ghost sm", type: "button", title: "Browse bucket" }, icon("folder-open", 14));
    browse.onclick = () => { if (!cfg.connection || !cfg.bucket) return toast("Choose a connection and bucket first", "error"); storageBrowser(cfg.connection, cfg.bucket, (path) => { const rel = path.split("/").slice(1).join("/"); inp.value = rel; set(rel, true); }); };
    const check = h("button", { class: "btn ghost sm", type: "button" }, icon("search", 14), "Matches");
    check.onclick = () => busy(check, async () => {
      try { const d = await api(`api/connections/${enc(cfg.connection)}/glob?bucket=${enc(cfg.bucket || "")}&pattern=${enc(inp.value || "**")}`); info.textContent = `${d.count} object(s): ` + d.files.slice(0, 4).map((x) => x.name).join(", ") + (d.count > 4 ? " …" : ""); }
      catch (e) { info.textContent = e.message; }
    });
    return h("div", {}, h("div", { class: "input-group" }, inp, browse), h("div", { class: "row", style: { marginTop: "6px" } }, check, info));
  }

  function storageBrowser(conn, start, onPick) {
    let path = start || "";
    const list = h("div", { class: "browser-list" });
    const where = h("div", { class: "row", style: { marginBottom: "8px", fontFamily: "var(--bsp-font-mono)", fontSize: "12px" } });
    const load = async () => {
      where.replaceChildren(h("button", { class: "btn ghost sm", onclick: () => { path = path.split("/").slice(0, -1).join("/"); load(); }, disabled: !path || null }, icon("arrow-up", 14)), (path || "(buckets)"));
      list.replaceChildren(h("div", { class: "opt" }, "Loading…"));
      try {
        const d = await api(`api/connections/${enc(conn)}/browse?path=${enc(path)}`);
        list.replaceChildren(...d.entries.map((e) => h("div", { class: "opt", onclick: () => { if (e.type === "file") { onPick(e.path); dr.close(); } else { path = e.path; load(); } } },
          icon(e.type === "file" ? "document" : "folder", 14), e.name, h("span", { class: "sz" }, e.type === "file" ? `${BSP.fmtBytes(e.size)} · ${fmtAgo(e.modified)}` : ""))));
        if (!d.entries.length) list.replaceChildren(h("div", { class: "opt" }, "Empty"));
      } catch (e) { list.replaceChildren(h("div", { class: "opt" }, e.message)); }
    };
    const useFolder = h("button", { class: "btn secondary", onclick: () => { onPick(path + "/**"); dr.close(); } }, "Use this folder (**)");
    const dr = drawer({ title: `Browse ${conn}`, body: h("div", {}, where, list), foot: [useFolder, h("button", { class: "btn ghost", onclick: () => dr.close() }, "Close")] });
    load();
  }

  function uploadControl(v, onDone, opts) {
    const status = h("div", { class: "caption", style: { marginTop: "6px" } }, v ? `Current: ${v}` : "");
    const dz = BSP.dropzone({ label: "Drop a CSV / Excel / Parquet / JSON file", multiple: false, onFiles: async (files) => {
      if (!editor.doc.id) { toast("Save the pipeline first (uploads are stored with it)", "error"); return; }
      const fd = new FormData(); fd.append("file", files[0]);
      status.textContent = "Uploading…";
      try { const r = await api(`api/pipelines/${enc(editor.doc.id)}/upload`, { body: fd }); status.textContent = `Uploaded ${r.file} (${BSP.fmtBytes(r.size)})`; onDone(r.file); }
      catch (e) { status.textContent = e.message; }
    } });
    return h("div", {}, dz, status);
  }

  // ── editor ─────────────────────────────────────────────────────────────
  let editor = null;

  async function openEditor(id) {
    await loadConnections();
    let doc, issues = [], state = {};
    if (id) {
      try { const d = await api("api/pipelines/" + enc(id)); doc = d.pipeline; issues = d.issues; state = d.state; }
      catch (e) { toast(e.message, "error"); location.hash = "#/"; return; }
    } else {
      doc = { id: "", name: "New pipeline", description: "", schedule: { cron: "", enabled: false }, nodes: [], edges: [] };
    }
    editor = { doc, issues, state, sel: null, selEdge: null, previews: {}, dirty: false, zoom: 1, pan: { x: 20, y: 20 }, lastRun: null };
    buildEditor();
  }

  function markDirty() { editor.dirty = true; saveBtn && saveBtn.classList.add("primary"); }
  let saveBtn = null;

  function buildEditor() {
    const E = editor;
    const nameInput = h("input", { class: "input", style: { height: "30px", width: "320px", fontWeight: 600 }, value: E.doc.name, oninput: (e) => { E.doc.name = e.target.value; markDirty(); } });
    setCrumbs(h("a", { href: "#/" }, "Pipelines"), nameInput);
    saveBtn = h("button", { class: "btn secondary sm" }, icon("check", 14), "Save");
    saveBtn.onclick = () => busy(saveBtn, save);
    const runBtn = h("button", { class: "btn primary sm" }, icon("play", 14), "Run");
    runBtn.onclick = () => busy(runBtn, async () => { if (E.dirty || !E.doc.id) await save(); if (!E.doc.id) return; const r = await runPipeline(E.doc.id); E.lastRun = r; showTab("log"); followRun(r); });
    topActions.replaceChildren(
      h("button", { class: "btn ghost sm", onclick: validateNow }, icon("tick-circle", 14), "Validate"),
      h("button", { class: "btn ghost sm", onclick: scheduleDialog }, icon("clock", 14), "Schedule"),
      h("button", { class: "btn ghost sm", onclick: () => { if (!E.doc.id) return toast("Save first", "error"); location.href = BASE + `api/pipelines/${enc(E.doc.id)}/export`; } }, icon("download", 14), "Export YAML"),
      h("button", { class: "btn ghost sm", onclick: moreMenu, id: "more-btn" }, icon("more", 14)),
      saveBtn, runBtn);

    // palette
    const pal = h("div", { class: "ed-palette" });
    const search = h("input", { class: "input", placeholder: "Search connectors…", oninput: () => drawPalette(search.value) });
    pal.append(h("div", { class: "search" }, search), h("div", { id: "pal-list" }));
    // canvas
    const world = h("div", { class: "cv-world" });
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "cv-edges"); svg.setAttribute("width", "4000"); svg.setAttribute("height", "3000");
    world.append(svg);
    const canvas = h("div", { class: "ed-canvas", tabindex: "0" }, world,
      h("div", { class: "cv-tools" },
        h("button", { class: "btn icon sm", title: "Zoom in", onclick: () => zoom(1.15) }, icon("add", 14)),
        h("button", { class: "btn icon sm", title: "Zoom out", onclick: () => zoom(1 / 1.15) }, h("span", {}, "−")),
        h("button", { class: "btn sm", title: "Fit", onclick: fit }, "Fit"),
        h("button", { class: "btn sm", title: "Auto-layout", onclick: autoLayout }, "Tidy")),
      h("div", { class: "cv-empty", id: "cv-empty" }, h("div", {}, icon("stacks", 32), h("div", { style: { marginTop: "8px", fontWeight: 600 } }, "Drag a source from the left to start"), h("div", { class: "caption" }, "Then add transforms and a destination; connect ports by dragging."))));
    const insp = h("div", { class: "ed-inspector" });
    const bottom = h("div", { class: "ed-bottom" });
    const ed = h("div", { class: "ed" }, pal, canvas, insp, bottom);
    view.replaceChildren(ed);
    E.els = { world, svg, canvas, insp, bottom, ed, nodes: {} };
    drawPalette("");
    wireCanvas();
    drawNodes();
    drawInspector();
    drawBottom();
    setTimeout(fit, 30);
    if (!E.doc.id && !E.doc.nodes.length) toast("Drag connectors from the palette onto the canvas", "info");
  }

  function drawPalette(q) {
    q = (q || "").toLowerCase();
    const groups = [["source", "Sources"], ["transform", "Transforms"], ["destination", "Destinations"]];
    document.getElementById("pal-list").replaceChildren(...groups.map(([cat, title]) => {
      const items = PALETTE.filter((t) => t.category === cat && (!q || (t.label + " " + t.blurb + " " + t.group).toLowerCase().includes(q)));
      if (!items.length) return null;
      return h("div", { class: "pal-group" }, h("span", { class: "eyebrow" }, title), ...items.map((t) => {
        const el = h("div", { class: "pal-item", draggable: "true", title: t.blurb, ondblclick: () => addNode(t.type, null) }, h("span", { class: "ic " + cat }, icon(t.icon, 14)),
          h("div", { style: { minWidth: 0 } }, h("div", { class: "t" }, t.label), h("div", { class: "b" }, t.blurb)));
        el.addEventListener("dragstart", (e) => { e.dataTransfer.setData("text/bsp-node", t.type); e.dataTransfer.effectAllowed = "copy"; });
        return el;
      }));
    }));
  }

  function newId(type) {
    const base = type.split(".")[1].replace(/[^a-z]/g, "").slice(0, 8) || "node";
    let i = 1; while (editor.doc.nodes.some((n) => n.id === `${base}${i}`)) i++;
    return `${base}${i}`;
  }

  function addNode(type, pos) {
    const t = TYPES[type];
    const cfg = {};
    t.fields.forEach((f) => { if (f.default !== undefined && f.type !== "bool") cfg[f.key] = f.default; });
    const E = editor;
    if (!pos) { const xs = E.doc.nodes.map((n) => n.position.x); pos = { x: (xs.length ? Math.max(...xs) + 260 : 60), y: 140 }; }
    const n = { id: newId(type), type, label: t.label, position: pos, config: cfg };
    E.doc.nodes.push(n);
    // auto-connect from the selected node when it makes sense
    const from = E.doc.nodes.find((x) => x.id === E.sel);
    if (from && TYPES[from.type].category !== "destination" && t.category !== "source") E.doc.edges.push({ from: from.id, to: n.id });
    E.sel = n.id; markDirty(); drawNodes(); drawInspector();
  }

  function screenToWorld(cx, cy) {
    const r = editor.els.canvas.getBoundingClientRect();
    return { x: (cx - r.left - editor.pan.x) / editor.zoom, y: (cy - r.top - editor.pan.y) / editor.zoom };
  }
  function applyTransform() { editor.els.world.style.transform = `translate(${editor.pan.x}px, ${editor.pan.y}px) scale(${editor.zoom})`; }
  function zoom(f) { editor.zoom = Math.min(2, Math.max(0.3, editor.zoom * f)); applyTransform(); }
  function fit() {
    const E = editor; if (!E || !E.doc.nodes.length) { applyTransform(); return; }
    const xs = E.doc.nodes.map((n) => n.position.x), ys = E.doc.nodes.map((n) => n.position.y);
    const w = Math.max(...xs) - Math.min(...xs) + 260, hgt = Math.max(...ys) - Math.min(...ys) + 140;
    const r = E.els.canvas.getBoundingClientRect();
    E.zoom = Math.min(1.2, Math.max(0.3, Math.min(r.width / w, r.height / hgt) * 0.9));
    E.pan = { x: (r.width - w * E.zoom) / 2 - Math.min(...xs) * E.zoom + 10, y: (r.height - hgt * E.zoom) / 2 - Math.min(...ys) * E.zoom + 10 };
    applyTransform();
  }
  function autoLayout() {
    const E = editor; const depth = {};
    const order = topoOrder();
    order.forEach((id) => { const ins = E.doc.edges.filter((e) => e.to === id).map((e) => depth[e.from] + 1); depth[id] = ins.length ? Math.max(...ins) : 0; });
    const rows = {};
    order.forEach((id) => { const d = depth[id]; rows[d] = (rows[d] || 0) + 1; const n = E.doc.nodes.find((x) => x.id === id); n.position = { x: 60 + d * 270, y: 60 + (rows[d] - 1) * 130 }; });
    markDirty(); drawNodes(); fit();
  }
  function topoOrder() {
    const E = editor; const done = new Set(), out = [];
    let guard = 0;
    while (out.length < E.doc.nodes.length && guard++ < 1000) {
      for (const n of E.doc.nodes) { if (done.has(n.id)) continue; if (E.doc.edges.filter((e) => e.to === n.id).every((e) => done.has(e.from))) { done.add(n.id); out.push(n.id); } }
    }
    E.doc.nodes.forEach((n) => { if (!done.has(n.id)) out.push(n.id); });
    return out;
  }

  function wireCanvas() {
    const E = editor, c = E.els.canvas;
    c.addEventListener("dragover", (e) => { if ([...e.dataTransfer.types].includes("text/bsp-node")) { e.preventDefault(); c.classList.add("dragover"); } });
    c.addEventListener("dragleave", () => c.classList.remove("dragover"));
    c.addEventListener("drop", (e) => { e.preventDefault(); c.classList.remove("dragover"); const t = e.dataTransfer.getData("text/bsp-node"); if (t) { const p = screenToWorld(e.clientX, e.clientY); addNode(t, { x: Math.round(p.x - 100), y: Math.round(p.y - 30) }); } });
    c.addEventListener("wheel", (e) => { e.preventDefault(); const before = screenToWorld(e.clientX, e.clientY); zoom(e.deltaY < 0 ? 1.08 : 1 / 1.08); const r = c.getBoundingClientRect(); E.pan.x = e.clientX - r.left - before.x * E.zoom; E.pan.y = e.clientY - r.top - before.y * E.zoom; applyTransform(); }, { passive: false });
    c.addEventListener("mousedown", (e) => {
      if (e.target !== c && e.target !== E.els.world && e.target !== E.els.svg) return;
      E.sel = null; E.selEdge = null; drawNodes(); drawInspector();
      const sx = e.clientX, sy = e.clientY, px = E.pan.x, py = E.pan.y;
      const mv = (ev) => { E.pan.x = px + ev.clientX - sx; E.pan.y = py + ev.clientY - sy; applyTransform(); };
      const up = () => { window.removeEventListener("mousemove", mv); window.removeEventListener("mouseup", up); };
      window.addEventListener("mousemove", mv); window.addEventListener("mouseup", up);
    });
    c.addEventListener("keydown", (e) => {
      if (e.target !== c) return;
      if (e.key === "Delete" || e.key === "Backspace") {
        if (E.selEdge != null) { E.doc.edges.splice(E.selEdge, 1); E.selEdge = null; markDirty(); drawNodes(); }
        else if (E.sel) removeNode(E.sel);
      }
    });
    document.addEventListener("keydown", (e) => { if ((e.ctrlKey || e.metaKey) && e.key === "s" && editor) { e.preventDefault(); save(); } });
    applyTransform();
  }

  function removeNode(id) {
    const E = editor;
    E.doc.nodes = E.doc.nodes.filter((n) => n.id !== id);
    E.doc.edges = E.doc.edges.filter((e) => e.from !== id && e.to !== id);
    if (E.sel === id) E.sel = null;
    markDirty(); drawNodes(); drawInspector();
  }

  function nodeSubtitle(n) {
    const c = n.config || {};
    return c.table ? (c.schema || c.dataset || c.folder || c.bucket ? `${c.schema || c.dataset || c.folder || c.bucket}.${c.table}` : c.table)
      : c.folder ? `${c.folder}/${c.pattern || ""}` : c.mailbox || c.pattern || c.base_url || c.expression || c.column || c.list_name || c.query || (TYPES[n.type] || {}).label || "";
  }

  function drawNodes() {
    const E = editor, world = E.els.world;
    world.querySelectorAll(".cv-node").forEach((x) => x.remove());
    document.getElementById("cv-empty").hidden = E.doc.nodes.length > 0;
    const bad = new Set(E.issues.filter((i) => i.level === "error" && i.node).map((i) => i.node));
    E.els.nodes = {};
    for (const n of E.doc.nodes) {
      const t = TYPES[n.type] || { category: "transform", icon: "apps", label: n.type, inputs: ["in"], outputs: ["out"] };
      const pv = E.previews[n.id];
      const run = E.lastRunRecord && E.lastRunRecord.nodes ? E.lastRunRecord.nodes[n.id] : undefined;
      const el = h("div", { class: "cv-node" + (E.sel === n.id ? " sel" : "") + (bad.has(n.id) ? " err" : ""), style: { left: n.position.x + "px", top: n.position.y + "px" }, dataset: { id: n.id } },
        h("div", { class: "hd" }, h("span", { class: "ic " + t.category }, icon(t.icon, 15)),
          h("div", { style: { minWidth: 0, flex: 1 } }, h("div", { class: "ttl" }, n.label || t.label), h("div", { class: "sub" }, String(nodeSubtitle(n)).slice(0, 60)))),
        h("div", { class: "ft" }, h("span", {}, t.label), pv ? h("span", { class: pv.error ? "bad" : "ok" }, pv.error ? "error" : `${pv.row_count} rows`) : run !== undefined ? h("span", { class: "ok" }, `${run.toLocaleString()} rows`) : h("span")));
      (t.inputs || []).forEach((p) => { const port = h("div", { class: "port in " + (p === "lookup" ? "lookup" : ""), title: p === "lookup" ? "lookup input" : "input", dataset: { port: p } }); el.append(port); if (p === "lookup") el.append(h("span", { class: "port-label" }, "lookup")); });
      (t.outputs || []).forEach(() => { const port = h("div", { class: "port out", title: "drag to connect" }); port.addEventListener("mousedown", (e) => startConnect(e, n.id)); el.append(port); });
      el.addEventListener("mousedown", (e) => { if (e.target.classList.contains("port")) return; startDrag(e, n); });
      el.addEventListener("dblclick", () => previewNode(n.id));
      world.append(el);
      E.els.nodes[n.id] = el;
    }
    drawEdges();
  }

  function portPos(id, kind, port) {
    const n = editor.doc.nodes.find((x) => x.id === id); const el = editor.els.nodes[id];
    if (!n || !el) return { x: 0, y: 0 };
    if (kind === "out") return { x: n.position.x + el.offsetWidth + 1, y: n.position.y + 27 };
    if (port === "lookup") return { x: n.position.x - 1, y: n.position.y + el.offsetHeight - 17 };
    return { x: n.position.x - 1, y: n.position.y + 27 };
  }
  const curve = (a, b) => { const dx = Math.max(40, Math.abs(b.x - a.x) / 2); return `M${a.x},${a.y} C${a.x + dx},${a.y} ${b.x - dx},${b.y} ${b.x},${b.y}`; };

  function drawEdges(ghost) {
    const E = editor, svg = E.els.svg;
    svg.replaceChildren();
    E.doc.edges.forEach((e, i) => {
      const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
      p.setAttribute("d", curve(portPos(e.from, "out"), portPos(e.to, "in", e.port)));
      if (E.selEdge === i) p.setAttribute("class", "sel");
      p.addEventListener("mousedown", (ev) => { ev.stopPropagation(); E.selEdge = i; E.sel = null; drawNodes(); drawInspector(); E.els.canvas.focus(); });
      svg.append(p);
    });
    if (ghost) { const p = document.createElementNS("http://www.w3.org/2000/svg", "path"); p.setAttribute("d", curve(ghost.a, ghost.b)); p.setAttribute("class", "ghost"); svg.append(p); }
  }

  function startDrag(e, n) {
    const E = editor; e.preventDefault();
    if (E.sel !== n.id) { E.sel = n.id; E.selEdge = null; E.els.world.querySelectorAll(".cv-node").forEach((x) => x.classList.toggle("sel", x.dataset.id === n.id)); drawInspector(); }
    E.els.canvas.focus();
    const sx = e.clientX, sy = e.clientY, ox = n.position.x, oy = n.position.y; let moved = false;
    const mv = (ev) => { const dx = (ev.clientX - sx) / E.zoom, dy = (ev.clientY - sy) / E.zoom; if (Math.abs(dx) + Math.abs(dy) > 2) moved = true;
      n.position = { x: Math.round((ox + dx) / 10) * 10, y: Math.round((oy + dy) / 10) * 10 }; const el = E.els.nodes[n.id]; el.style.left = n.position.x + "px"; el.style.top = n.position.y + "px"; drawEdges(); };
    const up = () => { window.removeEventListener("mousemove", mv); window.removeEventListener("mouseup", up); if (moved) markDirty(); };
    window.addEventListener("mousemove", mv); window.addEventListener("mouseup", up);
  }

  function startConnect(e, fromId) {
    e.preventDefault(); e.stopPropagation();
    const E = editor; const a = portPos(fromId, "out");
    const mv = (ev) => { drawEdges({ a, b: screenToWorld(ev.clientX, ev.clientY) }); };
    const up = (ev) => {
      window.removeEventListener("mousemove", mv); window.removeEventListener("mouseup", up);
      let target = document.elementFromPoint(ev.clientX, ev.clientY);
      const port = target && target.classList.contains("port") && target.classList.contains("in") ? target : null;
      const nodeEl = target && target.closest(".cv-node");
      if (nodeEl && nodeEl.dataset.id !== fromId) {
        const toId = nodeEl.dataset.id; const t = TYPES[E.doc.nodes.find((x) => x.id === toId).type];
        if (t.category === "source") { toast("Sources have no inputs", "error"); }
        else {
          const p = port ? port.dataset.port : "in";
          E.doc.edges = E.doc.edges.filter((x) => !(x.to === toId && (x.port || "in") === p));
          E.doc.edges.push(p === "in" ? { from: fromId, to: toId } : { from: fromId, to: toId, port: p });
          markDirty();
        }
      }
      drawNodes();
    };
    window.addEventListener("mousemove", mv); window.addEventListener("mouseup", up);
  }

  function upstreamColumns(id) {
    const e = editor.doc.edges.find((x) => x.to === id && (x.port || "in") === "in");
    const pv = e && editor.previews[e.from];
    const own = editor.previews[id];
    return (pv && pv.columns ? pv.columns.map((c) => c.name) : own && own.columns ? own.columns.map((c) => c.name) : []);
  }

  function drawInspector() {
    const E = editor, insp = E.els.insp;
    const n = E.doc.nodes.find((x) => x.id === E.sel);
    if (!n) {
      insp.replaceChildren(h("h3", {}, "Pipeline"),
        field("Name", h("input", { class: "input", value: E.doc.name, oninput: (e) => { E.doc.name = e.target.value; markDirty(); } })),
        field("Description", h("textarea", { class: "textarea", oninput: (e) => { E.doc.description = e.target.value; markDirty(); } }, E.doc.description || "")),
        field("Id", h("input", { class: "input mono", value: E.doc.id || "", disabled: !!E.doc.id || null, placeholder: "derived from the name", oninput: (e) => (E.doc.id = e.target.value) }), "File: canvas/pipelines/<id>.yaml"),
        h("div", { class: "caption" }, `${E.doc.nodes.length} node(s) · ${E.doc.edges.length} connection(s)`),
        E.doc.schedule && E.doc.schedule.enabled ? h("div", { class: "chip outline", style: { marginTop: "10px" } }, icon("clock", 12), E.doc.schedule.cron) : null,
        h("div", { class: "section" }, h("span", { class: "eyebrow" }, "Sync state"), h("pre", { class: "logbox" }, JSON.stringify(E.state || {}, null, 2)),
          E.doc.id ? h("button", { class: "btn ghost sm", onclick: async () => { if (await confirm({ title: "Reset sync state?", body: "Incremental cursors and high-water marks are forgotten; the next run re-reads everything.", danger: true, okText: "Reset" })) { await api(`api/pipelines/${enc(E.doc.id)}/state`, { method: "DELETE" }); E.state = {}; drawInspector(); } } }, icon("refresh", 14), "Reset state") : null));
      return;
    }
    const t = TYPES[n.type];
    const cfg = n.config = n.config || {};
    const redraw = () => drawInspector();
    const opts = { upstreamColumns: upstreamColumns(n.id), onchange: () => { markDirty(); const el = E.els.nodes[n.id]; if (el) el.querySelector(".sub").textContent = String(nodeSubtitle(n)).slice(0, 60); } };
    const myIssues = E.issues.filter((i) => i.node === n.id);
    const pvBtn = h("button", { class: "btn secondary sm" }, icon("search", 14), "Preview");
    pvBtn.onclick = () => busy(pvBtn, () => previewNode(n.id));
    insp.replaceChildren(
      h("div", { class: "insp-head" }, h("span", { class: "ic " + t.category }, icon(t.icon, 18)), h("div", { style: { flex: 1, minWidth: 0 } }, h("h3", {}, t.label), h("div", { class: "caption" }, t.blurb))),
      field("Label", h("input", { class: "input", value: n.label || "", oninput: (e) => { n.label = e.target.value; markDirty(); const el = E.els.nodes[n.id]; if (el) el.querySelector(".ttl").textContent = n.label; } })),
      ...myIssues.map((i) => h("div", { class: "alert " + (i.level === "error" ? "error" : "warning"), style: { marginBottom: "10px", fontSize: "13px" } }, i.message)),
      ...t.fields.filter((f) => visible(f, cfg, t.fields)).map((f) => renderField(f, cfg, redraw, opts)),
      h("div", { class: "row", style: { marginTop: "6px" } }, pvBtn, h("span", { style: { flex: 1 } }),
        h("button", { class: "btn danger sm", onclick: () => removeNode(n.id) }, icon("delete", 14), "Remove")),
      h("div", { class: "caption", style: { marginTop: "10px" } }, "Node id: ", h("code", {}, n.id), " · double-click a node to preview"));
  }

  // ── bottom panel ───────────────────────────────────────────────────────
  let bottomTab = "preview";
  function showTab(t) { bottomTab = t; drawBottom(); }

  function drawBottom() {
    const E = editor, b = E.els.bottom;
    const tabs = [["preview", "Preview", "search"], ["log", "Run log", "notes"], ["runs", "Runs", "history"], ["issues", `Issues (${E.issues.length})`, "error"], ["yaml", "YAML", "contract"]];
    const pane = h("div", { class: "pane" });
    const resizer = h("div", { class: "resizer" });
    resizer.addEventListener("mousedown", (e) => {
      const sy = e.clientY, start = parseInt(getComputedStyle(E.els.ed).getPropertyValue("--cv-bottom")) || 260;
      const mv = (ev) => E.els.ed.style.setProperty("--cv-bottom", Math.max(120, Math.min(window.innerHeight - 240, start - (ev.clientY - sy))) + "px");
      const up = () => { window.removeEventListener("mousemove", mv); window.removeEventListener("mouseup", up); };
      window.addEventListener("mousemove", mv); window.addEventListener("mouseup", up);
    });
    b.replaceChildren(resizer, h("div", { class: "tabs" }, ...tabs.map(([id, label, ic]) => h("button", { class: bottomTab === id ? "active" : "", onclick: () => showTab(id) }, icon(ic, 14), label))), pane);
    if (bottomTab === "preview") {
      const pv = E.previews[E.sel];
      if (!E.sel) pane.append(h("div", { class: "caption" }, "Select a node and press Preview (or double-click it) to see sample rows at that step."));
      else if (!pv) pane.append(h("div", { class: "caption" }, "No preview yet for this node."));
      else if (pv.error) pane.append(h("div", { class: "alert error" }, pv.error));
      else pane.append(previewTable(pv));
    } else if (bottomTab === "log") {
      pane.append(h("pre", { class: "logbox", id: "logbox" }, E.logText || "Run the pipeline to see live logs here."));
    } else if (bottomTab === "runs") {
      if (!E.doc.id) return pane.append(h("div", { class: "caption" }, "Save the pipeline to run it."));
      api(`api/pipelines/${enc(E.doc.id)}/runs`).then((d) => {
        const t = BSP.table({ columns: [
          { key: "status", label: "Status", render: (r) => statusBadge(r.status) },
          { key: "started_at", label: "Started", render: (r) => fmtTime(r.started_at) },
          { key: "trigger", label: "Trigger" },
          { key: "rows", label: "Rows loaded", num: true, render: (r) => (r.loaded || []).reduce((a, x) => a + x.rows, 0).toLocaleString() },
          { key: "detail", label: "Detail", render: (r) => h("span", { class: "caption" }, (r.error || (r.loaded || []).map((x) => `${x.target}: ${x.rows}`).join(" · ")).slice(0, 160)) },
          { key: "x", label: "", render: (r) => h("button", { class: "btn ghost sm", onclick: () => { showTab("log"); followRun(r); } }, "Log") },
        ], rows: d.runs, pageSize: 20, empty: h("div", { class: "empty" }, "No runs yet.") });
        pane.append(t.el);
      });
    } else if (bottomTab === "issues") {
      if (!E.issues.length) pane.append(h("div", { class: "caption" }, "No issues. Validate after editing to re-check."));
      E.issues.forEach((i) => pane.append(h("div", { class: "issue", onclick: () => { if (i.node) { E.sel = i.node; drawNodes(); drawInspector(); } } },
        h("span", { class: "badge " + (i.level === "error" ? "error" : "warning") }, i.level), h("span", {}, i.node ? h("b", {}, i.node + ": ") : null, i.message))));
    } else if (bottomTab === "yaml") {
      pane.append(h("div", { class: "caption", style: { marginBottom: "6px" } }, "Pipelines are stored as YAML in canvas/pipelines/ — export to version them in git or move them between appliances."),
        h("pre", { class: "yaml" }, toYaml(E.doc)));
    }
  }

  function previewTable(pv) {
    const cols = pv.columns;
    return h("div", {}, h("div", { class: "caption", style: { marginBottom: "6px" } }, `${pv.row_count} sample row(s) · ${cols.length} column(s) · ${pv.ms} ms — names in grey are how columns land (snake_case)`),
      h("table", { class: "prev-table" }, h("thead", {}, h("tr", {}, ...cols.map((c) => h("th", {}, c.name, h("span", { class: "ty" }, c.type + (c.landed_as !== c.name ? " · " + c.landed_as : "")))))),
        h("tbody", {}, ...pv.rows.map((r) => h("tr", {}, ...cols.map((c) => { const v = r[c.name]; return h("td", { title: v == null ? "" : String(v) }, v == null ? h("span", { class: "muted" }, "null") : typeof v === "object" ? JSON.stringify(v) : String(v)); }))))));
  }

  async function previewNode(id) {
    const E = editor; E.sel = id; drawNodes(); drawInspector();
    showTab("preview");
    E.els.bottom.querySelector(".pane").replaceChildren(h("div", { class: "caption" }, "Running the pipeline up to this node on a sample…"));
    try {
      const pv = await api("api/preview", { body: { pipeline: E.doc, node_id: id, limit: 50 } });
      E.previews[id] = pv;
    } catch (e) { E.previews[id] = { error: e.message }; }
    drawNodes(); drawInspector(); drawBottom();
  }

  async function validateNow() {
    const d = await api("api/validate", { body: editor.doc });
    editor.issues = d.issues; drawNodes(); drawInspector(); showTab("issues");
    const errs = d.issues.filter((i) => i.level === "error").length;
    toast(errs ? `${errs} error(s) to fix` : "Pipeline is valid", errs ? "error" : "success");
  }

  async function save() {
    const E = editor;
    try {
      const d = await api("api/pipelines", { body: E.doc });
      const isNew = !E.doc.id;
      E.doc = d.pipeline; E.issues = d.issues; E.dirty = false; saveBtn.classList.remove("primary");
      toast("Saved", "success");
      if (isNew) { history.replaceState(null, "", "#/p/" + enc(E.doc.id)); }
      drawNodes(); drawInspector(); drawBottom();
    } catch (e) { toast(e.message, "error"); throw e; }
  }

  async function followRun(run) {
    const E = editor; let offset = 0; E.logText = "";
    const pid = run.pipeline_id, rid = run.run_id;
    for (;;) {
      if (editor !== E) return;
      const d = await api(`api/runs/${enc(pid)}/${enc(rid)}/log?offset=${offset}`);
      offset = d.offset;
      if (d.lines.length) { E.logText += d.lines.join("\n") + "\n"; const lb = document.getElementById("logbox"); if (lb) { lb.textContent = E.logText; lb.parentElement.scrollTop = 1e9; } }
      if (d.status !== "running") {
        const r = await api(`api/runs/${enc(pid)}/${enc(rid)}`);
        E.lastRunRecord = r.run; drawNodes();
        toast(r.run.status === "success" ? `Run finished — ${(r.run.loaded || []).reduce((a, x) => a + x.rows, 0).toLocaleString()} row(s) loaded` : `Run failed: ${r.run.error}`, r.run.status === "success" ? "success" : "error");
        const st = await api(`api/pipelines/${enc(pid)}/state`); E.state = st.state; if (!E.sel) drawInspector();
        return;
      }
      await new Promise((res) => setTimeout(res, 1000));
    }
  }

  function scheduleDialog() {
    const E = editor; const s = E.doc.schedule = E.doc.schedule || { cron: "", enabled: false };
    const presets = [["", "custom"], ["*/15 * * * *", "every 15 minutes"], ["0 * * * *", "hourly"], ["0 6 * * *", "daily 06:00"], ["0 6 * * 1-5", "weekdays 06:00"], ["0 3 * * 0", "weekly Sun 03:00"]];
    const cron = h("input", { class: "input mono", value: s.cron || "", placeholder: "0 6 * * *" });
    const tz = h("input", { class: "input", value: s.timezone || "UTC", placeholder: "America/Chicago" });
    const en = h("input", { type: "checkbox", checked: !!s.enabled || null });
    const m = modal({ title: "Schedule", body: h("div", {},
      field("Preset", select(presets, "", (v) => { if (v) cron.value = v; })),
      field("Cron (min hour day month weekday)", cron, "Runs in the canvas service's scheduler"),
      field("Time zone", tz),
      h("label", { class: "switch" }, en, h("span", { class: "track" }), h("span", {}, "Enabled"))),
      foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Cancel"), h("button", { class: "btn primary", onclick: async () => { E.doc.schedule = { cron: cron.value.trim(), enabled: en.checked, timezone: tz.value.trim() || "UTC" }; markDirty(); m.close(); try { await save(); } catch (e) { /* toast shown */ } } }, "Save schedule")] });
  }

  function moreMenu(e) {
    const E = editor;
    BSP.menu(e.currentTarget, [
      { label: "Duplicate pipeline", icon: "copy", onClick: async () => { const copy = JSON.parse(JSON.stringify(E.doc)); copy.id = ""; copy.name = E.doc.name + " (copy)"; delete copy.example; const d = await api("api/pipelines", { body: copy }); location.hash = "#/p/" + enc(d.pipeline.id); } },
      { label: "Delete pipeline", icon: "delete", danger: true, onClick: async () => { if (!E.doc.id) return; if (await confirm({ title: `Delete ${E.doc.name}?`, body: "The YAML, run history and state are removed. Loaded data stays.", danger: true, okText: "Delete" })) { await api("api/pipelines/" + enc(E.doc.id), { method: "DELETE" }); E.dirty = false; location.hash = "#/"; } } },
    ]);
  }

  // minimal YAML rendering for the YAML tab (server stores the canonical file)
  function toYaml(o, ind) {
    ind = ind || "";
    const sc = (v) => (v === null || v === undefined ? "null" : typeof v === "string" ? (/^[\w./@-][\w ./@:-]*$/.test(v) && !/^(true|false|null|yes|no|\d.*)$/i.test(v) ? v : JSON.stringify(v)) : String(v));
    if (Array.isArray(o)) return o.length ? o.map((v) => (v && typeof v === "object" ? `${ind}- ` + toYaml(v, ind + "  ").trimStart() : `${ind}- ${sc(v)}`)).join("\n") : `${ind}[]`;
    if (o && typeof o === "object") return Object.entries(o).map(([k, v]) => (v && typeof v === "object" && Object.keys(v).length ? `${ind}${k}:\n${toYaml(v, ind + "  ")}` : `${ind}${k}: ${v && typeof v === "object" ? (Array.isArray(v) ? "[]" : "{}") : sc(v)}`)).join("\n");
    return ind + sc(o);
  }

  window.addEventListener("beforeunload", (e) => { if (editor && editor.dirty) { e.preventDefault(); e.returnValue = ""; } });
  boot().catch((e) => toast("Canvas failed to load: " + (e.message || e), "error"));
})();
