"""update_contact_details end to end (Assignment 2, Batch 4): the tool, the real executor, the HTTP routes, the agent and
privacy. Scripted model, temporary database, fictional students and contact values; the real student data is never touched."""
import json
import sqlite3
import types
from contextlib import closing

import pytest

from auth_helpers import STAFF_ID, bearer, client_for, db_dump, db_one, principal, seed_users
from campuspilot.agent import Agent
from campuspilot.approvals import ApprovalService, load_or_create_key
from campuspilot.audit import AuditStore
from campuspilot.config import ROOT
from campuspilot.memory import MemoryStore
from campuspilot.provider import ChatResponse, LLMProvider, ToolCall
from campuspilot.sensitive_actions import ContactStore, mask_value, validate_contact_update
from campuspilot.tool_registry import PENDING_MESSAGE, ToolRegistry
from campuspilot.tracing import TraceStore

EMAIL, PHONE = "dev.new@example.edu", "+91 91234 56789"


class Script(LLMProvider):
    name = "script"

    def __init__(self, *responses):
        self.responses = list(responses)

    def chat(self, messages, tool_schemas):
        return self.responses.pop(0)


def tool_call(args, cid="c1"):
    return ChatResponse(tool_calls=[ToolCall(cid, "update_contact_details", args)])


@pytest.fixture
def stack(settings):
    auth = seed_users(settings)
    audit = AuditStore(settings.database_path)
    approvals = ApprovalService(settings.database_path, auth=auth, audit=audit, signing_key=b"t" * 32)
    registry = ToolRegistry(settings.data_dir, settings.database_path, audit=audit, approvals=approvals)
    return registry, approvals, audit, settings


def contacts(settings):
    return db_one(settings.database_path, "SELECT COUNT(*) FROM student_contacts")[0]


# ------------------------------------------------------------------ validation
@pytest.mark.parametrize("args,expected", [
    ({"field": "email", "value": "  Dev.New@Example.EDU "}, {"field": "email", "value": "dev.new@example.edu"}),
    ({"field": "phone", "value": "+91 (912) 345-6789"}, {"field": "phone", "value": "+919123456789"}),
    ({"field": "phone", "value": "09123456789"}, {"field": "phone", "value": "09123456789"}),
])
def test_valid_values_are_normalised(args, expected):
    assert validate_contact_update(args) == expected


@pytest.mark.parametrize("args", [{"field": "email", "value": "a@b"}, {"field": "email", "value": "a..b@example.com"},
                                  {"field": "email", "value": "x" * 130 + "@example.com"}, {"field": "phone", "value": "abc"},
                                  {"field": "phone", "value": "1234567890123456789"}, {"field": "name", "value": "Bob"},
                                  {"field": "email", "value": 5}, {"value": "a@b.co"}, [], "x"])
def test_invalid_values_are_refused(args):
    with pytest.raises(ValueError) as err:
        validate_contact_update(args)
    assert "x" * 50 not in str(err.value)


def test_masking():
    assert mask_value("email", EMAIL) == "d***@example.edu"
    assert mask_value("phone", PHONE) == "********6789" and mask_value("phone", "abc") == "***" and mask_value("email", "") == ""


# ------------------------------------------------------------------ the tool
def test_the_tool_creates_a_pending_request_and_changes_nothing(stack):
    registry, approvals, _, settings = stack
    result = registry.execute("update_contact_details", {"field": "email", "value": EMAIL}, principal(settings, "STU001"))
    assert result["status"] == "pending_approval" and result["message"] == PENDING_MESSAGE and result["approval_id"].startswith("APR-")
    assert EMAIL not in json.dumps(result) and contacts(settings) == 0
    assert db_one(settings.database_path, "SELECT status FROM approvals")[0] == "pending"


def test_the_model_cannot_aim_the_change_at_another_student(stack):
    registry, _, _, settings = stack
    registry.execute("update_contact_details", {"field": "email", "value": EMAIL, "student_id": "STU002"}, principal(settings, "STU001"))
    assert db_one(settings.database_path, "SELECT student_id, requested_by FROM approvals") == ("STU001", "STU001")


