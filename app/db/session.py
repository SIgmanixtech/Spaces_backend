"""Engine and session management for Supabase PostgreSQL (psycopg 3)."""

from collections.abc import Iterator
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
# Supabase's transaction-mode pooler (Supavisor) listens on this port.
_TRANSACTION_POOLER_PORT = 6543


def engine_config(raw_url: str) -> tuple[URL, dict[str, Any]]:
    """Normalize a PostgreSQL URL for psycopg 3 and derive connection arguments."""
    url = make_url(raw_url).set(drivername="postgresql+psycopg")
    connect_args: dict[str, Any] = {"connect_timeout": 10}
    if url.host not in _LOCAL_HOSTS and "sslmode" not in url.query:
        url = url.update_query_dict({"sslmode": "require"})
    if url.port == _TRANSACTION_POOLER_PORT:
        # Transaction pooling hands each transaction a different server
        # connection, so server-side prepared statements cannot be reused.
        connect_args["prepare_threshold"] = None
    return url, connect_args


@lru_cache
def get_engine() -> Engine:
    url, connect_args = engine_config(get_settings().database_url.get_secret_value())
    return create_engine(
        url,
        connect_args=connect_args,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_recycle=1800,
    )


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """Yield a session per request. Services commit; anything else is rolled back."""
    session = get_session_factory()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def database_is_reachable(session: Session) -> bool:
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return False
    return True


def dispose_engine() -> None:
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_session_factory.cache_clear()
    get_engine.cache_clear()
