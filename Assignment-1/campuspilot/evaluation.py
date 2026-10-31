"""Evaluation harness for Assignment 1: fictional test cases, deterministic scoring, and
metrics computed only from what was actually measured.

Two modes (the report always says which one produced the numbers):
  live     real LLM provider; success rate, latency and tokens describe the real agent.
  offline  scripted LLM; exercises tools, retrieval, memory and tracing end to end but says
           NOTHING about model quality. Token counts are 'not available' (never invented).

Scoring is rule-based (tool choice + checks on tool results/evidence/response text), not an
LLM judge. Text checks are keyword heuristics and can misjudge unusual wording.
"""
# Which runs count where (see compute_metrics):
#   outcome "completed"           -> counted in every success/tool-choice/latency/token figure (PASS or genuine FAIL)
#   outcome "blocked_by_provider" -> listed separately (quota 429, outage 408/5xx, key/model problem 401/403/404);
#                                    never counted as an agent failure
#   outcome "skipped"             -> not run (circuit breaker); listed separately
# HTTP 400, unexpected exceptions and max_steps_exceeded are NOT excused: they may be this project's own fault.
import re
import statistics
from dataclasses import dataclass, field
from typing import Callable

from campuspilot.agent import Agent
from campuspilot.provider import PROVIDER_BLOCKED_KINDS, ChatResponse, LLMProvider, ToolCall, kind_for_status

NOT_FOUND_RE = re.compile(
    r"not (found|available|mentioned|covered|specified|included)|no (relevant )?information|don'?t have|do not have|"
    r"couldn'?t find|could not find|can'?t find|cannot find|isn'?t (in|covered)|unable to find|"
    r"(doesn'?t|does not|do not|don'?t|did not|didn'?t) (contain|mention|cover|include|specify|address)|"
    r"no (mention|details?|record) |not (in|part of) the (student )?handbook",
    re.IGNORECASE)
ERROR_ACK_RE = re.compile(r"not found|verify|couldn'?t|could not|unable|can'?t|cannot|no record|invalid", re.IGNORECASE)


def _digits(text: str) -> str:
    return re.sub(r"[^0-9]", "", text)


# ---------------------------------------------------------------- cases
@dataclass
class Turn:
    message: str
    expected_tools: frozenset = frozenset()
    allowed_extra: frozenset = frozenset()   # tools that are acceptable but not required
    check: Callable | None = None            # (result, ctx) -> (bool, note)
    session: str = "s1"
    script: list = field(default_factory=list)  # offline only: scripted ChatResponses


@dataclass
class Case:
    case_id: str
    category: str
    student_id: str
    turns: list


def _tool(result, name):
    return next((t for t in result.tools_called if t["name"] == name), None)


def _call(name, args, cid="c1"):
    return ChatResponse(tool_calls=[ToolCall(cid, name, args)])


def _say(text):
    return ChatResponse(content=text)


def check_fee(result, ctx):
    t = _tool(result, "check_fee_status")
    ok = bool(t) and t["result"].get("status") == "overdue" and "300000" in _digits(result.response)
    return ok, "reports the 300000 due and overdue status from the tool" if ok else "due amount 300000 missing from answer"


def check_scholarship(result, ctx):
    ok = "review" in result.response.lower()
    return ok, "mentions 'under review'" if ok else "does not say the scholarship is under review"


def check_policy_portal(result, ctx):
    ok = any(e["section"].startswith("2.") for e in result.evidence) and "24" in result.response
    return ok, "evidence from section 2 and the 24-hour rule stated" if ok else "missing section-2 evidence or the 24-hour rule"


def check_policy_attendance(result, ctx):
    ok = any(e["section"].startswith("6.") for e in result.evidence) and "75" in result.response
    return ok, "evidence from section 6 and 75% stated" if ok else "missing section-6 evidence or the 75% rule"


def check_not_found(result, ctx):
    if result.evidence:
        return False, "retrieval returned handbook evidence for an out-of-scope question"
    if not NOT_FOUND_RE.search(result.response):
        return False, "no evidence retrieved, but the answer does not clearly say the information was not found"
    return True, "no evidence retrieved; answer says information was not found"


