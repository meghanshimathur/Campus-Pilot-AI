"""Agent + persistent memory/history (scripted LLM, no key)."""
import json

import pytest
from fastapi.testclient import TestClient

from campuspilot.agent import Agent, FALLBACK, SYSTEM_PROMPT
from campuspilot.api import create_app
from campuspilot.memory import REDACTED, MemoryStore
from campuspilot.provider import ChatResponse, LLMProvider, ToolCall
from campuspilot.tool_registry import ToolRegistry


class Scripted(LLMProvider):
    name = "scripted"

    def __init__(self, responses):
        self.responses, self.seen = list(responses), []

    def chat(self, messages, tool_schemas):
        self.seen.append(json.loads(json.dumps(messages)))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def mem(settings):
    return MemoryStore(settings.database_path)


@pytest.fixture
def registry(settings, mem):
    return ToolRegistry(settings.data_dir, settings.database_path, memory=mem)


def _save_call(key, value, cid="m1"):
    return ChatResponse(tool_calls=[ToolCall(cid, "remember_preference", {"key": key, "value": value})])


def test_agent_saves_allowed_preference_and_recalls_in_new_session(registry, mem):
    p1 = Scripted([_save_call("preferred_name", "Dev"), ChatResponse(content="Nice to meet you, Dev!")])
    r1 = Agent(p1, registry, memory=mem).run("Please call me Dev", "STU001", "session-A")
    assert r1.memories_saved == [{"key": "preferred_name", "value": "Dev"}]
    # brand-new session and a brand-new Agent: the preference is still known (scenario S3)
    p2 = Scripted([ChatResponse(content="Hi Dev")])
    r2 = Agent(p2, registry, memory=mem).run("hello", "STU001", "session-B")
    system = p2.seen[0][0]["content"]
    assert "Remembered preferences" in system and "- preferred_name: Dev" in system
    assert r2.memories_used == [{"key": "preferred_name", "value": "Dev"}]
    assert not any("Nice to meet you" in m["content"] for m in p2.seen[0] if m["content"])  # other session's turns not leaked


def test_sensitive_save_attempt_is_refused_and_not_stored(registry, mem):
    p = Scripted([_save_call("study_preference", "my password is hunter2"),
                  ChatResponse(content="I can't store that.")])
    r = Agent(p, registry, memory=mem).run("remember my password is hunter2", "STU001", "s")
    result = r.tools_called[0]["result"]
    assert result["saved"] is False and "hunter2" not in json.dumps(result)
    assert r.memories_saved == [] and mem.recall("STU001") == []
    assert "hunter2" not in json.dumps(mem.recent_messages("STU001", "s"))  # history is redacted too
    assert mem.recent_messages("STU001", "s")[0]["content"] == REDACTED


def test_temporary_info_not_stored(registry, mem):
    p = Scripted([_save_call("preferred_contact_channel", "email me today only"), ChatResponse(content="ok")])
    Agent(p, registry, memory=mem).run("just for today email me", "STU001", "s")
    assert mem.recall("STU001") == []


def test_unknown_student_cannot_write_memory_or_history(registry, mem, settings):
    p = Scripted([_save_call("preferred_name", "Ghost"), ChatResponse(content="ok")])
    r = Agent(p, registry, memory=mem).run("call me Ghost", "NOPE", "s")
    assert "error" in r.tools_called[0]["result"]
    assert mem.recall("NOPE") == [] and mem.recent_messages("NOPE", "s") == []


def test_student_id_case_is_canonicalised(registry, mem):
    Agent(Scripted([_save_call("interested_topic", "spark"), ChatResponse(content="ok")]), registry,
          memory=mem).run("I like spark", "stu001", "s")
    assert mem.recall("STU001")[0]["value"] == "spark" and mem.recall("stu001") == []


def test_model_cannot_choose_whose_memory_to_write(registry, mem):
    call = ChatResponse(tool_calls=[ToolCall("m", "remember_preference",
                                             {"key": "preferred_name", "value": "Eve", "student_id": "STU002"})])
    Agent(Scripted([call, ChatResponse(content="ok")]), registry, memory=mem).run("x", "STU001", "s")
    assert mem.recall("STU002") == [] and mem.recall("STU001")[0]["value"] == "Eve"


