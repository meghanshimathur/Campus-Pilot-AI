"""Agent + handbook retrieval integration (scripted LLM, offline test embedder)."""
import json

import pytest
from fastapi.testclient import TestClient

from campuspilot.agent import Agent, SYSTEM_PROMPT
from campuspilot.api import create_app
from campuspilot.config import ROOT
from campuspilot.provider import ChatResponse, LLMProvider, ToolCall
from campuspilot.rag import NOT_FOUND_MESSAGE
from campuspilot.tool_registry import ToolRegistry

HANDBOOK_TEXT = (ROOT / "data" / "handbook" / "student_handbook.md").read_text()


class Scripted(LLMProvider):
    name = "scripted"

    def __init__(self, responses):
        self.responses, self.seen = list(responses), []

    def chat(self, messages, tool_schemas):
        self.seen.append(json.loads(json.dumps(messages)))
        return self.responses.pop(0)


@pytest.fixture
def registry(settings, retriever):
    return ToolRegistry(settings.data_dir, settings.database_path, retriever)


def _policy_call(q):
    return ChatResponse(tool_calls=[ToolCall("h1", "search_handbook", {"query": q})])


def test_policy_question_retrieves_relevant_chunks_only(registry):
    q = "The payment portal failed. Can I pay on the next working day?"
    p = Scripted([_policy_call(q), ChatResponse(content="Yes, if reported within 24 hours [student_handbook.md#s2-1].")])
    r = Agent(p, registry).run(q, "STU002")
    # the tool result sent back to the LLM contains the relevant clause...
    tool_msg = json.loads([m for m in p.seen[1] if m["role"] == "tool"][0]["content"])
    assert tool_msg["found"] is True
    assert any("24 hours" in c["text"] for c in tool_msg["chunks"])
    # ...but never the whole handbook: only a few chunks, and unrelated sections are absent
    sent = json.dumps(p.seen[1])
    assert len(tool_msg["chunks"]) <= 3 and len(sent) < len(HANDBOOK_TEXT)
    assert "Cooking appliances" not in sent and "caution deposit" not in sent
    # evidence carries source/chunk references (no full text)
    assert r.evidence and all(set(e) == {"chunk_id", "source", "section", "clauses", "score", "keyword_coverage", "match"} for e in r.evidence)
    assert r.evidence[0]["chunk_id"].startswith("student_handbook.md#s2-")
    assert r.response.startswith("Yes")


def test_missing_evidence_tells_model_not_to_invent(registry):
    q = "Where can I park my car on campus?"
    p = Scripted([_policy_call(q), ChatResponse(content="That is not in the handbook.")])
    r = Agent(p, registry).run(q, "STU001")
    tool_msg = json.loads([m for m in p.seen[1] if m["role"] == "tool"][0]["content"])
    assert tool_msg["found"] is False and tool_msg["chunks"] == []
    assert tool_msg["message"] == NOT_FOUND_MESSAGE
    assert r.evidence == []
    assert r.response == "That is not in the handbook."


def test_system_prompt_enforces_handbook_rules():
    assert "search_handbook" in SYSTEM_PROMPT and "found=false" in SYSTEM_PROMPT
    assert "do NOT invent" in SYSTEM_PROMPT and "chunk_id" in SYSTEM_PROMPT
    assert HANDBOOK_TEXT[:200] not in SYSTEM_PROMPT  # handbook is never embedded in the prompt


def test_direct_answer_does_not_touch_retriever(settings):
    class Boom:
        def search(self, q):
            raise AssertionError("retriever must not be called")
    reg = ToolRegistry(settings.data_dir, settings.database_path, Boom())
    r = Agent(Scripted([ChatResponse(content="Hello!")]), reg).run("hi", "STU001")
    assert r.response == "Hello!" and r.evidence == []


def test_retriever_failure_is_contained(settings):
    class Broken:
        def search(self, q):
            raise RuntimeError("model download failed at /secret/path")
    reg = ToolRegistry(settings.data_dir, settings.database_path, Broken())
    p = Scripted([_policy_call("What is the late fee policy?"), ChatResponse(content="Sorry, handbook is unavailable.")])
    r = Agent(p, reg).run("late fee?", "STU001")
    assert "error" in r.tools_called[0]["result"] and "secret" not in json.dumps(r.tools_called)
    assert r.response == "Sorry, handbook is unavailable."


def test_handbook_and_student_tools_in_one_turn(registry):
    p = Scripted([
        ChatResponse(tool_calls=[ToolCall("a", "check_fee_status", {}),
                                 ToolCall("b", "search_handbook", {"query": "late fee after due date"})]),
        ChatResponse(content="You are overdue; a 2% monthly late fee applies."),
    ])
    r = Agent(p, registry).run("I'm overdue, what happens?", "STU003")
    assert [t["name"] for t in r.tools_called] == ["check_fee_status", "search_handbook"]
    assert r.tools_called[0]["result"]["status"] == "overdue"
    assert any(e["section"].startswith("1.") for e in r.evidence)


def test_all_original_tools_still_work_with_retriever(registry):
    assert registry.execute("check_fee_status", {}, "STU002")["status"] == "partial"
    assert registry.execute("check_scholarship_status", {}, "STU001")["status"] == "active"
    assert registry.execute("create_support_request", {"category": "fees", "description": "x"}, "STU001")["ticket_id"]
    assert registry.execute("search_course_catalog", {"query": "spark"}, "STU001")["count"] == 1
    assert registry.execute("get_professor_office_hours", {"professor_name": "rao"}, "STU001")["professors"]
    assert registry.execute("check_room_availability", {"room_id": "A-101", "day": "Mon"}, "STU001")["bookings"]
    assert registry.execute("get_campus_events", {"from_date": "2026-11-01"}, "STU001")["count"] >= 1
    assert registry.execute("search_handbook", {"query": "hostel gate closing time"}, "STU001")["found"] is True


def test_api_chat_returns_evidence(settings, retriever):
    p = Scripted([_policy_call("hostel gate closing time"), ChatResponse(content="Gate closes at 22:30.")])
    c = TestClient(create_app(settings, provider=p, retriever=retriever))
    body = c.post("/api/chat", json={"message": "When does the hostel gate close?", "student_id": "STU001"}).json()
    assert body["evidence"][0]["chunk_id"].startswith("student_handbook.md#s8-")
    assert "text" not in body["evidence"][0]
