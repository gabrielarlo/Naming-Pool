from __future__ import annotations

import logging
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import ConfigError, Settings, load_settings
from app.db import connect, init_db
from app.main import create_app
from app.security import device_hash, ip_hash
from app.util import utc_before, utc_now

ROOM_CODE = "room-test-code"
MODERATOR_CODE = "moderator-test-code"
SALT = "test-salt-0123456789abcdef"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        room_code=ROOM_CODE,
        moderator_code=MODERATOR_CODE,
        identity_salt=SALT,
        min_suggestions=1,
        db_path=str(tmp_path / "naming-pool.db"),
        cookie_secure=False,
    )


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


def join(client: TestClient, code: str = ROOM_CODE):
    return client.post("/api/join", json={"code": code})


def advance(client: TestClient, stage: str):
    return client.post("/api/moderator/advance", json={"stage": stage})


def unlock(client: TestClient):
    return client.post("/api/moderator", json={"code": MODERATOR_CODE})


def suggest(client: TestClient, name: str, reason: str = "Because it fits."):
    return client.post("/api/suggestions", json={"name": name, "reason": reason})


def vote(client: TestClient, stage: str, suggestion_ids: list[int]):
    return client.put(
        "/api/votes",
        json={"stage": stage, "suggestion_ids": suggestion_ids},
    )


def state(client: TestClient) -> dict:
    return client.get("/api/state").json()


def add_guest(
    stack: ExitStack,
    app,
    device: str,
    ip: str = "203.0.113.10",
) -> TestClient:
    client = stack.enter_context(
        TestClient(app, client=(ip, 50000), headers={"X-Device-Id": device})
    )
    response = join(client)
    assert response.status_code == 200, response.text
    return client


def expire_guest(settings: Settings, device: str) -> None:
    conn = connect(settings.db_path)
    try:
        conn.execute(
            "UPDATE guests SET last_seen_at = ? WHERE device_hash = ?",
            ("2000-01-01T00:00:00.000Z", device_hash(settings, device)),
        )
    finally:
        conn.close()


def add_names(client: TestClient, names: list[str]) -> list[dict]:
    added = []
    for name in names:
        response = suggest(client, name, f"Reason for {name}.")
        assert response.status_code == 200, response.text
        added.extend(response.json()["own_suggestions"])
    return added


def test_config_requires_settings_and_trims_codes(monkeypatch):
    for name in (
        "ROOM_CODE",
        "MODERATOR_CODE",
        "IDENTITY_SALT",
        "MIN_SUGGESTIONS",
        "DB_PATH",
        "COOKIE_SECURE",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("ROOM_CODE", "  room  ")
    monkeypatch.setenv("MODERATOR_CODE", "mod")
    monkeypatch.setenv("IDENTITY_SALT", "salt")
    monkeypatch.setenv("MIN_SUGGESTIONS", "3")
    loaded = load_settings()
    assert loaded.room_code == "room"
    assert loaded.min_suggestions == 3
    assert loaded.cookie_secure is True

    for required in ("ROOM_CODE", "MODERATOR_CODE", "IDENTITY_SALT", "MIN_SUGGESTIONS"):
        monkeypatch.delenv(required)
        with pytest.raises(ConfigError, match=required):
            load_settings()
        monkeypatch.setenv("ROOM_CODE", "room")
        monkeypatch.setenv("MODERATOR_CODE", "mod")
        monkeypatch.setenv("IDENTITY_SALT", "salt")
        monkeypatch.setenv("MIN_SUGGESTIONS", "3")

    monkeypatch.setenv("MODERATOR_CODE", "room")
    with pytest.raises(ConfigError, match="must be different"):
        load_settings()

    monkeypatch.setenv("MODERATOR_CODE", "mod")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    assert load_settings().cookie_secure is False


def test_config_rejects_invalid_min_suggestions(monkeypatch):
    monkeypatch.setenv("ROOM_CODE", "room")
    monkeypatch.setenv("MODERATOR_CODE", "mod")
    monkeypatch.setenv("IDENTITY_SALT", "salt")

    for value in ("", "   ", "abc", "0", "-1"):
        monkeypatch.setenv("MIN_SUGGESTIONS", value)
        with pytest.raises(ConfigError, match="MIN_SUGGESTIONS"):
            load_settings()


def test_utc_before_returns_offset_timestamp():
    now = datetime.fromisoformat(utc_now().replace("Z", "+00:00"))
    cutoff = datetime.fromisoformat(utc_before(15).replace("Z", "+00:00"))
    assert timedelta(seconds=14) <= now - cutoff <= timedelta(seconds=16)


def test_db_initialization_is_idempotent(tmp_path: Path):
    db_path = str(tmp_path / "db.sqlite")
    init_db(db_path)
    init_db(db_path)

    conn = connect(db_path)
    try:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "guests",
            "suggestions",
            "event",
            "votes",
            "stage_results",
        }.issubset(tables)
        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert "idx_guests_last_seen" in indexes
        rows = conn.execute("SELECT * FROM event").fetchall()
        assert len(rows) == 1
        assert rows[0]["stage"] == "submission"
    finally:
        conn.close()


