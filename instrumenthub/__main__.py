from __future__ import annotations

import argparse
import getpass
import sqlite3
import sys
from pathlib import Path

import uvicorn
from sqlalchemy import func, select

from .config import Settings
from .db import Database
from .models import User
from .services import break_glass_reset_admin_password, create_user
from .ops import create_backup, restore_backup, verify_database
from .notifications import deliver_pending


def bootstrap_admin(settings: Settings) -> int:
    database = Database(settings)
    with database.session() as db:
        if db.scalar(select(func.count()).select_from(User)):
            print("Bootstrap is disabled after the first account exists.", file=sys.stderr)
            return 2
        username = input("Administrator username: ").strip()
        display_name = input("Display name: ").strip()
        initials = input("Initials: ").strip()
        while True:
            password = getpass.getpass("Password (minimum 12 characters): ")
            confirmation = getpass.getpass("Confirm password: ")
            if password != confirmation:
                print("Passwords do not match; please try again.", file=sys.stderr)
                continue
            if len(password) < 12:
                print("Password must contain at least 12 characters; please try again.", file=sys.stderr)
                continue
            break
        try:
            create_user(db, username=username, password=password, display_name=display_name, initials=initials, role="admin")
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    print(f"Administrator created in {settings.database_path}. Sign in to complete initial setup.")
    return 0


def status(settings: Settings) -> int:
    """Show the exact local instance used by this command without exposing secrets."""
    database = Database(settings)
    with database.session() as db:
        account_count = int(db.scalar(select(func.count()).select_from(User)) or 0)
    print(f"Instance directory: {settings.instance_dir}")
    print(f"Database: {settings.database_path}")
    print(f"Accounts: {account_count}")
    return 0


def reset_admin_password(settings: Settings, username: str, *, confirmed: bool) -> int:
    if not confirmed:
        print(
            "Administrator password reset refused. Pass --confirm-reset after verifying the configured instance.",
            file=sys.stderr,
        )
        return 2
    print(f"Database: {settings.database_path}")
    while True:
        password = getpass.getpass("New password (minimum 12 characters): ")
        confirmation = getpass.getpass("Confirm new password: ")
        if password != confirmation:
            print("Passwords do not match; please try again.", file=sys.stderr)
            continue
        if len(password) < 12:
            print("Password must contain at least 12 characters; please try again.", file=sys.stderr)
            continue
        break
    database = Database(settings)
    with database.session() as db:
        administrator = break_glass_reset_admin_password(db, username=username, new_password=password)
        display_name = administrator.display_name
    print(f"Administrator password reset for {display_name}; the account is active and all prior sessions are revoked.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m instrumenthub")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("bootstrap-admin", help="Create the first administrator interactively")
    reset_admin = subparsers.add_parser("reset-admin-password", help="Recover an existing administrator account interactively")
    reset_admin.add_argument("username")
    reset_admin.add_argument("--confirm-reset", action="store_true")
    subparsers.add_parser("status", help="Show the local instance path and account count")
    serve = subparsers.add_parser("serve", help="Run the application with one Uvicorn worker")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    backup = subparsers.add_parser("backup", help="Create and verify an online SQLite backup")
    backup.add_argument("--destination", type=Path)
    verify = subparsers.add_parser("verify-backup", help="Verify a SQLite backup")
    verify.add_argument("path", type=Path)
    restore = subparsers.add_parser("restore", help="Restore a verified backup while the app service is stopped")
    restore.add_argument("path", type=Path)
    restore.add_argument("--confirm-restore", action="store_true")
    deliver = subparsers.add_parser("deliver-notifications", help="Deliver a bounded batch of queued notification emails")
    deliver.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
        if args.command == "bootstrap-admin":
            return bootstrap_admin(settings)
        if args.command == "reset-admin-password":
            return reset_admin_password(settings, args.username, confirmed=args.confirm_reset)
        if args.command == "status":
            return status(settings)
        if args.command == "serve":
            uvicorn.run("instrumenthub:create_app", factory=True, host=args.host, port=args.port, workers=1)
            return 0
        if args.command == "backup":
            print(create_backup(settings, destination=args.destination))
            return 0
        if args.command == "verify-backup":
            verify_database(args.path)
            print("Backup integrity: ok")
            return 0
        if args.command == "restore":
            if not args.confirm_restore:
                print("Restore refused. Stop the service and pass --confirm-restore.", file=sys.stderr)
                return 2
            safety = restore_backup(settings, args.path)
            print(f"Pre-restore safety backup: {safety}" if safety else "Restore completed without a safety backup because the previous database was missing or corrupt.")
            return 0
        if args.command == "deliver-notifications":
            database = Database(settings)
            with database.session() as db:
                result = deliver_pending(db, settings, limit=args.limit)
            print(" ".join(f"{key}={value}" for key, value in result.items()))
            return 0
    except (ValueError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
