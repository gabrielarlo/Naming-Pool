from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable

from app.config import Settings
from app.security import (
    MODERATOR_ATTEMPT_LIMIT,
    ROOM_ATTEMPT_LIMIT,
    FailureLimiter,
    code_matches,
    device_hash,
    ip_hash,
    new_session_token,
    session_hash,
)
from app.util import normalized_name, utc_before, utc_now

MAX_SUGGESTIONS = 5
VOTE_ALLOWANCES = {"stage1": 10, "stage2": 3, "final": 1, "tiebreak": 1}
RESULT_STAGES = ("stage1", "stage2", "final", "tiebreak")
PRESENCE_WINDOW_SECONDS = 15


class ApiError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def get_event(conn: sqlite3.Connection) -> sqlite3.Row:
    event = conn.execute("SELECT * FROM event WHERE id = 1").fetchone()
    if event is None:
        raise RuntimeError("Event row is missing; database was not initialized")
    return event


def _get_guest(conn: sqlite3.Connection, guest_id: int) -> sqlite3.Row:
    guest = conn.execute("SELECT * FROM guests WHERE id = ?", (guest_id,)).fetchone()
    if guest is None:
        raise RuntimeError("Guest row disappeared")
    return guest


def join_room(
    conn: sqlite3.Connection,
    settings: Settings,
    limiter: FailureLimiter,
    code: str,
    device: str | None,
    ip: str,
    current_guest: sqlite3.Row | None,
) -> tuple[sqlite3.Row, str]:
    if not device:
        raise ApiError(400, "A device identifier is required.")
    if current_guest is not None:
        # A browser may have lost its device token but still hold a valid session.
        guest = current_guest
    else:
        guest = conn.execute(
            "SELECT * FROM guests WHERE device_hash = ?",
            (device_hash(settings, device),),
        ).fetchone()

    ip_value_hash = ip_hash(settings, ip)
    limit_key = f"room:{ip_value_hash}"
    if limiter.is_limited(limit_key, ROOM_ATTEMPT_LIMIT):
        raise ApiError(429, "Too many attempts. Try again in a minute.")

    if not code_matches(code, settings.room_code):
        limiter.record(limit_key)
        raise ApiError(401, "Invalid room code.")

    token = new_session_token()
    now = utc_now()

    with transaction(conn):
        if guest is None:
            conn.execute(
                """
                INSERT INTO guests
                    (device_hash, session_hash, ip_hash, is_moderator, created_at, last_seen_at)
                VALUES (?, ?, ?, 0, ?, ?)
                """,
                (
                    device_hash(settings, device),
                    session_hash(settings, token),
                    ip_value_hash,
                    now,
                    now,
                ),
            )
            guest_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        else:
            guest_id = int(guest["id"])
            conn.execute(
                """
                UPDATE guests
                SET session_hash = ?, ip_hash = ?, last_seen_at = ?
                WHERE id = ?
                """,
                (session_hash(settings, token), ip_value_hash, now, guest_id),
            )

    return _get_guest(conn, guest_id), token


def unlock_moderator(
    conn: sqlite3.Connection,
    settings: Settings,
    limiter: FailureLimiter,
    guest: sqlite3.Row | None,
    code: str,
    ip: str,
) -> sqlite3.Row:
    if guest is None:
        raise ApiError(401, "Join the room before unlocking moderator controls.")
    if bool(guest["is_moderator"]):
        return guest

    limit_key = f"moderator:{ip_hash(settings, ip)}"
    if limiter.is_limited(limit_key, MODERATOR_ATTEMPT_LIMIT):
        raise ApiError(429, "Too many moderator attempts. Try again in a minute.")

    if not code_matches(code, settings.moderator_code):
        limiter.record(limit_key)
        raise ApiError(401, "Invalid moderator code.")

    with transaction(conn):
        conn.execute(
            "UPDATE guests SET is_moderator = 1, last_seen_at = ? WHERE id = ?",
            (utc_now(), guest["id"]),
        )
    return _get_guest(conn, int(guest["id"]))


def _own_suggestions(conn: sqlite3.Connection, guest_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM suggestions WHERE guest_id = ? ORDER BY id",
        (guest_id,),
    ).fetchall()


def _suggestion_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM suggestions").fetchone()[0])


