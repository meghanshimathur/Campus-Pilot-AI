"""Tool schemas, authorization and safe dispatch.

Whose records a tool touches is decided by the authenticated Principal passed in by the API, never by the model. The fee
and scholarship tools accept an OPTIONAL requested ``student_id`` only so that an attempt to read someone else's record
reaches code where it is compared with the principal, refused, and written to the audit log. That requested id is
untrusted input: it is never used to look anything up.

Every tool maps to an authz.Action (TOOL_ACTIONS); a tool without an entry cannot run. Authorization and auditing happen
here, in ordinary code, not in the system prompt:
  * denied attempts are audited (decision=denied, outcome=blocked) and answered with a generic refusal;
  * private reads are audited BEFORE the data is returned (no audit record, no data);
  * writes are audited BEFORE they run (no audit record, no write) and again with their result.
"""
import logging

from campuspilot import campus_tools, memory as memory_mod, rag, tools
from campuspilot.approvals import ApprovalError
from campuspilot.audit import AuditError, AuditStore
from campuspilot.authz import STUDENT, Action, Decision, Principal, authorize

logger = logging.getLogger("campuspilot.tools")

# What each tool is, for authorization. Public tools need only an authenticated principal; private reads and writes are
# limited to the principal's own records.
TOOL_ACTIONS = {
    "check_fee_status": Action.READ_OWN_PRIVATE,
    "check_scholarship_status": Action.READ_OWN_PRIVATE,
    "create_support_request": Action.CREATE_SUPPORT_REQUEST,
    "remember_preference": Action.WRITE_OWN_MEMORY,
    "search_handbook": Action.READ_PUBLIC,
    "search_course_catalog": Action.READ_PUBLIC,
    "get_professor_office_hours": Action.READ_PUBLIC,
    "check_room_availability": Action.READ_PUBLIC,
    "get_campus_events": Action.READ_PUBLIC,
    "update_contact_details": Action.REQUEST_SENSITIVE_CHANGE,
}
PRIVATE_READ_TOOLS = frozenset({"check_fee_status", "check_scholarship_status"})
WRITE_TOOLS = frozenset({"create_support_request", "remember_preference"})  # audited before they run
DENIED_MESSAGE = "Access denied: you can only use your own records."
PENDING_MESSAGE = ("Your request has been sent for staff approval. Nothing has been changed yet; "
                   "it takes effect only if a staff member approves it.")
AUDIT_DOWN_MESSAGE = "The audit log is unavailable, so the request was not performed."
_REQUESTED_ID_PROPERTY = {"type": "string", "description": "Optional. Only your own student id is allowed; a request for "
                          "anyone else's record is refused and logged. Leave it out to use your own record."}

TOOL_SCHEMAS = [
    {
        "name": "remember_preference",
        "description": ("Save ONE lasting, non-sensitive preference the student has explicitly stated, so it can be "
                        "recalled in future sessions. Never use for passwords, payment or bank details, health "
                        "information, amounts, or anything temporary (like 'today' or 'this week')."),
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "enum": sorted(memory_mod.ALLOWED_KEYS)},
                "value": {"type": "string", "description": "Short value, at most 100 characters."},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "search_handbook",
        "description": ("Search the student handbook for policies (fees, payment problems, refunds, scholarships, "
                        "exams, attendance, library, hostel, grievances, deadlines, course changes, ID cards). "
                        "Returns only the relevant excerpts with chunk references, or found=false."),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "The policy question in plain words."}},
            "required": ["query"],
        },
    },
    {
        "name": "check_fee_status",
        "description": "Get the current student's fee status: total, paid, due amount, due date, status.",
        "parameters": {"type": "object", "properties": {"student_id": _REQUESTED_ID_PROPERTY}, "required": []},
    },
    {
        "name": "check_scholarship_status",
        "description": "Get the current student's scholarship name, amount and status.",
        "parameters": {"type": "object", "properties": {"student_id": _REQUESTED_ID_PROPERTY}, "required": []},
    },
    {
        "name": "create_support_request",
        "description": "Create a support ticket for the current student. Only call when the student asks for help to be raised.",
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": sorted(tools.VALID_CATEGORIES)},
                "description": {"type": "string", "description": "Short summary of the issue."},
            },
            "required": ["category", "description"],
        },
    },
    {
        "name": "update_contact_details",
        "description": ("Request a change to the CURRENT student's own email or phone number. This only creates a request "
                        "that staff must approve; nothing changes until they do. Only call when the student explicitly "
                        "asks to change their contact details."),
        "parameters": {
            "type": "object",
            "properties": {
                "field": {"type": "string", "enum": ["email", "phone"]},
                "value": {"type": "string", "description": "The new email address or phone number."},
            },
            "required": ["field", "value"],
        },
    },
    {
        "name": "search_course_catalog",
        "description": "Search the course catalog by course name, ID or topic keyword, optionally within one department.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Course name, ID or topic, e.g. 'machine learning'."},
                "department": {"type": "string", "description": "Optional department: Analytics, AI or Management."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_professor_office_hours",
        "description": "Get a professor's office room and weekly office hours by (part of) their name.",
        "parameters": {
            "type": "object",
            "properties": {"professor_name": {"type": "string"}},
            "required": ["professor_name"],
        },
    },
    {
        "name": "check_room_availability",
        "description": "Check a room's bookings on a weekday, and optionally whether a time window is free.",
        "parameters": {
            "type": "object",
            "properties": {
                "room_id": {"type": "string", "description": "e.g. A-101, B-203, LAB-1, STUDY-2."},
                "day": {"type": "string", "enum": campus_tools.DAYS},
                "start_time": {"type": "string", "description": "Optional 24h HH:MM."},
                "end_time": {"type": "string", "description": "Optional 24h HH:MM, required with start_time."},
            },
            "required": ["room_id", "day"],
        },
    },
    {
        "name": "get_campus_events",
        "description": "List upcoming campus events, optionally by category and/or starting from a date.",
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": sorted(campus_tools.EVENT_CATEGORIES)},
                "from_date": {"type": "string", "description": "Optional YYYY-MM-DD; defaults to today."},
            },
            "required": [],
        },
    },
]


