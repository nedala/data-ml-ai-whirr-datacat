/* Workbench · Files (/files/) — browse the shared lake (/data/lake) and your
   workspace (~/work): upload, download, preview, organise, and hand tables
   to Import & Export for loading into the warehouse. Vanilla JS on BSP + WB. */
(function () {
  "use strict";
  const { h, icon } = BSP;
  const api = WB.api;
  const A = (p) => `/files/api${p}`;
  const q = (o) => Object.entries(o).filter(([, v]) => v !== undefined && v !== null).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");

  const shell = WB.shell("files");
  const state = { root: "lake", path: "", entries: [], filter: "", hidden: false, status: null, protected: false };
  const ABS = { lake: "/data/lake", work: "/home/analyst/work" };
  const NICE = { lake: "Shared lake", work: "My work" };
  const FOLDER_HINT = {
    landing: "Raw files on their way into the warehouse",
    reference: "Lookup tables, mappings, rate tables",
    exports: "Results exported from the warehouse",
    samples: "Sample datasets",
  };

  (function readHash() {
    const m = (location.hash || "").match(/^#\/(lake|work)(?:\/(.*))?$/);
    if (m) { state.root = m[1]; state.path = decodeURIComponent(m[2] || ""); }
  })();
  const writeHash = () => history.replaceState(null, "", `#/${state.root}${state.path ? "/" + state.path.split("/").map(encodeURIComponent).join("/") : ""}`);

  // ── layout ─────────────────────────────────────────────────────────
  const side = h("aside", { class: "fx-side", "aria-label": "Locations" });
  const crumbs = h("nav", { class: "crumbs", "aria-label": "Folder path" });
  const bar = h("div", { class: "fx-bar" });
  const listHost = h("div");
  const drop = h("div", { class: "fx-drop", "aria-hidden": "true" }, h("div", null, h("div", { class: "big" }, icon("upload", 26)),
    h("div", { style: { fontWeight: 600, fontSize: "16px" } }, "Drop to upload"), h("div", { class: "caption dropto" }, "")));
  const mainEl = h("section", { class: "fx-main", id: "fx-main" }, drop, crumbs, bar, listHost);
  shell.main.append(h("div", { class: "fx" }, side, mainEl));

  // ── sidebar ────────────────────────────────────────────────────────
  function renderSide() {
    const roots = (state.status && state.status.roots) || [{ id: "lake", label: NICE.lake, folders: Object.keys(FOLDER_HINT) }, { id: "work", label: NICE.work, folders: [] }];
    const items = [];
    for (const r of roots) {
      const active = state.root === r.id && !state.path;
      items.push(h("button", { class: `bk ${active ? "active" : ""}`, onclick: () => go(r.id, ""), title: ABS[r.id] },
        h("span", { class: "bic" }, icon(r.id === "lake" ? "database" : "profile", 16)),
        h("span", { class: "txt" }, h("div", { class: "nm" }, r.label),
          h("div", { class: "meta" }, r.exists === false ? "not mounted" : r.id === "lake" ? "shared with the team" : "only you"))));
      for (const f of r.folders || []) {
        const on = state.root === r.id && (state.path === f || state.path.startsWith(f + "/"));
        items.push(h("button", { class: `bk ${on ? "active" : ""}`, style: { paddingLeft: "26px" }, onclick: () => go(r.id, f), title: FOLDER_HINT[f] || "" },
          h("span", { class: "bic", style: { width: "24px", height: "24px" } }, icon("folder", 14)),
          h("span", { class: "txt" }, h("div", { class: "nm", style: { fontWeight: 500 } }, f),
            h("div", { class: "meta" }, FOLDER_HINT[f] || ""))));
      }
    }
    const lake = roots.find((r) => r.id === state.root) || {};
    side.replaceChildren(
      h("div", { class: "fx-side-head" }, h("h4", null, "Locations")),
      h("div", { class: "fx-buckets" }, items),
      h("div", { class: "fx-side-foot" },
        lake.free != null ? h("div", null, `${BSP.fmtBytes(lake.free)} free`) : null,
        state.status ? h("div", null, `Uploads up to ${BSP.fmtBytes(state.status.limits.upload_max_bytes)}`) : null,
        h("div", { style: { marginTop: "6px" } }, "In notebooks: ", h("code", null, ABS[state.root]))));
  }

  // ── navigation ─────────────────────────────────────────────────────
  function go(root, path) {
    state.root = root; state.path = path || ""; state.filter = "";
    writeHash(); renderSide(); load();
  }

  function renderCrumbs(list) {
    const parts = (list && list.crumbs) || [{ name: NICE[state.root], path: "" }];
    crumbs.replaceChildren(...parts.flatMap((c, i) => {
      const last = i === parts.length - 1;
      const btn = h("button", { class: last ? "cur" : "", "aria-current": last ? "location" : null, onclick: () => go(state.root, c.path) },
        i === 0 ? icon(state.root === "lake" ? "database" : "profile", 16) : null, c.name);
      return i ? [h("span", { class: "sep" }, icon("chevron-right", 14)), btn] : [btn];
    }));
  }

  // ── listing ────────────────────────────────────────────────────────
  let table = null;
  const filterInput = h("input", { class: "input", type: "search", placeholder: "Filter this folder", "aria-label": "Filter files in this folder",
    oninput: BSP.debounce(() => { state.filter = filterInput.value.toLowerCase(); fill(); }, 120) });
  const selPill = h("span");
  const fileInput = h("input", { type: "file", multiple: true, style: { display: "none" }, onchange: () => { upload([...fileInput.files]); fileInput.value = ""; } });
  const upBtn = h("button", { class: "btn primary sm", onclick: () => fileInput.click() }, icon("upload", 14), "Upload");
  const newBtn = h("button", { class: "btn secondary sm", onclick: newFolder }, icon("folder", 14), "New folder");
  const moreBtn = h("button", { class: "btn ghost icon sm", "aria-label": "More", title: "More" }, icon("more", 16));
  moreBtn.onclick = () => BSP.menu(moreBtn, [
    { label: state.hidden ? "Hide hidden files" : "Show hidden files", icon: state.hidden ? "visibility-off" : "visibility", onClick: () => { state.hidden = !state.hidden; load(); } },
    { label: "Copy folder path", icon: "copy", onClick: () => BSP.copy(absPath(state.path), "Path") },
  ]);
  bar.append(h("div", { class: "search" }, icon("search", 16), filterInput), selPill, h("span", { class: "spacer" }),
    h("button", { class: "btn ghost icon sm", "aria-label": "Refresh", title: "Refresh", onclick: () => load() }, icon("refresh", 16)),
    moreBtn, newBtn, upBtn, fileInput);
  WB.shortcut("/", () => filterInput.focus());
  WB.shortcut("u", () => fileInput.click());

  const absPath = (rel) => `${ABS[state.root]}${rel ? "/" + rel : ""}`;
  const join = (name) => (state.path ? `${state.path}/${name}` : name);
  const FIC = { folder: ["folder", "folder"], table: ["file-table", "table"], image: ["image", "image"], code: ["file-code", "code"], text: ["document", ""], file: ["file", ""] };

  async function load() {
    listHost.replaceChildren(WB.skeletonRows(6));
    renderCrumbs(null);
    let r;
    try { r = await api(A(`/list?${q({ root: state.root, path: state.path, hidden: state.hidden ? 1 : 0 })}`)); }
    catch (e) {
      if (e.status === 404 && state.path) { BSP.toast("That folder no longer exists", "info"); return go(state.root, ""); }
      listHost.replaceChildren(h("div", { class: "alert error" }, icon("error", 18), h("div", null, h("div", { class: "title" }, "Can't open this folder"), e.message)));
      return;
    }
    state.entries = r.entries; state.protected = r.protected;
    renderCrumbs(r);
    filterInput.value = state.filter;
    table = BSP.table({
      rows: [], pageSize: 200, rowKey: "name", selectable: true,
      onRowClick: (e) => (e.type === "dir" ? go(state.root, join(e.name)) : preview(e)),
      empty: WB.empty({ ic: "folder-open", title: state.filter ? "Nothing matches the filter" : "This folder is empty",
        body: state.filter ? "" : "Drag files here, or use Upload. CSV, Excel, Parquet and JSON files can be loaded into the warehouse from here.",
        actions: state.filter ? null : [h("button", { class: "btn primary sm", onclick: () => fileInput.click() }, icon("upload", 14), "Upload files")] }),
      columns: [
        { key: "name", label: "Name", sort: true, render: (e) => {
          const [ic, cls] = FIC[e.kind] || FIC.file;
          return h("button", { class: "obj-name", title: e.name, onclick: () => (e.type === "dir" ? go(state.root, join(e.name)) : preview(e)) },
            h("span", { class: `fic ${cls}` }, icon(ic, 15)), h("span", { class: "nm" }, e.name));
        } },
        { key: "size", label: "Size", num: true, sort: (e) => (e.type === "dir" ? -1 : e.size), render: (e) => (e.type === "dir" ? "" : BSP.fmtBytes(e.size)) },
        { key: "mtime", label: "Modified", sort: (e) => -e.mtime, render: (e) => h("span", { title: BSP.fmtTime(e.mtime * 1000) }, BSP.fmtAgo(e.mtime * 1000)) },
        { key: "_a", label: "", class: "actions", render: (e) => h("div", { class: "row", style: { gap: "2px", justifyContent: "flex-end" } },
          e.kind === "table" ? h("button", { class: "btn ghost sm", title: "Load this file into the warehouse", onclick: () => loadIntoWarehouse(e) }, icon("database", 14), "Load") : null,
          e.type === "file" ? h("button", { class: "btn ghost icon sm", "aria-label": `Download ${e.name}`, title: "Download", onclick: () => download(e) }, icon("download", 15)) : null,
          h("button", { class: "btn ghost icon sm", "aria-label": `Actions for ${e.name}`, onclick: (ev) => rowMenu(ev.currentTarget, e) }, icon("more", 16))) },
      ],
    });
    table.el.addEventListener("selection", renderSel);
    fill();
    listHost.replaceChildren(r.truncated ? h("div", { class: "alert warning", style: { marginBottom: "10px" } }, icon("warning", 18), "Showing the first 5,000 entries of this folder.") : "", table.el);
    renderSel();
  }
  function fill() {
    if (!table) return;
    const f = state.filter;
    table.update(f ? state.entries.filter((e) => e.name.toLowerCase().includes(f)) : state.entries);
    renderSel();
  }
  function renderSel() {
    const sel = table ? table.selected() : [];
    if (!sel.length) return selPill.replaceChildren();
    selPill.replaceChildren(h("span", { class: "sel-pill" }, `${sel.length} selected`,
      sel.length === 1 && sel[0].type === "file" ? h("button", { class: "btn ghost sm", onclick: () => download(sel[0]) }, icon("download", 14), "Download") : null,
      h("button", { class: "btn ghost sm", onclick: () => remove(sel) }, icon("delete", 14), "Delete"),
      h("button", { class: "btn ghost icon sm", "aria-label": "Clear selection", onclick: () => { table.clearSelection(); renderSel(); } }, icon("close", 14))));
  }

  function rowMenu(anchor, e) {
    const prot = state.root === "lake" && !state.path && Object.keys(FOLDER_HINT).includes(e.name);
    BSP.menu(anchor, [
      e.type === "dir" ? { label: "Open", icon: "folder-open", onClick: () => go(state.root, join(e.name)) } : { label: "Preview", icon: "visibility", onClick: () => preview(e) },
      e.type === "file" ? { label: "Download", icon: "download", onClick: () => download(e) } : null,
      e.kind === "table" ? { label: "Load into warehouse…", icon: "database", onClick: () => loadIntoWarehouse(e) } : null,
      { label: "Copy path for notebooks", icon: "copy", onClick: () => BSP.copy(absPath(join(e.name)), "Path") },
      prot ? null : "-",
      prot ? null : { label: "Rename…", icon: "rename", onClick: () => rename(e) },
      prot ? null : { label: "Delete…", icon: "delete", danger: true, onClick: () => remove([e]) },
    ]);
  }

  // ── actions ────────────────────────────────────────────────────────
  function download(e) { WB.download(A(`/download?${q({ root: state.root, path: join(e.name) })}`)); }
  function loadIntoWarehouse(e, path) {
    location.href = `/ingress/#/in?${q({ root: state.root, path: path || join(e.name) })}`;
  }
  async function newFolder() {
    const name = await BSP.prompt({ title: "New folder", label: "Folder name", placeholder: "e.g. 2025-renewals", okText: "Create" });
    if (!name || !name.trim()) return;
    try { await api(A("/mkdir"), { body: { root: state.root, path: state.path, name: name.trim() } }); BSP.toast(`Created ${name.trim()}`, "success"); load(); }
    catch (e) { BSP.toast(e.message, "error"); }
  }
  async function rename(e) {
    const name = await BSP.prompt({ title: `Rename ${e.type === "dir" ? "folder" : "file"}`, label: "New name", value: e.name, okText: "Rename",
      hint: e.type === "file" ? "Keep the extension (.csv, .xlsx …) so the file still opens correctly." : null });
    if (!name || name.trim() === e.name) return;
    try { await api(A("/rename"), { body: { root: state.root, path: join(e.name), new_name: name.trim() } }); BSP.toast("Renamed", "success"); load(); }
    catch (err) { BSP.toast(err.message, "error"); }
  }
  async function remove(list) {
    const dirs = list.filter((e) => e.type === "dir").length;
    const ok = await BSP.confirm({
      title: list.length === 1 ? `Delete ${list[0].name}?` : `Delete ${list.length} items?`, danger: true, okText: "Delete permanently",
      body: h("div", null,
        h("p", null, "This can't be undone — there is no recycle bin.", dirs ? ` ${dirs === 1 ? "The folder" : "Folders"} and everything inside will be deleted.` : ""),
        state.root === "lake" ? h("div", { class: "alert warning", style: { marginTop: "10px" } }, icon("warning", 18), "The shared lake is shared with your team — make sure nobody else needs these files.") : null,
        h("div", { class: "row wrap", style: { marginTop: "10px" } }, list.slice(0, 20).map((e) => h("code", { class: "chip outline" }, e.name)), list.length > 20 ? h("span", { class: "caption" }, `+${list.length - 20} more`) : null)),
    });
    if (!ok) return;
    try {
      const r = await api(A("/delete"), { body: { root: state.root, paths: list.map((e) => join(e.name)) } });
      BSP.toast(`Deleted ${r.deleted.length} item${r.deleted.length === 1 ? "" : "s"}`, "success");
      load();
    } catch (err) { BSP.toast(err.message, "error"); }
  }

  // ── preview drawer ─────────────────────────────────────────────────
  async function preview(e, sheet) {
    const path = join(e.name);
    const bodyEl = h("div", { class: "stack lg" }, WB.skeletonRows(5));
    const foot = [
      h("button", { class: "btn ghost", onclick: () => BSP.copy(absPath(path), "Path") }, icon("copy", 16), "Copy path"),
      h("button", { class: "btn secondary", onclick: () => download(e) }, icon("download", 16), "Download"),
    ];
    const loadBtn = h("button", { class: "btn primary", onclick: () => loadIntoWarehouse(e) }, icon("database", 16), "Load into warehouse");
    if (e.kind === "table") foot.push(loadBtn);
    BSP.drawer({ title: e.name, subtitle: `${NICE[state.root]} › ${path} · ${BSP.fmtBytes(e.size)} · modified ${BSP.fmtAgo(e.mtime * 1000)}`, wide: true, body: bodyEl, foot });
    let p;
    try { p = await api(A(`/preview?${q({ root: state.root, path, sheet })}`)); }
    catch (err) { bodyEl.replaceChildren(h("div", { class: "alert error" }, icon("error", 18), h("div", null, h("div", { class: "title" }, "Couldn't preview this file"), err.message))); return; }
    const parts = [];
    if (p.message && p.type !== "none") parts.push(h("div", { class: "alert warning" }, icon("warning", 18), h("div", null, p.message)));
    if (p.type === "table") {
      if (p.sheets && p.sheets.length > 1) {
        const sel = h("select", { class: "select", style: { width: "auto" }, "aria-label": "Sheet", onchange: () => { closeDrawers(); preview(e, sel.value); } },
          p.sheets.map((s) => h("option", { value: s, selected: s === p.sheet || null }, s)));
        parts.push(h("div", { class: "row" }, h("span", { class: "caption" }, "Sheet"), sel));
      }
      parts.push(h("div", { class: "row wrap", style: { gap: "8px" } },
        h("span", { class: "chip" }, p.format.toUpperCase()),
        h("span", { class: "chip" }, `${p.columns.length} columns`),
        h("span", { class: "chip" }, p.total_rows != null ? `${Number(p.total_rows).toLocaleString()} rows` : "row count not computed (large file)"),
        h("span", { class: "caption" }, `showing the first ${p.rows.length}`)));
      parts.push(gridOf(p.columns, p.rows));
    } else if (p.type === "text") {
      parts.push(h("pre", { class: "codeblock pv-text" }, p.text || "(empty file)"));
      if (p.truncated) parts.push(h("div", { class: "caption" }, "Only the beginning of the file is shown — download it to see everything."));
    } else if (p.type === "image") {
      parts.push(h("div", { class: "pv-img" }, h("img", { src: A(`/raw?${q({ root: state.root, path })}`), alt: e.name })));
    } else {
      parts.push(WB.empty({ ic: "file", title: "No preview", body: p.message || "Download the file to open it." }));
    }
    bodyEl.replaceChildren(...parts);
  }
  function closeDrawers() { document.querySelectorAll(".drawer.open .drawer-head .btn").forEach((b) => b.click()); }

  function gridOf(cols, rows) {
    return h("div", { class: "table-wrap pv-table" }, h("table", { class: "table" },
      h("thead", null, h("tr", null, cols.map((c) => h("th", { title: c.name }, c.name, h("span", { class: "ty" }, c.type || c.source_type || ""))))),
      h("tbody", null, rows.map((r) => h("tr", null, r.map((v) => v === null ? h("td", { class: "null" }, "null") : h("td", { title: String(v) }, typeof v === "object" ? JSON.stringify(v) : String(v))))))));
  }

  // ── uploads (streamed PUT with progress) ───────────────────────────
  let tray = null, trayList = null, active = 0;
  const queue = [];
  function ensureTray() {
    if (tray && tray.isConnected) return;
    trayList = h("div", { class: "tray-list" });
    const t = h("div", { class: "t" }, "Uploads");
    const min = h("button", { class: "btn ghost icon sm", "aria-label": "Minimise", onclick: () => tray.classList.toggle("min") }, icon("chevron-down", 14));
    const close = h("button", { class: "btn ghost icon sm", "aria-label": "Close", onclick: () => { if (!active && !queue.length) tray.remove(); else BSP.toast("Uploads are still running"); } }, icon("close", 14));
    tray = h("div", { class: "tray", role: "region", "aria-label": "Uploads" }, h("div", { class: "tray-head" }, icon("upload", 16), t, min, close), trayList);
    document.body.appendChild(tray);
  }
  async function upload(files) {
    if (!files.length) return;
    const limit = state.status ? state.status.limits.upload_max_bytes : Infinity;
    const big = files.filter((f) => f.size > limit);
    if (big.length) BSP.toast(`${big.map((f) => f.name).join(", ")} ${big.length > 1 ? "are" : "is"} larger than the ${BSP.fmtBytes(limit)} upload limit`, "error");
    files = files.filter((f) => f.size <= limit);
    const existing = new Set(state.entries.map((e) => e.name));
    const clash = files.filter((f) => existing.has(f.name));
    let overwrite = false;
    if (clash.length) {
      overwrite = await BSP.confirm({ title: clash.length === 1 ? `Replace ${clash[0].name}?` : `Replace ${clash.length} existing files?`, okText: "Replace",
        cancelText: "Skip them", body: "Files with the same name already exist in this folder." });
      if (!overwrite) files = files.filter((f) => !existing.has(f.name));
    }
    if (!files.length) return;
    ensureTray(); tray.classList.remove("min");
    const target = { root: state.root, path: state.path };
    for (const f of files) {
      const meter = h("div", { class: "meter" }, h("span", { style: { width: "0%" } }));
      const s = h("span", { class: "s" }, "waiting");
      const row = h("div", { class: "up" }, h("span", { class: "n", title: f.name }, f.name), s, meter);
      trayList.prepend(row);
      queue.push({ f, row, s, meter, target, overwrite });
    }
    pump();
  }
  function pump() {
    while (active < 2 && queue.length) {
      const job = queue.shift();
      active++;
      send(job).finally(() => { active--; pump(); if (!active && !queue.length) load(); });
    }
  }
  function send({ f, row, s, meter, target, overwrite }) {
    return new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      xhr.open("PUT", A(`/upload?${q({ root: target.root, path: target.path, name: f.name, overwrite: overwrite ? 1 : 0 })}`));
      xhr.setRequestHeader("X-Studio", "1");
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      const t0 = Date.now();
      xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable) return;
        const pct = (100 * e.loaded) / e.total;
        meter.firstChild.style.width = `${pct}%`;
        const rate = e.loaded / Math.max(0.5, (Date.now() - t0) / 1000);
        s.textContent = `${Math.floor(pct)}% · ${BSP.fmtBytes(rate)}/s`;
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          row.classList.add("done"); meter.firstChild.style.width = "100%"; s.textContent = BSP.fmtBytes(f.size);
          const kind = /\.(csv|tsv|parquet|pq|xlsx|xlsm|xls|json|jsonl|ndjson)$/i.test(f.name);
          if (kind) {
            const rel = target.path ? `${target.path}/${f.name}` : f.name;
            row.appendChild(h("button", { class: "btn ghost sm", style: { gridColumn: "1 / -1", justifySelf: "start", height: "24px" },
              onclick: () => { location.href = `/ingress/#/in?${q({ root: target.root, path: rel })}`; } }, icon("database", 12), "Load into warehouse"));
          }
        } else {
          let msg = xhr.statusText || "failed";
          try { msg = JSON.parse(xhr.responseText).detail || msg; } catch (_) { /* not json */ }
          if (xhr.status === 413 && !/limit/.test(msg)) msg = "too large for the gateway (ask your admin to raise the upload limit)";
          row.classList.add("failed"); s.textContent = msg; s.title = msg;
        }
        resolve();
      };
      xhr.onerror = () => { row.classList.add("failed"); s.textContent = "network error"; resolve(); };
      const cancel = h("button", { class: "btn ghost icon sm", "aria-label": `Cancel ${f.name}`, style: { height: "20px", width: "20px" }, onclick: () => { xhr.abort(); row.classList.add("failed"); s.textContent = "cancelled"; cancel.remove(); resolve(); } }, icon("close", 12));
      s.after(cancel);
      xhr.onloadend = () => cancel.remove();
      s.textContent = "starting…";
      xhr.send(f);
    });
  }

  // drag & drop anywhere over the listing
  let dragDepth = 0;
  mainEl.addEventListener("dragenter", (e) => { if (![...(e.dataTransfer.types || [])].includes("Files")) return; e.preventDefault(); dragDepth++; drop.querySelector(".dropto").textContent = `into ${NICE[state.root]}${state.path ? " › " + state.path : ""}`; drop.classList.add("show"); });
  mainEl.addEventListener("dragover", (e) => { if ([...(e.dataTransfer.types || [])].includes("Files")) e.preventDefault(); });
  mainEl.addEventListener("dragleave", () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) drop.classList.remove("show"); });
  mainEl.addEventListener("drop", (e) => {
    e.preventDefault(); dragDepth = 0; drop.classList.remove("show");
    const items = [...(e.dataTransfer.items || [])];
    if (items.some((it) => it.webkitGetAsEntry && it.webkitGetAsEntry() && it.webkitGetAsEntry().isDirectory)) BSP.toast("Folders can't be dropped — create the folder, then drop its files in", "info");
    upload([...e.dataTransfer.files].filter((f) => f.size > 0 || f.type));
  });
  window.addEventListener("beforeunload", (e) => { if (active || queue.length) { e.preventDefault(); e.returnValue = ""; } });

  // ── boot ───────────────────────────────────────────────────────────
  renderSide();
  api(A("/status")).then((s) => { state.status = s; renderSide(); }).catch(() => {});
  load();
})();
