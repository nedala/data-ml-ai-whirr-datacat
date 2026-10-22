/* Workbench · Import & Export (/ingress/) — load files, web downloads, BigQuery
   tables, Trino queries and sample data into the warehouse (Postgres `landing` schema), and
   take query results out as CSV / Excel / Parquet. Vanilla JS on BSP + WB. */
(function () {
  "use strict";
  const { h, icon } = BSP;
  const api = WB.api;
  const BASE = "/ingress";
  const A = (p) => `${BASE}/api${p}`;
  const F = (p) => `/files/api${p}`;
  const q = (o) => Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== "").map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
  const NICE = { lake: "Shared lake", work: "My work" };
  const TYPES = [["text", "Text"], ["integer", "Whole number"], ["number", "Decimal number"], ["boolean", "Yes / no"], ["date", "Date"], ["timestamp", "Date & time"], ["json", "JSON"]];
  const TABLE_EXT = /\.(csv|tsv|txt|parquet|pq|xlsx|xlsm|xls|json|jsonl|ndjson)$/i;

  const shell = WB.shell("ingress");
  const main = shell.main;
  main.classList.add("io-main");

  const state = { tab: "in", status: null, params: {} };
  function readHash() {
    const [path, qs] = (location.hash || "").replace(/^#\/?/, "").split("?");
    if (["in", "out", "warehouse", "activity"].includes(path)) state.tab = path;
    state.params = Object.fromEntries(new URLSearchParams(qs || ""));
  }
  readHash();
  const writeHash = (params) => history.replaceState(null, "", `#/${state.tab}${params ? "?" + q(params) : ""}`);

  // ── frame ──────────────────────────────────────────────────────────
  const health = h("div", { class: "io-health" });
  const tabsEl = h("div", { class: "tabs", role: "tablist" });
  const body = h("div");
  main.append(
    h("div", { class: "wb-head" },
      h("div", null, h("div", { class: "eyebrow" }, "Workbench"), h("h1", null, "Import & Export"),
        h("div", { class: "lede" }, "Bring files, web downloads, BigQuery tables, Trino queries and the sample ISP dataset into the warehouse, then explore it and take results out as CSV, Excel or Parquet.")),
      health),
    tabsEl, body);

  const TABS = [["in", "Import", "upload"], ["out", "Export", "download"], ["warehouse", "Warehouse", "database"], ["activity", "Activity", "history"]];
  function renderTabs() {
    tabsEl.replaceChildren(...TABS.map(([id, label, ic]) => h("button", {
      role: "tab", "aria-selected": String(state.tab === id), class: state.tab === id ? "active" : "",
      onclick: () => { state.tab = id; state.params = {}; writeHash(); render(); },
    }, icon(ic, 15), label)));
  }
  function renderHealth() {
    const s = state.status;
    if (!s) return health.replaceChildren(h("span", { class: "io-dot" }, h("span", { class: "io-spin" }), "Checking…"));
    const wh = s.warehouse;
    health.replaceChildren(
      h("span", { class: `io-dot ${wh.ok ? "ok" : "off"}`, title: wh.ok ? `${wh.database} on ${wh.host} · PostgreSQL ${wh.version}` : wh.error || "" },
        h("span", { class: "d" }), wh.ok ? `Warehouse · ${wh.database}` : "Warehouse unreachable"),
      h("span", { class: `io-dot ${s.bigquery.configured ? "ok" : ""}`, title: s.bigquery.configured ? `${s.bigquery.principal || s.bigquery.identity || "assigned identity"} · data projects ${(s.bigquery.data_projects || [s.bigquery.project]).join(", ")} · jobs in ${s.bigquery.exec_project || s.bigquery.project} · ${s.bigquery.location}` : (s.bigquery.hint || "Not connected") },
        h("span", { class: "d" }), s.bigquery.configured ? `BigQuery · ${s.bigquery.project}` : "BigQuery not connected"),
      h("span", { class: `io-dot ${s.trino.configured ? "ok" : ""}`, title: s.trino.configured ? s.trino.host : (s.trino.hint || "Not running") },
        h("span", { class: "d" }), s.trino.configured ? "Trino" : "Trino not running"));
  }
  async function loadStatus() {
    try { state.status = await api(A("/status")); } catch (e) { state.status = { warehouse: { ok: false, error: e.message }, bigquery: { configured: false }, trino: { configured: false }, limits: {} }; }
    renderHealth();
    return state.status;
  }

  let cleanup = [];
  function render() {
    cleanup.forEach((f) => f()); cleanup = [];
    renderTabs();
    body.replaceChildren();
    body.className = "fade-in";
    if (state.tab === "in") renderIn();
    else if (state.tab === "out") renderOut();
    else if (state.tab === "warehouse") renderWarehouse();
    else renderActivity();
  }

  // ── shared bits ────────────────────────────────────────────────────
  const errBox = (title, msg) => h("div", { class: "alert error" }, icon("error", 18), h("div", null, h("div", { class: "title" }, title), h("div", { class: "io-errmsg" }, msg)));
  const qi = (s) => (/^[a-z_][a-z0-9_]*$/.test(s) ? s : `"${s.replace(/"/g, '""')}"`);
  const tableRef = (schema, name) => `${qi(schema)}.${qi(name)}`;
  function grid(cols, rows, opts) {
    opts = opts || {};
    return h("div", { class: "table-wrap io-grid" }, h("table", { class: "table" },
      h("thead", null, h("tr", null, h("th", { class: "rn" }, "#"), cols.map((c) => h("th", { title: c.name }, h("span", { class: "cn" }, c.name), c.type ? h("span", { class: "ty" }, c.type) : null)))),
      h("tbody", null, rows.length ? rows.map((r, i) => h("tr", null, h("td", { class: "rn" }, String(i + 1)),
        r.map((v) => v === null ? h("td", { class: "null" }, "null") : h("td", { title: typeof v === "object" ? JSON.stringify(v) : String(v) }, typeof v === "object" ? JSON.stringify(v) : String(v)))))
        : h("tr", null, h("td", { colspan: cols.length + 1 }, h("div", { class: "empty" }, opts.empty || "No rows."))))));
  }
  function seg(options, value, onChange, label) {
    const el = h("div", { class: "segmented", role: "radiogroup", "aria-label": label || "" });
    const draw = () => el.replaceChildren(...options.map(([v, l, dis]) => h("button", { type: "button", role: "radio", "aria-checked": String(v === value), class: v === value ? "active" : "",
      disabled: dis || null, title: dis || "", onclick: () => { value = v; draw(); onChange(v); } }, l)));
    draw();
    return el;
  }
  function pyRead(table) {
    return `import os, psycopg2, pandas as pd\n\nconn = psycopg2.connect(host=os.environ["PG_HOST"], port=os.environ.get("PG_PORT", "5432"),\n                        dbname=os.environ["PG_DB"], user=os.environ["PG_USER"], password=os.environ["PG_PASS"])\nwith conn, conn.cursor() as cur:\n    cur.execute("SELECT * FROM ${table}")\n    df = pd.DataFrame(cur.fetchall(), columns=[c.name for c in cur.description])\ndf.head()`;
  }
  function snippet(title, code) {
    return h("div", { class: "io-snip" }, h("div", { class: "row between" }, h("span", { class: "label" }, title),
      h("button", { class: "btn ghost sm", onclick: () => BSP.copy(code, title) }, icon("copy", 14), "Copy")), h("pre", { class: "codeblock" }, code));
  }
  function warehouseDown() {
    const wh = state.status && state.status.warehouse;
    return wh && !wh.ok ? h("div", { class: "alert warning", style: { marginBottom: "16px" } }, icon("warning", 18),
      h("div", null, h("div", { class: "title" }, "The warehouse isn't reachable right now"), h("div", { class: "io-errmsg" }, wh.error || ""),
        h("div", { class: "caption", style: { color: "inherit", marginTop: "4px" } }, "Make sure the Base (Postgres) stack is running in Studio, then refresh."))) : null;
  }

  // ═════════════════════════════ BRING DATA IN ════════════════════════
  function renderIn() {
    const p = state.params;
    if (p.root && p.path) return renderInspect({ root: p.root, path: p.path });
    body.append(warehouseDown() || "");
    const wrap = h("div", { class: "io-wizard" });
    body.append(wrap);

    // 1. upload (primary)
    const fileIn = h("input", { type: "file", style: { display: "none" }, accept: ".csv,.tsv,.txt,.parquet,.pq,.xlsx,.xlsm,.xls,.json,.jsonl,.ndjson" });
    const dropEl = h("div", { class: "io-drop", tabindex: "0", role: "button", "aria-label": "Upload a file" },
      h("div", { class: "big" }, icon("upload", 26)),
      h("div", { class: "t" }, "Drop a file here, or ", h("span", { class: "lnk" }, "browse")),
      h("div", { class: "s" }, "It's saved to the shared lake (landing/) and you'll check the columns before anything is loaded."),
      h("div", { class: "fmts" }, ["CSV", "TSV", "Excel", "Parquet", "JSON"].map((f) => h("span", { class: "chip outline" }, f))), fileIn);
    dropEl.onclick = () => fileIn.click();
    dropEl.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileIn.click(); } };
    dropEl.ondragover = (e) => { e.preventDefault(); dropEl.classList.add("over"); };
    dropEl.ondragleave = () => dropEl.classList.remove("over");
    dropEl.ondrop = (e) => { e.preventDefault(); dropEl.classList.remove("over"); if (e.dataTransfer.files[0]) uploadThenInspect(e.dataTransfer.files[0]); };
    fileIn.onchange = () => { if (fileIn.files[0]) uploadThenInspect(fileIn.files[0]); fileIn.value = ""; };
    const upHost = h("div");

    // 2. other sources
    const bq = (state.status && state.status.bigquery) || {};
    const tr = (state.status && state.status.trino) || {};
    const panel = h("div");
    const tiles = [
      ["files", "folder-open", "From Files", "A file already in the shared lake or in My work."],
      ["url", "globe", "From a web address", "Download an https:// link (approved sites only)."],
      ["bigquery", "cloud", "From BigQuery", bq.configured ? `Browse ${(bq.data_projects || [bq.project]).length > 1 ? `${bq.data_projects.length} projects (${bq.project}, …)` : `project ${bq.project}`} as ${bq.principal || bq.identity || "the assigned identity"} — or switch identity — then preview and land a table or query.` : "Not connected — an admin assigns a Google identity in Studio → Cloud identities."],
      ["trino", "database", "From Trino", tr.configured ? "Run a federated query (warehouse, BigQuery, lake) and land the result." : "Start the Trino capability in Studio."],
      ["samples", "file-table", "Sample datasets", "The synthetic ISP dataset — customers, billing, tickets, network, churn."],
    ];
    let open = p.source || null;
    const tileEls = tiles.map(([id, ic, t, blurb]) => {
      const off = (id === "bigquery" && !bq.configured) || (id === "trino" && !tr.configured);
      const el = h("button", { class: `tile ${off ? "io-off" : ""}`, type: "button", "aria-disabled": off ? "true" : null, onclick: () => { if (off) return sourceHelp(id); open = open === id ? null : id; drawPanel(); } },
        h("div", { class: "tile-ic" }, icon(ic, 18)), h("div", { class: "tile-title" }, t), h("div", { class: "tile-blurb" }, blurb));
      el.dataset.id = id;
      return el;
    });
    function drawPanel() {
      tileEls.forEach((el) => el.classList.toggle("selected", el.dataset.id === open));
      panel.replaceChildren();
      if (open === "files") panel.append(filePicker());
      else if (open === "url") panel.append(urlPanel());
      else if (open === "bigquery" && bq.configured) panel.append(bigqueryPanel());
      else if (open === "trino" && tr.configured) panel.append(trinoPanel());
      else if (open === "samples") panel.append(samplesPanel());
      if (open) setTimeout(() => panel.scrollIntoView({ behavior: "smooth", block: "nearest" }), 30);
    }
    wrap.append(dropEl, upHost, h("div", { class: "grid auto io-tiles" }, tileEls), panel);
    drawPanel();

    function uploadThenInspect(file) {
      if (!TABLE_EXT.test(file.name)) return BSP.toast("Choose a CSV, TSV, Excel, Parquet or JSON file", "error");
      const lim = state.status && state.status.limits && state.status.limits.upload_max_bytes;
      if (lim && file.size > lim) return BSP.toast(`That file is larger than the ${BSP.fmtBytes(lim)} upload limit`, "error");
      const meter = h("div", { class: "meter" }, h("span", { style: { width: "0%" } }));
      const sub = h("div", { class: "caption" }, "Uploading to Shared lake › landing …");
      const card = h("div", { class: "card io-upcard" }, h("div", { class: "row" }, h("div", { class: "io-fic" }, icon("upload", 20)),
        h("div", { style: { flex: 1, minWidth: 0 } }, h("div", { class: "io-fname" }, file.name), sub)), meter);
      upHost.replaceChildren(card);
      const go = (overwrite) => {
        const xhr = new XMLHttpRequest();
        xhr.open("PUT", F(`/upload?${q({ root: "lake", path: "landing", name: file.name, overwrite: overwrite ? 1 : 0 })}`));
        xhr.setRequestHeader("X-Studio", "1");
        xhr.setRequestHeader("Content-Type", "application/octet-stream");
        xhr.upload.onprogress = (e) => { if (e.lengthComputable) { meter.firstChild.style.width = `${(100 * e.loaded) / e.total}%`; sub.textContent = `Uploading… ${BSP.fmtBytes(e.loaded)} of ${BSP.fmtBytes(e.total)}`; } };
        xhr.onload = async () => {
          if (xhr.status === 409 && !overwrite) {
            const ok = await BSP.confirm({ title: `Replace landing/${file.name}?`, body: "A file with this name is already in the shared lake's landing folder.", okText: "Replace" });
            return ok ? go(true) : upHost.replaceChildren();
          }
          if (xhr.status >= 200 && xhr.status < 300) {
            const r = JSON.parse(xhr.responseText);
            state.params = { root: r.root, path: r.path }; writeHash(state.params); render();
          } else {
            let msg = xhr.statusText; try { msg = JSON.parse(xhr.responseText).detail || msg; } catch (_) { /* not json */ }
            if (xhr.status === 413 && !/limit/.test(msg)) msg = "The file is too large for the gateway — ask your admin to raise the upload limit.";
            upHost.replaceChildren(errBox("Upload failed", msg));
          }
        };
        xhr.onerror = () => upHost.replaceChildren(errBox("Upload failed", "Network error — check your connection and try again."));
        xhr.send(file);
      };
      go(false);
    }
  }

  function sourceHelp(id) {
    if (id === "trino") return BSP.modal({ title: "Start Trino", body: h("div", { class: "stack" },
      h("p", null, "Trino runs as its own capability. In Studio → Compose, switch on Trino and apply; it federates the local warehouse, BigQuery and lake files."),
      h("p", { class: "caption" }, "This page checks trino:8080 every time it loads.")) });
    BSP.modal({ title: "Connect BigQuery", body: h("div", { class: "stack" },
      h("p", null, "BigQuery isn't connected for this workspace. An admin adds a Google identity (a service-account key or a Google sign-in) in Studio → Cloud identities, assigns it to BigQuery · read with its data projects and job project, and applies."),
      h("p", null, "Identities an admin shares with you can then be picked under \"Acting as\" on this card."),
      h("p", { class: "caption" }, "*.googleapis.com is on the default egress allowlist.")) });
  }

  // — pick a file from the lake / workspace —
  function filePicker() {
    let root = "lake", path = "landing";
    const crumbs = h("div", { class: "crumbs" });
    const list = h("div");
    const card = h("div", { class: "card" }, h("div", { class: "card-head" }, h("div", null, h("h3", null, "Choose a file"),
      h("div", { class: "caption" }, "Tables only: CSV, TSV, Excel, Parquet, JSON.")),
      seg([["lake", "Shared lake"], ["work", "My work"]], root, (v) => { root = v; path = v === "lake" ? "landing" : ""; load(); }, "Location")), crumbs, list);
    async function load() {
      list.replaceChildren(WB.skeletonRows(4));
      let r;
      try { r = await api(F(`/list?${q({ root, path })}`)); }
      catch (e) { if (path) { path = ""; return load(); } return list.replaceChildren(errBox("Can't open this folder", e.message)); }
      crumbs.replaceChildren(...r.crumbs.flatMap((c, i) => {
        const b = h("button", { class: i === r.crumbs.length - 1 ? "cur" : "", onclick: () => { path = c.path; load(); } }, c.name);
        return i ? [h("span", { class: "sep" }, icon("chevron-right", 14)), b] : [b];
      }));
      const rows = r.entries.filter((e) => e.type === "dir" || TABLE_EXT.test(e.name));
      const t = BSP.table({ rows, pageSize: 50, rowKey: "name",
        empty: WB.empty({ ic: "folder-open", title: "No tables here", body: "Upload a file above, or look in another folder." }),
        onRowClick: (e) => pick(e),
        columns: [
          { key: "name", label: "Name", sort: true, render: (e) => h("span", { class: "obj-name" }, h("span", { class: `fic ${e.type === "dir" ? "folder" : "table"}` }, icon(e.type === "dir" ? "folder" : "file-table", 15)), h("span", { class: "nm" }, e.name)) },
          { key: "size", label: "Size", num: true, render: (e) => (e.type === "dir" ? "" : BSP.fmtBytes(e.size)) },
          { key: "mtime", label: "Modified", render: (e) => BSP.fmtAgo(e.mtime * 1000) },
          { key: "_a", label: "", render: (e) => e.type === "dir" ? h("span", { class: "caption" }, icon("chevron-right", 14)) : h("button", { class: "btn secondary sm", onclick: () => pick(e) }, "Use this file") },
        ] });
      list.replaceChildren(t.el);
    }
    function pick(e) {
      const rel = path ? `${path}/${e.name}` : e.name;
      if (e.type === "dir") { path = rel; return load(); }
      state.params = { root, path: rel }; writeHash(state.params); render();
    }
    load();
    return card;
  }

  // — https URL —
  function urlPanel() {
    const input = h("input", { class: "input", type: "url", placeholder: "https://example.com/data/outages.csv", "aria-label": "Web address", autocomplete: "off", spellcheck: "false" });
    const out = h("div");
    const btn = h("button", { class: "btn primary" }, icon("download", 16), "Download");
    const submit = async () => {
      const url = input.value.trim();
      if (!/^https:\/\//i.test(url)) { out.replaceChildren(errBox("Use an https:// address", "Only secure web addresses are supported.")); return; }
      out.replaceChildren();
      try {
        const r = await api(A("/url"), { body: { url } });
        out.replaceChildren(h("div", { class: "row", style: { marginTop: "10px" } }, h("span", { class: "io-spin" }), h("span", { class: "caption" }, "Downloading — follow progress in the job panel…")));
        const s = await WB.watchJob(r.job, { base: BASE, hint: false });
        if (s.status === "succeeded" && s.result && s.result.path) { state.params = { root: s.result.root, path: s.result.path }; writeHash(state.params); render(); }
        else out.replaceChildren(errBox("Download failed", s.error || "see the job log"));
      } catch (e) { out.replaceChildren(errBox("Can't download", e.message)); }
    };
    btn.onclick = () => BSP.busy(btn, submit);
    input.onkeydown = (e) => { if (e.key === "Enter") btn.click(); };
    setTimeout(() => input.focus(), 50);
    return h("div", { class: "card" }, h("div", { class: "card-head" }, h("div", null, h("h3", null, "Download from a web address"),
      h("div", { class: "caption" }, "The file is saved to Shared lake › landing, then you check its columns. Downloads go through the appliance's egress proxy, so only sites on the approved list work."))),
      h("div", { class: "input-group" }, input, btn), out);
  }

  // — shared: query box → preview / load into landing —
  function sqlLoader({ title, caption, placeholder, preview, load, extraBody }) {
    const ta = h("textarea", { class: "textarea mono io-sql", spellcheck: "false", placeholder });
    const tbl = h("input", { class: "input mono", placeholder: "table_name", "aria-label": "Landing table name" });
    let mode = "replace";
    const out = h("div");
    const prevBtn = h("button", { class: "btn secondary" }, icon("visibility", 16), "Preview");
    const loadBtn = h("button", { class: "btn primary" }, icon("database", 16), "Load into warehouse");
    const body = () => Object.assign({ sql: ta.value.trim() || null }, extraBody ? extraBody() : {});
    prevBtn.onclick = () => BSP.busy(prevBtn, async () => {
      try {
        const r = await api(A(preview), { body: Object.assign(body(), { limit: 50 }) });
        out.replaceChildren(h("div", { class: "caption", style: { margin: "10px 0 6px" } },
          `First ${r.rows.length} rows` + (r.total_rows != null ? ` of ${Number(r.total_rows).toLocaleString()}` : "") +
          (r.bytes_processed != null ? ` · ${BSP.fmtBytes(r.bytes_processed)} scanned` : "")), grid(r.columns, r.rows));
      } catch (e) { out.replaceChildren(errBox("Query failed", e.message)); }
    });
    loadBtn.onclick = () => BSP.busy(loadBtn, async () => {
      const table = tbl.value.trim();
      if (!/^[a-z_][a-z0-9_]{0,62}$/.test(table)) return BSP.toast("Table names use lower-case letters, digits and _", "error");
      try {
        const r = await api(A(load), { body: Object.assign(body(), { table, mode }) });
        const s = await WB.watchJob(r.job, { base: BASE, hint: false });
        if (s.status === "succeeded") out.replaceChildren(doneCard(s.result));
      } catch (e) { out.replaceChildren(errBox("Can't start the load", e.message)); }
    });
    const card = h("div", { class: "card" },
      h("div", { class: "card-head" }, h("div", null, h("h3", null, title), h("div", { class: "caption" }, caption))),
      ta, h("div", { class: "row wrap", style: { marginTop: "12px", gap: "12px" } },
        h("span", { class: "io-opt" }, "Load into ", h("code", null, "landing."), tbl),
        seg([["replace", "Replace"], ["append", "Append"]], mode, (v) => { mode = v; }, "Load mode"),
        h("span", { class: "spacer", style: { flex: 1 } }), prevBtn, loadBtn), out);
    return { card, ta, tbl };
  }

  // — "Acting as" principal picker (Studio → Cloud identities) —
  // The browser only sees names, e-mails and projects; Studio checks the
  // identity's access list and hands the SERVICE a short-lived token.
  let myIds = null;
  const loadMyIds = () => (myIds = myIds || api("/studio/api/gcp/my-identities").catch(() => ({ identities: [], defaults: {} })));
  const store = { get(k) { try { return JSON.parse(localStorage.getItem(k) || "null"); } catch (_) { return null; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) { /* private mode */ } } };

  function principalPicker({ consumer, tool, fallback, onChange }) {
    const idSel = h("select", { class: "select", "aria-label": "Acting as" }, h("option", { value: "" }, "Loading identities…"));
    const pSel = h("select", { class: "select", "aria-label": "Project" });
    const pOther = h("input", { class: "input mono", placeholder: "project-id", "aria-label": "Other project", hidden: true, style: { width: "200px" } });
    const line = h("div", { class: "caption io-acting" });
    const el = h("div", { class: "io-principal" },
      h("div", { class: "row wrap", style: { gap: "10px", alignItems: "center" } },
        h("span", { class: "io-opt" }, icon("user", 14), "Acting as", idSel), h("span", { class: "io-opt" }, "Project", pSel, pOther)), line);
    let data = { identities: [], defaults: {} }, key = null;
    const cur = () => {
      const id = idSel.value;
      const ent = id ? data.identities.find((i) => i.id === id) : null;
      const d = data.defaults[consumer] || {};
      const project = pSel.value === "__other" ? pOther.value.trim() : pSel.value;
      return { identity: id, project, exec_project: ent ? (ent.exec_project || project) : "",
        principal: ent ? ent.principal : (d.principal || fallback.principal || ""), name: ent ? ent.name : (d.name || fallback.identity || "assigned identity"),
        jobs: ent ? (ent.exec_project || project) : (d.exec_project || fallback.exec_project || project) };
    };
    function paintLine() {
      const c = cur();
      line.replaceChildren(...[icon("shield", 12), " Acting as ", h("b", null, c.principal || c.name), c.project ? [" on ", h("code", null, c.project)] : "",
        c.jobs && c.jobs !== c.project ? h("span", { class: "muted" }, ` · jobs run in ${c.jobs}`) : "", idSel.value ? "" : h("span", { class: "muted" }, " · assigned default")].flat());
    }
    function fillProjects(keep) {
      const id = idSel.value, d = data.defaults[consumer] || {};
      const ent = id ? data.identities.find((i) => i.id === id) : null;
      const list = ent ? ent.projects.map((p) => [p.id, p.name && p.name !== p.id ? `${p.id} — ${p.name}` : p.id])
        : (d.data_projects && d.data_projects.length ? d.data_projects : (fallback.data_projects || [fallback.project])).filter(Boolean).map((p) => [p, p]);
      const def = ent ? (ent.default_project || (list[0] || [""])[0]) : ((list[0] || [""])[0]);
      pSel.replaceChildren(...list.map(([v, l]) => h("option", { value: v }, l)), h("option", { value: "__other" }, "Other project…"));
      const want = keep && (list.some(([v]) => v === keep) ? keep : null);
      if (keep && !want) { pSel.value = "__other"; pOther.value = keep; } else pSel.value = want || def || "__other";
      pOther.hidden = pSel.value !== "__other";
    }
    const changed = () => { paintLine(); if (key) store.set(key, { identity: idSel.value, project: cur().project }); onChange(cur()); };
    idSel.onchange = () => { fillProjects(null); changed(); };
    pSel.onchange = () => { pOther.hidden = pSel.value !== "__other"; if (pSel.value !== "__other") changed(); else pOther.focus(); };
    pOther.onchange = changed;
    (async () => {
      data = await loadMyIds();
      data.identities = data.identities || []; data.defaults = data.defaults || {};
      key = `bsp.acting.${tool}.${data.user || "me"}`;
      const d = data.defaults[consumer] || {};
      const others = data.identities.filter((i) => i.id !== d.identity);
      idSel.replaceChildren(h("option", { value: "" }, `Assigned default — ${d.name || fallback.identity || "BigQuery · read"}${(d.principal || fallback.principal) ? ` (${d.principal || fallback.principal})` : ""}`),
        ...others.map((i) => h("option", { value: i.id }, `${i.name}${i.principal ? ` (${i.principal})` : ""}`)));
      idSel.disabled = !others.length;
      idSel.title = others.length ? "Identities you may act as (Studio → Cloud identities → access)" : "No other identity is shared with you — ask a Studio admin";
      const saved = store.get(key) || {};
      if (saved.identity && others.some((i) => i.id === saved.identity)) idSel.value = saved.identity;
      fillProjects(saved.project || null);
      paintLine(); onChange(cur());
    })();
    return { el, get: cur };
  }

  // — BigQuery —
  function bigqueryPanel() {
    const bq = state.status.bigquery;
    let picked = null;   // {dataset, table}
    let who = { identity: "", project: bq.project, exec_project: "" };
    const P = () => ({ identity: who.identity, project: who.project, exec_project: who.exec_project });
    const dsSel = h("select", { class: "select", "aria-label": "Dataset" }, h("option", { value: "" }, "Loading datasets…"));
    const tSel = h("select", { class: "select", "aria-label": "Table", disabled: true }, h("option", { value: "" }, "Choose a dataset first"));
    const loader = sqlLoader({
      title: "BigQuery",
      caption: `Pick a table, or write a read-only query (it takes precedence). Location ${bq.location}.`,
      placeholder: `SELECT * FROM \`${bq.project}.dataset.table\` WHERE …`,
      preview: "/bigquery/preview", load: "/bigquery/load",
      extraBody: () => Object.assign(P(), picked ? { dataset: picked.dataset, source_table: picked.table } : {}),
    });
    let seq = 0;
    async function loadDatasets() {
      const n = ++seq;
      picked = null; tSel.disabled = true; tSel.replaceChildren(h("option", { value: "" }, "Choose a dataset first"));
      dsSel.replaceChildren(h("option", { value: "" }, "Loading datasets…"));
      loader.ta.placeholder = `SELECT * FROM \`${who.project || "project"}.dataset.table\` WHERE …`;
      try {
        const r = await api(A(`/bigquery/datasets?${q(P())}`));
        if (n !== seq) return;
        dsSel.replaceChildren(h("option", { value: "" }, r.datasets.length ? `Choose a dataset (${r.datasets.length})` : "No datasets visible to this identity here"),
          ...r.datasets.map((d) => h("option", { value: d }, d)));
      } catch (e) { if (n !== seq) return; dsSel.replaceChildren(h("option", { value: "" }, e.status === 403 ? "Not allowed for you" : "Couldn't list datasets")); BSP.toast(e.message, "error"); }
    }
    const picker = principalPicker({ consumer: "bigquery_read", tool: "ingress.bigquery", fallback: bq,
      onChange: (c) => { who = c; if (who.project) loadDatasets(); } });
    dsSel.onchange = async () => {
      picked = null; tSel.disabled = true;
      if (!dsSel.value) return;
      tSel.replaceChildren(h("option", { value: "" }, "Loading tables…"));
      try {
        const r = await api(A(`/bigquery/tables?${q(Object.assign({ dataset: dsSel.value }, P()))}`));
        tSel.replaceChildren(h("option", { value: "" }, "Choose a table"), ...r.tables.map((t) => h("option", { value: t.name }, `${t.name}${t.type && t.type !== "TABLE" ? " (" + t.type.toLowerCase() + ")" : ""}`)));
        tSel.disabled = false;
      } catch (e) { tSel.replaceChildren(h("option", { value: "" }, "Couldn't list tables")); BSP.toast(e.message, "error"); }
    };
    tSel.onchange = () => {
      picked = tSel.value ? { dataset: dsSel.value, table: tSel.value } : null;
      if (picked && !loader.tbl.value) loader.tbl.value = tSel.value.toLowerCase().replace(/[^a-z0-9_]/g, "_").replace(/^([0-9])/, "_$1").slice(0, 63);
    };
    loader.card.insertBefore(picker.el, loader.ta);
    loader.card.insertBefore(h("div", { class: "row wrap", style: { gap: "12px", marginBottom: "10px" } }, dsSel, tSel), loader.ta);
    return loader.card;
  }

  // — Trino —
  function trinoPanel() {
    const tr = state.status.trino;
    const loader = sqlLoader({
      title: "Trino (federated SQL)",
      caption: `Coordinator ${tr.host}. Query across catalogs — e.g. the warehouse and BigQuery — in one read-only statement. Trino's BigQuery catalog uses one Google identity shared by everyone (set in Studio → Cloud identities), so there is no "Acting as" switch here.`,
      placeholder: "SELECT * FROM warehouse.marts.dim_customer LIMIT 100",
      preview: "/trino/preview", load: "/trino/load",
    });
    const cats = h("div", { class: "row wrap caption", style: { gap: "6px", marginBottom: "8px" } }, "Catalogs: …");
    (async () => {
      try {
        const r = await api(A("/trino/catalogs"));
        cats.replaceChildren("Catalogs: ", ...r.catalogs.map((c) => h("code", null, c)));
      } catch (e) { cats.replaceChildren("Catalogs unavailable: ", e.message); }
    })();
    loader.card.insertBefore(cats, loader.ta);
    return loader.card;
  }

  // — sample datasets —
  function samplesPanel() {
    const card = h("div", { class: "card" }, WB.skeletonRows(3));
    (async function load() {
      let r;
      try { r = await api(A("/samples")); } catch (e) { return card.replaceChildren(errBox("Couldn't list sample datasets", e.message)); }
      const ds = r.datasets || [];
      const labels = r.targets || {};
      if (!ds.length) return card.replaceChildren(WB.empty({ ic: "file-table", title: "No sample datasets", body: r.note || "This image has no sample-data catalogue." }));
      const allBtn = null;
      card.replaceChildren(
        h("div", { class: "card-head" }, h("div", null, h("h3", null, "Sample datasets"),
          h("div", { class: "caption" }, "Loads the synthetic ISP dataset into the warehouse's ", h("code", null, "raw"), " schema (what the dbt project, notebooks and dashboards build on), or as Parquet into the shared lake.")), allBtn),
        h("div", { class: "grid auto" }, ds.map((d) => {
          const hy = d.hydrated || {};
          const done = Object.keys(hy).filter((t) => hy[t]);
          const others = (d.targets || []).filter((t) => t !== "warehouse");
          const more = h("button", { class: "btn ghost sm" }, "Copy to…", icon("chevron-down", 12));
          more.onclick = () => BSP.menu(more, others.map((t) => ({ label: labels[t] || t, icon: t === "lake" ? "database" : "folder", onClick: () => hydrate([d.id], t, load) })));
          return h("div", { class: "ds-card" },
            h("div", { class: "top" }, h("div", { class: "ds-ic" }, icon("file-table", 18)), h("div", { style: { minWidth: 0, flex: 1 } }, h("div", { class: "title" }, d.title), h("div", { class: "caption mono" }, d.id))),
            h("div", { class: "desc" }, d.description || ""),
            h("div", { class: "meta" }, d.rows != null ? h("span", null, `${Number(d.rows).toLocaleString()} rows`) : null, d.table ? h("span", { class: "mono" }, d.table) : null),
            h("div", { class: "row wrap" }, done.length ? done.map((t) => h("span", { class: "badge ok" }, icon("check", 12), labels[t] || t)) : h("span", { class: "badge neutral" }, "Not loaded yet")),
            h("div", { class: "actions" },
              (d.targets || []).includes("warehouse") ? h("button", { class: `btn ${hy.warehouse ? "ghost" : "secondary"} sm`, onclick: () => hydrate([d.id], "warehouse", load) }, icon("database", 14), hy.warehouse ? "Reload" : "Load into warehouse") : null,
              others.length ? more : null));
        })));
    })();
    return card;
  }
  async function hydrate(ids, target, after) {
    try { const r = await api(A("/samples/load"), { body: { ids, target } }); WB.watchJob(r.job, { base: BASE, hint: false, onDone: after }); }
    catch (e) { BSP.toast(e.message, "error"); }
  }

  // — inspect + load a file —
  function renderInspect(src) {
    const opts = { header: true, delimiter: "auto", encoding: "utf-8", skip_rows: 0, sheet: null };
    let info = null, cols = [], table = "", mode = "replace", exists = null;
    const back = h("button", { class: "btn ghost sm", onclick: () => { state.params = {}; writeHash(); render(); } }, icon("arrow-left", 14), "Choose another source");
    const head = h("div", { class: "card io-summary" }, h("div", { class: "io-srchead" }, h("div", { class: "io-fic" }, icon("file-table", 20)),
      h("div", { style: { minWidth: 0 } }, h("div", { class: "io-fname" }, src.path.split("/").pop()), h("div", { class: "caption" }, `${NICE[src.root] || src.root} › ${src.path}`))));
    const optsRow = h("div", { class: "io-opts" });
    const schemaCard = h("div", { class: "card" }, WB.skeletonRows(5));
    const previewCard = h("div", { class: "card" });
    const destCard = h("div", { class: "card io-dest" });
    const bar = h("div", { class: "actionbar io-actionbar", style: { display: "none" } });
    const result = h("div");
    body.append(warehouseDown() || "", h("div", { class: "row", style: { marginBottom: "12px" } }, back),
      h("div", { class: "io-wizard" }, head, schemaCard, previewCard, destCard, bar, result));

    async function inspect() {
      schemaCard.replaceChildren(h("div", { class: "row" }, h("span", { class: "io-spin" }), h("span", { class: "caption" }, "Reading the file and guessing column types…")));
      try { info = await api(A("/inspect"), { body: { root: src.root, path: src.path, options: opts, rows: 50 } }); }
      catch (e) {
        schemaCard.replaceChildren(errBox("Can't read this file", e.message), optsRow);
        drawOpts(); previewCard.style.display = "none"; destCard.style.display = "none"; bar.style.display = "none";
        return;
      }
      previewCard.style.display = ""; destCard.style.display = "";
      cols = info.columns.map((c) => ({ ...c, include: true }));
      if (!table) { table = info.suggested_table; exists = info.table_exists; }
      opts.sheet = info.sheet;
      const stats = h("div", { class: "io-stats" },
        h("div", { class: "io-stat" }, h("div", { class: "label" }, "Rows"), h("div", { class: "value" }, info.total_rows != null ? Number(info.total_rows).toLocaleString() : "—")),
        h("div", { class: "io-stat" }, h("div", { class: "label" }, "Columns"), h("div", { class: "value" }, String(info.columns.length))),
        h("div", { class: "io-stat" }, h("div", { class: "label" }, "Format"), h("div", { class: "value" }, info.format.toUpperCase())));
      if (head.childNodes.length > 1) head.lastChild.remove();
      head.append(stats);
      drawOpts(); drawSchema(); drawPreview(); drawDest();
    }

    function drawOpts() {
      const items = [];
      const fmt = info ? info.format : (/\.xls/i.test(src.path) ? "excel" : "csv");
      if (fmt === "excel" && info && info.sheets && info.sheets.length) {
        items.push(h("label", { class: "io-opt" }, "Sheet", h("select", { class: "select", onchange: (e) => { opts.sheet = e.target.value; inspect(); } },
          info.sheets.map((s) => h("option", { value: s, selected: s === opts.sheet || null }, s)))));
      }
      if (fmt === "csv") {
        items.push(h("label", { class: "io-opt" }, "Separator", h("select", { class: "select", onchange: (e) => { opts.delimiter = e.target.value; inspect(); } },
          [["auto", "Detect"], ["comma", "Comma ,"], ["semicolon", "Semicolon ;"], ["tab", "Tab"], ["pipe", "Pipe |"]].map(([v, l]) => h("option", { value: v, selected: v === opts.delimiter || null }, l)))));
        items.push(h("label", { class: "io-opt" }, "Encoding", h("select", { class: "select", onchange: (e) => { opts.encoding = e.target.value; inspect(); } },
          [["utf-8", "UTF-8"], ["latin-1", "Latin-1 (Windows)"], ["utf-16", "UTF-16"]].map(([v, l]) => h("option", { value: v, selected: v === opts.encoding || null }, l)))));
      }
      if (fmt === "csv" || fmt === "excel") {
        items.push(h("label", { class: "io-opt checkbox" }, h("input", { type: "checkbox", checked: opts.header || null, onchange: (e) => { opts.header = e.target.checked; inspect(); } }), "First row has column names"));
        items.push(h("label", { class: "io-opt" }, "Skip rows at top", h("input", { class: "input", type: "number", min: "0", max: "10000", value: String(opts.skip_rows),
          onchange: (e) => { opts.skip_rows = Math.max(0, parseInt(e.target.value, 10) || 0); inspect(); } })));
      }
      optsRow.replaceChildren(...items);
    }

    function drawSchema() {
      const dupMsg = h("div", { class: "io-dup caption" });
      const rows = cols.map((c, i) => {
        const sample = info.rows.map((r) => r[i]).find((v) => v !== null && v !== "");
        const tr = h("tr", { class: c.include ? "" : "off" });
        const name = h("input", { class: "input mono io-cname", value: c.target, "aria-label": `Column name for ${c.name}`, spellcheck: "false",
          oninput: (e) => { c.target = e.target.value.trim(); validate(); } });
        const ty = h("select", { class: "select io-ctype", "aria-label": `Type for ${c.name}`, onchange: (e) => { c.type = e.target.value; tr.classList.toggle("changed", c.type !== c.inferred); } },
          TYPES.map(([v, l]) => h("option", { value: v, selected: v === c.type || null }, l)));
        c.inferred = c.inferred || c.type;
        tr.append(
          h("td", null, h("input", { type: "checkbox", checked: c.include || null, "aria-label": `Include ${c.name}`, onchange: (e) => { c.include = e.target.checked; tr.classList.toggle("off", !c.include); validate(); } })),
          h("td", { title: c.name }, h("span", { class: "mono" }, c.name)),
          h("td", null, name), h("td", null, ty),
          h("td", { class: "caption", title: sample == null ? "" : String(sample), style: { maxWidth: "220px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, sample == null ? "—" : typeof sample === "object" ? JSON.stringify(sample) : String(sample)));
        tr._name = name;
        return tr;
      });
      function validate() {
        const seen = new Map(); let bad = "";
        rows.forEach((tr, i) => {
          const c = cols[i]; let err = false;
          if (c.include) {
            if (!/^[a-z_][a-z0-9_]{0,62}$/.test(c.target)) { err = true; bad = bad || `“${c.target || "(empty)"}” — use lower-case letters, digits and _`; }
            else if (seen.has(c.target)) { err = true; bad = bad || `“${c.target}” is used twice`; }
            seen.set(c.target, i);
          }
          tr.classList.toggle("bad", err);
        });
        dupMsg.textContent = bad;
        drawBar(!bad && cols.some((c) => c.include));
      }
      schemaCard.replaceChildren(
        h("div", { class: "card-head" }, h("div", null, h("h3", null, "Columns", h("span", { class: "count" }, String(cols.length))),
          h("div", { class: "caption" }, "Types were guessed from the data — change any that look wrong. Untick columns you don't need. Values that don't fit a type are left empty.")),
          h("div", { class: "row" }, h("button", { class: "btn ghost sm", onclick: () => { cols.forEach((c) => { c.include = true; }); drawSchema(); } }, "Select all"),
            h("button", { class: "btn ghost sm", onclick: () => { cols.forEach((c) => { c.include = false; }); drawSchema(); } }, "Select none"))),
        optsRow,
        h("div", { class: "table-wrap io-schema", style: { marginTop: "12px" } }, h("table", { class: "table" },
          h("thead", null, h("tr", null, h("th", { style: { width: "36px" } }, ""), h("th", null, "In the file"), h("th", null, "Column name in the warehouse"), h("th", null, "Type"), h("th", null, "Example"))),
          h("tbody", null, rows))), dupMsg);
      validate();
    }

    function drawPreview() {
      previewCard.replaceChildren(h("div", { class: "card-head" }, h("h3", null, "Preview"), h("span", { class: "caption" }, `first ${info.rows.length} rows, as read from the file`)),
        grid(info.columns.map((c) => ({ name: c.name, type: c.source_type.toLowerCase() })), info.rows, { empty: "The file has no data rows." }));
    }

    const existsEl = h("span", { class: "io-exists caption" });
    const checkExists = BSP.debounce(async () => {
      if (!/^[a-z_][a-z0-9_]{0,62}$/.test(table)) { exists = null; return drawExists(); }
      try { exists = (await api(A(`/table-exists?table=${encodeURIComponent(table)}`))).exists; } catch (_) { exists = null; }
      drawExists();
    }, 300);
    function drawExists() {
      existsEl.replaceChildren(exists === true ? h("span", { class: "badge warning" }, icon("warning", 12), mode === "replace" ? "Exists — will be replaced" : "Exists — rows will be added")
        : exists === false ? h("span", { class: "badge ok" }, icon("add", 12), "New table") : "");
      drawModeSeg();
      drawBar();
    }
    const modeHost = h("span");
    function drawModeSeg() {
      if (exists === false && mode === "append") mode = "replace";
      modeHost.replaceChildren(seg([["replace", "Replace the table"], ["append", "Add rows to it", exists === false ? "The table doesn't exist yet" : null]], mode, (v) => { mode = v; drawExists(); }, "Load mode"));
    }
    function drawDest() {
      const tIn = h("input", { class: "input mono", value: table, "aria-label": "Table name", spellcheck: "false", style: { maxWidth: "320px" },
        oninput: (e) => { table = e.target.value.trim(); tIn.classList.toggle("invalid", !/^[a-z_][a-z0-9_]{0,62}$/.test(table)); checkExists(); } });
      destCard.replaceChildren(
        h("div", { class: "card-head" }, h("h3", null, icon("database", 18), "Where it goes")),
        h("div", { class: "field" }, h("label", null, "Warehouse table"),
          h("div", { class: "row wrap" }, h("code", { class: "chip outline" }, "landing ."), tIn, existsEl),
          h("div", { class: "hint" }, "Lower-case letters, digits and _. The landing schema is where raw uploads live; dbt models build on top of it.")),
        h("div", { class: "field" }, h("label", null, "If the table already exists"), modeHost));
      drawExists();
    }
    let canLoad = false;
    function drawBar(ok) {
      if (ok !== undefined) canLoad = ok;
      const n = cols.filter((c) => c.include).length;
      const go = h("button", { class: "btn primary", disabled: !canLoad || !/^[a-z_][a-z0-9_]{0,62}$/.test(table) || null }, icon("database", 16), "Load into warehouse");
      go.onclick = () => BSP.busy(go, startLoad);
      bar.style.display = "";
      bar.replaceChildren(h("div", { class: "io-ab-sum" }, h("b", null, `${n} column${n === 1 ? "" : "s"}`),
        info && info.total_rows != null ? ` · ${Number(info.total_rows).toLocaleString()} rows` : "", " → ", h("code", null, `landing.${table || "…"}`),
        h("span", { class: "caption" }, mode === "append" ? "  (add rows)" : exists ? "  (replace)" : "")), h("span", { class: "spacer" }), go);
    }
    async function startLoad() {
      if (exists && mode === "replace") {
        const ok = await BSP.confirm({ title: `Replace landing.${table}?`, okText: "Replace", danger: true,
          body: "The current contents of this table will be replaced by this file. Anything built on it (dbt models, dashboards) will see the new data." });
        if (!ok) return;
      }
      result.replaceChildren(h("div", { class: "card io-hero" }, h("div", { class: "io-hero-ic run" }, h("span", { class: "io-spin lg" })), h("h2", null, "Loading…"),
        h("div", { class: "caption" }, "You can keep working — progress is also shown in the job panel.")));
      let r;
      try {
        r = await api(A("/load"), { body: { root: src.root, path: src.path, table, mode, options: opts,
          columns: cols.map((c) => ({ name: c.name, target: c.target, type: c.type, include: c.include })) } });
      } catch (e) { return result.replaceChildren(errBox("Can't start the load", e.message)); }
      bar.style.display = "none";
      result.scrollIntoView({ behavior: "smooth", block: "center" });
      const s = await WB.watchJob(r.job, { base: BASE, hint: false });
      if (s.status === "succeeded") { result.replaceChildren(doneCard(s.result)); exists = true; drawExists(); bar.style.display = "none"; }
      else {
        result.replaceChildren(h("div", { class: "card io-hero" }, h("div", { class: "io-hero-ic err" }, icon("error", 28)), h("h2", null, s.status === "cancelled" ? "Load cancelled" : "The load didn't finish"),
          h("div", { class: "io-errmsg" }, s.error || ""), h("div", { class: "row", style: { justifyContent: "center", marginTop: "14px" } },
            h("button", { class: "btn secondary", onclick: () => WB.jobDrawer(BASE, s) }, icon("notes", 16), "See the log"),
            h("button", { class: "btn primary", onclick: () => { bar.style.display = ""; result.replaceChildren(); } }, "Try again"))));
      }
    }
    inspect();
  }

  function doneCard(res) {
    const full = res.table;
    const [schema, name] = full.split(".");
    return h("div", { class: "card io-hero fade-in" },
      h("div", { class: "io-hero-ic ok" }, icon("tick-circle", 30)),
      h("h2", null, `${Number(res.rows || 0).toLocaleString()} rows in ${full}`),
      h("div", { class: "caption" }, res.mode === "append" ? "Rows were added to the table." : "The table is ready to query."),
      h("div", { class: "row wrap", style: { justifyContent: "center", marginTop: "16px" } },
        h("button", { class: "btn primary", onclick: () => { state.tab = "out"; state.params = { schema, table: name }; writeHash(state.params); render(); } }, icon("search", 16), "Query it"),
        h("button", { class: "btn secondary", onclick: () => { state.tab = "warehouse"; state.params = { schema, table: name }; writeHash(state.params); render(); } }, icon("database", 16), "See it in the warehouse"),
        h("a", { class: "btn ghost", href: "/superset/sqllab/", target: "_blank", rel: "noopener" }, icon("open-in-new", 16), "Superset SQL Lab"),
        h("button", { class: "btn ghost", onclick: () => { state.tab = "in"; state.params = {}; writeHash(); render(); } }, icon("add", 16), "Load something else")),
      h("div", { style: { textAlign: "left", marginTop: "20px" } }, snippet("Use it in a notebook", pyRead(full))));
  }

  // ═════════════════════════════ TAKE DATA OUT ════════════════════════
  function renderOut() {
    body.append(warehouseDown() || "");
    const tree = h("div", { class: "io-tree", role: "tree" });
    const filter = h("input", { class: "input", type: "search", placeholder: "Find a table", "aria-label": "Find a table" });
    const cat = h("div", { class: "card io-catcard" }, h("div", { class: "io-cat" },
      h("div", { class: "io-cat-head" }, h("span", { class: "label" }, "Warehouse"), h("button", { class: "btn ghost icon sm", "aria-label": "Refresh", onclick: () => loadTree() }, icon("refresh", 14))),
      h("div", { class: "search" }, icon("search", 16), filter), tree));
    const ta = h("textarea", { class: "textarea mono io-sql", spellcheck: "false", placeholder: "SELECT * FROM gold.some_table LIMIT 100", "aria-label": "SQL query" });
    const runBtn = h("button", { class: "btn primary" }, icon("play", 16), "Run", h("span", { class: "kbd io-kbd" }, navigator.platform.includes("Mac") ? "⌘↵" : "Ctrl↵"));
    const resHost = h("div");
    const sqlCard = h("div", { class: "card" }, h("div", { class: "card-head" }, h("div", null, h("h3", null, "Query"),
      h("div", { class: "caption" }, "Read-only: SELECT / WITH queries. Pick a table on the left to start from it.")), runBtn), ta, resHost);
    let fmt = "csv", dest = "download";
    const nameIn = h("input", { class: "input", placeholder: "file name", "aria-label": "File name", style: { maxWidth: "260px" } });
    const expBtn = h("button", { class: "btn primary" }, icon("download", 16), "Export");
    const expOut = h("div");
    const destSeg = h("span");
    const drawDest = () => destSeg.replaceChildren(seg([["download", "Download to my computer"], ["lake", "Save to Shared lake › exports"]], dest, (v) => { dest = v; }, "Destination"));
    drawDest();
    const expCard = h("div", { class: "card io-export" },
      h("div", { class: "card-head" }, h("div", null, h("h3", null, "Export the full result"), h("div", { class: "caption" }, `All rows of the query above (up to ${Number((state.status && state.status.limits.export_max_rows) || 5000000).toLocaleString()}).`))),
      h("div", { class: "io-exgrid" },
        h("div", { class: "field" }, h("label", null, "Format"), seg([["csv", "CSV"], ["excel", "Excel"], ["parquet", "Parquet"]], fmt, (v) => { fmt = v; }, "Format")),
        h("div", { class: "field" }, h("label", null, "Where"), destSeg),
        h("div", { class: "field" }, h("label", null, "File name"), nameIn)),
      h("div", { class: "row io-saverow" }, h("span", { class: "spacer", style: { flex: 1 } }), expBtn), expOut);
    body.append(h("div", { class: "io-split" }, cat, h("div", { class: "stack lg" }, sqlCard, expCard)));

    let lastTable = null;
    async function run() {
      resHost.replaceChildren(h("div", { class: "row", style: { marginTop: "12px" } }, h("span", { class: "io-spin" }), h("span", { class: "caption" }, "Running…")));
      const t0 = performance.now();
      try {
        const r = await api(A("/query"), { body: { sql: ta.value, limit: 200 } });
        const ms = Math.round(performance.now() - t0);
        resHost.replaceChildren(h("div", { class: "row between", style: { margin: "12px 0 8px" } },
          h("span", { class: "caption" }, r.truncated ? `Showing the first ${r.rows.length} rows · export to get them all` : `${r.rows.length} row${r.rows.length === 1 ? "" : "s"}`, ` · ${ms} ms`)),
          grid(r.columns, r.rows, { empty: "The query returned no rows." }));
      } catch (e) { resHost.replaceChildren(h("div", { style: { marginTop: "12px" } }, errBox("Query failed", e.message))); }
    }
    runBtn.onclick = () => BSP.busy(runBtn, run);
    ta.addEventListener("keydown", (e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); runBtn.click(); } });
    expBtn.onclick = () => BSP.busy(expBtn, async () => {
      const body_ = { sql: ta.value, format: fmt, destination: dest, filename: nameIn.value.trim() || lastTable || null };
      let r;
      try { r = await api(A("/export"), { body: body_ }); }
      catch (e) {
        if (e.status === 409) {
          const ok = await BSP.confirm({ title: "Replace the existing export?", body: e.message, okText: "Replace" });
          if (!ok) return;
          try { r = await api(A("/export"), { body: { ...body_, overwrite: true } }); } catch (e2) { return expOut.replaceChildren(errBox("Export failed", e2.message)); }
        } else return expOut.replaceChildren(h("div", { style: { marginTop: "12px" } }, errBox("Export failed", e.message)));
      }
      expOut.replaceChildren();
      const s = await WB.watchJob(r.job, { base: BASE, hint: false });
      if (s.status !== "succeeded") return expOut.replaceChildren(h("div", { style: { marginTop: "12px" } }, errBox("Export failed", s.error || "see the job log")));
      const res = s.result;
      if (res.token) {
        const url = A(`/export/${res.token}`);
        WB.download(url);
        expOut.replaceChildren(h("div", { class: "alert success", style: { marginTop: "12px" } }, icon("tick-circle", 18),
          h("div", null, h("div", { class: "title" }, `${Number(res.rows).toLocaleString()} rows exported`), "Your download should start automatically. ",
            h("a", { href: url }, `Download ${res.name} again`), h("span", { class: "caption" }, " (available for a few hours)"))));
      } else {
        expOut.replaceChildren(h("div", { class: "alert success", style: { marginTop: "12px" } }, icon("tick-circle", 18),
          h("div", null, h("div", { class: "title" }, `${Number(res.rows).toLocaleString()} rows saved to Shared lake › ${res.path}`),
            h("a", { href: "/files/#/lake/exports" }, "Open in Files"), " · in notebooks: ", h("code", null, `/data/lake/${res.path}`))));
      }
    });

    let all = [];
    async function loadTree() {
      tree.replaceChildren(...[1, 2, 3, 4].map(() => h("div", { class: "io-node-skel" }, h("div", { class: "skeleton", style: { width: "70%", height: "12px" } }))));
      try { all = (await api(A("/warehouse/tables"))).tables; } catch (e) { tree.replaceChildren(h("div", { class: "io-node-msg" }, e.message)); return; }
      drawTree();
    }
    const openSchemas = new Set(["landing", "gold"]);
    function drawTree() {
      const f = filter.value.trim().toLowerCase();
      const by = {};
      all.filter((t) => !f || t.name.toLowerCase().includes(f) || t.schema.toLowerCase().includes(f)).forEach((t) => (by[t.schema] = by[t.schema] || []).push(t));
      const nodes = [];
      Object.keys(by).sort().forEach((s) => {
        const isOpen = f || openSchemas.has(s);
        nodes.push(h("button", { class: `io-node ${isOpen ? "open" : ""}`, role: "treeitem", "aria-expanded": String(!!isOpen), onclick: () => { openSchemas.has(s) ? openSchemas.delete(s) : openSchemas.add(s); drawTree(); } },
          h("span", { class: "chev" }, icon("chevron-right", 12)), icon("database", 14), h("span", { class: "nm" }, s), h("span", { class: "count" }, String(by[s].length))));
        if (isOpen) by[s].forEach((t) => nodes.push(h("button", { class: "io-node", role: "treeitem", style: { paddingLeft: "28px" }, title: `${t.kind}${t.approx_rows != null ? " · ~" + Number(t.approx_rows).toLocaleString() + " rows" : ""}`,
          onclick: () => useTable(t.schema, t.name) }, icon(t.kind === "view" ? "visibility" : "file-table", 14), h("span", { class: "nm" }, t.name))));
      });
      tree.replaceChildren(...(nodes.length ? nodes : [h("div", { class: "io-node-msg" }, f ? "No match." : "The warehouse is empty — bring some data in first.")]));
    }
    filter.addEventListener("input", BSP.debounce(drawTree, 120));
    function useTable(schema, name) {
      ta.value = `SELECT *\nFROM ${tableRef(schema, name)}\nLIMIT 1000`;
      lastTable = name;
      if (!nameIn.value) nameIn.placeholder = name;
      runBtn.click();
    }
    loadTree();
    if (state.params.schema && state.params.table) useTable(state.params.schema, state.params.table);
    else if (state.params.sql) { ta.value = state.params.sql; }
  }

  // ═════════════════════════════ WAREHOUSE ════════════════════════════
  function renderWarehouse() {
    body.append(warehouseDown() || "");
    const schemasEl = h("div", { class: "row wrap", style: { marginBottom: "14px" } });
    const holder = h("div", { class: "card" }, WB.skeletonRows(6));
    body.append(schemasEl, holder);
    let schema = state.params.schema || "";
    let tablesAll = [];
    async function load() {
      let sch;
      try { [sch, tablesAll] = await Promise.all([api(A("/warehouse/schemas")).then((r) => r.schemas), api(A("/warehouse/tables")).then((r) => r.tables)]); }
      catch (e) { holder.replaceChildren(errBox("Can't read the warehouse", e.message)); return; }
      const chips = [h("button", { class: `chip ${schema ? "outline" : "primary"}`, onclick: () => { schema = ""; draw(); load2(); } }, "All schemas", h("span", { class: "count" }, String(tablesAll.length)))]
        .concat(sch.map((s) => h("button", { class: `chip ${schema === s.schema ? "primary" : "outline"}`, title: s.bytes ? BSP.fmtBytes(s.bytes) : "", onclick: () => { schema = s.schema; draw(); load2(); } },
          s.schema, h("span", { class: "count" }, String(s.tables + s.views)))));
      function draw() { schemasEl.replaceChildren(...chips); chips.forEach((c, i) => c.className = `chip ${(i === 0 ? !schema : sch[i - 1].schema === schema) ? "primary" : "outline"}`); }
      draw(); load2();
    }
    let t = null;
    function load2() {
      const rows = schema ? tablesAll.filter((x) => x.schema === schema) : tablesAll;
      const filter = h("input", { class: "input", type: "search", placeholder: "Filter tables", "aria-label": "Filter tables" });
      t = BSP.table({ rows, pageSize: 50, rowKey: (r) => `${r.schema}.${r.name}`, onRowClick: (r) => detail(r.schema, r.name, r),
        empty: WB.empty({ ic: "database", title: schema ? `Nothing in ${schema} yet` : "The warehouse is empty", body: "Bring data in from a file, BigQuery, Trino or the sample dataset.",
          actions: [h("button", { class: "btn primary sm", onclick: () => { state.tab = "in"; state.params = {}; writeHash(); render(); } }, icon("upload", 14), "Import data")] }),
        initialSort: { key: "schema", dir: 1 },
        columns: [
          { key: "name", label: "Table", sort: true, render: (r) => h("span", { class: "obj-name" }, h("span", { class: `fic ${r.kind === "view" ? "" : "table"}` }, icon(r.kind === "view" ? "visibility" : "file-table", 15)), h("span", { class: "nm" }, r.name)) },
          { key: "schema", label: "Schema", sort: true, render: (r) => h("span", { class: "chip xs outline" }, r.schema) },
          { key: "kind", label: "Kind", sort: true },
          { key: "approx_rows", label: "Rows (approx.)", num: true, sort: true, render: (r) => (r.approx_rows != null && r.approx_rows >= 0 ? Number(r.approx_rows).toLocaleString() : "—") },
          { key: "bytes", label: "Size", num: true, sort: true, render: (r) => (r.bytes != null ? BSP.fmtBytes(r.bytes) : "") },
        ] });
      filter.addEventListener("input", BSP.debounce(() => { const f = filter.value.toLowerCase(); t.update(f ? rows.filter((r) => r.name.toLowerCase().includes(f)) : rows); }, 120));
      holder.replaceChildren(h("div", { class: "card-head" }, h("h3", null, schema || "All tables", h("span", { class: "count" }, String(rows.length))),
        h("div", { class: "wb-toolbar" }, h("div", { class: "search" }, icon("search", 16), filter), h("button", { class: "btn ghost icon sm", "aria-label": "Refresh", onclick: load }, icon("refresh", 16)))), t.el);
    }
    async function detail(sch, name, row) {
      const bodyEl = h("div", { class: "stack lg" }, WB.skeletonRows(4));
      const foot = [
        h("button", { class: "btn ghost", onclick: () => BSP.copy(`SELECT * FROM ${tableRef(sch, name)}`, "SQL") }, icon("copy", 16), "Copy SQL"),
        h("button", { class: "btn primary", onclick: () => { d.close(); state.tab = "out"; state.params = { schema: sch, table: name }; writeHash(state.params); render(); } }, icon("download", 16), "Query / export"),
      ];
      if (sch === "landing" && (!row || row.kind === "table")) foot.unshift(h("button", { class: "btn ghost", style: { color: "var(--bsp-text-error)", marginRight: "auto" }, onclick: () => drop(name) }, icon("delete", 16), "Drop table"));
      const d = BSP.drawer({ title: `${sch}.${name}`, subtitle: row ? `${row.kind}${row.bytes ? " · " + BSP.fmtBytes(row.bytes) : ""}` : "", wide: true, body: bodyEl, foot });
      let r;
      try { r = await api(A(`/warehouse/table?${q({ schema: sch, table: name, rows: 50 })}`)); }
      catch (e) { return bodyEl.replaceChildren(errBox("Can't read this table", e.message)); }
      bodyEl.replaceChildren(
        h("div", { class: "row wrap" }, h("span", { class: "chip" }, `${r.columns.length} columns`),
          h("span", { class: "chip" }, r.row_count != null ? `${Number(r.row_count).toLocaleString()} rows` : row && row.approx_rows != null ? `~${Number(row.approx_rows).toLocaleString()} rows` : "row count unavailable")),
        h("div", null, h("h4", { style: { marginBottom: "8px" } }, "Columns"), h("div", { class: "table-wrap", style: { maxHeight: "260px" } }, h("table", { class: "table" },
          h("thead", null, h("tr", null, h("th", null, "Name"), h("th", null, "Type"), h("th", null, "Nullable"))),
          h("tbody", null, r.columns.map((c) => h("tr", null, h("td", { class: "mono" }, c.name), h("td", { class: "caption mono" }, c.type), h("td", { class: "caption" }, c.nullable ? "yes" : "no"))))))),
        h("div", null, h("h4", { style: { marginBottom: "8px" } }, `First ${r.rows.length} rows`), grid(r.columns.map((c) => ({ name: c.name, type: c.type })), r.rows, { empty: "This table is empty." })),
        snippet("Use it in a notebook", pyRead(`${tableRef(sch, name)}`)));
    }
    async function drop(name) {
      const ok = await BSP.confirm({ title: `Drop landing.${name}?`, danger: true, okText: "Drop table",
        body: "The table and its data are deleted from the warehouse. The original file (if any) stays in the shared lake. This can't be undone." });
      if (!ok) return;
      try { await api(A(`/warehouse/landing/${encodeURIComponent(name)}`), { method: "DELETE" }); BSP.toast(`Dropped landing.${name}`, "success"); document.querySelectorAll(".drawer.open .drawer-head .btn").forEach((b) => b.click()); load(); }
      catch (e) { BSP.toast(e.message, "error"); }
    }
    load();
    if (state.params.schema && state.params.table) detail(state.params.schema, state.params.table, null);
  }

  // ═════════════════════════════ ACTIVITY ═════════════════════════════
  function renderActivity() {
    const holder = h("div", { class: "card" }, WB.skeletonRows(6));
    body.append(holder);
    let t = null;
    async function load() {
      let r;
      try { r = await api(A("/jobs?queue=ingress,data&limit=100")); } catch (e) { holder.replaceChildren(errBox("Couldn't load activity", e.message)); return; }
      if (!t) {
        t = BSP.table({ rows: r.jobs, pageSize: 25, rowKey: "id", onRowClick: (j) => (j.status === "running" || j.status === "queued") ? WB.watchJob(j, { base: BASE }) : WB.jobDrawer(BASE, j),
          empty: WB.empty({ ic: "history", title: "No activity yet", body: "Loads, downloads and exports show up here with their full logs." }),
          columns: [
            { key: "dir", label: "", render: (j) => { const out = (j.meta || {}).direction === "out"; return h("span", { class: `io-dir ${out ? "out" : "in"}`, title: out ? "Out" : "In" }, icon(out ? "download" : "upload", 14)); } },
            { key: "title", label: "What", sort: true, render: (j) => h("div", { class: "pk-name" }, h("span", { class: "nm" }, j.title), j.error ? h("span", { class: "sum", style: { color: "var(--bsp-text-error)" } }, j.error) : j.result && j.result.rows != null ? h("span", { class: "sum" }, `${Number(j.result.rows).toLocaleString()} rows`) : null) },
            { key: "status", label: "Status", sort: true, render: (j) => WB.statusBadge(j.status) },
            { key: "created", label: "When", sort: (j) => -(j.created || 0), render: (j) => h("span", { title: BSP.fmtTime((j.started || j.created) * 1000) }, BSP.fmtAgo((j.started || j.created) * 1000)) },
            { key: "dur", label: "Took", num: true, render: (j) => (j.finished && j.started ? WB.fmtDur(j.finished - j.started) : j.status === "running" ? "…" : "") },
          ] });
        holder.replaceChildren(h("div", { class: "card-head" }, h("h3", null, "Activity"), h("button", { class: "btn ghost icon sm", "aria-label": "Refresh", onclick: load }, icon("refresh", 16))), t.el);
      } else t.update(r.jobs);
    }
    load();
    const iv = setInterval(() => { if (!document.hidden) load(); }, 5000);
    cleanup.push(() => clearInterval(iv));
  }

  // ── boot ───────────────────────────────────────────────────────────
  window.addEventListener("hashchange", () => { readHash(); render(); });
  renderHealth();
  render();
  loadStatus().then(() => {
    // the source tiles and warnings depend on the status; other views keep their state
    if (state.tab === "in" && !state.params.path) render();
    else { const w = warehouseDown(); if (w) body.prepend(w); }
  });
  WB.resume(BASE, ["ingress", "data"]);
})();
