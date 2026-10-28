"""Pure helpers for the read API (GET /users, GET /activity).

No database and no web framework imports here, so this module is unit-testable
with the standard library alone (see server-python/tests/).
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta
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
                   latest_window: list[dict] | None = None,
                   person_of=None) -> tuple[list[dict], dict]:
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
    if person_of is not None:
        for _, item in timed:
            item["person"] = person_of(item["computer"], item["user"])["name"]
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


# ---- persons: link accounts of different PCs into one person ------------------

_PL = str.maketrans({"\u0142": "l", "\u0141": "L"})   # l-stroke does not decompose in NFKD


def slugify(name: str) -> str:
    """'\u0141ukasz' -> 'lukasz' (stable id for a person name)."""
    ascii_ = "".join(c for c in unicodedata.normalize("NFKD", (name or "").translate(_PL))
                     if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-") or "unknown"


def user_part(account: str | None) -> str:
    """'LENOVO\\anna' -> 'anna' (the part before the backslash is the computer)."""
    return (account or "").rsplit("\\", 1)[-1]


def resolve_person(computer: str | None, account: str | None, rows: list[dict]) -> dict:
    """Person of an account: {'id', 'name', 'excluded'}.

    rows come from cfg.Person: {'person', 'computer' (None = any PC), 'username',
    'excluded'}. A row matches on the user name (the part after the backslash,
    case-insensitive), so one row covers that login on every PC; a row with a
    computer wins over one without. An account with no row is its own person, named
    after its user name, so nothing needs configuring to get started.
    A person is excluded when any of its rows is.
    """
    uname = user_part(account).casefold()
    comp = (computer or "").casefold()
    best = None
    for r in rows:
        if r["username"].casefold() != uname:
            continue
        if r["computer"] and r["computer"].casefold() != comp:
            continue
        specific = 1 if r["computer"] else 0
        if best is None or specific > best[0]:
            best = (specific, r)
    if best is None:
        name = user_part(account) or (account or "")
        return {"id": slugify(name), "name": name, "excluded": False}
    name = best[1]["person"]
    excluded = any(r["excluded"] for r in rows
                   if slugify(r["person"]) == slugify(name))
    return {"id": slugify(name), "name": name, "excluded": excluded}


def build_persons(accounts: list[tuple], rows: list[dict], is_recording_excluded,
                  tz: ZoneInfo, include_excluded: bool = False) -> list[dict]:
    """GET /persons: accounts = [(computer, account, last_seen naive local or None)]."""
    people: dict[str, dict] = {}
    for comp, acc, last in accounts:
        who = resolve_person(comp, acc, rows)
        p = people.setdefault(who["id"], {"id": who["id"], "name": who["name"],
                                          "excluded": who["excluded"], "last_seen": None,
                                          "accounts": []})
        p["accounts"].append({
            "computer": comp, "user": acc,
            "last_seen": local_iso(last, tz) if last else None,
            "recording_excluded": bool(is_recording_excluded(acc, comp)),
        })
        if last and (p["last_seen"] is None or local_iso(last, tz) > p["last_seen"]):
            p["last_seen"] = local_iso(last, tz)
    out = [p for p in people.values() if include_excluded or not p["excluded"]]
    return sorted(out, key=lambda p: p["name"].casefold())


# ---- usage: time in front of the computer -------------------------------------

def format_duration(seconds: int) -> str:
    """Readable and language-neutral: '1 h 15 min', '2 h', '45 min', '< 1 min'."""
    s = max(0, int(seconds))
    if s == 0:
        return "0 min"
    if s < 60:
        return "< 1 min"
    h, m = divmod(s // 60, 60)
    if h and m:
        return f"{h} h {m} min"
    return f"{h} h" if h else f"{m} min"


def duration(seconds: int) -> dict:
    return {"seconds": int(seconds), "text": format_duration(seconds)}


MAX_RANGE_DAYS = 366


def resolve_range(day: str | None, start: str | None, end: str | None,
                  today: date) -> tuple[date, date]:
    """Inclusive date range of GET /usage. `date` = one day; `from`/`to` = a range
    (a missing end defaults to today, a missing start to the end). Nothing = today."""
    try:
        if day and (start or end):
            raise ValueError("use either date or from/to, not both")
        if day:
            d = date.fromisoformat(day)
            return d, d
        b = date.fromisoformat(start) if start else None
        e = date.fromisoformat(end) if end else None
    except ValueError as exc:
        raise ValueError(str(exc) if "either" in str(exc)
                         else "dates must look like 2026-10-28") from None
    if b is None and e is None:
        return today, today
    e = e or today
    b = b or e
    if b > e:
        raise ValueError("from must not be after to")
    if (e - b).days + 1 > MAX_RANGE_DAYS:
        raise ValueError(f"range is limited to {MAX_RANGE_DAYS} days")
    return b, e


def build_usage(rows: list[dict], accounts: list[tuple], person_rows: list[dict],
                start: date, end: date, programs_limit: int = 10,
                include_excluded: bool = False, person_ids: set | None = None,
                computer: str | None = None,
                host_programs: frozenset = frozenset()) -> list[dict]:
    """GET /usage: time in front per person, split by computer, program and day.

    rows     [{'computer','user','program','window','day': date,'seconds'}] already
             limited to the range (a window row counts on the day of its last activity)
    host_programs  lower-case process names that only host other apps (Store apps run in
             ApplicationFrameHost): listed by window title, with 'host' set
    accounts [(computer, account, ...)] every known account, so a person with no time
             in the range still appears with zeros
    programs_limit  top N programs, the rest summed in 'programs_other' (0 = all)
    """
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    people: dict[str, dict] = {}

    def person(comp, acc):
        who = resolve_person(comp, acc, person_rows)
        return people.setdefault(who["id"], {
            "id": who["id"], "name": who["name"], "excluded": who["excluded"],
            "computers": {}, "programs": {}, "per_day": {d: 0 for d in days}})

    for a in accounts:
        if not computer or a[0] == computer:
            person(a[0], a[1])
    for r in rows:
        if computer and r["computer"] != computer:
            continue
        if r["day"] < start or r["day"] > end:
            continue                      # defensive: never count rows outside the range
        p = person(r["computer"], r["user"])
        sec = int(r["seconds"] or 0)
        p["computers"][r["computer"]] = p["computers"].get(r["computer"], 0) + sec
        prog = r["program"] or "(unknown)"
        host = None
        if prog.casefold() in host_programs and (r.get("window") or "").strip():
            host, prog = prog, r["window"].strip()
        key = ((host or "").casefold(), prog.casefold())
        # spelling shown = the one of the biggest contributing row (stable, not row order)
        name, h0, total, best = p["programs"].get(key, (prog, host, 0, -1))
        if sec > best:
            name, h0, best = prog, host, sec
        p["programs"][key] = (name, h0, total + sec, best)
        p["per_day"][r["day"]] += sec

    out = []
    for p in people.values():
        if (p["excluded"] and not include_excluded) or (person_ids and p["id"] not in person_ids):
            continue
        progs = sorted(p["programs"].values(), key=lambda x: (-x[2], x[0].casefold()))
        shown = progs if programs_limit == 0 else progs[:programs_limit]
        rest = [] if programs_limit == 0 else progs[programs_limit:]
        per_day = [{"date": d.isoformat(), **duration(p["per_day"][d])} for d in days]
        total = sum(p["per_day"].values())
        active = sum(1 for d in days if p["per_day"][d] > 0)
        out.append({
            "id": p["id"], "name": p["name"], "excluded": p["excluded"],
            "total": duration(total),
            "computers": [{"computer": c, **duration(s)} for c, s in
                          sorted(p["computers"].items(), key=lambda x: (-x[1], x[0].casefold()))],
            "programs": [{"program": n, **({"host": h} if h else {}), **duration(s)}
                         for n, h, s, _b in shown],
            "programs_other": ({"count": len(rest), **duration(sum(x[2] for x in rest))}
                               if rest else None),
            "per_day": per_day,
            "active_days": active,
            "average": {"per_active_day": duration(total // active if active else 0),
                        "per_day": duration(total // len(days))},
        })
    return sorted(out, key=lambda x: x["name"].casefold())