def check_tool_error(result, ctx):
    t = _tool(result, "check_fee_status")
    digits_leaked = len(_digits(result.response)) >= 4
    ok = bool(t) and "error" in t["result"] and bool(ERROR_ACK_RE.search(result.response)) and not digits_leaked
    return ok, "tool error surfaced; no amounts invented" if ok else "tool error not handled cleanly"


def check_memory_saved(result, ctx):
    ok = any(m["key"] == "preferred_name" and m["value"].lower() == "dev" for m in result.memories_saved)
    return ok, "saved preferred_name=Dev" if ok else "preference was not saved"


def check_memory_recalled(result, ctx):
    ok = any(m["key"] == "preferred_name" for m in result.memories_used) and "dev" in result.response.lower()
    return ok, "recalled preferred_name in a new session and used it" if ok else "stored name not recalled/used"


def check_sensitive_not_stored(result, ctx):
    stored = ctx["memory"].recall(ctx["student_id"])
    ok = not result.memories_saved and not stored
    return ok, "nothing stored" if ok else "sensitive text was stored in memory"


def check_direct(result, ctx):
    ok = bool(result.response.strip()) and result.response != "" and not result.tools_called
    return ok, "answered directly without tools" if ok else "used a tool or gave an empty answer"


def check_ticket(result, ctx):
    t = _tool(result, "create_support_request")
    ok = bool(t) and str(t["result"].get("ticket_id", "")).startswith("TKT-")
    return ok, "ticket created" if ok else "no ticket created"


def check_office_hours(result, ctx):
    ok = "14:00" in result.response or "monday" in result.response.lower() or "mon" in result.response.lower()
    return ok, "gives Monday office hours" if ok else "office hours not stated"


def build_cases() -> list:
    F = frozenset
    return [
        Case("fee_status", "fee status", "STU003", [Turn(
            "What is my current fee status?", F({"check_fee_status"}), check=check_fee,
            script=[_call("check_fee_status", {}), _say("Your fees are overdue: Rs. 300000 is due.")])]),
        Case("scholarship_status", "fee status", "STU002", [Turn(
            "Where is my scholarship application?", F({"check_scholarship_status"}), check=check_scholarship,
            script=[_call("check_scholarship_status", {}), _say("Your Women in Tech Scholarship is under review.")])]),
        Case("policy_portal_failure", "handbook policy", "STU002", [Turn(
            "The payment portal failed. Can I pay on the next working day?", F({"search_handbook"}),
            F({"check_fee_status", "create_support_request"}), check=check_policy_portal,
            script=[_call("search_handbook", {"query": "payment portal failed pay next working day"}),
                    _say("Yes, if you report the failure within 24 hours and before the due date, no late fee "
                         "applies [student_handbook.md#s2-1].")])]),
        Case("policy_attendance", "handbook policy", "STU001", [Turn(
            "What attendance do I need to be allowed to sit the end-term exam?", F({"search_handbook"}),
            check=check_policy_attendance,
            script=[_call("search_handbook", {"query": "minimum attendance end-term examination"}),
                    _say("You need at least 75% attendance in each course [student_handbook.md#s6-1].")])]),
        Case("missing_parking", "missing evidence", "STU001", [Turn(
            "Where can I park my car on campus?", F({"search_handbook"}), check=check_not_found,
            script=[_call("search_handbook", {"query": "student car parking on campus"}),
                    _say("I could not find anything about parking in the handbook. Please contact the Security "
                         "Office or raise a support request.")])]),
        Case("missing_visa", "missing evidence", "STU004", [Turn(
            "How do I apply for a student visa extension?", F({"search_handbook"}), check=check_not_found,
            script=[_call("search_handbook", {"query": "student visa extension"}),
                    _say("The handbook does not contain information about visa extensions.")])]),
        Case("unknown_student_error", "tool error", "NOPE", [Turn(
            "What is my fee status?", F({"check_fee_status"}), check=check_tool_error,
            script=[_call("check_fee_status", {}),
                    _say("I couldn't find a student with that ID. Please verify it and try again.")])]),
        Case("memory_recall", "memory recall", "STU004", [
            Turn("Please call me Dev from now on.", F({"remember_preference"}), check=check_memory_saved, session="A",
                 script=[_call("remember_preference", {"key": "preferred_name", "value": "Dev"}),
                         _say("Sure, I'll call you Dev.")]),
            Turn("Hi, what name do you have for me?", check=check_memory_recalled, session="B",
                 script=[_say("Hello Dev! How can I help?")])]),
        Case("sensitive_not_stored", "memory safety", "STU001", [Turn(
            "Please remember my bank account number 123456789012 for refunds.", F(), F({"remember_preference"}),
            check=check_sensitive_not_stored,
            script=[_call("remember_preference", {"key": "study_preference", "value": "bank account 123456789012"}),
                    _say("I can't store bank or payment details.")])]),
        Case("direct_answer", "direct answer", "STU001", [Turn(
            "Hello! What can you help me with?", F(), check=check_direct,
            script=[_say("I can help with fees, scholarships, courses, the handbook and support requests.")])]),
        Case("support_request", "support request", "STU002", [Turn(
            "Please raise a support request: my fee payment failed twice.", F({"create_support_request"}),
            check=check_ticket,
            script=[_call("create_support_request", {"category": "fees", "description": "Fee payment failed twice"}),
                    _say("I have raised your support request.")])]),
        Case("office_hours", "course info", "STU001", [Turn(
            "When are Dr. Kavita Rao's office hours?", F({"get_professor_office_hours"}), check=check_office_hours,
            script=[_call("get_professor_office_hours", {"professor_name": "Kavita Rao"}),
                    _say("Dr. Kavita Rao holds office hours on Monday 14:00-16:00 in F-204.")])]),
    ]


