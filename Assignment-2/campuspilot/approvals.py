"""Human approval of sensitive actions (Assignment 2, Batch 4).

A student asks for a sensitive change (today: ``update_contact_details``). That creates a PENDING approval and changes
nothing. A staff member (a separate account) approves or rejects it. Only an approved, unexpired, untampered request is
executed, once.

State machine (every arrow is a single atomic ``UPDATE ... WHERE status = <from>``, so two callers can never both win):

    pending --staff approves--> approved --executed in one transaction--> executed
       |                           |                                  \\-> failed   (executor error, failed recheck, tampering)
       |--staff rejects--> rejected (terminal: NEVER executes)
       |--requester cancels--> cancelled (terminal)
       \\--time passes--> expired (also from ``approved`` if it was never executed) (terminal)

Rules enforced in code (not in the prompt):
  * Creating a request does not touch the data it asks to change.
  * Only a staff account may decide (authz.DECIDE_APPROVAL); a requester can never decide their own request.
  * Expiry: a request is valid for ``ttl`` (24 hours by default) from creation; an expired request is never executed.
  * Tamper evidence: the proposed change is stored as canonical JSON with an HMAC-SHA256 (``request_mac``) over the request's
    id, student, requester, action, arguments and times; the approval adds a ``decision_mac`` over the approver and time.
    Both are verified before the decision and again before execution. Editing the stored arguments, the target student or the
    status in the database without the signing key is detected and the request is never executed.
  * Authorization is re-checked at execution: the approver must still exist and still be allowed to decide, the requester must
    still exist as that student; otherwise the request fails closed.
  * Audit: the request, the decision and the start of execution are written to the audit log BEFORE the change runs; if a
    record cannot be written the change does not run (503). The closing result record is written after the commit; if THAT
    fails it is logged, and the approval row itself still records the result.
  * Execution and the ``executed`` status flip happen in one SQLite transaction (``BEGIN IMMEDIATE``): the change either
    happens exactly once together with the status, or not at all.

Limits (stated): the signing key lives in a file next to the database (mode 0600), not in it, so an attacker who can write the
database but cannot read the key cannot forge a valid request or decision; someone who can read both can. The proposed value is
stored in the approvals table in clear text (the executor needs it) but never in audit records, traces or staff views, which
show a masked form.
"""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from campuspilot.audit import AuditError, AuditStore
from campuspilot.auth import AuthService
from campuspilot.authz import STAFF, STUDENT, Action, Principal, authorize
from campuspilot.db import connect
from campuspilot.secrets_guard import scrub_for_storage
from campuspilot.sensitive_actions import ACTIONS, mask_value

logger = logging.getLogger("campuspilot.approvals")

DEFAULT_TTL = timedelta(hours=24)
MAX_PENDING_PER_STUDENT = 5
STATUSES = ("pending", "approved", "rejected", "cancelled", "expired", "executed", "failed")
_ID_RE = re.compile(r"\AAPR-[0-9A-F]{12}\Z")
SYSTEM = ("system", "system")          # actor for records written by the app itself (expiry, tamper detection)


class ApprovalError(Exception):
    """kind: not_found | forbidden | conflict | expired | integrity | audit_unavailable | invalid | limit | execution_failed"""

    def __init__(self, kind: str, message: str = "", **info):
        super().__init__(message or kind)
        self.kind, self.info = kind, info


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def load_or_create_key(path) -> bytes:
    """The approval signing key: 32 random bytes in a file (mode 0600) next to the database, created on first use."""
    path = Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(secrets.token_bytes(32))
        except FileExistsError:                       # another process created it first
            pass
    key = path.read_bytes()
    if len(key) < 32:
        raise ValueError("the approval signing key file is invalid; remove it only if no request is pending")
    return key


