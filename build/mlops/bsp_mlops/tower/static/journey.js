/* Model journey — a vertical timeline spine (left rail) of every lifecycle stage of one
   registered model version, with the stage's evidence on the right. Data: GET /mlops/api/journeys,
   /mlops/api/journeys/{model}/{version}, /mlops/api/journeys/id/{journey_id}. Artifacts (PNG, JSON,
   executed notebook) are proxied by the mlops service from MLflow's artifact store.
   Registered by tower.js as page "journey"; doubles as the live progress view of a running demo
   or retrain (polls every 3 s, the current stage pulses). */
(function () {
  "use strict";
  const { h, icon, table, fmtAgo, fmtTime } = BSP;
  const NS = "http://www.w3.org/2000/svg";
  const STATUS_ICON = { done: "check", running: "sync", failed: "close", skipped: "minus", pending: null };
  const STATUS_LABEL = { done: "Done", running: "In progress", failed: "Failed", skipped: "Skipped", pending: "Not yet" };
  const BADGE = { done: "success", running: "info", failed: "error", skipped: "neutral", pending: "neutral" };
  const KEY_METRICS = {
    integration: ["rows", "accounts", "labelled_rows", "churn_rate"],
    eda: ["rows", "churn_rate", "n_columns", "max_missing_pct", "top_abs_corr_with_label"],
    notebook: ["cells_executed", "outputs", "execution_seconds"],
    selection: ["n_candidates", "n_kept", "n_dropped", "n_in_model"],
    holdout: ["n_train", "n_valid", "n_test", "time_optimism", "random_optimism"],
    feature_store: ["views", "online_accounts"],
    algo_selection: ["n_candidates", "best_valid_auc", "best_test_auc"],
    roc_auc: ["test_auc", "test_pr_auc", "base_rate", "n_test", "positives"],
    tuning: ["n_trials", "best_trial_valid_auc", "first_trial_valid_auc", "tuned_test_auc"],
    evaluation: ["test_auc", "test_lift_at_10", "test_capture_at_10", "test_ks", "test_brier", "test_logloss"],
    registration: ["version"],
    release: ["gates_passed", "gates", "delta_auc", "ci_low", "delong_p", "promotions"],
    deployment: ["served", "shadow_scored", "routing_events"],
    monitoring: ["checks", "breaches", "max_psi", "prediction_psi"],
    retrain: ["retrains", "promoted"],
    freeze: ["bundles", "size_mb"],
  };
  const PCT = /(churn_rate|base_rate|capture|optimism_pct|_pct_share)$/;

  const num = (v) => typeof v === "number" && isFinite(v);
  function fmt(v, key) {
    if (v == null || v === "") return "—";
    if (typeof v === "boolean") return v ? "yes" : "no";
    if (!num(v)) return String(v);
    if (key && PCT.test(key)) return (100 * v).toFixed(1) + "%";
    if (key && /pct$/.test(key)) return v.toFixed(1) + "%";
    if (Number.isInteger(v)) return v.toLocaleString();
    if (Math.abs(v) >= 1000) return Math.round(v).toLocaleString();
    if (Math.abs(v) >= 10) return v.toFixed(1);
    return v.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
  }
  const label = (k) => k.replace(/_/g, " ").replace(/\bauc\b/g, "AUC").replace(/\bpr\b/g, "PR").replace(/\bpsi\b/g, "PSI").replace(/\bks\b/g, "KS");
  function dur(s) {
    if (s == null) return "";
    if (s < 1) return "<1 s";
    if (s < 60) return Math.round(s) + " s";
    if (s < 3600) return (s / 60).toFixed(s < 600 ? 1 : 0) + " min";
    if (s < 86400) return (s / 3600).toFixed(1) + " h";
    return (s / 86400).toFixed(1) + " d";
  }
  function svg(tag, attrs, text) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs || {}) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    return e;
  }

  // ── inline SVG curve (ROC / PR) ─────────────────────────────────────────
  function curveChart({ points, title, xLabel, yLabel, diagonal, baseline, value }) {
    const W = 340, H = 300, L = 44, R = 12, T = 14, B = 38;
    const X = (x) => L + x * (W - L - R), Y = (y) => H - B - y * (H - T - B);
    const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "jy-curve", role: "img", "aria-label": `${title}: ${value}` });
    const grid = svg("g", { class: "jy-grid" });
    for (const t of [0, 0.25, 0.5, 0.75, 1]) {
      grid.append(svg("line", { x1: X(0), x2: X(1), y1: Y(t), y2: Y(t) }), svg("line", { x1: X(t), x2: X(t), y1: Y(0), y2: Y(1) }));
      s.append(svg("text", { x: L - 6, y: Y(t) + 4, "text-anchor": "end" }, t.toFixed(2)), svg("text", { x: X(t), y: H - B + 15, "text-anchor": "middle" }, t.toFixed(2)));
    }
    s.prepend(grid);
    s.append(svg("text", { x: (L + W - R) / 2, y: H - 6, "text-anchor": "middle", class: "jy-axis-label" }, xLabel),
      svg("text", { x: 12, y: (T + H - B) / 2, "text-anchor": "middle", transform: `rotate(-90 12 ${(T + H - B) / 2})`, class: "jy-axis-label" }, yLabel));
    if (diagonal) s.append(svg("line", { x1: X(0), y1: Y(0), x2: X(1), y2: Y(1), class: "jy-ref" }));
    if (baseline != null) s.append(svg("line", { x1: X(0), y1: Y(baseline), x2: X(1), y2: Y(baseline), class: "jy-ref" }),
      svg("text", { x: X(1) - 2, y: Y(baseline) - 5, "text-anchor": "end" }, `base rate ${baseline.toFixed(3)}`));
    const d = points.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join("");
    if (diagonal) s.append(svg("path", { d: `${d}L${X(1)},${Y(0)}L${X(0)},${Y(0)}Z`, class: "jy-area" }));
    s.append(svg("path", { d, class: "jy-line" }));
    s.append(svg("text", { x: X(diagonal ? 0.97 : 0.97), y: Y(diagonal ? 0.06 : 0.92), "text-anchor": "end", class: "jy-value" }, value));
    // hover read-out
    const dot = svg("circle", { r: 4.5, class: "jy-dot", visibility: "hidden" });
    const tip = svg("text", { class: "jy-tip", visibility: "hidden" });
    const hit = svg("rect", { x: X(0), y: Y(1), width: X(1) - X(0), height: Y(0) - Y(1), fill: "transparent" });
    s.append(dot, tip, hit);
    hit.addEventListener("mousemove", (e) => {
      const r = s.getBoundingClientRect();
      const x = ((e.clientX - r.left) * (W / r.width) - L) / (W - L - R);
      let best = points[0];
      for (const p of points) if (Math.abs(p[0] - x) < Math.abs(best[0] - x)) best = p;
      dot.setAttribute("cx", X(best[0])); dot.setAttribute("cy", Y(best[1])); dot.setAttribute("visibility", "visible");
      tip.textContent = `${xLabel.split(" ").pop()} ${best[0].toFixed(3)} · ${yLabel.split(" ").pop()} ${best[1].toFixed(3)}`;
      tip.setAttribute("x", Math.min(X(best[0]) + 8, W - R - 150)); tip.setAttribute("y", Math.max(Y(best[1]) - 8, T + 10)); tip.setAttribute("visibility", "visible");
    });
    hit.addEventListener("mouseleave", () => { dot.setAttribute("visibility", "hidden"); tip.setAttribute("visibility", "hidden"); });
    return h("figure", { class: "jy-figure" }, h("figcaption", null, title), s);
  }

  // PSI per feature, horizontal bars with warn / breach thresholds
  function psiChart(rows, warn, breach) {
    const top = rows.slice(0, 12);
    const W = 520, rowH = 22, L = 170, R = 48, T = 8, B = 26, H = T + B + rowH * top.length;
    const max = Math.max(breach * 1.3, ...top.map((r) => r.psi || 0));
    const X = (v) => L + (v / max) * (W - L - R);
    const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "jy-curve jy-psi", role: "img", "aria-label": "PSI per feature" });
    top.forEach((r, i) => {
      const y = T + i * rowH;
      s.append(svg("text", { x: L - 8, y: y + 15, "text-anchor": "end" }, r.feature));
      s.append(svg("rect", { x: L, y: y + 5, width: Math.max(2, X(r.psi || 0) - L), height: 12, rx: 3, class: `jy-bar ${r.status || ""}` }));
      s.append(svg("text", { x: X(r.psi || 0) + 5, y: y + 15, class: "jy-bar-value" }, (r.psi || 0).toFixed(3)));
    });
    for (const [v, cls, txt] of [[warn, "warn", "warn"], [breach, "breach", "breach"]]) {
      s.append(svg("line", { x1: X(v), x2: X(v), y1: T, y2: H - B + 4, class: `jy-threshold ${cls}` }));
      s.append(svg("text", { x: X(v), y: H - 6, "text-anchor": "middle", class: "jy-axis-label" }, `${txt} ${v}`));
    }
    return h("figure", { class: "jy-figure wide" }, h("figcaption", null, "PSI per feature (worst check)"), s);
  }

  function tableFor(t) {
    const cols = (t.columns || []).filter((c) => !["run_id", "experiment_id"].includes(c));
    return table({
      pageSize: t.rows.length > 12 ? 10 : 12, rows: t.rows,
      initialSort: null,
      columns: cols.map((c) => ({
        key: c, label: label(c), sort: true, num: t.rows.some((r) => num(r[c])),
        render: (r) => {
          const v = r[c];
          if (c === "chosen" || c === "best" || c === "in_model" || c === "names_this_version") return v ? h("span", { class: "badge success" }, h("span", { class: "dot" }), "yes") : h("span", { class: "muted" }, "—");
          if (c === "decision") return h("span", { class: `badge ${v === "keep" ? "success" : "neutral"}` }, h("span", { class: "dot" }), v);
          if (c === "result") return h("span", { class: `badge ${/fail/i.test(v) ? "error" : v === "warn" ? "warning" : v === "n/a" || v === "skip" ? "neutral" : "success"}` }, h("span", { class: "dot" }), v);
          if (c === "detail" && typeof v === "string") return h("span", { style: { whiteSpace: "normal" } }, v);
          if (c === "status" && typeof v === "string") return h("span", { class: `badge ${v === "breach" || v === "failed" ? "error" : v === "warn" ? "warning" : "success"}` }, h("span", { class: "dot" }), v);
          if (c === "sha256" || c === "def_hash" || c === "snapshot_sha256") return h("span", { class: "mono" }, String(v || "").slice(0, 16) + (v && v.length > 16 ? "…" : ""));
          if (/(_ts|^ts|_at)$/.test(c) && v) return h("span", { title: v }, fmtTime(v));
          return fmt(v, c);
        },
      })),
    }).el;
  }

  // ── the page ────────────────────────────────────────────────────────────
  async function page(ctx) {
    const { render, head, api, every, err } = ctx;
    const q = new URLSearchParams((location.hash.split("?")[1] || ""));
    const list = await api("/api/journeys");
    const model = (list.models[0] || { name: "bsp_churn", versions: [] });
    const withJ = model.versions.filter((v) => v.journey_id);
    let sel = q.get("id") ? { id: q.get("id") } : q.get("v") ? { v: q.get("v") } : null;
    if (!sel) {
      const champ = withJ.find((v) => (v.aliases || []).includes("champion"));
      if (list.active) sel = list.active.version ? { v: list.active.version } : { id: list.active.journey_id };
      else if (champ) sel = { v: champ.version };
      else if (withJ.length) sel = { v: withJ[0].version };
      else if (list.journeys.length) sel = { id: list.journeys[0].journey_id };
    }
    let selected = q.get("stage") || null, followLive = !selected, last = null;

    const picker = h("select", { class: "select jy-picker", "aria-label": "Model version", onchange: (e) => { location.hash = "#journey?" + e.target.value; } });
    const running = list.journeys.filter((j) => j.status === "running" && !j.version);
    for (const j of running) picker.append(h("option", { value: "id=" + j.journey_id, selected: sel && sel.id === j.journey_id || null }, `Live · ${j.title || j.journey_id}`));
    for (const v of model.versions) {
      const al = (v.aliases || []).join(", ");
      picker.append(h("option", { value: "v=" + v.version, disabled: !v.journey_id || null, selected: sel && sel.v === v.version || null },
        `${model.name} v${v.version}${al ? " · " + al : ""}${v.model_kind ? " · " + v.model_kind : ""}${v.journey_id ? (v.kind === "backfill" ? " · reconstructed" : "") : " · no journey"}`));
    }
    const rail = h("aside", { class: "jy-rail" });
    const detail = h("section", { class: "jy-detail" });
    const actions = h("div", { class: "row wrap jy-actions" }, picker);
    render([
      head("Model journey", "Every step that produced this model version — data, analysis, selection, training, evaluation, registration and life in production — reconstructed from MLflow evidence. Click a stage."),
      actions,
      !sel ? h("div", { class: "alert info" }, icon("info", 18), h("div", null, "No journeys yet. Run ", h("code", null, "python -m bsp_mlops.demo"), " (or ", h("code", null, "python -m bsp_mlops.journey backfill"), " for versions registered earlier).")) : null,
      sel ? h("div", { class: "jy-layout" }, rail, detail) : null,
    ]);
    if (!sel) return;

    async function load() {
      const j = sel.id ? await api(`/api/journeys/id/${encodeURIComponent(sel.id)}`) : await api(`/api/journeys/${model.name}/${sel.v}`);
      // a live journey that has registered its version: keep following it under its version
      last = j;
      if (followLive && j.current) selected = j.current;
      if (!selected || !j.stages.find((s) => s.key === selected)) selected = (j.stages.find((s) => s.status === "done") || j.stages[0]).key;
      drawRail(j);
      drawDetail(j, j.stages.find((s) => s.key === selected));
      const live = j.status === "running" || j.stages.some((s) => s.status === "running");
      every(live ? 3000 : 30000, load);
    }

    function drawRail(j) {
      const pct = Math.round((100 * j.progress.done) / j.progress.total);
      const head = h("div", { class: "jy-rail-head" },
        h("div", { class: "jy-rail-title" }, h("b", null, j.version ? `${j.model} v${j.version}` : (j.title || j.journey_id)),
          ...(j.aliases || []).map((a) => h("span", { class: `badge ${a === "champion" ? "success" : "info"}` }, h("span", { class: "dot" }), a))),
        h("div", { class: "caption" }, `${j.kind === "backfill" ? "reconstructed" : j.kind} journey · ${j.status === "running" ? "running now" : j.status} · started ${fmtAgo(j.started)}`),
        h("div", { class: "jy-progress", role: "progressbar", "aria-valuenow": pct, "aria-valuemin": 0, "aria-valuemax": 100 }, h("span", { style: { width: pct + "%" } })),
        h("div", { class: "caption" }, `${j.progress.done} of ${j.progress.total} stages complete`));
      const ol = h("ol", { class: "jy-spine" }, j.stages.map((s, i) => {
        const ic = STATUS_ICON[s.status];
        return h("li", { class: `jy-step ${s.status}${s.key === selected ? " selected" : ""}${s.key === j.current ? " current" : ""}` },
          h("button", { type: "button", class: "jy-step-btn", "aria-current": s.key === selected ? "step" : null,
            onclick: () => { selected = s.key; followLive = false; drawRail(j); drawDetail(j, s); history.replaceState(null, "", `#journey?${sel.id ? "id=" + sel.id : "v=" + sel.v}&stage=${s.key}`); } },
            h("span", { class: "jy-dot", "aria-hidden": "true" }, ic ? icon(ic, 12) : h("span", { class: "jy-num" }, String(i + 1))),
            h("span", { class: "jy-step-body" },
              h("span", { class: "jy-step-title" }, s.title),
              h("span", { class: "jy-step-headline" }, s.headline || STATUS_LABEL[s.status]),
              h("span", { class: "jy-step-meta", title: s.started ? fmtTime(s.started) : null }, [STATUS_LABEL[s.status], s.duration_s != null && s.status !== "pending" ? dur(s.duration_s) : null, s.started ? fmtAgo(s.started) : null].filter(Boolean).join(" · ")))));
      }));
      rail.replaceChildren(h("div", { class: "card jy-rail-card" }, head, ol,
        h("div", { class: "jy-rail-foot" }, h("a", { class: "btn ghost sm", href: j.mlflow_url, target: "_blank", rel: "noopener" }, icon("open-in-new", 14), "Journey run in MLflow"),
          j.parent_journey_id ? h("a", { class: "btn ghost sm", href: `#journey?id=${j.parent_journey_id}` }, icon("branch", 14), "Parent journey") : null)));
    }

    function drawDetail(j, s) {
      if (!s) return;
      const keys = (KEY_METRICS[s.key] || []).filter((k) => s.metrics && s.metrics[k] != null);
      const extra = Object.keys(s.metrics || {}).filter((k) => !keys.includes(k)).slice(0, Math.max(0, 4 - keys.length));
      const tiles = [...keys, ...extra].slice(0, 6).map((k) => h("div", { class: "stat" }, h("div", { class: "label" }, label(k)), h("div", { class: "value" }, fmt(s.metrics[k], k))));
      const figs = [];
      if (s.curves && s.curves.roc) figs.push(curveChart({ points: s.curves.roc.points, title: "ROC curve — test window", xLabel: "false positive rate", yLabel: "true positive rate", diagonal: true, value: `AUC ${s.curves.roc.auc.toFixed(3)}` }));
      if (s.curves && s.curves.pr) figs.push(curveChart({ points: s.curves.pr.points, title: "Precision–recall — test window", xLabel: "recall", yLabel: "precision", baseline: s.curves.pr.base_rate, value: `PR-AUC ${s.curves.pr.ap.toFixed(3)}` }));
      if (s.data && s.data.psi && s.data.psi.length) figs.push(psiChart(s.data.psi, s.data.psi_warn, s.data.psi_breach));
      const curvesShown = s.curves && s.curves.roc;
      for (const c of s.charts || []) {
        if (curvesShown && /^(roc|pr)_curve\.png$/.test(c.path)) continue;
        figs.push(h("figure", { class: "jy-figure wide png" }, h("figcaption", null, c.title),
          h("img", { src: c.url, alt: c.title, loading: "lazy" }), c.caption ? h("div", { class: "caption" }, c.caption) : null));
      }
      const params = Object.entries(s.params || {}).filter(([k]) => !["kept", "dropped"].includes(k));
      const links = [
        s.mlflow_url ? h("a", { class: "btn secondary sm", href: s.mlflow_url, target: "_blank", rel: "noopener" }, icon("open-in-new", 14), "Open in MLflow") : null,
        ...(s.source_runs || []).slice(0, 8).map((r) => h("a", { class: "btn ghost sm", href: r.mlflow_url, target: "_blank", rel: "noopener" }, icon("link", 14), r.label)),
        ...(s.links || []).filter((l) => l.href).map((l) => h("a", { class: "btn ghost sm", href: l.href, target: l.href.startsWith("#") ? null : "_blank", rel: "noopener" }, icon(l.artifact ? "download" : "link", 14), l.label)),
      ].filter(Boolean);
      const files = (s.files || []).filter((f) => !/\.png$/.test(f.name));
      detail.replaceChildren(h("div", { class: `card jy-stage ${s.status}` },
        h("div", { class: "jy-stage-head" },
          h("div", null,
            h("div", { class: "eyebrow" }, `Stage ${j.stages.indexOf(s) + 1} of ${j.stages.length}${s.live ? " · live from the mlops schema" : ""}`),
            h("h2", null, s.title),
            h("div", { class: "row wrap jy-stage-meta" }, h("span", { class: `badge ${BADGE[s.status]}` }, h("span", { class: "dot" }), STATUS_LABEL[s.status]),
              s.duration_s != null && s.status !== "pending" ? h("span", { class: "caption" }, icon("clock", 12), " ", dur(s.duration_s)) : null,
              s.started ? h("span", { class: "caption" }, fmtTime(s.started) + (s.ended && s.ended !== s.started ? " → " + fmtTime(s.ended) : "")) : null)),
          links.length ? h("div", { class: "row wrap jy-links" }, links) : null),
        s.headline ? h("div", { class: "jy-headline" }, s.headline) : null,
        s.summary ? h("p", { class: "jy-summary" }, s.summary) : null,
        s.status === "running" ? h("div", { class: "alert info" }, icon("sync", 16), h("div", null, "This stage is running — evidence appears here when it completes.")) : null,
        ...(s.notes || []).map((n) => h("div", { class: "alert warning" }, icon("warning", 16), h("div", null, n))),
        tiles.length ? h("div", { class: "jy-tiles" }, tiles) : null,
        figs.length ? h("div", { class: "jy-figures" }, figs) : null,
        ...(s.tables || []).filter((t) => t.rows && t.rows.length).map((t) => h("div", { class: "jy-block" }, h("h4", null, t.title, h("span", { class: "caption" }, ` · ${t.rows.length} row${t.rows.length === 1 ? "" : "s"}`)), tableFor(t))),
        params.length ? h("div", { class: "jy-block" }, h("h4", null, "Parameters"), h("dl", { class: "kv jy-kv" }, params.flatMap(([k, v]) => [h("dt", null, label(k)), h("dd", { class: String(v).length > 40 ? "mono" : null }, String(v))]))) : null,
        files.length ? h("div", { class: "jy-block" }, h("h4", null, "Evidence files"), h("div", { class: "row wrap" }, files.map((f) => h("a", { class: "btn ghost sm", href: f.url, target: "_blank", rel: "noopener" }, icon("file", 14), f.name)))) : null,
        !s.tables.length && !figs.length && !tiles.length && s.status === "pending" ? h("div", { class: "jy-empty" }, icon("hourglass", 20), h("div", null, s.summary || "Nothing recorded yet.")) : null,
        teachPanel(s)));
    }

    // Teach: why it matters · what ran · evidence · code · GCP-native equivalent
    function teachPanel(s) {
      const t = s.teach;
      if (!t) return null;
      const P = window.BSPPages || {};
      const ev = [s.mlflow_url ? "MLflow stage run" : null, (s.source_runs || []).length ? `${s.source_runs.length} linked run(s)` : null,
        (s.tables || []).length ? `${s.tables.length} table(s)` : null, (s.charts || []).length ? `${s.charts.length} chart(s)` : null, (s.files || []).length ? `${s.files.length} file(s)` : null].filter(Boolean);
      return h("div", { class: "jy-block jy-teach" }, h("h4", null, icon("insight", 14), " Teach"),
        h("dl", { class: "kv jy-kv" },
          h("dt", null, "Why it matters"), h("dd", null, t.why),
          h("dt", null, "What ran"), h("dd", null, s.headline ? `${s.headline}${s.summary ? " — " + s.summary : ""}` : (s.summary || "—")),
          h("dt", null, "Evidence"), h("dd", null, ev.length ? ev.join(" · ") + " (above)" : "nothing recorded yet"),
          h("dt", null, "Code"), h("dd", null, h("div", { class: "stack sm" }, (t.code || []).map((r) => (P.codeLink ? P.codeLink(r) : h("span", { class: "mono" }, `${r.file}:${r.line}`))))),
          t.notebook ? h("dt", null, "Notebook") : null, t.notebook ? h("dd", { class: "mono" }, t.notebook.path) : null,
          h("dt", null, "GCP-native equivalent"), h("dd", null, t.gcp)));
    }

    await load().catch((e) => { err(e); detail.replaceChildren(h("div", { class: "alert error" }, icon("error", 18), h("div", null, e.message || String(e)))); });
  }

  window.BSPJourney = { page };
})();
