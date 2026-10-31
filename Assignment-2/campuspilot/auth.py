"""Authentication primitives (Assignment 2 foundation): password hashing, accounts, lockout and login sessions.

What is stored: a salted scrypt hash of each password and the SHA-256 of each session token. Plaintext passwords and
plaintext tokens are never written to the database, a log line or an exception message. A session token is returned to
the caller once, when the session is created.

No HTTP here: the login routes and the "who is calling" dependency are added in a later batch and build on this module.
Standard library only (hashlib.scrypt, hmac, secrets); no new dependency.
"""
import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from campuspilot.authz import ROLES, STAFF, STUDENT, Principal, normalize_student_id
from campuspilot.db import connect

# scrypt cost parameters (OWASP-recommended minimum class: N=2^14, r=8, p=1). They are written into each stored hash,
# so they can be raised later without invalidating old hashes.
SCRYPT_N, SCRYPT_R, SCRYPT_P, KEY_LEN, SALT_LEN = 2 ** 14, 8, 1, 32, 16
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128          # also bounds the CPU a single attempt can cost
_N_BOUNDS = (2 ** 10, 2 ** 20)     # reject absurd parameters in a tampered stored hash instead of burning CPU on them
_USER_ID_RE = re.compile(r"\A[A-Z0-9_-]{1,32}\Z")

DEFAULT_SESSION_TTL = timedelta(minutes=60)
DEFAULT_MAX_FAILED_LOGINS = 5
DEFAULT_LOCKOUT = timedelta(minutes=15)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str) -> str:
    """``scrypt$N$r$p$salt$hash`` with a fresh random salt. Raises ValueError for a password outside the length limits."""
    if not isinstance(password, str) or not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValueError(f"password must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters")
    salt = secrets.token_bytes(SALT_LEN)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_LEN)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password, stored) -> bool:
    """True only if ``password`` matches ``stored``. Never raises: anything malformed simply fails."""
    try:
        if not isinstance(password, str) or not isinstance(stored, str) or len(password) > MAX_PASSWORD_LENGTH:
            return False
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        n, r, p = int(n), int(r), int(p)
        if scheme != "scrypt" or not _N_BOUNDS[0] <= n <= _N_BOUNDS[1] or not 1 <= r <= 32 or not 1 <= p <= 8:
            return False
        salt, expected = base64.b64decode(salt_b64, validate=True), base64.b64decode(hash_b64, validate=True)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected),
                                maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, OverflowError, MemoryError):
        return False


# Verified against when a login names an unknown user, so unknown and wrong-password attempts cost the same time.
_DUMMY_HASH = hash_password("not-a-real-password-0000")


def new_token() -> str:
    """A fresh unguessable session token (256 bits)."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """What is stored for a token. The token is high-entropy random, so an unsalted SHA-256 is appropriate."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")   # fixed width: sorts like time


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_user_id(value) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().upper()
    return cleaned if _USER_ID_RE.match(cleaned) else None


@dataclass(frozen=True)
class AuthResult:
    principal: Principal | None
    reason: str       # ok | invalid_input | unknown_user | bad_credentials | locked   (for the audit log, NOT for clients)

    @property
    def ok(self) -> bool:
        return self.principal is not None


