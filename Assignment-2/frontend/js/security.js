// Renders an Assignment 2 security-evaluation report (served by GET /api/evaluation/reports/{name}) exactly as saved:
// metrics with numerator/denominator, scenario results with their trace and audit evidence, the Assignment 1 vs Assignment 2
// comparison and the limitations. All text goes in as text nodes. Numbers come from the report; a missing value reads "n/a".
import { badge, h, kv } from "./dom.js";
import { formatTimestamp } from "./format.js";
import {
  baselineSources, categoryRows, comparisonRows, contextRows, failedChecks, limitationList, metricRows, scenarioDetail, scenarioList, securityBanner,
} from "./securityLogic.js";

const STATUS_BADGE = { meets: ["meets target", "success"], misses: ["misses target", "danger"], "not measured": ["not measured", "neutral"] };

function metricsTable(report) {
  const rows = metricRows(report);
  if (!rows.length) return h("p", { class: "muted" }, "This report contains no metrics.");
  return h("div", { class: "table-wrap", role: "region", "aria-label": "Security metrics", tabindex: "0" },
    h("table", { class: "table" },
      h("caption", { class: "sr-only" }, "Security metrics with numerator and denominator"),
      h("thead", {}, h("tr", {}, ["Metric", "Result", "Rate", "Target", "Status"].map((t) => h("th", { scope: "col" }, t)))),
      h("tbody", {}, rows.map((r) => {
        const [text, tone] = STATUS_BADGE[r.status];
        return h("tr", {},
          h("th", { scope: "row" }, r.label, r.definition ? h("div", { class: "muted small metric-def" }, r.definition) : null),
          h("td", {}, h("strong", {}, r.result)), h("td", {}, r.percent), h("td", {}, r.target), h("td", {}, badge(text, tone)));
      }))));
}

function scenarioBody(scenario) {
  const d = scenarioDetail(scenario);
  return h("div", { class: "scenario-body" },
    h("ul", { class: "plain" }, d.checks.map((c) => h("li", {}, badge(c.ok ? "ok" : "FAILED", c.ok ? "success" : "danger"), " ", badge(c.kind, "neutral"), " ", c.label,
      c.detail ? h("span", { class: "muted small" }, ` — ${c.detail}`) : null))),
    d.traces.length ? h("div", { class: "block" }, h("h5", { class: "block-title" }, "Traces"),
      h("ul", { class: "plain" }, d.traces.map((t) => h("li", {}, h("code", {}, t.id.slice(0, 12)), ` ${t.user}: ${t.action}, ${t.status}`,
        t.tools.length ? ` · tools: ${t.tools.join(", ")}` : "", t.masked.length ? ` · secrets masked: ${t.masked.join(", ")}` : "")))) : null,
    h("div", { class: "block" }, h("h5", { class: "block-title" }, `Audit records (${d.events.length}${d.eventsTotal > d.events.length ? ` of ${d.eventsTotal}` : ""})`),
      d.events.length ? h("div", { class: "table-wrap", role: "region", "aria-label": "Audit records", tabindex: "0" },
        h("table", { class: "table table-dense" },
          h("thead", {}, h("tr", {}, ["Event", "Actor", "Decision", "Outcome", "Approval", "Trace"].map((t) => h("th", { scope: "col" }, t)))),
          h("tbody", {}, d.events.map((e) => h("tr", {},
            h("th", { scope: "row" }, e.event), h("td", {}, `${e.actor_id} (${e.actor_role})`), h("td", {}, e.decision), h("td", {}, e.outcome),
            h("td", {}, e.approval_id ?? "—"), h("td", {}, e.trace_id ? e.trace_id.slice(0, 12) : "—"))))))
        : h("p", { class: "muted small" }, "No audit records in this scenario (for example, requests with no credentials have no identity to record).")));
}

