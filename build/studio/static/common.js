/* Studio — helpers shared by the sign-in screens and the app shell.
   Exposes window.StudioUI. Builds on window.BSP (UI kit). */
(function () {
  "use strict";
  const { h, icon } = BSP;

  const COMMON = ["password", "passw0rd",  "dataplatform", "letmein", "welcome", "qwerty", "123456", "admin", "changeme", "secret"];
  const LABELS = ["", "Weak", "Fair", "Good", "Strong"];

  // 0 (empty) … 4 (strong): rough entropy estimate, penalising repeats and common words.
  function strength(pw) {
    if (!pw) return 0;
    let pool = 0;
    if (/[a-z]/.test(pw)) pool += 26;
    if (/[A-Z]/.test(pw)) pool += 26;
    if (/[0-9]/.test(pw)) pool += 10;
    if (/[^A-Za-z0-9]/.test(pw)) pool += 33;
    const unique = new Set(pw).size / pw.length;
    let bits = pw.length * Math.log2(Math.max(pool, 2)) * Math.min(1, 0.4 + unique);
    if (COMMON.some((w) => pw.toLowerCase().includes(w))) bits *= 0.5;
    return bits < 30 ? 1 : bits < 50 ? 2 : bits < 70 ? 3 : 4;
  }

  /* Password input with show/hide and (optionally) a strength meter.
     Returns {el, input, valid()}. */
  function passwordField({ label, name, min, meter, autocomplete, placeholder, hint, value }) {
    const input = h("input", { class: "input", type: "password", name, id: `f-${name}`, autocomplete: autocomplete || "current-password",
      placeholder: placeholder || "", value: value || "", spellcheck: "false" });
    const eye = h("button", { type: "button", class: "btn ghost icon sm eye", "aria-label": "Show password" }, icon("visibility", 15));
    eye.onclick = () => {
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      eye.replaceChildren(icon(show ? "visibility-off" : "visibility", 15));
      eye.setAttribute("aria-label", show ? "Hide password" : "Show password");
    };
    const bar = meter ? h("div", { class: "strength", "aria-hidden": "true" }, h("span"), h("span"), h("span"), h("span")) : null;
    const note = h("div", { class: "hint" }, hint || "");
    const update = () => {
      if (!meter) return;
      const v = input.value, s = strength(v);
      bar.className = `strength s${v ? s : 0}`;
      note.textContent = !v ? (hint || "") : (min && v.length < min) ? `At least ${min} characters` : LABELS[s];
    };
    input.addEventListener("input", update);
    update();
    const el = h("div", { class: "field" }, label ? h("label", { for: `f-${name}` }, label) : null,
      h("div", { class: "pw-wrap" }, input, eye), bar, note);
    return { el, input, valid: () => input.value.length >= (min || 1), update };
  }

  /* Small (?) button that opens an explanatory popover. */
  function help(text) {
    const btn = h("button", { type: "button", class: "help", "aria-label": "What is this?", "aria-expanded": "false" }, icon("question", 15));
    let pop = null;
    const close = () => { pop && pop.remove(); pop = null; btn.setAttribute("aria-expanded", "false"); document.removeEventListener("click", outside, true); document.removeEventListener("keydown", esc); };
    const outside = (e) => { if (pop && !pop.contains(e.target) && e.target !== btn && !btn.contains(e.target)) close(); };
    const esc = (e) => { if (e.key === "Escape") { close(); btn.focus(); } };
    btn.onclick = (e) => {
      e.stopPropagation();
      if (pop) return close();
      pop = h("div", { class: "popover help-pop", role: "tooltip" }, typeof text === "string" ? text : text());
      document.body.appendChild(pop);
      const r = btn.getBoundingClientRect();
      const left = Math.min(window.scrollX + r.left - 12, window.scrollX + window.innerWidth - pop.offsetWidth - 12);
      pop.style.top = `${window.scrollY + r.bottom + 6}px`;
      pop.style.left = `${Math.max(12, left)}px`;
      btn.setAttribute("aria-expanded", "true");
      document.addEventListener("click", outside, true);
      document.addEventListener("keydown", esc);
    };
    return btn;
  }

  function initials(name) {
    const parts = String(name || "?").split(/[\s._@-]+/).filter(Boolean);
    return ((parts[0] || "?")[0] + (parts[1] ? parts[1][0] : "")).toUpperCase();
  }

  function fmtMB(mb) {
    return mb >= 1024 ? `${(mb / 1024).toFixed(mb >= 10240 ? 0 : 1)} GB` : `${Math.round(mb)} MB`;
  }

  function fmtUptime(iso) {
    if (!iso) return "";
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return "just started";
    if (s < 3600) return `${Math.floor(s / 60)}m`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
    return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
  }

  // replaceChildren() that skips null/false (replaceChildren would print "null").
  function fill(el, ...kids) {
    el.replaceChildren();
    return BSP.append(el, kids);
  }

  /* Connection strings with copy buttons (catalog `endpoints`). */
  function endpointList(endpoints) {
    return h("div", { class: "endpoints" }, endpoints.map((e) => {
      const value = e.value.replace("<facade>", location.host);
      return h("div", { class: "endpoint" }, h("span", { class: "lbl" }, e.label), h("code", { class: "v", title: value }, value),
        h("button", { type: "button", class: "btn ghost icon sm", title: "Copy", "aria-label": `Copy ${e.label}`, onclick: () => BSP.copy(value, e.label) }, icon("copy", 14)));
    }));
  }

  window.StudioUI = { fill, endpointList, strength, passwordField, help, initials, fmtMB, fmtUptime, STRENGTH_LABELS: LABELS };
})();
