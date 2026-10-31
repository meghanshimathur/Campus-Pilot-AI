"""Retry-with-backoff and error descriptions for the LLM providers (fake clients; no network, no real key)."""
import logging
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from google.genai import errors

from campuspilot.agent import FALLBACK, Agent
from campuspilot.api import create_app
from campuspilot.provider import (ChatResponse, GeminiProvider, LLMProvider, OpenAIProvider, ToolCall,
                                  is_transient_error)
from campuspilot.tool_registry import ToolRegistry
from campuspilot.tracing import TraceStore

FAKE_KEY = "AQ.FakeKeyFakeKeyFakeKeyFakeKeyFakeKey1234567"


def api_error(code, status, message="boom"):
    cls = errors.ServerError if code >= 500 else errors.ClientError
    return cls(code, {"error": {"code": code, "message": message, "status": status}})


OK_PARTS = [NS(function_call=None, text="hello", thought=None, thought_signature=None)]
OK_RESPONSE = NS(candidates=[NS(content=NS(parts=OK_PARTS))],
                 usage_metadata=NS(prompt_token_count=5, candidates_token_count=2, thoughts_token_count=None), model_version="m")


class FlakyGemini:
    """Fake google-genai client: raises the given errors in order, then returns a normal response."""
    def __init__(self, *failures):
        self.failures, self.calls = list(failures), 0
        self.models = NS(generate_content=self._generate)

    def _generate(self, **kw):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return OK_RESPONSE


def gemini(client, **kw):
    sleeps = []
    return GeminiProvider(FAKE_KEY, "m", client=client, sleep=sleeps.append, **kw), sleeps


MSG = [{"role": "user", "content": "hi"}]


# ---- which errors are transient ----
@pytest.mark.parametrize("code,status,transient", [
    (503, "UNAVAILABLE", True), (500, "INTERNAL", True), (502, "BAD_GATEWAY", True), (504, "DEADLINE_EXCEEDED", True),
    (429, "RESOURCE_EXHAUSTED", True), (408, "TIMEOUT", True),
    (400, "INVALID_ARGUMENT", False), (401, "UNAUTHENTICATED", False), (403, "PERMISSION_DENIED", False),
    (404, "NOT_FOUND", False),
])
def test_transient_classification(code, status, transient):
    assert is_transient_error(api_error(code, status)) is transient


def test_connection_style_errors_by_class_name_and_plain_errors():
    APIConnectionError = type("APIConnectionError", (Exception,), {})
    ReadTimeout = type("ReadTimeout", (Exception,), {})
    assert is_transient_error(APIConnectionError()) and is_transient_error(ReadTimeout())
    assert not is_transient_error(RuntimeError("x")) and not is_transient_error(ValueError("x"))
    assert is_transient_error(NS(status_code=429)) and not is_transient_error(NS(status_code=401))  # OpenAI-style attribute


# ---- retry behaviour ----
def test_retries_503_twice_then_succeeds():
    client = FlakyGemini(api_error(503, "UNAVAILABLE"), api_error(503, "UNAVAILABLE"))
    p, sleeps = gemini(client)
    r = p.chat(MSG, [])
    assert r.content == "hello" and client.calls == 3 and sleeps == [1.0, 2.0]
    assert (r.input_tokens, r.output_tokens) == (5, 2)  # usage comes from the one successful response only


def test_gives_up_after_the_retries_and_raises_the_last_error():
    client = FlakyGemini(*[api_error(503, "UNAVAILABLE")] * 5)
    p, sleeps = gemini(client)
    with pytest.raises(errors.ServerError):
        p.chat(MSG, [])
    assert client.calls == 3 and sleeps == [1.0, 2.0]


@pytest.mark.parametrize("code,status", [(404, "NOT_FOUND"), (400, "INVALID_ARGUMENT"), (401, "UNAUTHENTICATED"), (403, "PERMISSION_DENIED")])
def test_client_errors_are_never_retried(code, status):
    client = FlakyGemini(api_error(code, status), api_error(code, status))
    p, sleeps = gemini(client)
    with pytest.raises(errors.ClientError):
        p.chat(MSG, [])
    assert client.calls == 1 and sleeps == []


def test_rate_limit_is_retried():
    client = FlakyGemini(api_error(429, "RESOURCE_EXHAUSTED"))
    p, sleeps = gemini(client)
    assert p.chat(MSG, []).content == "hello" and client.calls == 2 and sleeps == [1.0]


def test_retries_can_be_disabled_or_tuned():
    client = FlakyGemini(api_error(503, "UNAVAILABLE"))
    p, sleeps = gemini(client, retry_delays=())
    with pytest.raises(errors.ServerError):
        p.chat(MSG, [])
    assert client.calls == 1 and sleeps == []
    client2 = FlakyGemini(api_error(503, "UNAVAILABLE"))
    p2, sleeps2 = gemini(client2, retry_delays=(0.25,))
    p2.chat(MSG, [])
    assert sleeps2 == [0.25] and client2.calls == 2


def test_retry_logs_a_warning_without_the_key(caplog):
    p, _ = gemini(FlakyGemini(api_error(503, "UNAVAILABLE", f"overloaded for {FAKE_KEY}")))
    with caplog.at_level(logging.WARNING, logger="campuspilot.provider"):
        p.chat(MSG, [])
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "retry 1/2" in text and "503" in text and FAKE_KEY not in text and "<KEY>" in text


def test_openai_provider_retries_too():
    calls = []

    class RateLimited(Exception):
        status_code = 429
        message = "slow down"

    msg = NS(content="ok", tool_calls=None)

    def create(**kw):
        calls.append(1)
        if len(calls) == 1:
            raise RateLimited()
        return NS(choices=[NS(message=msg)], usage=None, model="gpt")

    sleeps = []
    p = OpenAIProvider("sk-fake", "gpt", client=NS(chat=NS(completions=NS(create=create))), sleep=sleeps.append)
    assert p.chat(MSG, []).content == "ok" and len(calls) == 2 and sleeps == [1.0]


