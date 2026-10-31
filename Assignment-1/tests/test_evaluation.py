import importlib.util
import json
from pathlib import Path

import pytest

from campuspilot.config import ROOT
from campuspilot.evaluation import (Case, ScriptedProvider, Turn, _call, _say, build_cases, check_fee, check_not_found,
                                    compute_metrics, render_markdown, run_cases)
from campuspilot.memory import MemoryStore
from campuspilot.provider import ChatResponse
from campuspilot.tool_registry import ToolRegistry
from campuspilot.tracing import TraceStore


@pytest.fixture
def env(settings, retriever):
    mem, tracer = MemoryStore(settings.database_path), TraceStore(settings.database_path)
    return ToolRegistry(settings.data_dir, settings.database_path, retriever, mem), mem, tracer


def row(**kw):
    base = dict(case_id="c", category="x", turn=1, message="m", expected_tools=[], called_tools=[], tool_outcomes=[],
                tool_choice_appropriate=True, check_passed=True, note="n", run_error=None, success=True, response="r",
                latency_ms=10, input_tokens=None, output_tokens=None, evidence_ids=[], cited_handbook=False,
                trace_recorded=True, provider="p", model="m")
    base.update(kw)
    return base


def test_cases_cover_required_scenarios():
    cases = build_cases()
    cats = {c.category for c in cases}
    assert {"fee status", "handbook policy", "missing evidence", "memory recall", "tool error"} <= cats
    assert len({c.case_id for c in cases}) == len(cases) >= 10
    assert all(t.script for c in cases for t in c.turns)  # every case can run offline
    assert all(t.check for c in cases for t in c.turns)


def test_offline_pipeline_runs_and_records_traces(env, settings):
    registry, mem, tracer = env
    rows = run_cases(build_cases(), registry, mem, tracer)
    by = {(r["case_id"], r["turn"]): r for r in rows}
    assert len(rows) == 13
    # cases that don't depend on borderline retrieval scores must pass in the offline pipeline
    for key in [("fee_status", 1), ("scholarship_status", 1), ("unknown_student_error", 1), ("memory_recall", 1),
                ("memory_recall", 2), ("sensitive_not_stored", 1), ("direct_answer", 1), ("support_request", 1),
                ("office_hours", 1), ("missing_parking", 1), ("policy_portal_failure", 1)]:
        assert by[key]["success"], (key, by[key]["note"])
    assert all(r["trace_recorded"] for r in rows)
    assert all(r["input_tokens"] is None and r["output_tokens"] is None for r in rows)  # nothing invented
    assert all(r["provider"] == "offline-scripted" for r in rows)


def test_memory_recall_case_spans_sessions(env):
    registry, mem, tracer = env
    rows = run_cases([c for c in build_cases() if c.case_id == "memory_recall"], registry, mem, tracer)
    assert [r["called_tools"] for r in rows] == [["remember_preference"], []]
    assert mem.recall("STU004")[0]["value"] == "Dev"


def test_sensitive_case_stores_nothing(env):
    registry, mem, tracer = env
    rows = run_cases([c for c in build_cases() if c.case_id == "sensitive_not_stored"], registry, mem, tracer)
    assert rows[0]["success"] and rows[0]["tool_outcomes"] == ["ok"] and mem.recall("STU001") == []


def test_negative_control_harness_detects_a_bad_agent(env):
    """A model that invents a parking policy without searching must be scored as a failure."""
    registry, mem, tracer = env
    bad = Case("bad", "missing evidence", "STU001", [Turn(
        "Where can I park my car on campus?", frozenset({"search_handbook"}), check=check_not_found,
        script=[_say("Parking is free in Lot B from 8am to 6pm.")])])
    r = run_cases([bad], registry, mem, tracer)[0]
    assert r["tool_choice_appropriate"] is False and r["check_passed"] is False and r["success"] is False


def test_negative_control_wrong_numbers_and_extra_tools(env):
    registry, mem, tracer = env
    wrong = Case("w", "fee status", "STU003", [Turn(
        "fee status?", frozenset({"check_fee_status"}), check=check_fee,
        script=[_call("check_fee_status", {}), _say("You owe Rs. 5000.")])])
    r = run_cases([wrong], registry, mem, tracer)[0]
    assert r["tool_choice_appropriate"] is True and r["check_passed"] is False and r["success"] is False
    extra = Case("e", "fee status", "STU003", [Turn(
        "fee status?", frozenset({"check_fee_status"}), check=check_fee,
        script=[ChatResponse(tool_calls=[_call("check_fee_status", {}).tool_calls[0],
                                         _call("get_campus_events", {}, "c2").tool_calls[0]]),
                _say("Overdue: 300000 due.")])])
    r = run_cases([extra], registry, mem, tracer)[0]
    assert r["tool_choice_appropriate"] is False and r["check_passed"] is True and r["success"] is False


