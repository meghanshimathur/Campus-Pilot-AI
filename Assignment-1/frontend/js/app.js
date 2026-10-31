import { api } from "./api.js";
import { initActivity } from "./activity.js";
import { initChat } from "./chat.js";
import { announce, clear, h } from "./dom.js";
import { initEvaluation } from "./evaluation.js";
import { initEvidence } from "./evidence.js";
import { initMemory } from "./memory.js";
import { clip } from "./format.js";
import { createStore } from "./state.js";

const store = createStore();
const $ = (id) => document.getElementById(id);

const studentInput = $("student-id");
const studentError = $("student-error");
const sessionChip = $("session-chip");
const apiStatus = $("api-status");
const turnSelect = $("turn-select");

// ---------- tabs (WAI-ARIA tabs pattern: roving tabindex, arrow/Home/End keys)
const tabIds = ["evidence", "activity", "memory", "evaluation"];
const tabEls = Object.fromEntries(tabIds.map((id) => [id, $(`tab-${id}`)]));
const panelEls = Object.fromEntries(tabIds.map((id) => [id, $(`panel-${id}`)]));

let evidence, activity, memory, evaluation;

function activateTab(id, { focus = false } = {}) {
  for (const t of tabIds) {
    const on = t === id;
    tabEls[t].setAttribute("aria-selected", String(on));
    tabEls[t].tabIndex = on ? 0 : -1;
    panelEls[t].hidden = !on;
  }
  if (focus) tabEls[id].focus();
  if (id === "activity") activity.render();
  if (id === "memory") memory.ensureFresh();
  if (id === "evaluation") evaluation.ensureLoaded();
}

for (const id of tabIds) {
  tabEls[id].addEventListener("click", () => activateTab(id));
  tabEls[id].addEventListener("keydown", (e) => {
    const i = tabIds.indexOf(id);
    const move = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
    if (move) { e.preventDefault(); activateTab(tabIds[(i + move + tabIds.length) % tabIds.length], { focus: true }); }
    else if (e.key === "Home") { e.preventDefault(); activateTab(tabIds[0], { focus: true }); }
    else if (e.key === "End") { e.preventDefault(); activateTab(tabIds[tabIds.length - 1], { focus: true }); }
  });
}

// ---------- mobile switch between chat and inspector (both are visible side by side on wide screens)
const inspector = $("inspector");
const viewChat = $("view-chat");
const viewInsights = $("view-insights");

function setView(view) {
  const insights = view === "insights";
  document.body.dataset.view = view;
  viewChat.setAttribute("aria-pressed", String(!insights));
  viewInsights.setAttribute("aria-pressed", String(insights));
}
viewChat.addEventListener("click", () => setView("chat"));
viewInsights.addEventListener("click", () => setView("insights"));
setView("chat");

function openInspector(tab) {
  setView("insights");
  activateTab(tab);
  inspector.scrollIntoView({ block: "nearest" });
}

// ---------- turn picker
function renderTurnPicker() {
  clear(turnSelect);
  const turns = store.state.turns.filter((t) => t.response || t.error);
  if (!turns.length) {
    turnSelect.append(h("option", { value: "" }, "No turns yet"));
    turnSelect.disabled = true;
    return;
  }
  turnSelect.disabled = false;
  turns.forEach((t, i) => turnSelect.append(h("option", { value: String(t.id) }, `${i + 1}. ${clip(t.userText, 48)}${t.error ? " (failed)" : ""}`)));
  turnSelect.value = String(store.state.selectedTurnId ?? turns[turns.length - 1].id);
}
turnSelect.addEventListener("change", () => store.selectTurn(Number(turnSelect.value)));

function renderSession() {
  const id = store.state.sessionId;
  sessionChip.textContent = id ? `Session ${id.slice(0, 8)}` : "No session yet";
  sessionChip.title = id ? `Session id: ${id}` : "A session starts with your first message";
}

store.subscribe((what) => {
  if (["turn-completed", "turn-failed", "turn-removed", "selection", "reset"].includes(what)) renderTurnPicker();
  if (["turn-completed", "reset"].includes(what)) renderSession();
});

// ---------- identity form
function validateStudent() {
  const value = studentInput.value.trim();
  const problem = !value ? "Enter a student ID." : value.length > 20 ? "A student ID has at most 20 characters." : "";
  studentError.hidden = !problem;
  studentError.textContent = problem;
  studentInput.toggleAttribute("aria-invalid", Boolean(problem));
  return problem ? null : value;
}

function applyStudent() {
  const value = validateStudent();
  if (value === null) {
    // An invalid field must not leave a stale ID active: clear it so sending is blocked until it is fixed.
    store.setStudentId("");
    return;
  }
  if (store.state.studentId && store.state.studentId !== value && store.state.turns.length) {
    store.resetConversation();
    announce(`Switched to ${value}. Started a new conversation.`);
  }
  store.setStudentId(value);
}
studentInput.addEventListener("change", applyStudent);
studentInput.addEventListener("blur", applyStudent);
$("identity-form").addEventListener("submit", (e) => { e.preventDefault(); applyStudent(); });
$("new-session").addEventListener("click", () => {
  store.resetConversation();
  announce("Started a new session. Saved memory is kept.");
  $("message").focus();
});

// ---------- health indicator
async function checkHealth() {
  try {
    const health = await api.health();
    apiStatus.className = `chip ${health.llm_configured ? "chip-success" : "chip-warning"}`;
    apiStatus.textContent = health.llm_configured ? "API online · LLM configured" : "API online · no LLM key configured";
    apiStatus.title = health.llm_configured ? "" : "Chat will return an error until an API key is set in .env (see .env.example).";
  } catch {
    apiStatus.className = "chip chip-danger";
    apiStatus.textContent = "API unreachable";
  }
}

// ---------- boot
store.setStudentId(validateStudent() ?? "");
evidence = initEvidence({ store });
activity = initActivity({ store });
memory = initMemory({ store });
evaluation = initEvaluation();
initChat({
  store,
  openInspector,
  onMemorySaved: () => { memory.markStale(); if (!panelEls.memory.hidden) memory.refresh(); },
});
renderTurnPicker();
renderSession();
activateTab("evidence");
checkHealth();
