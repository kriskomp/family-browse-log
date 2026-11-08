/* Commands for the agent of one account on one PC (cfg.AgentCommand, since 2026.11.06.8).

   The agent fetches pending commands with GET /config (about once a cycle) and confirms them
   in its next POST /ingest; the confirmation sets AckedAt. Nothing has to be done on the PC,
   and the child may be using it at that moment.

   Run the statements one by one in CloudBeaver (no GO, names are qualified). Template only. */

/* ---- 'backfill': read the history again from <Arg> days back (empty = setting backfill_days,
        default 7), at most max_backfill (default 3000) newest visits per profile.
        What the server already has is skipped, so this is safe to repeat.
        Use it when history is missing: the agent catches up on its own. ---- */
-- INSERT INTO monitor.cfg.AgentCommand (Computer, UserAccount, Command, Arg)
-- VALUES ('LEGION', 'LEGION\vvojc', 'backfill', 14);

/* ---- pending commands (not confirmed by the agent yet) ---- */
-- SELECT * FROM monitor.cfg.AgentCommand WHERE AckedAt IS NULL;

/* ---- the last ones, with when the agent confirmed them ---- */
-- SELECT TOP 20 * FROM monitor.cfg.AgentCommand;

/* ---- what the agents report about each Edge profile (version, History file time,
        newest visit in it, last problem) next to what the server stored ---- */
-- SELECT s.Computer, s.UserAccount, s.EdgeProfile, s.AgentVersion, s.ReportedAt,
--        s.HistoryMtime, s.MaxVisitTime AS newest_visit_in_file, s.RowsRead, s.LastError,
--        (SELECT MAX(LastVisit) FROM monitor.dbo.BrowsingHistory h
--          WHERE h.Computer = s.Computer AND h.UserAccount = s.UserAccount
--            AND h.EdgeProfile = s.EdgeProfile) AS newest_visit_stored
--   FROM monitor.dbo.AgentStatus s
--  ORDER BY s.ReportedAt DESC;