class ToolRegistry:
    def __init__(self, data_dir, db_path, retriever=None, memory=None, audit=None, approvals=None):
        self.data_dir = data_dir
        self.db_path = db_path
        self.retriever = retriever
        self.memory = memory
        self.audit = audit or AuditStore(db_path)   # auditing is always on; there is no 'no audit' mode
        self.approvals = approvals                  # ApprovalService; without one, sensitive requests are unavailable
        self.schemas = TOOL_SCHEMAS

    def canonical_student_id(self, student_id: str) -> str | None:
        """Normalised ID (e.g. 'stu001' -> 'STU001') or None if the student does not exist."""
        student = tools._find_student(self.data_dir, student_id)
        return student["student_id"] if student else None

    def resolve_principal(self, who) -> Principal | None:
        """A Principal passes through. A plain student-id string is the INTERNAL compatibility path used by tests and the
        offline evaluation harness: it becomes a student principal if that student exists. The API never uses it; it
        always passes the verified Principal (tests/test_route_security.py checks that)."""
        if isinstance(who, Principal):
            return who
        if isinstance(who, str):
            sid = self.canonical_student_id(who)
            return Principal(sid, STUDENT, sid) if sid else None
        return None

    # ---- auditing helpers
    def _audit(self, who, event, action, decision, outcome, *, target=None, trace_id=None, details=None):
        actor = who if isinstance(who, Principal) else ("anonymous", "anonymous")
        return self.audit.record(event, actor=actor, action=action, decision=decision, outcome=outcome,
                                 resource_student_id=target, trace_id=trace_id, details=details)

    def _audit_best_effort(self, *args, **kwargs):
        """For the closing record of an action that has already happened: a failure is logged, not hidden or raised."""
        try:
            self._audit(*args, **kwargs)
        except AuditError as exc:
            logger.warning("audit write failed after an action (%s)", exc)

    def note_secret_masked(self, who, categories, trace_id: str | None = None, source: str = "chat_message") -> None:
        """Audit that a secret was masked (categories only, never the value). Protective, so best effort: masking itself
        never depends on this record being written."""
        principal = self.resolve_principal(who)
        if principal is None or not categories:
            return
        self._audit_best_effort(principal, "secret.masked", source, "not_applicable", "blocked",
                                target=principal.student_id, trace_id=trace_id, details={"categories": sorted(categories)})

    def execute(self, name: str, arguments, principal, *, trace_id: str | None = None) -> dict:
        """Run a tool. Always returns a dict; failures come back as {"error": ...}.

        ``principal`` is the authenticated Principal (a student-id string is accepted only as the internal test path)."""
        if not isinstance(arguments, dict):
            return {"error": "Tool arguments must be an object."}
        action = TOOL_ACTIONS.get(name) if isinstance(name, str) else None
        if action is None:
            return {"error": f"Unknown tool '{name}'."}
        who = self.resolve_principal(principal)
        if who is None and isinstance(principal, str) and action is not Action.READ_PUBLIC:
            return tools._not_found(principal)       # internal string path with an unknown student: the old behaviour
        # The model may name a student for the two private-read tools. It is only ever compared, never used.
        requested = arguments.get("student_id") if name in PRIVATE_READ_TOOLS else None
        if who is None and isinstance(principal, str):
            decision = Decision(True, "internal_public_tool", name)       # internal string path, public tool (private ones returned above)
        else:
            decision = authorize(who, action, target_student_id=requested)
        if not decision.allowed:
            try:
                self._audit(who, "authz.denied", name, "denied", "blocked", target=_clean_id(requested), trace_id=trace_id,
                            details={"tool": name, "reason": decision.reason})
            except AuditError:
                logger.warning("audit write failed for a denied request")     # the request is refused either way
            return {"error": DENIED_MESSAGE, "denied": True, "reason": decision.reason}

        sid = who.student_id if who else None         # the principal's own id: the only id ever used for a lookup
        d, db, a = self.data_dir, self.db_path, arguments
        handlers = {
            "check_fee_status": lambda: tools.check_fee_status(d, sid),
            "check_scholarship_status": lambda: tools.check_scholarship_status(d, sid),
            "create_support_request": lambda: tools.create_support_request(
                d, db, sid, str(a.get("category", "")), str(a.get("description", ""))),
            "search_course_catalog": lambda: campus_tools.search_course_catalog(d, a.get("query"), a.get("department")),
            "get_professor_office_hours": lambda: campus_tools.get_professor_office_hours(d, a.get("professor_name")),
            "check_room_availability": lambda: campus_tools.check_room_availability(
                d, a.get("room_id"), a.get("day"), a.get("start_time"), a.get("end_time")),
            "get_campus_events": lambda: campus_tools.get_campus_events(d, a.get("category"), a.get("from_date")),
            "remember_preference": lambda: self._remember(sid, a.get("key"), a.get("value")),
            "search_handbook": lambda: rag.search_handbook(self.retriever, a.get("query")),
        }
        handlers["update_contact_details"] = lambda: self._request_contact_change(who, a, trace_id)
        handler = handlers[name]
        try:
            if name in WRITE_TOOLS:
                # no audit record, no write
                self._audit(who, "tool.write", name, "allowed", "pending", target=sid, trace_id=trace_id)
                result = handler()
                self._audit_best_effort(who, "tool.write", name, "allowed", _write_outcome(result), target=sid,
                                        trace_id=trace_id, details=_result_summary(result))
                return result
            result = handler()
            if name in PRIVATE_READ_TOOLS:
                # no audit record, no data
                self._audit(who, "tool.private_read", name, "allowed", "failure" if "error" in result else "success",
                            target=sid, trace_id=trace_id)
            return result
        except AuditError:
            return {"error": AUDIT_DOWN_MESSAGE, "audit_unavailable": True}
        except ApprovalError as exc:
            if exc.kind == "audit_unavailable":
                return {"error": AUDIT_DOWN_MESSAGE, "audit_unavailable": True}
            return {"error": str(exc) if exc.kind in ("invalid", "limit", "conflict") else DENIED_MESSAGE, "approval_error": exc.kind}
        except Exception as exc:  # the model and user only ever see this generic message, never exception text
            # Log the tool and the exception TYPE only: the message or a traceback could contain student data,
            # file paths or keys, and this log may be shared. The type is enough to start debugging.
            logger.warning("Tool %s failed unexpectedly (%s)", name, type(exc).__name__)
            return {"error": f"Tool '{name}' failed unexpectedly."}

    def _request_contact_change(self, who, args, trace_id):
        """Creates a PENDING approval. The student's data is not changed here, whatever the model says."""
        if self.approvals is None:
            return {"error": "Contact changes are not available right now."}
        if who is None or who.role != STUDENT:
            return {"error": DENIED_MESSAGE}
        approval = self.approvals.request(who, "update_contact_details",
                                          {"field": args.get("field"), "value": args.get("value")}, trace_id=trace_id)
        return {"status": "pending_approval", "approval_id": approval["approval_id"], "field": approval["field"],
                "expires_at": approval["expires_at"], "message": PENDING_MESSAGE}

    def _remember(self, sid, key, value):
        if self.memory is None:
            return {"error": "Memory is not available."}
        return self.memory.remember(sid, key, value)


def _clean_id(value):
    """A requested id for the audit record: a short string, never an arbitrary object."""
    return str(value)[:32] if isinstance(value, (str, int)) else None


def _write_outcome(result: dict) -> str:
    if "error" in result:
        return "failure"
    return "blocked" if result.get("saved") is False else "success"       # the memory validator may refuse a value


def _result_summary(result: dict) -> dict:
    """What the closing audit record keeps of a write result: outcome flags and ids, never the stored values."""
    return {k: result[k] for k in ("saved", "key", "ticket_id", "status", "category", "secret_redacted") if k in result}
