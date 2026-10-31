"""Approval state machine (Assignment 2, Batch 4). A MOCK executor stands in for the real change, so these tests prove
WHEN a change would run, not what it does. Temporary databases, fictional students, no network."""
import json
import os
import sqlite3
import stat
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from auth_helpers import STAFF_ID, db_dump, db_one, principal, seed_users
from campuspilot.approvals import ApprovalError, ApprovalService, load_or_create_key
from campuspilot.audit import AuditError, AuditStore
from campuspilot.authz import STAFF, Principal

KEY = b"k" * 32
NEW_EMAIL = "fresh.address@example.edu"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class Mock:
    """Counts executions; the real executor is not involved."""

    def __init__(self):
        self.calls = []

    def __call__(self, conn, row, args, now):
        self.calls.append((row["approval_id"], args))
        return {"field": row["field"]}


class FlakyAudit(AuditStore):
    """Fails for the named events, like a full disk or a locked file would."""

    def __init__(self, db_path, fail_events=()):
        super().__init__(db_path)
        self.fail_events = set(fail_events)

    def record(self, event, **kwargs):
        if event in self.fail_events:
            raise AuditError("audit store is down")
        return super().record(event, **kwargs)


@pytest.fixture
def env(settings):
    auth = seed_users(settings)
    clock, mock = Clock(), Mock()
    audit = FlakyAudit(settings.database_path)
    svc = ApprovalService(settings.database_path, auth=auth, audit=audit, signing_key=KEY,
                          executors={"update_contact_details": mock}, clock=clock)
    return svc, mock, clock, audit, settings


def student(settings, sid="STU001"):
    return principal(settings, sid)


def staff(settings):
    return principal(settings, STAFF_ID)


def ask(svc, settings, sid="STU001", field="email", value=NEW_EMAIL):
    return svc.request(student(settings, sid), "update_contact_details", {"field": field, "value": value})


def status_of(settings, approval_id):
    return db_one(settings.database_path, "SELECT status FROM approvals WHERE approval_id=?", approval_id)[0]


def tamper(settings, sql, *args):
    with closing(sqlite3.connect(settings.database_path)) as conn:
        conn.execute(sql, args)
        conn.commit()


# ------------------------------------------------------------------ request
def test_a_request_creates_a_pending_approval_and_executes_nothing(env):
    svc, mock, _, _, settings = env
    approval = ask(svc, settings)
    assert approval["status"] == "pending" and approval["approval_id"].startswith("APR-")
    assert approval["proposed_value"] == NEW_EMAIL            # the owner sees their own request
    assert mock.calls == []
    assert db_one(settings.database_path, "SELECT COUNT(*) FROM student_contacts")[0] == 0


def test_the_request_is_audited_before_it_is_stored(env):
    svc, _, _, _, settings = env
    svc.audit = FlakyAudit(settings.database_path, fail_events={"approval.requested"})
    with pytest.raises(ApprovalError) as err:
        ask(svc, settings)
    assert err.value.kind == "audit_unavailable"
    assert db_one(settings.database_path, "SELECT COUNT(*) FROM approvals")[0] == 0


def test_staff_cannot_request_a_contact_change(env):
    svc, _, _, _, settings = env
    with pytest.raises(ApprovalError) as err:
        svc.request(staff(settings), "update_contact_details", {"field": "email", "value": NEW_EMAIL})
    assert err.value.kind == "forbidden"


@pytest.mark.parametrize("args", [{"field": "address", "value": "x"}, {"field": "email", "value": "not-an-email"},
                                  {"field": "phone", "value": "12"}, {"field": "email"}, "email=a@b.co", None])
def test_invalid_requests_are_refused_without_echoing_the_value(env, args):
    svc, _, _, _, settings = env
    with pytest.raises(ApprovalError) as err:
        svc.request(student(settings), "update_contact_details", args)
    assert err.value.kind == "invalid" and "not-an-email" not in str(err.value)
    assert db_one(settings.database_path, "SELECT COUNT(*) FROM approvals")[0] == 0


def test_one_pending_request_per_field(env):
    svc, _, _, _, settings = env
    ask(svc, settings)
    with pytest.raises(ApprovalError) as err:
        ask(svc, settings, value="other@example.edu")
    assert err.value.kind == "conflict"
    ask(svc, settings, field="phone", value="+91 98765 43210")        # a different field is fine


