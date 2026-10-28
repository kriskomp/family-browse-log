/* =====================================================================
   family-browse-log - helper: link accounts of different PCs into one person.

   Used by the read API (GET /persons, GET /usage, person= in GET /activity),
   so Home Assistant can show one child's time across all PCs and per PC.
   Nothing here changes what the agents record.

   UserName = the login WITHOUT the computer part: LEGION\anna -> anna.
   One row covers that login on EVERY PC (Computer NULL). Give Computer to restrict
   a row to one PC (it wins over a row without Computer).
   Excluded = 1 hides the person in the read API (e.g. parents, tests). It is not
   cfg.ExcludedUser (that one stops the agent from recording).
   An account with no row is shown as its own person named after its login.
   Names are matched case-insensitively. Changes apply at once, no restart.
   ===================================================================== */
USE monitor;
GO

/* ---- 1) see accounts that exist and who they currently resolve to -------------- */
SELECT DISTINCT w.Computer, w.UserAccount,
       p.Person AS linked_to, p.Excluded
  FROM dbo.WindowActivity w
  LEFT JOIN cfg.Person p
         ON p.UserName = RIGHT(w.UserAccount, CHARINDEX('\', REVERSE(w.UserAccount) + '\') - 1)
        AND (p.Computer IS NULL OR p.Computer = w.Computer)
 ORDER BY w.UserAccount;
GO

/* ---- 2) link logins to a person (EXAMPLE names - replace with yours) ----------- */
IF NOT EXISTS (SELECT 1 FROM cfg.Person WHERE UserName = N'anna' AND Computer IS NULL)
    INSERT INTO cfg.Person (Person, Computer, UserName) VALUES (N'Anna', NULL, N'anna');

-- the same person has a different login on another PC: one more row, same Person
IF NOT EXISTS (SELECT 1 FROM cfg.Person WHERE UserName = N'ania' AND Computer IS NULL)
    INSERT INTO cfg.Person (Person, Computer, UserName) VALUES (N'Anna', NULL, N'ania');

-- a parent: shown nowhere unless the API is asked with include_excluded=true
IF NOT EXISTS (SELECT 1 FROM cfg.Person WHERE UserName = N'parent' AND Computer IS NULL)
    INSERT INTO cfg.Person (Person, Computer, UserName, Excluded) VALUES (N'Parent', NULL, N'parent', 1);
GO

/* ---- 3) change / remove ---------------------------------------------------------
   UPDATE cfg.Person SET Person = N'Ania' WHERE Person = N'Anna';  -- rename everywhere
   UPDATE cfg.Person SET Excluded = 0 WHERE Person = N'Parent';    -- show again
   DELETE FROM cfg.Person WHERE UserName = N'ania';                -- unlink
*/
