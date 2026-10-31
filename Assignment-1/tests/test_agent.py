import json

import pytest

from campuspilot.agent import FALLBACK, Agent
from campuspilot.provider import ChatResponse, LLMProvider, ToolCall
from campuspilot.tool_registry import ToolRegistry


class ScriptedProvider(LLMProvider):
    """Fake LLM that returns pre-set responses and records the messages it saw."""
    name = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)
        self.seen = []

    def chat(self, messages, tool_schemas):
        self.seen.append(json.loads(json.dumps(messages)))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def registry(settings):
    return ToolRegistry(settings.data_dir, settings.database_path)


def test_answers_directly_without_tool(registry):
    p = ScriptedProvider([ChatResponse(content="Hello! How can I help?", input_tokens=10, output_tokens=5)])
    r = Agent(p, registry).run("hi", "STU001")
    assert r.response == "Hello! How can I help?"
    assert r.tools_called == []
    assert (r.input_tokens, r.output_tokens) == (10, 5)


def test_calls_tool_then_uses_result(registry):
    p = ScriptedProvider([
        ChatResponse(tool_calls=[ToolCall("c1", "check_fee_status", {})], input_tokens=20, output_tokens=3),
        ChatResponse(content="You owe 300000.", input_tokens=40, output_tokens=6),
    ])
    r = Agent(p, registry).run("What is my fee status?", "STU003")
    assert [t["name"] for t in r.tools_called] == ["check_fee_status"]
    assert r.tools_called[0]["result"]["due"] == 300000
    # the tool result must have been sent back to the model before the final answer
    tool_msgs = [m for m in p.seen[1] if m["role"] == "tool"]
    assert json.loads(tool_msgs[0]["content"])["status"] == "overdue"
    assert r.response == "You owe 300000."
    assert (r.input_tokens, r.output_tokens) == (60, 9)
    assert r.latency_ms >= 0


def test_unknown_student_error_reaches_model(registry):
    p = ScriptedProvider([
        ChatResponse(tool_calls=[ToolCall("c1", "check_fee_status", {})]),
        ChatResponse(content="I couldn't find that student ID."),
    ])
    r = Agent(p, registry).run("fees?", "NOPE")
    assert "error" in r.tools_called[0]["result"]
    assert "error" in json.loads([m for m in p.seen[1] if m["role"] == "tool"][0]["content"])
    assert r.response == "I couldn't find that student ID."


def test_invalid_tool_name_and_args_handled(registry):
    p = ScriptedProvider([
        ChatResponse(tool_calls=[ToolCall("c1", "hack", {}), ToolCall("c2", "check_fee_status", None)]),
        ChatResponse(content="Sorry, I couldn't do that."),
    ])
    r = Agent(p, registry).run("x", "STU001")
    assert all("error" in t["result"] for t in r.tools_called)
    assert r.response == "Sorry, I couldn't do that."


def test_missing_token_usage_stays_none(registry):
    r = Agent(ScriptedProvider([ChatResponse(content="ok")]), registry).run("hi", "STU001")
    assert r.input_tokens is None and r.output_tokens is None


def test_llm_failure_returns_fallback(registry):
    r = Agent(ScriptedProvider([RuntimeError("api key sk-secret")]), registry).run("hi", "STU001")
    assert r.response == FALLBACK
    assert r.error == "llm_error: RuntimeError"
    assert "sk-secret" not in json.dumps(r.__dict__)


def test_step_limit_stops_endless_tool_loop(registry):
    looping = [ChatResponse(tool_calls=[ToolCall(f"c{i}", "check_fee_status", {})]) for i in range(10)]
    r = Agent(ScriptedProvider(looping), registry, max_steps=3).run("x", "STU001")
    assert r.error == "max_steps_exceeded" and r.response == FALLBACK
    assert len(r.tools_called) == 3


def test_support_request_flow(registry):
    p = ScriptedProvider([
        ChatResponse(tool_calls=[ToolCall("c1", "create_support_request",
                                          {"category": "fees", "description": "Portal failed"})]),
        ChatResponse(content="Ticket raised."),
    ])
    r = Agent(p, registry).run("Please raise a ticket", "STU002")
    assert r.tools_called[0]["result"]["ticket_id"].startswith("TKT-")
