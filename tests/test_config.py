import pytest

from app.core.config import ConfigurationError, load_settings
from app.db.session import engine_config

SECRET_PASSWORD = "s3cr3t-pw"
VALID_URL = f"postgresql://postgres:{SECRET_PASSWORD}@db.example.supabase.co:5432/postgres"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in ("DATABASE_URL", "SUPABASE_URL", "CORS_ALLOWED_ORIGINS"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_missing_required_settings_are_named(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        load_settings(env_file=None)

    message = str(excinfo.value)
    assert "DATABASE_URL" in message
    assert "SUPABASE_URL" in message


def test_empty_values_count_as_missing(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DATABASE_URL", "")
    clean_env.setenv("SUPABASE_URL", "https://example.supabase.co")

    with pytest.raises(ConfigurationError, match="DATABASE_URL"):
        load_settings(env_file=None)


def test_invalid_database_url_error_does_not_leak_secret(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DATABASE_URL", f"mysql://root:{SECRET_PASSWORD}@db.example.com/app")
    clean_env.setenv("SUPABASE_URL", "https://example.supabase.co")

    with pytest.raises(ConfigurationError) as excinfo:
        load_settings(env_file=None)

    message = str(excinfo.value)
    assert "DATABASE_URL" in message
    assert SECRET_PASSWORD not in message
    assert "db.example.com" not in message


def test_valid_settings_load_and_hide_secret(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DATABASE_URL", VALID_URL)
    clean_env.setenv("SUPABASE_URL", "https://example.supabase.co")
    clean_env.setenv("CORS_ALLOWED_ORIGINS", "http://localhost:5173/, https://app.example.com")

    settings = load_settings(env_file=None)

    assert settings.cors_allowed_origins == ["http://localhost:5173", "https://app.example.com"]
    assert SECRET_PASSWORD not in repr(settings)
    assert settings.database_url.get_secret_value() == VALID_URL


def test_wildcard_cors_origin_is_rejected(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DATABASE_URL", VALID_URL)
    clean_env.setenv("SUPABASE_URL", "https://example.supabase.co")
    clean_env.setenv("CORS_ALLOWED_ORIGINS", "*")

    with pytest.raises(ConfigurationError, match="CORS_ALLOWED_ORIGINS"):
        load_settings(env_file=None)


def test_engine_config_uses_psycopg_and_requires_ssl_for_remote_hosts() -> None:
    url, connect_args = engine_config(VALID_URL)

    assert url.drivername == "postgresql+psycopg"
    assert url.query["sslmode"] == "require"
    assert "prepare_threshold" not in connect_args


def test_engine_config_keeps_explicit_sslmode_and_skips_ssl_for_localhost() -> None:
    explicit, _ = engine_config(VALID_URL + "?sslmode=verify-full")
    local, _ = engine_config("postgresql://postgres:pw@localhost:5432/app_test")

    assert explicit.query["sslmode"] == "verify-full"
    assert "sslmode" not in local.query


def test_engine_config_disables_prepared_statements_on_transaction_pooler() -> None:
    pooled = f"postgres://postgres.ref:{SECRET_PASSWORD}@aws-0-ap-south-1.pooler.supabase.com:6543/postgres"

    url, connect_args = engine_config(pooled)

    assert url.drivername == "postgresql+psycopg"
    assert connect_args["prepare_threshold"] is None
