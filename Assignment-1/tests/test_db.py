from campuspilot.db import connect


def test_tables_created(settings):
    conn = connect(settings.database_path)
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"memories", "conversations", "support_requests", "traces"} <= names
