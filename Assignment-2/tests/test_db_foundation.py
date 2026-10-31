"""Schema additions for Assignment 2 and the safety of the migration (additive, existing data untouched)."""
import sqlite3

from campuspilot.db import connect, init_db

NEW_TABLES = {"users", "auth_sessions", "audit_log", "approvals", "student_contacts"}
OLD_TABLES = {"memories", "conversations", "support_requests", "traces"}


def _tables(conn):
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _cols(conn, table):
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def test_fresh_database_has_all_old_and_new_tables(settings):
    conn = connect(settings.database_path)
    assert _tables(conn) == OLD_TABLES | NEW_TABLES
    assert _cols(conn, "users") == ["user_id", "role", "student_id", "display_name", "password_hash", "failed_attempts",
                                    "locked_until", "created_at"]
    assert _cols(conn, "auth_sessions") == ["token_hash", "user_id", "created_at", "expires_at", "last_used_at", "revoked_at"]
    assert _cols(conn, "audit_log") == ["id", "audit_id", "ts", "actor_id", "actor_role", "event", "action",
                                        "resource_student_id", "decision", "outcome", "approval_id", "trace_id", "details",
                                        "prev_hash", "entry_hash"]
    conn.close()


def test_no_table_stores_a_plaintext_secret_column(settings):
    conn = connect(settings.database_path)
    forbidden = {"password", "passwd", "token", "secret", "otp", "pin"}
    for table in NEW_TABLES:
        assert not forbidden & set(_cols(conn, table)), table      # only password_hash / token_hash exist
    conn.close()


def test_init_db_is_idempotent_and_keeps_rows_and_triggers(settings):
    conn = connect(settings.database_path)
    conn.execute("INSERT INTO memories (student_id, key, value) VALUES ('STU001','preferred_name','Dev')")
    conn.commit(); conn.close()
    init_db(settings.database_path); init_db(settings.database_path)
    conn = connect(settings.database_path)
    assert conn.execute("SELECT value FROM memories").fetchone()["value"] == "Dev"
    triggers = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert triggers == {"audit_log_no_update", "audit_log_no_delete"}
    conn.close()


def test_an_existing_assignment_1_database_is_upgraded_without_touching_its_data(tmp_path):
    """A database as Assignment 1 left it (all four tables, real-looking rows) gains the new tables only."""
    db = tmp_path / "a1.sqlite3"
    init_db(db)
    conn = sqlite3.connect(db)
    for table in NEW_TABLES:                                           # rewind to the Assignment 1 shape
        conn.execute(f"DROP TABLE {table}")
    conn.execute("INSERT INTO memories (student_id, key, value) VALUES ('STU001','preferred_name','Dev')")
    conn.execute("INSERT INTO conversations (student_id, session_id, role, content) VALUES ('STU001','s','user','hi')")
    conn.execute("INSERT INTO support_requests (ticket_id, student_id, category, description) VALUES ('TKT-1','STU001','fees','x')")
    conn.execute("INSERT INTO traces (trace_id, student_id, session_id, user_query) VALUES ('t1','STU001','s','q')")
    conn.commit()
    assert _tables_raw(conn) == OLD_TABLES
    before = {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in OLD_TABLES}
    conn.close()
    init_db(db); init_db(db)
    conn = connect(db)
    assert _tables(conn) == OLD_TABLES | NEW_TABLES
    after = {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t}")] for t in OLD_TABLES}
    assert {t: [tuple(r) for r in rows] for t, rows in before.items()} == after
    conn.close()


def _tables_raw(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def test_the_old_tables_keep_exactly_their_assignment_1_columns(settings):
    conn = connect(settings.database_path)
    assert _cols(conn, "memories") == ["id", "student_id", "key", "value", "created_at"]
    assert _cols(conn, "conversations") == ["id", "student_id", "session_id", "role", "content", "timestamp"]
    assert _cols(conn, "support_requests") == ["id", "ticket_id", "student_id", "category", "description", "status", "created_at"]
    assert "action" in _cols(conn, "traces") and "student_id" in _cols(conn, "traces")
    conn.close()