# ---------------------------------------------------------------- running
class ScriptedProvider(LLMProvider):
    """Offline stand-in for the LLM. Reports NO token usage (None), so none is ever invented."""
    name = "offline-scripted"

    def __init__(self, script):
        self.script = list(script)

    def chat(self, messages, tool_schemas):
        return self.script.pop(0)


def _blank_row(case, i, turn, **over):
    row = {"case_id": case.case_id, "category": case.category, "turn": i, "message": turn.message,
           "expected_tools": sorted(turn.expected_tools), "called_tools": [], "tool_outcomes": [],
           "tool_choice_appropriate": False, "check_passed": False, "note": "", "run_error": None, "success": False,
           "response": "", "latency_ms": 0, "input_tokens": None, "output_tokens": None, "evidence_ids": [],
           "cited_handbook": False, "trace_recorded": False, "provider": None, "model": None,
           "outcome": "completed", "error_kind": None}
    row.update(over)
    return row


def run_cases(cases, registry, memory, tracer, provider=None, stop_after_provider_errors=0) -> list[dict]:
    """provider=None -> offline mode (scripted per turn, never touches the network); otherwise the given live provider.

    Every row has an `outcome`: "completed" (the run reached the model and finished: it passed or genuinely failed),
    "blocked_by_provider" (the call failed with a quota/availability/config error, so the agent was never evaluated)
    or "skipped" (not run). With stop_after_provider_errors=N, after N consecutive provider-blocked turns the remaining
    turns are skipped instead of hammering a provider that is refusing us.
    """
    rows, consecutive_blocked, stopped = [], 0, False
    provider_name = getattr(provider, "name", None)
    for case in cases:
        for i, turn in enumerate(case.turns, 1):
            if stopped:
                rows.append(_blank_row(case, i, turn, outcome="skipped", provider=provider_name,
                                       run_error="not run: provider unavailable",
                                       note=f"not run: stopped after {stop_after_provider_errors} consecutive provider errors"))
                continue
            llm = provider or ScriptedProvider(turn.script)
            agent = Agent(llm, registry, memory=memory, tracer=tracer)
            result = agent.run(turn.message, case.student_id, f"{case.case_id}-{turn.session}")
            blocked = result.error is not None and result.error_kind in PROVIDER_BLOCKED_KINDS
            called = [t["name"] for t in result.tools_called]
            called_set = set(called)
            tool_ok = turn.expected_tools <= called_set and called_set <= (turn.expected_tools | turn.allowed_extra)
            ctx = {"memory": memory, "student_id": case.student_id}
            try:
                check_ok, note = turn.check(result, ctx) if turn.check else (True, "no content check")
            except Exception as exc:  # a broken check must fail loudly, not crash the whole run
                check_ok, note = False, f"check raised {type(exc).__name__}"
            if blocked:
                note = f"blocked by provider ({result.error_kind}): the model was not evaluated"
            trace = tracer.get(result.trace_id) if result.trace_id else None
            rows.append(_blank_row(
                case, i, turn,
                called_tools=called, tool_outcomes=[t["result"].get("error") and "error" or "ok" for t in result.tools_called],
                tool_choice_appropriate=tool_ok, check_passed=check_ok, note=note,
                run_error=result.error, success=bool(tool_ok and check_ok and result.error is None),
                response=result.response, latency_ms=result.latency_ms,
                input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                evidence_ids=[e["chunk_id"] for e in result.evidence],
                cited_handbook="student_handbook.md#s" in result.response,
                trace_recorded=bool(trace) and trace["user_query"] is not None,
                provider=result.provider, model=result.model,
                outcome="blocked_by_provider" if blocked else "completed",
                error_kind=result.error_kind if result.error else None))
            consecutive_blocked = consecutive_blocked + 1 if blocked else 0
            if stop_after_provider_errors and consecutive_blocked >= stop_after_provider_errors:
                stopped = True
    return rows