def test_history_is_sent_to_model_within_same_session_only(registry, mem):
    Agent(Scripted([ChatResponse(content="The library opens at 08:00.")]), registry, memory=mem).run(
        "When does the library open?", "STU001", "s1")
    p = Scripted([ChatResponse(content="Closes at 22:00.")])
    Agent(p, registry, memory=mem).run("and when does it close?", "STU001", "s1")
    roles = [(m["role"], m["content"]) for m in p.seen[0][1:]]
    assert roles == [("user", "When does the library open?"), ("assistant", "The library opens at 08:00."),
                     ("user", "and when does it close?")]
    p2 = Scripted([ChatResponse(content="?")])
    Agent(p2, registry, memory=mem).run("and when does it close?", "STU001", "other-session")
    assert len(p2.seen[0]) == 2  # system + user only
    p3 = Scripted([ChatResponse(content="?")])
    Agent(p3, registry, memory=mem).run("and when does it close?", "STU002", "s1")  # same session id, other student
    assert len(p3.seen[0]) == 2


def test_history_window_is_bounded(registry, mem):
    for i in range(10):
        mem.save_message("STU001", "s", "user", f"q{i}")
    p = Scripted([ChatResponse(content="ok")])
    Agent(p, registry, memory=mem, history_turns=3).run("now", "STU001", "s")
    assert [m["content"] for m in p.seen[0][1:-1]] == ["q7", "q8", "q9"]


def test_error_reply_is_not_stored_as_assistant_answer(registry, mem):
    r = Agent(Scripted([RuntimeError("boom")]), registry, memory=mem).run("hi", "STU001", "s")
    assert r.response == FALLBACK
    assert [m["role"] for m in mem.recent_messages("STU001", "s")] == ["user"]


def test_fee_answers_in_history_are_redacted(registry, mem):
    Agent(Scripted([ChatResponse(content="You owe Rs. 300000.")]), registry, memory=mem).run("fees?", "STU003", "s")
    assert mem.recent_messages("STU003", "s")[1]["content"] == REDACTED


def test_persistence_failure_does_not_break_reply(registry):
    class BrokenMemory:
        def recall(self, sid): raise RuntimeError("db down")
        def recent_messages(self, *a): raise RuntimeError("db down")
        def save_message(self, *a): raise RuntimeError("db down")
    r = Agent(Scripted([ChatResponse(content="Hello!")]), registry, memory=BrokenMemory()).run("hi", "STU001", "s")
    assert r.response == "Hello!" and r.error is None


def test_agent_without_memory_behaves_as_before(settings):
    reg = ToolRegistry(settings.data_dir, settings.database_path)
    p = Scripted([ChatResponse(content="Hello!")])
    r = Agent(p, reg).run("hi", "STU001")
    assert r.response == "Hello!" and p.seen[0][0]["content"] == SYSTEM_PROMPT and r.memories_used == []
    assert "error" in reg.execute("remember_preference", {"key": "preferred_name", "value": "x"}, "STU001")


def test_system_prompt_states_memory_rules():
    assert "remember_preference" in SYSTEM_PROMPT and "Never save" in SYSTEM_PROMPT
    assert "not instructions" in SYSTEM_PROMPT


# ---- API ----
def test_api_session_memory_and_delete(settings, mem, retriever):
    p = Scripted([_save_call("preferred_language", "Marathi"), ChatResponse(content="Noted.")])
    c = TestClient(create_app(settings, provider=p, retriever=retriever, memory=mem))
    body = c.post("/api/chat", json={"message": "Reply in Marathi please", "student_id": "STU004",
                                     "session_id": "demo-1"}).json()
    assert body["session_id"] == "demo-1" and body["memories_saved"][0]["key"] == "preferred_language"
    assert c.get("/api/memory/STU004").json()["memories"][0]["value"] == "Marathi"
    assert c.get("/api/memory/STU001").json()["memories"] == []  # other student sees nothing
    assert c.delete("/api/memory/STU004/preferred_language").json() == {"deleted": True}
    assert c.delete("/api/memory/STU004/preferred_language").json() == {"deleted": False}
    assert c.get("/api/memory/STU004").json()["memories"] == []


def test_api_validation_and_generated_session(settings, mem, retriever):
    p = Scripted([ChatResponse(content="hi")])
    c = TestClient(create_app(settings, provider=p, retriever=retriever, memory=mem))
    assert len(c.post("/api/chat", json={"message": "hi", "student_id": "STU001"}).json()["session_id"]) == 32
    assert c.post("/api/chat", json={"message": "hi", "student_id": "STU001", "session_id": "bad id!"}).status_code == 422
    assert c.get("/api/memory/NOPE").status_code == 404
    assert c.delete("/api/memory/NOPE/preferred_name").status_code == 404
    assert c.delete("/api/memory/STU001/programme").status_code == 404