def test_health_static_and_unknown_api(app):
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/health").json() == {"status": "ok"}

        index = client.get("/")
        assert index.status_code == 200
        assert "Naming Pool" in index.text

        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/styles.css").status_code == 200
        assert client.get("/static/vendor/alpine.min.js").status_code == 200

        assert client.get("/api/does-not-exist").status_code == 404
        assert client.get("/favicon.ico").status_code == 404

        api_headers = client.get("/api/health").headers
        assert api_headers["x-content-type-options"] == "nosniff"
        assert api_headers["x-frame-options"] == "DENY"
        assert api_headers["referrer-policy"] == "no-referrer"
        assert api_headers["cache-control"] == "no-store"
        page_headers = client.get("/").headers
        assert page_headers["x-content-type-options"] == "nosniff"
        assert page_headers["x-frame-options"] == "DENY"
        assert page_headers["referrer-policy"] == "no-referrer"


def test_swagger_docs_are_disabled(app):
    with TestClient(app) as client:
        for path in ("/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"):
            assert client.get(path).status_code == 404
        assert client.get("/api/health").json() == {"status": "ok"}


def test_join_identity_cookie_loss_and_code_secrecy(app, settings, caplog):
    caplog.set_level(logging.DEBUG)
    with ExitStack() as stack:
        client = stack.enter_context(
            TestClient(
                app,
                client=("198.51.100.30", 50000),
                headers={"X-Device-Id": "device-alpha"},
            )
        )

        assert state(client) == {"joined": False}
        assert client.post("/api/join", json={}).status_code == 400
        assert join(client, "wrong").status_code == 401
        assert join(client, "wrong-🔒").status_code == 401
        assert state(client) == {"joined": False}

        joined = join(client, f"  {ROOM_CODE}  ")
        assert joined.status_code == 200
        assert joined.json()["joined"] is True
        assert joined.json()["is_moderator"] is False
        assert client.cookies.get("np_session")

        body = joined.text
        assert ROOM_CODE not in body
        assert MODERATOR_CODE not in body
        assert "device-alpha" not in body
        assert "198.51.100.30" not in body
        assert device_hash(settings, "device-alpha") not in body
        assert ip_hash(settings, "198.51.100.30") not in body
        assert ROOM_CODE not in caplog.text
        assert MODERATOR_CODE not in caplog.text

        assert suggest(client, "Alpha", "The first letter.").status_code == 200

        # A browser that loses the cookie but keeps the device token keeps its guest.
        restored = stack.enter_context(
            TestClient(
                app,
                client=("198.51.100.30", 50000),
                headers={"X-Device-Id": "device-alpha"},
            )
        )
        restored_state = state(restored)
        assert restored_state["joined"] is True
        assert restored_state["own_suggestion_count"] == 1
        assert restored_state["is_moderator"] is False
        assert restored.cookies.get("np_session")

        # Another device on the same IP is still a different guest.
        other = stack.enter_context(
            TestClient(
                app,
                client=("198.51.100.30", 50000),
                headers={"X-Device-Id": "device-beta"},
            )
        )
        assert state(other) == {"joined": False}

        # The room code alone never grants moderator status.
        assert state(client)["is_moderator"] is False
        assert advance(client, "submission").status_code == 403


