import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from campuspilot.agent import FALLBACK, Agent
from campuspilot.api import create_app
from campuspilot.db import connect, init_db
from campuspilot.memory import REDACTED, MemoryStore
from campuspilot.provider import ChatResponse, LLMProvider, ToolCall
from campuspilot.tool_registry import ToolRegistry
from campuspilot.tracing import TraceStore, sanitize_arguments, summarize_output, tool_outcome


class Scripted(LLMProvider):
    name = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)

    def chat(self, messages, tool_schemas):
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def parts(settings, retriever):
    mem, tracer = MemoryStore(settings.database_path), TraceStore(settings.database_path)
    reg = ToolRegistry(settings.data_dir, settings.database_path, retriever, mem)
    return lambda responses, **kw: (Agent(Scripted(responses), reg, memory=mem, tracer=tracer, **kw), tracer)


def dump_db(settings):
    conn = connect(settings.database_path)
    text = " ".join(str(v) for row in conn.execute("SELECT * FROM traces") for v in tuple(row))
    conn.close()
    return text


def call(name, args, cid="c"):
    return ToolCall(cid, name, args)


def test_direct_answer_trace(parts):
    agent, tracer = parts([ChatResponse(content="Hello!", input_tokens=10, output_tokens=4, model="m-1")])
    r = agent.run("hi", "STU001", "s1")
    t = tracer.get(r.trace_id)
    assert (t["action"], t["status"], t["tools_called"], t["evidence_used"]) == ("answered_directly", "success", [], [])
    assert (t["user_query"], t["agent_response"], t["student_id"], t["session_id"]) == ("hi", "Hello!", "STU001", "s1")
    assert (t["input_tokens"], t["output_tokens"], t["llm_provider"], t["llm_model"]) == (10, 4, "scripted", "m-1")
    assert t["total_latency_ms"] == r.latency_ms and t["error"] is None


def test_missing_token_counts_stored_as_null_not_zero(parts):
    agent, tracer = parts([ChatResponse(content="ok")])
    t = tracer.get(agent.run("hi", "STU001", "s").trace_id)
    assert t["input_tokens"] is None and t["output_tokens"] is None


def test_tool_trace_has_summary_but_no_raw_values(parts, settings):
    agent, tracer = parts([ChatResponse(tool_calls=[call("check_fee_status", {})]),
                           ChatResponse(content="You have fees due.")])
    r = agent.run("What is my fee status?", "STU003", "s")
    t = tracer.get(r.trace_id)
    tool = t["tools_called"][0]
    assert t["action"] == "used_tools" and tool["name"] == "check_fee_status" and tool["outcome"] == "ok"
    assert "status" in tool["summary"]["fields"] and "result" not in tool
    raw = dump_db(settings)
    assert "300000" not in raw and "Rohan" not in raw and "overdue" not in raw  # raw record values never stored


def test_handbook_evidence_references_only(parts, settings):
    agent, tracer = parts([ChatResponse(tool_calls=[call("search_handbook", {"query": "payment portal failed"})]),
                           ChatResponse(content="Report it within 24 hours.")])
    t = tracer.get(agent.run("portal failed", "STU001", "s").trace_id)
    assert t["evidence_used"] and all(set(e) == {"chunk_id", "source", "section", "clauses", "score", "keyword_coverage", "match"}
                                      for e in t["evidence_used"])
    assert t["tools_called"][0]["summary"]["chunk_ids"] == [e["chunk_id"] for e in t["evidence_used"]]
    assert "24 hours" not in dump_db(settings).replace("Report it within 24 hours.", "")  # no chunk text stored


def test_no_evidence_outcome(parts):
    agent, tracer = parts([ChatResponse(tool_calls=[call("search_handbook", {"query": "parking for my car"})]),
                           ChatResponse(content="Not found in the handbook.")])
    t = tracer.get(agent.run("parking?", "STU001", "s").trace_id)
    assert t["tools_called"][0]["outcome"] == "no_evidence" and t["evidence_used"] == [] and t["status"] == "success"


def test_tool_error_recorded(parts):
    agent, tracer = parts([ChatResponse(tool_calls=[call("check_fee_status", {})]),
                           ChatResponse(content="I could not find that student.")])
    t = tracer.get(agent.run("fees?", "NOPE", "s").trace_id)
    assert t["tools_called"][0]["outcome"] == "error" and "not found" in t["tools_called"][0]["summary"]["error"]
    assert t["status"] == "completed_with_tool_errors"


def test_unknown_tool_and_bad_arguments_recorded(parts):
    agent, tracer = parts([ChatResponse(tool_calls=[call("hack", {}, "a"), call("check_fee_status", None, "b")]),
                           ChatResponse(content="Sorry.")])
    t = tracer.get(agent.run("x", "STU001", "s").trace_id)
    assert [x["outcome"] for x in t["tools_called"]] == ["error", "error"]
    assert t["tools_called"][1]["arguments"] == {"_invalid_arguments": True}


def test_rejected_memory_outcome_and_value_omitted(parts, settings):
    agent, tracer = parts([ChatResponse(tool_calls=[call("remember_preference", {"key": "study_preference",
                                                                                 "value": "password hunter2"})]),
                           ChatResponse(content="I can't store that.")])
    t = tracer.get(agent.run("remember my password hunter2", "STU001", "s").trace_id)
    assert t["tools_called"][0]["outcome"] == "rejected" and t["tools_called"][0]["arguments"]["value"] == "[omitted]"
    assert "hunter2" not in dump_db(settings)
    assert t["user_query"] == REDACTED


