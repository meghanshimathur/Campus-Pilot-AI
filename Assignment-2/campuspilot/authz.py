"""Principal model and authorization policy (Assignment 2 foundation).

Authorization lives in ordinary application code and is DENY BY DEFAULT: an action that is not in ``POLICY`` is
refused, and so is a missing or malformed principal. The authenticated principal is the only authority: a requested
student id (from a URL, a request body or a model's tool arguments) is treated as untrusted input to be COMPARED with
the principal, never as proof of identity or permission.

Roles:
  student  may use public information and their OWN private records, memory and requests.
  staff    may review and decide sensitive-change approvals and read the audit log. Staff are deliberately NOT given
           access to students' private fee/scholarship records (least privilege), and cannot approve their own
           requests (a staff user makes none; the check is still enforced for approvals).
"""
import re
from dataclasses import dataclass
from enum import Enum

STUDENT = "student"
STAFF = "staff"
ROLES = frozenset({STUDENT, STAFF})

_ID_RE = re.compile(r"\A[A-Z0-9_-]{1,32}\Z")


class AuthorizationError(Exception):
    """Raised by callers that prefer an exception to checking ``Decision.allowed``."""

    def __init__(self, decision: "Decision"):
        super().__init__(decision.reason)
        self.decision = decision


@dataclass(frozen=True)
class Principal:
    """Who is making a request. Created only by authentication code, never from request data."""
    user_id: str
    role: str
    student_id: str | None = None
    display_name: str = ""

    def __post_init__(self):
        if self.role not in ROLES:
            raise ValueError(f"unknown role {self.role!r}")
        if not isinstance(self.user_id, str) or not _ID_RE.match(self.user_id):
            raise ValueError("invalid user_id")
        if self.role == STUDENT:
            if self.student_id != self.user_id:
                raise ValueError("a student principal must carry its own student_id")
        elif self.student_id is not None:
            raise ValueError("a staff principal has no student_id")


class Action(str, Enum):
    READ_PUBLIC = "read_public"                  # courses, events, rooms, office hours, handbook
    READ_OWN_PRIVATE = "read_own_private"        # fee status, scholarship status, contact details
    READ_OWN_MEMORY = "read_own_memory"
    WRITE_OWN_MEMORY = "write_own_memory"        # remember a preference
    DELETE_OWN_MEMORY = "delete_own_memory"
    CREATE_SUPPORT_REQUEST = "create_support_request"
    REQUEST_SENSITIVE_CHANGE = "request_sensitive_change"   # e.g. update contact details (needs approval)
    VIEW_OWN_TRACE = "view_own_trace"
    VIEW_OWN_AUDIT = "view_own_audit"
    VIEW_OWN_APPROVALS = "view_own_approvals"
    CANCEL_OWN_APPROVAL = "cancel_own_approval"
    LIST_PENDING_APPROVALS = "list_pending_approvals"
    DECIDE_APPROVAL = "decide_approval"          # approve or reject a sensitive change
    VIEW_ALL_AUDIT = "view_all_audit"


# ownership modes
_ANY = "any"            # any authenticated principal, no target
_OWN = "own"            # the target student must be the principal's own student id
_NOT_REQUESTER = "not_requester"   # the principal must not be the person who made the request


@dataclass(frozen=True)
class Rule:
    roles: frozenset
    ownership: str


POLICY: dict = {
    Action.READ_PUBLIC: Rule(frozenset({STUDENT, STAFF}), _ANY),
    Action.READ_OWN_PRIVATE: Rule(frozenset({STUDENT}), _OWN),
    Action.READ_OWN_MEMORY: Rule(frozenset({STUDENT}), _OWN),
    Action.WRITE_OWN_MEMORY: Rule(frozenset({STUDENT}), _OWN),
    Action.DELETE_OWN_MEMORY: Rule(frozenset({STUDENT}), _OWN),
    Action.CREATE_SUPPORT_REQUEST: Rule(frozenset({STUDENT}), _OWN),
    Action.REQUEST_SENSITIVE_CHANGE: Rule(frozenset({STUDENT}), _OWN),
    Action.VIEW_OWN_TRACE: Rule(frozenset({STUDENT}), _OWN),
    Action.VIEW_OWN_AUDIT: Rule(frozenset({STUDENT}), _OWN),
    Action.VIEW_OWN_APPROVALS: Rule(frozenset({STUDENT}), _OWN),
    Action.CANCEL_OWN_APPROVAL: Rule(frozenset({STUDENT}), _OWN),
    Action.LIST_PENDING_APPROVALS: Rule(frozenset({STAFF}), _ANY),
    Action.DECIDE_APPROVAL: Rule(frozenset({STAFF}), _NOT_REQUESTER),
    Action.VIEW_ALL_AUDIT: Rule(frozenset({STAFF}), _ANY),
}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str            # short machine-readable code, safe to store in an audit record
    action: str = ""

    def __bool__(self) -> bool:
        return self.allowed

    def raise_if_denied(self) -> None:
        if not self.allowed:
            raise AuthorizationError(self)


def normalize_student_id(value) -> str | None:
    """Canonical form of a requested student id (upper-case, trimmed), or None if it is not a plausible id."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip().upper()
    return cleaned if _ID_RE.match(cleaned) else None


def authorize(principal, action, *, target_student_id=None, requester_id=None) -> Decision:
    """May ``principal`` perform ``action``?

    ``target_student_id`` is the student whose record is being touched, as REQUESTED by the caller (untrusted); when it
    is omitted the principal's own record is meant. ``requester_id`` is the user who made the request being decided
    (for approvals). Anything unknown, missing or malformed is denied."""
    name = action.value if isinstance(action, Action) else str(action)
    if not isinstance(principal, Principal):
        return Decision(False, "unauthenticated", name)
    try:
        act = action if isinstance(action, Action) else Action(action)
    except ValueError:
        return Decision(False, "unknown_action", name)
    rule = POLICY.get(act)
    if rule is None:
        return Decision(False, "unknown_action", name)
    if principal.role not in rule.roles:
        return Decision(False, "role_not_permitted", act.value)
    if rule.ownership == _OWN:
        if target_student_id is None:
            return Decision(True, "own_record", act.value)
        target = normalize_student_id(target_student_id)
        if target is None:
            return Decision(False, "invalid_target", act.value)
        if target != principal.student_id:
            return Decision(False, "cross_student_access", act.value)
        return Decision(True, "own_record", act.value)
    if rule.ownership == _NOT_REQUESTER:
        if requester_id is None:
            return Decision(False, "missing_requester", act.value)
        if requester_id == principal.user_id:
            return Decision(False, "self_approval_forbidden", act.value)
        return Decision(True, "independent_approver", act.value)
    return Decision(True, "role_permitted", act.value)
