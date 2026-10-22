/* Story: a guided, executive walk-through of the appliance. A vertical spine
   of chapters on the left, one chapter in focus on the right, each backed by
   live proof numbers from GET /studio/api/story (aggregates only, cached
   server-side for a minute). URL: #story/<n>. Keys: ← → / j k, Home/End,
   p = presenter mode, Esc = leave presenter mode. */
Studio.registerPage({
  id: "story",
  async render(el, ctx) {
    const { h, icon } = ctx.ui;
    const { fill } = StudioUI;
    const PRESENT_KEY = "studio.story.present";
    const app = document.getElementById("app");

    // Narrative. `api` = chapter key in the /studio/api/story payload;
    // `show` = preferred deep links (app ids from the catalog or Studio pages).
    // Copy rules: `title` is the claim (2-5 words), `mark` is the one word or
    // phrase in it that gets the signal-yellow highlight, `deck` is one plain
    // sentence (90 characters or fewer). `label` is the 1-3 word spine entry.
    // Short sentences, no em-dashes.
    const CHAPTERS = [
      {
        api: "ask", label: "The ask", icon: "rocket",
        title: "One stack, every team", mark: "every team",
        deck: "Data, ML and AI self-service on open source, on one platform, under one set of rules.",
        replaces: ["Self-service data", "Governed models", "AI on our own data"],
        talk: [
          "DataPlatform is building a better, faster, AI-ready enterprise. This is the workbench that gets us there.",
          "This appliance gives data engineers, data scientists and AI engineers one workbench they run themselves.",
          "Everything on screen is live. The numbers come from the services running right now.",
          "It is open source end to end. We own it, we can move it, and we pay for compute, not seats.",
        ],
        hood: "Docker Compose · nginx front door · Studio control plane · PostgreSQL warehouse",
        show: [{ page: "launchpad", label: "Open the Launchpad" }],
      },
      {
        api: "connect", label: "Connect", icon: "branch",
        title: "Data in, in minutes", mark: "in minutes",
        deck: "Teams land data from any source themselves, with no code.",
        talk: [
          "Pick a source, pick a destination, press run. The canvas builds the pipeline.",
          "Object storage, Postgres, Oracle, SQL Server and BigQuery. Outlook, SharePoint and REST APIs too.",
          "Incremental loads, schedules and PII masking are settings, not projects.",
          "Every run is logged with its lineage, so we always know where data came from.",
        ],
        hood: "dlt pipelines · Integration Canvas · enterprise connectors (mock or live) · PostgreSQL landing schema",
        show: [{ app: "canvas", label: "Show me the canvas" }],
      },
      {
        api: "model", label: "Model", icon: "layers",
        title: "One model of the customer", mark: "customer",
        deck: "Raw feeds become trusted tables keyed on BAN, tested on every build.",
        talk: [
          "Staging cleans. Intermediate joins. Marts answer business questions.",
          "Every fact hangs off the billing account number, so churn, revenue and network data line up.",
          "Hundreds of data tests run with each build. A failing test stops bad data before it reaches a dashboard.",
          "Docs and lineage are generated, not hand-written. DataHub can catalogue it all when switched on.",
        ],
        hood: "dbt Core · dbt Studio · Trino · DataHub (optional)",
        show: [{ app: "dbt", label: "Show me the models" }],
      },
      {
        api: "see", label: "See", icon: "dashboard",
        title: "One version of the truth", mark: "truth",
        deck: "Churn, revenue, network and billing dashboards on the governed model.",
        talk: [
          "Dashboards read the marts directly. The board-pack number matches the notebook number.",
          "Churn and retention, revenue bridge, network reliability and billing are ready on day one.",
          "Analysts build new charts themselves in SQL Lab.",
          "Access follows the same single sign-on roles as every other tool.",
        ],
        hood: "Apache Superset on the dbt marts · Power BI through the connector",
        show: [{ app: "superset", label: "Show me the dashboards" }],
      },
      {
        api: "predict", label: "Predict", icon: "insight",
        title: "Models with a paper trail", mark: "paper trail",
        deck: "The churn model trains, ships and retrains itself. Every step is on record.",
        talk: [
          "Features come from the same governed marts. No side copies of data.",
          "Every candidate is tracked. A new model becomes champion only if it beats the incumbent.",
          "Blue/green, canary and shadow serving test a new model on live traffic without risk.",
          "When drift appears, the system retrains on its own and records why.",
        ],
        hood: "MLflow · LightGBM · MLOps Control Tower · model gateway · frozen model bundles",
        show: [{ app: "mlops", label: "Show me the model journey" }],
      },
      {
        api: "automate", label: "Automate", icon: "zap",
        title: "AI that does the work", mark: "does the work",
        deck: "Disputes flow from inbox to SAP BRIM. Every call is tagged for insight.",
        talk: [
          "Dispute e-mails are read, classified and checked against billing. Confident cases are approved automatically.",
          "A browser bridge keys approved cases into SAP BRIM today. The released OData API is one setting away.",
          "Contact-centre calls are tagged against a 104-leaf taxonomy, with churn-risk flags.",
          "Managers review low-confidence tags in their portal. Each correction sharpens accuracy.",
        ],
        hood: "Dispute Desk · Playwright bridge · SAP BRIM connector · CCAI tagger · LLM gateway",
        show: [{ app: "disputes", label: "Show me the dispute desk" }, { app: "ccai", label: "Call insights" }],
      },
      {
        api: "ai", label: "Ask", icon: "chat",
        title: "Answers in plain English", mark: "plain English",
        deck: "Ask anything. Answers come from our data, our catalog and our runbooks.",
        talk: [
          "Open WebUI is the chat front end. The AI harness routes each request to the right model.",
          "Gemini on Vertex AI for quality. Local Ollama models as a fallback, so data can stay on the host.",
          "MCP tools reach BigQuery, Atlan, Power BI, Microsoft 365, the browser and the warehouse.",
          "Runbooks are indexed for retrieval, so answers cite their source.",
        ],
        hood: "Open WebUI · AI harness · MCP tools · Vertex AI Gemini · Ollama · Qdrant / pgvector",
        show: [{ app: "openwebui", label: "Show me the assistant" }, { app: "ai-harness", label: "AI harness" }],
      },
      {
        api: "govern", label: "Govern", icon: "shield",
        title: "Governed by default", mark: "default",
        deck: "One sign-in, encrypted secrets, controlled egress and a full audit trail.",
        talk: [
          "Every tool sits behind one front door that checks Entra ID first.",
          "Credentials live in an encrypted vault and are injected at start. Nobody passes passwords around.",
          "Outbound traffic goes through an allowlist with deny rules. Everything else is blocked and logged.",
          "Each component runs under its own cloud identity. Access is least-privilege and traceable.",
          "Every sign-in, secret reveal and change lands in the audit log.",
        ],
        hood: "Entra ID (OIDC) · Studio vault · egress proxy allowlist · per-component cloud identities · audit log",
        show: [{ page: "audit", label: "Show me the audit log" }, { page: "egress", label: "Egress policy" }, { page: "services", label: "Services" }],
      },
      {
        api: "own", label: "Own", icon: "key",
        title: "Ours to keep", mark: "keep",
        deck: "Open source on one platform. Every artifact exports. No lock-in.",
        talk: [
          "No per-seat licences. The cost is compute plus metered cloud model calls.",
          "Pipelines export as Python, models as frozen bundles, transformations as a dbt project in git.",
          "Any component can be swapped. Open formats and standard protocols throughout.",
          "Exit path: the same images and artifacts run anywhere containers run.",
        ],
        hood: "Docker Compose · open formats (Parquet, SQL, MLflow) · git · OSS licences",
        show: [{ href: "/docs/", label: "Show me the docs" }, { page: "services", label: "Services" }],
      },
    ];
    const N = CHAPTERS.length;
    const clamp = (i) => Math.max(0, Math.min(N - 1, i));
    let idx = clamp(parseInt(ctx.params.sub, 10) || 0);
    let data = null;
    let loadError = "";
    let present = sessionStorage.getItem(PRESENT_KEY) === "1";
    let talkOpen = false;   // the talk track stays open (or closed) as the presenter moves on

    // ── layout ───────────────────────────────────────────────────────────
    const progressBar = h("span");
    const progressLbl = h("span", { class: "caption" });
    const ageLbl = h("span", { class: "caption story-age" });
    const presentBtn = h("button", { type: "button", class: "btn secondary sm", "aria-pressed": "false",
      title: "Presenter mode (P). Hides the admin chrome and enlarges type." });
    const aboutBtn = h("button", { type: "button", class: "btn ghost sm", title: "About datacat", onclick: () => Studio.about() }, icon("info", 15), "About");
    const refreshBtn = h("button", { type: "button", class: "btn ghost icon sm", title: "Refresh live numbers", "aria-label": "Refresh live numbers" }, icon("refresh", 16));
    const rail = h("ol", { class: "story-rail", "aria-label": "Chapters" });
    const stage = h("section", { class: "story-stage", "aria-live": "polite" });
    const exitBtn = h("button", { type: "button", class: "btn secondary sm story-exit", title: "Leave presenter mode (Esc)" }, icon("close", 14), "Exit presenter");

    el.classList.add("story");
    el.append(
      h("header", { class: "story-head" },
        h("div", { class: "story-title" },
          h("span", { class: "eyebrow" }, "Executive story"),
          h("h1", { class: "story-page-title" }, "One governed stack for Data, ML and AI")),
        h("div", { class: "story-tools" },
          h("div", { class: "story-progress", role: "progressbar", "aria-valuemin": "1", "aria-valuemax": String(N) },
            h("div", { class: "meter" }, progressBar), progressLbl),
          ageLbl, refreshBtn, aboutBtn, presentBtn)),
      h("div", { class: "story-layout" }, h("nav", { class: "story-spine", "aria-label": "Story chapters" }, rail), stage),
      exitBtn);

    // ── helpers ──────────────────────────────────────────────────────────
    const myPages = new Set((ctx.me().pages || []).map((p) => p.id));
    function chapterData(c) { return data && data.chapters ? data.chapters[c.api] : null; }

    function resolveShow(c) {
      const cd = chapterData(c);
      const links = (cd && cd.links) || [];
      const out = [];
      for (const s of c.show) {
        if (s.page) {
          if (myPages.has(s.page)) out.push({ label: s.label, page: s.page, up: true });
        } else if (s.href) {
          out.push({ label: s.label, href: s.href, up: true });
        } else {
          const l = links.find((x) => x.id === s.app);
          if (l) out.push({ label: s.label, href: l.href, up: l.up });
          else if (!data) out.push({ label: s.label, href: null, up: false });
        }
      }
      return out;
    }

    function linkButton(s, primary) {
      const cls = `btn ${primary ? "primary lg" : "secondary"}`;
      if (s.page) return h("button", { type: "button", class: cls, onclick: () => ctx.navigate(s.page) }, s.label, icon("arrow-right", 16));
      if (!s.up || !s.href) {
        return h("button", { type: "button", class: `${cls} disabled`, disabled: true, title: "Not running in this appliance" },
          s.label, h("span", { class: "story-off" }, "not running"));
      }
      return h("a", { class: cls, href: s.href, target: "_blank", rel: "noopener", title: `Opens ${s.href} in a new tab` },
        s.label, icon("external", 15));
    }

    function stateDot(c) {
      const cd = chapterData(c);
      const st = cd ? cd.state : "loading";
      const label = { live: "Live", partial: "Partly live", absent: "Not in this appliance", loading: "Loading" }[st] || st;
      return h("span", { class: `story-dot ${st}`, title: label, "aria-label": label });
    }

    function tileEl(t) {
      const off = t.state !== "ok";
      const value = t.value === null || t.value === undefined || t.value === "" ? "n/a" : String(t.value);
      return h("div", { class: `story-tile ${t.state}` },
        h("div", { class: "lbl" }, t.label),
        h("div", { class: "val" }, value),
        h("div", { class: "det" }, off ? icon(t.state === "absent" ? "info" : "warning", 13) : null,
          t.detail || (t.state === "absent" ? "Not running in this appliance" : "")));
    }

    function skeletonTiles() {
      return [1, 2, 3].map(() => h("div", { class: "story-tile skeleton-tile" },
        h("div", { class: "skeleton sk-line", style: { width: "60%" } }), h("div", { class: "skeleton", style: { height: "34px", width: "45%", borderRadius: "6px", margin: "10px 0" } }),
        h("div", { class: "skeleton sk-line", style: { width: "80%" } })));
    }

    // ── render ───────────────────────────────────────────────────────────
    function renderRail() {
      fill(rail, ...CHAPTERS.map((c, i) => h("li", { class: `${i === idx ? "active" : ""} ${i < idx ? "past" : ""}` },
        h("button", { type: "button", "aria-current": i === idx ? "step" : null, onclick: () => go(i) },
          h("span", { class: "node", "aria-hidden": "true" }, String(i)),
          h("span", { class: "t" }, c.label),
          stateDot(c)))));
      const active = rail.children[idx];
      if (active && active.scrollIntoView && window.matchMedia("(max-width: 900px)").matches) {
        active.scrollIntoView({ block: "nearest", inline: "center", behavior: "smooth" });
      }
    }

    // eyebrow names where the spine label is shortened
    const NAMES = { see: "See it", model: "Model the business", own: "Own it" };

    // title text with the one highlighted phrase as its own span (text nodes only)
    function titleParts(c) {
      const at = c.mark ? c.title.indexOf(c.mark) : -1;
      if (at < 0) return [c.title];
      return [c.title.slice(0, at), h("span", { class: "story-mark" }, c.mark), c.title.slice(at + c.mark.length)].filter((x) => x !== "");
    }

    function renderStage() {
      const c = CHAPTERS[idx];
      const cd = chapterData(c);
      const shows = resolveShow(c);
      const talk = h("details", { class: "story-talk", open: talkOpen || null },
        h("summary", null, icon("notes", 15), "Talk track", h("span", { class: "caption" }, `${c.talk.length} points`)),
        h("ul", null, c.talk.map((t) => h("li", null, t))));
      talk.addEventListener("toggle", () => { talkOpen = talk.open; });
      let tiles;
      if (cd) tiles = cd.tiles.map(tileEl);
      else if (loadError) tiles = [tileEl({ label: "Live numbers", value: null, detail: loadError, state: "error" })];
      else tiles = skeletonTiles();
      fill(stage, h("article", { class: "story-card fade-in", "aria-labelledby": "story-h" },
        h("div", { class: "story-kicker" }, h("span", { class: "ic" }, icon(c.icon, 16)),
          h("span", { class: "story-eyebrow" }, `Chapter ${idx} · ${NAMES[c.api] || c.label}`), stateDot(c)),
        h("h2", { id: "story-h", class: "story-display" }, ...titleParts(c)),
        h("p", { class: "story-deck" }, c.deck),
        c.replaces ? h("div", { class: "story-replaces" }, h("span", { class: "eyebrow" }, "What every team gets"),
          ...c.replaces.map((r) => h("span", { class: "chip" }, r))) : null,
        h("div", { class: "story-tiles", "data-n": String(Math.min(tiles.length, 4)) }, tiles),
        h("div", { class: "story-actions" }, shows.map((s, i) => linkButton(s, i === 0))),
        talk,
        h("div", { class: "story-hood" }, icon("tool", 14), h("span", null, h("b", null, "Under the hood: "), c.hood))),
      h("div", { class: "story-nav" },
        h("button", { type: "button", class: "btn ghost", disabled: idx === 0, onclick: () => go(idx - 1) },
          icon("arrow-left", 16), idx > 0 ? CHAPTERS[idx - 1].label : "Start"),
        h("span", { class: "caption story-keys" }, "← → or j k to move · P presenter mode"),
        h("button", { type: "button", class: "btn ghost", disabled: idx === N - 1, onclick: () => go(idx + 1) },
          idx < N - 1 ? CHAPTERS[idx + 1].label : "The end", icon("arrow-right", 16))));
    }

    function renderProgress() {
      progressBar.style.width = `${Math.round(((idx + 1) / N) * 100)}%`;
      progressLbl.textContent = `${idx + 1} / ${N}`;
      progressLbl.parentElement.setAttribute("aria-valuenow", String(idx + 1));
      progressLbl.parentElement.setAttribute("aria-valuetext", `Chapter ${idx} of ${N - 1}: ${CHAPTERS[idx].label}`);
      if (data && data.generated_at) {
        ageLbl.textContent = `Live numbers · ${BSP.fmtAgo ? BSP.fmtAgo(data.generated_at) : data.generated_at}`;
        ageLbl.title = `Gathered ${data.generated_at}; refreshed at most once a minute`;
      } else ageLbl.textContent = loadError ? "Live numbers unavailable" : "Gathering live numbers…";
    }

    function renderPresent() {
      app.classList.toggle("story-present", present);
      presentBtn.setAttribute("aria-pressed", present ? "true" : "false");
      fill(presentBtn, icon(present ? "fullscreen" : "screen-share", 15), present ? "Presenting" : "Presenter mode");
      exitBtn.hidden = !present;
    }

    function render() { renderRail(); renderStage(); renderProgress(); }

    function go(i) {
      const n = clamp(i);
      if (n === idx && stage.childNodes.length) return;
      idx = n;
      // replaceState: keep the hash shareable (#story/3) without a full page re-route
      history.replaceState(null, "", `#story/${idx}`);
      render();
      if (!present) el.scrollIntoView({ block: "start", behavior: "smooth" });
    }

    function setPresent(v) {
      present = v;
      try { sessionStorage.setItem(PRESENT_KEY, v ? "1" : "0"); } catch (e) { /* private mode */ }
      renderPresent();
    }

    async function load(force) {
      try {
        data = await ctx.api(`/studio/api/story${force ? "?refresh=1" : ""}`);
        loadError = "";
      } catch (e) {
        if (!data) loadError = e.message || "Could not reach Studio";
      }
      render();
    }

    // ── events ───────────────────────────────────────────────────────────
    presentBtn.onclick = () => setPresent(!present);
    exitBtn.onclick = () => setPresent(false);
    refreshBtn.onclick = () => BSP.busy(refreshBtn, () => load(true));
    ctx.on("keydown", (e) => {
      if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return;
      const t = e.target;
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
      if (document.querySelector('[aria-modal="true"]')) return;
      const k = e.key;
      if (k === "ArrowRight" || k === "j" || k === "PageDown" || (k === " " && present)) { e.preventDefault(); go(idx + 1); }
      else if (k === "ArrowLeft" || k === "k" || k === "PageUp") { e.preventDefault(); go(idx - 1); }
      else if (k === "Home") { e.preventDefault(); go(0); }
      else if (k === "End") { e.preventDefault(); go(N - 1); }
      else if (k === "p" || k === "P") { e.preventDefault(); setPresent(!present); }
      else if (k === "Escape" && present) { setPresent(false); }
      else if (k === "t" || k === "T") { const d = stage.querySelector(".story-talk"); if (d) d.open = !d.open; }
    });
    // leaving the page always restores the normal chrome
    ctx.on("hashchange", () => { if (!location.hash.startsWith("#story")) app.classList.remove("story-present"); });

    history.replaceState(null, "", `#story/${idx}`);
    renderPresent();
    render();
    await load(false);
    ctx.every(60000, () => load(false));
  },
  onLeave() {
    const app = document.getElementById("app");
    if (app) app.classList.remove("story-present");
  },
});
