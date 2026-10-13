"""CLI for managing SnapIN users.

Usage:
    python backend/manage_users.py add <username>        # prompts for password
    python backend/manage_users.py add <username> <password>
    python backend/manage_users.py list
    python backend/manage_users.py remove <username>
"""

from __future__ import annotations

import getpass
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import auth  # noqa: E402

MIN_PASSWORD_LENGTH = 8


def _read_password(username: str) -> str:
    password = getpass.getpass(f"Password for {username}: ")
    if not password:
        raise SystemExit("Password cannot be empty.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SystemExit(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    if password != getpass.getpass("Confirm password: "):
        raise SystemExit("Passwords do not match.")
    return password


def _load_raw() -> dict:
    path = auth._users_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.strip())
        return 1

    command, *rest = argv[1:]
    path = auth._users_path()

    if command == "add":
        if not rest:
            raise SystemExit("Usage: manage_users.py add <username> [password]")
        username = rest[0]
        password = rest[1] if len(rest) > 1 else _read_password(username)
        if len(password) < MIN_PASSWORD_LENGTH:
            raise SystemExit(
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
            )
        auth.set_user(username, password)
        print(f"User {username!r} written to {path}")
        return 0

    if command == "list":
        users = auth.load_users()
        if not users:
            print(f"No users configured (looked at {path}).")
            return 0
        for name in sorted(users):
            print(name)
        return 0

    if command == "remove":
        if not rest:
            raise SystemExit("Usage: manage_users.py remove <username>")
        username = rest[0]
        users = _load_raw()
        if username not in users:
            print(f"No such user: {username!r}")
            return 1
        del users[username]
        path.write_text(json.dumps(users, indent=2) + "\n", encoding="utf-8")
        print(f"Removed {username!r}")
        return 0

    print(f"Unknown command: {command!r}")
    print(__doc__.strip())
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))