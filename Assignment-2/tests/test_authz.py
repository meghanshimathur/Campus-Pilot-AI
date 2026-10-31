"""Principal model and deny-by-default authorization policy (Assignment 2 foundation)."""
import itertools

import pytest

from campuspilot.authz import (POLICY, STAFF, STUDENT, Action, AuthorizationError, Decision, Principal, authorize,
                               normalize_student_id)

S1 = Principal("STU001", STUDENT, "STU001")
S2 = Principal("STU002", STUDENT, "STU002")
STAFF1 = Principal("STAFF01", STAFF, None)
OWN_PRIVATE = [Action.READ_OWN_PRIVATE, Action.READ_OWN_MEMORY, Action.WRITE_OWN_MEMORY, Action.DELETE_OWN_MEMORY,
               Action.CREATE_SUPPORT_REQUEST, Action.REQUEST_SENSITIVE_CHANGE, Action.VIEW_OWN_TRACE,
               Action.VIEW_OWN_AUDIT, Action.VIEW_OWN_APPROVALS, Action.CANCEL_OWN_APPROVAL]


# ---------------------------------------------------------------- the principal
def test_principal_rules_are_enforced_at_construction():
    assert Principal("STU001", STUDENT, "STU001").role == STUDENT
    assert Principal("STAFF01", STAFF).student_id is None
    for args in (("STU001", "admin", None), ("STU001", STUDENT, None), ("STU001", STUDENT, "STU002"),
                 ("STAFF01", STAFF, "STU001"), ("", STUDENT, ""), ("bad id!", STAFF, None), (123, STAFF, None)):
        with pytest.raises(ValueError):
            Principal(*args)


def test_principal_is_immutable():
    with pytest.raises(Exception):
        S1.student_id = "STU002"


# ---------------------------------------------------------------- the policy table
def test_every_action_has_a_rule_and_every_rule_has_a_known_role():
    assert set(POLICY) == set(Action)
    for rule in POLICY.values():
        assert rule.roles and rule.roles <= {STUDENT, STAFF}


@pytest.mark.parametrize("action", OWN_PRIVATE)
def test_a_student_may_use_their_own_records_with_or_without_naming_themselves(action):
    assert authorize(S1, action).allowed
    assert authorize(S1, action, target_student_id="STU001").allowed
    assert authorize(S1, action, target_student_id=" stu001 ").allowed        # normalised, not trusted as typed


@pytest.mark.parametrize("action", OWN_PRIVATE)
def test_a_student_is_denied_every_other_students_records(action):
    d = authorize(S1, action, target_student_id="STU002")
    assert not d.allowed and d.reason == "cross_student_access"
    assert authorize(S2, action, target_student_id="STU001").reason == "cross_student_access"


@pytest.mark.parametrize("action", OWN_PRIVATE)
def test_staff_cannot_read_or_change_a_students_private_data(action):
    for target in (None, "STU001"):
        d = authorize(STAFF1, action, target_student_id=target)
        assert not d.allowed and d.reason == "role_not_permitted"


def test_public_information_is_open_to_any_authenticated_principal():
    assert authorize(S1, Action.READ_PUBLIC) and authorize(STAFF1, Action.READ_PUBLIC)


def test_only_staff_may_list_approvals_or_read_the_whole_audit_log():
    for action in (Action.LIST_PENDING_APPROVALS, Action.VIEW_ALL_AUDIT):
        assert authorize(STAFF1, action).allowed
        assert authorize(S1, action).reason == "role_not_permitted"


def test_staff_may_decide_someone_elses_request_but_students_never_may():
    assert authorize(STAFF1, Action.DECIDE_APPROVAL, requester_id="STU001").allowed
    d = authorize(S1, Action.DECIDE_APPROVAL, requester_id="STU002")
    assert not d.allowed and d.reason == "role_not_permitted"                  # students cannot approve, even for others


def test_nobody_approves_their_own_request_and_the_requester_must_be_known():
    d = authorize(STAFF1, Action.DECIDE_APPROVAL, requester_id="STAFF01")
    assert not d.allowed and d.reason == "self_approval_forbidden"
    d = authorize(STAFF1, Action.DECIDE_APPROVAL)
    assert not d.allowed and d.reason == "missing_requester"
    assert not authorize(S1, Action.DECIDE_APPROVAL, requester_id="STU001").allowed       # a student approving their own


# ---------------------------------------------------------------- deny by default
@pytest.mark.parametrize("principal", [None, "STU001", {"user_id": "STU001"}, 42, object()])
def test_missing_or_malformed_principals_are_denied_every_action(principal):
    for action in Action:
        d = authorize(principal, action)
        assert not d.allowed and d.reason == "unauthenticated"


@pytest.mark.parametrize("action", ["delete_everything", "", None, 7, "READ_PUBLIC"])
def test_unknown_actions_are_denied(action):
    d = authorize(S1, action)
    assert not d.allowed and d.reason == "unknown_action"


@pytest.mark.parametrize("target", ["", "   ", "STU 001", "../STU001", "STU001; DROP TABLE users", 123, ["STU001"], "X" * 200])
def test_implausible_requested_ids_are_denied_not_guessed_at(target):
    d = authorize(S1, Action.READ_OWN_PRIVATE, target_student_id=target)
    assert not d.allowed and d.reason == "invalid_target"


def test_the_action_may_be_given_as_its_string_value():
    assert authorize(S1, "read_own_private").allowed
    assert not authorize(S1, "read_own_private", target_student_id="STU002").allowed


def test_a_requested_id_is_never_proof_of_identity():
    """The principal decides. Whatever id a request or a model names, only the principal's own id is allowed."""
    for requested in ("STU001", "stu001", " STU001 "):
        assert authorize(S1, Action.READ_OWN_PRIVATE, target_student_id=requested).allowed
    for requested, principal in itertools.permutations([S1, S2], 2):
        assert not authorize(principal, Action.READ_OWN_PRIVATE, target_student_id=requested.student_id).allowed


# ---------------------------------------------------------------- decisions
def test_decision_truthiness_reason_and_raise():
    ok, no = authorize(S1, Action.READ_PUBLIC), authorize(S1, Action.READ_OWN_PRIVATE, target_student_id="STU002")
    assert bool(ok) is True and bool(no) is False and ok.action == "read_public"
    ok.raise_if_denied()
    with pytest.raises(AuthorizationError) as err:
        no.raise_if_denied()
    assert err.value.decision == no and isinstance(no, Decision)


def test_normalize_student_id():
    assert normalize_student_id(" stu001 ") == "STU001"
    for bad in (None, 5, "", "a b", "x" * 40, "../x"):
        assert normalize_student_id(bad) is None
