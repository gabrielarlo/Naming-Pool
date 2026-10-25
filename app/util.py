from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta, timezone


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_now() -> str:
    return _iso(datetime.now(timezone.utc))


def utc_before(seconds: int) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(seconds=seconds))


def normalized_name(name: str) -> str:
    """Return the key used for case-insensitive duplicate detection."""
    folded = unicodedata.normalize("NFKC", name).casefold()
    return " ".join(folded.split())
