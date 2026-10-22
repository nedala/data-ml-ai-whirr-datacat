"""Recover a locked-out local Studio admin (`make admin-reset APPLIANCE=… [ADMIN=name]`).

Runs inside the studio container (it owns users.json):

    python /opt/studio/admin_reset.py <name> [--create] [--generate]

* interactive terminal: prompts for the new password twice (not echoed);
* otherwise, or with --generate: prints a generated password ONCE to this
  terminal — it is never logged or written anywhere but the hash in users.json;
* --create: add <name> as a new local admin if it does not exist.

Existing sessions of that user are signed out (password epoch bump). The
vault master passphrase cannot be recovered this way — it encrypts the vault.
"""
from __future__ import annotations

import argparse
import getpass
import secrets
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit  # noqa: E402
import users  # noqa: E402


def _generated() -> str:
    alphabet = string.ascii_letters + string.digits
    return "-".join("".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(4))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="admin_reset", description=__doc__.splitlines()[0])
    ap.add_argument("name", help="local user name (e.g. admin)")
    ap.add_argument("--create", action="store_true", help="create the user as an admin if missing")
    ap.add_argument("--generate", action="store_true", help="generate a password instead of prompting")
    a = ap.parse_args(argv)

    existing = users.get(a.name)
    if existing is None and not a.create:
        names = ", ".join(u["name"] for u in users.list_all() if u.get("source") == "local") or "none"
        print(f"No local user {a.name!r}. Local users: {names}. Use --create to add it as an admin.", file=sys.stderr)
        return 2
    if existing is not None and existing.get("source") != "local":
        print(f"{a.name!r} signs in with SSO — manage it in Entra ID.", file=sys.stderr)
        return 2

    shown = False
    if a.generate or not sys.stdin.isatty():
        password, shown = _generated(), True
    else:
        password = getpass.getpass(f"New password for {a.name} (min {users.MIN_PASSWORD} chars): ")
        if password != getpass.getpass("Repeat: "):
            print("Passwords do not match — nothing changed.", file=sys.stderr)
            return 1
    try:
        if existing is None:
            users.add(a.name, "admin", password)
            action = "created as admin"
        else:
            users.set_password(a.name, password)
            if existing.get("role") != "admin":
                users.set_role(a.name, "admin")
            action = "password reset (admin)"
    except users.UserError as exc:
        print(f"Not changed: {exc}", file=sys.stderr)
        return 1
    audit.record("user.admin_reset", f"{a.name}: {action} from the studio container", user="cli", peer="local")
    print(f"✓ {a.name}: {action}. Other sessions of this user are signed out.")
    if shown:
        print(f"  New password (shown once, not stored in clear anywhere): {password}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
