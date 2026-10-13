"""Administrative commands.

    python -m app.cli grant-admin user@example.com
    python -m app.cli revoke-access user@example.com
    python -m app.cli list-users

The user must already exist in Supabase Auth (dashboard > Authentication > Users).
These commands only decide who this backend treats as an admin.
"""

import argparse
import sys

from sqlalchemy import select, text

from app.db.session import get_session_factory
from app.models.enums import UserRole
from app.models.user import AppUser


def grant_admin(email: str) -> int:
    with get_session_factory()() as session:
        row = session.execute(
            text("SELECT id, email FROM auth.users WHERE lower(email) = lower(:email)"),
            {"email": email},
        ).one_or_none()
        if row is None:
            print(f"No Supabase Auth user with email {email}. Create the user first.")
            return 1
        user = session.get(AppUser, row.id)
        if user is None:
            session.add(AppUser(id=row.id, email=row.email, role=UserRole.ADMIN))
        else:
            user.role, user.is_active, user.email = UserRole.ADMIN, True, row.email
        session.commit()
    print(f"{row.email} is now an active admin.")
    return 0


def revoke_access(email: str) -> int:
    with get_session_factory()() as session:
        user = session.scalar(select(AppUser).where(AppUser.email.ilike(email)))
        if user is None:
            print(f"{email} has no access to revoke.")
            return 1
        user.is_active = False
        session.commit()
    print(f"{email} can no longer use the API.")
    return 0


def list_users() -> int:
    with get_session_factory()() as session:
        users = session.scalars(select(AppUser).order_by(AppUser.email)).all()
    if not users:
        print("No application users. Use grant-admin to add one.")
    for user in users:
        print(f"{user.email}\t{user.role}\t{'active' if user.is_active else 'inactive'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("grant-admin").add_argument("email")
    commands.add_parser("revoke-access").add_argument("email")
    commands.add_parser("list-users")
    args = parser.parse_args()

    if args.command == "grant-admin":
        return grant_admin(args.email)
    if args.command == "revoke-access":
        return revoke_access(args.email)
    return list_users()


if __name__ == "__main__":
    sys.exit(main())
