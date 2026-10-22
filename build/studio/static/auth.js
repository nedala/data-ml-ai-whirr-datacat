/* Studio sign-in and first-run setup (served at /studio/login and /studio/setup). */
(function () {
  "use strict";
  const { h, icon, toast, genPassword, copy } = BSP;
  const { passwordField, strength, fill } = StudioUI;
  const api = BSP.api("");
  const card = document.getElementById("card");
  const params = new URLSearchParams(location.search);

  const POINTS = [
    ["wand", "One click to start", "Everything your role needs is preselected — switch off what you don't."],
    ["key", "Credentials handled for you", "Every service password is generated and kept in an encrypted vault."],
    ["shield", "Secure by default", "Single front door, deny-by-default egress, no root workloads."],
  ];
  document.getElementById("points").replaceChildren(...POINTS.map(([ic, t, d]) =>
    h("li", null, h("span", { class: "ic" }, icon(ic, 16)), h("span", null, h("b", null, t), h("br"), d))));

  function errorBox() {
    const el = h("div", { class: "alert error", role: "alert", style: { display: "none", marginBottom: "14px" } });
    return { el, show(msg) { fill(el, icon("error", 18), h("div", null, msg)); el.style.display = "flex"; el.classList.remove("shake"); void el.offsetWidth; el.classList.add("shake"); }, hide() { el.style.display = "none"; } };
  }

  async function submit(btn, fn, err) {
    err.hide();
    try { await BSP.busy(btn, fn); } catch (e) { err.show(e.message || "Something went wrong"); }
  }

  // ── login ─────────────────────────────────────────────────────────────
  function renderLogin(state) {
    const err = errorBox();
    if (params.get("error")) setTimeout(() => err.show(params.get("error")), 0);
    const next = params.get("next") || "/studio/";
    const sso = state.sso ? h("a", { class: "btn secondary lg", href: `/studio/oidc/login?next=${encodeURIComponent(next)}` },
      icon("company", 18), "Sign in with Microsoft") : null;
    let form = null;
    if (state.local_login) {
      const user = h("input", { class: "input", name: "username", id: "f-username", autocomplete: "username", autofocus: true, required: true });
      const pw = passwordField({ label: "Password", name: "password" });
      const btn = h("button", { class: "btn primary lg", type: "submit" }, "Sign in");
      form = h("form", { onsubmit: (e) => { e.preventDefault(); submit(btn, async () => {
        const r = await api("/studio/api/auth/login", { body: { username: user.value, password: pw.input.value, next } });
        location.href = r.next;
      }, err); } },
        h("div", { class: "field" }, h("label", { for: "f-username" }, "Username"), user), pw.el, btn);
      if (state.break_glass) form.prepend(h("div", { class: "alert warning", style: { marginBottom: "14px" } }, icon("warning", 18),
        h("div", null, h("div", { class: "title" }, "Break-glass sign-in"), "Local passwords are enabled for emergencies only. Every sign-in is audited.")));
    }
    fill(card, 
      h("h2", null, "Welcome back"),
      h("div", { class: "sub" }, state.local_login ? "Sign in to your appliance." : "This appliance uses your DataPlatform account."),
      err.el, sso, sso && form ? h("div", { class: "or" }, "or with a local account") : null, form,
      !sso && !form ? h("div", { class: "alert warning" }, icon("warning", 18), h("div", null, "Sign-in is not configured. Ask your administrator to set SSO_PROVIDER=oidc.")) : null);
  }

  // ── first-run setup ───────────────────────────────────────────────────
  function renderSetup(state) {
    const err = errorBox();
    const needsMaster = !state.master_from_env && state.vault !== "unlocked";
    const existingVault = state.vault === "locked";
    let step = 1;
    const user = h("input", { class: "input", name: "username", id: "f-username", autocomplete: "username", value: "admin", required: true });
    const pw = passwordField({ label: "Password", name: "password", meter: true, min: state.min_password, autocomplete: "new-password", hint: `At least ${state.min_password} characters` });
    const pw2 = passwordField({ label: "Confirm password", name: "password2", autocomplete: "new-password" });

    // master passphrase: generate | same | custom
    let mode = existingVault ? "custom" : "generate";
    let generated = genPassword(24, { symbolSet: "-_.~" });
    const saved = h("input", { type: "checkbox" });
    const genBox = h("div", { class: "mono-box" }, h("span", { class: "v" }, generated),
      h("button", { type: "button", class: "btn ghost icon sm", title: "Copy", "aria-label": "Copy passphrase", onclick: () => copy(generated, "Passphrase") }, icon("copy", 14)),
      h("button", { type: "button", class: "btn ghost icon sm", title: "Generate another", "aria-label": "Generate another", onclick: () => { generated = genPassword(24, { symbolSet: "-_.~" }); genBox.querySelector(".v").textContent = generated; saved.checked = false; } }, icon("refresh", 14)));
    const master = passwordField({ label: existingVault ? "Existing vault passphrase" : "Master passphrase", name: "master", meter: !existingVault, min: state.min_master, autocomplete: "new-password" });
    const master2 = passwordField({ label: "Confirm passphrase", name: "master2", autocomplete: "new-password" });
    const modeBody = h("div");
    const seg = h("div", { class: "segmented", role: "tablist", style: { marginBottom: "14px" } });
    const MODES = [["generate", "wand", "Generate"], ["same", "link", "Same as password"], ["custom", "edit", "My own"]];
    function renderMode() {
      fill(seg, ...MODES.map(([k, ic, t]) => h("button", { type: "button", role: "tab", "aria-selected": String(mode === k), class: mode === k ? "active" : "", onclick: () => { mode = k; renderMode(); } }, icon(ic, 14), t)));
      if (mode === "generate") fill(modeBody, genBox,
        h("label", { class: "checkbox", style: { marginTop: "12px" } }, saved, "I've saved this passphrase somewhere safe (e.g. a password manager)"));
      else if (mode === "same") fill(modeBody, h("div", { class: "alert info" }, icon("info", 18),
        h("div", null, "Your sign-in password will also unlock the vault. Convenient for a personal laptop; use a separate passphrase for shared appliances.")));
      else fill(modeBody, master.el, existingVault ? null : master2.el);
    }
    renderMode();

    const stepper = h("div", { class: "stepper" });
    const body = h("div");
    const back = h("button", { type: "button", class: "btn ghost", onclick: () => { step = 1; render(); } }, icon("arrow-left", 16), "Back");
    const nextBtn = h("button", { class: "btn primary lg", type: "submit" });

    function render() {
      const steps = needsMaster ? ["Admin account", "Vault"] : ["Admin account"];
      fill(stepper, ...steps.flatMap((t, i) => [
        i ? h("div", { class: `bar ${step > i ? "done" : ""}` }) : null,
        h("div", { class: `step ${step > i + 1 ? "done" : step === i + 1 ? "current" : ""}` }, h("span", { class: "n" }, step > i + 1 ? icon("check", 12) : String(i + 1)), t)]));
      if (step === 1) {
        fill(body, h("div", { class: "field" }, h("label", { for: "f-username" }, "Admin username"), user), pw.el, pw2.el);
        fill(nextBtn, needsMaster ? "Continue" : "Create appliance", icon("arrow-right", 16));
      } else {
        fill(body, 
          h("p", { class: "caption", style: { fontSize: "13px", lineHeight: "19px", marginBottom: "14px" } }, existingVault
            ? "A vault already exists on this appliance. Enter its passphrase to keep every existing credential."
            : "The master passphrase encrypts every service credential (AES-256). It is never stored — you'll enter it after a restart and to reveal secrets."),
          state.legacy_vault ? h("div", { class: "alert info", style: { marginBottom: "14px" } }, icon("info", 18), h("div", null, "Credentials from the previous bootstrap wizard will be migrated into the new vault.")) : null,
          existingVault ? null : seg, modeBody);
        fill(nextBtn, "Create appliance", icon("arrow-right", 16));
      }
      back.style.display = step === 2 ? "" : "none";
      card.querySelector("h2").textContent = step === 1 ? "Set up your appliance" : (existingVault ? "Unlock the existing vault" : "Protect the vault");
    }

    function validateStep1() {
      if (!user.value.trim()) return "Choose an admin username";
      if (pw.input.value.length < state.min_password) return `Password must be at least ${state.min_password} characters`;
      if (strength(pw.input.value) < 2) return "Pick a stronger password — mix words, numbers and symbols";
      if (pw.input.value !== pw2.input.value) return "Passwords don't match";
      return "";
    }
    function masterValue() {
      if (mode === "generate") return saved.checked ? generated : null;
      if (mode === "same") return pw.input.value;
      return master.input.value;
    }
    function validateStep2() {
      if (mode === "generate" && !saved.checked) return "Confirm you've saved the passphrase";
      const m = masterValue() || "";
      if (m.length < state.min_master) return mode === "same" ? `To reuse it as the vault passphrase your password needs ${state.min_master}+ characters` : `Passphrase must be at least ${state.min_master} characters`;
      if (mode === "custom" && !existingVault && m !== master2.input.value) return "Passphrases don't match";
      return "";
    }

    const form = h("form", { onsubmit: (e) => {
      e.preventDefault();
      const problem = step === 1 ? validateStep1() : validateStep2();
      if (problem) return err.show(problem);
      err.hide();
      if (step === 1 && needsMaster) { step = 2; render(); return; }
      submit(nextBtn, async () => {
        const r = await api("/studio/api/auth/setup", { body: { username: user.value.trim(), password: pw.input.value, master: needsMaster ? masterValue() : "" } });
        (r.notes || []).slice(0, 3).forEach((n) => toast(n, "info"));
        location.href = r.next;
      }, err);
    } }, body, h("div", { class: "row", style: { marginTop: "6px" } }, back, h("div", { style: { flex: 1 } }), nextBtn));
    nextBtn.classList.remove("lg");

    fill(card, h("h2"), h("div", { class: "sub" }, "Two quick steps and you're composing."), stepper, err.el,
      state.master_from_env ? h("div", { class: "alert info", style: { marginBottom: "14px" } }, icon("lock", 18),
        h("div", null, "The vault passphrase is provided by the environment (APPLIANCE_MASTER_PASSWORD).")) : null, form);
    render();
    setTimeout(() => user.select(), 50);
  }

  api("/studio/api/auth/state").then((state) => {
    const wantSetup = location.pathname.endsWith("/setup");
    const name = (state.appliance && state.appliance.name) || "Data & AI";
    document.title = `${wantSetup ? "Set up" : "Sign in"} · DataPlatform ${name} Studio`;
    document.getElementById("brand").textContent = `DataPlatform · ${name} Appliance`;
    const HERO = {
      "data-pro": ["Integrate, model, govern and publish.", "No-code integration, dbt on BigQuery and Trino, Atlan governance, Superset and Power BI — one appliance."],
      "ml-scientist": ["From exploration to monitored models.", "Features, experiments, registry, blue/green · shadow · canary serving, drift and retraining — one appliance."],
      "ai-engineer": ["LLM apps wired to the enterprise.", "Model harness, MCP tools, dispute automation and call tagging over the systems you already use."],
    }[state.appliance && state.appliance.persona];
    if (HERO) { document.getElementById("hero-title").textContent = HERO[0]; document.getElementById("hero-sub").textContent = HERO[1]; }
    (wantSetup && state.setup_needed ? renderSetup : renderLogin)(state);
  }).catch(() => fill(card, h("div", { class: "alert error" }, icon("error", 18), h("div", null, "Studio is not reachable. Refresh in a moment."))));
})();
