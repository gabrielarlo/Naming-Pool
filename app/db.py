from __future__ import annotations

import sqlite3
from pathlib import Path

from app.util import utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS guests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_hash TEXT NOT NULL UNIQUE,
    session_hash TEXT NOT NULL UNIQUE,
    ip_hash TEXT NOT NULL,
    is_moderator INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guest_id INTEGER NOT NULL REFERENCES guests(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    stage TEXT NOT NULL CHECK (
        stage IN ('submission', 'stage1', 'stage2', 'final', 'tiebreak', 'closed')
    ),
    winner_suggestion_id INTEGER REFERENCES suggestions(id) ON DELETE SET NULL,
    stage_started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS votes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guest_id INTEGER NOT NULL REFERENCES guests(id) ON DELETE CASCADE,
    suggestion_id INTEGER NOT NULL REFERENCES suggestions(id) ON DELETE CASCADE,
    stage TEXT NOT NULL CHECK (stage IN ('stage1', 'stage2', 'final', 'tiebreak')),
    created_at TEXT NOT NULL,
    UNIQUE (guest_id, suggestion_id, stage)
);

CREATE TABLE IF NOT EXISTS stage_results (
    stage TEXT NOT NULL CHECK (stage IN ('stage1', 'stage2', 'final', 'tiebreak')),
    suggestion_id INTEGER NOT NULL REFERENCES suggestions(id) ON DELETE CASCADE,
    vote_count INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    advanced INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (stage, suggestion_id)
);

CREATE INDEX IF NOT EXISTS idx_guests_last_seen ON guests (last_seen_at);
CREATE INDEX IF NOT EXISTS idx_suggestions_guest ON suggestions (guest_id);
CREATE INDEX IF NOT EXISTS idx_votes_stage_suggestion ON votes (stage, suggestion_id);
CREATE INDEX IF NOT EXISTS idx_votes_guest_stage ON votes (guest_id, stage);
CREATE INDEX IF NOT EXISTS idx_stage_results_stage_advanced
    ON stage_results (stage, advanced, rank);
"""


def connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        db_path, timeout=5.0, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        now = utc_now()
        conn.execute(
            """
            INSERT OR IGNORE INTO event (id, stage, stage_started_at, updated_at)
            VALUES (1, 'submission', ?, ?)
            """,
            (now, now),
        )
    finally:
        conn.close()
