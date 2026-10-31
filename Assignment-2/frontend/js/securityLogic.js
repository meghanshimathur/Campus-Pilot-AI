// Pure helpers for displaying an Assignment 2 security-evaluation report (reports/assignment2_evaluation_*.json, written by
// scripts/run_security_evaluation.py and served by GET /api/evaluation/reports/{name}). No DOM, no network
// (unit-tested under Node: tests/frontend/security.test.mjs). Nothing is computed that the report does not contain:
// a missing value is shown as "n/a", never as a default or an estimate.

export const SECURITY_KIND = "assignment2_security_evaluation";

// Display names and targets for the metrics the report defines (see metric_definitions in the report). "zero": the count in the
// numerator must be 0 (violations, executions, leaks). "all": numerator must equal the denominator. The saved JSON has no
// targets, so they are stated here, once, and a metric the report lacks is simply not shown.
const METRICS = [
  { key: "scenario_pass_rate", label: "Scenarios passed", target: "all" },
  { key: "task_success", label: "Task success (legitimate operations that worked)", target: "all" },
  { key: "unauthorized_access_violations", label: "Unauthorized-access violations", target: "zero" },
  { key: "unsafe_action_executions", label: "Unsafe-action executions", target: "zero" },
  { key: "approval_correctness", label: "Approval correctness", target: "all" },
  { key: "secret_leakage", label: "Secret leakage (secret × location pairs)", target: "zero" },
  { key: "audit_coverage", label: "Audit coverage", target: "all" },
  { key: "trace_coverage", label: "Trace coverage", target: "all" },
  { key: "audit_chain_integrity", label: "Audit hash-chain integrity", target: "all" },
];

export function isSecurityReport(report) {
  return Boolean(report) && typeof report === "object" && report.kind === SECURITY_KIND;
}

export function pct1(num, den) {
  return typeof num === "number" && typeof den === "number" && den > 0 ? `${((num / den) * 100).toFixed(1)}%` : "n/a";
}

// "meets" / "misses" / "not measured". A metric with an empty denominator was not measured, which is not a pass.
export function targetStatus(target, num, den) {
  if (typeof num !== "number" || typeof den !== "number" || den <= 0) return "not measured";
  if (target === "zero") return num === 0 ? "meets" : "misses";
  return num === den ? "meets" : "misses";
}

export function metricRows(report) {
  const m = report?.metrics ?? {};
  const defs = report?.metric_definitions ?? {};
  return METRICS.filter((spec) => m[spec.key] && typeof m[spec.key] === "object").map((spec) => {
    const { numerator, denominator } = m[spec.key];
    return {
      key: spec.key, label: spec.label, numerator, denominator,
      result: typeof numerator === "number" && typeof denominator === "number" ? `${numerator}/${denominator}` : "n/a",
      percent: pct1(numerator, denominator),
      target: spec.target === "zero" ? "0" : "100%",
      status: targetStatus(spec.target, numerator, denominator),
      definition: typeof defs[spec.key] === "string" ? defs[spec.key] : "",
    };
  });
}

function latencyLine(lat) {
  return lat && typeof lat === "object" ? `mean ${lat.mean} ms · median ${lat.median} ms · max ${lat.max} ms (n=${lat.n})` : "n/a";
}

// Latency and token usage are context, not pass/fail metrics.
export function contextRows(report) {
  const m = report?.metrics ?? {};
  const defs = report?.metric_definitions ?? {};
  const tk = m.token_usage;
  return [
    { label: "Latency: agent turn", value: latencyLine(m.latency?.agent_turn_ms), note: "local pipeline with a scripted model: no LLM time" },
    { label: "Latency: API call", value: latencyLine(m.latency?.api_call_ms), note: "in-process test client, excludes sign-in" },
    { label: "Token usage", value: tk && !tk.turns_with_usage ? `not available (${tk.turns_with_usage ?? 0}/${tk.turns ?? 0} turns reported usage)`
        : tk ? `${tk.turns_with_usage}/${tk.turns} turns reported usage · input ${tk.input_total ?? "n/a"} · output ${tk.output_total ?? "n/a"}` : "n/a",
      note: typeof defs.token_usage === "string" ? defs.token_usage : "" },
  ];
}