def test_agent_error_run_is_failure_and_counted(env):
    registry, mem, tracer = env
    boom = Case("b", "direct answer", "STU001", [Turn("hi", check=lambda r, c: (True, "ok"), script=[])])
    r = run_cases([boom], registry, mem, tracer)[0]  # empty script -> provider raises IndexError
    assert r["run_error"] == "llm_error: IndexError" and r["success"] is False and r["trace_recorded"]


def test_broken_check_fails_instead_of_crashing(env):
    registry, mem, tracer = env
    def bad_check(r, c): raise KeyError("x")
    case = Case("k", "direct answer", "STU001", [Turn("hi", check=bad_check, script=[_say("hello")])])
    r = run_cases([case], registry, mem, tracer)[0]
    assert r["check_passed"] is False and "KeyError" in r["note"]


# ---- metrics: exact numbers from synthetic rows ----
def test_metrics_exact_values():
    rows = [row(case_id="a", latency_ms=10, input_tokens=100, output_tokens=20, evidence_ids=["x#s1"], cited_handbook=True),
            row(case_id="a", turn=2, latency_ms=30, success=False, check_passed=False, note="bad"),
            row(case_id="b", latency_ms=20, input_tokens=50, output_tokens=10, tool_outcomes=["error"],
                tool_choice_appropriate=False, success=False),
            row(case_id="c", latency_ms=40, evidence_ids=["x#s2"], cited_handbook=False, trace_recorded=False)]
    m = compute_metrics(rows)
    assert (m["runs"], m["cases"], m["cases_passed"], m["runs_passed"]) == (4, 3, 1, 2)
    assert m["case_success_rate"] == 0.333 and m["run_success_rate"] == 0.5
    assert m["tool_choice_appropriate"] == 3 and m["tool_errors_observed"] == 1 and m["traces_recorded"] == 3
    assert m["latency_ms"] == {"n": 4, "min": 10, "mean": 25.0, "median": 25.0, "max": 40}
    tk = m["tokens"]  # only the 2 runs that reported usage contribute; the rest are not estimated
    assert (tk["runs_with_usage"], tk["runs_total"], tk["input_total"], tk["output_total"]) == (2, 4, 150, 30)
    assert (tk["input_mean"], tk["output_mean"]) == (75.0, 15.0)
    assert (m["handbook_runs_with_evidence"], m["handbook_cited"], m["handbook_citation_rate"]) == (2, 1, 0.5)
    assert len(m["failed_runs"]) == 2 and m["by_category"]["x"] == {"passed": 2, "runs": 4, "blocked": 0}


def test_metrics_token_unavailable_is_none_not_zero():
    m = compute_metrics([row(), row(case_id="d")])
    assert m["tokens"]["runs_with_usage"] == 0 and m["tokens"]["input_total"] is None and m["tokens"]["input_mean"] is None
    assert m["handbook_citation_rate"] is None


def test_metrics_empty_input_does_not_crash():
    m = compute_metrics([])
    assert m["runs"] == 0 and m["case_success_rate"] is None and m["latency_ms"] is None


def test_partial_token_reporting_handled():
    m = compute_metrics([row(input_tokens=10, output_tokens=None), row(case_id="e", input_tokens=30, output_tokens=None)])
    assert m["tokens"]["input_total"] == 40 and m["tokens"]["output_mean"] is None


# ---- report ----
META = {"timestamp": "2026-01-01 00:00:00", "provider": "p", "model": "m"}


def test_offline_report_is_labelled_and_claims_no_tokens():
    rows = [row()]
    md = render_markdown("offline", rows, compute_metrics(rows), META)
    assert "OFFLINE mode — pipeline check only" in md and "not available (0/1 runs reported usage)" in md
    assert "says nothing about how well a real model" in md


def test_live_report_shows_reported_tokens_only():
    rows = [row(input_tokens=120, output_tokens=30), row(case_id="z")]
    md = render_markdown("live", rows, compute_metrics(rows), META)
    assert "OFFLINE" not in md and "1/2 runs had it" in md and "input total 120" in md


