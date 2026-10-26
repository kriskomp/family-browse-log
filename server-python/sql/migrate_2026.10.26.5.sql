/* 2026.10.26.5 - window activity grouping: ONE-OFF migration of existing data.

   Run once in CloudBeaver / sqlcmd against database [monitor], AFTER the new
   server code is deployed (or before - it is safe either way).

   What it does
   1. (STEP 1, run it ALONE before this script - the column must exist at compile time)
          IF COL_LENGTH('dbo.WindowActivity','EndTime') IS NULL
              ALTER TABLE dbo.WindowActivity ADD EndTime DATETIME2(7) NULL;
      The same statement is in schema.sql.
   2. (STEP 2, this script) merges the old "one row per 30 s sample" rows into groups: consecutive rows of
      the same Computer + UserAccount + Program + WindowTitle, with no pause longer
      than @gap seconds, become ONE row:
        EventTime = first sample, EndTime = last sample,
        SessionMinutes = whole minutes between them,
   3. deletes the merged-away duplicate rows.

   Only rows with EndTime IS NULL (not yet grouped) are touched, so re-running is
   harmless. Everything runs in one transaction.

   Preview first: set @apply = 0 (default) to only SHOW what would happen,
   then set @apply = 1 to execute. */

DECLARE @apply BIT = 0;      -- 0 = preview only, 1 = apply
DECLARE @gap   INT = 120;    -- seconds; keep equal to max(120, 3 * window_sample_sec)

BEGIN TRAN;

;WITH r AS (
    SELECT Id, Computer, UserAccount, EventTime, Program, WindowTitle,
           LAG(EventTime)   OVER (PARTITION BY Computer, UserAccount ORDER BY EventTime, Id) AS pt,
           LAG(Program)     OVER (PARTITION BY Computer, UserAccount ORDER BY EventTime, Id) AS pp,
           LAG(WindowTitle) OVER (PARTITION BY Computer, UserAccount ORDER BY EventTime, Id) AS pw
      FROM dbo.WindowActivity
     WHERE EndTime IS NULL
), f AS (
    SELECT *,
           CASE WHEN pt IS NULL
                  OR DATEDIFF(SECOND, pt, EventTime) > @gap
                  OR ISNULL(pp,N'')  <> ISNULL(Program,N'')
                  OR ISNULL(pw,N'')  <> ISNULL(WindowTitle,N'')
                THEN 1 ELSE 0 END AS isnew
      FROM r
), g AS (
    SELECT *, SUM(isnew) OVER (PARTITION BY Computer, UserAccount
                               ORDER BY EventTime, Id
                               ROWS UNBOUNDED PRECEDING) AS grp
      FROM f
)
SELECT Id, Computer, UserAccount, EventTime, Program, WindowTitle, grp
  INTO #g FROM g;

SELECT Computer, UserAccount, grp,
       MIN(Id)        AS KeepId,
       MIN(EventTime) AS FirstTime,
       MAX(EventTime) AS LastTime,
       COUNT(*)       AS Samples
  INTO #grp
  FROM #g GROUP BY Computer, UserAccount, grp;

SELECT 'preview' AS what, COUNT(*) AS groups, SUM(Samples) AS old_rows,
       SUM(Samples) - COUNT(*) AS rows_to_delete
  FROM #grp;

IF @apply = 1
BEGIN
    UPDATE w
       SET EventTime      = x.FirstTime,
           EndTime        = x.LastTime,
           SessionMinutes = DATEDIFF(SECOND, x.FirstTime, x.LastTime) / 60
      FROM dbo.WindowActivity w
      JOIN #grp x ON x.KeepId = w.Id;

    DELETE w
      FROM dbo.WindowActivity w
      JOIN #g a ON a.Id = w.Id
      JOIN #grp x ON x.Computer = a.Computer AND x.UserAccount = a.UserAccount
                 AND x.grp = a.grp
     WHERE w.Id <> x.KeepId;

    COMMIT;
    SELECT 'applied' AS what, COUNT(*) AS rows_now FROM dbo.WindowActivity;
END
ELSE
BEGIN
    ROLLBACK;
    SELECT 'nothing changed (preview). Set @apply = 1 to execute.' AS what;
END

DROP TABLE IF EXISTS #g;
DROP TABLE IF EXISTS #grp;
