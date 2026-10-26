"""Write path for POST /ingest: aggregate + upsert in one transaction."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import get_settings
from .activity import group_samples, session_gap_sec
from .db import get_last_visit_id_for_profile, resolve_settings
from .models import IngestRequest, IngestResponse


def _local_dt(ts: int, tz: ZoneInfo) -> datetime:
    """Epoch seconds (UTC) -> naive local datetime for SQL."""
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .astimezone(tz)
        .replace(tzinfo=None)
    )


def process_ingest(conn, req: IngestRequest) -> IngestResponse:
    tz = ZoneInfo(get_settings().local_tz)
    cur = conn.cursor()

    last_id = get_last_visit_id_for_profile(cur, req.computer, req.user, req.profile)

    # 1) idempotency: drop anything already applied
    new_visits = [v for v in req.visits if v.visit_id > last_id]
    skipped = len(req.visits) - len(new_visits)

    # 2) aggregate by (Day, Url)
    agg: dict[tuple[str, str], dict] = {}
    max_id = last_id
    for v in new_visits:
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
        if v.visit_id > max_id:
            max_id = v.visit_id

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
     WHERE Computer=%(c)s AND UserAccount=%(u)s AND SessionStart=%(s)s;
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
       SET EndTime=%(end)s, ActiveSeconds=%(secs)s, SessionMinutes=%(secs)s / 60
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

    try:
        for a in agg.values():
            cur.execute(upsert_hist, {
                "c": req.computer, "u": req.user, "p": req.profile,
                "day": a["day"], "url": a["url"], "title": a["title"],
                "cnt": a["count"], "first": a["first"], "last": a["last"],
            })

        resent = 0
        if req.windows:
            st = resolve_settings(cur, req.computer, req.user)
            sample_sec = st["window_sample_sec"]
            key = {"c": req.computer, "u": req.user}
            cur.execute(select_last_win, key)
            row = cur.fetchone()
            last_end, session = (row[0], row[1]) if row else (None, None)
            rows: dict = {}
            if session is not None:
                cur.execute(select_session_rows, dict(key, s=session))
                for r in cur.fetchall():
                    rows[(r[4] or None, r[5] or None)] = {
                        "id": r[0], "start": r[1], "end": r[2], "secs": int(r[3] or 0),
                        "session": session, "program": r[4], "window": r[5]}

            samples = sorted(
                ((_local_dt(w.ts, tz), w.program, (w.window or "")[:2048]) for w in req.windows),
                key=lambda x: x[0])
            touched, resent = group_samples(last_end, session, rows, samples,
                                             session_gap_sec(sample_sec, st["idle_limit_sec"]),
                                             sample_sec)
            for g in touched:
                if g["id"] is None:
                    cur.execute(insert_win, dict(key, s=g["session"], start=g["start"],
                                                 end=g["end"], secs=g["secs"],
                                                 prog=g["program"], win=g["window"]))
                else:
                    cur.execute(update_win, {"id": g["id"], "end": g["end"], "secs": g["secs"]})

        if max_id > last_id:
            cur.execute(upsert_state, {
                "c": req.computer, "u": req.user, "p": req.profile, "v": max_id,
            })

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