# ---- the CLI script ----
def load_script():
    spec = importlib.util.spec_from_file_location("run_evaluation", ROOT / "scripts" / "run_evaluation.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_refuses_live_without_provider(settings, tmp_path, monkeypatch, capsys):
    mod = load_script()
    monkeypatch.setattr(mod, "load_settings", lambda: settings)  # llm_provider None, no keys
    assert mod.main(["--out-dir", str(tmp_path / "r")]) == 2
    assert "cannot run" in capsys.readouterr().out and not (tmp_path / "r").exists()
    assert mod.main(["--live", "--out-dir", str(tmp_path / "r")]) == 2


def test_script_offline_writes_reports_and_leaves_real_db_untouched(settings, retriever, tmp_path, monkeypatch, capsys):
    mod = load_script()
    monkeypatch.setattr(mod, "load_settings", lambda: settings)
    monkeypatch.setattr(mod, "HandbookRetriever", lambda *a, **k: retriever)
    code = mod.main(["--offline", "--out-dir", str(tmp_path / "r")])
    assert code in (0, 1)
    md = next((tmp_path / "r").glob("evaluation_offline_*.md")).read_text()
    data = json.loads(next((tmp_path / "r").glob("evaluation_offline_*.json")).read_text())
    assert "OFFLINE mode" in md and data["mode"] == "offline" and data["metrics"]["runs"] == 13
    from campuspilot.db import connect
    conn = connect(settings.database_path)
    assert conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0] == 0  # eval used its own throw-away DB
    assert conn.execute("SELECT COUNT(*) FROM support_requests").fetchone()[0] == 0
    conn.close()
    assert "AIza" not in md and "sk-" not in md


# ---- the 'not found' wording heuristic ----
@pytest.mark.parametrize("text", [
    "The handbook does not contain information about visa extensions.", "I could not find anything about parking.",
    "That isn't covered in the handbook.", "Sorry, this is not in the student handbook.", "The handbook doesn't mention it.",
    "I don't have information on that.", "No relevant information was found.", "There is no mention of this policy.",
])
def test_not_found_wording_recognised(text):
    from campuspilot.evaluation import NOT_FOUND_RE
    assert NOT_FOUND_RE.search(text), text


@pytest.mark.parametrize("text", [
    "Parking is free in Lot B from 8am to 6pm.", "Yes, the hostel gate closes at 22:30.",
    "You need 75% attendance [student_handbook.md#s6-1].", "Visa extensions take 10 working days.",
])
def test_invented_or_normal_answers_do_not_look_like_not_found(text):
    from campuspilot.evaluation import NOT_FOUND_RE
    assert not NOT_FOUND_RE.search(text), text


def test_not_found_check_distinguishes_evidence_from_wording():
    from campuspilot.agent import AgentResult
    leaked = AgentResult(response="Not found.", evidence=[{"chunk_id": "x"}])
    assert check_not_found(leaked, {})[0] is False and "returned handbook evidence" in check_not_found(leaked, {})[1]
    wording = AgentResult(response="Parking is free.")
    assert check_not_found(wording, {})[0] is False and "does not clearly say" in check_not_found(wording, {})[1]
    assert check_not_found(AgentResult(response="That is not in the handbook."), {})[0] is True


# ---- report integrity: a report's label must agree with who actually produced its runs ----
def _rep(mode, run_providers, meta_provider=None):
    return {"mode": mode, "meta": {"provider": meta_provider or run_providers[0], "timestamp": "t"},
            "runs": [{"provider": p} for p in run_providers], "metrics": {"runs": len(run_providers)}}


def test_report_integrity_rule():
    from campuspilot.evaluation import report_integrity
    assert report_integrity(_rep("offline", ["offline-scripted"] * 3)) == "ok"
    assert report_integrity(_rep("live", ["gemini"] * 3)) == "ok"
    assert report_integrity(_rep("live", ["openai", "openai"])) == "ok"
    # a live run in which every call failed still names its real provider, so it stays 'ok' (and shows its failures)
    assert report_integrity(_rep("live", ["gemini"])) == "ok"
    # the confirmed hole: scripted results saved/labelled as live
    assert report_integrity(_rep("live", ["offline-scripted"] * 3)) == "inconsistent"
    assert report_integrity(_rep("live", ["gemini", "offline-scripted"])) == "inconsistent"     # mixed
    assert report_integrity(_rep("live", ["gemini"], meta_provider="offline-scripted")) == "inconsistent"
    # the reverse: real-model runs labelled offline
    assert report_integrity(_rep("offline", ["gemini"] * 3)) == "inconsistent"
    assert report_integrity(_rep("offline", ["offline-scripted", "gemini"])) == "inconsistent"


@pytest.mark.parametrize("bad", [None, {}, {"mode": "offline"}, {"mode": "weird", "meta": {"provider": "x"}, "runs": []},
                                 {"mode": "live", "meta": {}, "runs": []}, {"mode": "live", "meta": None, "runs": None},
                                 {"mode": "live", "meta": {"provider": "gemini"}, "runs": [None]}, "text", 42, []])
def test_report_integrity_never_crashes_and_never_trusts_garbage(bad):
    from campuspilot.evaluation import report_integrity
    assert report_integrity(bad) == "inconsistent"


