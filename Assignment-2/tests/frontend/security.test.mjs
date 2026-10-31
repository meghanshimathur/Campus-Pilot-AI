import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  SECURITY_KIND, baselineSources, categoryRows, checkKindLabel, comparableInfo, comparisonRows, contextRows, failedChecks, isSecurityReport,
  limitationList, metricRows, pct1, scenarioDetail, scenarioList, securityBanner, targetStatus,
} from "../../frontend/js/securityLogic.js";

const rate = (n, d) => ({ numerator: n, denominator: d, rate: d ? n / d : null });
function report(over = {}) {
  return {
    kind: SECURITY_KIND, mode: "offline", integrity: "ok", meta: { timestamp: "2026-10-30 09:25:30", provider: "offline-scripted", model: "n/a" },
    metric_definitions: { unauthorized_access_violations: "probes not refused / probes made", token_usage: "never estimated" },
    metrics: {
      scenario_pass_rate: rate(2, 2), task_success: rate(3, 3), unauthorized_access_violations: rate(0, 64), unsafe_action_executions: rate(0, 22),
      approval_correctness: rate(19, 19), secret_leakage: rate(0, 108), audit_coverage: rate(28, 28), trace_coverage: rate(58, 58), audit_chain_integrity: rate(39, 39),
      scenarios_by_category: { ownership: rate(1, 1), approval: rate(1, 1) },
      latency: { agent_turn_ms: { n: 58, min: 2, mean: 2.9, median: 3, max: 5 }, api_call_ms: { n: 139, min: 1.2, mean: 5.5, median: 4.4, max: 17.1 } },
      token_usage: { turns: 58, turns_with_usage: 0, input_total: null, output_total: null }, failed_checks: [], errors: [],
    },
    scenarios: [
      { id: "OWN-1", category: "ownership", title: "Own fees", passed: true, error: null, wall_ms: 200,
        checks: [{ kind: "task", label: "reads own fees", ok: true, detail: "" }, { kind: "audit", label: "read audited", ok: true, detail: "tool.private_read" }],
        evidence: { traces: [{ trace_id: "a".repeat(32), user: "STU002", action: "used_tools", status: "success", tools: [{ name: "check_fee_status", outcome: "ok" }], secrets_masked: [] }],
                    audit_events: [{ event: "tool.private_read", actor_id: "STU002", actor_role: "student", decision: "allowed", outcome: "success", approval_id: null, trace_id: null, resource_student_id: "STU002" }], audit_events_total: 1 } },
      { id: "APR-3", category: "approval", title: "Rejected", passed: false, error: null, wall_ms: 300,
        checks: [{ kind: "unsafe", label: "not executed", ok: false, detail: "executed" }], evidence: { traces: [], audit_events: [], audit_events_total: 0 } },
    ],
    comparison: [{ dimension: "Task success", assignment1: "13/13", assignment2: "13/13", comparable: "yes", note: "same cases" },
                 { dimension: "Unauthorized access", assignment1: "not measured", assignment2: "0/64", comparable: "no", note: "" }],
    assignment1_baseline: { offline: { file: "evaluation_offline_20261022_000402.json", sha256: "1047a4f234b6" + "0".repeat(52), timestamp: "2026-10-22 00:04:02" } },
    limitations: ["offline only", "self-written probes"], ...over,
  };
}

test("only a report of the security kind is treated as one", () => {
  assert.equal(isSecurityReport(report()), true);
  for (const x of [null, undefined, {}, { kind: "other" }, { mode: "offline", metrics: { runs: 13 } }, "text", 3]) assert.equal(isSecurityReport(x), false);
});

test("percentages keep one decimal so 63/64 never looks perfect; an empty denominator is n/a", () => {
  assert.equal(pct1(63, 64), "98.4%"); assert.equal(pct1(64, 64), "100.0%"); assert.equal(pct1(0, 64), "0.0%");
  assert.equal(pct1(0, 0), "n/a"); assert.equal(pct1(undefined, 3), "n/a");
});

test("targets: violation-style metrics must be 0, coverage-style metrics must be complete, and nothing measured is not a pass", () => {
  assert.equal(targetStatus("zero", 0, 64), "meets"); assert.equal(targetStatus("zero", 1, 64), "misses");
  assert.equal(targetStatus("all", 19, 19), "meets"); assert.equal(targetStatus("all", 18, 19), "misses");
  assert.equal(targetStatus("zero", 0, 0), "not measured"); assert.equal(targetStatus("all", 0, 0), "not measured");
  assert.equal(targetStatus("all", undefined, 3), "not measured");
});

test("metric rows present numerator/denominator, percent, target, status and the report's own definition", () => {
  const rows = metricRows(report());
  const byKey = Object.fromEntries(rows.map((r) => [r.key, r]));
  assert.equal(rows.length, 9);
  assert.deepEqual([byKey.unauthorized_access_violations.result, byKey.unauthorized_access_violations.target, byKey.unauthorized_access_violations.status], ["0/64", "0", "meets"]);
  assert.equal(byKey.unauthorized_access_violations.definition, "probes not refused / probes made");
  assert.deepEqual([byKey.approval_correctness.result, byKey.approval_correctness.percent, byKey.approval_correctness.target], ["19/19", "100.0%", "100%"]);
});

test("a failing metric is shown as missing its target, not hidden or rounded away", () => {
  const r = report(); r.metrics.secret_leakage = rate(2, 108); r.metrics.task_success = rate(20, 21);
  const byKey = Object.fromEntries(metricRows(r).map((x) => [x.key, x]));
  assert.equal(byKey.secret_leakage.status, "misses"); assert.equal(byKey.task_success.status, "misses");
  assert.equal(byKey.task_success.percent, "95.2%");
});

