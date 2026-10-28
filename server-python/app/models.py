"""Pydantic request/response models mirroring docs/api.md."""
from __future__ import annotations

from pydantic import BaseModel, Field


# ---------- /ingest request ----------

class Visit(BaseModel):
    visit_id: int
    url: str
    title: str | None = ""
    ts: int  # epoch seconds, UTC


class WindowSample(BaseModel):
    ts: int  # epoch seconds, UTC
    session_min: int = 0
    program: str | None = None
    window: str | None = None


class IngestRequest(BaseModel):
    computer: str
    user: str
    profile: str
    visits: list[Visit] = Field(default_factory=list)
    windows: list[WindowSample] = Field(default_factory=list)


# ---------- responses ----------

class ConfigResponse(BaseModel):
    enabled: bool
    settings: dict[str, int]
    last_visit_id: dict[str, int]


class IngestResponse(BaseModel):
    ok: bool
    last_visit_id: int
    accepted_visits: int
    skipped_visits: int
    accepted_windows: int


# ---------- read API responses (GET /users, GET /activity) ----------

class UserEntry(BaseModel):
    computer: str
    user: str
    person: str
    last_seen: str | None
    excluded: bool


class UsersResponse(BaseModel):
    users: list[UserEntry]


class ActivitySummary(BaseModel):
    last_web_at: str | None
    last_window_at: str | None


class ActivityResponse(BaseModel):
    generated_at: str
    kind: str
    limit: int
    items: list[dict]
    summary: ActivitySummary


class PersonsResponse(BaseModel):
    persons: list[dict]


class UsageResponse(BaseModel):
    generated_at: str
    period: dict
    programs_limit: int
    persons: list[dict]