def test_join_and_moderator_rate_limits(app):
    with ExitStack() as stack:
        limited = stack.enter_context(
            TestClient(
                app,
                client=("198.51.100.40", 50000),
                headers={"X-Device-Id": "rate-device"},
            )
        )
        for _ in range(5):
            assert join(limited, "bad-code").status_code == 401
        assert join(limited, ROOM_CODE).status_code == 429

        elsewhere = stack.enter_context(
            TestClient(
                app,
                client=("198.51.100.41", 50000),
                headers={"X-Device-Id": "other-device"},
            )
        )
        assert join(elsewhere, ROOM_CODE).status_code == 200
        assert join(elsewhere, "bad-code").status_code == 401
        assert join(elsewhere, ROOM_CODE).status_code == 200

        moderator = add_guest(stack, app, "moderator-device", "198.51.100.42")
        for _ in range(5):
            assert (
                moderator.post(
                    "/api/moderator", json={"code": "wrong-moderator"}
                ).status_code
                == 401
            )
        assert unlock(moderator).status_code == 429


def test_submission_validation_cap_duplicate_and_visibility(app):
    with ExitStack() as stack:
        # Shared IP proves the cap belongs to the guest, not the network address.
        g1 = add_guest(stack, app, "device-g1", "198.51.100.50")
        g2 = add_guest(stack, app, "device-g2", "198.51.100.50")
        outsider = stack.enter_context(
            TestClient(
                app,
                client=("198.51.100.51", 50000),
                headers={"X-Device-Id": "device-outsider"},
            )
        )

        assert suggest(outsider, "No entry").status_code == 401

        assert suggest(g2, "Aurora", "Northern lights.").status_code == 200
        duplicate = suggest(g1, "  aurora  ", "Same idea.")
        assert duplicate.status_code == 409
        assert "already in the pool" in duplicate.json()["detail"]

        for payload in (
            {"name": "   ", "reason": "reason"},
            {"name": "Valid", "reason": "   "},
            {"name": "x" * 61, "reason": "reason"},
            {"name": "Valid", "reason": "x" * 501},
        ):
            response = g1.post("/api/suggestions", json=payload)
            assert response.status_code == 400, response.text

        add_names(g1, [f"G1 name {index}" for index in range(1, 6)])
        sixth = suggest(g1, "G1 name 6", "Too many.")
        assert sixth.status_code == 409
        assert "five-suggestion limit" in sixth.json()["detail"]

        add_names(g2, ["<b>Bold</b>", "G2 name 2", "G2 name 3", "G2 name 4"])
        assert suggest(g2, "G2 name 6", "Too many.").status_code == 409

        g1_state = state(g1)
        assert g1_state["own_suggestion_count"] == 5
        assert g1_state["total_suggestions"] == 10
        assert g1_state["can_submit"] is False
        assert "Aurora" not in g1.get("/api/state").text
        assert "<b>Bold</b>" not in g1.get("/api/state").text

        g2_state = state(g2)
        assert g2_state["own_suggestion_count"] == 5
        assert "G1 name 1" not in g2.get("/api/state").text
        bold = next(
            item for item in g2_state["own_suggestions"] if item["name"] == "<b>Bold</b>"
        )
        assert bold["name"] == "<b>Bold</b>"
        raw = suggest(g2, "Different name", "Different reason.")
        assert raw.status_code == 409


def test_submission_closes_and_stage_guard(app):
    with ExitStack() as stack:
        moderator = add_guest(stack, app, "mod-device", "198.51.100.60")
        guest = add_guest(stack, app, "guest-device", "198.51.100.60")

        assert advance(moderator, "submission").status_code == 403
        assert unlock(moderator).status_code == 200
        assert state(moderator)["is_moderator"] is True

        # No suggestions yet, so voting cannot start.
        assert advance(moderator, "submission").status_code == 409
        assert state(moderator)["stage"] == "submission"

        assert suggest(guest, "First name", "First reason.").status_code == 200
        started = advance(moderator, "submission")
        assert started.status_code == 200
        assert started.json()["stage"] == "stage1"

        blocked = suggest(guest, "Late name", "Late reason.")
        assert blocked.status_code == 409
        assert "Submissions are closed" in blocked.json()["detail"]
        assert state(guest)["total_suggestions"] == 1

        # A stale expected stage cannot close the next stage by accident.
        stale = advance(moderator, "submission")
        assert stale.status_code == 409
        assert state(moderator)["stage"] == "stage1"

        # Moderator status survives a lost cookie via the device token.
        restored = stack.enter_context(
            TestClient(
                app,
                client=("198.51.100.60", 50000),
                headers={"X-Device-Id": "mod-device"},
            )
        )
        assert state(restored)["is_moderator"] is True
        assert advance(restored, "stage1").status_code == 200
        assert state(restored)["stage"] == "stage2"
        assert advance(restored, "stage1").status_code == 409


