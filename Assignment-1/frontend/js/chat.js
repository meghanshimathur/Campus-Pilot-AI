import { api } from "./api.js";
import { announce, badge, h } from "./dom.js";
import { formatMs, formatTokens, toolNames } from "./format.js";
import { renderMarkdown } from "./markdown.js";

const SUGGESTIONS = [
  "What is my current fee status?",
  "The payment portal failed. Can I pay on the next working day?",
  "Please call me Dev from now on.",
  "Where can I park my car on campus?",
  "When are Dr. Kavita Rao's office hours?",
];

export function initChat({ store, openInspector, onMemorySaved }) {
  const log = document.getElementById("chat-log");
  const empty = document.getElementById("chat-empty");
  const typing = document.getElementById("typing");
  const form = document.getElementById("composer");
  const input = document.getElementById("message");
  const sendBtn = document.getElementById("send");
  const sendLabel = sendBtn.querySelector(".btn-label");
  const counter = document.getElementById("char-count");
  const errorEl = document.getElementById("composer-error");

  const list = document.getElementById("suggestions");
  for (const text of SUGGESTIONS) {
    list.append(h("li", {}, h("button", { type: "button", class: "chip-btn", onclick: () => send(text) }, text)));
  }

  function showError(message) {
    errorEl.hidden = !message;
    errorEl.textContent = message || "";
    input.toggleAttribute("aria-invalid", Boolean(message));
  }

  function metaRow(turn) {
    const r = turn.response;
    const names = toolNames(r);
    return h("div", { class: "msg-meta" },
      names.length ? names.map((n) => badge(n, "info", "Tool called by the agent")) : badge("answered directly", "neutral", "No tool was called"),
      (r.memories_used || []).length ? badge(`memory used: ${r.memories_used.map((m) => m.key).join(", ")}`, "neutral") : null,
      (r.memories_saved || []).length ? badge(`memory saved: ${r.memories_saved.map((m) => m.key).join(", ")}`, "success") : null,
      r.error ? badge(`agent error: ${r.error}`, "danger") : null,
      h("span", { class: "meta-text" }, `${formatMs(r.latency_ms)} · tokens ${formatTokens(r.input_tokens, r.output_tokens)}`),
      h("span", { class: "meta-actions" },
        h("button", { type: "button", class: "link-btn", onclick: () => { store.selectTurn(turn.id); openInspector("evidence"); } }, "Evidence"),
        h("button", { type: "button", class: "link-btn", onclick: () => { store.selectTurn(turn.id); openInspector("activity"); } }, "Trace")));
  }

  function render() {
    const keepScroll = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
    for (const el of [...log.children]) if (el !== empty) el.remove();
    empty.hidden = store.state.turns.length > 0;
    for (const turn of store.state.turns) {
      log.append(h("article", { class: "msg msg-user", "aria-label": "You said" },
        h("div", { class: "msg-who" }, "You"), h("div", { class: "msg-body" }, turn.userText)));
      if (turn.response) {
        log.append(h("article", { class: "msg msg-agent", "aria-label": "CampusPilot replied" },
          h("div", { class: "msg-who" }, "CampusPilot"),
          // assistant text is Markdown (rendered safely: text nodes only); the user's own text stays literal
          h("div", { class: "msg-body" }, renderMarkdown(turn.response.response)), metaRow(turn)));
      } else if (turn.error) {
        log.append(h("article", { class: "msg msg-error", role: "alert", "aria-label": "Error" },
          h("div", { class: "msg-who" }, "Could not get a reply"),
          h("div", { class: "msg-body" }, turn.error.message),
          h("div", { class: "msg-meta" },
            h("button", { type: "button", class: "btn btn-secondary btn-sm", onclick: () => { store.removeTurn(turn); send(turn.userText); } }, "Retry"),
            h("button", { type: "button", class: "link-btn", onclick: () => store.removeTurn(turn) }, "Dismiss"))));
      }
    }
    if (keepScroll) log.scrollTop = log.scrollHeight;
  }

  // After a reply: short replies end at the bottom of the log; a reply taller than most of the log is scrolled so
  // its FIRST line is visible (jumping to the end of a long answer would hide how it starts).
  function revealLatestReply() {
    const reply = [...log.querySelectorAll(".msg-agent, .msg-error")].pop();
    if (!reply) return;
    if (reply.offsetHeight > log.clientHeight * 0.7) {
      log.scrollTop = reply.getBoundingClientRect().top - log.getBoundingClientRect().top + log.scrollTop - 8;
    } else {
      log.scrollTop = log.scrollHeight;
    }
  }

  function setBusy(busy) {
    store.setBusy(busy);
    typing.hidden = !busy;
    log.setAttribute("aria-busy", String(busy));
    sendBtn.disabled = busy;
    sendLabel.textContent = busy ? "Sending…" : "Send";
    if (busy) log.scrollTop = log.scrollHeight;
  }

  async function send(rawText) {
    const text = (rawText ?? input.value).trim();
    const studentId = store.state.studentId.trim();
    if (store.state.busy) return;
    if (!studentId) { showError("Enter a student ID first (top right)."); document.getElementById("student-id").focus(); return; }
    if (!text) { showError("Type a message first."); input.focus(); return; }
    if (text.length > 2000) { showError("Messages can be at most 2000 characters."); return; }
    showError("");
    input.value = "";
    counter.textContent = "0";
    const turn = store.addTurn(text);
    render();
    setBusy(true);
    try {
      const response = await api.chat({ message: text, studentId, sessionId: store.state.sessionId });
      store.completeTurn(turn, response);
      announce("CampusPilot replied.");
      if ((response.memories_saved || []).length) onMemorySaved();
    } catch (err) {
      store.failTurn(turn, err);
      announce(`Error: ${err.message}`);
    } finally {
      setBusy(false);
      render();
      revealLatestReply();
      input.focus();
    }
  }

  form.addEventListener("submit", (e) => { e.preventDefault(); send(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  input.addEventListener("input", () => { counter.textContent = String(input.value.length); if (errorEl.hidden === false) showError(""); });

  store.subscribe((what) => {
    if (what === "reset") { typing.hidden = true; showError(""); render(); }
    else if (what === "turn-removed") render(); // e.g. "Dismiss" on an error message
  });

  render();
  return { render, send };
}
