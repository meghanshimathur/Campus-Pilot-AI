from types import SimpleNamespace as NS

from campuspilot.config import Settings
from campuspilot.provider import OpenAIProvider, build_provider


def _client(message, usage=None):
    resp = NS(choices=[NS(message=message)], usage=usage, model="fake-model")
    return NS(chat=NS(completions=NS(create=lambda **kw: resp)))


def test_parses_tool_calls_and_usage():
    tc = NS(id="c1", function=NS(name="check_fee_status", arguments="{}"))
    p = OpenAIProvider("k", "m", client=_client(NS(content=None, tool_calls=[tc]),
                                               NS(prompt_tokens=12, completion_tokens=4)))
    r = p.chat([], [])
    assert r.tool_calls[0].name == "check_fee_status" and r.tool_calls[0].arguments == {}
    assert (r.input_tokens, r.output_tokens) == (12, 4)


def test_bad_json_arguments_become_none_and_no_usage():
    tc = NS(id="c1", function=NS(name="x", arguments="{not json"))
    r = OpenAIProvider("k", "m", client=_client(NS(content="hi", tool_calls=[tc]))).chat([], [])
    assert r.tool_calls[0].arguments is None
    assert r.input_tokens is None


def test_build_provider_none_without_key(settings):
    assert build_provider(settings) is None


# ---- GeminiProvider (fake client; no network, no key) ----
import json  # noqa: E402

import pytest  # noqa: E402

from campuspilot.provider import GeminiProvider  # noqa: E402


def _gem_client(parts, usage=None):
    resp = NS(candidates=[NS(content=NS(parts=parts))], usage_metadata=usage, model_version="gem-fake")
    captured = {}

    def create(**kw):
        captured.update(kw)
        return resp
    return NS(models=NS(generate_content=create)), captured


def _part(**kw):
    base = dict(function_call=None, text=None, thought=None, thought_signature=None)
    base.update(kw)
    return NS(**base)


def test_gemini_parses_tool_call_signature_and_usage():
    fc = NS(name="check_fee_status", args={})
    client, cap = _gem_client([_part(function_call=fc, thought_signature=b"sig")],
                              NS(prompt_token_count=11, candidates_token_count=3, thoughts_token_count=2))
    r = GeminiProvider("k", "m", client=client).chat(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}],
        [{"name": "check_fee_status", "description": "d", "parameters": {"type": "object", "properties": {}}}])
    assert r.tool_calls[0].name == "check_fee_status" and r.tool_calls[0].provider_data == b"sig"
    assert (r.input_tokens, r.output_tokens) == (11, 5)  # output includes thinking tokens
    assert cap["config"].automatic_function_calling.disable is True
    assert cap["config"].system_instruction == "s"


def test_gemini_text_and_missing_usage():
    client, _ = _gem_client([_part(text="hidden", thought=True), _part(text="Hello")])
    r = GeminiProvider("k", "m", client=client).chat([{"role": "user", "content": "hi"}], [])
    assert r.content == "Hello" and r.tool_calls == []
    assert r.input_tokens is None and r.output_tokens is None


def test_gemini_empty_candidates_is_safe():
    client = NS(models=NS(generate_content=lambda **kw: NS(candidates=[], usage_metadata=None)))
    r = GeminiProvider("k", "m", client=client).chat([{"role": "user", "content": "hi"}], [])
    assert r.content is None and r.tool_calls == []


def test_gemini_message_translation():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "fees?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "check_fee_status", "arguments": "{}"},
             "provider_data": b"sig"},
            {"id": "b", "type": "function", "function": {"name": "check_scholarship_status", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": json.dumps({"status": "paid"})},
        {"role": "tool", "tool_call_id": "b", "content": json.dumps({"status": "active"})},
    ]
    system, contents = GeminiProvider.to_gemini(msgs)
    assert system == "sys"
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert contents[1].parts[0].thought_signature == b"sig"
    resp_parts = contents[2].parts  # both tool results grouped into one turn
    assert [p.function_response.name for p in resp_parts] == ["check_fee_status", "check_scholarship_status"]
    assert resp_parts[0].function_response.response == {"status": "paid"}


def test_build_provider_selects_gemini_and_openai():
    from campuspilot.config import ROOT
    base = dict(data_dir=ROOT / "data", database_path=ROOT / "x.sqlite3")
    g = build_provider(Settings(**base, llm_provider="gemini", gemini_api_key="fake-key-1"))
    assert isinstance(g, GeminiProvider) and "fake-key-1" not in repr(g.__dict__.get("model"))
    o = build_provider(Settings(**base, llm_provider="openai", openai_api_key="fake-key-2"))
    assert isinstance(o, OpenAIProvider)
    assert build_provider(Settings(**base, llm_provider="gemini")) is None  # no key


def test_agent_roundtrips_provider_data():
    from campuspilot.agent import Agent
    from campuspilot.provider import ChatResponse, LLMProvider, ToolCall
    from campuspilot.tool_registry import ToolRegistry
    from campuspilot.config import ROOT

    class P(LLMProvider):
        def __init__(self):
            self.rs = [ChatResponse(tool_calls=[ToolCall("c", "check_fee_status", {}, provider_data=b"sig")]),
                       ChatResponse(content="ok")]
            self.second_call_msgs = None

        def chat(self, messages, tool_schemas):
            if len(self.rs) == 1:
                self.second_call_msgs = list(messages)
            return self.rs.pop(0)

    p = P()
    Agent(p, ToolRegistry(ROOT / "data", ROOT / "unused.sqlite3")).run("x", "STU001")
    assistant = [m for m in p.second_call_msgs if m["role"] == "assistant"][0]
    assert assistant["tool_calls"][0]["provider_data"] == b"sig"
