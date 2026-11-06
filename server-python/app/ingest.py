"""Write path for POST /ingest: aggregate + upsert in one transaction."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import get_settings
from .activity import group_key, group_samples, session_gap_sec
from .db import get_last_visit_id_for_profile, get_visit_cutoff, resolve_settings
from .models import IngestRequest, IngestResponse
from .visits import ZERO_HASH, is_before_cutoff, order_visits, url_hash


def _local_dt(ts: int, tz: ZoneInfo) -> datetime:
    """Epoch seconds (UTC) -> naive local datetime for SQL."""
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .astimezone(tz)
        .replace(tzinfo=None)
    )


def aggregate_visits(visits, tz: ZoneInfo) -> dict[tuple[str, str], dict]:
    """Visits -> one entry per (Day, Url): count, earliest/latest time, latest title."""
    agg: dict[tuple[str, str], dict] = {}
    for v in visits:
        dt = _local_dt(v.ts, tz)
        day = dt.date()
        url = (v.url or "")[:2048]
        title = (v.title or "")[:1024]
        key = (day.isoformat(), url)
        a = agg.get(key)
        if a is None:
            agg[key] = {"day": day, "url": url, "title": title,
                        "count": 1, "first": dt, "last": dt}
        else:
            a["count"] += 1
            if dt < a["first"]:
                a["first"] = dt
            if dt > a["last"]:
                a["last"] = dt
                a["title"] = title
    return agg


def process_ingest(conn, req: IngestRequest) -> IngestResponse:
    settings = get_settings()
    tz = ZoneInfo(settings.local_tz)
    cur = conn.cursor()

    # Edge's visit id is only kept as a legacy resume point for older agents (GET /config
    # still serves it) and for diagnostics: it is NOT used to decide what is new.
    last_id = get_last_visit_id_for_profile(cur, req.computer, req.user, req.profile)

    # A visit is new when dbo.BrowsingVisit has no row for (account, profile, time, URL hash)
    # yet. The check is made visit by visit inside the transaction below.
    insert_visit = """
    INSERT INTO dbo.BrowsingVisit (Computer, UserAccount, EdgeProfile, VisitTs, UrlHash)
    OUTPUT inserted.VisitTs
    SELECT %(c)s, %(u)s, %(p)s, %(ts)s, CONVERT(BINARY(32), %(h)s, 2)
     WHERE NOT EXISTS (SELECT 1 FROM dbo.BrowsingVisit
                        WHERE Computer=%(c)s AND UserAccount=%(u)s AND EdgeProfile=%(p)s
                          AND VisitTs=%(ts)s AND UrlHash=CONVERT(BINARY(32), %(h)s, 2));
    """

    # dbo.BrowsingVisit only has to cover the de-duplication window
    purge_visits = """
    DELETE TOP (500) FROM dbo.BrowsingVisit
     WHERE Computer=%(c)s AND UserAccount=%(u)s AND EdgeProfile=%(p)s AND VisitTs < %(before)s
       AND UrlHash <> CONVERT(BINARY(32), %(zero)s, 2);
    """

    ack_command = """
    UPDATE cfg.AgentCommand SET AckedAt = SYSDATETIME()
     WHERE Id=%(id)s AND Computer=%(c)s AND UserAccount=%(u)s AND AckedAt IS NULL;
    """

    upsert_status = """
    UPDATE dbo.AgentStatus
       SET ReportedAt=SYSDATETIME(), AgentVersion=%(ver)s, HistoryMtime=%(hm)s,
           MaxVisitId=%(mid)s, MaxVisitTime=%(vt)s, RowsRead=%(rows)s, LastError=%(err)s
     WHERE Computer=%(c)s AND UserAccount=%(u)s AND EdgeProfile=%(p)s;
    IF @@ROWCOUNT = 0
    INSERT INTO dbo.AgentStatus
        (Computer, UserAccount, EdgeProfile, ReportedAt, AgentVersion, HistoryMtime,
         MaxVisitId, MaxVisitTime, RowsRead, LastError)
    VALUES (%(c)s, %(u)s, %(p)s, SYSDATETIME(), %(ver)s, %(hm)s,
            %(mid)s, %(vt)s, %(rows)s, %(err)s);
    """

    upsert_hist = """
    UPDATE dbo.BrowsingHistory
       SET VisitCount = VisitCount + %(cnt)s,
           LastVisit  = CASE WHEN %(last)s  > LastVisit  THEN %(last)s  ELSE LastVisit  END,
           FirstVisit = CASE WHEN %(first)s < FirstVisit THEN %(first)s ELSE FirstVisit END,
           Title      = %(title)s
     WHERE Computer=%(c)s AND UserAccount=%(u)s AND EdgeProfile=%(p)s
           AND [Day]=%(day)s AND Url=%(url)s;
    IF @@ROWCOUNT = 0
    INSERT INTO dbo.BrowsingHistory
        (Computer, UserAccount, EdgeProfile, [Day], Url, Title, VisitCount, FirstVisit, LastVisit)
    VALUES (%(c)s, %(u)s, %(p)s, %(day)s, %(url)s, %(title)s, %(cnt)s, %(first)s, %(last)s);
    """

    # Window samples are grouped: ONE row per window (program + title) per session of
    # the account. Returning to a window updates its row; nothing is duplicated.
    select_last_win = """
    SELECT TOP 1 ISNULL(EndTime, EventTime), SessionStart
      FROM dbo.WindowActivity
     WHERE Computer=%(c)s AND UserAccount=%(u)s
     ORDER BY ISNULL(EndTime, EventTime) DESC, Id DESC;
    """

    select_session_rows = """
    SELECT Id, EventTime, ISNULL(EndTime, EventTime), ActiveSeconds, Program, WindowTitle
      FROM dbo.WindowActivity
     WHERE Computer=%(c)s AND UserAccount=%(u)s AND SessionStart=%(s)s
     ORDER BY ISNULL(EndTime, EventTime);
    """

    insert_win = """
    INSERT INTO dbo.WindowActivity
        (Computer, UserAccount, SessionStart, EventTime, EndTime,
         ActiveSeconds, SessionMinutes, Program, WindowTitle)
    VALUES (%(c)s, %(u)s, %(s)s, %(start)s, %(end)s,
            %(secs)s, %(secs)s / 60, %(prog)s, %(win)s);
    """

    update_win = """
    UPDATE dbo.WindowActivity
       SET EndTime=%(end)s, ActiveSeconds=%(secs)s, SessionMinutes=%(secs)s / 60,
           WindowTitle=%(win)s
     WHERE Id=%(id)s;
    """

    upsert_state = """
    UPDATE dbo.IngestState SET LastVisitId=%(v)s
     WHERE Computer=%(c)s AND UserAccount=%(u)s AND EdgeProfile=%(p)s;
    IF @@ROWCOUNT = 0
    INSERT INTO dbo.IngestState (Computer, UserAccount, EdgeProfile, LastVisitId)
    VALUES (%(c)s, %(u)s, %(p)s, %(v)s);
    """

    upsert_device = """
    UPDATE cfg.Device SET LastSeen=SYSDATETIME() WHERE Computer=%(c)s;
    IF @@ROWCOUNT = 0
    INSERT INTO cfg.Device (Computer, LastSeen) VALUES (%(c)s, SYSDATETIME());
    """

    new_visits: list = []
    skipped = 0
    max_id = last_id
    try:
        # 1) idempotency: keep only visits not counted yet, oldest first
        cutoff, create_marker = (get_visit_cutoff(cur, req.computer, req.user, req.profile, tz)
                                 if req.visits else (None, False))
        if create_marker:               # remember the cutoff for good (see get_visit_cutoff)
            cur.execute(insert_visit, {"c": req.computer, "u": req.user, "p": req.profile,
                                       "ts": cutoff, "h": ZERO_HASH})
            cur.fetchone()
        for v in order_visits(req.visits):
            if is_before_cutoff(v.ts, cutoff):
                skipped += 1            # counted by an older server version
                continue
            cur.execute(insert_visit, {
                "c": req.computer, "u": req.user, "p": req.profile, "ts": v.ts,
                "h": url_hash((v.url or "")[:2048]),
            })
            if cur.fetchone() is None:
                skipped += 1            # already stored: a re-sent block or the read overlap
                continue
            new_visits.append(v)
            if v.visit_id > max_id:
                max_id = v.visit_id

        # keep dbo.BrowsingVisit small: only the de-duplication window is needed
        if req.visits and settings.visit_dedup_days > 0:
            cur.execute(purge_visits, {
                "c": req.computer, "u": req.user, "p": req.profile,
                "before": int(time.time()) - settings.visit_dedup_days * 86400,
                "zero": ZERO_HASH,          # the cutoff marker is never purged
            })

        # 2) aggregate what is new by (Day, Url) and upsert
        for a in aggregate_visits(new_visits, tz).values():
            cur.execute(upsert_hist, {
                "c": req.computer, "u": req.user, "p": req.profile,
                "day": a["day"], "url": a["url"], "title": a["title"],
                "cnt": a["count"], "first": a["first"], "last": a["last"],
            })

        resent = 0
        if req.windows:
            by_program = get_settings().window_group_by_program
            st = resolve_settings(cur, req.computer, req.user)
            sample_sec = st["window_sample_sec"]
            key = {"c": req.computer, "u": req.user}
            cur.execute(select_last_win, key)
            row = cur.fetchone()
            last_end, session = (row[0], row[1]) if row else (None, None)
            rows: dict = {}
            if session is not None:
                cur.execute(select_session_rows, dict(key, s=session))
                # oldest first: if old data has several rows for one key, the newest wins
                for r in cur.fetchall():
                    rows[group_key(r[4], r[5], by_program)] = {
                        "id": r[0], "start": r[1], "end": r[2], "secs": int(r[3] or 0),
                        "session": session, "program": r[4], "window": r[5]}

            samples = sorted(
                ((_local_dt(w.ts, tz), w.program, (w.window or "")[:2048]) for w in req.windows),
                key=lambda x: x[0])
            touched, resent = group_samples(last_end, session, rows, samples,
                                             session_gap_sec(sample_sec, st["idle_limit_sec"]),
                                             sample_sec, by_program)
            for g in touched:
                if g["id"] is None:
                    cur.execute(insert_win, dict(key, s=g["session"], start=g["start"],
                                                 end=g["end"], secs=g["secs"],
                                                 prog=g["program"], win=g["window"]))
                else:
                    cur.execute(update_win, {"id": g["id"], "end": g["end"], "secs": g["secs"],
                                             "win": g["window"]})

        if max_id > last_id:
            cur.execute(upsert_state, {
                "c": req.computer, "u": req.user, "p": req.profile, "v": max_id,
            })

        # agent diagnostics (what it sees in each Edge profile) and executed commands
        for d in req.diag:
            cur.execute(upsert_status, {
                "c": req.computer, "u": req.user, "p": (d.profile or "")[:128],
                "ver": (req.agent_version or "")[:32] or None,
                "hm": _local_dt(d.history_mtime, tz) if d.history_mtime else None,
                "mid": d.max_visit_id,
                "vt": _local_dt(d.max_visit_ts, tz) if d.max_visit_ts else None,
                "rows": d.rows_read, "err": (d.error or "")[:256] or None,
            })
        for cmd_id in req.acked[:50]:
            cur.execute(ack_command, {"id": cmd_id, "c": req.computer, "u": req.user})

        cur.execute(upsert_device, {"c": req.computer})

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    windows_accepted = len(req.windows) - resent
    return IngestResponse(
        ok=True,
        last_visit_id=max_id,
        accepted_visits=len(new_visits),
        skipped_visits=skipped,
        accepted_windows=windows_accepted,
    )
