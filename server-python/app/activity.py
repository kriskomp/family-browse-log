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

def merge_gap_sec(sample_sec: int) -> int:
    """How long after its last sample a window still counts as in front ("active"):
    a few missed samples (agent jitter, a slow push) are tolerated. At least 120 s."""
    return max(120, 3 * int(sample_sec))


def session_gap_sec(sample_sec: int, idle_limit_sec: int) -> int:
    """Longest pause between two samples of an account inside ONE session.

    The agent stops sampling after idle_limit_sec without mouse/keyboard (reading,
    watching a film), so a shorter gap would split a session on every such pause.
    A pause longer than idle + 2 samples means the PC was off, asleep or logged off.
    """
    return max(merge_gap_sec(sample_sec), int(idle_limit_sec) + 2 * int(sample_sec))


def group_key(program: str | None, title: str | None, by_program: frozenset) -> tuple:
    """Row key inside a session. Programs in by_program (browsers) are one row whatever
    the title (each tab changes it); others are one row per program + title."""
    program = program or None
    if program and program.lower() in by_program:
        return (program.lower(), None)
    return (program, title or None)


def group_samples(last_end, session_start, session_rows: dict, samples: list,
                  gap_sec: int, sample_sec: int, by_program: frozenset = frozenset()):
    """Fold window samples of ONE account into one row per window per session.

    last_end       newest EndTime of the account in the DB (None = no data)
    session_start  SessionStart of that newest row (None = unknown/old data)
    session_rows   {group_key: row} of the current session, row =
                   {'id', 'start', 'end', 'secs', 'session'} (naive local datetimes)
    samples        [(t, program, title)] sorted oldest first

    A session ends when no sample of ANY window arrives for more than gap_sec.
    Within a session, a window that comes back to the front updates its row
    (EndTime = last time in front, secs += sample_sec), so switching windows never
    creates duplicate rows. Programs in by_program (browsers) have one row per
    session; its title is the latest one. Samples not newer than last_end are re-sends (the agent
    sends in order and only one agent records an account) and are skipped.

    Returns (touched rows in first-touch order, skipped count). Rows with id None
    are new (INSERT); others are UPDATEs. Mutates session_rows.
    """
    touched: list[dict] = []
    seen: set[int] = set()
    skipped = 0
    for t, prog, title in samples:
        prog = prog or None
        title = title or None
        if last_end is not None and t <= last_end:
            skipped += 1
            continue
        if (last_end is None or session_start is None
                or (t - last_end).total_seconds() > gap_sec):
            session_start = t
            session_rows.clear()
        key = group_key(prog, title, by_program)
        row = session_rows.get(key)
        if row is None:
            row = {"id": None, "start": t, "end": t, "secs": int(sample_sec),
                   "session": session_start, "program": prog, "window": title}
            session_rows[key] = row
        else:
            row["end"] = t
            row["secs"] += int(sample_sec)
            row["window"] = title          # latest title (changes only for browsers)
        if id(row) not in seen:
            seen.add(id(row))
            touched.append(row)
        last_end = t
    return touched, skipped


def mark_active(window_rows: list[dict], now: datetime, gap_for) -> None:
    """Set row['active'] on every window row (in place).

    A window is active (still open) when it is the newest row of its account and its
    last sample is not older than merge_gap_sec (2 min by default; a missed sample or a
    slow push is tolerated). The session gap of the write path is longer on purpose: an
    idle pause keeps the session, but the window is not "in front now". Older rows of an account are never active, even when they ended
    a moment ago. gap_for(computer, user) -> seconds.
    """
    seen: set[tuple] = set()
    for r in sorted(window_rows, key=lambda x: x["time"], reverse=True):
        key = (r["computer"], r["user"])
        newest = key not in seen
        seen.add(key)
        r["active"] = bool(newest and (now - r["time"]).total_seconds()
                           <= gap_for(r["computer"], r["user"]))


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
