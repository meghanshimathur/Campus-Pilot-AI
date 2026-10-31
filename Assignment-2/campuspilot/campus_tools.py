"""Public campus-information tools (courses, office hours, rooms, events).
Like tools.py: return JSON-serialisable dicts, and {"error": ...} for bad input
or unknown records rather than raising."""
import json
import re
from datetime import date
from pathlib import Path

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
EVENT_CATEGORIES = {"career", "academic", "cultural", "technical", "sports"}
MAX_RESULTS = 10
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _load(data_dir, name):
    with open(Path(data_dir) / name, encoding="utf-8") as f:
        return json.load(f)


def _text(value, min_len=1, max_len=100):
    """Return a cleaned string or None if invalid."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if min_len <= len(value) <= max_len else None


def _norm_day(day):
    day = _text(day, 3, 9)
    if not day:
        return None
    match = [d for d in DAYS if d.lower() == day[:3].lower()]
    return match[0] if match else None


def search_course_catalog(data_dir, query: str, department: str | None = None) -> dict:
    q = _text(query, 2, 100)
    if not q:
        return {"error": "Please provide a search query of at least 2 characters."}
    courses = _load(data_dir, "courses.json")
    if department is not None:
        dept = _text(department)
        if not dept:
            return {"error": "Invalid department."}
        known = {c["department"].lower() for c in courses}
        if dept.lower() not in known:
            return {"error": f"Unknown department '{dept}'. Known departments: {sorted({c['department'] for c in courses})}."}
        courses = [c for c in courses if c["department"].lower() == dept.lower()]
    q = q.lower()
    hits = [c for c in courses
            if q in c["name"].lower() or q in c["course_id"].lower()
            or any(q in k for k in c["keywords"])]
    if not hits:
        return {"query": query, "count": 0, "courses": [], "message": "No matching courses found."}
    profs = {p["professor_id"]: p["name"] for p in _load(data_dir, "professors.json")}
    out = [{"course_id": c["course_id"], "name": c["name"], "department": c["department"],
            "credits": c["credits"], "term": c["term"], "schedule": c["schedule"],
            "professor": profs.get(c["professor_id"], "TBA")} for c in hits[:MAX_RESULTS]]
    return {"query": query, "count": len(hits), "courses": out}


def get_professor_office_hours(data_dir, professor_name: str) -> dict:
    name = _text(professor_name, 2, 100)
    if not name:
        return {"error": "Please provide the professor's name (at least 2 characters)."}
    n = name.lower()
    hits = [p for p in _load(data_dir, "professors.json") if n in p["name"].lower()]
    if not hits:
        return {"error": f"No professor found matching '{name}'."}
    return {"professors": [{"name": p["name"], "department": p["department"],
                            "office_room": p["office_room"], "office_hours": p["office_hours"]}
                           for p in hits]}


def check_room_availability(data_dir, room_id: str, day: str,
                            start_time: str | None = None, end_time: str | None = None) -> dict:
    rid = _text(room_id, 1, 20)
    if not rid:
        return {"error": "Please provide a room ID."}
    d = _norm_day(day)
    if not d:
        return {"error": f"Invalid day '{day}'. Use one of: {DAYS}."}
    if (start_time is None) != (end_time is None):
        return {"error": "Provide both start_time and end_time, or neither."}
    for t in (start_time, end_time):
        if t is not None and not (isinstance(t, str) and _TIME_RE.match(t)):
            return {"error": "Times must be in 24-hour HH:MM format."}
    if start_time is not None and start_time >= end_time:
        return {"error": "start_time must be earlier than end_time."}
    room = next((r for r in _load(data_dir, "rooms.json") if r["room_id"].lower() == rid.lower()), None)
    if not room:
        return {"error": f"Room '{room_id}' not found."}
    bookings = sorted((b for b in room["bookings"] if b["day"] == d), key=lambda b: b["start"])
    result = {"room_id": room["room_id"], "name": room["name"], "capacity": room["capacity"],
              "day": d, "open": room["open"], "close": room["close"], "bookings": bookings}
    if start_time is not None:
        within_hours = room["open"] <= start_time and end_time <= room["close"]
        conflicts = [b for b in bookings if b["start"] < end_time and start_time < b["end"]]
        result.update(requested={"start": start_time, "end": end_time},
                      available=within_hours and not conflicts, conflicts=conflicts,
                      within_opening_hours=within_hours)
    return result


def get_campus_events(data_dir, category: str | None = None, from_date: str | None = None) -> dict:
    if category is not None:
        cat = _text(category)
        if not cat or cat.lower() not in EVENT_CATEGORIES:
            return {"error": f"Invalid category. Use one of: {sorted(EVENT_CATEGORIES)}."}
        category = cat.lower()
    if from_date is None:
        start = date.today()
    else:
        try:
            start = date.fromisoformat(from_date) if isinstance(from_date, str) else None
        except ValueError:
            start = None
        if start is None:
            return {"error": "from_date must be a valid date in YYYY-MM-DD format."}
    events = [e for e in _load(data_dir, "events.json")
              if date.fromisoformat(e["date"]) >= start and (category is None or e["category"] == category)]
    events.sort(key=lambda e: (e["date"], e["time"]))
    if not events:
        return {"from_date": start.isoformat(), "count": 0, "events": [], "message": "No upcoming events found."}
    return {"from_date": start.isoformat(), "count": len(events), "events": events[:MAX_RESULTS]}
