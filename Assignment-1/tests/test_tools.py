import json

from campuspilot import tools
from campuspilot.db import connect


def test_data_valid_and_consistent(settings):
    students = json.loads((settings.data_dir / "students.json").read_text())
    assert len(students) >= 3
    for s in students:
        f = s["fees"]
        assert f["paid"] + f["due"] == f["total"]


def test_fee_status(settings):
    r = tools.check_fee_status(settings.data_dir, "stu003")
    assert r["status"] == "overdue" and r["due"] == 300000


def test_unknown_student(settings):
    assert "error" in tools.check_fee_status(settings.data_dir, "NOPE")
    assert "error" in tools.check_scholarship_status(settings.data_dir, "")


def test_scholarship(settings):
    assert tools.check_scholarship_status(settings.data_dir, "STU002")["status"] == "under_review"
    assert tools.check_scholarship_status(settings.data_dir, "STU003")["scholarship"] is None


def test_support_request_persisted(settings):
    r = tools.create_support_request(settings.data_dir, settings.database_path, "STU001", "fees", "Receipt needed")
    conn = connect(settings.database_path)
    row = conn.execute("SELECT * FROM support_requests WHERE ticket_id=?", (r["ticket_id"],)).fetchone()
    conn.close()
    assert row["student_id"] == "STU001"


def test_support_request_validation(settings):
    args = (settings.data_dir, settings.database_path)
    assert "error" in tools.create_support_request(*args, "STU001", "bogus", "x")
    assert "error" in tools.create_support_request(*args, "STU001", "fees", "  ")
    assert "error" in tools.create_support_request(*args, "XXX", "fees", "x")
