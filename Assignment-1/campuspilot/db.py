"""SQLite setup. Stores only non-sensitive data: memory, support requests, traces."""
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
