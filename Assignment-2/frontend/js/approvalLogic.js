// Pure helpers for the Approvals view: no DOM, no network (unit-tested under Node: tests/frontend/approvals.test.mjs).
// Everything here is presentation. Whether a request may be approved, rejected or cancelled is decided by the server;
// `canAct` only decides which buttons to SHOW, and the server refuses anything the user is not allowed to do.

export const STATUSES = ["pending", "approved", "executed", "rejected", "cancelled", "expired", "failed"];

const STATUS = {
  pending: { label: "Pending approval", tone: "warning", help: "Waiting for a staff decision. Nothing has been changed." },
  approved: { label: "Approved, not yet applied", tone: "info", help: "Approved, but the change has not been applied yet." },
  executed: { label: "Approved and applied", tone: "success", help: "Approved by staff and applied once." },
  rejected: { label: "Rejected", tone: "danger", help: "Staff rejected this request. Nothing was changed." },
  cancelled: { label: "Withdrawn", tone: "neutral", help: "The student withdrew this request. Nothing was changed." },
  expired: { label: "Expired", tone: "neutral", help: "Nobody decided in time. Nothing was changed." },
  failed: { label: "Could not be applied", tone: "danger", help: "The request failed a safety check or could not be applied. Nothing was changed." },
};

export function statusInfo(status) {
  return STATUS[status] ?? { label: status ? String(status) : "Unknown", tone: "neutral", help: "" };
}

const FIELDS = { email: "Email address", phone: "Phone number" };
export function fieldLabel(field) { return FIELDS[field] ?? (field ? String(field) : "—"); }

// The owner is shown their own proposed value; staff only ever get a masked form from the server. Show whichever the API sent.
export function proposedValue(approval) {
  if (approval && typeof approval.proposed_value === "string") return { text: approval.proposed_value, masked: false };
  if (approval && typeof approval.proposed_value_masked === "string") return { text: approval.proposed_value_masked, masked: true };
  return { text: "—", masked: false };
}

// Which actions to offer. UI affordance only: a student who calls the approve endpoint anyway gets HTTP 403 from the server.
export function canAct(user, approval) {
  const pending = approval?.status === "pending";
  return {
    decide: Boolean(user && user.role === "staff" && pending),
    cancel: Boolean(user && user.role === "student" && pending && approval.student_id === user.student_id),
  };
}

export function listStatuses(user) {
  // staff open on the pending queue; a student sees all of their own requests unless they filter
  return user?.role === "staff" ? { initial: "pending", options: STATUSES } : { initial: "", options: ["", ...STATUSES] };
}

const VERB_DONE = { approve: "approve", reject: "reject", cancel: "withdraw" };

// One readable sentence for a failed action. Server messages for these routes are fixed strings with no personal data.
export function describeActionError(err, verb = "approve") {
  const status = err?.status ?? 0;
  const what = VERB_DONE[verb] ?? verb;
  if (err?.kind === "unauthorized") return err.message;
  if (err?.kind === "network" || err?.kind === "timeout") return `Could not ${what} the request: ${err.message}`;
  if (status === 403) return `You are not allowed to ${what} this request. Only staff can approve or reject requests.`;
  if (status === 404) return "That request was not found. It may belong to someone else or no longer exist.";
  if (status === 409) return `${err.message} Nothing was changed. Refresh the list to see its current status.`;
  if (status === 503) return `${err.message}`;
  return `Could not ${what} the request: ${err?.message ?? "unknown error"}`;
}

export function successMessage(verb, result) {
  if (verb === "approve") {
    return result?.executed ? "Approved: the change was applied." : "Approved. The change has not been applied yet.";
  }
  if (verb === "reject") return "Rejected: nothing was changed.";
  if (verb === "cancel") return "Request withdrawn: nothing was changed.";
  return "Done.";
}

// The approve/reject response is {approval, executed}; cancel returns {approval}; list/get return the approval itself.
export function approvalOf(result) { return result && typeof result === "object" && result.approval ? result.approval : result; }

// One line about what happened to a request after it was made, from the fields the API returns (null if nothing yet).
// A withdrawn request was ended by the student, not decided by staff, so it is worded that way.
export function decisionSummary(a, format = (x) => x) {
  if (!a || (!a.decided_by && !a.executed_at)) return null;
  const parts = [];
  if (a.decided_by) parts.push(`${a.status === "cancelled" ? "Withdrawn by" : "Decided by"} ${a.decided_by} · ${format(a.decided_at)}`);
  if (a.decision_reason) parts.push(`Reason: ${a.decision_reason}`);
  if (a.executed_at) parts.push(`Applied ${format(a.executed_at)}`);
  return parts.join(" · ");
}
