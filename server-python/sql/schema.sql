/* =====================================================================
   family-browse-log - v2 schema
   Database: monitor
   Schemas:  dbo  -> incoming data
             cfg  -> configuration
   Safe to run repeatedly (idempotent-ish: guards on object existence).
   Does NOT touch the v1 database (DB) in any way.
   ===================================================================== */

/* ---------- database ---------- */
IF DB_ID('monitor') IS NULL
    CREATE DATABASE monitor;
GO

USE monitor;
GO

/* ---------- schema cfg ---------- */
IF SCHEMA_ID('cfg') IS NULL
    EXEC('CREATE SCHEMA cfg');
GO

/* =====================================================================
   DATA  (schema dbo)
   ===================================================================== */

/* Browsing history, aggregated per (Computer, UserAccount, EdgeProfile, Day, Url).
   PK on Id DESC so a plain SELECT returns newest rows first. */
IF OBJECT_ID('dbo.BrowsingHistory','U') IS NULL
BEGIN
    CREATE TABLE dbo.BrowsingHistory (
        Id          BIGINT IDENTITY(1,1) NOT NULL,
        Computer    NVARCHAR(64)   NOT NULL,
        UserAccount NVARCHAR(128)  NOT NULL,
        EdgeProfile NVARCHAR(128)  NOT NULL,
        [Day]       DATE           NOT NULL,
        Url         NVARCHAR(2048) NOT NULL,
        Title       NVARCHAR(1024) NULL,
        VisitCount  INT            NOT NULL,
        FirstVisit  DATETIME2(7)   NOT NULL,
        LastVisit   DATETIME2(7)   NOT NULL,
        CONSTRAINT PK_BrowsingHistory PRIMARY KEY CLUSTERED (Id DESC)
    );

    /* Upsert lookup. Url (2048) cannot be an index key (1700-byte limit),
       so we seek by day/user and match Url as a predicate - within one day
       and account the number of URLs is small, so this is cheap. */
    CREATE INDEX IX_BrowsingHistory_key
        ON dbo.BrowsingHistory (Computer, UserAccount, EdgeProfile, [Day]);
END
GO

/* Active-window / app activity. One row = one uninterrupted stay in a window:
   EventTime = first sample, EndTime = last sample, SessionMinutes = whole minutes
   between them (grows while the window stays in front). */
IF OBJECT_ID('dbo.WindowActivity','U') IS NULL
BEGIN
    CREATE TABLE dbo.WindowActivity (
        Id             BIGINT IDENTITY(1,1) NOT NULL,
        Computer       NVARCHAR(64)   NOT NULL,
        UserAccount    NVARCHAR(128)  NOT NULL,
        EventTime      DATETIME2(7)   NOT NULL,
        EndTime        DATETIME2(7)   NULL,
        SessionMinutes INT            NOT NULL,
        Program        NVARCHAR(256)  NULL,
        WindowTitle    NVARCHAR(2048) NULL,
        CONSTRAINT PK_WindowActivity PRIMARY KEY CLUSTERED (Id DESC)
    );

    CREATE INDEX IX_WindowActivity_user_time
        ON dbo.WindowActivity (UserAccount, EventTime DESC);
END
GO

/* Upgrade from 2026.10.25.4 and older: add EndTime (idempotent).
   Existing rows keep their single sample: EndTime = EventTime.
   To merge old one-row-per-sample data run sql/migrate_2026.10.26.5.sql. */
IF COL_LENGTH('dbo.WindowActivity','EndTime') IS NULL
    ALTER TABLE dbo.WindowActivity ADD EndTime DATETIME2(7) NULL;
GO

/* Resume state - last processed Edge visit id per (Computer, UserAccount, EdgeProfile). */
IF OBJECT_ID('dbo.IngestState','U') IS NULL
BEGIN
    CREATE TABLE dbo.IngestState (
        Computer    NVARCHAR(64)  NOT NULL,
        UserAccount NVARCHAR(128) NOT NULL,
        EdgeProfile NVARCHAR(128) NOT NULL,
        LastVisitId BIGINT        NOT NULL,
        CONSTRAINT PK_IngestState PRIMARY KEY (Computer, UserAccount, EdgeProfile)
    );
