"""Create the fictional demo accounts: one per student in data/students.json plus one staff approver.

Only a salted scrypt hash of each password is stored. By default each password is random and printed ONCE here (it cannot
be recovered afterwards). With --demo the passwords are fixed, publicly documented and meant for a classroom demo only.

  .venv/bin/python scripts/seed_demo_users.py            # random passwords, printed once
  .venv/bin/python scripts/seed_demo_users.py --demo     # fixed demo passwords (fictional data only)
  .venv/bin/python scripts/seed_demo_users.py --db /tmp/x.sqlite3

Existing accounts are left untouched. It adds rows only (the database file is created if missing); nothing is deleted.
"""
import argparse
import json
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from campuspilot.auth import AuthService  # noqa: E402
from campuspilot.authz import STAFF, STUDENT  # noqa: E402
from campuspilot.config import load_settings  # noqa: E402
from campuspilot.db import init_db  # noqa: E402

STAFF_ID = "STAFF01"


def demo_password(user_id: str) -> str:
    return f"Demo-{user_id.lower()}-2026"      # fictional, documented, for the classroom demo only


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="SQLite file (default: the application's database)")
    ap.add_argument("--demo", action="store_true", help="use the fixed, publicly documented demo passwords")
    args = ap.parse_args(argv)

    settings = load_settings()
    db = Path(args.db) if args.db else settings.database_path
    init_db(db)
    svc = AuthService(db)
    students = json.loads((settings.data_dir / "students.json").read_text(encoding="utf-8"))
    accounts = [(s["student_id"], STUDENT, s["name"]) for s in students] + [(STAFF_ID, STAFF, "Demo staff approver")]
    created = 0
    for user_id, role, name in accounts:
        if svc.get_principal(user_id) is not None:
            print(f"exists   {user_id}")
            continue
        password = demo_password(user_id) if args.demo else secrets.token_urlsafe(9)
        svc.create_user(user_id, role, password, display_name=name)
        created += 1
        print(f"created  {user_id:8} ({role})  password: {password}" if not args.demo else f"created  {user_id:8} ({role})")
    if args.demo:
        print("demo passwords follow the pattern Demo-<user id in lower case>-2026 (fictional accounts only)")
    elif created:
        print("Passwords above are shown once and are not stored anywhere; keep them if you need them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