def test_real_saved_offline_reports_pass_the_integrity_rule():
    from campuspilot.evaluation import report_integrity
    reports = sorted((ROOT / "reports").glob("evaluation_offline_*.json"))
    assert reports
    for f in reports:
        assert report_integrity(json.loads(f.read_text())) == "ok", f.name


# =====================================================================================================
# Provider failures (quota/outage) are reported separately from agent behaviour failures
# =====================================================================================================
import httpx  # noqa: E402
from google.genai import errors as genai_errors  # noqa: E402

from campuspilot.evaluation import normalize_report  # noqa: E402
from campuspilot.provider import LLMProvider  # noqa: E402


def _api_error(code, status="X", message="provider says no"):
    body = {"error": {"code": code, "status": status, "message": message}}
    return (genai_errors.ServerError if code >= 500 else genai_errors.ClientError)(code, body, httpx.Response(code, json=body))


class Refusing(LLMProvider):
    """A live-style provider that always fails with the given exception; counts how often it was called."""
    name = "gemini"

    def __init__(self, exc):
        self.exc, self.calls = exc, 0

    def chat(self, messages, tool_schemas):
        self.calls += 1
        raise self.exc


class WorksThenRefuses(LLMProvider):
    """Answers like the offline script for the first `ok_turns` cases' calls, then fails with `exc`."""
    name = "gemini"

    def __init__(self, ok_cases, exc):
        self.queue = [r for c in build_cases()[:ok_cases] for t in c.turns for r in t.script]
        self.exc, self.calls = exc, 0

    def chat(self, messages, tool_schemas):
        self.calls += 1
        if self.queue:
            return self.queue.pop(0)
        raise self.exc


def test_quota_errors_are_blocked_runs_not_agent_failures(env):
    registry, mem, tracer = env
    rows = run_cases(build_cases(), registry, mem, tracer, Refusing(_api_error(429, "RESOURCE_EXHAUSTED")))
    assert len(rows) == 13 and all(r["outcome"] == "blocked_by_provider" and r["error_kind"] == "quota" for r in rows)
    assert not any(r["success"] for r in rows)
    m = compute_metrics(rows)
    assert (m["runs"], m["runs_completed"], m["runs_blocked"], m["runs_skipped"]) == (13, 0, 13, 0)
    assert m["run_success_rate"] is None and m["case_success_rate"] is None and m["tool_choice_rate"] is None  # nothing was measured
    assert m["cases_evaluated"] == 0 and m["cases_blocked"] == 12 and m["failed_runs"] == [] and len(m["blocked_runs"]) == 13
    assert m["provider_error_kinds"] == {"quota": 13} and m["latency_ms"] is None and m["tokens"]["runs_total"] == 0


@pytest.mark.parametrize("code,kind", [(429, "quota"), (503, "unavailable"), (500, "unavailable"), (401, "config"), (403, "config"), (404, "config")])
def test_every_provider_side_error_kind_is_excused(env, code, kind):
    registry, mem, tracer = env
    rows = run_cases(build_cases()[:1], registry, mem, tracer, Refusing(_api_error(code)))
    assert rows[0]["outcome"] == "blocked_by_provider" and rows[0]["error_kind"] == kind


@pytest.mark.parametrize("exc", [_api_error(400, "INVALID_ARGUMENT"), RuntimeError("bug in our code"), ValueError("x")])
def test_errors_that_may_be_our_own_bug_still_count_as_agent_failures(env, exc):
    registry, mem, tracer = env
    rows = run_cases(build_cases()[:1], registry, mem, tracer, Refusing(exc))
    assert rows[0]["outcome"] == "completed" and rows[0]["success"] is False and rows[0]["error_kind"] in ("request", "unknown")
    m = compute_metrics(rows)
    assert m["runs_completed"] == 1 and m["runs_blocked"] == 0 and len(m["failed_runs"]) == 1 and m["run_success_rate"] == 0.0


def test_success_rates_use_only_eligible_completed_runs(env):
    registry, mem, tracer = env
    provider = WorksThenRefuses(3, _api_error(429, "RESOURCE_EXHAUSTED"))   # fee, scholarship, portal answered; then quota
    rows = run_cases(build_cases(), registry, mem, tracer, provider)
    m = compute_metrics(rows)
    assert (m["runs_completed"], m["runs_blocked"], m["runs_passed"]) == (3, 10, 3)
    assert m["run_success_rate"] == 1.0                                       # 3/3 of the eligible runs, not 3/13
    assert (m["cases_evaluated"], m["cases_blocked"], m["cases_passed"], m["case_success_rate"]) == (3, 9, 3, 1.0)
    assert (m["tool_choice_appropriate"], m["tool_choice_rate"]) == (3, 1.0)
    assert m["failed_runs"] == [] and len(m["blocked_runs"]) == 10
    assert m["latency_ms"]["n"] == 3 and m["tokens"]["runs_total"] == 3        # blocked runs do not dilute latency/token stats
    assert m["traces_recorded"] == 13 and m["runs_executed"] == 13             # blocked runs still leave a trace
    assert m["by_category"]["fee status"] == {"passed": 2, "runs": 2, "blocked": 0}
    assert m["by_category"]["memory recall"] == {"passed": 0, "runs": 0, "blocked": 2}


