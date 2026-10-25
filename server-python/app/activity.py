"""Pure helpers for the read API (GET /users, GET /activity).

No database and no web framework imports here, so this module is unit-testable
with the standard library alone (see server-python/tests/).
"""
from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

KINDS = ("web", "windows", "both")


def clean_url(url: str | None) -> str:
    """scheme://host/path - query string and fragment removed.

    Query strings often carry login codes and session tokens, so they are never
    returned by the read API.
    """
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def clamp_limit(requested: int | None, default: int, maximum: int) -> int:
    """Number of items to return: the default when not given, never above the maximum."""
    if requested is None:
        return min(default, maximum)
    if requested < 1:
        raise ValueError("limit must be >= 1")
    return min(requested, maximum)


def local_iso(naive: datetime, tz: ZoneInfo) -> str:
    """The database stores naive LOCAL time (see ingest._local_dt).
    Attach the local zone (not convert) so the offset (+01/+02) is correct."""
    return naive.replace(tzinfo=tz).isoformat(timespec="seconds")


def merge_activity(web_rows: list[dict], window_rows: list[dict],
                   tz: ZoneInfo, limit: int) -> tuple[list[dict], dict]:
    """Merge web and window rows into one timeline, newest first.

    Rows are dicts with a naive local 'time' key. Sorting uses the naive time, so
    the order stays right across a DST change.
    """
    timed: list[tuple[datetime, dict]] = []
    for r in web_rows:
        timed.append((r["time"], {
            "type": "web",
            "time": local_iso(r["time"], tz),
            "user": r["user"],
            "computer": r["computer"],
            "url": clean_url(r["url"]),
            "title": r["title"] or "",
            "visits": int(r["visits"]),
        }))
    for r in window_rows:
        timed.append((r["time"], {
            "type": "window",
            "time": local_iso(r["time"], tz),
            "user": r["user"],
            "computer": r["computer"],
            "program": r["program"] or "",
            "window": r["window"] or "",
        }))
    timed.sort(key=lambda pair: pair[0], reverse=True)
    items = [item for _, item in timed[:limit]]

    summary = {
        "last_web_at": (local_iso(max(r["time"] for r in web_rows), tz)
                        if web_rows else None),
        "last_window_at": (local_iso(max(r["time"] for r in window_rows), tz)
                           if window_rows else None),
    }
    return items, summary
