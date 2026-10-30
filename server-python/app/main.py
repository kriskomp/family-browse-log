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
from .activity import (KINDS, build_persons, build_usage, clamp_limit, local_iso,
                       mark_active, merge_activity, merge_gap_sec, resolve_person,
                       resolve_range, slugify)
from .config import get_settings
from .ingest import process_ingest
from .models import (ActivityResponse, ConfigResponse, IngestRequest,
                     IngestResponse, PersonsResponse, UsageResponse,
                     UsersResponse)

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
        persons = db.fetch_persons(cur)

    def is_excluded(user: str, computer: str) -> bool:
        return any(u == user and (c is None or c == computer) for u, c in excluded)

    users = [
        {"computer": comp, "user": user,
         "person": resolve_person(comp, user, persons)["name"],
         "last_seen": local_iso(last, tz) if last else None,
         "excluded": is_excluded(user, comp)}
        for comp, user, last in rows
    ]
    return UsersResponse(users=users)


@app.get("/persons", response_model=PersonsResponse)
def get_persons(include_excluded: bool = Query(False),
                _=Depends(require_reader)) -> PersonsResponse:
    """People (accounts of several PCs linked in cfg.Person), for the HA config flow.

    include_excluded=false hides persons marked Excluded in cfg.Person.
    'recording_excluded' on an account is the separate cfg.ExcludedUser switch
    (the agent does not record it at all).
    """
    tz = ZoneInfo(get_settings().local_tz)
    with db.connect() as conn:
        cur = conn.cursor()
        accounts = db.list_users(cur)
        excluded = db.excluded_accounts(cur)
        persons = db.fetch_persons(cur)

    def rec_excluded(user: str, computer: str) -> bool:
        return any(u == user and (c is None or c == computer) for u, c in excluded)

    return PersonsResponse(persons=build_persons(accounts, persons, rec_excluded, tz,
                                                 include_excluded))


@app.get("/usage", response_model=UsageResponse)
def get_usage(
    date_: str | None = Query(default=None, alias="date"),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    person: list[str] = Query(default=[]),
    computer: str | None = Query(default=None),
    programs: int = Query(10, ge=0, le=100),
    include_excluded: bool = Query(False),
    _=Depends(require_reader),
) -> UsageResponse:
    """Time in front of the computer per person: total, per computer, per program, per day.

    - date: one day (YYYY-MM-DD); from/to: inclusive range; nothing = today (local time)
    - person: repeat to pick people (id or name); computer: one PC
    - programs: top N programs, the rest in programs_other (0 = all)
    - include_excluded: also persons marked Excluded in cfg.Person
    """
    tz = ZoneInfo(get_settings().local_tz)
    now = datetime.now(tz)
    try:
        start, end = resolve_range(date_, from_, to, now.date())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    with db.connect() as conn:
        cur = conn.cursor()
        accounts = db.list_users(cur)
        person_rows = db.fetch_persons(cur)
        rows = db.fetch_usage_rows(cur, start, end, computer)

    ids = {slugify(p) for p in person} or None
    result = build_usage(rows, accounts, person_rows, start, end, programs,
                         include_excluded, ids, computer,
                         get_settings().usage_host_programs,
                         get_settings().usage_ignore_titles)
    return UsageResponse(
        generated_at=now.isoformat(timespec="seconds"),
        period={"from": start.isoformat(), "to": end.isoformat(),
                "days": (end - start).days + 1},
        programs_limit=programs,
        persons=result,
    )


@app.get("/activity", response_model=ActivityResponse)
def get_activity(
    kind: str = Query("both", pattern="^(web|windows|both)$"),
    user: list[str] = Query(default=[]),
    computer: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
    person: list[str] = Query(default=[]),
    _=Depends(require_reader),
) -> ActivityResponse:
    """Newest activity, merged into one timeline (newest first).

    - kind: web | windows | both (default both)
    - user: repeat the parameter to pick several accounts (default: all)
    - person: repeat to pick people (id or name); adds all their accounts
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
        person_rows = db.fetch_persons(cur)
        users = list(user)
        if person:
            ids = {slugify(p) for p in person}
            users += [u for c, u, _l in db.list_users(cur)
                      if resolve_person(c, u, person_rows)["id"] in ids]
            if not users:
                # an empty list would mean "everybody": a person without accounts is nobody
                return ActivityResponse(
                    generated_at=datetime.now(tz).isoformat(timespec="seconds"),
                    kind=kind, limit=n, items=[],
                    summary={"last_web_at": None, "last_window_at": None})
        user = users
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

    items, summary = merge_activity(web, win, tz, n, latest_web, latest_win,
                                   person_of=lambda c, u: resolve_person(c, u, person_rows))
    return ActivityResponse(
        generated_at=datetime.now(tz).isoformat(timespec="seconds"),
        kind=kind,
        limit=n,
        items=items,
        summary=summary,
    )
