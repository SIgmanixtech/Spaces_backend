"""Application settings loaded from environment variables and ``.env``."""

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import AnyHttpUrl, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = BACKEND_ROOT / ".env"

_ACCEPTED_DRIVERS = {"postgres", "postgresql", "postgresql+psycopg"}


class ConfigurationError(RuntimeError):
    """Raised at startup when required settings are missing or invalid."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    # "text" prints one readable line per request (default outside production);
    # "json" prints structured lines for log collectors (default in production).
    log_format: Literal["text", "json"] | None = None

    database_url: SecretStr

    supabase_url: AnyHttpUrl
    # Browser-safe key, used here only to call Supabase Auth's sign-in endpoints.
    supabase_publishable_key: SecretStr | None = None
    supabase_secret_key: SecretStr | None = None
    property_storage_bucket: str = "property-files"
    # Where uploaded files go: Supabase Storage, or a local folder for development
    # and tests only. "supabase" needs SUPABASE_SECRET_KEY and an existing private bucket.
    storage_backend: Literal["supabase", "local"] = "supabase"
    local_storage_dir: Path = BACKEND_ROOT / "var" / "uploads"
    max_upload_mb: int = 10
    # Most files one owner (a property itself, or one building, layout, space or asset) may hold.
    max_files_per_owner: int = 40

    supabase_jwt_audience: str = "authenticated"
    supabase_jwt_secret: SecretStr | None = None

    cors_allowed_origins: Annotated[list[str], NoDecode] = []

    # Calendar dates (occupancy start/end, "today") are interpreted in this zone.
    # Timestamps are always stored and returned in UTC.
    business_timezone: str = "Asia/Kolkata"

    @field_validator("business_timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except Exception:
            raise ValueError("is not a valid IANA time zone name") from None
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: SecretStr) -> SecretStr:
        # Messages here must never echo the value: it contains the password.
        try:
            url = make_url(value.get_secret_value())
        except Exception:
            raise ValueError("is not a valid PostgreSQL connection URL") from None
        if url.drivername not in _ACCEPTED_DRIVERS:
            raise ValueError(
                "must use postgresql:// or postgresql+psycopg:// (psycopg 3 is the only driver)"
            )
        if not url.host or not url.database:
            raise ValueError("must include a host and a database name")
        return value

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip().rstrip("/") for item in value.split(",") if item.strip()]
        return value

    @field_validator("cors_allowed_origins")
    @classmethod
    def _check_origins(cls, value: list[str]) -> list[str]:
        for origin in value:
            if "*" in origin:
                raise ValueError(
                    "wildcard origins are not allowed; list each frontend origin explicitly"
                )
            if not origin.startswith(("http://", "https://")):
                raise ValueError("each origin must start with http:// or https://")
        return value

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


def load_settings(env_file: Path | None = ENV_FILE) -> Settings:
    try:
        return Settings(_env_file=env_file)
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(part) for part in error['loc']).upper()}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        ]
        raise ConfigurationError(
            "Invalid backend configuration. Fix these settings in the environment or .env "
            "(see .env.example):\n  - " + "\n  - ".join(problems)
        ) from None


@lru_cache
def get_settings() -> Settings:
    # Tests configure everything through environment variables so a developer's
    # real .env (and the database it points to) is never picked up.
    testing = os.environ.get("APP_ENV", "").lower() == "test"
    return load_settings(None if testing else ENV_FILE)