END
GO

/* =====================================================================
   CONFIGURATION  (schema cfg)
   ===================================================================== */

/* Settings as scoped key/value. Resolution precedence: user > computer > global.
   Add a new parameter by inserting a row - no schema change, no agent change. */
IF OBJECT_ID('cfg.Setting','U') IS NULL
BEGIN
    CREATE TABLE cfg.Setting (
        Id           INT IDENTITY(1,1) NOT NULL,
        Scope        NVARCHAR(16)  NOT NULL,   -- 'global' | 'computer' | 'user'
        Computer     NVARCHAR(64)  NULL,
        UserAccount  NVARCHAR(128) NULL,
        SettingKey   NVARCHAR(64)  NOT NULL,
        SettingValue NVARCHAR(256) NOT NULL,
        CONSTRAINT PK_Setting PRIMARY KEY (Id),
        CONSTRAINT CK_Setting_Scope CHECK (Scope IN ('global','computer','user'))
    );

    /* One value per key within a given scope target. */
    CREATE UNIQUE INDEX UX_Setting_scope_key
        ON cfg.Setting (Scope, Computer, UserAccount, SettingKey);
END
GO

/* Users excluded from collection (e.g. the parent's own account).
   Computer NULL = excluded on all machines. */
IF OBJECT_ID('cfg.ExcludedUser','U') IS NULL
BEGIN
    CREATE TABLE cfg.ExcludedUser (
        Id          INT IDENTITY(1,1) NOT NULL,
        UserAccount NVARCHAR(128) NOT NULL,
        Computer    NVARCHAR(64)  NULL,
        Reason      NVARCHAR(256) NULL,
        AddedAt     DATETIME2(7)  NOT NULL CONSTRAINT DF_ExcludedUser_AddedAt DEFAULT (SYSDATETIME()),
        CONSTRAINT PK_ExcludedUser PRIMARY KEY (Id)
    );
END
GO

/* Known machines + heartbeat (for supervision / alerts). */
IF OBJECT_ID('cfg.Device','U') IS NULL
BEGIN
    CREATE TABLE cfg.Device (
        Id       INT IDENTITY(1,1) NOT NULL,
        Computer NVARCHAR(64)  NOT NULL,
        LastSeen DATETIME2(7)  NULL,
        Note     NVARCHAR(256) NULL,
        CONSTRAINT PK_Device PRIMARY KEY (Id),
        CONSTRAINT UX_Device_Computer UNIQUE (Computer)
    );
END
GO

/* =====================================================================
   SEED - default global settings (only if not present)
   ===================================================================== */
MERGE cfg.Setting AS t
USING (VALUES
    ('push_interval_sec', '30'),
    ('window_sample_sec', '30'),
    ('history_read_sec',  '120'),
    ('block_size',        '500'),
    ('max_backfill',      '3000'),
    ('idle_limit_sec',    '300'),
    ('window_all',        '0')     -- 0 = only the active window (recommended), 1 = all windows (v1 behaviour)
) AS s (SettingKey, SettingValue)
    ON  t.Scope = 'global'
    AND t.Computer IS NULL
    AND t.UserAccount IS NULL
    AND t.SettingKey = s.SettingKey
WHEN NOT MATCHED THEN
    INSERT (Scope, Computer, UserAccount, SettingKey, SettingValue)
    VALUES ('global', NULL, NULL, s.SettingKey, s.SettingValue);
GO

/* Example (commented): exclude the parent account on all machines
   INSERT INTO cfg.ExcludedUser (UserAccount, Computer, Reason)
   VALUES ('LENOVO\Krzysztof', NULL, 'parent account');
*/