# ---- error descriptions ----
def test_describe_includes_type_http_status_and_short_message():
    p, _ = gemini(FlakyGemini())
    d = p.describe_error(api_error(503, "UNAVAILABLE", "This model is currently experiencing high demand."))
    assert d == "ServerError 503 UNAVAILABLE: This model is currently experiencing high demand."
    assert p.describe_error(api_error(404, "NOT_FOUND", "model gone")) == "ClientError 404 NOT_FOUND: model gone"


def test_describe_masks_the_key_and_key_like_text_and_truncates():
    p, _ = gemini(FlakyGemini())
    d = p.describe_error(api_error(400, "INVALID_ARGUMENT",
                                   f"bad key {FAKE_KEY} and AIzaSyABCDEFGHIJKLMNOPQRSTUV and ya29.abcdefghijklmnop"))
    assert FAKE_KEY not in d and "AIzaSy" not in d and "ya29." not in d and d.count("<KEY>") == 3
    long = p.describe_error(api_error(400, "INVALID_ARGUMENT", "x" * 5000))
    assert len(long) < 260 and "\n" not in p.describe_error(api_error(400, "X", "a\n\nb\tc"))


def test_describe_never_includes_messages_of_unrecognised_exceptions():
    p, _ = gemini(FlakyGemini())
    assert p.describe_error(RuntimeError(f"secret {FAKE_KEY} in /home/user/path")) == "RuntimeError"
    odd = RuntimeError("leaky")
    odd.message = f"leaky {FAKE_KEY}"  # has .message but no HTTP status: still type-only
    assert p.describe_error(odd) == "RuntimeError"


def test_base_provider_default_is_type_only():
    class Plain(LLMProvider):
        def chat(self, messages, tool_schemas):
            raise NotImplementedError
    assert Plain().describe_error(RuntimeError("anything")) == "RuntimeError"


# ---- agent / trace / API integration ----
@pytest.fixture
def parts(settings):
    reg = ToolRegistry(settings.data_dir, settings.database_path)
    return reg, TraceStore(settings.database_path)


def test_agent_reports_http_status_in_error_trace_and_log(parts, caplog):
    reg, tracer = parts
    provider, _ = gemini(FlakyGemini(*[api_error(503, "UNAVAILABLE", "high demand")] * 5))
    with caplog.at_level(logging.WARNING):
        r = Agent(provider, reg, tracer=tracer).run("hi", "STU001", "s")
    assert r.response == FALLBACK and r.error == "llm_error: ServerError 503 UNAVAILABLE: high demand"
    assert tracer.get(r.trace_id)["error"] == r.error and FAKE_KEY not in str(tracer.get(r.trace_id))
    assert any("LLM call failed: ServerError 503 UNAVAILABLE" in rec.getMessage() for rec in caplog.records)


def test_agent_survives_a_blip_without_the_user_noticing(parts):
    reg, tracer = parts
    provider, sleeps = gemini(FlakyGemini(api_error(503, "UNAVAILABLE")))
    r = Agent(provider, reg, tracer=tracer).run("hi", "STU001", "s")
    assert r.error is None and r.response == "hello" and sleeps == [1.0]
    assert tracer.get(r.trace_id)["status"] == "success"


def test_agent_falls_back_to_type_name_if_describe_itself_breaks(parts):
    reg, _ = parts

    class Broken(LLMProvider):
        def chat(self, messages, tool_schemas):
            raise RuntimeError("x")

        def describe_error(self, exc):
            raise ValueError("describe failed")
    r = Agent(Broken(), reg).run("hi", "STU001")
    assert r.error == "llm_error: RuntimeError" and r.response == FALLBACK


def test_404_surfaces_immediately_with_its_message(parts):
    reg, _ = parts
    msg = "This model models/x is no longer available to new users."
    provider, sleeps = gemini(FlakyGemini(api_error(404, "NOT_FOUND", msg)))
    r = Agent(provider, reg).run("hi", "STU001")
    assert r.error == f"llm_error: ClientError 404 NOT_FOUND: {msg}" and sleeps == []


def test_api_response_carries_the_detail_but_not_the_key(settings, retriever):
    provider, _ = gemini(FlakyGemini(*[api_error(503, "UNAVAILABLE", f"high demand {FAKE_KEY}")] * 5))
    c = TestClient(create_app(settings, provider=provider, retriever=retriever))
    body = c.post("/api/chat", json={"message": "hi", "student_id": "STU001"}).json()
    assert body["error"].startswith("llm_error: ServerError 503 UNAVAILABLE") and FAKE_KEY not in str(body)
    assert body["response"] == FALLBACK


# ---- retry ownership: _with_retry is the only retry layer ----
def test_openai_sdk_internal_retries_are_disabled():
    """The OpenAI SDK retries twice by default; that would stack on top of _with_retry. Constructing a client
    makes no network call."""
    from campuspilot.provider import OpenAIProvider
    provider = OpenAIProvider("sk-test-not-a-real-key-0000000000", "gpt-test")
    assert provider.client.max_retries == 0


def test_gemini_client_is_built_without_sdk_retry_options(monkeypatch):
    """google-genai (checked in the installed SDK source) does not retry unless retry_options are passed, so the
    provider must not pass any. This guards against someone adding them and doubling the retries."""
    import google.genai as genai
    from campuspilot.provider import GeminiProvider
    seen = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(genai, "Client", FakeClient)
    GeminiProvider("fake-key-not-real", "gemini-test")
    assert seen == {"api_key": "fake-key-not-real"}