def test_a_case_with_one_blocked_turn_is_not_evaluable(env):
    registry, mem, tracer = env
    memory_case = [c for c in build_cases() if c.case_id == "memory_recall"]
    prov = WorksThenRefuses(0, _api_error(503, "UNAVAILABLE"))
    prov.queue = list(memory_case[0].turns[0].script)                         # turn 1 answered, turn 2 hits the outage
    rows = run_cases(memory_case, registry, mem, tracer, prov)
    assert [r["outcome"] for r in rows] == ["completed", "blocked_by_provider"]
    m = compute_metrics(rows)
    assert m["cases_evaluated"] == 0 and m["cases_blocked"] == 1 and m["case_success_rate"] is None
    assert m["runs_completed"] == 1 and m["runs_passed"] == 1


def test_circuit_breaker_stops_hammering_a_refusing_provider(env):
    registry, mem, tracer = env
    provider = Refusing(_api_error(429, "RESOURCE_EXHAUSTED"))
    rows = run_cases(build_cases(), registry, mem, tracer, provider, stop_after_provider_errors=2)
    assert [r["outcome"] for r in rows] == ["blocked_by_provider"] * 2 + ["skipped"] * 11
    assert provider.calls == 2                                                 # the 11 skipped turns made NO request
    skipped = rows[-1]
    assert skipped["trace_recorded"] is False and skipped["provider"] == "gemini" and "not run" in skipped["note"]
    m = compute_metrics(rows)
    assert (m["runs_blocked"], m["runs_skipped"], m["runs_executed"], m["traces_recorded"]) == (2, 11, 2, 2)
    assert m["run_success_rate"] is None and m["failed_runs"] == []


def test_breaker_counts_only_consecutive_blocked_turns_and_is_off_by_default(env):
    registry, mem, tracer = env
    prov = WorksThenRefuses(1, _api_error(503, "UNAVAILABLE"))                 # turn 1 ok, then outage
    rows = run_cases(build_cases(), registry, mem, tracer, prov, stop_after_provider_errors=3)
    assert [r["outcome"] for r in rows][:4] == ["completed", "blocked_by_provider", "blocked_by_provider", "blocked_by_provider"]
    assert all(r["outcome"] == "skipped" for r in rows[4:])
    off = run_cases(build_cases()[:4], registry, mem, tracer, Refusing(_api_error(503)))      # default: never stops
    assert all(r["outcome"] == "blocked_by_provider" for r in off)


def test_offline_rows_are_all_completed_and_unchanged(env):
    registry, mem, tracer = env
    rows = run_cases(build_cases(), registry, mem, tracer)
    assert {r["outcome"] for r in rows} == {"completed"} and {r["error_kind"] for r in rows} == {None}
    m = compute_metrics(rows)
    assert (m["runs_completed"], m["runs_blocked"], m["runs_skipped"], m["cases_evaluated"], m["cases_blocked"]) == (13, 0, 0, 12, 0)


def test_metrics_exact_numbers_with_mixed_outcomes():
    rows = [row(case_id="a", latency_ms=10, input_tokens=100, output_tokens=20),
            row(case_id="b", latency_ms=30, success=False, check_passed=False, note="wrong answer"),
            row(case_id="c", outcome="blocked_by_provider", error_kind="quota", latency_ms=9999, success=False,
                tool_choice_appropriate=False, run_error="llm_error: ClientError 429", note="blocked"),
            row(case_id="d", outcome="skipped", success=False, latency_ms=0, trace_recorded=False, run_error="not run")]
    m = compute_metrics(rows)
    assert (m["runs"], m["runs_completed"], m["runs_blocked"], m["runs_skipped"]) == (4, 2, 1, 1)
    assert (m["runs_passed"], m["run_success_rate"]) == (1, 0.5)               # 1 of the 2 ELIGIBLE runs
    assert (m["cases"], m["cases_evaluated"], m["cases_blocked"], m["cases_passed"], m["case_success_rate"]) == (4, 2, 2, 1, 0.5)
    assert m["tool_choice_appropriate"] == 2 and m["tool_choice_rate"] == 1.0
    assert m["latency_ms"] == {"n": 2, "min": 10, "mean": 20, "median": 20.0, "max": 30}   # the 9999 ms blocked run is excluded
    assert m["tokens"]["runs_total"] == 2 and m["tokens"]["runs_with_usage"] == 1
    assert m["failed_runs"] == ["b#1: wrong answer"] and len(m["blocked_runs"]) == 2
    assert (m["traces_recorded"], m["runs_executed"], m["runs_with_agent_error"]) == (3, 3, 0)   # the skipped run is not counted
    assert m["provider_error_kinds"] == {"quota": 1}


