/* Certificates: the facade TLS certificate. dev → appliance CA (download +
   trust one-liners, re-issue); preprod/prod → upload a corporate cert or note
   that the ingress terminates TLS. */
Studio.registerPage({
  id: "certificates",
  async render(el, ctx) {
    const { h, icon } = ctx.ui;
    const { fill } = StudioUI;
    let info = await ctx.api("/studio/api/certs");
    const origin = info.facade_url;
    const caUrl = `${origin}/studio/ca.pem`;

    const status = h("div");
    const trust = h("div");
    const reissue = h("div");
    const upload = h("div");
    el.append(
      h("div", { class: "bsp-page-head" }, h("div", null, h("h1", null, "Certificates"),
        h("div", { class: "lede" }, "The certificate your browser sees at the appliance's front door."))),
      h("div", { class: "vstack" }, status, trust, reissue, upload));

    function cmd(text) {
      return h("div", { class: "cmd" }, h("pre", { class: "codeblock" }, text),
        h("button", { class: "btn ghost icon sm", title: "Copy", "aria-label": "Copy command", onclick: () => ctx.copy(text, "Command") }, icon("copy", 14)));
    }

    function renderStatus() {
      const s = info.server;
      if (info.termination === "ingress") {
        fill(status, h("div", { class: "alert info" }, icon("cloud", 18), h("div", null, h("div", { class: "title" }, "TLS is terminated by the ingress"),
          "On AKS, cert-manager issues and renews the facade certificate on the ingress controller. Nothing to manage here.")));
        return;
      }
      if (!s) {
        fill(status, h("div", { class: "alert warning" }, icon("warning", 18), h("div", null, h("div", { class: "title" }, "No facade certificate"),
          info.env === "dev" ? "Issue one below." : "Upload your corporate certificate below — nginx cannot serve HTTPS without it.")));
        return;
      }
      const warn = s.days_left < 30;
      const pct = Math.max(0, Math.min(100, (s.days_left / 397) * 100));
      fill(status, h("div", { class: "card" },
        h("div", { class: "cert-hero" }, h("span", { class: `ic ${warn ? "warn" : ""}` }, icon(warn ? "warning" : "shield", 22)),
          h("div", { style: { flex: 1, minWidth: 0 } },
            h("h3", null, s.sans[0] || s.subject),
            h("div", { class: "caption" }, info.issued_by_appliance_ca ? "Issued by this appliance's local CA" : `Issued by ${s.issuer}`),
            h("div", { style: { margin: "14px 0 6px", maxWidth: "420px" } },
              h("div", { class: "row between caption" }, h("span", null, s.days_left > 0 ? `Expires in ${s.days_left} days` : "Expired"), h("span", null, ctx.ui.fmtTime(s.not_after))),
              h("div", { class: `meter ${warn ? "warn" : ""}`, style: { marginTop: "6px" } }, h("span", { style: { width: `${pct}%` } }))))),
        h("hr", { class: "divider" }),
        h("dl", { class: "kv" },
          h("dt", null, "Hostnames"), h("dd", null, h("div", { class: "chips" }, s.sans.map((n) => h("span", { class: "chip outline" }, n)))),
          h("dt", null, "Subject"), h("dd", null, h("code", null, s.subject)),
          h("dt", null, "Valid from"), h("dd", null, ctx.ui.fmtTime(s.not_before)),
          h("dt", null, "SHA-256"), h("dd", null, h("code", { style: { fontSize: "11.5px" } }, s.sha256.match(/.{2}/g).join(":"))),
          info.chain.length ? h("dt", null, "Chain") : null, info.chain.length ? h("dd", null, info.chain.map((c) => h("div", { class: "caption" }, c.subject))) : null)));
    }

    function renderTrust() {
      if (!info.ca || info.env !== "dev") { fill(trust, ); return; }
      const host = new URL(origin).host;
      const OS = {
        macOS: [`curl -fsSLk ${caUrl} -o appliance-ca.pem`, "sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain appliance-ca.pem"],
        Windows: [`curl.exe -fsSLk ${caUrl} -o appliance-ca.pem`, "certutil -addstore -f Root appliance-ca.pem   # in an elevated PowerShell"],
        Linux: [`curl -fsSLk ${caUrl} -o appliance-ca.crt`, "sudo cp appliance-ca.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates"],
      };
      let os = /Mac/.test(navigator.platform) ? "macOS" : /Win/.test(navigator.platform) ? "Windows" : "Linux";
      const seg = h("div", { class: "segmented", role: "tablist" });
      const body = h("div", { style: { marginTop: "12px" } });
      const paint = () => {
        fill(seg, ...Object.keys(OS).map((k) => h("button", { role: "tab", "aria-selected": String(os === k), class: os === k ? "active" : "", onclick: () => { os = k; paint(); } }, k)));
        fill(body, cmd(OS[os].join("\n")),
          h("div", { class: "caption", style: { marginTop: "8px" } }, os === "Linux"
            ? "Fedora/RHEL: sudo cp appliance-ca.crt /etc/pki/ca-trust/source/anchors/ && sudo update-ca-trust. "
            : "", "Then quit and reopen your browser. Firefox keeps its own store: enable security.enterprise_roots.enabled in about:config."));
      };
      paint();
      fill(trust, h("div", { class: "card" },
        h("div", { class: "card-head" }, h("div", null, h("h3", null, "Trust the appliance CA on this computer"),
          h("div", { class: "caption" }, `Do this once per computer — the browser stops warning about https://${host}`)),
          h("a", { class: "btn secondary", href: "/studio/ca.pem", download: "appliance-ca.pem" }, icon("download", 16), "Download CA")),
        h("div", { class: "alert info", style: { marginBottom: "14px" } }, icon("info", 18), h("div", null, h("div", { class: "title" }, "Why bother?"),
          "Clicking through the warning works for plain pages, but browsers refuse to run service workers and webviews on an untrusted certificate. VS Code in the browser needs both — trusting the CA makes VS Code, notebooks and every other tool work properly.")),
        seg, body));
    }

    function renderReissue() {
      if (info.env !== "dev" || info.termination === "ingress") { fill(reissue, ); return; }
      const names = new Set(info.server ? info.server.sans : info.default_hostnames);
      const box = h("div", { class: "host-input" });
      const input = h("input", { placeholder: "Add hostname or IP and press Enter", "aria-label": "Add hostname" });
      const paint = () => fill(box, ...[...names].map((n) => h("span", { class: "chip primary" }, n,
        h("button", { "aria-label": `Remove ${n}`, onclick: () => { names.delete(n); paint(); } }, icon("close", 12)))), input);
      input.onkeydown = (e) => {
        if ((e.key === "Enter" || e.key === "," || e.key === " ") && input.value.trim()) { e.preventDefault(); names.add(input.value.trim().toLowerCase()); input.value = ""; paint(); }
        else if (e.key === "Backspace" && !input.value && names.size) { names.delete([...names].pop()); paint(); }
      };
      box.onclick = () => input.focus();
      paint();
      const btn = h("button", { class: "btn primary" }, icon("refresh", 16), "Re-issue certificate");
      btn.onclick = async () => {
        if (input.value.trim()) { names.add(input.value.trim().toLowerCase()); input.value = ""; paint(); }
        if (!names.size) return ctx.toast("Add at least one hostname", "error");
        if (!(await ctx.confirm({ title: "Re-issue the facade certificate?", body: `nginx reloads with a new certificate for ${[...names].join(", ")}. Open tabs may need a refresh.`, okText: "Re-issue" }))) return;
        try {
          const r = await ctx.ui.busy(btn, () => ctx.api("/studio/api/certs/reissue", { body: { hostnames: [...names] } }));
          info = { ...info, ...r };
          ctx.toast(`Certificate re-issued · ${r.reload}`, r.reload.startsWith("nginx reloaded") ? "success" : "info");
          renderAll();
        } catch (e) { if (e.status) ctx.toast(e.message, "error"); }
      };
      fill(reissue, h("div", { class: "card" },
        h("div", { class: "card-head" }, h("div", null, h("h3", null, "Hostnames"),
          h("div", { class: "caption" }, "Add every name people use to reach this appliance (e.g. its LAN IP or DNS name)."))),
        box, h("div", { class: "row", style: { marginTop: "14px", justifyContent: "flex-end" } }, btn)));
    }

    function renderUpload() {
      if (info.termination === "ingress") { fill(upload, ); return; }
      let cert = null, key = null;
      const label = (f, what) => (f ? `${what}: ${f.name}` : `Drop the ${what.toLowerCase()} (PEM) here or click`);
      const dzCert = ctx.ui.dropzone({ multiple: false, label: label(null, "Certificate chain"), onFiles: (fs) => { cert = fs[0]; dzCert.querySelector("div").textContent = label(cert, "Certificate chain"); } });
      const dzKey = ctx.ui.dropzone({ multiple: false, label: label(null, "Private key"), onFiles: (fs) => { key = fs[0]; dzKey.querySelector("div").textContent = label(key, "Private key"); } });
      const btn = h("button", { class: "btn primary" }, icon("upload", 16), "Validate and install");
      btn.onclick = async () => {
        if (!cert || !key) return ctx.toast("Choose both the certificate chain and the private key", "error");
        const fd = new FormData();
        fd.append("cert", cert); fd.append("key", key);
        try {
          const r = await ctx.ui.busy(btn, () => ctx.api("/studio/api/certs/upload", { body: fd }));
          info = { ...info, ...r };
          (r.warnings || []).forEach((w) => ctx.toast(w, "info"));
          ctx.toast(`Certificate installed · ${r.reload}`, "success");
          renderAll();
        } catch (e) { if (e.status) ctx.toast(e.message, "error"); }
      };
      const inner = h("div", null,
        h("p", { class: "muted" }, "Leaf certificate first, then intermediates, in one PEM file; plus its unencrypted private key. Studio checks the key matches, the chain links up and the dates are valid before nginx reloads."),
        h("div", { class: "grid cols-2", style: { marginTop: "12px" } }, dzCert, dzKey),
        h("div", { class: "row", style: { marginTop: "14px", justifyContent: "flex-end" } }, btn));
      fill(upload, info.env === "dev"
        ? h("details", { class: "section" }, h("summary", { class: "eyebrow", style: { cursor: "pointer" } }, "Use your own certificate instead"), h("div", { class: "card", style: { marginTop: "10px" } }, inner))
        : h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", null, "Install the corporate certificate")), inner));
    }

    function renderAll() { renderStatus(); renderTrust(); renderReissue(); renderUpload(); }
    renderAll();
  },
});
