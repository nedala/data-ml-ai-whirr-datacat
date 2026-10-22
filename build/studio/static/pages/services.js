/* Services: every catalog + control-plane service with state, uptime, memory,
   logs, and start / stop / restart for admins. */
Studio.registerPage({
  id: "services",
  async render(el, ctx) {
    const { h, icon } = ctx.ui;
    const { fmtMB, fmtUptime, fill } = StudioUI;
    let rows = [], memory = {}, filter = "all", query = "", canManage = false;

    const tableHost = h("div");
    const notice = h("div");
    const search = h("input", { class: "input", placeholder: "Search services", "aria-label": "Search services",
      oninput: ctx.ui.debounce((e) => { query = e.target.value.toLowerCase(); paint(); }, 120) });
    const seg = h("div", { class: "segmented", role: "tablist" });
    const FILTERS = [["all", "All"], ["running", "Running"], ["stopped", "Stopped"]];
    const renderSeg = () => fill(seg, ...FILTERS.map(([k, t]) => h("button", { class: filter === k ? "active" : "", role: "tab", "aria-selected": String(filter === k), onclick: () => { filter = k; renderSeg(); paint(); } }, t)));
    renderSeg();
    el.append(
      h("div", { class: "bsp-page-head" }, h("div", null, h("h1", null, "Services"),
        h("div", { class: "lede" }, "Everything the appliance can run. Use Start & stop to change what runs; use this page to inspect, restart or read logs."))),
      h("div", { class: "toolbar" }, h("div", { class: "search" }, icon("search"), search), seg,
        h("button", { class: "btn ghost", onclick: () => refresh(true) }, icon("refresh", 16), "Refresh")),
      notice, tableHost);

    const STATE = { running: ["running", "Running"], starting: ["starting", "Starting"], unhealthy: ["unhealthy", "Unhealthy"], exited: ["exited", "Stopped"], missing: ["neutral", "Not created"] };
    const up = (r) => ["running", "starting", "unhealthy"].includes(r.state);

    const table = ctx.ui.table({
      rowKey: "name", pageSize: 50, initialSort: { key: "title", dir: 1 },
      empty: h("div", { class: "empty" }, h("div", { class: "ic" }, icon("cpu", 22)), h("h4", null, "No matching services")),
      onRowClick: (r) => (up(r) || r.state === "exited") && openLogs(r),
      columns: [
        { key: "title", label: "Service", sort: true, render: (r) => h("div", null, h("div", { style: { fontWeight: 600 } }, r.title),
          h("div", { class: "caption mono" }, r.name)) },
        { key: "state", label: "State", sort: true, render: (r) => { const [c, t] = STATE[r.state] || STATE.missing;
          return h("span", { class: `badge ${c}`, title: r.health || "" }, h("span", { class: "dot" }), t); } },
        { key: "started_at", label: "Uptime", sort: (r) => (r.started_at ? -new Date(r.started_at).getTime() : null), render: (r) => (up(r) ? fmtUptime(r.started_at) : "—") },
        { key: "mem", label: "Memory", num: true, sort: (r) => memory[r.name] || 0, render: (r) => (memory[r.name] ? fmtMB(memory[r.name] / 1048576)
          : up(r) ? h("span", { class: "skeleton", style: { display: "inline-block", width: "48px" } }) : h("span", { class: "muted" }, r.ram_estimate_mb ? `~${fmtMB(r.ram_estimate_mb)}` : "—")) },
        { key: "profile", label: "Profile", sort: true, render: (r) => h("span", { class: `chip ${r.control_plane ? "primary" : "outline"}` }, r.profile) },
        { key: "actions", label: "", class: "actions", render: actions },
      ],
    });
    tableHost.append(table.el);

    function actions(r) {
      const btns = [];
      if (r.endpoints.length) btns.push(h("button", { class: "btn ghost icon sm", title: "Connection strings", "aria-label": `Connection strings for ${r.title}`,
        onclick: () => ctx.ui.drawer({ title: `Connect to ${r.title}`, subtitle: "From notebooks or VS Code inside the appliance",
          body: StudioUI.endpointList(r.endpoints) }) }, icon("link", 15)));
      if (up(r) || r.state === "exited") btns.push(h("button", { class: "btn ghost icon sm", title: "Logs", "aria-label": `Logs for ${r.title}`, onclick: () => openLogs(r) }, icon("document", 15)));
      if (canManage && !r.control_plane) {
        if (up(r)) {
          btns.push(h("button", { class: "btn ghost icon sm", title: "Restart (recreate)", "aria-label": `Restart ${r.title}`, onclick: () => act(r, "restart") }, icon("refresh", 15)));
          btns.push(h("button", { class: "btn ghost icon sm", title: "Stop", "aria-label": `Stop ${r.title}`, onclick: () => act(r, "stop") }, icon("stop", 15)));
        } else btns.push(h("button", { class: "btn ghost icon sm", title: "Start", "aria-label": `Start ${r.title}`, onclick: () => act(r, "start") }, icon("play", 15)));
      }
      return h("div", { class: "row", style: { justifyContent: "flex-end", gap: "2px" } }, btns);
    }

    async function act(r, action) {
      const verbs = { start: "Start", stop: "Stop", restart: "Restart" };
      const doing = { start: "Starting", stop: "Stopping", restart: "Restarting" };
      const body = action === "stop"
        ? `Stop ${r.title}? Anything that depends on it will stop working until it starts again. To change your setup permanently, use Start & stop.`
        : action === "restart" ? `Recreate ${r.title} so it picks up current configuration and credentials? It will be unavailable for a moment.`
          : `Start ${r.title} and anything it needs?`;
      if (!(await ctx.confirm({ title: `${verbs[action]} ${r.title}`, body, okText: verbs[action], danger: action === "stop" }))) return;
      try {
        const { job } = await ctx.api(`/studio/api/services/${encodeURIComponent(r.name)}/${action}`, { method: "POST" });
        await ctx.job(job, { title: `${doing[action]} ${r.title}` });
      } catch (e) { if (e.status) ctx.toast(e.message, "error"); }
      refresh(true);
    }

    function openLogs(r) {
      let since = 0, follow = true, timer = null, busy = false;
      const pre = h("pre", { class: "codeblock", "aria-live": "off" });
      const followSw = h("input", { type: "checkbox", checked: true });
      followSw.onchange = () => { follow = followSw.checked; if (follow) tick(); };
      const tail = h("select", { class: "select", style: { width: "auto", height: "30px" }, "aria-label": "Lines" },
        [100, 300, 1000].map((n) => h("option", { value: n, selected: n === 300 || null }, `Last ${n}`)));
      tail.onchange = () => { since = 0; pre.textContent = ""; tick(); };
      const d = ctx.ui.drawer({ title: `${r.title} logs`, subtitle: r.name, wide: true,
        body: h("div", { class: "logs-drawer" }, h("div", { class: "row", style: { marginBottom: "10px" } },
          h("label", { class: "switch" }, followSw, h("span", { class: "track" }), "Follow"), h("div", { style: { flex: 1 } }), tail,
          h("button", { class: "btn ghost sm", onclick: () => ctx.copy(pre.textContent, "Logs") }, icon("copy", 14), "Copy")), pre),
        onClose: () => clearTimeout(timer) });
      async function tick() {
        clearTimeout(timer);
        if (busy || !document.body.contains(d.el)) return;
        busy = true;
        try {
          const res = await ctx.api(`/studio/api/services/${encodeURIComponent(r.name)}/logs?tail=${tail.value}&since=${since}`);
          const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 12;
          if (res.lines.length) pre.append(res.lines.join("\n") + "\n");
          else if (!since) pre.textContent = "No log output yet.";
          since = res.since || since;
          if (atBottom || !since) pre.scrollTop = pre.scrollHeight;
        } catch (e) { pre.append(`\n[${e.message}]\n`); } finally { busy = false; }
        if (follow) timer = setTimeout(tick, 2000);
      }
      tick();
    }

    function paint() {
      table.update(rows.filter((r) => (filter === "all" || (filter === "running" ? up(r) : !up(r)))
        && (!query || `${r.title} ${r.name} ${r.profile}`.toLowerCase().includes(query))));
    }

    async function refresh(withMemory) {
      try {
        const d = await ctx.api("/studio/api/services");
        rows = d.services; canManage = d.can_manage;
        fill(notice, d.docker ? "" : h("div", { class: "alert error", style: { marginBottom: "12px" } }, icon("error", 18),
          h("div", null, "Docker is not reachable from Studio — states below are unknown.")));
        paint();
        if (withMemory) { memory = await ctx.api("/studio/api/services/memory"); paint(); }
      } catch (e) { if (e.status) ctx.toast(e.message, "error"); }
    }
    await refresh(true);
    ctx.every(5000, () => refresh(false));
    ctx.every(30000, async () => { memory = await ctx.api("/studio/api/services/memory").catch(() => memory); paint(); });
    ctx.on("studio:job-done", () => refresh(true));
  },
});