def test_rows_from_before_outcomes_existed_are_all_completed():
    m = compute_metrics([row(case_id="a"), row(case_id="b", success=False)])
    assert (m["runs_completed"], m["runs_blocked"], m["run_success_rate"], m["cases_evaluated"]) == (2, 0, 0.5, 2)


# ---- report text ----
def test_markdown_for_a_partly_blocked_live_run_separates_the_two_kinds_of_failure():
    rows = [row(case_id="a", provider="gemini"), row(case_id="b", success=False, check_passed=False, note="wrong", provider="gemini"),
            row(case_id="c", outcome="blocked_by_provider", error_kind="quota", success=False, note="blocked by provider (quota)",
                run_error="llm_error: ClientError 429 RESOURCE_EXHAUSTED", provider="gemini"),
            row(case_id="d", outcome="skipped", success=False, note="not run", run_error="not run: provider unavailable", provider="gemini")]
    m = compute_metrics(rows)
    md = render_markdown("live", rows, m, {**META, "provider": "gemini", "pacing": {"min_interval": 6.0, "quota_delays": [10, 20, 40], "max_wait": 90.0, "stop_after": 2}})
    assert "**Provider errors left 2 of 4 runs unevaluated** (1 blocked: quota x1; 1 not run after the evaluation stopped early)" in md and "**not** counted as agent failures" in md and "only the 2 completed runs" in md
    assert "| Runs completed (reached the model and finished) | 2/4 (50%) |" in md and "| Runs blocked by provider errors | 1 |" in md and "| Runs not run (stopped early) | 1 |" in md
    assert "Task success (cases fully evaluated and passed) | 1/2 (50%)" in md and "Task success (completed runs) | 1/2 (50%)" in md
    assert "| BLOCKED |" in md and "| SKIPPED |" in md and md.count("| FAIL |") == 1            # only the genuine failure is a FAIL
    assert "## Blocked or not run (excluded from success rates)" in md and "b#1: wrong" in md.split("## Failures")[1].split("## Blocked")[0]
    assert "Pacing: at least 6.0s between model requests; HTTP 429 backoff 10/20/40s" in md
    assert "excluded from every success rate" in md


def test_markdown_says_so_when_no_run_completed():
    rows = [row(case_id="a", outcome="blocked_by_provider", error_kind="quota", success=False, provider="gemini")]
    md = render_markdown("live", rows, compute_metrics(rows), {**META, "provider": "gemini"})
    assert "No run completed, so this evaluation measured nothing about the agent." in md and "Task success (completed runs) | n/a" in md


def test_markdown_for_a_clean_live_run_has_no_blocked_section_but_shows_completion():
    rows = [row(case_id="a", provider="gemini"), row(case_id="b", provider="gemini")]
    md = render_markdown("live", rows, compute_metrics(rows), {**META, "provider": "gemini"})
    assert "| Runs completed (reached the model and finished) | 2/2 (100%) |" in md and "Provider errors blocked" not in md
    assert "Blocked or not run" not in md and "Task success (individual runs)" in md


def test_offline_report_text_is_unchanged_byte_for_byte():
    """Re-rendering a real saved OFFLINE report from its saved rows must reproduce the saved Markdown exactly."""
    saved = sorted((ROOT / "reports").glob("evaluation_offline_20261022_000402.json"))[0]
    data = json.loads(saved.read_text())
    again = render_markdown("offline", data["runs"], compute_metrics(data["runs"]), data["meta"])
    assert again == saved.with_suffix(".md").read_text()
    assert "blocked" not in again.lower().replace("blocked_by", "")  # no new provider-error wording in offline reports