# ---------------------------------------------------------------- metrics
def _outcome(r) -> str:
    return r.get("outcome", "completed")      # reports and rows written before outcomes existed were all "completed"


def compute_metrics(rows: list[dict]) -> dict:
    """Agent-behaviour metrics use ONLY completed runs. Runs blocked by the provider (quota/outage/config) or never
    run are reported separately and are never counted as agent failures."""
    n = len(rows)
    done = [r for r in rows if _outcome(r) == "completed"]
    blocked = [r for r in rows if _outcome(r) == "blocked_by_provider"]
    skipped = [r for r in rows if _outcome(r) == "skipped"]
    executed = n - len(skipped)
    case_ids = list(dict.fromkeys(r["case_id"] for r in rows))
    evaluable = [c for c in case_ids if all(_outcome(r) == "completed" for r in rows if r["case_id"] == c)]
    case_ok = {c: all(r["success"] for r in rows if r["case_id"] == c) for c in evaluable}
    lat = [r["latency_ms"] for r in done]
    tok_rows = [r for r in done if r["input_tokens"] is not None or r["output_tokens"] is not None]
    cats = {}
    for r in rows:
        c = cats.setdefault(r["category"], [0, 0, 0])      # passed, completed, blocked or not run
        if _outcome(r) == "completed":
            c[1] += 1
            c[0] += r["success"]
        else:
            c[2] += 1
    evid_turns = [r for r in done if r["evidence_ids"]]
    kinds = {}
    for r in blocked:
        kinds[r.get("error_kind") or "unknown"] = kinds.get(r.get("error_kind") or "unknown", 0) + 1
    nd = len(done)
    return {
        "runs": n, "cases": len(case_ids),
        "runs_completed": nd, "runs_blocked": len(blocked), "runs_skipped": len(skipped),
        "provider_error_kinds": kinds,
        "cases_evaluated": len(evaluable), "cases_blocked": len(case_ids) - len(evaluable),
        "cases_passed": sum(case_ok.values()),
        "case_success_rate": round(sum(case_ok.values()) / len(evaluable), 3) if evaluable else None,
        "runs_passed": sum(r["success"] for r in done),
        "run_success_rate": round(sum(r["success"] for r in done) / nd, 3) if nd else None,
        "tool_choice_appropriate": sum(r["tool_choice_appropriate"] for r in done),
        "tool_choice_rate": round(sum(r["tool_choice_appropriate"] for r in done) / nd, 3) if nd else None,
        "by_category": {k: {"passed": v[0], "runs": v[1], "blocked": v[2]} for k, v in cats.items()},
        "failed_runs": [f"{r['case_id']}#{r['turn']}: {r['note']}" + (f" [{r['run_error']}]" if r["run_error"] else "")
                        for r in done if not r["success"]],
        "blocked_runs": [f"{r['case_id']}#{r['turn']}: {r['note']}" + (f" [{r['run_error']}]" if r["run_error"] else "")
                         for r in blocked + skipped],
        "latency_ms": ({"n": nd, "min": min(lat), "mean": round(statistics.mean(lat), 1),
                        "median": statistics.median(lat), "max": max(lat)} if lat else None),
        "tokens": {
            "runs_with_usage": len(tok_rows), "runs_total": nd,
            "input_total": sum(r["input_tokens"] or 0 for r in tok_rows) if tok_rows else None,
            "output_total": sum(r["output_tokens"] or 0 for r in tok_rows) if tok_rows else None,
            "input_mean": round(statistics.mean(r["input_tokens"] for r in tok_rows if r["input_tokens"] is not None), 1)
            if any(r["input_tokens"] is not None for r in tok_rows) else None,
            "output_mean": round(statistics.mean(r["output_tokens"] for r in tok_rows if r["output_tokens"] is not None), 1)
            if any(r["output_tokens"] is not None for r in tok_rows) else None,
        },
        "handbook_runs_with_evidence": len(evid_turns),
        "handbook_cited": sum(r["cited_handbook"] for r in evid_turns),
        "handbook_citation_rate": (round(sum(r["cited_handbook"] for r in evid_turns) / len(evid_turns), 3)
                                   if evid_turns else None),
        "traces_recorded": sum(r["trace_recorded"] for r in rows if _outcome(r) != "skipped"), "runs_executed": executed,
        "tool_errors_observed": sum(o == "error" for r in done for o in r["tool_outcomes"]),
        "runs_with_agent_error": sum(bool(r["run_error"]) for r in done),
    }


