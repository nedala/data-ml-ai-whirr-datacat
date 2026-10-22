/* Composer: personas (multi-select = hybrid) + à-la-carte capabilities →
   live plan (services, auto-included dependencies, RAM, start/stop diff) →
   one Apply. Resolution is server-side (catalog.yaml is the single source). */
Studio.registerPage({
  id: "compose",
  async render(el, ctx) {
    const { h, icon } = ctx.ui;
    const { fmtMB, fill } = StudioUI;
    const cat = await ctx.api("/studio/api/compose/catalog");
    const capById = Object.fromEntries(cat.capabilities.map((c) => [c.id, c]));
    const personaById = Object.fromEntries(cat.personas.map((p) => [p.id, p]));
    const svcTitle = (s) => (cat.services[s] || {}).title || s;
    const capTitle = (c) => (capById[c] || {}).title || c;
    const isAdmin = ctx.user.role === "admin";

    // Selection model: personas ∪ extra − removed. Personas stay visible as
    // "selected" even when a capability is removed (shown as customised).
    const state = { personas: new Set(), extra: new Set(), removed: new Set() };
    const union = () => new Set([...state.personas].flatMap((p) => personaById[p] ? personaById[p].capabilities : []));
    const effective = () => {
      const s = union();
      state.removed.forEach((c) => s.delete(c));
      state.extra.forEach((c) => s.add(c));
      return cat.capabilities.map((c) => c.id).filter((c) => s.has(c));
    };
    function load(sel) {
      state.personas = new Set((sel.personas || []).filter((p) => personaById[p]));
      const u = union();
      const caps = new Set(sel.capabilities && sel.capabilities.length ? sel.capabilities : [...u]);
      state.extra = new Set([...caps].filter((c) => !u.has(c) && capById[c]));
      state.removed = new Set([...u].filter((c) => !caps.has(c)));
    }
    load(cat.selection);
    // first visit: preselect this appliance's persona (APPLIANCE_PERSONA) so
    // "Start" is a single click; the others stay one toggle away.
    const single = cat.personas.length === 1;
    const fresh = !(cat.selection.personas || []).length && !(cat.selection.capabilities || []).length;
    if (fresh && cat.default_persona && personaById[cat.default_persona]) state.personas.add(cat.default_persona);
    else if (single && fresh) state.personas.add(cat.personas[0].id);
    // lead with this appliance's persona
    cat.personas.sort((a, b) => (b.id === cat.default_persona) - (a.id === cat.default_persona));
    let saved = JSON.stringify({ p: [...state.personas].sort(), c: effective() });
    if (ctx.params.add && capById[ctx.params.add]) { state.removed.delete(ctx.params.add); if (!union().has(ctx.params.add)) state.extra.add(ctx.params.add); }
    if (ctx.params.persona && personaById[ctx.params.persona]) state.personas.add(ctx.params.persona);

    let plan = null, seq = 0, applying = false;

    // ── layout ──────────────────────────────────────────────────────────
    const personaGrid = h("div", { class: "persona-grid", role: "group", "aria-label": "Personas" });
    const capGrid = h("div", { class: "cap-grid", role: "group", "aria-label": "Capabilities" });
    const summary = h("div", { class: "card summary-card" });
    const live = h("div", { class: "sr-only", "aria-live": "polite" });
    const applyBtn = h("button", { class: "btn primary lg" });
    const resetBtn = h("button", { class: "btn ghost", onclick: () => { load(cat.selection); changed(); } }, icon("history", 16), "Undo changes");
    const abTitle = h("div", { class: "ab-title" });
    const abSub = h("div", { class: "caption" });
    const bar = h("div", { class: "actionbar" }, h("div", null, abTitle, abSub), h("div", { class: "spacer" }), resetBtn, applyBtn);

    el.append(
      h("div", { class: "bsp-page-head" }, h("div", null,
        h("h1", null, `Your ${cat.appliance_name || ""} appliance`.replace("  ", " ")),
        h("div", { class: "lede" }, fresh && cat.default_persona
          ? `Everything a ${(personaById[cat.default_persona] || {}).title || ""} needs is selected. Switch off anything you don't need — or add another persona — then start. Passwords and dependencies are handled for you.`
          : "Pick one or more personas, fine-tune capabilities, then apply. Dependencies and credentials are handled for you."))),
      h("div", { class: "composer" },
        h("div", null,
          h("div", { class: "section-head" }, h("span", { class: "eyebrow" }, single ? "Your role" : "Start from a persona",
            ctx.help(single ? "Your role is a preset of capabilities. Fine-tune it with the toggles below."
              : "Personas are presets for common roles. Select more than one to combine them — a hybrid persona gets every capability of each. Fine-tune with the capabilities below.")),
            h("span", { class: "caption" }, single ? "" : "Select several to combine")),
          personaGrid,
          h("div", { class: "section-head" }, h("span", { class: "eyebrow" }, "Capabilities",
            ctx.help("Capabilities are the building blocks. Toggling one adds or removes its services. Anything a capability depends on is included automatically and shown in blue — you can't remove it while something needs it.")),
            h("span", { class: "caption" }, "Toggle à la carte")),
          capGrid),
        h("aside", { class: "composer-aside" }, summary)),
      bar, live);

    // ── personas (built once, updated in place: keeps focus + transitions) ─
    const personaEls = {};
    cat.personas.forEach((p) => {
      const chips = Object.fromEntries(p.capabilities.map((c) => [c, h("span", { class: "chip" }, capTitle(c))]));
      const badge = h("span", { class: "chip primary", style: { marginLeft: "8px", height: "20px", fontSize: "11px" } }, "customised");
      const tile = h("button", { type: "button", class: "tile", "data-tone": "analyse", onclick: () => togglePersona(p.id) },
        h("span", { class: "tile-check" }, icon("check", 12)),
        h("span", { class: "tile-ic" }, icon(p.icon || "profile", 18)),
        h("span", { class: "tile-title" }, p.title, badge),
        h("span", { class: "tile-blurb" }, p.blurb),
        h("span", { class: "caps" }, Object.values(chips)));
      personaEls[p.id] = { tile, chips, badge };
      personaGrid.append(tile);
    });

    function renderPersonas() {
      const eff = new Set(effective());
      cat.personas.forEach((p) => {
        const { tile, chips, badge } = personaEls[p.id];
        const on = state.personas.has(p.id);
        const customised = on && p.capabilities.some((c) => !eff.has(c));
        tile.classList.toggle("selected", on);
        tile.classList.toggle("partial", customised);
        tile.setAttribute("aria-pressed", String(on));
        badge.style.display = customised ? "" : "none";
        Object.entries(chips).forEach(([c, chip]) => chip.classList.toggle("off", on && !eff.has(c)));
      });
    }

    function togglePersona(id) {
      if (state.personas.has(id)) {
        state.personas.delete(id);
        const u = union();
        state.removed.forEach((c) => { if (!u.has(c)) state.removed.delete(c); });
        live.textContent = `${personaById[id].title} removed`;
      } else {
        state.personas.add(id);
        personaById[id].capabilities.forEach((c) => { state.removed.delete(c); state.extra.delete(c); });
        live.textContent = `${personaById[id].title} added`;
      }
      changed();
    }

    // ── capabilities ────────────────────────────────────────────────────
    const capEls = {};
    cat.capabilities.forEach((c) => {
      const dot = h("span", { class: "live-dot", title: "Running" });
      const why = h("div", { class: "why" });
      const blurb = h("div", { class: "tile-blurb" }, c.blurb);
      const tile = h("button", { type: "button", class: "tile compact", "data-tone": c.tone || "analyse", onclick: () => toggleCap(c.id, tile) },
        h("span", { class: "tile-ic" }, icon(c.icon || "apps", 16)),
        h("span", { class: "tile-body" }, h("div", { class: "tile-title" }, c.title, dot), why, blurb),
        h("span", { class: "tile-check" }, icon("check", 12)));
      capEls[c.id] = { tile, dot, why, blurb };
      capGrid.append(tile);
    });

    function implied(id) {
      return plan && plan.capabilities[id] && plan.capabilities[id].state === "implied" && !effective().includes(id);
    }

    function capServicesRunning(c) {
      if (!plan) return false;
      const st = Object.fromEntries(plan.services.map((s) => [s.name, s.state]));
      return capById[c].services.every((s) => st[s] === "running");
    }

    function renderCaps() {
      const eff = new Set(effective());
      cat.capabilities.forEach((c) => {
        const { tile, dot, why, blurb } = capEls[c.id];
        const on = eff.has(c.id), imp = implied(c.id);
        tile.classList.toggle("selected", on);
        tile.classList.toggle("implied", !!imp);
        tile.setAttribute("aria-pressed", String(on || !!imp));
        tile.toggleAttribute("aria-disabled", !!imp);
        why.textContent = imp ? `Included · needed by ${plan.capabilities[c.id].required_by.map(capTitle).join(", ")}` : "";
        why.style.display = imp ? "" : "none";
        blurb.style.display = imp ? "none" : "";
        tile.title = imp ? why.textContent : c.blurb;
        dot.style.display = (on || imp) && capServicesRunning(c.id) ? "" : "none";
      });
    }

    function toggleCap(id, tile) {
      const eff = new Set(effective());
      if (eff.has(id)) {
        if (state.extra.has(id)) state.extra.delete(id); else state.removed.add(id);
        live.textContent = `${capTitle(id)} removed`;
      } else if (implied(id)) {
        tile.classList.remove("shake"); void tile.offsetWidth; tile.classList.add("shake");
        live.textContent = `${capTitle(id)} is required by ${plan.capabilities[id].required_by.map(capTitle).join(", ")}`;
        return;
      } else {
        if (state.removed.has(id)) state.removed.delete(id); else state.extra.add(id);
        live.textContent = `${capTitle(id)} added`;
      }
      changed();
    }

    // ── summary ─────────────────────────────────────────────────────────
    function selectionLabel() {
      const names = [...state.personas].map((p) => personaById[p].title);
      const custom = state.extra.size || state.removed.size;
      if (!names.length) return effective().length ? "Custom selection" : "Nothing selected yet";
      return names.join(" + ") + (custom ? " (customised)" : "");
    }

    function reason(s) {
      if (s.action === "stop") return h("div", { class: "rs" }, "No longer needed by your selection");
      if (s.capabilities.length) return h("div", { class: "rs" }, `for ${s.capabilities.map(capTitle).join(", ")}`);
      const by = s.needed_by.map(svcTitle);
      return h("div", { class: "rs auto" }, `included because ${by.join(" & ")} need${by.length > 1 ? "" : "s"} ${s.title}`);
    }

    function group(label, items, mk) {
      if (!items.length) return null;
      return h("div", { class: "svc-group" },
        h("div", { class: "eyebrow" }, label, h("span", { class: "chip outline", style: { height: "18px", fontSize: "11px" } }, String(items.length))),
        items.map((s) => h("div", { class: "svc-item" }, h("span", { class: `mk ${mk}` }),
          h("div", { style: { minWidth: 0 } }, h("div", { class: "nm" }, s.title), reason(s)),
          h("span", { class: "mb" }, fmtMB(s.ram_mb)))));
    }

    function renderSummary() {
      const head = h("div", { class: "sc-head" }, h("div", { class: "eyebrow" }, "Your appliance"), h("div", { class: "label-line" }, selectionLabel()));
      if (!plan) { fill(summary, head, h("div", { class: "sc-body" }, h("div", { class: "skeleton sk-line" }), h("div", { class: "skeleton sk-line", style: { width: "70%" } }))); return; }
      if (!plan.services.length) {
        fill(summary, head, h("div", { class: "sc-body" }, h("div", { class: "empty", style: { padding: "24px 8px" } },
          h("div", { class: "ic" }, icon("wand", 22)), h("h4", null, single ? "Select a capability to begin" : "Pick a persona to begin"),
          h("div", null, "Not sure? Python Analyst is a light, friendly start."))));
        return;
      }
      const wanted = plan.services.filter((s) => s.action !== "stop");
      const total = plan.ram.selected_mb + plan.ram.baseline_mb;
      const host = plan.ram.host_mb;
      const pct = host ? Math.min(100, (total / host) * 100) : 0;
      const over = host && total > host * 0.9;
      const ram = h("div", { class: "ram" },
        h("div", { class: "nums" }, h("span", null, h("b", null, `≈ ${fmtMB(total)}`), " memory",
          ctx.help("A rough steady-state estimate from the service catalog (plus ~0.5 GB for the always-on control plane), compared with the memory Docker may use on this machine. On Docker Desktop: Settings → Resources.")),
          host ? h("span", null, `of ${fmtMB(host)}`) : h("span", null, "host memory unknown")),
        h("div", { class: `meter ${pct > 70 ? "warn" : ""}`, role: "meter", "aria-valuenow": Math.round(pct), "aria-valuemin": 0, "aria-valuemax": 100, "aria-label": "Estimated memory use" },
          h("span", { style: { width: `${pct}%` } })),
        over ? h("div", { class: "hint-line" }, icon("warning"), h("span", null, "More than Docker can comfortably give. Drop a capability, or raise Docker's memory limit.")) : null);

      const hints = plan.hints.map((x) => {
        if (x.missing) return h("div", { class: "hint-line" }, icon("info"), h("span", null, x.detail));
        const cap = cat.capabilities.find((c) => c.services.includes(x.wants));
        if (!cap || effective().includes(cap.id)) return null;
        return h("div", { class: "hint-line" }, icon("insight"), h("span", null, `${svcTitle(x.service)} works even better with ${svcTitle(x.wants)}. `,
          h("button", { type: "button", onclick: () => { state.removed.delete(cap.id); if (!union().has(cap.id)) state.extra.add(cap.id); changed(); } }, `Add ${cap.title}`)));
      }).filter(Boolean).slice(0, 5);

      const vaultNote = plan.start.length && plan.vault !== "unlocked"
        ? h("div", { class: "hint-line" }, icon("lock"), h("span", null, isAdmin ? "The vault is locked — you'll be asked to unlock it when you apply." : "The vault is locked — ask an admin to unlock it before applying."))
        : plan.credentials_to_generate ? h("div", { class: "hint-line" }, icon("key"), h("span", null, `${plan.credentials_to_generate} credential${plan.credentials_to_generate > 1 ? "s" : ""} will be generated and stored in the vault automatically.`)) : null;

      const dl = Object.entries(plan.downloads || {});
      const downloadNote = dl.length
        ? h("div", { class: "hint-line" }, icon("download"), h("span", null,
            `First start downloads ${dl.length === 1 ? "an image" : `${dl.length} images`} for ${dl.map(([s]) => svcTitle(s)).join(", ")} (needs internet from Docker). `,
            "If downloads fail with “Bad Gateway”, turn off the manual proxy in Docker Desktop → Settings → Resources → Proxies."))
        : null;

      fill(summary, head, h("div", { class: "sc-body" }, ram,
        group("Will start", plan.services.filter((s) => s.action === "start"), "start"),
        group("Running", plan.services.filter((s) => s.action === "keep"), "keep"),
        group("Will stop", plan.services.filter((s) => s.action === "stop"), "stop"),
        vaultNote, downloadNote, ...hints,
        h("div", { class: "caption", style: { marginTop: "12px" } }, `${wanted.length} service${wanted.length === 1 ? "" : "s"} · profiles: ${plan.profiles.join(", ") || "—"}`)));
    }

    function renderBar() {
      const dirty = JSON.stringify({ p: [...state.personas].sort(), c: effective() }) !== saved;
      abTitle.textContent = selectionLabel();
      const n = plan ? plan.services.filter((s) => s.action !== "stop").length : 0;
      abSub.textContent = plan ? `${n} service${n === 1 ? "" : "s"} · ≈ ${fmtMB(plan.ram.selected_mb + plan.ram.baseline_mb)}` : "Planning…";
      resetBtn.style.display = dirty ? "" : "none";
      const start = plan ? plan.start.length : 0, stop = plan ? plan.stop.length : 0;
      const blocked = !cat.can_apply || (plan && start && plan.vault !== "unlocked" && !isAdmin);
      if (!plan) fill(applyBtn, "Apply");
      else if (!start && !stop) fill(applyBtn, icon("check", 16), dirty ? "Save selection" : plan.services.length ? "Up to date" : "Nothing to apply");
      else fill(applyBtn, icon("zap", 16), start && !stop ? "Start" : "Apply",
        start ? h("span", { class: "count", title: "services to start" }, `start ${start}`) : null,
        stop ? h("span", { class: "count", title: "services to stop" }, `stop ${stop}`) : null);
      applyBtn.disabled = applying || !plan || blocked || (!start && !stop && !dirty);
      applyBtn.title = !cat.can_apply ? "Your role can view but not change the appliance" : "";
    }

    function renderAll() { renderPersonas(); renderCaps(); renderSummary(); renderBar(); }

    // ── planning ────────────────────────────────────────────────────────
    const replan = ctx.ui.debounce(async () => {
      const mine = ++seq;
      try {
        const p = await ctx.api("/studio/api/compose/plan", { body: { personas: [...state.personas], capabilities: effective() } });
        if (mine !== seq) return;
        plan = p;
        renderAll();
      } catch (e) { if (mine === seq) ctx.toast(e.message || "Could not plan this selection", "error"); }
    }, 90);

    function changed() { renderPersonas(); renderCaps(); renderBar(); replan(); }

    applyBtn.onclick = async () => {
      if (!plan) return;
      if (plan.stop.length) {
        const ok = await ctx.confirm({
          title: `Stop ${plan.stop.length} service${plan.stop.length > 1 ? "s" : ""}?`, danger: true, okText: "Apply and stop",
          body: h("div", null, h("p", null, "Nothing you've selected uses these any more. Their data is kept — add them back any time."),
            h("ul", { style: { margin: "8px 0 0", paddingLeft: "18px" } }, plan.services.filter((s) => s.action === "stop").map((s) => h("li", null, h("b", null, s.title), h("span", { class: "muted" }, ` · ${s.name}`))))),
        });
        if (!ok) return;
      }
      applying = true;
      renderBar();
      try {
        const r = await ctx.ui.busy(applyBtn, () => ctx.api("/studio/api/compose/apply", { body: { personas: [...state.personas], capabilities: effective() } }));
        saved = JSON.stringify({ p: [...state.personas].sort(), c: effective() });
        cat.selection = { personas: [...state.personas], capabilities: effective() };
        const hasWork = plan.start.length || plan.stop.length;
        if (hasWork) await ctx.job(r.job, { title: plan.start.length ? "Whirring up your appliance" : "Updating your appliance" });
        else ctx.toast("Selection saved", "success");
      } catch (e) {
        if (e.status) ctx.toast(e.message, "error");
      } finally {
        applying = false;
        replan();
      }
    };

    ctx.on("studio:job-done", () => replan());
    ctx.on("studio:vault", () => replan());
    ctx.every(8000, () => { if (!applying) replan(); });
    renderAll();
    replan();
  },
});
