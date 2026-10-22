/* Workbench · Packages (/pkg/) — Python package management, sample data
   and job activity. Vanilla JS on BSP + WB. */
(function () {
  "use strict";
  const { h, icon } = BSP;
  const api = WB.api;
  const BASE = "/pkg";
  const A = (p) => `${BASE}/api${p}`;

  const shell = WB.shell("pkg");
  const main = shell.main;

  const state = { tab: "packages", eco: "python", status: null };
  (function readHash() {
    const [, tab] = (location.hash || "").split("/");
    if (["packages", "samples", "activity"].includes(tab)) state.tab = tab;
  })();
  const writeHash = () => history.replaceState(null, "", `#/${state.tab}`);

  // ── ecosystem adapters ─────────────────────────────────────────────
  const ECO = {
    python: {
      label: "Python", noun: "PyPI", queue: "python", hasResolve: true, hasLatest: true, hasSize: true, hasVersions: true,
      placeholder: "Search PyPI or type a requirement — polars, scikit-learn>=1.5, pandas[excel]",
      validFree: (q) => /^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]+\])?\s*((===|==|!=|<=|>=|~=|<|>)\s*[A-Za-z0-9.*+!_-]+\s*,?\s*)*$/.test(q.trim()),
      list: () => api(A("/python/packages")),
      platform: async () => (await api(A("/python/platform"))).base,
      search: (q, signal) => api(A(`/python/search?q=${encodeURIComponent(q)}`), { signal }),
      info: (n) => api(A(`/python/pypi/${encodeURIComponent(n)}`)),
      install: (specs, pin) => api(A("/python/install"), { body: { specs, pin } }),
      resolve: (specs) => api(A("/python/resolve"), { body: { specs } }),
      remove: (names) => api(A("/python/remove"), { body: { names } }),
      pin: (names, pinned) => api(A("/python/pin"), { body: { names, pinned } }),
      upgrade: (names) => api(A("/python/upgrade"), { body: { names } }),
      manifestText: () => api(A("/python/manifest")),
      manifestUrl: A("/python/manifest?download=1"), manifestPath: "/data/pyuser/requirements.lock",
      restore: () => api(A("/python/manifest/restore"), { body: {} }),
      projectUrl: (n) => `https://pypi.org/project/${encodeURIComponent(n)}/`,
    },
  };

  // ── page frame ─────────────────────────────────────────────────────
  const tabsEl = h("div", { class: "tabs", role: "tablist" });
  const body = h("div");
  main.append(
    h("div", { class: "wb-head" },
      h("div", null, h("div", { class: "eyebrow" }, "Workbench"), h("h1", null, "Packages & data"),
        h("div", { class: "lede" }, "Add Python libraries to JupyterLab and VS Code, pin what matters, and load the sample datasets. Everything lands in your persistent workspace and survives restarts.")),
      h("div", { class: "row" }, h("a", { class: "btn ghost sm", href: "/jupyter/", target: "_blank", rel: "noopener" }, icon("open-in-new", 14), "JupyterLab"),
        h("a", { class: "btn ghost sm", href: "/code/", target: "_blank", rel: "noopener" }, icon("open-in-new", 14), "VS Code"))),
    tabsEl, body);

  const TABS = [["packages", "Packages", "package"], ["samples", "Sample data", "file-table"], ["activity", "Activity", "history"]];
  function renderTabs() {
    tabsEl.replaceChildren(...TABS.map(([id, label, ic]) => h("button", {
      role: "tab", "aria-selected": String(state.tab === id), class: state.tab === id ? "active" : "",
      onclick: () => { state.tab = id; writeHash(); render(); },
    }, icon(ic, 15), label)));
  }

  let cleanup = [];
  function render() {
    cleanup.forEach((f) => f()); cleanup = [];
    renderTabs();
    body.replaceChildren();
    body.className = "fade-in";
    if (state.tab === "packages") renderPackages();
    else if (state.tab === "samples") renderSamples();
    else renderActivity();
  }

  // ═════════════════════════════ PACKAGES ═════════════════════════════
  function renderPackages() {
    const eco = ECO.python;
    const infoLine = h("div", { class: "caption" });
    body.append(h("div", { class: "row between wrap", style: { marginBottom: "16px", gap: "12px" } },
      h("span", { class: "chip primary" }, icon("package", 14), `Python ${state.status && state.status.python ? state.status.python.version : ""}`), infoLine));
    const stats = h("div", { class: "wb-stats" }, ...[0, 1, 2, 3].map(() => h("div", { class: "stat" }, h("div", { class: "skeleton", style: { width: "60%", height: "10px" } }), h("div", { class: "skeleton", style: { width: "40%", height: "20px", marginTop: "8px" } }))));
    const addCard = h("div", { class: "card" });
    const userCard = h("div", { class: "card" }, WB.skeletonRows(5));
    const platCard = h("div", { class: "card" });
    body.append(stats, addCard, userCard, platCard);

    let data = null;
    async function load() {
      try {
        data = await eco.list();
      } catch (e) {
        userCard.replaceChildren(h("div", { class: "alert error" }, icon("error", 18), h("div", null, h("div", { class: "title" }, "Could not load packages"), e.message)));
        return;
      }
      infoLine.replaceChildren(`Installer: `, h("b", null, data.installer), ` · layer `, h("code", null, data.userbase));
      renderStats();
      renderUser();
    }

    function renderStats() {
      const u = data.user || [];
      const pinned = u.filter((r) => r.pinned).length;
      const size = u.reduce((a, r) => a + (r.size || 0), 0);
      const cells = [
        ["Your packages", u.filter((r) => !r.missing).length, `${u.filter((r) => r.requested).length} requested directly`],
        ["Pinned", pinned, pinned ? "exact versions kept on restore" : "nothing pinned"],
        ["Layer size", BSP.fmtBytes(size) || "0 B", "on your volume"],
        ["Platform", "…", "baked into the image"],
      ];
      stats.replaceChildren(...cells.map(([l, v, d], i) => h("div", { class: "stat", id: i === 3 ? "stat-plat" : null }, h("div", { class: "label" }, l), h("div", { class: "value" }, String(v)), h("div", { class: "delta" }, d))));
      eco.platform().then((b) => { const el = stats.querySelector("#stat-plat .value"); if (el) el.textContent = b.length.toLocaleString(); platRows = b; }).catch(() => {});
    }

    // ── add packages (search + cart) ────────────────────────────────
    const cart = [];
    const cartEl = h("div", { class: "wb-cart" });
    const cartFoot = h("div", { class: "wb-cart-foot" });
    const input = h("input", { class: "input", type: "search", placeholder: eco.placeholder, "aria-label": `Search ${eco.noun}`, autocomplete: "off", spellcheck: "false", role: "combobox", "aria-expanded": "false", "aria-controls": "suggest" });
    const sug = h("div", { class: "wb-suggest", id: "suggest", role: "listbox" });
    const searchBox = h("div", { class: "wb-search" }, icon("search", 18), input, h("div", { class: "hint-right" }, h("span", { class: "kbd" }, "/")), sug);
    addCard.append(
      h("div", { class: "card-head" }, h("div", null, h("h3", null, "Add packages"),
        h("div", { class: "caption" }, `Search ${eco.noun}, pick versions, review the changes, install. A dry-run shows exactly what changes before anything is installed.`)),
        h("button", { class: "btn ghost sm", onclick: importReqs }, icon("upload", 14), "Import requirements.txt")),
      searchBox, cartEl, cartFoot);
    WB.shortcut("/", () => input.focus());

    let ctrl = null, hl = -1;
    const doSearch = BSP.debounce(async () => {
      const q = input.value.trim();
      if (!q) return closeSug();
      ctrl && ctrl.abort(); ctrl = new AbortController();
      sug.replaceChildren(h("div", { class: "wb-note" }, h("span", { class: "skeleton", style: { width: "100%", height: "36px" } })));
      openSug();
      try {
        const res = await eco.search(q, ctrl.signal);
        if (input.value.trim() !== q) return;
        renderSug(q, res);
        if (res.index === "loading") setTimeout(() => { if (input.value.trim() === q) doSearch(); }, 1500);
      }
      catch (e) { if (e.name !== "AbortError") renderSug(q, { error: e.message }); }
    }, 260);
    input.addEventListener("input", doSearch);
    input.addEventListener("focus", () => { if (input.value.trim() && sug.childElementCount) openSug(); });
    input.addEventListener("keydown", (e) => {
      const opts = [...sug.querySelectorAll(".wb-opt")];
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault(); if (!opts.length) return;
        hl = (hl + (e.key === "ArrowDown" ? 1 : -1) + opts.length) % opts.length;
        opts.forEach((o, i) => o.classList.toggle("hl", i === hl)); opts[hl].scrollIntoView({ block: "nearest" });
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (hl >= 0 && opts[hl]) opts[hl].click();
        else if (eco.validFree(input.value)) addFree(input.value.trim());
      } else if (e.key === "Escape") { closeSug(); }
    });
    const outside = (e) => { if (!searchBox.contains(e.target)) closeSug(); };
    document.addEventListener("click", outside);
    cleanup.push(() => document.removeEventListener("click", outside));
    function openSug() { sug.classList.add("open"); input.setAttribute("aria-expanded", "true"); }
    function closeSug() { sug.classList.remove("open"); input.setAttribute("aria-expanded", "false"); hl = -1; }

    function opt(content, onclick, cls) { return h("button", { class: `wb-opt ${cls || ""}`, role: "option", onclick: () => { onclick(); closeSug(); input.value = ""; } }, content); }
    function renderSug(q, res) {
      hl = -1;
      const items = [];
      if (res.error) items.push(h("div", { class: "wb-note" }, icon("warning", 14), res.error));
      const ex = res.exact;
      if (ex && ex.found) {
        items.push(opt([h("div", { style: { flex: 1, minWidth: 0 } }, h("div", null, h("span", { class: "nm" }, ex.name), " ", h("span", { class: "ver" }, ex.latest)), h("div", { class: "sum" }, ex.summary || "")),
          h("span", { class: "tag btn primary sm" }, icon("add", 14), "Add")], () => addItem({ name: ex.name, latest: ex.latest, summary: ex.summary, versions: ex.versions }), "hero"));
      } else if (ex && ex.message) items.push(h("div", { class: "wb-note" }, icon(ex.offline ? "warning" : "info", 14), ex.message));
      const sugg = (res.suggestions || []).filter((n) => !ex || n !== (ex.name || "").toLowerCase().replace(/[-_.]+/g, "-"));
      if (sugg.length) {
        items.push(h("div", { class: "grp eyebrow" }, "On PyPI"));
        sugg.slice(0, 10).forEach((n) => items.push(opt([icon("package", 15), h("span", { class: "nm" }, n)], () => addByName(n))));
      } else if (res.index === "loading") items.push(h("div", { class: "wb-note" }, icon("clock", 14), "Building the PyPI name index for suggestions (first search only)…"));
      if ((res.local || []).length) {
        items.push(h("div", { class: "grp eyebrow" }, "Already available"));
        res.local.forEach((l) => items.push(opt([icon(l.layer === "user" ? "profile" : "lock", 15), h("span", { class: "nm" }, l.name), h("span", { class: "ver" }, l.version),
          h("span", { class: `tag chip xs ${l.layer === "user" ? "primary" : ""}` }, l.layer === "user" ? "your layer" : "platform")], () => addByName(l.name))));
      }
      if (eco.validFree(q)) items.push(opt([icon("arrow-right", 15), h("span", null, "Add “", h("b", null, q), "” as typed")], () => addFree(q)));
      else if (!items.length) items.push(h("div", { class: "wb-note" }, icon("info", 14), "Not a valid package name."));
      sug.replaceChildren(...items);
    }

    async function addByName(n) {
      if (!eco.info) return addItem({ name: n });
      try { const i = await eco.info(n); addItem({ name: i.name || n, latest: i.latest, summary: i.summary, versions: i.versions }); }
      catch (e) { addItem({ name: n }); }
    }
    function addFree(q) {
      const m = q.match(/^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]+\])?\s*(.*)$/);
      const name = m[1], rest = (m[3] || "").replace(/\s+/g, "");
      const exact = rest.match(/^==([^,]+)$/);
      if (exact) addItem({ name, extras: m[2] || "", version: exact[1], pin: true });
      else if (rest || m[2]) addItem({ name, spec: `${name}${m[2] || ""}${rest}` });
      else addByName(name);
    }
    function addItem(it) {
      const key = it.name.toLowerCase().replace(/[-_.]+/g, "-");
      if (cart.some((c) => c.key === key)) { BSP.toast(`${it.name} is already in the list`); return; }
      cart.push({ key, version: null, pin: false, ...it });
      renderCart();
      if (eco.hasVersions && !it.versions && !it.spec && eco.info) eco.info(it.name).then((i) => { const c = cart.find((c) => c.key === key); if (c) { c.versions = i.versions || []; c.latest = c.latest || i.latest; c.summary = c.summary || i.summary; renderCart(); } }).catch(() => {});
    }
    function renderCart() {
      cartEl.replaceChildren(...cart.map((c) => {
        let ver;
        if (c.spec) ver = h("code", { class: "chip outline" }, c.spec);
        else {
          const sel = h("select", { class: "select", "aria-label": `Version for ${c.name}`, onchange: (e) => { c.version = e.target.value || null; if (c.version) { c.pin = true; renderCart(); } } },
            h("option", { value: "" }, c.latest ? `Latest (${c.latest})` : "Latest"),
            ...(c.versions || (c.version ? [c.version] : [])).map((v) => h("option", { value: v }, v)));
          sel.value = c.version || "";
          ver = sel;
        }
        const pin = h("label", { class: "checkbox", title: "Record the exact installed version in the manifest" },
            h("input", { type: "checkbox", checked: c.pin || null, onchange: (e) => { c.pin = e.target.checked; } }), h("span", { class: "caption" }, "Pin"));
        return h("div", { class: "wb-cart-item" }, icon("package", 16),
          h("span", { class: "nm" }, c.name), h("span", { class: "sum" }, c.summary || ""), ver, pin,
          h("button", { class: "btn ghost icon sm", "aria-label": `Remove ${c.name} from list`, onclick: () => { cart.splice(cart.indexOf(c), 1); renderCart(); } }, icon("close", 14)));
      }));
      cartFoot.replaceChildren();
      if (!cart.length) return;
      const go = h("button", { class: "btn primary" }, icon(eco.hasResolve ? "search" : "download", 16), eco.hasResolve ? `Review & install ${cart.length}` : `Install ${cart.length}`);
      go.onclick = () => BSP.busy(go, reviewAndInstall);
      cartFoot.append(h("button", { class: "btn ghost sm", onclick: () => { cart.length = 0; renderCart(); } }, "Clear"), h("span", { class: "spacer" }), go);
    }
    function specsOf() {
      return cart.map((c) => {
        if (c.spec) return c.spec;
        return `${c.name}${c.extras || ""}${c.version ? "==" + c.version : ""}`;
      });
    }

    async function reviewAndInstall() {
      const specs = specsOf();
      const pin = cart.filter((c) => c.pin).map((c) => c.name);
      if (!eco.hasResolve) {
        const ok = await BSP.confirm({ title: `Install ${specs.length} ${eco.label} package${specs.length > 1 ? "s" : ""}?`,
          body: h("div", null, h("p", null, "These will be installed into your persistent library:"), h("div", { class: "row wrap" }, specs.map((s) => h("code", { class: "chip outline" }, s)))), okText: "Install" });
        if (!ok) return;
        return startInstall(specs, pin);
      }
      let plan;
      try { plan = await eco.resolve(specs); } catch (e) { return BSP.toast(e.message, "error"); }
      showPlan(plan, specs, pin);
    }

    function showPlan(plan, specs, pin) {
      const bodyEl = h("div", { class: "stack lg" });
      const ok = h("button", { class: "btn primary" }, "Install");
      const cancel = h("button", { class: "btn ghost" }, "Cancel");
      if (!plan.ok) {
        bodyEl.append(h("div", { class: "alert error" }, icon("error", 18), h("div", null, h("div", { class: "title" }, "These requirements can't be resolved"),
          h("pre", { class: "mono", style: { whiteSpace: "pre-wrap", margin: "6px 0 0" } }, plan.error))),
          logToggle(plan.log));
        ok.style.display = "none";
      } else {
        const ch = plan.changes;
        bodyEl.append(h("div", { class: "caption" }, ch.length ? `${ch.length} package${ch.length > 1 ? "s" : ""} will change in your layer` : "Everything requested is already installed.",
          ` · resolved with ${plan.installer}`, plan.relaxed ? " · other packages in your layer had to move" : ""));
        plan.warnings.forEach((w) => bodyEl.append(h("div", { class: "alert warning" }, icon("warning", 18), h("div", null, w.severity === "high" ? h("div", { class: "title" }, "Heads up") : null, w.message))));
        if (ch.length) {
          bodyEl.append(h("div", { class: "plan-list" }, ch.map((c) => h("div", { class: "plan-row" },
            h("span", { class: `act ${c.action}` }, c.action),
            h("div", null, h("span", { class: "nm" }, c.name), c.requested ? null : h("span", { class: "caption" }, "  dependency"),
              c.from_layer === "platform" ? h("span", { class: "chip xs warn", style: { marginLeft: "8px" } }, "shadows platform") : null),
            h("span", { class: "vers" }, c.from ? [c.from, " → "] : "", h("b", null, c.version))))));
        }
        bodyEl.append(logToggle(plan.log));
        ok.textContent = ch.length ? `Install ${ch.length} package${ch.length > 1 ? "s" : ""}` : "Record in manifest";
      }
      const m = BSP.modal({ title: "Review changes", subtitle: specs.join("  ·  "), body: bodyEl, foot: [cancel, ok], wide: true });
      cancel.onclick = m.close;
      ok.onclick = () => { m.close(); startInstall(specs, pin); };
    }
    function logToggle(log) {
      if (!log) return null;
      const pre = h("pre", { class: "codeblock", style: { display: "none", marginTop: "8px" } }, log);
      return h("div", null, h("button", { class: "btn ghost sm", onclick: (e) => { pre.style.display = pre.style.display ? "" : "none"; e.currentTarget.classList.toggle("open"); } }, icon("chevron-right", 14), "Resolver output"), pre);
    }
    async function startInstall(specs, pin) {
      try {
        const r = await eco.install(specs, pin);
        cart.length = 0; renderCart();
        WB.watchJob(r.job, { base: BASE, onDone: () => load() });
      } catch (e) { BSP.toast(e.message, "error"); }
    }

    function importReqs() {
      const ta = h("textarea", { class: "textarea mono", rows: "10", placeholder: "pandas>=2.1\npolars\nscikit-learn==1.5.2\n# comments are fine" });
      const file = h("input", { type: "file", accept: ".txt,.in,.lock,text/plain", style: { display: "none" }, onchange: async () => { const f = file.files[0]; if (f) ta.value = await f.text(); } });
      const out = h("div");
      const parse = h("button", { class: "btn secondary" }, "Check");
      const add = h("button", { class: "btn primary", disabled: true }, "Add to list");
      let accepted = [];
      parse.onclick = () => BSP.busy(parse, async () => {
        const r = await api(A("/python/requirements/parse"), { body: { text: ta.value } });
        accepted = r.accepted;
        out.replaceChildren(h("div", { class: "stack", style: { marginTop: "12px" } },
          h("div", { class: "row wrap" }, h("b", null, `${accepted.length} requirement${accepted.length === 1 ? "" : "s"}`), ...accepted.map((s) => h("code", { class: "chip outline" }, s))),
          r.rejected.length ? h("div", { class: "alert warning" }, icon("warning", 18), h("div", null, h("div", { class: "title" }, `${r.rejected.length} line(s) skipped`),
            ...r.rejected.slice(0, 8).map((x) => h("div", { class: "caption", style: { color: "inherit" } }, h("code", null, x.line), " — ", x.reason)))) : null));
        add.disabled = !accepted.length;
      });
      const m = BSP.modal({ title: "Import requirements", subtitle: "Paste a requirements.txt or choose a file. Options like -r, -e and --index-url are ignored for safety.",
        body: h("div", null, h("div", { class: "row", style: { marginBottom: "8px" } }, h("button", { class: "btn ghost sm", onclick: () => file.click() }, icon("document", 14), "Choose file…"), file), ta, out),
        foot: [parse, add], wide: true });
      add.onclick = () => { accepted.forEach((s) => addFree(s)); m.close(); };
    }

    // ── your packages table ─────────────────────────────────────────
    const latest = new Map(); const waiting = new Map();
    function latestCell(r) {
      const el = h("span", { class: "caption" }, r.missing ? "" : "…");
      if (r.missing || !r.version) return el;
      const fill = (v) => {
        if (!v) { el.textContent = "—"; return; }
        if (cmpVer(v, r.version) > 0) el.replaceChildren(h("span", { class: "upd", title: `Version ${v} available` }, icon("arrow-up", 12), v));
        else el.textContent = "up to date";
      };
      if (latest.has(r.name)) fill(latest.get(r.name));
      else { if (!waiting.has(r.name)) waiting.set(r.name, []); waiting.get(r.name).push(fill); }
      return el;
    }
    async function fetchLatest(names) {
      const q = names.filter((n) => !latest.has(n));
      const worker = async () => {
        while (q.length) {
          const n = q.shift();
          let v = null;
          try { const i = await eco.info(n); v = i.latest || null; } catch (_) { v = null; }
          latest.set(n, v); (waiting.get(n) || []).forEach((f) => f(v)); waiting.delete(n);
        }
      };
      await Promise.all([worker(), worker(), worker(), worker()]);
    }
    function cmpVer(a, b) {
      const pa = String(a).split(/[.+-]/), pb = String(b).split(/[.+-]/);
      for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
        const x = pa[i] || "0", y = pb[i] || "0";
        const nx = parseInt(x, 10), ny = parseInt(y, 10);
        if (!isNaN(nx) && !isNaN(ny) && nx !== ny) return nx - ny;
        if (isNaN(nx) || isNaN(ny)) { const c = x.localeCompare(y); if (c) return /^\d/.test(x) ? 1 : -c; }
      }
      return 0;
    }

    let table, rows = [];
    function renderUser() {
      rows = data.user || [];
      const filter = h("input", { class: "input", type: "search", placeholder: "Filter", "aria-label": "Filter your packages" });
      const bulk = h("div");
      const cols = [
        { key: "name", label: "Package", sort: true, render: (r) => h("div", { class: "pk-name" },
          h("span", { class: "nm" }, r.name,
            r.missing ? h("span", { class: "chip xs warn" }, "not installed") : null,
            !r.requested && !r.missing ? h("span", { class: "chip xs outline", title: r.required_by && r.required_by.length ? `Required by ${r.required_by.join(", ")}` : "Installed as a dependency" }, "dependency") : null,
            r.platform_version ? h("span", { class: "chip xs auto", title: `Shadows platform ${r.name} ${r.platform_version}` }, `platform ${r.platform_version}`) : null),
          r.summary ? h("span", { class: "sum", title: r.summary }, r.summary) : r.spec && r.spec !== r.name ? h("span", { class: "sum mono" }, r.spec) : null) },
        { key: "version", label: "Version", sort: true, render: (r) => h("span", { class: "mono" }, r.version || "—") },
        eco.hasLatest ? { key: "latest", label: "Latest", render: latestCell } : null,
        { key: "pinned", label: "Pinned", sort: (r) => (r.pinned ? 0 : 1), render: (r) => h("button", {
          class: `pin-btn ${r.pinned ? "on" : ""}`, "aria-pressed": String(!!r.pinned), title: r.pinned ? "Pinned — click to unpin" : "Pin this exact version", "aria-label": r.pinned ? `Unpin ${r.name}` : `Pin ${r.name}`,
          disabled: r.missing || null, onclick: () => togglePin([r.name], !r.pinned) }, icon("pin", 16)) },
        eco.hasSize ? { key: "size", label: "Size", num: true, sort: true, render: (r) => r.size != null ? BSP.fmtBytes(r.size) : "" } : null,
        { key: "_a", label: "", class: "actions", render: (r) => h("button", { class: "btn ghost icon sm", "aria-label": `Actions for ${r.name}`, onclick: (e) => rowMenu(e.currentTarget, r) }, icon("more", 16)) },
      ].filter(Boolean);
      table = BSP.table({ columns: cols, rows, pageSize: 50, rowKey: "name", selectable: true, initialSort: { key: "name", dir: 1 },
        empty: WB.empty({ ic: "package", title: `No ${eco.label} packages in your layer yet`, body: `Search above to add one. Platform packages are already available to every notebook.` }) });
      table.el.addEventListener("selection", () => renderBulk());
      filter.addEventListener("input", BSP.debounce(() => { const q = filter.value.toLowerCase(); table.update(q ? rows.filter((r) => r.name.toLowerCase().includes(q) || (r.summary || "").toLowerCase().includes(q)) : rows); renderBulk(); }, 120));
      function renderBulk() {
        const sel = table.selected();
        if (!sel.length) return bulk.replaceChildren();
        const names = sel.map((r) => r.name);
        bulk.replaceChildren(h("div", { class: "bulkbar" }, h("b", null, `${sel.length} selected`), h("span", { class: "spacer" }),
          h("button", { class: "btn ghost sm", onclick: () => togglePin(names, true) }, icon("pin", 14), "Pin"),
          h("button", { class: "btn ghost sm", onclick: () => togglePin(names, false) }, "Unpin"),
          h("button", { class: "btn ghost sm", onclick: () => upgrade(names) }, icon("arrow-up", 14), "Upgrade"),
          h("button", { class: "btn ghost sm", onclick: () => remove(names) }, icon("delete", 14), "Remove"),
          h("button", { class: "btn ghost icon sm", "aria-label": "Clear selection", onclick: () => { table.clearSelection(); renderBulk(); } }, icon("close", 14))));
      }
      const moreBtn = h("button", { class: "btn ghost sm", "aria-label": "More actions" }, icon("more", 16));
      moreBtn.onclick = () => BSP.menu(moreBtn, [
        { label: "View manifest", icon: "document", onClick: showManifest },
        eco.manifestUrl ? { label: "Download manifest", icon: "download", onClick: () => WB.download(eco.manifestUrl) } : null,
        { label: "Restore from manifest", icon: "history", onClick: restore },
        "-",
        { label: "Remove unused dependencies", icon: "wand", onClick: orphans },
      ]);
      const tools = h("div", { class: "wb-toolbar" },
        h("div", { class: "search" }, icon("search", 16), filter),
        h("button", { class: "btn secondary sm", onclick: (e) => BSP.busy(e.currentTarget, check) }, icon("shield", 14), "Check"),
        h("button", { class: "btn ghost icon sm", "aria-label": "Refresh", title: "Refresh", onclick: () => load() }, icon("refresh", 16)), moreBtn);
      userCard.replaceChildren(
        h("div", { class: "card-head" }, h("h3", null, "Your packages", h("span", { class: "count" }, String(rows.filter((r) => !r.missing).length))), tools),
        bulk, table.el);
      if (eco.hasLatest && rows.length) fetchLatest(rows.filter((r) => !r.missing).map((r) => r.name));
      renderPlatform();
    }

    function rowMenu(anchor, r) {
      BSP.menu(anchor, [
        r.pinned ? { label: "Unpin", icon: "pin", onClick: () => togglePin([r.name], false) } : { label: "Pin this version", icon: "pin", onClick: () => togglePin([r.name], true) },
        { label: "Upgrade to latest", icon: "arrow-up", onClick: () => upgrade([r.name]) },
        { label: "Change version…", icon: "edit", onClick: () => { addByName(r.name); input.scrollIntoView({ block: "center", behavior: "smooth" }); } },
        { label: `Open on ${eco.noun.split(" ")[0]}`, icon: "open-in-new", onClick: () => window.open(eco.projectUrl(r.name), "_blank", "noopener") },
        "-",
        { label: "Remove", icon: "delete", danger: true, onClick: () => remove([r.name]) },
      ]);
    }

    async function togglePin(names, pinned) {
      try {
        const r = await eco.pin(names, pinned);
        if (r && r.job) return WB.watchJob(r.job, { base: BASE, hint: false, onDone: () => load() });
        BSP.toast(`${pinned ? "Pinned" : "Unpinned"} ${names.length === 1 ? names[0] : names.length + " packages"}`, "success");
        load();
      } catch (e) { BSP.toast(e.message, "error"); }
    }
    async function upgrade(names) {
      const pinnedSel = rows.filter((r) => names.includes(r.name) && r.pinned).map((r) => r.name);
      const ok = await BSP.confirm({ title: names.length ? `Upgrade ${names.length === 1 ? names[0] : names.length + " packages"}?` : "Update every package?",
        body: pinnedSel.length ? `Pinned packages (${pinnedSel.join(", ")}) will be re-pinned at the new version.` : "The newest compatible versions will be installed into your layer.", okText: "Upgrade" });
      if (!ok) return;
      try { const r = await eco.upgrade(names); WB.watchJob(r.job, { base: BASE, onDone: () => { latest.clear(); load(); } }); }
      catch (e) { BSP.toast(e.message, "error"); }
    }
    async function remove(names) {
      const ok = await BSP.confirm({ title: `Remove ${names.length === 1 ? names[0] : names.length + " packages"}?`, danger: true, okText: "Remove",
        body: h("div", null, h("p", null, "They'll be uninstalled from your layer and dropped from the manifest. Platform packages are never touched."),
          h("div", { class: "row wrap" }, names.slice(0, 30).map((n) => h("code", { class: "chip outline" }, n)))) });
      if (!ok) return;
      try { const r = await eco.remove(names); WB.watchJob(r.job, { base: BASE, onDone: () => load() }); }
      catch (e) { BSP.toast(e.message, "error"); }
    }
    async function check() {
      const r = await api(A("/python/check"));
      if (r.ok) BSP.toast(`No new dependency conflicts${r.baseline ? ` (${r.baseline} pre-existing in the platform image ignored)` : ""}`, "success");
      else BSP.modal({ title: "Dependency conflicts", subtitle: "Introduced by packages in your layer", body: h("pre", { class: "codeblock" }, r.conflicts.join("\n")) });
    }
    async function orphans() {
      const r = await api(A("/python/orphans"));
      if (!r.orphans.length) return BSP.toast("Nothing to clean up — every package in your layer is used.", "success");
      const ok = await BSP.confirm({ title: `Remove ${r.orphans.length} unused package${r.orphans.length > 1 ? "s" : ""}?`, okText: "Remove", danger: true,
        body: h("div", null, h("p", null, "These were installed as dependencies of packages you've since removed, and nothing else needs them:"),
          h("div", { class: "row wrap" }, r.orphans.map((o) => h("code", { class: "chip outline" }, `${o.name} ${o.version}`)))) });
      if (ok) { try { const j = await eco.remove(r.orphans.map((o) => o.name)); WB.watchJob(j.job, { base: BASE, onDone: () => load() }); } catch (e) { BSP.toast(e.message, "error"); } }
    }
    async function showManifest() {
      let text = "";
      try { text = await eco.manifestText(); } catch (e) { text = `# ${e.message}`; }
      BSP.modal({ title: `${eco.label} manifest`, subtitle: eco.manifestPath, wide: true,
        body: h("div", { class: "snip" }, h("pre", { class: "codeblock" }, text || "# (empty — install something to start your manifest)"),
          h("button", { class: "btn ghost icon sm", style: { color: "#fff" }, "aria-label": "Copy manifest", onclick: () => BSP.copy(text, "Manifest") }, icon("copy", 14))),
        foot: eco.manifestUrl ? [h("button", { class: "btn secondary", onclick: () => WB.download(eco.manifestUrl) }, icon("download", 16), "Download")] : null });
    }
    async function restore() {
      const ok = await BSP.confirm({ title: "Restore from manifest?", okText: "Restore",
        body: `Re-installs everything listed in ${eco.manifestPath} (pinned entries at their exact versions). Useful after a reset or on a new workspace.` });
      if (!ok) return;
      try { const r = await eco.restore(); WB.watchJob(r.job, { base: BASE, onDone: () => load() }); } catch (e) { BSP.toast(e.message, "error"); }
    }

    // ── platform packages ───────────────────────────────────────────
    let platRows = null, platOpen = false;
    function renderPlatform() {
      const head = h("button", { class: `collapse-head ${platOpen ? "open" : ""}`, "aria-expanded": String(platOpen) },
        h("span", { class: "chev", style: { display: "inline-flex" } }, icon("chevron-right", 16)),
        h("h3", null, "Platform packages", platRows ? h("span", { class: "count" }, platRows.length.toLocaleString()) : null),
        h("span", { class: "caption", style: { marginLeft: "auto" } }, icon("lock", 12), " read-only · baked into the image"));
      const bodyEl = h("div", { style: { marginTop: "14px", display: platOpen ? "" : "none" } });
      head.onclick = () => { platOpen = !platOpen; renderPlatform(); };
      platCard.replaceChildren(head, bodyEl);
      if (!platOpen) return;
      bodyEl.append(WB.skeletonRows(4));
      (platRows ? Promise.resolve(platRows) : eco.platform()).then((b) => {
        platRows = b;
        const f = h("input", { class: "input", type: "search", placeholder: `Search ${b.length.toLocaleString()} platform packages`, "aria-label": "Search platform packages" });
        const t = BSP.table({ rows: b, pageSize: 25, rowKey: "name", initialSort: { key: "name", dir: 1 }, columns: [
          { key: "name", label: "Package", sort: true, render: (r) => h("div", { class: "pk-name" }, h("span", { class: "nm" }, r.name), r.summary ? h("span", { class: "sum" }, r.summary) : r.env ? h("span", { class: "sum" }, `env ${r.env}`) : null) },
          { key: "version", label: "Version", sort: true, render: (r) => h("span", { class: "mono" }, r.version || "—") },
          { key: "overridden_by", label: "", render: (r) => r.overridden_by ? h("span", { class: "chip xs primary", title: "Your layer has its own version" }, `yours: ${r.overridden_by}`) : "" },
        ] });
        f.addEventListener("input", BSP.debounce(() => { const q = f.value.toLowerCase(); t.update(q ? b.filter((r) => r.name.toLowerCase().includes(q) || (r.summary || "").toLowerCase().includes(q)) : b); }, 120));
        bodyEl.replaceChildren(h("div", { class: "row", style: { marginBottom: "10px" } }, h("div", { class: "search", style: { flex: 1, maxWidth: "420px" } }, icon("search", 16), f),
          h("span", { class: "caption" }, "Installing a different version adds it to your layer, which takes precedence.")), t.el);
      }).catch((e) => bodyEl.replaceChildren(h("div", { class: "alert error" }, icon("error", 18), e.message)));
    }

    renderCart();
    load();
  }

  // ═════════════════════════════ SAMPLE DATA ══════════════════════════
  function renderSamples() {
    const wrap = h("div", null, h("div", { class: "grid auto" }, Array.from({ length: 6 }, () => h("div", { class: "ds-card" },
      h("div", { class: "skeleton", style: { height: "36px", width: "36px" } }), h("div", { class: "skeleton", style: { width: "70%" } }), h("div", { class: "skeleton", style: { width: "90%" } })))));
    body.append(wrap);
    const TI = { warehouse: "database", lake: "folder-open", workspace: "folder" };
    async function load() {
      let r;
      try { r = await api(A("/samples")); } catch (e) { wrap.replaceChildren(h("div", { class: "alert error" }, icon("error", 18), e.message)); return; }
      const ds = r.datasets || [];
      const TL = r.targets || {};
      const allBtn = h("button", { class: "btn secondary sm", onclick: () => hydrate(["all"], "warehouse") }, icon("database", 14), "Load all into the warehouse");
      const head = h("div", { class: "row between wrap", style: { marginBottom: "14px", gap: "12px" } },
        h("div", null, h("h3", null, "Sample datasets"), h("div", { class: "caption" }, "The synthetic ISP dataset (bsp_seed) the starter notebooks, the bsp_edw dbt project and the dashboards build on. ",
          "The warehouse copy lands in the ", h("code", null, "raw"), " schema; the Parquet copy in the shared lake (", h("code", null, "/data/lake/samples/bsp"), ").")),
        ds.length ? allBtn : null);
      const note = r.note ? h("div", { class: "alert info", style: { marginBottom: "14px" } }, icon("info", 18), h("div", null, r.note)) : null;
      if (!ds.length) {
        wrap.replaceChildren(head, note || "", h("div", { class: "card" }, WB.empty({ ic: "file-table", title: "No sample datasets available", body: "This image has no sample-data catalogue." })));
        return;
      }
      wrap.replaceChildren(head, note || "", h("div", { class: "grid auto" }, ds.map((d) => {
        const hy = d.hydrated || {};
        const targets = d.targets || ["warehouse"];
        const done = targets.filter((t) => hy[t]);
        const others = targets.filter((t) => t !== "warehouse");
        const more = h("button", { class: "btn ghost sm", "aria-label": `Copy ${d.title || d.id} to…` }, "Copy to…", icon("chevron-down", 12));
        more.onclick = () => BSP.menu(more, others.map((t) => ({ label: `${TL[t] || t}${hy[t] ? " (again)" : ""}`, icon: TI[t] || "folder", onClick: () => hydrate([d.id], t) })));
        return h("div", { class: "ds-card" },
          h("div", { class: "top" }, h("div", { class: "ds-ic" }, icon("file-table", 18)),
            h("div", { style: { minWidth: 0, flex: 1 } }, h("div", { class: "title" }, d.title || d.id), h("div", { class: "caption mono", style: { overflow: "hidden", textOverflow: "ellipsis" } }, d.id))),
          h("div", { class: "desc" }, d.description || ""),
          h("div", { class: "meta" }, d.rows != null ? h("span", null, `${Number(d.rows).toLocaleString()} rows`) : null,
            d.table ? h("span", { class: "mono" }, d.table) : null,
            d.columns ? h("span", { title: [].concat(d.columns).join(", ") }, `${[].concat(d.columns).length} columns`) : null),
          h("div", { class: "row wrap" }, done.length ? done.map((t) => h("span", { class: "badge ok" }, icon("check", 12), TL[t] || t)) : h("span", { class: "badge neutral" }, "Not loaded yet")),
          h("div", { class: "actions" },
            targets.includes("warehouse") ? h("button", { class: `btn ${hy.warehouse ? "ghost" : "secondary"} sm`, onclick: () => hydrate([d.id], "warehouse") },
              icon("database", 14), hy.warehouse ? "Reload" : "Load into warehouse") : null,
            others.length ? more : null));
      })));
    }
    async function hydrate(ids, target) {
      try { const r = await api(A("/samples/hydrate"), { body: { ids, target } }); WB.watchJob(r.job, { base: BASE, hint: false, onDone: load }); }
      catch (e) { BSP.toast(e.message, "error"); }
    }
    load();
  }

  // ═════════════════════════════ ACTIVITY ═════════════════════════════
  function renderActivity() {
    const holder = h("div", { class: "card" }, WB.skeletonRows(6));
    body.append(holder);
    const qmap = { python: "Python", data: "Sample data" };
    let t = null;
    async function load() {
      let r;
      try { r = await api(A("/jobs?queue=python,data&limit=100")); } catch (e) { holder.replaceChildren(h("div", { class: "alert error" }, icon("error", 18), e.message)); return; }
      const rows = r.jobs;
      if (!t) {
        t = BSP.table({ rows, pageSize: 25, rowKey: "id", onRowClick: (j) => (j.status === "running" || j.status === "queued") ? WB.watchJob(j, { base: BASE }) : WB.jobDrawer(BASE, j),
          empty: WB.empty({ ic: "history", title: "No activity yet", body: "Installs, removals and sample-data loads will show up here with their full logs." }),
          columns: [
            { key: "title", label: "Job", sort: true, render: (j) => h("div", { class: "pk-name" }, h("span", { class: "nm" }, j.title), j.error ? h("span", { class: "sum", style: { color: "var(--bsp-text-error)" } }, j.error) : null) },
            { key: "queue", label: "Area", sort: true, render: (j) => h("span", { class: "chip xs outline" }, qmap[j.queue] || j.queue) },
            { key: "status", label: "Status", sort: true, render: (j) => WB.statusBadge(j.status) },
            { key: "created", label: "Started", sort: (j) => -(j.created || 0), render: (j) => h("span", { title: BSP.fmtTime((j.started || j.created) * 1000) }, BSP.fmtAgo((j.started || j.created) * 1000)) },
            { key: "dur", label: "Duration", num: true, render: (j) => j.finished && j.started ? WB.fmtDur(j.finished - j.started) : j.status === "running" ? "…" : "" },
          ] });
        holder.replaceChildren(h("div", { class: "card-head" }, h("h3", null, "Activity"),
          h("button", { class: "btn ghost icon sm", "aria-label": "Refresh", onclick: load }, icon("refresh", 16))), t.el);
      } else t.update(rows);
    }
    load();
    const iv = setInterval(() => { if (!document.hidden) load(); }, 5000);
    cleanup.push(() => clearInterval(iv));
  }

  // ── boot ───────────────────────────────────────────────────────────
  api(A("/status")).then((s) => { state.status = s; }).catch(() => {}).finally(render);
  WB.resume(BASE, ["python", "data"], () => { if (state.tab !== "activity") render(); });
})();