# ------------------------------------------------------------------ who may decide
def test_a_student_cannot_approve_or_reject_their_own_request(env):
    svc, mock, _, _, settings = env
    a = ask(svc, settings)
    for approve in (True, False):
        with pytest.raises(ApprovalError) as err:
            svc.decide(a["approval_id"], student(settings), approve=approve)
        assert err.value.kind == "forbidden"
    assert status_of(settings, a["approval_id"]) == "pending" and mock.calls == []


def test_another_student_cannot_decide_and_cannot_even_see_the_request(env):
    svc, mock, _, _, settings = env
    a = ask(svc, settings)
    other = student(settings, "STU002")
    with pytest.raises(ApprovalError) as err:
        svc.decide(a["approval_id"], other, approve=True)
    assert err.value.kind == "forbidden"
    for call in (lambda: svc.get(other, a["approval_id"]), lambda: svc.cancel(other, a["approval_id"])):
        with pytest.raises(ApprovalError) as err:
            call()
        assert err.value.kind == "not_found"                           # same answer as a missing id
    assert svc.list_for(other) == [] and mock.calls == []


@pytest.mark.parametrize("who", [None, "STU001", ("anonymous", "anonymous")])
def test_unauthenticated_or_malformed_callers_cannot_decide(env, who):
    svc, mock, _, _, settings = env
    a = ask(svc, settings)
    with pytest.raises(ApprovalError) as err:
        svc.decide(a["approval_id"], who, approve=True)
    assert err.value.kind == "forbidden" and mock.calls == []


def test_a_student_learns_nothing_about_which_ids_exist(env):
    svc, _, _, _, settings = env
    a = ask(svc, settings)
    with pytest.raises(ApprovalError) as e1:
        svc.decide(a["approval_id"], student(settings, "STU002"), approve=True)
    with pytest.raises(ApprovalError) as e2:
        svc.decide("APR-000000000000", student(settings, "STU002"), approve=True)
    assert e1.value.kind == e2.value.kind == "forbidden"


def test_the_decision_is_audited_with_who_and_what(env):
    svc, mock, _, audit, settings = env
    a = ask(svc, settings)
    svc.decide(a["approval_id"], staff(settings), approve=True)
    events = audit.list_events(limit=100)
    by_event = {e["event"]: e for e in events if e["approval_id"] == a["approval_id"]}
    assert by_event["approval.requested"]["actor_id"] == "STU001" and by_event["approval.requested"]["decision"] == "approval_required"
    assert by_event["approval.decision"]["actor_id"] == STAFF_ID and by_event["approval.decision"]["decision"] == "approved"
    assert by_event["approval.execute"]["outcome"] in ("pending", "success")
    assert audit.verify_chain().ok
    assert NEW_EMAIL not in json.dumps(events)                             # the value never reaches the audit log


# ------------------------------------------------------------------ approve / reject
def test_staff_can_approve_and_the_change_runs_exactly_once(env):
    svc, mock, _, _, settings = env
    a = ask(svc, settings)
    out = svc.decide(a["approval_id"], staff(settings), approve=True, reason="verified by phone")
    assert out["executed"] is True and out["approval"]["status"] == "executed"
    assert out["approval"]["decided_by"] == STAFF_ID and out["approval"]["decision_reason"] == "verified by phone"
    assert mock.calls == [(a["approval_id"], {"field": "email", "value": NEW_EMAIL})]


def test_staff_can_reject_and_a_rejected_request_never_executes(env):
    svc, mock, _, _, settings = env
    a = ask(svc, settings)
    out = svc.decide(a["approval_id"], staff(settings), approve=False, reason="cannot verify")
    assert out["executed"] is False and out["approval"]["status"] == "rejected"
    with pytest.raises(ApprovalError) as err:                              # nothing can drive it forward again
        svc.execute(a["approval_id"])
    assert err.value.kind == "conflict"
    with pytest.raises(ApprovalError):
        svc.decide(a["approval_id"], staff(settings), approve=True)
    assert mock.calls == [] and status_of(settings, a["approval_id"]) == "rejected"


