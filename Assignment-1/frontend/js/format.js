// Pure helpers: no DOM access, no network. Unit-tested under Node (tests/frontend/format.test.mjs).

export function formatMs(ms) {
  if (ms === null || ms === undefined || Number.isNaN(Number(ms))) return "—";
  const n = Number(ms);
  return n < 1000 ? `${Math.round(n)} ms` : `${(n / 1000).toFixed(2)} s`;
}

// Tokens are shown only when the provider reported them; never estimated.
export function formatTokens(input, output) {
  if (input == null && output == null) return "not reported";
  return `in ${input ?? "n/a"} · out ${output ?? "n/a"}`;
}

export function percent(num, den) {
  if (!den) return "n/a";
  return `${Math.round((num / den) * 100)}%`;
}

export function formatScore(x) {
  return typeof x === "number" ? x.toFixed(2) : "n/a";
}

export function formatCoverage(x) {
  return typeof x === "number" ? `${Math.round(x * 100)}%` : "n/a";
}

export function clip(text, n = 80) {
  const s = String(text ?? "");
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

const ACTIONS = { answered_directly: "Answered directly", used_tools: "Used tools", failed: "Failed" };
export function humanizeAction(action) {
  return ACTIONS[action] ?? (action ? String(action) : "—");
}

const STATUS_TONE = { success: "success", completed_with_tool_errors: "warning", error: "danger" };
const STATUS_LABEL = { success: "Success", completed_with_tool_errors: "Completed with tool errors", error: "Error" };
export function statusTone(status) { return STATUS_TONE[status] ?? "neutral"; }
export function statusLabel(status) { return STATUS_LABEL[status] ?? (status ? String(status) : "—"); }

const OUTCOME_TONE = { ok: "success", error: "danger", rejected: "warning", no_evidence: "info" };
const OUTCOME_LABEL = { ok: "OK", error: "Error", rejected: "Rejected", no_evidence: "No evidence" };
export function outcomeTone(outcome) { return OUTCOME_TONE[outcome] ?? "neutral"; }
export function outcomeLabel(outcome) { return OUTCOME_LABEL[outcome] ?? (outcome ? String(outcome) : "—"); }

const MATCH_HELP = {
  semantic: "Matched on meaning (embedding similarity) alone",
  keyword: "Matched on exact key terms even though similarity was weak",
  "semantic+keyword": "Matched on similarity together with shared key terms",
};
export function matchHelp(match) { return MATCH_HELP[match] ?? ""; }

// Turn an HTTP error body from FastAPI into one readable sentence.
export function parseApiError(status, body) {
  const detail = body && typeof body === "object" ? body.detail : undefined;
  if (Array.isArray(detail) && detail.length) {
    return detail.map((d) => {
      const field = Array.isArray(d.loc) ? d.loc.filter((p) => p !== "body").join(".") : "";
      return field ? `${field}: ${d.msg}` : String(d.msg);
    }).join("; ");
  }
  if (typeof detail === "string" && detail) return detail;
  if (status === 404) return "Not found.";
  if (status === 422) return "The request was not valid.";
  if (status === 503) return "The service is not available right now.";
  if (status >= 500) return "The server hit an error. Please try again.";
  return `Request failed (HTTP ${status}).`;
}

// Everything the Evidence view shows about the handbook comes from the tool calls in one /api/chat response.
export function extractHandbookSearches(response) {
  const calls = (response && Array.isArray(response.tools_called)) ? response.tools_called : [];
  return calls.filter((c) => c.name === "search_handbook").map((c) => {
    const result = c.result && typeof c.result === "object" ? c.result : {};
    const query = c.arguments && typeof c.arguments === "object" ? c.arguments.query : undefined;
    if (result.error) return { state: "error", query, message: String(result.error) };
    if (result.found) return { state: "found", query: result.query ?? query, chunks: Array.isArray(result.chunks) ? result.chunks : [] };
    return { state: "not_found", query: result.query ?? query, message: result.message ?? "" };
  });
}

export function toolNames(response) {
  const calls = (response && Array.isArray(response.tools_called)) ? response.tools_called : [];
  return [...new Set(calls.map((c) => c.name))];
}

// Words used to explain evaluation modes. 'offline' must never be presented as a model-quality result.
export function modeInfo(mode) {
  if (mode === "live") {
    return { tone: "success", label: "LIVE", text: "Real model run. One run per case, so treat rates as indicative." };
  }
  if (mode === "offline") {
    return {
      tone: "warning", label: "OFFLINE",
      text: "Scripted LLM: this is a pipeline check (tools, retrieval, memory, tracing). It says nothing about real model quality. " +
            "Latency excludes any LLM time and token usage is not available.",
    };
  }
  return { tone: "neutral", label: String(mode ?? "UNKNOWN").toUpperCase(), text: "" };
}

export function tokenSummary(tokens) {
  if (!tokens || !tokens.runs_with_usage) {
    return `not available (${tokens ? tokens.runs_with_usage : 0}/${tokens ? tokens.runs_total : 0} runs reported usage)`;
  }
  return `${tokens.runs_with_usage}/${tokens.runs_total} runs reported usage · input total ${tokens.input_total ?? "n/a"} ` +
         `(mean ${tokens.input_mean ?? "n/a"}) · output total ${tokens.output_total ?? "n/a"} (mean ${tokens.output_mean ?? "n/a"})`;
}

export function formatTimestamp(value) {
  return value ? String(value).replace("T", " ").slice(0, 19) : "—";
}

// Counts of saved reports per mode, from the real report list (never assumed). A report whose label disagrees with
// who produced its runs (integrity "inconsistent") is counted under `other`, never as offline or live.
export function countReportsByMode(reports) {
  const counts = { offline: 0, live: 0, other: 0 };
  for (const r of reports ?? []) {
    const trusted = r.integrity !== "inconsistent" && (r.mode === "offline" || r.mode === "live");
    counts[trusted ? r.mode : "other"] += 1;
  }
  return counts;
}

// Banner for a report whose label cannot be trusted (e.g. scripted runs saved under mode "live").
export function inconsistentInfo(report) {
  const providers = [...new Set((report.runs ?? []).map((r) => r.provider).filter(Boolean))];
  const label = String(report.mode ?? "unknown");
  return {
    tone: "danger", label: "INCONSISTENT",
    text: `This report is labelled “${label}” but its runs came from: ${providers.join(", ") || "unknown"}. ` +
          "Its label cannot be trusted, so do not use it as evidence of real-model quality.",
  };
}

// How a run ended: "completed" (reached the model and finished), "blocked_by_provider" (quota/outage: the agent was
// never evaluated) or "skipped" (not run). Reports saved before this distinction existed were all "completed".
export function runOutcome(run) {
  return run?.outcome ?? "completed";
}

// Normalised counts from a report's metrics, with fallbacks for older reports that lack the newer fields.
export function reportStats(m) {
  const runs = m?.runs ?? 0;
  const blocked = m?.runs_blocked ?? 0;
  const skipped = m?.runs_skipped ?? 0;
  const completed = m?.runs_completed ?? runs - blocked - skipped;
  const cases = m?.cases ?? 0;
  const casesEvaluated = m?.cases_evaluated ?? cases;
  return { runs, completed, blocked, skipped, notRun: blocked + skipped, cases, casesEvaluated,
           casesNotEvaluable: m?.cases_blocked ?? cases - casesEvaluated, executed: m?.runs_executed ?? runs - skipped,
           kinds: m?.provider_error_kinds ?? {} };
}

export function blockedBannerText(stats) {
  const kinds = Object.entries(stats.kinds).map(([k, v]) => `${k} ×${v}`).join(", ");
  const detail = `${stats.blocked} blocked${kinds ? `: ${kinds}` : ""}${stats.skipped ? `; ${stats.skipped} not run after the evaluation stopped early` : ""}`;
  let text = `Provider errors left ${stats.notRun} of ${stats.runs} runs unevaluated (${detail}). ` +
             `They are not counted as agent failures: every success rate below uses only the ${stats.completed} completed run${stats.completed === 1 ? "" : "s"}.`;
  if (stats.completed === 0) text += " No run completed, so this evaluation measured nothing about the agent.";
  return text;
}
