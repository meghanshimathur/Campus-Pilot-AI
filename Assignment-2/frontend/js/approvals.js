// Approvals view: students see their own change requests (and can withdraw a pending one); staff review the pending queue and
// approve or reject. Talks to the Batch 4 routes through api.js; the SERVER decides who may do what, when a request has expired,
// whether it was tampered with and whether it already ran. This view only shows the answer. A student who is somehow offered a
// button would still get HTTP 403, and the buttons are only shown to the roles the server accepts.
import { api } from "./api.js";
import { announce, append, badge, clear, emptyBlock, errorBlock, h, kv, loadingBlock } from "./dom.js";
import { formatTimestamp } from "./format.js";
import { approvalOf, canAct, decisionSummary, describeActionError, fieldLabel, listStatuses, proposedValue, statusInfo, successMessage } from "./approvalLogic.js";

export function initApprovals({ store }) {
  const root = document.getElementById("panel-approvals");
  let token = 0;               // a newer request makes older answers obsolete
  let loaded = false;
  let filter = null;           // null = not chosen yet (the role decides the starting filter)
  let rows = [];
  let contact = null;          // student only: {email, phone, updated_at} or {error}
  let message = null;          // {tone, text} shown above the list
  let confirming = null;       // {id, verb}: an action waiting for its confirming click
  let working = null;          // id of the request an action is running on
  const drafts = {};           // reason text typed per request, kept across re-renders

  const user = () => store.state.user;

  function statusBadge(status) {
    const info = statusInfo(status);
    return badge(info.label, info.tone, info.help);
  }

  function messageBlock() {
    if (!message) return null;
    return h("div", { class: `alert alert-${message.tone === "danger" ? "danger" : "success"}`, tabindex: "-1", role: message.tone === "danger" ? "alert" : "status" }, message.text);
  }

  function filterControl() {
    const { options } = listStatuses(user());
    const select = h("select", { id: "approval-filter", "aria-label": "Show requests with status" },
      options.map((s) => h("option", { value: s, selected: s === filter }, s === "" ? "All my requests" : statusInfo(s).label)));
    select.addEventListener("change", () => { filter = select.value; message = null; refresh(); });
    return h("div", { class: "toolbar" },
      h("div", { class: "turn-picker" }, h("label", { for: "approval-filter" }, "Show"), select),
      h("button", { type: "button", class: "btn btn-secondary btn-sm", onclick: () => { message = null; refresh(); } }, "Refresh"));
  }

  // ---- one action (approve / reject / cancel), with a confirming second click
  async function run(id, verb) {
    working = id;
    confirming = null;
    paint();
    try {
      const result = verb === "approve" ? await api.approve(id, drafts[id]?.trim()) : verb === "reject" ? await api.reject(id, drafts[id]?.trim()) : await api.cancelApproval(id);
      message = { tone: "success", text: `${successMessage(verb, result)} (${approvalOf(result)?.approval_id ?? id})` };
      announce(message.text);
      delete drafts[id];
    } catch (err) {
      if (err.kind === "unauthorized") return;               // the page is going back to the sign-in screen
      message = { tone: "danger", text: describeActionError(err, verb) };
      announce(message.text);
    } finally {
      working = null;
    }
    await refresh();                                         // always show the server's current state, whatever happened
    root.querySelector(".alert[tabindex]")?.focus();
  }

  function actionButtons(a) {
    const can = canAct(user(), a);
    if (!can.decide && !can.cancel) return null;
    const busy = working !== null;
    if (confirming && confirming.id === a.approval_id) {
      const { verb } = confirming;
      const sentence = { approve: "Approve this request? The change is applied right away.", reject: "Reject this request? Nothing will be changed.", cancel: "Withdraw this request? Nothing will be changed." }[verb];
      return h("div", { class: "confirm", role: "group", "aria-label": "Confirm" },
        h("span", { class: "muted small" }, sentence),
        h("button", { type: "button", class: verb === "approve" ? "btn btn-primary btn-sm" : "btn btn-danger btn-sm", disabled: busy, onclick: () => run(a.approval_id, verb) },
          { approve: "Confirm approve", reject: "Confirm reject", cancel: "Confirm withdraw" }[verb]),
        h("button", { type: "button", class: "btn btn-secondary btn-sm", onclick: () => { confirming = null; paint(); } }, "Keep as is"));
    }
    const ask = (verb) => () => { confirming = { id: a.approval_id, verb }; paint(); root.querySelector(".confirm .btn")?.focus(); };
    return can.decide
      ? h("div", { class: "action-row" },
        h("button", { type: "button", class: "btn btn-primary btn-sm", disabled: busy, onclick: ask("approve") }, "Approve", h("span", { class: "sr-only" }, ` request ${a.approval_id}`)),
        h("button", { type: "button", class: "btn btn-secondary btn-sm", disabled: busy, onclick: ask("reject") }, "Reject", h("span", { class: "sr-only" }, ` request ${a.approval_id}`)))
      : h("div", { class: "action-row" },
        h("button", { type: "button", class: "btn btn-secondary btn-sm", disabled: busy, onclick: ask("cancel") }, "Withdraw", h("span", { class: "sr-only" }, ` request ${a.approval_id}`)));
  }

  function decisionLine(a) {
    const text = decisionSummary(a, formatTimestamp);
    return text ? h("p", { class: "muted small" }, text) : null;
  }

  // ---- staff: cards, so the reason box and the confirm step have room
  function staffCard(a) {
    const value = proposedValue(a);
    const can = canAct(user(), a);
    return h("article", { class: "card approval-card", "aria-label": `Request ${a.approval_id}` },
      h("div", { class: "card-head" }, h("code", {}, a.approval_id), statusBadge(a.status), h("span", { class: "muted small" }, `Student ${a.student_id}`)),
      h("dl", { class: "summary-grid" },
        kv("Change", fieldLabel(a.field)),
        kv(value.masked ? "Proposed value (masked)" : "Proposed value", value.text),
        kv("Requested", formatTimestamp(a.created_at)),
        kv("Expires", formatTimestamp(a.expires_at))),
      decisionLine(a),
      can.decide ? h("div", { class: "field" },
        h("label", { for: `reason-${a.approval_id}` }, "Reason (optional, kept in the audit trail)"),
        h("textarea", { id: `reason-${a.approval_id}`, rows: 2, maxlength: 200, oninput: (e) => { drafts[a.approval_id] = e.target.value; } }, drafts[a.approval_id] ?? "")) : null,
      actionButtons(a));
  }

  // ---- student: the same cards, without the reason box (a student can only withdraw a pending request)
  function studentCard(a) {
    return h("article", { class: "card approval-card", "aria-label": `Request ${a.approval_id}` },
      h("div", { class: "card-head" }, h("code", {}, a.approval_id), statusBadge(a.status)),
      h("dl", { class: "summary-grid" },
        kv("Change", fieldLabel(a.field)),
        kv("Proposed value", proposedValue(a).text),
        kv("Requested", formatTimestamp(a.created_at)),
        kv("Expires", formatTimestamp(a.expires_at))),
      decisionLine(a),
      actionButtons(a));
  }

  function contactCard() {
    if (!contact) return null;
    if (contact.error) return h("div", { class: "alert alert-warning", role: "note" }, `Could not load your current contact details: ${contact.error}`);
    return h("div", { class: "block" },
      h("h4", { class: "block-title" }, "Contact details on file"),
      h("dl", { class: "summary-grid" },
        kv("Email", contact.email ?? "not set"), kv("Phone", contact.phone ?? "not set"), kv("Last changed", contact.updated_at ? formatTimestamp(contact.updated_at) : "never")),
      h("p", { class: "muted small" }, "These change only when staff approve a request. Only you can see them here."));
  }

  function intro() {
    return user()?.role === "staff"
      ? h("p", { class: "muted" }, "Requests wait here for a staff decision. Proposed values are masked. The server checks your permission, the expiry and the request's integrity, and records every decision in the audit log; a rejected, expired or already-applied request can never be applied.")
      : h("p", { class: "muted" }, "To change your email or phone, ask the assistant in the chat (for example “Please update my phone number to …”). That only creates a request: nothing changes until a staff member approves it, and a request that is not decided in time expires.");
  }

  function paint() {
    if (!user()) { clear(root); return; }
    const staff = user().role === "staff";
    append(clear(root), [intro(), staff ? null : contactCard(), filterControl(), messageBlock()]);      // the helper skips nulls (native append would print "null")
    if (!rows.length) {
      root.append(staff
        ? emptyBlock(filter === "pending" ? "No pending requests" : "No requests with this status", filter === "pending" ? "When a student asks to change their contact details, the request appears here." : "")
        : emptyBlock(filter ? "No requests with this status" : "You have not made any requests", filter ? "" : "Ask the assistant in the chat to change your email or phone and it will appear here."));
      return;
    }
    root.append(h("div", { class: "approval-list" }, rows.map(staff ? staffCard : studentCard)));
  }

  async function refresh() {
    if (!user()) return;
    const mine = ++token;
    loaded = true;
    if (filter === null) filter = listStatuses(user()).initial;
    if (!rows.length) clear(root).append(loadingBlock("Loading requests…"));
    try {
      const wantContact = user().role === "student" && user().student_id;
      const [list, contactResult] = await Promise.all([
        api.approvals(filter),
        wantContact ? api.contact(user().student_id).catch((err) => ({ error: err.kind === "unauthorized" ? null : err.message })) : null,
      ]);
      if (mine !== token || !user()) return;
      rows = Array.isArray(list.approvals) ? list.approvals : [];
      contact = contactResult;
      paint();
    } catch (err) {
      if (mine !== token || err.kind === "unauthorized") return;
      loaded = false;
      rows = [];
      clear(root).append(errorBlock(`Could not load requests: ${err.message}`, refresh));
    }
  }

  // Sign-out / expired session: forget the list, any typed reasons and any half-finished action.
  function reset() {
    token += 1; loaded = false; filter = null; rows = []; contact = null; message = null; confirming = null; working = null;
    for (const k of Object.keys(drafts)) delete drafts[k];
    clear(root);
  }

  return { refresh, reset, markStale() { loaded = false; }, ensureFresh() { if (!loaded) return refresh(); } };
}
