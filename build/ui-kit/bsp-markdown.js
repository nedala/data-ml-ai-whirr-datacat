/* DataPlatform Appliance UI kit — tiny, safe Markdown → HTML for LLM answers.
   window.BSP.md(text) → HTML string. Everything is HTML-escaped FIRST, then a
   small, predictable subset is formatted: headings, **bold**, *italic*,
   `code`, fenced code blocks, bullet / numbered lists (one nesting level),
   > quotes, horizontal rules, pipe tables and http(s) links. No raw HTML ever
   passes through, so model output cannot inject markup or scripts. */
(function () {
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  function inline(s) {
    // s is already escaped
    const codes = [];
    s = s.replace(/`([^`]+)`/g, (_, c) => `\u0000${codes.push(c) - 1}\u0000`);
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/__([^_]+)__/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[+i]}</code>`);
  }

  function table(rows) {
    const cells = (r) => r.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
    const head = cells(rows[0]);
    const body = rows.slice(2).map(cells);
    return `<div class="md-table"><table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>` +
      body.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("") + "</tbody></table></div>";
  }

  function md(text) {
    const lines = esc(text || "").replace(/\r\n?/g, "\n").split("\n");
    const out = [];
    let para = [], list = null;
    const flushPara = () => { if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; } };
    const flushList = () => {
      if (!list) return;
      const render = (l) => `<${l.tag}>${l.items.map((it) => `<li>${inline(it.text)}${it.sub ? render(it.sub) : ""}</li>`).join("")}</${l.tag}>`;
      out.push(render(list)); list = null;
    };
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const fence = line.match(/^\s*```(\w*)\s*$/);
      if (fence) {
        flushPara(); flushList();
        const code = [];
        while (++i < lines.length && !/^\s*```\s*$/.test(lines[i])) code.push(lines[i]);
        out.push(`<pre class="md-code"><code>${code.join("\n")}</code></pre>`);
        continue;
      }
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1])) {
        flushPara(); flushList();
        const rows = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(lines[i++].trim());
        i--; out.push(table(rows)); continue;
      }
      const h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) { flushPara(); flushList(); const n = Math.min(h[1].length + 2, 6); out.push(`<h${n}>${inline(h[2])}</h${n}>`); continue; }
      if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { flushPara(); flushList(); out.push("<hr>"); continue; }
      const q = line.match(/^\s*&gt;\s?(.*)$/);
      if (q) { flushPara(); flushList(); out.push(`<blockquote>${inline(q[1])}</blockquote>`); continue; }
      const li = line.match(/^(\s*)([*+-]|\d+[.)])\s+(.*)$/);
      if (li) {
        flushPara();
        const tag = /\d/.test(li[2]) ? "ol" : "ul";
        const nested = li[1].length >= 2 && list && list.items.length;
        if (nested) {
          const parent = list.items[list.items.length - 1];
          parent.sub = parent.sub || { tag, items: [] };
          parent.sub.items.push({ text: li[3] });
        } else {
          if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
          list.items.push({ text: li[3] });
        }
        continue;
      }
      if (!line.trim()) { flushPara(); flushList(); continue; }
      if (list && /^\s{2,}\S/.test(line)) { list.items[list.items.length - 1].text += " " + line.trim(); continue; }
      flushList(); para.push(line.trim());
    }
    flushPara(); flushList();
    return out.join("");
  }

  window.BSP = Object.assign(window.BSP || {}, { md });
})();
