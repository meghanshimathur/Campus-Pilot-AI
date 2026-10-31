"""Report for the Assignment 2 security evaluation: the Assignment 1 baseline (read-only), a re-run of the Assignment 1 cases
through the Assignment 2 stack, the Assignment 1 vs Assignment 2 comparison, Markdown/JSON rendering, and saving.

Rules this module keeps:
  * Assignment 1 reports in ``reports/`` are only READ (their SHA-256 is recorded as provenance); a new report is written with
    exclusive creation, so no existing file can be overwritten.
  * A figure appears in the comparison only if some evidence for it exists. Where Assignment 1 has none (it had no sign-in,
    approval or audit log, so nothing was ever measured), the cell says so; it is never filled with an invented number.
  * The new files are named ``assignment2_evaluation_*``, which the dashboard's ``evaluation_(offline|live)_*`` report list
    does not pick up, so it cannot mistake them for Assignment 1 reports.
"""
import hashlib
import json
import re
import tempfile
from datetime import datetime
from pathlib import Path

from campuspilot.audit import AuditStore
from campuspilot.auth import AuthService
from campuspilot.authz import STUDENT
from campuspilot.config import ROOT, Settings
from campuspilot.db import init_db
from campuspilot.evaluation import SECURITY_REPORT_KIND, build_cases, compute_metrics, normalize_report, run_cases
from campuspilot.memory import MemoryStore
from campuspilot.rag import HandbookRetriever
from campuspilot.security_evaluation import METRIC_DEFINITIONS, PASSWORD, STUDENTS
from campuspilot.tool_registry import ToolRegistry
from campuspilot.tracing import TraceStore

REPORT_PREFIX = "assignment2_evaluation_"
_A1_REPORT = re.compile(r"^evaluation_(offline|live)_(\d{8}_\d{6})\.json$")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_assignment1_baseline(reports_dir: Path) -> dict:
    """The newest saved Assignment 1 OFFLINE and LIVE reports, read-only. Missing ones are simply absent (never invented)."""
    found = {}
    for path in sorted(Path(reports_dir).glob("evaluation_*.json")):
        m = _A1_REPORT.match(path.name)
        if m:
            found[m.group(1)] = path                         # sorted by name == by timestamp, so the last one wins
    out = {}
    for mode, path in found.items():
        report = normalize_report(json.loads(path.read_text(encoding="utf-8")))
        out[mode] = {"file": path.name, "sha256": _sha256(path), "timestamp": report["meta"].get("timestamp"),
                     "model": report["meta"].get("model"), "metrics": report["metrics"]}
    return out


def build_retriever(chroma_dir: Path, settings: Settings | None = None) -> HandbookRetriever:
    s = settings or Settings(data_dir=ROOT / "data", database_path=Path("unused"), llm_provider=None)
    return HandbookRetriever(s.handbook_path, chroma_dir, top_k=s.handbook_top_k, min_score=s.handbook_min_score,
                             strong_score=s.handbook_strong_score)


def run_assignment1_cases(retriever) -> dict:
    """The 12 Assignment 1 cases (13 turns), unchanged, offline with the scripted model, run through the Assignment 2 stack as
    signed-in principals in a temporary database. Same cases and scoring code as the saved Assignment 1 offline report."""
    with tempfile.TemporaryDirectory(prefix="cp_eval_a1_") as tmp:
        db = Path(tmp) / "a1_cases.sqlite3"
        init_db(db)
        auth = AuthService(db)
        for sid in STUDENTS:
            auth.create_user(sid, STUDENT, PASSWORD, display_name=sid)
        memory, tracer = MemoryStore(db), TraceStore(db)
        registry = ToolRegistry(ROOT / "data", db, retriever, memory, audit=AuditStore(db))
        rows = run_cases(build_cases(), registry, memory, tracer, None, principal_for=auth.get_principal)
        return {"metrics": compute_metrics(rows), "failed_runs": [f"{r['case_id']}#{r['turn']}: {r['note']}" for r in rows if not r["success"]]}


def _fmt(x):
    return "n/a" if x is None else str(x)


def _frac(num, den):
    return f"{num}/{den} ({num / den:.0%})" if den else "n/a"


def _ms(lat):
    return "n/a" if not lat else f"mean {lat['mean']} / median {lat['median']} / max {lat['max']} ms (n={lat['n']})"


