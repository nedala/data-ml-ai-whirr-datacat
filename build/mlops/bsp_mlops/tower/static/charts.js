/* Tiny dependency-free SVG charts for the control tower (no CDN in the appliance).
   Charts.line({series:[{name,color,points:[[x,y],...]}], height, yMax, thresholds:[{y,label,color}], xFmt, yFmt})
   Charts.bars({categories:[...], series:[{name,color,values:[...]}], stacked, height, yFmt})
   Charts.legend(series) */
(function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const PALETTE = ["#0b6e79", "#3380f0", "#ff8205", "#3ae1ce", "#6ebce9", "#186371", "#ffcf00", "#828487"];
  function el(tag, attrs, text) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs || {}) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    return e;
  }
  function niceMax(v) {
    if (!isFinite(v) || v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
    return 10 * p;
  }
  function frame(width, height, pad) {
    const svg = el("svg", { viewBox: `0 0 ${width} ${height}`, class: "chart", role: "img" });
    return { svg, x0: pad.l, x1: width - pad.r, y0: height - pad.b, y1: pad.t };
  }
  function yAxis(f, yMax, yFmt, ticks) {
    const g = el("g", { class: "grid" });
    for (let i = 0; i <= ticks; i++) {
      const v = (yMax * i) / ticks, y = f.y0 - ((f.y0 - f.y1) * i) / ticks;
      g.append(el("line", { x1: f.x0, x2: f.x1, y1: y, y2: y }));
      f.svg.append(el("text", { x: f.x0 - 6, y: y + 4, "text-anchor": "end" }, yFmt(v)));
    }
    f.svg.append(g);
  }
  function line({ series, height = 220, width = 720, yMax, yMin = 0, thresholds = [], xFmt, yFmt, xType = "time" }) {
    const f = frame(width, height, { l: 46, r: 12, t: 12, b: 26 });
    yFmt = yFmt || ((v) => (Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(2)));
    const pts = series.flatMap((s) => s.points);
    if (!pts.length) { f.svg.append(el("text", { x: width / 2, y: height / 2, "text-anchor": "middle" }, "No data yet")); return f.svg; }
    const xs = pts.map((p) => +p[0]);
    const xMin = Math.min(...xs), xMax = Math.max(...xs) === xMin ? xMin + 1 : Math.max(...xs);
    const top = niceMax(yMax != null ? yMax : Math.max(...pts.map((p) => p[1]), ...thresholds.map((t) => t.y)) * 1.05);
    const X = (x) => f.x0 + ((x - xMin) / (xMax - xMin)) * (f.x1 - f.x0);
    const Y = (y) => f.y0 - ((y - yMin) / (top - yMin)) * (f.y0 - f.y1);
    yAxis(f, top, yFmt, 4);
    xFmt = xFmt || ((x) => xType === "time" ? new Date(x).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : String(x));
    for (let i = 0; i <= 4; i++) {
      const x = xMin + ((xMax - xMin) * i) / 4;
      f.svg.append(el("text", { x: X(x), y: height - 8, "text-anchor": i === 0 ? "start" : i === 4 ? "end" : "middle" }, xFmt(x)));
    }
    thresholds.forEach((t) => {
      f.svg.append(el("line", { x1: f.x0, x2: f.x1, y1: Y(t.y), y2: Y(t.y), stroke: t.color || "#c0392b", "stroke-dasharray": "5 4", "stroke-width": 1.2 }));
      f.svg.append(el("text", { x: f.x1 - 4, y: Y(t.y) - 4, "text-anchor": "end", fill: t.color || "#c0392b" }, t.label || ""));
    });
    series.forEach((s, i) => {
      const c = s.color || PALETTE[i % PALETTE.length];
      const sorted = [...s.points].sort((a, b) => a[0] - b[0]);
      if (!sorted.length) return;
      f.svg.append(el("path", { d: sorted.map((p, j) => `${j ? "L" : "M"}${X(+p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(""), fill: "none", stroke: c, "stroke-width": 2, "stroke-dasharray": s.dash || "" }));
      if (sorted.length < 60) sorted.forEach((p) => {
        const dot = el("circle", { cx: X(+p[0]), cy: Y(p[1]), r: 2.6, fill: s.pointColor ? s.pointColor(p) : c });
        dot.append(el("title", {}, `${s.name}: ${yFmt(p[1])} @ ${xFmt(+p[0])}`));
        f.svg.append(dot);
      });
    });
    return f.svg;
  }
  function bars({ categories, series, stacked = true, height = 220, width = 720, yFmt, catFmt, maxLabels = 12 }) {
    const f = frame(width, height, { l: 46, r: 12, t: 12, b: 26 });
    yFmt = yFmt || ((v) => (v >= 1000 ? (v / 1000).toFixed(1) + "k" : v >= 10 ? v.toFixed(0) : v.toFixed(2)));
    catFmt = catFmt || String;
    if (!categories.length) { f.svg.append(el("text", { x: width / 2, y: height / 2, "text-anchor": "middle" }, "No data yet")); return f.svg; }
    const totals = categories.map((_, i) => stacked ? series.reduce((a, s) => a + (s.values[i] || 0), 0) : Math.max(...series.map((s) => s.values[i] || 0)));
    const top = niceMax(Math.max(...totals) * 1.05);
    yAxis(f, top, yFmt, 4);
    const bw = (f.x1 - f.x0) / categories.length;
    const inner = stacked ? bw * 0.78 : (bw * 0.82) / series.length;
    const step = Math.max(1, Math.ceil(categories.length / maxLabels));
    categories.forEach((c, i) => {
      let acc = 0;
      series.forEach((s, k) => {
        const v = s.values[i] || 0;
        if (!v) return;
        const h = (v / top) * (f.y0 - f.y1);
        const x = stacked ? f.x0 + i * bw + (bw - inner) / 2 : f.x0 + i * bw + bw * 0.09 + k * inner;
        const y = stacked ? f.y0 - acc - h : f.y0 - h;
        const r = el("rect", { x, y, width: Math.max(1, inner - (stacked ? 0 : 1)), height: Math.max(0.5, h), fill: s.color || PALETTE[k % PALETTE.length], rx: 1.5 });
        r.append(el("title", {}, `${s.name} · ${catFmt(c)}: ${yFmt(v)}`));
        f.svg.append(r);
        if (stacked) acc += h;
      });
      if (i % step === 0) f.svg.append(el("text", { x: f.x0 + i * bw + bw / 2, y: height - 8, "text-anchor": "middle" }, catFmt(c)));
    });
    return f.svg;
  }
  function legend(series) {
    const d = document.createElement("div");
    d.className = "legend";
    series.forEach((s, i) => {
      const sp = document.createElement("span");
      const sw = document.createElement("i");
      sw.style.background = s.color || PALETTE[i % PALETTE.length];
      sp.append(sw, s.name);
      d.append(sp);
    });
    return d;
  }
  window.Charts = { line, bars, legend, PALETTE };
})();
