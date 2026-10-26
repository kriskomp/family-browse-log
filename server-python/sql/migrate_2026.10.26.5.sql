/* 2026.10.26.5 - window activity per session: ONE-OFF migration of existing data.

   STEP 1 - run schema.sql (or at least its "Upgrade from 2026.10.25.4" part) first,
            so the columns EndTime, SessionStart and ActiveSeconds exist.
   STEP 2 - run this script against database [monitor].

   What it does with rows that have no SessionStart yet (written before this version,
   one row per 30 s sample, or by an early test build of this branch):
   1. splits each account's rows into sessions: a new session starts after a pause
      longer than @gap seconds with no row at all (PC off, asleep, logged off),
   2. merges, inside each session, all rows of the same Program + WindowTitle into ONE
      row - also when the user switched away and came back:
        EventTime     = first time in front, EndTime = last time in front,
        ActiveSeconds = total time in front, SessionMinutes = ActiveSeconds / 60,
        SessionStart  = first row of the session,
   3. deletes the merged-away rows.

   Rows that already have SessionStart are not touched, so re-running is harmless.
   One transaction. Preview first: @apply = 0 (default) only SHOWS the effect. */

DECLARE @apply  BIT = 0;     -- 0 = preview only, 1 = apply
DECLARE @sample INT = 30;    -- window_sample_sec
DECLARE @gap    INT = 360;   -- session gap = idle_limit_sec + 2 * window_sample_sec

BEGIN TRAN;

;WITH b AS (
    SELECT Id, Computer, UserAccount, Program, WindowTitle, EventTime,
           ISNULL(EndTime, EventTime) AS E,
           CASE WHEN ActiveSeconds > 0 THEN ActiveSeconds
                WHEN EndTime IS NULL   THEN @sample
                ELSE DATEDIFF(SECOND, EventTime, EndTime) + @sample END AS Secs
      FROM dbo.WindowActivity
     WHERE SessionStart IS NULL
), p AS (
    SELECT *,
           MAX(E) OVER (PARTITION BY Computer, UserAccount ORDER BY EventTime, Id
                        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS PrevEnd
      FROM b
), f AS (
    SELECT *,
           CASE WHEN PrevEnd IS NULL OR DATEDIFF(SECOND, PrevEnd, EventTime) > @gap
                THEN 1 ELSE 0 END AS IsNew
      FROM p
), s AS (
    SELECT *,
           SUM(IsNew) OVER (PARTITION BY Computer, UserAccount ORDER BY EventTime, Id
                            ROWS UNBOUNDED PRECEDING) AS Sess
      FROM f
)
SELECT Id, Computer, UserAccount, Program, WindowTitle, EventTime, E, Secs, Sess,
       MIN(EventTime) OVER (PARTITION BY Computer, UserAccount, Sess) AS SessStart
  INTO #r FROM s;

SELECT Computer, UserAccount, Sess, Program, WindowTitle,
       MIN(Id) AS KeepId, MIN(EventTime) AS FirstTime, MAX(E) AS LastTime,
       SUM(Secs) AS Secs, MIN(SessStart) AS SessStart, COUNT(*) AS OldRows
  INTO #g
  FROM #r
 GROUP BY Computer, UserAccount, Sess, Program, WindowTitle;

SELECT 'preview' AS what,
       (SELECT COUNT(DISTINCT CONCAT(Computer, N'|', UserAccount, N'|', Sess)) FROM #g) AS sessions,
       COUNT(*) AS rows_after, SUM(OldRows) AS rows_before,
       SUM(OldRows) - COUNT(*) AS rows_to_delete
  FROM #g;

IF @apply = 1
BEGIN
    UPDATE w
       SET EventTime      = g.FirstTime,
           EndTime        = g.LastTime,
           SessionStart   = g.SessStart,
           ActiveSeconds  = g.Secs,
           SessionMinutes = g.Secs / 60
      FROM dbo.WindowActivity w
      JOIN #g g ON g.KeepId = w.Id;

    DELETE w
      FROM dbo.WindowActivity w
      JOIN #r r ON r.Id = w.Id
     WHERE NOT EXISTS (SELECT 1 FROM #g g WHERE g.KeepId = w.Id);

    COMMIT;
    SELECT 'applied' AS what, COUNT(*) AS rows_now FROM dbo.WindowActivity;
END
ELSE
BEGIN
    ROLLBACK;
    SELECT 'nothing changed (preview). Set @apply = 1 to execute.' AS what;
END

DROP TABLE IF EXISTS #r;
DROP TABLE IF EXISTS #g;
