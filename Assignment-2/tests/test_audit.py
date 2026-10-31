"""Append-only audit log with integrity verification (Assignment 2 foundation)."""
import json
import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from campuspilot.audit import GENESIS, AuditError, AuditStore
from campuspilot.authz import STAFF, STUDENT, Principal

S1 = Principal("STU001", STUDENT, "STU001")
STAFF1 = Principal("STAFF01", STAFF)


@pytest.fixture
def audit(settings):
    return AuditStore(settings.database_path)


def _raw(settings):
    return sqlite3.connect(settings.database_path)


def _add(audit, n=3):
    return [audit.record("tool.invoked", actor=S1, action=f"check_fee_status_{i}", decision="allowed", outcome="success",
                         resource_student_id="STU001", trace_id=f"t{i}", details={"i": i}) for i in range(n)]


# ---------------------------------------------------------------- what a record says
def test_a_record_answers_who_what_decision_and_result(audit):
    aid = audit.record("authz.decision", actor=S1, action="read_own_private", decision="denied", outcome="blocked",
                       resource_student_id="STU002", trace_id="abc123", approval_id="APR-1",
                       details={"tool": "check_fee_status", "reason": "cross_student_access"})
    (rec,) = audit.list_events()
    assert rec["audit_id"] == aid and rec["audit_id"].startswith("AUD-")
    assert (rec["actor_id"], rec["actor_role"]) == ("STU001", "student")                  # who requested
    assert (rec["event"], rec["action"], rec["resource_student_id"]) == ("authz.decision", "read_own_private", "STU002")  # what
    assert (rec["decision"], rec["outcome"]) == ("denied", "blocked")                     # allowed/approved? and result
    assert rec["trace_id"] == "abc123" and rec["approval_id"] == "APR-1"
    assert rec["details"] == {"tool": "check_fee_status", "reason": "cross_student_access"}
    assert rec["ts"].endswith("Z") and len(rec["entry_hash"]) == 64


def test_non_principal_actors_are_supported_for_unauthenticated_attempts(audit):
    audit.record("auth.login", actor=("anonymous", "anonymous"), action="login", decision="denied", outcome="failure",
                 details={"reason": "bad_credentials"})
    assert audit.list_events()[0]["actor_role"] == "anonymous"


@pytest.mark.parametrize("kwargs", [
    dict(decision="maybe", outcome="success"), dict(decision="allowed", outcome="great"),
    dict(decision="allowed", outcome="success", actor=("x", "root")),
])
def test_values_outside_the_allowed_sets_are_rejected_and_nothing_is_written(audit, kwargs):
    base = dict(actor=S1, action="a", decision="allowed", outcome="success")
    base.update(kwargs)
    with pytest.raises(ValueError):
        audit.record("e", **base)
    assert audit.list_events() == []


def test_event_and_action_are_required(audit):
    with pytest.raises(ValueError):
        audit.record("", actor=S1, action="a", decision="allowed", outcome="success")
    with pytest.raises(ValueError):
        audit.record("e", actor=S1, action="", decision="allowed", outcome="success")


# ---------------------------------------------------------------- secrets never reach the log
def test_secrets_in_details_are_masked_before_they_are_written(audit, settings):
    audit.record("memory.write", actor=S1, action="remember", decision="denied", outcome="blocked",
                 details={"attempt": "my otp is 48291350", "password": "hunter2", "card": "4111 1111 1111 1111",
                          "email": "someone@example.com", "field": "email"})
    (rec,) = audit.list_events()
    blob = json.dumps(rec)
    # values a random hex hash cannot contain by chance (letters beyond f, a space, or 8+ digits): the scan is deterministic
    for secret in ("48291350", "hunter2", "4111 1111", "someone@example.com"):
        assert secret not in blob
    assert rec["details"]["field"] == "email"                                 # what was attempted is still recorded
    on_disk = "\n".join(_raw(settings).iterdump())
    assert "48291350" not in on_disk and "hunter2" not in on_disk and "someone@example.com" not in on_disk


def test_huge_details_do_not_bloat_the_log(audit):
    audit.record("e", actor=S1, action="a", decision="allowed", outcome="success", details={"k": ["x" * 5000] * 50})
    assert len(json.dumps(audit.list_events()[0]["details"])) <= 2000


# ---------------------------------------------------------------- append-only
def test_update_and_delete_are_refused_by_the_database(audit, settings):
    _add(audit)
    conn = _raw(settings)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE audit_log SET outcome='success', decision='allowed'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM audit_log WHERE id=1")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM audit_log")
    conn.close()
    assert len(audit.list_events()) == 3 and audit.verify_chain().ok


