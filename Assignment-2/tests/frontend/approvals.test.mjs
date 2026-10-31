import test, { afterEach } from "node:test";
import assert from "node:assert/strict";
import { ApiError, api, clearSessionToken, setSessionToken } from "../../frontend/js/api.js";
import { STATUSES, approvalOf, canAct, decisionSummary, describeActionError, fieldLabel, listStatuses, proposedValue, statusInfo, successMessage } from "../../frontend/js/approvalLogic.js";

afterEach(() => clearSessionToken());
const student = { user_id: "STU001", role: "student", student_id: "STU001" };
const other = { user_id: "STU002", role: "student", student_id: "STU002" };
const staff = { user_id: "STAFF01", role: "staff", student_id: null };
const pending = { approval_id: "APR-0123456789AB", student_id: "STU001", status: "pending" };
const err = (status, message, kind = "http") => new ApiError(message, { status, kind });

test("each status has a label, a tone and an explanation; an unknown status is shown as is, not hidden", () => {
  for (const s of STATUSES) { const i = statusInfo(s); assert.ok(i.label && i.tone && i.help, s); }
  assert.equal(statusInfo("pending").tone, "warning");
  assert.equal(statusInfo("executed").tone, "success");
  assert.equal(statusInfo("rejected").tone, "danger");
  assert.deepEqual(statusInfo("weird"), { label: "weird", tone: "neutral", help: "" });
  assert.equal(statusInfo(undefined).label, "Unknown");
});

test("expired, rejected, withdrawn and failed requests all say that nothing was changed", () => {
  for (const s of ["pending", "rejected", "cancelled", "expired", "failed"]) assert.match(statusInfo(s).help, /Nothing/i, s);
});

test("field names and values: owners see their value, staff only the masked form the server sends", () => {
  assert.equal(fieldLabel("email"), "Email address"); assert.equal(fieldLabel("phone"), "Phone number"); assert.equal(fieldLabel(undefined), "—");
  assert.deepEqual(proposedValue({ proposed_value: "dev@example.edu" }), { text: "dev@example.edu", masked: false });
  assert.deepEqual(proposedValue({ proposed_value_masked: "d***@example.edu" }), { text: "d***@example.edu", masked: true });
  assert.deepEqual(proposedValue({}), { text: "—", masked: false });
  assert.deepEqual(proposedValue(null), { text: "—", masked: false });
});

test("buttons: only staff are offered Approve/Reject, only on pending requests; only the owner is offered Cancel", () => {
  assert.deepEqual(canAct(staff, pending), { decide: true, cancel: false });
  assert.deepEqual(canAct(student, pending), { decide: false, cancel: true });          // a student is never offered Approve for their own request
  assert.deepEqual(canAct(other, pending), { decide: false, cancel: false });
  for (const s of ["approved", "executed", "rejected", "cancelled", "expired", "failed"]) {
    assert.deepEqual(canAct(staff, { ...pending, status: s }), { decide: false, cancel: false }, s);
    assert.deepEqual(canAct(student, { ...pending, status: s }), { decide: false, cancel: false }, s);
  }
  assert.deepEqual(canAct(null, pending), { decide: false, cancel: false });
  assert.deepEqual(canAct(staff, undefined), { decide: false, cancel: false });
});

test("filters: staff open on the pending queue; students see all of their own requests", () => {
  assert.equal(listStatuses(staff).initial, "pending");
  assert.equal(listStatuses(student).initial, "");
  assert.ok(listStatuses(student).options.includes("") && !listStatuses(staff).options.includes(""));
});

