// Agent activity view: the stored execution trace (GET /api/traces/{id}) for the selected turn.
// Traces are sanitised server-side (no raw tool outputs; sensitive-looking text is redacted).
import { api } from "./api.js";
import { badge, clear, emptyBlock, errorBlock, h, kv, loadingBlock } from "./dom.js";
import {
  formatMs, formatScore, formatTokens, humanizeAction, outcomeLabel, outcomeTone, statusLabel, statusTone,
} from "./format.js";

function argList(args) {
  const entries = Object.entries(args || {});
  if (!entries.length) return h("p", { class: "muted small" }, "No arguments");
  return h("dl", { class: "args" }, entries.map(([k, v]) => kv(k, typeof v === "string" ? v : JSON.stringify(v))));
}

function toolStep(tool, index) {
  const summary = tool.summary || {};
  const detail = [];
  if (summary.error) detail.push(h("p", { class: "step-error" }, `Error: ${summary.error}`));
  if (summary.chunk_ids) detail.push(h("p", { class: "muted small" }, summary.chunk_ids.length ? `Retrieved: ${summary.chunk_ids.join(", ")}` : "No chunks retrieved"));
  if (summary.fields) detail.push(h("p", { class: "muted small" }, `Returned fields: ${summary.fields.join(", ")}${summary.count != null ? ` (count ${summary.count})` : ""}`));
  if (summary.ticket_id) detail.push(h("p", { class: "muted small" }, `Ticket ${summary.ticket_id} (${summary.category})`));
  if (summary.saved !== undefined) detail.push(h("p", { class: "muted small" }, `Memory ${summary.saved ? "saved" : "not saved"}${summary.key ? `: ${summary.key}` : ""}`));
  return h("li", { class: "step" },
    h("span", { class: "step-dot", "aria-hidden": "true" }),
    h("div", { class: "step-body" },
      h("div", { class: "step-head" }, h("strong", {}, `Tool ${index + 1}: `), h("code", {}, tool.name),
        badge(outcomeLabel(tool.outcome), outcomeTone(tool.outcome)), h("span", { class: "muted small" }, formatMs(tool.latency_ms))),
      argList(tool.arguments), ...detail));
}

function renderTrace(trace) {
  const tools = trace.tools_called || [];
  return h("div", {},
    h("dl", { class: "summary-grid" },
      kv("Action", humanizeAction(trace.action)),
      kv("Status", badge(statusLabel(trace.status), statusTone(trace.status))),
      kv("Total latency", formatMs(trace.total_latency_ms)),
      kv("Tokens", formatTokens(trace.input_tokens, trace.output_tokens)),
      kv("Provider / model", `${trace.llm_provider ?? "—"} / ${trace.llm_model ?? "not reported"}`),
      kv("Trace id", h("code", {}, trace.trace_id))),
    trace.error ? h("div", { class: "alert alert-danger", role: "alert" }, `Run error: ${trace.error}`) : null,
    h("ol", { class: "timeline", "aria-label": "Execution steps" },
      h("li", { class: "step" }, h("span", { class: "step-dot", "aria-hidden": "true" }),
        h("div", { class: "step-body" }, h("strong", {}, "Question received"), h("p", { class: "quote" }, trace.user_query))),
      tools.length ? tools.map(toolStep)
        : h("li", { class: "step" }, h("span", { class: "step-dot", "aria-hidden": "true" }),
          h("div", { class: "step-body" }, h("strong", {}, "No tools called"), h("p", { class: "muted small" }, "The agent answered from the conversation context alone."))),
      h("li", { class: "step" }, h("span", { class: "step-dot", "aria-hidden": "true" }),
        h("div", { class: "step-body" }, h("strong", {}, "Answer produced"), h("p", { class: "quote" }, trace.agent_response ?? "—")))),
    (trace.evidence_used || []).length ? h("section", { class: "block" }, h("h4", { class: "block-title" }, "Handbook evidence referenced"),
      h("ul", { class: "plain" }, trace.evidence_used.map((e) => h("li", {}, h("code", {}, e.chunk_id), ` · ${e.section} · similarity ${formatScore(e.score)}`)))) : null,
    (trace.memories_used || []).length ? h("section", { class: "block" }, h("h4", { class: "block-title" }, "Memory keys used"),
      h("p", {}, trace.memories_used.map((k) => badge(k, "neutral")))) : null,
    h("p", { class: "muted small" }, "Traces are stored without raw tool outputs, and text that looks sensitive is replaced with “[redacted…]”."));
}

export function initActivity({ store }) {
  const root = document.getElementById("panel-activity");
  const cache = new Map(); // trace_id -> trace (a stored trace never changes)
  let token = 0;

  async function render() {
    const mine = ++token;
    clear(root);
    const turn = store.selectedTurn();
    if (!turn) { root.append(emptyBlock("No turn selected", "Send a message, then choose a turn to see what the agent did.")); return; }
    if (!turn.response) { root.append(emptyBlock("No trace for this turn", turn.error ? `The request failed: ${turn.error.message}` : "Waiting for a reply…")); return; }
    const id = turn.response.trace_id;
    if (!id) { root.append(emptyBlock("No trace was recorded", "The server did not return a trace id for this turn.")); return; }
    if (cache.has(id)) { root.append(renderTrace(cache.get(id))); return; }
    if (root.hidden) return; // fetch lazily: app.js re-renders when this tab is opened
    root.append(loadingBlock("Loading trace…"));
    try {
      const trace = await api.trace(id);
      cache.set(id, trace);
      if (mine !== token) return; // a newer selection superseded this request
      clear(root).append(renderTrace(trace));
    } catch (err) {
      if (mine !== token) return;
      clear(root).append(errorBlock(`Could not load the trace: ${err.message}`, render));
    }
  }

  store.subscribe((what) => { if (["selection", "turn-completed", "turn-failed", "turn-removed", "reset"].includes(what)) render(); });
  render();
  return { render };
}
