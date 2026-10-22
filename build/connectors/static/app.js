/* Connectors status page — lists systems, mode, configuration and a connection test. */
(function () {
  "use strict";
  const { h, icon, toast, api: mkApi, busy } = window.BSP;
  const api = mkApi(location.pathname.replace(/\/?$/, "/").replace(/\/index\.html$/, "/"));
  const grid = document.getElementById("systems");
  const results = {};

  document.getElementById("docs-link").append(icon("document", 16), " API docs");
  const testAll = document.getElementById("test-all");
  testAll.append(icon("refresh", 16), " Test all");

  function modeBadge(mode) {
    if (mode === "live") return h("span", { class: "badge ok" }, h("span", { class: "dot" }), "live");
    if (mode === "mock") return h("span", { class: "badge info" }, h("span", { class: "dot" }), "mock");
    return h("span", { class: "badge error" }, mode);
  }

  function card(s) {
    const res = h("div", { class: "result", hidden: true });
    const btn = h("button", { class: "btn primary sm" }, icon("play", 14), " Test connection");
    btn.onclick = () => busy(btn, () => run(s.id, res));
    const envs = Object.entries(s.configured || {}).map(([k, v]) =>
      h("span", { class: "chip " + (v ? "set" : (s.required || []).includes(k) ? "req outline" : "outline"), title: v ? "set" : "not set" },
        v ? icon("check", 12) : null, k));
    const plug = s.plugin || {};
    const foot = h("div", { class: "sys-foot" }, btn);
    if (s.explore && plug.mounted) foot.append(h("a", { class: "btn secondary sm", href: s.url || s.explore, target: "_blank", rel: "noopener" }, icon("open-in-new", 14), s.url ? "Open Atlan" : " Explore"));
    if (plug.mounted) foot.append(h("a", { class: "btn ghost sm", href: "docs#/" + encodeURIComponent(tagFor(s.id)) }, "Endpoints"));
    const el = h("div", { class: "card sys-card" },
      h("div", { class: "sys-head" }, h("div", { class: "sys-ic" }, icon(s.icon || "apps", 20)),
        h("div", { style: { flex: 1, minWidth: 0 } }, h("div", { class: "sys-title" }, s.title), h("div", { class: "caption" }, s.blurb)),
        modeBadge(s.mode)),
      h("div", {}, h("div", { class: "eyebrow", style: { marginBottom: "6px" } }, "Configuration"),
        envs.length ? h("div", { class: "env-list" }, ...envs) : h("div", { class: "caption" }, "Uses bsp_common.gcp credentials")),
      plug.mounted ? h("div", { class: "caption" }, "Routes: ", (plug.prefixes || []).join(", "))
        : h("div", { class: "caption" }, plug.error ? "Plugin not mounted: " + plug.error : "No HTTP routes (SDK only)"),
      res, foot);
    results[s.id] = { res, btn };
    return el;
  }

  function tagFor(id) { return { m365: "m365 (Graph mock)", atlan: "atlan (mock)", powerbi: "powerbi (mock)" }[id] || id; }

  async function run(id, res) {
    try {
      const r = await api(`api/systems/${id}/test`, { method: "POST" });
      res.hidden = false;
      res.className = "result " + (r.ok ? "ok" : "bad");
      res.textContent = `${r.ok ? "OK" : "FAILED"} · ${r.mode} · ${r.ms} ms\n${r.detail}`;
    } catch (e) { toast(e.message || String(e), "error"); }
  }

  testAll.onclick = () => busy(testAll, async () => {
    await Promise.all(Object.entries(results).map(([id, { res }]) => run(id, res)));
  });

  async function load() {
    try {
      const d = await api("api/systems");
      grid.replaceChildren(...d.systems.map(card));
      document.getElementById("paths").replaceChildren(
        h("div", {}, "Fixtures (read-only seed): ", h("code", { class: "inline" }, d.fixtures)),
        h("div", { style: { marginTop: "4px" } }, "Mock state: ", h("code", { class: "inline" }, d.state)));
    } catch (e) { toast("Could not load systems: " + (e.message || e), "error"); }
  }
  load();
})();