def test_pool_wide_submission_count_and_minimum_gate(settings):
    gate_app = create_app(replace(settings, min_suggestions=3))

    with ExitStack() as stack:
        moderator = add_guest(stack, gate_app, "gate-moderator", "198.51.100.70")
        guest = add_guest(stack, gate_app, "gate-guest", "198.51.100.71")
        assert unlock(moderator).status_code == 200

        initial = state(moderator)
        assert initial["min_suggestions"] == 3
        assert initial["min_suggestions_met"] is False
        assert initial["can_advance"] is False

        assert suggest(moderator, "Gate one", "First gate reason.").status_code == 200
        first = state(moderator)
        assert first["total_suggestions"] == 1
        assert first["own_suggestion_count"] == 1
        assert first["min_suggestions_met"] is False
        assert first["can_advance"] is False

        other_view = state(guest)
        assert other_view["total_suggestions"] == 1
        assert other_view["own_suggestion_count"] == 0
        assert other_view["min_suggestions"] == 3
        assert other_view["min_suggestions_met"] is False
        assert other_view["can_advance"] is False

        below = advance(moderator, "submission")
        assert below.status_code == 409
        assert "at least 3" in below.json()["detail"]
        assert "has 1" in below.json()["detail"]
        assert "Add 2 more" in below.json()["detail"]
        assert state(moderator)["stage"] == "submission"

        add_names(guest, ["Gate two", "Gate three"])
        at_minimum = state(moderator)
        assert at_minimum["total_suggestions"] == 3
        assert at_minimum["min_suggestions_met"] is True
        assert at_minimum["can_advance"] is True
        assert state(guest)["total_suggestions"] == 3
        assert state(guest)["own_suggestion_count"] == 2
        assert state(guest)["min_suggestions_met"] is True
        assert state(guest)["can_advance"] is False

        started = advance(moderator, "submission")
        assert started.status_code == 200
        assert started.json()["stage"] == "stage1"


def test_minimum_gate_allows_above_minimum(settings):
    gate_app = create_app(replace(settings, min_suggestions=2))

    with ExitStack() as stack:
        moderator = add_guest(stack, gate_app, "above-moderator", "198.51.100.72")
        add_names(moderator, ["Above one", "Above two", "Above three"])
        assert unlock(moderator).status_code == 200
        assert state(moderator)["min_suggestions_met"] is True
        assert state(moderator)["can_advance"] is True
        assert advance(moderator, "submission").status_code == 200