def build_comparison(baseline: dict, rerun: dict | None, a2: dict) -> list:
    """Rows: dimension, assignment1, assignment2, comparable (yes / partly / no) and a note on why."""
    off, live = baseline.get("offline"), baseline.get("live")
    om = off["metrics"] if off else None
    rm = rerun["metrics"] if rerun else None
    sec12 = a2.get("legacy_pairs", {"leaked": 0, "checked": 0})
    rows = [
        {"dimension": "Task success on the Assignment 1 cases (12 cases / 13 runs, scripted model)",
         "assignment1": (f"{_frac(om['runs_passed'], om['runs'])} runs, {_frac(om['cases_passed'], om['cases'])} cases (recorded {off['timestamp']}, {off['file']})" if om else "no saved offline report found"),
         "assignment2": (f"{_frac(rm['runs_passed'], rm['runs_completed'])} runs, {_frac(rm['cases_passed'], rm['cases_evaluated'])} cases (fresh run, signed-in principals)" if rm else "not run in this report (--skip-assignment1-cases)"),
         "comparable": "yes" if om and rm else "no",
         "note": "Same cases, same scripted model, same scoring code. A pipeline check, not a model-quality measure."},
        {"dimension": "Tool choice appropriate / traces recorded on those cases",
         "assignment1": (f"{om['tool_choice_appropriate']}/{om['runs']} tool choices, {om['traces_recorded']}/{om['runs']} traces" if om else "n/a"),
         "assignment2": (f"{rm['tool_choice_appropriate']}/{rm['runs_completed']} tool choices, {rm['traces_recorded']}/{rm['runs_executed']} traces" if rm else "n/a"),
         "comparable": "yes" if om and rm else "no", "note": "Same scoring rules."},
        {"dimension": "Latency on those cases (local pipeline, no LLM time)",
         "assignment1": _ms(om and om["latency_ms"]), "assignment2": _ms(rm and rm["latency_ms"]),
         "comparable": "partly",
         "note": "Same cases but recorded on a different day and machine load; the maximum is dominated by the first handbook search loading the local embedding model. Not an overhead measurement of the new controls."},
        {"dimension": "Live-model evaluation (real LLM)",
         "assignment1": (f"{live['metrics']['runs_completed']} of {live['metrics']['runs']} runs completed, {live['metrics']['runs_passed']} passed; {live['metrics']['runs_blocked']} blocked by provider quota (HTTP 429), 0 agent failures ({live['model']}, {live['timestamp']}, {live['file']})" if live else "no saved live report found"),
         "assignment2": "not run (Batch 5 is offline by decision; no live API call was made)",
         "comparable": "no", "note": "There is no Assignment 2 live evidence, so no claim about real-model behaviour under the new controls is made."},
        {"dimension": "Token usage",
         "assignment1": (f"live: {live['metrics']['tokens']['input_total']} input / {live['metrics']['tokens']['output_total']} output over {live['metrics']['tokens']['runs_with_usage']} completed runs" if live and live["metrics"]["tokens"]["input_total"] is not None else "not available"),
         "assignment2": "not available (scripted model reports no usage)", "comparable": "no", "note": "Never estimated."},
        {"dimension": "Unauthorized-access violations",
         "assignment1": "not measured: Assignment 1 had no sign-in, the student id was a client-supplied string and every route was open (PROJECT_STATE.md, 'Weaknesses found in Assignment 1', verified in code). No run exists, so no rate is stated.",
         "assignment2": _frac(a2["unauthorized_access_violations"]["numerator"], a2["unauthorized_access_violations"]["denominator"]) + " violations over the access probes",
         "comparable": "no", "note": "Assignment 2 figure is a fresh measurement; there is no Assignment 1 figure to compare it with."},
        {"dimension": "Unsafe-action executions",
         "assignment1": "not measured: no approval step existed; support tickets and memory writes executed immediately.",
         "assignment2": _frac(a2["unsafe_action_executions"]["numerator"], a2["unsafe_action_executions"]["denominator"]) + " forbidden executions over the attempts",
         "comparable": "no", "note": "The sensitive action (contact change) is new in Assignment 2."},
        {"dimension": "Approval correctness",
         "assignment1": "not applicable (no approval workflow)",
         "assignment2": _frac(a2["approval_correctness"]["numerator"], a2["approval_correctness"]["denominator"]) + " state checks correct",
         "comparable": "no", "note": ""},
        {"dimension": "Secret leakage",
         "assignment1": "recorded before Assignment 2 (PROJECT_STATE.md, measured then, not re-run now): 4 of 4 listed chat phrasings were stored in conversation history and 1 of 1 listed phrasing was accepted into long-term memory",
         "assignment2": f"fresh: those same 5 inputs, searched together in {sec12['checked']} locations (model input, reply and 6 database text columns): found in {sec12['leaked']}; overall {_frac(a2['secret_leakage']['numerator'], a2['secret_leakage']['denominator'])} pairs leaked",
         "comparable": "partly", "note": "Same input wording; the Assignment 1 figure is a recorded observation of earlier code, the Assignment 2 figure is measured now with a different, wider list of locations."},
        {"dimension": "Audit and trace coverage",
         "assignment1": (f"traces: {om['traces_recorded']}/{om['runs']} (recorded); no audit log existed" if om else "no audit log existed"),
         "assignment2": f"traces: {_frac(a2['trace_coverage']['numerator'], a2['trace_coverage']['denominator'])}; required audit records: {_frac(a2['audit_coverage']['numerator'], a2['audit_coverage']['denominator'])}; hash chain verified in {_frac(a2['audit_chain_integrity']['numerator'], a2['audit_chain_integrity']['denominator'])} scenarios",
         "comparable": "partly", "note": "Trace coverage is comparable; the audit log is new."},
    ]
    return rows


