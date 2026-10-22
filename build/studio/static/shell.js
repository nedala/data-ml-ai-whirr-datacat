/* Studio app shell: layout, hash routing, page loading and the page `ctx`
   (plan §4.2). Pages call Studio.registerPage({id, render(el, ctx), onLeave?}). */
(function () {
  "use strict";
  const { h, icon, toast } = BSP;
  const { passwordField, help, initials, fill } = StudioUI;
  const rawApi = BSP.api("");

  const registry = {};          // page id → definition
  const waiting = {};           // page id → resolve fn while its script loads
  const ENV_LABEL = { dev: "DEV", preprod: "PRE-PROD", prod: "PROD" };
  const PHASES = {
    queued: ["Waiting", 0], pulling: ["Pulling image…", 0.25], creating: ["Creating…", 0.4],
    starting: ["Starting…", 0.7], healthy: ["Ready", 1], stopping: ["Stopping…", 0.5], stopped: ["Stopped", 1], failed: ["Failed", 1],
  };

  let me = null;
  let current = null;           // {id, def, cleanups: []}
  let readOnly = sessionStorage.getItem("studio.readonly") === "1";
  const els = {};

  // ── API with transparent step-up / unlock ────────────────────────────
  async function api(path, opts, retried) {
    try { return await rawApi(path, opts || {}); } catch (e) {
      const admin = me && me.user.role === "admin";
      if (!retried && admin && e.status === 428 && await stepUp()) return api(path, opts, true);
      if (!retried && admin && e.status === 423 && await unlock()) return api(path, opts, true);
      throw e;
    }
  }

  function passwordModal({ title, subtitle, lede, okText, endpoint }) {
    return new Promise((resolve) => {
      let done = false;
      const pw = passwordField({ label: "Master passphrase", name: "master" });
      const err = h("div", { class: "error", role: "alert" });
      const ok = h("button", { class: "btn primary", type: "submit", form: "pw-modal" }, okText);
      const cancel = h("button", { class: "btn ghost", type: "button" }, "Cancel");
      const form = h("form", { id: "pw-modal" }, lede ? h("p", { class: "caption", style: { fontSize: "13px", lineHeight: "19px", marginBottom: "14px" } }, lede) : null, pw.el, err);
      const m = BSP.modal({ title, subtitle, body: form, foot: [cancel, ok], onClose: () => { if (!done) resolve(false); } });
      form.onsubmit = async (e) => {
        e.preventDefault();
        err.textContent = "";
        try {
          const r = await BSP.busy(ok, () => rawApi(endpoint, { body: { password: pw.input.value } }));
          done = true;
          setVault(r.vault || "unlocked");
          (r.notes || []).slice(0, 3).forEach((n) => toast(n, "info"));
          resolve(true);
          m.close();
        } catch (ex) {
          err.textContent = ex.message || "That didn't work";
          pw.input.select();
          form.classList.remove("shake"); void form.offsetWidth; form.classList.add("shake");
        }
      };
      cancel.onclick = () => { done = true; resolve(false); m.close(); };
      setTimeout(() => pw.input.focus(), 80);
    });
  }

  function stepUp() {
    return passwordModal({ title: "Confirm it's you", okText: "Confirm", endpoint: "/studio/api/auth/step-up",
      lede: "Revealing, copying or exporting secrets needs your vault master passphrase. You won't be asked again for 5 minutes." });
  }

  function unlock() {
    return passwordModal({ title: "Unlock the vault", okText: "Unlock", endpoint: "/studio/api/vault/unlock",
      lede: "Service credentials are encrypted at rest. Unlock the vault so Studio can start services and manage secrets." });
  }

  function setVault(state) {
    if (!me) return;
    me.vault = state;
    renderVaultButton();
    window.dispatchEvent(new CustomEvent("studio:vault", { detail: state }));
  }

  // ── jobs ─────────────────────────────────────────────────────────────
  let jobIndicator = null;

  function watchJob(id, opts) {
    opts = opts || {};
    return new Promise((resolve) => {
      let job = null, logFrom = 0, timer = null, modal = null, finished = false;
      const list = h("div", { class: "progress-list" });
      const bar = h("span", { style: { width: "0%" } });
      const meter = h("div", { class: "meter job-bar" }, bar);
      const log = h("pre", { class: "codeblock" });
      const details = h("details", { class: "job-log" }, h("summary", null, "Show details"), log);
      const notes = h("div", { class: "stack", style: { marginTop: "12px" } });
      const status = h("div", { class: "caption" }, "Starting…");

      // Progress docks in the bottom-right corner — NOT a modal: the page stays
      // usable while services restart (a Superset restart can take minutes).
      // Minimise it to the top-bar indicator; it reopens from there.
      const openModal = () => {
        if (modal) return;
        document.querySelectorAll(".job-dock").forEach((d) => d.remove());
        const minimise = h("button", { class: "btn ghost icon sm", title: "Minimise", "aria-label": "Minimise progress" }, icon("close", 16));
        const foot = h("div", { class: "modal-foot job-dock-foot" });
        const el = h("section", { class: "job-dock", role: "status", "aria-live": "polite" },
          h("div", { class: "job-dock-head" }, h("div", null, h("h3", null, opts.title || "Working on it"), status), minimise),
          h("div", { class: "job-dock-body" }, meter, list, notes, details), foot);
        document.body.append(el);
        requestAnimationFrame(() => el.classList.add("open"));
        modal = { el, close: () => { el.remove(); modal = null; if (!finished) showIndicator(); } };
        minimise.onclick = () => modal && modal.close();
        hideIndicator();
        if (job) paint();
      };
      const showIndicator = () => {
        hideIndicator();
        jobIndicator = h("button", { class: "top-btn job", title: "Show progress", onclick: openModal },
          h("span", { class: "spin" }), h("span", { class: "lbl" }, progressText()));
        fill(els.jobSlot, jobIndicator);
      };
      const hideIndicator = () => { fill(els.jobSlot, ); jobIndicator = null; };
      const progressText = () => {
        if (!job || !job.steps.length) return "Working…";
        const n = job.steps.filter((s) => ["healthy", "stopped", "failed"].includes(s.phase)).length;
        return `${n}/${job.steps.length} ready`;
      };

      function paint() {
        const steps = job.steps;
        const frac = steps.length ? steps.reduce((a, s) => a + (PHASES[s.phase] || PHASES.queued)[1], 0) / steps.length : (finished ? 1 : 0.1);
        bar.style.width = `${Math.round(frac * 100)}%`;
        meter.classList.toggle("warn", job.state === "failed");
        fill(list, ...steps.map((s) => {
          const [label] = PHASES[s.phase] || PHASES.queued;
          const terminal = ["healthy", "stopped", "failed"].includes(s.phase);
          const st = s.phase === "failed" ? icon("error", 16) : terminal ? icon("tick-circle", 16) : s.phase === "queued" ? icon("clock", 14) : h("span", { class: "spin" });
          return h("div", { class: `progress-item ${s.phase} ${!terminal && s.phase !== "queued" ? "active" : ""} ${s.phase === "healthy" || s.phase === "stopped" ? "done" : ""}` },
            h("span", { class: "state" }, st), h("span", { class: "name" }, s.title),
            h("span", { class: "phase" }, label, s.detail && s.phase !== "healthy" ? h("div", null, s.detail) : null));
        }));
        if (!steps.length && finished) fill(list, h("div", { class: "caption" }, job.kind === "rotate"
          ? "Saved to the vault — nothing running uses it yet, so no restarts were needed." : "Nothing to change — your appliance already matches."));
        fill(notes, 
          job.error ? h("div", { class: "alert error" }, icon("error", 18), h("div", null, h("div", { class: "title" }, "Something went wrong"), h("div", { style: { whiteSpace: "pre-wrap" } }, job.error))) : null,
          ...(job.notes || []).map((n) => h("div", { class: "alert info" }, icon("info", 18), h("div", null, n))));
        status.textContent = finished ? (job.state === "failed" ? "Finished with problems" : "All done") :
          "This keeps running if you close this window.";
        if (jobIndicator) jobIndicator.querySelector(".lbl").textContent = progressText();
        if (finished && modal) {
          const foot = modal.el.querySelector(".modal-foot");
          const closeBtn = h("button", { class: "btn ghost", onclick: () => modal.close() }, "Close");
          const go = job.state !== "failed" && ["apply", "start"].includes(job.kind) && current && current.id !== "launchpad"
            ? h("button", { class: "btn primary", onclick: () => { modal.close(); navigate("launchpad"); } }, "Open Launchpad", icon("arrow-right", 16)) : null;
          fill(foot, closeBtn, go);
          if (job.state === "failed") details.open = true;
        }
      }

      async function poll() {
        try {
          const j = await rawApi(`/studio/api/jobs/${id}?log_from=${logFrom}`);
          const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
          if (j.log.length) { log.append(j.log.join("\n") + "\n"); logFrom = j.log_total; }
          if (atBottom) log.scrollTop = log.scrollHeight;
          job = j;
          finished = ["done", "failed"].includes(j.state);
          paint();
          if (finished) {
            hideIndicator();
            if (!modal) toast(j.state === "failed" ? `${j.title}: ${j.error || "failed"}` : `${j.title} — done`, j.state === "failed" ? "error" : "success");
            window.dispatchEvent(new CustomEvent("studio:job-done", { detail: j }));
            resolve(j);
            return;
          }
        } catch (e) {
          if (e.status === 404) { hideIndicator(); resolve(null); return; }
        }
        timer = setTimeout(poll, 1000);
      }
      if (opts.background) showIndicator(); else openModal();
      poll();
    });
  }

  // ── layout ───────────────────────────────────────────────────────────
  function changePassphrase() {
    const cur = passwordField({ label: "Current master passphrase", name: "current" });
    const nw = passwordField({ label: "New master passphrase (12+ characters)", name: "new", meter: true });
    const again = passwordField({ label: "Repeat the new passphrase", name: "again" });
    [cur, nw, again].forEach((f) => f.input.setAttribute("autocomplete", f === cur ? "current-password" : "new-password"));
    const err = h("div", { class: "error", role: "alert" });
    const ok = h("button", { class: "btn primary", type: "submit", form: "pp-modal" }, "Change passphrase");
    const cancel = h("button", { class: "btn ghost", type: "button" }, "Cancel");
    const form = h("form", { id: "pp-modal" }, h("p", { class: "caption", style: { marginBottom: "14px" } },
      "Re-encrypts the vault under a new passphrase. Every stored secret is kept. Store it somewhere safe — it cannot be recovered."),
      cur.el, nw.el, again.el, err);
    const m = BSP.modal({ title: "Change master passphrase", body: form, foot: [cancel, ok] });
    cancel.onclick = () => m.close();
    form.onsubmit = async (e) => {
      e.preventDefault(); err.textContent = "";
      if (nw.input.value.length < 12) { err.textContent = "Use at least 12 characters"; return; }
      if (nw.input.value !== again.input.value) { err.textContent = "The new passphrases don't match"; return; }
      try {
        await BSP.busy(ok, () => rawApi("/studio/api/vault/passphrase", { body: { current: cur.input.value, new: nw.input.value } }));
        m.close(); toast("Master passphrase changed", "success");
      } catch (ex) { err.textContent = ex.message || "That didn't work"; }
    };
    setTimeout(() => cur.input.focus(), 80);
  }

  function renderVaultButton() {
    if (!els.vaultSlot) return;
    if (me.user.role !== "admin") { fill(els.vaultSlot, ); return; }
    // automatic key: encrypted at rest, nothing to unlock or confirm — no vault control at all
    if (me.vault_auto && me.vault === "unlocked") { fill(els.vaultSlot, ); return; }
    const locked = me.vault !== "unlocked";
    const btn = h("button", { class: `top-btn ${locked ? "locked" : ""}`, title: locked ? "Vault locked — click to unlock" : "Vault unlocked" },
      icon(locked ? "lock" : "unlock", 15), h("span", { class: "lbl" }, locked ? "Vault locked" : "Vault unlocked"));
    btn.onclick = () => {
      if (locked) return me.vault === "uninitialized" ? navigate(current ? current.id : "launchpad") : unlock();
      BSP.menu(btn, [
        { icon: "secret-key", label: "Change master passphrase…", onClick: changePassphrase },
        { icon: "lock", label: "Lock vault now", onClick: async () => {
          if (me.vault_source === "env") toast("The vault unlocks itself from APPLIANCE_MASTER_PASSWORD after a restart", "info");
          const r = await api("/studio/api/vault/lock", { method: "POST" }); setVault(r.vault); toast("Vault locked", "success");
        } },
      ]);
    };
    fill(els.vaultSlot, btn);
  }

  function renderNav() {
    const groups = {};
    me.pages.forEach((p) => (groups[p.group] = groups[p.group] || []).push(p));
    fill(els.nav, ...Object.entries(groups).map(([g, items]) => h("div", null,
      h("div", { class: "bsp-nav-group eyebrow" }, g),
      h("nav", { class: "bsp-nav", "aria-label": g }, items.map((p) =>
        h("a", { href: `#${p.id}`, dataset: { page: p.id }, class: current && current.id === p.id ? "active" : "" }, icon(p.icon || "apps", 17), p.title))))));
  }

  // About datacat: why it exists, who built it, what it does (for executives)
  function showAbout() {
    const person = (initialsTxt, name, role, note) => h("div", { class: "about-person" },
      h("span", { class: "about-mono", "aria-hidden": "true" }, initialsTxt),
      h("div", null, h("div", { class: "about-name" }, name), h("div", { class: "about-role" }, role),
        note ? h("div", { class: "about-note" }, note) : null));
    const body = h("div", { class: "about" },
      h("div", { class: "about-hero" },
        h("span", { class: "about-mark", "aria-hidden": "true" }),
        h("div", null, h("div", { class: "about-word" }, "datacat"),
          h("div", { class: "about-tag" }, "One governed workbench for Data, ML and AI self-service at DataPlatform"))),
      h("section", null, h("h4", null, "Why"),
        h("p", null, "DataPlatform is building a better, faster, AI-ready enterprise. datacat gives analysts, data scientists and AI engineers one governed, open-source workbench to go from a question to data, models and AI-powered automation on their own, with security, audit and cost control built in.")),
      h("section", null, h("h4", null, "Who"),
        h("div", { class: "about-people" },
          person("SE", "Seshu Edala", "Data Scientist", "Designed and built datacat with AI-assisted development (vibe coding)"),
          person("JL", "Jeff Lawlor", "VP of Data & Analytics, CFO Office", null))),
      h("section", null, h("h4", null, "What"),
        h("ul", { class: "about-what" },
          h("li", null, h("b", null, "Data: "), "no-code integration, BigQuery and dbt models keyed on BAN, Superset dashboards."),
          h("li", null, h("b", null, "ML: "), "a governed churn model lifecycle with MLflow, from features to serving, drift and retraining."),
          h("li", null, h("b", null, "AI: "), "an assistant grounded in our data and runbooks, disputes into SAP BRIM, and call tagging."),
          h("li", null, h("b", null, "Guardrails: "), "Entra sign-in, an encrypted vault, outbound allowlist, per-component cloud identity and an audit log."),
          h("li", null, h("b", null, "Ownership: "), "open source end to end, exportable artifacts, with Google Cloud Run as the production target."))));
    const close = h("button", { class: "btn ghost", type: "button" }, "Close");
    const story = h("button", { class: "btn primary", type: "button" }, "Start the story", icon("arrow-right", 16));
    const m = BSP.modal({ title: "About datacat", body, foot: [close, story], wide: true });
    close.onclick = () => m.close();
    story.onclick = () => { m.close(); navigate("story"); };
  }

  function buildLayout() {
    els.jobSlot = h("span");
    els.vaultSlot = h("span");
    const userBtn = h("button", { class: "user-btn", "aria-label": "Account menu" },
      h("span", { class: "avatar" }, initials(me.user.name)),
      h("span", { class: "who" }, me.user.name, " ", h("span", { class: "role" }, `· ${me.user.role}`)), icon("chevron-down", 14));
    userBtn.onclick = () => BSP.menu(userBtn, [
      { icon: "profile", label: `${me.user.name} (${me.user.source === "sso" ? "single sign-on" : "local account"})` },
      "-",
      { icon: "info", label: "About datacat", onClick: showAbout },
      { icon: "document", label: "Documentation", onClick: () => window.open("/docs/", "_blank", "noopener") },
      { icon: "logout", label: "Sign out", onClick: async () => { const r = await rawApi("/studio/api/auth/logout", { method: "POST" }); location.href = r.next; } },
    ]);
    els.nav = h("div");
    els.banner = h("div");
    els.main = h("main", { class: "bsp-main", id: "main", tabindex: "-1" });
    const app = document.getElementById("app");
    app.className = "bsp-app";
    fill(app, 
      h("header", { class: "bsp-topbar" },
        h("a", { class: "bsp-brand", href: "#launchpad", style: { color: "inherit", textDecoration: "none" } },
          h("span", { class: "mark", "aria-hidden": "true" }), h("span", { class: "word" }, "datacat",
            h("span", { class: "sub" }, ` · ${(me.appliance && me.appliance.name) || "Data & AI"} · Studio`))),
        h("span", { class: `env-pill ${me.env}`, title: `APPLIANCE_ENV=${me.env}` }, ENV_LABEL[me.env] || me.env.toUpperCase()),
        h("div", { class: "spacer" }), els.jobSlot, els.vaultSlot, userBtn),
      h("aside", { class: "bsp-sidebar" }, els.nav,
        h("div", { class: "foot caption" },
          h("button", { type: "button", class: "foot-link", onclick: showAbout }, icon("info", 14), "About datacat"),
          h("a", { class: "foot-link", href: "/docs/", target: "_blank", rel: "noopener" }, icon("document", 14), "Docs"))),
      h("div", { style: { minWidth: 0 } }, els.banner, els.main));
    app.removeAttribute("aria-busy");
    renderNav();
    renderVaultButton();
    if (me.plugin_errors.length && sessionStorage.getItem("studio.plugin-warn") !== "1") {
      const close = h("button", { class: "btn ghost icon sm", "aria-label": "Dismiss", style: { marginLeft: "auto" } }, icon("close", 14));
      const b = h("div", { class: "alert warning", style: { margin: "16px 32px 0" } }, icon("warning", 18),
        h("div", null, h("div", { class: "title" }, `${me.plugin_errors.length} Studio plugin${me.plugin_errors.length > 1 ? "s" : ""} could not load`),
          me.plugin_errors.map((e) => h("div", { class: "caption" }, `${e.plugin} (${e.where}): ${e.error}`))), close);
      close.onclick = () => { sessionStorage.setItem("studio.plugin-warn", "1"); b.remove(); };
      fill(els.banner, b);
    }
  }

  // ── vault gate (unlock / create screen) ─────────────────────────────
  function vaultGate() {
    if (me.user.role !== "admin" || me.vault === "unlocked") return null;
    if (me.vault === "locked" && readOnly) return null;
    const creating = me.vault === "uninitialized";
    const pw = passwordField({ label: creating ? "New master passphrase" : "Master passphrase", name: "gate", meter: creating, min: 12, autocomplete: creating ? "new-password" : "current-password" });
    const pw2 = creating ? passwordField({ label: "Confirm passphrase", name: "gate2", autocomplete: "new-password" }) : null;
    const err = h("div", { class: "error", role: "alert" });
    const btn = h("button", { class: "btn primary lg", type: "submit", style: { width: "100%" } }, icon(creating ? "shield" : "unlock", 16), creating ? "Create vault" : "Unlock");
    const form = h("form", null, pw.el, pw2 ? pw2.el : null, err, btn);
    form.onsubmit = async (e) => {
      e.preventDefault();
      err.textContent = "";
      if (pw2 && pw.input.value !== pw2.input.value) { err.textContent = "Passphrases don't match"; return; }
      try {
        const r = await BSP.busy(btn, () => rawApi(creating ? "/studio/api/vault/init" : "/studio/api/vault/unlock", { body: { password: pw.input.value } }));
        (r.notes || []).slice(0, 3).forEach((n) => toast(n, "info"));
        toast(creating ? "Vault created" : "Vault unlocked", "success");
        setVault(r.vault);
        route();
      } catch (ex) { err.textContent = ex.message; form.classList.remove("shake"); void form.offsetWidth; form.classList.add("shake"); pw.input.select(); }
    };
    setTimeout(() => pw.input.focus(), 60);
    return h("div", { class: "unlock fade-in" }, h("div", { class: "card" },
      h("div", { class: "big-ic" }, icon(creating ? "shield" : "lock", 26)),
      h("h2", null, creating ? "Create the appliance vault" : "Unlock the vault"),
      h("p", { class: "muted", style: { marginTop: "6px" } }, creating
        ? "Every service credential is encrypted with this passphrase. Studio never stores it."
        : "Studio restarted, so the vault is locked. Your services keep running — unlock to start new ones or manage credentials."),
      form,
      creating ? null : h("button", { class: "btn ghost sm", style: { marginTop: "14px" }, onclick: () => { readOnly = true; sessionStorage.setItem("studio.readonly", "1"); route(); } }, "Continue read-only")));
  }

  // ── routing ──────────────────────────────────────────────────────────
  // "#page", "#page?x=1" or "#page/sub" (e.g. #story/3 — a page's own
  // sub-route, handed to it as params.sub)
  function parseHash() {
    const [path, q] = location.hash.replace(/^#/, "").split("?");
    const [id, ...rest] = (path || "").split("/");
    const params = Object.fromEntries(new URLSearchParams(q || ""));
    if (rest.length) params.sub = rest.join("/");
    return { id, params };
  }

  function navigate(id, params) {
    const q = params && Object.keys(params).length ? `?${new URLSearchParams(params)}` : "";
    const target = `#${id}${q}`;
    if (location.hash === target) route(); else location.hash = target;
  }

  function loadScript(page) {
    if (registry[page.id]) return Promise.resolve(registry[page.id]);
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error(`${page.title} did not load`)), 10000);
      waiting[page.id] = (def) => { clearTimeout(t); resolve(def); };
      const s = h("script", { src: page.script });
      s.onerror = () => { clearTimeout(t); reject(new Error(`Could not load ${page.script}`)); };
      document.head.appendChild(s);
    });
  }

  function leave() {
    if (!current) return;
    current.cleanups.forEach((fn) => { try { fn(); } catch (e) { console.error(e); } });
    try { current.def && current.def.onLeave && current.def.onLeave(); } catch (e) { console.error(e); }
    current = null;
  }

  function errorState(title, detail) {
    return h("div", { class: "card" }, h("div", { class: "empty" }, h("div", { class: "ic" }, icon("warning", 22)),
      h("h4", null, title), h("div", null, detail || "")));
  }

  async function route() {
    let { id, params } = parseHash();
    if (!id) {
      // admins land on the power-user Launchpad (or Start & stop when nothing
      // runs yet); everyone else lands on the executive Story
      const hasStory = me.pages.some((p) => p.id === "story");
      id = me.user.role !== "admin" && hasStory ? "story" : me.running > 0 ? "launchpad" : "compose";
      history.replaceState(null, "", `#${id}`);
    }
    leave();
    const gate = vaultGate();
    const page = me.pages.find((p) => p.id === id);
    current = { id, def: null, cleanups: [] };
    els.nav.querySelectorAll("a").forEach((a) => a.classList.toggle("active", a.dataset.page === id));
    if (gate) { fill(els.main, gate); return; }
    if (!page) { fill(els.main, errorState("Page not found", "It may need a role you don't have.")); return; }
    document.title = `${page.title} · Studio`;
    const mine = current;
    fill(els.main, h("div", { class: "page" }, h("div", { class: "skeleton sk-line", style: { width: "220px", height: "28px" } }),
      h("div", { class: "skeleton sk-line", style: { width: "420px" } }), h("div", { class: "grid auto", style: { marginTop: "24px" } },
        [1, 2, 3].map(() => h("div", { class: "skeleton sk-card" })))));
    let def;
    try { def = await loadScript(page); } catch (e) { if (current === mine) fill(els.main, errorState("This page failed to load", e.message)); return; }
    if (current !== mine) return;
    mine.def = def;
    const el = h("div", { class: "page fade-in" });
    fill(els.main, el);
    els.main.focus({ preventScroll: true });
    window.scrollTo(0, 0);
    try { await def.render(el, makeCtx(mine, params)); } catch (e) {
      console.error(e);
      if (current === mine) fill(el, errorState("Something went wrong on this page", e.message || String(e)));
    }
  }

  function makeCtx(page, params) {
    return {
      api, toast, confirm: BSP.confirm, copy: BSP.copy, stepUp, navigate, user: me.user, ui: BSP,
      // Studio extras (core pages use these; plugins may too)
      params, help, unlock, job: watchJob, me: () => me,
      every(ms, fn) { const t = setInterval(() => { if (!document.hidden) fn(); }, ms); page.cleanups.push(() => clearInterval(t)); return t; },
      on(evt, fn) { window.addEventListener(evt, fn); page.cleanups.push(() => window.removeEventListener(evt, fn)); },
      async refreshMe() { me = await rawApi("/studio/api/me"); renderNav(); renderVaultButton(); return me; },
    };
  }

  window.Studio = {
    about: () => showAbout(),
    registerPage(def) {
      if (!def || !def.id || typeof def.render !== "function") { console.error("Studio.registerPage: invalid page", def); return; }
      registry[def.id] = def;
      if (waiting[def.id]) { waiting[def.id](def); delete waiting[def.id]; }
    },
  };

  async function boot() {
    try { me = await rawApi("/studio/api/me"); } catch (e) {
      document.getElementById("app").replaceChildren(h("div", { class: "unlock" }, errorState("Studio is not reachable", "Refresh in a moment.")));
      return;
    }
    buildLayout();
    window.addEventListener("hashchange", route);
    window.addEventListener("studio:vault", () => { if (me.vault === "unlocked") { readOnly = false; sessionStorage.removeItem("studio.readonly"); } });
    await route();
    rawApi("/studio/api/jobs").then((list) => {
      const running = list.find((j) => !["done", "failed"].includes(j.state));
      if (running) watchJob(running.id, { title: running.title, background: true });
    }).catch(() => {});
  }
  boot();
})();
