#!/usr/bin/env python3
"""manage_operator.py — the ONLY way an account becomes (or stops being) an
operator.

Operator status gates every account/trading/market endpoint and the live
feed. It is deliberately not grantable over HTTP: no bootstrap-on-empty-DB,
no email allowlist, no automatic promotion at startup. Running this command
requires shell access to the host, which is the trust boundary.

Usage (from Backend/):
  ./venv/bin/python -m scripts.manage_operator list
  ./venv/bin/python -m scripts.manage_operator create  --email you@example.com --name "Your Name"
  ./venv/bin/python -m scripts.manage_operator promote --email existing@example.com
  ./venv/bin/python -m scripts.manage_operator demote  --email someone@example.com

``create`` prompts for the password (hidden); pass ``--password-stdin`` to
read it from standard input instead (for scripts/tests). Passwords and hashes
are never printed. All commands are idempotent.
"""
from __future__ import annotations

import argparse
import getpass
import sys

from app.auth import list_operators, provision_operator, revoke_operator
from app.db import Base, SessionLocal, engine


def _read_password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Password for the new operator (min 8 chars): ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        raise SystemExit("passwords do not match")
    return first


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="manage_operator", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show operator accounts")
    p_create = sub.add_parser("create", help="create a new operator account")
    p_create.add_argument("--email", required=True)
    p_create.add_argument("--name", required=True)
    p_create.add_argument("--password-stdin", action="store_true")
    p_promote = sub.add_parser("promote", help="grant operator status to an existing account")
    p_promote.add_argument("--email", required=True)
    p_demote = sub.add_parser("demote", help="revoke operator status")
    p_demote.add_argument("--email", required=True)
    args = ap.parse_args(argv)

    Base.metadata.create_all(bind=engine)     # safe on an existing DB; needed on a fresh one
    with SessionLocal() as db:
        if args.cmd == "list":
            ops = list_operators(db)
            if not ops:
                print("no operator accounts")
            for u in ops:
                print(f"{u.id}\t{u.email}\t{u.name}")
            return 0
        if args.cmd == "create":
            try:
                user, action = provision_operator(db, args.email, name=args.name,
                                                  password=_read_password(args))
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            print(f"{action}: {user.email} (id={user.id}) is an operator")
            return 0
        if args.cmd == "promote":
            try:
                user, action = provision_operator(db, args.email)
            except ValueError:
                print(f"error: no account with email {args.email!r}; use `create` to add one",
                      file=sys.stderr)
                return 2
            print(f"{action}: {user.email} (id={user.id}) is an operator")
            return 0
        if args.cmd == "demote":
            changed = revoke_operator(db, args.email)
            print("demoted" if changed else "unchanged: not an operator (or no such account)")
            return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