def submit_suggestion(
    conn: sqlite3.Connection,
    guest: sqlite3.Row | None,
    name: str,
    reason: str,
) -> sqlite3.Row:
    if guest is None:
        raise ApiError(401, "Join the room before submitting a suggestion.")

    name = name.strip()
    reason = reason.strip()
    if not name:
        raise ApiError(400, "A name is required.")
    if len(name) > 60:
        raise ApiError(400, "Names must be 60 characters or fewer.")
    if not reason:
        raise ApiError(400, "A reason is required.")
    if len(reason) > 500:
        raise ApiError(400, "Reasons must be 500 characters or fewer.")

    name_key = normalized_name(name)
    now = utc_now()

    with transaction(conn):
        event = get_event(conn)
        if event["stage"] != "submission":
            raise ApiError(409, "Submissions are closed.")

        count = int(
            conn.execute(
                "SELECT COUNT(*) FROM suggestions WHERE guest_id = ?",
                (guest["id"],),
            ).fetchone()[0]
        )
        if count >= MAX_SUGGESTIONS:
            raise ApiError(409, "You have reached the five-suggestion limit.")

        duplicate = conn.execute(
            "SELECT id FROM suggestions WHERE name_key = ?", (name_key,)
        ).fetchone()
        if duplicate is not None:
            raise ApiError(409, "That name is already in the pool.")

        try:
            conn.execute(
                """
                INSERT INTO suggestions (guest_id, name, name_key, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (guest["id"], name, name_key, reason, now),
            )
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, "That name is already in the pool.") from exc

        suggestion_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    return conn.execute(
        "SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)
    ).fetchone()


def _candidates(conn: sqlite3.Connection, stage: str) -> list[sqlite3.Row]:
    if stage == "stage1":
        return conn.execute(
            "SELECT * FROM suggestions ORDER BY created_at, id"
        ).fetchall()

    if stage == "stage2":
        return conn.execute(
            """
            SELECT s.*
            FROM suggestions s
            JOIN stage_results r ON r.suggestion_id = s.id
            WHERE r.stage = 'stage1' AND r.advanced = 1
            ORDER BY r.rank, s.id
            """
        ).fetchall()

    if stage == "final":
        return conn.execute(
            """
            SELECT s.*
            FROM suggestions s
            JOIN stage_results r ON r.suggestion_id = s.id
            WHERE r.stage = 'stage2' AND r.advanced = 1
            ORDER BY r.rank, s.id
            """
        ).fetchall()

    if stage == "tiebreak":
        return conn.execute(
            """
            SELECT s.*
            FROM suggestions s
            JOIN stage_results r ON r.suggestion_id = s.id
            WHERE r.stage = 'final'
            ORDER BY r.rank, s.id
            """
        ).fetchall()

    if stage == "closed":
        return conn.execute(
            """
            SELECT s.*
            FROM suggestions s
            JOIN stage_results r ON r.suggestion_id = s.id
            WHERE r.stage = 'final'
            ORDER BY r.rank, s.id
            """
        ).fetchall()

    return []


def _ranked(
    conn: sqlite3.Connection, stage: str, candidates: Iterable[sqlite3.Row]
) -> list[tuple[sqlite3.Row, int, int]]:
    counts = {
        int(row["suggestion_id"]): int(row["votes"])
        for row in conn.execute(
            """
            SELECT suggestion_id, COUNT(*) AS votes
            FROM votes
            WHERE stage = ?
            GROUP BY suggestion_id
            """,
            (stage,),
        )
    }
    ordered = sorted(
        candidates,
        key=lambda row: (-counts.get(int(row["id"]), 0), row["created_at"], row["id"]),
    )
    return [
        (row, counts.get(int(row["id"]), 0), rank)
        for rank, row in enumerate(ordered, start=1)
    ]


def _insert_results(
    conn: sqlite3.Connection,
    stage: str,
    ranked: Iterable[tuple[sqlite3.Row, int, int]],
    advanced_ids: set[int],
) -> None:
    for row, count, rank in ranked:
        conn.execute(
            """
            INSERT INTO stage_results (stage, suggestion_id, vote_count, rank, advanced)
            VALUES (?, ?, ?, ?, ?)
            """,
            (stage, row["id"], count, rank, 1 if int(row["id"]) in advanced_ids else 0),
        )


def _set_stage(
    conn: sqlite3.Connection,
    expected: str,
    new_stage: str,
    winner_id: int | None = None,
) -> None:
    now = utc_now()
    cursor = conn.execute(
        """
        UPDATE event
        SET stage = ?, winner_suggestion_id = ?, stage_started_at = ?, updated_at = ?
        WHERE id = 1 AND stage = ?
        """,
        (new_stage, winner_id, now, now, expected),
    )
    if cursor.rowcount != 1:
        raise ApiError(409, "The event changed while this action was being processed.")


def _reset_tiebreak(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM votes WHERE stage = 'tiebreak'")
    conn.execute("DELETE FROM stage_results WHERE stage = 'tiebreak'")
    now = utc_now()
    conn.execute(
        "UPDATE event SET stage_started_at = ?, updated_at = ? WHERE id = 1 AND stage = 'tiebreak'",
        (now, now),
    )


def advance(
    conn: sqlite3.Connection,
    guest: sqlite3.Row | None,
    expected_stage: str,
    min_suggestions: int,
) -> str:
    if guest is None:
        raise ApiError(401, "Join the room before controlling the event.")
    if not bool(guest["is_moderator"]):
        raise ApiError(403, "Moderator access required.")

    with transaction(conn):
        event = get_event(conn)
        stage = event["stage"]
        if expected_stage != stage:
            raise ApiError(409, "The stage already changed. Refresh and try again.")

        if stage == "submission":
            total = _suggestion_count(conn)
            if total < min_suggestions:
                remaining = min_suggestions - total
                raise ApiError(
                    409,
                    f"Stage 1 needs at least {min_suggestions} suggestions; "
                    f"the pool has {total}. Add {remaining} more before starting.",
                )
            _set_stage(conn, "submission", "stage1")
            return "Stage 1 is open."

        if stage == "stage1":
            candidates = _candidates(conn, "stage1")
            ranked = _ranked(conn, "stage1", candidates)
            advanced = {int(row["id"]) for row, _, rank in ranked if rank <= 10}
            _insert_results(conn, "stage1", ranked, advanced)
            _set_stage(conn, "stage1", "stage2")
            return "Stage 1 closed. Stage 2 is open."

        if stage == "stage2":
            candidates = _candidates(conn, "stage2")
            if not candidates:
                raise ApiError(409, "There are no candidates to advance.")
            ranked = _ranked(conn, "stage2", candidates)
            advanced = {int(row["id"]) for row, _, rank in ranked if rank <= 3}
            _insert_results(conn, "stage2", ranked, advanced)
            _set_stage(conn, "stage2", "final")
            return "Stage 2 closed. The final is open."

        if stage == "final":
            candidates = _candidates(conn, "final")
            if not candidates:
                raise ApiError(409, "There are no candidates in the final.")
            ranked = _ranked(conn, "final", candidates)
            top_count = ranked[0][1]
            leaders = [row for row, count, _ in ranked if count == top_count]
            if len(leaders) == 1:
                winner_id = int(leaders[0]["id"])
                _insert_results(conn, "final", ranked, {winner_id})
                _set_stage(conn, "final", "closed", winner_id)
                return "The final is closed. A winner has been selected."
            _insert_results(conn, "final", ranked, set())
            _set_stage(conn, "final", "tiebreak")
            return "The final is tied. All finalists advance to a tiebreak."

        if stage == "tiebreak":
            candidates = _candidates(conn, "tiebreak")
            if not candidates:
                raise ApiError(409, "There are no candidates in the tiebreak.")
            ranked = _ranked(conn, "tiebreak", candidates)
            top_count = ranked[0][1]
            leaders = [row for row, count, _ in ranked if count == top_count]
            if len(leaders) == 1:
                winner_id = int(leaders[0]["id"])
                _insert_results(conn, "tiebreak", ranked, {winner_id})
                _set_stage(conn, "tiebreak", "closed", winner_id)
                return "The tiebreak is closed. A winner has been selected."
            _reset_tiebreak(conn)
            return "The finalists are still tied. Votes were reset; run the tiebreak again."

        raise ApiError(409, "The event is already closed.")


def set_votes(
    conn: sqlite3.Connection,
    guest: sqlite3.Row | None,
    stage: str,
    suggestion_ids: list[int],
) -> None:
    if guest is None:
        raise ApiError(401, "Join the room before voting.")
    if stage not in VOTE_ALLOWANCES:
        raise ApiError(400, "Unknown voting stage.")

    unique_ids = list(dict.fromkeys(suggestion_ids))
    allowance = VOTE_ALLOWANCES[stage]
    if len(unique_ids) > allowance:
        raise ApiError(400, f"You can select at most {allowance} names in this stage.")

    with transaction(conn):
        event = get_event(conn)
        if event["stage"] != stage:
            raise ApiError(409, "This stage is not open for voting.")

        candidate_ids = {int(row["id"]) for row in _candidates(conn, stage)}
        if any(int(suggestion_id) not in candidate_ids for suggestion_id in unique_ids):
            raise ApiError(400, "One or more selections are not candidates in this stage.")

        conn.execute(
            "DELETE FROM votes WHERE guest_id = ? AND stage = ?",
            (guest["id"], stage),
        )
        now = utc_now()
        for suggestion_id in unique_ids:
            conn.execute(
                """
                INSERT INTO votes (guest_id, suggestion_id, stage, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (guest["id"], suggestion_id, stage, now),
            )


def _my_votes(conn: sqlite3.Connection, guest_id: int, stage: str) -> list[int]:
    return [
        int(row["suggestion_id"])
        for row in conn.execute(
            "SELECT suggestion_id FROM votes WHERE guest_id = ? AND stage = ? ORDER BY id",
            (guest_id, stage),
        )
    ]


def _voted_guest_count(conn: sqlite3.Connection, stage: str) -> int:
    row = conn.execute(
        "SELECT COUNT(DISTINCT guest_id) FROM votes WHERE stage = ?",
        (stage,),
    ).fetchone()
    return int(row[0])


def _online_guest_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM guests WHERE last_seen_at >= ?",
        (utc_before(PRESENCE_WINDOW_SECONDS),),
    ).fetchone()
    return int(row[0])


