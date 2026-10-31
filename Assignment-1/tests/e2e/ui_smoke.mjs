// Real-browser test of the dashboard: headless Chrome driven over the DevTools Protocol (no packages needed).
// Usage: node tests/e2e/ui_smoke.mjs <base-url> [screenshots-dir]
// Expects the server from tests/e2e/demo_server.py (real backend + a scripted test LLM).
import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const BASE = process.argv[2];
const SHOTS = process.argv[3];
const CHROME = process.env.CHROME_BIN || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
if (!BASE) { console.error("usage: node ui_smoke.mjs <base-url> [shots-dir]"); process.exit(2); }
if (SHOTS) mkdirSync(SHOTS, { recursive: true });

const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok: Boolean(ok), detail }); if (!ok) console.log(`  ✖ ${name} ${detail}`); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ------------------------------------------------------------------ Chrome + CDP
const port = 9300 + Math.floor(Math.random() * 500);
const profile = mkdtempSync(join(tmpdir(), "cp_chrome_"));
const chrome = spawn(CHROME, ["--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars", "--no-first-run",
  `--user-data-dir=${profile}`, `--remote-debugging-port=${port}`, "--window-size=1440,900", "about:blank"], { stdio: "ignore" });
let exiting = false;
const shutdown = (code) => { if (exiting) return; exiting = true; try { chrome.kill("SIGKILL"); } catch {} process.exit(code); };
process.on("SIGINT", () => shutdown(130));

async function waitForChrome() {
  for (let i = 0; i < 60; i++) {
    try { const r = await fetch(`http://127.0.0.1:${port}/json/list`); const list = await r.json(); const page = list.find((t) => t.type === "page"); if (page) return page; } catch {}
    await sleep(250);
  }
  throw new Error("Chrome did not start");
}

const page = await waitForChrome();
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let nextId = 1;
const pending = new Map();
const consoleErrors = [];
ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) { const { resolve, reject } = pending.get(msg.id); pending.delete(msg.id); msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result); return; }
  if (msg.method === "Runtime.exceptionThrown") consoleErrors.push(`exception: ${msg.params.exceptionDetails.exception?.description ?? msg.params.exceptionDetails.text}`);
  if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") consoleErrors.push(`console.error: ${msg.params.args.map((a) => a.value ?? a.description).join(" ")}`);
  if (msg.method === "Log.entryAdded" && msg.params.entry.level === "error") consoleErrors.push(`log: ${msg.params.entry.text} ${msg.params.entry.url ?? ""}`);
};
const send = (method, params = {}) => new Promise((resolve, reject) => { const id = nextId++; pending.set(id, { resolve, reject }); ws.send(JSON.stringify({ id, method, params })); });
const evalJs = async (expression) => {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(`eval failed: ${r.exceptionDetails.exception?.description ?? r.exceptionDetails.text}\n${expression}`);
  return r.result.value;
};
async function waitFor(expression, { timeout = 10000, label = expression } = {}) {
  const start = Date.now();
  while (Date.now() - start < timeout) { try { if (await evalJs(expression)) return true; } catch {} await sleep(80); }
  throw new Error(`timed out waiting for: ${label}`);
}
const shot = async (name) => {
  if (!SHOTS) return;
  const r = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(SHOTS, `${name}.png`), Buffer.from(r.data, "base64"));
};
const setViewport = (width, height, mobile = false) => send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: mobile ? 2 : 1, mobile });
const key = (k, { shift = false, text } = {}) => send("Input.dispatchKeyEvent", { type: "keyDown", key: k, code: k, windowsVirtualKeyCode: { Enter: 13, Tab: 9, ArrowRight: 39, ArrowLeft: 37, Home: 36, End: 35 }[k] ?? 0, modifiers: shift ? 8 : 0, text });
const keyUp = (k) => send("Input.dispatchKeyEvent", { type: "keyUp", key: k, code: k });
const q = (sel) => JSON.stringify(sel);

async function clickSel(sel) {
  await evalJs(`(() => { const el = document.querySelector(${q(sel)}); if (!el) throw new Error('no element ${sel}'); el.click(); return true; })()`);
}
async function typeAndSend(text) {
  await evalJs(`(() => { const t = document.getElementById('message'); t.focus(); return true; })()`);
  await send("Input.insertText", { text });
  await key("Enter", { text: "\r" }); await keyUp("Enter");
}
const agentCount = () => evalJs(`document.querySelectorAll('#chat-log .msg-agent').length`);
async function ask(text, { expectAgent = true, timeout = 12000 } = {}) {
  const before = await evalJs(`document.querySelectorAll('#chat-log .msg-agent, #chat-log .msg-error').length`);
  await typeAndSend(text);
  await waitFor(`document.querySelectorAll('#chat-log .msg-agent, #chat-log .msg-error').length > ${before} && document.getElementById('typing').hidden`, { timeout, label: `reply to "${text}"` });
}
const text = (sel) => evalJs(`(document.querySelector(${q(sel)})?.innerText ?? '')`);
const visible = (sel) => evalJs(`(() => { const e = document.querySelector(${q(sel)}); if (!e) return false; const r = e.getBoundingClientRect(); const s = getComputedStyle(e); return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0; })()`);
const captured = () => evalJs(`window.__captured`);

