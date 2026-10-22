/* Users: local accounts (scrypt-hashed) and SSO role overrides. */
Studio.registerPage({
  id: "users",
  async render(el, ctx) {
    const { h, icon } = ctx.ui;
    const { initials, passwordField, fill } = StudioUI;
    let data = await ctx.api("/studio/api/users");
    const ROLE_HELP = {
      viewer: "Sees the Launchpad and opens tools that allow viewers.",
      analyst: "Opens every tool and can compose (start/stop) the appliance.",
      modeler: "Analyst plus access to modelling tools gated at the modeler level.",
      admin: "Everything: credentials, users, certificates, backups, connectors, egress.",
    };

    const addBtn = h("button", { class: "btn primary", onclick: () => addDialog() }, icon("add", 16), "Add user");
    const notes = h("div");
    const table = ctx.ui.table({
      rowKey: "name", pageSize: 50, initialSort: { key: "name", dir: 1 },
      columns: [
        { key: "name", label: "User", sort: true, render: (u) => h("div", { class: "row" }, h("span", { class: "avatar" }, initials(u.name)),
          h("div", null, h("div", { style: { fontWeight: 600 } }, u.name, u.name === data.me ? h("span", { class: "chip primary", style: { marginLeft: "8px", height: "20px" } }, "you") : null))) },
        { key: "source", label: "Sign-in", sort: true, render: (u) => h("span", { class: "chip outline" }, u.source === "sso" ? "Single sign-on" : "Local password") },
        { key: "role", label: "Role", sort: true, render: (u) => {
          const sel = h("select", { class: "select role-select", "aria-label": `Role for ${u.name}`, disabled: u.name === data.me || null },
            data.roles.map((r) => h("option", { value: r, selected: r === u.role || null }, r)));
          sel.onchange = async () => {
            if (!(await ctx.confirm({ title: `Make ${u.name} ${sel.value === "admin" ? "an" : "a"} ${sel.value}?`, body: ROLE_HELP[sel.value], okText: "Change role" }))) { sel.value = u.role; return; }
            try { await ctx.api(`/studio/api/users/${encodeURIComponent(u.name)}/role`, { body: { role: sel.value } }); ctx.toast("Role updated", "success"); load(); } catch (e) { sel.value = u.role; ctx.toast(e.message, "error"); }
          };
          return sel;
        } },
        { key: "last_login", label: "Last sign-in", sort: true, render: (u) => (u.last_login ? h("span", { title: ctx.ui.fmtTime(u.last_login) }, ctx.ui.fmtAgo(u.last_login)) : h("span", { class: "muted" }, "never")) },
        { key: "actions", label: "", class: "actions", render: (u) => {
          const more = h("button", { class: "btn ghost icon sm", "aria-label": `Actions for ${u.name}` }, icon("more", 16));
          more.onclick = () => ctx.ui.menu(more, [
            u.source === "local" ? { icon: "key", label: "Reset password", onClick: () => resetDialog(u) } : null,
            u.name !== data.me ? { icon: "delete", label: "Remove user", danger: true, onClick: () => remove(u) } : null,
          ]);
          return more;
        } },
      ],
      empty: h("div", { class: "empty" }, h("div", { class: "ic" }, icon("user-list", 22)), h("h4", null, "No users yet")),
    });

    el.append(
      h("div", { class: "bsp-page-head" }, h("div", null, h("h1", null, "Users"),
        h("div", { class: "lede" }, "Who can sign in to the appliance and what they can do. Roles are enforced at the front door for every tool."),
      ), h("div", { class: "page-actions" }, addBtn)),
      notes, table.el,
      h("div", { class: "section-head" }, h("span", { class: "eyebrow" }, "Roles", ctx.help("nginx asks Studio on every request whether the signed-in user's role meets the floor set for that tool (e.g. the object-store console needs admin)."))),
      h("div", { class: "grid cols-4" }, data.roles.map((r) => h("div", { class: "stat" }, h("div", { class: "label" }, r), h("div", { style: { fontSize: "13px", marginTop: "4px" } }, ROLE_HELP[r])))));

    function paintNotes() {
      fill(notes, 
        data.sso ? h("div", { class: "alert info", style: { marginBottom: "14px" } }, icon("info", 18), h("div", null,
          "Single sign-on is on. Entra ID users get ", h("b", null, "admin"), data.admin_group ? ` if they are in ${data.admin_group}` : "", ", otherwise ", h("b", null, "analyst"),
          ". Add an SSO user here to pin a different role.")) : null,
        !data.local_login ? h("div", { class: "alert warning", style: { marginBottom: "14px" } }, icon("warning", 18), h("div", null,
          "Password sign-in is disabled in this environment; local users can only sign in with STUDIO_BREAK_GLASS=1.")) : null);
    }

    function addDialog() {
      let source = data.sso && !data.local_login ? "sso" : "local";
      const name = h("input", { class: "input", id: "nu-name", placeholder: source === "sso" ? "name@example.com" : "jdoe", autocomplete: "off" });
      const role = h("select", { class: "select", id: "nu-role" }, data.roles.map((r) => h("option", { value: r, selected: r === "analyst" || null }, r)));
      const roleHint = h("div", { class: "hint" }, ROLE_HELP.analyst);
      role.onchange = () => (roleHint.textContent = ROLE_HELP[role.value]);
      const pw = passwordField({ label: "Initial password", name: "nu-pw", meter: true, min: 10, autocomplete: "new-password" });
      const gen = h("button", { type: "button", class: "btn ghost sm" }, icon("wand", 14), "Generate");
      gen.onclick = () => { pw.input.value = ctx.ui.genPassword(18, { symbolSet: "-_.~" }); pw.input.type = "text"; pw.update(); };
      const pwBlock = h("div", null, pw.el, h("div", { class: "row", style: { marginTop: "-8px", marginBottom: "10px" } }, gen,
        h("button", { type: "button", class: "btn ghost sm", onclick: () => pw.input.value && ctx.copy(pw.input.value, "Password") }, icon("copy", 14), "Copy")));
      const seg = h("div", { class: "segmented", style: { marginBottom: "14px" } });
      const paintSeg = () => {
        fill(seg, ...[["local", "Local password"], ["sso", "Single sign-on"]].filter(([k]) => k === "local" || data.sso).map(([k, t]) =>
          h("button", { type: "button", class: source === k ? "active" : "", onclick: () => { source = k; paintSeg(); } }, t)));
        pwBlock.style.display = source === "local" ? "" : "none";
        name.placeholder = source === "sso" ? "name@example.com" : "jdoe";
      };
      paintSeg();
      const ok = h("button", { class: "btn primary" }, "Add user");
      const cancel = h("button", { class: "btn ghost" }, "Cancel");
      const m = ctx.ui.modal({ title: "Add user", body: h("form", { onsubmit: (e) => { e.preventDefault(); ok.click(); } }, data.sso ? seg : null,
        h("div", { class: "field" }, h("label", { for: "nu-name" }, "Username"), name),
        h("div", { class: "field" }, h("label", { for: "nu-role" }, "Role"), role, roleHint), pwBlock), foot: [cancel, ok] });
      cancel.onclick = () => m.close();
      ok.onclick = async () => {
        try {
          await ctx.ui.busy(ok, () => ctx.api("/studio/api/users", { body: { name: name.value.trim(), role: role.value, source, password: source === "local" ? pw.input.value : null } }));
          m.close(); ctx.toast(`${name.value.trim()} added`, "success"); load();
        } catch (e) { ctx.toast(e.message, "error"); }
      };
    }

    async function resetDialog(u) {
      const pw = passwordField({ label: "New password", name: "rp", meter: true, min: 10, autocomplete: "new-password" });
      const ok = h("button", { class: "btn primary" }, "Reset password");
      const cancel = h("button", { class: "btn ghost" }, "Cancel");
      const gen = h("button", { type: "button", class: "btn ghost sm", onclick: () => { pw.input.value = ctx.ui.genPassword(18, { symbolSet: "-_.~" }); pw.input.type = "text"; pw.update(); ctx.copy(pw.input.value, "Password"); } }, icon("wand", 14), "Generate & copy");
      const m = ctx.ui.modal({ title: `Reset password for ${u.name}`, subtitle: "Their current sessions are signed out.", body: h("div", null, pw.el, gen), foot: [cancel, ok] });
      cancel.onclick = () => m.close();
      ok.onclick = async () => {
        try { await ctx.ui.busy(ok, () => ctx.api(`/studio/api/users/${encodeURIComponent(u.name)}/password`, { body: { password: pw.input.value } })); m.close(); ctx.toast("Password reset", "success"); load(); } catch (e) { ctx.toast(e.message, "error"); }
      };
    }

    async function remove(u) {
      if (!(await ctx.confirm({ title: `Remove ${u.name}?`, body: "They are signed out immediately and can no longer reach any tool.", danger: true, okText: "Remove" }))) return;
      try { await ctx.api(`/studio/api/users/${encodeURIComponent(u.name)}`, { method: "DELETE" }); ctx.toast(`${u.name} removed`, "success"); load(); } catch (e) { ctx.toast(e.message, "error"); }
    }

    async function load() { data = await ctx.api("/studio/api/users"); paintNotes(); table.update(data.users); }
    paintNotes();
    table.update(data.users);
  },
});