def test_the_store_offers_no_way_to_change_or_remove_records():
    public = [n for n in dir(AuditStore) if not n.startswith("_")]
    assert sorted(public) == ["head", "list_events", "record", "verify_chain"]


# ---------------------------------------------------------------- integrity chain
def test_an_empty_log_is_valid_and_its_head_is_the_genesis_value(audit):
    report = audit.verify_chain()
    assert report.ok and report.checked == 0 and audit.head() == GENESIS


def test_each_record_links_to_the_previous_one_and_the_head_moves(audit):
    heads = [audit.head()]
    for _ in range(3):
        _add(audit, 1)
        heads.append(audit.head())
    assert len(set(heads)) == 4
    rows = audit.list_events()
    assert rows[0]["prev_hash"] == GENESIS and rows[1]["prev_hash"] == rows[0]["entry_hash"] \
        and rows[2]["prev_hash"] == rows[1]["entry_hash"] and audit.head() == rows[2]["entry_hash"]
    assert audit.verify_chain().ok and audit.verify_chain().checked == 3


def _tamper(settings, sql, *args):
    """Simulate someone with write access to the database FILE: drop the protection, edit, put it back."""
    conn = _raw(settings)
    conn.executescript("DROP TRIGGER audit_log_no_update; DROP TRIGGER audit_log_no_delete;")
    conn.execute(sql, args)
    conn.commit(); conn.close()


def test_editing_a_record_is_detected(audit, settings):
    _add(audit, 4)
    before = audit.list_events()[1]
    _tamper(settings, "UPDATE audit_log SET decision='denied', outcome='blocked' WHERE id=2")
    assert (before["decision"], before["outcome"]) == ("allowed", "success")      # the edit really changes the record
    assert audit.list_events()[1]["decision"] == "denied"
    report = audit.verify_chain()
    assert not report.ok and report.first_bad_id == 2 and "content" in report.reason


def test_removing_a_record_from_the_middle_is_detected(audit, settings):
    _add(audit, 4)
    _tamper(settings, "DELETE FROM audit_log WHERE id=2")
    report = audit.verify_chain()
    assert not report.ok and report.first_bad_id == 3


def test_rewriting_a_link_is_detected(audit, settings):
    _add(audit, 3)
    _tamper(settings, "UPDATE audit_log SET prev_hash=? WHERE id=3", "f" * 64)
    assert not audit.verify_chain().ok


def test_reordering_is_detected(audit, settings):
    _add(audit, 3)
    _tamper(settings, "UPDATE audit_log SET id = 99 WHERE id = 1")
    assert not audit.verify_chain().ok


def test_documented_limit_deleting_the_newest_records_leaves_a_valid_shorter_chain(audit, settings):
    """Stated, not hidden: a shortened chain is still internally consistent. Compare head() with a copy kept elsewhere."""
    _add(audit, 3)
    anchored_head = audit.head()
    _tamper(settings, "DELETE FROM audit_log WHERE id=3")
    assert audit.verify_chain().ok
    assert audit.head() != anchored_head                                          # ...which the anchored head reveals


def test_concurrent_writers_keep_a_single_valid_chain(settings):
    store = AuditStore(settings.database_path)
    errors = []

    def worker(n):
        try:
            for i in range(20):
                store.record("tool.invoked", actor=S1, action=f"a{n}-{i}", decision="allowed", outcome="success")
        except Exception as exc:                                                 # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    report = store.verify_chain()
    assert report.ok and report.checked == 160
    assert len({r["audit_id"] for r in store.list_events(limit=500)}) == 160


# ---------------------------------------------------------------- querying
def test_list_events_filters_orders_and_limits(audit):
    audit.record("a", actor=S1, action="x", decision="allowed", outcome="success", resource_student_id="STU001", trace_id="t1")
    audit.record("b", actor=STAFF1, action="y", decision="approved", outcome="success", resource_student_id="STU002", trace_id="t2")
    audit.record("a", actor=S1, action="z", decision="denied", outcome="blocked", resource_student_id="STU002", trace_id="t3")
    assert [r["action"] for r in audit.list_events(actor_id="STU001")] == ["x", "z"]
    assert [r["action"] for r in audit.list_events(resource_student_id="STU002")] == ["y", "z"]
    assert [r["action"] for r in audit.list_events(event="a", newest_first=True)] == ["z", "x"]
    assert [r["action"] for r in audit.list_events(trace_id="t2")] == ["y"]
    assert len(audit.list_events(limit=1)) == 1 and len(audit.list_events(limit=0)) == 1
    assert audit.list_events(actor_id="NOBODY") == []