def test_full_stage_flow_and_visibility(app):
    with ExitStack() as stack:
        g1 = add_guest(stack, app, "full-g1", "203.0.113.1")
        g2 = add_guest(stack, app, "full-g2", "203.0.113.2")
        g3 = add_guest(stack, app, "full-g3", "203.0.113.3")
        g4 = add_guest(stack, app, "full-g4", "203.0.113.4")

        names = [f"Name {index:02d}" for index in range(1, 13)]
        add_names(g1, names[0:3])
        add_names(g2, names[3:6])
        add_names(g3, names[6:9])
        add_names(g4, names[9:12])

        assert unlock(g1).status_code == 200
        assert advance(g1, "submission").status_code == 200

        stage1 = state(g1)
        assert stage1["stage"] == "stage1"
        assert len(stage1["candidates"]) == 12
        assert all("reason" not in candidate for candidate in stage1["candidates"])
        assert stage1["results"] == []
        assert "votes" not in stage1["candidates"][0]

        ids = {candidate["name"]: candidate["id"] for candidate in stage1["candidates"]}
        by_name = {name: ids[name] for name in names}

        over_cap = vote(g1, "stage1", [by_name[name] for name in names[:11]])
        assert over_cap.status_code == 400
        assert state(g1)["my_votes"] == []

        def stage1_votes(start: int, end: int) -> list[int]:
            return [by_name[f"Name {index:02d}"] for index in range(start, end + 1)]

        for client in (g1, g2, g3):
            assert vote(client, "stage1", stage1_votes(1, 10)).status_code == 200
        assert vote(g4, "stage1", stage1_votes(3, 12)).status_code == 200

        open_stage1 = state(g1)
        assert open_stage1["remaining_votes"] == 0
        assert len(open_stage1["my_votes"]) == 10
        assert open_stage1["voted_guests"] == 4
        assert open_stage1["online_guests"] == 4
        assert open_stage1["results"] == []
        assert all("votes" not in candidate for candidate in open_stage1["candidates"])

        closed1 = advance(g1, "stage1")
        assert closed1.status_code == 200
        assert closed1.json()["stage"] == "stage2"
        assert advance(g1, "stage1").status_code == 409
        assert state(g1)["stage"] == "stage2"

        stage2 = state(g1)
        stage1_results = next(
            result for result in stage2["results"] if result["stage"] == "stage1"
        )
        assert len(stage1_results["entries"]) == 12
        assert all("reason" not in entry for entry in stage1_results["entries"])
        advanced = {
            entry["name"]
            for entry in stage1_results["entries"]
            if entry["advanced"]
        }
        assert advanced == set(names[:10])
        assert len(stage2["candidates"]) == 10
        assert all(candidate.get("reason") for candidate in stage2["candidates"])
        assert all("votes" not in candidate for candidate in stage2["candidates"])

        stage2_ids = {
            candidate["name"]: candidate["id"] for candidate in stage2["candidates"]
        }

        four = vote(
            g1,
            "stage2",
            [stage2_ids[name] for name in names[:3]] + [stage2_ids["Name 04"]],
        )
        assert four.status_code == 400
        assert state(g1)["my_votes"] == []

        eliminated = vote(g1, "stage2", [by_name["Name 12"]])
        assert eliminated.status_code == 400

        assert (
            vote(
                g1,
                "stage2",
                [stage2_ids[name] for name in names[:3]],
            ).status_code
            == 200
        )
        assert (
            vote(
                g2,
                "stage2",
                [stage2_ids[name] for name in names[:3]],
            ).status_code
            == 200
        )
        assert (
            vote(
                g3,
                "stage2",
                [stage2_ids[name] for name in names[:3]],
            ).status_code
            == 200
        )
        assert (
            vote(
                g4,
                "stage2",
                [stage2_ids[name] for name in names[:3]],
            ).status_code
            == 200
        )

        changed = vote(
            g4,
            "stage2",
            [stage2_ids[name] for name in ("Name 01", "Name 03", "Name 04")],
        )
        assert changed.status_code == 200
        assert state(g4)["my_votes"] == [
            stage2_ids["Name 01"],
            stage2_ids["Name 03"],
            stage2_ids["Name 04"],
        ]

        too_many = vote(
            g4,
            "stage2",
            [stage2_ids[name] for name in ("Name 01", "Name 02", "Name 03", "Name 04")],
        )
        assert too_many.status_code == 400
        assert len(state(g4)["my_votes"]) == 3
        assert state(g1)["results"][-1]["stage"] == "stage1"

        closed2 = advance(g1, "stage2")
        assert closed2.status_code == 200
        assert closed2.json()["stage"] == "final"

        stage2_after = state(g1)
        stage2_results = next(
            result for result in stage2_after["results"] if result["stage"] == "stage2"
        )
        assert all(entry.get("reason") for entry in stage2_results["entries"])
        assert {
            entry["name"] for entry in stage2_results["entries"] if entry["advanced"]
        } == {"Name 01", "Name 02", "Name 03"}
        assert len(stage2_after["candidates"]) == 3
        assert all(candidate.get("reason") for candidate in stage2_after["candidates"])

        final_ids = {
            candidate["name"]: candidate["id"]
            for candidate in stage2_after["candidates"]
        }
        assert vote(g1, "final", [final_ids["Name 01"], final_ids["Name 02"]]).status_code == 400
        assert state(g1)["my_votes"] == []
        assert vote(g1, "final", [final_ids["Name 01"]]).status_code == 200
        assert vote(g2, "final", [final_ids["Name 01"]]).status_code == 200
        assert vote(g3, "final", [final_ids["Name 02"]]).status_code == 200
        assert vote(g4, "final", [final_ids["Name 03"]]).status_code == 200

        open_final = state(g1)
        assert not any(result["stage"] == "final" for result in open_final["results"])

        closed = advance(g1, "final")
        assert closed.status_code == 200
        final_state = closed.json()
        assert final_state["stage"] == "closed"
        assert final_state["winner"]["name"] == "Name 01"
        assert final_state["winner"]["reason"] == "Reason for Name 01."
        assert len(final_state["candidates"]) == 3
        assert all(candidate.get("reason") for candidate in final_state["candidates"])
        assert any(result["stage"] == "final" for result in final_state["results"])
        assert vote(g1, "final", [final_ids["Name 01"]]).status_code == 409
        assert advance(g1, "closed").status_code == 409


