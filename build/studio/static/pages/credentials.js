/* Credentials: every catalog credential slot. Reveal and
   copy need a fresh master-passphrase confirmation (step-up); generating or
   setting a value rotates it live and recreates the containers that use it. */
Studio.registerPage({
  id: "credentials",
  async render(el, ctx) {
    const { h, icon } = ctx.ui;
    const { fill } = StudioUI;
    let data = null, query = "";

    const hostShared = h("div", { class: "section" });
    const hostMain = h("div");
    const hostInternal = h("div");
    const hostConn = h("div", { class: "section" });
    const hostOther = h("details", { class: "section" }); // filled below; declared early so el.append() can place it
    const search = h("input", { class: "input", placeholder: "Search credentials", "aria-label": "Search credentials",
      oninput: ctx.ui.debounce((e) => { query = e.target.value.toLowerCase(); paint(); }, 120) });
    el.append(
      h("div", { class: "bsp-page-head" }, h("div", null, h("h1", null, "Credentials"),
        h("div", { class: "lede" }, "Studio generates every service credential automatically the first time a service starts. Rotate any of them here — running services are updated for you."))),
      h("div", { class: "toolbar" }, h("div", { class: "search" }, icon("search"), search),
        h("span", { class: "caption" }, icon("shield", 14), " Values are encrypted in the vault (AES-256-GCM)",
          ctx.help("Values are encrypted at rest with a key Studio manages for this appliance. Rotation runs the service's live hook (e.g. ALTER ROLE) before the vault is updated, then recreates every running container that reads the value."))),
      hostShared, hostConn, hostMain, hostInternal, hostOther);

    // ── Connection secrets: names the Integration Canvas references as vault:NAME
    const connTable = ctx.ui.table({
      rowKey: (r) => r.name, pageSize: 100,
      empty: "No connection secrets yet — add the ones your Canvas connections reference (e.g. CARE_MSSQL_PASSWORD).",
      columns: [
        { key: "name", label: "Name", sort: true, render: (r) => h("code", null, r.name) },
        { key: "ref", label: "Use in a Canvas connection as", render: (r) => h("code", { class: "muted" }, `vault:${r.name}`) },
        { key: "updated", label: "Updated", render: (r) => h("span", { class: "rotated" }, r.updated ? ctx.ui.fmtAgo(r.updated) : "—") },
        { key: "actions", label: "", class: "actions", render: (r) => h("div", { class: "row", style: { justifyContent: "flex-end", gap: "4px" } },
          h("button", { class: "btn secondary sm", onclick: () => connDialog(r.name) }, icon("edit", 14), "Replace"),
          h("button", { class: "btn ghost icon sm", title: `Delete ${r.name}`, "aria-label": `Delete ${r.name}`, onclick: () => deleteConn(r.name) }, icon("delete", 15))) },
      ] });
    hostConn.append(h("div", { class: "section-head" },
      h("span", { class: "eyebrow" }, "Connection secrets (Integration Canvas)",
        ctx.help("Passwords and keys for the databases, buckets and APIs your Canvas connections use. A connection refers to one as vault:NAME. Values are write-only: they go to the vault and to the Canvas, never back to the browser.")),
      h("button", { class: "btn secondary sm", onclick: () => connDialog("") }, icon("add", 14), "Add secret")), connTable.el);

    async function refreshConn() {
      try { connTable.update((await ctx.api("/studio/api/connection-secrets")).secrets); } catch (e) { /* vault locked: the page shows the unlock card */ }
    }
    function connDialog(existing) {
      const name = h("input", { class: "input", value: existing, placeholder: "CARE_MSSQL_PASSWORD", disabled: !!existing,
        autocomplete: "off", spellcheck: false, "aria-label": "Secret name" });
      const value = h("textarea", { class: "input", rows: 3, autocomplete: "off", spellcheck: false, placeholder: "Paste the value (multi-line JSON keys are fine)", "aria-label": "Secret value" });
      const err = h("div", { class: "error", role: "alert" });
      const ok = h("button", { class: "btn primary" }, existing ? "Replace value" : "Save secret");
      const cancel = h("button", { class: "btn ghost" }, "Cancel");
      const m = ctx.ui.modal({ title: existing ? `Replace ${existing}` : "Add connection secret",
        body: h("div", { class: "stack" },
          h("label", { class: "field" }, h("span", { class: "label" }, "Name"), name,
            h("span", { class: "hint" }, "UPPER_CASE, as referenced in the connection (vault:NAME)")),
          h("label", { class: "field" }, h("span", { class: "label" }, "Value"), value), err),
        foot: [cancel, ok] });
      cancel.onclick = () => m.close();
      ok.onclick = () => ctx.ui.busy(ok, async () => {
        err.textContent = "";
        const n = (name.value || "").trim().toUpperCase();
        try {
          const r = await ctx.api(`/studio/api/connection-secrets/${encodeURIComponent(n)}`, { method: "PUT", body: { value: value.value } });
          m.close(); ctx.toast(`${n} saved`, "success"); refreshConn();
          if (r.job) ctx.job(r.job, { title: "Updating the Integration Canvas" });
        } catch (e) { err.textContent = e.message || "That didn't work"; }
      });
      setTimeout(() => (existing ? value : name).focus(), 80);
    }
    async function deleteConn(n) {
      if (!(await ctx.confirm({ title: `Delete ${n}?`, body: "Canvas connections that reference it will show “Needs setup” until it's added again.", okText: "Delete", danger: true }))) return;
      try {
        const r = await ctx.api(`/studio/api/connection-secrets/${encodeURIComponent(n)}`, { method: "DELETE" });
        ctx.toast(`${n} deleted`, "success"); refreshConn();
        if (r.job) ctx.job(r.job, { title: "Updating the Integration Canvas" });
      } catch (e) { if (e.status) ctx.toast(e.message, "error"); }
    }

    const reveal = (r) => async () => {
      try { return (await ctx.api("/studio/api/credentials/reveal", { body: { kind: r.kind, service: r.service, env: r.env } })).value; } catch (e) {
        if (e.status) ctx.toast(e.message, "error");
        return null;
      }
    };

    const columns = () => [
      { key: "service_title", label: "Service", sort: true, render: (r) => h("div", null,
        h("div", { style: { fontWeight: 600 } }, r.service_title),
        r.running ? h("span", { class: "badge running", style: { marginTop: "4px" } }, h("span", { class: "dot" }), "running") : null) },
      { key: "label", label: "Credential", sort: true, render: (r) => h("div", null, h("div", { class: "cred-label" }, r.label), h("div", { class: "cred-env" }, r.env),
        r.init_only ? h("div", { class: "rotated", title: "Read only when the service's workspace is first created" }, "Applies on first start; to change, reset the workspace") : null) },
      { key: "user", label: "Username", render: (r) => (r.user ? h("code", null, r.user) : h("span", { class: "muted" }, "—")) },
      { key: "secret", label: "Secret", render: (r) => (r.set ? ctx.ui.secret(null, { onReveal: reveal(r), label: r.label })
        : r.external ? h("span", { class: "badge pending", title: "A key you obtain (from IT / the platform owner) and set here" }, "not set — add yours")
          : h("span", { class: "badge pending", title: "Generated automatically when the service first starts" }, "not generated yet")) },
      { key: "rotated", label: "Last rotated", sort: (r) => r.rotated || "", render: (r) => h("span", { class: "rotated", title: r.rotated ? ctx.ui.fmtTime(r.rotated) : "" }, r.rotated ? ctx.ui.fmtAgo(r.rotated) : "—") },
      { key: "actions", label: "", class: "actions", render: (r) => h("div", { class: "row", style: { justifyContent: "flex-end", gap: "4px" } },
        r.external ? null : h("button", { class: "btn secondary sm", onclick: () => rotateDialog(r, "generate") }, icon("wand", 14), r.set ? "Rotate" : "Generate"),
        h("button", { class: "btn ghost icon sm", title: "Set a custom value", "aria-label": `Set custom value for ${r.label}`, onclick: () => rotateDialog(r, "custom") }, icon("edit", 15))) },
    ];

    const mainTable = ctx.ui.table({ columns: columns(), rowKey: (r) => `${r.service}.${r.env}`, pageSize: 100 });
    const internalTable = ctx.ui.table({ columns: columns(), rowKey: (r) => `${r.service}.${r.env}`, pageSize: 100 });
    const sharedTable = ctx.ui.table({ columns: columns(), rowKey: (r) => `${r.service}.${r.env}`, pageSize: 100 });
    // services this appliance doesn't run (other personas' components) — folded away
    const otherTable = ctx.ui.table({ columns: columns(), rowKey: (r) => `${r.service}.${r.env}`, pageSize: 100 });
    const otherCount = h("span");
    hostOther.append(h("summary", { class: "eyebrow", style: { cursor: "pointer" } },
      "Services not in this appliance", otherCount), h("div", { style: { marginTop: "10px" } }, otherTable.el));
    hostShared.append(h("div", { class: "section-head" }, h("span", { class: "eyebrow" }, "Enterprise keys (shared by every component)",
      ctx.help("Keys for BigQuery / Vertex AI, Microsoft 365, Atlan, SAP BRIM and Anthropic. Set once; Studio injects them into every service it starts. Empty = the in-appliance mock or local stand-in is used."))),
      sharedTable.el);
    hostMain.append(h("div", { class: "section-head" }, h("span", { class: "eyebrow" }, "Service credentials")), mainTable.el);
    hostInternal.append(h("details", { class: "section" }, h("summary", { class: "eyebrow", style: { cursor: "pointer" } }, "Internal keys (signing keys, inter-service secrets)"),
      h("div", { style: { marginTop: "10px" } }, internalTable.el)));

    function paint() {
      const match = (r) => !query || `${r.service_title} ${r.label} ${r.env} ${r.user}`.toLowerCase().includes(query);
      const rows = data.slots.filter(match);
      sharedTable.update(rows.filter((r) => r.shared));
      mainTable.update(rows.filter((r) => r.kind === "service" && !r.hidden && !r.shared && r.in_appliance));
      internalTable.update(rows.filter((r) => r.kind === "service" && r.hidden && !r.shared && r.in_appliance));
      const others = rows.filter((r) => r.kind === "service" && !r.shared && !r.in_appliance);
      otherTable.update(others);
      otherCount.textContent = others.length ? ` (${others.length})` : "";
      hostOther.hidden = !others.length;
    }

    async function rotateDialog(r, mode) {
      let impact = null;
      const impactBox = h("div", { class: "stack", style: { marginTop: "6px" } }, h("div", { class: "skeleton sk-line" }));
      const length = h("input", { type: "range", min: 16, max: 96, step: 4, value: Math.max(16, Math.min(96, r.length || 32)), "aria-label": "Length" });
      const lenVal = h("span", { class: "v" }, length.value);
      length.oninput = () => (lenVal.textContent = length.value);
      const symbols = h("input", { type: "checkbox", checked: true });
      const custom = r.multiline
        ? (() => { const input = h("textarea", { class: "input mono", rows: 10, spellcheck: false, autocomplete: "off",
            placeholder: '{"type": "service_account", ...}', style: { width: "100%", fontSize: "12px" } });
          return { input, el: h("div", { class: "field" }, h("label", null, "New value"), input, h("div", { class: "hint" }, r.hint || "Pasted verbatim")) }; })()
        : StudioUI.passwordField({ label: "New value", name: "custom", meter: !r.external, autocomplete: "new-password",
          hint: r.external ? (r.hint || "Stored verbatim in the vault") : "Avoid $, quotes and backslashes" });
      const body = h("div", null,
        h("p", { class: "muted", style: { marginBottom: "14px" } }, h("b", null, r.label), ` · ${r.service_title}`),
        mode === "generate"
          ? h("div", null,
            h("div", { class: "field" }, h("label", null, "Length"), h("div", { class: "range-row" }, length, lenVal)),
            h("label", { class: "switch", style: { marginBottom: "6px" } }, symbols, h("span", { class: "track" }), "Include symbols ( - _ )"))
          : custom.el,
        impactBox);
      const ok = h("button", { class: "btn primary" }, mode === "generate" ? (r.set ? "Rotate now" : "Generate") : "Save value");
      const cancel = h("button", { class: "btn ghost" }, "Cancel");
      const m = ctx.ui.modal({ title: mode === "generate" ? (r.set ? "Rotate credential" : "Generate credential") : "Set a custom value", body, foot: [cancel, ok] });
      cancel.onclick = () => m.close();
      try {
        impact = await ctx.api(`/studio/api/credentials/impact?service=${encodeURIComponent(r.service)}&env=${encodeURIComponent(r.env)}`);
        fill(impactBox, 
          impact.recreate.length
            ? h("div", { class: "alert info" }, icon("refresh", 18), h("div", null, h("div", { class: "title" }, "These running services will be recreated"),
              h("div", { class: "chips", style: { marginTop: "6px" } }, impact.recreate.map((s) => h("span", { class: "chip outline" }, s)))))
            : h("div", { class: "alert info" }, icon("info", 18), h("div", null, "Nothing using it is running — the new value applies the next time it starts.")),
          impact.hooks.length && impact.running ? h("div", { class: "caption" }, icon("check", 12), ` Applied live to ${r.service} first (${impact.hooks.join(", ")} plugin).`) : null,
          impact.warning ? h("div", { class: "alert warning", role: impact.strong ? "alert" : null }, icon("warning", 18),
            h("div", null, impact.strong ? h("div", { class: "title" }, "Read this first") : null, impact.warning)) : null);
        if (impact.generate_blocked && mode === "generate") { ok.disabled = true; ok.title = "Use “Set custom value” to record a password changed inside the app"; }
        if (impact.strong && !impact.generate_blocked) fill(ok, `Recreate ${impact.recreate.length} services`);
      } catch (e) { fill(impactBox, h("div", { class: "alert error" }, icon("error", 18), h("div", null, e.message))); }
      ok.onclick = async () => {
        const payload = { service: r.service, env: r.env };
        if (mode === "generate") Object.assign(payload, { length: Number(length.value), policy: symbols.checked ? "default" : "alnum" });
        else {
          if (!custom.input.value) { custom.input.focus(); return; }
          payload.value = custom.input.value;
        }
        try {
          const { job } = await ctx.ui.busy(ok, () => ctx.api("/studio/api/credentials/rotate", { body: payload }));
          m.close();
          const done = await ctx.job(job, { title: `Rotating ${r.label}` });
          if (done && done.state === "done") ctx.toast(`${r.label} rotated`, "success");
        } catch (e) { if (e.status) ctx.toast(e.message, "error"); }
        load();
      };
    }

    async function load() {
      refreshConn();
      try { data = await ctx.api("/studio/api/credentials"); paint(); } catch (e) {
        if (e.status === 423) fill(el, h("div", { class: "card" }, h("div", { class: "empty" }, h("div", { class: "ic" }, icon("lock", 22)),
          h("h4", null, "The vault is locked"), h("div", null, "Unlock it to see and manage credentials."),
          h("button", { class: "btn primary", style: { marginTop: "14px" }, onclick: async () => { if (await ctx.unlock()) ctx.navigate("credentials"); } }, icon("unlock", 16), "Unlock vault"))));
        else if (e.status) ctx.toast(e.message, "error");
      }
    }
    await load();
    ctx.on("studio:job-done", load);
  },
});
