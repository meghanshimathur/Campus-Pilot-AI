"""Sensitive actions that need staff approval before they run (Assignment 2, Batch 4).

Each action is a small spec: how to validate and normalise the requested change, how to describe it without revealing the
value, and how to carry it out. ``update_contact_details`` changes a student's OWN email address or phone number.

Where the change is stored: the student record file (data/students.json) is read-only and has no contact fields, so approved
contact details live in the ``student_contacts`` table. Nothing else is modified. An executor receives the open database
connection of the approval transaction, so the change and the approval's ``executed`` status are committed together or not
at all.
"""
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from campuspilot.db import connect

UPDATE_CONTACT_DETAILS = "update_contact_details"
CONTACT_FIELDS = ("email", "phone")
_EMAIL_RE = re.compile(r"\A[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}\Z")
_PHONE_CHARS_RE = re.compile(r"\A\+?[0-9 ()\-]{6,24}\Z")


def validate_contact_update(args) -> dict:
    """The normalised change ``{"field", "value"}`` or ValueError with a message that does not echo the value."""
    if not isinstance(args, dict):
        raise ValueError("The request must name a field and a new value.")
    field, value = args.get("field"), args.get("value")
    if field not in CONTACT_FIELDS:
        raise ValueError(f"The field must be one of: {', '.join(CONTACT_FIELDS)}.")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A new value is required.")
    value = value.strip()
    if field == "email":
        value = value.lower()
        if len(value) > 120 or not _EMAIL_RE.match(value) or ".." in value:
            raise ValueError("That does not look like a valid email address.")
    else:
        if not _PHONE_CHARS_RE.match(value):
            raise ValueError("That does not look like a valid phone number.")
        digits = re.sub(r"\D", "", value)
        if not 8 <= len(digits) <= 15:
            raise ValueError("A phone number has 8 to 15 digits.")
        value = ("+" if value.startswith("+") else "") + digits
    return {"field": field, "value": value}


def mask_value(field: str, value: str) -> str:
    """A recognisable but not reusable form (``a***@example.com``, ``******3210``) for staff queues and logs."""
    if not isinstance(value, str) or not value:
        return ""
    if field == "email" and "@" in value:
        local, domain = value.split("@", 1)
        return f"{local[:1]}***@{domain}"
    digits = re.sub(r"\D", "", value)
    return "*" * max(len(digits) - 4, 0) + digits[-4:] if digits else "***"


def _execute_update_contact(conn: sqlite3.Connection, row: dict, args: dict, now: datetime) -> dict:
    field = args["field"]
    if field not in CONTACT_FIELDS:                               # defence in depth: the column name never comes from input
        raise ValueError("unsupported field")
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    sql = {"email": "INSERT INTO student_contacts (student_id, email, updated_at, approval_id) VALUES (?,?,?,?) "
                    "ON CONFLICT(student_id) DO UPDATE SET email=excluded.email, updated_at=excluded.updated_at, approval_id=excluded.approval_id",
           "phone": "INSERT INTO student_contacts (student_id, phone, updated_at, approval_id) VALUES (?,?,?,?) "
                    "ON CONFLICT(student_id) DO UPDATE SET phone=excluded.phone, updated_at=excluded.updated_at, approval_id=excluded.approval_id"}[field]
    conn.execute(sql, (row["student_id"], args["value"], stamp, row["approval_id"]))
    return {"field": field}                                       # the result never contains the value


@dataclass(frozen=True)
class ActionSpec:
    validate: Callable
    execute: Callable          # (conn, approval_row, args, now) -> result dict (no personal values)
    describe: str


ACTIONS = {
    UPDATE_CONTACT_DETAILS: ActionSpec(validate_contact_update, _execute_update_contact,
                                       "change your own email address or phone number"),
}


class ContactStore:
    """Read access to approved contact details (the owner's own, never anyone else's)."""

    def __init__(self, db_path):
        self.db_path = db_path

    def get(self, student_id: str) -> dict | None:
        conn = connect(self.db_path)
        try:
            row = conn.execute("SELECT student_id, email, phone, updated_at, approval_id FROM student_contacts WHERE student_id=?",
                               (student_id,)).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None