def test_live_participation_and_presence_counts(app, settings):
    with ExitStack() as stack:
        g1 = add_guest(stack, app, "presence-g1", "203.0.113.51")
        g2 = add_guest(stack, app, "presence-g2", "203.0.113.52")
        g3 = add_guest(stack, app, "presence-g3", "203.0.113.53")
        add_names(g1, ["Presence Alpha"])
        assert unlock(g1).status_code == 200
        assert advance(g1, "submission").status_code == 200

        open_stage = state(g1)
        assert open_stage["stage"] == "stage1"
        assert open_stage["voted_guests"] == 0
        assert open_stage["online_guests"] == 3
        assert all("votes" not in candidate for candidate in open_stage["candidates"])
        assert open_stage["results"] == []

        candidate_id = open_stage["candidates"][0]["id"]
        assert vote(g1, "stage1", [candidate_id]).status_code == 200
        assert state(g1)["voted_guests"] == 1

        assert vote(g2, "stage1", [candidate_id]).status_code == 200
        assert state(g1)["voted_guests"] == 2

        assert vote(g1, "stage1", []).status_code == 200
        assert state(g1)["voted_guests"] == 1

        # The moderator is a guest participant and was counted while voted.
        assert state(g1)["is_moderator"] is True

        # A guest outside the presence window drops out of the active count.
        expire_guest(settings, "presence-g3")
        assert state(g1)["online_guests"] == 2

        # A valid session-cookie request refreshes the requesting guest.
        expire_guest(settings, "presence-g1")
        expire_guest(settings, "presence-g2")
        assert state(g1)["online_guests"] == 1

        # A new stage starts with no saved voters.
        assert advance(g1, "stage1").status_code == 200
        assert state(g1)["stage"] == "stage2"
        assert state(g1)["voted_guests"] == 0

    with TestClient(app, headers={"X-Device-Id": "presence-visitor"}) as visitor:
        assert state(visitor) == {"joined": False}


def test_tiebreak_reruns_all_finalists_and_resets_votes(app):
    with ExitStack() as stack:
        g1 = add_guest(stack, app, "tie-g1", "203.0.113.21")
        g2 = add_guest(stack, app, "tie-g2", "203.0.113.22")
        g3 = add_guest(stack, app, "tie-g3", "203.0.113.23")
        g4 = add_guest(stack, app, "tie-g4", "203.0.113.24")

        add_names(g1, ["Alpha"])
        add_names(g2, ["Bravo"])
        add_names(g3, ["Charlie"])
        add_names(g4, ["Delta"])

        assert unlock(g1).status_code == 200
        assert advance(g1, "submission").status_code == 200
        assert advance(g1, "stage1").status_code == 200  # 4 <= 10, all advance
        assert advance(g1, "stage2").status_code == 200  # no votes -> first 3 by submission

        stage2 = state(g1)
        assert {c["name"] for c in stage2["candidates"]} == {"Alpha", "Bravo", "Charlie"}
        final_ids = {c["name"]: c["id"] for c in stage2["candidates"]}

        assert vote(g1, "final", [final_ids["Alpha"]]).status_code == 200
        assert vote(g2, "final", [final_ids["Alpha"]]).status_code == 200
        assert vote(g3, "final", [final_ids["Bravo"]]).status_code == 200
        assert vote(g4, "final", [final_ids["Bravo"]]).status_code == 200

        tied = advance(g1, "final")
        assert tied.status_code == 200
        assert tied.json()["stage"] == "tiebreak"
        assert tied.json()["winner"] is None
        assert {c["name"] for c in tied.json()["candidates"]} == {
            "Alpha",
            "Bravo",
            "Charlie",
        }

        assert vote(g1, "tiebreak", [final_ids["Alpha"]]).status_code == 200
        assert vote(g2, "tiebreak", [final_ids["Bravo"]]).status_code == 200
        assert vote(g3, "tiebreak", [final_ids["Alpha"]]).status_code == 200
        assert vote(g4, "tiebreak", [final_ids["Bravo"]]).status_code == 200

        reset = advance(g1, "tiebreak")
        assert reset.status_code == 200
        assert reset.json()["stage"] == "tiebreak"
        assert reset.json()["winner"] is None
        assert "reset" in reset.json()["notice"].lower()
        assert reset.json()["my_votes"] == []
        assert not any(result["stage"] == "tiebreak" for result in reset.json()["results"])
        assert state(g2)["my_votes"] == []

        assert vote(g1, "tiebreak", [final_ids["Alpha"]]).status_code == 200
        assert vote(g2, "tiebreak", [final_ids["Alpha"]]).status_code == 200
        assert vote(g3, "tiebreak", [final_ids["Bravo"]]).status_code == 200
        assert vote(g4, "tiebreak", [final_ids["Charlie"]]).status_code == 200

        closed = advance(g1, "tiebreak")
        assert closed.status_code == 200
        assert closed.json()["stage"] == "closed"
        assert closed.json()["winner"]["name"] == "Alpha"
        assert any(result["stage"] == "tiebreak" for result in closed.json()["results"])


