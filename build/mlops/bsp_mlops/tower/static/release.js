/* Governance pages of the control tower, registered into tower.js's router via window.BSPPages:
     release   Release & approvals — Dev → Staging → Prod, gate results, four-eyes approval, rollback,
               serving ⇄ registry reconciliation, reproductions, audit trail
     outcomes  Outcomes & experiments — delayed-label performance, champion / challenger statistics,
               retention-offer A/B
     slo       SLO, access & alerts — HTTP latency / errors vs SLO, callers, client tokens, alert workflow
     teach     Teach — the A–G capability checklist with live evidence, the OSS ↔ GCP ownership map,
               code viewer, and "Run the full lifecycle"
   Data: /mlops/api/release, /api/outcomes, /api/slo, /api/evidence, /api/code, /api/lifecycle. */
(function () {
  "use strict";
  const { h, icon, toast, table, api: mkApi, busy, fmtAgo, fmtTime, drawer, confirm, prompt } = BSP;
  const api = mkApi("/mlops");
  const f3 = (v) => (v == null || isNaN(v) ? "—" : Number(v).toFixed(3));
  const f4 = (v) => (v == null || isNaN(v) ? "—" : Number(v).toFixed(4));
  const sgn = (v) => (v == null || isNaN(v) ? "—" : (v >= 0 ? "+" : "") + Number(v).toFixed(4));
  const pct = (v) => (v == null || isNaN(v) ? "—" : (100 * v).toFixed(1) + "%");
  const int = (v) => (v == null ? "—" : Number(v).toLocaleString());
  const badge = (kind, text) => h("span", { class: `badge ${kind}` }, h("span", { class: "dot" }), text);
  const stat = (label, value, delta) => h("div", { class: "stat" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), delta ? h("div", { class: "delta" }, delta) : null);
  const card = (title, body, actions) => h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", null, title), actions || null), body);
  const STATUS = { pass: "success", warn: "warning", fail: "error", skip: "neutral", completed: "success", approved: "success", pending_approval: "info",
    gates_failed: "error", rejected: "error", superseded: "neutral", running: "info", succeeded: "success", failed: "error", open: "error",
    acknowledged: "warning", resolved: "success", identical: "success", within_tolerance: "warning", diverged: "error",
    implemented: "success", delegated: "info", needs_engineering: "warning", roadmap: "neutral",
    production: "success", staging: "info", dev: "neutral", archived: "neutral", rejected_stage: "error", rolled_back: "warning" };
  const st = (s, label) => badge(STATUS[s] || "neutral", label || String(s || "—").replace(/_/g, " "));
  const STAGE_COL = { dev: "#828487", staging: "#3380f0", production: "#0b6e79", archived: "#b0b3b8", rejected: "#c0392b", rolled_back: "#e0a800" };

  function codeLink(r) {
    if (!r) return null;
    const label = `${r.file}${r.line ? ":" + r.line : ""}`;
    if (!r.url) return h("span", { class: "mono caption", title: r.symbol }, label);
    return h("a", { href: "#", class: "mono", title: r.symbol, onclick: (e) => { e.preventDefault(); showCode(r); } }, label);
  }
  async function showCode(r) {
    try {
      const d = await api(r.url.replace(/^\/mlops/, "") + "&context=1");
      drawer({ title: `${d.symbol} — ${d.file}:${d.line}`, wide: true, body: h("div", { class: "stack" },
        h("p", { class: "caption" }, "Read-only view of the code that runs in this appliance (resolved live, so the line numbers are current)."),
        h("pre", { class: "log" }, d.source)) });
    } catch (e) { toast(e.message || String(e), "error"); }
  }
  function gateTable(gates) {
    return table({ pageSize: 20, rows: gates || [], columns: [
      { key: "name", label: "Gate", render: (g) => h("span", null, h("b", null, g.name), g.blocking ? null : h("span", { class: "caption" }, " · advisory")) },
      { key: "status", label: "Result", render: (g) => st(g.status) },
      { key: "value", label: "Value", render: (g) => h("span", { class: "mono" }, g.value == null ? "—" : String(g.value)) },
      { key: "threshold", label: "Threshold", render: (g) => h("span", { class: "mono" }, g.threshold == null ? "—" : String(g.threshold)) },
      { key: "detail", label: "Evidence", render: (g) => h("span", { style: { whiteSpace: "normal" } }, g.detail) },
    ] }).el;
  }
  function showPromotion(id) {
    api(`/api/release/${id}`).then((p) => {
      const nr = (p.gates || []).find((g) => g.key === "non_regression");
      const seg = (p.gates || []).find((g) => g.key === "segment_parity");
      const sch = (p.gates || []).find((g) => g.key === "schema_contract");
      drawer({ title: `Promotion #${p.id} · v${p.model_version} → ${p.to_stage}`, wide: true, body: h("div", { class: "stack" },
        h("div", { class: "row wrap" }, st(p.status), st(p.action), p.gates_passed === true ? badge("success", "all blocking gates passed") : p.gates_passed === false ? badge("error", "blocked by gates") : null),
        h("dl", { class: "kv" }, ...Object.entries({
          "Requested by": `${p.requested_by || "—"} (${p.requested_role || "—"}, ${p.requested_source || "—"}) · ${fmtTime(p.created_at)}`,
          "Decided by": p.approved_by ? `${p.approved_by} (${p.approved_role || "—"}, ${p.approved_source || "—"}) · ${fmtTime(p.approved_at)}` : "—",
          "Note": p.decision_note || "—", "Baseline": p.baseline_version ? "v" + p.baseline_version : "—",
          "Champion": `${p.champion_before ? "v" + p.champion_before : "—"} → ${p.champion_after ? "v" + p.champion_after : "—"}`,
          "Hold-out": p.holdout ? `${(p.holdout.window || []).join(" .. ")} · ${int(p.holdout.rows)} rows · ${int(p.holdout.positives)} churners${(p.holdout.flags || []).length ? " · " + p.holdout.flags.join("; ") : ""}` : "—",
          "Artifact sha256": p.artifact_sha256 || "—", "Data snapshot sha256": p.data_snapshot_sha256 || "—", "Code sha256": p.code_sha256 || "—",
          "Environment lock sha256": p.env_lock_sha256 || "—", "Git commit": p.git_sha || "—",
        }).flatMap(([k, v]) => [h("dt", null, k), h("dd", { class: /sha|commit/i.test(k) ? "mono" : null }, v)])),
        p.evidence_run_id ? h("a", { class: "btn secondary sm", href: `/mlflow/#/runs/${p.evidence_run_id}`, target: "_blank" }, icon("open-in-new", 14), "Gate evidence run in MLflow (gates.json, hold-out scores, promotion records)") : null,
        (p.gates || []).length ? h("h4", null, "Gate results") : null, (p.gates || []).length ? gateTable(p.gates) : null,
        nr && nr.data && nr.data.delta_auc != null ? h("div", { class: "alert info" }, icon("info", 16), h("div", null, `Non-regression: ΔAUC ${sgn(nr.data.delta_auc)} with 95 % paired-bootstrap CI [${sgn(nr.data.ci_low)}, ${sgn(nr.data.ci_high)}] (${nr.data.n_boot} resamples of the SAME hold-out rows); DeLong p = ${nr.data.delong_p}. Pass rule: CI lower bound ≥ −margin (non-inferiority).`)) : null,
        seg && seg.data && seg.data.segments ? h("div", null, h("h4", null, "Segment parity"), table({ pageSize: 12, rows: seg.data.segments, columns: [
          { key: "segment", label: "Segment" }, { key: "n", label: "Rows", num: true, render: (r) => int(r.n) }, { key: "positives", label: "Churners", num: true },
          { key: "auc", label: "AUC", num: true, render: (r) => f3(r.auc) }, { key: "auc_baseline", label: "Baseline AUC", num: true, render: (r) => f3(r.auc_baseline) },
          { key: "delta", label: "Δ", num: true, render: (r) => sgn(r.delta) }, { key: "evaluated", label: "Judged", render: (r) => (r.evaluated ? "yes" : "too few churners") }] }).el) : null,
        sch && sch.data && sch.data.checks ? h("div", null, h("h4", null, "Schema & data contract checks"), table({ pageSize: 12, rows: sch.data.checks, columns: [
          { key: "check", label: "Check", render: (r) => h("span", { style: { whiteSpace: "normal" } }, r.check) }, { key: "result", label: "Result", render: (r) => st(r.result) }, { key: "detail", label: "Detail" }] }).el,
          h("div", { class: "caption" }, `Source: ${sch.data.source}`)) : null,
        h("h4", null, "All records of this version"), table({ pageSize: 10, rows: p.related || [], onRowClick: (r) => showPromotion(r.id), columns: [
          { key: "id", label: "#" }, { key: "action", label: "Action" }, { key: "to_stage", label: "To" }, { key: "status", label: "Status", render: (r) => st(r.status) },
          { key: "created_at", label: "When", render: (r) => fmtTime(r.created_at) }] }).el) });
    }).catch((e) => toast(e.message || String(e), "error"));
  }

  // ── Release & approvals ────────────────────────────────────────────────
  async function release(ctx) {
    const { render, head, every, err } = ctx;
    const [d, me, rep] = await Promise.all([api("/api/release"), api("/api/me"), api("/api/reproduce")]);
    const q = new URLSearchParams((location.hash.split("?")[1] || ""));
    if (q.get("id")) setTimeout(() => showPromotion(q.get("id")), 50);
    const al = d.aliases || {};
    const sv = d.serving || {};
    const div = sv.divergence || [];
    const lanes = ["dev", "staging", "production", "archived", "rejected", "rolled_back"];
    const vs = (d.versions || []).filter((v) => !v.negative_control || v.stage === "rejected");
    const lane = (s) => h("div", { class: "rl-lane" }, h("div", { class: "rl-lane-head", style: { borderColor: STAGE_COL[s] } }, s.replace("_", " "), h("span", { class: "caption" }, ` · ${vs.filter((v) => v.stage === s).length}`)),
      ...vs.filter((v) => v.stage === s).slice(0, s === "archived" || s === "dev" ? 6 : 8).map((v) => h("div", { class: "rl-chip", title: `${v.model_kind || ""} · test AUC ${f4(v.test_auc)}` },
        h("b", null, "v" + v.version), " ", ...(v.aliases || []).map((a) => badge(a === "champion" ? "success" : a === "staging" ? "info" : "neutral", a)),
        v.negative_control ? badge("warning", "negative control") : null,
        h("div", { class: "caption" }, `${v.model_kind || "?"} · AUC ${f3(v.test_auc)}${v.approved_by ? " · approved by " + v.approved_by : ""}${v.gates ? " · gates " + v.gates : ""}`))));
    const pipelineView = h("div", { class: "rl-lanes" }, lanes.map(lane));
    const canApprove = me.can_approve;
    const pend = d.pending || [];
    const pendCard = card(`Awaiting production approval (${pend.length})`, pend.length ? h("div", { class: "stack" }, pend.map((p) => h("div", { class: "rl-pending" },
      h("div", { class: "row between wrap" },
        h("div", null, h("b", null, `#${p.id} · v${p.model_version} → production`), h("div", { class: "caption" }, `requested by ${p.requested_by} (${p.requested_role || "—"}, ${p.requested_source}) ${fmtAgo(p.created_at)} · gates ${p.gates_passed ? "passed" : "—"} · baseline ${p.baseline_version ? "v" + p.baseline_version : "none"}`)),
        h("div", { class: "row" },
          h("button", { class: "btn ghost sm", onclick: () => showPromotion(p.id) }, "Gate evidence"),
          h("button", { class: "btn sm primary", disabled: (!canApprove || (me.user || "").toLowerCase() === (p.requested_by || "").toLowerCase()) || null, title: !canApprove ? `needs one of ${me.approver_roles.join(", ")}` : (me.user || "").toLowerCase() === (p.requested_by || "").toLowerCase() ? "four-eyes: you requested this release" : "", onclick: async (e) => {
            const note = await prompt({ title: `Approve v${p.model_version} for production?`, label: "Approval note (recorded)", placeholder: "e.g. reviewed gates, CI and shadow evidence", okText: "Approve", body: h("p", null, `Champion moves from v${al.champion || "—"} to v${p.model_version}; the gateway follows the registry within seconds. You are ${me.user} (${me.role}).`) });
            if (note === null) return;
            busy(e.currentTarget, () => api(`/api/release/${p.id}/approve`, { body: { note } }).then(() => { toast(`v${p.model_version} approved to production`, "success"); route(); }).catch(err));
          } }, icon("check", 14), "Approve"),
          h("button", { class: "btn sm secondary", disabled: !canApprove || null, onclick: async (e) => {
            const note = await prompt({ title: `Reject v${p.model_version}?`, label: "Reason (recorded)", okText: "Reject" });
            if (note === null) return;
            busy(e.currentTarget, () => api(`/api/release/${p.id}/reject`, { body: { note } }).then(() => { toast("Rejected", "success"); route(); }).catch(err));
          } }, "Reject"))))))
      : h("p", { class: "muted" }, "Nothing waiting. A version enters here after passing every automated gate (Submit below, or automatically after a retrain)."),
      h("span", { class: "caption" }, `you: ${me.user || "not signed in"} · ${me.role || "—"} · ${canApprove ? "can approve (not your own requests)" : "cannot approve"}`));
    const subSel = h("select", { class: "select" }, (d.versions || []).filter((v) => !(v.aliases || []).includes("champion")).slice(0, 25).map((v) => h("option", { value: v.version }, `v${v.version} · ${v.model_kind || "?"} · ${v.stage}`)));
    const repSel = h("select", { class: "select" }, (d.versions || []).slice(0, 25).map((v) => h("option", { value: v.version, selected: (v.aliases || []).includes("champion") || null }, `v${v.version}${(v.aliases || []).length ? " · " + v.aliases.join(", ") : ""}`)));
    const actions = card("Actions", h("div", { class: "stack" },
      h("div", { class: "row wrap" }, h("span", null, "Submit for release:"), subSel,
        h("button", { class: "btn primary", onclick: async (e) => {
          if (!(await confirm({ title: `Run the release gates on v${subSel.value}?`, body: "Gates run against the current champion on a hold-out unseen by both models (about 10–30 s). Pass → staging + challenger in shadow + a production request for an approver. Fail → recorded as blocked.", okText: "Run gates" }))) return;
          busy(e.currentTarget, () => api("/api/release/submit", { body: { version: subSel.value } }).then((r) => { toast(r.staged ? `v${subSel.value} staged — approval requested (#${r.pending && r.pending.id})` : `v${subSel.value} blocked by the gates`, r.staged ? "success" : "error"); route(); showPromotion(r.promotion.id); }).catch(err));
        } }, icon("play", 14), "Run gates")),
      h("div", { class: "row wrap" }, h("span", null, "Reproduce:"), repSel,
        h("button", { class: "btn secondary", onclick: (e) => busy(e.currentTarget, () => api("/api/reproduce", { body: { version: repSel.value } }).then(() => { toast(`Reproducing v${repSel.value} from its recorded recipe…`, "success"); setTimeout(route, 4000); }).catch(err)) }, icon("refresh", 14), "Reproduce")),
      h("div", { class: "row wrap" },
        h("button", { class: "btn secondary", disabled: !canApprove || null, onclick: async (e) => {
          const reason = await prompt({ title: "Roll back production?", label: "Reason (recorded)", okText: "Roll back", body: h("p", null, "Champion is re-pointed to the previous APPROVED production version; the current one stays as challenger in shadow for diagnosis.") });
          if (reason === null) return;
          busy(e.currentTarget, () => api("/api/release/rollback", { body: { reason } }).then((r) => { toast(`Rolled back to v${r.promotion.champion_after}`, "success"); route(); }).catch(err));
        } }, icon("history", 14), "Roll back champion"),
        h("button", { class: "btn ghost", onclick: (e) => busy(e.currentTarget, () => api("/api/release/reconcile", { body: { policy: "shadow" } }).then(() => { toast("Serving re-derived from the registry (challenger in shadow)", "success"); route(); }).catch(err)) }, "Reconcile serving → shadow"),
        h("button", { class: "btn ghost", onclick: (e) => busy(e.currentTarget, () => api("/api/release/reconcile", { body: { policy: "canary", canary_weight: 0.1 } }).then(() => { toast("Challenger on a 10 % canary", "success"); route(); }).catch(err)) }, "→ canary 10 %"))));
    const eff = sv.effective || {};
    const serving = card("Serving ⇄ registry", h("div", { class: "stack" },
      h("div", { class: "row wrap" }, badge(sv.in_sync === false ? "error" : "success", sv.in_sync === false ? "registry and serving DISAGREE" : "serving follows the registry"),
        h("span", null, `mode ${sv.mode || "—"}`), ...(eff.serving || []).map((s) => h("span", { class: "mono" }, `v${s.version} ${pct(s.share)} (${s.why})`)), eff.shadow ? h("span", { class: "mono" }, `shadow v${eff.shadow}`) : null),
      h("div", { class: "caption" }, `registry: ${Object.entries(eff.registry || al).map(([k, v]) => `${k} v${v}`).join(" · ") || "—"}`),
      ...div.map((x) => h("div", { class: `alert ${x.level === "error" ? "error" : x.level === "warning" ? "warning" : "info"}` }, icon(x.level === "error" ? "error" : x.level === "warning" ? "warning" : "info", 16), h("div", null, h("b", null, x.code.replace(/_/g, " ")), " — ", x.message)))));
    const hist = table({ pageSize: 12, rowKey: "id", rows: d.history || [], onRowClick: (r) => showPromotion(r.id), columns: [
      { key: "id", label: "#", sort: true }, { key: "created_at", label: "When", render: (r) => fmtTime(r.created_at) },
      { key: "model_version", label: "Version", render: (r) => h("b", null, "v" + r.model_version) }, { key: "action", label: "Action" },
      { key: "to_stage", label: "→", render: (r) => `${r.from_stage || "—"} → ${r.to_stage}` }, { key: "status", label: "Status", render: (r) => st(r.status) },
      { key: "requested_by", label: "Requested by" }, { key: "approved_by", label: "Decided by", render: (r) => r.approved_by ? `${r.approved_by} (${r.approved_source || "—"})` : "—" },
      { key: "gates_passed", label: "Gates", render: (r) => (r.gates_passed === true ? st("pass", "passed") : r.gates_passed === false ? st("fail", "failed") : "—") },
      { key: "champ", label: "Champion", render: (r) => (r.champion_after ? `v${r.champion_before || "—"} → v${r.champion_after}` : "—") }] });
    const reps = table({ pageSize: 8, rows: rep.runs || [], columns: [
      { key: "id", label: "#" }, { key: "created_at", label: "When", render: (r) => fmtTime(r.created_at) }, { key: "model_version", label: "Version", render: (r) => "v" + r.model_version },
      { key: "parity", label: "Parity", render: (r) => (r.status === "running" ? st("running") : r.parity ? st(r.parity) : st(r.status)) },
      { key: "data_hash_match", label: "Data hash", render: (r) => (r.data_hash_match == null ? "not recorded" : r.data_hash_match ? "match" : "DIFFERS") },
      { key: "code_match", label: "Code", render: (r) => (r.code_match == null ? "not recorded" : r.code_match ? "same" : "changed") },
      { key: "env_match", label: "Env lock", render: (r) => (r.env_match == null ? "not recorded" : r.env_match ? "identical" : `${(r.env_diff && r.env_diff.changed || []).length} changed`) },
      { key: "max", label: "max |Δ prediction|", render: (r) => (r.details && r.details.max_prediction_diff != null ? Number(r.details.max_prediction_diff).toExponential(1) : "—") },
      { key: "run", label: "", render: (r) => (r.repro_run_id ? h("a", { href: `/mlflow/#/runs/${r.repro_run_id}`, target: "_blank" }, "MLflow") : "") }] });
    const audit = table({ pageSize: 10, rows: d.audit || [], columns: [
      { key: "ts", label: "When", render: (r) => fmtTime(r.ts) }, { key: "actor", label: "Who", render: (r) => `${r.actor || "—"}${r.role ? " (" + r.role + ")" : ""}` },
      { key: "source", label: "Identity source" }, { key: "action", label: "Action", render: (r) => h("span", { class: "mono" }, r.action) }, { key: "target", label: "Target" },
      { key: "details", label: "Details", render: (r) => h("span", { class: "mono caption" }, JSON.stringify(r.details || {}).slice(0, 120)) }] });
    const th = d.thresholds || {};
    render([
      head("Release & approvals", "Dev → Staging → Production for every registered version: automated, recorded gates; a human approver who is not the requester; rollback to the previous approved version. Serving is derived from the registry."),
      ...(d.governance_warnings || []).map((w) => h("div", { class: "alert warning" }, icon("warning", 18), h("div", null, h("b", null, "Governance: "), w))),
      div.some((x) => x.level === "error") ? h("div", { class: "alert error" }, icon("error", 18), h("div", null, h("b", null, "Registry and serving disagree. "), div.filter((x) => x.level === "error").map((x) => x.message).join(" "))) : null,
      h("div", { class: "grid cols-4" }, stat("Champion (production)", al.champion ? "v" + al.champion : "—", "serves customers"), stat("Staging", al.staging ? "v" + al.staging : "—", "passed every gate"),
        stat("Challenger", al.challenger ? "v" + al.challenger : "—", eff.shadow ? "shadow-scored" : "no live traffic"), stat("Awaiting approval", int(pend.length), `approver roles: ${(d.approver_roles || []).join(", ")}`)),
      h("div", { class: "section" }, card("Release pipeline", pipelineView)),
      h("div", { class: "section grid cols-2" }, pendCard, serving),
      h("div", { class: "section" }, actions),
      h("div", { class: "section" }, card("Promotion records", hist.el, h("span", { class: "caption" }, "click a row for gate evidence"))),
      h("div", { class: "section" }, card("Reproductions", reps.el)),
      h("div", { class: "section" }, card("Gate thresholds (environment-configurable)", h("dl", { class: "kv" }, ...Object.entries(th).flatMap(([k, v]) => [h("dt", null, k), h("dd", { class: "mono" }, String(v))])))),
      h("div", { class: "section" }, card("Audit trail (latest)", audit.el, h("a", { class: "btn ghost sm", href: "/mlops/api/audit?limit=2000", target: "_blank" }, "JSON"))),
    ]);
    every(15000, () => release(ctx));
  }

  // ── Outcomes & experiments ─────────────────────────────────────────────
  async function outcomes(ctx) {
    const { render, head, err } = ctx;
    const d = await api("/api/outcomes");
    const L = d.labels || {};
    const overall = (L.overall || []);
    const cohorts = (L.cohorts || []).filter((c) => c.realised_auc != null || c.n_pending);
    const versions = [...new Set(cohorts.map((c) => `${c.role}:${c.model_version}`))];
    const pts = versions.map((k, i) => ({ name: k.replace(":", " v"), color: Charts.PALETTE[i % Charts.PALETTE.length],
      points: cohorts.filter((c) => `${c.role}:${c.model_version}` === k && c.realised_auc != null).map((c) => [new Date(c.cohort).getTime(), c.realised_auc]) }));
    const exp = overall.filter((o) => o.role === "served" && o.expected_auc != null);
    const chart = Charts.line({ series: pts, yMin: 0.5, yMax: 1, thresholds: exp.slice(0, 2).map((o, i) => ({ y: o.expected_auc, label: `expected v${o.model_version} ${f3(o.expected_auc)}`, color: i ? "#ff8205" : "#0b6e79" })),
      xFmt: (x) => new Date(x).toISOString().slice(0, 7), yFmt: (v) => v.toFixed(2) });
    const ex = d.experiments || [];
    const lastOf = (k) => ex.find((x) => x.kind === k);
    const sh = lastOf("shadow_paired"), ca = lastOf("canary"), ab = lastOf("ab_retention_offer");
    const shR = sh ? sh.result : null, abR = ab ? ab.result : null, caR = ca ? ca.result : null;
    const run = (what, body) => (e) => busy(e.currentTarget, () => api(`/api/outcomes/${what}`, { body: body || {} }).then(() => { toast("Done", "success"); outcomes(ctx); }).catch(err));
    render([
      head("Outcomes & experiments", "What the model actually achieved once the truth arrived — delayed-label performance, champion vs challenger with confidence intervals, and a business A/B readout.",
        [h("button", { class: "btn primary", onclick: run("labels") }, icon("refresh", 14), "Evaluate delayed labels"), h("button", { class: "btn secondary", onclick: run("shadow") }, "Shadow comparison"),
          h("button", { class: "btn secondary", onclick: run("canary") }, "Canary comparison"), h("button", { class: "btn secondary", onclick: run("ab", { effect: 0.25 }) }, "Run retention A/B")]),
      h("div", { class: "grid cols-4" }, stat("Scored requests", int(L.n_scored), L.created_at ? "evaluated " + fmtAgo(L.created_at) : "not evaluated yet"),
        stat("Labels arrived", int(L.n_labelled), "90-day churn window closed"), stat("Labels pending", int(L.n_pending), "never guessed"),
        stat("Degradation alert", L.breached ? "YES" : "no", `realised AUC < expected − ${L.breach_threshold ?? "—"}`)),
      h("div", { class: "section grid cols-2" },
        card("Realised AUC by scoring cohort vs expected at release", h("div", null, chart, Charts.legend(pts), h("p", { class: "caption" }, L.note || ""))),
        card("Per version (served and shadow)", table({ pageSize: 10, rows: overall, columns: [
          { key: "role", label: "Role" }, { key: "model_version", label: "Version", render: (r) => "v" + r.model_version }, { key: "n", label: "Scored", num: true, render: (r) => int(r.n) },
          { key: "n_labelled", label: "Labelled", num: true, render: (r) => int(r.n_labelled) }, { key: "realised_auc", label: "Realised AUC", num: true, render: (r) => f4(r.realised_auc) },
          { key: "expected_auc", label: "Expected", num: true, render: (r) => f4(r.expected_auc) }, { key: "auc_gap", label: "Gap", num: true, render: (r) => sgn(r.auc_gap) },
          { key: "in_sample_excluded", label: "In-sample excluded", num: true, render: (r) => int(r.in_sample_excluded) },
          { key: "lift_at_10", label: "Lift@10", num: true, render: (r) => f3(r.lift_at_10) }, { key: "ece", label: "ECE", num: true, render: (r) => f4(r.ece) }] }).el)),
      h("div", { class: "section" }, card("Cohorts (snapshot month) — label due 90 days after the snapshot", table({ pageSize: 12, rows: L.cohorts || [], columns: [
        { key: "role", label: "Role" }, { key: "model_version", label: "Version", render: (r) => "v" + r.model_version }, { key: "cohort", label: "Cohort" }, { key: "label_due", label: "Label due" },
        { key: "in_sample", label: "Judged", render: (r) => (r.in_sample ? h("span", { class: "caption" }, "in-sample (trained on)") : "out of sample") },
        { key: "n", label: "Scored", num: true }, { key: "n_labelled", label: "Labelled", num: true }, { key: "n_pending", label: "Pending", num: true },
        { key: "realised_auc", label: "Realised AUC", num: true, render: (r) => f4(r.realised_auc) }, { key: "realised_rate", label: "Churn", num: true, render: (r) => pct(r.realised_rate) },
        { key: "mean_score", label: "Mean score", num: true, render: (r) => pct(r.mean_score) }] }).el)),
      h("div", { class: "section grid cols-2" },
        card("Champion vs challenger — shadow (paired, same requests)", shR ? h("div", { class: "stack" },
          h("div", { class: "row wrap" }, badge(shR.ci_low > 0 ? "success" : shR.ci_high < 0 ? "error" : "neutral", shR.verdict || "—"), h("span", { class: "caption" }, `experiment #${sh.id} · ${fmtAgo(sh.created_at)}`)),
          h("dl", { class: "kv" }, ...Object.entries({ "Pair": `challenger v${shR.challenger_version} vs champion v${shR.champion_version}`, "Pairs (out-of-sample labelled)": `${int(shR.n_pairs)} (${int(shR.n_labelled)}; ${int(shR.in_sample_excluded || 0)} in-sample excluded, judged after ${shR.out_of_sample_after || "—"})`,
            "AUC challenger / champion": `${f4(shR.auc_challenger)} / ${f4(shR.auc_champion)}`, "ΔAUC [95 % paired bootstrap CI]": `${sgn(shR.delta_auc)} [${sgn(shR.ci_low)}, ${sgn(shR.ci_high)}]`,
            "DeLong p": shR.delong_p == null ? "—" : Number(shR.delong_p).toPrecision(3), "Score correlation": f3(shR.corr), "Top-decile overlap": pct(shR.top_decile_overlap) }).flatMap(([k, v]) => [h("dt", null, k), h("dd", null, v)])))
          : h("p", { class: "muted" }, "No paired shadow traffic evaluated yet.")),
        card("Canary arms (independent samples)", caR && caR.arms ? h("div", { class: "stack" }, table({ rows: Object.entries(caR.arms).map(([k, v]) => ({ arm: k, ...v })), columns: [
          { key: "arm", label: "Arm" }, { key: "model_version", label: "Version", render: (r) => "v" + r.model_version }, { key: "n_labelled", label: "Labelled", num: true },
          { key: "realised_auc", label: "AUC", num: true, render: (r) => f4(r.realised_auc) }] }).el,
          caR.delta_auc != null ? h("div", { class: "caption" }, `ΔAUC canary − stable ${sgn(caR.delta_auc)} [${sgn(caR.ci_low)}, ${sgn(caR.ci_high)}] (independent bootstrap)`) : null)
          : h("p", { class: "muted" }, "No canary traffic evaluated yet."))),
      h("div", { class: "section" }, card("Business outcome — retention-offer A/B", abR ? h("div", { class: "stack" },
        h("div", { class: "alert warning" }, icon("warning", 16), h("div", null, h("b", null, "Simulated effect. "), abR.note)),
        h("div", { class: "grid cols-4" }, stat("Control churn", pct(abR.control.rate), `${int(abR.control.churned)} / ${int(abR.control.n)}`), stat("Offer churn", pct(abR.treatment.rate), `${int(abR.treatment.churned)} / ${int(abR.treatment.n)}`),
          stat("Uplift (abs)", pct(abR.abs_uplift), `95 % CI [${pct(abR.ci_low)}, ${pct(abR.ci_high)}]`), stat("p-value", abR.p_value == null ? "—" : Number(abR.p_value).toPrecision(2), abR.significant ? "significant at 5 %" : "not significant")),
        h("dl", { class: "kv" }, ...Object.entries({ "Month / champion": `${abR.month} · v${abR.champion_version}`, "Eligible (top risk)": `${int(abR.eligible)} customers (top ${pct(abR.target_share)})`,
          "Balance check (A/A, before the offer)": abR.balance_check ? `treatment ${pct(abR.balance_check.treatment_rate_without_offer)} vs control ${pct(abR.balance_check.control_rate)} (p ${Number(abR.balance_check.p_value).toPrecision(2)})` : "—",
          "Expected abs uplift (from the simulated effect)": pct(abR.expected_abs_uplift), "Customers per arm for 80 % power": int(abR.n_per_arm_for_80pct_power),
          "Saved per 1 000 offers": f3(abR.saved_per_1000_offers ? abR.saved_per_1000_offers : null),
          "Targeting value (champion vs challenger)": abR.targeting ? `capture ${pct(abR.targeting.capture_champion)} vs ${pct(abR.targeting.capture_challenger)} of churners in the top ${pct(abR.target_share)} (v${abR.champion_version} vs v${abR.targeting.challenger_version})` : "—" }).flatMap(([k, v]) => [h("dt", null, k), h("dd", null, v)])))
        : h("p", { class: "muted" }, "Run the A/B to see a readout."))),
      h("div", { class: "section" }, card("Experiment log", table({ pageSize: 10, rows: ex, columns: [{ key: "id", label: "#" }, { key: "created_at", label: "When", render: (r) => fmtTime(r.created_at) },
        { key: "kind", label: "Kind" }, { key: "name", label: "Name" }, { key: "created_by", label: "By" }] }).el)),
    ]);
  }

  // ── SLO, access & alerts ───────────────────────────────────────────────
  async function slo(ctx) {
    const { render, head, every, err } = ctx;
    const [d, me] = await Promise.all([api("/api/slo?minutes=60"), api("/api/me")]);
    const H = d.http || {}, I = d.instance || {}, S = H.slo || {};
    const ser = (H.series || []);
    const lat = Charts.line({ series: [{ name: "HTTP p95 (ms)", color: "#ff8205", points: ser.map((s) => [new Date(s.minute).getTime(), s.p95_ms]) }],
      thresholds: S.p95_request_ms ? [{ y: S.p95_request_ms, label: `SLO ${S.p95_request_ms} ms` }] : [], yFmt: (v) => v.toFixed(0) + " ms" });
    const alertRows = d.alerts || [];
    const alertsT = table({ pageSize: 10, rows: alertRows, columns: [
      { key: "id", label: "#" }, { key: "created_at", label: "Raised", render: (r) => fmtTime(r.created_at) }, { key: "kind", label: "Kind" },
      { key: "severity", label: "Severity", render: (r) => badge(r.severity === "critical" ? "error" : r.severity === "warning" ? "warning" : "info", r.severity) },
      { key: "title", label: "Alert", render: (r) => h("span", { style: { whiteSpace: "normal" } }, r.title) }, { key: "owner", label: "Owner" },
      { key: "status", label: "Status", render: (r) => st(r.status) },
      { key: "act", label: "", render: (r) => h("div", { class: "row" },
        r.status === "open" ? h("button", { class: "btn sm secondary", onclick: (e) => busy(e.currentTarget, () => api(`/api/alerts/${r.id}/ack`, { body: {} }).then(() => slo(ctx)).catch(err)) }, "Acknowledge") : null,
        r.status !== "resolved" ? h("button", { class: "btn sm ghost", onclick: async (e) => { const res = await prompt({ title: `Resolve alert #${r.id}`, label: "Resolution (recorded)", okText: "Resolve" }); if (res === null) return; busy(e.currentTarget, () => api(`/api/alerts/${r.id}/resolve`, { body: { resolution: res } }).then(() => slo(ctx)).catch(err)); } }, "Resolve") : h("span", { class: "caption" }, `${r.resolved_by || ""}`)) }] });
    const tokens = table({ pageSize: 8, rows: d.tokens || [], columns: [{ key: "name", label: "Token" }, { key: "scope", label: "Scope" }, { key: "created_by", label: "Issued by" },
      { key: "created_at", label: "Issued", render: (r) => fmtTime(r.created_at) }, { key: "sha256_prefix", label: "sha256", render: (r) => h("span", { class: "mono" }, r.sha256_prefix + "…") },
      { key: "revoked", label: "State", render: (r) => (r.revoked ? st("rejected", "revoked") : st("pass", "active")) },
      { key: "act", label: "", render: (r) => (r.name !== "service" && !r.revoked ? h("button", { class: "btn sm ghost", disabled: me.role !== "admin" || null, onclick: (e) => busy(e.currentTarget, () => api(`/api/gateway/tokens/${r.name}`, { method: "DELETE" }).then(() => slo(ctx)).catch(err)) }, "Revoke") : "") }] });
    render([
      head("SLO, access & alerts", `Scoring-API reliability against its SLO (owner: ${S.owner || "—"}), who is calling, client tokens, and the alert / incident workflow.`),
      h("div", { class: "grid cols-4" }, stat("HTTP p95 (60 min)", H.p95_ms == null ? "—" : H.p95_ms + " ms", `SLO ≤ ${S.p95_request_ms ?? "—"} ms · ${H.p95_ok === false ? "BREACH" : "ok"}`),
        stat("Availability", H.availability == null ? "—" : (100 * H.availability).toFixed(2) + "%", `SLO ≥ ${S.availability != null ? (100 * S.availability).toFixed(1) + "%" : "—"} · error budget used ${H.error_budget_consumed == null ? "—" : pct(H.error_budget_consumed)}`),
        stat("Requests (60 min)", int(H.requests), `5xx ${int(H.errors_5xx)} · 4xx ${int(H.errors_4xx)} · rejected unauthenticated ${int(H.unauthenticated_rejected)}`),
        stat("Per-instance model latency", I.p95 == null ? "—" : f3(I.p95) + " ms p95", `p50 ${f3(I.p50)} · p99 ${f3(I.p99)} ms`)),
      h("div", { class: "section grid cols-2" }, card("HTTP latency vs SLO (per minute, since gateway start)", lat),
        card("Callers (identity on every scoring request)", table({ rows: d.callers || [], columns: [{ key: "caller", label: "Caller" }, { key: "n", label: "Requests", num: true, render: (r) => int(r.n) }] }).el,
          h("span", { class: "caption" }, `auth mode: ${H.auth_mode || "—"}`))),
      h("div", { class: "section" }, card("Alerts & incidents", alertsT.el)),
      h("div", { class: "section grid cols-2" },
        card("Client tokens for /serve", h("div", { class: "stack" }, tokens.el,
          h("div", { class: "row" }, h("button", { class: "btn secondary", disabled: me.role !== "admin" || null, title: me.role !== "admin" ? "admins issue tokens" : "", onclick: async (e) => {
            const name = await prompt({ title: "Issue a client token", label: "Name (who will call /serve)", placeholder: "crm-batch" });
            if (!name) return;
            busy(e.currentTarget, () => api("/api/gateway/tokens", { body: { name } }).then((r) => { drawer({ title: `Token for ${r.name}`, body: h("div", { class: "stack" }, h("p", null, r.note), h("pre", { class: "codeblock" }, r.token), h("pre", { class: "codeblock" }, `curl -H 'Authorization: Bearer ${r.token}' -H 'content-type: application/json' \\\n  -d '{"ban":"3100000008"}' $FACADE/serve/v1/predict`)) }); slo(ctx); }).catch(err));
          } }, icon("key", 14), "Issue token")))),
        card("Retries & failure handling", h("dl", { class: "kv" }, ...Object.entries(d.retries || {}).flatMap(([k, v]) => [h("dt", null, k), h("dd", null, v)])))),
    ]);
    every(20000, () => slo(ctx));
  }

  // ── Teach: capability evidence + ownership map + run the lifecycle ─────
  async function teach(ctx) {
    const { render, head, err } = ctx;
    const [c, lc] = await Promise.all([api("/api/evidence"), api("/api/lifecycle")]);
    const filt = { area: "all", status: "all" };
    const host = h("div");
    function draw() {
      const items = c.items.filter((i) => (filt.area === "all" || i.area === filt.area) && (filt.status === "all" || i.status === filt.status));
      host.replaceChildren(...Object.entries(c.areas).filter(([a]) => filt.area === "all" || a === filt.area).map(([a, title]) => {
        const rows = items.filter((i) => i.area === a);
        if (!rows.length) return null;
        return h("div", { class: "section" }, card(`${a} · ${title}`, h("div", { class: "ev-list" }, rows.map((i) => h("div", { class: "ev-item" },
          h("div", { class: "ev-head" }, h("span", { class: "ev-id" }, i.id), h("b", null, i.title), st(i.status, i.status_label),
            i.status === "implemented" && !i.evidence_recorded ? badge("neutral", "no live record yet — run the lifecycle") : null),
          h("p", { class: "ev-how" }, i.how),
          i.limits ? h("p", { class: "ev-limits" }, icon("warning", 12), " ", h("b", null, "Limits: "), i.limits) : null,
          h("div", { class: "ev-grid" },
            h("div", null, h("div", { class: "eyebrow" }, "Evidence (live)"), i.evidence.length ? h("div", { class: "stack sm" }, i.evidence.map((e) => h("a", { href: e.href, target: e.href.startsWith("#") ? null : "_blank" }, icon(e.kind === "mlflow" ? "open-in-new" : "link", 12), " ", e.label))) : h("span", { class: "caption" }, "—")),
            h("div", null, h("div", { class: "eyebrow" }, "Code"), i.code.length ? h("div", { class: "stack sm" }, i.code.map(codeLink)) : h("span", { class: "caption" }, "—"),
              i.notebook ? h("div", { class: "caption", style: { marginTop: "6px" } }, icon("file", 12), " ", i.notebook.path) : null),
            h("div", null, h("div", { class: "eyebrow" }, "GCP-native equivalent"), h("span", null, i.gcp))))))));
      }).filter(Boolean));
    }
    const areaSel = h("select", { class: "select", onchange: (e) => { filt.area = e.target.value; draw(); } }, h("option", { value: "all" }, "All areas"), Object.entries(c.areas).map(([a, t]) => h("option", { value: a }, `${a} · ${t}`)));
    const stSel = h("select", { class: "select", onchange: (e) => { filt.status = e.target.value; draw(); } }, h("option", { value: "all" }, "All statuses"), Object.entries(c.status_labels).map(([k, t]) => h("option", { value: k }, t)));
    const own = table({ pageSize: 25, rows: c.ownership, columns: [
      { key: "stage", label: "Lifecycle stage", render: (r) => h("b", null, r.stage) }, { key: "oss", label: "OSS component (here)" }, { key: "gcp", label: "GCP-native equivalent" },
      { key: "boundary", label: "Integration boundary" }, { key: "status", label: "Status", render: (r) => st(r.status, r.status_label) }, { key: "note", label: "Note" }] });
    const last = (lc.runs || [])[0];
    const trials = h("input", { type: "number", class: "input", value: 6, min: 2, max: 30, style: { width: "70px" } });
    const fresh = h("input", { type: "checkbox" });
    const lifecycle = card("Run the full lifecycle", h("div", { class: "stack" },
      h("p", null, "Runs the source-controlled script ", h("code", null, "python -m bsp_mlops.demo"), " as you: integrate → EDA → selection → hold-outs → feature store → leaderboard → Optuna → register → release gates (+ a negative control) → your production request → routing drills → reconciled serving → paired champion / challenger statistics → drift → alert → retrain → gates → delayed-label evaluation → A/B → batch scores → reproduce → export bundle. ",
        h("b", null, "Production approval waits for another person with an approver role"), " (Release & approvals page) for up to 15 minutes."),
      h("div", { class: "row wrap" }, h("label", { class: "row" }, "Optuna trials", trials), h("label", { class: "switch" }, fresh, h("span", { class: "track" })), h("span", null, "Fresh release train (retire the current champion first)"),
        h("button", { class: "btn primary", onclick: (e) => busy(e.currentTarget, () => api("/api/lifecycle", { body: { trials: +trials.value, fresh_release_train: fresh.checked } }).then((r) => { toast(`Lifecycle run #${r.id} started`, "success"); teach(ctx); }).catch(err)) }, icon("play", 14), "Run the full lifecycle")),
      h("pre", { class: "codeblock" }, "make mlops-lifecycle REQUESTER=<you> APPROVER=<someone else>      # same script, from the host\npython -m bsp_mlops.evidence --markdown                             # this page as a table"),
      last ? h("div", { class: "row wrap" }, st(last.status), h("span", null, `run #${last.id} by ${last.requested_by} · ${fmtAgo(last.created_at)}`),
        last.summary && last.summary.minutes ? h("span", { class: "caption" }, `${last.summary.minutes} min · ${(last.summary.proof || []).filter((p) => p.ok).length}/${(last.summary.proof || []).length} proofs passed`) : null,
        h("button", { class: "btn ghost sm", onclick: async () => { const t = await api(`/api/lifecycle/${last.id}/log`); drawer({ title: `Lifecycle run #${last.id}`, wide: true, body: h("pre", { class: "log" }, t) }); } }, icon("file", 14), "Log")) : null));
    render([
      head("Teach — capability evidence", "The checklist a sceptical data scientist brings, item by item: how it is done here, the honest status, the live evidence (click through), the code that does it (file:line, resolved live) and the GCP-native equivalent."),
      h("div", { class: "grid cols-4" }, ...Object.entries(c.status_labels).map(([k, t]) => stat(t, int(c.counts[k] || 0), k === "implemented" ? `${c.items.filter((i) => i.status === k && i.evidence_recorded).length} with live records` : ""))),
      h("div", { class: "section" }, lifecycle),
      h("div", { class: "section row wrap" }, areaSel, stSel),
      host,
      h("div", { class: "section" }, card("Who owns what — OSS here vs GCP-native, and where they meet", own.el)),
    ]);
    draw();
  }

  let route = () => window.dispatchEvent(new HashChangeEvent("hashchange"));
  window.BSPPages = { release, outcomes, slo, teach, showCode, codeLink };
})();
