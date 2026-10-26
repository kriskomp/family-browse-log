"""Pure helpers for the read API (GET /users, GET /activity).

No database and no web framework imports here, so this module is unit-testable
with the standard library alone (see server-python/tests/).
"""
from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

KINDS = ("web", "windows", "both")


# ---- window-activity grouping (write path, used by ingest.py) ---------------

def minutes_between(start: datetime, end: datetime) -> int:
    """Whole minutes elapsed from start to end (never negative)."""
    return max(0, int((end - start).total_seconds() // 60))


def merge_gap_sec(sample_sec: int) -> int:
    """Longest pause between two samples that still belongs to the same group.

    A few missed samples (agent cycle jitter, one slow push) must not split a
    group, but a real break (idle, logoff, PC asleep) must. At least 120 s.
    """
    return max(120, 3 * int(sample_sec))


def mark_active(window_rows: list[dict], now: datetime, gap_for) -> None:
    """Set row['active'] on every window row (in place).

    A window is active (still open) when it is the newest row of its account and its
    last sample is not older than the same gap the write path uses to extend a group
    (merge_gap_sec). Older rows of an account are never active, even when they ended
    a moment ago. gap_for(computer, user) -> seconds.
    """
    seen: set[tuple] = set()
    for r in sorted(window_rows, key=lambda x: x["time"], reverse=True):
        key = (r["computer"], r["user"])
        newest = key not in seen
        seen.add(key)
        r["active"] = bool(newest and (now - r["time"]).total_seconds()
                           <= gap_for(r["computer"], r["user"]))


def can_extend(group: dict | None, t: datetime, program: str | None,
               title: str | None, gap_sec: int) -> bool:
    """True when the sample at t continues `group` (same program and title, no
    longer pause than gap_sec, not before the group started).

    group: {'start', 'end', 'program', 'window'} (naive local datetimes).
    A sample inside [start, end] also counts: it is a re-send of an old sample
    and changes nothing.
    """
    if group is None:
        return False
    if (group["program"] or None) != (program or None):
        return False
    if (group["window"] or None) != (title or None):
        return False
    if t < group["start"]:
        return False
    return (t - group["end"]).total_seconds() <= gap_sec


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
                   tz: ZoneInfo, limit: int,
                   latest_web: list[dict] | None = None,
                   latest_window: list[dict] | None = None) -> tuple[list[dict], dict]:
    """Merge web and window rows into one timeline, newest first.

    Rows are dicts with a naive local 'time' key (for windows: the END of the group;
    'start' and 'minutes' describe the group). Sorting uses the naive time, so
    the order stays right across a DST change.

    summary always reports the newest record of EACH kind. latest_web / latest_window
    carry those records when the kind was not part of the items (e.g. kind=windows),
    so a missing kind is never confused with "no activity".
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
            "from": local_iso(r["start"], tz) if r.get("start") else local_iso(r["time"], tz),
            "to": local_iso(r["time"], tz),
            "minutes": int(r.get("minutes") or 0),
            "active": bool(r.get("active", False)),
            "user": r["user"],
            "computer": r["computer"],
            "program": r["program"] or "",
            "window": r["window"] or "",
        }))
    timed.sort(key=lambda pair: pair[0], reverse=True)
    items = [item for _, item in timed[:limit]]

    web_for_summary = web_rows if latest_web is None else latest_web
    win_for_summary = window_rows if latest_window is None else latest_window
    summary = {
        "last_web_at": (local_iso(max(r["time"] for r in web_for_summary), tz)
                        if web_for_summary else None),
        "last_window_at": (local_iso(max(r["time"] for r in win_for_summary), tz)
                           if win_for_summary else None),
    }
    return items, summary
