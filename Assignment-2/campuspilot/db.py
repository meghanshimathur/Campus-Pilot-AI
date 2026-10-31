"""SQLite setup. Stores only non-sensitive data: memory, conversation history, support requests, traces, and (Assignment 2)
accounts (password hashes only), login sessions (token hashes only) and the audit log."""
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(student_id, key)
);
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS support_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT UNIQUE NOT NULL,
    student_id TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT DEFAULT 'open',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT UNIQUE NOT NULL,
    student_id TEXT,
    session_id TEXT NOT NULL,
    action TEXT,
    status TEXT,
    user_query TEXT NOT NULL,
    agent_response TEXT,
    tools_called TEXT,
    evidence_used TEXT,
    memories_used TEXT,
    total_latency_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    llm_provider TEXT,
    llm_model TEXT,
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Assignment 2 foundation (additive: CREATE ... IF NOT EXISTS, so existing databases and rows are left untouched).
-- Accounts. A student's user_id IS their student id. Only a salted scrypt hash of the password is stored (auth.py).
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('student', 'staff')),
    student_id TEXT,
    display_name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    created_at TEXT NOT NULL,
    CHECK ((role = 'student' AND student_id IS NOT NULL AND student_id = user_id)
           OR (role = 'staff' AND student_id IS NULL))
);
-- Login sessions. Only the SHA-256 of the bearer token is stored, never the token itself.
CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);
-- Append-only audit log with a hash chain (audit.py). The triggers make UPDATE and DELETE fail.
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_id TEXT UNIQUE NOT NULL,
    ts TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    event TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_student_id TEXT,
    decision TEXT NOT NULL,
    outcome TEXT NOT NULL,
    approval_id TEXT,
    trace_id TEXT,
    details TEXT NOT NULL DEFAULT '{}',
    prev_hash TEXT NOT NULL,
    entry_hash TEXT UNIQUE NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_id, id);
CREATE INDEX IF NOT EXISTS idx_audit_resource ON audit_log(resource_student_id, id);
CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_log(event, id);
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only: UPDATE is not allowed'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only: DELETE is not allowed'); END;

-- Assignment 2 sensitive-action approvals (approvals.py). The student record file (data/students.json) is read-only and has
-- no contact fields, so approved contact changes are kept in student_contacts; nothing outside this table is modified.
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    action TEXT NOT NULL,
    field TEXT NOT NULL,
    args_json TEXT NOT NULL,
    request_mac TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled', 'expired', 'executed', 'failed')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    decision_reason TEXT,
    decision_mac TEXT,
    executed_at TEXT,
    result TEXT,
    trace_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_approvals_student ON approvals(student_id, status);
CREATE TABLE IF NOT EXISTS student_contacts (
    student_id TEXT PRIMARY KEY,
    email TEXT,
    phone TEXT,
    updated_at TEXT NOT NULL,
    approval_id TEXT
);
"""


def connect(db_path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# Columns added after a table first shipped: {table: {column: DDL type}}. init_db() adds any that are missing so a
# database created by an earlier phase keeps working; new databases already get them from SCHEMA.
_MIGRATIONS = {
    "conversations": {"student_id": "TEXT NOT NULL DEFAULT ''"},
    "traces": {"student_id": "TEXT", "action": "TEXT", "status": "TEXT"},
}


def init_db(db_path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        for table, columns in _MIGRATIONS.items():  # upgrade databases created by earlier phases
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for column, ddl in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_lookup ON conversations(student_id, session_id, id)")
        conn.commit()
    finally:
        conn.close()
