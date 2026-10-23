"""MS SQL access via pymssql (database 'monitor').

pymssql is used rather than pyodbc so the service needs no system ODBC driver -
`pip install pymssql` is enough on a plain Linux LXC.
"""
from __future__ import annotations

import contextlib
from typing import Iterator

import pymssql

from .config import get_settings

# default settings, used if a key is missing from cfg.Setting
DEFAULT_SETTINGS: dict[str, int] = {
    "push_interval_sec": 30,
    "window_sample_sec": 30,
    "history_read_sec": 120,
    "block_size": 500,
    "max_backfill": 3000,
    "idle_limit_sec": 300,
}


@contextlib.contextmanager
def connect() -> Iterator["pymssql.Connection"]:
    s = get_settings()
    conn = pymssql.connect(
        server=s.db_server,
        port=str(s.db_port),
        user=s.db_user,
        password=s.db_password,
        database=s.db_name,
        timeout=15,
        login_timeout=15,
    )
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------- config reads

def is_excluded(cur, computer: str, user: str) -> bool:
    cur.execute(
        """
        SELECT TOP 1 1 FROM cfg.ExcludedUser
        WHERE UserAccount = %s AND (Computer IS NULL OR Computer = %s)
        """,
        (user, computer),
    )
    return cur.fetchone() is not None


def resolve_settings(cur, computer: str, user: str) -> dict[str, int]:
    """Resolve settings with precedence user > computer > global."""
    cur.execute(
        """
        SELECT Scope, SettingKey, SettingValue
        FROM cfg.Setting
        WHERE Scope = 'global'
           OR (Scope = 'computer' AND Computer = %s)
           OR (Scope = 'user' AND UserAccount = %s)
        """,
        (computer, user),
    )
    rank = {"global": 0, "computer": 1, "user": 2}
    chosen: dict[str, tuple[int, str]] = {}
    for scope, key, value in cur.fetchall():
        r = rank.get(scope, 0)
        if key not in chosen or r >= chosen[key][0]:
            chosen[key] = (r, value)

    result = dict(DEFAULT_SETTINGS)
    for key, (_rank, value) in chosen.items():
        try:
            result[key] = int(value)
        except (TypeError, ValueError):
            # keep the default if a stored value is non-numeric
            pass
    return result


def get_last_visit_ids(cur, computer: str, user: str) -> dict[str, int]:
    cur.execute(
        """
        SELECT EdgeProfile, LastVisitId FROM dbo.IngestState
        WHERE Computer = %s AND UserAccount = %s
        """,
        (computer, user),
    )
    return {row[0]: int(row[1]) for row in cur.fetchall()}


def get_last_visit_id_for_profile(cur, computer: str, user: str, profile: str) -> int:
    cur.execute(
        """
        SELECT LastVisitId FROM dbo.IngestState
        WHERE Computer = %s AND UserAccount = %s AND EdgeProfile = %s
        """,
        (computer, user, profile),
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0
