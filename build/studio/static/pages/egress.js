/* Studio page: Egress policy (px-proxy allowlist, traffic, trust, tunnels).
   Backend: build/studio/plugins/egress.py (/studio/api/egress/*).
   Uses only the BSP UI kit (build/ui-kit). */
(function () {
  "use strict";
  const BASE = "/studio/api/egress";
  let dispose = null; // timers of the currently rendered instance

  const CSS = `
  .eg-kpis { margin-bottom: 20px; }
  .eg-kpis .stat { position: relative; overflow: hidden; }
  .eg-kpis .stat .spark { position: absolute; right: 12px; bottom: 10px; opacity: .9; }
  .eg-mode { display: flex; align-items: center; gap: 10px; }
  .eg-toolbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 14px; }
  .eg-toolbar .search { flex: 1 1 260px; max-width: 420px; }
  .eg-cats { display: flex; gap: 6px; flex-wrap: wrap; }
  .eg-cats .chip { cursor: pointer; }
  .eg-cats .chip.on { background: var(--bsp-primary-fiber); color: #fff; }
  .eg-group { margin-bottom: 14px; }
  .eg-group .card-head h4 { display: flex; gap: 8px; align-items: center; margin: 0; font-size: 14px; }
  .eg-rule { display: grid; grid-template-columns: minmax(200px, 1.1fr) minmax(120px, auto) 2fr auto; gap: 12px;
             align-items: center; padding: 9px 4px; border-top: 1px solid var(--bsp-border-subtle); }
  .eg-rule:first-of-type { border-top: 0; }
  .eg-rule .pat { font-family: var(--bsp-font-mono); font-size: 13px; word-break: break-all; }
  .eg-rule .why { color: var(--bsp-text-caption); font-size: 13px; }
  .eg-rule .meta { font-size: 11px; color: var(--bsp-text-caption); }
  .eg-rule .acts { display: flex; gap: 2px; opacity: .35; transition: opacity .15s; }
  .eg-rule:hover .acts, .eg-rule:focus-within .acts { opacity: 1; }
  .eg-rule.off .pat { text-decoration: line-through; color: var(--bsp-text-caption); }
  .eg-mono { font-family: var(--bsp-font-mono); font-size: 12.5px; }
  .eg-preview { margin-top: 8px; display: flex; flex-wrap: wrap; gap: 6px; max-height: 140px; overflow: auto; }
  .eg-tester { display: flex; gap: 8px; }
  .eg-tester .input { flex: 1; }
  .eg-result { margin-top: 10px; }
  .eg-live-head { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }
  .eg-dot-live { width: 8px; height: 8px; border-radius: 50%; background: var(--bsp-tints-green-100); display: inline-block;
                 animation: bsp-pulse 2s infinite; }
  .eg-dot-live.paused { background: var(--bsp-neutral-gray-mid); animation: none; }
  .eg-chain { width: 100%; height: auto; max-width: 860px; display: block; margin: 4px auto 0; }
  .eg-kv { display: grid; grid-template-columns: 140px 1fr; gap: 6px 14px; font-size: 13px; }
  .eg-kv dt { color: var(--bsp-text-caption); }
  .eg-kv dd { margin: 0; word-break: break-all; }
  .eg-fp { font-family: var(--bsp-font-mono); font-size: 11.5px; word-break: break-all; min-width: 0; }
  .eg-blocked-row .host { font-family: var(--bsp-font-mono); font-weight: 600; }
  @media (max-width: 900px) { .eg-rule { grid-template-columns: 1fr auto; } .eg-rule .why, .eg-rule .ports { grid-column: 1 / -1; } }
  `;

  function injectCss() {
    if (document.getElementById("eg-css")) return;
    const s = document.createElement("style");
    s.id = "eg-css";
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  // Same semantics as build/px-proxy/policy.py compile_pattern (preview only;
  // the server validates authoritatively).
  function patternToRegex(p) {
    p = (p || "").trim();
    if (!p) return null;
    if (p.startsWith("regex:")) { try { return new RegExp("^(?:" + p.slice(6) + ")$", "i"); } catch (e) { return null; } }
    p = p.toLowerCase().replace(/\.$/, "");
    const labels = p.split(".");
    const parts = labels.map((lbl, i) => {
      const last = i === labels.length - 1;
      if (lbl === "*" && i === 0) return "(?:[a-z0-9_-]+\\.)+";
      const rx = lbl === "*" ? "[a-z0-9_-]+" : lbl.split("*").map((x) => x.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("[a-z0-9_-]*");
      return rx + (last ? "" : "\\.");
    });
    return new RegExp("^" + parts.join("") + "$");
  }

  function fmtPorts(p) { return p === "any" || p == null ? "any port" : (Array.isArray(p) ? p.join(", ") : String(p)); }

  function sparkline(series, key, color, w = 120, hgt = 30) {
    const vals = (series || []).map((s) => s[key] || 0);
    if (!vals.length) return null;
    const max = Math.max(1, ...vals);
    const bw = w / vals.length;
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("width", w); svg.setAttribute("height", hgt); svg.setAttribute("class", "spark");
    svg.setAttribute("aria-hidden", "true");
    vals.forEach((v, i) => {
      const r = document.createElementNS(ns, "rect");
      const hh = Math.max(v ? 2 : 1, (v / max) * (hgt - 2));
      r.setAttribute("x", (i * bw + 0.5).toFixed(1)); r.setAttribute("y", (hgt - hh).toFixed(1));
      r.setAttribute("width", Math.max(1, bw - 1.5).toFixed(1)); r.setAttribute("height", hh.toFixed(1));
      r.setAttribute("rx", "1"); r.style.fill = v ? color : "var(--bsp-gray-matter-150)";
      svg.appendChild(r);
    });
    return svg;
  }

  function actionBadge(d) {
    const { h } = window.BSP;
    if (d.error === "client-tls-failed") return h("span", { class: "badge warning", title: d.detail || "" }, h("span", { class: "dot" }), "client TLS");
    if (d.error) return h("span", { class: "badge warning", title: d.error }, h("span", { class: "dot" }), "upstream error");
    if (d.action === "allow") return h("span", { class: "badge ok" }, h("span", { class: "dot" }), "allowed");
    if (d.action === "audit-allow") return h("span", { class: "badge warning" }, h("span", { class: "dot" }), "would block");
    return h("span", { class: "badge neutral" }, h("span", { class: "dot" }), "blocked");
  }

  // Trust-chain diagram (inline SVG; brand colours; no red).
  function chainDiagram(hasCorp, upstream) {
    const W = 860, H = 170;
    const box = (x, label, sub, fill, stroke, txt) => `
      <g><rect x="${x}" y="40" width="160" height="74" rx="8" fill="${fill}" stroke="${stroke}"/>
      <text x="${x + 80}" y="70" text-anchor="middle" font-size="14" font-weight="600" fill="${txt}">${label}</text>
      <text x="${x + 80}" y="92" text-anchor="middle" font-size="11.5" fill="${txt}" opacity=".8">${sub}</text></g>`;
    const arrow = (x1, x2, label) => `
      <line x1="${x1}" y1="77" x2="${x2 - 6}" y2="77" stroke="#828487" stroke-width="1.6" marker-end="url(#eg-arr)"/>
      <text x="${(x1 + x2) / 2}" y="66" text-anchor="middle" font-size="11" fill="#7f7f7d">${label}</text>`;
    const trust = (x, label) => `<text x="${x + 80}" y="140" text-anchor="middle" font-size="11" fill="#0b6e79">${label}</text>`;
    return `<svg class="eg-chain" viewBox="0 0 ${W} ${H}" role="img" aria-label="Trust chain: workloads trust the appliance egress CA; px-proxy verifies the internet with public roots${hasCorp ? " plus corporate roots" : ""}.">
      <defs><marker id="eg-arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="#828487"/></marker></defs>
      ${box(10, "Workloads", "Jupyter · VS Code · Superset…", "#f9f7fe", "#e5d7f0", "#1c2127")}
      ${arrow(170, 240, ":3128")}
      ${box(240, "px-proxy", "allowlist · audit log", "#0b6e79", "#0b6e79", "#ffffff")}
      ${arrow(400, 470, upstream ? "chained" : "direct")}
      ${box(470, upstream ? "Corporate proxy" : "Cloud egress", upstream ? (hasCorp ? "TLS inspection (re-signs)" : "upstream proxy") : "Cloud firewall / NAT", "#ffffff", "#d9dbdc", "#1c2127")}
      ${arrow(630, 690, "")}
      ${box(690, "Internet", "pypi · CRAN · HF · GitHub…", "#ebf3fe", "#6ebce9", "#1c2127")}
      ${trust(10, "trust: egress CA only")}
      ${trust(240, hasCorp ? "trusts: Mozilla roots + corporate" : "trusts: Mozilla roots")}
      ${trust(470, upstream ? "sees: allowlisted hosts only" : "FQDN rules (defence in depth)")}
    </svg>`;
  }

  Studio.registerPage({
    id: "egress",
    render(el, ctx) {
      if (dispose) dispose();
      injectCss();
      const W = window.BSP;
      const { h, icon } = W;
      const call = W.api("");
      const api = (p, o) => call(BASE + p, o);
      const st = { policy: null, version: "", source: "", stats: null, tab: "rules", q: "", cat: null,
                   live: { filter: "all", host: "", paused: false, timer: null }, seenHosts: new Set(), problems: [] };
      const disposers = [];

      const head = h("div", { class: "bsp-page-head" },
        h("div", null, h("div", { class: "eyebrow" }, "Configure"), h("h1", null, "Egress policy"),
          h("div", { class: "lede" }, "Every outbound connection from the appliance goes through px-proxy. Only hosts on this allowlist are reachable; everything else is refused and logged.")),
        h("div", { class: "eg-mode" }));
      const banners = h("div", { class: "stack", style: { marginBottom: "16px" } });
      const kpis = h("div", { class: "grid cols-4 eg-kpis" });
      const tabs = h("div", { class: "tabs", role: "tablist" });
      const body = h("div");
      el.replaceChildren(head, banners, kpis, tabs, body);

      const TABS = [["rules", "Rules", "shield"], ["blocked", "Recently blocked", "stop"], ["live", "Live traffic", "zap"],
                    ["test", "URL tester", "search"], ["trust", "Trust", "lock"], ["tunnels", "TCP tunnels", "plug"]];
      function renderTabs() {
        tabs.replaceChildren(...TABS.map(([id, label, ic]) => h("button", { class: st.tab === id ? "active" : "", role: "tab",
          "aria-selected": st.tab === id ? "true" : "false", onclick: () => { st.tab = id; renderTabs(); renderBody(); } }, icon(ic, 15), " ", label)));
      }

      // ── data ──────────────────────────────────────────────────────
      async function loadPolicy() {
        const p = await api("/policy");
        st.policy = p.policy || { mode: "enforce", rules: [], tcp_tunnels: [] };
        st.version = p.version; st.source = p.source; st.problems = p.problems || [];
        st.allowAudit = p.allow_audit !== false;
        st.readonly = !!p.readonly;
        st.policy.rules = st.policy.rules || [];
        st.policy.tcp_tunnels = st.policy.tcp_tunnels || [];
      }
      async function loadStats() {
        try { st.stats = await api("/stats?hours=24"); } catch (e) { st.stats = null; }
        (st.stats?.top_blocked || []).forEach((b) => st.seenHosts.add(b.host));
        (st.stats?.top_allowed || []).forEach((b) => st.seenHosts.add(b.host));
      }
      async function refresh(all = true) {
        try { await Promise.all([loadPolicy(), all ? loadStats() : null]); }
        catch (e) { W.toast(`Could not load egress policy: ${e.message}`, "error"); }
        renderHead(); renderBanners(); renderKpis(); renderBody();
      }

      // ── header: mode toggle ───────────────────────────────────────
      function renderHead() {
        const mode = st.policy?.mode || "enforce";
        const seg = h("div", { class: "segmented", role: "group", "aria-label": "Egress mode" },
          ["enforce", "audit"].map((m) => h("button", { class: mode === m ? "active" : "", "aria-pressed": mode === m ? "true" : "false",
            disabled: (m === "audit" && !st.allowAudit) || st.readonly || null,
            title: m === "audit" && !st.allowAudit ? "Audit mode is disabled in this environment" : "",
            onclick: () => setMode(m) }, icon(m === "enforce" ? "lock" : "visibility", 14), m === "enforce" ? "Enforce" : "Audit")));
        head.querySelector(".eg-mode").replaceChildren(
          h("span", { class: "caption" }, "Mode"), seg,
          h("button", { class: "btn primary", disabled: st.readonly || null, onclick: () => openRuleDrawer() }, icon("add"), "Add rule"));
      }
      async function setMode(m) {
        if (m === st.policy.mode) return;
        const ok = await W.confirm(m === "audit" ? {
          title: "Switch to audit mode?", danger: true, okText: "Switch to audit",
          body: h("div", null, h("p", null, "Audit mode lets ", h("strong", null, "every"), " outbound connection through and only logs what would have been blocked."),
            h("p", { class: "muted" }, "Use it for a short discovery window, then return to Enforce and allow what you need from “Recently blocked”.")) }
          : { title: "Switch to enforce mode?", okText: "Enforce allowlist",
              body: "Connections to hosts that are not on the allowlist will be refused immediately." });
        if (!ok) return;
        try { await api("/mode", { method: "PUT", body: { mode: m } }); W.toast(`Egress is now in ${m} mode`, "success"); refresh(); }
        catch (e) { W.toast(e.message, "error"); }
      }

      function renderBanners() {
        const out = [];
        if (st.source === "file") out.push(h("div", { class: "alert error" }, icon("error"), h("div", null,
          h("div", { class: "title" }, "px-proxy is not reachable"),
          "All egress is blocked while the proxy is down. Edits are saved to data/egress/allowlist.yaml and applied when it starts.")));
        if (st.readonly) out.push(h("div", { class: "alert info" }, icon("lock"), h("div", null,
          h("div", { class: "title" }, "Managed by GitOps"),
          "In this environment the allowlist comes from the infrastructure repository (Helm values → ConfigMap). Propose changes there; this page is read-only.")));
        if (st.policy?.mode === "audit") out.push(h("div", { class: "alert warning" }, icon("warning"), h("div", null,
          h("div", { class: "title" }, "Audit mode — nothing is being blocked"),
          "Connections that are not allowlisted are let through and logged as “would block”. Switch back to Enforce when done.")));
        if (st.problems?.length) out.push(h("div", { class: "alert warning" }, icon("warning"), h("div", null,
          h("div", { class: "title" }, `${st.problems.length} rule problem(s) — those rules are ignored`),
          h("ul", { style: { margin: "4px 0 0 18px" } }, st.problems.slice(0, 6).map((p) => h("li", null, p))))));
        banners.replaceChildren(...out);
      }

      function renderKpis() {
        const s = st.stats || {};
        const rules = st.policy?.rules || [];
        const cats = new Set(rules.map((r) => r.category || "Custom"));
        const stat = (label, value, delta, spark) => h("div", { class: "stat" }, h("div", { class: "label" }, label),
          h("div", { class: "value" }, value), h("div", { class: "delta" }, delta), spark);
        kpis.replaceChildren(
          stat("Allowed · 24h", (s.allowed ?? "–").toLocaleString(), s.bytes != null ? `${W.fmtBytes(s.bytes)} transferred` : "",
            sparkline(s.series, "allowed", "var(--bsp-primary-fiber)")),
          stat(st.policy?.mode === "audit" ? "Would block · 24h" : "Blocked · 24h",
            ((st.policy?.mode === "audit" ? s.would_block : s.blocked) ?? "–").toLocaleString(),
            s.top_blocked?.length ? `${s.top_blocked.length} distinct host(s)` : "nothing blocked",
            sparkline(s.series, "blocked", "var(--bsp-neutral-charcoal)")),
          stat("Rules", rules.length, `${cats.size} categories · ${(st.policy?.tcp_tunnels || []).length} tunnel(s)`),
          stat("TLS issues · 24h", ((s.client_tls_failures || 0) + (s.upstream_errors || 0)).toLocaleString(),
            `${s.client_tls_failures || 0} client trust · ${s.upstream_errors || 0} upstream`));
      }

      // ── tab bodies ────────────────────────────────────────────────
      function stopLive() { if (st.live.timer) { clearInterval(st.live.timer); st.live.timer = null; } }
      function renderBody() {
        stopLive();
        const f = { rules: renderRules, blocked: renderBlocked, live: renderLive, test: renderTester, trust: renderTrust, tunnels: renderTunnels }[st.tab];
        body.replaceChildren(h("div", { class: "fade-in" }, f()));
      }

      function renderRules() {
        const wrap = h("div");
        const rules = st.policy?.rules || [];
        const cats = [...new Set(rules.map((r) => r.category || "Custom"))].sort();
        const search = h("input", { class: "input", type: "search", placeholder: "Search hosts, categories, reasons…", value: st.q,
          "aria-label": "Search rules", oninput: W.debounce((e) => { st.q = e.target.value; draw(); }, 120) });
        const catBar = h("div", { class: "eg-cats" });
        const groups = h("div");
        wrap.append(h("div", { class: "eg-toolbar" }, h("div", { class: "search" }, icon("search"), search), catBar), groups);
        function draw() {
          catBar.replaceChildren(h("span", { class: `chip ${st.cat ? "outline" : "on"}`, onclick: () => { st.cat = null; draw(); } }, "All"),
            ...cats.map((c) => h("span", { class: `chip ${st.cat === c ? "on" : "outline"}`, onclick: () => { st.cat = st.cat === c ? null : c; draw(); } },
              c, h("span", { class: "muted" }, String(rules.filter((r) => (r.category || "Custom") === c).length)))));
          const q = st.q.trim().toLowerCase();
          let rx = null;
          if (q && /^[a-z0-9.-]+\.[a-z]{2,}$/.test(q)) rx = q; // looks like a hostname → also match by rule
          const shown = rules.filter((r) => (!st.cat || (r.category || "Custom") === st.cat) &&
            (!q || [r.pattern, r.category, r.reason, r.added_by].join(" ").toLowerCase().includes(q) ||
              (rx && patternToRegex(r.pattern)?.test(rx))));
          const byCat = {};
          shown.forEach((r) => (byCat[r.category || "Custom"] = byCat[r.category || "Custom"] || []).push(r));
          const names = Object.keys(byCat).sort((a, b) => (a === "DataPlatform" ? -1 : b === "DataPlatform" ? 1 : a.localeCompare(b)));
          if (!names.length) {
            groups.replaceChildren(h("div", { class: "card" }, h("div", { class: "empty" }, h("div", { class: "ic" }, icon("search")),
              h("h4", null, q ? `No rule matches “${st.q}”` : "No rules yet"),
              h("p", null, q && rx ? h("span", null, `${rx} would be blocked. `, h("button", { class: "btn secondary sm", onclick: () => openRuleDrawer({ pattern: rx }) }, icon("add", 14), `Allow ${rx}`)) : "Add a rule to allow a host."))));
            return;
          }
          groups.replaceChildren(...names.map((c) => h("div", { class: "card eg-group" },
            h("div", { class: "card-head" }, h("h4", null, icon("globe", 16), c, h("span", { class: "chip" }, String(byCat[c].length))),
              h("button", { class: "btn ghost sm", onclick: () => openRuleDrawer({ category: c }) }, icon("add", 14), "Add")),
            byCat[c].map((r) => h("div", { class: `eg-rule${r.enabled === false ? " off" : ""}` },
              h("div", null, h("div", { class: "pat" }, r.pattern),
                h("div", { class: "meta" }, [r.added_by ? `by ${r.added_by}` : null, r.added_at ? W.fmtAgo(r.added_at) : null].filter(Boolean).join(" · "))),
              h("div", { class: "row wrap ports" }, h("span", { class: "chip outline" }, fmtPorts(r.ports)),
                r.tls === "passthrough" ? h("span", { class: "chip auto", title: "Not decrypted (certificate-pinning clients)" }, "TLS passthrough") : null),
              h("div", { class: "why" }, r.reason || h("span", { class: "muted" }, "—")),
              h("div", { class: "acts" },
                h("button", { class: "btn ghost icon sm", title: "Edit", "aria-label": `Edit ${r.pattern}`, onclick: () => openRuleDrawer(r, true) }, icon("edit", 14)),
                h("button", { class: "btn ghost icon sm", title: "Remove", "aria-label": `Remove ${r.pattern}`, onclick: () => removeRule(r) }, icon("delete", 14))))))));
        }
        draw();
        return wrap;
      }

      async function removeRule(r) {
        const ok = await W.confirm({ title: `Remove ${r.pattern}?`, danger: true, okText: "Remove rule",
          body: h("p", null, "New connections to hosts matching ", h("code", null, r.pattern), " will be refused immediately.") });
        if (!ok) return;
        try { await api(`/rules?pattern=${encodeURIComponent(r.pattern)}`, { method: "DELETE" }); W.toast(`Removed ${r.pattern}`, "success"); refresh(false); }
        catch (e) { W.toast(e.message, "error"); }
      }

      // ── add / edit rule drawer with live validation + preview ────
      function openRuleDrawer(init = {}, editing = false) {
        const cats = [...new Set((st.policy?.rules || []).map((r) => r.category).filter(Boolean))].sort();
        const pat = h("input", { class: "input mono", value: init.pattern || "", placeholder: "pypi.org  ·  *.example.com  ·  cdn-*.example.net  ·  regex:…", autocomplete: "off", spellcheck: "false" });
        const patHint = h("div", { class: "hint" }, "Exact host, *.domain (any sub-domain, not the apex), a * inside one label, or regex:<python-regex>.");
        const preview = h("div", { class: "eg-preview" });
        const cat = h("input", { class: "input", value: init.category || "", placeholder: "e.g. PyPI, Google Cloud, DataPlatform", list: "eg-cats" });
        const dl = h("datalist", { id: "eg-cats" }, cats.map((c) => h("option", { value: c })));
        const reason = h("textarea", { class: "textarea", rows: "3", placeholder: "Why is this needed? Who asked? (shown to other admins)" }, init.reason || "");
        const ports = h("input", { class: "input mono", value: init.ports == null ? "443" : fmtPorts(init.ports).replace("any port", "any"), placeholder: "443  ·  80, 443  ·  any" });
        const tls = h("select", { class: "select" }, h("option", { value: "intercept" }, "Inspect (default)"),
          h("option", { value: "passthrough", selected: init.tls === "passthrough" || null }, "Passthrough — do not decrypt (pinned clients)"));
        const err = h("div", { class: "error", style: { display: "none" } });
        const save = h("button", { class: "btn primary" }, icon("check"), editing ? "Save rule" : "Add rule");
        const cancel = h("button", { class: "btn ghost" }, "Cancel");
        let valid = !!init.pattern;
        const check = W.debounce(async () => {
          const p = pat.value.trim();
          if (!p) { valid = false; pat.classList.remove("invalid"); preview.replaceChildren(); return; }
          const hosts = [...st.seenHosts].filter(Boolean);
          try {
            const r = await api("/validate", { method: "POST", body: { pattern: p, hosts } });
            valid = r.ok;
            pat.classList.toggle("invalid", !r.ok);
            err.style.display = r.ok ? "none" : "block";
            err.textContent = r.error || "";
            const rx = patternToRegex(p);
            const examples = rx ? ["", "www.", "api.", "a.b."].map((pre) => pre + p.replace(/^\*\./, "").replace(/\*/g, "x"))
              .filter((x) => !x.startsWith("regex:") && rx.test(x)) : [];
            preview.replaceChildren(...[
              r.ok ? h("span", { class: "caption" }, r.matches.length ? `Matches ${r.matches.length} host(s) seen in the last 24h:` : "Matches no host seen in the last 24h.") : null,
              ...(r.matches || []).slice(0, 40).map((x) => h("span", { class: "chip primary eg-mono" }, x)),
              r.ok && !r.matches.length && examples.length ? h("span", { class: "caption" }, ` e.g. ${[...new Set(examples)].slice(0, 3).join(", ")}`) : null].filter(Boolean));
          } catch (e) { valid = true; }
        }, 220);
        pat.addEventListener("input", check);
        const form = h("form", { class: "stack lg", onsubmit: (e) => { e.preventDefault(); save.click(); } },
          h("div", { class: "field" }, h("label", null, "Host pattern"), pat, err, patHint, preview),
          h("div", { class: "grid cols-2" },
            h("div", { class: "field" }, h("label", null, "Category"), cat, dl),
            h("div", { class: "field" }, h("label", null, "Ports"), ports, h("div", { class: "hint" }, "Comma-separated, or “any”."))),
          h("div", { class: "field" }, h("label", null, "Reason"), reason),
          h("div", { class: "field" }, h("label", null, "TLS handling"), tls,
            h("div", { class: "hint" }, "Passthrough skips inspection for clients that pin certificates. The host is still allowlisted by SNI.")),
          h("div", { class: "alert info" }, icon("info"), h("div", null, "Changes apply within seconds — px-proxy hot-reloads the allowlist. No restarts.")));
        const d = W.drawer({ title: editing ? `Edit rule` : "Allow a host", subtitle: editing ? init.pattern : "Add an egress allowlist rule", body: form, foot: [cancel, save] });
        cancel.onclick = () => d.close();
        save.onclick = () => W.busy(save, async () => {
          const p = pat.value.trim();
          if (!p) { W.toast("Enter a host pattern", "error"); return; }
          if (!reason.value.trim()) { W.toast("Give a reason — it is shown to other admins and in the audit log", "error"); reason.focus(); return; }
          if (!valid) { W.toast("Fix the pattern first", "error"); return; }
          const pv = ports.value.trim().toLowerCase();
          const body = { pattern: p, category: cat.value.trim() || "Custom", reason: reason.value.trim(), tls: tls.value,
                         ports: pv === "any" || pv === "*" ? "any" : pv.split(/[\s,]+/).filter(Boolean).map(Number) };
          if (editing && init.pattern && init.pattern !== p) body.replace = init.pattern;
          try { await api("/rules", { method: "POST", body }); W.toast(`${editing ? "Saved" : "Allowed"} ${p}`, "success"); d.close(); refresh(false); }
          catch (e) { W.toast(e.message, "error"); }
        });
        if (init.pattern) check();
      }

      // ── recently blocked ──────────────────────────────────────────
      function renderBlocked() {
        const wrap = h("div", { class: "card" });
        wrap.append(h("div", { class: "card-head" }, h("div", null, h("h3", null, "Recently blocked"),
          h("div", { class: "caption" }, "Hosts refused in the last 24 hours, most frequent first. One click adds an allow rule.")),
          h("button", { class: "btn ghost sm", onclick: async () => { await loadStats(); renderKpis(); renderBody(); } }, icon("refresh", 14), "Refresh")));
        const items = st.stats?.top_blocked || [];
        if (!st.stats) { wrap.append(h("div", { class: "empty" }, "Traffic statistics are unavailable while px-proxy is down.")); return wrap; }
        const t = W.table({
          rowKey: (r) => `${r.host}:${r.port}`, pageSize: 25, initialSort: { key: "count", dir: -1 },
          empty: h("div", { class: "empty" }, h("div", { class: "ic" }, icon("tick-circle")), h("h4", null, "Nothing blocked"), "No connection was refused in the last 24 hours."),
          columns: [
            { key: "host", label: "Host", sort: true, render: (r) => h("span", { class: "eg-mono", style: { fontWeight: 600 } }, r.host) },
            { key: "port", label: "Port", num: true, sort: true },
            { key: "count", label: "Attempts", num: true, sort: true },
            { key: "reason", label: "Why", render: (r) => h("span", { class: "chip outline" }, (r.reason || "").replace(/-/g, " ")) },
            { key: "clients", label: "From", render: (r) => (r.clients || []).join(", ") },
            { key: "last", label: "Last seen", sort: true, render: (r) => W.fmtAgo(r.last) },
            { key: "act", label: "", class: "actions", render: (r) => h("button", { class: "btn secondary sm", onclick: () => quickAllow(r) }, icon("check", 14), "Allow") },
          ], rows: items,
        });
        wrap.append(t.el);
        return wrap;
      }

      async function quickAllow(r) {
        const parts = (r.host || "").split(".");
        const wildcard = parts.length > 2 ? "*." + parts.slice(1).join(".") : null;
        const radios = [h("input", { type: "radio", name: "eg-scope", value: r.host, checked: true })];
        if (wildcard) radios.push(h("input", { type: "radio", name: "eg-scope", value: wildcard }));
        const scope = h("div", { class: "stack" },
          h("label", { class: "radio" }, radios[0], h("span", { class: "eg-mono" }, r.host), h("span", { class: "caption" }, "this host only")),
          wildcard ? h("label", { class: "radio" }, radios[1], h("span", { class: "eg-mono" }, wildcard), h("span", { class: "caption" }, "all sub-domains")) : null);
        const portNote = r.reason === "port-not-allowed" ? h("div", { class: "alert info" }, icon("info"), h("div", null, `The host is already allowed, but not on port ${r.port}. The new rule allows port ${r.port}.`)) : null;
        const reason = await W.prompt({ title: "Allow this host?", label: "Reason (required)", placeholder: "e.g. partner API used by the billing pipeline",
          okText: "Allow", body: h("div", { class: "stack lg", style: { marginBottom: "12px" } }, scope, portNote) });
        if (reason == null) return;
        if (!reason.trim()) { W.toast("A reason is required", "error"); return; }
        const pattern = (radios.find((x) => x.checked) || {}).value || r.host;
        try {
          await api("/rules", { method: "POST", body: { pattern, category: "Custom", reason: reason.trim(),
            ports: r.port && ![80, 443].includes(r.port) ? [r.port] : [443] } });
          W.toast(`Allowed ${pattern}`, "success");
          await refresh();
        } catch (e) { W.toast(e.message, "error"); }
      }

      // ── live tail ────────────────────────────────────────────────
      function renderLive() {
        const wrap = h("div", { class: "card" });
        const dot = h("span", { class: `eg-dot-live${st.live.paused ? " paused" : ""}` });
        const filter = h("div", { class: "segmented" }, [["all", "All"], ["true", "Allowed"], ["false", "Blocked"]].map(([v, l]) =>
          h("button", { class: st.live.filter === v ? "active" : "", onclick: () => { st.live.filter = v; renderBody(); } }, l)));
        const host = h("input", { class: "input", type: "search", placeholder: "Filter by host…", value: st.live.host,
          oninput: W.debounce((e) => { st.live.host = e.target.value; poll(); }, 250) });
        const pause = h("button", { class: "btn ghost sm", onclick: () => { st.live.paused = !st.live.paused; renderBody(); } },
          icon(st.live.paused ? "play" : "pause", 14), st.live.paused ? "Resume" : "Pause");
        const t = W.table({ pageSize: 50, rowKey: (r) => `${r.t}-${r.host}-${r.client}-${r.path || ""}`,
          empty: h("div", { class: "empty" }, "No traffic yet."),
          columns: [
            { key: "ts", label: "Time", render: (r) => h("span", { class: "eg-mono", title: r.ts }, new Date(r.t * 1000).toLocaleTimeString()) },
            { key: "client", label: "Client", render: (r) => r.client_name || r.client || "" },
            { key: "method", label: "", render: (r) => h("span", { class: "chip outline" }, r.method || r.via || "") },
            { key: "host", label: "Destination", render: (r) => h("span", { class: "eg-mono", title: r.path || "" }, `${r.host}${r.port && ![80, 443].includes(r.port) ? ":" + r.port : ""}${r.path ? r.path.slice(0, 60) : ""}`) },
            { key: "action", label: "Decision", render: actionBadge },
            { key: "rule", label: "Rule", render: (r) => r.rule ? h("span", { class: "eg-mono" }, r.rule) : h("span", { class: "muted" }, (r.reason || "").replace(/-/g, " ")) },
            { key: "status", label: "Status", num: true },
            { key: "bytes", label: "Bytes", num: true, render: (r) => W.fmtBytes((r.bytes_in || 0) + (r.bytes_out || 0)) },
            { key: "ms", label: "ms", num: true },
          ], rows: [] });
        wrap.append(h("div", { class: "eg-live-head" }, dot, h("strong", null, "Live decisions"), filter,
          h("div", { class: "search", style: { flex: "1 1 220px", maxWidth: "320px" } }, icon("search"), host), h("span", { style: { flex: 1 } }), pause), t.el);
        async function poll() {
          if (st.live.paused) return;
          const q = new URLSearchParams({ limit: "300" });
          if (st.live.filter !== "all") q.set("allowed", st.live.filter);
          if (st.live.host) q.set("host", st.live.host);
          try { const r = await api(`/decisions?${q}`); t.update(r.items || []); (r.items || []).forEach((d) => st.seenHosts.add(d.host)); }
          catch (e) { /* transient */ }
        }
        poll();
        st.live.timer = setInterval(poll, 3000);
        return wrap;
      }

      // ── URL tester ───────────────────────────────────────────────
      function renderTester() {
        const input = h("input", { class: "input mono", placeholder: "https://pypi.org/simple/pandas/  ·  sql01.dataplatform.com:1433", autocomplete: "off" });
        const out = h("div", { class: "eg-result" });
        const btn = h("button", { class: "btn primary" }, icon("search"), "Test");
        const run = () => W.busy(btn, async () => {
          if (!input.value.trim()) return;
          try {
            const r = await api("/test", { method: "POST", body: { url: input.value.trim() } });
            const kind = r.matched ? "success" : r.allowed ? "warning" : "info";
            out.replaceChildren(h("div", { class: `alert ${kind}` }, icon(r.matched ? "tick-circle" : r.allowed ? "warning" : "stop"),
              h("div", { style: { flex: 1 } }, h("div", { class: "title" }, r.matched ? `Allowed — ${r.host}:${r.port}` : r.allowed ? `Allowed only because of audit mode — ${r.host}` : `Blocked — ${r.host}:${r.port}`),
                h("div", null, r.explanation || ""),
                r.rule_detail ? h("div", { class: "caption", style: { marginTop: "4px" } }, `Rule ${r.rule_detail.pattern} · ${r.rule_detail.category} · ${fmtPorts(r.rule_detail.ports)} · ${r.rule_detail.reason || ""}`) : null),
              !r.matched ? h("button", { class: "btn secondary sm", onclick: () => openRuleDrawer({ pattern: r.host, ports: [r.port] }) }, icon("add", 14), "Allow…") : null));
          } catch (e) { out.replaceChildren(h("div", { class: "alert error" }, icon("error"), h("div", null, e.message))); }
        });
        btn.onclick = run;
        input.onkeydown = (e) => { if (e.key === "Enter") run(); };
        return h("div", { class: "card" }, h("div", { class: "card-head" }, h("div", null, h("h3", null, "Would this be allowed?"),
          h("div", { class: "caption" }, "Checks a URL or host:port against the live allowlist — nothing is fetched."))),
          h("div", { class: "eg-tester" }, input, btn), out);
      }

      // ── trust ────────────────────────────────────────────────────
      function renderTrust() {
        const wrap = h("div", { class: "stack lg" });
        const loading = h("div", { class: "card" }, h("div", { class: "skeleton", style: { height: "120px" } }));
        wrap.append(loading);
        api("/trust").then((t) => {
          const ca = t.egress_ca || {};
          const corp = t.corporate || [];
          const certRow = (c) => h("div", { class: "eg-rule", style: { gridTemplateColumns: "minmax(200px,1.2fr) auto 2fr" } },
            h("div", null, h("div", { style: { fontWeight: 600 } }, c.cn), h("div", { class: "meta" }, c.source || "")),
            h("span", { class: `badge ${c.expired ? "error" : c.days_left < 30 ? "warning" : "ok"}` }, c.expired ? "expired" : `${c.days_left} days left`),
            h("div", { class: "eg-fp", title: c.subject }, c.sha256));
          wrap.replaceChildren(
            ...(t.warnings || []).map((w) => h("div", { class: "alert warning" }, icon("warning"), h("div", null, w))),
            h("div", { class: "card" }, h("div", { class: "card-head" }, h("div", null, h("h3", null, "How trust works"),
              h("div", { class: "caption" }, "Workloads only ever trust the appliance egress CA. Corporate roots stay inside px-proxy, so rotating them never touches a workload."))),
              h("div", { html: chainDiagram(corp.length > 0, !!t.upstream_proxy) })),
            h("div", { class: "grid cols-2" },
              h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", null, "Appliance egress CA"),
                h("a", { class: "btn secondary sm", href: `${BASE}/ca.pem`, download: "bsp-appliance-egress-ca.pem" }, icon("download", 14), "Download")),
                h("dl", { class: "eg-kv" },
                  h("dt", null, "Subject"), h("dd", null, ca.subject || ""),
                  h("dt", null, "Valid until"), h("dd", null, `${(ca.not_after || "").slice(0, 10)} (${ca.days_left} days)`),
                  h("dt", null, "SHA-256"), h("dd", { class: "eg-fp" }, ca.sha256 || ""),
                  h("dt", null, "Distributed as"), h("dd", null, h("code", null, "/trust/ca-bundle.pem"), ", ", h("code", null, "/trust/egress-ca.pem"))),
                h("p", { class: "caption", style: { marginTop: "10px" } }, "Import it on your laptop only if you browse through px-proxy directly. Workloads are configured automatically.")),
              h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", null, "Upstream"), h("span", { class: "chip" }, t.env || "")),
                h("dl", { class: "eg-kv" },
                  h("dt", null, "Route"), h("dd", null, t.upstream_proxy ? `chained via ${t.upstream_proxy}` : "direct (cloud egress / firewall)"),
                  h("dt", null, "Verifies with"), h("dd", null, `Mozilla roots + ${corp.length} corporate root(s)`),
                  h("dt", null, "Corporate dir"), h("dd", null, h("code", null, t.corporate_dir || "")),
                  t.corporate_reloaded_at ? [h("dt", null, "Reloaded"), h("dd", null, W.fmtAgo(t.corporate_reloaded_at))] : null))),
            h("div", { class: "card" }, h("div", { class: "card-head" }, h("div", null, h("h3", null, "Corporate root CAs"),
              h("div", { class: "caption" }, "Extracted from the host by ", h("code", null, "make corporate-ca"), " into build/certs/corporate/. Picked up within 30 s — no restart."))),
              corp.length ? corp.map(certRow) : h("div", { class: "empty" }, h("div", { class: "ic" }, icon("shield")), h("h4", null, "No corporate roots loaded"),
                t.upstream_proxy ? "Behind a TLS-inspecting corporate proxy run make corporate-ca, otherwise upstream TLS verification fails." : "Not needed on networks without TLS interception.")));
        }).catch((e) => loading.replaceChildren(h("div", { class: "alert error" }, icon("error"), h("div", null, e.message))));
        return wrap;
      }

      // ── tunnels ──────────────────────────────────────────────────
      function renderTunnels() {
        const wrap = h("div", { class: "card" });
        const add = h("button", { class: "btn primary sm", onclick: () => editTunnel() }, icon("add", 14), "Add tunnel");
        wrap.append(h("div", { class: "card-head" }, h("div", null, h("h3", null, "TCP tunnels"),
          h("div", { class: "caption" }, "For non-HTTP egress (databases over TCP, SSH). Clients connect to ", h("code", null, "px-proxy:<listen port>"),
            "; traffic is forwarded only while the target is allowed by a rule.")), add));
        const holder = h("div");
        wrap.append(holder);
        const draw = (list) => {
          const t = W.table({ rowKey: "name", pageSize: 50, empty: h("div", { class: "empty" }, "No tunnels. Most egress is HTTPS and needs none."),
            columns: [
              { key: "name", label: "Name", render: (r) => h("strong", null, r.name) },
              { key: "listen_port", label: "Connect to", render: (r) => h("span", { class: "eg-mono" }, `px-proxy:${r.listen_port}`) },
              { key: "target", label: "Target", render: (r) => h("span", { class: "eg-mono" }, `${r.target_host}:${r.target_port}`) },
              { key: "state", label: "State", render: (r) => r.error ? h("span", { class: "badge error", title: r.error }, "error")
                  : r.target_allowed === false ? h("span", { class: "badge warning", title: "Add a rule for the target host + port" }, "target not allowed")
                  : r.listening ? h("span", { class: "badge running" }, h("span", { class: "dot" }), `listening · ${r.active_connections || 0} open`)
                  : h("span", { class: "badge neutral" }, "pending") },
              { key: "reason", label: "Reason" },
              { key: "a", label: "", class: "actions", render: (r) => h("div", { class: "row" },
                  h("button", { class: "btn ghost icon sm", "aria-label": "Edit", onclick: () => editTunnel(r) }, icon("edit", 14)),
                  h("button", { class: "btn ghost icon sm", "aria-label": "Remove", onclick: () => removeTunnel(r) }, icon("delete", 14))) },
            ], rows: list });
          holder.replaceChildren(t.el);
        };
        draw(st.policy?.tcp_tunnels || []);
        api("/tunnels").then((r) => draw(r.tunnels || [])).catch(() => {});
        return wrap;
      }
      async function saveTunnels(list) {
        await api("/tunnels", { method: "PUT", body: { tcp_tunnels: list.map(({ name, listen_port, target_host, target_port, reason }) => ({ name, listen_port, target_host, target_port, reason })) } });
        await refresh(false);
      }
      function editTunnel(init) {
        const f = (label, input, hint) => h("div", { class: "field" }, h("label", null, label), input, hint ? h("div", { class: "hint" }, hint) : null);
        const name = h("input", { class: "input", value: init?.name || "", placeholder: "dwh-sql" });
        const lp = h("input", { class: "input mono", type: "number", min: "1025", max: "65535", value: init?.listen_port || "", placeholder: "14330" });
        const th = h("input", { class: "input mono", value: init?.target_host || "", placeholder: "sql01.dataplatform.com" });
        const tp = h("input", { class: "input mono", type: "number", min: "1", max: "65535", value: init?.target_port || "", placeholder: "1433" });
        const reason = h("input", { class: "input", value: init?.reason || "", placeholder: "On-prem DWH for the reserving models" });
        const save = h("button", { class: "btn primary" }, "Save tunnel");
        const cancel = h("button", { class: "btn ghost" }, "Cancel");
        const d = W.drawer({ title: init ? `Edit tunnel ${init.name}` : "Add TCP tunnel", body: h("div", { class: "stack lg" },
          f("Name", name), h("div", { class: "grid cols-2" }, f("Listen port", lp, "1025–65535 (px-proxy is non-root)"), f("Target port", tp)),
          f("Target host", th, "Must also be allowed by a rule (host + port), e.g. *.dataplatform.com with ports “any”."), f("Reason", reason),
          h("div", { class: "alert info" }, icon("info"), h("div", null, "Clients then use host ", h("code", null, "px-proxy"), " and the listen port. "))),
          foot: [cancel, save] });
        cancel.onclick = () => d.close();
        save.onclick = () => W.busy(save, async () => {
          const t = { name: name.value.trim(), listen_port: Number(lp.value), target_host: th.value.trim(), target_port: Number(tp.value), reason: reason.value.trim() };
          const list = (st.policy.tcp_tunnels || []).filter((x) => x.name !== (init?.name || t.name));
          list.push(t);
          try { await saveTunnels(list); d.close(); W.toast(`Tunnel ${t.name} saved`, "success"); } catch (e) { W.toast(e.message, "error"); }
        });
      }
      async function removeTunnel(r) {
        if (!(await W.confirm({ title: `Remove tunnel ${r.name}?`, danger: true, okText: "Remove", body: `px-proxy stops listening on ${r.listen_port}.` }))) return;
        try { await saveTunnels((st.policy.tcp_tunnels || []).filter((x) => x.name !== r.name)); W.toast(`Removed ${r.name}`, "success"); } catch (e) { W.toast(e.message, "error"); }
      }

      renderTabs();
      renderHead();
      refresh();
      const statsTimer = setInterval(async () => { if (document.hidden) return; await loadStats(); renderKpis(); }, 30000);
      disposers.push(() => clearInterval(statsTimer), stopLive);
      dispose = () => { disposers.forEach((f) => f()); dispose = null; };
    },
    onLeave() { if (dispose) dispose(); },
  });
})();
