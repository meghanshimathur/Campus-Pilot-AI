// The only module that talks to the backend. Same-origin requests to the existing FastAPI endpoints.
//
// Sign-in state lives here and nowhere else: the bearer token is a module variable (memory only, never written to
// localStorage/sessionStorage, never put in the store, the DOM, an error message or a log). Every request except sign-in
// sends it. If the server refuses a request that carried a token with HTTP 401 (expired, revoked or unknown), the token is
// dropped and one registered handler is told, so the page can go back to the sign-in screen. The server stays the authority:
// nothing here decides what a user may do.
import { parseApiError } from "./format.js";

const session = { token: null, onUnauthorized: null };

export function setSessionToken(token) { session.token = typeof token === "string" && token ? token : null; }
export function clearSessionToken() { session.token = null; }
export function hasSessionToken() { return session.token !== null; }
export function setUnauthorizedHandler(fn) { session.onUnauthorized = typeof fn === "function" ? fn : null; }

export class ApiError extends Error {
  constructor(message, { status = 0, kind = "http" } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.kind = kind; // "network" | "timeout" | "http" | "parse" | "unauthorized" (a signed-in request was refused with 401)
  }
}

export async function request(path, { method = "GET", body, timeoutMs = 30000, fetchImpl = globalThis.fetch, auth = true } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const sentToken = auth ? session.token : null;           // the token this request was signed with (may change while it is in flight)
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (sentToken) headers.Authorization = `Bearer ${sentToken}`;
  let res;
  try {
    res = await fetchImpl(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (err) {
    if (err && err.name === "AbortError") throw new ApiError("The request took too long and was cancelled. Please try again.", { kind: "timeout" });
    throw new ApiError("Could not reach the server. Check that it is running, then try again.", { kind: "network" });
  } finally {
    clearTimeout(timer);
  }
  let data = null;
  const text = await res.text();
  if (text) {
    try { data = JSON.parse(text); } catch { data = null; }
  }
  if (res.status === 401 && sentToken) {
    // Only a refused SIGNED-IN request means "session over". A failed sign-in (auth: false) is just wrong credentials.
    // Several requests can fail together: only the one that still holds the live token ends the session.
    if (session.token === sentToken) {
      session.token = null;
      if (session.onUnauthorized) session.onUnauthorized();
    }
    throw new ApiError("Your session has expired or was ended. Please sign in again.", { status: 401, kind: "unauthorized" });
  }
  if (!res.ok) throw new ApiError(parseApiError(res.status, data), { status: res.status, kind: "http" });
  if (data === null) throw new ApiError("The server sent a response that could not be read.", { status: res.status, kind: "parse" });
  return data;
}

const enc = encodeURIComponent;

export const api = {
  health: () => request("/api/health", { timeoutMs: 8000 }),
  // Sign-in: credentials go in the body once, over the same-origin request, and are not kept. No Authorization header.
  login: (userId, password) => request("/api/auth/login", { method: "POST", body: { user_id: userId, password }, auth: false, timeoutMs: 15000 }),
  me: () => request("/api/auth/me"),
  logout: () => request("/api/auth/logout", { method: "POST", timeoutMs: 8000 }),
  // The student is always the signed-in one (identity comes from the token), so the body carries no student id.
  chat: ({ message, sessionId }) => request("/api/chat", {
    method: "POST", timeoutMs: 90000,
    body: { message, ...(sessionId ? { session_id: sessionId } : {}) },
  }),
  trace: (traceId) => request(`/api/traces/${enc(traceId)}`),
  memory: (studentId) => request(`/api/memory/${enc(studentId)}`),
  deleteMemory: (studentId, key) => request(`/api/memory/${enc(studentId)}/${enc(key)}`, { method: "DELETE" }),
  reports: () => request("/api/evaluation/reports"),
  report: (name) => request(`/api/evaluation/reports/${enc(name)}`),
  // Sensitive-action approvals (Batch 4 API). Who may do what is decided by the server.
  contact: (studentId) => request(`/api/students/${enc(studentId)}/contact`),
  approvals: (status) => request(`/api/approvals${status ? `?status=${enc(status)}` : ""}`),
  approval: (id) => request(`/api/approvals/${enc(id)}`),
  approve: (id, reason) => request(`/api/approvals/${enc(id)}/approve`, { method: "POST", body: reason ? { reason } : {} }),
  reject: (id, reason) => request(`/api/approvals/${enc(id)}/reject`, { method: "POST", body: reason ? { reason } : {} }),
  cancelApproval: (id) => request(`/api/approvals/${enc(id)}/cancel`, { method: "POST" }),
};
