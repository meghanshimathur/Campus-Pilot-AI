"""Append-only audit log with integrity verification (Assignment 2 foundation).

Each record says who acted (actor), what was attempted (event, action, sanitised details), the permission decision
(allowed / denied / approval_required / approved / rejected), the outcome, and links to the trace and approval ids.

Integrity properties, and their limits:
  * The application only ever INSERTs. SQLite triggers (db.py) make UPDATE and DELETE fail, so a bug or a hostile
    request cannot rewrite history through this code or through ordinary SQL.
  * Every record carries ``entry_hash = SHA-256(previous hash + this record's content)``. ``verify_chain`` recomputes
    the chain and reports the first record that was edited, removed from the middle, or reordered.
  * NOT guaranteed: someone with write access to the database FILE can drop the triggers and rewrite the whole chain
    consistently, or delete the newest records (a shortened chain is still internally valid). Keeping ``head()`` somewhere
    else (for example in an evaluation report) lets a later check notice a shortened or replaced chain.

Secrets never reach the log: details pass through ``secrets_guard.sanitize_details`` before they are written, and the id
columns (which can be filled from a URL) are masked or replaced by a marker if they are not shaped like an id.
"""
import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from campuspilot.authz import Principal
from campuspilot.db import connect
from campuspilot.secrets_guard import sanitize_details

GENESIS = "0" * 64
DECISIONS = frozenset({"allowed", "denied", "approval_required", "approved", "rejected", "not_applicable"})
OUTCOMES = frozenset({"success", "failure", "pending", "blocked", "not_executed", "error"})
ACTOR_ROLES = frozenset({"student", "staff", "anonymous", "system"})
MAX_DETAILS_CHARS = 2000
_FIELDS = ("audit_id", "ts", "actor_id", "actor_role", "event", "action", "resource_student_id", "decision", "outcome",
           "approval_id", "trace_id", "details")


class AuditError(Exception):
    """An audit record could not be written. Callers performing a sensitive action must treat this as 'do not proceed'."""


@dataclass(frozen=True)
class ChainReport:
    ok: bool
    checked: int
    first_bad_id: int | None = None
    reason: str = ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(row: dict) -> str:
    return json.dumps([row[f] for f in _FIELDS], ensure_ascii=True, separators=(",", ":"))


def _entry_hash(prev_hash: str, row: dict) -> str:
    return hashlib.sha256((prev_hash + "\n" + _canonical(row)).encode("utf-8")).hexdigest()


_ID_RE = re.compile(r"\A[A-Za-z0-9_.-]{1,64}\Z")
_STUDENT_ID_RE = re.compile(r"\A[A-Z0-9_-]{1,32}\Z")
_TRACE_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")        # what the agent assigns (uuid4 hex): known to be a safe id, not a token
_APPROVAL_ID_RE = re.compile(r"\AAPR-[0-9A-F]{12}\Z")  # what approvals.py assigns; a run of digits in it must not read as a number


def _text(value, limit=80):
    return None if value is None else str(value)[:limit]


def _id_field(value, limit: int, *, kind: str = "id"):
    """An identifier column (student id, trace id, approval id). These can be filled from a URL or a request, so they are
    never stored as free text: a secret or personal value in them is masked, and anything that is not shaped like an id of
    that kind becomes a marker. The record still shows THAT an odd id was used, not what it said."""
    if value is None:
        return None
    text = str(value).strip()[:limit]
    if (kind == "trace" and _TRACE_ID_RE.match(text)) or (kind == "approval" and _APPROVAL_ID_RE.match(text)):
        return text
    clean = sanitize_details(text)                       # secrets and personal values are masked first, on the raw text
    if clean != text:
        return str(clean)[:limit]
    if kind == "student":
        text = text.upper()
        return text if _STUDENT_ID_RE.match(text) else "[invalid id]"
    return text if _ID_RE.match(text) else "[invalid id]"


