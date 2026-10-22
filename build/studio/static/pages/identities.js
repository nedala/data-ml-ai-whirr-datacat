/* Cloud identities: named Google Cloud credentials (service-account keys and
   personal gcloud sign-ins) kept in the vault, tested per capability and
   assigned per consumer (registry: build/studio/gcp_consumers.yaml).
   The browser only ever sees metadata — never a key or refresh token. */
Studio.registerPage({
  id: "identities",
  async render(el, ctx) {
    const { h, icon } = ctx.ui;
    const { fill } = StudioUI;
    let state = null;
    let draft = {};               // consumer key → identity id (unsaved edits)

    const TYPE = { service_account: ["Service account", "primary", "server"], authorized_user: ["User (personal)", "warning", "user"] };
    const VERDICT = { ok: ["ok", "tick-circle"], partial: ["warning", "warning"], failed: ["error", "error"],
      unsupported: ["neutral", "stop"], untested: ["neutral", "clock"] };
    const CAP_TITLE = { token: "Sign-in", bigquery: "BigQuery", bq_write: "BQ write", gcs: "Cloud Storage", gcs_write: "GCS write",
      vertex: "Vertex AI", artifact_registry: "Artifact Registry", cloud_run: "Cloud Run" };

    const applyHost = h("div");
    const govHost = h("div");
    const idsHost = h("div", { class: "section" });
    const asgHost = h("div", { class: "section" });
    el.append(
      h("div", { class: "bsp-page-head" },
        h("div", null, h("h1", null, "Cloud identities"),
          h("div", { class: "lede" }, "The Google Cloud credentials this appliance uses — service-account keys or a personal sign-in — tested per capability and assigned per component.")),
        h("div", { class: "page-actions row", style: { gap: "8px" } },
          h("button", { class: "btn secondary", onclick: () => addDialog() }, icon("upload", 15), "Add key file"),
          h("button", { class: "btn primary", onclick: () => googleDialog() }, icon("globe", 15), "Sign in with Google"))),
      applyHost, govHost, idsHost, asgHost);

    // ── helpers ──────────────────────────────────────────────────────
    const typeBadge = (t) => { const [l, c, ic] = TYPE[t] || [t, "neutral", "key"]; return h("span", { class: `badge ${c}` }, icon(ic, 12), l); };
    const verdictChip = (label, v, title) => {
      const [cls, ic] = VERDICT[v] || VERDICT.untested;
      return h("span", { class: `gid-chip ${cls}`, title: title || `${label}: ${v || "not tested"}` }, icon(ic, 12), label);
    };
    const idName = (id) => ((state.identities.find((i) => i.id === id) || {}).name || "");
    const consumerTitle = (k) => ((state.consumers.find((c) => c.key === k) || {}).title || k);
    const personalAssigned = () => state.identities.filter((i) => i.type === "authorized_user" && state.consumers.some((c) => c.effective === i.id));

    // ── pending Apply bar ────────────────────────────────────────────
    function paintApply() {
      const imp = state.impact || {};
      if (!imp.pending) { fill(applyHost); return; }
      const n = imp.recreate.length;
      const btn = h("button", { class: "btn primary sm", onclick: () => apply(btn) }, icon("sync", 14), n ? `Apply — recreate ${n}` : "Apply");
      fill(applyHost, h("div", { class: "actionbar gid-apply" }, icon("info", 18),
        h("div", { style: { flex: 1, minWidth: 0 } },
          h("div", { class: "ab-title" }, "Changes not applied to running services yet"),
          h("div", { class: "caption", style: { color: "inherit", opacity: .85 } }, n
            ? `Recreates ${imp.recreate.join(", ")} so they pick up the new identities. Running notebooks and queries in them are interrupted.`
            : "Nothing that uses Google Cloud is running — the new identities apply the next time services start.")),
        btn));
    }

    async function apply(btn) {
      try {
        const r = await ctx.ui.busy(btn, () => ctx.api("/studio/api/gcp/apply", { method: "POST" }));
        if (r.job) await ctx.job(r.job, { title: "Applying cloud identities" }); else ctx.toast("Saved — applies on the next start", "success");
      } catch (e) { if (e.status) ctx.toast(e.message, "error"); }
      load();
    }

    // ── governance note ──────────────────────────────────────────────
    function paintGov() {
      const personal = personalAssigned();
      if (!personal.length) { fill(govHost); return; }
      fill(govHost, h("div", { class: "alert warning", style: { marginTop: "4px" } }, icon("warning", 18), h("div", null,
        h("div", { class: "title" }, "A personal Google identity is in use"),
        personal.map((p) => h("div", null, "Everything assigned to ", h("b", null, p.name), " acts as ",
          h("b", null, p.principal || "your Google account"), " in Google Cloud — queries, storage access and model calls are attributed to that person and limited by their permissions. ")),
        h("div", { class: "caption", style: { marginTop: "4px" } }, "Fine for a single-user development appliance; use a service account for shared appliances and anything scheduled."))));
    }

    // ── identities table ─────────────────────────────────────────────
    const table = ctx.ui.table({
      rowKey: (r) => r.id, pageSize: 50,
      empty: h("div", { class: "empty" }, h("div", { class: "ic" }, icon("cloud", 22)), h("h4", null, "No cloud identities yet"),
        h("div", null, "Sign in with Google, or add a service-account key / gcloud application_default_credentials.json file.")),
      columns: [
        { key: "name", label: "Identity", sort: true, width: "17%", render: (r) => h("div", null,
          h("div", { style: { fontWeight: 600 } }, r.name),
          h("div", { class: "caption" }, r.source === "google-login" ? "Signed in from Studio" : r.source === "GCP_SA_JSON" ? "Imported from GCP_SA_JSON" : "Key file",
            r.added_at ? ` · added ${ctx.ui.fmtAgo(r.added_at)}` : "")) },
        { key: "type", label: "Type", render: (r) => typeBadge(r.type) },
        { key: "principal", label: "Acts as", width: "23%", render: (r) => h("div", { class: "gid-principal" },
          r.principal ? h("code", null, r.principal) : h("span", { class: "muted" }, "unknown until tested"),
          h("div", { style: { marginTop: "4px" } }, projectChip("project", r.project, (a) => projectPicker(a, { identityId: r.id, current: r.project,
            title: `Default project · ${r.name}`, onPick: (id) => setIdentityProject(r, id) }), { title: "Default project — click to change" }))) },
        { key: "last_test", label: "Last test", width: "32%", render: (r) => {
          const t = r.last_test;
          if (!t) return h("span", { class: "muted" }, "not tested");
          return h("div", null, h("div", { class: "chips" },
            Object.entries(t.capabilities || {}).map(([k, v]) => verdictChip(CAP_TITLE[k] || k, v))),
          h("div", { class: "caption", style: { marginTop: "4px" }, title: ctx.ui.fmtTime(t.at) }, `${ctx.ui.fmtAgo(t.at)}${t.by ? ` · ${t.by}` : ""}`));
        } },
        { key: "access", label: "Who can act as it", render: (r) => accessChip(r) },
        { key: "assigned", label: "Used by", render: (r) => r.assigned.length
          ? h("span", { class: "chip outline", title: r.assigned.map(consumerTitle).join(", ") }, `${r.assigned.length} component${r.assigned.length > 1 ? "s" : ""}`)
          : h("span", { class: "muted" }, "—") },
        { key: "actions", label: "", class: "actions", render: (r) => h("div", { class: "row", style: { justifyContent: "flex-end", gap: "4px", flexWrap: "nowrap" } },
          h("button", { class: "btn secondary sm", onclick: () => testDrawer(r) }, icon("play", 14), "Test"),
          h("button", { class: "btn ghost icon sm", title: `Who may act as ${r.name}`, "aria-label": `Access for ${r.name}`, onclick: () => accessDialog(r) }, icon("users", 15)),
          h("button", { class: "btn ghost icon sm", title: `Edit ${r.name} (default project, known projects)`, "aria-label": `Edit ${r.name}`, onclick: () => editIdentity(r) }, icon("edit", 15)),
          h("button", { class: "btn ghost icon sm", title: `Delete ${r.name}`, "aria-label": `Delete ${r.name}`, onclick: () => remove(r) }, icon("delete", 15))) },
      ] });
    idsHost.append(h("div", { class: "section-head" }, h("span", { class: "eyebrow" }, "Identities",
      ctx.help("Each identity is a Google credential stored encrypted in the vault. Service-account keys come from IAM; personal sign-ins come from gcloud auth application-default login (or the Sign in with Google button). Studio never shows the key again — only who it acts as and what it can reach.")),
    h("span", { class: "caption" }, icon("shield", 13), " Encrypted in the vault · never sent to the browser")), table.el);

    async function remove(r) {
      const used = r.assigned.length;
      const ok = await ctx.confirm({ title: `Delete ${r.name}?`, danger: true, okText: used ? "Unassign and delete" : "Delete",
        body: h("div", null, h("p", null, "The credential is removed from the vault. It is not revoked in Google Cloud — delete the key in IAM (or sign out with gcloud auth application-default revoke) if it should stop working everywhere."),
          used ? h("div", { class: "alert warning", style: { marginTop: "10px" } }, icon("warning", 18), h("div", null, `Used by ${r.assigned.map(consumerTitle).join(", ")} — they fall back to their parent consumer, then to GCP_SA_JSON.`)) : null) });
      if (!ok) return;
      try {
        await ctx.api(`/studio/api/gcp/identities/${encodeURIComponent(r.id)}${used ? "?force=true" : ""}`, { method: "DELETE" });
        ctx.toast(`${r.name} deleted`, "success"); load();
      } catch (e) { if (e.status) ctx.toast(e.message, "error"); }
    }

    // ── test drawer ──────────────────────────────────────────────────
    function resultRow(r) {
      return h("div", { class: `gid-result ${r.ok ? "ok" : "failed"}` },
        h("span", { class: "st" }, icon(r.ok ? "tick-circle" : "error", 16)),
        h("div", { style: { minWidth: 0, flex: 1 } },
          h("div", { class: "row between", style: { gap: "8px" } }, h("b", null, r.label),
            h("span", { class: "caption" }, r.status ? `HTTP ${r.status}` : "", r.ms ? ` · ${r.ms} ms` : "")),
          h("div", { class: "caption gid-detail" }, r.detail),
          r.hint ? h("div", { class: "gid-hint" }, icon("bulb", 13), h("span", null, r.hint)) : null,
          r.permission ? h("div", { style: { marginTop: "4px" } }, h("code", { class: "gid-perm" }, r.permission)) : null));
    }

    function renderResult(box, t, keys) {
      const cons = Object.entries(t.consumers || {}).filter(([k]) => !keys || keys.includes(k));
      fill(box,
        h("div", { class: "caption", style: { marginBottom: "8px" } }, `As `, h("b", null, t.principal || "this identity"),
          ` · data `, (t.data_projects || [t.project]).map((p, i) => [i ? ", " : "", h("code", null, p)]),
          t.exec_project && t.exec_project !== t.project ? [` · jobs in `, h("code", null, t.exec_project)] : null, ` · ${t.region}`),
        cons.length ? h("div", { class: "chips", style: { marginBottom: "12px" } }, cons.map(([k, v]) => verdictChip(consumerTitle(k), v.verdict, v.why))) : null,
        h("div", { class: "stack", style: { gap: "8px" } }, t.results.map(resultRow)));
    }

    function testDrawer(ident, onlyConsumer) {
      const bucket = h("input", { class: "input", placeholder: "my-bucket (optional)", autocomplete: "off", spellcheck: false, value: (ident.last_test && ident.last_test.bucket) || "" });
      const dataset = h("input", { class: "input", placeholder: "sandbox (optional)", autocomplete: "off", spellcheck: false, value: (ident.last_test && ident.last_test.dataset) || "" });
      const out = h("div", { style: { marginTop: "18px" } });
      const run = h("button", { class: "btn primary" }, icon("play", 15), "Run test");
      const keys = onlyConsumer ? [onlyConsumer] : null;
      const d = ctx.ui.drawer({ title: onlyConsumer ? `Test ${consumerTitle(onlyConsumer)}` : `Test ${ident.name}`, wide: true,
        subtitle: onlyConsumer ? `With ${ident.name} — exactly what this component needs` : "Live calls from Studio through the egress proxy (10 s limit each)",
        body: h("div", null,
          h("div", { class: "grid cols-2" },
            h("label", { class: "field" }, h("span", { class: "label" }, "Bucket"), bucket, h("span", { class: "hint" }, "Also read (and for MLflow artifacts, write) this bucket")),
            h("label", { class: "field" }, h("span", { class: "label" }, "Sandbox dataset"), dataset, h("span", { class: "hint" }, "BigQuery write: create + drop a probe table here"))),
          out),
        foot: [h("button", { class: "btn ghost", onclick: () => d.close() }, "Close"), run] });
      if (ident.last_test && !onlyConsumer) renderResult(out, ident.last_test);
      run.onclick = () => ctx.ui.busy(run, async () => {
        fill(out, h("div", { class: "stack", style: { gap: "8px" } }, [1, 2, 3, 4].map(() => h("div", { class: "skeleton sk-line", style: { height: "44px" } }))));
        try {
          const body = { bucket: bucket.value.trim(), dataset: dataset.value.trim() };
          const r = onlyConsumer
            ? await ctx.api(`/studio/api/gcp/consumers/${encodeURIComponent(onlyConsumer)}/test`, { body })
            : await ctx.api(`/studio/api/gcp/identities/${encodeURIComponent(ident.id)}/test`, { body });
          renderResult(out, r.result, keys);
          load();
        } catch (e) { fill(out, h("div", { class: "alert error" }, icon("error", 18), h("div", null, e.message || "Test failed"))); }
      });
      if (!ident.last_test || onlyConsumer) setTimeout(() => run.click(), 300);
    }

    // ── inline project picker (chip → searchable popover) ────────────
    function projectChip(label, value, onClick, opts) {
      opts = opts || {};
      return h("button", { type: "button", class: `gid-pchip${opts.override ? " override" : ""}${value ? "" : " empty"}`, title: opts.title || "Change project", onclick: (e) => { e.stopPropagation(); onClick(e.currentTarget); } },
        h("span", { class: "k" }, label), h("span", { class: "v" }, value || opts.placeholder || "choose"), icon("chevron-down", 11));
    }

    function projectPicker(anchor, { identityId, current, onPick, title }) {
      document.querySelectorAll(".gid-pop").forEach((p) => p.remove());
      const search = h("input", { class: "input", placeholder: "Search projects or type a project ID…", autocomplete: "off", spellcheck: false });
      const list = h("div", { class: "gid-pop-list" }, h("div", { class: "caption", style: { padding: "10px 12px" } }, h("span", { class: "spin" }), " Loading projects…"));
      const foot = h("div", { class: "gid-pop-foot caption" });
      const pop = h("div", { class: "popover gid-pop", role: "dialog" }, title ? h("div", { class: "gid-pop-title eyebrow" }, title) : null, search, list, foot);
      document.body.appendChild(pop);
      const r = anchor.getBoundingClientRect();
      pop.style.top = `${window.scrollY + r.bottom + 6}px`;
      pop.style.left = `${Math.max(8, Math.min(window.scrollX + r.left, window.scrollX + window.innerWidth - pop.offsetWidth - 12))}px`;
      let projects = [], active = 0;
      const close = () => { pop.remove(); document.removeEventListener("mousedown", outside, true); document.removeEventListener("keydown", keys); };
      const outside = (e) => { if (!pop.contains(e.target) && !anchor.contains(e.target)) close(); };
      const pick = (id) => { close(); if (id && id !== current) onPick(id); };
      const matches = () => {
        const q = search.value.trim().toLowerCase();
        return projects.filter((p) => !q || p.id.toLowerCase().includes(q) || (p.name || "").toLowerCase().includes(q));
      };
      function paintPop() {
        const rows = matches();
        const typed = search.value.trim();
        const exact = rows.some((p) => p.id === typed);
        active = Math.min(active, Math.max(0, rows.length - (typed && !exact ? 0 : 1)));
        fill(list,
          rows.map((p, i) => h("button", { type: "button", class: `gid-pop-item${p.id === current ? " current" : ""}${i === active ? " active" : ""}`, onclick: () => pick(p.id) },
            h("span", { class: "nm" }, p.name && p.name !== p.id ? p.name : p.id),
            p.name && p.name !== p.id ? h("code", null, p.id) : null,
            p.known ? h("span", { class: "chip outline" }, "known") : null,
            p.id === current ? icon("check", 13) : null)),
          typed && !exact ? h("button", { type: "button", class: `gid-pop-item typed${active === rows.length ? " active" : ""}`, onclick: () => pick(typed) },
            icon("edit", 12), h("span", { class: "nm" }, "Use project ID "), h("code", null, typed)) : null,
          !rows.length && !typed ? h("div", { class: "caption", style: { padding: "10px 12px" } }, "No projects listed — type a project ID") : null);
      }
      const keys = (e) => {
        const n = matches().length + (search.value.trim() && !matches().some((p) => p.id === search.value.trim()) ? 1 : 0);
        if (e.key === "Escape") { close(); anchor.focus(); }
        else if (e.key === "ArrowDown") { active = Math.min(n - 1, active + 1); paintPop(); e.preventDefault(); }
        else if (e.key === "ArrowUp") { active = Math.max(0, active - 1); paintPop(); e.preventDefault(); }
        else if (e.key === "Enter") { const items = list.querySelectorAll(".gid-pop-item"); if (items[active]) items[active].click(); e.preventDefault(); }
      };
      search.oninput = () => { active = 0; paintPop(); };
      setTimeout(() => { document.addEventListener("mousedown", outside, true); document.addEventListener("keydown", keys); search.focus(); }, 0);
      projectsOf(identityId).then((res) => {
        projects = res.projects || [];
        if (current && !projects.some((p) => p.id === current)) projects = [{ id: current, name: current }, ...projects];
        foot.textContent = res.available ? `${(res.projects || []).length} project(s) this identity can see` : `Can't list projects (${res.reason || "denied"}) — known projects shown; type any ID`;
        paintPop();
      });
      return { close };
    }

    async function setIdentityProject(r, id) {
      try {
        const res = await ctx.api(`/studio/api/gcp/identities/${encodeURIComponent(r.id)}`, { method: "PATCH", body: { project: id } });
        ctx.toast(`Default project for ${r.name} set to ${id}`, "success");
        state = res; paint();
        const t = await ctx.api(`/studio/api/gcp/identities/${encodeURIComponent(r.id)}/test`, { body: {} });
        load();
        const caps = t.result.capabilities || {};
        const bad = Object.entries(caps).filter(([, v]) => v !== "ok").map(([k]) => CAP_TITLE[k] || k);
        ctx.toast(`Re-tested ${r.name} on ${id}: ${bad.length ? `not OK — ${bad.join(", ")}` : "all checks passed"}`, bad.length ? "info" : "success");
      } catch (e) { if (e.status) ctx.toast(e.message, "error"); }
    }

    // ── assignments ──────────────────────────────────────────────────
    const projCache = {};          // identity id → Promise<{available, reason, projects:[{id,name,known}]}>
    const projectsOf = (id, fresh) => {
      if (!id) return Promise.resolve({ available: false, projects: [] });
      if (fresh || !projCache[id]) projCache[id] = ctx.api(`/studio/api/gcp/identities/${encodeURIComponent(id)}/projects`).catch((e) => ({ available: false, reason: e.message, projects: [] }));
      return projCache[id];
    };
    const settingsOf = (c) => (c.key in draft ? draft[c.key] : { ...c.settings, data_projects: [...(c.settings.data_projects || [])] });
    const setDraft = (c, v) => {
      const same = JSON.stringify(v) === JSON.stringify({ ...c.settings, data_projects: c.settings.data_projects || [] });
      if (same) delete draft[c.key]; else draft[c.key] = v;
      paintAssignments();
    };

    function paintAssignments() {
      const dirty = Object.keys(draft).length > 0;
      const save = h("button", { class: "btn primary sm", disabled: !dirty || null, onclick: () => saveAssignments(save) }, icon("save", 14), "Save assignments");
      const reset = h("button", { class: "btn ghost sm", disabled: !dirty || null, onclick: () => { draft = {}; paintAssignments(); } }, "Discard");
      const suggest = h("button", { class: "btn secondary sm", title: "Probe what each identity can read and run, then propose a setup (nothing is saved until you do)", onclick: () => suggestDialog(suggest) }, icon("bulb", 14), "Suggest from access");
      const groups = state.groups.map((g) => {
        const rows = state.consumers.filter((c) => c.group === g.id);
        if (!rows.length) return null;
        return h("div", { class: "gid-group" }, h("div", { class: "gid-group-title eyebrow" }, g.title), rows.map(consumerRow));
      });
      fill(asgHost,
        h("div", { class: "section-head" }, h("span", { class: "eyebrow" }, "Assignments",
          ctx.help("Pick the identity — and the Google Cloud project — each component uses. BigQuery separates the DATA project (where the datasets live) from the EXECUTION project (where query jobs run and are billed): a read-only account often may read a business project but not run jobs in it. “Inherit” follows the parent shown; at the end of the chain components use GCP_SA_JSON, then application default credentials. Saving changes the vault; Apply recreates the running services that read them.")),
        h("div", { class: "row", style: { gap: "6px" } }, suggest, reset, save)),
        h("div", { class: "card gid-assign" }, groups));
    }

    // chips: data project(s) [+ "jobs in" execution project]; immediate save; reset link for overrides
    function projectChips(c, st, eff, inherits) {
      const pr = c.key in draft ? null : c.projects;
      const identDefault = (eff && eff.project) || "";
      const data = pr ? (c.multi_project ? pr.data_projects : [pr.project]) : (c.multi_project && st.data_projects.length ? st.data_projects : [st.project || identDefault]);
      const exec = pr ? pr.exec_project : (st.exec_project || data[0]);
      const overridden = !!(st.project || st.exec_project || (st.data_projects || []).length);
      const identId = st.identity || c.effective;
      if (!eff) return h("div", { class: "caption gid-proj-row" }, "Project: GCP_PROJECT_ID");
      if (inherits) return h("div", { class: "gid-proj-row row" }, h("span", { class: "caption" }, "Projects inherited from ", h("b", null, consumerTitle(c.fallback))),
        h("span", { class: "gid-pchip ro" }, h("span", { class: "k" }, "data"), h("span", { class: "v" }, (data.filter(Boolean).join(", ")) || identDefault)),
        c.execution_project ? h("span", { class: "gid-pchip ro" }, h("span", { class: "k" }, "jobs in"), h("span", { class: "v" }, exec || "—")) : null);
      const save = (patch) => saveRow(c, { ...st, identity: st.identity || c.assigned, ...patch });
      const dataLabel = c.multi_project ? (data.filter(Boolean).length > 1 ? `${data[0]} +${data.length - 1}` : data[0]) : data[0];
      const dataChip = projectChip(c.execution_project ? "data" : "project", dataLabel, (a) => c.multi_project && data.filter(Boolean).length > 1
        ? projectDialog(c)
        : projectPicker(a, { identityId: identId, current: data[0], title: `${c.title} · ${c.execution_project ? "data project" : "project"}`,
          onPick: (id) => save(c.multi_project ? { data_projects: [id], project: "" } : { project: id }) }),
      { override: overridden && (st.project || (st.data_projects || []).length), title: c.multi_project ? "Data project(s) — click to change; several via “more”" : "Project — click to change" });
      const execChip = c.execution_project ? projectChip("jobs in", exec, (a) => projectPicker(a, { identityId: identId, current: exec, title: `${c.title} · execution project (jobs, billing)`,
        onPick: (id) => save({ exec_project: id }) }), { override: !!st.exec_project, title: "Execution project: where BigQuery runs and bills the query jobs" }) : null;
      return h("div", { class: "gid-proj-row row" }, dataChip, execChip,
        c.multi_project || c.execution_project ? h("button", { class: "linkish", type: "button", onclick: () => projectDialog(c) }, c.multi_project ? "more…" : "check access…") : null,
        overridden ? h("button", { class: "linkish", type: "button", title: `Use ${identDefault || "the identity's default project"}`, onclick: () => save({ project: "", exec_project: "", data_projects: [] }) }, "reset to identity default") : null);
    }

    async function saveRow(c, v) {
      if (Object.keys(draft).some((k) => k !== c.key)) { draft[c.key] = v; paintAssignments(); ctx.toast("Changed — Save assignments to keep it", "info"); return; }
      try {
        const r = await ctx.api("/studio/api/gcp/assignments", { method: "PUT", body: { [c.key]: v } });
        delete draft[c.key];
        ctx.toast(`${c.title}: saved${r.impact.recreate.length ? " — Apply to update running services" : ""}`, "success");
        load();
      } catch (e) { if (e.status) ctx.toast(e.message, "error"); }
    }

    function projectSummary(c, st, eff) {
      const pr = c.key in draft ? null : c.projects;      // effective (server-resolved) when unchanged
      const data = pr ? (c.multi_project ? pr.data_projects : [pr.project]) : (c.multi_project && st.data_projects.length ? st.data_projects : [st.project || (eff && eff.project) || ""]);
      const exec = pr ? pr.exec_project : (st.exec_project || data[0]);
      const list = data.filter(Boolean);
      return h("div", { class: "gid-proj" },
        h("span", { class: "k" }, c.execution_project ? (list.length > 1 ? "Data projects" : "Data project") : "Project"),
        list.length ? list.map((p) => h("code", null, p)) : h("span", { class: "muted" }, "identity default"),
        c.execution_project ? [h("span", { class: "k" }, "Jobs run in"), h("code", null, exec || "—")] : null);
    }

    function consumerRow(c) {
      const st = settingsOf(c);
      const parent = c.fallback ? consumerTitle(c.fallback) : "";
      const sel = h("select", { class: "input select", "aria-label": `Identity for ${c.title}` },
        h("option", { value: "" }, parent ? `Inherit from ${parent}` : "Not set — GCP_SA_JSON / default credentials"),
        state.identities.map((i) => h("option", { value: i.id, selected: i.id === st.identity || null,
          disabled: c.service_account_only && i.type !== "service_account" || null },
        `${i.name}${i.type === "authorized_user" ? " (personal)" : ""}${c.service_account_only && i.type !== "service_account" ? " — needs a service account" : ""}`)));
      sel.onchange = () => setDraft(c, { identity: sel.value, project: "", exec_project: "", data_projects: [] });
      const eff = state.identities.find((i) => i.id === (c.key in draft ? (st.identity || c.effective) : c.effective));
      const v = eff && eff.last_test && eff.last_test.consumers ? (eff.last_test.consumers[c.key] || {}).verdict : null;
      const changed = c.key in draft;
      const inherits = !st.identity && c.fallback;
      return h("div", { class: `gid-consumer${changed ? " changed" : ""}` },
        h("div", { class: "gid-c-main" },
          h("div", { class: "gid-c-title" }, c.title,
            c.future ? h("span", { class: "chip outline", title: "Wired for a feature that is not built yet" }, "planned") : null,
            c.service_account_only ? h("span", { class: "chip outline", title: "Personal sign-ins cannot be used here" }, icon("server", 11), "service account only") : null),
          h("div", { class: "caption" }, c.description),
          h("div", { class: "caption gid-env" }, h("code", null, c.env))),
        h("div", { class: "gid-c-pick" }, sel,
          projectChips(c, st, eff, inherits),
          h("div", { class: "caption gid-uses" }, changed ? h("span", null, icon("edit", 12), " unsaved")
            : eff ? h("span", null, "Uses ", h("b", null, eff.name), c.via && c.via !== c.key ? ` (via ${consumerTitle(c.via)})` : "")
              : h("span", null, "Uses GCP_SA_JSON / default credentials"))),
        h("div", { class: "gid-c-status" },
          eff && !changed ? verdictChip(v ? { ok: "Passed", partial: "Partly", failed: "Failed", unsupported: "Unsupported", untested: "Not tested" }[v] : "Not tested", v || "untested") : null,
          eff && !changed ? h("button", { class: "btn ghost sm", onclick: () => testDrawer(eff, c.key) }, icon("play", 13), "Test") : null));
    }

    // project picker for one consumer: data project(s) + execution project
    async function projectDialog(c) {
      const st = settingsOf(c);
      const identId = st.identity || c.effective;
      const ident = state.identities.find((i) => i.id === identId);
      let chosen = new Set(c.multi_project ? (st.data_projects.length ? st.data_projects : (c.key in draft ? [] : c.projects.data_projects)) : []);
      let single = c.multi_project ? "" : (st.project || (c.key in draft ? "" : c.projects.project) || "");
      let exec = st.exec_project || "";
      let probe = null;
      const listBox = h("div", { class: "gid-plist" }, h("div", { class: "skeleton sk-line" }));
      const note = h("div");
      const free = h("input", { class: "input", placeholder: "or type a project id", autocomplete: "off", spellcheck: false });
      const execSel = h("select", { class: "input select" });
      const ok = h("button", { class: "btn primary" }, "Use these projects");
      const suggestBtn = h("button", { class: "btn secondary sm" }, icon("bulb", 13), "Check access");
      const m = ctx.ui.modal({ title: `${c.title} — projects`, wide: true,
        subtitle: `As ${ident ? ident.name : "the inherited identity"}${ident && ident.principal ? ` (${ident.principal})` : ""}`,
        body: h("div", { class: "stack" },
          c.execution_project ? h("div", { class: "alert info" }, icon("info", 18), h("div", null,
            h("b", null, "Data project"), " = where the datasets live (tables are read / written there). ",
            h("b", null, "Execution project"), " = where BigQuery runs the query jobs and bills them — it needs bigquery.jobs.create (roles/bigquery.jobUser) for this identity. A read-only account typically reads a business project but runs its jobs in a project of its own.")) : null,
          h("div", { class: "row between" }, h("span", { class: "label" }, c.multi_project ? "Data projects (tick one or more)" : c.execution_project ? "Data project" : "Project"),
            c.execution_project ? suggestBtn : null),
          listBox, h("div", { class: "row", style: { gap: "8px" } }, free,
            h("button", { class: "btn ghost sm", onclick: () => { const v = free.value.trim(); if (!v) return; if (c.multi_project) chosen.add(v); else single = v; free.value = ""; extra.add(v); paintList(); } }, icon("add", 13), "Add")),
          c.execution_project ? h("label", { class: "field" }, h("span", { class: "label" }, "Execution project (query jobs, billing)"), execSel,
            h("span", { class: "hint" }, "Default = the (first) data project")) : null,
          note),
        foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Cancel"), ok] });
      const extra = new Set([...chosen, single, exec].filter(Boolean));
      let projects = [];
      function paintList() {
        const all = [...projects];
        extra.forEach((id) => { if (!all.some((p) => p.id === id)) all.push({ id, name: id }); });
        const pr = (id) => probe && probe.projects.find((x) => x.id === id);
        fill(listBox, all.length ? all.map((p) => {
          const r = pr(p.id);
          const input = h("input", { type: c.multi_project ? "checkbox" : "radio", name: `p-${c.key}`, checked: (c.multi_project ? chosen.has(p.id) : single === p.id) || null,
            onchange: (e) => { if (c.multi_project) { e.target.checked ? chosen.add(p.id) : chosen.delete(p.id); } else single = p.id; paintExec(); } });
          return h("label", { class: "gid-prow" }, input,
            h("span", { class: "nm" }, p.name && p.name !== p.id ? [h("b", null, p.name), " "] : null, h("code", null, p.id), p.known ? h("span", { class: "chip outline", title: "From this identity's known-projects list" }, "known") : null),
            r ? h("span", { class: "gid-pstat" },
              r.datasets != null ? verdictChip(`${r.datasets} dataset${r.datasets === 1 ? "" : "s"}`, r.datasets ? "ok" : "untested", r.datasets_error || "")
                : verdictChip("no BigQuery access", "failed", r.datasets_error),
              verdictChip(r.jobs_create ? "jobs allowed" : "no jobs", r.jobs_create ? "ok" : "failed", r.jobs_error || "")) : null);
        }) : h("div", { class: "caption" }, "No projects found — type a project id below."));
        paintExec();
      }
      function paintExec() {
        if (!c.execution_project) return;
        const data = c.multi_project ? [...chosen] : [single].filter(Boolean);
        const opts = [...new Set([...projects.map((p) => p.id), ...extra])];
        const runnable = (id) => probe && (probe.projects.find((x) => x.id === id) || {}).jobs_create;
        fill(execSel, h("option", { value: "" }, `Same as data project${data[0] ? ` (${data[0]})` : ""}`),
          opts.map((id) => h("option", { value: id, selected: id === exec || null }, `${id}${probe ? (runnable(id) ? " — jobs allowed" : " — no jobs.create") : ""}`)));
        execSel.onchange = () => { exec = execSel.value; };
      }
      suggestBtn.onclick = () => ctx.ui.busy(suggestBtn, async () => {
        try {
          probe = await ctx.api(`/studio/api/gcp/identities/${encodeURIComponent(identId)}/probe`, { body: { projects: [...new Set([...projects.map((p) => p.id), ...extra])].slice(0, 25) } });
          const pp = probe.proposal;
          if (c.multi_project) chosen = new Set(pp.data_projects); else if (pp.project) single = pp.project;
          exec = pp.exec_project || "";
          fill(note, h("div", { class: "alert success" }, icon("bulb", 18), h("div", null, "Proposed from live access: ",
            c.multi_project ? `data projects ${pp.data_projects.join(", ") || "(none readable)"}` : `data project ${pp.project || "—"}`,
            `, jobs in ${pp.exec_project || "— (no project allows jobs)"}. Review, then “Use these projects”.`)));
          paintList();
        } catch (e) { fill(note, h("div", { class: "alert error" }, icon("error", 18), h("div", null, e.message))); }
      });
      ok.onclick = () => {
        const v = { identity: st.identity, project: c.multi_project ? "" : single, exec_project: c.execution_project ? exec : "",
          data_projects: c.multi_project ? [...chosen] : [] };
        if (!v.identity) v.identity = c.assigned;
        m.close(); saveRow(c, v);
      };
      const res = await projectsOf(identId);
      projects = res.projects || [];
      if (!res.available) fill(note, h("div", { class: "alert warning" }, icon("warning", 18), h("div", null,
        `Could not list projects for this identity (${res.reason || "not allowed"}). Showing its known projects — type any project id to add it; edit the known list from the identity's Edit button.`)));
      paintList();
    }

    // page-level proposal: probe every identity, propose BigQuery read / write (+ keep Vertex)
    async function suggestDialog(btn) {
      const body = h("div", { class: "stack" }, h("div", { class: "caption" }, "Checking what each identity can read and where it may run jobs…"),
        h("div", { class: "skeleton sk-line", style: { height: "60px" } }));
      const ok = h("button", { class: "btn primary", disabled: true }, "Use this proposal");
      const m = ctx.ui.modal({ title: "Suggested setup", wide: true, subtitle: "Based on live access — nothing is saved until you click Save assignments",
        body, foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Cancel"), ok] });
      const results = {};
      await Promise.all(state.identities.map(async (i) => {
        try { results[i.id] = await ctx.api(`/studio/api/gcp/identities/${encodeURIComponent(i.id)}/probe`, { body: {} }); } catch (e) { results[i.id] = { error: e.message, projects: [] }; }
      }));
      const score = (r) => (r.projects || []).reduce((a, p) => a + (p.datasets || 0), 0);
      const ranked = state.identities.filter((i) => results[i.id] && !results[i.id].error).sort((a, b) => score(results[b.id]) - score(results[a.id]));
      const reader = ranked[0];
      const def = state.default_project;
      const writer = state.identities.find((i) => i.id !== (reader && reader.id) && (results[i.id].projects || []).some((p) => p.id === def && p.jobs_create))
        || state.identities.find((i) => (results[i.id].projects || []).some((p) => p.id === def && p.jobs_create));
      const proposal = {};
      if (reader) { const pp = results[reader.id].proposal; proposal.bigquery_read = { identity: reader.id, project: "", exec_project: pp.exec_project, data_projects: pp.data_projects }; }
      if (writer) proposal.bigquery_write = { identity: writer.id, project: def, exec_project: def, data_projects: [] };
      const row = (k, v) => h("tr", null, h("td", null, h("b", null, consumerTitle(k))), h("td", null, idName(v.identity)),
        h("td", null, (v.data_projects.length ? v.data_projects : [v.project]).filter(Boolean).map((p) => h("code", { style: { marginRight: "6px" } }, p))),
        h("td", null, v.exec_project ? h("code", null, v.exec_project) : "—"));
      fill(body,
        h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", null, h("tr", null, h("th", null, "Identity"), h("th", null, "Projects visible"), h("th", null, "BigQuery datasets"), h("th", null, "Jobs allowed in"))),
          h("tbody", null, state.identities.map((i) => { const r = results[i.id] || {}; const ps = r.projects || [];
            return h("tr", null, h("td", null, h("b", null, i.name)), h("td", null, r.error ? h("span", { class: "muted" }, r.error) : String(ps.length)),
              h("td", null, ps.filter((p) => p.datasets).map((p) => h("div", { class: "caption" }, h("code", null, p.id), ` · ${p.datasets}`))),
              h("td", null, ps.filter((p) => p.jobs_create).map((p) => h("div", { class: "caption" }, h("code", null, p.id))))); })))),
        h("div", { class: "eyebrow", style: { marginTop: "8px" } }, "Proposal"),
        Object.keys(proposal).length ? h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", null, h("tr", null, h("th", null, "Component"), h("th", null, "Identity"), h("th", null, "Data project(s)"), h("th", null, "Execution project"))),
          h("tbody", null, Object.entries(proposal).map(([k, v]) => row(k, v)))))
          : h("div", { class: "caption" }, "No identity can read BigQuery yet — nothing to propose."),
        h("div", { class: "caption" }, "Everything else (Vertex AI, Cloud Storage, …) keeps its current identity."));
      ok.disabled = !Object.keys(proposal).length || null;
      ok.onclick = () => { Object.assign(draft, proposal); m.close(); paintAssignments(); ctx.toast("Proposal loaded — review and Save assignments", "info"); };
    }

    async function saveAssignments(btn) {
      try {
        const r = await ctx.ui.busy(btn, () => ctx.api("/studio/api/gcp/assignments", { method: "PUT", body: draft }));
        draft = {};
        ctx.toast(r.impact.recreate.length ? "Assignments saved — Apply to update running services" : "Assignments saved", "success");
        load();
      } catch (e) { if (e.status) ctx.toast(e.message, "error"); }
    }

    // ── access list: who may "act as" this identity (principal switch) ──
    function accessChip(r) {
      const a = r.access || { mode: "owner", users: [] };
      const label = a.mode === "everyone" ? "Everyone" : a.mode === "list" ? `${a.users.length + 1} user${a.users.length ? "s" : ""} + admins` : "Owner + admins";
      const title = a.mode === "everyone" ? "Anyone signed in to this appliance may pick it under “Acting as”"
        : `Owner ${a.owner || "(none)"} and Studio admins${a.mode === "list" ? `, plus ${a.users.join(", ")}` : ""}`;
      return h("button", { class: `chip ${a.mode === "everyone" ? "warning" : "outline"}`, title, onclick: () => accessDialog(r) }, icon(a.mode === "everyone" ? "globe" : "users", 12), label);
    }

    function accessDialog(r) {
      const a = r.access || { mode: "owner", users: [] };
      let mode = a.mode;
      const users = h("textarea", { class: "textarea mono", rows: 5, spellcheck: false, placeholder: "one per line: user name or e-mail\nanna.lee@example.com" }, (a.users || []).join("\n"));
      const usersField = h("label", { class: "field" }, h("span", { class: "label" }, "Users and e-mails"), users,
        h("span", { class: "hint" }, "Matched against the Studio user name and the signed-in e-mail."));
      const radio = (v, title, sub) => h("label", { class: "row", style: { gap: "8px", alignItems: "flex-start", cursor: "pointer" } },
        h("input", { type: "radio", name: "gid-access", value: v, checked: mode === v || null, onchange: () => { mode = v; usersField.hidden = v !== "list"; } }),
        h("div", null, h("div", { style: { fontWeight: 600 } }, title), h("div", { class: "caption" }, sub)));
      usersField.hidden = mode !== "list";
      const ok = h("button", { class: "btn primary" }, "Save");
      const m = ctx.ui.modal({ title: `Who may act as ${r.name}`, subtitle: r.principal || "", body: h("div", { class: "stack" },
        h("div", { class: "caption" }, "People on this list can pick the identity under “Acting as” in Import & Export, the Integration Canvas and the MCP BigQuery tools. Studio issues their tools a short-lived token — the key never leaves the vault. Every token is audited. Components assigned to this identity keep using it either way; Trino always uses its one shared identity."),
        radio("owner", `Owner and admins${a.owner ? ` — ${a.owner}` : ""}`, "The default: the person who added it, and Studio admins."),
        radio("list", "Owner, admins and these users", "Name people who may act as it."),
        usersField,
        radio("everyone", "Everyone in this appliance", "Any signed-in user. Queries are attributed to this identity in Google Cloud."),
        a.updated_by ? h("div", { class: "caption" }, `Last changed by ${a.updated_by} ${ctx.ui.fmtAgo(a.updated_at)}`) : null),
        foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Cancel"), ok] });
      ok.onclick = () => ctx.ui.busy(ok, async () => {
        try {
          const res = await ctx.api(`/studio/api/gcp/identities/${encodeURIComponent(r.id)}/access`, { method: "PUT", body: { mode, users: users.value } });
          m.close(); ctx.toast(`Access for ${r.name} saved`, "success"); state = res; paint();
        } catch (e) { ctx.toast(e.message, "error"); }
      });
    }

    // identity: default project + known projects
    async function editIdentity(r) {
      const sel = h("select", { class: "input select" }, h("option", { value: r.project }, r.project || "—"));
      const known = h("textarea", { class: "textarea mono", rows: 5, spellcheck: false,
        placeholder: "one per line: project-id  display name" }, (r.known_projects || []).map((p) => `${p.id}${p.name && p.name !== p.id ? `  ${p.name}` : ""}`).join("\n"));
      const note = h("div", { class: "caption" }, "Loading the projects this identity can see…");
      const ok = h("button", { class: "btn primary" }, "Save");
      const m = ctx.ui.modal({ title: `Edit ${r.name}`, subtitle: r.principal || "", body: h("div", { class: "stack" },
        h("label", { class: "field" }, h("span", { class: "label" }, "Default project"), sel, note),
        h("label", { class: "field" }, h("span", { class: "label" }, "Known projects"), known,
          h("span", { class: "hint" }, "Offered in the project pickers, also when this identity may not list projects (Resource Manager API denied).")) ),
        foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Cancel"), ok] });
      ok.onclick = () => ctx.ui.busy(ok, async () => {
        try {
          const res = await ctx.api(`/studio/api/gcp/identities/${encodeURIComponent(r.id)}`, { method: "PATCH", body: { project: sel.value, known_projects: known.value } });
          delete projCache[r.id]; m.close(); ctx.toast(`${r.name} updated`, "success"); state = res; paint();
        } catch (e) { ctx.toast(e.message, "error"); }
      });
      const res = await projectsOf(r.id, true);
      const ids = new Set([r.project]);
      (res.projects || []).forEach((p) => { if (!ids.has(p.id)) { ids.add(p.id); sel.append(h("option", { value: p.id }, p.name && p.name !== p.id ? `${p.name} — ${p.id}` : p.id)); } });
      note.textContent = res.available ? `${(res.projects || []).length} project(s) visible to this identity` : `Could not list projects (${res.reason || "denied"}) — showing known projects`;
    }

    // ── add key file ─────────────────────────────────────────────────
    function gcloudHint() {
      const cmd = "gcloud auth application-default login";
      return h("div", { class: "gid-howto" },
        h("div", { style: { fontWeight: 600, marginBottom: "6px" } }, "Use your own Google account"),
        h("div", { class: "caption" }, "On your laptop (Google Cloud SDK installed), run:"),
        h("div", { class: "gid-cmd" }, h("code", null, cmd), h("button", { class: "btn ghost icon sm", title: "Copy", "aria-label": "Copy command", onclick: () => ctx.copy(cmd, "Command") }, icon("copy", 13))),
        h("div", { class: "caption" }, "then upload the file it writes:"),
        h("dl", { class: "kv gid-paths" },
          h("dt", null, "macOS / Linux"), h("dd", null, h("code", null, "~/.config/gcloud/application_default_credentials.json")),
          h("dt", null, "Windows"), h("dd", null, h("code", null, "%APPDATA%\\gcloud\\application_default_credentials.json"))),
        h("div", { class: "caption", style: { marginTop: "6px" } }, "Or skip the laptop step: close this and use ", h("b", null, "Sign in with Google"), "."));
    }

    function addDialog() {
      const name = h("input", { class: "input", placeholder: "e.g. my BigQuery sign-in", autocomplete: "off" });
      const project = h("input", { class: "input", placeholder: state.default_project || "my-project", autocomplete: "off", spellcheck: false });
      const json = h("textarea", { class: "textarea mono", rows: 8, spellcheck: false, autocomplete: "off", placeholder: '{"type": "service_account", …}  or  {"type": "authorized_user", …}' });
      const kind = h("div", { class: "caption", style: { minHeight: "18px", marginTop: "4px" } });
      const err = h("div", { class: "error", role: "alert" });
      const sniff = () => {
        let t = "";
        try { const j = JSON.parse(json.value); t = j.type; if (!name.value && j.client_email) name.value = j.client_email.split("@")[0]; if (!project.value && (j.project_id || j.quota_project_id)) project.placeholder = j.project_id || j.quota_project_id; } catch (e) { t = json.value.trim() ? "?" : ""; }
        fill(kind, t === "service_account" ? [typeBadge(t), " service-account key"] : t === "authorized_user" ? [typeBadge(t), " personal sign-in — everything assigned to it acts as you"]
          : t === "?" ? "Not valid JSON yet" : t ? `Unsupported type “${t}”` : "");
      };
      json.addEventListener("input", sniff);
      const drop = ctx.ui.dropzone({ multiple: false, label: "Drop the .json file here or click to choose", onFiles: async (files) => {
        if (!files[0]) return;
        if (files[0].size > 65536) { err.textContent = "That file is too large to be a Google credential"; return; }
        json.value = await files[0].text(); sniff();
      } });
      const ok = h("button", { class: "btn primary" }, "Add identity");
      const cancel = h("button", { class: "btn ghost" }, "Cancel");
      const m = ctx.ui.modal({ title: "Add a cloud identity", wide: true,
        subtitle: "A service-account key from IAM, or a personal gcloud sign-in file",
        body: h("div", { class: "gid-add" },
          h("div", { class: "stack" },
            h("label", { class: "field" }, h("span", { class: "label" }, "Name"), name),
            h("div", { class: "field" }, h("span", { class: "label" }, "Credential JSON"), drop, json, kind),
            h("label", { class: "field" }, h("span", { class: "label" }, "Project"), project, h("span", { class: "hint" }, "Where BigQuery jobs run and buckets are listed. Blank = the key's project, else GCP_PROJECT_ID")),
            err),
          gcloudHint()),
        foot: [cancel, ok] });
      cancel.onclick = () => m.close();
      ok.onclick = () => ctx.ui.busy(ok, async () => {
        err.textContent = "";
        try {
          const r = await ctx.api("/studio/api/gcp/identities", { body: { name: name.value, json: json.value, project: project.value } });
          json.value = "";
          m.close(); ctx.toast(`${r.identity.name} added`, "success");
          state = r; draft = {}; paint();
          afterAdd(r.identity);
        } catch (e) { err.textContent = e.message || "That didn't work"; }
      });
    }

    async function afterAdd(ident) {
      const go = await ctx.confirm({ title: `${ident.name} is ready`, okText: "Test it now", cancelText: "Later",
        body: h("div", null, h("p", null, "Run a live test to see which Google services it can reach, then pick it in Assignments below for the components that should use it."),
          ident.type === "authorized_user" ? h("div", { class: "alert warning", style: { marginTop: "10px" } }, icon("warning", 18),
            h("div", null, "Personal identity: everything you assign to it acts as ", h("b", null, ident.principal || "your Google account"), " in Google Cloud. Fine for a single-user dev appliance; use a service account for shared ones.")) : null) });
      if (go) testDrawer(state.identities.find((i) => i.id === ident.id) || ident);
      else asgHost.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    // ── Sign in with Google (gcloud application-default login in a container) ──
    function googleDialog() {
      let session = null, popup = null, finished = false;
      const status = h("div");
      const code = h("input", { class: "input mono", placeholder: "4/0Ab…", autocomplete: "off", spellcheck: false, disabled: true, "aria-label": "Verification code" });
      const name = h("input", { class: "input", placeholder: "Defaults to your Google e-mail", autocomplete: "off" });
      const err = h("div", { class: "error", role: "alert" });
      const startBtn = h("button", { class: "btn primary" }, icon("globe", 15), "Open Google sign-in");
      const ok = h("button", { class: "btn primary", disabled: true }, "Add identity");
      const cancel = h("button", { class: "btn ghost" }, "Cancel");
      const m = ctx.ui.modal({ title: "Sign in with Google", wide: true,
        subtitle: "Runs gcloud auth application-default login for you — no SDK needed on your laptop",
        onClose: () => { if (session && !finished) ctx.api(`/studio/api/gcp/google-login/${session}`, { method: "DELETE" }).catch(() => {}); if (popup && !popup.closed) popup.close(); },
        body: h("div", { class: "gid-add" },
          h("div", { class: "stack" },
            h("div", { class: "gid-step" }, h("span", { class: "n" }, "1"), h("div", null,
              h("div", { style: { fontWeight: 600 } }, "Sign in and allow access"),
              h("div", { class: "caption" }, "A Google window opens. Choose your account and allow “Google Auth Library”. It ends on a Google page (sdk.cloud.google.com) that shows a verification code."),
              h("div", { style: { marginTop: "10px" } }, startBtn), status)),
            h("div", { class: "gid-step" }, h("span", { class: "n" }, "2"), h("div", { style: { flex: 1, minWidth: 0 } },
              h("div", { style: { fontWeight: 600 } }, "Paste the verification code Google shows you"),
              h("label", { class: "field", style: { marginTop: "8px" } }, h("span", { class: "label" }, "Verification code"), code),
              h("label", { class: "field" }, h("span", { class: "label" }, "Identity name"), name))),
            err),
          h("div", { class: "gid-howto" },
            h("div", { style: { fontWeight: 600, marginBottom: "6px" } }, "Good to know"),
            h("ul", { class: "gid-list" },
              h("li", null, "The code works once, for a few minutes, and only in this sign-in."),
              h("li", null, "Your Google Workspace admin can block gcloud for third-party access — then Google shows “Access blocked”; ask IT or use a service-account key."),
              h("li", null, "The resulting credential is stored encrypted in the vault; Studio never shows it again."),
              h("li", null, h("b", null, "Personal identity: "), "everything you assign to it acts as you in Google Cloud. Fine for a single-user dev appliance — use a service account for shared ones.")))),
        foot: [cancel, ok] });
      cancel.onclick = () => m.close();
      const openPopup = (url) => {
        popup = window.open(url || "about:blank", "bsp-google-signin", "popup=yes,width=520,height=680");
        return popup;
      };
      startBtn.onclick = async () => {
        err.textContent = "";
        const w = openPopup();          // open synchronously (popup blockers), navigate when the link is ready
        if (w) { try { w.document.title = "Starting Google sign-in…"; w.document.body.innerHTML = '<p style="font:14px system-ui;padding:24px;color:#475569">Starting Google sign-in…</p>'; } catch (e) { /* cross-origin */ } }
        fill(status, h("div", { class: "caption", style: { marginTop: "8px" } }, h("span", { class: "spin" }), " Starting gcloud…"));
        try {
          if (session) ctx.api(`/studio/api/gcp/google-login/${session}`, { method: "DELETE" }).catch(() => {});
          const r = await ctx.ui.busy(startBtn, () => ctx.api("/studio/api/gcp/google-login/start", { method: "POST" }));
          session = r.session;
          if (w && !w.closed) w.location.href = r.auth_url;
          fill(status, h("div", { class: "alert info", style: { marginTop: "10px" } }, icon("info", 18), h("div", null,
            w && !w.closed ? "Finish signing in in the Google window, then paste the code below. " : "Your browser blocked the window. ",
            h("a", { href: r.auth_url, target: "_blank", rel: "noopener noreferrer" }, "Open the Google sign-in page", icon("open-in-new", 12)))));
          fill(startBtn, icon("refresh", 15), "Start over");
          code.disabled = false; ok.disabled = false;
          setTimeout(() => code.focus(), 50);
        } catch (e) {
          if (w && !w.closed) w.close();
          fill(status); err.textContent = e.message || "Could not start the sign-in";
        }
      };
      ok.onclick = () => ctx.ui.busy(ok, async () => {
        err.textContent = "";
        if (!code.value.trim()) { code.focus(); return; }
        try {
          const r = await ctx.api(`/studio/api/gcp/google-login/${session}/code`, { body: { code: code.value.trim(), name: name.value.trim() } });
          finished = true; code.value = "";
          m.close(); ctx.toast(`Signed in as ${r.identity.principal || r.identity.name}`, "success");
          state = r; draft = {}; paint();
          afterAdd(r.identity);
        } catch (e) {
          err.textContent = e.message || "That didn't work";
          if (e.status === 400 || e.status === 404) { finished = true; session = null; code.disabled = true; ok.disabled = true; fill(startBtn, icon("refresh", 15), "Start again"); }
        }
      });
    }

    // ── load / paint ─────────────────────────────────────────────────
    function paint() {
      paintApply(); paintGov();
      table.update(state.identities);
      paintAssignments();
    }

    async function load() {
      try { state = await ctx.api("/studio/api/gcp/identities"); paint(); } catch (e) {
        if (e.status === 423) fill(el, h("div", { class: "card" }, h("div", { class: "empty" }, h("div", { class: "ic" }, icon("lock", 22)),
          h("h4", null, "The vault is locked"), h("div", null, "Unlock it to manage cloud identities."),
          h("button", { class: "btn primary", style: { marginTop: "14px" }, onclick: async () => { if (await ctx.unlock()) ctx.navigate("identities"); } }, icon("unlock", 16), "Unlock vault"))));
        else if (e.status) ctx.toast(e.message, "error");
      }
    }
    await load();
    ctx.on("studio:job-done", load);
  },
});
