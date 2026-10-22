/* Backup & restore: the whole appliance configuration as one encrypted
   .bspconfig file (vault, selection, users, plugin data, package manifests,
   egress allowlist). */
Studio.registerPage({
  id: "backup",
  async render(el, ctx) {
    const { h, icon } = ctx.ui;
    const { passwordField, strength, fill } = StudioUI;
    const SECTION_INFO = {
      vault: ["key", "Vault secrets", "Service credentials and connector secrets"],
      users: ["user-list", "Users", "Accounts and roles (password hashes included)"],
      selection: ["wand", "Start & stop selection", "Role and capabilities"],
      plugins: ["plug", "Plugin settings", "Connectors, egress and other plugin data"],
      packages: ["package", "Package manifests", "Pinned Python packages (restored through the workspace)"],
      egress: ["shield", "Egress allowlist", "Outbound hosts the proxy allows"],
    };

    // ── export ──────────────────────────────────────────────────────────
    const pass = passwordField({ label: "Export passphrase", name: "exp", meter: true, min: 12, autocomplete: "new-password",
      hint: "At least 12 characters. You'll need it to import the file — Studio cannot recover it." });
    const pass2 = passwordField({ label: "Confirm passphrase", name: "exp2", autocomplete: "new-password" });
    const expErr = h("div", { class: "error", role: "alert" });
    const expBtn = h("button", { class: "btn primary", type: "submit" }, icon("download", 16), "Download backup");
    const exportForm = h("form", null, pass.el, pass2.el, expErr, h("div", { class: "row", style: { justifyContent: "flex-end" } }, expBtn));
    exportForm.onsubmit = async (e) => {
      e.preventDefault();
      expErr.textContent = "";
      if (pass.input.value.length < 12) { expErr.textContent = "Use at least 12 characters"; return; }
      if (strength(pass.input.value) < 2) { expErr.textContent = "Pick a stronger passphrase"; return; }
      if (pass.input.value !== pass2.input.value) { expErr.textContent = "Passphrases don't match"; return; }
      try {
        const res = await ctx.ui.busy(expBtn, () => ctx.api("/studio/api/backup/export", { body: { passphrase: pass.input.value }, raw: true }));
        const name = (/filename="([^"]+)"/.exec(res.headers.get("content-disposition") || "") || [])[1] || "appliance.bspconfig";
        const url = URL.createObjectURL(await res.blob());
        const a = h("a", { href: url, download: name });
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 2000);
        pass.input.value = pass2.input.value = ""; pass.update();
        ctx.toast(`Saved ${name}`, "success");
      } catch (ex) { if (ex.status) expErr.textContent = ex.message; }
    };

    // ── import ──────────────────────────────────────────────────────────
    const importBody = h("div");
    function resetImport() {
      fill(importBody, ctx.ui.dropzone({ multiple: false, label: "Drop a .bspconfig file here, or click to choose", onFiles: (fs) => fs[0] && askPassphrase(fs[0]) }),
        h("div", { class: "caption", style: { marginTop: "10px" } }, "You'll see exactly what changes before anything is applied."));
    }

    async function askPassphrase(file) {
      const phrase = await ctx.ui.prompt({ title: "Decrypt backup", label: `Passphrase for ${file.name}`, type: "password", okText: "Preview changes" });
      if (!phrase) return;
      const fd = new FormData();
      fd.append("file", file); fd.append("passphrase", phrase);
      fill(importBody, h("div", { class: "skeleton sk-line" }), h("div", { class: "skeleton sk-line", style: { width: "60%" } }));
      try { showPreview(file, await ctx.api("/studio/api/backup/preview", { body: fd })); } catch (e) {
        if (e.status) ctx.toast(e.message, "error");
        resetImport();
      }
    }

    function diffList(items, label) {
      return items.length ? h("div", null, h("div", { class: "caption" }, `${label} (${items.length})`),
        h("ul", { class: "diff-list" }, items.slice(0, 40).map((k) => h("li", null, h("code", null, k))), items.length > 40 ? h("li", null, `…and ${items.length - 40} more`) : null)) : null;
    }

    function showPreview(file, { token, diff }) {
      const checks = {};
      const v = diff.vault, u = diff.users;
      const summary = {
        vault: v.added.length + v.changed.length + v.removed.length ? `${v.added.length} added · ${v.changed.length} changed · ${v.removed.length} removed` : "identical",
        users: u.added.length + u.changed.length + u.removed.length ? `${u.added.length} added · ${u.changed.length} role changes · ${u.removed.length} removed` : "identical",
        selection: (diff.selection.incoming.personas || []).length || (diff.selection.incoming.capabilities || []).length
          ? `${(diff.selection.incoming.personas || []).join(" + ") || "custom"} · ${(diff.selection.incoming.capabilities || []).length} capabilities` : "empty",
        plugins: diff.plugins.length ? diff.plugins.join(", ") : "none in file",
        packages: diff.packages.length ? diff.packages.join(", ") : "none in file",
        egress: diff.egress.present ? (diff.egress.changed ? "differs from current" : "identical") : "none in file",
      };
      const available = { vault: true, users: true, selection: true, plugins: diff.plugins.length > 0, packages: diff.packages.length > 0, egress: diff.egress.present };
      const rows = Object.entries(SECTION_INFO).map(([k, [ic, title, desc]]) => {
        const cb = h("input", { type: "checkbox", checked: available[k] || null, disabled: !available[k] || null, "aria-label": title });
        checks[k] = cb;
        return h("label", { class: "import-section" }, h("span", { class: "checkbox" }, cb),
          h("div", { class: "body" }, h("div", { class: "row" }, icon(ic, 15), h("b", null, title), h("span", { class: "caption" }, `· ${summary[k]}`)),
            h("div", { class: "caption" }, desc),
            k === "vault" ? h("div", null, diffList(v.added, "Added"), diffList(v.changed, "Changed"), diffList(v.removed, "Removed")) : null,
            k === "users" ? h("div", null, diffList(u.added, "Added"), diffList(u.changed, "Role changes"), diffList(u.removed, "Removed"),
              !u.keeps_you ? h("div", { class: "caption" }, "You're not in this backup — your account will be kept.") : null) : null));
      });
      const apply = h("button", { class: "btn primary" }, icon("upload", 16), "Import selected");
      const cancel = h("button", { class: "btn ghost", onclick: resetImport }, "Cancel");
      apply.onclick = async () => {
        const sections = Object.keys(checks).filter((k) => checks[k].checked);
        if (!sections.length) return ctx.toast("Choose at least one section", "error");
        let confirmOverwrite = false;
        if (sections.includes("vault") && diff.vault_has_data) {
          confirmOverwrite = await ctx.confirm({ title: "Replace the vault's secrets?", danger: true, okText: "Replace secrets",
            body: "Every credential in this appliance's vault is replaced by the backup's. Running services keep their current passwords until they are recreated — re-apply your setup afterwards." });
          if (!confirmOverwrite) return;
        }
        try {
          const r = await ctx.ui.busy(apply, () => ctx.api("/studio/api/backup/apply", { body: { token, sections, confirm_overwrite: confirmOverwrite } }));
          ctx.toast(`Imported: ${r.applied.join(", ")}`, "success");
          fill(importBody, h("div", { class: "alert success" }, icon("tick-circle", 18), h("div", null,
            h("div", { class: "title" }, "Configuration imported"), `Restored ${r.applied.join(", ")} from ${file.name}.`,
            h("div", { class: "row", style: { marginTop: "10px" } },
              h("button", { class: "btn primary sm", onclick: () => ctx.navigate("compose") }, icon("wand", 14), "Re-apply composer selection"),
              h("button", { class: "btn ghost sm", onclick: resetImport }, "Import another")))));
        } catch (e) { if (e.status) ctx.toast(e.message, "error"); }
      };
      const meta = diff.meta || {};
      fill(importBody, 
        h("div", { class: "alert info", style: { marginBottom: "10px" } }, icon("info", 18), h("div", null, h("b", null, file.name),
          ` · created ${meta.created ? ctx.ui.fmtTime(meta.created) : "unknown"}${meta.by ? ` by ${meta.by}` : ""}${meta.appliance_env ? ` · ${meta.appliance_env}` : ""}`)),
        h("div", null, rows), h("div", { class: "row", style: { justifyContent: "flex-end", marginTop: "12px" } }, cancel, apply));
    }
    resetImport();

    el.append(
      h("div", { class: "bsp-page-head" }, h("div", null, h("h1", null, "Backup & restore"),
        h("div", { class: "lede" }, "Move an appliance to a new machine, or keep a safe copy of its configuration. One encrypted file holds everything."))),
      h("div", { class: "grid cols-2", style: { alignItems: "start" } },
        h("div", { class: "card" }, h("div", { class: "card-head" }, h("div", null, h("h3", null, "Export configuration"),
          h("div", { class: "caption" }, "Encrypted with your passphrase (scrypt + AES-256-GCM)"))),
          h("div", { class: "chips", style: { marginBottom: "16px" } }, Object.values(SECTION_INFO).map(([ic, t]) => h("span", { class: "chip" }, icon(ic, 12), t))),
          exportForm),
        h("div", { class: "card" }, h("div", { class: "card-head" }, h("div", null, h("h3", null, "Import configuration"),
          h("div", { class: "caption" }, "Preview, choose sections, then apply"))), importBody)),
      h("div", { class: "caption", style: { marginTop: "16px" } }, icon("shield", 12),
        " Exporting and importing ask for your vault master passphrase. Data volumes (tables, notebooks) are not part of the file — back up the appliance data root (data/<appliance>) for those."));
  },
});