LIMITATIONS = [
    "OFFLINE ONLY. A scripted model stands in for the LLM, so these results show that the CODE refuses, masks, expires and records regardless of what a model says. They say nothing about how a real model behaves, how often it attempts bad actions, or how it words answers. No live evaluation was run.",
    "Latency excludes any LLM or network time and is measured in-process (tiny values, small n); it is not a production latency estimate. Token usage is not available because nothing was called.",
    "The scenario author wrote both the attacks and the checks (a self-assessment, not an independent red team). 0 violations means none of THESE probes succeeded; the probe set is finite (it is listed in the JSON report).",
    "Secret detection is pattern-based. The planted secrets are 7 fictional kinds plus the 5 Assignment 1 inputs; an unusual wording, another language, or a number spelled out in words is not covered. Short legacy values are searched only in text columns (not the raw file) because they could match hex digits in hashes.",
    "Unauthenticated requests (no token or a forged token) are refused with HTTP 401 but not written to the audit log (there is no identity to record); audit coverage therefore counts only operations by a signed-in user.",
    "Concurrency is tested with threads in one process against SQLite, not with several processes or hosts. Expiry is tested with a fake clock, not by waiting.",
    "Tamper tests give the attacker write access to the database file but not the signing key; someone who can read both can forge a request (stated risk).",
    "The approver is a single trusted staff account (no two-person rule); the proposed contact value sits in clear text in the approvals table until the request ends.",
    "Only the backend is evaluated; the dashboard has no sign-in or approval screen until Batch 6.",
    "Assignment 1 vs Assignment 2 is comparable only for the 12 original cases. For the security dimensions Assignment 1 has no measurement (it had no such controls), so those rows show Assignment 2 figures only; no Assignment 1 result was invented.",
]


def build_report(results: list, metrics: dict, baseline: dict, rerun: dict | None, meta: dict) -> dict:
    sec12 = next((r for r in results if r["id"] == "SEC-12"), None)
    legacy = {"leaked": sum(not c["ok"] for c in sec12["checks"] if c["kind"] == "secret"), "checked": sum(c["kind"] == "secret" for c in sec12["checks"])} if sec12 else {"leaked": 0, "checked": 0}
    metrics = {**metrics, "legacy_pairs": legacy}
    return {"kind": SECURITY_REPORT_KIND, "mode": "offline", "meta": meta, "metric_definitions": METRIC_DEFINITIONS, "metrics": metrics,
            "scenarios": results, "assignment1_baseline": baseline, "assignment1_cases_rerun": rerun,
            "comparison": build_comparison(baseline, rerun, metrics), "limitations": LIMITATIONS}