def test_stage1_cutoff_tie_uses_earliest_submission(app):
    with ExitStack() as stack:
        g1 = add_guest(stack, app, "cutoff-g1", "203.0.113.31")
        g2 = add_guest(stack, app, "cutoff-g2", "203.0.113.32")
        g3 = add_guest(stack, app, "cutoff-g3", "203.0.113.33")

        names = [f"Cut {index:02d}" for index in range(1, 12)]
        add_names(g1, names[0:5])
        add_names(g2, names[5:10])
        add_names(g3, names[10:11])

        assert unlock(g1).status_code == 200
        assert advance(g1, "submission").status_code == 200
        assert advance(g1, "stage1").status_code == 200

        results = state(g1)["results"]
        assert len(results) == 1
        entries = results[0]["entries"]
        assert len(entries) == 11
        advanced = [entry["name"] for entry in entries if entry["advanced"]]
        assert len(advanced) == 10
        assert advanced == names[:10]
        assert names[10] not in advanced


def test_small_pool_advances_everyone_and_restart_persists(app, settings):
    with ExitStack() as stack:
        g1 = add_guest(stack, app, "small-g1", "203.0.113.41")
        add_names(g1, ["One", "Two"])
        assert unlock(g1).status_code == 200
        assert advance(g1, "submission").status_code == 200
        assert advance(g1, "stage1").status_code == 200
        assert len(state(g1)["candidates"]) == 2
        assert advance(g1, "stage2").status_code == 200
        assert len(state(g1)["candidates"]) == 2

        # Leave one open final to prove an in-progress stage survives a restart.
        assert vote(g1, "final", [state(g1)["candidates"][0]["id"]]).status_code == 200

    restarted = create_app(settings)
    with ExitStack() as stack:
        g1 = stack.enter_context(
            TestClient(
                restarted,
                client=("203.0.113.41", 50000),
                headers={"X-Device-Id": "small-g1"},
            )
        )
        resumed = state(g1)
        assert resumed["joined"] is True
        assert resumed["stage"] == "final"
        assert len(resumed["candidates"]) == 2
        assert len(resumed["my_votes"]) == 1
        assert resumed["is_moderator"] is True
        assert advance(g1, "final").status_code == 200
        assert state(g1)["stage"] == "closed"
        winner = state(g1)["winner"]["name"]

    restarted_again = create_app(settings)
    with ExitStack() as stack:
        g1 = stack.enter_context(
            TestClient(
                restarted_again,
                client=("203.0.113.41", 50000),
                headers={"X-Device-Id": "small-g1"},
            )
        )
        final_state = state(g1)
        assert final_state["stage"] == "closed"
        assert final_state["winner"]["name"] == winner


