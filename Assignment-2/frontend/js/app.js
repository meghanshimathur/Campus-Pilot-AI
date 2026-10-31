import { api } from "./api.js";
import { initActivity } from "./activity.js";
import { initApprovals } from "./approvals.js";
import { initAuth } from "./auth.js";
import { initChat } from "./chat.js";
import { announce, clear, h } from "./dom.js";
import { initEvaluation } from "./evaluation.js";
import { initEvidence } from "./evidence.js";
import { initMemory } from "./memory.js";
import { clip } from "./format.js";
import { createStore } from "./state.js";

const store = createStore();
const $ = (id) => document.getElementById(id);

const sessionChip = $("session-chip");
const apiStatus = $("api-status");
const turnSelect = $("turn-select");

// ---------- tabs (WAI-ARIA tabs pattern: roving tabindex, arrow/Home/End keys). Which tabs a role sees is a convenience:
// the server authorizes every request itself, so showing or hiding a tab never grants or removes access.
const tabIds = ["evidence", "activity", "memory", "approvals", "evaluation"];
const TABS_BY_ROLE = { student: ["evidence", "activity", "memory", "approvals", "evaluation"], staff: ["approvals", "evaluation"] };
const tabEls = Object.fromEntries(tabIds.map((id) => [id, $(`tab-${id}`)]));
const panelEls = Object.fromEntries(tabIds.map((id) => [id, $(`panel-${id}`)]));
let visibleTabs = [];

let evidence, activity, memory, approvals, evaluation;

function setTabsFor(role) {
  visibleTabs = TABS_BY_ROLE[role] ?? [];
  for (const id of tabIds) tabEls[id].hidden = !visibleTabs.includes(id);
}

function activateTab(id, { focus = false } = {}) {
  if (!visibleTabs.includes(id)) return;
  for (const t of tabIds) {
    const on = t === id;
    tabEls[t].setAttribute("aria-selected", String(on));
    tabEls[t].tabIndex = on ? 0 : -1;
    panelEls[t].hidden = !on;
  }
  if (focus) tabEls[id].focus();
  if (id === "activity") activity.render();
  if (id === "memory") memory.ensureFresh();
  if (id === "approvals") approvals.ensureFresh();
  if (id === "evaluation") evaluation.ensureLoaded();
}

for (const id of tabIds) {
  tabEls[id].addEventListener("click", () => activateTab(id));
  tabEls[id].addEventListener("keydown", (e) => {
    const i = visibleTabs.indexOf(id);
    const move = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
    if (move) { e.preventDefault(); activateTab(visibleTabs[(i + move + visibleTabs.length) % visibleTabs.length], { focus: true }); }
    else if (e.key === "Home") { e.preventDefault(); activateTab(visibleTabs[0], { focus: true }); }
    else if (e.key === "End") { e.preventDefault(); activateTab(visibleTabs[visibleTabs.length - 1], { focus: true }); }
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
  if (["turn-completed", "turn-failed", "turn-removed", "selection", "reset", "signout"].includes(what)) renderTurnPicker();
  if (["turn-completed", "reset", "signout"].includes(what)) renderSession();
});

$("new-session").addEventListener("click", () => {
  store.resetConversation();
  announce("Started a new session. Saved memory is kept.");
  $("message").focus();
});

// ---------- signed in / signed out. The dashboard is only a view: hiding it when nobody is signed in is for the user's benefit;
// the protected API routes refuse every request without a valid token regardless of what this page shows.
const authMain = $("auth-main");
const mainEl = $("main");
const viewSwitch = document.querySelector(".view-switch");
const userBox = $("user-box");
const skipLink = document.querySelector(".skip-link");

function applySignedIn(user) {
  document.body.dataset.auth = "in";
  document.body.dataset.role = user.role;
  authMain.hidden = true;
  mainEl.hidden = false;
  userBox.hidden = false;
  const student = user.role === "student";
  viewSwitch.hidden = !student;
  sessionChip.hidden = !student;
  $("new-session").hidden = !student;
  skipLink.setAttribute("href", "#main");
  setTabsFor(user.role);
  setView(student ? "chat" : "insights");
  renderTurnPicker();
  renderSession();
  activateTab(student ? "evidence" : "approvals");
  if (student) $("message").focus(); else mainEl.focus();
}

function applySignedOut() {
  document.body.dataset.auth = "out";
  delete document.body.dataset.role;
  mainEl.hidden = true;
  authMain.hidden = false;
  viewSwitch.hidden = true;
  userBox.hidden = true;
  sessionChip.hidden = true;
  skipLink.setAttribute("href", "#auth-main");
  setTabsFor(null);
  memory.reset();
  approvals.reset();
  evaluation.reset();                                   // evidence, activity and chat clear themselves on the store's "signout"
  renderTurnPicker();
  renderSession();
}

// ---------- health indicator (public route: works before sign-in)
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
evidence = initEvidence({ store });
activity = initActivity({ store });
memory = initMemory({ store });
approvals = initApprovals({ store });
evaluation = initEvaluation();
initChat({
  store,
  openInspector,
  onMemorySaved: () => { memory.markStale(); if (!panelEls.memory.hidden) memory.refresh(); },
  onTurnDone: (response) => {
    const asked = (response.tools_called || []).find((t) => t.name === "update_contact_details" && t.result?.status === "pending_approval");
    if (!asked) return;
    approvals.markStale();
    if (!panelEls.approvals.hidden) approvals.refresh();
    announce("Your change request was sent for staff approval. See the Approvals tab.");
  },
});
const auth = initAuth({ store, onSignedIn: applySignedIn, onSignedOut: applySignedOut });
applySignedOut();
checkHealth();
auth.focus();
