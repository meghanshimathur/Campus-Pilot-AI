"""Pacing, 429/quota backoff, server retry hints and error classification (fake clients and clocks: no network, no sleeping)."""
import email.utils
import random
import time
from types import SimpleNamespace as NS

import httpx
import pytest
from google.genai import errors

from campuspilot.agent import Agent
from campuspilot.config import Settings, ROOT
from campuspilot.provider import (PROVIDER_BLOCKED_KINDS, GeminiProvider, LLMProvider, OpenAIProvider, build_provider,
                                  error_kind, is_quota_error, retry_after_seconds)
from campuspilot.tool_registry import ToolRegistry

KEY = "AQ.PacingTestKeyPacingTestKeyPacingTestKey1234"
MSG = [{"role": "user", "content": "hi"}]


def api_error(code, status, message="boom", *, header=None, retry_info=None):
    body = {"error": {"code": code, "status": status, "message": message}}
    if retry_info is not None:
        body["error"]["details"] = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_info}]
    response = httpx.Response(code, headers={"retry-after": header} if header is not None else {}, json=body)
    return (errors.ServerError if code >= 500 else errors.ClientError)(code, body, response)


def quota(**kw):
    return api_error(429, "RESOURCE_EXHAUSTED", **kw)


OK = NS(candidates=[NS(content=NS(parts=[NS(function_call=None, text="hello", thought=None, thought_signature=None)]))],
        usage_metadata=NS(prompt_token_count=5, candidates_token_count=2, thoughts_token_count=None), model_version="m")


class Clock:
    """Virtual time: sleeping advances it, and every model request takes `call_seconds`."""
    def __init__(self, call_seconds=0.0):
        self.t, self.sleeps, self.starts, self.call_seconds = 0.0, [], [], call_seconds

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 6))
        self.t += seconds


class Flaky:
    def __init__(self, clock, *failures):
        self.clock, self.failures, self.calls = clock, list(failures), 0
        self.models = NS(generate_content=self._generate)

    def _generate(self, **kw):
        self.calls += 1
        self.clock.starts.append(round(self.clock.t, 6))
        self.clock.t += self.clock.call_seconds
        if self.failures:
            raise self.failures.pop(0)
        return OK


def provider(*failures, call_seconds=0.0, **policy):
    clock = Clock(call_seconds)
    client = Flaky(clock, *failures)
    return GeminiProvider(KEY, "m", client=client, sleep=clock.sleep, clock=clock.now, **policy), client, clock


# ------------------------------------------------------------------ server retry hints
def test_hint_from_retry_after_header_seconds_and_http_date():
    assert retry_after_seconds(quota(header="37")) == 37.0
    in_30s = email.utils.formatdate(time.time() + 30, usegmt=True)
    assert 27 <= retry_after_seconds(quota(header=in_30s)) <= 31
    assert retry_after_seconds(quota(header=email.utils.formatdate(time.time() - 500, usegmt=True))) == 0.0


@pytest.mark.parametrize("retry_info,expected", [("34s", 34.0), ("1.5s", 1.5), ("34", 34.0), ("0s", 0.0)])
def test_hint_from_google_retry_info_details(retry_info, expected):
    assert retry_after_seconds(quota(retry_info=retry_info)) == expected


def test_hint_from_protobuf_style_duration_and_message_text_and_snake_case():
    e = errors.ClientError(429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "x",
                                           "details": [{"retry_delay": {"seconds": 7, "nanos": 500000000}}]}})
    assert retry_after_seconds(e) == 7.5
    assert retry_after_seconds(quota(message="You exceeded your quota. Please retry in 34.5s.")) == 34.5
    assert retry_after_seconds(quota(message="please RETRY IN 12 s")) == 12.0


def test_longest_hint_wins_and_garbage_is_ignored():
    assert retry_after_seconds(quota(header="5", retry_info="20s", message="Please retry in 12s.")) == 20.0
    for bad in ("soon", "-5", "nan", "inf", ""):
        assert retry_after_seconds(quota(header=bad)) is None, bad
    assert retry_after_seconds(quota()) is None
    assert retry_after_seconds(RuntimeError("nothing")) is None
    assert retry_after_seconds(None) is None
    assert retry_after_seconds(NS(response=NS(headers={"retry-after": "9"}), details=None, message=None)) == 9.0   # OpenAI-style


# ------------------------------------------------------------------ 429 backoff
def test_quota_errors_back_off_exponentially_then_succeed():
    p, client, clock = provider(quota(), quota(), quota(), quota_delays=(10, 20, 40), max_wait=90)
    assert p.chat(MSG, []).content == "hello"
    assert clock.sleeps == [10.0, 20.0, 40.0] and client.calls == 4


