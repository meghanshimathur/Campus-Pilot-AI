import test from "node:test";
import assert from "node:assert/strict";
import {
  formatMs, formatTokens, percent, formatScore, formatCoverage, clip, humanizeAction, statusTone, statusLabel,
  outcomeTone, outcomeLabel, parseApiError, extractHandbookSearches, toolNames, modeInfo, tokenSummary, formatTimestamp, matchHelp,
} from "../../frontend/js/format.js";

test("formatMs", () => {
  assert.equal(formatMs(null), "—");
  assert.equal(formatMs(undefined), "—");
  assert.equal(formatMs(0), "0 ms");
  assert.equal(formatMs(999), "999 ms");
  assert.equal(formatMs(1500), "1.50 s");
  assert.equal(formatMs("abc"), "—");
});

test("tokens are never invented", () => {
  assert.equal(formatTokens(null, null), "not reported");
  assert.equal(formatTokens(undefined, undefined), "not reported");
  assert.equal(formatTokens(120, 30), "in 120 · out 30");
  assert.equal(formatTokens(120, null), "in 120 · out n/a");
  assert.equal(formatTokens(0, 0), "in 0 · out 0"); // a real zero is shown, not hidden
});

test("percent / score / coverage / clip", () => {
  assert.equal(percent(12, 12), "100%");
  assert.equal(percent(11, 12), "92%");
  assert.equal(percent(0, 0), "n/a");
  assert.equal(formatScore(0.6744), "0.67");
  assert.equal(formatScore(undefined), "n/a");
  assert.equal(formatCoverage(0.824), "82%");
  assert.equal(formatCoverage(null), "n/a");
  assert.equal(clip("abcdef", 4), "abc…");
  assert.equal(clip("abc", 4), "abc");
  assert.equal(clip(null), "");
});

test("labels and tones, with safe fallbacks", () => {
  assert.equal(humanizeAction("answered_directly"), "Answered directly");
  assert.equal(humanizeAction("used_tools"), "Used tools");
  assert.equal(humanizeAction("weird"), "weird");
  assert.equal(humanizeAction(undefined), "—");
  assert.equal(statusTone("success"), "success");
  assert.equal(statusTone("completed_with_tool_errors"), "warning");
  assert.equal(statusTone("error"), "danger");
  assert.equal(statusTone("???"), "neutral");
  assert.equal(statusLabel("completed_with_tool_errors"), "Completed with tool errors");
  assert.equal(outcomeTone("no_evidence"), "info");
  assert.equal(outcomeTone("rejected"), "warning");
  assert.equal(outcomeLabel("ok"), "OK");
  assert.ok(matchHelp("keyword").length > 0);
  assert.equal(matchHelp("nope"), "");
});

test("parseApiError understands FastAPI error shapes", () => {
  assert.equal(parseApiError(503, { detail: "No LLM provider configured. See .env.example." }), "No LLM provider configured. See .env.example.");
  assert.equal(parseApiError(422, { detail: [{ loc: ["body", "message"], msg: "String should have at least 1 character" }] }),
    "message: String should have at least 1 character");
  assert.equal(parseApiError(422, { detail: [{ loc: ["body", "student_id"], msg: "too long" }, { loc: ["body", "session_id"], msg: "bad" }] }),
    "student_id: too long; session_id: bad");
  assert.equal(parseApiError(404, null), "Not found.");
  assert.equal(parseApiError(500, null), "The server hit an error. Please try again.");
  assert.equal(parseApiError(418, {}), "Request failed (HTTP 418).");
});

test("handbook evidence is extracted only from real tool results", () => {
  const found = { tools_called: [{ name: "search_handbook", arguments: { query: "portal failed" },
    result: { found: true, query: "portal failed", chunks: [{ chunk_id: "x#s2-1" }] } }] };
  assert.deepEqual(extractHandbookSearches(found), [{ state: "found", query: "portal failed", chunks: [{ chunk_id: "x#s2-1" }] }]);

  const missing = { tools_called: [{ name: "search_handbook", arguments: { query: "parking" },
    result: { found: false, query: "parking", chunks: [], message: "not found" } }] };
  assert.deepEqual(extractHandbookSearches(missing), [{ state: "not_found", query: "parking", message: "not found" }]);

  const failed = { tools_called: [{ name: "search_handbook", arguments: { query: "q" }, result: { error: "Handbook search is not available." } }] };
  assert.deepEqual(extractHandbookSearches(failed), [{ state: "error", query: "q", message: "Handbook search is not available." }]);

  assert.deepEqual(extractHandbookSearches({ tools_called: [{ name: "check_fee_status", result: {} }] }), []);
  assert.deepEqual(extractHandbookSearches({}), []);
  assert.deepEqual(extractHandbookSearches(null), []);
  assert.deepEqual(extractHandbookSearches({ tools_called: [{ name: "search_handbook", result: { found: true, chunks: "bad" } }] })[0].chunks, []);
});

test("toolNames de-duplicates", () => {
  assert.deepEqual(toolNames({ tools_called: [{ name: "a" }, { name: "b" }, { name: "a" }] }), ["a", "b"]);
  assert.deepEqual(toolNames({}), []);
});