def _results_payload(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for stage in RESULT_STAGES:
        rows = conn.execute(
            """
            SELECT s.id, s.name, s.reason, r.vote_count, r.rank, r.advanced
            FROM stage_results r
            JOIN suggestions s ON s.id = r.suggestion_id
            WHERE r.stage = ?
            ORDER BY r.rank
            """,
            (stage,),
        ).fetchall()
        if not rows:
            continue
        entries = []
        for row in rows:
            entry: dict[str, Any] = {
                "id": int(row["id"]),
                "name": row["name"],
                "votes": int(row["vote_count"]),
                "rank": int(row["rank"]),
                "advanced": bool(row["advanced"]),
            }
            if stage != "stage1":
                entry["reason"] = row["reason"]
            entries.append(entry)
        results.append({"stage": stage, "entries": entries})
    return results


def get_state(
    conn: sqlite3.Connection,
    guest: sqlite3.Row | None,
    min_suggestions: int,
) -> dict[str, Any]:
    if guest is None:
        return {"joined": False}

    event = get_event(conn)
    stage = event["stage"]
    own = _own_suggestions(conn, int(guest["id"]))
    total = _suggestion_count(conn)
    voted_guests = _voted_guest_count(conn, stage) if stage in VOTE_ALLOWANCES else 0
    online_guests = _online_guest_count(conn)

    if stage == "submission":
        own_payload = [
            {
                "id": int(row["id"]),
                "name": row["name"],
                "reason": row["reason"],
            }
            for row in own
        ]
        candidates: list[dict[str, Any]] = []
    else:
        own_payload = []
        candidate_stage = "closed" if stage == "closed" else stage
        candidates = []
        for row in _candidates(conn, candidate_stage):
            candidate: dict[str, Any] = {"id": int(row["id"]), "name": row["name"]}
            if candidate_stage != "stage1":
                candidate["reason"] = row["reason"]
            candidates.append(candidate)

    if stage in VOTE_ALLOWANCES:
        my_votes = _my_votes(conn, int(guest["id"]), stage)
        allowance: int | None = VOTE_ALLOWANCES[stage]
        remaining: int | None = max(allowance - len(my_votes), 0)
    else:
        my_votes = []
        allowance = None
        remaining = None

    winner = None
    if event["winner_suggestion_id"] is not None:
        winner_row = conn.execute(
            "SELECT id, name, reason FROM suggestions WHERE id = ?",
            (event["winner_suggestion_id"],),
        ).fetchone()
        if winner_row is not None:
            winner = {
                "id": int(winner_row["id"]),
                "name": winner_row["name"],
                "reason": winner_row["reason"],
            }

    return {
        "joined": True,
        "is_moderator": bool(guest["is_moderator"]),
        "stage": stage,
        "total_suggestions": total,
        "voted_guests": voted_guests,
        "online_guests": online_guests,
        "min_suggestions": min_suggestions,
        "min_suggestions_met": total >= min_suggestions,
        "suggestion_limit": MAX_SUGGESTIONS,
        "own_suggestion_count": len(own),
        "own_suggestions": own_payload,
        "can_submit": stage == "submission" and len(own) < MAX_SUGGESTIONS,
        "candidates": candidates,
        "my_votes": my_votes,
        "vote_allowance": allowance,
        "remaining_votes": remaining,
        "results": _results_payload(conn),
        "winner": winner,
        "can_advance": bool(guest["is_moderator"])
        and stage != "closed"
        and (stage != "submission" or total >= min_suggestions),
    }
