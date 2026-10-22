/* AI harness: Federated SQL tab. Catalog browser with cascading searchable selects, catalog refresh jobs,
   SQL editor (run / explain / format / re-plan), Ask in English, saved queries and collections.
   Vanilla JS on window.BSP; registers itself in window.BSP_AI.views (see app.js). Result rows stay in memory only. */
(function () {
  "use strict";
  if (!window.BSP || !window.BSP_AI) return;
  const { h, esc, toast } = BSP;
  const { views, api, sse, store } = window.BSP_AI;
  const $ = (s, r) => (r || document).querySelector(s);
  const icon = (n, s) => BSP.icon(n, s || 15);

  // Star icons (Ant Design StarOutlined / StarFilled, MIT) registered in the kit's icon table at runtime.
  const STAR = "M908.1 353.1l-253.9-36.9L540.7 86.1c-3.1-6.3-8.2-11.4-14.5-14.5-15.8-7.8-35-1.3-42.9 14.5L369.8 316.2l-253.9 36.9c-7 1-13.4 4.3-18.3 9.3a32.05 32.05 0 00.6 45.3l183.7 179.1-43.4 252.9a31.95 31.95 0 0046.4 33.7L512 754l227.1 119.4c6.2 3.3 13.4 4.4 20.3 3.2 17.4-3 29.1-19.5 26.1-36.9l-43.4-252.9 183.7-179.1c5-4.9 8.3-11.3 9.3-18.3 2.7-17.5-9.5-33.7-27-36.3z";
  BSP.ICONS["star-filled"] = `<path d="${STAR}"/>`;
  BSP.ICONS["star-outlined"] = `<path d="${STAR}M664.8 561.6l36.1 210.3L512 672.7 323.1 772l36.1-210.3-152.8-149L417.6 382 512 190.7 606.4 382l211.2 30.7-152.8 148.9z" fill-rule="evenodd"/>`;

  const ago = (iso) => (iso ? BSP.fmtAgo(iso) : "");
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

  // ── searchable select (single or multi) ─────────────────────────────────────
  // options: [{value, label, sub, group, badges: [[cls, text]]}]
  function combo({ placeholder, multi, onChange, emptyText, searchText }) {
    let options = [], value = multi ? [] : null, open = false, act = -1, shown = [], disabled = false, loading = false;
    const valEl = h("span", { class: "cb-val" });
    const trigger = h("button", { type: "button", class: "input cb-trigger", "aria-haspopup": "listbox", "aria-expanded": "false" },
      valEl, h("span", { class: "cb-caret" }, icon("chevron-down", 13)));
    const search = h("input", { class: "input cb-search", placeholder: searchText || "Search", "aria-label": "Search options" });
    const list = h("div", { class: "cb-list", role: "listbox", "aria-multiselectable": multi ? "true" : null });
    const acts = multi ? h("div", { class: "cb-acts" },
      h("button", { type: "button", class: "link-btn", onclick: () => { const add = shown.map((o) => o.value); set([...new Set([...value, ...add])], true); } }, "Select shown"),
      h("button", { type: "button", class: "link-btn", onclick: () => set([], true) }, "Clear")) : null;
    const pop = h("div", { class: "cb-pop", hidden: true }, search, acts, list);
    const el = h("div", { class: "cb" + (multi ? " multi" : "") }, trigger, pop);
    const has = (v) => (multi ? value.includes(v) : value === v);
    const byVal = (v) => options.find((o) => o.value === v);

    function renderValue() {
      valEl.replaceChildren();
      if (loading) { valEl.append(h("span", { class: "ph" }, "Loading…")); return; }
      if (multi) {
        if (!value.length) { valEl.append(h("span", { class: "ph" }, placeholder || "Select…")); return; }
        value.slice(0, 4).forEach((v) => {
          const o = byVal(v);
          const x = h("button", { type: "button", "aria-label": `Remove ${o ? o.label : v}`, onclick: (e) => { e.stopPropagation(); set(value.filter((y) => y !== v), true); } }, icon("close", 11));
          valEl.append(h("span", { class: "chip primary", title: o ? o.label : v }, h("span", null, o ? o.label : v), x));
        });
        if (value.length > 4) valEl.append(h("span", { class: "chip" }, `+${value.length - 4}`));
      } else {
        const o = byVal(value);
        valEl.append(o ? h("span", { class: "one" }, o.label) : h("span", { class: "ph" }, placeholder || "Select…"));
      }
    }
    function renderList() {
      const q = search.value.trim().toLowerCase();
      const words = q.split(/\s+/).filter(Boolean);
      const match = (o) => !words.length || words.every((w) => `${o.label} ${o.sub || ""} ${o.group || ""} ${(o.badges || []).map((b) => b[1]).join(" ")}`.toLowerCase().includes(w));
      const all = options.filter(match);
      shown = all.slice(0, 400);
      if (act >= shown.length) act = shown.length - 1;
      list.replaceChildren();
      if (!shown.length) { list.append(h("div", { class: "cb-empty" }, options.length ? "No matches" : (emptyText || "Nothing to choose yet"))); return; }
      let grp = null;
      shown.forEach((o, i) => {
        if (o.group && o.group !== grp) { grp = o.group; list.append(h("div", { class: "cb-grp" }, grp)); }
        const row = h("div", { class: `cb-opt${has(o.value) ? " sel" : ""}${i === act ? " act" : ""}`, role: "option", "aria-selected": has(o.value) ? "true" : "false",
          onmousedown: (e) => e.preventDefault(), onclick: () => pick(o) },
          multi ? h("span", { class: "ck" }, has(o.value) ? icon("check", 11) : null) : null,
          h("span", { class: "lb", title: o.label }, o.label),
          h("span", { class: "mt" }, ...(o.badges || []).map((b) => h("span", { class: "mini " + b[0] }, b[1]))),
          o.sub ? h("span", { class: "sb", title: o.sub }, o.sub) : null);
        list.append(row);
      });
      if (all.length > shown.length) list.append(h("div", { class: "cb-empty" }, `${all.length - shown.length} more, type to narrow`));
      const a = list.querySelector(".cb-opt.act"); if (a) a.scrollIntoView({ block: "nearest" });
    }
    function pick(o) {
      if (multi) set(has(o.value) ? value.filter((v) => v !== o.value) : [...value, o.value], true);
      else { set(o.value, true); close(); }
    }
    function set(v, fire) {
      value = multi ? [...(v || [])] : v;
      renderValue(); if (open) renderList();
      if (fire && onChange) onChange(value);
    }
    function openPop() {
      if (disabled || open) return;
      document.querySelectorAll(".cb-pop:not([hidden])").forEach((p) => { if (p !== pop) p.dispatchEvent(new Event("cb-close")); });
      open = true; pop.hidden = false; trigger.setAttribute("aria-expanded", "true");
      search.value = ""; act = -1; renderList(); setTimeout(() => search.focus(), 0);
      document.addEventListener("mousedown", outside, true);
    }
    function close() {
      if (!open) return;
      open = false; pop.hidden = true; trigger.setAttribute("aria-expanded", "false");
      document.removeEventListener("mousedown", outside, true);
    }
    function outside(e) { if (!el.contains(e.target)) close(); }
    pop.addEventListener("cb-close", close);
    trigger.addEventListener("click", () => (open ? close() : openPop()));
    trigger.addEventListener("keydown", (e) => { if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") { e.preventDefault(); openPop(); } });
    search.addEventListener("input", () => { act = 0; renderList(); });
    search.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") { e.preventDefault(); act = Math.min(shown.length - 1, act + 1); renderList(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); act = Math.max(0, act - 1); renderList(); }
      else if (e.key === "Enter") { e.preventDefault(); if (shown[act]) pick(shown[act]); }
      else if (e.key === "Escape") { e.preventDefault(); close(); trigger.focus(); }
      else if (e.key === "Tab") close();
    });
    renderValue();
    return {
      el,
      setOptions(opts) { options = opts || []; if (multi) value = value.filter((v) => options.some((o) => o.value === v)); else if (value != null && !byVal(value)) value = null; renderValue(); if (open) renderList(); },
      get value() { return value; },
      set(v, fire) { set(v, fire); },
      loading(on) { loading = !!on; renderValue(); },
      disable(on) { disabled = !!on; trigger.disabled = disabled; if (on) close(); },
      open() { openPop(); },
      options() { return options; },
    };
  }

  // ── tiny SQL formatter (keeps strings, quoted names and comments intact) ────
  const KW = ["select", "from", "where", "group", "by", "order", "having", "limit", "offset", "join", "left", "right", "inner", "outer", "full", "cross",
    "on", "and", "or", "not", "in", "is", "null", "as", "with", "union", "all", "distinct", "case", "when", "then", "else", "end", "between", "like",
    "exists", "asc", "desc", "count", "sum", "avg", "min", "max", "filter", "over", "partition", "using", "cast", "coalesce", "round", "date_trunc", "true", "false", "interval", "except", "intersect", "values"];
  const CLAUSES = ["SELECT", "FROM", "WHERE", "GROUP BY", "ORDER BY", "HAVING", "LIMIT", "OFFSET", "UNION", "UNION ALL", "EXCEPT", "INTERSECT", "WITH",
    "JOIN", "LEFT JOIN", "RIGHT JOIN", "INNER JOIN", "FULL JOIN", "CROSS JOIN", "LEFT OUTER JOIN", "RIGHT OUTER JOIN", "FULL OUTER JOIN"];
  const NOSPACE_KEEP = ["IN", "AS", "ON", "AND", "OR", "NOT", "EXISTS", "USING", "OVER", "FILTER", "VALUES", "THEN", "ELSE", "WHEN", "BETWEEN", "IS", "LIKE"];
  function formatSql(sql) {
    const toks = sql.match(/'(?:[^']|'')*'|"(?:[^"]|"")*"|--[^\n]*|\/\*[\s\S]*?\*\/|\s+|[A-Za-z_][A-Za-z0-9_$]*|\d+(?:\.\d+)?|<>|!=|<=|>=|\|\||::|./g) || [];
    const words = [];
    toks.forEach((t) => {
      if (/^\s+$/.test(t)) return;
      if (/^[A-Za-z_]/.test(t) && KW.includes(t.toLowerCase())) t = t.toUpperCase();
      words.push(t);
    });
    // merge multi-word clauses
    const merged = [];
    for (let i = 0; i < words.length; i++) {
      const w = words[i];
      const two = w + " " + (words[i + 1] || ""), three = two + " " + (words[i + 2] || "");
      if (CLAUSES.includes(three)) { merged.push(three); i += 2; } else if (CLAUSES.includes(two) && (words[i + 1] === "BY" || words[i + 1] === "JOIN" || words[i + 1] === "ALL")) { merged.push(two); i += 1; } else merged.push(w);
    }
    let out = "", depth = 0;
    const stack = [];   // for each "(": is it a subquery?
    let clause = "";
    const ind = () => "  ".repeat(depth);
    merged.forEach((w, i) => {
      const prev = merged[i - 1];
      if (CLAUSES.includes(w)) {
        out = out.replace(/[ ]+$/, "");
        if (out && !out.endsWith("\n") && !out.endsWith("(\n" + ind())) out += "\n" + ind();
        else if (out.endsWith("\n")) out += ind();
        out += w; clause = w;
        out += (w === "SELECT" || w === "GROUP BY" || w === "ORDER BY") ? "\n" + ind() + "  " : " ";
        return;
      }
      if (w === "(") {
        const sub = merged[i + 1] === "SELECT" || merged[i + 1] === "WITH";
        stack.push({ sub, clause });
        if (prev && /^[A-Za-z_]/.test(prev) && !NOSPACE_KEEP.includes(prev.toUpperCase()) && !CLAUSES.includes(prev)) out = out.replace(/ $/, "");
        out += "(";
        if (sub) { depth++; out += "\n" + ind(); }
        return;
      }
      if (w === ")") {
        const top = stack.pop() || {};
        if (top.sub) { depth = Math.max(0, depth - 1); out = out.replace(/\s+$/, ""); out += "\n" + ind() + ")"; clause = top.clause || ""; }
        else { out = out.replace(/ $/, ""); out += ")"; }
        out += " ";
        return;
      }
      if (w === ",") {
        out = out.replace(/ $/, "");
        const top = stack[stack.length - 1];
        if ((!top || top.sub) && (clause === "SELECT" || clause === "GROUP BY" || clause === "ORDER BY")) out += ",\n" + ind() + "  ";
        else if ((!top || top.sub) && clause === "WITH") out += ",\n" + ind();
        else out += ", ";
        return;
      }
      if ((w === "AND" || w === "OR") && (clause === "WHERE" || clause === "HAVING" || clause.endsWith("JOIN")) && (!stack.length || stack[stack.length - 1].sub)) {
        out = out.replace(/ $/, ""); out += "\n" + ind() + "  " + w + " "; return;
      }
      if (w === "." || prev === ".") { out = out.replace(/ $/, ""); out += w; if (w !== ".") out += " "; return; }
      if (w.startsWith("--")) { out += w + "\n" + ind(); return; }
      out += w + " ";
    });
    return out.replace(/[ \t]+\n/g, "\n").replace(/\(\s+\)/g, "()").trim();
  }

  function download(name, obj) {
    const a = h("a", { href: URL.createObjectURL(new Blob([JSON.stringify(obj, null, 2)], { type: "application/json" })), download: name });
    document.body.appendChild(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  }

  // ══ view ═════════════════════════════════════════════════════════════════
  views.sql = function (root) {
    const prefs = store.get("fsql", { sys: "warehouse", schema: "marts", tables: [], terms: [], cap: 200, useDefs: true, tab: "catalog", sql: "", question: "" });
    const st = { status: null, systems: [], schemas: [], tables: [], glossary: [], cols: {}, lib: null, current: null, question: prefs.question || "",
      job: null, jobTimer: null, lastPlan: null, filter: "all", openCols: new Set(store.get("fsql-open", [])), alive: true };
    const save = () => store.set("fsql", prefs);
    let scopeKind = "schema", showLog = false;   // refresh scope and job log toggle (used while the panel is built)

    root.innerHTML = `
      <div class="bsp-page-head"><div><h1>Federated SQL</h1><div class="lede">One query across every connected system through Trino. Browse catalog definitions, generate SQL from English and keep favourite queries.</div></div>
        <div class="row"><span class="chip outline" id="be-chip">Catalog: checking…</span>
          <a class="btn ghost sm" href="/superset/sqllab/" target="_blank" rel="noopener" title="Superset SQL Lab has saved Federated queries on the Trino (federated) connection">${BSP.iconHtml("external", 14)}<span>Open in SQL Lab</span></a>
          <button class="btn secondary sm" id="trino-toggle" aria-pressed="false" title="Show the Trino monitor (Alt+Shift+M)">${BSP.iconHtml("monitor", 14)}<span>Trino monitor</span></button></div></div>
      <div class="fsql">
        <aside class="card tight fsql-side">
          <div class="segmented" id="side-tabs"><button data-tab="catalog">${BSP.iconHtml("catalog", 14)}Catalog</button><button data-tab="saved">${BSP.iconHtml("star-outlined", 14)}Saved</button><button data-tab="glossary">${BSP.iconHtml("book", 14)}Glossary</button></div>
          <div id="side-catalog" class="stack"></div>
          <div id="side-saved" class="stack" hidden></div>
          <div id="side-glossary" class="stack" hidden></div>
        </aside>
        <section class="fsql-main">
          <div id="jobstrip" hidden></div>
          <div class="card tight">
            <div class="fsql-ask"><input class="input" id="ask" placeholder="Ask in English, for example: open care orders on unpaid invoices by technology" value="${esc(st.question)}">
              <button class="btn secondary" id="ask-btn">${BSP.iconHtml("wand", 15)}<span>Generate SQL</span></button></div>
            <div class="fsql-askopts"><label class="checkbox"><input type="checkbox" id="use-defs" ${prefs.useDefs ? "checked" : ""}>Use selected tables and glossary terms as context</label><span class="caption" id="ask-ctx"></span><span class="row wrap" id="ask-terms" style="gap:4px"></span></div>
            <div class="fsql-bar">
              <div class="fsql-title"><button class="btn ghost icon sm star-btn" id="star" title="Star this query" aria-label="Star this query">${BSP.iconHtml("star-outlined", 16)}</button>
                <span class="t" id="qtitle">Unsaved query</span><span class="caption" id="qsrc"></span></div>
              <div class="row">
                <select class="select input" id="cap" title="Row cap" aria-label="Row cap">${[50, 100, 200, 500].map((n) => `<option value="${n}" ${n === prefs.cap ? "selected" : ""}>${n} rows max</option>`).join("")}</select>
                <button class="btn ghost sm" id="fmt" title="Format SQL">${BSP.iconHtml("edit", 14)}<span>Format</span></button>
                <button class="btn secondary sm" id="explain" title="Show how Trino plans the query">${BSP.iconHtml("branch", 14)}<span>Explain</span></button>
                <button class="btn secondary sm" id="replan" title="Explain again with refreshed definitions and compare">${BSP.iconHtml("sync", 14)}<span>Re-plan</span></button>
                <button class="btn secondary sm" id="save">${BSP.iconHtml("save", 14)}<span>Save</span></button>
                <button class="btn ghost icon sm" id="save-more" title="More save options" aria-label="More save options">${BSP.iconHtml("more", 15)}</button>
                <button class="btn primary sm" id="run">${BSP.iconHtml("play", 14)}<span>Run</span></button>
              </div>
            </div>
            <textarea class="textarea fsql-sql" id="sql" spellcheck="false" aria-label="SQL editor" placeholder="SELECT ... FROM catalog.schema.table"></textarea>
            <div class="caption" style="margin-top:6px">Ctrl+Enter or Cmd+Enter runs the query. Results are shown here only; saved queries keep the SQL, timing and row count, never the rows.</div>
          </div>
          <div id="agent"></div>
          <div id="plan"></div>
          <div id="result"></div>
        </section>
        <aside class="fsql-trino card tight" id="trino-pane" hidden aria-label="Trino monitor">
          <div class="fsql-trino-head"><b>Trino monitor</b><span class="caption" id="trino-state">Query list</span><span style="flex:1"></span>
            <a class="btn ghost sm" id="trino-open" href="/trino/ui/" target="_blank" rel="noopener">${BSP.iconHtml("external", 13)}<span>Open Trino monitor</span></a>
            <button class="btn ghost icon sm" id="trino-close" title="Hide (Alt+Shift+M)" aria-label="Hide Trino monitor">${BSP.iconHtml("close", 14)}</button></div>
          <div class="fsql-trino-frame" id="trino-frame"></div>
          <div class="alert warning fsql-note" id="trino-fallback" hidden>${BSP.iconHtml("warning", 15)}<div>The Trino web UI did not load here. Use Open Trino monitor, or start the Trino capability in Studio.</div></div>
        </aside>
      </div>`;
    const ed = $("#sql", root);
    ed.value = prefs.sql || "";

    // ── side tabs ──
    function showTab(t) {
      prefs.tab = t; save();
      root.querySelectorAll("#side-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === t));
      $("#side-catalog", root).hidden = t !== "catalog"; $("#side-saved", root).hidden = t !== "saved"; $("#side-glossary", root).hidden = t !== "glossary";
      if (t === "glossary" && !st.gl) loadGlossaryView();
    }
    $("#side-tabs", root).onclick = (e) => { const b = e.target.closest("button"); if (b) showTab(b.dataset.tab); };

    // ══ catalog panel ══════════════════════════════════════════════════════
    const cat = $("#side-catalog", root);
    const cSys = combo({ placeholder: "Choose a system", searchText: "Search systems", onChange: (v) => { prefs.sys = v; prefs.schema = null; prefs.tables = []; save(); loadSchemas(); } });
    const cSchema = combo({ placeholder: "Choose a schema", searchText: "Search schemas", emptyText: "Pick a system first", onChange: (v) => { prefs.schema = v; prefs.tables = []; save(); loadTables(); } });
    const cTerms = combo({ multi: true, placeholder: "Any term", searchText: "Search glossary", onChange: (v) => { prefs.terms = v; save(); applyTableFilter(); renderCards(); } });
    const cTables = combo({ multi: true, placeholder: "Choose tables", searchText: "Search tables, descriptions, tags", emptyText: "Pick a schema first", onChange: (v) => { prefs.tables = v; save(); loadColumns(); renderCards(); updateRefreshHints(); } });
    const cCols = combo({ multi: true, placeholder: "Optional: choose columns", searchText: "Search columns", emptyText: "Choose tables first", onChange: () => renderCards() });
    const field = (label, c, extra) => h("div", { class: "field" }, h("label", null, label, extra || null), c.el);
    const note = h("div", { class: "alert info fsql-note", hidden: true });
    const termsHint = h("span", { class: "caption" });
    const tablesHint = h("span", { class: "caption" });
    const cards = h("div", { class: "tcards" });
    const cardsHead = h("div", { class: "eyebrow" });
    cat.append(note, field("System", cSys), field("Schema", cSchema), field("Glossary terms", cTerms, termsHint), field("Tables", cTables, tablesHint), field("Columns", cCols),
      h("div", { class: "row wrap" },
        h("button", { class: "btn secondary sm", onclick: draftSelect, title: "Write a SELECT for the chosen tables and columns" }, icon("code", 14), h("span", null, "Draft SELECT")),
        h("button", { class: "btn ghost sm", onclick: insertNames, title: "Insert the chosen names at the cursor" }, icon("add", 14), h("span", null, "Insert names"))),
      cardsHead, cards, refreshBox());

    function sysOpts() {
      return st.systems.map((s) => ({ value: s.name, label: s.name, sub: [s.label, s.datasets != null ? plural(s.datasets, "dataset") + " in catalog" : null, s.refreshed ? "refreshed " + ago(s.refreshed) : null].filter(Boolean).join(" · "),
        group: s.label || "Other", badges: s.in_trino === false ? [["", "catalog only"]] : [] }));
    }
    async function loadStatus() {
      try { st.status = await api("/sql/catalog/status"); } catch (e) { st.status = { backend: "trino", message: "Catalog status unavailable.", can_annotate: false }; }
      const dh = st.status.backend === "datahub";
      $("#be-chip", root).className = "chip " + (dh ? "primary" : "outline");
      $("#be-chip", root).textContent = dh ? "Catalog: DataHub" : "Catalog: Trino structure";
      note.hidden = dh;
      note.replaceChildren(icon("info"), h("div", null, st.status.message));
      const ra = $("#ra", root); if (ra) { ra.disabled = !st.status.can_annotate; ra.closest("label").title = st.status.can_annotate ? "" : "Needs DataHub"; updateRefreshHints(); }
    }
    async function loadSystems() {
      cSys.loading(true);
      try { const r = await api("/sql/catalog/systems"); st.systems = r.systems || []; if (r.error) toast("Systems: " + r.error, "error"); }
      catch (e) { st.systems = []; toast(e.message, "error"); }
      cSys.loading(false); cSys.setOptions(sysOpts());
      if (prefs.sys && st.systems.some((s) => s.name === prefs.sys)) cSys.set(prefs.sys); else if (st.systems[0]) { prefs.sys = st.systems[0].name; cSys.set(prefs.sys); }
      await loadSchemas();
    }
    async function loadSchemas() {
      st.schemas = []; cSchema.setOptions([]); st.tables = []; cTables.setOptions([]); cCols.setOptions([]); renderCards();
      if (!prefs.sys) return;
      cSchema.loading(true);
      try { const r = await api("/sql/catalog/schemas?catalog=" + encodeURIComponent(prefs.sys)); st.schemas = r.schemas || []; if (r.error) toast(r.error, "error"); }
      catch (e) { toast(e.message, "error"); }
      cSchema.loading(false);
      cSchema.setOptions(st.schemas.map((s) => ({ value: s.name, label: s.name, sub: [s.datasets != null ? plural(s.datasets, "dataset") + " in catalog" : null, s.refreshed ? "refreshed " + ago(s.refreshed) : s.crawled ? "read " + ago(s.crawled) : null].filter(Boolean).join(" · "),
        badges: s.in_trino === false ? [["", "catalog only"]] : [] })));
      if (prefs.schema && st.schemas.some((s) => s.name === prefs.schema)) cSchema.set(prefs.schema);
      else if (st.schemas.length === 1) { prefs.schema = st.schemas[0].name; cSchema.set(prefs.schema); }
      else { prefs.schema = null; }
      save(); updateRefreshHints();
      if (prefs.schema) await loadTables();
    }
    async function loadTables(keep) {
      cTables.setOptions([]); cCols.setOptions([]); st.tables = []; renderCards();
      if (!prefs.sys || !prefs.schema) return;
      cTables.loading(true);
      cardsHead.textContent = ""; cards.replaceChildren(h("div", { class: "skeleton", style: "height:90px" }));
      try { const r = await api(`/sql/catalog/tables?catalog=${encodeURIComponent(prefs.sys)}&schema=${encodeURIComponent(prefs.schema)}`); st.tables = r.tables || []; if (r.error) toast(r.error, "error"); }
      catch (e) { toast(e.message, "error"); }
      cTables.loading(false);
      applyTableFilter();
      const want = (keep || prefs.tables || []).filter((t) => st.tables.some((x) => x.name === t));
      cTables.set(want); prefs.tables = want; save();
      st.cols = {}; await loadColumns(); renderCards(); updateRefreshHints();
    }
    function termLinked(t, terms) {
      if (!terms.length) return true;
      return terms.some((term) => (t.terms || []).includes(term) || ((st.glossary.find((g) => g.name === term) || {}).related_tables || []).includes(t.fqn));
    }
    function filteredTables() { return st.tables.filter((t) => termLinked(t, prefs.terms || [])); }
    function applyTableFilter() {
      const list = filteredTables();
      tablesHint.textContent = st.tables.length ? (list.length === st.tables.length ? plural(st.tables.length, "table") : `${list.length} of ${st.tables.length} match the terms`) : "";
      const keep = cTables.value || [];
      cTables.setOptions(list.map((t) => ({ value: t.name, label: t.name, sub: t.description || (t.column_count != null ? plural(t.column_count, "column") : ""),
        badges: [...(t.has_ban ? [["ban", "ban"]] : []), ...((t.tags || []).includes("PII (suspected)") ? [["pii", "PII?"]] : []), ...(t.ai_annotated ? [["ai", "AI"]] : [])] }))
        .concat(keep.filter((v) => !list.some((t) => t.name === v)).map((v) => ({ value: v, label: v, sub: "not linked to the chosen terms" }))));
      cTables.set(keep);
    }
    async function loadGlossary() {
      try { const r = await api("/sql/catalog/glossary"); st.glossary = r.terms || []; termsHint.textContent = r.source === "datahub" ? "" : "from name hints"; }
      catch (e) { st.glossary = []; }
      cTerms.setOptions(st.glossary.map((g) => ({ value: g.name, label: g.name, sub: g.definition || "", badges: (g.related_tables || []).length ? [["", String(g.related_tables.length)]] : [] })));
      cTerms.set((prefs.terms || []).filter((t) => st.glossary.some((g) => g.name === t)));
      applyTableFilter();
    }
    async function loadColumns() {
      const tabs = cTables.value || [];
      const keep = cCols.value || [];
      await Promise.all(tabs.filter((t) => !st.cols[t]).map(async (t) => {
        try { st.cols[t] = await api("/sql/catalog/columns?table=" + encodeURIComponent(`${prefs.sys}.${prefs.schema}.${t}`)); }
        catch (e) { st.cols[t] = { columns: [], error: e.message }; }
      }));
      if (!st.alive) return;
      cCols.setOptions(tabs.flatMap((t) => ((st.cols[t] || {}).columns || []).map((c) => ({ value: `${t}.${c.name}`, label: c.name, group: t,
        sub: [c.type, c.description].filter(Boolean).join(" · "), badges: [...(c.pii ? [["pii", "PII?"]] : []), ...((c.terms || []).slice(0, 2).map((x) => ["", x]))] }))));
      cCols.set(keep);
    }
    function tableByName(n) { return st.tables.find((t) => t.name === n); }
    function cardFor(t, pick) {
      const tags = (t.tags || []).filter((x) => x !== "PII (suspected)" && x !== "ai-annotated");
      const chips = [
        t.has_ban ? h("span", { class: "chip ban", title: "Has a ban column: joins to every customer table" }, "ban") : null,
        (t.tags || []).includes("PII (suspected)") ? h("span", { class: "chip pii", title: (t.pii_columns || []).length ? "Columns: " + t.pii_columns.join(", ") : "Personal data suspected from column names" }, icon("warning", 11), "PII (suspected)") : null,
        t.ai_annotated || (t.tags || []).includes("ai-annotated") ? h("span", { class: "chip ai", title: "Description suggested by AI" }, icon("sparkle", 11), "ai-annotated") : null,
        ...tags.map((x) => h("span", { class: "chip tag" }, x)),
        ...(t.terms || []).map((x) => h("span", { class: "chip term" + (t.terms_suggested ? " sug" : ""), title: t.terms_suggested ? "Suggested from names" : "Glossary term" }, x)),
      ].filter(Boolean);
      const when = [t.refreshed ? "Refreshed " + ago(t.refreshed) : "Not refreshed from here yet", t.crawled ? "structure read " + ago(t.crawled) : null, t.column_count != null ? plural(t.column_count, "column") : null, t.in_catalog ? "in DataHub" : null].filter(Boolean).join(" · ");
      const ins = h("button", { class: "btn ghost icon sm", title: "Insert name at cursor", "aria-label": `Insert ${t.fqn}`, onclick: (e) => { e.stopPropagation(); insertAt(t.fqn); } }, icon("add", 14));
      const card = h("div", { class: "tcard" + (pick ? " pick" : ""), title: pick ? "Add to chosen tables" : null },
        h("div", { class: "tcard-head" }, h("b", { title: t.fqn }, `${prefs.schema}.${t.name}`), ins),
        h("div", { class: "tcard-desc" + (t.description ? "" : " none") }, t.description || "No description yet."),
        chips.length ? h("div", { class: "tcard-tags" }, ...chips) : null,
        h("div", { class: "caption" }, when));
      if (pick) card.onclick = () => { const v = [...new Set([...(cTables.value || []), t.name])]; cTables.set(v, true); };
      return card;
    }
    function renderCards() {
      const sel = (cTables.value || []).map(tableByName).filter(Boolean);
      cards.replaceChildren();
      if (!prefs.schema) { cardsHead.textContent = ""; return; }
      if (sel.length) { cardsHead.textContent = "Chosen tables"; sel.forEach((t) => cards.append(cardFor(t, false))); return; }
      const list = filteredTables();
      cardsHead.textContent = list.length ? `Tables in ${prefs.schema}${list.length > 25 ? " (first 25)" : ""}` : "";
      if (!list.length) cards.append(h("div", { class: "caption" }, st.tables.length ? "No tables linked to the chosen terms here." : "No tables found."));
      list.slice(0, 25).forEach((t) => cards.append(cardFor(t, true)));
    }

    // ── refresh definitions ──
    // Scope follows the selection (tables > schema > system > all systems) until the user picks a segment.
    let scopeAuto = true;
    const need = { schema: "Pick a schema first", tables: "Pick tables first", catalog: "Pick a system first" };
    function autoKind() { return (cTables.value || []).length && prefs.schema ? "tables" : prefs.schema ? "schema" : prefs.sys ? "catalog" : "all"; }
    function refreshBox() {
      const seg = h("div", { class: "segmented", id: "scope-seg", role: "radiogroup", "aria-label": "Refresh scope" }, ...[["catalog", "System"], ["schema", "Schema"], ["tables", "Chosen tables"]].map(([k, l]) =>
        h("button", { type: "button", "data-k": k, role: "radio", onclick: () => pickScope(k) }, l)));
      return h("div", { class: "fsql-refresh" },
        h("div", { class: "eyebrow" }, "Refresh definitions"), seg, h("div", { class: "hint", id: "scope-need", style: "margin-left:0;margin-top:0", hidden: true }),
        h("label", { class: "checkbox" }, h("input", { type: "checkbox", id: "ra", onchange: updateRefreshHints }), "Re-annotate with AI"),
        h("div", { class: "hint", id: "ra-hint" }),
        h("label", { class: "checkbox" }, h("input", { type: "checkbox", id: "rf" }), "Overwrite AI suggestions"),
        h("div", { class: "hint" }, "Human-written descriptions are never changed."),
        h("div", { class: "row wrap" }, h("button", { class: "btn secondary sm", id: "refresh-btn", onclick: startRefresh }, icon("sync", 14), h("span", { id: "refresh-lbl" }, "Refresh definitions")),
          h("span", { class: "caption", id: "scope-txt" })),
        h("a", { class: "link-btn", href: "#sql/jobs", style: "font-size:12px" }, "Catalog jobs and history"));
    }
    function pickScope(k) {
      const missing = (k === "catalog" && !prefs.sys) || (k === "schema" && !prefs.schema) || (k === "tables" && !(cTables.value || []).length);
      const box = $("#scope-need", root);
      if (missing) {
        const want = k === "tables" && !prefs.schema ? "schema" : k;
        box.textContent = need[want]; box.hidden = false;
        const c = want === "catalog" ? cSys : want === "schema" ? cSchema : cTables;
        c.el.scrollIntoView({ block: "center", behavior: "smooth" }); c.open();
        scopeAuto = false; scopeKind = k; updateRefreshHints(); return;
      }
      box.hidden = true; scopeAuto = false; scopeKind = k; updateRefreshHints();
    }
    function effectiveKind() {
      if (scopeAuto) return autoKind();
      const ok = (scopeKind === "catalog" && prefs.sys) || (scopeKind === "schema" && prefs.schema) || (scopeKind === "tables" && (cTables.value || []).length);
      return ok ? scopeKind : autoKind();
    }
    function scopeObj() {
      const k = effectiveKind();
      if (k === "all") return { all: true };
      if (k === "catalog") return { catalog: prefs.sys };
      if (k === "schema") return { catalog: prefs.sys, schema: prefs.schema };
      return { catalog: prefs.sys, schema: prefs.schema, tables: cTables.value || [] };
    }
    function scopeText(s) {
      if (!s || s.all) return "all systems";
      return s.tables && s.tables.length ? `${s.catalog}.${s.schema}: ${plural(s.tables.length, "table")}` : [s.catalog, s.schema].filter(Boolean).join(".");
    }
    function updateRefreshHints() {
      const seg = $("#scope-seg", root); if (!seg) return;
      const k = effectiveKind(), s = scopeObj();
      if (!scopeAuto && k === scopeKind) $("#scope-need", root).hidden = true;
      seg.querySelectorAll("button").forEach((b) => { const on = b.dataset.k === k; b.classList.toggle("active", on); b.setAttribute("aria-checked", on ? "true" : "false"); });
      const anon = st.lib && !st.lib.signed_in;
      $("#refresh-btn", root).disabled = !!anon;
      $("#refresh-lbl", root).textContent = s.all ? "Refresh all systems (structure)" : "Refresh definitions";
      $("#scope-txt", root).textContent = anon ? "Sign in through Studio to refresh" : s.all ? "metadata only, no AI" : scopeText(s) + (!scopeAuto && k === scopeKind ? " (chosen)" : "");
      const ra = $("#ra", root), rf = $("#rf", root);
      const can = !!(st.status && st.status.can_annotate) && !s.all;
      ra.disabled = !can; if (!can) ra.checked = false;
      rf.disabled = !ra.checked; if (!ra.checked) rf.checked = false;
      $("#ra-hint", root).textContent = s.all ? "All systems refresh structure only. Pick a system, schema or tables to annotate." : (st.status && st.status.can_annotate)
        ? "Adds AI-suggested descriptions, terms and PII tags from metadata only (no data rows)." : "Needs DataHub. Without it, refresh re-reads the structure from Trino.";
    }
    function confirmAll() {
      return new Promise((resolve) => {
        const names = st.systems.filter((x) => x.in_trino !== false && x.connector !== "tpch").map((x) => x.name);
        const bq = st.systems.filter((x) => x.connector === "bigquery").map((x) => x.name);
        let done = false;
        const ok = h("button", { class: "btn primary", type: "button", onclick: () => { done = true; resolve(true); m.close(); } }, "Refresh all");
        const cancel = h("button", { class: "btn ghost", type: "button", onclick: () => m.close() }, "Cancel");
        const m = BSP.modal({ title: "Refresh all systems?", foot: [cancel, ok], onClose: () => { if (!done) resolve(false); },
          body: h("div", { class: "stack" }, h("div", null, `Re-reads table and column structure for ${plural(names.length, "system")}: ${names.join(", ")}.`),
            bq.length ? h("div", { class: "alert warning fsql-note" }, icon("warning"), h("div", null, `BigQuery catalogs (${bq.join(", ")}) are big and can take several minutes.`)) : null,
            h("div", { class: "caption" }, "Metadata only: no data rows are read and no AI annotation runs. Progress shows on the Catalog jobs page.")) });
      });
    }
    async function startRefresh() {
      const scope = scopeObj();
      if (scope.all && !(await confirmAll())) return;
      try {
        const r = await api("/sql/catalog/refresh", { body: scope.all ? { all: true } : { scope, reannotate: $("#ra", root).checked, force: $("#rf", root).checked } });
        attachJob(r.job);
        toast("Refresh started. Progress shows above the editor and on the Catalog jobs page.", "info");
      } catch (e) { toast(e.message, "error"); }
    }
    function attachJob(j) { st.job = j; st.jobNotified = false; renderJob(); pollJob(); }
    const TERMINAL = ["succeeded", "failed", "cancelled", "interrupted"];
    async function pollJob() {
      clearTimeout(st.jobTimer);
      if (!st.job || !st.alive) return;
      try { st.job = await api("/sql/catalog/jobs/" + st.job.job_id); } catch (e) { st.job.state = "failed"; st.job.log_tail = (st.job.log_tail || "") + "\n" + e.message; }
      renderJob();
      if (TERMINAL.includes(st.job.state)) { if (!st.jobNotified) { st.jobNotified = true; await jobFinished(st.job); } return; }
      st.jobTimer = setTimeout(pollJob, 2000);
    }
    async function jobFinished(j) {
      const c = j.counts || {};
      if (j.state === "succeeded") {
        const tables = c.tables_annotated != null ? c.tables_annotated : (c.tables_scanned || 0);
        toast(`Definitions refreshed: ${plural(tables, "table")}${c.tables_annotated != null ? " annotated" : " read"}, ${c.columns_described || 0} columns annotated${ed.value.trim() ? ". Re-plan is ready above the editor." : ""}`, "success");
        await Promise.all([loadStatus(), loadGlossary()]);
        const sc = j.scope || {};
        if (sc.all || sc.catalog === prefs.sys) {
          const keep = cTables.value || []; st.cols = {};
          if (sc.all || !sc.schema || sc.schema === prefs.schema) await loadTables(keep);
        }
      } else toast(`Refresh ${j.state}. See the log on the Catalog jobs page.`, "error");
    }
    function countsText(c) {
      c = c || {};
      return [c.tables_scanned != null ? `${plural(c.tables_scanned, "table")} read` : null, c.events_produced ? `${c.events_produced} metadata events` : null,
        c.tables_annotated != null ? `${c.tables_annotated} annotated` : null, c.columns_described ? `${c.columns_described} columns described` : null,
        c.skipped ? `${c.skipped} skipped (human text kept)` : null, c.pii_columns ? `${c.pii_columns} PII columns` : null,
        c.tables_pii_suspected ? `${c.tables_pii_suspected} with suspected personal data` : null, c.terms_linked ? `${c.terms_linked} terms linked` : null,
        c.errors ? `${c.errors} errors` : null].filter(Boolean).join(" · ");
    }
    function renderJob() {
      const j = st.job, box = $("#jobstrip", root);
      if (!j) { box.hidden = true; return; }
      box.hidden = false;
      const done = TERMINAL.includes(j.state);
      const label = { succeeded: "Definitions refreshed", failed: "Refresh failed", cancelled: "Refresh cancelled", interrupted: "Refresh interrupted", queued: "Refresh queued" }[j.state] || "Refreshing definitions";
      box.className = "jobstrip " + (done ? (j.state === "succeeded" ? "succeeded" : "failed") : "");
      const logBtn = h("button", { type: "button", class: "link-btn", onclick: () => { showLog = !showLog; renderJob(); } }, showLog ? "Hide log" : "Show log");
      const page = h("a", { class: "link-btn", href: "#sql/jobs", target: "_blank", rel: "noopener", title: "Open the progress page in a new tab" }, "Open progress page");
      const close = h("button", { class: "btn ghost icon sm", title: "Dismiss", "aria-label": "Dismiss", onclick: () => { st.job = null; renderJob(); } }, icon("close", 14));
      const cancel = !done && j.cancellable ? h("button", { class: "btn ghost sm", onclick: async () => { try { st.job = await api(`/sql/catalog/jobs/${j.job_id}/cancel`, { method: "POST" }); renderJob(); } catch (e) { toast(e.message, "error"); } } }, "Cancel") : null;
      const replanBtn = done && j.state === "succeeded" && ed.value.trim() ? h("button", { class: "btn secondary sm", onclick: replan }, icon("sync", 13), h("span", null, "Re-plan now")) : null;
      box.replaceChildren(...[
        h("div", { class: "row1" }, done ? icon(j.state === "succeeded" ? "tick-circle" : "error", 16) : h("span", { class: "spin" }),
          h("b", null, label), h("span", { class: "chip outline" }, scopeText(j.scope || {})),
          h("span", { class: "caption grow" }, [j.backend === "datahub" ? `DataHub ${j.phase || ""}`.trim() : "from Trino", j.reannotate ? "with AI annotation" : null, countsText(j.counts)].filter(Boolean).join(" · ")),
          replanBtn, cancel, page, logBtn, done ? close : null),
        done ? null : h("div", { class: "jobbar indet" }, h("i")),
        showLog ? h("pre", { class: "codeblock" }, (j.log_tail || "(no log yet)").split("\n").slice(-30).join("\n")) : null].filter(Boolean));
    }

    function insertAt(text) {
      const s = ed.selectionStart ?? ed.value.length, e = ed.selectionEnd ?? s;
      ed.value = ed.value.slice(0, s) + text + ed.value.slice(e);
      ed.focus(); ed.selectionStart = ed.selectionEnd = s + text.length; onEdit();
    }
    function insertNames() {
      const cols = cCols.value || [];
      const tabs = cTables.value || [];
      if (!tabs.length) return toast("Choose tables first", "info");
      insertAt(cols.length ? cols.join(", ") : tabs.map((t) => `${prefs.sys}.${prefs.schema}.${t}`).join(", "));
    }
    function draftSelect() {
      const tabs = cTables.value || [];
      if (!tabs.length) return toast("Choose tables first", "info");
      const alias = {}; tabs.forEach((t, i) => (alias[t] = String.fromCharCode(97 + (i % 26)) + (i >= 26 ? i : "")));
      const cols = cCols.value || [];
      const pii = cols.filter((c) => { const [t, n] = c.split("."); return (((st.cols[t] || {}).columns || []).find((x) => x.name === n) || {}).pii; });
      const sel = cols.length ? cols.map((c) => { const [t, n] = c.split("."); return `${alias[t]}.${n}`; }) : ["count(*) AS row_count"];
      const first = tabByName(tabs[0]);
      let sql = `SELECT ${sel.join(",\n       ")}\nFROM ${prefs.sys}.${prefs.schema}.${tabs[0]} ${alias[tabs[0]]}`;
      tabs.slice(1).forEach((t) => {
        const x = tabByName(t);
        sql += `\nJOIN ${prefs.sys}.${prefs.schema}.${t} ${alias[t]} ON ` + (first && first.has_ban && x && x.has_ban ? `${alias[t]}.ban = ${alias[tabs[0]]}.ban` : `/* add a join condition */ TRUE`);
      });
      if (cols.length) sql += "\nLIMIT 100";
      setEditor(sql, { keepRef: false });
      if (pii.length) toast(`${pii.join(", ")} may hold personal data. Prefer counts and aggregates.`, "info");
    }
    const tabByName = tableByName;

    // ══ editor ═════════════════════════════════════════════════════════════
    function onEdit() { prefs.sql = ed.value; save(); renderTitle(); }
    ed.addEventListener("input", onEdit);
    ed.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); run(); }
      else if (e.key === "Tab" && !e.shiftKey) { e.preventDefault(); insertAt("  "); }
    });
    function setEditor(sql, { keepRef, question } = {}) {
      ed.value = sql; if (!keepRef) st.current = null;
      if (question !== undefined) { st.question = question || ""; prefs.question = st.question; $("#ask", root).value = st.question; }
      onEdit();
    }
    $("#fmt", root).onclick = () => { if (ed.value.trim()) { ed.value = formatSql(ed.value); onEdit(); } };
    $("#cap", root).onchange = (e) => { prefs.cap = +e.target.value; save(); };
    $("#use-defs", root).onchange = (e) => { prefs.useDefs = e.target.checked; save(); };
    $("#run", root).onclick = run;
    $("#explain", root).onclick = () => explain(true);
    $("#replan", root).onclick = replan;
    $("#ask-btn", root).onclick = () => ask();
    $("#ask", root).addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); ask(); } });

    function currentQuery() {
      const c = st.current; if (!c || !st.lib) return null;
      if (c.kind === "own") return st.lib.queries.find((q) => q.id === c.id) || null;
      const col = st.lib.shared.find((x) => x.id === c.cid);
      return col ? (col.queries || []).find((q) => q.id === c.id) || null : null;
    }
    function renderTitle() {
      const q = currentQuery();
      const dirty = q && q.sql !== ed.value;
      $("#qtitle", root).textContent = q ? q.title : "Unsaved query";
      const col = st.current && st.current.kind === "shared" ? st.lib.shared.find((x) => x.id === st.current.cid) : null;
      $("#qsrc", root).textContent = [col ? `in ${col.name}${col.editable ? "" : " (read-only)"}` : null, dirty ? "edited" : null, st.question ? "from an English question" : null].filter(Boolean).join(" · ");
      const starred = q && st.current.kind === "own" && q.starred;
      const sb = $("#star", root); sb.classList.toggle("on", !!starred); sb.replaceChildren(icon(starred ? "star-filled" : "star-outlined", 16));
      sb.title = starred ? "Unstar" : "Star this query";
      root.querySelectorAll(".sq-row").forEach((r) => r.classList.toggle("cur", !!st.current && r.dataset.qid === st.current.id && (r.dataset.cid || "") === (st.current.cid || "")));
    }
    function refOf() {
      const q = currentQuery();
      if (!q || q.sql !== ed.value) return null;
      return st.current.kind === "own" ? { qid: q.id } : { cid: st.current.cid, qid: q.id };
    }
    function systemChips(list) { return (list || []).map((s) => h("span", { class: "chip outline" }, icon("database", 11), s)); }

    async function run() {
      const sql = ed.value.trim(); if (!sql) return toast("Write or generate a query first", "info");
      const btn = $("#run", root), out = $("#result", root);
      btn.classList.add("loading");
      out.replaceChildren(h("div", { class: "card tight" }, h("div", { class: "skeleton", style: "height:80px" })));
      try {
        let finished = false;
        followQuery(sql, false, () => finished);
        const r = await api("/sql/run", { body: { sql, max_rows: prefs.cap, ref: refOf() } }).finally(() => { finished = true; });
        renderResult(r);
        if (r.ok && refOf()) loadLibrary();
      } catch (e) { out.replaceChildren(h("div", { class: "alert error" }, icon("error"), h("div", null, e.message))); }
      finally { btn.classList.remove("loading"); }
    }
    function renderResult(r) {
      const out = $("#result", root);
      const head = h("div", { class: "card-head" }, h("h4", null, "Result"),
        h("div", { class: "fsql-meta" }, r.ok ? h("span", { class: "chip primary" }, plural(r.row_count, "row")) : null, h("span", { class: "chip" }, `${r.ms} ms`), ...systemChips(r.systems)));
      if (!r.ok) { out.replaceChildren(h("div", { class: "card tight" }, head, h("div", { class: "alert error" }, icon("error"), h("div", { style: "white-space:pre-wrap" }, r.error)))); return; }
      const warn = r.truncated ? h("div", { class: "alert warning fsql-note" }, icon("warning"), h("div", null, `More than ${r.cap} rows exist: this is not the full result. Aggregate in SQL or raise the row cap.`)) : null;
      const num = (c) => r.rows.length && r.rows.every((x) => x[c] == null || typeof x[c] === "number");
      const t = BSP.table({ columns: r.columns.map((c) => ({ key: c, label: c, sort: true, num: num(c), render: (row) => (row[c] == null ? h("span", { class: "caption" }, "null") : typeof row[c] === "object" ? JSON.stringify(row[c]) : String(row[c])) })),
        rows: r.rows, pageSize: 50, empty: h("div", { class: "empty" }, "No rows.") });
      out.replaceChildren(h("div", { class: "card tight fsql-res" }, head, warn, t.el));
    }

    async function explainSql(sql) { return api("/sql/explain", { body: { sql } }); }
    function planBlock(p, other) {
      if (!p || !p.ok) return h("div", { class: "alert error" }, icon("error"), h("div", { style: "white-space:pre-wrap" }, (p && p.error) || "No plan"));
      const sysList = h("ul", null, ...p.systems.map((s) => h("li", { class: other && !other.systems.includes(s) ? "add" : "" }, s)),
        ...(other ? other.systems.filter((s) => !p.systems.includes(s)).map((s) => h("li", { class: "rm" }, s)) : []));
      return h("div", { class: "plan-col" },
        h("div", { class: "fsql-meta" }, h("span", { class: "chip primary" }, plural(p.systems.length, "system")), h("span", { class: "chip" }, plural(p.stages, "stage")),
          h("span", { class: "chip" }, plural(p.pushdowns.length, "pushdown")), h("span", { class: "caption" }, `${p.ms} ms`)),
        h("h4", { style: "margin-top:8px" }, "Systems read"), sysList,
        p.systems_not_in_plan_text && p.systems_not_in_plan_text.length ? h("div", { class: "caption" }, `Also named in the query: ${p.systems_not_in_plan_text.join(", ")} (the plan text was cut before their scans).`) : null,
        h("h4", null, "Tables scanned"), h("ul", null, ...p.scans.map((s) => h("li", null, h("code", null, `${s.system}.${s.table}`)))),
        h("h4", null, "Pushed down"), p.pushdowns.length ? h("ul", null, ...p.pushdowns.map((x) => h("li", { class: other && !other.pushdowns.includes(x) ? "add" : "" }, x))) : h("div", { class: "caption" }, "Nothing pushed down beyond the column list."),
        h("details", null, h("summary", { class: "caption" }, "Full plan text"), h("pre", { class: "codeblock", style: "max-height:320px;overflow:auto" }, p.plan || "")));
    }
    async function explain(show) {
      const sql = ed.value.trim(); if (!sql) { toast("Write or generate a query first", "info"); return null; }
      const btn = $("#explain", root); btn.classList.add("loading");
      try {
        let finished = false;
        followQuery(sql, true, () => finished);
        const p = await explainSql(sql).finally(() => { finished = true; });
        p.sql = sql; st.lastPlan = p;
        if (show) $("#plan", root).replaceChildren(h("div", { class: "card tight" }, h("div", { class: "card-head" }, h("h4", null, "Plan"),
          h("button", { class: "btn ghost icon sm", "aria-label": "Close plan", onclick: () => $("#plan", root).replaceChildren() }, icon("close", 14))), planBlock(p)));
        return p;
      } catch (e) { toast(e.message, "error"); return null; } finally { btn.classList.remove("loading"); }
    }
    async function replan() {
      const sql = ed.value.trim(); if (!sql) return toast("Write or generate a query first", "info");
      const btn = $("#replan", root); btn.classList.add("loading");
      const box = $("#plan", root);
      try {
        const before = st.lastPlan && st.lastPlan.sql === sql ? st.lastPlan : { ...(await explainSql(sql)), sql };
        let afterSql = sql, regen = false;
        if (st.question) {
          box.replaceChildren(h("div", { class: "card tight" }, h("div", { class: "caption" }, "Generating SQL again with the refreshed definitions…")));
          const g = await ask(st.question, true);
          if (g) { afterSql = g; regen = true; }
        }
        const after = { ...(await explainSql(afterSql)), sql: afterSql };
        st.lastPlan = after;
        box.replaceChildren(h("div", { class: "card tight" },
          h("div", { class: "card-head" }, h("h4", null, "Re-plan: before and after"),
            h("div", { class: "row" }, h("span", { class: "caption" }, regen ? (afterSql === sql ? "Text-to-SQL gave the same query" : "SQL regenerated from the English question") : "Same SQL, planned again"),
              h("button", { class: "btn ghost icon sm", "aria-label": "Close", onclick: () => box.replaceChildren() }, icon("close", 14)))),
          h("div", { class: "plan-grid" }, h("div", null, h("div", { class: "eyebrow" }, "Before"), planBlock(before)), h("div", null, h("div", { class: "eyebrow" }, "After"), planBlock(after, before.ok ? before : null))),
          regen && afterSql !== sql ? h("details", { style: "margin-top:8px" }, h("summary", { class: "caption" }, "Previous SQL"), h("pre", { class: "codeblock" }, sql)) : null));
      } catch (e) { box.replaceChildren(h("div", { class: "alert error" }, icon("error"), h("div", null, e.message))); }
      finally { btn.classList.remove("loading"); }
    }

    // ── ask in English (agent: trino + catalog tools, model sql-default) ──
    let asking = null;
    async function ask(question, quiet) {
      const q = (question || $("#ask", root).value).trim(); if (!q) { toast("Type a question first", "info"); return null; }
      if (asking) asking.abort();
      asking = new AbortController();
      const btn = $("#ask-btn", root); btn.classList.add("loading");
      const tables = prefs.useDefs ? (cTables.value || []).map((t) => `${prefs.sys}.${prefs.schema}.${t}`) : [];
      const terms = prefs.useDefs ? cTerms.value || [] : [];
      const steps = h("div", { class: "fsql-steps" });
      const msg = h("div", { class: "caption", style: "margin-top:6px;white-space:pre-wrap" });
      const card = h("div", { class: "card tight" }, h("div", { class: "card-head" }, h("h4", null, "Generating SQL"), h("span", { class: "caption" }, q)), steps, msg);
      $("#agent", root).replaceChildren(card);
      const calls = {};
      let sql = "", err = null;
      try {
        await sse("/sql/ask", { question: q, model: "sql-default", tables, terms, sql: ed.value, glossary: st.askTerms || [] }, (ev) => {
          if (ev.type === "context") $("#ask-ctx", root).textContent = ev.definitions ? `Context: ${plural(ev.tables.length, "table")}${ev.refreshed ? ", refreshed " + ago(ev.refreshed) : ""}` : "";
          else if (ev.type === "tool_call") { const d = h("div", { class: "st" }, icon("tool", 12), h("code", null, ev.name), h("span", { class: "caption" }, "running…")); calls[ev.id] = d; steps.append(d); }
          else if (ev.type === "tool_result") { const d = calls[ev.id]; if (d) { d.classList.toggle("err", !ev.ok); d.lastChild.textContent = `${ev.ok ? "ok" : "error"} · ${ev.ms} ms${ev.text ? " · " + ev.text : ""}`; } }
          else if (ev.type === "route") card.querySelector(".card-head .caption").textContent = `${q} · ${ev.provider}/${ev.model}`;
          else if (ev.type === "message") {
            const text = (ev.content || "").replace(/```sql[\s\S]*?```/gi, "").trim().slice(0, 2000);
            if (BSP.md) { msg.className = "fsql-answer"; msg.innerHTML = BSP.md(text); } else msg.textContent = text;
          }
          else if (ev.type === "error") { err = ev.message; steps.append(h("div", { class: "st err" }, icon("error", 12), ev.message)); }
          else if (ev.type === "sql") sql = ev.sql || "";
        }, asking.signal);
      } catch (e) { if (e.name !== "AbortError") err = e.message || String(e); }
      finally { btn.classList.remove("loading"); asking = null; }
      card.querySelector("h4").textContent = sql ? "SQL generated" : "No SQL generated";
      if (!sql) { if (!err) steps.append(h("div", { class: "st err" }, "The model did not return a query. Try rephrasing or choose tables for context.")); return null; }
      setEditor(sql, { keepRef: false, question: q });
      if (!quiet) toast("SQL is in the editor. Review it, then Run.", "success");
      return sql;
    }

    // ── save, star ──
    $("#save", root).onclick = () => saveCurrent();
    $("#save-more", root).onclick = (e) => BSP.menu(e.currentTarget, [
      { label: "Save as new query…", icon: "add", onClick: () => saveDialog({}) },
      currentQuery() && st.current.kind === "own" ? { label: "Add to collections…", icon: "folder", onClick: () => collectionsDialog(currentQuery()) } : null,
      { label: "New empty query", icon: "file", onClick: () => { setEditor("", { keepRef: false, question: "" }); $("#result", root).replaceChildren(); $("#plan", root).replaceChildren(); } },
    ]);
    $("#star", root).onclick = async () => {
      const q = currentQuery();
      if (q && st.current.kind === "own") { await api(`/sql/queries/${q.id}`, { method: "PATCH", body: { starred: !q.starred } }); await loadLibrary(); }
      else saveDialog({ starred: true });
    };
    async function saveCurrent() {
      const sql = ed.value; if (!sql.trim()) return toast("Nothing to save yet", "info");
      const q = currentQuery();
      if (q && st.current.kind === "own") { await api(`/sql/queries/${q.id}`, { method: "PATCH", body: { sql, question: st.question } }); toast("Saved", "success"); return loadLibrary(); }
      if (q && st.current.kind === "shared") {
        const col = st.lib.shared.find((x) => x.id === st.current.cid);
        if (col && col.editable) { await api(`/sql/collections/${col.id}/queries/${q.id}`, { method: "PATCH", body: { sql, question: st.question } }); toast("Saved to the shared collection", "success"); return loadLibrary(); }
        return saveDialog({ title: q.title + " (copy)", note: q.note });
      }
      saveDialog({});
    }
    function saveDialog(init) {
      const title = h("input", { class: "input", value: init.title || ($("#ask", root).value.trim() || "").slice(0, 120), placeholder: "Name this query", required: true });
      const noteEl = h("textarea", { class: "textarea", placeholder: "Optional note", style: "min-height:64px" }, init.note || "");
      const star = h("input", { type: "checkbox", checked: init.starred || null });
      const pin = h("input", { type: "checkbox" });
      const cols = (st.lib ? st.lib.collections : []).map((c) => h("label", { class: "checkbox" }, h("input", { type: "checkbox", value: c.id }), c.name));
      const body = h("form", { class: "stack", onsubmit: (e) => { e.preventDefault(); ok.click(); } },
        h("div", { class: "field" }, h("label", null, "Title"), title), h("div", { class: "field" }, h("label", null, "Note"), noteEl),
        h("div", { class: "row" }, h("label", { class: "checkbox" }, star, "Star"), h("label", { class: "checkbox" }, pin, "Pin to top")),
        cols.length ? h("div", { class: "field" }, h("label", null, "Collections"), h("div", { class: "coll-pick" }, ...cols)) : h("div", { class: "caption" }, "Tip: create collections in Saved queries to group queries."));
      const ok = h("button", { class: "btn primary", type: "button" }, "Save");
      const cancel = h("button", { class: "btn ghost", type: "button" }, "Cancel");
      const m = BSP.modal({ title: "Save query", body, foot: [cancel, ok] });
      cancel.onclick = () => m.close();
      ok.onclick = async () => {
        if (!title.value.trim()) { title.classList.add("invalid"); title.focus(); return; }
        try {
          const q = await api("/sql/queries", { body: { title: title.value, note: noteEl.value, sql: ed.value, question: st.question, starred: star.checked, pinned: pin.checked,
            collection_ids: cols.map((l) => l.querySelector("input")).filter((i) => i.checked).map((i) => i.value) } });
          st.current = { kind: "own", id: q.id }; m.close(); toast("Saved", "success"); await loadLibrary();
        } catch (e) { toast(e.message, "error"); }
      };
    }
    function collectionsDialog(q) {
      const mine = st.lib.collections;
      if (!mine.length) return toast("Create a collection first", "info");
      const boxes = mine.map((c) => h("label", { class: "checkbox" }, h("input", { type: "checkbox", value: c.id, checked: (c.query_ids || []).includes(q.id) || null }), c.name));
      const ok = h("button", { class: "btn primary", type: "button" }, "Done");
      const cancel = h("button", { class: "btn ghost", type: "button" }, "Cancel");
      const m = BSP.modal({ title: `Collections for "${q.title}"`, body: h("div", { class: "coll-pick" }, ...boxes), foot: [cancel, ok] });
      cancel.onclick = () => m.close();
      ok.onclick = async () => {
        await api(`/sql/queries/${q.id}`, { method: "PATCH", body: { collection_ids: boxes.map((b) => b.querySelector("input")).filter((i) => i.checked).map((i) => i.value) } });
        m.close(); loadLibrary();
      };
    }
    async function editText(title, label, value, multiline) {
      if (!multiline) return BSP.prompt({ title, label, value });
      return new Promise((resolve) => {
        const ta = h("textarea", { class: "textarea", style: "min-height:100px" }, value || "");
        let done = false;
        const ok = h("button", { class: "btn primary", type: "button", onclick: () => { done = true; resolve(ta.value); m.close(); } }, "Save");
        const cancel = h("button", { class: "btn ghost", type: "button", onclick: () => m.close() }, "Cancel");
        const m = BSP.modal({ title, body: h("div", { class: "field" }, h("label", null, label), ta), foot: [cancel, ok], onClose: () => { if (!done) resolve(null); } });
      });
    }

    // ══ saved queries panel ═════════════════════════════════════════════════
    const saved = $("#side-saved", root);
    const searchIn = h("input", { class: "input", type: "search", placeholder: "Search saved queries", "aria-label": "Search saved queries" });
    const fileIn = h("input", { type: "file", accept: "application/json,.json", hidden: true });
    const filterSeg = h("div", { class: "segmented" }, h("button", { "data-f": "all", class: "active" }, "All"), h("button", { "data-f": "starred" }, icon("star-outlined", 13), "Starred"));
    const libBody = h("div", { class: "stack" });
    saved.append(
      h("div", { class: "sq-tools" }, searchIn),
      h("div", { class: "sq-tools" }, filterSeg, h("span", { style: "flex:1" }),
        h("button", { class: "btn ghost sm", title: "New collection", onclick: newCollection }, icon("folder", 14), h("span", null, "New")),
        h("button", { class: "btn ghost sm", title: "Import a collection from JSON", onclick: () => fileIn.click() }, icon("upload", 14), h("span", null, "Import")), fileIn),
      libBody);
    searchIn.addEventListener("input", BSP.debounce(renderLibrary, 120));
    filterSeg.onclick = (e) => { const b = e.target.closest("button"); if (!b) return; st.filter = b.dataset.f; filterSeg.querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b)); renderLibrary(); };
    fileIn.onchange = async () => {
      const f = fileIn.files[0]; fileIn.value = ""; if (!f) return;
      try { const doc = JSON.parse(await f.text()); const c = await api("/sql/collections/import", { body: { doc } }); toast(`Imported ${plural((c.query_ids || c.queries || []).length, "query")} into "${c.name}"`, "success"); st.openCols.add(c.id); loadLibrary(); }
      catch (e) { toast("Import failed: " + (e.message || e), "error"); }
    };
    async function newCollection() {
      const name = await BSP.prompt({ title: "New collection", label: "Name", placeholder: "Churn analysis" });
      if (!name) return;
      try { const c = await api("/sql/collections", { body: { name } }); st.openCols.add(c.id); loadLibrary(); } catch (e) { toast(e.message, "error"); }
    }
    async function loadLibrary() {
      try { st.lib = await api("/sql/library"); } catch (e) { libBody.replaceChildren(h("div", { class: "alert error" }, icon("error"), h("div", null, e.message))); return; }
      renderLibrary(); renderTitle(); updateRefreshHints();
      if (store.get("fsql-lastuser") !== st.lib.user) { store.set("fsql-lastuser", st.lib.user); showTrino(!!store.get(trinoKey(), false), false); }
    }
    function qMeta(q) {
      return [q.last_run ? `ran ${ago(q.last_run)}` : "not run yet", q.row_count != null ? plural(q.row_count, "row") : null, q.ms != null ? `${q.ms} ms` : null].filter(Boolean).join(" · ");
    }
    function openQuery(q, ctx) {
      st.current = ctx.shared ? { kind: "shared", cid: ctx.cid, id: q.id } : { kind: "own", id: q.id };
      setEditor(q.sql || "", { keepRef: true, question: q.question || "" });
      st.lastPlan = null; $("#result", root).replaceChildren(); $("#plan", root).replaceChildren();
      renderTitle();
    }
    function inlineConfirm(row, text, okText, onOk) {
      const keep = [...row.childNodes]; const drag = row.draggable; row.draggable = false;
      const restore = () => { row.replaceChildren(...keep); row.draggable = drag; };
      row.replaceChildren(h("div", { class: "sq-confirm" }, h("span", null, text),
        h("button", { class: "btn danger sm", onclick: async () => { try { await onOk(); } catch (e) { toast(e.message, "error"); restore(); } } }, okText),
        h("button", { class: "btn ghost sm", onclick: restore }, "Cancel")));
      row.querySelector(".btn.danger").focus();
    }
    // ctx: {list: "all"|"col"|"shared", cid, shared, editable}
    function queryRow(q, ctx) {
      const own = !ctx.shared;
      const row = h("div", { class: "sq-row", "data-qid": q.id, "data-cid": ctx.shared || ctx.list === "col" ? ctx.cid : "", draggable: (own || ctx.editable) && !searchIn.value.trim() ? "true" : null });
      if (st.current && st.current.id === q.id && (st.current.cid || "") === (ctx.shared ? ctx.cid : "")) row.classList.add("cur");
      const star = own ? h("button", { class: "btn ghost icon sm star-btn" + (q.starred ? " on" : ""), title: q.starred ? "Unstar" : "Star", "aria-label": q.starred ? `Unstar ${q.title}` : `Star ${q.title}`,
        onclick: async () => { await api(`/sql/queries/${q.id}`, { method: "PATCH", body: { starred: !q.starred } }); loadLibrary(); } }, icon(q.starred ? "star-filled" : "star-outlined", 14))
        : h("span", { class: "btn ghost icon sm", "aria-hidden": "true", style: "pointer-events:none;color:var(--bsp-slate-400)" }, icon("file", 13));
      const main = h("div", { class: "main", onclick: () => openQuery(q, ctx), title: q.note || q.title },
        h("span", { class: "t" }, q.title), h("span", { class: "m" }, (ctx.showCol ? ctx.showCol + " · " : "") + (q.note ? q.note : qMeta(q))));
      const more = h("button", { class: "btn ghost icon sm", title: "More", "aria-label": `More actions for ${q.title}` }, icon("more", 14));
      const pinB = own ? h("button", { class: "btn ghost icon sm pin-btn" + (q.pinned ? " on" : ""), title: q.pinned ? "Unpin" : "Pin to top", "aria-label": q.pinned ? `Unpin ${q.title}` : `Pin ${q.title}`,
        onclick: async () => { await api(`/sql/queries/${q.id}`, { method: "PATCH", body: { pinned: !q.pinned } }); loadLibrary(); } }, icon("pin", 13)) : null;
      more.onclick = () => BSP.menu(more, queryMenu(q, ctx, row));
      row.append(star, main, h("span", { class: "acts" }, pinB, more));
      return row;
    }
    function move(list, id, d) { const i = list.indexOf(id); const j = i + d; if (i < 0 || j < 0 || j >= list.length) return null; const out = [...list]; [out[i], out[j]] = [out[j], out[i]]; return out; }
    async function saveOrder(ctx, ids) {
      if (ctx.list === "all") await api("/sql/queries/order", { method: "PUT", body: { ids } });
      else await api(`/sql/collections/${ctx.cid}`, { method: "PATCH", body: { query_ids: ids } });
      loadLibrary();
    }
    function idsOf(ctx) {
      if (ctx.list === "all") return st.lib.queries.map((q) => q.id);
      if (ctx.shared) return (st.lib.shared.find((c) => c.id === ctx.cid).queries || []).map((q) => q.id);
      return [...(st.lib.collections.find((c) => c.id === ctx.cid).query_ids || [])];
    }
    function queryMenu(q, ctx, row) {
      const own = !ctx.shared, canEdit = own || ctx.editable;
      const patch = (body) => (own ? api(`/sql/queries/${q.id}`, { method: "PATCH", body }) : api(`/sql/collections/${ctx.cid}/queries/${q.id}`, { method: "PATCH", body }));
      const reorder = (d) => async () => { const ids = move(idsOf(ctx), q.id, d); if (ids) await saveOrder(ctx, ids); };
      const canMove = (ctx.list === "all" || ctx.list === "col" || (ctx.shared && ctx.editable)) && !searchIn.value.trim() && st.filter === "all";
      return [
        { label: "Open in editor", icon: "external", onClick: () => openQuery(q, ctx) },
        canEdit ? { label: "Rename", icon: "edit", onClick: async () => { const t = await editText("Rename query", "Title", q.title); if (t) { await patch({ title: t }); loadLibrary(); } } } : null,
        canEdit ? { label: q.note ? "Edit note" : "Add note", icon: "notes", onClick: async () => { const n = await editText("Note", "Note", q.note, true); if (n !== null) { await patch({ note: n }); loadLibrary(); } } } : null,
        own ? { label: "Duplicate", icon: "copy", onClick: async () => { await api(`/sql/queries/${q.id}/duplicate`, { method: "POST" }); loadLibrary(); } } : null,
        own ? { label: "Collections…", icon: "folder", onClick: () => collectionsDialog(q) } : null,
        !own ? { label: "Copy to my queries", icon: "copy", onClick: async () => { await api("/sql/queries", { body: { title: q.title, sql: q.sql, note: q.note, question: q.question } }); toast("Copied to your saved queries", "success"); loadLibrary(); } } : null,
        canMove ? { label: "Move up", icon: "arrow-up", onClick: reorder(-1) } : null,
        canMove ? { label: "Move down", icon: "arrow-down", onClick: reorder(1) } : null,
        ctx.list === "col" && own ? { label: "Remove from this collection", icon: "minus", onClick: async () => { await api(`/sql/collections/${ctx.cid}/queries/${q.id}`, { method: "DELETE" }); loadLibrary(); } } : null,
        canEdit ? "-" : null,
        own ? { label: "Delete", icon: "delete", danger: true, onClick: () => inlineConfirm(row, `Delete "${q.title}"? It leaves every collection.`, "Delete", async () => { await api(`/sql/queries/${q.id}`, { method: "DELETE" }); if (st.current && st.current.id === q.id) st.current = null; loadLibrary(); }) } : null,
        ctx.shared && ctx.editable ? { label: "Remove from shared collection", icon: "delete", danger: true, onClick: () => inlineConfirm(row, `Remove "${q.title}" for everyone?`, "Remove", async () => { await api(`/sql/collections/${ctx.cid}/queries/${q.id}`, { method: "DELETE" }); loadLibrary(); }) } : null,
      ];
    }
    function dnd(listEl, ctx) {
      if (!(ctx.list === "all" || ctx.list === "col" || ctx.editable) || searchIn.value.trim() || st.filter !== "all") return;
      let dragId = null;
      listEl.addEventListener("dragstart", (e) => { const r = e.target.closest(".sq-row"); if (!r) return; dragId = r.dataset.qid; e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", dragId); e.stopPropagation(); });
      listEl.addEventListener("dragover", (e) => { if (!dragId) return; const r = e.target.closest(".sq-row"); e.preventDefault(); listEl.querySelectorAll(".drag-over").forEach((x) => x.classList.remove("drag-over")); if (r) r.classList.add("drag-over"); });
      listEl.addEventListener("dragleave", (e) => { const r = e.target.closest(".sq-row"); if (r) r.classList.remove("drag-over"); });
      listEl.addEventListener("drop", async (e) => {
        if (!dragId) return; e.preventDefault(); e.stopPropagation();
        const r = e.target.closest(".sq-row"); listEl.querySelectorAll(".drag-over").forEach((x) => x.classList.remove("drag-over"));
        const ids = idsOf(ctx).filter((x) => x !== dragId);
        const at = r ? ids.indexOf(r.dataset.qid) : ids.length;
        ids.splice(at < 0 ? ids.length : at, 0, dragId); dragId = null;
        await saveOrder(ctx, ids);
      });
      listEl.addEventListener("dragend", () => { dragId = null; });
    }
    function matches(q) {
      const words = searchIn.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
      if (st.filter === "starred" && !q.starred) return false;
      return words.every((w) => `${q.title} ${q.sql} ${q.note || ""} ${q.question || ""}`.toLowerCase().includes(w));
    }
    function collectionBox(c, shared) {
      const qs = shared ? (c.queries || []) : (c.query_ids || []).map((id) => st.lib.queries.find((q) => q.id === id)).filter(Boolean);
      const vis = qs.filter(matches);
      const filtering = !!searchIn.value.trim() || st.filter !== "all";
      if (filtering && !vis.length) return null;
      const open = filtering || st.openCols.has(c.id);
      const box = h("div", { class: "sq-col" + (open ? " open" : ""), "data-cid": c.id, draggable: !shared && !filtering ? "true" : null });
      const more = h("button", { class: "btn ghost icon sm", title: "Collection actions", "aria-label": `Actions for ${c.name}` }, icon("more", 14));
      const owner = shared ? (c.seed ? "appliance" : c.owner) : null;
      const hd = h("div", { class: "hd", role: "button", tabindex: "0", "aria-expanded": open ? "true" : "false" }, h("span", { class: "caret" }, icon("caret-right", 12)),
        h("span", { class: "n", title: c.note || c.name }, icon(shared ? "user-list" : "folder", 13), " ", c.name, " ", h("span", { class: "caption" }, `${qs.length}${shared ? " · " + (c.editable ? "yours, shared" : "by " + owner) : ""}`)), more);
      const toggle = () => { if (st.openCols.has(c.id)) st.openCols.delete(c.id); else st.openCols.add(c.id); store.set("fsql-open", [...st.openCols]); renderLibrary(); };
      hd.addEventListener("click", (e) => { if (!e.target.closest("button")) toggle(); });
      hd.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
      more.onclick = () => BSP.menu(more, collectionMenu(c, shared, box));
      box.append(hd);
      if (open) {
        const ctx = { list: shared ? "shared" : "col", cid: c.id, shared, editable: !!c.editable };
        const list = h("div", { class: "sq-list" }, ...vis.map((q) => queryRow(q, ctx)));
        if (!vis.length) list.append(h("div", { class: "caption", style: "padding:4px 2px" }, shared ? "No queries yet." : "Empty. Use a query's menu, Collections…, to add it here."));
        dnd(list, ctx);
        box.append(h("div", { class: "bd" }, c.note ? h("div", { class: "note" }, c.note) : null, list));
      }
      return box;
    }
    function collectionMenu(c, shared, box) {
      const canEdit = !shared || c.editable;
      const exp = async () => { try { const d = await api(`/sql/collections/${c.id}/export`); download((c.name || "collection").replace(/[^A-Za-z0-9_-]+/g, "-").toLowerCase() + ".json", d); } catch (e) { toast(e.message, "error"); } };
      return [
        canEdit ? { label: "Rename", icon: "edit", onClick: async () => { const n = await editText("Rename collection", "Name", c.name); if (n) { await api(`/sql/collections/${c.id}`, { method: "PATCH", body: { name: n } }); loadLibrary(); } } } : null,
        canEdit ? { label: c.note ? "Edit note" : "Add note", icon: "notes", onClick: async () => { const n = await editText("Collection note", "Note", c.note, true); if (n !== null) { await api(`/sql/collections/${c.id}`, { method: "PATCH", body: { note: n } }); loadLibrary(); } } } : null,
        shared && c.editable && currentQuery() && st.current.kind === "own" ? { label: "Add the open query", icon: "add", onClick: async () => { await api(`/sql/collections/${c.id}/queries`, { body: { query_id: st.current.id } }); loadLibrary(); } } : null,
        { label: shared ? "Copy to my queries" : "Duplicate", icon: "copy", onClick: async () => { const d = await api(`/sql/collections/${c.id}/duplicate`, { method: "POST" }); st.openCols.add(d.id); toast(`Copied as "${d.name}"`, "success"); loadLibrary(); } },
        !shared && st.lib.signed_in ? { label: "Share with everyone", icon: "user-list", onClick: async () => { const d = await api(`/sql/collections/${c.id}/share`, { method: "POST" }); toast(`Shared as "${d.name}". Others can view and copy it.`, "success"); loadLibrary(); } } : null,
        { label: "Export as JSON", icon: "download", onClick: exp },
        canEdit ? "-" : null,
        !shared ? { label: "Delete collection", icon: "delete", danger: true, onClick: () => inlineConfirm(box.querySelector(".hd"), `Delete "${c.name}"? Its queries stay in your list.`, "Delete", async () => { await api(`/sql/collections/${c.id}`, { method: "DELETE" }); loadLibrary(); }) } : null,
        shared && c.editable ? { label: "Stop sharing and delete", icon: "delete", danger: true, onClick: () => inlineConfirm(box.querySelector(".hd"), `Delete shared "${c.name}" for everyone?`, "Delete", async () => { await api(`/sql/collections/${c.id}`, { method: "DELETE" }); loadLibrary(); }) } : null,
      ];
    }
    function colsDnd(listEl) {
      if (searchIn.value.trim() || st.filter !== "all") return;
      let dragId = null;
      listEl.addEventListener("dragstart", (e) => { const b = e.target.closest(".sq-col"); if (!b || e.target.closest(".sq-row")) return; dragId = b.dataset.cid; e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", dragId); });
      listEl.addEventListener("dragover", (e) => { if (!dragId) return; e.preventDefault(); const b = e.target.closest(".sq-col"); listEl.querySelectorAll(".sq-col.drag-over").forEach((x) => x.classList.remove("drag-over")); if (b) b.classList.add("drag-over"); });
      listEl.addEventListener("drop", async (e) => {
        if (!dragId) return; e.preventDefault();
        const b = e.target.closest(".sq-col"); listEl.querySelectorAll(".drag-over").forEach((x) => x.classList.remove("drag-over"));
        const ids = st.lib.collections.map((c) => c.id).filter((x) => x !== dragId);
        const at = b ? ids.indexOf(b.dataset.cid) : ids.length; ids.splice(at < 0 ? ids.length : at, 0, dragId); dragId = null;
        await api("/sql/collections/order", { method: "PUT", body: { ids } }); loadLibrary();
      });
      listEl.addEventListener("dragend", () => { dragId = null; });
    }
    function section(title, extra, ...kids) { return h("div", { class: "sq-sec" }, h("div", { class: "eyebrow" }, h("span", null, title), extra || h("span")), ...kids); }
    function renderLibrary() {
      if (!st.lib) return;
      const L = st.lib;
      const out = [];
      const pinned = L.queries.filter((q) => q.pinned && matches(q));
      if (pinned.length) out.push(section("Pinned", null, h("div", { class: "sq-list" }, ...pinned.map((q) => queryRow(q, { list: "pinned" })))));
      const cols = L.collections.map((c) => collectionBox(c, false)).filter(Boolean);
      const colsEl = h("div", { class: "sq-cols" }, ...cols);
      colsDnd(colsEl);
      if (cols.length || !searchIn.value.trim()) out.push(section("My collections", null, cols.length ? colsEl : h("div", { class: "caption" }, "No collections yet. New makes one.")));
      const all = L.queries.filter(matches);
      const listAll = h("div", { class: "sq-list" }, ...all.map((q) => queryRow(q, { list: "all" })));
      if (!all.length) listAll.append(h("div", { class: "caption" }, L.queries.length ? "No matches." : "Nothing saved yet. Write or generate a query, then Save."));
      dnd(listAll, { list: "all" });
      out.push(section(searchIn.value.trim() || st.filter !== "all" ? `Matching saved queries (${all.length})` : `All my queries (${L.queries.length})`, null, listAll));
      const sh = L.shared.map((c) => collectionBox(c, true)).filter(Boolean);
      if (sh.length) out.push(section("Shared collections", h("span", { class: "caption", style: "text-transform:none;letter-spacing:0;font-weight:400" }, "read-only unless yours"), h("div", { class: "sq-cols" }, ...sh)));
      if (!L.signed_in) out.unshift(h("div", { class: "alert warning fsql-note" }, icon("warning"), h("div", null, "Not signed in through Studio: queries are saved to a shared anonymous library and sharing is off.")));
      libBody.replaceChildren(...out);
    }

    // ══ Glossary tab: DataHub terms by group, linked tables with provenance ══
    const glBox = $("#side-glossary", root);
    const glSearch = h("input", { class: "input", type: "search", placeholder: "Search terms and definitions", "aria-label": "Search glossary" });
    const glInfo = h("span", { class: "caption" });
    const glList = h("div", { class: "stack", style: "gap:6px" });
    const glOpen = new Set();
    glBox.append(h("div", { class: "sq-tools" }, glSearch, h("button", { class: "btn ghost icon sm", title: "Reload from DataHub", "aria-label": "Reload glossary", onclick: () => loadGlossaryView(true) }, icon("refresh", 14))), glInfo, glList);
    glSearch.addEventListener("input", BSP.debounce(renderGlossaryView, 120));
    const SRC = { curated: ["chip primary", "curated"], imported: ["chip auto", "imported from BigQuery"], ai: ["chip ai", "AI-suggested"], hint: ["chip", "name hint"] };
    async function loadGlossaryView(refresh) {
      glList.replaceChildren(h("div", { class: "skeleton", style: "height:80px" }));
      try { st.gl = await api("/sql/glossary" + (refresh ? "?refresh=true" : "")); } catch (e) { glList.replaceChildren(h("div", { class: "alert error" }, icon("error"), h("div", null, e.message))); return; }
      renderGlossaryView();
    }
    function renderAskTerms() {
      const box = $("#ask-terms", root);
      box.replaceChildren(...(st.askTerms || []).map((g) => h("span", { class: "chip term", title: g.definition }, g.name,
        h("button", { type: "button", "aria-label": `Remove ${g.name} from the question context`, onclick: () => { st.askTerms = st.askTerms.filter((x) => x.name !== g.name); renderAskTerms(); } }, icon("close", 10)))));
    }
    async function jumpTo(fqn) {
      const [c, sc, t] = fqn.split(".");
      if (!st.systems.some((x) => x.name === c)) return toast(`${c} is not a connected system here`, "info");
      prefs.sys = c; prefs.schema = sc; prefs.tables = [t]; save();
      showTab("catalog"); cSys.set(c); await loadSchemas();
      if (!(cTables.value || []).includes(t)) toast(`${fqn} is in DataHub but was not found in Trino`, "info");
    }
    function termRow(t) {
      const opened = glOpen.has(t.urn || t.name);
      const src = [...(SRC[t.source] || SRC.curated)];
      if (t.ai_definition && t.source === "imported") src[1] = "imported from BigQuery · AI-suggested definition";
      const ai = t.tables.filter((x) => x.provenance === "ai").length;
      const head = h("div", { class: "hd", role: "button", tabindex: "0", "aria-expanded": opened ? "true" : "false" },
        h("span", { class: "caret" }, icon("caret-right", 12)),
        h("span", { class: "n", title: t.name }, t.name, " ", h("span", { class: "caption" }, `${plural(t.table_count, "table")}${ai ? ` · ${ai} AI-linked` : ""}`)),
        h("span", { class: src[0], style: "height:auto;min-height:20px;font-size:11px;white-space:normal;text-align:right;max-width:150px" }, src[1]));
      const toggle = () => { const k = t.urn || t.name; if (glOpen.has(k)) glOpen.delete(k); else glOpen.add(k); renderGlossaryView(); };
      head.addEventListener("click", toggle);
      head.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
      const box = h("div", { class: "sq-col" + (opened ? " open" : "") }, head);
      const def = h("div", { class: "note", style: "white-space:pre-wrap" }); def.textContent = t.definition || "No definition yet.";
      if (!opened) { def.style.cssText = "white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding:0 8px 6px 26px;font-size:12px;color:var(--bsp-text-caption)"; box.append(def); return box; }
      const use = h("button", { class: "btn secondary sm", onclick: () => {
        st.askTerms = (st.askTerms || []).filter((x) => x.name !== t.name).concat([{ name: t.name, definition: t.definition }]);
        renderAskTerms(); $("#ask", root).focus(); toast(`${t.name} added as context for Ask in English`, "success"); } }, icon("add", 13), h("span", null, "Use in question"));
      const dh = t.datahub_url ? h("a", { class: "btn ghost sm", href: t.datahub_url, target: "_blank", rel: "noopener" }, icon("external", 13), h("span", null, "Open in DataHub")) : null;
      const tables = h("div", { class: "sq-list" }, ...(t.tables.length ? t.tables.slice(0, 200).map((x) => {
        const label = x.trino || x.name;
        const row = h("div", { class: "sq-row", style: "grid-template-columns:minmax(0,1fr) auto" },
          h("div", { class: "main", title: x.trino ? `Show ${label} in Catalog` : `${x.platform}: not mapped to a Trino catalog`, onclick: () => (x.trino ? jumpTo(x.trino) : toast("This table has no Trino name here", "info")) },
            h("span", { class: "t mono", style: "font-size:12px" }, label), h("span", { class: "m" }, `${x.level} link · ${(x.platforms || [x.platform]).join(", ")}${x.also_ai ? " · also AI-linked" : ""}`)),
          h("span", { class: "mini " + (x.provenance === "ai" ? "ai" : "ban"), title: x.provenance === "ai" ? "Linked by the AI annotator (ai-annotated)" : "Curated link" }, x.provenance === "ai" ? "AI" : x.provenance === "hint" ? "hint" : "curated"));
        return row;
      }) : [h("div", { class: "caption" }, "No linked tables.")]));
      box.append(h("div", { class: "bd" }, def, h("div", { class: "row wrap", style: "margin:4px 0 6px" }, use, dh),
        t.tables.length > 200 ? h("div", { class: "caption" }, `First 200 of ${t.tables.length}`) : null, tables));
      return box;
    }
    function renderGlossaryView() {
      const g = st.gl; if (!g) return;
      const words = glSearch.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
      const vis = g.terms.filter((t) => words.every((w) => `${t.name} ${t.definition} ${t.group}`.toLowerCase().includes(w)));
      glInfo.textContent = `${plural(g.terms.length, "term")} in ${plural(g.groups.length, "group")} · ${g.source === "datahub" ? "from DataHub" : "from name hints (DataHub not running)"}${g.cached ? " · cached" : ""}`;
      const groups = {}; vis.forEach((t) => (groups[t.group] = groups[t.group] || []).push(t));
      glList.replaceChildren(...(vis.length ? Object.entries(groups).map(([name, ts]) => h("div", { class: "sq-sec" }, h("div", { class: "eyebrow" }, h("span", null, name), h("span", null, String(ts.length))), h("div", { class: "sq-cols" }, ...ts.map(termRow))))
        : [h("div", { class: "caption" }, "No terms match.")]));
    }

    // ══ Trino monitor pane (same-origin iframe of /trino/ui/, live query pages) ══
    const pane = $("#trino-pane", root), fsqlGrid = root.querySelector(".fsql");
    const trinoKey = () => "fsql-trino:" + ((st.lib && st.lib.user) || store.get("fsql-lastuser", "playground"));
    let trinoUrl = "/trino/ui/", findTimer = null;
    function setTrinoUrl(url, label) {
      trinoUrl = url; $("#trino-open", root).href = url;
      if (label) $("#trino-state", root).textContent = label;
      const fr = pane.querySelector("iframe"); if (fr && !pane.hidden) fr.src = url;
    }
    function frameCheck(fr) {
      let ok = false;
      try { const d = fr.contentDocument; ok = !!d && d.location.pathname.startsWith("/trino/ui") && !!d.querySelector("#root"); } catch (_) { ok = false; }
      $("#trino-fallback", root).hidden = ok;
    }
    function showTrino(on, remember) {
      pane.hidden = !on; fsqlGrid.classList.toggle("with-trino", on);
      const t = $("#trino-toggle", root); t.setAttribute("aria-pressed", on ? "true" : "false"); t.classList.toggle("active", on);
      if (remember !== false) store.set(trinoKey(), on);
      const box = $("#trino-frame", root);
      if (on && !box.querySelector("iframe")) {
        const fr = h("iframe", { src: trinoUrl, title: "Trino web UI", referrerpolicy: "same-origin" });
        fr.addEventListener("load", () => frameCheck(fr));
        box.append(fr);
      } else if (!on) box.replaceChildren();   // drop the iframe (no idle polling of the Trino UI)
    }
    $("#trino-toggle", root).onclick = () => showTrino(pane.hidden);
    $("#trino-close", root).onclick = () => showTrino(false);
    const onKey = (e) => { if (e.altKey && e.shiftKey && (e.key === "M" || e.key === "m" || e.code === "KeyM")) { e.preventDefault(); showTrino(pane.hidden); } };
    document.addEventListener("keydown", onKey);
    // find the Trino query id of what we just sent (trino_query / trino_explain do not return it) and open its live page
    function followQuery(sql, explain, isDone) {
      clearTimeout(findTimer);
      const since = Date.now(); let tries = 0;
      const tick = async () => {
        if (!st.alive) return;
        tries++;
        try {
          const r = await api("/sql/trino/find", { body: { sql, since, explain } });
          if (r.found) { setTrinoUrl(r.ui, `${explain ? "Explain" : "Query"} ${r.query_id} · ${String(r.state || "").toLowerCase()}`); return; }
        } catch (_) { /* monitor is best effort */ }
        if (isDone() && tries > 3) { setTrinoUrl("/trino/ui/", "Query list (no match found)"); return; }
        if (tries < 60) findTimer = setTimeout(tick, 700);
      };
      findTimer = setTimeout(tick, 400);
    }

    // ── boot ──
    showTab(["saved", "glossary"].includes(prefs.tab) ? prefs.tab : "catalog");
    renderTitle();
    loadStatus().then(updateRefreshHints);
    loadGlossary().then(loadSystems);
    loadLibrary();
    // #sql?monitor=1 (the Launchpad Trino card) always opens the Trino system dashboard on the right;
    // otherwise the user's last choice, open by default on wide screens
    const forceMonitor = /[?&]monitor=1\b/.test(location.hash);
    showTrino(forceMonitor || !!store.get(trinoKey(), window.innerWidth >= 1280), false);
    return { destroy() { st.alive = false; clearTimeout(st.jobTimer); clearTimeout(findTimer); document.removeEventListener("keydown", onKey); if (asking) asking.abort(); document.querySelectorAll(".cb-pop:not([hidden])").forEach((p) => p.dispatchEvent(new Event("cb-close"))); } };
  };

  // ══ Catalog jobs page (#sql/jobs): every refresh job, newest first, live while running ══════════
  views["sql/jobs"] = function (root) {
    document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === "sql"));
    const TERMINAL = ["succeeded", "failed", "cancelled", "interrupted"];
    const open = new Set(), full = {};
    let timer = null, alive = true;
    root.innerHTML = `<div class="bsp-page-head"><div><h1>Catalog jobs</h1><div class="lede">Refresh jobs for catalog definitions: scope, options, progress and logs. Kept on the appliance (last 100), also across restarts.</div></div>
      <div class="row"><a class="btn ghost sm" href="#sql">${BSP.iconHtml("arrow-left", 14)}<span>Federated SQL</span></a><button class="btn secondary sm" id="jr">${BSP.iconHtml("refresh", 14)}<span>Reload</span></button></div></div>
      <div id="jlist" class="stack"></div>`;
    const list = $("#jlist", root);
    const fmt = (t) => (t ? new Date(t * 1000).toLocaleString() : "");
    const dur = (j) => { const e = (j.finished || Date.now() / 1000) - (j.started || 0); return e < 90 ? `${Math.round(e)} s` : `${Math.round(e / 60)} min`; };
    const scopeText = (sc) => (!sc || sc.all ? "All systems" : [sc.catalog, sc.schema].filter(Boolean).join(".") + (sc.tables && sc.tables.length ? ` (${sc.tables.join(", ")})` : ""));
    const stateChip = (st) => h("span", { class: "chip " + (st === "succeeded" ? "primary" : st === "running" || st === "queued" ? "auto" : "") }, st);
    const COUNT_LABELS = [["tables_scanned", "tables read"], ["views_scanned", "views read"], ["events_produced", "metadata events"], ["tables_seen", "tables checked"],
      ["tables_annotated", "tables annotated"], ["columns_described", "columns annotated"], ["skipped", "skipped (human-written kept)"], ["pii_columns", "PII columns tagged"],
      ["tables_pii_suspected", "tables with suspected PII"], ["terms_linked", "terms linked"], ["schemas", "schemas"], ["errors", "errors"]];
    function card(j) {
      const running = !TERMINAL.includes(j.state);
      const counts = COUNT_LABELS.filter(([k]) => j.counts && j.counts[k] != null).map(([k, l]) => h("span", { class: "chip outline" }, `${j.counts[k]} ${l}`));
      const acts = h("div", { class: "row" },
        j.cancellable ? h("button", { class: "btn ghost sm", onclick: async () => { await api(`/sql/catalog/jobs/${j.job_id}/cancel`, { method: "POST" }); load(); } }, icon("stop", 13), h("span", null, "Cancel")) : null,
        !running ? h("button", { class: "btn secondary sm", onclick: async () => { try { await api(`/sql/catalog/jobs/${j.job_id}/retry`, { method: "POST" }); toast("Retry started", "info"); load(); } catch (e) { toast(e.message, "error"); } } }, icon("sync", 13), h("span", null, "Retry")) : null);
      const logOpen = open.has(j.job_id);
      const logEl = h("pre", { class: "codeblock", style: "max-height:420px;overflow:auto;white-space:pre-wrap" }, (logOpen && full[j.job_id]) || j.log_tail || "(no log yet)");
      const toggle = h("button", { type: "button", class: "link-btn", onclick: async () => {
        if (logOpen) open.delete(j.job_id); else { open.add(j.job_id); try { full[j.job_id] = (await api(`/sql/catalog/jobs/${j.job_id}?full=true`)).log; } catch (_) { /* tail is enough */ } }
        render(); } }, logOpen ? "Show log tail" : "Show full log");
      return h("div", { class: "card tight" },
        h("div", { class: "card-head" }, h("div", { class: "row wrap" }, running ? h("span", { class: "spin", style: "width:14px;height:14px;border:2px solid var(--bsp-teal-100);border-top-color:var(--bsp-teal-600);border-radius:50%;animation:bsp-spin .7s linear infinite;display:inline-block" }) : null,
          h("b", null, scopeText(j.scope)), stateChip(j.state), j.phase && running ? h("span", { class: "caption" }, j.phase) : null), acts),
        h("div", { class: "caption" }, [j.backend === "datahub" ? "DataHub" : "Trino structure", j.reannotate ? "re-annotate with AI" : "structure only", j.force ? "overwrite AI suggestions" : null,
          `by ${j.user}`, `started ${fmt(j.started)}`, j.finished ? `finished ${fmt(j.finished)}` : "running", dur(j), j.retry_of ? `retry of ${j.retry_of}` : null].filter(Boolean).join(" · ")),
        counts.length ? h("div", { class: "row wrap", style: "margin:8px 0" }, ...counts) : null,
        h("details", { open: running || logOpen ? "" : null }, h("summary", { class: "caption" }, "Log"), logEl, toggle));
    }
    let data = { jobs: [] };
    function render() {
      list.replaceChildren(...(data.jobs.length ? data.jobs.map(card) : [h("div", { class: "empty" }, h("h4", null, "No refresh jobs yet"), h("div", null, "Start one from Federated SQL, Catalog, Refresh definitions."))]));
      if (data.admin) list.prepend(h("div", { class: "caption" }, "You are an admin: jobs of every user are listed."));
    }
    async function load() {
      clearTimeout(timer);
      try { data = await api("/sql/catalog/jobs?limit=100"); render(); } catch (e) { list.replaceChildren(h("div", { class: "alert error" }, icon("error"), h("div", null, e.message))); }
      if (!alive) return;
      const anyRunning = data.jobs.some((j) => !TERMINAL.includes(j.state));
      timer = setTimeout(load, anyRunning ? 2000 : 10000);
    }
    $("#jr", root).onclick = load;
    load();
    return { destroy() { alive = false; clearTimeout(timer); } };
  };
  window.BSP_AI.formatSql = formatSql;
})();
