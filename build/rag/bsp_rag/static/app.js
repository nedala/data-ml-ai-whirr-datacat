/* Knowledge & RAG UI — Collections · Ingest · Explore · Ask · Front office · Evals · Settings */
(function () {
  "use strict";
  const { h, toast, table, api: mkApi, busy, fmtAgo, esc } = BSP;
  const api = mkApi("/rag/api");
  const main = document.getElementById("main");
  const S = { collections: [], current: localStorage.getItem("rag.coll") || "", status: null };

  // ── helpers ─────────────────────────────────────────────────────────
  const head = (title, lede, ...actions) => h("div", { class: "bsp-page-head" },
    h("div", null, h("h1", null, title), lede ? h("div", { class: "lede" }, lede) : null), h("div", { class: "row" }, ...actions));
  const field = (label, el, hint) => h("div", { class: "field" }, h("label", null, label), el, hint ? h("div", { class: "hint" }, hint) : null);
  const sel = (opts, value, onchange) => {
    const s = h("select", { class: "select", onchange: onchange ? (e) => onchange(e.target.value) : null },
      ...opts.map((o) => { const [v, l] = Array.isArray(o) ? o : [o, o]; return h("option", { value: v, selected: v === value || null }, l); }));
    return s;
  };
  const num = (v, d = 3) => (v === null || v === undefined ? "—" : typeof v === "number" ? v.toFixed(d) : v);
  const err = (e) => toast(e.message || String(e), "error");
  const collSelect = (onchange) => sel(S.collections.map((c) => [c.name, `${c.name} · ${c.backend}`]), S.current, (v) => {
    S.current = v; localStorage.setItem("rag.coll", v); if (onchange) onchange(v);
  });

  async function loadCollections() {
    try {
      const r = await api("/collections");
      S.collections = r.collections;
      if (!S.collections.find((c) => c.name === S.current)) S.current = S.collections[0] ? S.collections[0].name : "";
    } catch (e) { S.collections = []; }
  }

  // citations: turn [n] into clickable chips that highlight the source
  function citedText(text, onCite) {
    const el = h("div", { class: "answer" });
    String(text).split(/(\[\d{1,2}\])/).forEach((part) => {
      const m = part.match(/^\[(\d{1,2})\]$/);
      if (m) el.append(h("span", { class: "cite", title: "Show source", onclick: () => onCite(+m[1]) }, m[1]));
      else el.append(document.createTextNode(part));
    });
    return el;
  }

  function sourcesBox(citations, chunks) {
    const box = h("div");
    const list = h("div");
    const chunkBox = h("div");
    const show = (n) => {
      const c = citations.find((x) => x.n === n) || {};
      const ch = (chunks || []).find((x) => x.chunk_id === c.chunk_id);
      chunkBox.replaceChildren(h("div", { class: "chunk hl" }, h("div", { class: "caption" }, `[${n}] ${c.title || ""} › ${c.section || ""} · ${c.doc_key || ""}`),
        (ch ? ch.text : c.snippet) || ""), c.document_id ? h("button", { class: "btn ghost sm", onclick: () => openDoc(S.current, c.document_id, c.chunk_id) }, "Open document") : null);
      chunkBox.scrollIntoView({ behavior: "smooth", block: "nearest" });
    };
    (citations || []).forEach((c) => list.append(h("div", { class: "src", style: { cursor: "pointer" }, onclick: () => show(c.n) },
      h("div", { class: "t" }, `[${c.n}] ${c.title}`), h("div", { class: "s" }, `${c.doc_key} › ${c.section || "overview"} · ${c.uri || ""}`))));
    box.append(h("div", { class: "eyebrow" }, `Sources (${(citations || []).length})`), list, chunkBox);
    box.show = show;
    return box;
  }

  async function openDoc(coll, did, highlight) {
    try {
      const r = await api(`/collections/${encodeURIComponent(coll)}/documents/${did}`);
      const d = r.document;
      BSP.drawer({ title: d.title, wide: true, subtitle: `${d.doc_key} · ${d.doc_type} · v${d.version || "?"}`, body: h("div", null,
        h("div", { class: "kv" }, ...["doc_key", "doc_type", "system", "owner", "version", "effective_date", "acl_group", "source", "uri", "content_hash", "etag", "updated_at"]
          .flatMap((k) => [h("div", { class: "k" }, k), h("div", { class: "mono small" }, String(d[k] ?? "—"))])),
        h("div", { class: "section" }, h("span", { class: "eyebrow" }, `${r.chunks.length} chunks`),
          ...r.chunks.map((c) => h("div", { class: `chunk ${c.id === highlight ? "hl" : ""}` },
            h("div", { class: "caption" }, `#${c.ordinal} · ${c.kind} · ${c.tokens} tok · ${c.section || "(intro)"}`), c.body)))) });
    } catch (e) { err(e); }
  }

  // ── Collections ─────────────────────────────────────────────────────
  async function viewCollections() {
    await loadCollections();
    const st = S.status || {};
    const tbl = table({ rowKey: "name", columns: [
      { key: "name", label: "Collection", sort: true, render: (r) => h("a", { href: "#", onclick: (e) => { e.preventDefault(); detail(r.name); } }, r.name) },
      { key: "backend", label: "Backend", sort: true, render: (r) => h("span", { class: "chip primary" }, r.backend) },
      { key: "embed_model", label: "Embedding model", render: (r) => h("span", { class: "mono small" }, `${r.embed_model} (${r.embed_dim}d)`) },
      { key: "chunker", label: "Chunking", render: (r) => `${r.chunker} · ${r.chunk_size} tok` },
      { key: "n_docs", label: "Docs", num: true, sort: true }, { key: "n_chunks", label: "Chunks", num: true, sort: true },
      { key: "last_ingest", label: "Last ingest", render: (r) => (r.last_ingest ? fmtAgo(r.last_ingest) : "—") },
    ], rows: S.collections, empty: "No collections yet — create one, or wait for the demo corpus to seed." });
    const out = h("div");
    main.replaceChildren(...[head("Collections", "Each collection has one vector backend and one embedding model (recorded; queries with another model are refused).",
      h("button", { class: "btn primary", onclick: createDialog }, BSP.icon("add"), "New collection")),
    st.seed && typeof st.seed === "string" && st.seed.startsWith("seeding") ? h("div", { class: "alert warning" }, st.seed) : null,
    h("div", { class: "card" }, tbl.el), out].filter(Boolean));
    async function detail(name) {
      try {
        const r = await api(`/collections/${encodeURIComponent(name)}`);
        const c = r.collection;
        S.current = name; localStorage.setItem("rag.coll", name);
        out.replaceChildren(h("div", { class: "card section" },
          h("div", { class: "row", style: { justifyContent: "space-between" } }, h("h2", null, name),
            h("div", { class: "row" },
              h("button", { class: "btn secondary sm", onclick: async (e) => busy(e.target, async () => { const j = await api(`/collections/${name}/reindex`, { method: "POST" }); watchJob(j.id, out); }) }, "Re-index"),
              h("button", { class: "btn danger sm", onclick: async () => { if (await BSP.confirm({ title: `Delete ${name}?`, body: "Removes vectors, chunks and documents.", danger: true, okText: "Delete" })) { await api(`/collections/${name}`, { method: "DELETE" }); toast("Deleted", "success"); viewCollections(); } } }, "Delete"))),
          h("div", { class: "grid cols-4" },
            stat("Documents", r.documents.length), stat("Vectors", c.vector_count ?? "?"), stat("Backend", c.backend), stat("Embedding", c.embed_model)),
          h("div", { class: "caption", style: { margin: "8px 0" } }, Object.entries(r.by_type).map(([k, v]) => `${k}: ${v}`).join(" · ")),
          table({ rowKey: "id", pageSize: 40, columns: [
            { key: "title", label: "Document", sort: true, render: (d) => h("a", { href: "#", onclick: (e) => { e.preventDefault(); openDoc(name, d.id); } }, d.title || d.uri) },
            { key: "doc_key", label: "ID", sort: true, render: (d) => h("span", { class: "mono small" }, d.doc_key || "") },
            { key: "doc_type", label: "Type", sort: true }, { key: "source", label: "Source", sort: true },
            { key: "n_chunks", label: "Chunks", num: true }, { key: "status", label: "Status", render: (d) => h("span", { class: `badge ${d.status === "indexed" ? "ok" : "warning"}` }, d.status) },
            { key: "updated_at", label: "Updated", render: (d) => fmtAgo(d.updated_at) },
          ], rows: r.documents }).el));
      } catch (e) { err(e); }
    }
    if (S.current) detail(S.current);
  }
  const stat = (label, value) => h("div", { class: "card tight flat" }, h("div", { class: "caption" }, label), h("div", { class: "kpi-s", style: { fontSize: "18px", wordBreak: "break-all" } }, String(value)));

  function createDialog() {
    const name = h("input", { class: "input", placeholder: "e.g. network-designs" });
    const backend = sel(["qdrant", "pgvector"], (S.status || {}).default_backend || "qdrant");
    const emb = sel([["auto", "auto (gateway embed-default if up, else offline)"], ["gateway", "gateway: embed-default"], ["hash", "offline: bsp-hash-512-v1"], ["fastembed", "offline: fastembed bge-small (if pre-downloaded)"]], "auto");
    const chunker = sel([["structure", "structure-aware (headings, steps, tables)"], ["fixed", "fixed windows (baseline)"]], "structure");
    const size = h("input", { class: "input", type: "number", value: 380, min: 80, max: 2000 });
    const desc = h("input", { class: "input" });
    const ok = h("button", { class: "btn primary" }, "Create");
    const m = BSP.modal({ title: "New collection", body: h("div", null, field("Name", name, "lowercase, digits, - and _"), field("Vector backend", backend),
      field("Embedding model", emb, "Recorded on the collection; mixed-model queries are refused."), field("Chunking", chunker), field("Max chunk tokens", size), field("Description", desc)),
      foot: [ok] });
    ok.onclick = () => busy(ok, async () => {
      try {
        await api("/collections", { body: { name: name.value, backend: backend.value, embedder: emb.value, chunker: chunker.value, chunk_size: +size.value, description: desc.value } });
        m.close(); S.current = name.value; toast("Collection created", "success"); viewCollections();
      } catch (e) { err(e); }
    });
  }

  // ── Ingest ──────────────────────────────────────────────────────────
  function watchJob(id, mount) {
    const bar = h("div", { class: "progressbar" }, h("div", { style: { width: "0%" } }));
    const label = h("div", { class: "caption" }, "starting…");
    const logEl = h("div", { class: "log" });
    const card = h("div", { class: "card section" }, h("div", { class: "row", style: { justifyContent: "space-between" } }, h("strong", null, "Job " + id), label), bar, h("div", { style: { height: "8px" } }), logEl);
    mount.prepend(card);
    const tick = async () => {
      try {
        const j = await api("/jobs/" + id);
        const pct = j.total ? Math.round((100 * j.done) / j.total) : (j.status === "running" ? 5 : 100);
        bar.firstChild.style.width = pct + "%";
        label.textContent = `${j.status} · ${j.done}/${j.total || "?"} ${j.current ? "· " + j.current : ""} · ${Object.entries(j.counts || {}).map(([k, v]) => `${k} ${v}`).join(", ")}`;
        logEl.textContent = (j.log || []).join("\n") + (j.error ? "\n" + j.error : "");
        logEl.scrollTop = logEl.scrollHeight;
        if (j.status === "running") setTimeout(tick, 700); else { toast(`Job ${j.status}`, j.status.startsWith("done") ? "success" : "error"); loadCollections(); }
      } catch (e) { label.textContent = e.message; }
    };
    tick();
  }

  async function viewIngest() {
    await loadCollections();
    const jobs = h("div");
    const metaFields = { doc_type: sel(["", "runbook", "design", "policy", "faq", "reference"], ""), system: h("input", { class: "input" }), owner: h("input", { class: "input" }),
      version: h("input", { class: "input" }), effective_date: h("input", { class: "input", type: "date" }), acl_group: h("input", { class: "input", placeholder: "frontoffice" }), tags: h("input", { class: "input", placeholder: "comma,separated" }) };
    const upload = async (files) => {
      if (!S.current) return toast("Create a collection first", "error");
      const fd = new FormData();
      files.forEach((f) => fd.append("files", f, f.name));
      Object.entries(metaFields).forEach(([k, el]) => { if (el.value) fd.append(k, el.value); });
      try { const j = await api(`/collections/${S.current}/ingest/upload`, { body: fd }); watchJob(j.id, jobs); } catch (e) { err(e); }
    };
    const spFolder = h("input", { class: "input", value: (S.status || {}).sharepoint_folder || "FrontOffice/Runbooks" });
    const spList = h("div", { class: "caption" });
    const lakePath = h("input", { class: "input", placeholder: "reference/runbooks" });
    const lakeInfo = h("div", { class: "caption" });
    const url = h("input", { class: "input", placeholder: "https://intranet.example.net/kb/article.html" });
    const run = (path, body) => async (e) => busy(e.target, async () => { if (!S.current) return toast("Pick a collection", "error"); try { const j = await api(`/collections/${S.current}/ingest/${path}`, { body: body ? body() : {} }); watchJob(j.id, jobs); } catch (x) { err(x); } });
    main.replaceChildren(head("Ingest", "Upload files, pull a SharePoint folder (incremental by lastModified/eTag, deletes propagate), a lake folder, a URL, or the built-in demo corpus.",
      field("Collection", collSelect())),
    h("div", { class: "grid cols-2" },
      h("div", { class: "card" }, h("h3", null, "Upload"), h("div", { class: "caption" }, "md · pdf · docx · pptx · xlsx · html · txt · csv"),
        BSP.dropzone({ onFiles: upload, label: "Drop documents here or click to browse" }),
        h("details", { style: { marginTop: "10px" } }, h("summary", null, "Metadata applied to uploaded files"),
          h("div", { class: "grid cols-2", style: { marginTop: "8px" } }, ...Object.entries(metaFields).map(([k, el]) => field(k, el))))),
      h("div", { class: "stack lg" },
        h("div", { class: "card" }, h("h3", null, "SharePoint folder"), h("div", { class: "caption" }, "via bsp_connectors.m365 (mock until Microsoft 365 credentials are set)"),
          h("div", { class: "toolbar" }, field("Folder", spFolder), h("button", { class: "btn secondary", onclick: async () => { const r = await api("/sharepoint?folder=" + encodeURIComponent(spFolder.value)); spList.textContent = r.error ? r.error : `${r.mode}: ${r.files.length} files — ${r.files.slice(0, 6).map((f) => f.name).join(", ")}${r.files.length > 6 ? "…" : ""}`; } }, "Preview"),
            h("button", { class: "btn primary", onclick: run("sharepoint", () => ({ folder: spFolder.value })) }, "Sync")), spList),
        h("div", { class: "card" }, h("h3", null, "Lake folder"), h("div", { class: "toolbar" }, field("Path under /data/lake", lakePath),
          h("button", { class: "btn secondary", onclick: async () => { try { const r = await api("/lake?path=" + encodeURIComponent(lakePath.value)); lakeInfo.textContent = r.exists ? `${r.files} files; folders: ${r.dirs.join(", ") || "—"}` : "not found"; } catch (e) { err(e); } } }, "Browse"),
          h("button", { class: "btn primary", onclick: run("lake", () => ({ path: lakePath.value })) }, "Ingest")), lakeInfo),
        h("div", { class: "card" }, h("h3", null, "URL"), h("div", { class: "toolbar" }, field("Page or PDF URL (egress allowlist applies)", url), h("button", { class: "btn primary", onclick: run("url", () => ({ url: url.value })) }, "Fetch"))),
        h("div", { class: "card" }, h("h3", null, "Demo corpus"), h("div", { class: "caption" }, "~30 synthetic DataPlatform-style runbooks, design docs, policies, FAQs (md + pdf/docx/pptx/xlsx)."),
          h("button", { class: "btn secondary", style: { marginTop: "8px" }, onclick: run("corpus") }, "Ingest demo corpus")))),
    jobs);
    try { const j = await api("/jobs"); j.jobs.filter((x) => x.status === "running").forEach((x) => watchJob(x.id, jobs)); } catch (e) { /* ignore */ }
  }

  // ── Explore ─────────────────────────────────────────────────────────
  async function viewExplore() {
    await loadCollections();
    const q = h("input", { class: "input", placeholder: "e.g. ONT red LOS light no internet", style: { minWidth: "360px" } });
    const mode = sel(["hybrid", "dense", "lexical"], "hybrid");
    const k = h("input", { class: "input", type: "number", value: 8, min: 1, max: 30, style: { width: "80px" } });
    const dt = sel([["", "any type"], "runbook", "design", "policy", "faq", "reference"], "");
    const rerank = sel([["auto", "rerank: auto"], ["llm", "rerank: LLM"], ["lexical", "rerank: lexical"], ["none", "no rerank"]], "auto");
    const mmr = h("input", { type: "checkbox", class: "checkbox", checked: true });
    const rw = h("input", { type: "checkbox", class: "checkbox", checked: true });
    const out = h("div");
    const go = async (btn) => busy(btn, async () => {
      try {
        const r = await api("/search", { body: { query: q.value, collection: S.current, k: +k.value, mode: mode.value, filters: dt.value ? { doc_type: dt.value } : null, rerank: rerank.value, mmr: mmr.checked, rewrite: rw.checked } });
        out.replaceChildren(h("div", { class: "caption", style: { margin: "8px 0" } },
          `rewritten: “${r.rewritten}” · ${r.backend} · ${r.embed_model} · rerank ${r.rerank} · mmr ${r.mmr} · ${Object.entries(r.timings).map(([a, b]) => `${a} ${b}`).join(" · ")}`),
        ...r.results.map((c) => h("div", { class: "card tight" },
          h("div", { class: "row", style: { justifyContent: "space-between" } }, h("strong", null, `#${c.rank} ${c.title}`), h("span", { class: "chip" }, c.doc_type)),
          h("div", { class: "caption" }, `${c.doc_key} › ${c.section || "(intro)"} · ${c.uri}`),
          h("div", { class: "scores" }, chip(`fused ${num(c.score, 4)}`), c.dense_rank ? chip(`dense #${c.dense_rank} · ${num(c.dense_score)}`) : chip("dense —"),
            c.lexical_rank ? chip(`bm25 #${c.lexical_rank} · ${num(c.lexical_score)}`) : chip("bm25 —"), c.rerank_score !== undefined ? chip(`rerank ${num(c.rerank_score)}`) : null,
            c.mmr_score !== undefined ? chip(`mmr ${num(c.mmr_score)}`) : null),
          h("details", null, h("summary", { class: "small" }, "chunk text"), h("div", { class: "chunk" }, c.text)),
          h("button", { class: "btn ghost sm", onclick: () => openDoc(S.current, c.document_id, c.chunk_id) }, "Open document"))));
      } catch (e) { err(e); }
    });
    const btn = h("button", { class: "btn primary", onclick: (e) => go(e.currentTarget) }, BSP.icon("search"), "Search");
    q.onkeydown = (e) => { if (e.key === "Enter") go(btn); };
    main.replaceChildren(head("Explore", "Search playground: hybrid = BM25 (sparse / full-text) + dense, fused with reciprocal-rank fusion, then rerank and MMR diversity."),
      h("div", { class: "card" }, h("div", { class: "toolbar" }, field("Collection", collSelect()), field("Query", q), field("Mode", mode), field("k", k), field("Doc type", dt), field("Rerank", rerank),
        h("label", { class: "row small" }, mmr, "MMR"), h("label", { class: "row small" }, rw, "query rewrite"), btn)), out);
  }
  const chip = (t) => h("span", { class: "chip outline" }, t);

  // ── Ask ─────────────────────────────────────────────────────────────
  async function viewAsk() {
    await loadCollections();
    const q = h("textarea", { class: "textarea", placeholder: "Ask about a policy, procedure or design… e.g. Can I reverse a second late fee this year?", rows: 3 });
    const dt = sel([["", "any type"], "runbook", "design", "policy", "faq"], "");
    const llm = h("input", { type: "checkbox", class: "checkbox", checked: true });
    const thread = h("div");
    const ask = (btn) => busy(btn, async () => {
      if (!q.value.trim()) return;
      try {
        const r = await api("/ask", { body: { question: q.value, collection: S.current, filters: dt.value ? { doc_type: dt.value } : null, use_llm: llm.checked } });
        const src = sourcesBox(r.citations, r.chunks);
        thread.prepend(h("div", { class: "card section" }, h("div", { class: "caption" }, "Q"), h("strong", null, r.question),
          h("div", { style: { margin: "10px 0" } }, citedText(r.answer, (n) => src.show(n))),
          h("div", { class: "scores" }, chip(`mode: ${r.mode}`), chip(`coverage ${num(r.coverage, 2)}`), chip(`${r.latency_ms} ms`), ...r.model_route.map((x) => chip(`${x.stage}: ${x.model || x.alias}`))), src));
      } catch (e) { err(e); }
    });
    const btn = h("button", { class: "btn primary", onclick: (e) => ask(e.currentTarget) }, BSP.icon("send"), "Ask");
    q.onkeydown = (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) ask(btn); };
    main.replaceChildren(head("Ask", "Grounded answers with numbered citations — and an honest “I don't know” when the knowledge base doesn't cover it."),
      h("div", { class: "card" }, h("div", { class: "toolbar" }, field("Collection", collSelect()), field("Restrict to", dt), h("label", { class: "row small" }, llm, "use LLM (falls back to extractive offline)")), q,
        h("div", { class: "row", style: { marginTop: "8px" } }, btn, h("span", { class: "caption" }, "Ctrl+Enter"))), thread);
  }

  // ── Front office ────────────────────────────────────────────────────
  let SAMPLES = null;
  async function samples() { if (!SAMPLES) SAMPLES = await api("/frontoffice/samples"); return SAMPLES; }

  async function viewAssist() {
    await loadCollections();
    const sm = await samples();
    const text = h("textarea", { class: "textarea", rows: 12, placeholder: "Paste a customer email, chat or call transcript…" });
    const channel = sel(["email", "chat", "call", "ticket"], "email");
    const out = h("div");
    const go = (btn) => busy(btn, async () => {
      try {
        const r = await api("/frontoffice/agent-assist", { body: { text: text.value, channel: channel.value, collection: S.current } });
        const src = sourcesBox(r.citations, r.chunks);
        const e = r.entities;
        out.replaceChildren(h("div", { class: "split section" },
          h("div", { class: "stack lg" },
            h("div", { class: "card" }, h("div", { class: "eyebrow" }, "Classification"),
              h("div", { class: "row wrap" }, h("span", { class: "chip primary" }, r.intent.intent), ...(r.intent.secondary || []).map((s) => chip(s)), chip(r.intent.method), chip(`conf ${num(r.intent.confidence, 2)}`)),
              h("div", { class: "kv", style: { marginTop: "10px" } }, h("div", { class: "k" }, "Customer"), h("div", null, e.customer_name || "—"), h("div", { class: "k" }, "BANs"), h("div", { class: "mono" }, (e.bans || []).join(", ") || "—"),
                h("div", { class: "k" }, "Invoices"), h("div", { class: "mono" }, e.invoices.join(", ") || "—"), h("div", { class: "k" }, "Wire centers"), h("div", null, e.wire_centers.join(", ") || "—"), h("div", { class: "k" }, "Amounts"), h("div", null, e.amounts.join(", ") || "—"))),
            h("div", { class: "card" }, h("div", { class: "eyebrow" }, "Next best action"), h("div", { style: { fontWeight: 600, margin: "6px 0 10px" } }, r.next_best_action),
              ...r.flags.map((f) => h("div", { class: "flag" }, h("strong", null, f.flag + ": "), f.action)),
              h("div", { class: "eyebrow", style: { marginTop: "10px" } }, "Checklist (from the runbook)"),
              h("ul", { class: "steps" }, ...r.checklist.map((s) => h("li", null, h("input", { type: "checkbox", class: "checkbox" }), h("div", null, s.step, " ", h("span", { class: "cite", onclick: () => src.show(s.cite) }, s.cite))))))),
          h("div", { class: "stack lg" },
            h("div", { class: "card" }, h("div", { class: "row", style: { justifyContent: "space-between" } }, h("div", { class: "eyebrow" }, `Draft reply · ${r.draft_mode}`),
              h("button", { class: "btn ghost sm", onclick: () => BSP.copy(r.draft_reply_clean, "Draft") }, BSP.icon("copy"), "Copy (without citations)")),
              citedText(r.draft_reply, (n) => src.show(n)), h("div", { class: "caption", style: { marginTop: "8px" } }, "Review before sending — the agent stays accountable.")),
            h("div", { class: "card" }, src))));
      } catch (e) { err(e); }
    });
    main.replaceChildren(head("Agent assist", "Customer email / chat / call → intent → runbook retrieval → checklist + drafted reply, all with citations."),
      h("div", { class: "grid", style: { gridTemplateColumns: "260px minmax(0,1fr)" } },
        h("div", { class: "card sample-list" }, h("div", { class: "eyebrow" }, "Samples"), ...sm.agent_assist.map((s) => h("button", { class: "btn ghost sm", onclick: () => { text.value = s.text; channel.value = s.channel; } }, s.title))),
        h("div", { class: "card" }, h("div", { class: "toolbar" }, field("Collection", collSelect()), field("Channel", channel)), text,
          h("button", { class: "btn primary", style: { marginTop: "8px" }, onclick: (e) => go(e.currentTarget) }, "Assist"))), out);
  }

  async function viewCopilot() {
    await loadCollections();
    const sm = await samples();
    const text = h("textarea", { class: "textarea", rows: 3, placeholder: "Outage in wire center RLGHNCXA, ~6,000 customers down — what do I do?" });
    const out = h("div");
    const go = (btn) => busy(btn, async () => {
      try {
        const r = await api("/frontoffice/runbook", { body: { text: text.value, collection: S.current } });
        const src = sourcesBox(r.citations, r.chunks);
        const stepList = (arr) => h("ol", { class: "steps" }, ...arr.map((s) => h("li", null, h("input", { type: "checkbox", class: "checkbox" }), h("div", null, s.step.replace(/^\d+\.\s*/, ""), " ", h("span", { class: "cite", onclick: () => src.show(s.cite) }, s.cite)))));
        const lc = r.local_contacts || {};
        out.replaceChildren(h("div", { class: "grid cols-4 section" }, stat("Wire center", r.wire_center || "—"), stat("Region", r.region || "—"), stat("Severity", r.severity), stat("Customers", r.customer_count ?? "—")),
          r.narrative ? h("div", { class: "card section" }, h("div", { class: "eyebrow" }, "Response plan (LLM)"), citedText(r.narrative, (n) => src.show(n))) : null,
          h("div", { class: "split section" },
            h("div", { class: "card" }, h("div", { class: "eyebrow" }, "NOC steps"), stepList(r.noc_steps), h("div", { class: "eyebrow", style: { marginTop: "12px" } }, "Customer communications"), stepList(r.communication_steps)),
            h("div", { class: "stack lg" },
              h("div", { class: "card" }, h("div", { class: "eyebrow" }, "Escalation contacts"),
                ...r.escalation.map((c) => h("div", { class: "src" }, h("div", { class: "t" }, c.situation, " ", h("span", { class: "cite", onclick: () => src.show(c.cite) }, c.cite)),
                  h("div", { class: "small" }, "L1: " + c.level_1), h("div", { class: "small" }, "L2: " + c.level_2), h("div", { class: "small" }, "L3: " + c.level_3)))),
              Object.keys(lc).length ? h("div", { class: "card" }, h("div", { class: "eyebrow" }, `Local contacts · ${r.wire_center}`, " ", h("span", { class: "cite", onclick: () => src.show(lc.cite) }, lc.cite)),
                h("div", { class: "kv" }, ...Object.entries(lc).filter(([k]) => k !== "cite").flatMap(([k, v]) => [h("div", { class: "k" }, k), h("div", null, v)]))) : null,
              h("div", { class: "card" }, src))));
      } catch (e) { err(e); }
    });
    main.replaceChildren(head("Runbook copilot", "Describe an incident: get the NOC and communications steps, severity, regional escalation contacts and local wire-center contacts — cited."),
      h("div", { class: "card" }, h("div", { class: "toolbar" }, field("Collection", collSelect())), text,
        h("div", { class: "row wrap", style: { marginTop: "8px" } }, h("button", { class: "btn primary", onclick: (e) => go(e.currentTarget) }, "Get the plan"),
          ...sm.runbook.map((s) => h("button", { class: "btn ghost sm", onclick: () => { text.value = s; } }, s.slice(0, 48) + "…")))), out);
  }

  async function viewDesign() {
    await loadCollections();
    const sm = await samples();
    const q = h("input", { class: "input", style: { minWidth: "420px" }, placeholder: "How do GPON and XGS-PON coexist?" });
    const out = h("div");
    const go = (btn) => busy(btn, async () => {
      try {
        const r = await api("/frontoffice/design", { body: { text: q.value, collection: S.current } });
        const src = sourcesBox(r.citations, r.chunks);
        out.prepend(h("div", { class: "card section" }, h("strong", null, r.question), h("div", { style: { margin: "10px 0" } }, citedText(r.answer, (n) => src.show(n))),
          h("div", { class: "scores" }, chip(`mode: ${r.mode}`), chip(`${r.latency_ms} ms`)), src));
      } catch (e) { err(e); }
    });
    q.onkeydown = (e) => { if (e.key === "Enter") go(e.target); };
    main.replaceChildren(head("Design Q&A", "Questions answered only from network, billing, contact-center and data-platform design documents."),
      h("div", { class: "card" }, h("div", { class: "toolbar" }, field("Collection", collSelect()), field("Question", q), h("button", { class: "btn primary", onclick: (e) => go(e.currentTarget) }, "Ask")),
        h("div", { class: "row wrap" }, ...sm.design.map((s) => h("button", { class: "btn ghost sm", onclick: () => { q.value = s; } }, s)))), out);
  }

  // ── Evals ───────────────────────────────────────────────────────────
  async function viewEvals() {
    await loadCollections();
    let info;
    try { info = await api("/evals"); } catch (e) { return err(e); }
    const boxes = info.presets.map((p) => { const cb = h("input", { type: "checkbox", class: "checkbox", value: p, checked: ["hybrid", "dense", "lexical", "hybrid+rerank+mmr"].includes(p) || null }); return [p, cb]; });
    const answers = h("input", { type: "checkbox", class: "checkbox", checked: true });
    const jobs = h("div");
    const lb = h("div");
    const metrics = ["hit@1", "hit@3", "hit@5", "mrr", "faithfulness", "citation_correctness", "fact_recall", "abstain_accuracy"];
    async function loadLb() {
      const r = await api("/evals/leaderboard");
      const best = {};
      metrics.forEach((m) => { best[m] = Math.max(...r.rows.map((x) => (x.metrics || {})[m] ?? -1)); });
      lb.replaceChildren(table({ rowKey: "id", pageSize: 30, columns: [
        { key: "collection", label: "Collection", render: (x) => x.config.collection },
        { key: "backend", label: "Backend", render: (x) => x.config.backend },
        { key: "embed", label: "Embedding", render: (x) => h("span", { class: "mono small" }, x.config.embed_model) },
        { key: "chunker", label: "Chunking", render: (x) => `${x.config.chunker}/${x.config.chunk_size}` },
        { key: "preset", label: "Retrieval", render: (x) => x.config.preset },
        ...metrics.map((m) => ({ key: m, label: m, num: true, sort: (x) => (x.metrics || {})[m] ?? -1, render: (x) => h("span", { class: `metric ${(x.metrics || {})[m] === best[m] ? "best" : ""}` }, num((x.metrics || {})[m])) })),
        { key: "judge", label: "Judge" }, { key: "ts", label: "When", render: (x) => fmtAgo(x.ts) },
      ], rows: r.rows, onRowClick: (x) => showRun(x.id), empty: "No eval runs yet." }).el);
    }
    async function showRun(id) {
      const r = await api("/evals/" + id);
      BSP.drawer({ title: r.label, wide: true, body: table({ rowKey: "id", pageSize: 60, columns: [
        { key: "id", label: "Q" }, { key: "question", label: "Question" }, { key: "expected", label: "Expected", render: (x) => (x.expected || []).join(", ") || "(abstain)" },
        { key: "rank", label: "Rank", num: true }, { key: "faithfulness", label: "Faith.", num: true, render: (x) => num(x.faithfulness, 2) },
        { key: "citation_correct", label: "Cite ok", num: true, render: (x) => num(x.citation_correct, 2) }, { key: "answer", label: "Answer", render: (x) => h("div", { class: "small" }, (x.answer || "").slice(0, 220)) },
      ], rows: r.per_question }).el });
    }
    const runBtn = h("button", { class: "btn primary", onclick: (e) => busy(e.currentTarget, async () => {
      try {
        const presets = boxes.filter(([, cb]) => cb.checked).map(([p]) => p);
        const j = await api("/evals/run", { body: { collection: S.current, presets, with_answers: answers.checked } });
        watchJob(j.id, jobs);
        const poll = async () => { const s = await api("/jobs/" + j.id); if (s.status === "running") setTimeout(poll, 1500); else loadLb(); };
        poll();
      } catch (x) { err(x); }
    }) }, "Run evals");
    main.replaceChildren(head("Evals", `Golden set: ${info.golden} questions (retrieval hit@k / MRR; answer faithfulness & citation correctness — LLM judge when the gateway is up, lexical otherwise).`),
      h("div", { class: "card" }, h("div", { class: "toolbar" }, field("Collection", collSelect()), h("div", { class: "row wrap" }, ...boxes.map(([p, cb]) => h("label", { class: "row small" }, cb, p))),
        h("label", { class: "row small" }, answers, "score answers"), runBtn)), jobs,
      h("div", { class: "card section" }, h("div", { class: "eyebrow" }, "Leaderboard (latest run per configuration; click a row for per-question detail)"), lb));
    loadLb();
  }

  async function viewQueries() {
    let r;
    try { r = await api("/queries?limit=200"); } catch (e) { return err(e); }
    main.replaceChildren(head("Query log", "Every search / answer / front-office call is logged to rag.queries (latency, top-k ids, scores, model route)."),
      h("div", { class: "card" }, table({ rowKey: "id", pageSize: 50, columns: [
        { key: "ts", label: "When", render: (x) => fmtAgo(x.ts) }, { key: "username", label: "User" }, { key: "mode", label: "Mode" }, { key: "collection", label: "Collection" },
        { key: "query", label: "Query", render: (x) => h("div", { class: "small", title: x.rewritten || "" }, (x.query || "").slice(0, 140)) },
        { key: "latency_ms", label: "ms", num: true, sort: true }, { key: "n_citations", label: "Cites", num: true },
        { key: "route", label: "Route", render: (x) => h("span", { class: "small mono" }, ((x.model_route || {}).routes || []).map((y) => `${y.stage}:${y.model || y.alias}`).join(" ") || `${(x.model_route || {}).backend || ""} offline`) },
        { key: "status", label: "Status" },
      ], rows: r.queries }).el));
  }

  async function viewSettings() {
    let st;
    try { st = await api("/status"); } catch (e) { return err(e); }
    const kv = (o) => h("div", { class: "kv" }, ...Object.entries(o).flatMap(([k, v]) => [h("div", { class: "k" }, k), h("div", { class: "mono small" }, typeof v === "object" ? JSON.stringify(v) : String(v))]));
    main.replaceChildren(head("Settings", "Read-only view of the service configuration (env-driven: RAG_VECTOR_BACKEND, RAG_EMBEDDER, RAG_USE_LLM …)."),
      h("div", { class: "grid cols-2" },
        h("div", { class: "card" }, h("h3", null, "Service"), kv({ version: st.version, ready: st.ready, default_backend: st.default_backend, default_collection: st.default_collection, embedder: st.embedder_default, sharepoint_folder: st.sharepoint_folder, lake_dir: st.lake_dir, openwebui_port: st.openwebui_port, seed: st.seed })),
        h("div", { class: "card" }, h("h3", null, "LLM gateway"), kv(st.llm_gateway), h("h3", { style: { marginTop: "14px" } }, "Embedder (auto)"), kv(st.embedder_auto || {})),
        h("div", { class: "card" }, h("h3", null, "Qdrant"), kv(st.backends.qdrant || {})),
        h("div", { class: "card" }, h("h3", null, "pgvector"), kv({ ...(st.backends.pgvector || {}), ...(st.meta_store || {}) }))));
  }

  // ── router ──────────────────────────────────────────────────────────
  const VIEWS = { collections: viewCollections, ingest: viewIngest, explore: viewExplore, ask: viewAsk, assist: viewAssist, copilot: viewCopilot, design: viewDesign, evals: viewEvals, queries: viewQueries, settings: viewSettings };
  function route() {
    const v = (location.hash || "#collections").slice(1);
    document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === v));
    (VIEWS[v] || viewCollections)().catch(err);
  }
  window.addEventListener("hashchange", route);
  (async () => {
    try {
      S.status = await api("/status");
      const c = document.getElementById("llm-chip");
      c.textContent = S.status.llm_gateway.available ? "gateway: online" : "gateway: offline (deterministic fallbacks)";
      c.className = "chip " + (S.status.llm_gateway.available ? "primary" : "outline");
    } catch (e) { /* starting */ }
    route();
  })();
})();