def test_saved_memory_value_not_in_trace_only_key(parts, settings):
    agent, tracer = parts([ChatResponse(tool_calls=[call("remember_preference", {"key": "preferred_name", "value": "Dev"})]),
                           ChatResponse(content="ok")])
    agent.run("call me Dev", "STU001", "s1")
    agent2, tracer2 = parts([ChatResponse(content="hi")])
    t = tracer2.get(agent2.run("hello", "STU001", "s2").trace_id)
    assert t["memories_used"] == ["preferred_name"]  # keys only
    assert t["tools_called"] == []


def test_llm_failure_trace_has_type_only_never_key(parts, settings):
    secret = "AIzaSyFAKEKEYFAKEKEYFAKEKEYFAKEKEY12345"
    agent, tracer = parts([RuntimeError(f"401 invalid key {secret}")])
    r = agent.run("hi", "STU001", "s")
    t = tracer.get(r.trace_id)
    assert r.response == FALLBACK and t["status"] == "error" and t["action"] == "failed"
    assert t["error"] == "llm_error: RuntimeError" and secret not in dump_db(settings)


def test_step_limit_failure_recorded(parts):
    looping = [ChatResponse(tool_calls=[call("get_campus_events", {"from_date": "2026-11-01"}, f"c{i}")]) for i in range(5)]
    agent, tracer = parts(looping, max_steps=2)
    t = tracer.get(agent.run("events", "STU001", "s").trace_id)
    assert t["status"] == "error" and t["error"] == "max_steps_exceeded" and len(t["tools_called"]) == 2


def test_free_text_arguments_are_redacted(parts):
    agent, tracer = parts([ChatResponse(tool_calls=[call("create_support_request", {
        "category": "fees", "description": "my card number is 4111 1111 1111 1111"})]),
        ChatResponse(content="Ticket raised.")])
    t = tracer.get(agent.run("raise a ticket", "STU001", "s").trace_id)
    assert t["tools_called"][0]["arguments"]["description"] == REDACTED
    assert t["tools_called"][0]["summary"]["ticket_id"].startswith("TKT-")


def test_tracer_failure_never_breaks_reply(settings):
    class BrokenTracer:
        def record(self, trace):
            raise RuntimeError("disk full")
    reg = ToolRegistry(settings.data_dir, settings.database_path)
    r = Agent(Scripted([ChatResponse(content="Hello!")]), reg, tracer=BrokenTracer()).run("hi", "STU001")
    assert r.response == "Hello!" and r.trace_id is None


def test_agent_without_tracer_unchanged(settings):
    reg = ToolRegistry(settings.data_dir, settings.database_path)
    r = Agent(Scripted([ChatResponse(content="Hello!")]), reg).run("hi", "STU001")
    assert r.trace_id is None


def test_helpers():
    assert tool_outcome("x", {"error": "e"}) == "error" and tool_outcome("x", {"saved": False}) == "rejected"
    assert tool_outcome("search_handbook", {"found": False}) == "no_evidence" and tool_outcome("x", {"a": 1}) == "ok"
    assert summarize_output("search_course_catalog", {"count": 2, "courses": [1, 2]}) == {
        "fields": ["count", "courses"], "count": 2}
    assert sanitize_arguments("x", {"n": 3, "d": {"a": 1}}) == {"n": 3, "d": "[complex value]"}


def test_traces_table_migrates_from_older_schema(tmp_path):
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE traces (id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT UNIQUE NOT NULL, "
                 "session_id TEXT NOT NULL, user_query TEXT NOT NULL, agent_response TEXT, tools_called TEXT, "
                 "evidence_used TEXT, memories_used TEXT, total_latency_ms INTEGER, input_tokens INTEGER, "
                 "output_tokens INTEGER, llm_provider TEXT, llm_model TEXT, error TEXT, "
                 "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    conn.commit(); conn.close()
    init_db(db); init_db(db)
    conn = connect(db)
    assert {"student_id", "action", "status"} <= {r["name"] for r in conn.execute("PRAGMA table_info(traces)")}
    conn.close()


def test_list_for_student_scoped(parts):
    agent, tracer = parts([ChatResponse(content="a"), ChatResponse(content="b"), ChatResponse(content="c")])
    agent.run("q1", "STU001", "s"); agent.run("q2", "STU001", "s"); agent.run("q3", "STU002", "s")
    assert [t["user_query"] for t in tracer.list_for_student("STU001")] == ["q2", "q1"]
    assert len(tracer.list_for_student("STU002")) == 1 and tracer.list_for_student("STU009") == []


def test_api_returns_trace_id_and_fetches_trace(settings, retriever):
    p = Scripted([ChatResponse(tool_calls=[call("check_fee_status", {})]), ChatResponse(content="ok", input_tokens=7, output_tokens=2)])
    c = TestClient(create_app(settings, provider=p, retriever=retriever))
    body = c.post("/api/chat", json={"message": "fees?", "student_id": "STU002"}).json()
    t = c.get(f"/api/traces/{body['trace_id']}").json()
    assert t["tools_called"][0]["name"] == "check_fee_status" and t["input_tokens"] == 7
    assert c.get("/api/traces/" + "0" * 32).status_code == 404
    assert c.get("/api/traces/not-a-trace-id").status_code == 404
