"""Write path for POST /ingest: aggregate + upsert in one transaction."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import get_settings
from .activity import can_extend, merge_gap_sec, minutes_between
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

    # Window samples are grouped: consecutive samples of the same program + title
    # extend ONE row (EndTime and SessionMinutes grow) instead of adding a row each.
    select_latest_win = """
    SELECT TOP 1 Id, EventTime, ISNULL(EndTime, EventTime), Program, WindowTitle
      FROM dbo.WindowActivity
     WHERE Computer=%(c)s AND UserAccount=%(u)s
     ORDER BY Id DESC;
    """

    insert_win = """
    INSERT INTO dbo.WindowActivity
        (Computer, UserAccount, EventTime, EndTime, SessionMinutes, Program, WindowTitle)
    VALUES (%(c)s, %(u)s, %(t)s, %(t)s, 0, %(prog)s, %(win)s);
    SELECT CAST(SCOPE_IDENTITY() AS BIGINT);
    """

    extend_win = """
    UPDATE dbo.WindowActivity SET EndTime=%(t)s, SessionMinutes=%(m)s WHERE Id=%(id)s;
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

        if req.windows:
            gap = merge_gap_sec(resolve_settings(cur, req.computer, req.user)
                                .get("window_sample_sec", 30))
            cur.execute(select_latest_win, {"c": req.computer, "u": req.user})
            row = cur.fetchone()
            group = None if row is None else {
                "id": row[0], "start": row[1], "end": row[2],
                "program": row[3], "window": row[4]}

            # oldest first, so a block that arrives out of order still groups
            for w in sorted(req.windows, key=lambda x: x.ts):
                t = _local_dt(w.ts, tz)
                prog = (w.program or None)
                win = ((w.window or "")[:2048] or None)
                if can_extend(group, t, prog, win, gap):
                    if t > group["end"]:
                        group["end"] = t
                        cur.execute(extend_win, {
                            "id": group["id"], "t": t,
                            "m": minutes_between(group["start"], t)})
                    # t inside the group = a re-sent sample, nothing to change
                else:
                    cur.execute(insert_win, {
                        "c": req.computer, "u": req.user, "t": t,
                        "prog": prog, "win": win})
                    new_id = cur.fetchone()[0]
                    group = {"id": new_id, "start": t, "end": t,
                             "program": prog, "window": win}

        if max_id > last_id:
            cur.execute(upsert_state, {
                "c": req.computer, "u": req.user, "p": req.profile, "v": max_id,
            })

        cur.execute(upsert_device, {"c": req.computer})

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return IngestResponse(
        ok=True,
        last_visit_id=max_id,
        accepted_visits=len(new_visits),
        skipped_visits=skipped,
        accepted_windows=len(req.windows),
    )
