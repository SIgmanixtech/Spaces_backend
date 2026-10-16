"""Administrative commands.

    python -m app.cli grant-admin user@example.com
    python -m app.cli revoke-access user@example.com
    python -m app.cli list-users
    python -m app.cli purge-orphan-files [--delete] [--min-age-hours N]

The user must already exist in Supabase Auth (dashboard > Authentication > Users).
These commands only decide who this backend treats as an admin.
"""

import argparse
import sys

from sqlalchemy import select, text

from app.core.config import get_settings
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


def purge_orphan_files(delete: bool, min_age_hours: float) -> int:
    """Stored objects that no files row refers to. Lists them; removes them with --delete."""
    # Imported here: the API layer is not needed by the other commands.
    from app.api.v1.endpoints.files import storage_from_settings
    from app.services.files import FileService

    storage = storage_from_settings(get_settings(), base_url="")
    with get_session_factory()() as session:
        orphans = FileService(session, storage).purge_orphans(
            min_age_seconds=int(min_age_hours * 3600), dry_run=not delete
        )
    for path in orphans:
        print(path)
    verb = "Removed" if delete else "Found (not removed; pass --delete)"
    print(f"{verb}: {len(orphans)} unreferenced object(s) in bucket {storage.bucket}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("grant-admin").add_argument("email")
    commands.add_parser("revoke-access").add_argument("email")
    commands.add_parser("list-users")
    purge = commands.add_parser("purge-orphan-files")
    purge.add_argument("--delete", action="store_true", help="remove them, not just list them")
    purge.add_argument("--min-age-hours", type=float, default=1.0)
    args = parser.parse_args()

    if args.command == "grant-admin":
        return grant_admin(args.email)
    if args.command == "revoke-access":
        return revoke_access(args.email)
    if args.command == "purge-orphan-files":
        return purge_orphan_files(args.delete, args.min_age_hours)
    return list_users()


if __name__ == "__main__":
    sys.exit(main())