# ---------------------------------------------------------------- report
SCRIPTED_PROVIDER = ScriptedProvider.name  # "offline-scripted": the stand-in used by --offline runs


def report_integrity(report) -> str:
    """'ok' only if a saved report's label agrees with who actually produced its runs, else 'inconsistent'.

    An OFFLINE report must consist entirely of scripted runs; a LIVE report must contain none (a live run whose
    every call failed still names its real provider, so it stays 'ok' and honestly shows its failures). This stops
    scripted results being displayed as real-model evidence if a file is copied, edited or mislabelled.
    """
    try:
        mode = report["mode"]
        providers = {r.get("provider") for r in report.get("runs", [])} | {report["meta"].get("provider")}
    except (KeyError, AttributeError, TypeError):
        return "inconsistent"
    providers.discard(None)
    if not providers:
        return "inconsistent"
    if mode == "offline":
        return "ok" if providers == {SCRIPTED_PROVIDER} else "inconsistent"
    if mode == "live":
        return "ok" if SCRIPTED_PROVIDER not in providers else "inconsistent"
    return "inconsistent"


_LEGACY_PROVIDER_ERROR = re.compile(r"^llm_error: (?:ClientError|ServerError) (\d{3})\b")


def normalize_report(report):
    """Reports saved before runs had an `outcome` counted provider errors (e.g. HTTP 429 quota) as agent failures.
    For such a report, derive each run's outcome from its recorded error text and recompute the metrics, returning
    a NEW dict flagged `normalized` (the saved file is never modified). Reports that already have outcomes, or
    contain no provider-side errors, are returned unchanged."""
    runs = report.get("runs") if isinstance(report, dict) else None
    if not isinstance(runs, list) or not runs or not all(isinstance(r, dict) for r in runs) or any("outcome" in r for r in runs):
        return report
    new_runs, changed = [], False
    for r in runs:
        r = dict(r)
        match = _LEGACY_PROVIDER_ERROR.match(r.get("run_error") or "")
        kind = kind_for_status(int(match.group(1))) if match else None
        if kind in PROVIDER_BLOCKED_KINDS:
            r.update(outcome="blocked_by_provider", error_kind=kind,
                     note=f"blocked by provider ({kind}): the model was not evaluated")
            changed = True
        else:
            r["outcome"] = "completed"
        new_runs.append(r)
    if not changed:
        return report
    out = dict(report)
    out["runs"] = new_runs
    out["metrics"] = compute_metrics(new_runs)
    out["normalized"] = ("Derived when read: this report was saved before provider errors were separated from agent "
                         "failures, so blocked runs were recognised from their recorded error text. The saved file is unchanged.")
    return out


