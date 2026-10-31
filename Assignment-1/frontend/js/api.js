// The only module that talks to the backend. Same-origin requests to the existing FastAPI endpoints.
import { parseApiError } from "./format.js";

export class ApiError extends Error {
  constructor(message, { status = 0, kind = "http" } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.kind = kind; // "network" | "timeout" | "http" | "parse"
  }
}

export async function request(path, { method = "GET", body, timeoutMs = 30000, fetchImpl = globalThis.fetch } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let res;
  try {
    res = await fetchImpl(path, {
      method,
      headers: body === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" },
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
  if (!res.ok) throw new ApiError(parseApiError(res.status, data), { status: res.status, kind: "http" });
  if (data === null) throw new ApiError("The server sent a response that could not be read.", { status: res.status, kind: "parse" });
  return data;
}

const enc = encodeURIComponent;

export const api = {
  health: () => request("/api/health", { timeoutMs: 8000 }),
  chat: ({ message, studentId, sessionId }) => request("/api/chat", {
    method: "POST", timeoutMs: 90000,
    body: { message, student_id: studentId, ...(sessionId ? { session_id: sessionId } : {}) },
  }),
  trace: (traceId) => request(`/api/traces/${enc(traceId)}`),
  memory: (studentId) => request(`/api/memory/${enc(studentId)}`),
  deleteMemory: (studentId, key) => request(`/api/memory/${enc(studentId)}/${enc(key)}`, { method: "DELETE" }),
  reports: () => request("/api/evaluation/reports"),
  report: (name) => request(`/api/evaluation/reports/${enc(name)}`),
};
