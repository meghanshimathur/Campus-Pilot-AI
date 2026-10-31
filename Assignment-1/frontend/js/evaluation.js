// Evaluation view: real saved reports from GET /api/evaluation/reports[/{name}]. Offline vs live is always labelled.
import { api } from "./api.js";
import { badge, clear, emptyBlock, errorBlock, h, kv, loadingBlock } from "./dom.js";
import {
  blockedBannerText, clip, countReportsByMode, formatMs, formatTimestamp, inconsistentInfo, modeInfo, percent, reportStats, runOutcome, tokenSummary,
} from "./format.js";

function metricCard(label, value, sub) {
  return h("div", { class: "metric" }, h("div", { class: "metric-label" }, label), h("div", { class: "metric-value" }, value),
    sub ? h("div", { class: "metric-sub" }, sub) : null);
}

// Tool choice / check / result cells. Runs blocked by the provider were never judged, so they are neither PASS nor FAIL.
function runCells(r) {
  const outcome = runOutcome(r);
  if (outcome !== "completed") {
    return [h("td", {}, badge("n/a", "neutral")), h("td", {}, badge("n/a", "neutral")),
            h("td", {}, badge(outcome === "blocked_by_provider" ? "BLOCKED" : "SKIPPED", "warning",
              outcome === "blocked_by_provider" ? "The provider (quota/outage) blocked this run; the agent was not evaluated" : "Not run"))];
  }
  return [h("td", {}, badge(r.tool_choice_appropriate ? "appropriate" : "not appropriate", r.tool_choice_appropriate ? "success" : "danger")),
          h("td", {}, badge(r.check_passed ? "pass" : "fail", r.check_passed ? "success" : "danger")),
          h("td", {}, badge(r.success ? "PASS" : "FAIL", r.success ? "success" : "danger"))];
}

function runsTable(runs) {
  const head = ["Case", "Turn", "Tools called", "Tool choice", "Check", "Result", "Latency", "Tokens (in/out)", "Evidence", "Note"];
  return h("div", { class: "table-wrap", role: "region", "aria-label": "Per-run results", tabindex: "0" },
    h("table", { class: "table table-dense" },
      h("caption", { class: "sr-only" }, "Per-run evaluation results"),
      h("thead", {}, h("tr", {}, head.map((t) => h("th", { scope: "col" }, t)))),
      h("tbody", {}, runs.map((r) => h("tr", {},
        h("th", { scope: "row" }, r.case_id), h("td", {}, String(r.turn)),
        h("td", {}, (r.called_tools || []).join(", ") || "(none)"),
        ...runCells(r),
        h("td", {}, formatMs(r.latency_ms)),
        h("td", {}, r.input_tokens == null && r.output_tokens == null ? "n/a" : `${r.input_tokens ?? "n/a"} / ${r.output_tokens ?? "n/a"}`),
        h("td", {}, (r.evidence_ids || []).map((i) => i.split("#")[1] ?? i).join(", ") || "—"),
        h("td", {}, clip(r.note, 90)))))));
}

function renderReport(report) {
  const m = report.metrics;
  const inconsistent = report.integrity === "inconsistent";
  const info = inconsistent ? inconsistentInfo(report) : modeInfo(report.mode);
  const lat = m.latency_ms;
  const st = reportStats(m);
  const latencyScope = (report.mode === "live" ? "end-to-end incl. LLM API" : "local pipeline only, no LLM time") + (st.notRun ? ", completed runs" : "");
  const pacing = report.meta?.pacing;
  return h("div", {},
    h("div", { class: `alert alert-${info.tone}`, role: inconsistent ? "alert" : "note" }, badge(info.label, info.tone), " ", h("span", {}, info.text)),
    report.normalized ? h("p", { class: "muted small" }, report.normalized) : null,
    st.notRun ? h("div", { class: "alert alert-warning", role: "alert" }, h("strong", {}, "Incomplete evaluation. "), h("span", {}, blockedBannerText(st))) : null,
    h("dl", { class: "summary-grid" },
      kv("Run at", formatTimestamp(report.meta.timestamp)),
      kv("Provider / model", `${report.meta.provider} / ${report.meta.model}`),
      kv("Scope", `${m.runs} agent turns · ${m.cases} cases · one run each`),
      pacing ? kv("Pacing", `≥ ${pacing.min_interval}s between requests · 429 backoff ${pacing.quota_delays.join("/")}s · stop after ${pacing.stop_after} blocked turns`) : null),
    h("div", { class: "metrics" },
      report.mode === "live" || st.notRun ? metricCard("Runs completed", `${st.completed}/${st.runs}`, st.notRun ? `${st.blocked} blocked by provider · ${st.skipped} not run` : "all runs reached the model") : null,
      st.notRun ? metricCard("Blocked or not run", String(st.notRun), `${st.blocked} blocked${Object.keys(st.kinds).length ? ` (${Object.entries(st.kinds).map(([k, v]) => `${k} ×${v}`).join(", ")})` : ""} · ${st.skipped} not run`) : null,
      metricCard("Task success (cases)", `${m.cases_passed}/${st.casesEvaluated}`, percent(m.cases_passed, st.casesEvaluated) + (st.casesNotEvaluable ? ` · ${st.casesNotEvaluable} case(s) not evaluable` : "")),
      metricCard("Task success (runs)", `${m.runs_passed}/${st.completed}`, percent(m.runs_passed, st.completed) + (st.notRun ? " of completed runs" : "")),
      metricCard("Tool choice appropriate", `${m.tool_choice_appropriate}/${st.completed}`, percent(m.tool_choice_appropriate, st.completed)),
      metricCard("Traces recorded", `${m.traces_recorded}/${st.executed}`, percent(m.traces_recorded, st.executed)),
      metricCard("Handbook citations", `${m.handbook_cited ?? "n/a"}/${m.handbook_runs_with_evidence}`, "answers with evidence that cite a chunk id"),
      metricCard("Tool errors observed", String(m.tool_errors_observed), `${m.runs_with_agent_error} run(s) ended in an agent error`),
      metricCard("Latency (median)", lat ? formatMs(lat.median) : "n/a", lat ? `min ${formatMs(lat.min)} · mean ${formatMs(lat.mean)} · max ${formatMs(lat.max)} (${latencyScope})` : ""),
      metricCard("Token usage", m.tokens && m.tokens.runs_with_usage ? "reported" : "not available", tokenSummary(m.tokens))),
    h("h4", { class: "block-title" }, "Results by category"),
    h("ul", { class: "plain cols" }, Object.entries(m.by_category || {}).map(([k, v]) => h("li", {}, h("strong", {}, k), `: ${v.passed}/${v.runs} passed${v.blocked ? ` · ${v.blocked} blocked/not run` : ""}`))),
    h("h4", { class: "block-title" }, st.notRun ? "Failures (completed runs only)" : "Failures"),
    (m.failed_runs || []).length
      ? h("ul", { class: "plain" }, m.failed_runs.map((f) => h("li", {}, f)))
      : h("p", { class: "muted" }, "None in this report."),
    st.notRun ? h("h4", { class: "block-title" }, "Blocked or not run (excluded from success rates)") : null,
    st.notRun ? h("ul", { class: "plain" }, (m.blocked_runs || []).map((b) => h("li", {}, b))) : null,
    h("h4", { class: "block-title" }, "Per-run results"),
    runsTable(report.runs || []),
    h("p", { class: "muted small" }, "Scoring is rule-based (tool choice and keyword/evidence checks), not an LLM judge. Values are shown exactly as saved in the report file."));
}


