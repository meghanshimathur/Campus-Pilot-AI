"""The Assignment 2 security evaluation (Batch 5): that it runs, that its numbers mean something, that it can actually DETECT a
broken control (mutation tests), and that its report is honest, safe and non-destructive. Offline; temporary databases."""
import dataclasses
import hashlib
import importlib.util
import json
import socket
import types
from datetime import timedelta
from pathlib import Path

import pytest

from auth_helpers import client_for
from campuspilot import security_evaluation as se
from campuspilot import security_report as sr
from campuspilot.audit import AuditStore
from campuspilot.authz import Decision
from campuspilot.config import ROOT
from campuspilot.tracing import TraceStore

REAL_REPORTS = ROOT / "reports"
IDS = {s[0]: s for s in se.SCENARIOS}


def subset(*ids):
    return [IDS[i] for i in ids]


def metrics_for(*ids):
    return se.compute_security_metrics(se.run_all(scenarios=subset(*ids)))


@pytest.fixture(scope="module")
def full_run():
    """One full run, with every way out of the process closed: no network connection, no provider, no environment settings."""
    import campuspilot.api as api

    def no_network(*a, **k):
        raise AssertionError("the evaluation tried to open a network connection")

    def no_env(*a, **k):
        raise AssertionError("the evaluation tried to build a provider or load settings from the environment")

    mp = pytest.MonkeyPatch()
    mp.setattr(socket.socket, "connect", no_network)
    mp.setattr(api, "build_provider", no_env)
    mp.setattr(api, "load_settings", no_env)
    try:
        results = se.run_all()
    finally:
        mp.undo()
    return results, se.compute_security_metrics(results)


# ------------------------------------------------------------------ the evaluation passes, and covers what was asked
def test_every_scenario_passes_with_no_errors(full_run):
    results, m = full_run
    assert m["failed_checks"] == [] and m["errors"] == []
    assert m["scenario_pass_rate"]["numerator"] == m["scenario_pass_rate"]["denominator"] == len(se.SCENARIOS)
    assert [r["id"] for r in results] == [s[0] for s in se.SCENARIOS] and len({r["id"] for r in results}) == len(results)


def test_the_requested_scenario_themes_are_all_present():
    titles = {s[0]: s[2].lower() for s in se.SCENARIOS}
    cats = {s[1] for s in se.SCENARIOS}
    assert cats == {"ownership", "isolation", "approval", "secrets"}
    wanted = {"own data": "OWN-1", "cross-student": "ISO-3", "approval pending": "APR-1", "approved": "APR-2", "rejected": "APR-3", "expired": "APR-4",
              "unauthorized approval": "APR-5", "duplicate": "APR-6", "concurrent": "APR-7", "otp": "SEC-1", "password": "SEC-2"}
    for what, sid in wanted.items():
        assert sid in titles, what
    assert "otp" in titles["SEC-1"] and "password" in titles["SEC-2"] and "concurrent" in titles["APR-7"] and "expired" in titles["APR-4"]


def test_the_metrics_have_real_denominators_and_the_expected_results(full_run):
    _, m = full_run
    assert m["unauthorized_access_violations"] == {"numerator": 0, "denominator": m["unauthorized_access_violations"]["denominator"], "rate": 0.0}
    for key, floor in (("unauthorized_access_violations", 60), ("unsafe_action_executions", 20), ("approval_correctness", 15),
                       ("secret_leakage", 100), ("task_success", 20), ("audit_coverage", 25), ("trace_coverage", 50)):
        assert m[key]["denominator"] >= floor, key
    assert m["unsafe_action_executions"]["numerator"] == 0 and m["secret_leakage"]["numerator"] == 0
    assert m["approval_correctness"]["rate"] == m["task_success"]["rate"] == m["audit_coverage"]["rate"] == m["trace_coverage"]["rate"] == 1.0
    assert m["audit_chain_integrity"]["rate"] == 1.0 and m["secret_check_groups"] >= 8


def test_tokens_are_reported_unavailable_not_invented_and_latency_is_measured(full_run):
    _, m = full_run
    assert m["token_usage"]["turns_with_usage"] == 0 and m["token_usage"]["input_total"] is None and m["token_usage"]["turns"] > 50
    assert m["latency"]["agent_turn_ms"]["n"] == m["token_usage"]["turns"] and m["latency"]["api_call_ms"]["n"] > 100


def test_the_results_are_repeatable_for_the_timing_sensitive_scenarios():
    ids = ("APR-7", "SEC-1", "SEC-5", "SEC-12", "ISO-9")
    first = [[(c["kind"], c["label"], c["ok"]) for c in r["checks"]] for r in se.run_all(scenarios=subset(*ids))]
    second = [[(c["kind"], c["label"], c["ok"]) for c in r["checks"]] for r in se.run_all(scenarios=subset(*ids))]
    assert first == second and all(ok for run in first for _, _, ok in run)


