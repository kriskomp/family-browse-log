"""family-browse-log server (FastAPI).

Endpoints (see ../docs/api.md):
    GET  /config    - agent: per-agent settings + resume state        (agent token)
    POST /ingest    - agent: receive a block of visits + windows       (agent token)
    GET  /users     - reader: accounts with data, for the HA picker    (reader token)
    GET  /activity  - reader: newest web + window activity             (reader token)
    GET  /health    - liveness/DB check (no auth)
"""
from __future__ import annotations

import secrets
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import db
from .activity import (KINDS, clamp_limit, local_iso, mark_active, merge_activity,
                       merge_gap_sec)
from .config import get_settings
from .ingest import process_ingest
from .models import (ActivityResponse, ConfigResponse, IngestRequest,
                     IngestResponse, UsersResponse)

app = FastAPI(title="family-browse-log", version="2026.10.25.4")

_bearer = HTTPBearer(auto_error=False)


def _check_token(cred: HTTPAuthorizationCredentials | None, expected: str) -> None:
    # compare bytes: compare_digest on non-ASCII str raises TypeError (HTTP 500)
    if (not expected or cred is None or cred.scheme.lower() != "bearer"
            or not secrets.compare_digest(cred.credentials.encode("utf-8"),
                                          expected.encode("utf-8"))):
        raise HTTPException(status_code=401, detail="invalid or missing token")


def require_agent(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    """Write-side token: only the PC agents hold it."""
    _check_token(cred, get_settings().agent_token)


def require_reader(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    """Read-side token: Home Assistant holds it. It cannot ingest or read config."""
    _check_token(cred, get_settings().reader_token)


@app.get("/health")
def health() -> dict:
    problems = get_settings().validate()
    db_ok = True
    db_error = None
    try:
        with db.connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as exc:  # pragma: no cover - environment dependent
        db_ok = False
        db_error = str(exc)
    return {"ok": db_ok and not problems,
            "config_problems": problems,
            "db_ok": db_ok,
            "db_error": db_error}


@app.get("/config", response_model=ConfigResponse)
def get_config(
    computer: str = Query(...),
    user: str = Query(...),
    _=Depends(require_agent),
) -> ConfigResponse:
    with db.connect() as conn:
        cur = conn.cursor()
        if db.is_excluded(cur, computer, user):
            return ConfigResponse(enabled=False, settings={}, last_visit_id={})
        settings = db.resolve_settings(cur, computer, user)
        last = db.get_last_visit_ids(cur, computer, user)
    return ConfigResponse(enabled=True, settings=settings, last_visit_id=last)


@app.post("/ingest", response_model=IngestResponse)
def post_ingest(req: IngestRequest, _=Depends(require_agent)) -> IngestResponse:
    with db.connect() as conn:
        cur = conn.cursor()
        # excluded users: accept but write nothing, return their current resume point
        if db.is_excluded(cur, req.computer, req.user):
            last = db.get_last_visit_id_for_profile(cur, req.computer, req.user, req.profile)
            return IngestResponse(ok=True, last_visit_id=last,
                                  accepted_visits=0, skipped_visits=len(req.visits),
                                  accepted_windows=0)
        return process_ingest(conn, req)


@app.get("/users", response_model=UsersResponse)
def get_users(_=Depends(require_reader)) -> UsersResponse:
    """Accounts that have data, so the HA config flow can offer them as a list."""
    tz = ZoneInfo(get_settings().local_tz)
    with db.connect() as conn:
        cur = conn.cursor()
        rows = db.list_users(cur)
        excluded = db.excluded_accounts(cur)

    def is_excluded(user: str, computer: str) -> bool:
        return any(u == user and (c is None or c == computer) for u, c in excluded)

    users = [
        {"computer": comp, "user": user,
         "last_seen": local_iso(last, tz) if last else None,
         "excluded": is_excluded(user, comp)}
        for comp, user, last in rows
    ]
    return UsersResponse(users=users)


@app.get("/activity", response_model=ActivityResponse)
def get_activity(
    kind: str = Query("both", pattern="^(web|windows|both)$"),
    user: list[str] = Query(default=[]),
    computer: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
    _=Depends(require_reader),
) -> ActivityResponse:
    """Newest activity, merged into one timeline (newest first).

    - kind: web | windows | both (default both)
    - user: repeat the parameter to pick several accounts (default: all)
    - computer: optional single PC
    - limit: items to return; default FBL_ACTIVITY_DEFAULT_LIMIT, capped at FBL_ACTIVITY_MAX_LIMIT
    """
    if kind not in KINDS:  # pattern already enforces this; kept as a guard
        raise HTTPException(status_code=422, detail="kind must be web, windows or both")
    s = get_settings()
    tz = ZoneInfo(s.local_tz)
    try:
        n = clamp_limit(limit, s.activity_default_limit, s.activity_max_limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    with db.connect() as conn:
        cur = conn.cursor()
        web = (db.fetch_web_activity(cur, user, computer, n)
               if kind in ("web", "both") else [])
        win = (db.fetch_window_activity(cur, user, computer, n)
               if kind in ("windows", "both") else [])
        # summary = newest record of each kind, even when that kind is not in items
        # "active" = window in front now (merge_gap_sec, per account settings)
        gaps: dict[tuple, int] = {}

        def gap_for(c, u):
            if (c, u) not in gaps:
                gaps[(c, u)] = merge_gap_sec(db.resolve_settings(cur, c, u)["window_sample_sec"])
            return gaps[(c, u)]

        mark_active(win, datetime.now(tz).replace(tzinfo=None), gap_for)
        latest_web = web if kind in ("web", "both") else db.fetch_web_activity(cur, user, computer, 1)
        latest_win = win if kind in ("windows", "both") else db.fetch_window_activity(cur, user, computer, 1)

    items, summary = merge_activity(web, win, tz, n, latest_web, latest_win)
    return ActivityResponse(
        generated_at=datetime.now(tz).isoformat(timespec="seconds"),
        kind=kind,
        limit=n,
        items=items,
        summary=summary,
    )
