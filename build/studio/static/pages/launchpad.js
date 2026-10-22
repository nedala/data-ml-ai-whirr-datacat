/* Launchpad: the tools you can open right now, appliance health, and a
   getting-started checklist for newcomers. */
Studio.registerPage({
  id: "launchpad",
  async render(el, ctx) {
    const { h, icon } = ctx.ui;
    const { fill } = StudioUI;
    const GS_KEY = `studio.gs.${ctx.user.name}`;
    const HEALTH_TARGET = { vault: null, tls: "certificates", egress: "egress" };
    const store = {
      get() { try { return JSON.parse(localStorage.getItem(GS_KEY) || "{}"); } catch (e) { return {}; } },
      set(v) { try { localStorage.setItem(GS_KEY, JSON.stringify(v)); } catch (e) { /* private mode */ } },
    };

    const hero = h("div", { class: "hero" });
    const strip = h("div", { class: "health-list", "aria-label": "Appliance health" });
    const statusSummary = h("span", { class: "chip" });
    const welcome = h("div");
    const running = h("div");
    // later sections keep their own top spacing: the heading is the first
    // child of its wrapper, so `.section-head:first-child` would zero it
    const available = h("div", { class: "lp-section" });
    // connection strings live in the rail, under System Status
    const connect = h("div", { class: "lp-rail-card lp-connect", hidden: true });
    const openGroups = new Set();   // which connection groups the user expanded (kept across refreshes)
    // tools on the left; system status + connection strings in a sticky right-hand rail
    const rail = h("aside", { class: "lp-rail", "aria-label": "System status and connections" },
      h("div", { class: "lp-rail-card" },
        h("div", { class: "lp-rail-head" }, h("h3", null, "System Status"), statusSummary),
        strip),
      connect);
    el.append(hero, h("div", { class: "lp-layout" }, h("div", { class: "lp-main" }, welcome, running, available), rail));

    function greeting() {
      const hr = new Date().getHours();
      return hr < 12 ? "Good morning" : hr < 18 ? "Good afternoon" : "Good evening";
    }

    function renderHero(d) {
      const personas = (d.selection.personas || []).map((p) => d.personas[p]).filter(Boolean);
      const first = ctx.user.name.split(/[@._-]/)[0];
      fill(hero, 
        h("div", null, h("h1", null, `${greeting()}, ${first.charAt(0).toUpperCase()}${first.slice(1)}`),
          h("div", { class: "lede" }, d.running
            ? `${personas.length ? personas.join(" + ") : "Custom setup"} · ${d.running} service${d.running === 1 ? "" : "s"} running`
            : d.active_job ? "Setting things up. Your tools appear here as soon as they are ready."
              : `Nothing is running yet. Press Start to bring up your ${d.appliance_name || ""} appliance.`)),
        h("div", { class: "page-actions" },
          h("button", { class: d.running ? "btn secondary" : "btn primary", onclick: () => ctx.navigate("compose") }, icon("wand", 16), d.running ? "Change setup" : "Start my appliance")));
    }

    function renderHealth(items) {
      const ok = items.filter((it) => it.state === "ok").length;
      statusSummary.className = `chip ${ok === items.length ? "ok" : "warn"}`;
      statusSummary.textContent = ok === items.length ? "All healthy" : `${items.length - ok} need attention`;
      fill(strip, ...items.map((it) => {
        const target = it.id === "vault" ? (it.state !== "ok" && d.is_admin ? "unlock" : null) : HEALTH_TARGET[it.id];
        const pill = h(target ? "button" : "div", { class: `health-pill ${it.state} ${target ? "link" : ""}`, title: it.detail || "",
          onclick: target ? () => (target === "unlock" ? ctx.unlock() : ctx.navigate(target)) : null },
          h("span", { class: "dot" }), h("span", { class: "lbl" }, it.label), h("span", { class: "det" }, it.detail || it.state));
        return pill;
      }));
    }

    function renderWelcome(d) {
      const gs = store.get();
      if (gs.dismissed) { fill(welcome, ); return; }
      const up = (svc) => d.apps.some((a) => a.service === svc && a.state === "running");
      const STEPS = {
        "data-pro": [
          { k: "dbt", t: "Explore the bsp_edw models in dbt Studio", d: "Lineage from raw billing and servicing data to the marts. Build a model without writing YAML.", href: "/dbt/", ok: up("appliance") },
          { k: "ingress", t: "Bring a table in", d: "Import a file, a BigQuery table or a Trino query into the landing schema.", href: "/ingress/", ok: up("appliance") },
          { k: "notebooks", t: "Open the getting-started notebook", d: "Query the warehouse and the lake from JupyterLab.", href: "/jupyter/lab/tree/notebooks", ok: up("appliance") },
          { k: "dashboard", t: "Open the Superset dashboards", d: "Revenue, churn, fiber penetration and network reliability on the marts.", href: "/superset/", ok: up("superset"), need: "bi" },
        ],
        "ml-scientist": [
          { k: "notebooks", t: "Open the getting-started notebook", d: "Load the churn features from the warehouse.", href: "/jupyter/lab/tree/notebooks", ok: up("appliance") },
          { k: "mlops", t: "Open the MLOps control tower", d: "Experiments, registry, deployments and drift in one place.", href: "/mlops/", ok: up("mlops"), need: "mlops" },
          { k: "dbt", t: "See where the features come from", d: "mart_churn_features in dbt Studio.", href: "/dbt/", ok: up("appliance") },
        ],
        "ai-engineer": [
          { k: "ai", t: "Open the AI harness", d: "Playground and evals over the model aliases (Ollama, Vertex AI).", href: "/ai/", ok: up("harness"), need: "ai" },
          { k: "disputes", t: "Triage the dispute inbox", d: "E-mails → structured cases → SAP BRIM.", href: "/disputes/", ok: up("disputes"), need: "disputes" },
          { k: "notebooks", t: "Open the getting-started notebook", d: "Call the LLM gateway and the MCP tools from Python.", href: "/jupyter/lab/tree/notebooks", ok: up("appliance") },
        ],
      };
      const steps = STEPS[d.persona] || STEPS["data-pro"];
      const close = h("button", { class: "btn ghost icon sm", "aria-label": "Dismiss getting started", title: "Dismiss" }, icon("close", 14));
      close.onclick = () => { store.set({ ...store.get(), dismissed: true }); fill(welcome, ); };
      const done = steps.filter((s) => gs[s.k]).length;
      fill(welcome, h("div", { class: "card welcome", style: { marginBottom: "24px" } },
        h("div", { class: "card-head", style: { marginBottom: 0 } },
          h("div", null, h("h3", null, "Getting started"), h("div", { class: "caption" }, `${done} of ${steps.length} done · new here? Start with these.`)), close),
        h("div", { class: "checklist" }, steps.map((s, i) => {
          const isDone = !!gs[s.k];
          if (!s.ok) {
            return h("button", { type: "button", class: "check-step", style: { textAlign: "left", font: "inherit", cursor: "pointer" },
              onclick: () => ctx.navigate("compose", s.need ? { add: s.need } : { add: "workspace" }) },
              h("span", { class: "n" }, String(i + 1)),
              h("span", null, h("div", { class: "t" }, s.t), h("div", { class: "d" }, s.need ? `Add ${s.need} to your appliance first →` : "Add the Workspace to your appliance first →")));
          }
          return h("a", { class: `check-step ${isDone ? "done" : ""}`, href: s.href, target: "_blank", rel: "noopener",
            onclick: () => { store.set({ ...store.get(), [s.k]: true }); setTimeout(refresh, 300); } },
            h("span", { class: "n" }, isDone ? icon("check", 12) : String(i + 1)),
            h("span", null, h("div", { class: "t" }, s.t), h("div", { class: "d" }, s.d)));
        }))));
    }

    async function copyCred(app) {
      try {
        const r = await ctx.api("/studio/api/credentials/reveal", { body: { service: app.cred.service, env: app.cred.env } });
        ctx.copy(r.value, `${app.title} ${app.cred.label.toLowerCase().includes("token") ? "token" : "password"}`);
      } catch (e) { if (e.status) ctx.toast(e.message, "error"); }
    }

    // Route shown under the title: path only (query strings like noVNC's are noise)
    function route(path) {
      try { const u = new URL(path, location.origin); return u.origin === location.origin ? u.pathname : `${u.host}${u.pathname}`; }
      catch (e) { return String(path || "").split("?")[0]; }
    }

    // One quiet card per tool: icon chip (status dot on its corner) · name ·
    // one job line. A running card is itself the Open target (an overlay link,
    // so the admin tools below it are not nested inside the <a>). Status is
    // shown by exception: only "Starting" and "Not added" get words.
    function appCard(app, d) {
      const on = app.state === "running";
      const starting = ["starting", "unhealthy"].includes(app.state);
      const tone = on ? "ok" : starting ? "warn" : "off";
      const dotLabel = on ? "Running" : starting ? "Starting" : "Not added";
      const credKind = app.cred && /token/i.test(app.cred.label || "") ? "token" : "password";
      const stop = (fn) => (e) => { e.preventDefault(); e.stopPropagation(); fn(); };
      const tools = [];
      if ((on || starting) && app.alt && app.alt.url)
        tools.push(h("a", { class: "btn ghost icon sm lp-tool", href: app.alt.url, target: "_blank", rel: "noopener",
          title: app.alt.label, "aria-label": app.alt.label, onclick: (e) => e.stopPropagation() }, icon("screen-share", 16)));
      if ((on || starting) && app.cred && d.is_admin)
        tools.push(h("button", { type: "button", class: "btn ghost icon sm lp-tool",
          title: `Copy ${app.title} ${credKind}${app.cred.user ? ` (user ${app.cred.user})` : ""}`,
          "aria-label": `Copy ${app.title} ${credKind}`, onclick: stop(() => copyCred(app)) }, icon("copy", 16)));
      const add = !on && !starting && app.capability
        ? h("button", { type: "button", class: "btn secondary sm", onclick: stop(() => ctx.navigate("compose", { add: app.capability })) },
            icon("add", 14), "Add to appliance") : null;
      const stateText = on ? null : h("span", { class: `lp-state ${tone}` }, starting ? "Starting" : "Not added");
      const foot = tools.length || stateText || add
        ? h("div", { class: "actions" }, stateText, h("span", { class: "spacer" }), add, ...tools) : null;
      const cover = on ? h("a", { class: "lp-cover", href: app.path, target: "_blank", rel: "noopener",
        title: `Open ${app.title} in a new tab (${route(app.path)})`, "aria-label": `Open ${app.title} in a new tab` }) : null;
      return h("div", { class: `app-card ${on ? "linked" : ""} ${on || starting ? "" : "off"}` },
        cover,
        h("div", { class: "head" },
          h("span", { class: "ic" }, icon(app.icon || "apps", 20),
            h("span", { class: `lp-dot ${tone}`, title: dotLabel, "aria-label": dotLabel, role: "img" })),
          h("div", { class: "title-wrap" },
            h("div", { class: "title", title: app.title }, app.title),
            h("div", { class: "blurb", title: app.blurb }, app.blurb)),
          on ? h("span", { class: "lp-go", "aria-hidden": "true" }, icon("external", 15)) : null),
        foot);
    }

    // Tiles grouped into the catalog's panels (app_groups), in catalog order;
    // tiles without a known group land in a trailing "More" panel. Mosaic:
    // each panel grows in proportion to its tile count, so rows fill edge to
    // edge and every tile ends up about the same width.
    function panels(apps, d) {
      const groups = [...(d.groups || []), { id: "", title: "More", icon: "apps", blurb: "" }];
      const known = new Set(groups.map((g) => g.id));
      const sections = groups.map((g) => {
        const tiles = apps.filter((a) => (known.has(a.group) ? a.group : "") === g.id);
        if (!tiles.length) return null;
        const n = tiles.length;
        return h("section", { class: "lp-panel", "data-tone": g.id || "more", "aria-label": g.title,
          style: { flex: `${n} 1 min(100%, ${n * 270 + (n - 1) * 14 + 34}px)` } },
          h("div", { class: "lp-panel-head" }, h("span", { class: "ic" }, icon(g.icon || "apps", 16)),
            h("div", { class: "lp-panel-txt" }, h("h3", null, g.title), g.blurb ? h("div", { class: "lp-purpose" }, g.blurb) : null),
            h("span", { class: "lp-count", title: `${tiles.length} tools` }, `${tiles.length}`)),
          h("div", { class: "lp-tiles" }, tiles.map((a) => appCard(a, d))));
      }).filter(Boolean);
      return h("div", { class: "lp-panels" }, sections);
    }

    let d = null;
    function render() {
      renderHero(d);
      renderHealth(d.health);
      renderWelcome(d);
      const live = d.apps.filter((a) => a.state !== "missing" && a.state !== "exited");
      const off = d.apps.filter((a) => !live.includes(a));
      fill(running, h("div", { class: "section-head" }, h("span", { class: "eyebrow" }, "Your tools",
        ctx.help("Every tool opens in a new tab behind the appliance's single sign-in. Some tools also have their own password. Admins can copy it with the key button (you'll confirm your master passphrase first).")),
        d.active_job ? h("button", { class: "btn ghost sm", onclick: () => ctx.job(d.active_job, { title: "Applying changes" }) }, h("span", { class: "spin-sm" }), "Changes in progress") : null),
        live.length ? panels(live, d)
          : h("div", { class: "card" }, h("div", { class: "empty" }, h("div", { class: "ic" }, icon("wand", 22)),
            h("h4", null, "No tools running yet"), h("div", null, "Press Start. Your tools appear here in about a minute."),
            h("button", { class: "btn primary", style: { marginTop: "14px" }, onclick: () => ctx.navigate("compose") }, "Start my appliance"))));
      connect.hidden = !d.connect.length;
      if (d.connect.length) {
        if (!openGroups.size) openGroups.add(d.connect[0].title);   // first group open on first render
        fill(connect,
          h("div", { class: "lp-rail-head" }, h("h3", null, "Connection strings",
            ctx.help("Warehouse, shared-folder and DevTools addresses for code running inside the appliance (notebooks, VS Code) and for tools on this host. Credentials are on the Credentials page.")),
            h("span", { class: "chip" }, String(d.connect.reduce((n, c) => n + c.endpoints.length, 0)))),
          ...d.connect.map((c) => {
            const group = h("details", { class: "lp-conn", open: openGroups.has(c.title) },
              h("summary", null, h("span", null, c.title), h("span", { class: "n" }, String(c.endpoints.length))),
              StudioUI.endpointList(c.endpoints));
            group.addEventListener("toggle", () => (group.open ? openGroups.add(c.title) : openGroups.delete(c.title)));
            return group;
          }));
      }
      fill(available, off.length ? h("div", null, h("div", { class: "section-head" }, h("span", { class: "eyebrow" }, "Available to add")),
        h("div", { class: "app-grid" }, off.map((a) => appCard(a, d)))) : "");
    }

    async function refresh() {
      try { d = await ctx.api("/studio/api/launchpad"); render(); } catch (e) { if (e.status) ctx.toast(e.message, "error"); }
    }

    await refresh();
    ctx.every(5000, refresh);
    ctx.on("studio:job-done", refresh);
    ctx.on("studio:vault", refresh);
  },
});
