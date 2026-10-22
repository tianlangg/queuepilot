"""Validated configuration loaded independently for each application."""

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)
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
    websocket_origins: str = "http://127.0.0.1:8000,http://localhost:8000"
    auth_requests_per_minute: Annotated[int, Field(ge=1, le=10000)] = 30
    read_requests_per_minute: Annotated[int, Field(ge=1, le=10000)] = 300
    write_requests_per_minute: Annotated[int, Field(ge=1, le=10000)] = 120
    websocket_connection_limit: Annotated[int, Field(ge=1, le=10000)] = 500

    @model_validator(mode="after")
    def validate_production(self) -> "Settings":
        if self.environment == "production":
            origins = self.websocket_origins.split(",")
            if any(
                urlsplit(origin).scheme != "https"
                or urlsplit(origin).hostname in {"localhost", "127.0.0.1"}
                for origin in origins
            ):
                raise ValueError(
                    "Production requires explicit non-local HTTPS browser origins"
                )
            if self.token_secret.get_secret_value().startswith(
                ("REPLACE_", "ci-test-", "test-only-")
            ):
                raise ValueError("Production requires a unique token secret")
        return self

    @field_validator("websocket_origins")
    @classmethod
    def validate_websocket_origins(cls, value: str) -> str:
        origins = [part.strip() for part in value.split(",")]
        if not origins or any(
            not origin
            or origin == "*"
            or urlsplit(origin).scheme not in {"http", "https"}
            or not urlsplit(origin).hostname
            or urlsplit(origin).path not in {"", "/"}
            or urlsplit(origin).query
            or urlsplit(origin).fragment
            for origin in origins
        ):
            raise ValueError("Configure explicit HTTP(S) WebSocket origins")
        return ",".join(origins)

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
