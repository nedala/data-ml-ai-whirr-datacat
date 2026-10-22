/* MLOps Control Tower SPA — hash-routed pages on the shared UI kit (window.BSP). */
(function () {
  "use strict";
  const { h, icon, toast, table, api: mkApi, busy, fmtAgo, fmtTime, drawer, confirm } = BSP;
  const api = mkApi("/mlops");
  const main = document.getElementById("main");
  const COLOURS = { blue: "#3380f0", green: "#2e9e6e", canary: "#e0a800", stable: "#0b6e79", champion: "#0b6e79", shadow: "#9aa0a6" };
  const VCOL = (i) => Charts.PALETTE[i % Charts.PALETTE.length];
  let timer = null;

  const f3 = (v) => (v == null || isNaN(v) ? "—" : Number(v).toFixed(3));
  const f4 = (v) => (v == null || isNaN(v) ? "—" : Number(v).toFixed(4));
  const pct = (v) => (v == null || isNaN(v) ? "—" : (100 * v).toFixed(1) + "%");
  const int = (v) => (v == null ? "—" : Number(v).toLocaleString());
  const pill = (c, label) => h("span", { class: `colour-pill ${c}` }, label || c);
  const badge = (kind, text) => h("span", { class: `badge ${kind}` }, h("span", { class: "dot" }), text);
  const stat = (label, value, delta) => h("div", { class: "stat" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), delta ? h("div", { class: "delta" }, delta) : null);
  const card = (title, body, actions) => h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", null, title), actions || null), body);
  const head = (title, lede, actions) => h("div", { class: "bsp-page-head" }, h("div", null, h("h1", null, title), lede ? h("p", { class: "lede" }, lede) : null), actions ? h("div", { class: "row wrap" }, actions) : null);
  const err = (e) => toast(e && (e.message || e.detail) ? (e.message || e.detail) : String(e), "error");
  const mlflowRun = (r) => `/mlflow/#/experiments/${r.experiment_id}/runs/${r.run_id}`;

  function render(nodes) { main.replaceChildren(...[].concat(nodes).filter(Boolean)); }
  function busyUi() { const a = document.activeElement; return document.querySelector(".bsp-scrim") || (a && /INPUT|SELECT|TEXTAREA/.test(a.tagName)); }
  function every(ms, fn) { clearInterval(timer); timer = setInterval(() => { if (!document.hidden && !busyUi()) fn().catch(() => {}); }, ms); }

  // ── Overview ───────────────────────────────────────────────────────────
  async function overview() {
    const o = await api("/api/overview");
    const g = o.gateway || {};
    const al = o.aliases || {};
    const sim = o.simulator || {};
    const d = o.drift, r = o.retrain;
    const steps = [["Integrate & EDA", "features"], ["Feature selection", "leaderboard"], ["Hold-outs", "leaderboard"], ["Feature store", "features"],
      ["Pipelines in MLflow", "leaderboard"], ["Leaderboard", "leaderboard"], ["Tuning", "leaderboard"], ["Registry", "registry"],
      ["Release gates · approval", "release"], ["Blue/green · canary · shadow", "registry"], ["Telemetry · SLO", "slo"], ["Drift", "drift"],
      ["Delayed labels · A/B", "outcomes"], ["Retrain", "retrains"], ["Scores → warehouse", "scores"], ["Reproduce · export", "frozen"]];
    const life = h("div", { class: "lifecycle" }, steps.flatMap(([s, p], i) => [i ? h("span", { class: "arrow" }, icon("arrow-right", 12)) : null, h("a", { href: "#" + p }, h("span", { class: "step" }, s))]));
    const rate = h("input", { type: "number", class: "input", value: sim.rate || 20, min: 1, max: 500, style: { width: "90px" } });
    const shift = h("input", { type: "checkbox", checked: sim.shift || null });
    const simCard = card("Traffic simulator", h("div", { class: "stack" },
      h("p", { class: "muted" }, "Replays real customer snapshots through the model gateway. ", h("b", null, "Shift"), " samples the last months (price-increase wave + outage cluster) so drift detection fires naturally."),
      h("div", { class: "row wrap" },
        h("label", { class: "row" }, "Rate (req/s)", rate),
        h("label", { class: "switch" }, shift, h("span", { class: "track" })), h("span", null, "Shifted months"),
        h("button", { class: "btn primary", onclick: (e) => busy(e.currentTarget, () => api("/api/simulator", { body: { action: "start", rate: +rate.value, shift: shift.checked } }).then(() => { toast("Simulator running", "success"); route(); }).catch(err)) }, icon("play", 14), sim.running ? "Apply" : "Start"),
        sim.running ? h("button", { class: "btn secondary", onclick: (e) => busy(e.currentTarget, () => api("/api/simulator", { body: { action: "stop" } }).then(route).catch(err)) }, icon("stop", 14), "Stop") : null),
      h("div", { class: "caption" }, sim.running ? `Running · ${int(sim.sent)} sent · ${sim.errors} errors${sim.last_error ? " · last error: " + sim.last_error : ""}` : `Stopped · ${int(sim.sent)} sent so far`)));
    render([
      head("Churn model — live", "End-to-end MLOps for the DataPlatform 90-day churn model: every stage below is live and clickable."),
      h("div", { class: "grid cols-4" },
        stat("Champion", al.champion ? h("a", { href: `#journey?v=${al.champion}`, title: "Open the champion's journey" }, "v" + al.champion) : "—", "serves production · see its journey"),
        stat("Challenger", al.challenger ? "v" + al.challenger : "—", g.shadow_version ? "shadow-scored v" + g.shadow_version : "candidate"),
        stat("Routing mode", g.mode || "—", g.mode === "canary" ? `${pct(g.canary_weight)} to challenger` : g.mode === "blue_green" ? `${g.live_color} live` : ""),
        stat("Requests / min", int(o.traffic && o.traffic.req_1m), `p95 ${f3(o.traffic && o.traffic.p95_5m)} ms · ${int(o.traffic && o.traffic.total)} logged`)),
      h("div", { class: "grid cols-4", style: { marginTop: "16px" } },
        stat("Drift", d ? (d.breached ? "BREACH" : "stable") : "—", d ? `max PSI ${f3(d.max_psi)} · ${fmtAgo(d.created_at)}` : "no check yet"),
        stat("Last retrain", r ? `#${r.id} ${r.status}` : "—", r ? (r.decision || "").slice(0, 70) : "none"),
        stat("Scored customers", int(o.scored && o.scored.n), o.scored && o.scored.last_month ? "latest month " + o.scored.last_month : ""),
        stat("Frozen bundles", int(o.frozen && o.frozen.n), o.frozen && o.frozen.last ? fmtAgo(o.frozen.last) : "")),
      (o.pending_approvals || []).length ? h("div", { class: "alert info", style: { marginTop: "16px" } }, icon("info", 18), h("div", null, h("b", null, `${o.pending_approvals.length} production request(s) awaiting approval: `),
        o.pending_approvals.map((p) => `#${p.id} v${p.model_version} (requested by ${p.requested_by})`).join(", "), " — ", h("a", { href: "#release" }, "Release & approvals"))) : null,
      o.alerts_open && o.alerts_open.n ? h("div", { class: "alert warning", style: { marginTop: "8px" } }, icon("warning", 18), h("div", null, `${o.alerts_open.n} open alert(s) — `, h("a", { href: "#slo" }, "SLO, access & alerts"))) : null,
      h("div", { class: "section" }, card("Run the full lifecycle", h("div", { class: "row wrap" },
        h("span", null, "Every stage end to end, with release gates and a four-eyes approval, from source-controlled code — the same script as ", h("code", null, "python -m bsp_mlops.demo"), "."),
        o.lifecycle ? h("span", { class: "caption" }, `last run #${o.lifecycle.id} ${o.lifecycle.status} (${o.lifecycle.requested_by}, ${fmtAgo(o.lifecycle.created_at)})`) : null),
        h("a", { class: "btn primary sm", href: "#teach" }, icon("play", 14), "Run / teach"))),
      h("div", { class: "section" }, card("Lifecycle (conceptual)", life, h("a", { class: "btn secondary sm", href: al.champion ? `#journey?v=${al.champion}` : "#journey" }, icon("history", 14), "Champion's actual journey"))),
      h("div", { class: "section grid cols-2" }, simCard,
        card("Background jobs", h("dl", { class: "kv" },
          h("dt", null, "Drift monitor"), h("dd", null, `every ${o.settings.drift_interval_min} min over the last ${o.settings.drift_window_min} min (≤ ${o.settings.drift_last_n} requests)`),
          h("dt", null, "Last drift run"), h("dd", null, o.jobs.drift.last_run ? `${fmtAgo(o.jobs.drift.last_run)} — ${(o.jobs.drift.last_result || {}).status}` : "not yet"),
          h("dt", null, "Auto-retrain"), h("dd", null, o.settings.auto_retrain ? "on (drift breach → retrain)" : "off"),
          h("dt", null, "Retrain → release"), h("dd", null, o.settings.auto_promote ? "retrained versions are submitted to the release gates automatically (staging + shadow); production needs a human approver" : "off (retrained versions stay challenger)"),
          h("dt", null, "Registry"), h("dd", null, h("a", { href: "/mlflow/#/models/bsp_churn", target: "_blank" }, "MLflow model bsp_churn"))))),
    ]);
    every(10000, overview);
  }

  // ── Leaderboard ────────────────────────────────────────────────────────
  async function leaderboard() {
    let trials = false, exp = "all";
    const host = h("div");
    async function load() {
      const d = await api(`/api/leaderboard?include_trials=${trials}`);
      const rows = d.runs.filter((r) => exp === "all" || r.experiment === exp);
      const sel = h("select", { class: "select", onchange: (e) => { exp = e.target.value; load().catch(err); } }, h("option", { value: "all" }, "All experiments"), d.experiments.map((x) => h("option", { value: x, selected: x === exp || null }, x)));
      const tr = h("input", { type: "checkbox", checked: trials || null, onchange: (e) => { trials = e.target.checked; load().catch(err); } });
      const best = rows.filter((r) => r.valid_auc != null).sort((a, b) => b.valid_auc - a.valid_auc)[0];
      const t = table({
        rowKey: "run_id", pageSize: 25, initialSort: { key: "valid_auc", dir: -1 },
        onRowClick: (r) => showRun(r),
        columns: [
          { key: "run_name", label: "Run", sort: true, render: (r) => h("a", { href: mlflowRun(r), target: "_blank" }, r.run_name || r.run_id.slice(0, 8)) },
          { key: "experiment", label: "Experiment", sort: true },
          { key: "model_kind", label: "Model", sort: true },
          { key: "stage", label: "Stage", sort: true },
          { key: "valid_auc", label: "Valid AUC", sort: true, num: true, render: (r) => f4(r.valid_auc) },
          { key: "test_auc", label: "Test AUC", sort: true, num: true, render: (r) => f4(r.test_auc) },
          { key: "test_pr_auc", label: "PR-AUC", sort: true, num: true, render: (r) => f4(r.test_pr_auc) },
          { key: "test_logloss", label: "Logloss", sort: true, num: true, render: (r) => f4(r.test_logloss) },
          { key: "test_lift_at_10", label: "Lift@10", sort: true, num: true, render: (r) => (r.test_lift_at_10 == null ? "—" : r.test_lift_at_10.toFixed(2)) },
          { key: "train_window", label: "Train window" },
          { key: "params", label: "Hyper-parameters", render: (r) => h("span", { class: "mono" }, Object.entries(r.params || {}).slice(0, 4).map(([k, v]) => `${k}=${String(v).slice(0, 7)}`).join(" ")) },
        ], rows,
      });
      host.replaceChildren(
        h("div", { class: "grid cols-4" }, stat("Runs", int(rows.length)), stat("Best valid AUC", best ? f4(best.valid_auc) : "—", best ? `${best.run_name} (${best.model_kind})` : ""),
          stat("Best test AUC", best ? f4(best.test_auc) : "—", "same run, untouched hold-out"), stat("Experiments", int(d.experiments.length))),
        h("div", { class: "section" }, card("Runs across experiments", t.el, h("div", { class: "row" }, sel, h("label", { class: "switch" }, tr, h("span", { class: "track" })), h("span", { class: "caption" }, "Optuna trials")))));
    }
    function showRun(r) {
      drawer({ title: r.run_name || r.run_id, wide: true, body: h("div", { class: "stack" },
        h("dl", { class: "kv" }, ...Object.entries({ "Run id": r.run_id, Experiment: r.experiment, Model: r.model_kind, Stage: r.stage, "Train window": r.train_window, "Test window": r.test_window, Features: r.n_features, "Fit seconds": f3(r.fit_seconds) }).flatMap(([k, v]) => [h("dt", null, k), h("dd", null, v == null ? "—" : String(v))])),
        h("h4", null, "Metrics"), h("dl", { class: "kv" }, ...["valid_auc", "test_auc", "test_pr_auc", "test_logloss", "test_brier", "test_lift_at_10"].flatMap((k) => [h("dt", null, k), h("dd", null, f4(r[k]))])),
        h("h4", null, "Hyper-parameters"), h("pre", { class: "codeblock" }, JSON.stringify(r.params, null, 2)),
        h("a", { class: "btn primary", href: mlflowRun(r), target: "_blank" }, "Open in MLflow (artifacts, plots, model)")) });
    }
    render([head("Experimentation leaderboard", "Every MLflow run across the leaderboard, tuning and retrain experiments — selected on validation AUC, reported on an untouched time-based test window."), host]);
    await load();
  }

  // ── Registry & Deployments ─────────────────────────────────────────────
  async function registry() {
    const [reg, dep, tel] = await Promise.all([api("/api/registry"), api("/api/deployment"), api("/api/telemetry?minutes=15")]);
    const al = reg.aliases || {};
    const versions = reg.versions || [];
    const vt = table({
      rowKey: "version", pageSize: 15, columns: [
        { key: "version", label: "Version", sort: (r) => +r.version, render: (r) => h("b", null, "v" + r.version) },
        { key: "aliases", label: "Aliases", render: (r) => h("span", { class: "row" }, (r.aliases || []).map((a) => badge(a === "champion" ? "success" : "info", a)), r.tags.frozen === "true" ? badge("neutral", "frozen") : null) },
        { key: "stage", label: "Stage", render: (r) => h("span", { class: "row" }, badge({ production: "success", staging: "info", rejected: "error", rolled_back: "warning" }[r.tags["bsp.stage"]] || "neutral", r.tags["bsp.stage"] || "dev"), r.tags["bsp.approved_by"] ? h("span", { class: "caption" }, "approved by " + r.tags["bsp.approved_by"]) : null) },
        { key: "model_kind", label: "Model", sort: true },
        { key: "run_name", label: "Run", render: (r) => h("a", { href: `/mlflow/#/experiments/${r.experiment_id}/runs/${r.run_id}`, target: "_blank" }, r.run_name || r.run_id.slice(0, 8)) },
        { key: "journey", label: "Journey", render: (r) => r.tags && r.tags["bsp.journey_id"] ? h("a", { href: `#journey?v=${r.version}` }, "View journey") : h("span", { class: "caption" }, "—") },
        { key: "train_window", label: "Trained on" },
        { key: "auc", label: "Test AUC", num: true, sort: (r) => r.metrics.test_auc, render: (r) => f4(r.metrics.test_auc) },
        { key: "prauc", label: "PR-AUC", num: true, render: (r) => f4(r.metrics.test_pr_auc) },
        { key: "lift", label: "Lift@10", num: true, render: (r) => (r.metrics.test_lift_at_10 || 0).toFixed(2) },
        { key: "created", label: "Registered", render: (r) => fmtAgo(new Date(r.created).toISOString()) },
        { key: "act", label: "", class: "actions", render: (r) => h("div", { class: "row" },
          !(r.aliases || []).includes("champion") ? h("button", { class: "btn sm primary", onclick: async (e) => {
            if (!(await confirm({ title: `Submit v${r.version} for release?`, body: "Runs the automated release gates against the champion. Pass → staging + challenger in shadow + a production request that another person with an approver role must approve (Release & approvals).", okText: "Run gates" }))) return;
            busy(e.currentTarget, () => api("/api/release/submit", { body: { version: r.version } }).then((x) => { toast(x.staged ? `v${r.version} staged — approval requested` : `v${r.version} blocked by the gates`, x.staged ? "success" : "error"); location.hash = `#release?id=${x.promotion.id}`; }).catch(err));
          } }, "Submit for release") : null,
          !(r.aliases || []).length ? h("button", { class: "btn sm secondary", onclick: (e) => busy(e.currentTarget, () => api("/api/registry/alias", { body: { alias: "challenger", version: r.version } }).then(route).catch(err)) }, "Make challenger") : null) },
      ], rows: versions,
    });
    // deployment controls
    const modes = ["single", "blue_green", "canary", "shadow"];
    const seg = h("div", { class: "segmented" }, modes.map((m) => h("button", { class: dep.mode === m ? "active" : "", onclick: (e) => busy(e.currentTarget, () => api("/api/deployment", { method: "PUT", body: { mode: m } }).then(() => { toast(`Mode → ${m}`, "success"); route(); }).catch(err)) }, m.replace("_", "/"))));
    const w = h("input", { type: "range", class: "slider", min: 0, max: 100, step: 1, value: Math.round(100 * (dep.canary_weight || 0)) });
    const wl = h("b", null, `${Math.round(100 * (dep.canary_weight || 0))}%`);
    w.oninput = () => (wl.textContent = w.value + "%");
    const shadowOn = h("input", { type: "checkbox", checked: dep.shadow_enabled || dep.mode === "shadow" || null, onchange: (e) => api("/api/deployment", { method: "PUT", body: { shadow_enabled: e.target.checked } }).then(() => { toast(`Shadow ${e.target.checked ? "on" : "off"}`, "success"); route(); }).catch(err) });
    const bgPanel = (c) => h("div", { class: `bg-panel ${dep.mode === "blue_green" && dep.live_color === c ? "live" : ""}` }, dep.mode === "blue_green" && dep.live_color === c ? h("span", { class: "live-tag" }, "LIVE") : null,
      h("div", { class: "row between" }, pill(c, c.toUpperCase()), h("span", { class: "kpi-s" }, dep[c + "_version"] ? "v" + dep[c + "_version"] : "—")),
      h("div", { class: "caption", style: { marginTop: "6px" } }, dep[c + "_version"] === al.champion ? "champion" : dep[c + "_version"] === al.challenger ? "challenger" : "pinned version"));
    const blueGreen = h("div", { class: "stack" },
      h("div", { class: "grid cols-2" }, bgPanel("blue"), bgPanel("green")),
      h("div", { class: "row wrap" },
        h("button", { class: "btn primary", disabled: dep.mode !== "blue_green" || null, onclick: async (e) => {
          if (!(await confirm({ title: "Switch live colour?", body: `All traffic moves from ${dep.live_color} to ${dep.live_color === "blue" ? "green" : "blue"} instantly (both are pre-loaded).`, okText: "Switch" }))) return;
          busy(e.currentTarget, () => api("/api/deployment/switch", { body: {} }).then((d) => { toast(`${d.live_color} is live`, "success"); route(); }).catch(err));
        } }, icon("refresh", 14), "Switch blue ↔ green"),
        h("button", { class: "btn secondary", onclick: (e) => busy(e.currentTarget, () => api("/api/deployment", { method: "PUT", body: { mode: "blue_green", blue_version: al.champion, green_version: al.challenger || al.champion, live_color: "blue" } }).then(route).catch(err)) }, "Pin blue = champion, green = challenger"),
        h("button", { class: "btn ghost", onclick: (e) => busy(e.currentTarget, () => api("/api/deployment/rollback", { body: {} }).then(() => { toast("Rolled back the last change", "success"); route(); }).catch(err)) }, icon("history", 14), "Rollback last change")));
    // live split chart (served traffic by colour per minute)
    const served = tel.series.filter((s) => !s.shadow);
    const minutes = [...new Set(served.map((s) => s.minute))].sort();
    const keys = [...new Set(served.map((s) => `v${s.model_version} · ${s.colour}`))];
    const splitChart = Charts.bars({ categories: minutes, catFmt: (m) => new Date(m).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }), height: 200,
      series: keys.map((k, i) => ({ name: k, color: COLOURS[k.split(" · ")[1]] || VCOL(i), values: minutes.map((m) => served.filter((s) => s.minute === m && `v${s.model_version} · ${s.colour}` === k).reduce((a, s) => a + s.n, 0)) })) });
    const splitRows = tel.split.map((s) => ({ ...s, share: s.n / Math.max(1, tel.split.filter((x) => x.shadow === s.shadow).reduce((a, x) => a + x.n, 0)) }));
    const hist = (dep.history || []).slice().reverse();
    const dv = (dep.divergence || []);
    render([
      head("Registry & deployments", "Champion / challenger aliases in the MLflow registry, and how the model gateway routes live traffic between them. Serving is derived from the registry; any disagreement is shown here."),
      ...dv.map((x) => h("div", { class: `alert ${x.level === "error" ? "error" : x.level === "warning" ? "warning" : "info"}` }, icon(x.level === "info" ? "info" : "warning", 16), h("div", null, h("b", null, x.code.replace(/_/g, " ") + " — "), x.message,
        x.level !== "info" || x.code === "stale_pins" ? h("button", { class: "btn sm secondary", style: { marginLeft: "10px" }, onclick: (e) => busy(e.currentTarget, () => api("/api/release/reconcile", { body: { policy: "shadow" } }).then(() => { toast("Serving re-derived from the registry", "success"); route(); }).catch(err)) }, "Reconcile") : null))),
      card(`Model versions — ${reg.model}`, vt.el, h("div", { class: "row" },
        reg.tags && reg.tags.previous_champion ? h("button", { class: "btn secondary", onclick: async (e) => {
          if (!(await confirm({ title: `Roll champion back to v${reg.tags.previous_champion}?`, okText: "Roll back", danger: true }))) return;
          busy(e.currentTarget, () => api("/api/release/rollback", { body: { reason: "rollback from the registry page" } }).then(() => { toast("Champion rolled back (recorded)", "success"); route(); }).catch(err));
        } }, icon("history", 14), `Rollback champion → v${reg.tags.previous_champion}`) : null)),
      h("div", { class: "section grid cols-2" },
        card("Routing", h("div", { class: "stack lg" },
          h("div", null, h("div", { class: "eyebrow" }, "Mode"), seg,
            h("p", { class: "caption" }, { single: "All traffic → champion.", blue_green: "Two pinned versions; one colour takes 100 % — instant, reversible cut-over.", canary: "A sticky (per account) share of traffic → challenger.", shadow: "Champion answers; challenger scored asynchronously and logged only." }[dep.mode])),
          h("div", null, h("div", { class: "eyebrow" }, "Canary weight (share to challenger)"),
            h("div", { class: "row" }, w, wl, h("button", { class: "btn sm primary", onclick: (e) => busy(e.currentTarget, () => api("/api/deployment", { method: "PUT", body: { mode: "canary", canary_weight: +w.value / 100 } }).then(() => { toast(`Canary ${w.value}%`, "success"); route(); }).catch(err)) }, "Apply canary"))),
          h("div", { class: "row" }, h("label", { class: "switch" }, shadowOn, h("span", { class: "track" })), h("span", null, "Shadow-score the challenger"), dep.shadow_version ? pill("shadow", "shadow v" + dep.shadow_version) : null))),
        card("Blue / green", blueGreen)),
      h("div", { class: "section grid cols-2" },
        card("Live traffic split (last 15 min, served)", h("div", null, splitChart, Charts.legend(keys.map((k, i) => ({ name: k, color: COLOURS[k.split(" · ")[1]] || VCOL(i) }))))),
        card("Split by version / colour (last 15 min)", table({ pageSize: 10, columns: [
          { key: "model_version", label: "Version", render: (r) => "v" + r.model_version }, { key: "colour", label: "Colour", render: (r) => pill(r.colour) },
          { key: "shadow", label: "Kind", render: (r) => (r.shadow ? "shadow" : "served") }, { key: "n", label: "Requests", num: true, render: (r) => int(r.n) },
          { key: "share", label: "Share", num: true, render: (r) => pct(r.share) }, { key: "mean_score", label: "Mean score", num: true, render: (r) => f3(r.mean_score) }], rows: splitRows }).el)),
      h("div", { class: "section" }, card("Deployment history", table({ pageSize: 10, columns: [
        { key: "ts", label: "When", render: (r) => fmtTime(r.ts) }, { key: "actor", label: "Who" }, { key: "action", label: "Action" },
        { key: "after", label: "Result", render: (r) => h("span", { class: "mono" }, `${r.after.mode} · live ${r.after.live_color} · blue v${r.after.blue_version || "–"} · green v${r.after.green_version || "–"} · canary ${pct(r.after.canary_weight)} · shadow ${r.after.shadow_enabled ? "on" : "off"}`) }], rows: hist }).el)),
    ]);
    every(15000, registry);
  }

  // ── Telemetry ──────────────────────────────────────────────────────────
  async function telemetry(minutes) {
    minutes = minutes || +(sessionStorage.getItem("tel-min") || 30);
    const t = await api(`/api/telemetry?minutes=${minutes}`);
    const served = t.series.filter((s) => !s.shadow);
    const mins = [...new Set(t.series.map((s) => s.minute))].sort();
    const keys = [...new Set(t.series.map((s) => `v${s.model_version} · ${s.shadow ? "shadow" : s.colour}`))];
    const colourOf = (k, i) => COLOURS[k.split(" · ")[1]] || VCOL(i);
    const reqChart = Charts.bars({ categories: mins, catFmt: (m) => new Date(m).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
      series: keys.map((k, i) => ({ name: k, color: colourOf(k, i), values: mins.map((m) => t.series.filter((s) => s.minute === m && `v${s.model_version} · ${s.shadow ? "shadow" : s.colour}` === k).reduce((a, s) => a + s.n, 0)) })) });
    const byMin = (fn) => mins.map((m) => { const r = served.filter((s) => s.minute === m); const n = r.reduce((a, s) => a + s.n, 0); return n ? [Date.parse(m), r.reduce((a, s) => a + fn(s) * s.n, 0) / n] : null; }).filter(Boolean);
    const latChart = Charts.line({ series: [{ name: "p50", color: "#0b6e79", points: byMin((s) => s.p50) }, { name: "p95", color: "#ff8205", points: byMin((s) => s.p95) }], yFmt: (v) => v.toFixed(1) + " ms" });
    const hv = [...new Set(t.hist.map((x) => `v${x.model_version}${x.shadow ? " (shadow)" : ""}`))];
    const bins = Array.from({ length: 20 }, (_, i) => i + 1);
    const histChart = Charts.bars({ categories: bins, stacked: false, catFmt: (b) => ((b - 1) * 0.05).toFixed(2),
      series: hv.map((k, i) => { const tot = t.hist.filter((x) => `v${x.model_version}${x.shadow ? " (shadow)" : ""}` === k).reduce((a, x) => a + x.n, 0) || 1; return { name: k, color: VCOL(i), values: bins.map((b) => t.hist.filter((x) => `v${x.model_version}${x.shadow ? " (shadow)" : ""}` === k && x.b === b).reduce((a, x) => a + x.n, 0) / tot) }; }), yFmt: (v) => (100 * v).toFixed(0) + "%" });
    const sel = h("div", { class: "segmented" }, [15, 30, 60, 240, 1440].map((m) => h("button", { class: m === minutes ? "active" : "", onclick: () => { sessionStorage.setItem("tel-min", m); telemetry(m).catch(err); } }, m < 60 ? `${m}m` : `${m / 60}h`)));
    const tot = t.totals || {};
    const sh = t.shadow || {};
    render([
      head("Telemetry", "Every inference — served and shadow — is logged to mlops.inference_log by the gateway (batched writes). Prometheus metrics: /serve/metrics.", sel),
      h("div", { class: "grid cols-4" }, stat("Served", int(tot.served), `${int(tot.accounts)} customers`), stat("Shadow-scored", int(tot.shadow)),
        stat("Latency p50 / p95", `${f3(tot.p50)} / ${f3(tot.p95)}`, "ms per instance"), stat("Shadow agreement", sh.pairs ? pct(sh.agree_at_05) : "—", sh.pairs ? `corr ${f3(sh.corr)} · mean |Δ| ${f3(sh.mean_abs_diff)} over ${int(sh.pairs)} pairs` : "no shadow traffic")),
      h("div", { class: "section grid cols-2" },
        card("Requests per minute by version / colour", h("div", null, reqChart, Charts.legend(keys.map((k, i) => ({ name: k, color: colourOf(k, i) }))))),
        card("Latency (ms per instance)", h("div", null, latChart, Charts.legend([{ name: "p50", color: "#0b6e79" }, { name: "p95", color: "#ff8205" }])))),
      h("div", { class: "section grid cols-2" },
        card("Score distribution by version", h("div", null, histChart, Charts.legend(hv.map((k, i) => ({ name: k, color: VCOL(i) }))))),
        card("By version / colour", table({ pageSize: 10, columns: [
          { key: "model_version", label: "Version", render: (r) => "v" + r.model_version }, { key: "alias", label: "Alias" }, { key: "colour", label: "Colour", render: (r) => pill(r.colour) },
          { key: "n", label: "Requests", num: true, render: (r) => int(r.n) }, { key: "mean_score", label: "Mean score", num: true, render: (r) => f3(r.mean_score) },
          { key: "p95", label: "p95 ms", num: true, render: (r) => f3(r.p95) }], rows: t.split }).el)),
      h("div", { class: "section" }, card("Latest inferences", table({ pageSize: 15, columns: [
        { key: "ts", label: "Time", render: (r) => new Date(r.ts).toLocaleTimeString() }, { key: "ban", label: "BAN" }, { key: "model_version", label: "Version", render: (r) => "v" + r.model_version },
        { key: "colour", label: "Colour", render: (r) => pill(r.colour) }, { key: "mode", label: "Mode" }, { key: "score", label: "Score", num: true }, { key: "latency_ms", label: "ms", num: true },
        { key: "source", label: "Source" }, { key: "request_id", label: "Request", render: (r) => h("span", { class: "mono" }, r.request_id.slice(0, 10)) }], rows: t.recent }).el)),
    ]);
    every(10000, () => telemetry(minutes));
  }

  // ── Drift ──────────────────────────────────────────────────────────────
  async function drift() {
    const d = await api("/api/drift");
    const s = d.settings;
    const reps = d.reports || [];
    const last = reps[0];
    const series = [
      { name: "max feature PSI", color: "#0b6e79", points: reps.map((r) => [Date.parse(r.created_at), r.max_psi || 0]), pointColor: (p) => (p[1] > s.psi_breach ? "#c0392b" : "#0b6e79") },
      { name: "prediction PSI", color: "#3380f0", points: reps.map((r) => [Date.parse(r.created_at), r.prediction_psi || 0]), dash: "4 3" },
    ];
    const timeline = Charts.line({ series, thresholds: [{ y: s.psi_breach, label: `breach ${s.psi_breach}` }, { y: s.psi_warn, label: `warn ${s.psi_warn}`, color: "#ff8205" }],
      xFmt: (x) => new Date(x).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) });
    const maxPsi = last ? Math.max(0.3, ...(last.feature_drift || []).map((f) => f.psi || 0)) : 1;
    const ft = last ? table({ pageSize: 30, initialSort: { key: "psi", dir: -1 }, columns: [
      { key: "feature", label: "Feature", sort: true },
      { key: "psi", label: "PSI", sort: true, render: (r) => h("div", { class: "bar-cell" }, h("div", { class: `bar ${r.status}`, style: { width: `${Math.max(2, (100 * (r.psi || 0)) / maxPsi)}px` } }), f3(r.psi)) },
      { key: "ks", label: "KS", num: true, sort: true, render: (r) => f3(r.ks) }, { key: "ks_pvalue", label: "KS p", num: true, render: (r) => (r.ks_pvalue == null ? "—" : Number(r.ks_pvalue).toExponential(1)) },
      { key: "ref_mean", label: "Reference mean", num: true, render: (r) => f3(r.ref_mean) }, { key: "cur_mean", label: "Live mean", num: true, render: (r) => f3(r.cur_mean) },
      { key: "status", label: "Status", sort: true, render: (r) => badge(r.status === "breach" ? "error" : r.status === "warn" ? "warning" : "success", r.status) }], rows: last.feature_drift || [] }) : null;
    const inp = (k, step) => h("input", { class: "input", type: "number", step: step || "0.01", value: s[k], style: { width: "90px" }, "data-k": k });
    const auto = h("input", { type: "checkbox", checked: s.auto_retrain || null, "data-k": "auto_retrain" });
    const ap = h("input", { type: "checkbox", checked: s.auto_promote || null, "data-k": "auto_promote" });
    const form = h("div", { class: "stack" },
      h("div", { class: "row wrap" }, h("label", { class: "field" }, h("span", { class: "label" }, "PSI warn"), inp("psi_warn")), h("label", { class: "field" }, h("span", { class: "label" }, "PSI breach"), inp("psi_breach")),
        h("label", { class: "field" }, h("span", { class: "label" }, "Min breached features"), inp("min_breached_features", "1")), h("label", { class: "field" }, h("span", { class: "label" }, "AUC drop breach"), inp("auc_drop_breach")),
        h("label", { class: "field" }, h("span", { class: "label" }, "Check every (min)"), inp("drift_interval_min", "1")), h("label", { class: "field" }, h("span", { class: "label" }, "Window (min)"), inp("drift_window_min", "1"))),
      h("div", { class: "row wrap" }, h("label", { class: "switch" }, auto, h("span", { class: "track" })), h("span", null, "Auto-retrain on breach"),
        h("label", { class: "switch" }, ap, h("span", { class: "track" })), h("span", null, "Auto-promote (margin"), inp("promote_margin", "0.001"), h("span", null, "AUC)"),
        h("button", { class: "btn secondary", onclick: (e) => { const body = {}; form.querySelectorAll("[data-k]").forEach((x) => (body[x.dataset.k] = x.type === "checkbox" ? x.checked : +x.value)); busy(e.currentTarget, () => api("/api/settings", { method: "PUT", body }).then(() => toast("Saved", "success")).catch(err)); } }, "Save thresholds")));
    render([
      head("Drift", "Live served traffic vs the champion's release baseline (its validation + test data): PSI + KS per feature, prediction drift, and performance drift once 90-day outcomes are known.",
        h("button", { class: "btn primary", onclick: (e) => busy(e.currentTarget, () => api("/api/drift/check", { body: {} }).then((r) => { toast(r.status === "breach" ? `Breach: ${(r.breach_reasons || []).join("; ")}${r.retrain ? " → retrain " + (r.retrain.retrain_id || r.retrain.status) : ""}` : r.status === "insufficient_data" ? `Not enough traffic (${r.n_current})` : "No drift", r.status === "breach" ? "error" : "info"); route(); }).catch(err)) }, icon("refresh", 14), "Run check now")),
      h("div", { class: "grid cols-4" }, stat("Latest status", last ? (last.breached ? "BREACH" : "stable") : "—", last ? fmtAgo(last.created_at) : ""),
        stat("Features breached", last ? `${last.n_features_breached} / ${(last.feature_drift || []).length}` : "—", last ? `max PSI ${f3(last.max_psi)}` : ""),
        stat("Prediction PSI", last ? f3(last.prediction_psi) : "—"), stat("AUC ref → live", last && last.auc_current != null ? `${f3(last.auc_reference)} → ${f3(last.auc_current)}` : last ? `${f3(last.auc_reference)} → (awaiting outcomes)` : "—", last ? `${int(last.n_labeled)} scored rows with known outcome` : "")),
      last && last.breached ? h("div", { class: "alert error section" }, icon("error", 18), h("div", null, h("div", { class: "title" }, "Drift breach"), (last.breach_reasons || []).join(" · "), last.triggered_retrain_id ? h("div", null, h("a", { href: "#retrains" }, `→ retrain #${last.triggered_retrain_id}`)) : null)) : null,
      h("div", { class: "section" }, card("Drift timeline", h("div", null, timeline, Charts.legend(series)))),
      last ? h("div", { class: "section" }, card(`Latest report #${last.id} · v${last.model_version} · ${int(last.n_current)} live vs ${int(last.n_reference)} reference rows`, ft.el)) : null,
      h("div", { class: "section" }, card("Thresholds & automation", form)),
      h("div", { class: "section" }, card("History", table({ pageSize: 10, columns: [
        { key: "id", label: "#" }, { key: "created_at", label: "When", render: (r) => fmtTime(r.created_at) }, { key: "source", label: "Source" }, { key: "model_version", label: "Model", render: (r) => "v" + r.model_version },
        { key: "n_current", label: "Live rows", num: true }, { key: "max_psi", label: "Max PSI", num: true, render: (r) => f3(r.max_psi) }, { key: "prediction_psi", label: "Pred PSI", num: true, render: (r) => f3(r.prediction_psi) },
        { key: "breached", label: "Status", render: (r) => badge(r.breached ? "error" : "success", r.breached ? "breach" : "ok") }, { key: "triggered_retrain_id", label: "Retrain", render: (r) => (r.triggered_retrain_id ? "#" + r.triggered_retrain_id : "") }], rows: reps }).el)),
    ]);
    every(20000, drift);
  }

  // ── Retrains ───────────────────────────────────────────────────────────
  async function retrains() {
    const d = await api("/api/retrains");
    const s = d.settings;
    const showLog = async (r) => { const txt = await api(`/api/retrains/${r.id}/log`); drawer({ title: `Retrain #${r.id} log`, wide: true, body: h("pre", { class: "log" }, txt) }); };
    render([
      head("Retrains", `Triggered by a drift breach or by hand. The orchestrator runs the training pipeline as a subprocess, registers the result as challenger and applies the rule: promote when challenger AUC ≥ champion AUC + ${s.promote_margin} on the newest labelled month — otherwise keep it in shadow.`,
        h("button", { class: "btn primary", onclick: async (e) => { const reason = await BSP.prompt({ title: "Start a retrain", label: "Reason", value: "manual retrain from the control tower" }); if (reason == null) return; busy(e.currentTarget, () => api("/api/retrains", { body: { reason } }).then((r) => { toast(r.status === "queued" ? `Retrain #${r.retrain_id} queued` : r.status, "success"); route(); }).catch(err)); } }, icon("play", 14), "Retrain now")),
      card("Trigger history", table({ pageSize: 20, onRowClick: showLog, columns: [
        { key: "id", label: "#", sort: true }, { key: "created_at", label: "When", render: (r) => fmtTime(r.created_at) }, { key: "trigger", label: "Trigger", render: (r) => badge(r.trigger === "drift" ? "warning" : "info", r.trigger) },
        { key: "reason", label: "Reason", render: (r) => h("span", { title: r.reason }, (r.reason || "").slice(0, 80)) },
        { key: "status", label: "Status", render: (r) => badge(r.status === "succeeded" ? "success" : r.status === "failed" ? "error" : "pending", r.status) },
        { key: "model_version", label: "New version", render: (r) => (r.model_version ? "v" + r.model_version : "") },
        { key: "auc", label: "Challenger vs champion AUC", render: (r) => (r.challenger_auc != null ? `${f4(r.challenger_auc)} vs ${f4(r.champion_auc)} (${r.margin >= 0 ? "+" : ""}${f4(r.margin)})` : "") },
        { key: "promoted", label: "Outcome", render: (r) => (r.status === "succeeded" ? (r.promoted ? badge("success", "promoted") : badge("info", r.shadow_started ? "challenger · shadow" : "challenger")) : "") },
        { key: "mlflow_run_id", label: "Run", render: (r) => (r.mlflow_run_id ? h("span", { class: "mono" }, r.mlflow_run_id.slice(0, 8)) : "") },
        { key: "decision", label: "Decision", render: (r) => h("span", { class: "caption" }, r.decision || "") }], rows: d.runs, empty: h("div", { class: "empty" }, "No retrains yet — trigger one, or let a drift breach do it.") }).el),
    ]);
    every(8000, retrains);
  }

  // ── Frozen artifacts ───────────────────────────────────────────────────
  async function frozen() {
    const d = await api("/api/frozen");
    render([
      head("Frozen artifacts", `Immutable, hashed bundles — model (MLflow format) + training-data snapshot + feature definitions + metrics + environment lock + provenance manifest — pushed to the ${d.repository === "jfrog" ? "JFrog Artifactory" : "local file artifactory"} repo “${d.repo}”.`,
        h("button", { class: "btn primary", onclick: async (e) => { if (!(await confirm({ title: "Freeze the champion?", body: "Builds the bundle, hashes every file, pushes it and tags the registry version frozen=true.", okText: "Freeze" }))) return; busy(e.currentTarget, () => api("/api/frozen", { body: {} }).then((r) => { toast(`Frozen ${r.bundle} (${r.sha256.slice(0, 12)}…)`, "success"); route(); }).catch(err)); } }, icon("lock", 14), "Freeze champion")),
      h("div", { class: "caption", style: { margin: "-6px 0 12px" } }, icon("cloud", 12), " Google identity (Artifact Registry pushes): ",
        h("b", null, d.gcp_identity || "GCP_SA_JSON / default credentials"), " (", h("a", { href: "/studio/#identities", target: "_blank", rel: "noopener" }, "change"), ")"),
      card("Bundles", table({ pageSize: 20, columns: [
        { key: "id", label: "#" }, { key: "created_at", label: "Frozen", render: (r) => fmtTime(r.created_at) }, { key: "model_version", label: "Version", render: (r) => "v" + r.model_version },
        { key: "bundle_name", label: "Bundle", render: (r) => h("span", { class: "mono" }, r.bundle_name) },
        { key: "sha256", label: "Tarball sha256", render: (r) => h("span", { class: "mono", title: r.sha256 }, r.sha256.slice(0, 20) + "…", " ", h("button", { class: "btn ghost icon sm", onclick: () => BSP.copy(r.sha256, "sha256") }, icon("copy", 12))) },
        { key: "content_digest", label: "Content digest", render: (r) => h("span", { class: "mono", title: r.content_digest }, (r.content_digest || "").slice(0, 19) + "…") },
        { key: "size_bytes", label: "Size", num: true, render: (r) => BSP.fmtBytes(r.size_bytes) }, { key: "repository", label: "Repo" },
        { key: "uri", label: "Location", render: (r) => h("span", { class: "mono caption", title: r.uri }, r.uri.split("/").slice(-3).join("/")) },
        { key: "m", label: "", render: (r) => h("a", { class: "btn sm secondary", href: `/mlops/api/frozen/${r.id}/manifest` }, icon("download", 12), "Manifest") }], rows: d.bundles, empty: h("div", { class: "empty" }, "Nothing frozen yet.") }).el),
      h("div", { class: "section" }, card("Repository listing", table({ pageSize: 20, columns: [{ key: "path", label: "Path", render: (r) => h("span", { class: "mono" }, r.path || r.error) }, { key: "size", label: "Size", num: true, render: (r) => BSP.fmtBytes(r.size) }, { key: "modified", label: "Modified", render: (r) => fmtTime(r.modified) }], rows: d.listing }).el)),
    ]);
  }

  // ── Feature store ──────────────────────────────────────────────────────
  async function features() {
    const d = await api("/api/features");
    const acct = h("input", { class: "input", placeholder: "BAN e.g. 3100000008", style: { width: "180px" } });
    const out = h("pre", { class: "codeblock", style: { minHeight: "60px" } }, "Look up an account to see what the gateway reads when a request carries only a BAN.");
    render([
      head("Feature store", "Versioned feature views (YAML) → offline store in Postgres `features` (point-in-time training sets) → online store read by the model gateway.",
        h("button", { class: "btn primary", onclick: (e) => busy(e.currentTarget, () => api("/api/features/materialize", { body: {} }).then((r) => { toast(`Materialised ${r.offline.length} views, ${r.online.accounts} online rows`, "success"); route(); }).catch(err)) }, icon("refresh", 14), "Materialise now")),
      d.error ? h("div", { class: "alert warning" }, icon("info", 16), d.error) : null,
      card("Feature views", table({ pageSize: 20, columns: [
        { key: "feature_view", label: "View", render: (r) => h("b", null, `${r.feature_view}_v${r.version}`) }, { key: "description", label: "Description", render: (r) => h("span", { class: "caption" }, r.description) },
        { key: "n_features", label: "Features", num: true }, { key: "features", label: "Columns", render: (r) => h("span", { class: "mono" }, (r.features || []).map((f) => f.name).join(", ")) },
        { key: "ttl_days", label: "TTL (d)", num: true }, { key: "rows", label: "Offline rows", num: true, render: (r) => int(r.rows) }, { key: "last_event", label: "Latest event" },
        { key: "offline_age_min", label: "Offline age", render: (r) => (r.offline_age_min == null ? "—" : `${r.offline_age_min} min`) },
        { key: "online_rows", label: "Online rows", num: true, render: (r) => int(r.online_rows) }, { key: "online_age_min", label: "Online age", render: (r) => (r.online_age_min == null ? "—" : `${r.online_age_min} min`) },
        { key: "def_hash", label: "Definition hash", render: (r) => h("span", { class: "mono" }, r.def_hash) }], rows: d.views }).el),
      h("div", { class: "section grid cols-2" },
        card("Online lookup", h("div", { class: "stack" }, h("div", { class: "row" }, acct, h("button", { class: "btn secondary", onclick: () => api(`/api/features/online/${encodeURIComponent(acct.value.trim())}`).then((r) => (out.textContent = JSON.stringify(r, null, 2))).catch((e) => (out.textContent = e.message)) }, "Look up")), out)),
        card("Stores", h("dl", { class: "kv" }, h("dt", null, "Definitions"), h("dd", { class: "mono" }, d.definitions), h("dt", null, "Online store"), h("dd", null, d.online ? `${int(d.online.n)} accounts as of ${d.online.as_of} · materialised ${fmtAgo(d.online.materialized_at)}` : "—"),
          h("dt", null, "Feast mirror"), h("dd", null, d.feast.present ? `applied at ${d.feast.repo} (file offline store + sqlite online store)` : "not exported (optional)")))),
    ]);
  }

  // ── Scores dataset ─────────────────────────────────────────────────────
  async function scores() {
    const d = await api("/api/scoring/summary");
    const versions = [...new Set(d.outcome.map((r) => r.model_version))];
    const deciles = Array.from({ length: 10 }, (_, i) => i + 1);
    const chart = Charts.bars({ categories: deciles, stacked: false, catFmt: (x) => "D" + x, yFmt: (v) => (100 * v).toFixed(0) + "%",
      series: versions.map((v, i) => ({ name: "v" + v + " realised churn", color: VCOL(i), values: deciles.map((dd) => (d.outcome.find((r) => r.model_version === v && r.decile === dd) || {}).realised || 0) })) });
    render([
      head("Scores dataset", "Batch scores written back to the warehouse (mlops.scored_customers) with deciles and reason codes; mlops.v_score_outcomes joins them with realised churn as the 90-day window closes — ready for Superset / Power BI.",
        h("button", { class: "btn primary", onclick: (e) => busy(e.currentTarget, () => api("/api/scoring/run", { body: {} }).then((r) => { toast(`Scored ${int(r.rows)} customer-months with v${r.model_version}`, "success"); route(); }).catch(err)) }, icon("play", 14), "Run batch scoring")),
      h("div", { class: "grid cols-2" },
        card("Realised churn by score decile (outcomes known)", h("div", null, chart, Charts.legend(versions.map((v, i) => ({ name: "v" + v, color: VCOL(i) }))))),
        card("Scored months", table({ pageSize: 12, columns: [{ key: "snapshot_month", label: "Month" }, { key: "model_version", label: "Model", render: (r) => "v" + r.model_version }, { key: "n", label: "Customers", num: true, render: (r) => int(r.n) }, { key: "mean_score", label: "Mean score", num: true, render: (r) => f3(r.mean_score) }, { key: "high_risk", label: "High risk", num: true, render: (r) => int(r.high_risk) }], rows: d.by_month }).el)),
      h("div", { class: "section" }, card("Highest-risk customers (latest month)", table({ pageSize: 25, columns: [{ key: "ban", label: "BAN" }, { key: "score", label: "Score", num: true }, { key: "decile", label: "Decile", num: true }, { key: "risk_band", label: "Band", render: (r) => badge(r.risk_band === "high" ? "error" : "warning", r.risk_band) }, { key: "reason_1", label: "Reason 1" }, { key: "reason_2", label: "Reason 2" }, { key: "reason_3", label: "Reason 3" }, { key: "model_version", label: "Model", render: (r) => "v" + r.model_version }], rows: d.top }).el)),
    ]);
  }

  // ── router ─────────────────────────────────────────────────────────────
  // Model journey (journey.js): the default landing — the champion's (or the running) journey
  const journey = () => window.BSPJourney.page({ render, head, api, every, err });
  const ctx = { render, head, api, every, err };
  const gov = window.BSPPages || {};
  const PAGES = { journey, overview, leaderboard, registry, telemetry, drift, retrains, frozen, features, scores,
    release: () => gov.release(ctx), outcomes: () => gov.outcomes(ctx), slo: () => gov.slo(ctx), teach: () => gov.teach(ctx) };
  async function route() {
    clearInterval(timer);
    const page = (location.hash || "#journey").slice(1).split("?")[0];
    document.querySelectorAll(".bsp-nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === page));
    try { await (PAGES[page] || overview)(); }
    catch (e) { render([head("Something went wrong"), h("div", { class: "alert error" }, icon("error", 18), h("div", null, e.message || String(e)))]); }
  }
  async function topStatus() {
    try {
      const d = await api("/api/deployment");
      const bad = (d.divergence || []).filter((x) => x.level === "error" || x.level === "warning");
      document.getElementById("top-status").replaceChildren(badge("success", `champion v${d.aliases.champion || "—"}`), d.aliases.challenger ? badge("info", `challenger v${d.aliases.challenger}`) : "", badge("neutral", d.mode),
        bad.length ? h("a", { href: "#release", title: bad.map((x) => x.message).join("\n") }, badge(bad.some((x) => x.level === "error") ? "error" : "warning", "registry ≠ serving")) : badge("success", "serving = registry"));
    } catch (e) { document.getElementById("top-status").replaceChildren(badge("error", "gateway unreachable")); }
  }
  window.addEventListener("hashchange", route);
  route();
  topStatus();
  setInterval(topStatus, 15000);
})();