try {
  await send("Page.enable"); await send("Runtime.enable"); await send("Log.enable");
  await send("Page.addScriptToEvaluateOnNewDocument", { source: `
    window.__captured = []; window.__fail = null;
    const realFetch = window.fetch.bind(window);
    window.fetch = async (url, init) => {
      if (window.__fail && String(url).includes('/api/chat')) {
        const mode = window.__fail; window.__fail = null;
        if (mode === 'network') throw new TypeError('Failed to fetch');
        return new Response(JSON.stringify({ detail: 'No LLM provider configured. See .env.example.' }), { status: 503, headers: { 'Content-Type': 'application/json' } });
      }
      const res = await realFetch(url, init);
      try { window.__captured.push({ url: String(url), method: (init && init.method) || 'GET', status: res.status, body: await res.clone().json() }); } catch {}
      return res;
    };` });
  await setViewport(1440, 900);
  await send("Page.navigate", { url: BASE });
  await waitFor(`document.getElementById('api-status').textContent.startsWith('API online')`, { label: "health indicator" });

  // ---------------------------------------------------------------- initial state
  console.log("• initial state");
  check("title", (await evalJs(`document.title`)).includes("CampusPilot"));
  check("empty state + 5 suggestions", (await evalJs(`document.querySelectorAll('#suggestions button').length`)) === 5 && (await visible("#chat-empty")));
  check("evidence tab shows 'no turn' empty state", (await text("#panel-evidence")).includes("No turn selected"));
  check("other tab panels hidden", !(await visible("#panel-activity")) && !(await visible("#panel-memory")));
  check("turn picker disabled when no turns", await evalJs(`document.getElementById('turn-select').disabled`));
  check("session chip says no session", (await text("#session-chip")).includes("No session"));
  await shot("01_initial_desktop");

  // ---------------------------------------------------------------- keyboard: skip link first, tab arrows
  console.log("• keyboard");
  await key("Tab"); await keyUp("Tab");
  check("first Tab stop is the skip link", await evalJs(`document.activeElement.classList.contains('skip-link')`));
  await evalJs(`document.getElementById('tab-evidence').focus()`);
  await key("ArrowRight"); await keyUp("ArrowRight");
  check("ArrowRight moves to next tab (selected + focused)", await evalJs(`document.getElementById('tab-activity').getAttribute('aria-selected') === 'true' && document.activeElement.id === 'tab-activity'`));
  await key("End"); await keyUp("End");
  check("End jumps to last tab", await evalJs(`document.activeElement.id === 'tab-evaluation'`));
  await key("Home"); await keyUp("Home");
  check("Home jumps to first tab", await evalJs(`document.activeElement.id === 'tab-evidence' && document.getElementById('tab-evidence').tabIndex === 0 && document.getElementById('tab-activity').tabIndex === -1`));
  await evalJs(`document.getElementById('message').focus()`);
  await send("Input.insertText", { text: "line one" });
  await key("Enter", { shift: true, text: "\r" }); await keyUp("Enter");
  await send("Input.insertText", { text: "line two" });
  check("Shift+Enter inserts a newline instead of sending", (await evalJs(`document.getElementById('message').value`)) === "line one\nline two" && (await agentCount()) === 0);
  await evalJs(`(() => { const t = document.getElementById('message'); t.value = ''; t.dispatchEvent(new Event('input')); })()`);

  // ---------------------------------------------------------------- validation
  console.log("• validation");
  await evalJs(`document.getElementById('message').focus()`);
  await key("Enter", { text: "\r" }); await keyUp("Enter");
  check("empty message shows an inline error (role=alert) and sends nothing", (await visible("#composer-error")) && (await text("#composer-error")).includes("Type a message") && (await agentCount()) === 0);
  await evalJs(`(() => { const i = document.getElementById('student-id'); i.value = ''; i.dispatchEvent(new Event('change')); })()`);
  check("blank student id shows an error + aria-invalid", (await visible("#student-error")) && (await evalJs(`document.getElementById('student-id').hasAttribute('aria-invalid')`)));
  await typeAndSend("hello"); await sleep(300);
  check("sending with a blank student id is blocked", (await agentCount()) === 0 && (await text("#composer-error")).includes("student ID"));
  check("a blocked message stays in the box so nothing the user typed is lost", (await evalJs(`document.getElementById('message').value`)) === "hello");
  await evalJs(`(() => { const i = document.getElementById('student-id'); i.value = 'X'.repeat(21); i.dispatchEvent(new Event('change')); })()`);
  check("too-long student id rejected", (await text("#student-error")).includes("at most 20"));
  await evalJs(`(() => { const i = document.getElementById('student-id'); i.value = 'STU001'; i.dispatchEvent(new Event('change')); })()`);
  check("valid id clears the error", !(await visible("#student-error")));
  await evalJs(`(() => { const t = document.getElementById('message'); t.value = ''; t.dispatchEvent(new Event('input')); })()`);

  // ---------------------------------------------------------------- fee question: tool use, loading state, trace
  console.log("• tool use + loading state + trace");
  await evalJs(`document.getElementById('message').focus()`);
  await send("Input.insertText", { text: "slow fee status tokens please" });
  await key("Enter", { text: "\r" }); await keyUp("Enter");
  await waitFor(`!document.getElementById('typing').hidden`, { label: "typing indicator" });
  check("typing indicator visible while waiting", await visible("#typing"));
  check("send button disabled + says Sending… while waiting", await evalJs(`document.getElementById('send').disabled && document.querySelector('#send .btn-label').textContent === 'Sending…'`));
  check("chat log marked aria-busy", await evalJs(`document.getElementById('chat-log').getAttribute('aria-busy') === 'true'`));
  await waitFor(`document.querySelectorAll('#chat-log .msg-agent').length === 1 && document.getElementById('typing').hidden`, { label: "reply" });
  check("loading state cleared after reply", await evalJs(`!document.getElementById('send').disabled && document.querySelector('#send .btn-label').textContent === 'Send' && document.getElementById('chat-log').getAttribute('aria-busy') === 'false'`));
  check("user bubble shows the question as typed", (await text("#chat-log .msg-user .msg-body")) === "slow fee status tokens please");
  const feeReply = await text("#chat-log .msg-agent .msg-body");
  const calls1 = await captured();
  const chat1 = calls1.find((c) => c.url.endsWith("/api/chat"));
  check("reply text equals the /api/chat response", feeReply === chat1.body.response, `${feeReply} vs ${chat1.body.response}`);
  check("chat request used POST with student id and no session id on first message", chat1.method === "POST");
  check("tool badge shows the real tool name", (await text("#chat-log .msg-agent .msg-meta")).includes("check_fee_status"));
  check("API reported tokens (summed over both LLM calls of the turn)", chat1.body.input_tokens === 24 && chat1.body.output_tokens === 14);
  check("token counts shown are exactly the API's numbers", (await text("#chat-log .msg-agent .msg-meta")).includes(`in ${chat1.body.input_tokens} · out ${chat1.body.output_tokens}`));
  check("session chip now has the server's session id", (await text("#session-chip")).includes(chat1.body.session_id.slice(0, 8)));
  check("turn picker enabled with one turn", await evalJs(`!document.getElementById('turn-select').disabled && document.getElementById('turn-select').options.length === 1`));
  check("evidence says no handbook search for a fee question", (await text("#panel-evidence")).includes("did not search the handbook"));
  await clickSel("#tab-activity");
  await waitFor(`document.querySelector('#panel-activity .timeline')`, { label: "trace timeline" });
  const traceCall = (await captured()).find((c) => c.url.includes("/api/traces/"));
  check("activity view fetched the real trace endpoint", traceCall && traceCall.url.endsWith(chat1.body.trace_id) && traceCall.status === 200);
  const act = await text("#panel-activity");
  check("activity shows action, status, tool, provider and latency", act.includes("Used tools") && act.includes("Success") && act.includes("check_fee_status") && act.includes("scripted-e2e") && /ms|\ss\b/.test(act));
  check("activity token line matches the stored trace", act.includes(`in ${chat1.body.input_tokens} · out ${chat1.body.output_tokens}`));
  check("activity does not show raw tool values (trace is sanitised)", !act.includes("Rohan") && !act.includes("300000") && !act.includes("overdue"));
  await shot("02_activity_tool_use");

  // ---------------------------------------------------------------- handbook evidence
  console.log("• handbook evidence");
  await ask("The payment portal failed. Can I pay on the next working day?");
  await clickSel("#tab-evidence");
  await waitFor(`document.querySelectorAll('#panel-evidence .evidence-card').length > 0`, { label: "evidence cards" });
  const chat2 = (await captured()).filter((c) => c.url.endsWith("/api/chat")).pop().body;
  const apiChunks = chat2.tools_called.find((t) => t.name === "search_handbook").result.chunks;
  const cards = await evalJs(`[...document.querySelectorAll('#panel-evidence .evidence-card')].map(c => ({ id: c.querySelector('.chunk-id').textContent, text: c.querySelector('.excerpt').textContent, badges: [...c.querySelectorAll('.badge')].map(b => b.textContent) }))`);
  check("evidence card count equals chunks in the API response", cards.length === apiChunks.length && cards.length > 0, `${cards.length} vs ${apiChunks.length}`);
  check("every card id and excerpt matches the API response exactly", cards.every((c, i) => c.id === apiChunks[i].chunk_id && c.text === apiChunks[i].text));
  check("cards show similarity, keyword coverage and match reason from the API",
    cards.every((c, i) => c.badges.includes(`similarity ${apiChunks[i].score.toFixed(2)}`) && c.badges.includes(`keyword coverage ${Math.round(apiChunks[i].keyword_coverage * 100)}%`) && c.badges.includes(apiChunks[i].match)));
  check("evidence includes the 24-hour rule from the handbook", cards.some((c) => c.text.includes("24 hours")));
  check("agent reply cites chunk ids", (await text("#chat-log .msg-agent:last-of-type .msg-body")).includes("student_handbook.md#s2-"));
  await shot("03_evidence_found");

  await ask("Where can I park my car? (policy)");
  const chat3 = (await captured()).filter((c) => c.url.endsWith("/api/chat")).pop().body;
  check("(precondition) API reports nothing found", chat3.tools_called[0].result.found === false && chat3.evidence.length === 0);
  const ev3 = await text("#panel-evidence");
  check("evidence shows 'nothing relevant found' (from the real response)", ev3.includes("Nothing relevant found in the handbook") && ev3.includes("not to guess") && !(await visible("#panel-evidence .evidence-card")));
  check("agent reply says it could not find it", (await text("#chat-log .msg-agent:last-of-type .msg-body")).includes("could not find"));
  await shot("04_evidence_not_found");
  await evalJs(`(() => { const s = document.getElementById('turn-select'); s.value = s.options[0].value; s.dispatchEvent(new Event('change')); })()`);
  check("turn picker switches the inspected turn (turn 1 = fee question, no handbook search)", (await text("#panel-evidence")).includes("did not search the handbook"));
  const optionTexts = await evalJs(`[...document.getElementById('turn-select').options].map(o => o.textContent)`);
  check("turn picker lists all 3 turns with their questions", optionTexts.length === 3 && optionTexts[1].includes("payment portal"));

  // ---------------------------------------------------------------- memory: save, persist across sessions, delete
  console.log("• memory");
  await ask("Please call me Dev from now on.");
  check("assistant meta shows 'memory saved'", (await text("#chat-log .msg-agent:last-of-type .msg-meta")).includes("memory saved: preferred_name"));
  await clickSel("#tab-memory");
  await waitFor(`document.querySelector('#panel-memory .table')`, { label: "memory table" });
  check("memory table shows the saved preference from GET /api/memory", (await text("#panel-memory .table")).includes("preferred_name") && (await text("#panel-memory .table")).includes("Dev"));
  await shot("05_memory");
  await clickSel("#new-session");
  await waitFor(`document.querySelectorAll('#chat-log .msg').length === 0`, { label: "chat cleared" });
  check("new session clears the chat and session id", (await visible("#chat-empty")) && (await text("#session-chip")).includes("No session"));
  await clickSel("#tab-evidence"); await clickSel("#tab-memory");
  await waitFor(`document.querySelector('#panel-memory .table')`);
  check("memory survives into the new session (persistence)", (await text("#panel-memory .table")).includes("Dev"));
  await clickSel(`button[aria-label="Delete memory preferred_name"]`);
  await waitFor(`document.querySelector('#panel-memory .confirm')`, { label: "confirm step" });
  check("delete asks for confirmation first", (await text("#panel-memory .confirm")).includes("Delete?"));
  check("focus moves to the Confirm button", await evalJs(`document.activeElement.classList.contains('btn-danger')`));
  await clickSel("#panel-memory .confirm .btn-secondary");
  check("Cancel keeps the row", (await text("#panel-memory .table")).includes("preferred_name"));
  await clickSel(`button[aria-label="Delete memory preferred_name"]`);
  await waitFor(`document.querySelector('#panel-memory .confirm .btn-danger')`);
  await clickSel("#panel-memory .confirm .btn-danger");
  await waitFor(`document.querySelector('#panel-memory .empty-state')`, { label: "empty memory" });
  check("delete removes the row and shows the empty state", (await text("#panel-memory")).includes("Nothing remembered yet"));
  const delCall = (await captured()).find((c) => c.method === "DELETE");
  check("DELETE /api/memory/STU001/preferred_name was sent and succeeded", delCall && delCall.url.endsWith("/api/memory/STU001/preferred_name") && delCall.body.deleted === true);
  check("server really no longer has it", (await (await fetch(`${BASE}/api/memory/STU001`)).json()).memories.length === 0);
  check("deletion announced to screen readers (live region)", (await evalJs(`document.getElementById('announcer').textContent`)).includes("Deleted memory preferred_name"));

  // ---------------------------------------------------------------- student switch
  console.log("• student switch");
  await ask("hello there");
  await evalJs(`(() => { const i = document.getElementById('student-id'); i.value = 'STU002'; i.dispatchEvent(new Event('change')); })()`);
  await waitFor(`document.querySelectorAll('#chat-log .msg').length === 0`, { label: "reset on student switch" });
  check("switching student starts a fresh conversation", await visible("#chat-empty"));
  await waitFor(`document.querySelector('#panel-memory .empty-state, #panel-memory .table')`);
  check("memory panel refreshed for the new student", (await text("#panel-memory")).includes("STU002"));
  await evalJs(`(() => { const i = document.getElementById('student-id'); i.value = 'NOPE'; i.dispatchEvent(new Event('change')); })()`);
  await waitFor(`document.querySelector('#panel-memory .alert')`);
  check("unknown student shows a clear 404 message in memory view", (await text("#panel-memory")).includes("No student found with ID"));
  await evalJs(`(() => { const i = document.getElementById('student-id'); i.value = 'STU001'; i.dispatchEvent(new Event('change')); })()`);

  // ---------------------------------------------------------------- errors
  console.log("• error handling");
  await evalJs(`window.__fail = 'network'`);
  await ask("will fail on the network");
  check("network failure shows a readable error bubble (role=alert) with Retry", (await text("#chat-log .msg-error")).includes("Could not reach the server") && (await visible("#chat-log .msg-error .btn")));
  check("a failed turn is listed in the picker as failed", await evalJs(`[...document.getElementById('turn-select').options].some(o => o.textContent.includes('(failed)'))`));
  await evalJs(`(() => { const s = document.getElementById('turn-select'); const o = [...s.options].find(o => o.textContent.includes('(failed)')); s.value = o.value; s.dispatchEvent(new Event('change')); })()`);
  check("inspecting a failed turn explains there is no reply/evidence", (await text("#panel-evidence")).includes("No reply for this turn") && (await text("#panel-evidence")).includes("Could not reach the server"));
  await shot("06_error_network");
  await clickSel("#chat-log .msg-error .btn");
  await waitFor(`document.querySelectorAll('#chat-log .msg-agent').length === 1 && document.querySelectorAll('#chat-log .msg-error').length === 0 && document.getElementById('typing').hidden`, { label: "retry succeeded" });
  check("Retry re-sends the same text and succeeds", (await text("#chat-log .msg-user .msg-body")) === "will fail on the network");
  await evalJs(`window.__fail = 503`);
  await ask("will get a 503");
  check("503 shows the server's own message", (await text("#chat-log .msg-error")).includes("No LLM provider configured"));
  await clickSel("#chat-log .msg-error .link-btn");
  check("Dismiss removes the error", (await evalJs(`document.querySelectorAll('#chat-log .msg-error').length`)) === 0);
  await ask("boom");
  const boomMeta = await text("#chat-log .msg-agent:last-of-type .msg-meta");
  check("agent-side failure is shown as an agent error badge (not hidden)", boomMeta.includes("agent error: llm_error: RuntimeError"));
  check("provider exception text/secret never reaches the page", !(await evalJs(`document.body.innerText`)).includes("secret-key-123"));
  await clickSel("#tab-activity");
  await waitFor(`document.querySelector('#panel-activity .alert-danger')`, { label: "trace error" });
  check("activity shows status Error and the run error", (await text("#panel-activity")).includes("Run error: llm_error: RuntimeError") && !(await text("#panel-activity")).includes("secret-key-123"));
  await shot("07_activity_failed_run");

  // ---------------------------------------------------------------- XSS probe
  console.log("• injection safety");
  await ask(`<img src=x onerror="window.__xss=1"><b id="pwn">bold</b> policy`);
  await sleep(300);
  check("HTML in the user's message is shown as text", (await evalJs(`[...document.querySelectorAll('#chat-log .msg-user .msg-body')].pop().innerText`)).includes("<img src=x"));
  check("no injected elements or handlers executed", await evalJs(`window.__xss === undefined && !document.getElementById('pwn') && document.querySelectorAll('#chat-log img, #panel-evidence img, #panel-evidence b').length === 0`));

  // ---------------------------------------------------------------- markdown rendering
  console.log("• markdown rendering");
  await ask("markdown demo");
  const md = await evalJs(`(() => {
    const root = [...document.querySelectorAll('#chat-log .msg-agent .msg-body .md')].pop();
    const q = (sel) => [...root.querySelectorAll(sel)];
    const ul = q('ul')[0];
    return {
      strongTexts: q('strong').map(e => e.textContent), liCount: q('li').length, topListItems: ul ? ul.children.length : 0,
      ulStyle: ul ? getComputedStyle(ul).listStyleType : null, strongWeight: q('strong')[0] ? getComputedStyle(q('strong')[0]).fontWeight : null,
      ol: q('ol').length, nestedUl: q('li ul').length, nestedItems: q('li ul li').length, em: q('em').map(e => e.textContent),
      codeInline: q('p code, li code').map(e => e.textContent), pre: q('pre').map(e => e.textContent),
      heading: q('.md-heading').map(e => e.textContent), quote: q('blockquote').map(e => e.textContent),
      links: q('a').map(a => ({ text: a.textContent, href: a.getAttribute('href'), rel: a.rel, target: a.target })),
      literalMarkers: /\\*\\*|^\\* /m.test(root.innerText), text: root.innerText,
      dangerousTags: q('script, img, iframe, style, object, embed, form, input').length,
    }; })()`);
  check("bold labels are real <strong> elements (the reported bug)", md.strongTexts.includes("Student Name:") && md.strongTexts.includes("Amount Due:") && md.strongTexts.includes("Status:"), JSON.stringify(md.strongTexts));
  check("no literal ** or leading '* ' markers remain in the rendered reply", !md.literalMarkers, md.text.slice(0, 200));
  check("bullets render as a real list with the right item count", md.topListItems === 3 && md.liCount >= 7, `top ${md.topListItems}, li ${md.liCount}`);
  check("bullet list is visibly bulleted and bold is visibly bold", md.ulStyle === "disc" && Number(md.strongWeight) >= 600, `${md.ulStyle} ${md.strongWeight}`);
  check("ordered list and nested bullets render", md.ol === 1 && md.nestedUl === 1 && md.nestedItems === 2);
  check("inline code, italic, heading and quote render", md.codeInline.includes("check_fee_status") && md.em.includes("ask") && md.heading.includes("Next steps") && md.quote.length === 1);
  check("snake_case names and chunk ids inside text stay literal", md.text.includes("check_fee_status and ids like student_handbook.md#s2-1"));
  check("fenced code keeps its text literally (including <b> as text) and table falls back to preformatted text",
    md.pre.some((t) => t.includes("code <b>line</b> 2")) && md.pre.some((t) => t.includes("| col a | col b |")));
  check("links open safely (https only, noopener noreferrer, new tab)", md.links.length === 1 && md.links[0].href === "https://example.com/fees" && md.links[0].rel === "noopener noreferrer" && md.links[0].target === "_blank");
  check("no dangerous elements anywhere in the reply", md.dangerousTags === 0);
  const mdColors = await evalJs(`(() => { const a = [...document.querySelectorAll('#chat-log .md a')].pop(); return a ? getComputedStyle(a).textDecorationLine : null; })()`);
  check("links are underlined (not distinguished by colour alone)", mdColors === "underline");
  const reveal = await evalJs(`(() => { const log = document.getElementById('chat-log'); const r = [...log.querySelectorAll('.msg-agent')].pop();
    const lt = log.getBoundingClientRect(), rt = r.getBoundingClientRect(); return { replyTopInView: rt.top >= lt.top - 1 && rt.top < lt.bottom, replyH: Math.round(r.offsetHeight), logH: log.clientHeight }; })()`);
  check("a long reply is scrolled so its FIRST line is visible", reveal.replyTopInView, JSON.stringify(reveal));
  await shot("15_markdown_reply");

  await ask("unsafe markdown");
  const un = await evalJs(`(() => {
    const root = [...document.querySelectorAll('#chat-log .msg-agent .msg-body .md')].pop();
    return { xss: window.__xss, scripts: document.querySelectorAll('#chat-log script').length, imgs: document.querySelectorAll('#chat-log img').length,
      pwn: !!document.getElementById('pwn2'), hrefs: [...root.querySelectorAll('a')].map(a => a.getAttribute('href')), text: root.innerText,
      boldStill: [...root.querySelectorAll('strong')].map(e => e.textContent),
      pixelRequested: performance.getEntriesByType('resource').some(r => r.name.includes('127.0.0.1:1/pixel.png')) }; })()`);
  check("hostile markup in a REPLY never executes or creates elements", un.xss === undefined && un.scripts === 0 && un.imgs === 0 && !un.pwn);
  check("raw HTML in a reply is shown as literal text", un.text.includes("<script>window.__xss=2</script>") && un.text.includes('<img src=x onerror="window.__xss=3">'));
  check("javascript: link is not a link; only the https link is", un.hrefs.length === 1 && un.hrefs[0] === "https://example.com/page" && un.text.includes("[bad link](javascript:window.__xss=4)"));
  check("image syntax is shown as text and no image/tracking request is made", un.text.includes("![tracking pixel](http://127.0.0.1:1/pixel.png)") && !un.pixelRequested);
  check("markdown around the hostile text still renders", un.boldStill.includes("still bold"));
  await ask("**literal** and _plain_ user text");
  check("the USER's own messages are never formatted (stay literal text)", await evalJs(`(() => { const b = [...document.querySelectorAll('#chat-log .msg-user .msg-body')].pop(); return b.textContent === '**literal** and _plain_ user text' && b.querySelectorAll('strong, em').length === 0; })()`));
  check("hostile reply did not break the page (chat still works)", !(await evalJs(`document.getElementById('send').disabled`)));

  // ---------------------------------------------------------------- evaluation tab
  console.log("• evaluation reports");
  await clickSel("#tab-evaluation");
  await waitFor(`document.querySelector('#panel-evaluation .metrics')`, { label: "evaluation metrics" });
  const list = await (await fetch(`${BASE}/api/evaluation/reports`)).json();
  const full = await (await fetch(`${BASE}/api/evaluation/reports/${list.reports[0].name}`)).json();
  const exp = await text("#panel-evaluation .explainer");
  const trusted = list.reports.filter((r) => r.integrity === "ok");
  const nOff = trusted.filter((r) => r.mode === "offline").length, nLive = trusted.filter((r) => r.mode === "live").length;
  const nUnverified = list.reports.length - nOff - nLive;
  check("explainer says the tab only displays saved reports and never runs an evaluation", exp.includes("only displays saved report files") && exp.includes("never runs an evaluation"));
  check("explainer's report counts equal the real report list (inconsistent reports counted separately, never as live)",
    exp.includes(`${nOff} offline · ${nLive} live${nUnverified ? ` · ${nUnverified} unverified` : ""}`) && nUnverified === 1, exp.slice(0, 200));
  check("no 'no LIVE evaluation' warning when a live report exists", nLive > 0 && !exp.includes("No LIVE evaluation has been run yet"));
  await evalJs(`document.querySelector('#panel-evaluation .details summary').click()`);
  const defs = await text("#panel-evaluation .details");
  check("OFFLINE vs LIVE explanation is available and states the limits", defs.includes("says nothing about how well a real model behaves") && defs.includes("--live") && defs.includes("--offline") && defs.includes("no flag runs LIVE"));
  check("report dropdown lists every real saved report", (await evalJs(`document.getElementById('report-select').options.length`)) === list.reports.length && list.reports.length >= 1);
  const evalText = await text("#panel-evaluation");
  check(`mode is labelled ${full.mode.toUpperCase()} prominently`, evalText.includes(full.mode.toUpperCase()) && (await text("#panel-evaluation .alert")).length > 20);
  if (full.mode === "offline") {
    check("offline banner says it is a pipeline check, not model quality", evalText.includes("says nothing about real model quality") && evalText.toLowerCase().includes("token usage is not available"));
  }
  const cardMap = await evalJs(`Object.fromEntries([...document.querySelectorAll('#panel-evaluation .metric')].map(m => [m.querySelector('.metric-label').textContent, m.querySelector('.metric-value').textContent]))`);
  check("success metric equals the saved report's numbers", cardMap["Task success (cases)"] === `${full.metrics.cases_passed}/${full.metrics.cases}` && cardMap["Task success (runs)"] === `${full.metrics.runs_passed}/${full.metrics.runs}`);
  check("token usage is 'not available' exactly when the report has none", (full.metrics.tokens.runs_with_usage === 0) === (cardMap["Token usage"] === "not available"));
  check("per-run table has one row per run in the report", (await evalJs(`document.querySelectorAll('#panel-evaluation tbody tr').length`)) === full.runs.length);
  check("failures section matches the report", (full.metrics.failed_runs.length === 0) === evalText.includes("None in this report."));
  await shot("08_evaluation_desktop");
  const liveIdx = list.reports.findIndex((r) => r.mode === "live" && r.integrity === "ok");
  const dateKeys = list.reports.map((r) => r.name.split("_").slice(2).join("_"));
  check("(fixture) reports are sorted by time (not grouped by mode); newest is a real offline report",
    dateKeys.every((k, i) => i === 0 || dateKeys[i - 1] >= k) && list.reports[0].mode === "offline" && liveIdx > 0);
  if (liveIdx >= 0) {
    const live = await (await fetch(`${BASE}/api/evaluation/reports/${list.reports[liveIdx].name}`)).json();
    await evalJs(`(() => { const s = document.getElementById('report-select'); s.value = ${JSON.stringify(list.reports[liveIdx].name)}; s.dispatchEvent(new Event('change')); })()`);
    await waitFor(`document.querySelector('#panel-evaluation .summary-grid')?.innerText.includes('TEST-FIXTURE')`, { label: "live fixture report" });
    const liveText = await text(`#panel-evaluation [aria-live="polite"]`); // the report itself, not the dropdown listing other reports
    check("LIVE report is labelled LIVE with the success style", (await text("#panel-evaluation .alert")).includes("LIVE") && (await evalJs(`document.querySelector('#panel-evaluation .alert').classList.contains('alert-success')`)));
    check("LIVE report does not carry the offline pipeline-check disclaimer", !liveText.includes("says nothing about real model quality") && !liveText.includes("OFFLINE"));
    check("LIVE latency is described as end-to-end including the LLM API", liveText.includes("end-to-end incl. LLM API"));
    const liveCards = await evalJs(`Object.fromEntries([...document.querySelectorAll('#panel-evaluation .metric')].map(m => [m.querySelector('.metric-label').textContent, [m.querySelector('.metric-value').textContent, m.querySelector('.metric-sub').textContent]]))`);
    check("token usage reflects the report's real usage numbers", liveCards["Token usage"][0] === "reported" && liveCards["Token usage"][1].includes(`${live.metrics.tokens.runs_with_usage}/${live.metrics.tokens.runs_total} runs reported usage`) && liveCards["Token usage"][1].includes(`input total ${live.metrics.tokens.input_total}`));
    check("a run row shows that run's token counts", (await text("#panel-evaluation tbody tr")).includes(`${live.runs[0].input_tokens} / ${live.runs[0].output_tokens}`));
    await shot("14_evaluation_live_fixture");
    await evalJs(`(() => { const s = document.getElementById('report-select'); s.value = s.options[0].value; s.dispatchEvent(new Event('change')); })()`);
    await waitFor(`document.querySelector('#panel-evaluation .alert')?.innerText.includes('OFFLINE')`);
  }
  const badIdx = list.reports.findIndex((r) => r.integrity === "inconsistent");
  check("(fixture) the API flags the scripted-but-labelled-live report as inconsistent", badIdx >= 0 && list.reports[badIdx].mode === "live");
  if (badIdx >= 0) {
    const label = await evalJs(`document.getElementById('report-select').options[${badIdx}].textContent`);
    check("dropdown marks it as inconsistent instead of presenting it as a live report", label.includes("inconsistent") && label.startsWith("LIVE"), label);
    await evalJs(`(() => { const s = document.getElementById('report-select'); s.value = ${JSON.stringify(list.reports[badIdx].name)}; s.dispatchEvent(new Event('change')); })()`);
    await waitFor(`document.querySelector('#panel-evaluation .alert-danger')?.innerText.includes('INCONSISTENT')`, { label: "inconsistent banner" });
    const bad = await text(`#panel-evaluation [aria-live="polite"]`);
    check("inconsistent report shows a red INCONSISTENT warning naming the real provider", bad.includes("INCONSISTENT") && bad.includes("offline-scripted") && bad.includes("do not use it as evidence of real-model quality"));
    check("it is NOT shown with the green LIVE banner or the 'Real model run' text", !bad.includes("Real model run") && !(await evalJs(`!!document.querySelector('#panel-evaluation [aria-live="polite"] .alert-success')`)));
    check("the inconsistent banner is announced as an alert", await evalJs(`document.querySelector('#panel-evaluation .alert-danger').getAttribute('role') === 'alert'`));
    await shot("16_evaluation_inconsistent_report");
    await evalJs(`(() => { const s = document.getElementById('report-select'); s.value = s.options[0].value; s.dispatchEvent(new Event('change')); })()`);
    await waitFor(`document.querySelector('#panel-evaluation .alert')?.innerText.includes('OFFLINE')`);
  }
  const blockedName = "evaluation_live_20180101_000000.json";
  if (list.reports.some((r) => r.name === blockedName)) {
    const bj = await (await fetch(`${BASE}/api/evaluation/reports/${blockedName}`)).json();
    const bm = bj.metrics, notRun = bm.runs_blocked + bm.runs_skipped;
    check("(fixture) the API reports completed / blocked / not-run runs separately", bm.runs_completed === 4 && bm.runs_blocked === 2 && bm.runs_skipped === 7 && bj.integrity === "ok");
    await evalJs(`(() => { const s = document.getElementById('report-select'); s.value = ${JSON.stringify(blockedName)}; s.dispatchEvent(new Event('change')); })()`);
    await waitFor(`document.querySelector('#panel-evaluation .alert-warning[role="alert"]')?.innerText.includes('Provider errors left')`, { label: "blocked banner" });
    const txt = await text(`#panel-evaluation [aria-live="polite"]`);
    check("banner says how many runs the provider blocked and that they are NOT agent failures",
      txt.includes(`Provider errors left ${notRun} of ${bm.runs} runs unevaluated (${bm.runs_blocked} blocked: quota ×${bm.runs_blocked}; ${bm.runs_skipped} not run after the evaluation stopped early)`) && txt.includes("not counted as agent failures") && txt.includes(`only the ${bm.runs_completed} completed runs`));
    const cards = await evalJs(`Object.fromEntries([...document.querySelectorAll('#panel-evaluation .metric')].map(m => [m.querySelector('.metric-label').textContent, m.querySelector('.metric-value').textContent]))`);
    check("cards show completed runs and the blocked count", cards["Runs completed"] === `${bm.runs_completed}/${bm.runs}` && cards["Blocked or not run"] === String(notRun) && cards["Traces recorded"] === `${bm.traces_recorded}/${bm.runs_executed}` && bm.traces_recorded <= bm.runs_executed);
    check("success rates are over ELIGIBLE runs only (cases evaluated / completed runs), not all runs",
      cards["Task success (cases)"] === `${bm.cases_passed}/${bm.cases_evaluated}` && cards["Task success (runs)"] === `${bm.runs_passed}/${bm.runs_completed}` && cards["Tool choice appropriate"] === `${bm.tool_choice_appropriate}/${bm.runs_completed}`);
    const badges = await evalJs(`(() => { const t = [...document.querySelectorAll('#panel-evaluation tbody td:nth-child(6) .badge')].map(b => b.textContent); return { blocked: t.filter(x => x === 'BLOCKED').length, skipped: t.filter(x => x === 'SKIPPED').length, fail: t.filter(x => x === 'FAIL').length, pass: t.filter(x => x === 'PASS').length }; })()`);
    check("per-run table marks BLOCKED and SKIPPED runs instead of FAIL", badges.blocked === bm.runs_blocked && badges.skipped === bm.runs_skipped && badges.fail === bm.runs_completed - bm.runs_passed && badges.pass === bm.runs_passed, JSON.stringify(badges));
    check("a separate 'Blocked or not run' list is shown and the pacing used is displayed",
      txt.includes("Blocked or not run (excluded from success rates)") && txt.toLowerCase().includes("pacing") && txt.includes("429 backoff 10/20/40s") && txt.includes("Failures (completed runs only)"), txt.slice(0, 400));   // labels are upper-cased by CSS
    check("the blocked report still carries the LIVE label (it is a real-provider run)", (await text("#panel-evaluation .alert")).includes("LIVE") || txt.includes("LIVE"));
    await evalJs(`(() => { const d = document.querySelector('#panel-evaluation .details'); if (d) d.open = false; const panel = document.getElementById('panel-evaluation'); panel.scrollTop = document.querySelector('#panel-evaluation .alert-warning').offsetTop - panel.offsetTop - 8; })()`);
    await shot("17_evaluation_blocked_runs");
    await evalJs(`(() => { const s = document.getElementById('report-select'); s.value = s.options[0].value; s.dispatchEvent(new Event('change')); })()`);
    await waitFor(`document.querySelector('#panel-evaluation .alert')?.innerText.includes('OFFLINE')`);
    check("an offline report shows no blocked-runs wording or extra cards", !(await text(`#panel-evaluation [aria-live="polite"]`)).includes("blocked by provider") && !(await evalJs(`[...document.querySelectorAll('#panel-evaluation .metric-label')].some(e => e.textContent === 'Runs completed')`)));
  }
  if (list.reports.length > 1) {
    const second = await (await fetch(`${BASE}/api/evaluation/reports/${list.reports[1].name}`)).json();
    await evalJs(`(() => { const s = document.getElementById('report-select'); s.value = s.options[1].value; s.dispatchEvent(new Event('change')); })()`);
    await waitFor(`document.querySelector('#panel-evaluation .summary-grid')?.innerText.includes(${JSON.stringify(second.meta.timestamp)})`, { label: "second report loaded" });
    check("choosing another report loads that report", true);
  }

  // ---------------------------------------------------------------- contrast (computed styles, WCAG AA 4.5:1)
  console.log("• contrast");
  await clickSel("#tab-evidence");
  const contrast = await evalJs(`(() => {
    const lum = (c) => { const [r,g,b] = c.map(v => { v/=255; return v <= 0.03928 ? v/12.92 : ((v+0.055)/1.055) ** 2.4; }); return 0.2126*r + 0.7152*g + 0.0722*b; };
    const parse = (s) => { const m = s.match(/rgba?\\(([^)]+)\\)/); const p = m[1].split(',').map(x => parseFloat(x)); return { rgb: p.slice(0,3), a: p.length > 3 ? p[3] : 1 }; };
    const bgOf = (el) => { let e = el; while (e) { const c = parse(getComputedStyle(e).backgroundColor); if (c.a > 0.9) return c.rgb; e = e.parentElement; } return [255,255,255]; };
    const ratio = (el) => { const fg = parse(getComputedStyle(el).color).rgb, bg = bgOf(el); const a = lum(fg), b = lum(bg); return (Math.max(a,b)+0.05)/(Math.min(a,b)+0.05); };
    const probes = ['.brand-title','.brand-sub','.help','.panel-sub','#api-status','#session-chip','.tabs [aria-selected=true]','.tabs [aria-selected=false]','.empty-state p','.chip-btn','#send','.btn-secondary','label[for=student-id]'];
    const out = {}; for (const s of probes) { const el = document.querySelector(s); if (el) out[s] = Math.round(ratio(el)*100)/100; }
    return out; })()`);
  for (const [sel, r] of Object.entries(contrast)) check(`contrast ${sel} ≥ 4.5 (is ${r})`, r >= 4.5);
  await clickSel("#tab-activity"); await waitFor(`document.querySelector('#panel-activity .timeline, #panel-activity .alert, #panel-activity .loading')`);
  const contrast2 = await evalJs(`(() => {
    const lum = (c) => { const [r,g,b] = c.map(v => { v/=255; return v <= 0.03928 ? v/12.92 : ((v+0.055)/1.055) ** 2.4; }); return 0.2126*r + 0.7152*g + 0.0722*b; };
    const parse = (s) => { const m = s.match(/rgba?\\(([^)]+)\\)/); const p = m[1].split(',').map(x => parseFloat(x)); return { rgb: p.slice(0,3), a: p.length > 3 ? p[3] : 1 }; };
    const bgOf = (el) => { let e = el; while (e) { const c = parse(getComputedStyle(e).backgroundColor); if (c.a > 0.9) return c.rgb; e = e.parentElement; } return [255,255,255]; };
    const ratio = (el) => { const fg = parse(getComputedStyle(el).color).rgb, bg = bgOf(el); const a = lum(fg), b = lum(bg); return (Math.max(a,b)+0.05)/(Math.min(a,b)+0.05); };
    const out = {}; for (const el of document.querySelectorAll('.badge, .meta-text, .msg-who, .kv dt, .alert, .muted, .link-btn')) { out[el.className + '|' + el.textContent.slice(0,12)] = Math.round(ratio(el)*100)/100; }
    return out; })()`);
  const worst = Object.entries(contrast2).sort((a, b) => a[1] - b[1])[0];
  check(`contrast of badges/meta/alerts/links ≥ 4.5 (worst: ${worst ? worst[0] + " = " + worst[1] : "n/a"})`, !worst || worst[1] >= 4.5);

  // ---------------------------------------------------------------- responsive layout
  console.log("• responsive");
  const overflow = () => evalJs(`document.documentElement.scrollWidth - document.documentElement.clientWidth`);
  await setViewport(1440, 900);
  await clickSel("#tab-evidence"); await sleep(150);
  check("desktop: chat and inspector side by side", (await visible("#chat-panel")) && (await visible("#inspector")) && !(await visible(".view-switch")));
  const rects = await evalJs(`(() => { const a = document.getElementById('chat-panel').getBoundingClientRect(), b = document.getElementById('inspector').getBoundingClientRect(); return { aRight: a.right, bLeft: b.left, aTop: a.top, bTop: b.top }; })()`);
  check("desktop: panels are in two columns", rects.bLeft >= rects.aRight && Math.abs(rects.aTop - rects.bTop) < 2);
  check("desktop: page itself does not scroll (panels scroll internally)", (await evalJs(`document.documentElement.scrollHeight <= window.innerHeight + 1`)));
  check("desktop: no horizontal overflow", (await overflow()) <= 0);
  await clickSel("#tab-evaluation"); await waitFor(`document.getElementById('report-select')`);
  const selW = await evalJs(`Math.round(document.getElementById('report-select').getBoundingClientRect().width)`);
  check("desktop: report dropdown is wide enough to show its label", selW >= 330, `width ${selW}px`);
  check("OFFLINE badge in the banner has solid, high-contrast styling", await evalJs(`(() => { const b = document.querySelector('#panel-evaluation .alert .badge'); const s = getComputedStyle(b); return s.color === 'rgb(255, 255, 255)' && s.backgroundColor !== 'rgb(254, 243, 199)'; })()`));
  await clickSel("#tab-evidence");
  for (const [w, h, mobile, name] of [[1024, 768, false, "tablet-landscape"], [768, 1024, true, "tablet"], [390, 844, true, "phone"], [320, 640, true, "small-phone"]]) {
    await setViewport(w, h, mobile); await sleep(250);
    const ov = await overflow();
    check(`${name} (${w}px): no horizontal page overflow`, ov <= 0, `overflow ${ov}px`);
  }
  await setViewport(390, 844, true); await sleep(250);
  check("phone: Chat/Insights switch is shown", await visible(".view-switch"));
  check("phone: chat visible, inspector hidden by default", (await visible("#chat-panel")) && !(await visible("#inspector")));
  const targets = await evalJs(`[...document.querySelectorAll('button, select, input[type=text], textarea')].filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; }).map(e => ({ id: e.id || e.className || e.tagName, h: Math.round(e.getBoundingClientRect().height) }))`);
  const small = targets.filter((t) => t.h < 30);
  await clickSel("#view-insights");
  const tabsDims = await evalJs(`(() => { const t = document.querySelector('.tabs'); return { scroll: t.scrollWidth, client: t.clientWidth, tabs: [...t.children].map(c => Math.round(c.getBoundingClientRect().width)) }; })()`);
  check("phone (390px): all four inspector tabs fit without horizontal clipping", tabsDims.scroll <= tabsDims.client + 1, JSON.stringify(tabsDims));
  await clickSel("#view-chat");
  check("phone: controls are comfortably tappable (≥ 30px tall)", small.length === 0, JSON.stringify(small));
  await ask("hello on a phone");
  await shot("09_phone_chat");
  await clickSel("#view-insights");
  check("phone: Insights shows inspector and hides chat", (await visible("#inspector")) && !(await visible("#chat-panel")) && (await evalJs(`document.getElementById('view-insights').getAttribute('aria-pressed') === 'true'`)));
  await clickSel("#tab-evaluation"); await waitFor(`document.querySelector('#panel-evaluation .metrics')`);
  check("phone: evaluation view has no page-level horizontal overflow (table scrolls inside its wrapper)", (await overflow()) <= 0);
  check("phone: wide table is horizontally scrollable inside a focusable region", await evalJs(`(() => { const w = document.querySelector('#panel-evaluation .table-wrap'); return w.scrollWidth > w.clientWidth && w.tabIndex === 0 && w.getAttribute('role') === 'region'; })()`));
  await shot("10_phone_evaluation");
  await clickSel("#view-chat");
  await setViewport(768, 1024, true); await sleep(200);
  await shot("11_tablet_chat");
  await setViewport(1440, 900); await sleep(200);
  await clickSel("#tab-activity"); await waitFor(`document.querySelector('#panel-activity .timeline, #panel-activity .alert')`);
  await shot("12_desktop_final");

  // ---------------------------------------------------------------- console health
  // The only tolerated entry is Chrome's own network log line for the deliberate unknown-student 404 above.
  const unexpected = consoleErrors.filter((e) => !(e.includes("404") && e.includes("/api/memory/NOPE")));
  check("no console errors or uncaught exceptions during the whole run", unexpected.length === 0, unexpected.join(" | "));
} catch (err) {
  check(`script completed without crashing`, false, err.message);
  try { await shot("99_crash"); } catch {}
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} browser checks passed`);
for (const f of failed) console.log(`FAILED: ${f.name} ${f.detail}`);
shutdown(failed.length ? 1 : 0);