def test_staff_cannot_use_the_tool(stack):
    registry, _, _, settings = stack
    result = registry.execute("update_contact_details", {"field": "email", "value": EMAIL}, principal(settings, STAFF_ID))
    assert result.get("denied") and db_one(settings.database_path, "SELECT COUNT(*) FROM approvals")[0] == 0


def test_without_an_approval_service_the_tool_is_unavailable(settings):
    seed_users(settings)
    registry = ToolRegistry(settings.data_dir, settings.database_path)
    assert "error" in registry.execute("update_contact_details", {"field": "email", "value": EMAIL}, principal(settings, "STU001"))


def test_a_bad_value_gives_an_error_without_echoing_it(stack):
    registry, _, _, settings = stack
    result = registry.execute("update_contact_details", {"field": "email", "value": "bad-value-123"}, principal(settings, "STU001"))
    assert "error" in result and "bad-value-123" not in json.dumps(result)


def test_the_tool_fails_closed_when_the_audit_log_is_down(stack):
    registry, approvals, _, settings = stack
    from test_approvals import FlakyAudit
    approvals.audit = registry.audit = FlakyAudit(settings.database_path, fail_events={"approval.requested", "tool.write"})
    result = registry.execute("update_contact_details", {"field": "email", "value": EMAIL}, principal(settings, "STU001"))
    assert result.get("audit_unavailable") and db_one(settings.database_path, "SELECT COUNT(*) FROM approvals")[0] == 0


# ------------------------------------------------------------------ the real executor, through HTTP
def request_via_service(_client, settings, sid="STU001", field="email", value=EMAIL):
    reg = ToolRegistry(settings.data_dir, settings.database_path, approvals=_service(settings))
    return reg.execute("update_contact_details", {"field": field, "value": value}, principal(settings, sid))["approval_id"]


def _service(settings):
    return ApprovalService(settings.database_path, auth=seed_users(settings), audit=AuditStore(settings.database_path),
                           signing_key=load_or_create_key(settings.database_path.parent / ".approval_key"))


def test_end_to_end_request_approve_and_read(settings):
    student, staff = client_for(settings, "STU001"), client_for(settings, STAFF_ID)
    approval_id = request_via_service(student, settings)
    assert student.get("/api/students/STU001/contact").json()["email"] is None            # nothing changed yet
    queue = staff.get("/api/approvals").json()["approvals"]
    assert [q["approval_id"] for q in queue] == [approval_id] and EMAIL not in json.dumps(queue)
    done = staff.post(f"/api/approvals/{approval_id}/approve", json={"reason": "checked"}).json()
    assert done["executed"] is True and done["approval"]["status"] == "executed" and EMAIL not in json.dumps(done)
    mine = student.get("/api/students/STU001/contact").json()
    assert mine["email"] == EMAIL and mine["phone"] is None
    assert student.get(f"/api/approvals/{approval_id}").json()["status"] == "executed"
    assert staff.post(f"/api/approvals/{approval_id}/approve").status_code == 409          # no second execution
    assert db_one(settings.database_path, "SELECT COUNT(*) FROM student_contacts")[0] == 1


def test_a_second_change_updates_the_same_row_and_keeps_the_other_field(settings):
    student, staff = client_for(settings, "STU001"), client_for(settings, STAFF_ID)
    for field, value in (("email", EMAIL), ("phone", PHONE), ("email", "later@example.edu")):
        aid = request_via_service(student, settings, field=field, value=value)
        assert staff.post(f"/api/approvals/{aid}/approve").status_code == 200
    mine = student.get("/api/students/STU001/contact").json()
    assert mine["email"] == "later@example.edu" and mine["phone"] == "+919123456789"
    assert contacts(settings) == 1


def test_rejection_over_http_changes_nothing(settings):
    student, staff = client_for(settings, "STU001"), client_for(settings, STAFF_ID)
    aid = request_via_service(student, settings)
    assert staff.post(f"/api/approvals/{aid}/reject", json={"reason": "no"}).json()["approval"]["status"] == "rejected"
    assert contacts(settings) == 0 and staff.post(f"/api/approvals/{aid}/approve").status_code == 409


