/* =====================================================================
   family-browse-log - helper: tune agent settings from the database.
   Settings are served by GET /config and consumed by the agent, so you
   change behaviour here WITHOUT touching the PowerShell script.
   Precedence when resolved: user > computer > global.
   ===================================================================== */
USE monitor;
GO

/* ---- window_all: 0 = only the active (foreground) window [recommended],
                    1 = all open windows (v1 behaviour) ---- */
MERGE cfg.Setting AS t
USING (VALUES ('window_all','0')) AS s (SettingKey, SettingValue)
    ON t.Scope='global' AND t.Computer IS NULL AND t.UserAccount IS NULL AND t.SettingKey=s.SettingKey
WHEN MATCHED THEN UPDATE SET SettingValue = s.SettingValue
WHEN NOT MATCHED THEN
    INSERT (Scope, Computer, UserAccount, SettingKey, SettingValue)
    VALUES ('global', NULL, NULL, s.SettingKey, s.SettingValue);
GO

/* ---- examples (uncomment to use) ----

-- change a global value:
-- UPDATE cfg.Setting SET SettingValue='60'
--  WHERE Scope='global' AND Computer IS NULL AND UserAccount IS NULL AND SettingKey='push_interval_sec';

-- per-user override (e.g. all windows for one child):
-- MERGE cfg.Setting AS t
-- USING (VALUES ('window_all','1')) AS s (SettingKey, SettingValue)
--     ON t.Scope='user' AND t.UserAccount='LENOVO\Dziecko' AND t.SettingKey=s.SettingKey
-- WHEN MATCHED THEN UPDATE SET SettingValue=s.SettingValue
-- WHEN NOT MATCHED THEN INSERT (Scope,Computer,UserAccount,SettingKey,SettingValue)
--     VALUES ('user',NULL,'LENOVO\Dziecko',s.SettingKey,s.SettingValue);

-- exclude the parent/test account (agent then gets enabled:false):
-- INSERT INTO cfg.ExcludedUser (UserAccount, Computer, Reason)
-- VALUES ('THINKBOOK\krisk', NULL, 'parent/test account');

-- see current effective settings rows:
-- SELECT Scope, Computer, UserAccount, SettingKey, SettingValue FROM cfg.Setting ORDER BY SettingKey, Scope;
*/