def test_quota_retries_are_bounded_no_endless_retrying():
    p, client, clock = provider(*[quota()] * 20, quota_delays=(10, 20, 40), max_wait=90)
    with pytest.raises(errors.ClientError):
        p.chat(MSG, [])
    assert client.calls == 4 and clock.sleeps == [10.0, 20.0, 40.0]      # 1 attempt + 3 retries, then it gives up


def test_server_hint_is_respected_never_waiting_less_than_asked():
    p, _, clock = provider(quota(retry_info="25s"), quota_delays=(10, 20), max_wait=90)
    p.chat(MSG, [])
    assert clock.sleeps == [25.0]                                           # asked 25 > our 10
    p2, _, clock2 = provider(quota(retry_info="3s"), quota_delays=(10, 20), max_wait=90)
    p2.chat(MSG, [])
    assert clock2.sleeps == [10.0]                                          # our backoff is longer than the hint


def test_hint_longer_than_max_wait_gives_up_immediately():
    """A daily quota says 'retry in hours': waiting that out inside a run is pointless."""
    p, client, clock = provider(quota(retry_info="7200s"), quota_delays=(10, 20, 40), max_wait=90)
    with pytest.raises(errors.ClientError):
        p.chat(MSG, [])
    assert client.calls == 1 and clock.sleeps == []


def test_single_wait_is_capped_at_max_wait():
    p, _, clock = provider(quota(), quota_delays=(500,), max_wait=90)
    p.chat(MSG, [])
    assert clock.sleeps == [90.0]


def test_jitter_adds_a_bounded_random_extra(monkeypatch):
    monkeypatch.setattr(random, "uniform", lambda a, b: b)                  # worst case: full jitter
    p, _, clock = provider(quota(), quota_delays=(10,), max_wait=90, jitter=1.5)
    p.chat(MSG, [])
    assert clock.sleeps == [11.5]
    monkeypatch.undo()
    p2, _, clock2 = provider(*[quota()] * 5 + [], quota_delays=(10,) * 5, max_wait=90, jitter=1.0)
    p2.chat(MSG, [])
    assert all(10.0 <= s <= 11.0 for s in clock2.sleeps)


def test_quota_and_transient_failures_use_separate_schedules_and_counts():
    p, client, clock = provider(quota(), api_error(503, "UNAVAILABLE"), quota(), retry_delays=(2, 4), quota_delays=(10, 20), max_wait=90)
    p.chat(MSG, [])
    assert clock.sleeps == [10.0, 2.0, 20.0] and client.calls == 4


def test_non_retryable_errors_are_never_retried():
    for code, status in [(400, "INVALID_ARGUMENT"), (401, "UNAUTHENTICATED"), (403, "PERMISSION_DENIED"), (404, "NOT_FOUND")]:
        p, client, clock = provider(api_error(code, status), quota_delays=(10,), max_wait=90)
        with pytest.raises(errors.ClientError):
            p.chat(MSG, [])
        assert client.calls == 1 and clock.sleeps == []


def test_old_style_retry_delays_argument_still_means_one_schedule_for_both():
    p, client, clock = provider(quota(), api_error(503, "UNAVAILABLE"), retry_delays=(0.25,))
    p.chat(MSG, [])
    assert clock.sleeps == [0.25, 0.25] and client.calls == 3
    p2, client2, _ = provider(quota(), retry_delays=())
    with pytest.raises(errors.ClientError):
        p2.chat(MSG, [])
    assert client2.calls == 1                                              # () switches retries off for 429 too


# ------------------------------------------------------------------ pacing
def test_pacing_spaces_consecutive_requests_by_min_interval():
    p, client, clock = provider(call_seconds=1.0, min_interval=6.0)
    for _ in range(4):
        p.chat(MSG, [])
    assert clock.starts == [0.0, 6.0, 12.0, 18.0]                           # exactly one request start every 6 s
    assert clock.sleeps == [5.0, 5.0, 5.0]                                  # the 1 s a call took counts towards the gap


def test_pacing_also_applies_to_retries():
    p, client, clock = provider(quota(), call_seconds=0.0, min_interval=6.0, quota_delays=(1,), max_wait=90)
    p.chat(MSG, [])
    assert clock.starts == [0.0, 6.0] and clock.sleeps == [1.0, 5.0]        # backoff 1 s, then paced up to the 6 s gap


def test_no_pacing_by_default_and_when_zero():
    p, _, clock = provider(call_seconds=1.0)
    for _ in range(3):
        p.chat(MSG, [])
    assert clock.sleeps == [] and LLMProvider.min_interval == 0.0
    p0, _, clock0 = provider(call_seconds=1.0, min_interval=0)
    p0.chat(MSG, []); p0.chat(MSG, [])
    assert clock0.sleeps == []


