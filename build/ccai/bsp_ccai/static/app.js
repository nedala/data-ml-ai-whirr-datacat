/* CCAI Call Insights portal — single page, hash routed, built on window.BSP (ui-kit). */
(function () {
  "use strict";
  const { h, icon, toast, esc } = BSP;
  const api = BSP.api("/ccai");
  const main = document.getElementById("main");
  const pages = {};
  const S = { status: null, leaves: null, byId: {}, progress: null, page: null, wasActive: false };

  // ── helpers ─────────────────────────────────────────────────────────
  const fmtN = (n) => (n == null ? "–" : Number(n).toLocaleString());
  const pct = (x, d = 0) => (x == null ? "–" : `${(100 * x).toFixed(d)}%`);
  const fmtDur = (s) => { if (s == null) return "–"; s = Math.round(s); const m = Math.floor(s / 60); return m ? `${m}m ${String(s % 60).padStart(2, "0")}s` : `${s}s`; };
  const fmtMs = (ms) => (ms == null ? "–" : ms < 1000 ? `${ms} ms` : fmtDur(ms / 1000));
  const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "–");
  const qs = (o) => { const p = new URLSearchParams(); Object.entries(o).forEach(([k, v]) => { if (v !== "" && v != null && v !== false) p.set(k, v); }); const s = p.toString(); return s ? `?${s}` : ""; };
  const leafName = (id) => (S.byId[id] ? S.byId[id].name : id);
  const STATUS_BADGE = { succeeded: "ok", running: "running", partial: "warning", failed: "error", cancelled: "neutral", interrupted: "warning" };
  const RES_LABEL = { resolved: "Resolved", pending_followup: "Follow-up", escalated: "Escalated", unresolved: "Unresolved", unknown: "Unclear" };
  const RES_BADGE = { resolved: "ok", pending_followup: "info", escalated: "warning", unresolved: "error", unknown: "neutral" };

  function badge(kind, text, dot) { return h("span", { class: `badge ${kind}` }, dot ? h("span", { class: "dot" }) : null, text); }
  function confCell(c, band) {
    if (c == null) return h("span", { class: "muted" }, "–");
    return h("span", { class: `conf ${band || ""}`, "data-tip": `Calibrated confidence <b>${(c * 100).toFixed(0)}%</b> · ${band || ""}` },
      h("span", { class: "meter" }, h("span", { style: { width: `${Math.round(c * 100)}%` } })), h("span", { class: "nowrap" }, `${Math.round(c * 100)}%`));
  }
  function sentCell(traj) {
    if (!traj) return h("span", { class: "muted" }, "–");
    const ic = traj === "improving" ? "arrow-up" : traj === "declining" ? "arrow-down" : "arrow-right";
    const label = traj.replace("stable-", "steady ").replace("-", " ");
    return h("span", { class: "sent", "data-tip": "Customer sentiment from the first third to the last third of the call" }, icon(ic, 14), label);
  }
  function leafCell(row) {
    const id = row.leaf || row.override_leaf || row.primary_leaf;
    if (!id) return h("span", { class: "muted" }, row.status === "parsed" ? "awaiting tag" : "–");
    return h("div", { class: "leafcell" }, h("span", { class: "l1" }, row.l1_name || (S.byId[id] || {}).l1_name || ""),
      h("span", null, row.leaf_name || leafName(id), row.override_leaf ? h("span", { class: "chip auto", style: { marginLeft: "6px", height: "18px" }, "data-tip": "Reviewer override" }, "reviewed") : null));
  }
  function pageHead(title, lede, actions) {
    return h("div", { class: "bsp-page-head" }, h("div", null, h("h1", null, title), lede ? h("div", { class: "lede" }, lede) : null),
      actions ? h("div", { class: "row wrap" }, actions) : null);
  }
  function card(title, body, extra, sub) {
    return h("div", { class: "card" }, title ? h("div", { class: "card-head" }, h("div", null, h("h4", null, title), sub ? h("div", { class: "caption" }, sub) : null), extra || null) : null, body);
  }
  function emptyState(title, text, action) {
    return h("div", { class: "empty" }, h("div", { class: "ic" }, icon("insight", 22)), h("h4", null, title), h("div", null, text), action ? h("div", { style: { marginTop: "12px" } }, action) : null);
  }
  function stat(label, value, sub, cls, tipText) {
    return h("div", { class: `stat ${cls || ""}`, "data-tip": tipText || null }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), sub ? h("div", { class: "delta" }, sub) : null);
  }

  // tooltip layer: any element with data-tip (HTML allowed, values are escaped by producers)
  const tip = document.getElementById("tip");
  document.addEventListener("mousemove", (e) => {
    const t = e.target.closest && e.target.closest("[data-tip]");
    if (!t) { tip.hidden = true; return; }
    tip.innerHTML = t.getAttribute("data-tip");
    tip.hidden = false;
    const x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
    const y = e.clientY + 16 + tip.offsetHeight > window.innerHeight ? e.clientY - tip.offsetHeight - 10 : e.clientY + 16;
    tip.style.left = `${x}px`; tip.style.top = `${y}px`;
  });

  // ── charts (single-hue, inline SVG) ─────────────────────────────────
  const NS = "http://www.w3.org/2000/svg";
  function svg(tag, attrs, ...kids) {
    const el = document.createElementNS(NS, tag);
    Object.entries(attrs || {}).forEach(([k, v]) => { if (v != null) el.setAttribute(k, v); });
    kids.flat().forEach((k) => k != null && el.append(k instanceof Node ? k : document.createTextNode(String(k))));
    return el;
  }
  function roundedBarPath(x, y, w, hgt, r, horizontal) {
    r = Math.min(r, horizontal ? hgt / 2 : w / 2, horizontal ? w : hgt);
    if (w <= 0 || hgt <= 0) return "";
    if (horizontal) return `M${x},${y}h${w - r}a${r},${r} 0 0 1 ${r},${r}v${hgt - 2 * r}a${r},${r} 0 0 1 -${r},${r}h-${w - r}z`;
    return `M${x},${y + hgt}v-${hgt - r}a${r},${r} 0 0 1 ${r},-${r}h${w - 2 * r}a${r},${r} 0 0 1 ${r},${r}v${hgt - r}z`;
  }
  // horizontal bars: rows [{label, value, sub, tip, onClick}]
  function barsH(rows, { width = 560, labelW = 210, fmt = fmtN } = {}) {
    const rowH = 26, H = rows.length * rowH + 4;
    const max = Math.max(1, ...rows.map((r) => r.value));
    const plotW = width - labelW - 56;
    const s = svg("svg", { class: "viz", viewBox: `0 0 ${width} ${H}`, role: "img" });
    rows.forEach((r, i) => {
      const y = i * rowH + 4, w = Math.max(2, (r.value / max) * plotW);
      const g = svg("g", { class: "mark", "data-tip": r.tip || `<b>${esc(r.label)}</b><br>${fmt(r.value)}` });
      const lbl = r.label.length > 30 ? r.label.slice(0, 29) + "…" : r.label;
      g.append(svg("text", { x: labelW - 8, y: y + 14, "text-anchor": "end", class: "lbl" }, lbl),
        svg("path", { d: roundedBarPath(labelW, y + 3, w, rowH - 10, 4, true), class: "bar" }),
        svg("text", { x: labelW + w + 6, y: y + 14, class: "val" }, fmt(r.value)),
        svg("rect", { x: 0, y, width, height: rowH, class: "hit" }));
      if (r.onClick) { g.style.cursor = "pointer"; g.addEventListener("click", r.onClick); }
      s.append(g);
    });
    return s;
  }
  // vertical columns: bins [{label, value, tip, alt}] + optional threshold (0..1 along x)
  function columns(bins, { width = 560, height = 200, threshold = null, xLabels = true, fmt = fmtN } = {}) {
    const pad = { l: 34, r: 8, t: 10, b: 24 };
    const pw = width - pad.l - pad.r, ph = height - pad.t - pad.b;
    const max = Math.max(1, ...bins.map((b) => b.value));
    const step = pw / bins.length, bw = Math.max(3, step - 4);
    const s = svg("svg", { class: "viz", viewBox: `0 0 ${width} ${height}`, role: "img" });
    const grid = svg("g", { class: "grid" });
    const ticks = 4;
    for (let i = 0; i <= ticks; i++) {
      const v = Math.round((max * i) / ticks), y = pad.t + ph - (ph * i) / ticks;
      grid.append(svg("line", { x1: pad.l, x2: width - pad.r, y1: y, y2: y }), svg("text", { x: pad.l - 6, y: y + 4, "text-anchor": "end" }, fmt(v)));
    }
    s.append(grid);
    bins.forEach((b, i) => {
      const hh = (b.value / max) * ph, x = pad.l + i * step + 2, y = pad.t + ph - hh;
      const g = svg("g", { class: "mark", "data-tip": b.tip || `<b>${esc(b.label)}</b><br>${fmt(b.value)}` });
      g.append(svg("path", { d: roundedBarPath(x, y, bw, hh, 4, false), class: `bar ${b.alt ? "alt" : ""}` }),
        svg("rect", { x: pad.l + i * step, y: pad.t, width: step, height: ph, class: "hit" }));
      if (xLabels && (bins.length <= 12 || i % Math.ceil(bins.length / 10) === 0)) g.append(svg("text", { x: x + bw / 2, y: height - 8, "text-anchor": "middle" }, b.label));
      s.append(g);
    });
    if (threshold != null) {
      const x = pad.l + threshold * pw;
      s.append(svg("line", { x1: x, x2: x, y1: pad.t - 4, y2: pad.t + ph, class: "thr" }),
        svg("text", { x: x + 4, y: pad.t + 6, class: "val" }, `threshold ${Math.round(threshold * 100)}%`));
    }
    return s;
  }
  function sparkline(series, { width = 220, height = 40 } = {}) {
    const s = svg("svg", { class: "viz spark", viewBox: `0 0 ${width} ${height}`, width, height });
    if (!series || series.length < 2) return s;
    const x = (i) => 4 + (i * (width - 8)) / (series.length - 1), y = (v) => height / 2 - (v * (height - 8)) / 2;
    s.append(svg("line", { x1: 0, x2: width, y1: height / 2, y2: height / 2, stroke: "var(--bsp-border-subtle)", "stroke-dasharray": "3 3" }),
      svg("polyline", { points: series.map((v, i) => `${x(i)},${y(v)}`).join(" "), fill: "none", stroke: "var(--bsp-primary-fiber)", "stroke-width": 2, "stroke-linejoin": "round" }));
    series.forEach((v, i) => s.append(svg("circle", { cx: x(i), cy: y(v), r: 4, fill: "var(--bsp-primary-fiber)", stroke: "#fff", "stroke-width": 2, "data-tip": `Customer turn ${i + 1}: sentiment ${v.toFixed(2)}` })));
    return s;
  }
  // squarified treemap → absolutely positioned divs
  function squarify(items, x, y, w, hgt) {
    const out = [];
    const total = items.reduce((a, b) => a + b.value, 0);
    if (!total) return out;
    const scale = (w * hgt) / total;
    let rest = items.map((it) => ({ ...it, area: it.value * scale }));
    while (rest.length) {
      const short = Math.min(w, hgt);
      let row = [rest[0]], i = 1;
      const worst = (r) => { const s = r.reduce((a, b) => a + b.area, 0); const mx = Math.max(...r.map((q) => q.area)), mn = Math.min(...r.map((q) => q.area)); return Math.max((short * short * mx) / (s * s), (s * s) / (short * short * mn)); };
      while (i < rest.length && worst([...row, rest[i]]) <= worst(row)) { row.push(rest[i]); i++; }
      const sum = row.reduce((a, b) => a + b.area, 0);
      if (w >= hgt) { const cw = sum / hgt; let cy = y; row.forEach((r) => { const ch = r.area / cw; out.push({ ...r, x, y: cy, w: cw, h: ch }); cy += ch; }); x += cw; w -= cw; }
      else { const ch = sum / w; let cx = x; row.forEach((r) => { const cw = r.area / ch; out.push({ ...r, x: cx, y, w: cw, h: ch }); cx += cw; }); y += ch; hgt -= ch; }
      rest = rest.slice(row.length);
    }
    return out;
  }
  const RAMP = ["#e6f3f4", "#cfe8eb", "#9fd6dc", "#2f8f99", "#0b6e79", "#06454c"];
  function rampColor(v) { // v 0..1 → sequential teal
    if (v == null) return "#eef0f2";
    const t = Math.max(0, Math.min(1, (v - 0.3) / 0.65)) * (RAMP.length - 1);
    return RAMP[Math.round(t)];
  }

  // ── shell: nav, top status, live progress ───────────────────────────
  const PAGES = [
    { id: "overview", label: "Overview", icon: "dashboard" },
    { id: "runs", label: "Runs", icon: "history" },
    { id: "calls", label: "Calls", icon: "chat" },
    { id: "taxonomy", label: "Taxonomy", icon: "stacks" },
    { id: "quality", label: "Quality & review", icon: "shield" },
    { id: "downloads", label: "Downloads", icon: "download" },
    { id: "settings", label: "Settings", icon: "settings" },
  ];
  function renderNav() {
    const cur = (location.hash.slice(1).split("?")[0] || "overview");
    const nav = document.getElementById("nav");
    const queue = S.status ? S.status.counts.tags.needs_review : null;
    nav.replaceChildren(h("div", { class: "bsp-nav-group eyebrow" }, "Call insights"),
      ...PAGES.map((p) => h("a", { href: `#${p.id}`, class: cur === p.id ? "active" : "" }, icon(p.icon, 18), p.label,
        p.id === "quality" && queue ? h("span", { class: "badge warning", "data-tip": "Low-confidence calls waiting for review" }, String(queue)) : null)));
    document.getElementById("sideHelp").innerHTML =
      "<b>How it works</b><br>Call logs dropped in the SharePoint folder are picked up on a schedule (or with <b>Sync now</b>), parsed and tagged with one of 104 contact reasons plus a confidence score.";
  }
  function renderTop() {
    const st = S.status; if (!st) return;
    const p = S.progress || st.progress || {};
    const box = document.getElementById("topStatus");
    const src = st.source;
    box.replaceChildren(
      p.active ? h("span", { class: "badge running" }, h("span", { class: "dot" }), `Run #${p.run_id} · ${p.phase} · ${p.percent || 0}%`) :
        h("span", { class: "caption", "data-tip": "Last successful sync" }, st.last_sync ? `Synced ${BSP.fmtAgo(st.last_sync)}` : "Not synced yet"),
      h("span", { class: `chip ${src.mode === "live" ? "primary" : "outline"}`, "data-tip": `SharePoint ${esc(src.site)} / ${esc(src.folder)} via ${esc(src.adapter)}` }, icon("cloud", 12), `SharePoint · ${src.mode}`),
      h("span", { class: `chip ${st.llm.available && st.llm.route !== "offline" ? "primary" : "outline"}`, "data-tip": st.llm.available ? `LLM gateway reachable · route ${esc(st.llm.route)} · ${esc(st.llm.classify_alias)}` : "LLM gateway not reachable — the deterministic offline tagger is used" },
        icon("wand", 12), st.llm.available && st.llm.route !== "offline" ? `LLM · ${st.llm.classify_alias}` : "Offline tagger"));
    const btn = document.getElementById("runNow");
    btn.disabled = !!p.active;
    btn.textContent = p.active ? "Running…" : "Sync now";
  }
  async function loadStatus() {
    try { S.status = await api("/api/status"); if (!S.progress || !S.progress.active) S.progress = S.status.progress; renderTop(); renderNav(); }
    catch (e) { console.warn(e); }
  }
  async function startRun(mode, extra) {
    try {
      const r = await api("/api/runs", { body: { mode: mode || "incremental", ...(extra || {}) } });
      S.progress = r.progress; S.wasActive = true; renderTop();
      toast(`Run #${r.run_id} started`, "success");
      if (S.page === "overview") pages.overview();
    } catch (e) { toast(e.message || "Could not start run", "error"); }
  }
  document.getElementById("runNow").onclick = () => startRun("incremental");

  function connectEvents() {
    let es;
    try { es = new EventSource("/ccai/api/events"); } catch (e) { return poll(); }
    es.addEventListener("progress", (ev) => onProgress(JSON.parse(ev.data)));
    es.onerror = () => { es.close(); setTimeout(connectEvents, 5000); };
  }
  function poll() { setInterval(async () => { try { const st = await api("/api/status"); onProgress(st.progress); } catch (e) { /* ignore */ } }, 3000); }
  function onProgress(p) {
    const was = S.wasActive;
    S.progress = p; S.wasActive = !!p.active;
    renderTop();
    const live = document.getElementById("liveRun");
    if (live) live.replaceWith(runCard(p));
    if (was && !p.active && p.run_id) {
      toast(`Run #${p.run_id} ${p.status || "finished"} — ${p.message || ""}`, p.status === "succeeded" ? "success" : p.status === "failed" ? "error" : "info");
      loadStatus().then(() => route());
    }
  }

  // ── Overview ────────────────────────────────────────────────────────
  const PHASES = ["listing", "downloading", "tagging", "done"];
  function runCard(p) {
    p = p || {};
    const el = h("div", { class: "card", id: "liveRun" });
    if (!p.run_id) { el.append(h("div", { class: "card-head" }, h("h4", null, "Current run")), h("div", { class: "muted" }, "No run yet. Use Sync now to pull the call logs from SharePoint.")); return el; }
    const idx = PHASES.indexOf(p.phase);
    el.append(
      h("div", { class: "card-head" }, h("div", null, h("h4", null, p.active ? `Run #${p.run_id} in progress` : `Last run #${p.run_id}`),
        h("div", { class: "caption" }, `${p.trigger || ""} · ${p.mode || ""} · started ${fmtDate(p.started_at)}`)),
        p.active ? h("button", { class: "btn ghost sm", onclick: async () => { await api("/api/runs/cancel", { body: {} }); toast("Cancelling…"); } }, icon("stop", 14), "Cancel")
          : badge(STATUS_BADGE[p.status] || "neutral", p.status || "done", true)),
      h("div", { class: `progress-big ${p.active ? "live" : ""}`, role: "progressbar", "aria-valuenow": p.percent || 0, "aria-valuemin": 0, "aria-valuemax": 100 },
        h("span", { style: { width: `${p.active ? p.percent || 2 : 100}%` } })),
      h("div", { class: "phases" }, PHASES.map((ph, i) => h("span", { class: `chip ${i < idx || p.phase === "done" ? "done" : i === idx ? "now" : ""}` },
        i < idx || p.phase === "done" ? icon("check", 12) : null, { listing: "1 · List SharePoint", downloading: "2 · Download & parse", tagging: "3 · Tag", done: "4 · Done" }[ph]))),
      h("div", { class: "grid cols-4", style: { marginTop: "14px" } },
        stat("Files in folder", fmtN(p.discovered), p.skipped ? `${fmtN(p.skipped)} unchanged, skipped` : null),
        stat("Downloaded / parsed", `${fmtN(p.downloaded)} / ${fmtN(p.parsed)}`, `${BSP.fmtBytes(p.bytes || 0)} of ${fmtN(p.to_process)} to fetch`),
        stat("Tagged", `${fmtN(p.tagged)}${p.to_tag ? ` / ${fmtN(p.to_tag)}` : ""}`, p.route ? `route: ${p.route}` : null),
        stat("Throughput", p.rate_per_min ? `${p.rate_per_min}/min` : "–", p.active && p.eta_s != null ? `≈ ${fmtDur(p.eta_s)} left` : p.errors ? `${p.errors} error(s)` : null, p.errors ? "err" : "")),
      p.message ? h("div", { class: "caption", style: { marginTop: "10px" } }, p.message) : null);
    return el;
  }
  pages.overview = async function () {
    const st = S.status || (await api("/api/status"));
    const ov = await api("/api/overview");
    const c = st.counts;
    const thr = st.settings.confidence_threshold;
    const v = h("div", { class: "fade-in" });
    v.append(pageHead("Overview", "Contact-centre call logs from SharePoint, tagged against DataPlatform's 104 contact reasons. Everything on this page updates live while a run is in progress.",
      [h("button", { class: "btn secondary", onclick: () => (location.hash = "#runs") }, icon("history", 16), "Run history")]));
    v.append(h("div", { class: "funnel" },
      stat("Files discovered", fmtN(c.files.discovered), `${BSP.fmtBytes(c.files.bytes)} · ${fmtN(c.files.manifests)} manifests`, "", "Files seen in the SharePoint folder (all date sub-folders)"),
      stat("Downloaded", fmtN(c.files.downloaded), "copied to the appliance", "", "Stored under data/ccai/raw"),
      stat("Calls parsed", fmtN(c.calls.calls), `${c.calls.talk_seconds > 7200 ? `${(c.calls.talk_seconds / 3600).toFixed(1)} h` : fmtDur(c.calls.talk_seconds)} of talk`, "", "Normalised call records (turns, talk ratio, silence, sentiment)"),
      stat("Calls tagged", fmtN(c.calls.tagged), c.calls.pending_tag ? `${fmtN(c.calls.pending_tag)} waiting` : `avg confidence ${pct(c.tags.avg_conf)}`),
      stat("Errors", fmtN((c.files.errors || 0) + (c.calls.tag_errors || 0)), c.files.errors ? "open Runs → Re-run failed" : "none", c.files.errors ? "err" : "")));
    v.append(h("div", { class: "section" }, runCard(S.progress && S.progress.run_id ? S.progress : st.last_run && { ...st.last_run, phase: "done", percent: 100, discovered: st.last_run.files_discovered, downloaded: st.last_run.files_downloaded, parsed: st.last_run.calls_parsed + st.last_run.manifests, tagged: st.last_run.calls_tagged, to_process: st.last_run.files_new + st.last_run.files_changed, skipped: st.last_run.files_skipped, bytes: st.last_run.bytes_downloaded, errors: st.last_run.errors })));
    v.append(h("div", { class: "grid cols-4 section" },
      stat("Last sync", st.last_sync ? BSP.fmtAgo(st.last_sync) : "never", st.next_run ? `next scheduled ${fmtDate(st.next_run)}` : "schedule off"),
      stat("Tagging throughput", st.throughput.tag_calls_per_min ? `${st.throughput.tag_calls_per_min}/min` : "–", "calls per minute, last 5 runs"),
      stat("Needs review", fmtN(c.tags.needs_review), `below the ${pct(thr)} confidence threshold`, "", "Open Quality & review to confirm or correct these"),
      stat("Churn-risk calls", fmtN(c.tags.churn), c.tags.tags ? `${pct(c.tags.churn / c.tags.tags)} of tagged calls` : null, "", "Cancellation intent, competitor mentions, churn-flagged reasons, or a negative unresolved ending")));
    if (!c.tags.tags) {
      v.append(h("div", { class: "section" }, card(null, emptyState("No tagged calls yet", "Run a sync to download the call logs from SharePoint and tag them.",
        h("button", { class: "btn primary", onclick: () => startRun("incremental") }, icon("refresh", 16), "Sync now")))));
    } else {
      v.append(h("div", { class: "grid cols-2 section" },
        card("Top contact reasons", barsH(ov.top_reasons.map((t) => ({ label: t.name, value: t.n, tip: `<b>${esc(t.name)}</b><br>${esc(t.l1)}<br>${t.n} calls · avg confidence ${pct(t.conf)} · ${t.churn} churn-risk`, onClick: () => (location.hash = `#calls?leaf=${t.leaf}`) }))), null, "Click a bar to see the calls"),
        card("Calls by domain (L1)", barsH(ov.l1.map((x) => ({ label: x.k, value: x.n, onClick: () => (location.hash = "#taxonomy") }))), null, "Share of tagged calls per taxonomy domain")));
      v.append(h("div", { class: "grid cols-2 section" },
        card("Calls per day", columns(ov.daily.map((d) => ({ label: d.day.slice(5), value: d.calls, tip: `<b>${d.day}</b><br>${d.calls} calls · ${d.churn} churn-risk · avg conf ${pct(d.conf)}` }))), null, "By call start date"),
        card("Outcomes", h("div", { class: "stack lg" },
          h("div", null, h("div", { class: "eyebrow", style: { marginBottom: "6px" } }, "Resolution"), barsH(ov.resolution.map((x) => ({ label: RES_LABEL[x.k] || x.k, value: x.n })), { width: 520, labelW: 120 })),
          h("div", null, h("div", { class: "eyebrow", style: { marginBottom: "6px" } }, "Customer sentiment trajectory"), barsH(ov.sentiment.map((x) => ({ label: (x.k || "").replace("stable-", "steady ").replace("-", " "), value: x.n })), { width: 520, labelW: 120 }))))));
    }
    v.append(h("div", { class: "section" }, card("How the pipeline works", h("div", { class: "how" },
      ...[["List", `Watches <b>${esc(st.source.folder)}</b> on ${esc(st.source.site)} (incl. date sub-folders); only new or changed files are fetched.`],
        ["Parse", "CCAI Insights JSON, text and WebVTT transcripts become one call record: turns, talk ratio, silence, sentiment. Daily CSV manifests fill gaps."],
        ["Tag", `${st.llm.available && st.llm.route !== "offline" ? "The LLM picks" : "The offline tagger picks"} a primary reason (plus up to 2 secondary) from the ${st.taxonomy.leaves}-leaf taxonomy v${esc(st.taxonomy.version)}, with a calibrated confidence.`],
        ["Review & export", "Low-confidence calls go to the review queue; corrections are kept as feedback. Export CSV / Excel / Parquet or publish back to SharePoint."]]
        .map(([t, d], i) => h("div", { class: "step" }, h("span", { class: "n" }, String(i + 1)), h("div", null, h("b", null, t), h("div", { class: "caption", html: d }))))))));
    main.replaceChildren(v);
  };

  // ── Runs ────────────────────────────────────────────────────────────
  pages.runs = async function () {
    const runs = await api("/api/runs");
    const v = h("div", { class: "fade-in" });
    v.append(pageHead("Runs", "Every sync of the SharePoint folder. Runs are incremental — unchanged files are skipped — and safe to repeat.",
      [h("button", { class: "btn primary", onclick: () => startRun("incremental") }, icon("refresh", 16), "Sync now"),
        h("button", { class: "btn secondary", "data-tip": "Re-download and re-parse every file, then tag anything new", onclick: async () => { if (await BSP.confirm({ title: "Full re-scan?", body: "Downloads and parses every file in the folder again. Existing tags are kept unless the call content changed.", okText: "Re-scan" })) startRun("full"); } }, "Full re-scan"),
        h("button", { class: "btn secondary", "data-tip": "Re-tag every call with the current taxonomy, settings and calibration", onclick: async () => { if (await BSP.confirm({ title: "Re-tag all calls?", body: "Every call gets a new tag with the current taxonomy, route and calibration. Previous tags are kept in the call history; reviewer overrides stay on the old tag.", okText: "Re-tag" })) startRun("retag"); } }, "Re-tag all")]));
    const t = BSP.table({
      rowKey: "run_id", pageSize: 25, onRowClick: (r) => runDrawer(r.run_id),
      empty: emptyState("No runs yet", "Start the first sync with Sync now."),
      columns: [
        { key: "run_id", label: "Run", sort: true, render: (r) => h("b", null, `#${r.run_id}`) },
        { key: "status", label: "Status", render: (r) => badge(STATUS_BADGE[r.status] || "neutral", r.status, r.status === "running") },
        { key: "trigger", label: "Trigger", render: (r) => `${r.trigger} · ${r.mode}` },
        { key: "started_at", label: "Started", sort: true, render: (r) => fmtDate(r.started_at) },
        { key: "duration_ms", label: "Duration", num: true, sort: true, render: (r) => fmtMs(r.duration_ms) },
        { key: "files_discovered", label: "Files", num: true, render: (r) => fmtN(r.files_discovered) },
        { key: "files_new", label: "New / changed", num: true, render: (r) => `${fmtN(r.files_new)} / ${fmtN(r.files_changed)}` },
        { key: "calls_parsed", label: "Parsed", num: true },
        { key: "calls_tagged", label: "Tagged", num: true },
        { key: "errors", label: "Errors", num: true, render: (r) => (r.errors ? h("span", { style: { color: "var(--bsp-text-error)", fontWeight: 600 } }, String(r.errors)) : "0") },
        { key: "bytes_downloaded", label: "Data", num: true, render: (r) => BSP.fmtBytes(r.bytes_downloaded || 0) },
      ], rows: runs,
    });
    v.append(card(null, t.el));
    main.replaceChildren(v);
  };
  async function runDrawer(id) {
    const d = await api(`/api/runs/${id}`);
    const r = d.run;
    const body = h("div", { class: "stack lg" },
      h("div", { class: "row wrap" }, badge(STATUS_BADGE[r.status] || "neutral", r.status, true), h("span", { class: "chip outline" }, `${r.trigger} · ${r.mode}`), h("span", { class: "chip outline" }, r.source_mode || ""), r.route ? h("span", { class: "chip outline" }, `route ${r.route}`) : null),
      r.message ? h("div", { class: `alert ${r.status === "succeeded" ? "success" : r.status === "failed" ? "error" : "info"}` }, icon("info", 18), h("div", null, r.message)) : null,
      h("dl", { class: "kv" },
        ...[["Folder", `${r.site} / ${r.folder}`], ["Started", fmtDate(r.started_at)], ["Finished", fmtDate(r.finished_at)], ["Duration", fmtMs(r.duration_ms)],
          ["Files discovered", fmtN(r.files_discovered)], ["New / changed / skipped", `${fmtN(r.files_new)} / ${fmtN(r.files_changed)} / ${fmtN(r.files_skipped)}`],
          ["Downloaded", `${fmtN(r.files_downloaded)} files · ${BSP.fmtBytes(r.bytes_downloaded || 0)}`], ["Manifests", fmtN(r.manifests)],
          ["Calls parsed / tagged", `${fmtN(r.calls_parsed)} / ${fmtN(r.calls_tagged)}`], ["Errors", fmtN(r.errors)],
          ["Time: list · download · parse · tag", `${fmtMs(r.list_ms)} · ${fmtMs(r.download_ms)} · ${fmtMs(r.parse_ms)} · ${fmtMs(r.tag_ms)}`], ["Taxonomy", r.taxonomy_version]]
          .flatMap(([k, val]) => [h("dt", null, k), h("dd", null, val)])),
      d.tags.length ? h("div", null, h("h4", null, "Tags written"), h("div", { class: "caption" }, d.tags.map((t) => `${t.route}: ${t.n} calls, avg confidence ${pct(t.conf)}, ${t.low} low`).join(" · "))) : null,
      d.errors.length ? h("div", null, h("h4", { style: { marginBottom: "8px" } }, `Failed files (${d.errors.length})`),
        ...d.errors.map((f) => h("div", { class: "alert error", style: { marginBottom: "6px" } }, icon("error", 18), h("div", null, h("div", { class: "title mono-s" }, f.path), h("div", { class: "caption" }, f.error))))) : null,
      h("div", null, h("h4", { style: { marginBottom: "8px" } }, `Files processed (${d.files.length})`),
        BSP.table({ pageSize: 15, rows: d.files, columns: [
          { key: "path", label: "File", render: (f) => h("span", { class: "mono-s" }, f.path) },
          { key: "format", label: "Format" },
          { key: "status", label: "Status", render: (f) => badge(f.status === "error" ? "error" : f.status === "parsed" ? "ok" : "neutral", f.status) },
          { key: "call_id", label: "Call", render: (f) => (f.call_id ? h("a", { href: "#", onclick: (e) => { e.preventDefault(); dr.close(); callDrawer(f.call_id); } }, f.call_id) : "") }] }).el));
    const dr = BSP.drawer({ title: `Run #${r.run_id}`, wide: true, body,
      foot: [r.errors ? h("button", { class: "btn primary", onclick: async () => { try { const x = await api(`/api/runs/${r.run_id}/retry`, { body: {} }); toast(`Retry run #${x.run_id} started`, "success"); dr.close(); S.wasActive = true; } catch (e) { toast(e.message, "error"); } } }, icon("refresh", 16), "Re-run failed") : null,
        h("button", { class: "btn secondary", onclick: () => { dr.close(); location.hash = `#calls?run_id=${r.run_id}`; } }, "View calls"),
        h("button", { class: "btn ghost", onclick: () => dr.close() }, "Close")] });
  }

  // ── Calls ───────────────────────────────────────────────────────────
  const callFilters = { q: "", l1: "", band: "", trajectory: "", resolution: "", churn: "", reviewed: "", leaf: "", run_id: "", sort: "started_at" };
  pages.calls = async function (params) {
    Object.assign(callFilters, { leaf: "", run_id: "" }, params || {});
    const v = h("div", { class: "fade-in" });
    v.append(pageHead("Calls", "Search every ingested call. Click a row for the transcript with the evidence highlighted, the tags and the review controls.",
      [h("button", { class: "btn secondary", onclick: () => (location.hash = "#downloads") }, icon("download", 16), "Export")]));
    const sel = (key, label, opts) => h("select", { class: "select", "aria-label": label, onchange: (e) => { callFilters[key] = e.target.value; load(); } },
      h("option", { value: "" }, label), ...opts.map(([v2, l]) => h("option", { value: v2, selected: callFilters[key] === v2 || null }, l)));
    const l1s = [...new Map(S.leaves.map((l) => [l.l1, l.l1_name])).entries()];
    const chips = h("div", { class: "row wrap" });
    const bar = h("div", { class: "filters" },
      h("div", { class: "search" }, icon("search", 16), h("input", { class: "input", placeholder: "Search transcript, summary, call id, BAN, agent…", value: callFilters.q, oninput: BSP.debounce((e) => { callFilters.q = e.target.value; load(); }, 300) })),
      sel("l1", "All domains", l1s),
      sel("band", "Any confidence", [["high", "High"], ["medium", "Medium"], ["low", "Low (needs review)"]]),
      sel("trajectory", "Any sentiment", [["improving", "Improving"], ["declining", "Declining"], ["stable-negative", "Steady negative"], ["stable-neutral", "Steady neutral"], ["stable-positive", "Steady positive"]]),
      sel("resolution", "Any outcome", Object.entries(RES_LABEL)),
      sel("churn", "Churn: any", [["true", "Churn risk only"], ["false", "No churn risk"]]),
      sel("reviewed", "Review: any", [["false", "Not reviewed"], ["true", "Reviewed"]]),
      sel("sort", "Sort: newest", [["confidence", "Sort: confidence"], ["duration", "Sort: duration"], ["sentiment", "Sort: sentiment"]]));
    const countEl = h("span", { class: "caption" });
    const t = BSP.table({
      rowKey: "call_id", pageSize: 50, onRowClick: (r) => callDrawer(r.call_id),
      empty: emptyState("No calls match", "Clear a filter or run a sync."),
      columns: [
        { key: "started_at", label: "Call start", render: (r) => h("span", { class: "nowrap" }, fmtDate(r.started_at)) },
        { key: "call_id", label: "Call / BAN", render: (r) => h("div", { class: "leafcell" }, h("span", { class: "mono-s" }, r.call_id), h("span", { class: "l1" }, `${r.ban || "no BAN"} · ${r.queue || ""}`)) },
        { key: "leaf", label: "Primary contact reason", render: leafCell },
        { key: "secondary", label: "Also", render: (r) => (r.secondary && r.secondary.length ? h("span", { class: "chip outline", "data-tip": r.secondary.map((s) => esc(leafName(s.leaf))).join("<br>") }, `+${r.secondary.length}`) : "") },
        { key: "confidence", label: "Confidence", render: (r) => confCell(r.confidence, r.confidence_band) },
        { key: "sentiment_trajectory", label: "Sentiment", render: (r) => sentCell(r.sentiment_trajectory) },
        { key: "resolution_status", label: "Outcome", render: (r) => (r.resolution_status ? badge(RES_BADGE[r.resolution_status], RES_LABEL[r.resolution_status]) : "") },
        { key: "churn_risk", label: "Churn", render: (r) => (r.churn_risk ? badge("warning", "risk") : "") },
        { key: "duration_sec", label: "Length", num: true, render: (r) => fmtDur(r.duration_sec) },
      ], rows: [],
    });
    v.append(bar, chips, card(null, h("div", null, h("div", { class: "row between", style: { marginBottom: "8px" } }, countEl, h("span", { class: "caption" }, "Up to 1,000 rows shown — use Downloads for full exports")), t.el)));
    main.replaceChildren(v);
    async function load() {
      chips.replaceChildren(...["leaf", "run_id"].filter((k) => callFilters[k]).map((k) => h("span", { class: "chip primary", style: { marginBottom: "10px" } },
        k === "leaf" ? `Reason: ${leafName(callFilters.leaf)}` : `Run #${callFilters.run_id}`,
        h("button", { "aria-label": "Remove filter", onclick: () => { callFilters[k] = ""; history.replaceState(null, "", "#calls"); load(); } }, icon("close", 12)))));
      const f = { ...callFilters, limit: 1000, desc: true };
      if (f.sort === "confidence") f.desc = false;
      const r = await api(`/api/calls${qs(f)}`);
      countEl.textContent = `${fmtN(r.total)} call${r.total === 1 ? "" : "s"}`;
      t.update(r.rows);
    }
    await load();
  };

  async function callDrawer(id) {
    const d = await api(`/api/calls/${encodeURIComponent(id)}`);
    const c = d.call, tg = d.tag;
    const ev = new Set(tg ? tg.evidence_turns || [] : []);
    const secEv = new Map(); (tg ? tg.secondary : []).forEach((s) => (s.evidence || []).forEach((i) => secEv.set(i, s.name)));
    const turns = h("div", { class: "transcript" }, (c.turns || []).map((t) => h("div", { class: `turn ${t.role} ${ev.has(t.id) || secEv.has(t.id) ? "evidence" : ""}` },
      h("div", { class: "who" }, t.role === "agent" ? "Agent" : t.role === "customer" ? "Customer" : "System", h("br"), t.start != null ? fmtDur(t.start) : ""),
      h("div", { class: "bubble" }, t.text,
        ev.has(t.id) ? h("span", { class: "ev" }, icon("catalog", 11), "evidence") : secEv.has(t.id) ? h("span", { class: "ev" }, icon("catalog", 11), secEv.get(t.id)) : null,
        t.role === "customer" && t.sentiment != null ? h("span", { class: "s", "data-tip": t.sentiment_source === "lexicon" ? "Sentiment estimated from the words (no CCAI score in this format)" : "CCAI per-turn sentiment" }, `sentiment ${Number(t.sentiment).toFixed(2)}`) : null))));
    const tagBox = tg ? h("div", { class: "stack" },
      h("div", { class: "tagcard primary" }, h("div", { class: "row between" }, h("div", null, h("div", { class: "path" }, `${tg.primary.l1} › ${tg.primary.l2} · ${tg.primary_leaf}`), h("div", { class: "name" }, tg.primary.name)), confCell(tg.confidence, tg.confidence_band)),
        h("div", { class: "caption", style: { marginTop: "6px" } }, tg.rationale || ""),
        tg.override ? h("div", { class: "alert info", style: { marginTop: "8px" } }, icon("edit", 18), h("div", null, h("div", { class: "title" }, `Reviewer override: ${tg.override.name}`), h("div", { class: "caption" }, `${tg.reviewed_by} · ${fmtDate(tg.reviewed_at)}${tg.review_note ? " · " + tg.review_note : ""}`)))
          : tg.reviewed_at ? h("div", { class: "caption confirmed", style: { marginTop: "6px" } }, icon("tick-circle", 13), `Confirmed by ${tg.reviewed_by} · ${fmtDate(tg.reviewed_at)}`) : null),
      ...(tg.secondary || []).map((s) => h("div", { class: "tagcard" }, h("div", { class: "path" }, `Secondary · ${s.l1} › ${s.l2}`), h("div", { style: { fontWeight: 600 } }, s.name))),
      h("div", { class: "row wrap" }, badge(RES_BADGE[tg.resolution_status] || "neutral", RES_LABEL[tg.resolution_status] || tg.resolution_status),
        tg.churn_risk ? h("span", { class: "badge warning", "data-tip": (tg.churn_reasons || []).map(esc).join("<br>") }, icon("warning", 12), "Churn risk") : null,
        h("span", { class: "chip outline" }, sentCell(tg.sentiment_trajectory)), h("span", { class: "chip outline", "data-tip": `Model ${esc(tg.model)} · taxonomy ${esc(tg.taxonomy_version)}` }, `route: ${tg.route}`)),
      h("div", null, h("div", { class: "eyebrow" }, "Summary"), h("div", null, tg.summary || "")),
      tg.sentiment_series && tg.sentiment_series.length > 1 ? h("div", null, h("div", { class: "eyebrow" }, "Customer sentiment by turn"), sparkline(tg.sentiment_series, { width: 300, height: 44 })) : null)
      : h("div", { class: "alert warning" }, icon("clock", 18), h("div", null, "This call has not been tagged yet."));
    const meta = h("dl", { class: "kv" }, ...[["Call start", fmtDate(c.started_at)], ["BAN", c.ban || "not detected"], ["Agent · queue", `${c.agent_id || "–"} · ${c.queue || "–"}`],
      ["Caller", c.ani_masked || "–"], ["Duration", fmtDur(c.duration_sec)], ["Talk ratio", c.agent_talk_pct != null ? `agent ${c.agent_talk_pct}% · customer ${(100 - c.agent_talk_pct).toFixed(1)}%` : "–"],
      ["Silence", c.silence_pct != null ? `${fmtDur(c.silence_sec)} (${c.silence_pct}%)` : "–"], ["Source", `${c.source_path} (${c.format})`]].flatMap(([k, val]) => [h("dt", null, k), h("dd", null, val)]));
    const review = tg ? reviewBox(c.call_id, tg, () => { dr.close(); callDrawer(id); if (S.page === "quality") pages.quality(); }) : null;
    const tabs = h("div", { class: "tabs" });
    const pane = h("div");
    const views = { Tags: () => h("div", { class: "stack lg" }, tagBox, review), Transcript: () => turns, Details: () => h("div", { class: "stack lg" }, meta,
      tg ? h("div", null, h("div", { class: "eyebrow" }, "Candidates considered"), h("div", { class: "caption" }, (tg.candidates || []).slice(0, 8).map((x) => `${x.name} (${x.score != null ? x.score.toFixed(2) : "–"})`).join(" · "))) : null,
      tg ? h("div", null, h("div", { class: "eyebrow" }, "Confidence signals"), h("pre", { class: "codeblock" }, JSON.stringify(tg.signals, null, 2))) : null,
      h("div", null, h("div", { class: "eyebrow" }, "Tag history"), h("div", { class: "caption" }, d.history.map((x) => `#${x.tag_id} ${fmtDate(x.created_at)} · ${leafName(x.primary_leaf)} · ${pct(x.confidence)} · ${x.route}${x.override_leaf ? " → " + leafName(x.override_leaf) : ""}`).join("\n")))) };
    const show = (name) => { [...tabs.children].forEach((b) => b.classList.toggle("active", b.textContent === name)); pane.replaceChildren(views[name]()); };
    Object.keys(views).forEach((n) => tabs.append(h("button", { onclick: () => show(n) }, n)));
    const dr = BSP.drawer({ title: `Call ${c.call_id}`, wide: true, body: h("div", null, h("div", { class: "caption", style: { marginBottom: "10px" } }, `${fmtDate(c.started_at)} · ${c.ban || ""} · ${fmtDur(c.duration_sec)}`), tabs, pane) });
    show("Tags");
  }

  function leafPicker(onPick, initial) {
    const list = h("div", { class: "pickgrid" });
    let chosen = initial || null;
    const render = (q) => {
      q = (q || "").toLowerCase();
      list.replaceChildren(...S.leaves.filter((l) => !q || `${l.id} ${l.name} ${l.l1_name} ${l.l2_name} ${l.keywords.join(" ")}`.toLowerCase().includes(q)).slice(0, 120)
        .map((l) => h("div", { class: `opt ${chosen === l.id ? "sel" : ""}`, onclick: () => { chosen = l.id; onPick(l.id); render(input.value); } }, h("b", null, l.name), h("span", { class: "caption" }, `${l.l1_name} › ${l.l2_name} · ${l.id}`))));
    };
    const input = h("input", { class: "input", placeholder: "Search 104 contact reasons…", oninput: (e) => render(e.target.value) });
    render("");
    return h("div", { class: "stack" }, input, list);
  }
  function reviewBox(callId, tg, done) {
    let pick = null;
    const note = h("input", { class: "input", placeholder: "Note (optional)" });
    const submit = async (override) => {
      try { await api(`/api/calls/${encodeURIComponent(callId)}/review`, { body: { override_leaf: override, note: note.value } }); toast(override ? "Override saved as feedback" : "Tag confirmed", "success"); loadStatus(); done(); }
      catch (e) { toast(e.message, "error"); }
    };
    const cand = (tg.candidates || []).filter((x) => x.leaf !== tg.primary_leaf).slice(0, 4);
    return h("div", { class: "card flat tight" }, h("h4", null, "Review"),
      h("div", { class: "caption", style: { margin: "4px 0 10px" } }, "Confirm the tag or pick the correct reason. Reviews are stored with your name and measure tagger accuracy (Quality page)."),
      h("div", { class: "row wrap", style: { marginBottom: "10px" } }, h("button", { class: "btn primary sm", onclick: () => submit(null) }, icon("check", 14), "Confirm tag"),
        ...cand.map((x) => h("button", { class: "btn secondary sm", "data-tip": "Override with this candidate", onclick: () => submit(x.leaf) }, x.name || leafName(x.leaf)))),
      h("details", null, h("summary", { class: "caption", style: { cursor: "pointer" } }, "Choose any other reason…"),
        h("div", { style: { marginTop: "8px" } }, leafPicker((id) => (pick = id)), h("div", { class: "row", style: { marginTop: "8px" } }, note,
          h("button", { class: "btn secondary sm", onclick: () => (pick ? submit(pick) : toast("Pick a reason first", "error")) }, "Save override")))));
  }

  // ── Taxonomy ────────────────────────────────────────────────────────
  pages.taxonomy = async function () {
    const t = await api("/api/taxonomy");
    const v = h("div", { class: "fade-in" });
    v.append(pageHead("Taxonomy explorer", `Contact-reason taxonomy v${t.version} — ${t.tree.length} domains, ${t.leaves} reasons. Area = call volume, colour = average confidence. Click a block or row for the definition and the calls.`));
    if (t.validation.length) v.append(h("div", { class: "alert warning", style: { marginBottom: "12px" } }, icon("warning", 18), h("div", null, h("div", { class: "title" }, "Taxonomy validation"), t.validation.slice(0, 5).join(" · "))));
    let level = "l1", focus = null;
    const seg = h("div", { class: "segmented" });
    const tm = h("div", { class: "treemap" });
    const crumbs = h("div", { class: "row wrap caption", style: { minHeight: "24px" } });
    const detail = h("div", { class: "leaf-detail" }, h("div", { class: "muted" }, "Select a reason to see its definition, boundaries, examples and mappings."));
    const nodesFor = () => {
      let src = t.tree;
      if (focus) src = t.tree.filter((d) => d.id === focus.split(".")[0]);
      if (level === "l1" && !focus) return src.map((d) => ({ id: d.id, label: d.name, value: d.n, conf: d.avg_confidence, churn: d.churn, kind: "l1" }));
      if (level === "l2" || (focus && focus.split(".").length === 1)) return src.flatMap((d) => d.groups.map((g) => ({ id: g.id, label: g.name, sub: d.name, value: g.n, conf: g.avg_confidence, churn: g.churn, kind: "l2" })));
      let groups = src.flatMap((d) => d.groups.map((g) => ({ ...g, d })));
      if (focus && focus.split(".").length === 2) groups = groups.filter((g) => g.id === focus);
      return groups.flatMap((g) => g.leaves.map((l) => ({ id: l.id, label: l.name, sub: `${g.d.name} › ${g.name}`, value: l.n, conf: l.avg_confidence, churn: l.churn, kind: "l3" })));
    };
    function drawMap() {
      const W = tm.clientWidth || 900, H = 440;
      const items = nodesFor().filter((n) => n.value > 0).sort((a, b) => b.value - a.value);
      const total = items.reduce((a, b) => a + b.value, 0);
      if (!items.length) { tm.replaceChildren(emptyState("No tagged calls yet", "Volumes appear here after the first run.")); return; }
      tm.replaceChildren(...squarify(items, 0, 0, W, H).map((c) => {
        const bg = rampColor(c.conf), dark = ["#2f8f99", "#0b6e79", "#06454c"].includes(bg);
        const big = c.w > 70 && c.h > 34;
        return h("div", { class: `cell ${dark ? "dark" : ""}`, style: { left: `${c.x}px`, top: `${c.y}px`, width: `${c.w}px`, height: `${c.h}px`, background: bg },
          "data-tip": `<b>${esc(c.label)}</b>${c.sub ? `<br>${esc(c.sub)}` : ""}<br>${c.value} calls (${pct(c.value / total, 1)}) · avg confidence ${pct(c.conf)}<br>${c.churn} churn-risk`,
          onclick: () => select(c.id, c.kind) },
        big ? h("div", { class: "t" }, c.label) : null, big ? h("div", { class: "n" }, `${c.value} · ${pct(c.conf)}`) : null);
      }));
    }
    function drawCrumbs() {
      const parts = [h("a", { href: "#", onclick: (e) => { e.preventDefault(); focus = null; level = "l1"; syncSeg(); drawMap(); drawCrumbs(); } }, "All domains")];
      if (focus) {
        const [a, b] = focus.split(".");
        const d = t.tree.find((x) => x.id === a);
        parts.push(h("span", { class: "crumb-sep" }, icon("chevron-right", 11)), h("a", { href: "#", onclick: (e) => { e.preventDefault(); focus = a; level = "l2"; drawMap(); drawCrumbs(); } }, d.name));
        if (b) parts.push(h("span", { class: "crumb-sep" }, icon("chevron-right", 11)), d.groups.find((g) => g.id === focus).name);
      }
      crumbs.replaceChildren(...parts);
    }
    function syncSeg() { [...seg.children].forEach((b) => b.classList.toggle("active", b.dataset.l === level)); }
    [["l1", "Domains"], ["l2", "Intent groups"], ["l3", "Reasons"]].forEach(([l, label]) => seg.append(h("button", { "data-l": l, onclick: () => { level = l; focus = null; syncSeg(); drawMap(); drawCrumbs(); } }, label)));
    syncSeg();
    function select(id, kind) {
      if (kind === "l1") { focus = id; level = "l2"; drawMap(); drawCrumbs(); }
      else if (kind === "l2") { focus = id; level = "l3"; drawMap(); drawCrumbs(); }
      else showLeaf(id);
      tree.querySelectorAll(".node").forEach((n) => n.classList.toggle("sel", n.dataset.id === id));
    }
    function showLeaf(id) {
      const l = S.byId[id]; if (!l) return;
      const node = t.tree.flatMap((d) => d.groups.flatMap((g) => g.leaves)).find((x) => x.id === id) || {};
      detail.replaceChildren(h("div", { class: "stack" },
        h("div", null, h("div", { class: "caption" }, `${l.l1_name} › ${l.l2_name}`), h("h3", null, l.name), h("div", { class: "mono-s muted" }, l.id)),
        h("div", { class: "row wrap" }, h("span", { class: "chip primary" }, `${node.n || 0} calls`), node.secondary_n ? h("span", { class: "chip outline" }, `+${node.secondary_n} as secondary`) : null,
          h("span", { class: "chip outline" }, `avg confidence ${pct(node.avg_confidence)}`), l.churn_risk ? badge("warning", "churn-risk reason") : null),
        h("div", null, l.definition),
        h("div", null, h("div", { class: "eyebrow" }, "Include"), h("div", { class: "caption" }, l.include)),
        h("div", null, h("div", { class: "eyebrow" }, "Exclude"), h("div", { class: "caption" }, l.exclude)),
        h("div", null, h("div", { class: "eyebrow" }, "Example utterances"), h("ul", null, l.examples.map((x) => h("li", null, `“${x}”`)))),
        h("div", null, h("div", { class: "eyebrow" }, "Keywords"), h("div", { class: "row wrap" }, l.keywords.map((k) => h("span", { class: "chip" }, k)))),
        h("div", null, h("div", { class: "eyebrow" }, "Mappings"), h("div", { class: "caption" }, `Service ticket: ${l.mapping.ticket_category}/${l.mapping.ticket_subcategory} · SAP reason: ${l.mapping.sap_reason}`)),
        h("button", { class: "btn secondary sm", onclick: () => (location.hash = `#calls?leaf=${l.id}`) }, icon("chat", 14), "View calls")));
    }
    const tree = h("div", { class: "tree" }, h("div", { class: "node head" }, h("span", null, "Domain › group › reason"), h("span", { class: "num" }, "Calls"), h("span", { class: "num" }, "Share"), h("span", null, "Volume"), h("span", { class: "num" }, "Avg conf")));
    const maxLeaf = Math.max(1, ...t.tree.map((d) => d.n));
    t.tree.forEach((d) => {
      const kids = [];
      const row = (n, cls, label, kind) => h("div", { class: `node ${cls}`, "data-id": n.id, onclick: () => select(n.id, kind) },
        h("span", null, label), h("span", { class: "num" }, fmtN(n.n)), h("span", { class: "num" }, t.total ? pct(n.n / t.total, 1) : "–"),
        h("span", null, h("div", { class: "bar-in", style: { width: `${Math.max(2, (100 * n.n) / maxLeaf)}%` } })), h("span", { class: "num" }, pct(n.avg_confidence)));
      const tw = h("span", { class: "tw" }, icon("chevron-right", 14));
      const l1row = row(d, "l1", h("span", null, tw, d.name), "l1");
      tw.onclick = (e) => { e.stopPropagation(); const open = kids[0] && kids[0].hidden; kids.forEach((k) => (k.hidden = !open)); tw.replaceChildren(icon(open ? "chevron-down" : "chevron-right", 14)); };
      tree.append(l1row);
      d.groups.forEach((g) => { const gr = row(g, "l2", g.name, "l2"); gr.hidden = true; kids.push(gr); tree.append(gr);
        g.leaves.forEach((l) => { const lr = row(l, "l3", h("span", null, l.name, l.churn_risk ? h("span", { class: "muted churn-flag", "data-tip": "Churn-risk reason", "aria-label": "Churn-risk reason" }, icon("alert", 12)) : null), "l3"); lr.hidden = true; kids.push(lr); tree.append(lr); }); });
    });
    v.append(card(null, h("div", { class: "stack" }, h("div", { class: "row between wrap" }, seg, h("div", { class: "legend-ramp" }, "avg confidence 30%", h("span", { class: "ramp" }), "95%")), crumbs, tm)));
    v.append(h("div", { class: "grid tax-split section" }, card("All reasons", tree, null, "Expand a domain with the arrow; click a row to drill in"), card("Reason details", detail)));
    main.replaceChildren(v);
    drawCrumbs(); requestAnimationFrame(drawMap);
    window.onresize = BSP.debounce(() => { if (S.page === "taxonomy") drawMap(); }, 200);
  };

  // ── Quality ─────────────────────────────────────────────────────────
  pages.quality = async function () {
    const [qd, queue] = await Promise.all([api("/api/quality"), api("/api/quality/queue")]);
    const s = qd.summary;
    const v = h("div", { class: "fade-in" });
    v.append(pageHead("Quality & review", "How sure the tagger is, and how often reviewers agree. Calls below the confidence threshold queue here for a quick confirm-or-correct.",
      [h("button", { class: "btn secondary", "data-tip": "Refit the confidence calibration from reviewer feedback", onclick: async (e) => { const r = await BSP.busy(e.currentTarget, () => api("/api/quality/recalibrate", { body: {} })); toast(r.message, r.calibration.fitted_on ? "success" : "info"); } }, icon("wand", 16), "Recalibrate from reviews")]));
    v.append(h("div", { class: "grid cols-4" },
      stat("Review queue", fmtN(s.queue), `confidence below ${pct(qd.threshold)}`),
      stat("Reviewed calls", fmtN(s.reviewed), s.total ? `${pct(s.reviewed / s.total, 1)} of tagged calls` : null),
      stat("Reviewer-measured accuracy", s.accuracy == null ? "–" : pct(s.accuracy, 1), s.reviewed ? `${fmtN(s.confirmed)} confirmed · ${fmtN(s.overridden)} corrected` : "review some calls to measure", "", "Share of reviewed calls whose tag was confirmed (not overridden)"),
      stat("Routes", qd.routes.map((r) => `${r.route}: ${r.n}`).join(" · ") || "–", "how calls were tagged")));
    const hist = qd.histogram.map((b) => ({ label: `${Math.round(b.lo * 100)}`, value: b.n, alt: b.hi <= qd.threshold, tip: `<b>${Math.round(b.lo * 100)}–${Math.round(b.hi * 100)}% confidence</b><br>${b.n} calls${b.reviewed ? `<br>${b.reviewed} reviewed · ${pct(b.correct / b.reviewed)} confirmed` : ""}` }));
    const rel = qd.histogram.filter((b) => b.reviewed).map((b) => ({ label: `${Math.round(b.lo * 100)}–${Math.round(b.hi * 100)}%`, value: Math.round((100 * b.correct) / b.reviewed), tip: `<b>${Math.round(b.lo * 100)}–${Math.round(b.hi * 100)}%</b><br>${b.correct}/${b.reviewed} confirmed by reviewers` }));
    v.append(h("div", { class: "grid cols-2 section" },
      card("Confidence distribution", columns(hist, { threshold: qd.threshold }), null, "Calls per 10-point confidence bucket; light bars are below the review threshold"),
      card("Does confidence mean what it says?", rel.length ? columns(rel, { fmt: (x) => `${x}%` }) : emptyState("No reviews yet", "Confirm or correct calls from the queue below — accuracy per confidence bucket appears here."), null, "Reviewer-confirmed share per confidence bucket (should rise left → right)")));
    if (qd.confusions.length || qd.by_l1.length) v.append(h("div", { class: "grid cols-2 section" },
      card("Most common corrections", qd.confusions.length ? h("div", { class: "stack" }, qd.confusions.map((c) => h("div", { class: "row between" }, h("span", null, `${leafName(c.primary_leaf)} → `, h("b", null, leafName(c.override_leaf))), h("span", { class: "chip" }, String(c.n))))) : h("div", { class: "muted" }, "No overrides yet.")),
      card("Accuracy by domain", qd.by_l1.length ? barsH(qd.by_l1.map((a) => ({ label: a.l1, value: Math.round((100 * a.correct) / a.reviewed), tip: `<b>${esc(a.l1)}</b><br>${a.correct}/${a.reviewed} confirmed` })), { fmt: (x) => `${x}%` }) : h("div", { class: "muted" }, "No reviews yet."))));
    const t = BSP.table({
      rowKey: "call_id", pageSize: 20, onRowClick: (r) => callDrawer(r.call_id),
      empty: emptyState("Queue is empty", "Every call is above the confidence threshold or already reviewed."),
      columns: [
        { key: "confidence", label: "Confidence", sort: true, render: (r) => confCell(r.confidence, "low") },
        { key: "leaf_name", label: "Tagged as", render: (r) => h("div", { class: "leafcell" }, h("span", { class: "l1" }, (S.byId[r.primary_leaf] || {}).l1_name || ""), h("span", null, r.leaf_name)) },
        { key: "summary", label: "Summary", render: (r) => h("span", { class: "caption", style: { display: "block", maxWidth: "460px" } }, (r.summary || "").slice(0, 180)) },
        { key: "actions", label: "", render: (r) => h("div", { class: "row" },
          h("button", { class: "btn ghost sm", "data-tip": "Confirm this tag", onclick: async () => { await api(`/api/calls/${r.call_id}/review`, { body: { override_leaf: null } }); toast("Confirmed", "success"); pages.quality(); loadStatus(); } }, icon("check", 14)),
          h("button", { class: "btn secondary sm", onclick: () => callDrawer(r.call_id) }, "Review")) },
      ], rows: queue.rows, initialSort: { key: "confidence", dir: 1 },
    });
    v.append(h("div", { class: "section" }, card(`Low-confidence review queue (${queue.rows.length})`, t.el, null, "Click a row for the transcript and the full list of reasons")));
    main.replaceChildren(v);
  };

  // ── Downloads ───────────────────────────────────────────────────────
  pages.downloads = async function () {
    const [runs, hist] = await Promise.all([api("/api/runs?limit=50"), api("/api/exports/history")]);
    const st = S.status;
    const f = { format: "xlsx", date_from: "", date_to: "", run_id: "", min_conf: "" };
    const v = h("div", { class: "fade-in" });
    v.append(pageHead("Downloads", "Export the current tags (reviewer overrides applied) for a date range or a single run, or publish the file back to SharePoint for the CCAI team."));
    const seg = h("div", { class: "segmented" }, ...[["csv", "CSV"], ["xlsx", "Excel"], ["parquet", "Parquet"]].map(([k, l]) =>
      h("button", { class: k === f.format ? "active" : "", onclick: (e) => { f.format = k; [...seg.children].forEach((b) => b.classList.toggle("active", b === e.currentTarget)); } }, l)));
    const preview = h("div", { class: "caption" }, "…");
    const sample = h("div");
    const refresh = BSP.debounce(async () => {
      const p = await api(`/api/exports/preview${qs({ date_from: f.date_from, date_to: f.date_to, run_id: f.run_id, min_conf: f.min_conf })}`);
      preview.textContent = `${fmtN(p.rows)} calls match`;
      sample.replaceChildren(BSP.table({ pageSize: 8, rows: p.sample, columns: ["call_id", "started_at", "final_name", "confidence", "resolution_status", "churn_risk"].map((k) => ({ key: k, label: k, render: (r) => (k === "confidence" ? pct(r[k]) : k === "started_at" ? fmtDate(r[k]) : String(r[k] ?? "")) })) }).el);
      dict.replaceChildren(...p.columns.flatMap((c) => [h("dt", { class: "mono-s" }, c.name), h("dd", null, c.meaning)]));
    }, 200);
    const dict = h("dl", { class: "kv" });
    const field = (label, el, hint) => h("div", { class: "field" }, h("label", null, label), el, hint ? h("div", { class: "hint" }, hint) : null);
    const inp = (key, type, ph) => h("input", { class: "input", type, placeholder: ph || "", oninput: (e) => { f[key] = e.target.value; refresh(); } });
    const runSel = h("select", { class: "select", onchange: (e) => { f.run_id = e.target.value; refresh(); } }, h("option", { value: "" }, "All runs"), ...runs.map((r) => h("option", { value: r.run_id }, `#${r.run_id} · ${fmtDate(r.started_at)} · ${r.calls_tagged} tagged`)));
    const doDownload = async (btn) => {
      await BSP.busy(btn, async () => {
        const res = await api(`/api/exports${qs({ format: f.format, date_from: f.date_from, date_to: f.date_to, run_id: f.run_id, min_conf: f.min_conf })}`, { raw: true });
        const blob = await res.blob();
        const name = (res.headers.get("content-disposition") || "").match(/filename="([^"]+)"/);
        const a = h("a", { href: URL.createObjectURL(blob), download: name ? name[1] : `ccai_tags.${f.format}` }); document.body.append(a); a.click(); a.remove();
        toast(`Downloaded ${res.headers.get("x-row-count")} rows`, "success");
        pages.downloads();
      });
    };
    const doPublish = async (btn) => {
      const ok = await BSP.confirm({ title: "Publish to SharePoint?", body: `Uploads the ${f.format.toUpperCase()} export to ${st.source.site} / ${st.source.publish_folder} (${st.source.mode} mode). Anyone with access to that folder can open it.`, okText: "Publish" });
      if (!ok) return;
      await BSP.busy(btn, async () => {
        try { const r = await api("/api/exports/publish", { body: { format: f.format, date_from: f.date_from || null, date_to: f.date_to || null, run_id: f.run_id ? Number(f.run_id) : null, min_conf: f.min_conf ? Number(f.min_conf) : null } });
          toast(`Published ${r.name} (${r.rows} rows)`, "success"); pages.downloads(); }
        catch (e) { toast(e.message, "error"); }
      });
    };
    v.append(h("div", { class: "grid cols-2" },
      card("Export tags", h("div", null,
        field("Format", seg, "Excel adds a volume-by-reason sheet and a data dictionary; Parquet keeps types for analytics."),
        h("div", { class: "grid cols-2" }, field("Calls from", inp("date_from", "date")), field("Calls to", inp("date_to", "date"))),
        field("Run", runSel, "Only calls ingested or tagged in this run"),
        field("Minimum confidence", inp("min_conf", "number", "e.g. 0.6"), "Leave empty for all calls"),
        h("div", { class: "row between", style: { marginTop: "6px" } }, preview, h("div", { class: "row" },
          h("button", { class: "btn secondary", onclick: (e) => doPublish(e.currentTarget) }, icon("cloud", 16), "Publish to SharePoint"),
          h("button", { class: "btn primary", onclick: (e) => doDownload(e.currentTarget) }, icon("download", 16), "Download"))))),
      card("Recent exports", hist.length ? BSP.table({ pageSize: 10, rows: hist, columns: [
        { key: "at", label: "When", render: (r) => fmtDate(r.at) }, { key: "action", label: "Action", render: (r) => badge(r.action === "publish" ? "info" : "neutral", r.action) },
        { key: "name", label: "File", render: (r) => h("span", { class: "mono-s", "data-tip": r.web_url ? esc(r.web_url) : null }, r.name) }, { key: "rows", label: "Rows", num: true }] }).el
        : h("div", { class: "muted" }, "Nothing exported yet."), null, `Published files go to ${st.source.publish_folder}`)));
    v.append(h("div", { class: "grid cols-2 section" }, card("Preview", sample), card("Columns", dict)));
    main.replaceChildren(v);
    refresh();
  };

  // ── Settings ────────────────────────────────────────────────────────
  pages.settings = async function () {
    const d = await api("/api/settings");
    const st = { ...d.settings };
    const v = h("div", { class: "fade-in" });
    v.append(pageHead("Settings", "How often to sync, when a tag counts as confident, and which model route to use. Saved settings apply to the next run."));
    const field = (label, el, hint) => h("div", { class: "field" }, h("label", null, label), el, hint ? h("div", { class: "hint" }, hint) : null);
    const sw = (key, label) => h("label", { class: "switch" }, h("input", { type: "checkbox", checked: st[key] || null, onchange: (e) => (st[key] = e.target.checked) }), h("span", { class: "track" }), label);
    const thrVal = h("b", null, pct(st.confidence_threshold));
    const routeSel = h("select", { class: "select", onchange: (e) => (st.route = e.target.value) }, ...[["auto", "Auto — LLM when the gateway is up, else offline"], ["llm", "LLM preferred (falls back to offline per call on errors)"], ["offline", "Offline only (deterministic, no gateway)"]].map(([k, l]) => h("option", { value: k, selected: st.route === k || null }, l)));
    const save = async (btn) => BSP.busy(btn, async () => { try { await api("/api/settings", { method: "PUT", body: st }); toast("Settings saved", "success"); loadStatus(); } catch (e) { toast(e.message, "error"); } });
    v.append(h("div", { class: "grid cols-2" },
      card("Schedule", h("div", null,
        field("Automatic sync", sw("schedule_enabled", "Check the SharePoint folder on a schedule")),
        field("Every (minutes)", h("input", { class: "input", type: "number", min: 5, max: 10080, value: st.schedule_minutes, oninput: (e) => (st.schedule_minutes = Number(e.target.value)) }), "Between 5 minutes and 7 days"),
        field("After each run", sw("publish_on_complete", "Publish that run's tags to SharePoint")),
        field("Publish format", h("select", { class: "select", onchange: (e) => (st.publish_format = e.target.value) }, ...["xlsx", "csv", "parquet"].map((k) => h("option", { value: k, selected: st.publish_format === k || null }, k)))))),
      card("Tagging", h("div", null,
        field(h("span", null, "Confidence threshold · ", thrVal), h("input", { type: "range", min: 0.3, max: 0.9, step: 0.05, value: st.confidence_threshold, oninput: (e) => { st.confidence_threshold = Number(e.target.value); thrVal.textContent = pct(st.confidence_threshold); } }), "Calls below this go to the review queue and are marked low in exports"),
        field("Route", routeSel),
        h("div", { class: "grid cols-2" }, field("Classification model alias", h("input", { class: "input", value: st.classify_alias, oninput: (e) => (st.classify_alias = e.target.value) }), "From config/ai/providers.yaml"),
          field("Embedding model alias", h("input", { class: "input", value: st.embed_alias, oninput: (e) => (st.embed_alias = e.target.value) }))),
        field("Parallel LLM calls", h("input", { class: "input", type: "number", min: 1, max: 16, value: st.concurrency, oninput: (e) => (st.concurrency = Number(e.target.value)) }), "Also bounds parallel downloads")))));
    v.append(h("div", { class: "row", style: { margin: "16px 0" } }, h("button", { class: "btn primary", onclick: (e) => save(e.currentTarget) }, icon("check", 16), "Save settings")));
    const s = d.source;
    v.append(h("div", { class: "grid cols-2" },
      card("SharePoint source", h("dl", { class: "kv" }, ...[["Mode", `${s.mode} (${s.adapter})`], ["Site", s.site], ["Call-log folder", s.folder], ["Publish folder", s.publish_folder], ["Local copies", `${d.data_dir}/raw`]].flatMap(([k, x]) => [h("dt", null, k), h("dd", null, x)]),
        s.note ? [h("dt", null, "Note"), h("dd", null, s.note)] : []), null, "Set CCAI_SHAREPOINT_SITE / CCAI_SHAREPOINT_FOLDER and the Microsoft 365 app registration in Studio to go live"),
      card("Taxonomy", h("dl", { class: "kv" }, ...[["Version", S.status.taxonomy.version], ["Reasons", S.status.taxonomy.leaves], ["File", d.taxonomy_path], ["Digest", S.status.taxonomy.digest]].flatMap(([k, x]) => [h("dt", null, k), h("dd", null, String(x))])), null, "Edit config/taxonomy/ccai_taxonomy.yaml, bump the version, then Re-tag all")));
    main.replaceChildren(v);
  };

  // ── router ──────────────────────────────────────────────────────────
  async function route() {
    const [id, query] = (location.hash.slice(1) || "overview").split("?");
    const page = pages[id] ? id : "overview";
    S.page = page;
    renderNav();
    const params = Object.fromEntries(new URLSearchParams(query || ""));
    try { await pages[page](params); }
    catch (e) { main.replaceChildren(h("div", { class: "alert error" }, icon("error", 18), h("div", null, h("div", { class: "title" }, "Could not load this page"), e.message || String(e)))); }
  }
  window.addEventListener("hashchange", route);

  (async function boot() {
    main.replaceChildren(h("div", { class: "skeleton", style: { height: "120px" } }));
    try { S.leaves = await api("/api/taxonomy/leaves"); S.leaves.forEach((l) => (S.byId[l.id] = l)); } catch (e) { S.leaves = []; }
    await loadStatus();
    S.wasActive = !!(S.progress && S.progress.active);
    await route();
    connectEvents();
    setInterval(loadStatus, 30000);
  })();
})();
