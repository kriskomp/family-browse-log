"""family-browse-log ingest service (FastAPI).

Endpoints (see ../docs/api.md):
    GET  /config   - per-agent settings + resume state
    POST /ingest   - receive a block of visits + window activity
    GET  /health   - liveness/DB check (no auth)
"""
from __future__ import annotations

import secrets

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import db
from .config import get_settings
from .ingest import process_ingest
from .models import ConfigResponse, IngestRequest, IngestResponse

app = FastAPI(title="family-browse-log", version="2026.10.23.1")

_bearer = HTTPBearer(auto_error=False)


def require_token(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    expected = get_settings().api_token
    if not expected or cred is None or cred.scheme.lower() != "bearer" \
            or not secrets.compare_digest(cred.credentials, expected):
        raise HTTPException(status_code=401, detail="invalid or missing token")


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
    _=Depends(require_token),
) -> ConfigResponse:
    with db.connect() as conn:
        cur = conn.cursor()
        if db.is_excluded(cur, computer, user):
            return ConfigResponse(enabled=False, settings={}, last_visit_id={})
        settings = db.resolve_settings(cur, computer, user)
        last = db.get_last_visit_ids(cur, computer, user)
    return ConfigResponse(enabled=True, settings=settings, last_visit_id=last)


@app.post("/ingest", response_model=IngestResponse)
def post_ingest(req: IngestRequest, _=Depends(require_token)) -> IngestResponse:
    with db.connect() as conn:
        cur = conn.cursor()
        # excluded users: accept but write nothing, return their current resume point
        if db.is_excluded(cur, req.computer, req.user):
            last = db.get_last_visit_id_for_profile(cur, req.computer, req.user, req.profile)
            return IngestResponse(ok=True, last_visit_id=last,
                                  accepted_visits=0, skipped_visits=len(req.visits),
                                  accepted_windows=0)
        return process_ingest(conn, req)
