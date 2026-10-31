import test, { afterEach } from "node:test";
import assert from "node:assert/strict";
import { ApiError, api, clearSessionToken, hasSessionToken, request, setSessionToken, setUnauthorizedHandler } from "../../frontend/js/api.js";
import { createStore } from "../../frontend/js/state.js";

const TOKEN = "tok-eval-0123456789abcdefghijklmnopqrstuv";   // fictional
const PASSWORD = "pw-fictional-Zq77";
const json = (status, body) => ({ ok: status >= 200 && status < 300, status, text: async () => (body === undefined ? "" : JSON.stringify(body)) });
const recorder = (response) => { const calls = []; const fn = async (url, init) => { calls.push({ url, init }); return typeof response === "function" ? response(url, init) : response; }; fn.calls = calls; return fn; };
const loginBody = { token: TOKEN, token_type: "bearer", expires_at: "2026-11-01T10:00:00Z", user: { user_id: "STU001", role: "student", student_id: "STU001", display_name: "Student 1" } };

afterEach(() => { clearSessionToken(); setUnauthorizedHandler(null); });

test("sign-in posts the credentials once, without any Authorization header, and returns the server's answer", async () => {
  const f = recorder(json(200, loginBody));
  const orig = globalThis.fetch; globalThis.fetch = f;
  try { assert.deepEqual(await api.login("STU001", PASSWORD), loginBody); } finally { globalThis.fetch = orig; }
  assert.equal(f.calls[0].url, "/api/auth/login");
  assert.equal(f.calls[0].init.method, "POST");
  assert.deepEqual(JSON.parse(f.calls[0].init.body), { user_id: "STU001", password: PASSWORD });
  assert.equal(f.calls[0].init.headers.Authorization, undefined);
});

test("sign-in sends no Authorization header even if an old token is still held", async () => {
  setSessionToken(TOKEN);
  const f = recorder(json(200, loginBody));
  await request("/api/auth/login", { method: "POST", body: {}, auth: false, fetchImpl: f });
  assert.equal(f.calls[0].init.headers.Authorization, undefined);
});

