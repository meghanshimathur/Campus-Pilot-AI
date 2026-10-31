import test from "node:test";
import assert from "node:assert/strict";
import { ApiError, api, request } from "../../frontend/js/api.js";

const json = (status, body) => ({ ok: status >= 200 && status < 300, status, text: async () => (body === undefined ? "" : JSON.stringify(body)) });
const recorder = (response) => { const calls = []; const fn = async (url, init) => { calls.push({ url, init }); return typeof response === "function" ? response(url, init) : response; }; fn.calls = calls; return fn; };

test("successful JSON request", async () => {
  const f = recorder(json(200, { status: "ok" }));
  assert.deepEqual(await request("/api/health", { fetchImpl: f }), { status: "ok" });
  assert.equal(f.calls[0].init.method, "GET");
  assert.equal(f.calls[0].init.body, undefined);
});

test("POST sends JSON body and content type", async () => {
  const f = recorder(json(200, { ok: 1 }));
  await request("/api/chat", { method: "POST", body: { a: 1 }, fetchImpl: f });
  assert.equal(f.calls[0].init.body, '{"a":1}');
  assert.equal(f.calls[0].init.headers["Content-Type"], "application/json");
});

test("HTTP errors become readable ApiErrors", async () => {
  await assert.rejects(request("/api/chat", { fetchImpl: recorder(json(503, { detail: "No LLM provider configured. See .env.example." })) }),
    (e) => e instanceof ApiError && e.status === 503 && e.kind === "http" && /No LLM provider/.test(e.message));
  await assert.rejects(request("/x", { fetchImpl: recorder(json(422, { detail: [{ loc: ["body", "message"], msg: "too short" }] })) }),
    (e) => e.status === 422 && e.message === "message: too short");
});

test("network failure and timeout are distinguished", async () => {
  await assert.rejects(request("/x", { fetchImpl: async () => { throw new TypeError("fetch failed"); } }), (e) => e.kind === "network" && /Could not reach the server/.test(e.message));
  const abortErr = Object.assign(new Error("aborted"), { name: "AbortError" });
  await assert.rejects(request("/x", { fetchImpl: async () => { throw abortErr; } }), (e) => e.kind === "timeout");
});

test("timeout really aborts a hanging request", async () => {
  const hanging = (url, init) => new Promise((_, reject) => init.signal.addEventListener("abort", () => reject(Object.assign(new Error("a"), { name: "AbortError" }))));
  await assert.rejects(request("/x", { timeoutMs: 20, fetchImpl: hanging }), (e) => e.kind === "timeout");
});

test("unreadable success body is a parse error, not fake data", async () => {
  await assert.rejects(request("/x", { fetchImpl: async () => ({ ok: true, status: 200, text: async () => "<html>nope</html>" }) }), (e) => e.kind === "parse");
  await assert.rejects(request("/x", { fetchImpl: async () => ({ ok: true, status: 200, text: async () => "" }) }), (e) => e.kind === "parse");
});

test("endpoint wrappers use the existing backend routes and encode path parts", async () => {
  const f = recorder(json(200, {}));
  const orig = globalThis.fetch; globalThis.fetch = f;
  try {
    await api.memory("STU 001/../x");
    await api.deleteMemory("STU001", "preferred_name");
    await api.trace("abc123");
    await api.report("evaluation_offline_20261022_000402.json");
    await api.reports();
    await api.chat({ message: "hi", studentId: "STU001", sessionId: null });
    await api.chat({ message: "hi", studentId: "STU001", sessionId: "s-1" });
  } finally { globalThis.fetch = orig; }
  const urls = f.calls.map((c) => `${c.init.method} ${c.url}`);
  assert.deepEqual(urls, [
    "GET /api/memory/STU%20001%2F..%2Fx",
    "DELETE /api/memory/STU001/preferred_name",
    "GET /api/traces/abc123",
    "GET /api/evaluation/reports/evaluation_offline_20261022_000402.json",
    "GET /api/evaluation/reports",
    "POST /api/chat",
    "POST /api/chat",
  ]);
  assert.deepEqual(JSON.parse(f.calls[5].init.body), { message: "hi", student_id: "STU001" });
  assert.deepEqual(JSON.parse(f.calls[6].init.body), { message: "hi", student_id: "STU001", session_id: "s-1" });
});
