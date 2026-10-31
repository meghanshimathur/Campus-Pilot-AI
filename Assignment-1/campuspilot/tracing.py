"""Execution traces: one sanitized SQLite row per agent run (successful or not).

Privacy rules: raw tool outputs are never stored (only a summary: outcome, field names,
chunk ids, ticket id, counts); free text goes through the same redaction as chat history;
errors are recorded as the exception type plus, for recognised API errors only, the HTTP
status and a short message with key-like text masked (LLMProvider.describe_error). Any other
exception is recorded by type name only, so keys or internal paths in its message cannot
reach the database.
"""
import json
import uuid

from campuspilot.db import connect
from campuspilot.memory import redact_for_history

MAX_ARG_CHARS = 200


def _clean_text(value) -> str:
    return redact_for_history(str(value))[:MAX_ARG_CHARS]


def tool_outcome(name: str, output: dict) -> str:
    """ok | error | rejected (memory validator said no) | no_evidence (handbook found nothing)."""
    if "error" in output:
        return "error"
    if output.get("saved") is False:
        return "rejected"
    if name == "search_handbook" and output.get("found") is False:
        return "no_evidence"
    return "ok"


def summarize_output(name: str, output: dict) -> dict:
    """Describe a tool result without copying its (possibly sensitive) values."""
    if "error" in output:
        return {"error": _clean_text(output["error"])}
    if name == "search_handbook":
        return {"found": bool(output.get("found")), "chunk_ids": [c["chunk_id"] for c in output.get("chunks", [])]}
    if name == "remember_preference":
        return {"saved": bool(output.get("saved")), "key": output.get("key")}
    if name == "create_support_request":
        return {"ticket_id": output.get("ticket_id"), "category": output.get("category")}
    summary = {"fields": sorted(output.keys())}  # e.g. fee/scholarship records: names only, no values
    if isinstance(output.get("count"), int):
        summary["count"] = output["count"]
    return summary


def sanitize_arguments(name: str, arguments) -> dict:
    if not isinstance(arguments, dict):
        return {"_invalid_arguments": True}
    if name == "remember_preference":
        return {"key": _clean_text(arguments.get("key", "")), "value": "[omitted]"}
    return {str(k): (_clean_text(v) if isinstance(v, str) else v if isinstance(v, (int, float, bool)) or v is None
                     else "[complex value]") for k, v in arguments.items()}


def build_trace(*, student_id, session_id, query, result, provider, model) -> dict:
    tools = []
    for call in result.tools_called:
        output = call["result"] if isinstance(call["result"], dict) else {}
        tools.append({"name": call["name"], "arguments": sanitize_arguments(call["name"], call["arguments"]),
                      "outcome": tool_outcome(call["name"], output), "summary": summarize_output(call["name"], output),
                      "latency_ms": call["latency_ms"]})
    if result.error:
        status = "error"
    elif any(t["outcome"] == "error" for t in tools):
        status = "completed_with_tool_errors"
    else:
        status = "success"
    return {
        "trace_id": uuid.uuid4().hex,
        "student_id": str(student_id)[:20],
        "session_id": session_id or "",
        "user_query": redact_for_history(query),
        "action": "used_tools" if tools else ("failed" if result.error else "answered_directly"),
        "status": status,
        "agent_response": redact_for_history(result.response),
        "tools_called": tools,
        "evidence_used": result.evidence,  # references only (id, source, section, clauses, score)
        "memories_used": [m["key"] for m in result.memories_used],  # keys only
        "total_latency_ms": result.latency_ms,
        "input_tokens": result.input_tokens,    # None when the provider did not report usage
        "output_tokens": result.output_tokens,
        "llm_provider": provider,
        "llm_model": model,
        "error": result.error,  # "llm_error: <describe_error()>" (type, HTTP status, key-masked message) or a fixed code
    }


class TraceStore:
    JSON_FIELDS = ("tools_called", "evidence_used", "memories_used")

    def __init__(self, db_path):
        self.db_path = db_path

    def record(self, trace: dict) -> str:
        row = dict(trace)
        for f in self.JSON_FIELDS:
            row[f] = json.dumps(row[f])
        cols = ", ".join(row)
        conn = connect(self.db_path)
        try:
            conn.execute(f"INSERT INTO traces ({cols}) VALUES ({', '.join('?' for _ in row)})", list(row.values()))
            conn.commit()
        finally:
            conn.close()
        return trace["trace_id"]

    def get(self, trace_id: str) -> dict | None:
        conn = connect(self.db_path)
        try:
            r = conn.execute("SELECT * FROM traces WHERE trace_id=?", (trace_id,)).fetchone()
        finally:
            conn.close()
        if r is None:
            return None
        d = dict(r)
        for f in self.JSON_FIELDS:
            d[f] = json.loads(d[f]) if d[f] else []
        return d

    def list_for_student(self, student_id: str, limit: int = 20) -> list[dict]:
        # Used by tests only: no API route exposes it because there is no authentication yet.
        conn = connect(self.db_path)
        try:
            ids = [r[0] for r in conn.execute(
                "SELECT trace_id FROM traces WHERE student_id=? ORDER BY id DESC LIMIT ?", (student_id, limit))]
        finally:
            conn.close()
        return [self.get(i) for i in ids]