# ------------------------------------------------------------------ the metric arithmetic
def synthetic(kind_ok):
    checks = [{"kind": k, "label": f"{k} {i}", "ok": ok, "detail": ""} for i, (k, ok) in enumerate(kind_ok)]
    return {"id": "X", "category": "approval", "title": "t", "passed": all(c["ok"] for c in checks), "error": None, "wall_ms": 5,
            "checks": checks, "agent_turn_ms": [2, 4], "api_call_ms": [1.0], "tokens": [(None, None), (10, 5)],
            "evidence": {"traces": [], "audit_events": [], "audit_events_total": 0}}


def test_metric_numerators_and_denominators_are_computed_from_the_check_records():
    r = synthetic([("access", True), ("access", False), ("access", True), ("unsafe", True), ("unsafe", False), ("task", True), ("task", False),
                   ("secret", True), ("secret", True), ("secret", False), ("audit", True), ("trace", True), ("trace", False), ("approval", True), ("chain", True)])
    m = se.compute_security_metrics([r])
    assert (m["unauthorized_access_violations"]["numerator"], m["unauthorized_access_violations"]["denominator"]) == (1, 3)
    assert (m["unsafe_action_executions"]["numerator"], m["unsafe_action_executions"]["denominator"]) == (1, 2)
    assert (m["task_success"]["numerator"], m["task_success"]["denominator"]) == (1, 2)
    assert (m["secret_leakage"]["numerator"], m["secret_leakage"]["denominator"]) == (1, 3)
    assert (m["trace_coverage"]["numerator"], m["trace_coverage"]["denominator"]) == (1, 2)
    assert (m["audit_coverage"]["numerator"], m["audit_coverage"]["denominator"]) == (1, 1)
    assert m["scenario_pass_rate"] == {"numerator": 0, "denominator": 1, "rate": 0.0}
    assert m["token_usage"]["turns_with_usage"] == 1 and m["token_usage"]["input_total"] == 10
    assert len(m["failed_checks"]) == 5


def test_an_empty_kind_gives_no_rate_not_a_perfect_score():
    m = se.compute_security_metrics([synthetic([("task", True)])])
    assert m["secret_leakage"]["denominator"] == 0 and m["secret_leakage"]["rate"] is None


def test_a_scenario_that_raises_fails_and_is_reported():
    def boom(lab):
        raise RuntimeError("something broke")
    r = se.run_scenario("X-1", "approval", "broken", boom)
    assert r["passed"] is False and r["error"].startswith("RuntimeError")
    assert "X-1" in se.compute_security_metrics([r])["errors"][0]


# ------------------------------------------------------------------ the evaluation DETECTS broken controls (mutation tests)
def allow_all(*a, **k):
    return Decision(True, "mutation: allow everything", "x")


def test_it_detects_authorization_that_allows_everything(monkeypatch):
    monkeypatch.setattr("campuspilot.api.authorize", allow_all)
    monkeypatch.setattr("campuspilot.tool_registry.authorize", allow_all)
    m = metrics_for("ISO-1", "ISO-3", "ISO-4", "ISO-6")
    assert m["unauthorized_access_violations"]["numerator"] >= 4 and m["scenario_pass_rate"]["numerator"] == 0


def test_it_detects_authorization_that_denies_everything(monkeypatch):
    deny = lambda *a, **k: Decision(False, "mutation: deny everything", "x")
    monkeypatch.setattr("campuspilot.api.authorize", deny)
    monkeypatch.setattr("campuspilot.tool_registry.authorize", deny)
    m = metrics_for("OWN-1", "OWN-3")
    assert m["task_success"]["rate"] < 1.0                           # "secure because it refuses everything" is caught too


def test_it_detects_tampering_that_goes_unnoticed(monkeypatch):
    monkeypatch.setattr("campuspilot.approvals.ApprovalService._intact", lambda self, row, decided=False: True)
    m = metrics_for("APR-8")
    assert m["unsafe_action_executions"]["numerator"] >= 1


def test_it_detects_requests_that_never_expire(monkeypatch):
    real = se.ApprovalService
    monkeypatch.setattr(se, "ApprovalService", lambda *a, **k: real(*a, ttl=timedelta(days=9999), **k))
    m = metrics_for("APR-4")
    assert m["unsafe_action_executions"]["numerator"] >= 1