def test_a_rejected_status_forced_to_approved_in_the_database_still_does_not_execute(env):
    svc, mock, _, _, settings = env
    a = ask(svc, settings)
    svc.decide(a["approval_id"], staff(settings), approve=False)
    tamper(settings, "UPDATE approvals SET status='approved' WHERE approval_id=?", a["approval_id"])
    with pytest.raises(ApprovalError) as err:
        svc.execute(a["approval_id"])
    assert err.value.kind == "integrity" and mock.calls == []              # no decision signature for an approval


def test_a_secret_in_the_decision_note_is_masked(env):
    svc, _, _, _, settings = env
    a = ask(svc, settings)
    out = svc.decide(a["approval_id"], staff(settings), approve=False, reason="student said my password is hunter2zz")
    assert "hunter2zz" not in json.dumps(out) and "hunter2zz" not in db_dump(settings.database_path)


# ------------------------------------------------------------------ expiry
def test_an_expired_request_cannot_be_decided_or_executed(env):
    svc, mock, clock, _, settings = env
    a = ask(svc, settings)
    clock.advance(hours=24, seconds=1)
    with pytest.raises(ApprovalError) as err:
        svc.decide(a["approval_id"], staff(settings), approve=True)
    assert err.value.kind == "expired" and status_of(settings, a["approval_id"]) == "expired"
    with pytest.raises(ApprovalError):
        svc.execute(a["approval_id"])
    assert mock.calls == []


def test_a_request_just_inside_the_window_is_still_valid(env):
    svc, mock, clock, _, settings = env
    a = ask(svc, settings)
    clock.advance(hours=23, minutes=59)
    assert svc.decide(a["approval_id"], staff(settings), approve=True)["executed"] is True
    assert len(mock.calls) == 1


def test_an_approved_but_unexecuted_request_expires_too(env):
    svc, mock, clock, _, settings = env
    a = ask(svc, settings)
    svc.decide(a["approval_id"], staff(settings), approve=True, execute_now=False)
    clock.advance(days=2)
    with pytest.raises(ApprovalError) as err:
        svc.execute(a["approval_id"])
    assert err.value.kind == "expired" and mock.calls == [] and status_of(settings, a["approval_id"]) == "expired"


def test_expiry_is_audited_and_visible_in_the_list(env):
    svc, _, clock, audit, settings = env
    a = ask(svc, settings)
    clock.advance(hours=25)
    assert [x["status"] for x in svc.list_for(student(settings))] == ["expired"]
    assert any(e["event"] == "approval.expired" for e in audit.list_events(limit=100))
    assert svc.list_for(staff(settings)) == []                              # not in the pending queue


def test_the_ttl_is_configurable(env):
    svc, mock, clock, audit, settings = env
    short = ApprovalService(settings.database_path, auth=svc.auth, audit=audit, signing_key=KEY, ttl=timedelta(minutes=5),
                            executors={"update_contact_details": mock}, clock=clock)
    a = short.request(student(settings), "update_contact_details", {"field": "email", "value": NEW_EMAIL})
    clock.advance(minutes=6)
    with pytest.raises(ApprovalError) as err:
        short.decide(a["approval_id"], staff(settings), approve=True)
    assert err.value.kind == "expired"


def test_a_request_that_expires_between_the_check_and_the_claim_is_not_approved(env):
    svc, mock, clock, _, settings = env
    a = ask(svc, settings)
    svc.expire_stale = lambda: None                      # the expiry sweep did not run, so only the claim itself can stop it
    clock.advance(hours=25)
    with pytest.raises(ApprovalError) as err:
        svc.decide(a["approval_id"], staff(settings), approve=True)
    assert err.value.kind in ("expired", "conflict") and mock.calls == []
    assert status_of(settings, a["approval_id"]) == "pending"


# ------------------------------------------------------------------ double and concurrent execution
def test_a_second_approval_cannot_execute_again(env):
    svc, mock, _, _, settings = env
    a = ask(svc, settings)
    svc.decide(a["approval_id"], staff(settings), approve=True)
    with pytest.raises(ApprovalError) as err:
        svc.decide(a["approval_id"], staff(settings), approve=True)
    assert err.value.kind == "conflict"
    with pytest.raises(ApprovalError):
        svc.execute(a["approval_id"])
    assert len(mock.calls) == 1


