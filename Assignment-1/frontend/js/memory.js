// Memory view: GET /api/memory/{student_id} and DELETE /api/memory/{student_id}/{key}.
import { api } from "./api.js";
import { announce, clear, emptyBlock, errorBlock, h, loadingBlock } from "./dom.js";
import { formatTimestamp } from "./format.js";

export function initMemory({ store }) {
  const root = document.getElementById("panel-memory");
  let stale = true;
  let token = 0;
  let confirming = null; // key awaiting delete confirmation

  function header(studentId) {
    return h("div", { class: "toolbar" },
      h("p", { class: "muted" }, "Saved preferences for ", h("strong", {}, studentId || "—"), " — kept across sessions."),
      h("button", { type: "button", class: "btn btn-secondary btn-sm", onclick: () => refresh() }, "Refresh"));
  }

  function row(studentId, m) {
    const isConfirming = confirming === m.key;
    const actions = isConfirming
      ? h("span", { class: "confirm" },
        h("span", { class: "muted small" }, "Delete?"),
        h("button", { type: "button", class: "btn btn-danger btn-sm", onclick: () => remove(studentId, m.key) }, "Confirm", h("span", { class: "sr-only" }, ` delete ${m.key}`)),
        h("button", { type: "button", class: "btn btn-secondary btn-sm", onclick: () => { confirming = null; refresh(true); } }, "Cancel"))
      : h("button", { type: "button", class: "btn btn-secondary btn-sm", "aria-label": `Delete memory ${m.key}`,
        onclick: (e) => { confirming = m.key; refresh(true).then(() => root.querySelector(".confirm .btn-danger")?.focus()); e.preventDefault(); } }, "Delete");
    return h("tr", {}, h("th", { scope: "row" }, h("code", {}, m.key)), h("td", {}, m.value), h("td", {}, formatTimestamp(m.created_at)), h("td", { class: "actions" }, actions));
  }

  let lastData = null;
  function paint(studentId, data) {
    clear(root).append(header(studentId));
    const items = data.memories || [];
    if (!items.length) {
      root.append(emptyBlock("Nothing remembered yet",
        "When you state a lasting preference (for example “Please call me Dev from now on”) the agent can save it here. " +
        "The server rejects passwords, payment details, health information, amounts and temporary information."));
      return;
    }
    root.append(h("div", { class: "table-wrap", role: "region", "aria-label": "Saved memory", tabindex: "0" },
      h("table", { class: "table" },
        h("caption", { class: "sr-only" }, `Saved memory for ${studentId}`),
        h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Key"), h("th", { scope: "col" }, "Value"), h("th", { scope: "col" }, "Saved"), h("th", { scope: "col" }, h("span", { class: "sr-only" }, "Actions")))),
        h("tbody", {}, items.map((m) => row(studentId, m))))));
  }

  async function refresh(useLast = false) {
    const mine = ++token;
    const studentId = store.state.studentId.trim();
    stale = false;
    if (!studentId) { clear(root).append(emptyBlock("Enter a student ID", "Memory is stored per student.")); return; }
    if (useLast && lastData) { paint(studentId, lastData); return; }
    clear(root).append(header(studentId), loadingBlock("Loading memory…"));
    try {
      const data = await api.memory(studentId);
      if (mine !== token) return;
      lastData = data;
      paint(data.student_id || studentId, data);
    } catch (err) {
      if (mine !== token) return;
      lastData = null;
      clear(root).append(header(studentId), errorBlock(err.status === 404 ? `No student found with ID “${studentId}”.` : `Could not load memory: ${err.message}`, () => refresh()));
    }
  }

  async function remove(studentId, key) {
    try {
      const res = await api.deleteMemory(studentId, key);
      confirming = null;
      announce(res.deleted ? `Deleted memory ${key}.` : `Memory ${key} was already gone.`);
      await refresh();
    } catch (err) {
      confirming = null;
      clear(root).append(header(studentId), errorBlock(`Could not delete: ${err.message}`, () => refresh()));
    }
  }

  store.subscribe((what) => { if (what === "student" || what === "reset") { stale = true; lastData = null; confirming = null; if (!root.hidden) refresh(); } });

  return {
    refresh,
    markStale() { stale = true; },
    // called when the tab becomes visible or after the agent saved something
    ensureFresh() { if (stale) return refresh(); },
    isStale: () => stale,
  };
}
