"""Tool schemas and safe dispatch.

The student_id is injected from the request context, never taken from the LLM,
so the model cannot look up or modify another student's records.
"""
import logging

from campuspilot import campus_tools, memory as memory_mod, rag, tools

logger = logging.getLogger("campuspilot.tools")

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
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "check_scholarship_status",
        "description": "Get the current student's scholarship name, amount and status.",
        "parameters": {"type": "object", "properties": {}, "required": []},
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
    def __init__(self, data_dir, db_path, retriever=None, memory=None):
        self.data_dir = data_dir
        self.db_path = db_path
        self.retriever = retriever
        self.memory = memory
        self.schemas = TOOL_SCHEMAS

    def canonical_student_id(self, student_id: str) -> str | None:
        """Normalised ID (e.g. 'stu001' -> 'STU001') or None if the student does not exist."""
        student = tools._find_student(self.data_dir, student_id)
        return student["student_id"] if student else None

    def _remember(self, student_id, key, value):
        if self.memory is None:
            return {"error": "Memory is not available."}
        sid = self.canonical_student_id(student_id)
        if sid is None:
            return tools._not_found(student_id)  # same wording as the other student-record tools
        return self.memory.remember(sid, key, value)

    def execute(self, name: str, arguments, student_id: str) -> dict:
        """Run a tool. Always returns a dict; failures come back as {"error": ...}."""
        if not isinstance(arguments, dict):
            return {"error": "Tool arguments must be an object."}
        # Handlers resolve tools.* / campus_tools.* at call time (keeps them patchable in tests).
        d, db, a = self.data_dir, self.db_path, arguments
        handlers = {
            "check_fee_status": lambda: tools.check_fee_status(d, student_id),
            "check_scholarship_status": lambda: tools.check_scholarship_status(d, student_id),
            "create_support_request": lambda: tools.create_support_request(
                d, db, student_id, str(a.get("category", "")), str(a.get("description", ""))),
            "search_course_catalog": lambda: campus_tools.search_course_catalog(
                d, a.get("query"), a.get("department")),
            "get_professor_office_hours": lambda: campus_tools.get_professor_office_hours(
                d, a.get("professor_name")),
            "check_room_availability": lambda: campus_tools.check_room_availability(
                d, a.get("room_id"), a.get("day"), a.get("start_time"), a.get("end_time")),
            "get_campus_events": lambda: campus_tools.get_campus_events(
                d, a.get("category"), a.get("from_date")),
            "remember_preference": lambda: self._remember(student_id, a.get("key"), a.get("value")),
            "search_handbook": lambda: rag.search_handbook(self.retriever, a.get("query")),
        }
        handler = handlers.get(name) if isinstance(name, str) else None
        if handler is None:
            return {"error": f"Unknown tool '{name}'."}
        try:
            return handler()
        except Exception as exc:  # the model and user only ever see this generic message, never exception text
            # Log the tool and the exception TYPE only: the message or a traceback could contain student data,
            # file paths or keys, and this log may be shared. The type is enough to start debugging.
            logger.warning("Tool %s failed unexpectedly (%s)", name, type(exc).__name__)
            return {"error": f"Tool '{name}' failed unexpectedly."}