def test_concurrent_approvals_execute_exactly_once(env):
    svc, mock, _, _, settings = env
    a = ask(svc, settings)
    staff_p = staff(settings)
    outcomes, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait()
        try:
            outcomes.append(svc.decide(a["approval_id"], staff_p, approve=True)["executed"])
        except ApprovalError as exc:
            outcomes.append(exc.kind)

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(mock.calls) == 1 and outcomes.count(True) == 1
    assert set(outcomes) <= {True, "conflict"} and status_of(settings, a["approval_id"]) == "executed"


def test_concurrent_execute_calls_after_one_approval_run_once(env):
    svc, mock, _, _, settings = env
    a = ask(svc, settings)
    svc.decide(a["approval_id"], staff(settings), approve=True, execute_now=False)
    results, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait()
        try:
            results.append(svc.execute(a["approval_id"])["executed"])
        except ApprovalError as exc:
            results.append(exc.kind)

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(mock.calls) == 1 and results.count(True) == 1


def test_a_concurrent_approve_and_reject_have_one_winner(env):
    svc, mock, _, _, settings = env
    a = ask(svc, settings)
    staff_p, results, barrier = staff(settings), [], threading.Barrier(2)

    def go(approve):
        barrier.wait()
        try:
            results.append((approve, svc.decide(a["approval_id"], staff_p, approve=approve)["approval"]["status"]))
        except ApprovalError as exc:
            results.append((approve, exc.kind))

    threads = [threading.Thread(target=go, args=(x,)) for x in (True, False)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    final = status_of(settings, a["approval_id"])
    assert final in ("executed", "rejected") and len(mock.calls) == (1 if final == "executed" else 0)


# ------------------------------------------------------------------ tampering
@pytest.mark.parametrize("sql", [
    "UPDATE approvals SET args_json='{\"field\":\"email\",\"value\":\"evil@example.com\"}' WHERE approval_id=?",
    "UPDATE approvals SET student_id='STU002' WHERE approval_id=?",
    "UPDATE approvals SET requested_by='STU002' WHERE approval_id=?",
    "UPDATE approvals SET expires_at='2099-01-01T00:00:00.000000Z' WHERE approval_id=?",
    "UPDATE approvals SET request_mac='0000' WHERE approval_id=?",
])
def test_a_request_edited_while_pending_is_refused(env, sql):
    svc, mock, _, audit, settings = env
    a = ask(svc, settings)
    tamper(settings, sql, a["approval_id"])
    with pytest.raises(ApprovalError) as err:
        svc.decide(a["approval_id"], staff(settings), approve=True)
    assert err.value.kind == "integrity" and mock.calls == []
    assert status_of(settings, a["approval_id"]) == "failed"
    events = [e["event"] for e in audit.list_events(limit=100)]
    assert "approval.tamper_detected" in events and "approval.decision" not in events     # never even recorded as approved


@pytest.mark.parametrize("sql", [
    "UPDATE approvals SET args_json='{\"field\":\"email\",\"value\":\"evil@example.com\"}' WHERE approval_id=?",
    "UPDATE approvals SET student_id='STU002' WHERE approval_id=?",
    "UPDATE approvals SET decided_by='STAFF99' WHERE approval_id=?",
    "UPDATE approvals SET decision_mac=NULL WHERE approval_id=?",
    "UPDATE approvals SET decided_at='2026-01-01T00:00:00.000000Z' WHERE approval_id=?",
])
def test_a_request_edited_after_approval_is_refused_at_execution(env, sql):
    svc, mock, _, _, settings = env
    a = ask(svc, settings)
    svc.decide(a["approval_id"], staff(settings), approve=True, execute_now=False)
    tamper(settings, sql, a["approval_id"])
    with pytest.raises(ApprovalError) as err:
        svc.execute(a["approval_id"])
    assert err.value.kind == "integrity" and mock.calls == []


def test_an_edit_made_between_the_final_checks_and_the_change_is_still_caught(env):
    """The signature is verified again inside the transaction that runs the change (a race window the earlier check misses)."""
    svc, mock, _, audit, settings = env
    a = ask(svc, settings)
    svc.decide(a["approval_id"], staff(settings), approve=True, execute_now=False)
    original = audit.record

    def edit_then_record(event, **kw):
        if event == "approval.execute":                                    # the last thing before the change runs
            tamper(settings, "UPDATE approvals SET args_json='{\"field\":\"email\",\"value\":\"evil@example.com\"}' "
                             "WHERE approval_id=?", a["approval_id"])
        return original(event, **kw)

    audit.record = edit_then_record
    with pytest.raises(ApprovalError) as err:
        svc.execute(a["approval_id"])
    assert err.value.kind == "integrity" and mock.calls == []


def test_a_forged_request_made_with_the_wrong_key_is_refused(env):
    svc, mock, clock, audit, settings = env
    forger = ApprovalService(settings.database_path, auth=svc.auth, audit=audit, signing_key=b"x" * 32,
                             executors={"update_contact_details": mock}, clock=clock)
    a = forger.request(student(settings), "update_contact_details", {"field": "email", "value": NEW_EMAIL})
    with pytest.raises(ApprovalError) as err:
        svc.decide(a["approval_id"], staff(settings), approve=True)
    assert err.value.kind == "integrity" and mock.calls == []


# ------------------------------------------------------------------ authorization is rechecked at execution
def test_a_removed_approver_cannot_have_their_approval_executed(env):
    svc, mock, _, _, settings = env
    a = ask(svc, settings)
    svc.decide(a["approval_id"], staff(settings), approve=True, execute_now=False)
    tamper(settings, "DELETE FROM users WHERE user_id=?", STAFF_ID)
    with pytest.raises(ApprovalError) as err:
        svc.execute(a["approval_id"])
    assert err.value.kind == "forbidden" and mock.calls == []
    assert status_of(settings, a["approval_id"]) == "failed"


def test_a_removed_requester_means_nothing_runs(env):
    svc, mock, _, _, settings = env
    a = ask(svc, settings)
    svc.decide(a["approval_id"], staff(settings), approve=True, execute_now=False)
    tamper(settings, "DELETE FROM users WHERE user_id='STU001'")
    with pytest.raises(ApprovalError) as err:
        svc.execute(a["approval_id"])
    assert err.value.kind == "forbidden" and mock.calls == []


def test_an_approver_who_is_the_requester_is_refused_at_execution(env):
    """Defence in depth: even a correctly signed row cannot run if the decider IS the requester."""
    svc, mock, _, _, settings = env
    a = ask(svc, settings)
    svc.decide(a["approval_id"], staff(settings), approve=True, execute_now=False)
    row = svc._row(a["approval_id"])
    row["decided_by"] = row["requested_by"]
    row["decision_mac"] = svc._decision_mac(row, row["decided_by"], row["decided_at"])       # as if signed with the real key
    tamper(settings, "UPDATE approvals SET decided_by=?, decision_mac=? WHERE approval_id=?",
           row["decided_by"], row["decision_mac"], a["approval_id"])
    with pytest.raises(ApprovalError) as err:
        svc.execute(a["approval_id"])
    assert err.value.kind == "forbidden" and mock.calls == []


# ------------------------------------------------------------------ the audit log is required
def test_an_audit_failure_before_execution_prevents_execution(env):
    svc, mock, _, _, settings = env
    a = ask(svc, settings)
    svc.audit = FlakyAudit(settings.database_path, fail_events={"approval.execute"})
    with pytest.raises(ApprovalError) as err:
        svc.decide(a["approval_id"], staff(settings), approve=True)
    assert err.value.kind == "audit_unavailable" and mock.calls == []
    assert status_of(settings, a["approval_id"]) == "approved"             # approved, not executed: it can be retried
    svc.audit = FlakyAudit(settings.database_path)                          # the audit log recovers
    assert svc.execute(a["approval_id"])["executed"] is True and len(mock.calls) == 1


def test_an_audit_failure_while_recording_the_decision_prevents_execution(env):
    svc, mock, _, _, settings = env
    a = ask(svc, settings)
    svc.audit = FlakyAudit(settings.database_path, fail_events={"approval.decision"})
    with pytest.raises(ApprovalError) as err:
        svc.decide(a["approval_id"], staff(settings), approve=True)
    assert err.value.kind == "audit_unavailable" and mock.calls == []


def test_an_audit_failure_while_recording_a_rejection_is_reported(env):
    svc, mock, _, _, settings = env
    a = ask(svc, settings)
    svc.audit = FlakyAudit(settings.database_path, fail_events={"approval.decision"})
    with pytest.raises(ApprovalError) as err:
        svc.decide(a["approval_id"], staff(settings), approve=False)
    assert err.value.kind == "audit_unavailable" and mock.calls == []


def test_a_failed_closing_audit_record_does_not_undo_a_committed_change(env):
    svc, mock, _, _, settings = env
    a = ask(svc, settings)
    svc.audit = FlakyAudit(settings.database_path)
    original = svc.audit.record
    svc.audit.record = lambda event, **kw: (_ for _ in ()).throw(AuditError("down")) if (
        event == "approval.execute" and kw["outcome"] == "success") else original(event, **kw)
    out = svc.decide(a["approval_id"], staff(settings), approve=True)
    assert out["executed"] is True and status_of(settings, a["approval_id"]) == "executed" and len(mock.calls) == 1


# ------------------------------------------------------------------ executor failure
def test_an_executor_that_fails_leaves_the_request_failed_and_changes_nothing(env):
    svc, _, _, _, settings = env

    def broken(conn, row, args, now):
        conn.execute("INSERT INTO student_contacts (student_id, email, updated_at) VALUES ('STU001','half@example.edu','x')")
        raise RuntimeError("boom " + NEW_EMAIL)

    svc.executors["update_contact_details"] = broken
    a = ask(svc, settings)
    with pytest.raises(ApprovalError) as err:
        svc.decide(a["approval_id"], staff(settings), approve=True)
    assert err.value.kind == "execution_failed" and NEW_EMAIL not in str(err.value)
    assert db_one(settings.database_path, "SELECT COUNT(*) FROM student_contacts")[0] == 0     # rolled back
    assert status_of(settings, a["approval_id"]) == "failed"
    with pytest.raises(ApprovalError):
        svc.execute(a["approval_id"])


# ------------------------------------------------------------------ cancel, list, views
def test_a_student_can_cancel_their_own_pending_request_and_it_never_executes(env):
    svc, mock, _, _, settings = env
    a = ask(svc, settings)
    assert svc.cancel(student(settings), a["approval_id"])["status"] == "cancelled"
    with pytest.raises(ApprovalError) as err:
        svc.decide(a["approval_id"], staff(settings), approve=True)
    assert err.value.kind == "conflict" and mock.calls == []


def test_staff_see_the_pending_queue_with_masked_values_only(env):
    svc, _, _, _, settings = env
    ask(svc, settings)
    ask(svc, settings, sid="STU002", field="phone", value="+91 98765 43210")
    queue = svc.list_for(staff(settings))
    assert len(queue) == 2
    text = json.dumps(queue)
    assert NEW_EMAIL not in text and "98765 43210" not in text and "proposed_value" not in text.replace("proposed_value_masked", "")
    assert any(item["proposed_value_masked"].endswith("3210") for item in queue)


def test_a_student_lists_only_their_own_requests(env):
    svc, _, _, _, settings = env
    ask(svc, settings)
    ask(svc, settings, sid="STU002")
    assert [x["student_id"] for x in svc.list_for(student(settings, "STU001"))] == ["STU001"]


def test_an_unknown_status_filter_is_rejected(env):
    svc, _, _, _, settings = env
    with pytest.raises(ApprovalError) as err:
        svc.list_for(student(settings), "everything")
    assert err.value.kind == "invalid"


@pytest.mark.parametrize("bad", ["", "APR-1", "apr-000000000000", "APR-000000000000'; DROP TABLE approvals;--", None, 7])
def test_malformed_ids_are_just_not_found(env, bad):
    svc, _, _, _, settings = env
    with pytest.raises(ApprovalError) as err:
        svc.get(staff(settings), bad)
    assert err.value.kind == "not_found"


# ------------------------------------------------------------------ the signing key
def test_the_key_file_is_private_stable_and_outside_the_database(tmp_path, settings):
    path = tmp_path / "keys" / ".approval_key"
    key = load_or_create_key(path)
    assert len(key) == 32 and load_or_create_key(path) == key
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert key.hex() not in db_dump(settings.database_path)


def test_a_short_key_file_is_refused(tmp_path):
    path = tmp_path / ".approval_key"
    path.write_bytes(b"short")
    with pytest.raises(ValueError):
        load_or_create_key(path)


def test_the_service_refuses_a_weak_key(settings):
    with pytest.raises(ValueError):
        ApprovalService(settings.database_path, auth=seed_users(settings), audit=AuditStore(settings.database_path),
                        signing_key=b"abc")
