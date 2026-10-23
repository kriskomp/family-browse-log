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

        # --- API auth: the token agents must present ---
        self.api_token: str = os.getenv("FBL_API_TOKEN", "")

        # --- timezone used to derive the calendar Day from epoch timestamps ---
        self.local_tz: str = os.getenv("FBL_LOCAL_TZ", "Europe/Warsaw")

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.db_user:
            problems.append("FBL_DB_USER is not set")
        if not self.db_password:
            problems.append("FBL_DB_PASSWORD is not set")
        if not self.api_token:
            problems.append("FBL_API_TOKEN is not set")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
