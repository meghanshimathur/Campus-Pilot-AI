// Minimal, SAFE Markdown for assistant replies (LLMs answer in Markdown: **bold**, bullet lists, `code`, links).
//
// Safety model: the output is built ONLY from DOM elements and text nodes (via dom.js `h`); input text is never
// parsed as HTML, so "<script>", "<img onerror=…>" and friends appear as literal text. Links are limited to
// http(s) and mailto (anything else, such as javascript:, is shown as plain text). Images are never loaded.
//
// Emphasis with * or _ only starts/ends at word boundaries (so 5*6*7 and snake_case stay literal).
// Supported: paragraphs (single newlines kept as line breaks), # headings, **bold**/__bold__, *italic*/_italic_,
// `inline code`, ``` fenced code ```, -, *, + and 1. lists (nested by indentation), > quotes, --- rules, [text](url).
// Anything else (tables, HTML, images, footnotes) is shown as plain text. No dependencies.
import { h } from "./dom.js";

const SAFE_PROTOCOLS = new Set(["http:", "https:", "mailto:"]);
const MAX_LIST_DEPTH = 5;      // deeper indentation is flattened into the deepest allowed level
const MAX_MD_CHARS = 20000;    // longer replies are shown as plain preformatted text (keeps parsing cheap and predictable)

export function safeUrl(raw) {
  try {
    const url = new URL(String(raw).trim());
    return SAFE_PROTOCOLS.has(url.protocol) ? url.href : null;
  } catch {
    return null; // relative URLs, garbage, or javascript: with odd spacing all end up here or fail the protocol test
  }
}

// ------------------------------------------------------------------ inline
// Returns a flat list of nodes: {t:"text"|"strong"|"em"|"code"|"link", ...}. Pure, so it is unit-testable.
export function parseInline(text) {
  const out = [];
  let buf = "";
  const flush = () => { if (buf) { out.push({ t: "text", v: buf }); buf = ""; } };
  const s = String(text ?? "");
  let i = 0;
  scan: while (i < s.length) {
    const ch = s[i];
    const prev = i > 0 ? s[i - 1] : "";

    if (ch === "`") { // `code` (no markdown inside)
      const end = s.indexOf("`", i + 1);
      if (end > i + 1) { flush(); out.push({ t: "code", v: s.slice(i + 1, end) }); i = end + 1; continue scan; }
    }

    if (ch === "!" && s[i + 1] === "[") { // images are never rendered: keep ![alt](url) as literal text
      const img = /^!\[[^\]]*\]\([^)]*\)/.exec(s.slice(i));
      const len = img ? img[0].length : 1;
      buf += s.slice(i, i + len); i += len; continue;
    }

    if (ch === "[") { // [text](url)
      const close = s.indexOf("]", i + 1);
      if (close > i && s[close + 1] === "(") {
        const paren = s.indexOf(")", close + 2);
        if (paren > close) {
          const label = s.slice(i + 1, close);
          const href = safeUrl(s.slice(close + 2, paren));
          if (href && label.trim()) { flush(); out.push({ t: "link", href, children: parseInline(label) }); i = paren + 1; continue scan; }
        }
      }
    }

    for (const marker of ["**", "__"]) { // **bold** / __bold__
      if (s.startsWith(marker, i)) {
        const boundaryOk = marker === "**" || !/\w/.test(prev);
        let end = s.indexOf(marker, i + 2);
        while (end > 0 && s[end + 2] === marker[0]) end++; // in a run like ***, the closing pair is the LAST two characters
        const inner = end > i + 2 ? s.slice(i + 2, end) : "";
        if (boundaryOk && inner && !/^\s/.test(inner) && !/\s$/.test(inner) && (marker === "**" || !/\w/.test(s[end + 2] ?? ""))) {
          flush(); out.push({ t: "strong", children: parseInline(inner) }); i = end + 2; continue scan;
        }
      }
    }

    if (ch === "*" || ch === "_") { // *italic* / _italic_ (underscores only at word boundaries: snake_case stays literal)
      const end = s.indexOf(ch, i + 1);
      const inner = end > i + 1 ? s.slice(i + 1, end) : "";
      // Both * and _ need word boundaries: "5*6*7" and snake_case are far more likely maths/identifiers than emphasis.
      const opens = !/\w/.test(prev);
      const closes = !/\w/.test(s[end + 1] ?? "");
      if (opens && closes && inner && !/^\s/.test(inner) && !/\s$/.test(inner) && !inner.includes("\n") && s[i + 1] !== ch) {
        flush(); out.push({ t: "em", children: parseInline(inner) }); i = end + 1; continue;
      }
    }

    buf += ch;
    i += 1;
  }
  flush();
  return out;
}

