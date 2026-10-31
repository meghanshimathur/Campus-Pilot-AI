"""Route classification, ownership, staff authorization and fail-closed auditing over HTTP (Assignment 2, Batch 2)."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth_helpers import STUDENTS, bearer, client_for, db_dump, db_one, seed_users
from campuspilot import api as api_module
from campuspilot.api import FRAMEWORK_PUBLIC_PATHS, ROUTE_ACCESS, _flatten_routes, create_app, route_problems
from campuspilot.audit import AuditError, AuditStore
from campuspilot.authz import Principal
from campuspilot.config import ROOT
from campuspilot.memory import MemoryStore
from campuspilot.provider import ChatResponse, LLMProvider, ToolCall

DATA = {s["student_id"]: s for s in json.loads((ROOT / "data" / "students.json").read_text())}


class Script(LLMProvider):
    name = "script"

    def __init__(self, *responses):
        self.responses = list(responses)

    def chat(self, messages, tool_schemas):
        return self.responses.pop(0)


class BrokenAudit(AuditStore):
    def record(self, *args, **kwargs):
        raise AuditError("audit store is down")


def sample_path(template: str, student="STU001") -> str:
    return (template.replace("{student_id}", student).replace("{key}", "preferred_name")
            .replace("{trace_id}", "0" * 32).replace("{name}", "evaluation_offline_20261022_000402.json"))


def request(client, method, template, student="STU001", **kw):
    body = {"message": "hi"} if template == "/api/chat" else {"category": "fees", "description": "x"} if template == "/api/support-requests" \
        else {"user_id": "STU001", "password": "x" * 12} if template == "/api/auth/login" else None
    return client.request(method, sample_path(template, student), json=body, **kw)


# ---------------------------------------------------------------- every route is classified
def test_every_route_is_explicitly_public_or_protected(settings):
    app = create_app(settings)
    assert route_problems(app) == []
    found = set()
    for route, path in _flatten_routes(app.routes):
        if hasattr(route, "methods") and hasattr(route, "dependant"):
            for method in route.methods - {"HEAD", "OPTIONS"}:
                found.add((method, path))
    assert found == set(ROUTE_ACCESS)                                 # nothing unclassified, nothing classified that is not there
    assert set(ROUTE_ACCESS.values()) == {"public", "protected"}
    public = {k for k, v in ROUTE_ACCESS.items() if v == "public"}
    assert public == {("GET", "/api/health"), ("POST", "/api/auth/login")}          # the whole public surface, spelled out


def test_the_route_check_catches_unclassified_and_unguarded_routes(settings):
    app = create_app(settings)
    app.add_api_route("/api/secret-admin", lambda: {"x": 1}, methods=["GET"])        # a new route nobody classified
    assert any("unclassified route GET /api/secret-admin" in p for p in route_problems(app))
    plain = FastAPI()
    plain.add_api_route("/api/students/{student_id}/fees", lambda student_id: {}, methods=["GET"])
    assert any("has no session check" in p for p in route_problems(plain))             # classified protected but unguarded


def test_the_app_refuses_to_start_with_an_unclassified_route(settings, monkeypatch):
    monkeypatch.delitem(api_module.ROUTE_ACCESS, ("GET", "/api/memory/{student_id}"))
    with pytest.raises(RuntimeError, match="route security check failed"):
        create_app(settings)


def test_every_protected_route_rejects_a_missing_or_bad_token_with_401(settings):
    c = TestClient(create_app(settings, provider=Script()))
    protected = [(m, p) for (m, p), v in ROUTE_ACCESS.items() if v == "protected"]
    assert len(protected) == 17
    for method, template in protected:
        for hdrs in ({}, {"Authorization": "Bearer nope-nope-nope-nope-nope-nope"}):
            r = request(c, method, template, headers=hdrs)
            assert r.status_code == 401, (method, template, r.status_code)
            assert r.headers["www-authenticate"] == "Bearer"
            assert "due" not in r.text and "memories" not in r.text


def test_the_documentation_and_static_mount_expose_no_student_data(settings):
    c = TestClient(create_app(settings))
    for path in FRAMEWORK_PUBLIC_PATHS - {"/docs/oauth2-redirect"}:
        assert c.get(path).status_code == 200
    assert "STU001" not in c.get("/openapi.json").text and "300000" not in c.get("/openapi.json").text
    assert c.get("/api/does-not-exist").status_code == 404


# ---------------------------------------------------------------- students reach their own data and nobody else's
@pytest.mark.parametrize("owner", STUDENTS)
def test_a_student_reads_their_own_private_records(settings, owner):
    c = client_for(settings, owner)
    fee = c.get(f"/api/students/{owner}/fees")
    assert fee.status_code == 200 and fee.json()["student_id"] == owner and fee.json()["due"] == DATA[owner]["fees"]["due"]
    assert c.get(f"/api/students/{owner.lower()}/scholarship").status_code == 200       # case does not matter
    assert c.get(f"/api/memory/{owner}").status_code == 200


@pytest.mark.parametrize("owner,victim", [(a, b) for a in STUDENTS for b in STUDENTS if a != b])
def test_a_student_is_refused_every_other_students_records_on_every_route(settings, owner, victim):
    c = client_for(settings, owner, provider=Script(*[ChatResponse(content="hi")] * 3))
    checks = [
        c.get(f"/api/students/{victim}/fees"), c.get(f"/api/students/{victim}/scholarship"),
        c.get(f"/api/memory/{victim}"), c.delete(f"/api/memory/{victim}/preferred_name"),
        c.post("/api/support-requests", json={"student_id": victim, "category": "fees", "description": "x"}),
        c.post("/api/chat", json={"message": "hi", "student_id": victim}),
    ]
    for r in checks:
        assert r.status_code == 403 and r.json() == {"detail": "Access denied."}, r.url
        assert "450000" not in r.text and DATA[victim]["name"] not in r.text          # no part of the record leaks
    assert db_one(settings.database_path, "SELECT COUNT(*) FROM support_requests")[0] == 0


def test_a_refused_request_is_indistinguishable_for_real_and_made_up_students(settings):
    c = client_for(settings, "STU001")
    real, fake = c.get("/api/students/STU002/fees"), c.get("/api/students/NOPE99/fees")
    assert (real.status_code, real.json()) == (fake.status_code, fake.json()) == (403, {"detail": "Access denied."})


def test_refusals_are_audited_with_who_what_and_the_reason(settings):
    c = client_for(settings, "STU001")
    c.get("/api/students/STU002/fees")
    c.delete("/api/memory/STU003/preferred_name")
    denied = AuditStore(settings.database_path).list_events(event="authz.denied")
    assert [(e["actor_id"], e["action"], e["resource_student_id"], e["decision"], e["outcome"], e["details"]["reason"])
            for e in denied] == [("STU001", "check_fee_status", "STU002", "denied", "blocked", "cross_student_access"),
                                 ("STU001", "delete_own_memory", "STU003", "denied", "blocked", "cross_student_access")]


def test_the_support_request_is_created_for_the_signed_in_student_not_the_body(settings):
    c = client_for(settings, "STU002")
    r = c.post("/api/support-requests", json={"category": "fees", "description": "Portal failed"})        # no id at all
    assert r.status_code == 200
    assert db_one(settings.database_path, "SELECT student_id FROM support_requests WHERE ticket_id=?", r.json()["ticket_id"]) == ("STU002",)


def test_the_chat_agent_receives_the_verified_principal_never_the_body_id(settings, monkeypatch):
    seen = {}
    real_run = api_module.Agent.run

    def spy(self, message, who, session_id=None):
        seen["who"] = who
        return real_run(self, message, who, session_id)

    monkeypatch.setattr(api_module.Agent, "run", spy)
    c = client_for(settings, "STU003", provider=Script(ChatResponse(content="ok")))
    assert c.post("/api/chat", json={"message": "hi", "student_id": " stu003 "}).status_code == 200
    assert isinstance(seen["who"], Principal) and seen["who"].user_id == "STU003"
    assert c.post("/api/chat", json={"message": "hi"}).status_code == 200                         # the id is optional


# ---------------------------------------------------------------- traces belong to their owner
def test_a_trace_is_visible_to_its_owner_only_and_others_get_the_same_404_as_a_missing_trace(settings):
    owner = client_for(settings, "STU002", provider=Script(ChatResponse(tool_calls=[ToolCall("c1", "check_fee_status", {})]),
                                                             ChatResponse(content="ok")))
    trace_id = owner.post("/api/chat", json={"message": "fees?"}).json()["trace_id"]
    assert owner.get(f"/api/traces/{trace_id}").status_code == 200
    seed_users(settings)
    for who in ("STU001", "STAFF01"):
        other = TestClient(create_app(settings))
        other.headers.update(bearer(settings, who))
        hidden, missing = other.get(f"/api/traces/{trace_id}"), other.get("/api/traces/" + "0" * 32)
        assert hidden.status_code == missing.status_code == 404 and hidden.json() == missing.json()
    denied = [e for e in AuditStore(settings.database_path).list_events(event="authz.denied") if e["action"] == "view_own_trace"]
    assert {e["actor_id"] for e in denied} == {"STU001", "STAFF01"}


# ---------------------------------------------------------------- staff follow the policy
def test_staff_cannot_read_or_change_students_private_data(settings):
    staff = client_for(settings, "STAFF01", provider=Script())
    for r in (staff.get("/api/students/STU001/fees"), staff.get("/api/students/STU001/scholarship"),
              staff.get("/api/memory/STU001"), staff.delete("/api/memory/STU001/preferred_name"),
              staff.post("/api/support-requests", json={"student_id": "STU001", "category": "fees", "description": "x"}),
              staff.post("/api/support-requests", json={"category": "fees", "description": "x"}),
              staff.post("/api/chat", json={"message": "hi", "student_id": "STU001"})):
        assert r.status_code == 403
    assert staff.get("/api/auth/me").json()["role"] == "staff"


def test_staff_may_use_public_information_and_the_evaluation_reports(settings):
    staff = client_for(settings, "STAFF01", provider=Script(ChatResponse(content="hello")))
    assert staff.post("/api/chat", json={"message": "hi"}).status_code == 200
    assert staff.get("/api/evaluation/reports").status_code == 200


def test_a_staff_chat_cannot_reach_private_tools_through_the_model(settings):
    staff = client_for(settings, "STAFF01", provider=Script(ChatResponse(tool_calls=[ToolCall("c1", "check_fee_status", {"student_id": "STU001"})]),
                                                            ChatResponse(content="I could not.")))
    body = staff.post("/api/chat", json={"message": "fees of STU001"}).json()
    assert body["tools_called"][0]["result"]["denied"] is True and "450000" not in json.dumps(body) and DATA["STU001"]["name"] not in json.dumps(body)


# ---------------------------------------------------------------- private actions are audited, and fail closed
def test_private_reads_are_audited_before_they_are_returned(settings):
    c = client_for(settings, "STU001")
    c.get("/api/students/STU001/fees")
    c.get("/api/students/STU001/scholarship")
    c.get("/api/memory/STU001")
    events = AuditStore(settings.database_path).list_events(actor_id="STU001")
    assert [(e["event"], e["action"], e["decision"], e["outcome"]) for e in events] == [
        ("tool.private_read", "check_fee_status", "allowed", "success"),
        ("tool.private_read", "check_scholarship_status", "allowed", "success"),
        ("memory.read", "read_own_memory", "allowed", "success")]


def test_if_the_audit_log_is_down_private_data_is_not_returned_and_nothing_is_changed(settings):
    seed_users(settings)
    mem = MemoryStore(settings.database_path)
    mem.remember("STU001", "preferred_name", "Dev")
    c = TestClient(create_app(settings, provider=Script(), memory=mem, audit=BrokenAudit(settings.database_path)))
    c.headers.update(bearer(settings, "STU001"))            # sessions live in auth_sessions, not in the audit log
    for r in (c.get("/api/students/STU001/fees"), c.get("/api/students/STU001/scholarship"), c.get("/api/memory/STU001"),
              c.delete("/api/memory/STU001/preferred_name"),
              c.post("/api/support-requests", json={"category": "fees", "description": "x"})):
        assert r.status_code == 503 and "audit log is unavailable" in r.json()["detail"]
        assert "450000" not in r.text and DATA["STU001"]["name"] not in r.text and "Dev" not in r.text
    assert db_one(settings.database_path, "SELECT COUNT(*) FROM support_requests")[0] == 0     # no ticket was created
    assert mem.recall("STU001")[0]["value"] == "Dev"                                          # nothing was deleted


def test_memory_deletion_leaves_a_before_and_after_audit_trail(settings):
    mem = MemoryStore(settings.database_path)
    mem.remember("STU001", "preferred_name", "Dev")
    c = client_for(settings, "STU001", memory=mem)
    assert c.delete("/api/memory/STU001/preferred_name").json() == {"deleted": True}
    events = [e for e in AuditStore(settings.database_path).list_events(event="memory.delete")]
    assert [(e["decision"], e["outcome"]) for e in events] == [("allowed", "pending"), ("allowed", "success")]
    assert "Dev" not in json.dumps(events)                                                    # the value is never audited


def test_validation_errors_never_echo_what_was_sent(settings):
    c = client_for(settings, "STU001", provider=Script())
    r = c.post("/api/chat", json={"message": "x" * 5000, "student_id": "STU001"})
    assert r.status_code == 422 and "xxxx" not in r.text


def test_ids_typed_into_urls_never_reach_the_audit_log_as_text(settings):
    """Regression: the audit log used to store a URL id verbatim, so 'otp is 4821' in a path was persisted."""
    c = client_for(settings, "STU001")
    for path in ("/api/students/otp%20is%2090817265/fees", "/api/memory/pwd%3Dhunter2xyz",
                 "/api/students/AIzaSyA1234567890abcdefghij/scholarship"):
        assert c.get(path).status_code == 403
    dump = db_dump(settings.database_path)
    for secret in ("90817265", "hunter2xyz", "AIzaSyA1234567890abcdefghij"):       # none can occur in a hex hash by chance
        assert secret not in dump
    assert {e["resource_student_id"] for e in AuditStore(settings.database_path).list_events(event="authz.denied")} \
        <= {"[invalid id]", "otp is [REDACTED:otp]", "pwd=[REDACTED:password]"} | {"[REDACTED:api_key]"}
