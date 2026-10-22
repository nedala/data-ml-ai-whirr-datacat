/* Dispute Desk UI — built on window.BSP (ui-kit). */
(function () {
  "use strict";
  const { h, icon, api: mkApi, toast, table, fmtAgo, fmtTime, busy, confirm, prompt } = window.BSP;
  const api = mkApi("/disputes");
  const main = document.getElementById("main");
  let CFG = null;
  let liveTimer = null;
  let lastRunning = false;
  const LS = (k, d) => { try { const v = localStorage.getItem("dd." + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } };
  const setLS = (k, v) => { try { localStorage.setItem("dd." + k, JSON.stringify(v)); } catch (e) { /* ignore */ } };

  const STATUS = {
    needs_review: ["warning", "Needs review"], approved: ["info", "Approved"], submitting: ["running", "Submitting"],
    submitted: ["success", "In SAP"], failed: ["error", "Failed"], rejected: ["neutral", "Rejected"], not_dispute: ["neutral", "Not a dispute"],
  };
  const badge = (s) => { const [k, t] = STATUS[s] || ["neutral", s]; return h("span", { class: `badge ${k}` }, h("span", { class: "dot" }), t); };
  const money = (v) => (v === null || v === undefined || v === "" ? "—" : "$" + Number(v).toFixed(2));
  const pct = (v) => (v === null || v === undefined ? "—" : Math.round(v * 100) + "%");
  const meter = (v) => h("span", { class: "row", style: { gap: "6px" } },
    h("span", { class: "meterbar" }, h("span", { style: { width: Math.round((v || 0) * 100) + "%", background: v >= 0.85 ? "" : v >= 0.6 ? "var(--bsp-accent-signal)" : "var(--bsp-tints-red-100)" } })),
    h("span", { class: "caption" }, pct(v)));
  const shot = (p) => "/disputes/api/screenshot?path=" + encodeURIComponent(p);
  function zoom(src) { const z = h("div", { class: "zoom", onclick: () => z.remove() }, h("img", { src })); document.body.append(z); }
  function sla(due, status) {
    if (!due || !["needs_review", "approved", "failed"].includes(status)) return h("span", { class: "caption" }, "—");
    const ms = new Date(due) - Date.now(), hrs = ms / 36e5;
    const txt = ms < 0 ? `${Math.ceil(-hrs)}h overdue` : hrs < 1 ? `${Math.round(ms / 6e4)}m left` : `${Math.round(hrs)}h left`;
    return h("span", { class: `sla ${ms < 0 ? "late" : hrs < 8 ? "soon" : ""}` }, txt);
  }
  function head(title, lede, ...actions) {
    return h("div", { class: "bsp-page-head" }, h("div", null, h("h1", null, title), lede ? h("div", { class: "lede" }, lede) : null),
      h("div", { class: "row wrap" }, ...actions));
  }
  const err = (e) => toast(e && e.message ? e.message : String(e), "error");

  // ── chrome ─────────────────────────────────────────────────────────
  async function loadConfig() {
    CFG = await api("/api/config");
    const sap = CFG.sap || {};
    document.getElementById("mode-chips").replaceChildren(
      h("span", { class: "chip", title: "Mailbox source" }, "Mail: " + (CFG.m365_mode || "?")),
      h("span", { class: "chip", title: "SAP BRIM adapter (SAP_BRIM_MODE)" }, "SAP: " + (sap.mode || "?")),
      h("span", { class: `chip`, title: "LLM gateway (extract-default / vlm-default)" }, CFG.llm_available ? "LLM: on" : "LLM: rules fallback"));
    document.getElementById("watch-live").href = CFG.browser_path;
  }
  async function counts() {
    try {
      const m = await api("/api/metrics");
      document.getElementById("n-review").textContent = m.by_status.needs_review || "";
      document.getElementById("n-approved").textContent = m.by_status.approved || "";
    } catch (e) { /* ignore */ }
  }

  // ── review queue ───────────────────────────────────────────────────
  async function viewQueue() {
    const filt = LS("qfilter", "needs_review");
    const seg = h("div", { class: "segmented" });
    const FILTERS = [["needs_review", "Needs review"], ["approved", "Approved"], ["failed", "Failed"], ["submitted", "In SAP"],
      ["needs_review,approved,failed,submitted,submitting,rejected", "All disputes"], ["not_dispute", "Not a dispute"], ["rejected", "Rejected"]];
    const search = h("input", { class: "input", placeholder: "Search subject, account, invoice…", style: { width: "260px" } });
    const bulkBtn = h("button", { class: "btn primary", disabled: true }, "Approve selected");
    const t = table({
      selectable: true, rowKey: "id", pageSize: 50, onRowClick: (r) => (location.hash = "#case/" + r.id),
      empty: h("div", { class: "empty" }, "No cases. Use “Check mailbox now” on the Inbox page."),
      columns: [
        { key: "case_ref", label: "Case", sort: true },
        { key: "received_at", label: "Received", sort: true, render: (r) => h("span", { title: fmtTime(r.received_at) }, fmtAgo(r.received_at)) },
        { key: "customer", label: "Customer", render: (r) => h("div", null, h("div", null, r.fields.customer_name || r.sender_name || "—"), h("div", { class: "caption" }, r.fields.ban || r.fields.account_id || "no BAN")) },
        { key: "subject", label: "Subject", render: (r) => h("div", null, r.subject, r.n_attachments ? h("span", { class: "caption" }, ` · ${r.n_attachments} att.`) : null) },
        { key: "category", label: "Category", sort: (r) => r.fields.category_label, render: (r) => r.fields.category_label || "—" },
        { key: "amount", label: "Amount", num: true, sort: (r) => r.fields.disputed_amount, render: (r) => money(r.fields.disputed_amount) },
        { key: "overall_confidence", label: "Confidence", sort: true, render: (r) => meter(r.overall_confidence) },
        { key: "sla_due_at", label: "SLA", sort: true, render: (r) => sla(r.sla_due_at, r.status) },
        { key: "status", label: "Status", sort: true, render: (r) => h("span", { class: "row" }, badge(r.status), r.auto_approved ? h("span", { class: "chip", title: "auto-approved" }, "auto") : null) },
      ],
    });
    t.el.addEventListener("selection", () => { bulkBtn.disabled = !t.selected().some((r) => r.status === "needs_review"); });
    async function load() {
      const st = LS("qfilter", "needs_review");
      seg.replaceChildren(...FILTERS.map(([k, l]) => h("button", { class: k === st ? "active" : "", onclick: () => { setLS("qfilter", k); load(); } }, l)));
      try { t.update(await api(`/api/cases?status=${encodeURIComponent(st)}&q=${encodeURIComponent(search.value)}`)); } catch (e) { err(e); }
    }
    search.addEventListener("input", window.BSP.debounce(load, 300));
    bulkBtn.onclick = () => busy(bulkBtn, async () => {
      const ids = t.selected().filter((r) => r.status === "needs_review").map((r) => r.id);
      const r = await api("/api/cases/bulk-approve", { body: { ids } });
      toast(`${r.approved.length} approved${r.failed.length ? `, ${r.failed.length} need fields first` : ""}`, r.failed.length ? "info" : "success");
      load(); counts();
    });
    main.replaceChildren(head("Review queue", `Extracted from ${CFG.mailbox}. Cases at or above ${pct(CFG.auto_approve_threshold)} confidence with clean warehouse checks are auto-approved.`, bulkBtn),
      h("div", { class: "row between wrap", style: { marginBottom: "12px" } }, seg, search), h("div", { class: "card" }, t.el));
    void filt;
    load();
  }

  // ── inbox ──────────────────────────────────────────────────────────
  async function viewInbox() {
    const pollBtn = h("button", { class: "btn primary" }, "Check mailbox now");
    const info = h("div", { class: "caption" });
    const t = table({
      rowKey: "id", pageSize: 100, onRowClick: (r) => r.case_id && (location.hash = "#case/" + r.case_id),
      empty: h("div", { class: "empty" }, "Nothing ingested yet."),
      columns: [
        { key: "received_at", label: "Received", sort: true, render: (r) => fmtTime(r.received_at) },
        { key: "sender_name", label: "From", render: (r) => h("div", null, r.sender_name, h("div", { class: "caption" }, r.sender_email)) },
        { key: "subject", label: "Subject", sort: true },
        { key: "att", label: "Attachments", render: (r) => (r.attachments || []).map((a) => a.name).join(", ") || "—" },
        { key: "moved_to", label: "Moved to", render: (r) => r.moved_to || "—" },
        { key: "case_ref", label: "Case", render: (r) => r.case_ref ? h("span", { class: "row" }, r.case_ref, badge(r.status)) : "—" },
      ],
    });
    async function load() {
      try { t.update(await api("/api/messages")); } catch (e) { err(e); }
      const lp = CFG.last_poll || {};
      info.textContent = lp.at ? `Last check ${fmtAgo(lp.at)} · ${lp.fetched} fetched · ${lp.processed} processed · source ${lp.source} · ${lp.llm ? "LLM extraction" : "rules fallback"}${(lp.errors || []).length ? ` · ${lp.errors.length} errors` : ""}` : `Polling every ${CFG.poll_seconds}s`;
    }
    pollBtn.onclick = () => busy(pollBtn, async () => {
      const r = await api("/api/poll", { method: "POST" });
      toast(r.status === "busy" ? "A poll is already running" : `${r.processed} new message(s) processed in ${r.seconds}s`, "success");
      CFG.last_poll = r; load(); counts();
    });
    main.replaceChildren(head("Inbox", `Unread mail in ${CFG.mailbox} is ingested, marked read and moved to “Processed” or “Needs review”.`, pollBtn),
      h("div", { class: "card" }, info, h("div", { style: { marginTop: "10px" } }, t.el)));
    load();
  }

  // ── case detail ────────────────────────────────────────────────────
  const FIELD_DEFS = [
    ["ban", "BAN (billing account)"], ["customer_name", "Customer name"], ["contact_email", "Contact e-mail"], ["contact_phone", "Contact phone"],
    ["invoice_id", "Invoice"], ["billing_period", "Billing period"], ["disputed_amount", "Disputed amount"], ["currency", "Currency"],
    ["category", "Category"], ["requested_resolution", "Requested resolution"], ["service_address", "Service address"],
    ["urgency", "Urgency"], ["sentiment", "Sentiment"], ["summary", "Summary"],
  ];
  const ENUMS = {
    requested_resolution: ["refund", "credit", "waive", "apply_payment", "explanation", "other"],
    urgency: ["low", "normal", "high"], sentiment: ["negative", "neutral", "positive"], currency: ["USD", "CAD"],
  };
  function highlighted(text, evidence) {
    const spans = Object.entries(evidence || {}).filter(([, e]) => e && e.source === "body" && e.start >= 0 && e.end > e.start)
      .map(([k, e]) => ({ k, ...e })).sort((a, b) => a.start - b.start);
    const out = []; let pos = 0;
    for (const s of spans) {
      if (s.start < pos) continue;
      out.push(text.slice(pos, s.start));
      out.push(h("mark", { class: "f-" + s.k, title: s.k }, text.slice(s.start, s.end)));
      pos = s.end;
    }
    out.push(text.slice(pos));
    return h("div", { class: "mail" }, ...out);
  }
  async function viewCase(id) {
    let c;
    try { c = await api("/api/cases/" + id); } catch (e) { err(e); return; }
    const m = c.message || {};
    const f = c.fields || {}, conf = c.confidence || {}, ev = c.evidence || {};
    const editable = !["submitted", "submitting"].includes(c.status);
    const inputs = {};
    const fg = h("div", { class: "fgrid" });
    for (const [k, label] of FIELD_DEFS) {
      let inp;
      if (k === "category") {
        inp = h("select", { class: "select" }, h("option", { value: "" }, "—"),
          ...Object.entries(CFG.categories).map(([ck, cv]) => h("option", { value: ck, selected: f.category === ck || null }, `${cv.label} (${cv.case_type}/${cv.reason_code})`)));
      } else if (ENUMS[k]) {
        inp = h("select", { class: "select" }, h("option", { value: "" }, "—"), ...ENUMS[k].map((o) => h("option", { value: o, selected: f[k] === o || null }, o)));
      } else if (k === "summary") {
        inp = h("textarea", { class: "textarea", rows: 3 }, f[k] || "");
      } else {
        inp = h("input", { class: "input", value: f[k] ?? "", type: k === "disputed_amount" ? "number" : "text", step: k === "disputed_amount" ? "0.01" : null });
      }
      if (!editable) inp.disabled = true;
      inputs[k] = inp;
      const cv = conf[k];
      fg.append(h("label", null, label), inp, h("span", { class: `conf ${cv >= 0.85 ? "hi" : cv >= 0.6 ? "mid" : "lo"}`, title: "field confidence" }, cv === undefined ? "" : pct(cv)));
      if (ev[k]) fg.append(h("span", { class: "src" }, `${ev[k].source}${ev[k].text ? ": “" + String(ev[k].text).slice(0, 80) + "”" : ""}`));
    }
    const val = c.validation || {};
    const checks = h("div", { class: "checks" }, ...(val.checks || []).map((x) =>
      h("span", { class: `badge ${x.status === "pass" ? "success" : x.status === "warn" ? "warning" : "error"}` }, icon(x.status === "pass" ? "tick-circle" : x.status === "warn" ? "warning" : "error", 13), `${x.check.replace(/_/g, " ")} — ${x.detail}`)));
    const ctx = val.context || {};
    const ctxBox = h("div", { class: "caption stack" },
      ctx.customer_name ? h("div", null, `Account holder: ${ctx.customer_name} · ${ctx.segment || ""} · ${ctx.city || ""} ${ctx.state || ""} · autopay ${ctx.autopay ? "ON" : "off"}`) : null,
      ctx.invoice ? h("div", null, `Invoice ${ctx.invoice.invoice_id} (${ctx.invoice.bill_cycle}): due ${money(ctx.invoice.amount_due)}, taxes & fees ${money(ctx.invoice.taxes_fees)}, late fee ${money(ctx.invoice.late_fee)}, status ${ctx.invoice.status}`) : null,
      ctx.recent_price_increase !== undefined ? h("div", null, `Recent price increase: ${ctx.recent_price_increase ? "yes" : "no"} · promo expired: ${ctx.promo_expired ? "yes" : "no"}`) : null,
      ctx.technology ? h("div", null, `Service: ${ctx.technology}${ctx.plan_id ? " · " + ctx.plan_id : ""}${ctx.clli ? " · wire center " + ctx.clli : ""}`) : null,
      (ctx.prior_credits || []).length ? h("div", null, `Prior credits: ${ctx.prior_credits.map((a) => `${a.txn_date} ${money(a.amount)}`).join(", ")}`) : null,
      (ctx.prior_disputes || []).length ? h("div", null, `Disputes on file: ${ctx.prior_disputes.map((d) => `${d.dispute_id} ${d.category} ${money(d.disputed_amount)} (${d.status})`).join(", ")}`) : null,
      (val.notes || []).map((n) => h("div", null, "Note: " + n)));
    const collect = () => Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, el.value]));
    const save = async () => { const r = await api("/api/cases/" + id, { method: "PATCH", body: { fields: collect() } }); return r; };
    const act = (label, cls, fn) => { const b = h("button", { class: "btn " + cls }, label); b.onclick = () => busy(b, async () => { try { await fn(); } catch (e) { err(e); } }); return b; };
    const actions = h("div", { class: "row wrap" });
    if (editable) actions.append(act("Save", "secondary", async () => { await save(); toast("Saved", "success"); viewCase(id); }));
    if (["needs_review", "failed", "rejected", "not_dispute"].includes(c.status))
      actions.append(act(c.status === "failed" ? "Retry (re-approve)" : "Approve", "primary", async () => { await save(); await api(`/api/cases/${id}/approve`, { method: "POST" }); toast("Approved — queued for SAP", "success"); counts(); viewCase(id); }));
    if (["needs_review", "approved", "failed", "not_dispute"].includes(c.status))
      actions.append(act("Reject", "danger", async () => { const reason = await prompt({ title: "Reject case", label: "Reason", okText: "Reject" }); if (reason === null) return; await api(`/api/cases/${id}/reject`, { body: { reason } }); counts(); viewCase(id); }));
    if (["rejected", "not_dispute", "approved"].includes(c.status))
      actions.append(act("Back to review", "ghost", async () => { await api(`/api/cases/${id}/reopen`, { method: "POST" }); viewCase(id); }));
    actions.append(act(c.assignee ? `Assigned: ${c.assignee}` : "Assign…", "ghost", async () => { const a = await prompt({ title: "Assign case", label: "Reviewer", value: c.assignee || "", okText: "Assign" }); if (a === null) return; await api(`/api/cases/${id}/reassign`, { body: { assignee: a } }); viewCase(id); }));
    const atts = (m.attachments || []).map((a) => {
      const url = `/disputes/api/messages/${m.id}/attachments/${encodeURIComponent(a.name.replace(/[^\w.-]/g, "_"))}`;
      return (a.contentType || "").startsWith("image/") ? h("img", { src: url, style: { maxWidth: "100%", borderRadius: "4px", marginTop: "8px", cursor: "zoom-in" }, onclick: () => zoom(url) })
        : h("a", { href: url, target: "_blank", class: "chip" }, icon("attach", 12), a.name);
    });
    const subs = (c.submissions || []).map((s) => h("div", { class: "card flat tight" },
      h("div", { class: "row between" }, h("strong", null, s.sap_dispute_id ? `SAP dispute ${s.sap_dispute_id}` : s.status), h("span", { class: "caption" }, `${s.mode} · ${s.adapter || ""} · ${fmtTime(s.finished_at)}`)),
      s.error ? h("div", { class: "caption", style: { color: "var(--bsp-text-error)" } }, s.error) : null,
      h("div", { class: "timeline" }, ...(s.steps || []).map((st) => h("div", null, `${new Date(st.ts * 1000).toLocaleTimeString()} · w${st.worker} · try ${st.attempt} · ${st.step} · ${st.status}${st.error ? " — " + st.error : ""}`))),
      h("div", { class: "thumbs", style: { marginTop: "6px" } }, ...(s.screenshots || []).map((p) => h("img", { src: shot(p), title: p.split("/").pop(), onclick: () => zoom(shot(p)) })))));
    main.replaceChildren(
      head(`${c.case_ref} · ${f.category_label || "Case"}`, null, badge(c.status), h("span", { class: "chip" }, `confidence ${pct(c.overall_confidence)}`),
        h("span", { class: "chip" }, `extraction: ${c.extraction_method}`), c.sap_dispute_id ? h("a", { class: "chip", href: c.sap_url || "#", target: "_blank" }, `SAP ${c.sap_dispute_id}`) : null,
        h("a", { class: "btn ghost sm", href: "#queue" }, icon("arrow-left", 14), "Queue")),
      h("div", { class: "case-grid" },
        h("div", { class: "stack lg" },
          h("div", { class: "card" }, h("div", { class: "mhead" },
            h("div", null, h("strong", null, m.subject)), h("div", { class: "caption" }, `From ${m.sender_name || ""} <${m.sender_email || ""}> · ${fmtTime(m.received_at)}`),
            h("div", { class: "caption" }, `SLA: `, sla(c.sla_due_at, c.status))),
            highlighted(m.body_text || "", ev), ...atts),
          subs.length ? h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", null, "SAP submissions")), ...subs) : null),
        h("div", { class: "stack lg" },
          h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", null, "Extracted fields"), h("span", { class: "caption" }, `SAP: ${f.case_type || "?"} / ${f.reason_code || "?"}`)), fg,
            h("div", { style: { marginTop: "14px" } }, actions)),
          h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", null, "Warehouse validation")), checks, h("div", { style: { marginTop: "10px" } }, ctxBox)),
          h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", null, "History")),
            h("div", { class: "timeline" }, ...(c.history || []).slice().reverse().map((x) => h("div", null, `${fmtTime(x.ts)} · ${x.action} · ${x.by}${x.changes ? " · " + Object.keys(x.changes).join(", ") : ""}${x.dispute_id ? " · " + x.dispute_id : ""}${x.error ? " · " + x.error : ""}`)))))));
  }

  // ── submissions + live browser ─────────────────────────────────────
  function workerCards(L) {
    const ws = Object.entries(L.workers || {});
    if (!ws.length) return [h("div", { class: "caption" }, L.running ? "Starting workers…" : "No batch running.")];
    return ws.map(([wid, w]) => h("div", { class: "wcard" },
      h("div", { class: "row between" }, h("strong", null, `Worker ${wid}`), w.status ? h("span", { class: `badge ${w.status === "ok" ? "success" : w.status === "failed" ? "error" : w.status === "warning" ? "warning" : "running"}` }, w.status) : null),
      h("div", null, `${w.case_ref || "—"} · ${w.step || "idle"}${w.attempt > 1 ? " · try " + w.attempt : ""}`),
      w.dispute_id ? h("div", { class: "caption" }, "SAP " + w.dispute_id) : null,
      w.error ? h("div", { class: "caption", style: { color: "var(--bsp-text-error)" } }, w.error) : null,
      w.screenshot ? h("img", { src: shot(w.screenshot), onclick: () => zoom(shot(w.screenshot)) }) : null));
  }
  async function openLive() {
    const panel = document.getElementById("live-panel");
    const host = document.getElementById("live-frame-host");
    panel.hidden = false;
    if (!host.querySelector("iframe")) {
      host.replaceChildren(h("div", { class: "msg" }, "Connecting to the appliance desktop…"));
      let ok = false;
      try { const r = await fetch(CFG.browser_path, { credentials: "same-origin", redirect: "manual" }); ok = r.ok; } catch (e) { ok = false; }
      if (panel.hidden) return;
      host.replaceChildren(ok ? h("iframe", { src: CFG.browser_embed, title: "Live browser desktop", allow: "clipboard-read; clipboard-write" })
        : h("div", { class: "msg" }, h("strong", null, "The live desktop isn't running."), h("br"),
          "Add the Browser capability in Studio to watch the Playwright windows. Submissions still run (headless fallback) and every step is screenshotted below."));
    }
    startLivePolling();
  }
  function closeLive() {
    const panel = document.getElementById("live-panel");
    document.getElementById("live-frame-host").replaceChildren(); // destroy iframe → closes the VNC websocket
    panel.hidden = true;
  }
  document.getElementById("live-close").onclick = closeLive;
  function startLivePolling() {
    if (liveTimer) return;
    liveTimer = setInterval(pollLive, 1000);
    pollLive();
  }
  async function pollLive() {
    let L;
    try { L = await api("/api/live"); } catch (e) { return; }
    document.getElementById("live-workers").replaceChildren(
      h("div", { class: "caption" }, L.running ? `Batch ${L.batch_id} · ${L.mode} · ${L.adapter || ""}` : L.last ? `Last batch: ${L.last.cases} case(s) in ${L.last.seconds}s via ${L.last.adapter}` : ""),
      ...workerCards(L));
    const lw = document.getElementById("sub-live");
    if (lw) lw.replaceChildren(...workerCards(L));
    const st = document.getElementById("sub-state");
    if (st) st.textContent = L.running ? `Running batch ${L.batch_id} — mode ${L.mode}, adapter ${L.adapter}` : L.last ? `Last batch: ${JSON.stringify(L.last.counts)} in ${L.last.seconds}s · mode ${L.last.mode} · adapter ${L.last.adapter}` : "Idle";
    if (L.running && L.mode === "playwright" && !lastRunning && LS("autoOpenLive", true)) openLive();
    if (lastRunning && !L.running) { counts(); if (location.hash.startsWith("#submissions")) viewSubmissions(); }
    lastRunning = L.running;
    if (!L.running && document.getElementById("live-panel").hidden) { clearInterval(liveTimer); liveTimer = null; }
  }
  async function viewSubmissions() {
    const sap = CFG.sap || {};
    const modeSel = h("select", { class: "select", style: { width: "160px" } }, ...["playwright", "api", "mock"].map((m) => h("option", { value: m, selected: (LS("subMode", sap.mode) === m) || null }, m)));
    const conc = h("input", { class: "input", type: "number", min: 1, max: 8, value: LS("subConc", CFG.concurrency), style: { width: "80px" } });
    const runBtn = h("button", { class: "btn primary" }, "Submit approved cases");
    const liveBtn = h("button", { class: "btn secondary", onclick: openLive }, "Live browser");
    const autoOpen = h("input", { type: "checkbox", class: "checkbox", checked: LS("autoOpenLive", true) || null, onchange: (e) => setLS("autoOpenLive", e.target.checked) });
    runBtn.onclick = () => busy(runBtn, async () => {
      setLS("subMode", modeSel.value); setLS("subConc", +conc.value);
      try {
        const r = await api("/api/submit", { body: { mode: modeSel.value, concurrency: +conc.value } });
        toast(r.status === "busy" ? "A batch is already running" : "Batch started", r.status === "busy" ? "info" : "success");
        if (modeSel.value === "playwright" && LS("autoOpenLive", true)) openLive();
        startLivePolling();
      } catch (e) { err(e); }
    });
    const t = table({
      rowKey: "id", pageSize: 50, empty: h("div", { class: "empty" }, "No submissions yet."),
      columns: [
        { key: "finished_at", label: "When", sort: true, render: (r) => fmtTime(r.finished_at) },
        { key: "case_ref", label: "Case", render: (r) => h("a", { href: "#case/" + r.case_id }, r.case_ref) },
        { key: "mode", label: "Mode", render: (r) => `${r.mode} · ${r.adapter || ""}` },
        { key: "status", label: "Result", render: (r) => h("span", { class: `badge ${["created", "exists"].includes(r.status) ? "success" : "error"}` }, r.status) },
        { key: "sap_dispute_id", label: "SAP dispute", render: (r) => r.url ? h("a", { href: r.url, target: "_blank" }, r.sap_dispute_id || "—") : r.sap_dispute_id || "—" },
        { key: "worker", label: "Worker", num: true }, { key: "attempts", label: "Tries", num: true },
        { key: "duration_ms", label: "Duration", num: true, sort: true, render: (r) => r.duration_ms ? (r.duration_ms / 1000).toFixed(1) + "s" : "—" },
        { key: "shots", label: "Screenshots", render: (r) => h("div", { class: "thumbs" }, ...(r.screenshots || []).slice(-3).map((p) => h("img", { src: shot(p), style: { width: "64px", height: "40px" }, onclick: () => zoom(shot(p)) }))) },
        { key: "error", label: "Error", render: (r) => r.error ? h("span", { class: "caption", title: r.error }, r.error.slice(0, 60)) : "" },
      ],
    });
    main.replaceChildren(head("Submissions", "Approved cases are raised in SAP BRIM — over the OData API, or by Playwright driving the dispute UI in parallel browser windows you can watch live.", liveBtn),
      h("div", { class: "card" },
        h("div", { class: "row wrap", style: { gap: "14px" } },
          h("label", { class: "row" }, "Mode ", modeSel), h("label", { class: "row" }, "Parallel windows ", conc), runBtn,
          h("label", { class: "row caption" }, autoOpen, " open live browser when a Playwright batch starts")),
        h("div", { class: "caption", id: "sub-state", style: { marginTop: "10px" } }, "…"),
        h("div", { id: "sub-live", class: "grid auto-s", style: { marginTop: "10px" } })),
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", null, "Submission log")), t.el));
    try { t.update(await api("/api/submissions")); } catch (e) { err(e); }
    startLivePolling();
  }

  // ── dashboard ──────────────────────────────────────────────────────
  async function viewDashboard() {
    let m;
    try { m = await api("/api/metrics"); } catch (e) { err(e); return; }
    const kpi = (label, v, sub) => h("div", { class: "card tight" }, h("div", { class: "eyebrow" }, label), h("div", { class: "kpi-s" }, v), sub ? h("div", { class: "caption" }, sub) : null);
    const st = m.by_status || {};
    const total = Object.values(st).reduce((a, b) => a + b, 0) || 1;
    const COLORS = { submitted: "#0b6e79", approved: "#3380f0", needs_review: "#ffcf00", failed: "#ff3737", rejected: "#828487", not_dispute: "#d9dbdc", submitting: "#3ae1ce" };
    const fa = Object.entries(m.field_accuracy || {});
    main.replaceChildren(head("Dashboard", "Throughput, automation rate and reviewer-measured extraction accuracy."),
      h("div", { class: "grid cols-4" },
        kpi("Mails processed", m.total ?? 0, `${m.disputes ?? 0} disputes`),
        kpi("Auto-approved", m.auto_approved ?? 0, m.disputes ? pct(m.auto_approved / m.disputes) + " of disputes" : ""),
        kpi("In SAP", st.submitted || 0, m.median_secs_to_submit ? `median ${Math.round(m.median_secs_to_submit / 60)} min mail→SAP` : ""),
        kpi("SLA", (m.sla_breached || 0) + " breached", `${m.sla_due_soon || 0} due within 8h`)),
      h("div", { class: "card", style: { marginTop: "16px" } }, h("div", { class: "card-head" }, h("h3", null, "Case status")),
        h("div", { class: "bar" }, ...Object.entries(st).map(([k, v]) => h("span", { title: `${k}: ${v}`, style: { width: (100 * v / total) + "%", background: COLORS[k] || "#999" } }))),
        h("div", { class: "row wrap caption", style: { marginTop: "8px" } }, ...Object.entries(st).map(([k, v]) => h("span", { class: "row" }, h("span", { style: { width: "10px", height: "10px", background: COLORS[k] || "#999", display: "inline-block", borderRadius: "2px" } }), `${(STATUS[k] || [0, k])[1]} ${v}`)))),
      h("div", { class: "grid cols-2", style: { marginTop: "16px" } },
        h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", null, "Extraction accuracy (reviewed cases)")),
          fa.length && fa[0][1].reviewed ? h("table", { class: "table" }, h("thead", null, h("tr", null, h("th", null, "Field"), h("th", { class: "num" }, "Reviewed"), h("th", { class: "num" }, "Corrected"), h("th", { class: "num" }, "Accuracy"))),
            h("tbody", null, ...fa.map(([k, d]) => h("tr", null, h("td", null, k), h("td", { class: "num" }, d.reviewed), h("td", { class: "num" }, d.corrected), h("td", { class: "num" }, pct(d.accuracy))))))
            : h("div", { class: "empty" }, "Accuracy appears once reviewers approve cases (corrections are measured per field)."),
          h("div", { class: "caption", style: { marginTop: "8px" } }, "Extraction methods: " + Object.entries(m.methods || {}).map(([k, v]) => `${k} ${v}`).join(" · "))),
        h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", null, "SAP submissions")),
          (m.submissions || []).length ? h("table", { class: "table" }, h("thead", null, h("tr", null, h("th", null, "Mode"), h("th", null, "Adapter"), h("th", null, "Result"), h("th", { class: "num" }, "Count"), h("th", { class: "num" }, "Avg"))),
            h("tbody", null, ...m.submissions.map((s) => h("tr", null, h("td", null, s.mode), h("td", null, s.adapter || ""), h("td", null, s.status), h("td", { class: "num" }, s.n), h("td", { class: "num" }, s.avg_ms ? (s.avg_ms / 1000).toFixed(1) + "s" : "—")))))
            : h("div", { class: "empty" }, "No submissions yet."),
          h("div", { class: "caption", style: { marginTop: "8px" } }, "Categories: " + Object.entries(m.categories || {}).map(([k, v]) => `${k} ${v}`).join(" · ")))));
  }

  // ── settings ───────────────────────────────────────────────────────
  function viewSettings() {
    const reset = h("button", { class: "btn danger" }, "Reset demo data");
    reset.onclick = async () => {
      if (!(await confirm({ title: "Reset demo?", body: "Deletes all cases and submissions and marks the mock mailbox unread again. Mock SAP disputes are kept (idempotency will find them).", okText: "Reset", danger: true }))) return;
      busy(reset, async () => { try { await api("/api/reset-demo", { method: "POST" }); toast("Reset done", "success"); counts(); } catch (e) { err(e); } });
    };
    const rows = [["Mailbox", CFG.mailbox], ["Mail source", CFG.m365_mode], ["Poll interval", CFG.poll_seconds + " s"], ["Auto-submit after poll", String(CFG.auto_submit)],
      ["Auto-approve threshold", pct(CFG.auto_approve_threshold)], ["SLA", CFG.sla_hours + " h"], ["LLM gateway", CFG.llm_available ? "available (extract-default, vlm-default)" : "unavailable → deterministic rules"],
      ["SAP mode", (CFG.sap || {}).mode], ["SAP base URL", (CFG.sap || {}).base_url], ["SAP web UI", (CFG.sap || {}).ui_url], ["Browser CDP", (CFG.sap || {}).cdp_url], ["Parallel windows", CFG.concurrency]];
    main.replaceChildren(head("Settings", "Configured through Studio (environment); shown read-only here."),
      h("div", { class: "card" }, h("table", { class: "table" }, h("tbody", null, ...rows.map(([k, v]) => h("tr", null, h("td", { class: "muted" }, k), h("td", null, v ?? "—")))))),
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", null, "Category → SAP reason codes")),
        h("table", { class: "table" }, h("tbody", null, ...Object.entries(CFG.categories).map(([k, v]) => h("tr", null, h("td", null, k), h("td", null, v.label), h("td", null, `${v.case_type} / ${v.reason_code}`)))))),
      h("div", { class: "card" }, h("div", { class: "row between" }, h("span", { class: "caption" }, "Demo only — refused when the mailbox is live."), reset)));
  }

  // ── router ─────────────────────────────────────────────────────────
  async function route() {
    const hash = location.hash || "#queue";
    const [view, arg] = hash.slice(1).split("/");
    document.querySelectorAll(".bsp-nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === (view === "case" ? "queue" : view)));
    if (!CFG) { try { await loadConfig(); } catch (e) { main.replaceChildren(h("div", { class: "alert" }, "Dispute Desk API unavailable: " + (e.message || e))); return; } }
    if (view === "case") return viewCase(arg);
    return ({ queue: viewQueue, inbox: viewInbox, submissions: viewSubmissions, dashboard: viewDashboard, settings: viewSettings }[view] || viewQueue)();
  }
  window.addEventListener("hashchange", route);
  route().then(counts);
  setInterval(counts, 15000);
})();
