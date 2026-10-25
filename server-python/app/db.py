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


# ---------------------------------------------------------------- read API

def _filters(user_col: str, computer_col: str, users: list[str], computer: str | None):
    """WHERE clause built only from fixed column names and %s placeholders;
    user-supplied values travel as parameters, never inside the SQL text."""
    clauses: list[str] = []
    params: list = []
    if users:
        clauses.append(f"{user_col} IN ({', '.join(['%s'] * len(users))})")
        params.extend(users)
    if computer:
        clauses.append(f"{computer_col} = %s")
        params.append(computer)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def list_users(cur) -> list[tuple]:
    """(Computer, UserAccount, LastSeen) for every account that has any data."""
    cur.execute(
        """
        SELECT x.Computer, x.UserAccount, MAX(x.Seen)
        FROM (
            SELECT Computer, UserAccount, MAX(LastVisit) AS Seen
              FROM dbo.BrowsingHistory GROUP BY Computer, UserAccount
            UNION ALL
            SELECT Computer, UserAccount, MAX(EventTime) AS Seen
              FROM dbo.WindowActivity GROUP BY Computer, UserAccount
        ) x
        GROUP BY x.Computer, x.UserAccount
        ORDER BY x.Computer, x.UserAccount
        """
    )
    return cur.fetchall()


def excluded_accounts(cur) -> list[tuple]:
    """(UserAccount, Computer or None) pairs from cfg.ExcludedUser."""
    cur.execute("SELECT UserAccount, Computer FROM cfg.ExcludedUser")
    return cur.fetchall()


def fetch_web_activity(cur, users: list[str], computer: str | None, limit: int) -> list[dict]:
    """Newest web rows (aggregated per day and URL), newest first."""
    where, params = _filters("UserAccount", "Computer", users, computer)
    cur.execute(
        f"""
        SELECT TOP ({int(limit)}) Computer, UserAccount, Url, Title, LastVisit, VisitCount
          FROM dbo.BrowsingHistory {where}
         ORDER BY LastVisit DESC
        """,
        params,
    )
    return [
        {"computer": r[0], "user": r[1], "url": r[2], "title": r[3],
         "time": r[4], "visits": r[5]}
        for r in cur.fetchall()
    ]


def fetch_window_activity(cur, users: list[str], computer: str | None, limit: int) -> list[dict]:
    """Newest window samples, newest first."""
    where, params = _filters("UserAccount", "Computer", users, computer)
    cur.execute(
        f"""
        SELECT TOP ({int(limit)}) Computer, UserAccount, EventTime, Program, WindowTitle
          FROM dbo.WindowActivity {where}
         ORDER BY EventTime DESC
        """,
        params,
    )
    return [
        {"computer": r[0], "user": r[1], "time": r[2], "program": r[3], "window": r[4]}
        for r in cur.fetchall()
    ]
