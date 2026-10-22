/* Workbench shared shell: top bar, job dock with live log streaming,
   terminal renderer, small helpers. Builds on window.BSP (ui-kit). */
(function () {
  "use strict";
  const { h, icon } = BSP;
  const WB = {};
  const rawApi = BSP.api("");
  // Normalise FastAPI validation errors (detail = [{msg,…}]) into a readable string.
  WB.api = async function (path, opts) {
    try { return await rawApi(path, opts); }
    catch (e) {
      if (e && e.message != null && typeof e.message !== "string") {
        e.message = Array.isArray(e.message) ? e.message.map((x) => `${(x.loc || []).slice(-1)[0] || ""}: ${x.msg || JSON.stringify(x)}`).join("; ") : JSON.stringify(e.message);
      }
      throw e;
    }
  };

  // ── shell ──────────────────────────────────────────────────────────
  WB.shell = function (active) {
    const pill = h("button", { class: "wb-jobs-pill", title: "Background jobs", onclick: () => WB.dock.showAll() },
      h("span", { class: "spin" }), h("span", { class: "lbl" }, "1 job running"));
    WB._pill = pill;
    const link = (id, href, ic, label) => h("a", { href, class: active === id ? "active" : "", "aria-current": active === id ? "page" : null },
      icon(ic, 16), h("span", { class: "lbl" }, label));
    const nav = h("nav", { class: "wb-nav", "aria-label": "Workbench" },
      link("ingress", "/ingress/", "upload", "Import & Export"),
      link("files", "/files/", "folder", "Files"),
      link("dbt", "/dbt/", "stacks", "dbt"),
      link("pkg", "/pkg/", "package", "Packages"));
    const top = h("header", { class: "wb-topbar" },
      h("a", { class: "bsp-brand", href: `/${active}/` },
        h("span", { class: "mark", "aria-hidden": "true" }), h("span", null, "Workbench")),
      nav, h("div", { class: "spacer" }), pill,
      h("a", { class: "btn ghost sm", href: "/studio/" }, icon("arrow-left", 14), "Studio"));
    const main = h("main", { class: active === "files" ? "" : "wb-main", id: "main" });
    const root = h("div", { class: "wb-app" }, top, main);
    document.getElementById("app").replaceChildren(root);
    return { root, main, top };
  };

  // ── formatting ────────────────────────────────────────────────────
  WB.fmtDur = function (s) {
    if (s == null || isNaN(s)) return "";
    s = Math.max(0, Math.round(s));
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
    return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  };
  WB.statusBadge = function (st) {
    const map = { queued: ["pending", "Queued"], running: ["running", "Running"], succeeded: ["ok", "Succeeded"],
      failed: ["error", "Failed"], cancelled: ["neutral", "Cancelled"], finished: ["neutral", "Finished"] };
    const [cls, txt] = map[st] || ["neutral", st];
    return h("span", { class: `badge ${cls}` }, h("span", { class: "dot" }), txt);
  };
  WB.skeletonRows = function (n, cols) {
    return h("div", { class: "table-wrap" }, Array.from({ length: n || 6 }, (_, i) =>
      h("div", { class: "skel-row", style: cols ? { gridTemplateColumns: cols } : null },
        h("div", { class: "skeleton", style: { width: "16px" } }),
        h("div", { class: "skeleton", style: { width: `${50 + ((i * 37) % 40)}%` } }),
        h("div", { class: "skeleton", style: { width: "60%" } }),
        h("div", { class: "skeleton", style: { width: "70%" } }),
        h("div", { class: "skeleton", style: { width: "40%" } }))));
  };
  WB.empty = function ({ ic, title, body, actions }) {
    return h("div", { class: "empty" }, h("div", { class: "ic" }, icon(ic || "info", 22)),
      h("h4", null, title), body ? h("div", { style: { maxWidth: "460px", margin: "0 auto" } }, body) : null,
      actions ? h("div", { class: "row", style: { justifyContent: "center", marginTop: "14px" } }, actions) : null);
  };
  WB.copyLine = function (text, label) {
    return h("div", { class: "copyline" }, h("span", { title: text }, text),
      h("button", { class: "btn ghost icon sm", "aria-label": `Copy ${label || ""}`, title: "Copy", onclick: () => BSP.copy(text, label) }, icon("copy", 14)));
  };
  WB.download = function (url) {
    const a = h("a", { href: url, download: "", style: { display: "none" } });
    document.body.appendChild(a); a.click(); setTimeout(() => a.remove(), 1000);
  };

  // keyboard shortcuts (ignored while typing)
  const keys = {};
  WB.shortcut = (k, fn) => { keys[k] = fn; };
  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    if (document.querySelector(".modal.open, .drawer.open")) return;
    const fn = keys[e.key];
    if (fn) { e.preventDefault(); fn(e); }
  });

  // ── terminal ──────────────────────────────────────────────────────
  WB.terminal = function () {
    const el = h("div", { class: "term", role: "log", "aria-live": "polite", tabindex: "0" });
    const cursor = h("span", { class: "cursor" });
    let stick = true;
    el.addEventListener("scroll", () => { stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 24; });
    function cls(line) {
      if (line.startsWith("$ ")) return "cmd";
      if (/^\s*(✓|\+ |Successfully|Installed \d|Uninstalled \d|No new conflicts|Verified|Removed:|Copied)/.test(line)) return "ok";
      if (/^\s*(✗|ERROR|Error|error:|Traceback|Failed)/.test(line) || /\berror\b.*:/i.test(line) && !/^\s*warning/i.test(line)) return "err";
      if (/^\s*(⚠|warning|WARNING|Warning)/.test(line)) return "warn";
      if (/^\s*(- |==>|Using |Resolved |Prepared |Queued|Starting…|Audited|Would )/.test(line)) return "dim";
      return "";
    }
    const api = {
      el,
      add(lines) {
        const frag = document.createDocumentFragment();
        for (const ln of [].concat(lines)) frag.appendChild(h("span", { class: `ln ${cls(ln)}` }, ln + "\n"));
        el.insertBefore(frag, cursor.parentNode === el ? cursor : null);
        if (stick) el.scrollTop = el.scrollHeight;
      },
      clear() { el.replaceChildren(); },
      cursor(on) { if (on) el.appendChild(cursor); else cursor.remove(); },
    };
    return api;
  };

  // ── job dock ──────────────────────────────────────────────────────
  const dockEl = h("div", { class: "dock", "aria-label": "Background jobs" });
  const cards = new Map();
  let running = new Set();
  function updatePill() {
    if (!WB._pill) return;
    WB._pill.classList.toggle("show", running.size > 0);
    WB._pill.querySelector(".lbl").textContent = `${running.size} job${running.size === 1 ? "" : "s"} running`;
  }
  WB.dock = {
    showAll() { for (const c of cards.values()) c.el.classList.remove("min"); },
  };

  /** Stream a job into a dock card. opts: {base, onDone(summary), hint:boolean} */
  WB.watchJob = function (job, opts) {
    opts = opts || {};
    const base = opts.base || "/pkg";
    if (!dockEl.isConnected) document.body.appendChild(dockEl);
    if (cards.has(job.id)) { cards.get(job.id).el.classList.remove("min"); return cards.get(job.id).promise; }
    for (const c of cards.values()) if (c.done) c.el.classList.add("min");
    // keep the dock small
    const ids = [...cards.keys()];
    if (ids.length >= 3) { const old = cards.get(ids[0]); if (old.done) { old.el.remove(); cards.delete(ids[0]); } }

    const term = WB.terminal();
    term.cursor(true);
    const stIcon = h("span", { style: { display: "inline-flex" } });
    const meter = h("div", { class: "meter", style: { display: "none" } }, h("span", { style: { width: "0%" } }));
    const timer = h("span", { class: "caption" });
    const foot = h("div", { class: "dock-foot" });
    const extra = h("div");
    const cancelBtn = h("button", { class: "btn ghost sm", onclick: async (e) => {
      e.stopPropagation();
      if (!(await BSP.confirm({ title: "Cancel this job?", body: "The running command will be stopped. Partial changes may remain.", okText: "Cancel job", danger: true }))) return;
      try { await WB.api(`${base}/api/jobs/${job.id}/cancel`, { method: "POST" }); } catch (err) { BSP.toast(err.message, "error"); }
    } }, icon("stop", 14), "Cancel");
    const minBtn = h("button", { class: "btn ghost icon sm", "aria-label": "Minimise", title: "Minimise" }, icon("chevron-down", 14));
    const closeBtn = h("button", { class: "btn ghost icon sm", "aria-label": "Close", title: "Close", style: { display: "none" } }, icon("close", 14));
    const card = h("div", { class: "dock-card", role: "region", "aria-label": job.title },
      meter,
      h("div", { class: "dock-head" }, stIcon, h("div", { class: "t", title: job.title }, job.title), timer, minBtn, closeBtn),
      h("div", { class: "dock-body" }, term.el, extra, foot));
    const head = card.querySelector(".dock-head");
    head.onclick = (e) => { if (!e.target.closest("button")) card.classList.toggle("min"); };
    minBtn.onclick = () => card.classList.toggle("min");
    closeBtn.onclick = () => { card.remove(); cards.delete(job.id); };
    dockEl.appendChild(card);
    foot.append(h("span", { class: "caption" }, `Job ${job.id}`), h("span", { class: "spacer" }), cancelBtn);

    let summary = job;
    let t0 = (job.started || job.created || Date.now() / 1000);
    const tick = setInterval(() => { timer.textContent = WB.fmtDur(Date.now() / 1000 - t0); }, 1000);
    function setState(s) {
      summary = s;
      if (s.started) t0 = s.started;
      const st = s.status;
      stIcon.replaceChildren(st === "running" || st === "queued" ? h("span", { class: "spin", style: { width: "14px", height: "14px", border: "2px solid var(--bsp-tints-fiber-20)", borderTopColor: "var(--bsp-primary-fiber)", borderRadius: "50%", display: "inline-block", animation: "bsp-spin .7s linear infinite" } })
        : st === "succeeded" ? h("span", { style: { color: "#1f7a3c", display: "inline-flex" } }, icon("tick-circle", 18))
        : st === "cancelled" ? h("span", { style: { color: "var(--bsp-text-caption)", display: "inline-flex" } }, icon("stop", 18))
        : h("span", { style: { color: "var(--bsp-text-error)", display: "inline-flex" } }, icon("error", 18)));
      const p = s.progress;
      if (p && p.total) { meter.style.display = ""; meter.firstChild.style.width = `${Math.min(100, (100 * p.done) / p.total)}%`; timer.title = `${p.done}/${p.total} ${p.label || ""}`; }
    }
    setState(job);
    running.add(job.id); updatePill();

    let resolveP;
    const promise = new Promise((r) => { resolveP = r; });
    const entry = { el: card, done: false, promise };
    cards.set(job.id, entry);

    function finish(s) {
      if (entry.done) return;
      entry.done = true;
      clearInterval(tick);
      if (s.finished && s.started) timer.textContent = WB.fmtDur(s.finished - s.started);
      setState(s);
      term.cursor(false);
      cancelBtn.remove();
      closeBtn.style.display = "";
      running.delete(job.id); updatePill();
      const ok = s.status === "succeeded";
      const res = s.result || {};
      if (res.conflicts && res.conflicts.length) {
        extra.appendChild(h("div", { class: "alert warning", style: { marginTop: "10px" } }, icon("warning", 18),
          h("div", null, h("div", { class: "title" }, `${res.conflicts.length} new dependency conflict${res.conflicts.length > 1 ? "s" : ""}`),
            h("div", { class: "caption", style: { color: "inherit" } }, res.conflicts.slice(0, 4).join("\n")))));
      }
      if (ok && res.restart_hint && opts.hint !== false) {
        extra.appendChild(h("div", { class: "restart-hint" }, icon("refresh", 18),
          h("div", { style: { flex: 1 } }, h("b", null, "Restart your kernel"), " to use the new packages."),
          h("a", { class: "btn secondary sm", href: "/jupyter/", target: "_blank", rel: "noopener" }, "JupyterLab")));
      }
      BSP.toast(ok ? `${s.title} — done` : s.status === "cancelled" ? `${s.title} — cancelled` : `${s.title} — failed${s.error ? ": " + s.error : ""}`,
        ok ? "success" : s.status === "cancelled" ? "info" : "error");
      if (ok && !(res.conflicts && res.conflicts.length) && !res.restart_hint) setTimeout(() => card.classList.add("min"), 6000);
      opts.onDone && opts.onDone(s);
      resolveP(s);
    }

    let errors = 0;
    const es = new EventSource(`${base}/api/jobs/${job.id}/stream`);
    es.addEventListener("log", (e) => { errors = 0; try { term.add(JSON.parse(e.data)); } catch (_) { term.add(e.data); } });
    es.addEventListener("status", (e) => { try { setState(JSON.parse(e.data)); } catch (_) {} });
    es.addEventListener("end", (e) => { es.close(); let s = summary; try { const d = JSON.parse(e.data); if (d && d.status) s = d; } catch (_) {} if (!s.status || s.status === "running") s = { ...s, status: "finished" }; finish(s); });
    es.onerror = async () => {
      errors++;
      if (errors >= 3) {
        es.close();
        try { const s = await WB.api(`${base}/api/jobs/${job.id}`); if (["succeeded", "failed", "cancelled"].includes(s.status)) return finish(s); }
        catch (_) { /* fallthrough */ }
        term.add("✗ Lost connection to the job stream (the workbench may have restarted).");
        finish({ ...summary, status: "failed", error: "connection lost" });
      }
    };
    return promise;
  };

  /** Re-attach to jobs that are still running (e.g. after a reload). */
  WB.resume = async function (base, queues, onDone) {
    try {
      const r = await WB.api(`${base}/api/jobs?queue=${encodeURIComponent(queues.join(","))}&limit=20`);
      for (const j of r.jobs.filter((j) => j.status === "running" || j.status === "queued").reverse()) WB.watchJob(j, { base, onDone });
    } catch (_) { /* ignore */ }
  };

  /** Read-only log drawer for a finished/running job. */
  WB.jobDrawer = async function (base, job) {
    const term = WB.terminal();
    const d = BSP.drawer({ title: job.title, subtitle: `${job.status} · ${job.started ? BSP.fmtTime(job.started * 1000) : ""}`, wide: true,
      body: h("div", { class: "stack lg" }, h("div", { class: "row" }, WB.statusBadge(job.status),
        job.finished && job.started ? h("span", { class: "caption" }, `took ${WB.fmtDur(job.finished - job.started)}`) : null,
        job.error ? h("span", { class: "caption", style: { color: "var(--bsp-text-error)" } }, job.error) : null), term.el) });
    term.el.style.maxHeight = "calc(100vh - 200px)";
    try {
      const r = await WB.api(`${base}/api/jobs/${job.id}/log`);
      term.add(r.lines.length ? r.lines : ["(no output)"]);
    } catch (e) { term.add(`✗ ${e.message}`); }
    return d;
  };

  // ── tiny safe markdown (escape first, then a few constructs) ───────
  WB.markdown = function (src) {
    const esc = BSP.esc;
    const out = [];
    const lines = String(src).split("\n");
    let inCode = false, code = [], list = null;
    const inline = (s) => esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
      .replace(/(^|[^*])\*([^*]+)\*/g, "$1<i>$2</i>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
    for (const ln of lines) {
      if (ln.startsWith("```")) { if (inCode) { out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`); code = []; } else closeList(); inCode = !inCode; continue; }
      if (inCode) { code.push(ln); continue; }
      let m;
      if ((m = ln.match(/^(#{1,6})\s+(.*)/))) { closeList(); const n = Math.min(4, m[1].length); out.push(`<h${n}>${inline(m[2])}</h${n}>`); }
      else if ((m = ln.match(/^\s*[-*+]\s+(.*)/))) { if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; } out.push(`<li>${inline(m[1])}</li>`); }
      else if ((m = ln.match(/^\s*\d+[.)]\s+(.*)/))) { if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; } out.push(`<li>${inline(m[1])}</li>`); }
      else if ((m = ln.match(/^>\s?(.*)/))) { closeList(); out.push(`<blockquote>${inline(m[1])}</blockquote>`); }
      else if (!ln.trim()) { closeList(); out.push(""); }
      else { closeList(); out.push(`<p>${inline(ln)}</p>`); }
    }
    if (inCode) out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`);
    closeList();
    return h("div", { class: "md", html: out.join("\n") });
  };

  window.WB = WB;
})();