def test_it_detects_authorization_removed_from_the_approval_path(monkeypatch):
    monkeypatch.setattr("campuspilot.approvals.authorize", allow_all)
    monkeypatch.setattr("campuspilot.approvals.STAFF", "student")       # the role gate now lets students through
    m = metrics_for("APR-5")
    assert m["unsafe_action_executions"]["numerator"] >= 1


def test_it_detects_secrets_that_are_not_masked(monkeypatch):
    unmasked = lambda text, **k: types.SimpleNamespace(text=text, findings=[])
    monkeypatch.setattr("campuspilot.agent.redact_secrets", unmasked)
    m = metrics_for("SEC-1", "SEC-2", "SEC-12")
    assert m["secret_leakage"]["numerator"] >= 3
    # the storage layers mask again on their own (defence in depth), so what leaks is exactly what the model was shown
    assert all("model_input" in f for f in m["failed_checks"] if "not in" in f)


def test_it_detects_a_missing_audit_log(monkeypatch):
    monkeypatch.setattr(AuditStore, "record", lambda self, *a, **k: "AUD-0000000000000000")
    m = metrics_for("OWN-1", "ISO-3", "APR-2")
    assert m["audit_coverage"]["numerator"] < m["audit_coverage"]["denominator"]


def test_it_detects_missing_traces(monkeypatch):
    monkeypatch.setattr(TraceStore, "record", lambda self, trace: trace["trace_id"])
    m = metrics_for("OWN-3")
    assert m["trace_coverage"]["numerator"] < m["trace_coverage"]["denominator"]


# ------------------------------------------------------------------ the report
@pytest.fixture(scope="module")
def report(full_run):
    results, m = full_run
    meta = {"timestamp": "2026-10-30 12:00:00", "provider": "offline-scripted", "model": "n/a (no model called)"}
    return sr.build_report(results, m, sr.load_assignment1_baseline(REAL_REPORTS), None, meta)


def test_the_report_has_every_required_part(report):
    assert report["mode"] == "offline" and report["kind"] == "assignment2_security_evaluation"
    assert {"metrics", "scenarios", "comparison", "limitations", "metric_definitions", "assignment1_baseline"} <= set(report)
    assert any(s["evidence"]["traces"] for s in report["scenarios"]) and any(s["evidence"]["audit_events"] for s in report["scenarios"])
    iso9 = next(s for s in report["scenarios"] if s["id"] == "ISO-9")
    assert iso9["evidence"]["audit_events_total"] == 0        # 401s have no identity to audit (stated in the limitations)
    for key in ("unauthorized_access_violations", "unsafe_action_executions", "approval_correctness", "secret_leakage", "latency", "token_usage",
                "trace_coverage", "audit_coverage", "task_success"):
        assert key in report["metric_definitions"] or key in report["metrics"]
    md = sr.render_markdown(report)
    for needle in ("OFFLINE", "Numerator / denominator".lower(), "Assignment 1 vs Assignment 2", "Limitations", "Trace and audit evidence", "not available"):
        assert needle.lower() in md.lower(), needle


def test_the_comparison_never_invents_assignment_1_security_results(report):
    rows = {r["dimension"]: r for r in report["comparison"]}
    for dim in ("Unauthorized-access violations", "Unsafe-action executions"):
        assert rows[dim]["assignment1"].startswith("not measured") and rows[dim]["comparable"] == "no"
    assert rows["Approval correctness"]["assignment1"].startswith("not applicable")
    assert rows["Live-model evaluation (real LLM)"]["assignment2"].startswith("not run") and rows["Live-model evaluation (real LLM)"]["comparable"] == "no"
    assert all(r["comparable"] in ("yes", "partly", "no") for r in report["comparison"])
    base = report["assignment1_baseline"]
    assert base["offline"]["metrics"]["runs_passed"] == 13 and base["live"]["metrics"]["runs_completed"] == 3 and base["live"]["metrics"]["runs_blocked"] == 10


def test_the_report_contains_no_planted_secret_or_victim_data(report):
    text = sr.render_markdown(report) + json.dumps(report)
    for _, _, needles, _ in se.SECRETS:
        assert not any(n in text for n in needles), needles
    assert "hunter2" not in text and "Vulpine" not in text
    assert se.EMAIL_1 not in text and se.PHONE_1 not in text and se.PASSWORD not in text
    assert "AIza" not in text.replace("AIzaSyEVALFAKEKEY", "")          # no key-like text beyond the fictional marker name


