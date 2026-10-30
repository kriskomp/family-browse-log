"""Server configuration, loaded from environment (.env on netguard)."""
from __future__ import annotations

import os
from functools import lru_cache

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    # python-dotenv is optional; env vars may be provided by systemd instead.
    pass


def parse_program_list(value: str | None) -> frozenset[str]:
    """'msedge, Chrome,,firefox' -> {'msedge','chrome','firefox'} (case-insensitive)."""
    return frozenset(p.strip().lower() for p in (value or "").split(",") if p.strip())


class Settings:
    """Runtime configuration for the service itself (NOT the per-agent settings,
    which live in the cfg.Setting table)."""

    def __init__(self) -> None:
        # --- MS SQL connection (database 'monitor') ---
        self.db_server: str = os.getenv("FBL_DB_SERVER", "docker")
        self.db_port: int = int(os.getenv("FBL_DB_PORT", "1433"))
        self.db_name: str = os.getenv("FBL_DB_NAME", "monitor")
        self.db_user: str = os.getenv("FBL_DB_USER", "")
        self.db_password: str = os.getenv("FBL_DB_PASSWORD", "")

        # --- API tokens: two roles, never interchangeable ---
        # agent  : write  - POST /ingest, GET /config (PCs of the children)
        # reader : read   - GET /users, GET /activity (Home Assistant)
        # FBL_API_TOKEN is still accepted as the agent token (backward compatible).
        self.agent_token: str = os.getenv("FBL_AGENT_TOKEN") or os.getenv("FBL_API_TOKEN", "")
        self.reader_token: str = os.getenv("FBL_READER_TOKEN", "")

        # --- read API limits (items per /activity call) ---
        self.activity_default_limit: int = int(os.getenv("FBL_ACTIVITY_DEFAULT_LIMIT", "20"))
        self.activity_max_limit: int = int(os.getenv("FBL_ACTIVITY_MAX_LIMIT", "200"))

        # --- window activity: programs grouped by program only (title = latest) ---
        # Browsers change their window title with every tab; the pages themselves are in
        # BrowsingHistory, so one row per browser per session is enough.
        self.window_group_by_program: frozenset[str] = parse_program_list(
            os.getenv("FBL_WINDOW_GROUP_BY_PROGRAM", "msedge,chrome,firefox,brave,opera"))

        # --- usage: host processes shown by window title instead of by program ---
        # Microsoft Store apps all run inside ApplicationFrameHost, so "program" alone
        # would lump Solitaire, the media player, ... into one entry.
        self.usage_host_programs: frozenset[str] = parse_program_list(
            os.getenv("FBL_USAGE_HOST_PROGRAMS", "ApplicationFrameHost"))

        # --- usage: window titles left out of /usage entirely (exact match, case-insensitive,
        # separated by ";"). Technical windows, e.g. the Microsoft Family Safety app.
        self.usage_ignore_titles: frozenset[str] = frozenset(
            t.strip().casefold() for t in
            os.getenv("FBL_USAGE_IGNORE_TITLES", "Microsoft Family Safety").split(";")
            if t.strip())

        # --- timezone used to derive the calendar Day from epoch timestamps ---
        self.local_tz: str = os.getenv("FBL_LOCAL_TZ", "Europe/Warsaw")

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.db_user:
            problems.append("FBL_DB_USER is not set")
        if not self.db_password:
            problems.append("FBL_DB_PASSWORD is not set")
        if not self.agent_token:
            problems.append("FBL_AGENT_TOKEN (or FBL_API_TOKEN) is not set")
        if not self.reader_token:
            problems.append("FBL_READER_TOKEN is not set (read API disabled)")
        elif self.reader_token == self.agent_token:
            problems.append("FBL_READER_TOKEN must differ from the agent token")
        if self.activity_default_limit < 1 or self.activity_max_limit < self.activity_default_limit:
            problems.append("FBL_ACTIVITY_* limits are inconsistent")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
