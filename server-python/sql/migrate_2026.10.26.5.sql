/* 2026.10.26.5 - window activity per session: ONE-OFF migration of existing data.

   STEP 1 - the columns EndTime, SessionStart, ActiveSeconds must exist
            (schema.sql, part "Upgrade from 2026.10.25.4").
   STEP 2 - run this script against database [monitor] AS ONE BATCH
            (CloudBeaver: select all, Ctrl+Enter).

   Phase 1 - rows without SessionStart (one row per 30 s sample, or an early test build):
             each account's rows are split into sessions; a new session starts after a
             pause longer than @gap seconds with no row at all (PC off, asleep, logged off).
   Phase 2 - ALL rows: inside each session, rows with the same key are merged into ONE:
               browsers (@browsers)  -> key = Program            (title = the latest one)
               other programs        -> key = Program + WindowTitle
             EventTime = first time in front, EndTime = last time in front,
             ActiveSeconds = total time in front, SessionMinutes = ActiveSeconds / 60.
             Merged-away rows are deleted.

   Re-running is harmless (already merged rows form groups of one). One transaction.
   Preview first: @apply = 0 (default) only SHOWS the effect and rolls back. */

DECLARE @apply    BIT = 0;      -- 0 = preview only, 1 = apply
DECLARE @sample   INT = 30;     -- window_sample_sec
DECLARE @gap      INT = 360;    -- session gap = idle_limit_sec + 2 * window_sample_sec
DECLARE @browsers NVARCHAR(400) = N'msedge,chrome,firefox,brave,opera';  -- = FBL_WINDOW_GROUP_BY_PROGRAM

BEGIN TRAN;

/* ---- phase 1: sessions for rows that have none ---------------------------------- */
;WITH b AS (
    SELECT Id, Computer, UserAccount, EventTime,
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
SELECT Id, E, Secs,
       MIN(EventTime) OVER (PARTITION BY Computer, UserAccount, Sess) AS SessStart
  INTO #p1 FROM s;

UPDATE w
   SET SessionStart = p.SessStart, EndTime = p.E,
       ActiveSeconds = p.Secs, SessionMinutes = p.Secs / 60
  FROM dbo.WindowActivity w
  JOIN #p1 p ON p.Id = w.Id;

/* ---- phase 2: one row per key inside each session ------------------------------- */
SELECT w.Id, w.Computer, w.UserAccount, w.SessionStart, w.Program, w.WindowTitle,
       w.EventTime, ISNULL(w.EndTime, w.EventTime) AS E, w.ActiveSeconds,
       CASE WHEN b.value IS NOT NULL THEN NULL ELSE w.WindowTitle END AS TitleKey
  INTO #k
  FROM dbo.WindowActivity w
  LEFT JOIN (SELECT DISTINCT LOWER(LTRIM(RTRIM(value))) AS value
               FROM STRING_SPLIT(@browsers, ',')) b
         ON b.value = LOWER(w.Program);

SELECT k.*,
       ROW_NUMBER() OVER (PARTITION BY Computer, UserAccount, SessionStart,
                                       LOWER(Program), TitleKey
                          ORDER BY E DESC, Id DESC) AS Rn,
       DENSE_RANK() OVER (ORDER BY Computer, UserAccount, SessionStart,
                                   LOWER(Program), TitleKey) AS Grp
  INTO #r FROM #k k;

SELECT Grp,
       MIN(Id)            AS KeepId,
       MIN(EventTime)     AS FirstTime,
       MAX(E)             AS LastTime,
       SUM(ActiveSeconds) AS Secs,
       MAX(CASE WHEN Rn = 1 THEN WindowTitle END) AS LastTitle,
       COUNT(*)           AS OldRows
  INTO #g
  FROM #r
 GROUP BY Grp;

SELECT 'preview' AS what,
       (SELECT COUNT(*) FROM #p1) AS rows_given_session,
       SUM(OldRows) AS rows_before, COUNT(*) AS rows_after,
       SUM(OldRows) - COUNT(*) AS rows_to_delete
  FROM #g;

IF @apply = 1
BEGIN
    UPDATE w
       SET EventTime      = g.FirstTime,
           EndTime        = g.LastTime,
           ActiveSeconds  = g.Secs,
           SessionMinutes = g.Secs / 60,
           WindowTitle    = g.LastTitle
      FROM dbo.WindowActivity w
      JOIN #g g ON g.KeepId = w.Id
     WHERE g.OldRows > 1;

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

DROP TABLE IF EXISTS #p1;
DROP TABLE IF EXISTS #k;
DROP TABLE IF EXISTS #r;
DROP TABLE IF EXISTS #g;
