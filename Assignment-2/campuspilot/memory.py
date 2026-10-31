"""Persistent, non-sensitive student memory and conversation history (SQLite).

Deliberately independent of handbook retrieval (rag.py). Safety is enforced in code:
values are screened by deterministic rules (campuspilot/secrets_guard.py), never by trusting the LLM.
"""
import re

from campuspilot.db import connect
from campuspilot.secrets_guard import REDACTED_MESSAGE, scrub_for_storage, screen_sensitive  # one shared definition of 'sensitive'

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
REDACTED = REDACTED_MESSAGE

_FLAGS = re.IGNORECASE
# Only applied to values proposed for long-term memory.
_NOT_LONG_TERM = [
    ("temporary", re.compile(r"\b(today|tonight|tomorrow|yesterday|right now|at the moment|currently|this (?:week|month|"
                             r"morning|evening)|next (?:week|month)|just now|for now)\b", _FLAGS)),
    ("instruction_like", re.compile(r"\b(ignore|disregard|system prompt|instructions?|you must|you should|pretend)\b", _FLAGS)),
]


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


def redact_for_history(text: str, *, expect_secret: bool = False) -> str:
    """The text that may be written to history and traces: secret values (passwords, OTPs, keys, card and id numbers) are
    masked IN PLACE and the rest of the message is kept; text that still holds personal data (email, amount, health term,
    long number) is replaced whole, as before. The shared definition lives in secrets_guard.scrub_for_storage."""
    return scrub_for_storage(text, expect_secret=expect_secret, max_chars=MAX_HISTORY_CHARS)


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
