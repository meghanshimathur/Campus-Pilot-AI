import pytest

from campuspilot.db import connect
from campuspilot.tool_registry import ToolRegistry


@pytest.fixture
def registry(settings):
    return ToolRegistry(settings.data_dir, settings.database_path)


def test_schemas_cover_all_tools(registry):  # 7 original + search_handbook + remember_preference
    assert {s["name"] for s in registry.schemas} == {
        "check_fee_status", "check_scholarship_status", "create_support_request",
        "search_course_catalog", "get_professor_office_hours", "check_room_availability",
        "get_campus_events", "search_handbook", "remember_preference"}
    for s in registry.schemas:  # every schema is well-formed and requires only declared properties
        assert s["description"] and s["parameters"]["type"] == "object"
        assert set(s["parameters"]["required"]) <= set(s["parameters"]["properties"])


def test_registry_dispatches_campus_tools(registry):
    assert registry.execute("search_course_catalog", {"query": "spark"}, "STU001")["count"] == 1
    assert "professors" in registry.execute("get_professor_office_hours", {"professor_name": "rao"}, "STU001")
    assert registry.execute("check_room_availability", {"room_id": "A-101", "day": "Mon"}, "STU001")["day"] == "Mon"
    assert registry.execute("get_campus_events", {"from_date": "2026-11-01"}, "STU001")["count"] >= 1


def test_registry_campus_tools_reject_missing_args(registry):
    assert "error" in registry.execute("search_course_catalog", {}, "STU001")
    assert "error" in registry.execute("get_professor_office_hours", {}, "STU001")
    assert "error" in registry.execute("check_room_availability", {"room_id": "A-101"}, "STU001")
    # student_id must not be model-controllable
    assert all("student_id" not in s["parameters"]["properties"] for s in registry.schemas)


def test_execute_fee_and_scholarship(registry):
    assert registry.execute("check_fee_status", {}, "STU002")["status"] == "partial"
    assert registry.execute("check_scholarship_status", {}, "STU001")["status"] == "active"


def test_student_id_cannot_be_overridden(registry):
    r = registry.execute("check_fee_status", {"student_id": "STU001"}, "STU003")
    assert r["student_id"] == "STU003"


def test_unknown_student_tool_and_bad_args(registry):
    assert "error" in registry.execute("check_fee_status", {}, "BAD")
    assert "error" in registry.execute("delete_everything", {}, "STU001")
    assert "error" in registry.execute("check_fee_status", None, "STU001")
    assert "error" in registry.execute("create_support_request", {"category": "x", "description": "y"}, "STU001")
    assert "error" in registry.execute("create_support_request", {}, "STU001")


def test_support_request_persists(registry, settings):
    r = registry.execute("create_support_request", {"category": "fees", "description": "Receipt"}, "STU001")
    conn = connect(settings.database_path)
    n = conn.execute("SELECT COUNT(*) FROM support_requests WHERE ticket_id=?", (r["ticket_id"],)).fetchone()[0]
    conn.close()
    assert n == 1


def test_unexpected_exception_is_contained(registry, monkeypatch):
    from campuspilot import tools

    def boom(*a, **k):
        raise RuntimeError("secret internal detail")
    monkeypatch.setattr(tools, "check_fee_status", boom)
    r = registry.execute("check_fee_status", {}, "STU001")
    assert "error" in r and "secret" not in r["error"]


def test_all_schemas_accepted_by_gemini_and_openai_formats(registry):
    from google.genai import types
    for s in registry.schemas:
        d = types.FunctionDeclaration(name=s["name"], description=s["description"],
                                      parameters_json_schema=s["parameters"])
        assert d.name == s["name"]
        wrapped = {"type": "function", "function": s}  # OpenAI tool format
        assert wrapped["function"]["parameters"]["type"] == "object"
