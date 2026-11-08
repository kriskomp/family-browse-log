/* 2026.11.06.8 - upgrade: visits identified by TIME, commands and diagnostics for the agent.

   Run this against the server BEFORE deploying the new server code, as ONE BATCH
   (CloudBeaver: select all, Ctrl+Enter). No GO, every name is qualified with [monitor].
   Safe to run again: every object is created only if it does not exist yet.

   Creates
     monitor.dbo.BrowsingVisit  one row per ingested visit (account, profile, time, URL hash);
                                the server's de-duplication and the agent's resume point
     monitor.dbo.AgentStatus    what each agent reports about each Edge profile
     monitor.cfg.AgentCommand   commands for one account on one PC (see agent_commands.sql)
     cfg.Setting rows           history_overlap_sec = 900, backfill_days = 7 (only if missing)

   Existing data is NOT changed. dbo.IngestState stays (older agents still read it). */

IF OBJECT_ID('monitor.dbo.BrowsingVisit','U') IS NULL
BEGIN
    CREATE TABLE monitor.dbo.BrowsingVisit (
        Id          BIGINT IDENTITY(1,1) NOT NULL,
        Computer    NVARCHAR(64)  NOT NULL,
        UserAccount NVARCHAR(128) NOT NULL,
        EdgeProfile NVARCHAR(128) NOT NULL,
        VisitTs     BIGINT        NOT NULL,
        UrlHash     BINARY(32)    NOT NULL,
        CONSTRAINT PK_BrowsingVisit PRIMARY KEY CLUSTERED (Id DESC)
    );
    CREATE UNIQUE INDEX UX_BrowsingVisit_key
        ON monitor.dbo.BrowsingVisit (Computer, UserAccount, EdgeProfile, VisitTs, UrlHash);
END

IF OBJECT_ID('monitor.dbo.AgentStatus','U') IS NULL
BEGIN
    CREATE TABLE monitor.dbo.AgentStatus (
        Computer     NVARCHAR(64)  NOT NULL,
        UserAccount  NVARCHAR(128) NOT NULL,
        EdgeProfile  NVARCHAR(128) NOT NULL,
        ReportedAt   DATETIME2(7)  NOT NULL,
        AgentVersion NVARCHAR(32)  NULL,
        HistoryMtime DATETIME2(7)  NULL,
        MaxVisitId   BIGINT        NULL,
        MaxVisitTime DATETIME2(7)  NULL,
        RowsRead     INT           NOT NULL CONSTRAINT DF_AgentStatus_RowsRead DEFAULT 0,
        LastError    NVARCHAR(256) NULL,
        CONSTRAINT PK_AgentStatus PRIMARY KEY (Computer, UserAccount, EdgeProfile)
    );
END

IF OBJECT_ID('monitor.cfg.AgentCommand','U') IS NULL
BEGIN
    CREATE TABLE monitor.cfg.AgentCommand (
        Id          INT IDENTITY(1,1) NOT NULL,
        Computer    NVARCHAR(64)  NOT NULL,
        UserAccount NVARCHAR(128) NOT NULL,
        Command     NVARCHAR(32)  NOT NULL,
        Arg         INT           NULL,
        CreatedAt   DATETIME2(7)  NOT NULL CONSTRAINT DF_AgentCommand_CreatedAt DEFAULT (SYSDATETIME()),
        AckedAt     DATETIME2(7)  NULL,
        CONSTRAINT PK_AgentCommand PRIMARY KEY CLUSTERED (Id DESC),
        CONSTRAINT CK_AgentCommand_Command CHECK (Command IN ('backfill'))
    );
    CREATE INDEX IX_AgentCommand_pending
        ON monitor.cfg.AgentCommand (Computer, UserAccount, AckedAt);
END

MERGE monitor.cfg.Setting AS t
USING (VALUES
    ('history_overlap_sec', '900'),
    ('backfill_days',       '7')
) AS s (SettingKey, SettingValue)
    ON  t.Scope = 'global'
    AND t.Computer IS NULL
    AND t.UserAccount IS NULL
    AND t.SettingKey = s.SettingKey
WHEN NOT MATCHED THEN
    INSERT (Scope, Computer, UserAccount, SettingKey, SettingValue)
    VALUES ('global', NULL, NULL, s.SettingKey, s.SettingValue);

/* check: three tables exist, two new settings are there */
SELECT (SELECT COUNT(*) FROM monitor.sys.tables WHERE name IN ('BrowsingVisit','AgentStatus','AgentCommand')) AS new_tables,
       (SELECT COUNT(*) FROM monitor.cfg.Setting
         WHERE SettingKey IN ('history_overlap_sec','backfill_days')) AS new_settings;