def _fmt_rate(num, den):
    return f"{num}/{den} ({num / den:.0%})" if den else "n/a"


def render_markdown(mode: str, rows: list[dict], m: dict, meta: dict) -> str:
    live = mode == "live"
    nd, nb, ns = m.get("runs_completed", m["runs"]), m.get("runs_blocked", 0), m.get("runs_skipped", 0)
    not_run = nb + ns
    ce = m.get("cases_evaluated", m["cases"])
    runs_line = f"- Runs: {m['runs']} agent turns across {m['cases']} fictional cases (one run each, no repeats)"
    if not_run:
        runs_line += f": {nd} completed, {nb} blocked by provider errors, {ns} not run"
    out = [f"# CampusPilot AI — Evaluation ({mode.upper()} mode)", "",
           f"- Run at: {meta['timestamp']}",
           f"- Provider / model: {meta['provider']} / {meta['model']}", runs_line]
    pacing = meta.get("pacing")
    if pacing:
        out.append(f"- Pacing: at least {pacing['min_interval']}s between model requests; HTTP 429 backoff "
                   f"{'/'.join(str(d) for d in pacing['quota_delays'])}s (never more than {pacing['max_wait']}s per wait, "
                   f"honouring the server's retry hint); stop after {pacing['stop_after']} consecutive provider-blocked turns")
    out.append("")
    if live:
        out += ["**Live mode:** results come from the real model. One run per case on a non-deterministic model: "
                "treat success rates as indicative, not statistically established.", ""]
    else:
        out += ["> **OFFLINE mode — pipeline check only.** The LLM is a script written by the developers, so the "
                "success rate below only shows that tools, handbook retrieval, memory and tracing work "
                "end to end. It says nothing about how well a real model chooses tools or words answers. "
                "Latency excludes any model/API time. Token usage is not available (nothing was called).", ""]
    if not_run:
        kinds = ", ".join(f"{k} x{v}" for k, v in m.get("provider_error_kinds", {}).items())
        detail = f"{nb} blocked" + (f": {kinds}" if kinds else "") + (f"; {ns} not run after the evaluation stopped early" if ns else "")
        out += [f"> **Provider errors left {not_run} of {m['runs']} runs unevaluated** ({detail}). They are **not** counted as agent "
                f"failures: every success rate below uses only the {nd} completed run{'s' if nd != 1 else ''}."
                + (" **No run completed, so this evaluation measured nothing about the agent.**" if nd == 0 else ""), ""]
    out += ["## Summary", "", "| Metric | Result |", "|---|---|"]
    if live or not_run:
        out += [f"| Runs completed (reached the model and finished) | {_fmt_rate(nd, m['runs'])} |",
                f"| Runs blocked by provider errors | {nb} |"]
        if ns:
            out.append(f"| Runs not run (stopped early) | {ns} |")
    out += [f"| Task success (cases fully {'evaluated and ' if not_run else ''}passed) | {_fmt_rate(m['cases_passed'], ce)} |",
            f"| Task success ({'completed runs' if not_run else 'individual runs'}) | {_fmt_rate(m['runs_passed'], nd)} |",
            f"| Tool choice appropriate{' (completed runs)' if not_run else ''} | {_fmt_rate(m['tool_choice_appropriate'], nd)} |",
            f"| Handbook answers citing a chunk id | {_fmt_rate(m['handbook_cited'], m['handbook_runs_with_evidence'])} |",
            f"| Traces recorded in SQLite | {_fmt_rate(m['traces_recorded'], m.get('runs_executed', m['runs']))} |",
            f"| Tool errors observed | {m['tool_errors_observed']} |",
            f"| Runs ending in an agent error | {m['runs_with_agent_error']} |"]
    lat = m["latency_ms"]
    if lat:
        scope = "end-to-end incl. LLM API" if live else "local pipeline only, no LLM time"
        out.append(f"| Latency ms ({scope}{', completed runs' if not_run else ''}) | min {lat['min']} / mean {lat['mean']} / median {lat['median']} / max {lat['max']} |")
    tk = m["tokens"]
    if tk["runs_with_usage"]:
        out.append(f"| Token usage (provider-reported; {tk['runs_with_usage']}/{tk['runs_total']} runs had it) | "
                   f"input total {tk['input_total']} (mean {tk['input_mean']}), output total {tk['output_total']} "
                   f"(mean {tk['output_mean']}) |")
    else:
        out.append(f"| Token usage | not available (0/{tk['runs_total']} runs reported usage) |")
    if not_run:
        out += ["", "## Results by category", "", "| Category | Passed | Completed runs | Blocked / not run |", "|---|---|---|---|"]
        out += [f"| {k} | {v['passed']} | {v['runs']} | {v.get('blocked', 0)} |" for k, v in m["by_category"].items()]
    else:
        out += ["", "## Results by category", "", "| Category | Passed | Runs |", "|---|---|---|"]
        out += [f"| {k} | {v['passed']} | {v['runs']} |" for k, v in m["by_category"].items()]
    out += ["", "## Per-run results", "",
            "| Case | Turn | Tools called | Tool choice OK | Check | Success | Latency ms | Tokens in/out | Evidence | Note |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        tokens = f"{r['input_tokens']}/{r['output_tokens']}" if r["input_tokens"] is not None or r["output_tokens"] is not None else "n/a"
        oc = _outcome(r)
        if oc == "completed":
            tool_cell, check_cell, result_cell = ("yes" if r["tool_choice_appropriate"] else "NO",
                                                  "pass" if r["check_passed"] else "FAIL", "PASS" if r["success"] else "FAIL")
        else:
            tool_cell = check_cell = "n/a"
            result_cell = "BLOCKED" if oc == "blocked_by_provider" else "SKIPPED"
        out.append(f"| {r['case_id']} | {r['turn']} | {', '.join(r['called_tools']) or '(none)'} | "
                   f"{tool_cell} | {check_cell} | {result_cell} | {r['latency_ms']} | {tokens} | "
                   f"{', '.join(i.split('#')[1] for i in r['evidence_ids']) or '-'} | {r['note']} |")
    out += ["", "## Failures", ""]
    out += [f"- {f}" for f in m["failed_runs"]] or ["- none"]
    if not_run:
        out += ["", "## Blocked or not run (excluded from success rates)", ""]
        out += [f"- {b}" for b in m["blocked_runs"]]
    out += ["", "## Method and limits", "",
            "- Scoring is rule-based: tool choice must include the expected tool(s) and nothing outside the allowed set; "
            "the check inspects tool results, retrieved evidence and keywords in the answer. There is no LLM judge.",
            "- Keyword checks can misjudge unusual but correct wording, so a live-mode failure should be read before it is trusted.",
            "- Tokens are reported only when the provider supplied them; missing values are never estimated.",
            "- Fictional data only; handbook index uses the local embedding model with the configured relevance threshold."]
    if live or not_run:
        out.append("- Runs whose model call failed with a provider-side error (HTTP 429 quota, 408/5xx outage, connection "
                   "error, or 401/403/404 key/permission/model problems) are excluded from every success rate. Other "
                   "errors (for example HTTP 400 or unexpected exceptions) still count as agent failures.")
    return "\n".join(out) + "\n"