test("action errors are readable and name the real reason", () => {
  assert.match(describeActionError(err(403, "Access denied."), "approve"), /not allowed to approve.*Only staff/);
  assert.match(describeActionError(err(403, "Access denied."), "reject"), /not allowed to reject/);
  assert.match(describeActionError(err(404, "Approval request not found.")), /not found/i);
  const conflict = describeActionError(err(409, "This request has expired."), "approve");
  assert.match(conflict, /This request has expired\./); assert.match(conflict, /Nothing was changed/);
  assert.match(describeActionError(err(409, "This request failed an integrity check and was not executed.")), /integrity check/);
  assert.match(describeActionError(err(503, "The audit log is unavailable, so the request was not performed.")), /audit log is unavailable/);
  assert.match(describeActionError(err(0, "Could not reach the server.", "network"), "approve"), /Could not approve.*Could not reach the server/);
  assert.equal(describeActionError(err(401, "Your session has expired or was ended. Please sign in again.", "unauthorized")), "Your session has expired or was ended. Please sign in again.");
  assert.match(describeActionError(err(500, "The approved action could not be completed.")), /could not be completed/);
});

test("success messages never claim more than the server said", () => {
  assert.equal(successMessage("approve", { executed: true }), "Approved: the change was applied.");
  assert.match(successMessage("approve", { executed: false }), /not been applied/);
  assert.match(successMessage("reject", { executed: false }), /nothing was changed/);
  assert.match(successMessage("cancel", {}), /withdrawn/);
});

test("approvalOf unwraps the approve/reject/cancel responses and passes a plain approval through", () => {
  assert.deepEqual(approvalOf({ approval: pending, executed: true }), pending);
  assert.deepEqual(approvalOf(pending), pending);
  assert.equal(approvalOf(null), null);
});

test("approval endpoints use the real routes, methods and bodies, and the signed-in token", async () => {
  setSessionToken("tok-eval-0123456789abcdefghijklmnopqrstuv");
  const calls = [];
  const orig = globalThis.fetch;
  globalThis.fetch = async (url, init) => { calls.push({ url, init }); return { ok: true, status: 200, text: async () => "{}" }; };
  try {
    await api.approvals(); await api.approvals("pending"); await api.approval("APR-0123456789AB");
    await api.approve("APR-0123456789AB", "identity checked"); await api.approve("APR-0123456789AB");
    await api.reject("APR-0123456789AB", "cannot verify"); await api.cancelApproval("APR-0123456789AB");
    await api.contact("STU001"); await api.approvals("a b&c");
  } finally { globalThis.fetch = orig; }
  assert.deepEqual(calls.map((c) => `${c.init.method} ${c.url}`), [
    "GET /api/approvals", "GET /api/approvals?status=pending", "GET /api/approvals/APR-0123456789AB",
    "POST /api/approvals/APR-0123456789AB/approve", "POST /api/approvals/APR-0123456789AB/approve",
    "POST /api/approvals/APR-0123456789AB/reject", "POST /api/approvals/APR-0123456789AB/cancel",
    "GET /api/students/STU001/contact", "GET /api/approvals?status=a%20b%26c"]);
  assert.deepEqual(JSON.parse(calls[3].init.body), { reason: "identity checked" });
  assert.deepEqual(JSON.parse(calls[4].init.body), {});
  assert.deepEqual(JSON.parse(calls[5].init.body), { reason: "cannot verify" });
  assert.equal(calls[6].init.body, undefined);
  assert.ok(calls.every((c) => c.init.headers.Authorization.startsWith("Bearer ")));
});

test("decision line: who decided or withdrew, the reason and when it was applied; nothing before any decision", () => {
  const f = (x) => `@${x}`;
  assert.equal(decisionSummary(pending, f), null);
  assert.equal(decisionSummary(null, f), null);
  assert.equal(decisionSummary({ status: "executed", decided_by: "STAFF01", decided_at: "t1", decision_reason: "checked", executed_at: "t2" }, f), "Decided by STAFF01 · @t1 · Reason: checked · Applied @t2");
  assert.equal(decisionSummary({ status: "rejected", decided_by: "STAFF01", decided_at: "t1" }, f), "Decided by STAFF01 · @t1");
  assert.equal(decisionSummary({ status: "cancelled", decided_by: "STU001", decided_at: "t1" }, f), "Withdrawn by STU001 · @t1");     // not "decided"
});