// ------------------------------------------------------------------ blocks
const RE = {
  fence: /^\s*```/,
  heading: /^(#{1,6})\s+(.*?)\s*#*\s*$/,
  ul: /^(\s*)[-*+•]\s+(.*)$/,
  ol: /^(\s*)(\d{1,3})[.)]\s+(.*)$/,
  quote: /^\s*>\s?(.*)$/,
  rule: /^\s*([-*_])(\s*\1){2,}\s*$/,
  tableRow: /^\s*\|.*\|\s*$/,
};

// Returns blocks: paragraph | heading | list | quote | code | rule | pre (tables and other unsupported syntax).
export function parseMarkdown(source) {
  const text = String(source ?? "");
  if (text.length > MAX_MD_CHARS) return [{ t: "pre", v: text }];
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const blocks = [];
  let i = 0;

  const isBlank = (l) => !l.trim();
  const startsBlock = (l) => RE.fence.test(l) || RE.heading.test(l) || RE.ul.test(l) || RE.ol.test(l) || RE.quote.test(l) || RE.rule.test(l) || RE.tableRow.test(l);

  while (i < lines.length) {
    const line = lines[i];
    if (isBlank(line)) { i++; continue; }

    if (RE.fence.test(line)) { // ``` fenced code (an unterminated fence runs to the end)
      const body = [];
      i++;
      while (i < lines.length && !RE.fence.test(lines[i])) body.push(lines[i++]);
      i++;
      blocks.push({ t: "code", v: body.join("\n") });
      continue;
    }
    let m;
    if ((m = RE.heading.exec(line))) { blocks.push({ t: "heading", level: m[1].length, children: parseInline(m[2]) }); i++; continue; }
    if (RE.rule.test(line)) { blocks.push({ t: "rule" }); i++; continue; }

    if (RE.ul.test(line) || RE.ol.test(line)) { // lists, nested by indentation
      const items = [];
      while (i < lines.length && !isBlank(lines[i]) && (RE.ul.test(lines[i]) || RE.ol.test(lines[i]))) {
        const u = RE.ul.exec(lines[i]);
        const o = u ? null : RE.ol.exec(lines[i]);
        const indent = (u ? u[1] : o[1]).replace(/\t/g, "    ").length;
        items.push({ indent, ordered: Boolean(o), start: o ? Number(o[2]) : 1, text: u ? u[2] : o[3] });
        i++;
      }
      blocks.push(buildList(items));
      continue;
    }

    if (RE.quote.test(line)) {
      const body = [];
      while (i < lines.length && RE.quote.test(lines[i])) body.push(RE.quote.exec(lines[i++])[1]);
      blocks.push({ t: "quote", blocks: parseMarkdown(body.join("\n")) });
      continue;
    }

    if (RE.tableRow.test(line)) { // tables are not supported: show them verbatim, monospaced
      const body = [];
      while (i < lines.length && RE.tableRow.test(lines[i])) body.push(lines[i++]);
      blocks.push({ t: "pre", v: body.join("\n") });
      continue;
    }

    const para = []; // paragraph: consecutive plain lines, kept as line breaks
    while (i < lines.length && !isBlank(lines[i]) && !(para.length && startsBlock(lines[i]))) para.push(lines[i++]);
    blocks.push({ t: "paragraph", lines: para.map((l) => parseInline(l.trim())) });
  }
  return blocks;
}

function buildList(items) {
  // Turn a flat list of {indent, ordered, text} into a nested tree using indentation.
  const root = { t: "list", ordered: items[0].ordered, start: items[0].start, items: [] };
  const stack = [{ indent: items[0].indent, list: root }];
  for (const it of items) {
    while (stack.length > 1 && it.indent < stack[stack.length - 1].indent) stack.pop();
    let top = stack[stack.length - 1];
    if (it.indent > top.indent && stack.length < MAX_LIST_DEPTH) { // deeper: start a nested list under the previous item
      const parentItem = top.list.items[top.list.items.length - 1];
      if (parentItem) {
        const nested = { t: "list", ordered: it.ordered, start: it.start, items: [] };
        parentItem.children.push(nested);
        top = { indent: it.indent, list: nested };
        stack.push(top);
      }
    }
    top.list.items.push({ inline: parseInline(it.text), children: [] });
  }
  return root;
}

// ------------------------------------------------------------------ DOM
function inlineNodes(nodes) {
  return nodes.map((n) => {
    switch (n.t) {
      case "text": return n.v;
      case "strong": return h("strong", {}, inlineNodes(n.children));
      case "em": return h("em", {}, inlineNodes(n.children));
      case "code": return h("code", {}, n.v);
      case "link": return h("a", { href: n.href, target: "_blank", rel: "noopener noreferrer" }, inlineNodes(n.children));
      default: return "";
    }
  });
}

function blockNode(b) {
  switch (b.t) {
    case "paragraph": {
      const kids = [];
      b.lines.forEach((l, idx) => { if (idx) kids.push(h("br")); kids.push(...inlineNodes(l)); });
      return h("p", {}, kids);
    }
    case "heading": return h("p", { class: `md-heading md-h${Math.min(b.level, 4)}` }, inlineNodes(b.children));
    case "list": {
      const attrs = b.ordered && b.start > 1 ? { start: b.start } : {};
      return h(b.ordered ? "ol" : "ul", attrs, b.items.map((it) => h("li", {}, inlineNodes(it.inline), it.children.map(blockNode))));
    }
    case "quote": return h("blockquote", {}, b.blocks.map(blockNode));
    case "code": return h("pre", { class: "md-code" }, h("code", {}, b.v));
    case "pre": return h("pre", { class: "md-code" }, b.v);
    case "rule": return h("hr");
    default: return "";
  }
}

// Builds a <div class="md"> for an assistant reply.
export function renderMarkdown(source) {
  return h("div", { class: "md" }, parseMarkdown(source).map(blockNode));
}