def test_status_mapping_and_unjoined_actions(app):
    with ExitStack() as stack:
        outsider = stack.enter_context(
            TestClient(
                app,
                client=("203.0.113.51", 50000),
                headers={"X-Device-Id": "mapping-device"},
            )
        )
        assert outsider.post("/api/join", json={}).status_code == 400
        assert suggest(outsider, "No").status_code == 401
        assert vote(outsider, "stage1", []).status_code == 401
        assert advance(outsider, "submission").status_code == 401
        assert outsider.post("/api/moderator", json={"code": MODERATOR_CODE}).status_code == 401

        g1 = add_guest(stack, app, "mapping-g1", "203.0.113.52")
        assert suggest(g1, "Mapped", "Reason.").status_code == 200
        assert suggest(g1, "Mapped", "Duplicate.").status_code == 409
        assert advance(g1, "submission").status_code == 403
        assert vote(g1, "stage1", []).status_code == 409
        assert vote(g1, "nonsense", []).status_code == 400


def test_moderator_code_rejection_and_persistence(app):
    with ExitStack() as stack:
        guest = add_guest(stack, app, "mod-code-guest", "198.51.100.80")

        wrong = guest.post("/api/moderator", json={"code": "wrong-moderator"})
        assert wrong.status_code == 401
        assert "Invalid moderator code" in wrong.json()["detail"]
        assert state(guest)["is_moderator"] is False

        assert unlock(guest).status_code == 200
        assert state(guest)["is_moderator"] is True

        restored = stack.enter_context(
            TestClient(
                app,
                client=("198.51.100.80", 50000),
                headers={"X-Device-Id": "mod-code-guest"},
            )
        )
        assert state(restored)["is_moderator"] is True
        assert MODERATOR_CODE not in restored.get("/api/state").text


def test_out_of_order_and_repeated_stage_changes_are_refused(app):
    with ExitStack() as stack:
        moderator = add_guest(stack, app, "order-moderator", "198.51.100.81")
        add_names(moderator, ["Order one"])
        assert unlock(moderator).status_code == 200

        assert advance(moderator, "stage2").status_code == 409
        assert state(moderator)["stage"] == "submission"

        assert advance(moderator, "submission").status_code == 200
        assert advance(moderator, "final").status_code == 409
        assert state(moderator)["stage"] == "stage1"

        assert advance(moderator, "stage1").status_code == 200
        assert advance(moderator, "stage1").status_code == 409
        assert state(moderator)["stage"] == "stage2"

        assert advance(moderator, "stage2").status_code == 200
        assert advance(moderator, "stage2").status_code == 409
        assert state(moderator)["stage"] == "final"


def test_stage2_third_place_tie_uses_earliest_submission(app):
    with ExitStack() as stack:
        g1 = add_guest(stack, app, "third-tie-g1", "203.0.113.61")
        g2 = add_guest(stack, app, "third-tie-g2", "203.0.113.62")
        g3 = add_guest(stack, app, "third-tie-g3", "203.0.113.63")
        g4 = add_guest(stack, app, "third-tie-g4", "203.0.113.64")

        add_names(g1, ["Alpha"])
        add_names(g2, ["Bravo"])
        add_names(g3, ["Charlie"])
        add_names(g4, ["Delta"])

        assert unlock(g1).status_code == 200
        assert advance(g1, "submission").status_code == 200
        assert advance(g1, "stage1").status_code == 200

        stage2 = state(g1)
        ids = {candidate["name"]: candidate["id"] for candidate in stage2["candidates"]}
        assert set(ids) == {"Alpha", "Bravo", "Charlie", "Delta"}

        assert vote(g1, "stage2", [ids["Alpha"], ids["Bravo"], ids["Charlie"]]).status_code == 200
        assert vote(g2, "stage2", [ids["Alpha"], ids["Bravo"], ids["Delta"]]).status_code == 200
        assert vote(g3, "stage2", [ids["Alpha"], ids["Charlie"]]).status_code == 200
        assert vote(g4, "stage2", [ids["Bravo"], ids["Delta"]]).status_code == 200

        assert advance(g1, "stage2").status_code == 200
        final = state(g1)
        assert final["stage"] == "final"
        assert {candidate["name"] for candidate in final["candidates"]} == {
            "Alpha",
            "Bravo",
            "Charlie",
        }
        stage2_results = next(
            result for result in final["results"] if result["stage"] == "stage2"
        )
        assert {
            entry["name"] for entry in stage2_results["entries"] if entry["advanced"]
        } == {"Alpha", "Bravo", "Charlie"}
        assert next(
            entry for entry in stage2_results["entries"] if entry["name"] == "Delta"
        )["advanced"] is False
