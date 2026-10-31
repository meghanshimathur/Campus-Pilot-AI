"""Swallowed failures are logged for developers, without leaking secrets, sensitive data or raw exception text."""
import logging

import pytest

from campuspilot.agent import Agent
from campuspilot.provider import ChatResponse, LLMProvider, ToolCall
from campuspilot.tool_registry import ToolRegistry

SECRET = "AIzaSyFAKEKEY1234567890abcdef"
LEAKY = f"boom: student_id=STU001 bank account 123456789012 key={SECRET} at /Users/someone/private/path"


class Scripted(LLMProvider):
    name = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)

    def chat(self, messages, tool_schemas):
        return self.responses.pop(0)


class Boom:
    """Stands in for memory/tracer: every method raises an exception whose message is full of sensitive text."""

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise RuntimeError(LEAKY)
        return fail


def _assert_clean(caplog):
    text = caplog.text + "".join(str(r.exc_info) for r in caplog.records)
    for leaked in (SECRET, "123456789012", "STU001", "/Users/someone", "boom:"):
        assert leaked not in text
    assert all(r.exc_info is None for r in caplog.records)  # a traceback would carry the message


def test_unexpected_tool_failure_is_logged_by_type_only(settings, monkeypatch, caplog):
    registry = ToolRegistry(settings.data_dir, settings.database_path)

    def explode(*args, **kwargs):
        raise RuntimeError(LEAKY)

    monkeypatch.setattr("campuspilot.tools.check_fee_status", explode)
    with caplog.at_level(logging.WARNING, logger="campuspilot.tools"):
        out = registry.execute("check_fee_status", {}, "STU001")
    assert out == {"error": "Tool 'check_fee_status' failed unexpectedly."}   # unchanged, generic
    assert "check_fee_status" in caplog.text and "RuntimeError" in caplog.text
    _assert_clean(caplog)


def test_expected_tool_errors_and_unknown_tools_are_not_logged_as_failures(settings, caplog):
    registry = ToolRegistry(settings.data_dir, settings.database_path)
    with caplog.at_level(logging.WARNING, logger="campuspilot.tools"):
        assert "error" in registry.execute("check_fee_status", {}, "NOPE")   # normal {"error": ...}
        assert "error" in registry.execute("no_such_tool", {}, "STU001")
    assert caplog.records == []


@pytest.mark.parametrize("failing", ["memory", "tracer"])
def test_swallowed_agent_failures_are_logged_and_the_reply_still_works(settings, failing, caplog):
    registry = ToolRegistry(settings.data_dir, settings.database_path)
    agent = Agent(Scripted([ChatResponse(content="Hello!")]), registry,
                  memory=Boom() if failing == "memory" else None,
                  tracer=Boom() if failing == "tracer" else None)
    with caplog.at_level(logging.WARNING, logger="campuspilot.agent"):
        result = agent.run("hi", "STU001", "sess1")
    assert result.response == "Hello!" and result.error is None                 # behaviour unchanged
    assert "RuntimeError" in caplog.text
    _assert_clean(caplog)
    if failing == "memory":
        assert "context loading" in caplog.text and "history persistence" in caplog.text
    else:
        assert "trace recording" in caplog.text and result.trace_id is None


def test_tool_failure_inside_an_agent_turn_does_not_leak_into_logs_or_reply(settings, monkeypatch, caplog):
    registry = ToolRegistry(settings.data_dir, settings.database_path)
    monkeypatch.setattr("campuspilot.tools.check_fee_status", lambda *a, **k: (_ for _ in ()).throw(RuntimeError(LEAKY)))
    provider = Scripted([ChatResponse(tool_calls=[ToolCall("c1", "check_fee_status", {})]),
                         ChatResponse(content="I could not check that right now.")])
    with caplog.at_level(logging.WARNING):
        result = Agent(provider, registry).run("fees?", "STU001")
    assert result.tools_called[0]["result"] == {"error": "Tool 'check_fee_status' failed unexpectedly."}
    assert "boom" not in result.response
    _assert_clean(caplog)