# ---- legacy reports (saved before outcomes existed) ----
def _legacy_live_report(n_blocked=10, kind_text="llm_error: ClientError 429 RESOURCE_EXHAUSTED: You exceeded your current quota"):
    data = json.loads(sorted((ROOT / "reports").glob("evaluation_offline_*.json"))[-1].read_text())
    data["mode"], data["meta"]["provider"] = "live", "gemini"
    for i, r in enumerate(data["runs"]):
        r["provider"] = "gemini"
        r.pop("outcome", None); r.pop("error_kind", None)
        if i >= len(data["runs"]) - n_blocked:
            r.update(success=False, tool_choice_appropriate=False, check_passed=False, run_error=kind_text, called_tools=[], note="old note")
    data["metrics"] = {k: v for k, v in data["metrics"].items() if k not in ("runs_completed", "runs_blocked", "runs_skipped")}
    return data


def test_legacy_live_report_is_normalised_at_read_time_without_touching_the_input():
    legacy = _legacy_live_report()
    before = json.dumps(legacy, sort_keys=True)
    out = normalize_report(legacy)
    assert json.dumps(legacy, sort_keys=True) == before and out is not legacy                       # input untouched
    m = out["metrics"]
    assert (m["runs_completed"], m["runs_blocked"], m["provider_error_kinds"]) == (3, 10, {"quota": 10})
    assert (m["runs_passed"], m["run_success_rate"]) == (3, 1.0) and m["failed_runs"] == []
    assert "Derived when read" in out["normalized"] and "unchanged" in out["normalized"]
    assert {r["outcome"] for r in out["runs"]} == {"completed", "blocked_by_provider"}


def test_normalisation_leaves_other_reports_alone():
    offline = json.loads(sorted((ROOT / "reports").glob("evaluation_offline_*.json"))[-1].read_text())
    assert normalize_report(offline) is offline                                                     # nothing provider-side to separate
    new_format = _legacy_live_report(); new_format["runs"][0]["outcome"] = "completed"
    assert normalize_report(new_format) is new_format                                               # already has outcomes
    request_error = _legacy_live_report(kind_text="llm_error: ClientError 400 INVALID_ARGUMENT: bad request")
    assert normalize_report(request_error) is request_error                                         # a 400 may be our bug: not excused
    runtime = _legacy_live_report(kind_text="llm_error: RuntimeError")
    assert normalize_report(runtime) is runtime


@pytest.mark.parametrize("junk", [None, {}, {"runs": None}, {"runs": []}, {"runs": [None]}, {"runs": ["x"]}, "text", 3, []])
def test_normalisation_never_crashes_on_garbage(junk):
    assert normalize_report(junk) is junk


def test_503_and_auth_errors_in_legacy_reports_are_recognised_too():
    for text, kind in [("llm_error: ServerError 503 UNAVAILABLE: high demand", "unavailable"),
                       ("llm_error: ClientError 403 PERMISSION_DENIED: no", "config"),
                       ("llm_error: ClientError 404 NOT_FOUND: model gone", "config")]:
        out = normalize_report(_legacy_live_report(n_blocked=2, kind_text=text))
        assert out["metrics"]["provider_error_kinds"] == {kind: 2}


def test_api_serves_the_normalised_view_and_flags_integrity(settings, tmp_path):
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from campuspilot.api import create_app
    d = tmp_path / "reports"; d.mkdir()
    saved = d / "evaluation_live_20261023_003131.json"
    saved.write_text(json.dumps(_legacy_live_report()))
    raw_before = saved.read_text()
    c = TestClient(create_app(replace(settings, reports_dir=d)))
    got = c.get("/api/evaluation/reports/evaluation_live_20261023_003131.json").json()
    assert got["integrity"] == "ok" and got["metrics"]["runs_blocked"] == 10 and got["metrics"]["runs_completed"] == 3 and "normalized" in got
    assert saved.read_text() == raw_before                                                          # the saved file is never rewritten
    assert c.get("/api/evaluation/reports").json()["reports"][0]["runs"] == 13


