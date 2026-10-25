from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from collections import deque
from typing import Deque

from fastapi import Request, Response

from app.config import Settings
from app.util import utc_now

COOKIE_NAME = "np_session"
COOKIE_MAX_AGE = 30 * 24 * 60 * 60
ROOM_ATTEMPT_LIMIT = 5
MODERATOR_ATTEMPT_LIMIT = 5
ATTEMPT_WINDOW_SECONDS = 60


def _digest(salt: str, value: str) -> str:
    return hashlib.sha256(f"{salt}:{value}".encode("utf-8")).hexdigest()


def device_hash(settings: Settings, device: str) -> str:
    return _digest(settings.identity_salt, device)


def ip_hash(settings: Settings, ip: str) -> str:
    return _digest(settings.identity_salt, ip)


def session_hash(settings: Settings, token: str) -> str:
    return _digest(settings.identity_salt, token)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def code_matches(provided: str, configured: str) -> bool:
    return secrets.compare_digest(
        provided.strip().encode("utf-8"), configured.encode("utf-8")
    )


def client_ip(request: Request) -> str:
    if request.client is None:
        return "unknown"
    return request.client.host


def request_device_id(request: Request) -> str | None:
    value = request.headers.get("x-device-id", "").strip()
    if not value or len(value) > 200:
        return None
    return value


class FailureLimiter:
    """Small in-memory sliding-window limiter for access-code attempts."""

    def __init__(self) -> None:
        self._failures: dict[str, Deque[float]] = {}

    def _prune(self, key: str, window: int) -> Deque[float]:
        bucket = self._failures.setdefault(key, deque())
        cutoff = time.monotonic() - window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        return bucket

    def is_limited(self, key: str, limit: int, window: int = ATTEMPT_WINDOW_SECONDS) -> bool:
        return len(self._prune(key, window)) >= limit

    def record(self, key: str, window: int = ATTEMPT_WINDOW_SECONDS) -> None:
        self._prune(key, window).append(time.monotonic())


def resolve_guest(
    request: Request, conn: sqlite3.Connection, settings: Settings
) -> sqlite3.Row | None:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        guest = conn.execute(
            "SELECT * FROM guests WHERE session_hash = ?",
            (session_hash(settings, token),),
        ).fetchone()
        if guest is not None:
            conn.execute(
                "UPDATE guests SET last_seen_at = ? WHERE id = ?",
                (utc_now(), guest["id"]),
            )
            return guest

    device = request_device_id(request)
    if not device:
        return None

    guest = conn.execute(
        "SELECT * FROM guests WHERE device_hash = ?",
        (device_hash(settings, device),),
    ).fetchone()
    if guest is None:
        return None

    token = new_session_token()
    conn.execute(
        "UPDATE guests SET session_hash = ?, last_seen_at = ? WHERE id = ?",
        (session_hash(settings, token), utc_now(), guest["id"]),
    )
    request.state.new_session_token = token
    return conn.execute("SELECT * FROM guests WHERE id = ?", (guest["id"],)).fetchone()


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