def test_pacing_works_for_the_openai_provider_too():
    clock = Clock(1.0)

    def create(**kw):
        clock.starts.append(clock.t); clock.t += 1.0
        return NS(choices=[NS(message=NS(content="ok", tool_calls=None))], usage=None, model="gpt")
    p = OpenAIProvider("sk-fake", "gpt", client=NS(chat=NS(completions=NS(create=create))), min_interval=4.0,
                       sleep=clock.sleep, clock=clock.now)
    p.chat(MSG, []); p.chat(MSG, [])
    assert clock.starts == [0.0, 4.0]


def test_interactive_defaults_are_unchanged_short_and_unpaced():
    p = GeminiProvider(KEY, "m", client=NS())
    assert (p.retry_delays, p.quota_delays, p.max_wait, p.jitter, p.min_interval) == ((1.0, 2.0), (1.0, 2.0), 10.0, 0.0, 0.0)


def test_build_provider_passes_the_policy_through(monkeypatch):
    s = Settings(data_dir=ROOT / "data", database_path=ROOT / "x.sqlite3", llm_provider="gemini", gemini_api_key=KEY)
    monkeypatch.setattr("google.genai.Client", lambda **kw: NS())             # never builds a real client
    p = build_provider(s, min_interval=6, quota_delays=(10, 20, 40), max_wait=90, jitter=1.0)
    assert isinstance(p, GeminiProvider) and (p.min_interval, p.quota_delays, p.max_wait, p.jitter) == (6.0, (10, 20, 40), 90.0, 1.0)
    assert build_provider(Settings(data_dir=ROOT / "data", database_path=ROOT / "x", llm_provider=None), min_interval=6) is None


# ------------------------------------------------------------------ classification
@pytest.mark.parametrize("code,status,kind", [
    (429, "RESOURCE_EXHAUSTED", "quota"), (503, "UNAVAILABLE", "unavailable"), (500, "INTERNAL", "unavailable"),
    (504, "DEADLINE_EXCEEDED", "unavailable"), (408, "TIMEOUT", "unavailable"),
    (401, "UNAUTHENTICATED", "config"), (403, "PERMISSION_DENIED", "config"), (404, "NOT_FOUND", "config"),
    (400, "INVALID_ARGUMENT", "request"), (409, "ABORTED", "request"), (422, "X", "request"),
])
def test_error_kind_table(code, status, kind):
    assert error_kind(api_error(code, status)) == kind
    assert (kind in PROVIDER_BLOCKED_KINDS) == (kind in ("quota", "unavailable", "config"))


def test_error_kind_for_non_api_errors():
    assert error_kind(RuntimeError("x")) == "unknown" and error_kind(ValueError("x")) == "unknown"
    assert error_kind(type("ConnectError", (Exception,), {})()) == "unavailable"
    assert error_kind(type("ReadTimeout", (Exception,), {})()) == "unavailable"
    assert is_quota_error(NS(status_code=429)) and not is_quota_error(NS(status_code=503))
    assert "request" not in PROVIDER_BLOCKED_KINDS and "unknown" not in PROVIDER_BLOCKED_KINDS   # never excuse our own bugs


@pytest.mark.parametrize("failure,kind", [(quota(), "quota"), (api_error(503, "UNAVAILABLE"), "unavailable"),
                                          (api_error(404, "NOT_FOUND"), "config"), (api_error(400, "INVALID_ARGUMENT"), "request"),
                                          (RuntimeError("x"), "unknown")])
def test_agent_records_the_error_kind(settings, failure, kind):
    p, _, _ = provider(*[failure] * 10, retry_delays=(), quota_delays=())
    r = Agent(p, ToolRegistry(settings.data_dir, settings.database_path)).run("hi", "STU001")
    assert r.error and r.error.startswith("llm_error") and r.error_kind == kind


def test_agent_error_kind_is_empty_on_success_and_for_non_provider_failures(settings):
    reg = ToolRegistry(settings.data_dir, settings.database_path)
    p, _, _ = provider()
    assert Agent(p, reg).run("hi", "STU001").error_kind is None

    class Looping(LLMProvider):
        def chat(self, messages, tool_schemas):
            from campuspilot.provider import ChatResponse, ToolCall
            return ChatResponse(tool_calls=[ToolCall("c", "check_fee_status", {})])
    r = Agent(Looping(), reg, max_steps=2).run("hi", "STU001")
    assert r.error == "max_steps_exceeded" and r.error_kind is None            # an agent-behaviour failure, not the provider's
