"""Student-support tools. Each returns a JSON-serialisable dict and never raises
for expected problems (unknown student, bad input) - it returns {"error": ...}."""
import json
import uuid
from pathlib import Path

from campuspilot.db import connect

VALID_CATEGORIES = {"fees", "scholarship", "course_change", "other"}


def _find_student(data_dir, student_id):
    with open(Path(data_dir) / "students.json", encoding="utf-8") as f:
        students = json.load(f)
    sid = (student_id or "").strip().upper()
    return next((s for s in students if s["student_id"] == sid), None)


def _not_found(student_id):
    return {"error": f"Student '{student_id}' not found. Please verify the student ID."}


def check_fee_status(data_dir, student_id: str) -> dict:
    student = _find_student(data_dir, student_id)
    if not student:
        return _not_found(student_id)
    return {"student_id": student["student_id"], "name": student["name"], **student["fees"]}


def check_scholarship_status(data_dir, student_id: str) -> dict:
    student = _find_student(data_dir, student_id)
    if not student:
        return _not_found(student_id)
    if not student["scholarship"]:
        return {"student_id": student["student_id"], "scholarship": None,
                "message": "No scholarship is recorded for this student."}
    return {"student_id": student["student_id"], **student["scholarship"]}


def create_support_request(data_dir, db_path, student_id: str, category: str, description: str) -> dict:
    student = _find_student(data_dir, student_id)
    if not student:
        return _not_found(student_id)
    if category not in VALID_CATEGORIES:
        return {"error": f"Invalid category '{category}'. Use one of: {sorted(VALID_CATEGORIES)}."}
    if not (description or "").strip():
        return {"error": "Description is required."}
    ticket_id = f"TKT-{uuid.uuid4().hex[:8].upper()}"
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO support_requests (ticket_id, student_id, category, description) VALUES (?,?,?,?)",
            (ticket_id, student["student_id"], category, description.strip()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ticket_id": ticket_id, "status": "open", "category": category}
