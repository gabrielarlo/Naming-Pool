from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import Settings, load_settings
from app.db import connect, init_db
from app.security import (
    FailureLimiter,
    client_ip,
    request_device_id,
    resolve_guest,
    set_session_cookie,
)
from app.service import (
    ApiError,
    advance,
    get_state,
    join_room,
    set_votes,
    submit_suggestion,
    unlock_moderator,
)

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"


class JoinRequest(BaseModel):
    code: str = Field(min_length=1, max_length=200)


class ModeratorRequest(BaseModel):
    code: str = Field(min_length=1, max_length=200)


class SuggestionRequest(BaseModel):
    name: str
    reason: str


class VotesRequest(BaseModel):
    stage: str
    suggestion_ids: list[int]


class AdvanceRequest(BaseModel):
    stage: str


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        init_db(settings.db_path)
        yield

    app = FastAPI(
        title="Naming Pool",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.limiter = FailureLimiter()

    def get_db(request: Request):
        conn = connect(request.app.state.settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def get_guest(
        request: Request,
        db: sqlite3.Connection = Depends(get_db),
    ) -> sqlite3.Row | None:
        return resolve_guest(request, db, request.app.state.settings)

    def state_payload(
        request: Request,
        db: sqlite3.Connection,
        guest: sqlite3.Row | None,
    ) -> dict:
        return get_state(db, guest, request.app.state.settings.min_suggestions)

    @app.middleware("http")
    async def response_policy(request: Request, call_next):
        response = await call_next(request)

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        token = getattr(request.state, "new_session_token", None)
        if token:
            set_session_cookie(response, token, request.app.state.settings)
        return response

    @app.exception_handler(ApiError)
    async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _: Request, __: RequestValidationError
    ) -> JSONResponse:
        # Do not echo request input: it may contain access codes.
        return JSONResponse(status_code=400, content={"detail": "Invalid request."})

    @app.get("/api/health")
    def health(db: sqlite3.Connection = Depends(get_db)) -> dict[str, str]:
        db.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.get("/api/state")
    def state(
        request: Request,
        db: sqlite3.Connection = Depends(get_db),
        guest: sqlite3.Row | None = Depends(get_guest),
    ) -> dict:
        return state_payload(request, db, guest)

    @app.post("/api/join")
    def join(
        payload: JoinRequest,
        request: Request,
        db: sqlite3.Connection = Depends(get_db),
        guest: sqlite3.Row | None = Depends(get_guest),
    ) -> dict:
        updated, token = join_room(
            db,
            request.app.state.settings,
            request.app.state.limiter,
            payload.code,
            request_device_id(request),
            client_ip(request),
            guest,
        )
        request.state.new_session_token = token
        return state_payload(request, db, updated)

    @app.post("/api/moderator")
    def moderator(
        payload: ModeratorRequest,
        request: Request,
        db: sqlite3.Connection = Depends(get_db),
        guest: sqlite3.Row | None = Depends(get_guest),
    ) -> dict:
        updated = unlock_moderator(
            db,
            request.app.state.settings,
            request.app.state.limiter,
            guest,
            payload.code,
            client_ip(request),
        )
        return state_payload(request, db, updated)

    @app.post("/api/suggestions")
    def suggestion(
        payload: SuggestionRequest,
        request: Request,
        db: sqlite3.Connection = Depends(get_db),
        guest: sqlite3.Row | None = Depends(get_guest),
    ) -> dict:
        submit_suggestion(db, guest, payload.name, payload.reason)
        return state_payload(request, db, guest)

    @app.put("/api/votes")
    def votes(
        payload: VotesRequest,
        request: Request,
        db: sqlite3.Connection = Depends(get_db),
        guest: sqlite3.Row | None = Depends(get_guest),
    ) -> dict:
        set_votes(db, guest, payload.stage, payload.suggestion_ids)
        return state_payload(request, db, guest)

    @app.post("/api/moderator/advance")
    def moderator_advance(
        payload: AdvanceRequest,
        request: Request,
        db: sqlite3.Connection = Depends(get_db),
        guest: sqlite3.Row | None = Depends(get_guest),
    ) -> dict:
        notice = advance(
            db,
            guest,
            payload.stage,
            request.app.state.settings.min_suggestions,
        )
        payload_state = state_payload(request, db, guest)
        payload_state["notice"] = notice
        return payload_state

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app
