import test from "node:test";
import assert from "node:assert/strict";
import { parseInline, parseMarkdown, safeUrl } from "../../frontend/js/markdown.js";

const textOf = (nodes) => nodes.map((n) => (n.t === "text" || n.t === "code" ? n.v : textOf(n.children ?? []))).join("");
const kinds = (nodes) => nodes.map((n) => n.t);

// The exact shape of the real Gemini reply that displayed "**Student Name:**" literally (fictional student data).
const REAL_REPLY = `Here is your current fee status:

* **Student Name:** Rohan Mehta (ID: STU003)
* **Total Fees:** ₹4,50,000
* **Amount Paid:** ₹1,50,000
* **Amount Due:** ₹3,00,000
* **Due Date:** October 1, 2026
* **Status:** Overdue`;

test("the real Gemini fee reply renders as a paragraph plus a bullet list with bold labels", () => {
  const blocks = parseMarkdown(REAL_REPLY);
  assert.deepEqual(kinds(blocks), ["paragraph", "list"]);
  const list = blocks[1];
  assert.equal(list.ordered, false);
  assert.equal(list.items.length, 6);
  const first = list.items[0].inline;
  assert.deepEqual(kinds(first), ["strong", "text"]);
  assert.equal(textOf(first[0].children), "Student Name:");
  assert.equal(first[1].v, " Rohan Mehta (ID: STU003)");
  const all = list.items.map((it) => textOf(it.inline)).join("\n");
  assert.ok(!all.includes("**"), "no literal ** markers may remain");
  assert.match(all, /Amount Due: ₹3,00,000/);
  assert.match(all, /Status: Overdue/);
});

test("inline formatting", () => {
  assert.deepEqual(parseInline("a **b** c"), [{ t: "text", v: "a " }, { t: "strong", children: [{ t: "text", v: "b" }] }, { t: "text", v: " c" }]);
  assert.deepEqual(kinds(parseInline("*it* and _it2_ and __bold__")), ["em", "text", "em", "text", "strong"]);
  assert.deepEqual(parseInline("use `check_fee_status` now"), [{ t: "text", v: "use " }, { t: "code", v: "check_fee_status" }, { t: "text", v: " now" }]);
  assert.equal(textOf(parseInline("**bold *and italic***")), "bold and italic");
  assert.deepEqual(kinds(parseInline("**`code` inside bold**")), ["strong"]);
  assert.equal(parseInline("`**not bold**`")[0].t, "code");                   // no markdown inside code
});

test("things that look like markers but are not stay literal, character for character", () => {
  for (const literal of [
    "student_handbook.md#s2-1", "see [student_handbook.md#s2-1] and check_fee_status", "snake_case_name", "2 * 3 * 4 = 24",
    "**unclosed bold", "a * b", "5*6*7", "price is $5 * 2", "trailing *", "** **", "****", "my_var_name and other_var",
  ]) {
    assert.equal(textOf(parseInline(literal)), literal, literal);
    assert.ok(parseInline(literal).every((n) => n.t === "text"), `no formatting expected in: ${literal}`);
  }
  assert.deepEqual(parseInline("student_handbook.md#s2-1"), [{ t: "text", v: "student_handbook.md#s2-1" }]);
  assert.deepEqual(parseInline("check_fee_status and search_handbook"), [{ t: "text", v: "check_fee_status and search_handbook" }]);
  assert.deepEqual(parseInline("2 * 3 * 4"), [{ t: "text", v: "2 * 3 * 4" }]);
  assert.equal(textOf(parseInline("__init__ is special")), "init is special");  // __x__ at a word boundary IS bold in Markdown
});

test("links: only http, https and mailto become links", () => {
  const n = parseInline("see [the handbook](https://example.com/a?b=1) now");
  assert.deepEqual(kinds(n), ["text", "link", "text"]);
  assert.equal(n[1].href, "https://example.com/a?b=1");
  assert.equal(textOf(n[1].children), "the handbook");
  assert.equal(parseInline("[mail](mailto:help@example.com)")[0].href, "mailto:help@example.com");
  assert.equal(parseInline("[**bold link**](http://example.com)")[0].children[0].t, "strong");
});

test("dangerous link targets are NOT turned into links and stay visible as text", () => {
  for (const bad of ["javascript:alert(1)", "JaVaScRiPt:alert(1)", " javascript:alert(1)", "java\tscript:alert(1)", "java\nscript:alert(1)",
    "data:text/html,<script>alert(1)</script>", "vbscript:msgbox(1)", "file:///etc/passwd", "ftp://example.com/x", "//evil.example.com", "/relative/path",
    "#fragment", "blob:https://x", "about:blank", "", "   "]) {
    const nodes = parseInline(`[click](${bad})`);
    assert.ok(!nodes.some((x) => x.t === "link"), `must not link: ${JSON.stringify(bad)}`);
    assert.ok(textOf(nodes).startsWith("[click]"), `text kept for ${JSON.stringify(bad)}`);
  }
  assert.equal(safeUrl("https://example.com"), "https://example.com/");
  assert.equal(safeUrl("javascript:alert(1)"), null);
  assert.equal(safeUrl("HTTP://EXAMPLE.COM/x"), "http://example.com/x");
  assert.equal(safeUrl(undefined), null);
});

test("images are never rendered: the whole syntax stays literal text", () => {
  const nodes = parseInline("look ![logo](https://example.com/logo.png) here");
  assert.deepEqual(nodes, [{ t: "text", v: "look ![logo](https://example.com/logo.png) here" }]);
});