test("offline mode is never presented as model quality", () => {
  const off = modeInfo("offline");
  assert.equal(off.label, "OFFLINE");
  assert.match(off.text, /says nothing about real model quality/);
  assert.match(off.text, /token usage is not available/i);
  assert.equal(modeInfo("live").label, "LIVE");
  assert.doesNotMatch(modeInfo("live").text, /nothing about/);
  assert.equal(modeInfo("weird").label, "WEIRD");
  assert.equal(modeInfo(undefined).label, "UNKNOWN");
});

test("token summary", () => {
  assert.equal(tokenSummary({ runs_with_usage: 0, runs_total: 13 }), "not available (0/13 runs reported usage)");
  assert.equal(tokenSummary(null), "not available (0/0 runs reported usage)");
  assert.match(tokenSummary({ runs_with_usage: 2, runs_total: 4, input_total: 150, output_total: 30, input_mean: 75, output_mean: 15 }),
    /2\/4 runs reported usage · input total 150 \(mean 75\) · output total 30 \(mean 15\)/);
});

test("timestamps", () => {
  assert.equal(formatTimestamp("2026-10-22 00:04:02"), "2026-10-22 00:04:02");
  assert.equal(formatTimestamp("2026-10-22T00:04:02.123"), "2026-10-22 00:04:02");
  assert.equal(formatTimestamp(null), "—");
});

import { countReportsByMode } from "../../frontend/js/format.js";
test("report counts come from the real list and handle unknown modes and empty input", () => {
  assert.deepEqual(countReportsByMode([{ mode: "offline" }, { mode: "offline" }, { mode: "live" }, { mode: "weird" }]), { offline: 2, live: 1, other: 1 });
  assert.deepEqual(countReportsByMode([]), { offline: 0, live: 0, other: 0 });
  assert.deepEqual(countReportsByMode(undefined), { offline: 0, live: 0, other: 0 });
});

import { inconsistentInfo } from "../../frontend/js/format.js";
test("inconsistent reports are never counted as offline or live", () => {
  assert.deepEqual(countReportsByMode([
    { mode: "offline", integrity: "ok" }, { mode: "live", integrity: "inconsistent" }, { mode: "offline", integrity: "inconsistent" },
    { mode: "live", integrity: "ok" }, { mode: "live" },
  ]), { offline: 1, live: 2, other: 2 });   // a report without the field (older API) is treated as before
});

test("the inconsistent banner names the label and the providers that really produced the runs", () => {
  const info = inconsistentInfo({ mode: "live", runs: [{ provider: "offline-scripted" }, { provider: "offline-scripted" }] });
  assert.equal(info.tone, "danger"); assert.equal(info.label, "INCONSISTENT");
  assert.match(info.text, /labelled “live”/); assert.match(info.text, /offline-scripted/); assert.match(info.text, /do not use it as evidence/i);
  assert.match(inconsistentInfo({}).text, /unknown/);
});

import { blockedBannerText, reportStats, runOutcome } from "../../frontend/js/format.js";
test("runOutcome treats runs from before outcomes existed as completed", () => {
  assert.equal(runOutcome({}), "completed"); assert.equal(runOutcome(undefined), "completed");
  assert.equal(runOutcome({ outcome: "blocked_by_provider" }), "blocked_by_provider"); assert.equal(runOutcome({ outcome: "skipped" }), "skipped");
});

test("reportStats falls back for older reports and reads the new fields", () => {
  assert.deepEqual(reportStats({ runs: 13, cases: 12 }), { runs: 13, completed: 13, blocked: 0, skipped: 0, notRun: 0, cases: 12,
    casesEvaluated: 12, casesNotEvaluable: 0, executed: 13, kinds: {} });
  const s = reportStats({ runs: 13, cases: 12, runs_completed: 3, runs_blocked: 2, runs_skipped: 8, cases_evaluated: 3, cases_blocked: 9,
    runs_executed: 5, provider_error_kinds: { quota: 2 } });
  assert.deepEqual([s.completed, s.blocked, s.skipped, s.notRun, s.casesEvaluated, s.casesNotEvaluable, s.executed, s.kinds], [3, 2, 8, 10, 3, 9, 5, { quota: 2 }]);
  assert.equal(reportStats(undefined).runs, 0);
});

test("the blocked-runs banner separates blocked from not-run, never calls them agent failures, and handles zero completed runs", () => {
  const some = blockedBannerText(reportStats({ runs: 13, cases: 12, runs_completed: 3, runs_blocked: 10, runs_skipped: 0, provider_error_kinds: { quota: 10 } }));
  assert.match(some, /Provider errors left 10 of 13 runs unevaluated \(10 blocked: quota ×10\)/);
  assert.match(some, /not counted as agent failures/); assert.match(some, /only the 3 completed runs/);
  const early = blockedBannerText(reportStats({ runs: 13, cases: 12, runs_completed: 1, runs_blocked: 2, runs_skipped: 10, provider_error_kinds: { quota: 2 } }));
  assert.match(early, /left 12 of 13 runs unevaluated \(2 blocked: quota ×2; 10 not run after the evaluation stopped early\)/); assert.match(early, /only the 1 completed run\b/);
  const none = blockedBannerText(reportStats({ runs: 13, cases: 12, runs_completed: 0, runs_blocked: 13, runs_skipped: 0 }));
  assert.match(none, /\(13 blocked\)/); assert.match(none, /No run completed, so this evaluation measured nothing about the agent/);
  assert.doesNotMatch(none + some + early, /\.\./);
});
