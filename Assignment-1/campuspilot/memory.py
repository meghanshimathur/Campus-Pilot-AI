"""Persistent, non-sensitive student memory and conversation history (SQLite).

Deliberately independent of handbook retrieval (rag.py). Safety is enforced in code:
values are screened by deterministic rules, never by trusting the LLM.
"""
import re

from campuspilot.db import connect

# Closed allowlist: the only things the agent may remember across sessions.
ALLOWED_KEYS = {
    "preferred_name": "How the student likes to be addressed",
    "preferred_language": "Preferred language for replies",
    "preferred_contact_channel": "Preferred way to be contacted (e.g. email, portal)",
    "study_preference": "A lasting study preference (e.g. morning sessions)",
    "interested_topic": "A topic the student is interested in",
}
MAX_VALUE_CHARS = 100
MAX_HISTORY_CHARS = 2000
REDACTED = "[redacted: message may contain sensitive information]"

_FLAGS = re.IGNORECASE
# Credentials, payment/bank details, identity numbers, contact details, health, money amounts.
# Deliberately conservative: a false positive (a harmless message redacted or a preference refused) is accepted
# in exchange for never persisting something sensitive. Matching is by pattern, not by trusting the model.
_SENSITIVE = [
    ("credentials", re.compile(r"\b(password|passcode|passwd|otp|pin|cvv|cvc|secret|api[\s_-]?key|token)\b", _FLAGS)),
    ("payment_details", re.compile(r"\b(card|credit|debit|upi|ifsc|bank|account)\b.{0,20}\b(number|no|details|ending|id)\b"
                                   r"|\b(bank account|credit card|debit card|net ?banking)\b", _FLAGS)),
    ("identity_number", re.compile(r"\b(aadhaar|aadhar|passport|ssn|pan card|national id)\b", _FLAGS)),
    ("long_number", re.compile(r"\d(?:[\s-]?\d){5,}")),  # 6+ digits: card, account, phone, OTP
    ("contact_details", re.compile(r"\S+@\S+\.\S+")),
    ("health", re.compile(r"\b(health|medical|medicine|medication|diagnos\w*|illness|disease|depress\w*|anxiety|"
                          r"therapy|therapist|surgery|pregnan\w*|disabilit\w*|mental|doctor|hospital|injur\w*|"
                          r"sick|cancer|diabet\w*|asthma)\b", _FLAGS)),
    ("money_amount", re.compile(r"(?:\brs\.?|\binr|₹|\$|€|£)\s?\d|\b\d[\d,]*\s?(?:rupees|rs\b|inr\b|usd\b)"
                                r"|\b(salary|income|loan|debt)\b", _FLAGS)),
]
# Only applied to values proposed for long-term memory.
_NOT_LONG_TERM = [
    ("temporary", re.compile(r"\b(today|tonight|tomorrow|yesterday|right now|at the moment|currently|this (?:week|month|"
                             r"morning|evening)|next (?:week|month)|just now|for now)\b", _FLAGS)),
    ("instruction_like", re.compile(r"\b(ignore|disregard|system prompt|instructions?|you must|you should|pretend)\b", _FLAGS)),
]


def screen_sensitive(text: str) -> str | None:
    """Return the category if the text looks sensitive, else None."""
    for name, pattern in _SENSITIVE:
        if pattern.search(text):
            return name
    return None


def validate_memory(key, value) -> tuple[bool, str]:
    """(ok, message). Messages never echo the rejected value."""
    if key not in ALLOWED_KEYS:
        return False, f"Cannot remember '{key}'. Allowed keys: {sorted(ALLOWED_KEYS)}."
    if not isinstance(value, str) or not value.strip():
        return False, "A non-empty text value is required."
    value = value.strip()
    if len(value) > MAX_VALUE_CHARS:
        return False, f"Value is too long (max {MAX_VALUE_CHARS} characters)."
    if (reason := screen_sensitive(value)):
        return False, f"Not saved: this looks like sensitive information ({reason}) and is never stored."
    for name, pattern in _NOT_LONG_TERM:
        if pattern.search(value):
            return False, f"Not saved: this looks temporary or unsuitable for long-term memory ({name})."
    return True, "ok"


def redact_for_history(text: str) -> str:
    """Whole-message redaction if it looks sensitive; otherwise truncated text.
    The entire message is replaced (not just the matching part) because a partial mask could leave context
    that still identifies the value. Also used for trace text, so history and traces follow one rule."""
    text = (text or "").strip()
    if screen_sensitive(text):
        return REDACTED
    return text[:MAX_HISTORY_CHARS]


class MemoryStore:
    def __init__(self, db_path):
        self.db_path = db_path

    # ---- long-term memory (per student, across sessions) ----
    def remember(self, student_id: str, key: str, value: str) -> dict:
        ok, message = validate_memory(key, value)
        if not ok:
            return {"saved": False, "message": message}
        conn = connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO memories (student_id, key, value) VALUES (?,?,?) "
                "ON CONFLICT(student_id, key) DO UPDATE SET value=excluded.value, created_at=CURRENT_TIMESTAMP",
                (student_id, key, value.strip()))
            conn.commit()
        finally:
            conn.close()
        return {"saved": True, "key": key, "value": value.strip()}

    def recall(self, student_id: str) -> list[dict]:
        conn = connect(self.db_path)
        try:
            rows = conn.execute("SELECT key, value, created_at FROM memories WHERE student_id=? ORDER BY key",
                                (student_id,)).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows if r["key"] in ALLOWED_KEYS]

    def forget(self, student_id: str, key: str) -> bool:
        conn = connect(self.db_path)
        try:
            cur = conn.execute("DELETE FROM memories WHERE student_id=? AND key=?", (student_id, key))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    # ---- conversation history (scoped to student + session) ----
    def save_message(self, student_id: str, session_id: str, role: str, content: str) -> None:
        if role not in ("user", "assistant"):
            raise ValueError("role must be 'user' or 'assistant'")
        conn = connect(self.db_path)
        try:
            conn.execute("INSERT INTO conversations (student_id, session_id, role, content) VALUES (?,?,?,?)",
                         (student_id, session_id, role, redact_for_history(content)))
            conn.commit()
        finally:
            conn.close()

    def recent_messages(self, student_id: str, session_id: str, limit: int = 6) -> list[dict]:
        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT role, content FROM conversations WHERE student_id=? AND session_id=? "
                "ORDER BY id DESC LIMIT ?", (student_id, session_id, limit)).fetchall()
        finally:
            conn.close()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