def test_students_cannot_decide_over_http_and_other_students_cannot_look(settings):
    student, other = client_for(settings, "STU001"), client_for(settings, "STU002")
    aid = request_via_service(student, settings)
    for path in ("approve", "reject"):
        assert student.post(f"/api/approvals/{aid}/{path}").status_code == 403
        assert other.post(f"/api/approvals/{aid}/{path}").status_code == 403
    assert other.get(f"/api/approvals/{aid}").status_code == 404
    assert other.post(f"/api/approvals/{aid}/cancel").status_code == 404
    assert other.get("/api/approvals").json() == {"approvals": []}
    assert contacts(settings) == 0 and db_one(settings.database_path, "SELECT status FROM approvals")[0] == "pending"


def test_unauthenticated_callers_get_401_on_every_approval_route(settings):
    from fastapi.testclient import TestClient
    from campuspilot.api import create_app
    c = TestClient(create_app(settings))
    aid = "APR-000000000000"
    for method, path in (("get", "/api/approvals"), ("get", f"/api/approvals/{aid}"), ("post", f"/api/approvals/{aid}/approve"),
                         ("post", f"/api/approvals/{aid}/reject"), ("post", f"/api/approvals/{aid}/cancel"),
                         ("get", "/api/students/STU001/contact")):
        assert getattr(c, method)(path).status_code == 401


def test_the_requester_can_cancel_over_http(settings):
    student, staff = client_for(settings, "STU001"), client_for(settings, STAFF_ID)
    aid = request_via_service(student, settings)
    assert student.post(f"/api/approvals/{aid}/cancel").json()["approval"]["status"] == "cancelled"
    assert staff.post(f"/api/approvals/{aid}/approve").status_code == 409 and contacts(settings) == 0


def test_staff_cannot_read_a_students_contact_details(settings):
    student, staff = client_for(settings, "STU001"), client_for(settings, STAFF_ID)
    aid = request_via_service(student, settings)
    staff.post(f"/api/approvals/{aid}/approve")
    assert staff.get("/api/students/STU001/contact").status_code == 403
    assert client_for(settings, "STU002").get("/api/students/STU001/contact").status_code == 403
    assert client_for(settings, "STU002").get("/api/students/STU002/contact").json()["email"] is None


def test_the_audit_log_records_the_whole_story_without_the_value(settings):
    student, staff = client_for(settings, "STU001"), client_for(settings, STAFF_ID)
    aid = request_via_service(student, settings, field="phone", value=PHONE)
    staff.post(f"/api/approvals/{aid}/approve")
    audit = AuditStore(settings.database_path)
    story = [(e["event"], e["actor_id"], e["decision"], e["outcome"]) for e in audit.list_events(limit=200) if e["approval_id"] == aid]
    assert ("approval.requested", "STU001", "approval_required", "pending") in story
    assert ("approval.decision", STAFF_ID, "approved", "pending") in story
    assert ("approval.execute", STAFF_ID, "approved", "success") in story
    assert audit.verify_chain().ok
    audit_text = json.dumps(audit.list_events(limit=500))
    assert "91234" not in audit_text and "56789" not in audit_text and "6789" not in audit_text     # no digit of it, not even the tail


def test_a_denied_decision_is_audited(settings):
    student = client_for(settings, "STU001")
    aid = request_via_service(student, settings)
    student.post(f"/api/approvals/{aid}/approve")
    denied = [e for e in AuditStore(settings.database_path).list_events(limit=200)
              if e["event"] == "authz.denied" and e["action"] == "decide_approval"]
    assert denied and denied[0]["actor_id"] == "STU001" and denied[0]["outcome"] == "blocked"


def test_an_audit_outage_stops_the_http_approval(settings):
    from test_approvals import FlakyAudit
    from fastapi.testclient import TestClient
    from campuspilot.api import create_app
    student = client_for(settings, "STU001")
    aid = request_via_service(student, settings)
    flaky = FlakyAudit(settings.database_path, fail_events={"approval.decision"})
    svc = ApprovalService(settings.database_path, auth=seed_users(settings), audit=flaky,
                          signing_key=load_or_create_key(settings.database_path.parent / ".approval_key"))
    c = TestClient(create_app(settings, approvals=svc, audit=flaky))
    c.headers.update(bearer(settings, STAFF_ID))
    assert c.post(f"/api/approvals/{aid}/approve").status_code == 503 and contacts(settings) == 0


