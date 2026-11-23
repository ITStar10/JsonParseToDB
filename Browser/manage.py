#!/usr/bin/env python3
"""Account administration from the command line.

    python manage.py create-admin          create an approved administrator (prompts)
    python manage.py reset-password EMAIL  set a new password for any account (prompts)
    python manage.py list-users            show all accounts and their status
"""
import argparse
import getpass
import sys

from auth import UserStore
from config import INSTANCE_DIR, load_settings
from database import RequestError


def prompt_password():
    try:
        first = getpass.getpass("Password (min. 8 characters): ")
        if first != getpass.getpass("Confirm password: "):
            sys.exit("Error: passwords do not match")
        return first
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nCancelled.")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-admin", help="create an approved administrator")
    create.add_argument("--email")
    create.add_argument("--name")
    reset = sub.add_parser("reset-password", help="set a new password for an account")
    reset.add_argument("email")
    sub.add_parser("list-users", help="list accounts")
    args = parser.parse_args()

    settings = load_settings()
    users = UserStore(settings.users_db_path or INSTANCE_DIR / "users.sqlite3")
    try:
        if args.command == "create-admin":
            try:
                name = args.name or input("Full name: ").strip()
                email = args.email or input("E-mail: ").strip()
            except (EOFError, KeyboardInterrupt):
                sys.exit("\nCancelled.")
            row = users.create(name, email, prompt_password(), role="admin", status="approved")
            print(f"Administrator {row['email']} created. Sign in at http://{settings.host}:{settings.port}/login")
        elif args.command == "reset-password":
            row = users._one("SELECT id FROM users WHERE email = ?", (args.email.strip(),))
            if row is None:
                sys.exit(f"Error: no account with e-mail {args.email}")
            users.set_password(row["id"], prompt_password())
            print("Password updated. Existing sessions for this account were signed out.")
        elif args.command == "list-users":
            rows = users.list()
            if not rows:
                print("No accounts yet.")
            for u in rows:
                print(f"{u['id']:>4}  {u['status']:<9} {u['role']:<6} {u['email']:<35} {u['fullName']}")
    except RequestError as exc:
        sys.exit(f"Error: {exc}")
    finally:
        users.close()


if __name__ == "__main__":
    main()
