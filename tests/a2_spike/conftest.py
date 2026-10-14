"""Fixtures for the A2 spike: a throwaway schema in the disposable test database."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, create_engine, text

from app.db.session import engine_config
from tests.conftest import TEST_DATABASE_URL

SCHEMA = "a2_spike"
TABLES = "allocation_claims, allocations, layouts, spaces, floors, properties"
# Bounded waits, so a test that blocks unexpectedly fails instead of hanging.
SESSION_OPTIONS = f"-c search_path={SCHEMA},public -c lock_timeout=15000 -c statement_timeout=30000"


@pytest.fixture(scope="session")
def spike_engine(migrated_database: None) -> Iterator[Engine]:
    """The proposed tables, in their own schema next to the migrated application schema.

    Depends on ``migrated_database`` because revision 0001 installs btree_gist,
    which the exclusion constraints need.
    """
    url, connect_args = engine_config(TEST_DATABASE_URL)
    engine = create_engine(
        url,
        connect_args={**connect_args, "options": SESSION_OPTIONS},
        pool_size=16,
        max_overflow=0,
    )
    script = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cursor:
            cursor.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            cursor.execute(f"CREATE SCHEMA {SCHEMA}")
            # No parameters, so the driver sends the whole script as one batch.
            cursor.execute(script)
        raw.commit()
    finally:
        raw.close()
    try:
        yield engine
    finally:
        with engine.begin() as connection:
            connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        engine.dispose()


@pytest.fixture
def engine(spike_engine: Engine) -> Iterator[Engine]:
    yield spike_engine
    with spike_engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))


@pytest.fixture
def conn(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as connection:
        yield connection
