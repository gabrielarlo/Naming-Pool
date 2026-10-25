from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    room_code: str
    moderator_code: str
    identity_salt: str
    min_suggestions: int
    db_path: str = "/data/naming_pool.db"
    cookie_secure: bool = True


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _positive_int(name: str) -> int:
    raw = _required(name)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a positive integer.") from exc
    if value < 1:
        raise ConfigError(f"{name} must be a positive integer.")
    return value


def _as_bool(value: str) -> bool:
    return value.strip().lower() not in {"0", "false", "no", "off"}


def load_settings() -> Settings:
    room_code = _required("ROOM_CODE")
    moderator_code = _required("MODERATOR_CODE")
    identity_salt = _required("IDENTITY_SALT")
    min_suggestions = _positive_int("MIN_SUGGESTIONS")

    if room_code == moderator_code:
        raise ConfigError("ROOM_CODE and MODERATOR_CODE must be different")

    db_path = os.environ.get("DB_PATH", "/data/naming_pool.db").strip()
    if not db_path:
        db_path = "/data/naming_pool.db"

    return Settings(
        room_code=room_code,
        moderator_code=moderator_code,
        identity_salt=identity_salt,
        min_suggestions=min_suggestions,
        db_path=db_path,
        cookie_secure=_as_bool(os.environ.get("COOKIE_SECURE", "true")),
    )
