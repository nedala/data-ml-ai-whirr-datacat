/* AI harness UI — Playground (chat, VLM, MCP agent loop, live browser), Compare,
   Routing editor, Telemetry, Evals, Provider status. Vanilla JS on window.BSP. */
(function () {
  "use strict";
  const { esc, toast } = BSP;
  const api = BSP.api("/ai/api");
  const $ = (s, r) => (r || document).querySelector(s);
  const main = $("#main");
  const store = {
    get(k, d) { try { const v = localStorage.getItem("bsp-ai:" + k); return v == null ? d : JSON.parse(v); } catch (_) { return d; } },
    set(k, v) { try { localStorage.setItem("bsp-ai:" + k, JSON.stringify(v)); } catch (_) {} },
  };
  let MODELS = [];

  async function loadModels() {
    try {
      const r = await fetch("/ai/api/models", { credentials: "same-origin" });
      if (r.ok) MODELS = (await r.json()).data || [];
    } catch (_) { MODELS = []; }
    return MODELS;
  }
  function modelOptions(kind, selected) {
    const chat = MODELS.filter((m) => (kind === "embed" ? m.kind === "embed" : m.kind !== "embed"));
    const al = chat.filter((m) => m.alias), cc = chat.filter((m) => !m.alias);
    const opt = (m) => `<option value="${esc(m.id)}" ${m.id === selected ? "selected" : ""}>${esc(m.alias ? `${m.id} — ${m.name || ""}` : m.id)}</option>`;
    return `<optgroup label="Aliases">${al.map(opt).join("")}</optgroup><optgroup label="Concrete provider/model">${cc.map(opt).join("")}</optgroup>`;
  }

  // POST + Server-Sent Events reader
  async function sse(path, body, onEvent, signal) {
    const r = await fetch("/ai/api" + path, { method: "POST", headers: { "Content-Type": "application/json", "X-Studio": "1" },
      body: JSON.stringify(body), credentials: "same-origin", signal });
    if (!r.ok) throw new Error((await r.text()) || r.statusText);
    const rd = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await rd.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        for (const line of chunk.split("\n")) if (line.startsWith("data: ")) { try { onEvent(JSON.parse(line.slice(6))); } catch (e) { console.warn(e); } }
      }
    }
  }

  // ── rich rendering for the agent playground ───────────────────────────────
  // Tool calls and results arrive as JSON-ish text. Render them the way a data
  // tool should be read: SQL/JSON in highlighted code, markdown summaries as
  // tables, structured rows as a real table (plus a bar chart when it is a
  // series), and the untouched payload behind a "raw" toggle. Text is escaped
  // before any markup is added, so tool/model output cannot inject HTML.
  const codeEsc = (s) => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const SQL_WORDS = "select|from|where|group by|order by|having|limit|offset|join|left|right|inner|outer|full|cross|on|as|and|or|not|null|is|in|between|like|ilike|case|when|then|else|end|with|union|all|distinct|count|sum|avg|min|max|round|cast|coalesce|desc|asc|over|partition by|exists";
  function hlCode(code, lang) {
    const s = codeEsc(code);
    if (lang === "sql") {
      return s.replace(new RegExp(`(--[^\\n]*)|('(?:[^']|'')*')|\\b(${SQL_WORDS})\\b|\\b(\\d+(?:\\.\\d+)?)\\b`, "gi"),
        (m, com, str, kw, num) => com ? `<span class="tok-com">${com}</span>` : str ? `<span class="tok-str">${str}</span>`
          : kw ? `<span class="tok-kw">${kw}</span>` : num ? `<span class="tok-num">${num}</span>` : m);
    }
    if (lang === "json") {
      return s.replace(/("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
        (m, str, colon, kw, num) => str ? (colon ? `<span class="tok-key">${str}</span>${colon}` : `<span class="tok-str">${str}</span>`)
          : kw ? `<span class="tok-bool">${kw}</span>` : num ? `<span class="tok-num">${num}</span>` : m);
    }
    return s;
  }
  function guessLang(t) {
    const s = String(t == null ? "" : t).trim();
    if (!s) return "text";
    if (s[0] === "{" || s[0] === "[") { try { JSON.parse(s); return "json"; } catch (_) { /* not JSON */ } }
    if (/^\s*(select|with)\b/i.test(s)) return "sql";
    return "text";
  }
  function copyBtn(text, pre) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "copy-btn"; b.title = "Copy to clipboard"; b.textContent = "copy";
    b.onclick = (e) => { e.preventDefault(); BSP.copy(text, "Copied"); };
    pre.appendChild(b);
    return b;
  }
  function codeBlock(code, lang, label) {
    const wrap = document.createElement("div"); wrap.className = "code";
    if (label) { const l = document.createElement("div"); l.className = "code-label"; l.textContent = label; wrap.appendChild(l); }
    const pre = document.createElement("pre"); const c = document.createElement("code");
    const l2 = lang || guessLang(code);
    c.className = "lang-" + l2; c.innerHTML = hlCode(code, l2);
    pre.appendChild(c); copyBtn(code, pre); wrap.appendChild(pre);
    return wrap;
  }
  function kvChips(obj) {
    const w = document.createElement("div"); w.className = "tool-kv";
    for (const [k, v] of Object.entries(obj || {})) {
      const s = document.createElement("span"); s.className = "chip outline";
      s.textContent = `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`;
      w.appendChild(s);
    }
    return w;
  }
  function highlightIn(container) {
    if (!container) return;
    container.querySelectorAll("pre.md-code > code").forEach((c) => {
      const t = c.textContent || "", lang = guessLang(t);
      if (lang === "sql" || lang === "json") c.innerHTML = hlCode(t, lang);
      const pre = c.parentElement;
      if (pre && !pre.querySelector(".copy-btn")) copyBtn(t, pre);
    });
  }
  const looksMarkdown = (t) => typeof t === "string" && (/^\s*\|.+\|\s*$/m.test(t) || /```/.test(t) || /^#{1,4}\s/m.test(t) || /^\s*[-*]\s+/m.test(t));
  function parseMaybe(t) {
    const s = String(t == null ? "" : t).trim();
    if (!s || (s[0] !== "{" && s[0] !== "[")) return null;
    try { return JSON.parse(s); } catch (_) { return null; }
  }
  function mdBlock(text) {
    const d = document.createElement("div"); d.className = "md";
    d.innerHTML = BSP.md ? BSP.md(text || "") : esc(text || "");
    highlightIn(d);
    return d;
  }
  function chartFor(rows, cols) {
    if (rows.length < 2 || rows.length > 60) return null;
    const numeric = (c) => rows.every((r) => r[c] == null || typeof r[c] === "number") && rows.some((r) => typeof r[c] === "number");
    const vals = cols.filter(numeric);
    if (vals.length !== 1) return null;
    const val = vals[0], lab = cols.find((c) => c !== val);
    const max = Math.max(1, ...rows.map((r) => Math.abs(+r[val] || 0)));
    const W = 620, H = 150, bw = W / rows.length;
    const bars = rows.map((r, i) => {
      const v = +r[val] || 0, h = Math.max(1, (Math.abs(v) / max) * (H - 26));
      return `<rect class="qbar" x="${(i * bw + 2).toFixed(1)}" y="${(H - 18 - h).toFixed(1)}" width="${Math.max(1, bw - 4).toFixed(1)}" height="${h.toFixed(1)}"><title>${esc(lab ? String(r[lab]) : i + 1)}: ${esc(String(v))}</title></rect>`;
    }).join("");
    const wrap = document.createElement("div"); wrap.className = "tool-chart";
    const cap = document.createElement("div"); cap.className = "caption";
    cap.textContent = `chart · ${val}${lab ? " by " + lab : ""}`;
    const box = document.createElement("div");
    box.innerHTML = `<svg class="chart qchart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${bars}<text x="0" y="10">${esc(String(max))}</text></svg>`;
    wrap.append(cap, box.firstChild);
    return wrap;
  }
  function rowsBlock(rows) {
    const d = document.createElement("details"); d.className = "tool-data";
    d.appendChild(BSP.h("summary", null, `Data · ${rows.length} row${rows.length === 1 ? "" : "s"}`));
    const cols = [...new Set(rows.flatMap((r) => Object.keys(r)))];
    const numeric = (c) => rows.every((r) => r[c] == null || typeof r[c] === "number");
    const t = BSP.table({ columns: cols.map((c) => ({ key: c, label: c, sort: true, num: numeric(c) })), rows, pageSize: 25 });
    d.appendChild(t.el);
    const ch = chartFor(rows, cols); if (ch) d.appendChild(ch);
    return d;
  }
  function rawBlock(text) {
    const d = document.createElement("details"); d.className = "tool-raw";
    d.appendChild(BSP.h("summary", null, "Raw response"));
    d.appendChild(codeBlock(text || "", "json", null));
    return d;
  }
  function argsBlock(args) {
    const wrap = document.createElement("div");
    if (args && typeof args.sql === "string") {
      wrap.appendChild(codeBlock(args.sql, "sql", "SQL"));
      const rest = Object.fromEntries(Object.entries(args).filter(([k]) => k !== "sql"));
      if (Object.keys(rest).length) wrap.appendChild(kvChips(rest));
    } else {
      wrap.appendChild(codeBlock(JSON.stringify(args == null ? {} : args, null, 2), "json", null));
    }
    return wrap;
  }
  function resultBlock(ev) {
    const wrap = document.createElement("div");
    if (!ev.ok) {
      const a = document.createElement("div"); a.className = "alert error"; a.style.whiteSpace = "pre-wrap";
      a.textContent = ev.text || "Tool failed"; wrap.appendChild(a); return wrap;
    }
    const j = parseMaybe(ev.text);
    if (j && typeof j === "object") {
      const obj = Array.isArray(j) ? { rows: j } : j;
      const rows = Array.isArray(obj.rows) ? obj.rows : Array.isArray(obj.items) ? obj.items : null;
      if (typeof obj.summary === "string" && obj.summary.trim()) wrap.appendChild(mdBlock(obj.summary));
      else if (!(rows && rows.length)) wrap.appendChild(codeBlock(JSON.stringify(obj, null, 2), "json", null));
      const meta = {};
      for (const [k, v] of Object.entries(obj))
        if (!["summary", "rows", "columns", "items"].includes(k) && (v == null || ["string", "number", "boolean"].includes(typeof v))) meta[k] = v;
      if (Object.keys(meta).length) wrap.appendChild(kvChips(meta));
      if (rows && rows.length && rows.every((r) => r && typeof r === "object" && !Array.isArray(r))) wrap.appendChild(rowsBlock(rows));
      if (Object.keys(obj).length > 1 || !obj.summary) wrap.appendChild(rawBlock(ev.text));
    } else if (looksMarkdown(ev.text)) {
      wrap.appendChild(mdBlock(ev.text));
    } else {
      wrap.appendChild(codeBlock(ev.text || "(empty)", guessLang(ev.text), null));
    }
    return wrap;
  }

  // ── router ────────────────────────────────────────────────────────────────
  const views = {};
  // extra tabs in their own files (sqlstudio.js) register here: window.BSP_AI.views.<name> = fn(root)
  window.BSP_AI = { views, api, sse, store, esc };
  let current = null;
  function route() {
    const v = (location.hash || "#playground").slice(1).split("?")[0];
    document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === v));
    if (current && current.destroy) current.destroy();
    main.innerHTML = "";
    current = (views[v] || views.playground)(main) || null;
  }
  window.addEventListener("hashchange", route);

  // ══ Playground ═════════════════════════════════════════════════════════════
  views.playground = function (root) {
    const st = { messages: [], image: null, abort: null, live: null, tl: [], id: null };
    const cfg = store.get("pg", { preset: "", model: "chat-default", system: "You are a helpful assistant for DataPlatform's data & AI teams.",
      temperature: 0, json: false, dc: "", tools: false, groups: [], ground: false, collection: "", autoLive: true, maxSteps: 8 });
    // one-time move of the old 0.3 default to deterministic 0.0 (a value the user picked is kept)
    if (!cfg.tempV2) { if (cfg.temperature === 0.3) cfg.temperature = 0; cfg.tempV2 = true; store.set("pg", cfg); }
    root.innerHTML = `
      <div class="bsp-page-head"><div><h1>Playground</h1><div class="lede">Chat with any alias or concrete model, attach an image for vision models, or let an agent call MCP tools (Atlan, BigQuery, warehouse, M365, browser…).</div></div>
        <div class="row"><button class="btn ghost sm" id="history-btn">${BSP.iconHtml("history", 15)}<span>History</span></button><button class="btn secondary sm" id="live-btn">Live browser</button><button class="btn ghost sm" id="clear">${BSP.iconHtml("add", 15)}<span>New chat</span></button></div></div>
      <div class="pg" id="pg">
        <div class="card tight pg-settings">
          <div class="field"><label>Agent preset</label><select class="select input" id="preset"><option value="">Custom</option></select><span class="hint" id="preset-hint">Pick a workflow preset or configure the route and tool groups manually.</span></div>
          <div class="field"><label>Model / route</label><select class="select input" id="model">${modelOptions("chat", cfg.model)}</select></div>
          <div class="field"><label>System prompt</label><textarea class="textarea" id="system">${esc(cfg.system)}</textarea></div>
          <div class="field"><label>Temperature <span class="caption" id="tval">${cfg.temperature}</span></label><input type="range" min="0" max="1.5" step="0.1" value="${cfg.temperature}" id="temp"></div>
          <div class="field"><label>Data classification</label><select class="select input" id="dc">
            ${["", "internal", "confidential", "restricted"].map((x) => `<option value="${x}" ${x === cfg.dc ? "selected" : ""}>${x || "(default policy)"}</option>`).join("")}</select>
            <span class="hint">restricted → local providers only</span></div>
          <label class="switch"><input type="checkbox" id="json" ${cfg.json ? "checked" : ""}><span class="track"></span>JSON mode</label>
          <hr class="divider">
          <label class="switch"><input type="checkbox" id="ground" ${cfg.ground ? "checked" : ""}><span class="track"></span>Ground with knowledge base</label>
          <div class="field" style="margin-top:8px"><select class="select input" id="collection"><option value="">(all collections)</option></select><span class="hint" id="kb-hint"></span></div>
          <hr class="divider">
          <label class="switch"><input type="checkbox" id="tools" ${cfg.tools ? "checked" : ""}><span class="track"></span>MCP tools (agent loop)</label>
          <div class="groups" id="groups" style="margin-top:8px"><span class="caption">loading tools…</span></div>
          <label class="checkbox" style="margin-top:8px"><input type="checkbox" id="autolive" ${cfg.autoLive ? "checked" : ""}>Open live browser on first browser tool call</label>
        </div>
        <div class="card tight chat">
          <div class="chat-log" id="log"><div class="empty muted" id="empty">Ask something — or pick a suggestion below.</div></div>
          <div class="chat-input">
            <label class="btn ghost" title="Attach an image for vision models (VLM)">${BSP.iconHtml("image", 16)}<span>Attach image</span><input type="file" id="img" accept="image/*" hidden></label>
            <textarea class="textarea" id="input" placeholder="Message… (Enter to send, Shift+Enter for a new line)"></textarea>
            <button class="btn primary" id="send">${BSP.iconHtml("send", 16)}<span>Send</span></button>
          </div>
          <div class="suggest" id="suggest" aria-label="Suggested questions"></div>
          <div class="caption" id="attach"></div>
        </div>
      </div>`;
    const log = $("#log", root);
    let presets = {};
    const save = () => {
      Object.assign(cfg, { preset: $("#preset").value, model: $("#model").value, system: $("#system").value, temperature: +$("#temp").value, json: $("#json").checked,
        dc: $("#dc").value, tools: $("#tools").checked, ground: $("#ground").checked, collection: $("#collection").value, autoLive: $("#autolive").checked,
        groups: [...root.querySelectorAll("#groups input:checked")].map((i) => i.value) });
      store.set("pg", cfg);
    };
    function applyPreset(id, persist = true) {
      const p = presets[id];
      if (!p) return;
      Object.assign(cfg, { preset: id, model: p.model || cfg.model, system: p.system || cfg.system, tools: true,
        groups: (p.groups || []).slice(), maxSteps: p.max_steps || cfg.maxSteps || 8,
        temperature: p.temperature != null ? p.temperature : 0 });
      const modelSel = $("#model");
      if ([...modelSel.options].some((o) => o.value === cfg.model)) modelSel.value = cfg.model;
      $("#system").value = cfg.system;
      $("#tools").checked = true;
      $("#temp").value = cfg.temperature;
      $("#tval").textContent = cfg.temperature;
      $("#preset").value = id;
      root.querySelectorAll("#groups input").forEach((i) => { i.checked = cfg.groups.includes(i.value); });
      $("#preset-hint").textContent = p.description || "";
      if (persist) save(); else store.set("pg", cfg);
    }
    async function loadPresets() {
      try {
        const r = await api("/agents");
        presets = Object.fromEntries((r.agents || []).map((a) => [a.id, a]));
        $("#preset").innerHTML = `<option value="">Custom</option>` +
          (r.agents || []).map((a) => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join("");
        if (cfg.preset && presets[cfg.preset]) applyPreset(cfg.preset, false);
        else if (cfg.preset) { cfg.preset = ""; $("#preset").value = ""; }
      } catch (e) { /* presets are optional; keep Custom */ }
    }
    $("#preset").onchange = (e) => {
      if (presets[e.target.value]) applyPreset(e.target.value);
      else { cfg.preset = ""; $("#preset-hint").textContent = "Custom settings"; save(); }
    };
    root.addEventListener("change", save);
    $("#temp").addEventListener("input", (e) => { $("#tval").textContent = e.target.value; });
    const emptyHint = `<div class="empty muted" id="empty">Ask something — or pick a suggestion below.</div>`;
    function resetChat() { st.messages = []; st.id = null; log.innerHTML = emptyHint; st.tl = []; renderTimeline(); suggest(); }
    $("#clear").onclick = resetChat;

    // ── history: every conversation is saved on the appliance for the signed-in user ──
    async function persist() {
      if (!st.messages.length) return;
      try {
        const r = await api(`/chats/${st.id || "new"}`, { method: "PUT", body: { messages: st.messages, model: cfg.model, timeline: st.tl } });
        st.id = r.id;
      } catch (e) { /* history is best effort */ }
    }
    function plain(c) { return typeof c === "string" ? c : (Array.isArray(c) ? c.filter((p) => p.type === "text").map((p) => p.text).join(" ") : ""); }
    async function openChat(id) {
      const c = await api(`/chats/${id}`);
      st.id = c.id; st.messages = c.messages || []; st.tl = c.timeline || []; renderTimeline();
      log.innerHTML = "";
      st.messages.forEach((m) => {
        if (m.role === "user") bubble("user", plain(m.content));
        else if (m.role === "assistant" && plain(m.content)) bubble("assistant", plain(m.content));
      });
      if (!log.children.length) log.innerHTML = emptyHint;
      suggest();
    }
    $("#history-btn").onclick = async () => {
      const list = BSP.h("div", { class: "history-list" }, BSP.h("div", { class: "caption" }, "Loading…"));
      const dr = BSP.drawer({ title: "Chat history", body: list });
      const draw = async () => {
        const { chats } = await api("/chats");
        list.replaceChildren(...(chats.length ? chats.map((c) => {
          const del = BSP.h("button", { class: "btn ghost icon sm", title: "Delete chat", "aria-label": `Delete ${c.title}` }, BSP.icon("delete", 15));
          del.onclick = async (e) => { e.stopPropagation(); await api(`/chats/${c.id}`, { method: "DELETE" }); if (c.id === st.id) resetChat(); draw(); };
          const row = BSP.h("button", { type: "button", class: `history-item ${c.id === st.id ? "active" : ""}` },
            BSP.h("span", { class: "t" }, c.title), BSP.h("span", { class: "m" }, `${BSP.fmtAgo(c.updated)} · ${c.turns} message${c.turns === 1 ? "" : "s"}${c.model ? " · " + c.model : ""}`), del);
          row.onclick = async () => { await openChat(c.id); dr.close(); };
          return row;
        }) : [BSP.h("div", { class: "empty muted" }, "No saved chats yet — they appear here automatically after each answer.")]));
      };
      draw();
    };

    // ── next best questions ──
    async function suggest() {
      const box = $("#suggest", root);
      const groups = cfg.tools ? cfg.groups.length ? cfg.groups : [...root.querySelectorAll("#groups input")].map((i) => i.value) : [];
      const convo = st.messages.filter((m) => m.role === "user" || m.role === "assistant").map((m) => ({ role: m.role, content: plain(m.content) })).filter((m) => m.content);
      box.innerHTML = convo.length ? `<span class="caption">Thinking of next questions…</span>` : "";
      try {
        const r = await api("/suggest", { body: { messages: convo, groups } });
        box.replaceChildren(...(r.questions || []).map((q) => {
          const chip = BSP.h("button", { type: "button", class: "suggest-chip", title: "Ask this" }, q);
          chip.onclick = () => {
            const hint = (r.model_hints || {})[q];
            const sel = $("#model");
            if (hint && sel && cfg.model !== hint && [...sel.options].some((o) => o.value === hint)) {
              sel.value = hint; save();
              BSP.toast("Cross-system question: using the Data Platform SQL model", "info");
            }
            input.value = q; send();
          };
          return chip;
        }));
        if (r.questions && r.questions.length) box.prepend(BSP.h("span", { class: "caption" }, convo.length ? "Next questions" : "Try asking"));
      } catch (e) { box.innerHTML = ""; }
    }

    // MCP tool groups
    api("/mcp/tools").then((r) => {
      const el = $("#groups", root);
      if (!r.ok) { el.innerHTML = `<span class="caption">MCP server not reachable (${esc(r.error || "")}) — start AI Tools in Studio</span>`; return; }
      const labels = { atlan: "Atlan", bigquery: "BigQuery", browser: "Browser", m365: "Microsoft 365", mlflow: "MLflow",
        powerbi: "Power BI", rag: "Knowledge base", sap: "SAP BRIM", trino: "Trino", warehouse: "Warehouse" };
      el.innerHTML = `<div class="group-tools"><span class="caption">Tool groups</span><span><button type="button" class="link-btn" data-all="1">All</button> · <button type="button" class="link-btn" data-all="0">None</button></span></div>` +
        `<div class="group-chips">` + Object.entries(r.groups).map(([g, n]) =>
          `<label class="tool-chip" title="${n} ${esc(labels[g] || g)} tool${n === 1 ? "" : "s"}"><input type="checkbox" value="${esc(g)}" ${!cfg.groups.length || cfg.groups.includes(g) ? "checked" : ""}><span class="nm">${esc(labels[g] || g)}</span><span class="ct">${n}</span></label>`).join("") + `</div>`;
      el.querySelectorAll("[data-all]").forEach((b) => (b.onclick = () => {
        el.querySelectorAll(".tool-chip input").forEach((i) => (i.checked = b.dataset.all === "1"));
        el.querySelector(".tool-chip input").dispatchEvent(new Event("change", { bubbles: true }));
      }));
      loadPresets();
    }).catch(() => {});
    // knowledge base collections
    api("/rag/collections").then((r) => {
      if (!r.ok) { $("#kb-hint").textContent = r.error || "knowledge base not reachable"; return; }
      $("#collection").innerHTML = `<option value="">(all collections)</option>` + r.collections.map((c) => `<option ${c === cfg.collection ? "selected" : ""}>${esc(c)}</option>`).join("");
    }).catch(() => { $("#kb-hint").textContent = "knowledge base not reachable"; });

    $("#img").onchange = (e) => {
      const f = e.target.files[0]; if (!f) return;
      const rd = new FileReader();
      rd.onload = () => { st.image = rd.result; $("#attach").innerHTML = `image attached: ${esc(f.name)} <a href="#" id="rm-img">remove</a>`; $("#rm-img").onclick = (ev) => { ev.preventDefault(); st.image = null; $("#attach").textContent = ""; }; };
      rd.readAsDataURL(f);
    };
    const input = $("#input");
    input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
    $("#send").onclick = send;

    function bubble(role, text, img) {
      $("#empty") && $("#empty").remove();
      const d = BSP.h("div", { class: "msg " + role });
      if (img) d.appendChild(BSP.h("img", { class: "att", src: img }));
      // assistant answers are Markdown → rendered (escaped first by BSP.md); user / error text stays plain
      const t = BSP.h("div", { class: role === "assistant" ? "md" : "" });
      const set = (s) => { if (role === "assistant" && BSP.md) { t.innerHTML = BSP.md(s || ""); highlightIn(t); } else t.textContent = s || ""; };
      set(text);
      d.appendChild(t);
      log.appendChild(d); log.scrollTop = log.scrollHeight;
      return { el: d, text: t, set, meta(html) { let m = d.querySelector(".meta"); if (!m) { m = BSP.h("div", { class: "meta" }); d.appendChild(m); } m.innerHTML = html; } };
    }
    function cites(b, list) {
      if (!list || !list.length) return;
      const ul = BSP.h("ol", { class: "cites" });
      list.forEach((c) => { const li = BSP.h("li"); li.innerHTML = `<b>${esc(c.title || c.source || "excerpt")}</b> <span class="caption">${esc(c.source || "")}${c.score != null ? " · " + (+c.score).toFixed(3) : ""}</span>`; ul.appendChild(li); });
      b.el.appendChild(ul);
    }

    async function send() {
      const text = input.value.trim();
      if (!text && !st.image) return;
      save();
      const content = st.image ? [{ type: "text", text }, { type: "image_url", image_url: { url: st.image } }] : text;
      bubble("user", text, st.image);
      st.messages.push({ role: "user", content });
      input.value = ""; st.image = null; $("#attach").textContent = "";
      const msgs = (cfg.system ? [{ role: "system", content: cfg.system }] : []).concat(st.messages);
      const ground = { enabled: cfg.ground, collection: cfg.collection || null };
      const btn = $("#send"); btn.classList.add("loading");
      $("#suggest", root).innerHTML = "";
      try {
        if (cfg.tools) await runAgent(msgs, ground); else await runChat(msgs, ground);
      } catch (e) { bubble("error", String(e.message || e)); }
      finally { btn.classList.remove("loading"); }
      persist(); suggest();
    }

    async function runChat(msgs, ground) {
      const b = bubble("assistant", "");
      let acc = "", route = null, pending = null;
      await sse("/chat", { model: cfg.model, messages: msgs, temperature: cfg.temperature, json_mode: cfg.json, data_classification: cfg.dc || null, ground }, (ev) => {
        if (ev.type === "citations") pending = ev.citations;
        else if (ev.type === "route") route = ev;
        else if (ev.type === "delta") { acc += ev.content; b.set(acc); log.scrollTop = log.scrollHeight; }
        else if (ev.type === "error") { b.el.classList.add("error"); b.text.textContent = acc + (acc ? "\n" : "") + "Error: " + ev.message; }
        else if (ev.type === "done") {
          const u = ev.usage || {};
          b.meta(`${esc(route ? route.provider + "/" + route.model : "")} · ${ev.ms} ms · ${u.prompt_tokens || 0}→${u.completion_tokens || 0} tokens` +
            (route && route.attempts && route.attempts.length > 1 ? ` · fallback: ${route.attempts.filter((a) => a.status !== "ok").map((a) => esc(a.route)).join(", ")} skipped` : ""));
        }
      });
      cites(b, pending);
      st.messages.push({ role: "assistant", content: acc });
    }

    async function runAgent(msgs, ground) {
      let pendingCites = null, sawBrowser = false;
      const toolEls = {};
      await sse("/agent", { model: cfg.model, messages: msgs, temperature: cfg.temperature, groups: cfg.groups, max_steps: cfg.maxSteps, ground }, (ev) => {
        if (ev.type === "citations") pendingCites = ev.citations;
        else if (ev.type === "tool_call") {
          const d = BSP.h("details", { class: "tool-ev", open: true });
          d.innerHTML = `<summary>${BSP.iconHtml("tool", 13)} <b>${esc(ev.name)}</b> <span class="caption">running…</span></summary>`;
          const body = document.createElement("div"); body.className = "tool-body";
          body.appendChild(argsBlock(ev.args));
          d.appendChild(body);
          log.appendChild(d); log.scrollTop = log.scrollHeight; toolEls[ev.id] = d;
          if (ev.group === "browser") {
            if (!sawBrowser && cfg.autoLive && !st.live) openLive();
            sawBrowser = true;
            st.tl.push({ id: ev.id, name: ev.name, args: ev.args, status: "running", t0: Date.now() }); renderTimeline();
          }
        } else if (ev.type === "tool_result") {
          const d = toolEls[ev.id];
          if (d) {
            const cap = d.querySelector("summary .caption"); if (cap) cap.textContent = `${ev.ok ? "ok" : "error"} · ${ev.ms} ms`;
            d.classList.toggle("err", !ev.ok);
            const body = d.querySelector(".tool-body"); if (body) body.replaceChildren(resultBlock(ev));
            (ev.images || []).forEach((src) => d.appendChild(BSP.h("img", { src, style: "max-width:100%;margin-top:6px" })));
          }
          const it = st.tl.find((x) => x.id === ev.id);
          if (it) { Object.assign(it, { status: ev.ok ? "ok" : "error", ms: ev.ms, thumb: ev.thumbnail, text: ev.text }); renderTimeline(); }
        } else if (ev.type === "thought") bubble("assistant", ev.content).meta("thinking before tool calls");
        else if (ev.type === "route") { /* per-step route shown on final */ st.lastRoute = ev; }
        else if (ev.type === "message") {
          const b = bubble("assistant", ev.content);
          const r = st.lastRoute; if (r) b.meta(`${esc(r.provider + "/" + r.model)} · ${r.latency_ms} ms (last step)`);
          cites(b, pendingCites);
        } else if (ev.type === "error") bubble("error", ev.message);
        else if (ev.type === "done") st.messages = ev.messages.filter((m) => m.role !== "system");
      });
    }

    // ── live browser panel (lazy iframe of the gated same-origin noVNC route) ──
    $("#live-btn").onclick = () => (st.live ? closeLive() : openLive());
    async function openLive(control) {
      const pg = $("#pg", root);
      if (st.live) st.live.remove();
      const panel = BSP.h("div", { class: "live" });
      panel.innerHTML = `<div class="live-resize" title="drag to resize"></div>
        <div class="row between"><b>Live browser</b><div class="row">
          <label class="switch"><input type="checkbox" id="ctl" ${control ? "checked" : ""}><span class="track"></span>Take control</label>
          <button class="btn ghost sm" id="live-close">Close</button></div></div>
        <div class="live-frame" id="live-frame"><div class="notice">Connecting to the desktop…</div></div>
        <div class="eyebrow">Browser tool calls</div><div class="timeline" id="timeline"></div>`;
      pg.appendChild(panel); pg.classList.add("with-live"); st.live = panel;
      pg.style.setProperty("--live-w", store.get("liveW", 520) + "px");
      $("#live-close", panel).onclick = closeLive;
      $("#ctl", panel).onchange = (e) => openLive(e.target.checked);
      dragResize($(".live-resize", panel), pg);
      renderTimeline();
      let s;
      try { s = await api("/browser/status"); } catch (e) { s = { ok: false, error: String(e.message || e) }; }
      const fr = $("#live-frame", panel);
      if (!st.live || st.live !== panel) return;
      if (!s.ok) { fr.innerHTML = `<div class="notice"><b>The Browser capability isn't running.</b><br>${esc(s.error || "")}<br><br><a class="btn primary sm" href="${esc(s.studio || "/studio/#compose")}" target="_blank">Start it in Studio</a></div>`; return; }
      const src = s.iframe + (s.iframe.includes("?") ? "&" : "?") + "view_only=" + (control ? "0" : "1");
      fr.innerHTML = ""; fr.appendChild(BSP.h("iframe", { src, title: "Appliance desktop (noVNC)", allow: "clipboard-read; clipboard-write" }));
    }
    function closeLive() {
      if (!st.live) return;
      const fr = st.live.querySelector("iframe"); if (fr) fr.src = "about:blank";  // drop the VNC websocket
      st.live.remove(); st.live = null; $("#pg", root).classList.remove("with-live");
    }
    function dragResize(handle, pg) {
      handle.addEventListener("mousedown", (e) => {
        e.preventDefault(); const x0 = e.clientX; const w0 = st.live.getBoundingClientRect().width;
        const fr = st.live.querySelector("iframe"); if (fr) fr.style.pointerEvents = "none";
        const mv = (ev) => { const w = Math.max(320, Math.min(window.innerWidth * 0.7, w0 + (x0 - ev.clientX))); pg.style.setProperty("--live-w", w + "px"); store.set("liveW", Math.round(w)); };
        const up = () => { document.removeEventListener("mousemove", mv); document.removeEventListener("mouseup", up); if (fr) fr.style.pointerEvents = ""; };
        document.addEventListener("mousemove", mv); document.addEventListener("mouseup", up);
      });
    }
    function renderTimeline() {
      const el = st.live && st.live.querySelector("#timeline"); if (!el) return;
      if (!st.tl.length) { el.innerHTML = `<div class="empty muted" style="padding:12px">Browser tool calls from the agent appear here, in sync with the live view.</div>`; return; }
      el.innerHTML = st.tl.map((x) => `<div class="tl-item ${x.status === "running" ? "running" : x.status === "error" ? "err" : ""}">
        <div><b>${esc(x.name.replace("browser_", ""))}</b> <span class="caption">${x.status === "running" ? "running…" : esc(x.status) + " · " + x.ms + " ms"}</span><br><code>${esc(JSON.stringify(x.args))}</code></div>
        <div>${x.thumb ? `<img src="${x.thumb}" data-full="${x.thumb}">` : ""}</div></div>`).join("");
      el.querySelectorAll("img").forEach((im) => (im.onclick = () => BSP.modal({ title: "Screenshot", body: BSP.h("img", { src: im.dataset.full, style: "max-width:100%" }), wide: true })));
      el.scrollTop = el.scrollHeight;
    }
    return { destroy: closeLive };
  };

  // ══ Compare ════════════════════════════════════════════════════════════════
  views.compare = function (root) {
    const sel = store.get("cmp", ["chat-default", "", ""]);
    root.innerHTML = `<div class="bsp-page-head"><div><h1>Side-by-side compare</h1><div class="lede">Same prompt across up to three routes — latency, tokens and output.</div></div></div>
      <div class="card">
        <div class="grid cols-3">${[0, 1, 2].map((i) => `<div class="field"><label>Route ${i + 1}</label><select class="select input" data-i="${i}"><option value="">—</option>${modelOptions("chat", sel[i])}</select></div>`).join("")}</div>
        <div class="field"><label>System prompt (optional)</label><input class="input" id="csys"></div>
        <div class="field"><label>Prompt</label><textarea class="textarea" id="cprompt">Summarise in two sentences why fiber customers churn less than copper DSL customers.</textarea></div>
        <div class="row"><label class="switch"><input type="checkbox" id="cjson"><span class="track"></span>JSON mode</label><span class="spacer" style="flex:1"></span><button class="btn primary" id="crun">Run</button></div>
      </div><div class="section cmp-grid" id="cres"></div>`;
    $("#crun").onclick = async (e) => {
      const routes = [...root.querySelectorAll("select[data-i]")].map((s) => s.value); store.set("cmp", routes);
      const msgs = []; if ($("#csys").value) msgs.push({ role: "system", content: $("#csys").value }); msgs.push({ role: "user", content: $("#cprompt").value });
      const r = await BSP.busy(e.currentTarget, () => api("/compare", { body: { routes: routes.filter(Boolean), messages: msgs, json_mode: $("#cjson").checked } }));
      $("#cres").innerHTML = r.results.map((x) => `<div class="card tight"><div class="card-head"><b>${esc(x.route)}</b><span class="chip ${x.ok ? "primary" : ""}">${x.ms} ms</span></div>
        ${x.ok ? `<div class="caption">served by ${esc(x.served_by)} · ${x.usage ? x.usage.prompt_tokens + "→" + x.usage.completion_tokens + " tokens" : ""}</div><div class="cmp-out">${esc(x.content || "")}</div>` : `<div class="alert error">${esc(x.error)}</div>`}</div>`).join("");
    };
  };

  // ══ Routing ════════════════════════════════════════════════════════════════
  views.routing = function (root) {
    root.innerHTML = `<div class="bsp-page-head"><div><h1>Routing</h1><div class="lede">Aliases → ordered provider routes, policies and budgets. Edits go to the override file (merged over the repo's <code>config/ai/providers.yaml</code>) and hot-reload.</div></div>
      <div class="row"><button class="btn ghost sm" id="reset">Reset override</button></div></div>
      <div class="grid cols-2">
        <div class="card"><div class="card-head"><h3>Override (YAML)</h3><div class="row"><button class="btn secondary sm" id="val">Validate</button><button class="btn primary sm" id="save">Save</button></div></div>
          <textarea class="textarea yaml" id="ov" spellcheck="false"></textarea><div id="msgs" class="caption"></div>
          <div class="caption" id="paths"></div></div>
        <div class="stack lg">
          <div class="card"><div class="card-head"><h3>Effective aliases</h3></div><div id="aliases"></div></div>
          <div class="card"><div class="card-head"><h3>Test a route</h3></div>
            <div class="grid cols-2"><div class="field"><label>Alias or provider/model</label><select class="select input" id="tm"></select></div>
            <div class="field"><label>Data classification</label><select class="select input" id="tdc"><option value="">(default)</option><option>internal</option><option>confidential</option><option>restricted</option></select></div></div>
            <div class="field"><label>Prompt</label><input class="input" id="tp" value="Reply with one short sentence: which model are you?"></div>
            <button class="btn primary sm" id="trun">Test route</button><div id="tout" class="section"></div></div>
          <div class="card"><div class="card-head"><h3>Base file (read-only)</h3></div><pre class="codeblock" id="base" style="max-height:320px;overflow:auto"></pre></div>
        </div></div>`;
    const example = "# Example: send chat-default to Vertex first\n# aliases:\n#   chat-default:\n#     routes: [vertex/gemini-2.5-flash, ollama/qwen2.5:7b-instruct]\n";
    async function load() {
      const c = await api("/config");
      $("#ov").value = c.override_yaml || example; $("#base").textContent = c.base_yaml;
      $("#paths").innerHTML = `base: <code>${esc(c.base_path)}</code> · override: <code>${esc(c.override_path)}</code>` + (c.error ? `<div class="alert error">${esc(c.error)}</div>` : "");
      const al = c.effective.aliases || {};
      $("#aliases").innerHTML = `<table class="table"><thead><tr><th>alias</th><th>routes (in order)</th><th>params</th></tr></thead><tbody>${Object.entries(al).map(([k, a]) =>
        `<tr><td><b>${esc(k)}</b><div class="caption">${esc(a.description || "")}</div></td><td>${(a.routes || []).map((r, i) => `<span class="chip ${i ? "outline" : "primary"}">${esc(r)}</span>`).join(" → ")}</td><td class="mono caption">${esc(JSON.stringify(a.params || {}))}</td></tr>`).join("")}</tbody></table>`;
      await loadModels(); $("#tm").innerHTML = modelOptions("all") + modelOptions("embed");
    }
    $("#val").onclick = async () => { const r = await api("/config/validate", { body: { override_yaml: $("#ov").value } }); $("#msgs").innerHTML = r.ok ? `<span class="chip primary">valid</span>` : r.errors.map((e) => `<div class="alert error">${esc(e)}</div>`).join(""); };
    $("#save").onclick = async () => {
      try { await api("/config", { method: "PUT", body: { override_yaml: $("#ov").value } }); toast("Routing saved — hot-reloaded", "success"); load(); }
      catch (e) { $("#msgs").innerHTML = ((e.data && e.data.errors) || [e.message]).map((x) => `<div class="alert error">${esc(x)}</div>`).join(""); }
    };
    $("#reset").onclick = async () => { if (await BSP.confirm({ title: "Reset override?", body: "Removes the UI override file; the repo config applies again.", okText: "Reset", danger: true })) { await api("/config/override", { method: "DELETE" }); toast("Override removed"); load(); } };
    $("#trun").onclick = async (e) => {
      const r = await BSP.busy(e.currentTarget, () => api("/test-route", { body: { model: $("#tm").value, prompt: $("#tp").value, data_classification: $("#tdc").value || null } }));
      $("#tout").innerHTML = `<div class="kv"><div>plan</div><div>${esc((r.plan && r.plan.routes || []).join(" → "))} ${r.plan && r.plan.skipped.length ? `<div class="caption">skipped: ${esc(r.plan.skipped.map((s) => s.route + " (" + s.reason + ")").join("; "))}</div>` : ""}</div>
        <div>result</div><div>${r.ok ? `<span class="chip primary">${esc(r.served_by)}</span> ${r.ms} ms` : `<span class="alert error">${esc(r.error)}</span>`}</div>
        <div>output</div><div>${esc(r.output || "")}</div><div>attempts</div><div class="mono caption">${esc(JSON.stringify(r.attempts || []))}</div></div>`;
    };
    load().catch((e) => toast(e.message, "error"));
  };

  // ══ Telemetry ══════════════════════════════════════════════════════════════
  function barChart(series, key, errKey) {
    if (!series.length) return `<div class="empty muted">No calls in this window.</div>`;
    const W = 600, H = 170, max = Math.max(1, ...series.map((s) => s[key])), bw = W / series.length;
    const bars = series.map((s, i) => { const h = (s[key] / max) * (H - 24); const he = errKey ? ((s[errKey] || 0) / max) * (H - 24) : 0;
      return `<rect class="bar" x="${i * bw + 1}" y="${H - 16 - h}" width="${Math.max(1, bw - 2)}" height="${h}"><title>${esc(s.t)}: ${s[key]}</title></rect>` +
        (he ? `<rect class="bar err" x="${i * bw + 1}" y="${H - 16 - he}" width="${Math.max(1, bw - 2)}" height="${he}"><title>errors ${s[errKey]}</title></rect>` : ""); }).join("");
    return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${bars}<text x="0" y="10">${max}</text><text x="0" y="${H - 2}">${esc(new Date(series[0].t).toLocaleString())}</text></svg>`;
  }
  function lineChart(series) {
    const pts = series.filter((s) => s.p50_ms != null);
    if (!pts.length) return `<div class="empty muted">No successful calls.</div>`;
    const W = 600, H = 170, max = Math.max(1, ...pts.map((s) => s.p95_ms || s.p50_ms)), dx = W / Math.max(1, pts.length - 1);
    const path = (k) => pts.map((s, i) => `${i ? "L" : "M"}${(i * dx).toFixed(1)},${(H - 16 - ((s[k] || 0) / max) * (H - 24)).toFixed(1)}`).join(" ");
    return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><path class="line" d="${path("p50_ms")}"/><path class="line p95" d="${path("p95_ms")}"/><text x="0" y="10">${Math.round(max)} ms</text></svg><div class="caption">solid p50 · dashed p95</div>`;
  }
  const aggTable = (rows, key) => `<table class="table"><thead><tr><th>${key}</th><th>calls</th><th>errors</th><th>p50 ms</th><th>p95 ms</th><th>tokens in→out</th><th>$</th></tr></thead><tbody>${rows.map((r) =>
    `<tr><td>${esc(r[key])}</td><td>${r.calls}</td><td>${r.errors}</td><td>${r.p50_ms ?? "—"}</td><td>${r.p95_ms ?? "—"}</td><td>${r.prompt_tokens}→${r.completion_tokens}</td><td>${r.cost_usd}</td></tr>`).join("")}</tbody></table>`;
  views.telemetry = function (root) {
    let hours = store.get("tlh", 24);
    root.innerHTML = `<div class="bsp-page-head"><div><h1>Telemetry</h1><div class="lede">Every gateway call (ai.llm_calls): latency, errors, tokens and spend by alias, provider and model.</div></div>
      <div class="segmented" id="hrs">${[1, 6, 24, 168].map((h) => `<button data-h="${h}" class="${h === hours ? "active" : ""}">${h < 24 ? h + "h" : h / 24 + "d"}</button>`).join("")}</div></div><div id="tbody"></div>`;
    $("#hrs").onclick = (e) => { const b = e.target.closest("button"); if (!b) return; hours = +b.dataset.h; store.set("tlh", hours); root.querySelectorAll("#hrs button").forEach((x) => x.classList.toggle("active", x === b)); load(); };
    async function load() {
      const d = await api("/telemetry?hours=" + hours); const t = d.total;
      $("#tbody").innerHTML = `<div class="grid cols-4">${[["Calls", t.calls], ["Errors", t.errors], ["p50 / p95", `${t.p50_ms ?? "—"} / ${t.p95_ms ?? "—"} ms`], ["Tokens · $", `${(t.prompt_tokens + t.completion_tokens).toLocaleString()} · $${t.cost_usd}`]].map(([k, v]) => `<div class="card tight"><div class="eyebrow">${k}</div><div class="kpi-s">${v}</div></div>`).join("")}</div>
        <div class="grid cols-2 section"><div class="card"><div class="card-head"><h4>Calls over time</h4><span class="caption">errors in orange</span></div>${barChart(d.series, "calls", "errors")}</div>
        <div class="card"><div class="card-head"><h4>Latency</h4></div>${lineChart(d.series)}</div></div>
        <div class="card section"><div class="card-head"><h4>By alias</h4></div>${aggTable(d.by_alias, "alias")}</div>
        <div class="card"><div class="card-head"><h4>By provider</h4></div>${aggTable(d.by_provider, "provider")}</div>
        <div class="card"><div class="card-head"><h4>Recent calls</h4><span class="caption">source: ${esc(d.source)}</span></div><div class="table-wrap"><table class="table"><thead><tr><th>time</th><th>alias</th><th>served by</th><th>status</th><th>ms</th><th>tokens</th><th>caller</th></tr></thead><tbody>
        ${d.recent.map((r) => `<tr><td>${esc(BSP.fmtAgo(r.ts))}</td><td>${esc(r.alias || "")}</td><td>${esc((r.provider || "") + "/" + (r.model || ""))}</td><td>${r.status === "ok" ? `<span class="chip primary">ok</span>` : `<span class="chip" title="${esc(r.error || "")}">${esc(r.status)}</span>`}</td><td>${r.latency_ms ?? ""}</td><td>${r.total_tokens ?? ""}</td><td class="caption">${esc(r.caller || "")}</td></tr>`).join("")}</tbody></table></div></div>`;
    }
    load().catch((e) => toast(e.message, "error"));
    const iv = setInterval(() => load().catch(() => {}), 15000);
    return { destroy: () => clearInterval(iv) };
  };

  // ══ Evals ══════════════════════════════════════════════════════════════════
  views.evals = function (root) {
    root.innerHTML = `<div class="bsp-page-head"><div><h1>Evals</h1><div class="lede">Small golden sets (YAML) run against routes; scores land in ai.eval_runs. Add your own in <code>$DATA_ROOT/harness/evals/*.yaml</code>.</div></div></div>
      <div class="grid cols-2"><div class="card"><div class="card-head"><h3>Run a suite</h3></div>
        <div class="field"><label>Suite</label><select class="select input" id="suite"></select><span class="hint" id="sdesc"></span></div>
        <div class="field"><label>Routes (one per line — aliases or provider/model)</label><textarea class="textarea mono" id="routes"></textarea></div>
        <div class="row"><button class="btn primary" id="erun">Run</button><span class="caption" id="eprog"></span></div><div id="eres" class="section"></div></div>
      <div class="card"><div class="card-head"><h3>Leaderboard</h3><span class="caption">latest run per suite × route</span></div><div id="lb"></div></div></div>
      <div class="card section"><div class="card-head"><h3>Recent runs</h3></div><div id="runs"></div></div>`;
    let suites = [];
    api("/evals/suites").then((r) => { suites = r.suites; $("#suite").innerHTML = suites.map((s) => `<option value="${esc(s.suite)}">${esc(s.suite)} (${s.cases} cases)</option>`).join(""); pick(); });
    function pick() { const s = suites.find((x) => x.suite === $("#suite").value); if (!s) return; $("#sdesc").textContent = s.description; $("#routes").value = (s.default_routes || []).join("\n"); }
    $("#suite").onchange = pick;
    async function loadRuns() {
      const r = await api("/evals/runs");
      $("#lb").innerHTML = `<table class="table"><thead><tr><th>suite</th><th>route</th><th>score</th><th>p50 ms</th><th>when</th></tr></thead><tbody>${r.leaderboard.map((x) => `<tr><td>${esc(x.suite)}</td><td>${esc(x.route)}</td><td><b>${(x.score * 100).toFixed(0)}%</b></td><td>${x.latency_p50_ms ?? ""}</td><td class="caption">${esc(BSP.fmtAgo(x.ts))}</td></tr>`).join("")}</tbody></table>`;
      $("#runs").innerHTML = `<table class="table"><thead><tr><th>when</th><th>suite</th><th>route</th><th>cases</th><th>score</th><th>errors</th></tr></thead><tbody>${r.runs.slice(0, 30).map((x) => `<tr><td>${esc(BSP.fmtTime(x.ts))}</td><td>${esc(x.suite)}</td><td>${esc(x.route)}</td><td>${x.n_cases}</td><td>${(x.score * 100).toFixed(0)}%</td><td>${x.errors}</td></tr>`).join("")}</tbody></table>`;
    }
    $("#erun").onclick = async (e) => {
      const routes = $("#routes").value.split("\n").map((s) => s.trim()).filter(Boolean);
      const { job_id } = await api("/evals/run", { body: { suite: $("#suite").value, routes } });
      e.currentTarget.classList.add("loading"); const btn = e.currentTarget;
      const poll = async () => {
        const j = await api("/evals/jobs/" + job_id); $("#eprog").textContent = `${j.done}/${j.total}`;
        if (j.status === "running") return setTimeout(poll, 1200);
        btn.classList.remove("loading");
        if (j.status === "error") { $("#eres").innerHTML = `<div class="alert error">${esc(j.error)}</div>`; return; }
        $("#eres").innerHTML = j.results.map((r) => `<div class="card tight flat"><div class="card-head"><b>${esc(r.route)}</b><span class="chip primary">${(r.score * 100).toFixed(0)}%</span></div>
          <table class="table"><tbody>${r.details.map((d) => `<tr><td>${esc(d.id)}</td><td>${(d.score * 100).toFixed(0)}%</td><td class="caption">${esc(d.error || d.output || d.sql || "").slice(0, 160)}</td><td>${d.ms} ms</td></tr>`).join("")}</tbody></table></div>`).join("");
        loadRuns();
      };
      poll();
    };
    loadRuns().catch(() => {});
  };

  // ══ Provider status ════════════════════════════════════════════════════════
  views.providers = function (root) {
    root.innerHTML = `<div class="bsp-page-head"><div><h1>Provider status</h1><div class="lede">Reachability and credentials. Vertex runs a tiny generate call; Anthropic only checks the key (no spend).</div></div><button class="btn secondary sm" id="refresh">Refresh</button></div><div id="plist" class="stack lg"></div>`;
    async function load() {
      $("#plist").innerHTML = `<div class="skeleton" style="height:120px"></div>`;
      const r = await api("/providers/status");
      $("#plist").innerHTML = (r.telemetry_db ? "" : `<div class="alert warning">Telemetry DB unreachable — calls are logged to JSONL until the warehouse is up.</div>`) + r.providers.map((p) => `
        <div class="card"><div class="card-head"><div class="row"><span class="status-dot ${p.ok ? "ok" : "bad"}"></span><h3>${esc(p.name)}</h3><span class="chip outline">${esc(p.type)}</span>${p.local ? `<span class="chip primary">local</span>` : ""}${p.enabled ? "" : `<span class="chip">disabled</span>`}</div></div>
          <div class="caption">${esc(p.detail || "")}</div>
          ${p.type === "vertex" || p.type === "anthropic_vertex" ? `<div class="caption" style="margin-top:4px">${BSP.iconHtml("cloud", 12)} Google identity: <b>${esc(r.gcp_identity || "GCP_SA_JSON / default credentials")}</b> (<a href="/studio/#identities" target="_blank" rel="noopener">change</a>)</div>` : ""}
          ${p.type === "ollama" ? `<div class="section"><div class="eyebrow">Pulled models</div><div class="row wrap" style="margin:6px 0">${(p.models || []).map((m) => `<span class="chip" title="${esc(m.parameter_size || "")}">${esc(m.name)} · ${BSP.fmtBytes(m.size)}</span>`).join("") || `<span class="caption">none</span>`}</div>
            <div class="caption">configured: ${(p.configured_models || []).map((m) => `<code>${esc(m)}</code>${(p.models || []).some((x) => x.name === m || x.name === m + ":latest") ? "" : " (not pulled)"}`).join(", ")}</div>
            <div class="input-group" style="max-width:420px;margin-top:8px"><input class="input" placeholder="qwen2.5:7b-instruct" data-pull="${esc(p.name)}"><button class="btn secondary" data-pullbtn="${esc(p.name)}">Pull model</button></div><pre class="codeblock" data-pullout="${esc(p.name)}" hidden></pre></div>` : ""}
        </div>`).join("");
      root.querySelectorAll("[data-pullbtn]").forEach((b) => (b.onclick = () => pull(b.dataset.pullbtn, b)));
    }
    async function pull(name, btn) {
      const model = root.querySelector(`[data-pull="${name}"]`).value.trim(); if (!model) return;
      const out = root.querySelector(`[data-pullout="${name}"]`); out.hidden = false; out.textContent = "";
      btn.classList.add("loading");
      try {
        await sse("/ollama/pull", { provider: name, model }, (ev) => {
          if (ev.type !== "progress") return;
          const pct = ev.total ? ` ${Math.round((100 * (ev.completed || 0)) / ev.total)}%` : "";
          out.textContent = `${ev.status || ev.error || ""}${pct}`;
        });
        toast(`Pulled ${model}`, "success"); load();
      } catch (e) { toast(String(e.message || e), "error"); } finally { btn.classList.remove("loading"); }
    }
    $("#refresh").onclick = load; load().catch((e) => toast(e.message, "error"));
  };

  loadModels().then(route);
})();