class AuthService:
    """Accounts, lockout and login sessions backed by SQLite."""

    def __init__(self, db_path, *, session_ttl: timedelta = DEFAULT_SESSION_TTL, max_failed: int = DEFAULT_MAX_FAILED_LOGINS,
                 lockout: timedelta = DEFAULT_LOCKOUT, clock: Callable[[], datetime] = _utcnow):
        if max_failed < 1:
            raise ValueError("max_failed must be at least 1")
        self.db_path = db_path
        self.session_ttl = session_ttl
        self.max_failed = max_failed
        self.lockout = lockout
        self.clock = clock

    # ---- accounts
    def create_user(self, user_id: str, role: str, password: str, *, display_name: str = "") -> Principal:
        """Create an account. A student's id is their user id; a staff id is any other valid id. Raises ValueError."""
        uid = normalize_user_id(user_id)
        if uid is None:
            raise ValueError("invalid user id")
        if role not in ROLES:
            raise ValueError("invalid role")
        student_id = normalize_student_id(uid) if role == STUDENT else None
        row_hash = hash_password(password)            # validates the password length
        name = (display_name or "")[:80]
        conn = connect(self.db_path)
        try:
            conn.execute("INSERT INTO users (user_id, role, student_id, display_name, password_hash, created_at) "
                         "VALUES (?,?,?,?,?,?)", (uid, role, student_id, name, row_hash, _iso(self.clock())))
            conn.commit()
        except sqlite3.IntegrityError:
            raise ValueError("user already exists") from None
        finally:
            conn.close()
        return Principal(uid, role, student_id, name)

    def get_principal(self, user_id: str) -> Principal | None:
        uid = normalize_user_id(user_id)
        if uid is None:
            return None
        conn = connect(self.db_path)
        try:
            row = conn.execute("SELECT user_id, role, student_id, display_name FROM users WHERE user_id=?", (uid,)).fetchone()
        finally:
            conn.close()
        return Principal(row["user_id"], row["role"], row["student_id"], row["display_name"]) if row else None

    # ---- login
    def authenticate(self, user_id, password) -> AuthResult:
        """Check credentials. Counts failures and locks the account after ``max_failed`` of them. Constant-ish work
        for unknown users (a dummy hash is verified) so timing does not reveal which ids exist."""
        uid = normalize_user_id(user_id)
        if uid is None or not isinstance(password, str):
            verify_password("x", _DUMMY_HASH)
            return AuthResult(None, "invalid_input")
        conn = connect(self.db_path)
        try:
            row = conn.execute("SELECT user_id, role, student_id, display_name, password_hash, locked_until "
                               "FROM users WHERE user_id=?", (uid,)).fetchone()
            now = self.clock()
            if row is None:
                verify_password(password, _DUMMY_HASH)
                return AuthResult(None, "unknown_user")
            if row["locked_until"] and row["locked_until"] > _iso(now):
                verify_password(password, _DUMMY_HASH)           # no information, and no guessing, while locked
                return AuthResult(None, "locked")
            if not verify_password(password, row["password_hash"]):
                conn.execute("UPDATE users SET failed_attempts = failed_attempts + 1 WHERE user_id=?", (uid,))
                count = conn.execute("SELECT failed_attempts FROM users WHERE user_id=?", (uid,)).fetchone()[0]
                if count >= self.max_failed:
                    conn.execute("UPDATE users SET failed_attempts = 0, locked_until = ? WHERE user_id=?",
                                 (_iso(now + self.lockout), uid))
                conn.commit()
                return AuthResult(None, "bad_credentials")
            conn.execute("UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE user_id=?", (uid,))
            conn.commit()
            return AuthResult(Principal(row["user_id"], row["role"], row["student_id"], row["display_name"]), "ok")
        finally:
            conn.close()

    # ---- sessions
    def create_session(self, principal: Principal) -> tuple[str, str]:
        """Start a session. Returns ``(token, expires_at)``; the token is not recoverable afterwards."""
        token = new_token()
        now = self.clock()
        expires = _iso(now + self.session_ttl)
        conn = connect(self.db_path)
        try:
            conn.execute("INSERT INTO auth_sessions (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                         (hash_token(token), principal.user_id, _iso(now), expires))
            conn.commit()
        finally:
            conn.close()
        return token, expires

    def principal_for_token(self, token) -> Principal | None:
        """The principal for a valid, unexpired, unrevoked token, else None. Never raises for bad input."""
        if not isinstance(token, str) or not 20 <= len(token) <= 200:
            return None
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT s.expires_at, s.revoked_at, u.user_id, u.role, u.student_id, u.display_name FROM auth_sessions s "
                "JOIN users u ON u.user_id = s.user_id WHERE s.token_hash=?", (hash_token(token),)).fetchone()
            if row is None or row["revoked_at"] is not None:
                return None
            now = self.clock()
            if row["expires_at"] <= _iso(now):
                return None
            conn.execute("UPDATE auth_sessions SET last_used_at=? WHERE token_hash=?", (_iso(now), hash_token(token)))
            conn.commit()
            return Principal(row["user_id"], row["role"], row["student_id"], row["display_name"])
        finally:
            conn.close()

    def revoke(self, token) -> bool:
        """Log out one session. True if a live session was revoked."""
        if not isinstance(token, str):
            return False
        conn = connect(self.db_path)
        try:
            cur = conn.execute("UPDATE auth_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                               (_iso(self.clock()), hash_token(token)))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def revoke_all(self, user_id: str) -> int:
        """Log a user out everywhere. Returns how many live sessions were revoked."""
        uid = normalize_user_id(user_id)
        if uid is None:
            return 0
        conn = connect(self.db_path)
        try:
            cur = conn.execute("UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                               (_iso(self.clock()), uid))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