test("protected requests carry the bearer token once it is set, and none before", async () => {
  const f = recorder(json(200, {}));
  await request("/api/memory/STU001", { fetchImpl: f });
  assert.equal(f.calls[0].init.headers.Authorization, undefined);
  setSessionToken(TOKEN);
  assert.equal(hasSessionToken(), true);
  await request("/api/memory/STU001", { fetchImpl: f });
  await request("/api/chat", { method: "POST", body: { message: "hi" }, fetchImpl: f });
  assert.equal(f.calls[1].init.headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(f.calls[2].init.headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(f.calls[2].init.headers["Content-Type"], "application/json");
});

test("every protected wrapper sends the token; only login and health are exempt by design", async () => {
  setSessionToken(TOKEN);
  const f = recorder(json(200, {}));
  const orig = globalThis.fetch; globalThis.fetch = f;
  try {
    await api.me(); await api.logout(); await api.chat({ message: "hi" }); await api.trace("a".repeat(32)); await api.memory("STU001");
    await api.deleteMemory("STU001", "preferred_name"); await api.reports(); await api.report("x.json"); await api.contact("STU001");
    await api.approvals("pending"); await api.approval("APR-1"); await api.approve("APR-1", "ok"); await api.reject("APR-1"); await api.cancelApproval("APR-1");
  } finally { globalThis.fetch = orig; }
  assert.equal(f.calls.length, 14);
  assert.ok(f.calls.every((c) => c.init.headers.Authorization === `Bearer ${TOKEN}`));
});

test("a 401 on a signed-in request ends the session: token dropped, handler told once, readable error", async () => {
  setSessionToken(TOKEN);
  let told = 0; setUnauthorizedHandler(() => { told += 1; });
  await assert.rejects(request("/api/chat", { fetchImpl: recorder(json(401, { detail: "Not authenticated." })) }),
    (e) => e instanceof ApiError && e.kind === "unauthorized" && e.status === 401 && /session has expired/i.test(e.message));
  assert.equal(told, 1); assert.equal(hasSessionToken(), false);
});

test("several requests failing together tell the handler only once", async () => {
  setSessionToken(TOKEN);
  let told = 0; setUnauthorizedHandler(() => { told += 1; });
  const f = recorder(json(401, { detail: "Not authenticated." }));
  const results = await Promise.allSettled([request("/a", { fetchImpl: f }), request("/b", { fetchImpl: f }), request("/c", { fetchImpl: f })]);
  assert.ok(results.every((r) => r.status === "rejected" && r.reason.kind === "unauthorized"));
  assert.equal(told, 1);
});

test("a 401 from a SIGN-IN attempt is just wrong credentials: no session-expired handling, server message kept", async () => {
  let told = 0; setUnauthorizedHandler(() => { told += 1; });
  await assert.rejects(request("/api/auth/login", { method: "POST", body: {}, auth: false, fetchImpl: recorder(json(401, { detail: "Invalid credentials." })) }),
    (e) => e.kind === "http" && e.status === 401 && e.message === "Invalid credentials.");
  assert.equal(told, 0);
});

test("a 401 on a request that carried no token does not call the handler", async () => {
  let told = 0; setUnauthorizedHandler(() => { told += 1; });
  await assert.rejects(request("/api/chat", { fetchImpl: recorder(json(401, { detail: "Not authenticated." })) }), (e) => e.status === 401 && e.kind === "http");
  assert.equal(told, 0);
});

test("a 403, a 404, a 503 and a network failure do NOT end the session", async () => {
  setSessionToken(TOKEN);
  let told = 0; setUnauthorizedHandler(() => { told += 1; });
  for (const status of [403, 404, 409, 503]) {
    await assert.rejects(request("/x", { fetchImpl: recorder(json(status, { detail: "no" })) }), (e) => e.kind === "http" && e.status === status);
  }
  await assert.rejects(request("/x", { fetchImpl: async () => { throw new TypeError("fetch failed"); } }), (e) => e.kind === "network");
  assert.equal(told, 0); assert.equal(hasSessionToken(), true);
});

test("a token replaced while a request was in flight is not dropped by the late 401", async () => {
  setSessionToken(TOKEN);
  let told = 0; setUnauthorizedHandler(() => { told += 1; });
  const slow = async () => { setSessionToken("tok-NEW-0123456789abcdefghijklmnopqrstuvwx"); return json(401, { detail: "Not authenticated." }); };
  await assert.rejects(request("/api/chat", { fetchImpl: slow }), (e) => e.kind === "unauthorized");
  assert.equal(told, 0); assert.equal(hasSessionToken(), true);          // the newer sign-in survives
});

test("errors the client builds itself (expired session, network, timeout, unreadable reply) never contain the token or the password", async () => {
  setSessionToken(TOKEN);
  const body = { user_id: "u", password: PASSWORD };
  const abortErr = Object.assign(new Error(`aborted ${TOKEN}`), { name: "AbortError" });
  const attempts = [
    request("/x", { method: "POST", body, fetchImpl: recorder(json(401, { detail: "Not authenticated." })) }),
    request("/x", { method: "POST", body, fetchImpl: async () => { throw new TypeError(`fetch failed ${TOKEN} ${PASSWORD}`); } }),
    request("/x", { method: "POST", body, fetchImpl: async () => { throw abortErr; } }),
    request("/x", { method: "POST", body, fetchImpl: async () => ({ ok: true, status: 200, text: async () => `<html>${TOKEN}</html>` }) }),
  ];
  for (const attempt of attempts) {
    const e = await attempt.catch((x) => x);
    assert.ok(e instanceof ApiError, String(e));
    assert.ok(!e.message.includes(TOKEN) && !e.message.includes(PASSWORD) && !e.message.includes("Bearer"), e.message);
    assert.ok(!JSON.stringify({ ...e }).includes(TOKEN) && !JSON.stringify({ ...e }).includes(PASSWORD));
  }
});

test("the token is never written to browser storage or the console", async () => {
  const writes = [];
  globalThis.localStorage = { setItem: (...a) => writes.push(a) }; globalThis.sessionStorage = { setItem: (...a) => writes.push(a) };
  const logged = []; const origLog = console.log, origErr = console.error, origWarn = console.warn;
  console.log = (...a) => logged.push(a); console.error = (...a) => logged.push(a); console.warn = (...a) => logged.push(a);
  try {
    setSessionToken(TOKEN);
    await request("/x", { fetchImpl: recorder(json(200, {})) });
    await request("/x", { fetchImpl: recorder(json(401, {})) }).catch(() => {});
  } finally { console.log = origLog; console.error = origErr; console.warn = origWarn; delete globalThis.localStorage; delete globalThis.sessionStorage; }
  assert.deepEqual(writes, []); assert.deepEqual(logged, []);
});

test("chat requests carry no student id: identity comes from the token", async () => {
  setSessionToken(TOKEN);
  const f = recorder(json(200, {}));
  const orig = globalThis.fetch; globalThis.fetch = f;
  try { await api.chat({ message: "hi", sessionId: null }); await api.chat({ message: "hi", sessionId: "s-1" }); } finally { globalThis.fetch = orig; }
  assert.deepEqual(JSON.parse(f.calls[0].init.body), { message: "hi" });
  assert.deepEqual(JSON.parse(f.calls[1].init.body), { message: "hi", session_id: "s-1" });
});

test("sign-in and sign-out in the store hold only the user, never a token, and sign-out forgets everything shown", () => {
  const s = createStore();
  const events = []; s.subscribe((w) => events.push(w));
  s.setUser({ ...loginBody.user, token: TOKEN, password: PASSWORD });         // even if extra fields were passed in
  assert.deepEqual(s.state.user, { user_id: "STU001", role: "student", student_id: "STU001", display_name: "Student 1" });
  assert.equal(s.state.studentId, "STU001");
  assert.ok(!JSON.stringify(s.state).includes(TOKEN) && !JSON.stringify(s.state).includes(PASSWORD));
  s.completeTurn(s.addTurn("What is my fee status?"), { session_id: "abc", response: "…" });
  s.signOut();
  assert.deepEqual([s.state.user, s.state.studentId, s.state.turns.length, s.state.sessionId, s.state.selectedTurnId, s.state.busy], [null, "", 0, null, null, false]);
  assert.deepEqual(events.filter((e) => e === "signin" || e === "signout"), ["signin", "signout"]);
});

test("a staff user has no student id and so no student data scope", () => {
  const s = createStore();
  s.setUser({ user_id: "STAFF01", role: "staff", student_id: null, display_name: "Staff One" });
  assert.equal(s.state.studentId, ""); assert.equal(s.state.user.role, "staff");
});