test("a metric the report does not contain is not shown (nothing invented)", () => {
  const r = report(); delete r.metrics.secret_leakage; delete r.metrics.audit_coverage;
  assert.deepEqual(metricRows(r).map((x) => x.key).includes("secret_leakage"), false);
  assert.equal(metricRows(report({ metrics: {} })).length, 0);
  assert.equal(metricRows(null).length, 0);
});

test("latency is context with its scope, and token usage says 'not available' when nothing was reported", () => {
  const rows = contextRows(report());
  assert.match(rows[0].value, /mean 2\.9 ms · median 3 ms · max 5 ms \(n=58\)/); assert.match(rows[0].note, /no LLM time/);
  assert.equal(rows[2].value, "not available (0/58 turns reported usage)");
  const none = contextRows({ metrics: {} });
  assert.deepEqual(none.map((r) => r.value), ["n/a", "n/a", "n/a"]);
});

test("categories, scenario list and failed checks come straight from the report", () => {
  assert.deepEqual(categoryRows(report()), [{ name: "ownership", passed: 1, total: 1 }, { name: "approval", passed: 1, total: 1 }]);
  const list = scenarioList(report());
  assert.deepEqual(list.map((s) => [s.id, s.passed, s.checksOk, s.checksTotal]), [["OWN-1", true, 2, 2], ["APR-3", false, 0, 1]]);
  assert.deepEqual(failedChecks(report({ metrics: { failed_checks: ["APR-3: not executed"], errors: ["X-1: RuntimeError"] } })), ["APR-3: not executed", "X-1: RuntimeError"]);
  assert.deepEqual(failedChecks(report()), []);
});

test("a scenario that raised is never shown as passed", () => {
  const r = report(); r.scenarios[0].error = "RuntimeError: boom";
  assert.equal(scenarioList(r)[0].passed, false); assert.equal(scenarioList(r)[0].error, "RuntimeError: boom");
});

test("scenario detail exposes the checks, the trace and the audit evidence without inventing any", () => {
  const d = scenarioDetail(report().scenarios[0]);
  assert.deepEqual(d.checks.map((c) => [c.kind, c.ok]), [["task", true], ["audit", true]]);
  assert.deepEqual(d.traces[0].tools, ["check_fee_status: ok"]); assert.equal(d.events[0].event, "tool.private_read"); assert.equal(d.eventsTotal, 1);
  const empty = scenarioDetail({}); assert.deepEqual([empty.checks, empty.traces, empty.events, empty.eventsTotal], [[], [], [], 0]);
  assert.equal(checkKindLabel("unsafe"), "unsafe action"); assert.equal(checkKindLabel("chain"), "audit chain"); assert.equal(checkKindLabel("new"), "new");
});

test("the comparison keeps each row's comparability and never fills a gap", () => {
  const rows = comparisonRows(report());
  assert.deepEqual(rows.map((r) => r.comparable.label), ["comparable", "not comparable"]);
  assert.equal(rows[1].assignment1, "not measured");
  assert.deepEqual(comparableInfo("partly"), { tone: "warning", label: "partly comparable" });
  assert.deepEqual(comparableInfo("what"), { tone: "neutral", label: "unknown" });
  assert.deepEqual(baselineSources(report()), [{ file: "evaluation_offline_20261022_000402.json", sha: "1047a4f234b6", timestamp: "2026-10-22 00:04:02" }]);
  assert.deepEqual(limitationList(report()), ["offline only", "self-written probes"]); assert.deepEqual(limitationList({}), []);
});

test("the banner is always OFFLINE, and an inconsistent file is flagged instead of trusted", () => {
  const ok = securityBanner(report());
  assert.deepEqual([ok.tone, ok.label], ["warning", "OFFLINE"]); assert.match(ok.text, /scripted model/); assert.match(ok.text, /no live evaluation was run/);
  const bad = securityBanner(report({ integrity: "inconsistent" }));
  assert.deepEqual([bad.tone, bad.label], ["danger", "INCONSISTENT"]); assert.match(bad.text, /cannot be trusted/);
});

test("garbage input never throws", () => {
  for (const x of [null, undefined, {}, { metrics: null, scenarios: "x", comparison: 5 }]) {
    assert.doesNotThrow(() => { metricRows(x); contextRows(x); categoryRows(x); scenarioList(x); failedChecks(x); comparisonRows(x); baselineSources(x); limitationList(x); securityBanner(x); });
  }
});

// The real saved report (written by scripts/run_security_evaluation.py) must be readable with these helpers.
const reportsDir = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "reports");
const saved = existsSync(reportsDir) ? readdirSync(reportsDir).filter((n) => /^assignment2_evaluation_\d{8}_\d{6}\.json$/.test(n)).sort().pop() : null;
test("the real saved security report has the shape the dashboard reads", { skip: !saved && "no saved security report" }, () => {
  const real = JSON.parse(readFileSync(join(reportsDir, saved), "utf8"));
  assert.equal(isSecurityReport(real), true);
  const rows = metricRows(real);
  assert.equal(rows.length, 9); assert.ok(rows.every((r) => r.denominator > 0 && r.status !== "not measured" && r.definition), JSON.stringify(rows.filter((r) => !r.definition)));
  assert.ok(scenarioList(real).length >= 30 && comparisonRows(real).length >= 8 && limitationList(real).length >= 5 && contextRows(real).every((r) => r.value !== "n/a"));
  assert.ok(categoryRows(real).length === 4 && baselineSources(real).length >= 1);
});