test("HTML and script text is only ever text (no html node type exists)", () => {
  const hostile = [
    "<script>alert(1)</script>", `<img src=x onerror="window.__xss=1">`, "<b onmouseover=alert(1)>x</b>", "<iframe src=//evil></iframe>",
    "<a href=\"javascript:alert(1)\">x</a>", "&lt;b&gt; &amp; entities", "<style>*{display:none}</style>", "</div></main><script>",
  ];
  const allowed = new Set(["paragraph", "heading", "list", "quote", "code", "pre", "rule", "text", "strong", "em", "link"]);
  const walk = (node) => {
    assert.ok(allowed.has(node.t), `unexpected node type ${node.t}`);
    for (const key of ["children", "blocks", "items", "lines"]) for (const c of node[key] ?? []) Array.isArray(c) ? c.forEach(walk) : walk(c.t ? c : { t: "paragraph", lines: [] });
  };
  for (const h of hostile) {
    const blocks = parseMarkdown(h);
    blocks.forEach(walk);
    const flat = JSON.stringify(blocks);
    assert.ok(!flat.includes('"t":"html"'));
  }
  // the literal characters survive as text
  assert.equal(textOf(parseMarkdown("<script>alert(1)</script>")[0].lines[0]), "<script>alert(1)</script>");
  assert.equal(textOf(parseMarkdown(`<img src=x onerror="window.__xss=1">`)[0].lines[0]), `<img src=x onerror="window.__xss=1">`);
});

test("lists: bullets, ordered (with start), nested, mixed markers", () => {
  const [ul] = parseMarkdown("- a\n- b\n+ c\n* d\n• e");
  assert.equal(ul.t, "list"); assert.equal(ul.items.length, 5);
  const [ol] = parseMarkdown("1. first\n2. second\n3) third");
  assert.equal(ol.ordered, true); assert.equal(ol.items.length, 3);
  const [ol5] = parseMarkdown("5. five\n6. six");
  assert.equal(ol5.start, 5);
  const [nested] = parseMarkdown("* parent\n    * child one\n    * child two\n* second parent");
  assert.equal(nested.items.length, 2);
  assert.equal(nested.items[0].children[0].items.length, 2);
  assert.equal(textOf(nested.items[0].children[0].items[1].inline), "child two");
});

test("very deep indentation is flattened (no runaway nesting)", () => {
  const lines = Array.from({ length: 300 }, (_, i) => `${" ".repeat(i + 1)}* item ${i}`).join("\n");
  let depth = 0;
  let node = parseMarkdown(lines)[0];
  while (node && node.items?.[node.items.length - 1]?.children?.length) { depth++; node = node.items[node.items.length - 1].children[0]; }
  assert.ok(depth <= 5, `nesting depth ${depth}`);
});

test("other blocks: headings, rules, quotes, code fences, tables", () => {
  assert.deepEqual(parseMarkdown("## Title here")[0], { t: "heading", level: 2, children: [{ t: "text", v: "Title here" }] });
  assert.equal(parseMarkdown("#not a heading")[0].t, "paragraph");
  assert.equal(parseMarkdown("text\n\n---\n\nmore")[1].t, "rule");
  assert.equal(parseMarkdown("> quoted *text*")[0].t, "quote");
  assert.deepEqual(parseMarkdown("```js\nconst a = 1 < 2;\n```")[0], { t: "code", v: "const a = 1 < 2;" });
  assert.deepEqual(parseMarkdown("```\nunterminated")[0], { t: "code", v: "unterminated" });
  const table = parseMarkdown("| a | b |\n|---|---|\n| 1 | 2 |")[0];
  assert.equal(table.t, "pre"); assert.match(table.v, /\| a \| b \|/);
  assert.equal(parseMarkdown("**not** a ```fence``` here")[0].t, "paragraph");
});

test("paragraphs keep single newlines as line breaks and split on blank lines; CRLF is handled", () => {
  const blocks = parseMarkdown("line one\nline two\n\nsecond paragraph");
  assert.deepEqual(blocks.map((b) => b.t), ["paragraph", "paragraph"]);
  assert.equal(blocks[0].lines.length, 2);
  assert.equal(parseMarkdown("a\r\nb\r\n\r\nc").length, 2);
});

test("a list directly after a paragraph line starts a list; text after a list starts a new paragraph", () => {
  assert.deepEqual(parseMarkdown("Intro:\n- a\n- b\nOutro").map((b) => b.t), ["paragraph", "list", "paragraph"]);
});

test("robustness: empty, non-string, huge and pathological input", () => {
  assert.deepEqual(parseMarkdown(""), []);
  assert.deepEqual(parseMarkdown(null), []);
  assert.deepEqual(parseMarkdown(undefined), []);
  assert.equal(parseMarkdown(42)[0].lines[0][0].v, "42");
  const huge = parseMarkdown("x".repeat(25000));
  assert.deepEqual(huge.map((b) => b.t), ["pre"]);                       // over the size cap: plain preformatted text
  const t0 = Date.now();
  parseMarkdown("[".repeat(15000));                                       // quadratic-scan attempt, within the cap
  parseMarkdown("*a ".repeat(6000));
  parseMarkdown("**".repeat(8000));
  parseMarkdown("[a](".repeat(4000));
  assert.ok(Date.now() - t0 < 5000, `pathological inputs took ${Date.now() - t0} ms`);
});

test("rendering a reply never loses text: plain text without markers is returned unchanged", () => {
  const plain = "Sorry, I could not complete that request. Please try again or contact the help desk.";
  assert.equal(textOf(parseMarkdown(plain)[0].lines[0]), plain);
  const handbook = "According to the handbook [student_handbook.md#s2-1, student_handbook.md#s2-2]: 2.1 If the payment portal fails...";
  assert.equal(textOf(parseMarkdown(handbook)[0].lines[0]), handbook);
});