# =====================================================================================================
# The script: offline never touches a provider/network; live passes the pacing policy and reports honestly
# =====================================================================================================
def _script():
    spec = importlib.util.spec_from_file_location("run_evaluation_pacing", ROOT / "scripts" / "run_evaluation.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def script(settings, retriever, monkeypatch):
    mod = _script()
    monkeypatch.setattr(mod, "load_settings", lambda: settings)
    monkeypatch.setattr(mod, "HandbookRetriever", lambda *a, **k: retriever)
    return mod


def test_offline_evaluation_never_builds_a_provider_sleeps_or_opens_a_socket(script, tmp_path, monkeypatch):
    import socket
    import time as _time

    def forbidden(*a, **k):
        raise AssertionError("offline evaluation must not do this")
    monkeypatch.setattr(script, "build_provider", forbidden)                  # no real provider, so no SDK client either
    monkeypatch.setattr("google.genai.Client", forbidden)
    monkeypatch.setattr("openai.OpenAI", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)                  # no network at all
    monkeypatch.setattr(_time, "sleep", forbidden)                            # and no pacing/backoff waits
    code = script.main(["--offline", "--out-dir", str(tmp_path / "r"), "--min-interval", "9", "--quota-retries", "5", "--stop-after", "1"])
    data = json.loads(next((tmp_path / "r").glob("evaluation_offline_*.json")).read_text())
    assert code in (0, 1) and data["mode"] == "offline" and "pacing" not in data["meta"]
    assert data["metrics"]["runs_blocked"] == 0 and data["metrics"]["runs_skipped"] == 0 and data["metrics"]["runs_completed"] == 13


def test_live_run_passes_cli_pacing_to_the_provider_and_records_it(script, tmp_path, monkeypatch):
    seen, prov = {}, Refusing(_api_error(429, "RESOURCE_EXHAUSTED"))

    def fake_build(settings, **policy):
        seen.update(policy)
        return prov
    monkeypatch.setattr(script, "build_provider", fake_build)
    code = script.main(["--live", "--out-dir", str(tmp_path / "r"), "--min-interval", "2.5", "--quota-retries", "3",
                        "--quota-base-delay", "4", "--max-wait", "30", "--stop-after", "2"])
    assert seen["min_interval"] == 2.5 and seen["quota_delays"] == (4.0, 8.0, 16.0) and seen["max_wait"] == 30.0 and seen["jitter"] == 1.0
    data = json.loads(next((tmp_path / "r").glob("evaluation_live_*.json")).read_text())
    md = next((tmp_path / "r").glob("evaluation_live_*.md")).read_text()
    assert data["meta"]["pacing"] == {"min_interval": 2.5, "quota_delays": [4.0, 8.0, 16.0], "max_wait": 30.0, "stop_after": 2}
    m = data["metrics"]
    assert (m["runs_completed"], m["runs_blocked"], m["runs_skipped"]) == (0, 2, 11) and prov.calls == 2
    assert code == 3                                                            # inconclusive, not "failed"
    assert "**Provider errors left 13 of 13 runs unevaluated** (2 blocked: quota x2; 11 not run after the evaluation stopped early)" in md and "measured nothing about the agent" in md
    from campuspilot.evaluation import report_integrity
    assert report_integrity(data) == "ok"


def test_default_live_policy_is_paced_and_quota_aware(script, tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(script, "build_provider", lambda settings, **p: (seen.update(p), Refusing(_api_error(429)))[1])
    script.main(["--live", "--out-dir", str(tmp_path / "r")])
    assert seen["min_interval"] == 6.0 and seen["quota_delays"] == (10.0, 20.0, 40.0) and seen["max_wait"] == 90.0


def test_exit_codes_distinguish_failed_from_inconclusive(script, tmp_path, monkeypatch):
    # all completed runs pass but the provider blocks the rest -> 3 (inconclusive)
    monkeypatch.setattr(script, "build_provider", lambda s, **p: WorksThenRefuses(3, _api_error(429)))
    assert script.main(["--live", "--out-dir", str(tmp_path / "a"), "--stop-after", "0"]) == 3
    # a completed run genuinely fails -> 1, even if other runs were blocked
    bad = WorksThenRefuses(3, _api_error(429)); bad.queue[1] = ChatResponse(content="I do not know.")   # fee turn: no tool, wrong answer
    monkeypatch.setattr(script, "build_provider", lambda s, **p: bad)
    assert script.main(["--live", "--out-dir", str(tmp_path / "b"), "--stop-after", "0"]) == 1


def test_script_still_refuses_to_run_live_without_a_provider(script, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(script, "build_provider", lambda s, **p: None)
    assert script.main(["--live", "--out-dir", str(tmp_path / "r")]) == 2 and not (tmp_path / "r").exists()


@pytest.mark.parametrize("flag", ["--min-interval", "--quota-retries", "--quota-base-delay", "--max-wait", "--stop-after"])
def test_negative_pacing_options_are_rejected(script, tmp_path, flag):
    with pytest.raises(SystemExit) as e:
        script.main(["--live", flag, "-1", "--out-dir", str(tmp_path / "r")])
    assert e.value.code == 2


def test_trace_count_can_never_exceed_the_runs_that_actually_executed():
    """Regression: a skipped row (never executed) carrying a stale trace flag gave '13/6 = 217%'."""
    rows = [row(case_id="a"), row(case_id="b", outcome="blocked_by_provider", error_kind="quota", success=False),
            row(case_id="c", outcome="skipped", success=False, trace_recorded=True), row(case_id="d", outcome="skipped", success=False, trace_recorded=True)]
    m = compute_metrics(rows)
    assert (m["traces_recorded"], m["runs_executed"]) == (2, 2) and m["traces_recorded"] <= m["runs_executed"]