class AuditStore:
    def __init__(self, db_path, *, clock: Callable[[], datetime] = _now):
        self.db_path = db_path
        self.clock = clock

    def record(self, event: str, *, actor, action: str, decision: str, outcome: str, resource_student_id=None,
               approval_id=None, trace_id=None, details=None) -> str:
        """Append one record and return its audit id. ``actor`` is a Principal, or a (actor_id, actor_role) pair for
        callers without one (an unauthenticated login attempt is ``("anonymous", "anonymous")``). Raises ValueError for
        a value outside the allowed sets and AuditError if the write fails."""
        if isinstance(actor, Principal):
            actor_id, actor_role = actor.user_id, actor.role
        else:
            actor_id, actor_role = actor
        if decision not in DECISIONS or outcome not in OUTCOMES or actor_role not in ACTOR_ROLES:
            raise ValueError("invalid decision, outcome or actor role")
        if not event or not action:
            raise ValueError("event and action are required")
        clean = sanitize_details(details if details is not None else {})
        details_json = json.dumps(clean, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        if len(details_json) > MAX_DETAILS_CHARS:
            details_json = json.dumps({"truncated": True}, separators=(",", ":"))
        row = {
            "audit_id": "AUD-" + uuid.uuid4().hex[:16].upper(),
            "ts": self.clock().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "actor_id": _text(sanitize_details(str(actor_id)), 64), "actor_role": actor_role,
            "event": _text(event, 60), "action": _text(action, 60),
            "resource_student_id": _id_field(resource_student_id, 32, kind="student"), "decision": decision, "outcome": outcome,
            "approval_id": _id_field(approval_id, 40, kind="approval"), "trace_id": _id_field(trace_id, 40, kind="trace"), "details": details_json,
        }
        conn = connect(self.db_path)
        conn.isolation_level = None            # explicit transaction: the chain head is read and extended atomically
        try:
            conn.execute("BEGIN IMMEDIATE")
            last = conn.execute("SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
            prev = last["entry_hash"] if last else GENESIS
            conn.execute(
                "INSERT INTO audit_log (audit_id, ts, actor_id, actor_role, event, action, resource_student_id, decision, "
                "outcome, approval_id, trace_id, details, prev_hash, entry_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*[row[f] for f in _FIELDS], prev, _entry_hash(prev, row)))
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise AuditError(f"audit write failed ({type(exc).__name__})") from None
        finally:
            conn.close()
        return row["audit_id"]

    def head(self) -> str:
        """Hash of the newest record (the genesis value if the log is empty), for anchoring outside the database."""
        conn = connect(self.db_path)
        try:
            last = conn.execute("SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        return last["entry_hash"] if last else GENESIS

    def verify_chain(self) -> ChainReport:
        """Recompute every hash. Reports the first record that does not fit (edited, removed from the middle, reordered)."""
        conn = connect(self.db_path)
        try:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id ASC").fetchall()
        finally:
            conn.close()
        prev = GENESIS
        for count, r in enumerate(rows):
            if r["prev_hash"] != prev:
                return ChainReport(False, count, r["id"], "chain broken: a record is missing, moved or its link was altered")
            if _entry_hash(prev, dict(r)) != r["entry_hash"]:
                return ChainReport(False, count, r["id"], "record content does not match its hash")
            prev = r["entry_hash"]
        return ChainReport(True, len(rows))

    def list_events(self, *, actor_id=None, resource_student_id=None, event=None, trace_id=None, limit: int = 100,
                    newest_first: bool = False) -> list:
        """Matching records as dicts (details parsed). ``limit`` is capped at 500."""
        where, args = [], []
        for column, value in (("actor_id", actor_id), ("resource_student_id", resource_student_id), ("event", event),
                              ("trace_id", trace_id)):
            if value is not None:
                where.append(f"{column} = ?")
                args.append(value)
        sql = "SELECT * FROM audit_log" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY id " + ("DESC" if newest_first else "ASC") + " LIMIT ?"
        conn = connect(self.db_path)
        try:
            rows = conn.execute(sql, (*args, max(1, min(int(limit), 500)))).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            d = dict(r)
            d["details"] = json.loads(d["details"] or "{}")
            out.append(d)
        return out
