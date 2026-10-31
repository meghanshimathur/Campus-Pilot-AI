// Browser test of the dashboard against the REAL backend with no LLM provider configured (no key).
// Usage: node tests/e2e/ui_no_key.mjs <base-url> [screenshots-dir]
import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const BASE = process.argv[2], SHOTS = process.argv[3];
const CHROME = process.env.CHROME_BIN || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
if (SHOTS) mkdirSync(SHOTS, { recursive: true });
const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok: Boolean(ok) }); if (!ok) console.log(`  ✖ ${name} ${detail}`); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const port = 9800 + Math.floor(Math.random() * 150);
const chrome = spawn(CHROME, ["--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run", `--user-data-dir=${mkdtempSync(join(tmpdir(), "cp_chrome_"))}`,
  `--remote-debugging-port=${port}`, "--window-size=1440,900", "about:blank"], { stdio: "ignore" });
const done = (code) => { try { chrome.kill("SIGKILL"); } catch {} process.exit(code); };
let page;
for (let i = 0; i < 60 && !page; i++) { try { page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((t) => t.type === "page"); } catch {} if (!page) await sleep(250); }
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let id = 1; const pending = new Map(); const errors = [];
ws.onmessage = (ev) => { const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result); return; }
  if (m.method === "Runtime.exceptionThrown") errors.push(`exception ${m.params.exceptionDetails.text}`);
  if (m.method === "Log.entryAdded" && m.params.entry.level === "error") errors.push(`${m.params.entry.text} ${m.params.entry.url ?? ""}`); };
const send = (method, params = {}) => new Promise((resolve, reject) => { const i = id++; pending.set(i, { resolve, reject }); ws.send(JSON.stringify({ id: i, method, params })); });
const evalJs = async (e) => { const r = await send("Runtime.evaluate", { expression: e, awaitPromise: true, returnByValue: true }); if (r.exceptionDetails) throw new Error(r.exceptionDetails.text); return r.result.value; };
const waitFor = async (e, t = 10000) => { const s = Date.now(); while (Date.now() - s < t) { try { if (await evalJs(e)) return; } catch {} await sleep(80); } throw new Error(`timeout: ${e}`); };
const text = (sel) => evalJs(`(document.querySelector(${JSON.stringify(sel)})?.innerText ?? '')`);
try {
  await send("Page.enable"); await send("Runtime.enable"); await send("Log.enable");
  await send("Page.navigate", { url: BASE });
  await waitFor(`document.getElementById('api-status').textContent.startsWith('API online')`);
  check("health chip warns that no LLM key is configured", (await text("#api-status")).includes("no LLM key configured"));
  check("health chip uses the warning style (not success)", await evalJs(`document.getElementById('api-status').classList.contains('chip-warning')`));
  await evalJs(`document.getElementById('message').focus()`);
  await send("Input.insertText", { text: "What is my fee status?" });
  await send("Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, text: "\r" });
  await waitFor(`document.querySelector('#chat-log .msg-error')`);
  const err = await text("#chat-log .msg-error");
  check("real 503 from the backend is shown with the server's own message", err.includes("No LLM provider configured. See .env.example."));
  check("error offers Retry and Dismiss", (await evalJs(`document.querySelectorAll('#chat-log .msg-error button').length`)) === 2);
  check("send button re-enabled and focus returned to the box after the error", await evalJs(`!document.getElementById('send').disabled && document.activeElement.id === 'message'`));
  check("no fabricated assistant reply was shown", (await evalJs(`document.querySelectorAll('#chat-log .msg-agent').length`)) === 0);
  await evalJs(`document.getElementById('tab-memory').click()`);
  await waitFor(`document.querySelector('#panel-memory .empty-state')`);
  check("memory view works without a provider (real GET /api/memory)", (await text("#panel-memory")).includes("Nothing remembered yet"));
  await evalJs(`document.getElementById('tab-evaluation').click()`);
  await waitFor(`document.querySelector('#panel-evaluation .metrics, #panel-evaluation .empty-state, #panel-evaluation .alert-danger')`);
  check("evaluation view shows real saved reports (or an honest empty state)", (await text("#panel-evaluation")).includes("OFFLINE") || (await text("#panel-evaluation")).includes("No evaluation reports"));
  const rep = await (await fetch(`${BASE}/api/evaluation/reports`)).json();
  const nOff = rep.reports.filter((r) => r.mode === "offline").length, nLive = rep.reports.filter((r) => r.mode === "live").length;
  const exp = await text("#panel-evaluation .explainer");
  check("explainer counts match the real saved reports", exp.includes(`${nOff} offline · ${nLive} live`), exp.slice(0, 150));
  check("with zero LIVE reports the tab says real-model quality is unmeasured", nLive === 0 ? exp.includes("No LIVE evaluation has been run yet") && exp.includes("still unmeasured") : true);
  // The REAL saved live report (written before provider errors were separated) must be shown with its quota failures
  // recognised as provider blocks, not as 10 agent failures.
  const live = rep.reports.find((r) => r.mode === "live" && r.integrity === "ok");
  if (live) {
    const lj = await (await fetch(`${BASE}/api/evaluation/reports/${live.name}`)).json();
    const lm = lj.metrics;
    await evalJs(`(() => { const s = document.getElementById('report-select'); s.value = ${JSON.stringify(live.name)}; s.dispatchEvent(new Event('change')); })()`);
    await waitFor(`document.querySelector('#panel-evaluation .metrics')`);
    await sleep(300);
    const panel = await text(`#panel-evaluation [aria-live="polite"]`);
    if (lj.normalized) {
      check("legacy live report: outcomes are derived at read time and the page says so", panel.includes("Derived when read") && lm.runs_blocked > 0);
      check("legacy live report: the banner separates quota blocks from agent failures", panel.includes(`Provider errors left ${lm.runs_blocked + lm.runs_skipped} of ${lm.runs} runs unevaluated`) && panel.includes("not counted as agent failures"));
      const c = await evalJs(`Object.fromEntries([...document.querySelectorAll('#panel-evaluation .metric')].map(m => [m.querySelector('.metric-label').textContent, m.querySelector('.metric-value').textContent]))`);
      check("legacy live report: success is shown over completed runs, not all runs", c["Runs completed"] === `${lm.runs_completed}/${lm.runs}` && c["Task success (runs)"] === `${lm.runs_passed}/${lm.runs_completed}`, JSON.stringify(c));
    }
  }
  if (SHOTS) writeFileSync(join(SHOTS, "13_no_key_error.png"), Buffer.from((await send("Page.captureScreenshot", { format: "png" })).data, "base64"));
  const unexpected = errors.filter((e) => !e.includes("503"));
  check("no unexpected console errors (Chrome logs the deliberate 503 itself)", unexpected.length === 0, unexpected.join(" | "));
} catch (e) { check("script completed", false, e.message); }
const failed = results.filter((r) => !r.ok);
console.log(`${results.length - failed.length}/${results.length} no-key browser checks passed`);
done(failed.length ? 1 : 0);