// Always-visible orientation: what this tab is, what OFFLINE vs LIVE mean, and what has (not) been measured.
function explainer(reports) {
  const counts = countReportsByMode(reports);
  const noLive = counts.live === 0;
  return h("div", { class: "explainer" },
    h("p", { class: "muted small" }, h("strong", {}, "This tab only displays saved report files"), " from ", h("code", {}, "reports/"),
      " — it never runs an evaluation itself. Saved reports: ",
      h("strong", {}, `${counts.offline} offline · ${counts.live} live${counts.other ? ` · ${counts.other} unverified` : ""}`), "."),
    noLive ? h("div", { class: "alert alert-warning", role: "note" },
      h("strong", {}, "No LIVE evaluation has been run yet."),
      h("span", {}, " The numbers below come from an offline scripted run, so real-model quality (accuracy, latency, token usage) is still unmeasured.")) : null,
    h("details", { class: "details" },
      h("summary", {}, "How to read this tab: OFFLINE vs LIVE"),
      h("dl", { class: "defs" },
        h("dt", {}, badge("OFFLINE", "warning")),
        h("dd", {}, "A script plays the role of the LLM. It proves the plumbing works (tool dispatch, handbook retrieval, memory, tracing). ",
          "It says nothing about how well a real model behaves, uses no tokens, and its latency excludes any model time. ",
          "No API calls. Create one with ", h("code", {}, ".venv/bin/python scripts/run_evaluation.py --offline"), "."),
        h("dt", {}, badge("LIVE", "success")),
        h("dd", {}, "The real model (for example Gemini) answers the same fictional test cases. Success rate, latency and token usage describe the real agent ",
          "(one run per case, so indicative only). Makes real API calls that use tokens. Create one with ",
          h("code", {}, ".venv/bin/python scripts/run_evaluation.py --live"), "."),
        h("dt", {}, "Careful"),
        h("dd", {}, "Running the script with no flag runs LIVE whenever an API key is configured."))));
}

export function initEvaluation() {
  const root = document.getElementById("panel-evaluation");
  let loaded = false;
  let token = 0;

  async function showReport(name, container) {
    const mine = ++token;
    clear(container).append(loadingBlock("Loading report…"));
    try {
      const report = await api.report(name);
      if (mine !== token) return;
      clear(container).append(renderReport(report));
    } catch (err) {
      if (mine !== token) return;
      clear(container).append(errorBlock(`Could not load the report: ${err.message}`, () => showReport(name, container)));
    }
  }

  async function load() {
    loaded = true;
    clear(root).append(loadingBlock("Loading evaluation reports…"));
    try {
      const { reports } = await api.reports();
      if (!reports.length) {
        clear(root).append(explainer(reports), emptyBlock("No evaluation reports yet",
          "Create one with: .venv/bin/python scripts/run_evaluation.py --offline (scripted LLM, pipeline check) or --live (needs an API key)."));
        return;
      }
      const container = h("div", { "aria-live": "polite" });
      const select = h("select", { id: "report-select" },
        reports.map((r) => h("option", { value: r.name },
          `${r.mode.toUpperCase()}${r.integrity === "inconsistent" ? " ⚠ inconsistent" : ""} · ${formatTimestamp(r.timestamp)} · ${r.runs} runs`)));
      select.addEventListener("change", () => showReport(select.value, container));
      clear(root).append(
        explainer(reports),
        h("div", { class: "toolbar" },
          h("div", { class: "turn-picker" }, h("label", { for: "report-select" }, "Report"), select),
          h("button", { type: "button", class: "btn btn-secondary btn-sm", onclick: () => load() }, "Reload list")),
        container);
      await showReport(reports[0].name, container);
    } catch (err) {
      loaded = false;
      clear(root).append(errorBlock(`Could not load evaluation reports: ${err.message}`, load));
    }
  }

  return { load, ensureLoaded() { if (!loaded) return load(); } };
}
