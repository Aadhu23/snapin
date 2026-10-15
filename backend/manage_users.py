"""CLI for managing SnapIN users.

Accounts live in PostgreSQL. Run with the virtualenv active, and with the
database reachable:

    python backend/manage_users.py add alice
    python backend/manage_users.py list
    python backend/manage_users.py remove alice
    python backend/manage_users.py import-json [path]
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

import env as env_loader  # noqa: E402

# Must load before db, which reads the credentials when it connects.
env_loader.load_env()

import db  # noqa: E402
import repository  # noqa: E402

MIN_PASSWORD_LENGTH = 8


def _die(message: str) -> "SystemExit":
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def _connect() -> None:
    """Open the pool, failing with an actionable message."""
    ok, error = db.available()
    if not ok:
        _die(
            f"cannot reach the database ({error}).\n"
            "       Check that PostgreSQL is running and that PGHOST/PGPORT/"
            "PGUSER/PGPASSWORD\n"
            "       or DATABASE_URL are set. See .env.example."
        )
    db.migrate()


def _read_password(username: str) -> str:
    password = getpass.getpass(f"Password for {username}: ")
    if not password:
        _die("password cannot be empty.")
    if len(password) < MIN_PASSWORD_LENGTH:
        _die(f"password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if password != getpass.getpass("Confirm password: "):
        _die("passwords do not match.")
    return password


def cmd_add(argv: list[str]) -> int:
    if not argv:
        _die("usage: manage_users.py add <username> [password]")
    username = argv[0].strip()
    if not username:
        _die("username cannot be empty.")
    if username.startswith("google:"):
        _die("'google:' is reserved for Google-linked accounts.")

    password = argv[1] if len(argv) > 1 else _read_password(username)
    if len(password) < MIN_PASSWORD_LENGTH:
        _die(f"password must be at least {MIN_PASSWORD_LENGTH} characters.")

    repository.create_password_user(username, auth.hash_password(password))
    print(f"User {username!r} saved.")
    return 0


def cmd_list(_argv: list[str]) -> int:
    users = repository.list_users()
    if not users:
        print("No users yet. Create one with: manage_users.py add <username>")
        return 0

    width = max(len(u["username"]) for u in users)
    print(f"{'USERNAME'.ljust(width)}  {'PROVIDER':9}  {'CREATED':20}  EMAIL")
    for user in users:
        provider = "google" if user.get("google_sub") else "password"
        created = user["created_at"].strftime("%Y-%m-%d %H:%M") if user.get("created_at") else "-"
        email = user.get("email") or "-"
        print(f"{user['username'].ljust(width)}  {provider:9}  {created:20}  {email}")
    return 0


def cmd_remove(argv: list[str]) -> int:
    if not argv:
        _die("usage: manage_users.py remove <username>")
    username = argv[0]
    if repository.delete_user(username):
        print(f"Removed {username!r}. Its sessions and stats went with it.")
        return 0
    print(f"No such user: {username!r}")
    return 1


def cmd_import_json(argv: list[str]) -> int:
    """Import accounts from the pre-database users.json file."""
    path = Path(argv[0]) if argv else BACKEND_DIR / "users.json"
    if not path.exists():
        _die(f"{path} not found")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _die(f"{path} is not valid JSON: {exc}")

    imported = skipped = 0
    for username, record in data.items():
        if not isinstance(record, dict):
            skipped += 1
            continue

        if record.get("hash"):
            repository.create_password_user(username, record)
            imported += 1
        elif record.get("_google_sub"):
            repository.upsert_google_user(
                record["_google_sub"],
                record.get("email", ""),
                record.get("name", username),
                record.get("picture"),
            )
            imported += 1
        else:
            skipped += 1

    print(f"Imported {imported} account(s) from {path}.")
    if skipped:
        print(f"Skipped {skipped} entry/entries with no usable credential.")
    if imported:
        print(f"The old file is now redundant and can be deleted: {path}")
    return 0


COMMANDS = {
    "add": cmd_add,
    "list": cmd_list,
    "remove": cmd_remove,
    "import-json": cmd_import_json,
}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in {"-h", "--help"}:
        print(__doc__.strip())
        return 0 if len(argv) > 1 else 1

    command = argv[1]
    handler = COMMANDS.get(command)
    if handler is None:
        print(f"Unknown command: {command!r}\n", file=sys.stderr)
        print(__doc__.strip(), file=sys.stderr)
        return 1

    _connect()
    try:
        return handler(argv[2:])
    finally:
        db.close_pool()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