def test_filter_values_are_bound_parameters_not_sql(audit):
    _add(audit, 2)
    assert audit.list_events(actor_id="STU001' OR '1'='1") == []
    assert len(audit.list_events()) == 2


def test_a_failed_write_raises_audit_error_without_leaking_details(settings):
    store = AuditStore(settings.database_path)
    conn = _raw(settings)
    conn.execute("DROP TABLE audit_log"); conn.commit(); conn.close()
    with pytest.raises(AuditError) as err:
        store.record("e", actor=S1, action="a", decision="allowed", outcome="success", details={"password": "hunter2"})
    assert "hunter2" not in str(err.value)


def test_the_injected_clock_controls_timestamps(settings):
    fixed = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    store = AuditStore(settings.database_path, clock=lambda: fixed)
    store.record("e", actor=S1, action="a", decision="allowed", outcome="success")
    assert store.list_events()[0]["ts"].startswith("2026-11-01T12:00:00")


# ---------------------------------------------------------------- id columns are never free text
@pytest.mark.parametrize("hostile,secret", [
    ("otp is 90817265", "90817265"), ("pwd=hunter2xyz", "hunter2xyz"), ("AIzaSyA1234567890abcdefghij", "AIzaSyA1234567890abcdefghij"),
    ("my card 4111 1111 1111 1111", "4111 1111"), ("someone@example.com", "someone@example.com"),
])
def test_secrets_or_personal_values_in_the_id_columns_are_not_stored(audit, settings, hostile, secret):
    audit.record("authz.denied", actor=S1, action="read_own_private", decision="denied", outcome="blocked",
                 resource_student_id=hostile, trace_id=hostile, approval_id=hostile)
    (rec,) = audit.list_events()
    assert secret not in json.dumps(rec) and secret not in "\n".join(_raw(settings).iterdump())
    assert audit.verify_chain().ok


def test_id_columns_keep_real_ids_and_mark_anything_else(audit):
    audit.record("e", actor=S1, action="a", decision="allowed", outcome="success", resource_student_id="STU002",
                 trace_id="0a1b2c3d" * 4, approval_id="APR-12AB")
    audit.record("e", actor=S1, action="a", decision="allowed", outcome="success",
                 resource_student_id="a very long free text sentence with spaces", trace_id="not an id; free text!")
    first, second = audit.list_events()
    assert (first["resource_student_id"], first["approval_id"]) == ("STU002", "APR-12AB") and first["trace_id"] == "0a1b2c3d" * 4
    assert second["resource_student_id"] == "[invalid id]" and second["trace_id"] == "[invalid id]"


def test_a_real_trace_id_is_kept_exactly_so_audit_and_trace_stay_linked(audit):
    import uuid
    trace_id = uuid.uuid4().hex                        # what the agent assigns; token-shaped, but known to be safe
    audit.record("e", actor=S1, action="a", decision="allowed", outcome="success", trace_id=trace_id)
    assert audit.list_events(trace_id=trace_id)[0]["trace_id"] == trace_id


def test_student_ids_are_canonicalised_but_other_text_is_not_kept(audit):
    for raw in ("stu002", " STU002 "):
        audit.record("e", actor=S1, action="a", decision="denied", outcome="blocked", resource_student_id=raw)
    assert {e["resource_student_id"] for e in audit.list_events()} == {"STU002"}


def test_an_approval_id_made_only_of_digits_is_not_mistaken_for_a_number(settings):
    """APR- ids are random hex, so about one in 1,700 is all digits; that must not be masked and break the link."""
    audit = AuditStore(settings.database_path)
    for approval_id in ("APR-123456789012", "APR-000000000000", "APR-4F2A9C01B7D3"):
        audit.record("approval.requested", actor=("STU001", "student"), action="update_contact_details",
                     decision="approval_required", outcome="pending", approval_id=approval_id)
    assert [e["approval_id"] for e in audit.list_events(limit=10)] == ["APR-123456789012", "APR-000000000000", "APR-4F2A9C01B7D3"]
    audit.record("x.test", actor=("STU001", "student"), action="a", decision="allowed", outcome="success",
                 approval_id="otp is 4821")
    assert "4821" not in audit.list_events(limit=10, newest_first=True)[0]["approval_id"]      # free text is still masked