export function categoryRows(report) {
  const cats = report?.metrics?.scenarios_by_category ?? {};
  return Object.entries(cats).map(([name, v]) => ({ name, passed: v?.numerator ?? 0, total: v?.denominator ?? 0 }));
}

export function scenarioList(report) {
  return (Array.isArray(report?.scenarios) ? report.scenarios : []).map((s) => {
    const checks = Array.isArray(s.checks) ? s.checks : [];
    return { id: s.id, category: s.category, title: s.title, passed: Boolean(s.passed) && !s.error, error: s.error ?? null,
             checksTotal: checks.length, checksOk: checks.filter((c) => c.ok).length, wallMs: s.wall_ms };
  });
}

const KIND_LABEL = { task: "task", access: "access", unsafe: "unsafe action", approval: "approval", secret: "secret", audit: "audit", trace: "trace", chain: "audit chain" };
export function checkKindLabel(kind) { return KIND_LABEL[kind] ?? String(kind ?? ""); }

export function scenarioDetail(scenario) {
  const ev = scenario?.evidence ?? {};
  return {
    checks: (Array.isArray(scenario?.checks) ? scenario.checks : []).map((c) => ({ kind: checkKindLabel(c.kind), label: c.label, ok: Boolean(c.ok), detail: c.detail ?? "" })),
    traces: (Array.isArray(ev.traces) ? ev.traces : []).map((t) => ({
      id: t.trace_id, user: t.user, action: t.action ?? "—", status: t.status ?? "—",
      tools: (Array.isArray(t.tools) ? t.tools : []).map((x) => `${x.name}: ${x.outcome}`), masked: Array.isArray(t.secrets_masked) ? t.secrets_masked : [] })),
    events: Array.isArray(ev.audit_events) ? ev.audit_events : [],
    eventsTotal: typeof ev.audit_events_total === "number" ? ev.audit_events_total : (Array.isArray(ev.audit_events) ? ev.audit_events.length : 0),
  };
}

export function failedChecks(report) {
  const m = report?.metrics ?? {};
  return [...(Array.isArray(m.failed_checks) ? m.failed_checks : []), ...(Array.isArray(m.errors) ? m.errors : [])];
}

const COMPARABLE = { yes: { tone: "success", label: "comparable" }, partly: { tone: "warning", label: "partly comparable" }, no: { tone: "neutral", label: "not comparable" } };
export function comparableInfo(value) { return COMPARABLE[value] ?? { tone: "neutral", label: "unknown" }; }

export function comparisonRows(report) {
  return (Array.isArray(report?.comparison) ? report.comparison : []).map((c) => ({
    dimension: c.dimension, assignment1: c.assignment1, assignment2: c.assignment2, comparable: comparableInfo(c.comparable), note: c.note ?? "" }));
}

export function baselineSources(report) {
  return Object.values(report?.assignment1_baseline ?? {}).map((b) => ({ file: b.file, sha: String(b.sha256 ?? "").slice(0, 12), timestamp: b.timestamp }));
}

export function limitationList(report) {
  return Array.isArray(report?.limitations) ? report.limitations.map(String) : [];
}

// The banner every security report carries: it is an OFFLINE controls check, not a model-quality result.
export function securityBanner(report) {
  if (report?.integrity === "inconsistent") {
    return { tone: "danger", label: "INCONSISTENT",
             text: "This file claims to be an Assignment 2 security evaluation but does not match the expected format or provider. Its contents cannot be trusted; do not use them as evidence." };
  }
  return { tone: "warning", label: "OFFLINE",
           text: "A scripted model stands in for the LLM. These results show that the code enforces ownership, approval, expiry, masking and auditing whatever a model says. They say nothing about real-model behaviour; no live evaluation was run." };
}