function scenarioItem(s, scenario) {
  const body = h("div", {});
  const details = h("details", { class: "details scenario", dataset: { scenario: s.id } },
    h("summary", {}, h("code", {}, s.id), " ", s.title, " ",
      badge(s.passed ? "PASS" : "FAIL", s.passed ? "success" : "danger"), " ", h("span", { class: "muted small" }, `${s.checksOk}/${s.checksTotal} checks`)),
    body);
  // The evidence tables are built only when a scenario is opened: a report has dozens of scenarios and hundreds of records.
  details.addEventListener("toggle", () => { if (details.open && !body.firstChild) body.append(scenarioBody(scenario)); });
  return details;
}

export function renderSecurityReport(report) {
  const banner = securityBanner(report);
  const m = report.metrics ?? {};
  const failed = failedChecks(report);
  const scenarios = scenarioList(report);
  const byId = new Map((report.scenarios ?? []).map((x) => [x.id, x]));
  const comparison = comparisonRows(report);
  const baseline = baselineSources(report);
  return h("div", {},
    h("div", { class: `alert alert-${banner.tone}`, role: banner.tone === "danger" ? "alert" : "note" }, badge(banner.label, banner.tone), " ", h("span", {}, banner.text)),
    h("dl", { class: "summary-grid" },
      kv("Report", "Assignment 2 security evaluation"),
      kv("Run at", formatTimestamp(report.meta?.timestamp)),
      kv("Provider / model", `${report.meta?.provider ?? "n/a"} / ${report.meta?.model ?? "n/a"}`),
      kv("Scenarios", `${scenarios.length} (each in its own temporary database, fictional users)`),
      kv("Assignment 1 cases re-run", report.assignment1_cases_rerun ? "yes" : "no")),
    h("h4", { class: "block-title" }, "Metrics (numerator / denominator)"),
    metricsTable(report),
    h("dl", { class: "defs" }, contextRows(report).flatMap((r) => [h("dt", {}, r.label), h("dd", {}, r.value, r.note ? h("span", { class: "muted small" }, ` — ${r.note}`) : null)])),
    h("h4", { class: "block-title" }, "Scenarios by category"),
    h("ul", { class: "plain cols" }, categoryRows(report).map((c) => h("li", {}, h("strong", {}, c.name), `: ${c.passed}/${c.total} passed`))),
    failed.length ? h("div", { class: "alert alert-danger", role: "alert" }, h("strong", {}, `${failed.length} failed check${failed.length === 1 ? "" : "s"}`),
      h("ul", { class: "plain" }, failed.map((f) => h("li", {}, f)))) : h("p", { class: "muted" }, "No failed checks in this report."),
    h("h4", { class: "block-title" }, "Scenario results (open one for its checks, traces and audit records)"),
    h("div", { class: "scenario-list" }, scenarios.map((s) => scenarioItem(s, byId.get(s.id)))),
    h("h4", { class: "block-title" }, "Assignment 1 vs Assignment 2"),
    comparison.length ? h("div", { class: "table-wrap", role: "region", "aria-label": "Assignment 1 versus Assignment 2", tabindex: "0" },
      h("table", { class: "table table-dense" },
        h("caption", { class: "sr-only" }, "Assignment 1 versus Assignment 2, with how comparable each row is"),
        h("thead", {}, h("tr", {}, ["Dimension", "Assignment 1", "Assignment 2", "Comparable?", "Note"].map((t) => h("th", { scope: "col" }, t)))),
        h("tbody", {}, comparison.map((c) => h("tr", {}, h("th", { scope: "row" }, c.dimension), h("td", {}, c.assignment1), h("td", {}, c.assignment2),
          h("td", {}, badge(c.comparable.label, c.comparable.tone)), h("td", {}, c.note))))))
      : h("p", { class: "muted" }, "This report has no comparison."),
    baseline.length ? h("p", { class: "muted small" }, "Assignment 1 reports read for the comparison (never modified): ",
      baseline.map((b, i) => [i ? ", " : "", h("code", {}, b.file), ` (sha256 ${b.sha}…)`])) : null,
    h("h4", { class: "block-title" }, "Limitations"),
    h("ul", { class: "plain" }, limitationList(report).map((l) => h("li", {}, l))),
    h("p", { class: "muted small" }, "Values are shown exactly as saved in the report file. Targets and labels for the metrics are fixed by this page; the definitions come from the report."));
}