def test_saving_never_overwrites_and_leaves_the_assignment_1_reports_untouched(report, tmp_path, monkeypatch):
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in REAL_REPORTS.glob("evaluation_*")}
    assert before
    md, js = sr.save_report(report, tmp_path / "out")
    assert md.name.startswith("assignment2_evaluation_") and json.loads(js.read_text())["kind"] == "assignment2_security_evaluation"

    class FixedDatetime:
        @staticmethod
        def now():
            return type("D", (), {"strftime": staticmethod(lambda fmt: "20991231_235959")})()

    monkeypatch.setattr(sr, "datetime", FixedDatetime)
    first = sr.save_report(report, tmp_path / "out")
    original = first[0].read_text()
    with pytest.raises(FileExistsError):
        sr.save_report({**report, "meta": {**report["meta"], "timestamp": "changed"}}, tmp_path / "out")
    assert first[0].read_text() == original                                # the existing file is intact
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in REAL_REPORTS.glob("evaluation_*")} == before
    assert before["evaluation_offline_20261022_000402.json"] == sr.load_assignment1_baseline(REAL_REPORTS)["offline"]["sha256"]


def test_the_dashboards_report_list_keeps_the_new_reports_apart_from_assignment_1_ones(settings, report, tmp_path):
    """Batch 5 kept the new reports out of the dashboard list; Batch 6 lists them, but marked as a different kind, so an
    Assignment 1 report is never misclassified as a security report or the other way round."""
    reports = tmp_path / "reports"
    sr.save_report(report, reports)
    (reports / "evaluation_offline_20261022_000402.json").write_text((REAL_REPORTS / "evaluation_offline_20261022_000402.json").read_text())
    c = client_for(dataclasses.replace(settings, reports_dir=reports), "STU001")
    listing = c.get("/api/evaluation/reports").json()["reports"]
    by_name = {r["name"]: r for r in listing}
    a1 = by_name["evaluation_offline_20261022_000402.json"]
    assert "kind" not in a1 and set(a1) == {"name", "mode", "timestamp", "runs", "integrity"}
    a2 = next(r for r in listing if r["name"].startswith("assignment2_evaluation_"))
    assert a2["kind"] == "assignment2_security" and a2["integrity"] == "ok" and a2["runs"] == len(report["scenarios"])
    got = c.get(f"/api/evaluation/reports/{a2['name']}")
    assert got.status_code == 200 and got.json()["kind"] == "assignment2_security_evaluation" and got.json()["integrity"] == "ok"


def test_the_assignment_1_cases_run_through_the_new_stack(retriever):
    rerun = sr.run_assignment1_cases(retriever)
    m = rerun["metrics"]
    assert m["runs"] == 13 and m["runs_completed"] == 13 and m["traces_recorded"] == 13 and rerun["failed_runs"] == []


# ------------------------------------------------------------------ the script
def load_script():
    spec = importlib.util.spec_from_file_location("run_security_evaluation", Path(__file__).resolve().parent.parent / "scripts" / "run_security_evaluation.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_script_writes_a_report_and_never_touches_the_real_database(tmp_path, monkeypatch, capsys):
    mod = load_script()
    real_db = ROOT / "data" / "campuspilot.sqlite3"
    stat = real_db.stat() if real_db.exists() else None
    monkeypatch.setattr(mod, "run_all", lambda: se.run_all(scenarios=subset("OWN-1", "APR-3", "SEC-1")))
    code = mod.main(["--out-dir", str(tmp_path / "r"), "--skip-assignment1-cases"])
    out = capsys.readouterr().out
    assert code == 0 and "OFFLINE" in out and "Saved:" in out
    assert len(list((tmp_path / "r").glob("assignment2_evaluation_*.md"))) == 1 and len(list((tmp_path / "r").glob("assignment2_evaluation_*.json"))) == 1
    assert (real_db.stat().st_mtime_ns, real_db.stat().st_size) == (stat.st_mtime_ns, stat.st_size) if stat else True


def test_the_script_exits_nonzero_when_a_check_fails(tmp_path, monkeypatch, capsys, ):
    mod = load_script()
    monkeypatch.setattr("campuspilot.api.authorize", allow_all)
    monkeypatch.setattr("campuspilot.tool_registry.authorize", allow_all)
    monkeypatch.setattr(mod, "run_all", lambda: se.run_all(scenarios=subset("ISO-3")))
    assert mod.main(["--out-dir", str(tmp_path / "r"), "--skip-assignment1-cases"]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_the_script_has_no_live_mode_and_never_reads_the_environment():
    source = (Path(__file__).resolve().parent.parent / "scripts" / "run_security_evaluation.py").read_text()
    module_source = (ROOT / "campuspilot" / "security_evaluation.py").read_text() + (ROOT / "campuspilot" / "security_report.py").read_text()
    assert "--live" not in source and "build_provider" not in source + module_source and "load_settings" not in source + module_source
    assert "dotenv" not in source + module_source and ".env" not in (source + module_source).replace(".env`", "")
