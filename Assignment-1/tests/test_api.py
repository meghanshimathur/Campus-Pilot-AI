from fastapi.testclient import TestClient

from campuspilot.api import create_app
from campuspilot.provider import ChatResponse, LLMProvider, ToolCall


class _Fake(LLMProvider):
    def __init__(self):
        self.responses = [ChatResponse(tool_calls=[ToolCall("c1", "check_fee_status", {})]),
                          ChatResponse(content="Your fees are partially paid.", input_tokens=5, output_tokens=2)]

    def chat(self, messages, tool_schemas):
        return self.responses.pop(0)


def test_chat_with_fake_provider(settings):
    c = TestClient(create_app(settings, provider=_Fake()))
    r = c.post("/api/chat", json={"message": "fee status?", "student_id": "STU002"}).json()
    assert r["response"] == "Your fees are partially paid."
    assert r["tools_called"][0]["name"] == "check_fee_status"


def test_chat_validation_and_no_provider(settings):
    c = TestClient(create_app(settings))
    assert c.post("/api/chat", json={"message": "hi", "student_id": "STU001"}).status_code == 503
    assert c.post("/api/chat", json={"message": "", "student_id": "STU001"}).status_code == 422


def test_endpoints(settings):
    c = TestClient(create_app(settings))
    assert c.get("/api/health").json() == {"status": "ok", "llm_configured": False}
    assert c.get("/api/students/STU002/fees").json()["status"] == "partial"
    assert c.get("/api/students/STU004/scholarship").json()["status"] == "disbursed"
    body = {"student_id": "STU002", "category": "fees", "description": "Portal failed"}
    assert c.post("/api/support-requests", json=body).json()["ticket_id"].startswith("TKT-")
