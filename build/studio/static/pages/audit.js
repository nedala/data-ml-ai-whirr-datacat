/* Audit log: who did what, when, from where (data/vault/audit.log). */
Studio.registerPage({
  id: "audit",
  async render(el, ctx) {
    const { h, icon } = ctx.ui;
    const FILTERS = [["", "All"], ["auth.", "Sign-in"], ["vault.", "Vault"], ["compose.", "Compose"], ["service.", "Services"],
      ["credential.", "Credentials"], ["user.", "Users"], ["backup.", "Backup"], ["certs.", "Certificates"], ["egress.", "Egress"], ["connector", "Connectors"]];
    let event = "", query = "";
    const KIND = (e) => (/failed|remove|reveal/.test(e) ? "warning" : /^auth\.login$|unlock|step_up$/.test(e) ? "info" : "primary");

    const search = h("input", { class: "input", placeholder: "Search user, event, detail or address", "aria-label": "Search audit log",
      oninput: ctx.ui.debounce((e) => { query = e.target.value; load(); }, 250) });
    const sel = h("select", { class: "select", style: { width: "auto" }, "aria-label": "Event type" }, FILTERS.map(([v, t]) => h("option", { value: v }, t)));
    sel.onchange = () => { event = sel.value; load(); };
    const table = ctx.ui.table({
      pageSize: 50,
      columns: [
        { key: "ts", label: "When", render: (r) => h("span", { title: r.ts }, r.ts ? `${ctx.ui.fmtTime(r.ts)}` : "—") },
        { key: "user", label: "User", render: (r) => r.user || h("span", { class: "muted" }, "system") },
        { key: "event", label: "Event", render: (r) => h("span", { class: `badge ${KIND(r.event)}` }, r.event) },
        { key: "detail", label: "Detail", render: (r) => h("span", { style: { wordBreak: "break-word" } }, r.detail) },
        { key: "peer", label: "From", render: (r) => h("code", null, r.peer || "") },
      ],
      empty: h("div", { class: "empty" }, h("div", { class: "ic" }, icon("document", 22)), h("h4", null, "No matching entries")),
    });
    el.append(
      h("div", { class: "bsp-page-head" }, h("div", null, h("h1", null, "Audit log"),
        h("div", { class: "lede" }, "Every sign-in, vault, credential, user and configuration change. Newest first."))),
      h("div", { class: "toolbar" }, h("div", { class: "search" }, icon("search"), search), sel,
        h("button", { class: "btn ghost", onclick: () => load() }, icon("refresh", 16), "Refresh")),
      table.el);

    async function load() {
      const q = new URLSearchParams({ q: query, event, limit: "2000" });
      try { table.update((await ctx.api(`/studio/api/audit?${q}`)).entries); } catch (e) { if (e.status) ctx.toast(e.message, "error"); }
    }
    await load();
    ctx.every(15000, load);
  },
});
