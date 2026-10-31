"""Password hashing, accounts, lockout and login sessions (Assignment 2 foundation)."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from campuspilot import auth
from campuspilot.auth import (AuthService, hash_password, hash_token, new_token, normalize_user_id, verify_password)
from campuspilot.authz import STAFF, STUDENT

PASSWORD = "correct horse battery"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 1, 9, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def svc(settings, clock):
    return AuthService(settings.database_path, clock=clock, max_failed=3, lockout=timedelta(minutes=15),
                       session_ttl=timedelta(minutes=30))


def _db_text(path):
    """Everything stored in the database file, as text, for 'is the secret on disk?' checks."""
    conn = sqlite3.connect(path)
    try:
        dump = "\n".join(conn.iterdump())
    finally:
        conn.close()
    return dump + open(path, "rb").read().decode("latin-1")


# ---------------------------------------------------------------- password hashing
def test_hash_has_the_expected_shape_and_a_fresh_salt_each_time():
    a, b = hash_password(PASSWORD), hash_password(PASSWORD)
    assert a != b and a.startswith("scrypt$") and a.count("$") == 5
    assert PASSWORD not in a


def test_verify_accepts_the_right_password_only():
    stored = hash_password(PASSWORD)
    assert verify_password(PASSWORD, stored) is True
    for wrong in ("", "correct horse battery ", "Correct horse battery", "x" * 200, PASSWORD[:-1]):
        assert verify_password(wrong, stored) is False


@pytest.mark.parametrize("stored", ["", "garbage", "scrypt$1$2", "scrypt$16384$8$1$!!!$!!!", "md5$16384$8$1$AAAA$AAAA",
                                    "scrypt$999999999$8$1$AAAA$AAAA", "scrypt$16384$8$1$AAAA$", None, 42])
def test_verify_never_raises_on_malformed_stored_values(stored):
    assert verify_password(PASSWORD, stored) is False


def test_verify_rejects_non_strings_and_oversize_passwords_without_hashing_them():
    stored = hash_password(PASSWORD)
    for bad in (None, 123, b"bytes", ["x"]):
        assert verify_password(bad, stored) is False
    assert verify_password("x" * 100000, stored) is False


@pytest.mark.parametrize("password", ["short", "", None, 12345678, "x" * 129])
def test_hash_password_enforces_length_limits(password):
    with pytest.raises(ValueError):
        hash_password(password)


def test_tokens_are_random_long_and_hashed_with_sha256():
    tokens = {new_token() for _ in range(200)}
    assert len(tokens) == 200 and all(len(t) >= 43 for t in tokens)
    t = new_token()
    assert hash_token(t) != t and len(hash_token(t)) == 64 and hash_token(t) == hash_token(t)


# ---------------------------------------------------------------- accounts
def test_create_and_look_up_accounts(svc):
    s = svc.create_user("stu001", STUDENT, PASSWORD, display_name="Asha")
    st = svc.create_user("STAFF01", STAFF, PASSWORD)
    assert (s.user_id, s.role, s.student_id) == ("STU001", STUDENT, "STU001")
    assert (st.user_id, st.role, st.student_id) == ("STAFF01", STAFF, None)
    assert svc.get_principal("StU001") == s and svc.get_principal("NOBODY") is None and svc.get_principal(None) is None


@pytest.mark.parametrize("args", [("", STUDENT, PASSWORD), ("bad id", STUDENT, PASSWORD), ("STU001", "admin", PASSWORD),
                                  ("STU001", STUDENT, "short"), (None, STUDENT, PASSWORD), ("A" * 40, STAFF, PASSWORD)])
def test_create_user_rejects_bad_input(svc, args):
    with pytest.raises(ValueError):
        svc.create_user(*args)


def test_duplicate_users_are_rejected_and_the_original_is_kept(svc):
    svc.create_user("STU001", STUDENT, PASSWORD)
    with pytest.raises(ValueError):
        svc.create_user("stu001", STUDENT, "another password")
    assert svc.authenticate("STU001", PASSWORD).ok and not svc.authenticate("STU001", "another password").ok


def test_database_rejects_inconsistent_accounts_even_if_the_service_is_bypassed(settings):
    conn = sqlite3.connect(settings.database_path)
    for role, student_id in (("student", None), ("student", "STU002"), ("staff", "STU001"), ("admin", None)):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO users (user_id, role, student_id, password_hash, created_at) VALUES "
                         "('STU001', ?, ?, 'x', 'now')", (role, student_id))
    conn.close()


# ---------------------------------------------------------------- login
def test_correct_credentials_authenticate_and_the_user_id_is_case_insensitive(svc):
    svc.create_user("STU001", STUDENT, PASSWORD)
    r = svc.authenticate(" stu001 ", PASSWORD)
    assert r.ok and r.reason == "ok" and r.principal.student_id == "STU001"


def test_failures_are_reported_precisely_for_the_audit_log_but_all_look_alike_to_a_client(svc):
    svc.create_user("STU001", STUDENT, PASSWORD)
    assert svc.authenticate("STU001", "wrong password").reason == "bad_credentials"
    assert svc.authenticate("NOBODY", PASSWORD).reason == "unknown_user"
    for bad_id, bad_pw in ((None, PASSWORD), ("STU001", None), ("bad id!", PASSWORD), ("STU001", 123)):
        r = svc.authenticate(bad_id, bad_pw)
        assert not r.ok and r.reason == "invalid_input"


def test_unknown_users_cost_a_password_verification_too(svc, monkeypatch):
    calls = []
    real = auth.verify_password
    monkeypatch.setattr(auth, "verify_password", lambda pw, stored: (calls.append(stored), real(pw, stored))[1])
    svc.authenticate("NOBODY", "whatever password")
    assert calls and calls[0] == auth._DUMMY_HASH


def test_account_locks_after_repeated_failures_and_even_the_right_password_is_refused(svc, clock):
    svc.create_user("STU001", STUDENT, PASSWORD)
    for _ in range(3):
        assert svc.authenticate("STU001", "wrong password").reason == "bad_credentials"
    assert svc.authenticate("STU001", PASSWORD).reason == "locked"
    clock.advance(minutes=14)
    assert svc.authenticate("STU001", PASSWORD).reason == "locked"
    clock.advance(minutes=2)
    assert svc.authenticate("STU001", PASSWORD).ok


def test_a_successful_login_resets_the_failure_counter(svc):
    svc.create_user("STU001", STUDENT, PASSWORD)
    for _ in range(2):
        svc.authenticate("STU001", "wrong password")
    assert svc.authenticate("STU001", PASSWORD).ok
    for _ in range(2):
        svc.authenticate("STU001", "wrong password")
    assert svc.authenticate("STU001", PASSWORD).ok                         # 2 + 2 failures never reached 3 in a row


def test_lockout_is_per_account(svc):
    svc.create_user("STU001", STUDENT, PASSWORD)
    svc.create_user("STU002", STUDENT, PASSWORD)
    for _ in range(3):
        svc.authenticate("STU001", "wrong password")
    assert svc.authenticate("STU001", PASSWORD).reason == "locked" and svc.authenticate("STU002", PASSWORD).ok


# ---------------------------------------------------------------- sessions
def test_session_roundtrip_expiry_and_revocation(svc, clock):
    p = svc.create_user("STU001", STUDENT, PASSWORD)
    token, expires = svc.create_session(p)
    assert svc.principal_for_token(token) == p and expires.endswith("Z")
    clock.advance(minutes=29)
    assert svc.principal_for_token(token) == p
    clock.advance(minutes=2)
    assert svc.principal_for_token(token) is None                           # expired
    token2, _ = svc.create_session(p)
    assert svc.revoke(token2) is True and svc.principal_for_token(token2) is None and svc.revoke(token2) is False


def test_revoke_all_logs_a_user_out_everywhere_and_only_that_user(svc):
    a, b = svc.create_user("STU001", STUDENT, PASSWORD), svc.create_user("STU002", STUDENT, PASSWORD)
    ta1, _ = svc.create_session(a)
    ta2, _ = svc.create_session(a)
    tb, _ = svc.create_session(b)
    assert svc.revoke_all("stu001") == 2
    assert svc.principal_for_token(ta1) is None and svc.principal_for_token(ta2) is None and svc.principal_for_token(tb) == b


@pytest.mark.parametrize("token", [None, "", "short", 12345, "x" * 500, "A" * 43, "../../etc/passwd" + "x" * 30])
def test_unknown_or_malformed_tokens_never_authenticate(svc, token):
    assert svc.principal_for_token(token) is None
    assert svc.revoke(token) is False


def test_a_tampered_token_does_not_authenticate(svc):
    p = svc.create_user("STU001", STUDENT, PASSWORD)
    token, _ = svc.create_session(p)
    flipped = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert svc.principal_for_token(flipped) is None and svc.principal_for_token(token) == p


def test_a_session_does_not_outlive_its_account(svc, settings):
    p = svc.create_user("STU001", STUDENT, PASSWORD)
    token, _ = svc.create_session(p)
    conn = sqlite3.connect(settings.database_path)
    conn.execute("DELETE FROM users WHERE user_id='STU001'")
    conn.commit(); conn.close()
    assert svc.principal_for_token(token) is None


# ---------------------------------------------------------------- what is (not) on disk
def test_neither_the_password_nor_the_token_is_stored_in_plaintext(svc, settings):
    p = svc.create_user("STU001", STUDENT, "a very private passphrase 42")
    svc.authenticate("STU001", "a very private passphrase 42")
    svc.authenticate("STU001", "wrong guess entirely")
    token, _ = svc.create_session(p)
    on_disk = _db_text(settings.database_path)
    assert "a very private passphrase 42" not in on_disk and "wrong guess entirely" not in on_disk
    assert token not in on_disk
    assert hash_token(token) in on_disk                                      # the hash is what is kept


def test_user_id_normalisation():
    assert normalize_user_id(" stu001 ") == "STU001"
    for bad in (None, 7, "", "a b", "x" * 40):
        assert normalize_user_id(bad) is None


def test_invalid_max_failed_is_rejected(settings):
    with pytest.raises(ValueError):
        AuthService(settings.database_path, max_failed=0)
