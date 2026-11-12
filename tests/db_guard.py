"""Which database destructive tests may use: only the local disposable ``spaces_test``.

The test suite and the end-to-end server truncate tables, drop and recreate schemas and
run migrations up and down. Both call ``refuse_unless_test_database`` before importing
the application or connecting to anything. This module imports nothing from the app and
never connects; it only reads the address.
"""

from sqlalchemy.engine import make_url

LOCAL_DATABASE_HOSTS = {"localhost", "127.0.0.1", "::1"}
# Other local databases (rehearsal copies, backups) also have "test" in their names;
# only this one is disposable.
TEST_DATABASE_NAME = "spaces_test"
# libpq also reads these from the connection options, which would override the address.
HIDDEN_OVERRIDES = {"host", "hostaddr", "service", "dbname"}


def refuse_unless_local(raw_url: str) -> str | None:
    """Why ``raw_url`` must not be used for destructive tests, or None when it is the
    local disposable test database."""
    url = make_url(raw_url)
    lowered = raw_url.lower()
    if "supabase" in lowered or "pooler" in lowered:
        return "it names a Supabase or connection-pooler host"
    if (url.host or "").lower() not in LOCAL_DATABASE_HOSTS:
        return f"its host {url.host or '(none)'!r} is not on this machine"
    if HIDDEN_OVERRIDES & {key.lower() for key in url.query}:
        return "it sets host, hostaddr, service or dbname in its options"
    if url.database != TEST_DATABASE_NAME:
        return f"its database {url.database or '(none)'!r} is not {TEST_DATABASE_NAME!r}"
    return None


def refuse_unless_test_database(
    test_url: str | None, application_urls: set[str | None]
) -> str | None:
    """Every check, in order, for a configured test database address."""
    if not test_url:
        return None
    if test_url in application_urls:
        return (
            "TEST_DATABASE_URL is the same database as DATABASE_URL. "
            "Refusing to run tests against the application database."
        )
    # Supabase databases are always named "postgres"; kept from the original check.
    if "test" not in (make_url(test_url).database or ""):
        return 'TEST_DATABASE_URL must point at a database whose name contains "test".'
    reason = refuse_unless_local(test_url)
    if reason:
        return f"Refusing to run tests: TEST_DATABASE_URL must be a local database, but {reason}."
    return None