# ------------------------------------------------------------------ the agent
def test_the_agent_reports_pending_and_the_value_stays_out_of_the_trace(settings):
    seed_users(settings)
    approvals = _service(settings)
    audit, tracer = AuditStore(settings.database_path), TraceStore(settings.database_path)
    registry = ToolRegistry(settings.data_dir, settings.database_path, audit=audit, approvals=approvals)
    agent = Agent(Script(tool_call({"field": "email", "value": EMAIL}), ChatResponse(content="Sent for staff approval.")),
                  registry, memory=MemoryStore(settings.database_path), tracer=tracer)
    r = agent.run(f"Please change my email to {EMAIL}", principal(settings, "STU001"), "s1")
    trace = tracer.get(r.trace_id)
    assert trace["tools_called"][0]["arguments"] == {"field": "email", "value": "[omitted]"}
    assert trace["tools_called"][0]["summary"]["status"] == "pending_approval"
    assert contacts(settings) == 0
    assert db_one(settings.database_path, "SELECT trace_id FROM approvals")[0] == r.trace_id    # the request links to its trace
    assert EMAIL not in db_one(settings.database_path, "SELECT tools_called FROM traces")[0]


# ------------------------------------------------------------------ the real data is never touched
def test_the_student_records_file_carries_no_contact_fields_so_nothing_is_written_there():
    data = ROOT / "data"
    students = json.loads((data / "students.json").read_text())
    assert all("email" not in s and "phone" not in s for s in students)          # contact details are never written there


# ------------------------------------------------------------------ the change lands on the intended record, once
def test_an_approved_change_updates_only_the_requesting_students_record_exactly_once(settings):
    staff = client_for(settings, STAFF_ID)
    mine = request_via_service(None, settings, sid="STU001", value=EMAIL)
    theirs = request_via_service(None, settings, sid="STU002", value="other.person@example.edu")
    assert staff.post(f"/api/approvals/{mine}/approve").status_code == 200
    with closing(sqlite3.connect(settings.database_path)) as conn:
        rows = {r[0]: r[1:] for r in conn.execute("SELECT student_id, email, phone, approval_id FROM student_contacts")}
    assert rows == {"STU001": (EMAIL, None, mine)}                       # STU002's pending request changed nothing
    stamp = db_one(settings.database_path, "SELECT updated_at FROM student_contacts WHERE student_id='STU001'")[0]
    assert staff.post(f"/api/approvals/{mine}/approve").status_code == 409
    assert db_one(settings.database_path, "SELECT updated_at FROM student_contacts WHERE student_id='STU001'")[0] == stamp
    assert staff.post(f"/api/approvals/{theirs}/approve").status_code == 200
    assert db_one(settings.database_path, "SELECT email FROM student_contacts WHERE student_id='STU002'")[0] == "other.person@example.edu"
    assert db_one(settings.database_path, "SELECT email FROM student_contacts WHERE student_id='STU001'")[0] == EMAIL


def test_audit_records_stay_linked_to_the_approval_even_for_an_all_digit_id(settings, monkeypatch):
    import campuspilot.approvals as approvals_module

    class FixedUuid:
        hex = "123456789012" + "0" * 20

    monkeypatch.setattr(approvals_module, "uuid", types.SimpleNamespace(uuid4=lambda: FixedUuid))     # approval ids only
    student, staff = client_for(settings, "STU001"), client_for(settings, STAFF_ID)
    aid = request_via_service(student, settings)
    assert aid == "APR-123456789012"
    assert staff.post(f"/api/approvals/{aid}/approve").status_code == 200
    linked = [e["event"] for e in AuditStore(settings.database_path).list_events(limit=200) if e["approval_id"] == aid]
    assert linked == ["approval.requested", "approval.decision", "approval.execute", "approval.execute"]