class ApprovalService:
    def __init__(self, db_path, *, auth: AuthService, audit: AuditStore, signing_key: bytes, executors: dict | None = None,
                 ttl: timedelta = DEFAULT_TTL, clock: Callable[[], datetime] = _utcnow):
        if not isinstance(signing_key, (bytes, bytearray)) or len(signing_key) < 16:
            raise ValueError("a signing key of at least 16 bytes is required")
        self.db_path, self.auth, self.audit = db_path, auth, audit
        self._key, self.ttl, self.clock = bytes(signing_key), ttl, clock
        self.executors = executors if executors is not None else {name: spec.execute for name, spec in ACTIONS.items()}

    # ------------------------------------------------------------------ signing
    def _mac(self, *parts: str) -> str:
        return hmac.new(self._key, "\x1f".join(parts).encode("utf-8"), hashlib.sha256).hexdigest()

    def _request_mac(self, row: dict) -> str:
        return self._mac("request", row["approval_id"], row["student_id"], row["requested_by"], row["action"],
                         row["args_json"], row["created_at"], row["expires_at"])

    def _decision_mac(self, row: dict, decided_by: str, decided_at: str) -> str:
        return self._mac("decision", row["request_mac"], decided_by, decided_at)

    def _intact(self, row: dict, *, decided: bool = False) -> bool:
        if not hmac.compare_digest(self._request_mac(row), row["request_mac"] or ""):
            return False
        if decided:
            if not row.get("decided_by") or not row.get("decided_at") or not row.get("decision_mac"):
                return False
            return hmac.compare_digest(self._decision_mac(row, row["decided_by"], row["decided_at"]), row["decision_mac"])
        return True

    # ------------------------------------------------------------------ storage helpers
    def _row(self, approval_id) -> dict | None:
        if not isinstance(approval_id, str) or not _ID_RE.match(approval_id):
            return None
        conn = connect(self.db_path)
        try:
            row = conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def _set_terminal(self, approval_id: str, from_statuses: tuple, status: str, reason: str) -> bool:
        conn = connect(self.db_path)
        try:
            marks = ",".join("?" for _ in from_statuses)
            cur = conn.execute(f"UPDATE approvals SET status=?, result=? WHERE approval_id=? AND status IN ({marks})",
                               (status, json.dumps({"reason": reason}), approval_id, *from_statuses))
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()

    # ------------------------------------------------------------------ auditing
    def _audit_required(self, event, actor, action, decision, outcome, row=None, details=None):
        """No record, no action: a failed write stops the caller with ApprovalError('audit_unavailable')."""
        try:
            return self.audit.record(event, actor=actor, action=action, decision=decision, outcome=outcome,
                                     resource_student_id=row["student_id"] if row else None,
                                     approval_id=row["approval_id"] if row else None,
                                     trace_id=row.get("trace_id") if row else None, details=details)
        except AuditError:
            raise ApprovalError("audit_unavailable") from None

    def _audit_best_effort(self, *args, **kwargs):
        try:
            self._audit_required(*args, **kwargs)
        except ApprovalError:
            logger.warning("audit write failed (%s)", args[0] if args else "?")

    # ------------------------------------------------------------------ views
    def view(self, principal, row: dict) -> dict:
        """What a caller may see: the owner sees their own proposed value; staff see only a masked form."""
        args = json.loads(row["args_json"])
        out = {"approval_id": row["approval_id"], "student_id": row["student_id"], "action": row["action"],
               "field": row["field"], "status": row["status"], "created_at": row["created_at"],
               "expires_at": row["expires_at"], "decided_by": row["decided_by"], "decided_at": row["decided_at"],
               "decision_reason": row["decision_reason"], "executed_at": row["executed_at"]}
        if isinstance(principal, Principal) and principal.role == STUDENT and principal.student_id == row["student_id"]:
            out["proposed_value"] = args.get("value")
        else:
            out["proposed_value_masked"] = mask_value(row["field"], args.get("value", ""))
        return out

    # ------------------------------------------------------------------ expiry
    def expire_stale(self) -> list:
        """Mark every pending or approved-but-unexecuted request past its deadline as expired. Returns their ids."""
        now = _iso(self.clock())
        conn = connect(self.db_path)
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM approvals WHERE status IN ('pending','approved') AND expires_at <= ?", (now,))]
            expired = []
            for row in rows:
                cur = conn.execute("UPDATE approvals SET status='expired' WHERE approval_id=? AND status IN ('pending','approved')",
                                   (row["approval_id"],))
                if cur.rowcount == 1:
                    expired.append(row)
            conn.commit()
        finally:
            conn.close()
        for row in expired:
            self._audit_best_effort("approval.expired", SYSTEM, row["action"], "not_applicable", "not_executed", row,
                                    {"field": row["field"], "was": row["status"]})
        return [r["approval_id"] for r in expired]

    # ------------------------------------------------------------------ request
    def request(self, principal, action: str, args, trace_id: str | None = None) -> dict:
        spec = ACTIONS.get(action)
        if spec is None:
            raise ApprovalError("invalid", "unknown action")
        decision = authorize(principal, Action.REQUEST_SENSITIVE_CHANGE)
        if not decision.allowed:
            raise ApprovalError("forbidden", decision.reason)
        try:
            normalized = spec.validate(args)
        except ValueError as exc:
            raise ApprovalError("invalid", str(exc)) from None
        self.expire_stale()
        now = self.clock()
        row = {"approval_id": "APR-" + uuid.uuid4().hex[:12].upper(), "student_id": principal.student_id,
               "requested_by": principal.user_id, "action": action, "field": normalized["field"],
               "args_json": json.dumps(normalized, sort_keys=True, separators=(",", ":")),
               "created_at": _iso(now), "expires_at": _iso(now + self.ttl), "trace_id": trace_id}
        row["request_mac"] = self._request_mac(row)
        self._audit_required("approval.requested", principal, action, "approval_required", "pending", row,
                             {"field": row["field"], "type": action, "expires_at": row["expires_at"]})   # before anything is stored
        conn = connect(self.db_path)
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")                   # the limit checks and the insert are one atomic step
            pending = conn.execute("SELECT field FROM approvals WHERE student_id=? AND status='pending'",
                                   (row["student_id"],)).fetchall()
            if len(pending) >= MAX_PENDING_PER_STUDENT:
                conn.execute("ROLLBACK")
                raise ApprovalError("limit", "You already have the maximum number of pending requests.")
            same = conn.execute("SELECT approval_id FROM approvals WHERE student_id=? AND status='pending' AND action=? AND field=?",
                                (row["student_id"], action, row["field"])).fetchone()
            if same:
                conn.execute("ROLLBACK")
                raise ApprovalError("conflict", "You already have a pending request for this field.",
                                    existing=same["approval_id"])
            conn.execute("INSERT INTO approvals (approval_id, student_id, requested_by, action, field, args_json, request_mac, "
                         "status, created_at, expires_at, trace_id) VALUES (?,?,?,?,?,?,?,'pending',?,?,?)",
                         (row["approval_id"], row["student_id"], row["requested_by"], action, row["field"], row["args_json"],
                          row["request_mac"], row["created_at"], row["expires_at"], trace_id))
            conn.execute("COMMIT")
        except ApprovalError:
            raise
        except sqlite3.Error:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise ApprovalError("invalid", "The request could not be stored.") from None
        finally:
            conn.close()
        return self.view(principal, self._row(row["approval_id"]))

    # ------------------------------------------------------------------ reading
    def get(self, principal, approval_id: str) -> dict:
        """One request as the caller may see it. Someone else's request looks exactly like a missing one."""
        self.expire_stale()
        row = self._row(approval_id)
        if row is None:
            raise ApprovalError("not_found")
        if isinstance(principal, Principal) and principal.role == STAFF:
            allowed = authorize(principal, Action.LIST_PENDING_APPROVALS).allowed
        else:
            allowed = authorize(principal, Action.VIEW_OWN_APPROVALS, target_student_id=row["student_id"]).allowed
        if not allowed:
            self._audit_best_effort("authz.denied", principal if isinstance(principal, Principal) else ("anonymous", "anonymous"),
                                    "view_approval", "denied", "blocked", row, {"reason": "not_owner"})
            raise ApprovalError("not_found")
        return self.view(principal, row)

    def list_for(self, principal, status: str | None = None, limit: int = 50) -> list:
        self.expire_stale()
        if status is not None and status not in STATUSES:
            raise ApprovalError("invalid", "unknown status")
        if isinstance(principal, Principal) and principal.role == STAFF:
            if not authorize(principal, Action.LIST_PENDING_APPROVALS).allowed:
                raise ApprovalError("forbidden")
            where, args = "status = ?", [status or "pending"]
        else:
            decision = authorize(principal, Action.VIEW_OWN_APPROVALS)
            if not decision.allowed:
                raise ApprovalError("forbidden", decision.reason)
            where, args = "student_id = ?" + (" AND status = ?" if status else ""), [principal.student_id] + ([status] if status else [])
        conn = connect(self.db_path)
        try:
            rows = [dict(r) for r in conn.execute(f"SELECT * FROM approvals WHERE {where} ORDER BY created_at DESC LIMIT ?",
                                                  (*args, max(1, min(int(limit), 200))))]
        finally:
            conn.close()
        return [self.view(principal, r) for r in rows]

    # ------------------------------------------------------------------ cancel
    def cancel(self, principal, approval_id: str) -> dict:
        self.expire_stale()
        row = self._row(approval_id)
        if row is None:
            raise ApprovalError("not_found")
        decision = authorize(principal, Action.CANCEL_OWN_APPROVAL, target_student_id=row["student_id"])
        if not decision.allowed:
            self._audit_best_effort("authz.denied", principal if isinstance(principal, Principal) else ("anonymous", "anonymous"),
                                    "cancel_approval", "denied", "blocked", row, {"reason": decision.reason})
            raise ApprovalError("not_found")
        if row["requested_by"] != principal.user_id:
            raise ApprovalError("not_found")
        if not self._claim(approval_id, "cancelled", principal.user_id, None):
            raise ApprovalError("conflict", status=(self._row(approval_id) or {}).get("status"))
        row = self._row(approval_id)
        self._audit_required("approval.cancelled", principal, row["action"], "allowed", "not_executed", row, {"field": row["field"]})
        return self.view(principal, row)

    # ------------------------------------------------------------------ decide
    def _claim(self, approval_id, new_status, decided_by, reason, decision_mac=None, decided_at=None) -> bool:
        """pending -> new_status as ONE atomic update. False if anyone else got there first or it has expired."""
        now = _iso(self.clock())
        conn = connect(self.db_path)
        try:
            cur = conn.execute(
                "UPDATE approvals SET status=?, decided_by=?, decided_at=?, decision_reason=?, decision_mac=? "
                "WHERE approval_id=? AND status='pending' AND expires_at > ?",
                (new_status, decided_by, decided_at or now, reason, decision_mac, approval_id, now))
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()

    def _tampered(self, row: dict, actor) -> ApprovalError:
        self._set_terminal(row["approval_id"], ("pending", "approved"), "failed", "integrity_check_failed")
        self._audit_best_effort("approval.tamper_detected", actor, row["action"], "denied", "blocked", row,
                                {"reason": "signature mismatch"})
        return ApprovalError("integrity")

    def decide(self, approval_id: str, principal, *, approve: bool, reason: str | None = None, execute_now: bool = True) -> dict:
        """Staff approve or reject. Returns ``{"approval": view, "executed": bool}``."""
        actor = principal if isinstance(principal, Principal) else ("anonymous", "anonymous")
        if not isinstance(principal, Principal) or principal.role != STAFF:
            # role gate BEFORE any lookup: a student learns nothing about which request ids exist
            self._audit_best_effort("authz.denied", actor, "decide_approval", "denied", "blocked", None, {"reason": "role_not_permitted"})
            raise ApprovalError("forbidden", "role_not_permitted")
        self.expire_stale()
        row = self._row(approval_id)
        if row is None:
            raise ApprovalError("not_found")
        decision = authorize(principal, Action.DECIDE_APPROVAL, requester_id=row["requested_by"])
        if not decision.allowed:
            self._audit_best_effort("authz.denied", principal, "decide_approval", "denied", "blocked", row, {"reason": decision.reason})
            raise ApprovalError("forbidden", decision.reason)
        if row["status"] != "pending":
            raise ApprovalError("expired" if row["status"] == "expired" else "conflict", status=row["status"])
        if not self._intact(row):
            raise self._tampered(row, principal)
        note = scrub_for_storage(reason, max_chars=200) if isinstance(reason, str) and reason.strip() else None
        if not approve:
            if not self._claim(approval_id, "rejected", principal.user_id, note):
                latest = self._row(approval_id) or {}
                raise ApprovalError("expired" if latest.get("status") == "expired" else "conflict", status=latest.get("status"))
            row = self._row(approval_id)
            self._audit_required("approval.decision", principal, row["action"], "rejected", "not_executed", row,
                                 {"field": row["field"], "requested_by": row["requested_by"]})
            return {"approval": self.view(principal, row), "executed": False}
        decided_at = _iso(self.clock())
        mac = self._decision_mac(row, principal.user_id, decided_at)
        if not self._claim(approval_id, "approved", principal.user_id, note, mac, decided_at):      # only one caller can win
            latest = self._row(approval_id) or {}
            raise ApprovalError("expired" if latest.get("status") == "expired" else "conflict", status=latest.get("status"))
        row = self._row(approval_id)
        self._audit_required("approval.decision", principal, row["action"], "approved", "pending", row,
                             {"field": row["field"], "requested_by": row["requested_by"]})
        if not execute_now:
            return {"approval": self.view(principal, row), "executed": False}
        return self.execute(approval_id, viewer=principal)

    # ------------------------------------------------------------------ execute
    def execute(self, approval_id: str, viewer=None) -> dict:
        """Run an APPROVED request, once, after re-checking everything. Safe to call again: a second call changes nothing."""
        row = self._row(approval_id)
        if row is None:
            raise ApprovalError("not_found")
        if row["status"] != "approved":
            raise ApprovalError("conflict", status=row["status"])      # rejected, expired, executed, ... never execute
        if row["expires_at"] <= _iso(self.clock()):
            self.expire_stale()
            raise ApprovalError("expired", status="expired")
        if not self._intact(row, decided=True):
            raise self._tampered(row, SYSTEM)
        spec_exec = self.executors.get(row["action"])
        if spec_exec is None:
            raise self._fail(row, "no_executor")
        # --- authorization is checked AGAIN, against the accounts as they are now
        approver = self.auth.get_principal(row["decided_by"])
        requester = self.auth.get_principal(row["requested_by"])
        recheck = (approver is not None and authorize(approver, Action.DECIDE_APPROVAL, requester_id=row["requested_by"]).allowed
                   and requester is not None and requester.role == STUDENT and requester.student_id == row["student_id"]
                   and authorize(requester, Action.REQUEST_SENSITIVE_CHANGE, target_student_id=row["student_id"]).allowed)
        if not recheck:
            self._audit_best_effort("approval.execute", approver or SYSTEM, row["action"], "denied", "blocked", row,
                                    {"reason": "authorization_recheck_failed"})
            raise self._fail(row, "authorization_recheck_failed", kind="forbidden")
        self._audit_required("approval.execute", approver, row["action"], "approved", "pending", row,
                             {"field": row["field"]})                       # before the change: no record, no change
        args = json.loads(row["args_json"])
        conn = connect(self.db_path)
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            fresh = conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
            fresh = dict(fresh) if fresh else None
            if fresh is None or fresh["status"] != "approved":
                conn.execute("ROLLBACK")
                raise ApprovalError("conflict", status=fresh["status"] if fresh else "missing")      # someone already executed it
            if not self._intact(fresh, decided=True) or fresh["args_json"] != row["args_json"]:
                conn.execute("ROLLBACK")
                raise self._tampered(fresh, SYSTEM)
            now = self.clock()
            if fresh["expires_at"] <= _iso(now):
                conn.execute("ROLLBACK")
                self.expire_stale()
                raise ApprovalError("expired", status="expired")
            result = spec_exec(conn, fresh, args, now)
            cur = conn.execute("UPDATE approvals SET status='executed', executed_at=?, result=? WHERE approval_id=? AND status='approved'",
                               (_iso(now), json.dumps(result, sort_keys=True), approval_id))
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                raise ApprovalError("conflict", status="not_approved")
            conn.execute("COMMIT")
        except ApprovalError:
            raise
        except Exception as exc:                                   # the executor failed: nothing it wrote is kept
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            logger.warning("approved action failed (%s)", type(exc).__name__)
            conn.close()
            raise self._fail(row, f"executor_error:{type(exc).__name__}", kind="execution_failed")
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        done = self._row(approval_id)
        self._audit_best_effort("approval.execute", approver, row["action"], "approved", "success", done,
                                {"field": done["field"], "result": json.loads(done["result"] or "{}")})
        return {"approval": self.view(viewer, done), "executed": True}

    def _fail(self, row: dict, reason: str, kind: str = "execution_failed") -> ApprovalError:
        self._set_terminal(row["approval_id"], ("approved",), "failed", reason)
        self._audit_best_effort("approval.execute", SYSTEM, row["action"], "approved", "failure", row, {"reason": reason})
        return ApprovalError(kind, reason)
