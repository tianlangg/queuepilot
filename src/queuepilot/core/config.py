"""Validated configuration loaded independently for each application."""

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, StringConstraints, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseSettings):
    """Environment variables override an optional .env in the working directory."""

    model_config = SettingsConfigDict(
        env_prefix="QUEUEPILOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    environment: Literal["development", "test", "production"]
    app_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] = (
        "QueuePilot"
    )
    database_url: SecretStr
    redis_url: SecretStr
    token_secret: Annotated[SecretStr, Field(min_length=32)]
    token_issuer: Annotated[str, StringConstraints(min_length=1)] = "queuepilot"
    token_audience: Annotated[str, StringConstraints(min_length=1)] = "queuepilot-api"
    access_token_minutes: Annotated[int, Field(ge=1, le=60)] = 15
    refresh_token_days: Annotated[int, Field(ge=1, le=90)] = 30
    probe_timeout_seconds: Annotated[float, Field(gt=0, le=10, allow_inf_nan=False)] = 2

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
            valid = (
                url.drivername == "postgresql+psycopg"
                and bool(url.host)
                and bool(url.database)
                and url.port != 0
            )
        except (ArgumentError, ValueError):
            valid = False
        if not valid:
            raise ValueError("Use a postgresql+psycopg URL with host and database")
        return value

    @field_validator("redis_url")
    @classmethod
    def validate_redis_url(cls, value: SecretStr) -> SecretStr:
        try:
            url = urlsplit(value.get_secret_value())
            valid = (
                url.scheme in {"redis", "rediss"}
                and bool(url.hostname)
                and url.port != 0
                and (not url.path or url.path.removeprefix("/").isdigit())
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Use a redis or rediss URL with host and numeric database")
        return value