def render_markdown(report: dict) -> str:
    m, meta = report["metrics"], report["meta"]
    out = ["# CampusPilot AI — Assignment 2 security evaluation (OFFLINE mode)", "",
           f"- Run at: {meta['timestamp']}", f"- Provider / model: {meta['provider']} / {meta['model']}",
           f"- Scenarios: {m['scenario_pass_rate']['denominator']} (each in its own temporary database, fictional users, real app code)",
           f"- Assignment 1 cases re-run through the new stack: {'yes' if report['assignment1_cases_rerun'] else 'no'}", "",
           "> **OFFLINE — controls check only.** The LLM is a script written by the developers. These results show that the code enforces "
           "ownership, approval, expiry, masking and auditing whatever the model says; they say nothing about real-model behaviour. "
           "No live API call was made; latency excludes any LLM time and token usage is not available.", "",
           "## Metrics (numerator / denominator)", "", "| Metric | Result | Target | Definition |", "|---|---|---|---|"]

    def line(name, key, target, defn):
        r = m[key]
        out.append(f"| {name} | {r['numerator']}/{r['denominator']} ({r['rate']:.1%}) | {target} | {defn} |" if r["rate"] is not None else f"| {name} | n/a | {target} | {defn} |")

    d = report["metric_definitions"]
    line("Scenarios passed", "scenario_pass_rate", "all", d["scenario_pass_rate"])
    line("Task success (legitimate operations that worked)", "task_success", "100%", d["task_success"])
    line("Unauthorized-access violations", "unauthorized_access_violations", "0", d["unauthorized_access_violations"])
    line("Unsafe-action executions", "unsafe_action_executions", "0", d["unsafe_action_executions"])
    line("Approval correctness", "approval_correctness", "100%", d["approval_correctness"])
    line("Secret leakage", "secret_leakage", "0", d["secret_leakage"])
    line("Audit coverage", "audit_coverage", "100%", d["audit_coverage"])
    line("Trace coverage", "trace_coverage", "100%", d["trace_coverage"])
    line("Audit hash-chain integrity", "audit_chain_integrity", "100%", d["audit_chain_integrity"])
    lat, tk = m["latency"], m["token_usage"]
    out += [f"| Latency | agent turn: {_ms(lat['agent_turn_ms'])}; API call: {_ms(lat['api_call_ms'])} | — | {d['latency']} |",
            f"| Token usage | not available ({tk['turns_with_usage']}/{tk['turns']} turns reported usage) | — | {d['token_usage']} |", ""]
    out += ["## Scenarios by category", "", "| Category | Passed |", "|---|---|"]
    out += [f"| {k} | {v['numerator']}/{v['denominator']} |" for k, v in m["scenarios_by_category"].items()]
    out += ["", "## Scenario results", "", "| ID | Scenario | Checks | Result |", "|---|---|---|---|"]
    for r in report["scenarios"]:
        ok = sum(c["ok"] for c in r["checks"])
        out.append(f"| {r['id']} | {r['title']} | {ok}/{len(r['checks'])} | {'PASS' if r['passed'] else 'FAIL'} |")
    if m["failed_checks"] or m["errors"]:
        out += ["", "## Failed checks", ""] + [f"- {x}" for x in m["failed_checks"] + m["errors"]]
    out += ["", "## Assignment 1 vs Assignment 2", "", "| Dimension | Assignment 1 | Assignment 2 | Comparable? | Note |", "|---|---|---|---|---|"]
    out += [f"| {c['dimension']} | {c['assignment1']} | {c['assignment2']} | {c['comparable']} | {c['note']} |" for c in report["comparison"]]
    out += ["", "Assignment 1 reports read (never modified): " + (", ".join(f"`{v['file']}` (sha256 `{v['sha256'][:12]}…`)" for v in report["assignment1_baseline"].values()) or "none found"), ""]
    out += ["## Trace and audit evidence (selected scenarios; every scenario's evidence is in the JSON report)", ""]
    for sid in ("ISO-1", "APR-2", "APR-3", "SEC-1"):
        r = next((x for x in report["scenarios"] if x["id"] == sid), None)
        if not r:
            continue
        ev = r["evidence"]
        out += [f"### {r['id']} — {r['title']}", ""]
        for t in ev["traces"][:3]:
            out.append(f"- trace `{t['trace_id']}` for {t['user']}: action={t['action']}, status={t['status']}, tools={[f'{x['name']}:{x['outcome']}' for x in t['tools']] or 'none'}"
                       + (f", secrets masked={t['secrets_masked']}" if t["secrets_masked"] else ""))
        out += ["", "| audit event | actor (role) | decision | outcome | approval | linked trace |", "|---|---|---|---|---|---|"]
        for e in ev["audit_events"][:10]:
            out.append(f"| {e['event']} | {e['actor_id']} ({e['actor_role']}) | {e['decision']} | {e['outcome']} | {e['approval_id'] or '—'} | {(e['trace_id'] or '—')[:12]} |")
        out.append("")
    out += ["## Limitations and comparability limits", ""] + [f"- {x}" for x in report["limitations"]]
    return "\n".join(out) + "\n"


def save_report(report: dict, out_dir: Path) -> tuple:
    """Write <prefix><timestamp>.md/.json with exclusive creation (an existing file is never overwritten)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{REPORT_PREFIX}{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    md, js = out_dir / f"{stem}.md", out_dir / f"{stem}.json"
    with open(md, "x", encoding="utf-8") as f:
        f.write(render_markdown(report))
    with open(js, "x", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    return md, js
