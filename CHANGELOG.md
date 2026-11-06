# Changelog

All notable changes to this project are documented here.
Versions are date-based: `YYYY.MM.DD.N`.

## 2026.11.06.8

History is resumed by TIME, decided by the server; the agent keeps no cursor. Fixes Edge history silently stopping for an account (LEGION\vvojc, 25.10 - 06.11.2026).

### Why
The agent resumed from Edge's `visits.id`, kept both in a file on the PC and in `dbo.IngestState`. When Edge rebuilds its History file the ids start again, so the stored id is higher than every new visit and nothing is read again - silently, with nothing in the log. Fixing it meant editing files on the child's PC.

### Added
- `dbo.BrowsingVisit`: one row per visit stored (account, profile, time, URL hash). A visit is new when it has no row yet, so reading the same visits twice is harmless (idempotent, no `VisitCount` double counting).
- `GET /config` returns `since` (newest visit per profile, unix seconds), `commands` and `server_time`. The agent resumes from `since` minus `history_overlap_sec` (default 900), keeps the position in memory only, and re-reads from the last position every cycle.
- `cfg.AgentCommand` + command `backfill`: queued in SQL (`sql/agent_commands.sql`), picked up by the agent within a cycle, confirmed in its next `POST /ingest` (`acked`). Re-reads the last N days (`Arg`, default setting `backfill_days` = 7), at most `max_backfill` newest visits. Nothing is done on the PC.
- `dbo.AgentStatus`: what each agent reports per Edge profile (`agent_version`, `diag`: History file time, newest visit in it, rows read, last error). Lets the server see "the file has newer visits than were stored".
- Settings `history_overlap_sec` (900) and `backfill_days` (7); `.env` key `FBL_VISIT_DEDUP_DAYS` (60, `0` = keep all) for how long `dbo.BrowsingVisit` rows are kept.
- Agent: an Edge profile with no History file or a failing read is now logged and reported instead of silently skipped.

### Changed
- The agent no longer uses `state\cursor_*.txt` (old files are ignored and can be deleted). `IngestState.LastVisitId` is still maintained for agents older than this version but is not used to decide what is new.
- A profile that already had data before this version gets a marker row in `dbo.BrowsingVisit`: visits up to its newest stored visit count as already ingested, so the upgrade does not count them twice.

### Notes
- Upgrade order: run `server-python/sql/upgrade_2026.11.06.8.sql`, deploy the server, then update the agents (`server-python/README.md`, *Upgrading to 2026.11.06.8*). A new agent against an old server waits (it needs `since`); old agents keep working against the new server.
- Two visits of the same URL in the same second count as one.
- Not verified: the exact cause of the stopped history for `LEGION\vvojc` (its History file cannot be read from the server); the agent was not run on Windows before this release (tested: server logic with unit tests, the read-position algorithm in a simulation).

## 2026.10.30.7

Usage: technical windows can be left out.

### Added
- `FBL_USAGE_IGNORE_TITLES` (default `Microsoft Family Safety`, separated by `;`): window titles left out of `GET /usage` entirely. Exact match, case-insensitive; browser titles that only contain the phrase are kept. The stored data is not changed and `/activity` is not affected.

## 2026.10.28.6

Persons and time in front of the computer (server side, groundwork for the Home Assistant integration).

### Added
- `GET /usage`: time in front of the computer per person: total, per PC, per program (top N, `programs_other`) and per day, with averages. `date`, `from`/`to` (a month is a range), `person`, `computer`, `programs`, `include_excluded`; default is today. Every duration has exact `seconds` and a readable `text` (`1 h 15 min`). A window row counts on the day of its last activity.
- `GET /persons`: people and their accounts. Table `cfg.Person` links the logins of one person across PCs (`UserName` without the computer part, optional `Computer`, `Excluded` to hide a person); an account without a row is its own person, so nothing has to be configured. Edited with `server-python/sql/persons.sql` (a template with example names).
- `person` in `GET /users` and in every `GET /activity` item, and a `person=` filter on `GET /activity`.

- `GET /usage` lists host processes by window title: Microsoft Store apps all run in `ApplicationFrameHost`, so they are named by the app (`Solitaire & Casual Games`) with `"host"` set, instead of lumped together. `.env` key `FBL_USAGE_HOST_PROGRAMS` (default `ApplicationFrameHost`, comma-separated, empty = none).

### Fixed
- `docs/api.md`: a stale copy of the old window-grouping text left behind by 2026.10.26.5 is removed.

### Notes
- Read API login needs `SELECT` on `cfg.Person`. Until the table exists the API still works.
- `cfg.Person.Excluded` (hide in the read API) is not `cfg.ExcludedUser` (stop recording).
- Upgrade steps: `server-python/README.md`, *Upgrading to 2026.10.28.6*.

## 2026.10.26.5

Window activity: one row per window per session.

### Changed
- `dbo.WindowActivity` holds **one row per window (program + title) per session** instead of one row per 30-second sample. Coming back to a window in the same session updates its row: `EndTime` (last time in front) moves forward and `ActiveSeconds` / `SessionMinutes` (total time in front) grow. Switching windows no longer creates duplicate rows.
- Browsers (`FBL_WINDOW_GROUP_BY_PROGRAM`, default `msedge,chrome,firefox,brave,opera`) have **one row per session**, whatever the tab: the title changes with every tab or tab count, so the row keeps the latest title (pages are in `BrowsingHistory`). Other programs: one row per program + window title.
- A session ends after no sample of any window for `idle_limit_sec + 2 * window_sample_sec` (default 6 min): PC off, asleep, logged off. Idle reading or a film does not split it. Gaps are measured on sample times, so offline queues, connection outages and server restarts never start a new session.
- Re-sent blocks are skipped (samples not newer than the account's newest `EndTime`) and not counted in `accepted_windows`.
- `GET /activity` window items carry `from`, `to`, `minutes` and `active` (`true` = window in front now; computed at read time, nothing stored). `time` is kept and equals `to`.
- Agent: queue file names are UTC timestamps, so blocks are always sent oldest first, also when the clock is turned back at the end of DST. `session_min` in the payload is ignored.

### Added
- Columns `EndTime`, `SessionStart`, `ActiveSeconds` and index `IX_WindowActivity_session` on `dbo.WindowActivity` (`schema.sql`, idempotent).
- `server-python/sql/migrate_2026.10.26.5.sql`: one-off, preview-first, re-runnable merge of existing rows into sessions (browsers: one row per session).
- `.env` key `FBL_WINDOW_GROUP_BY_PROGRAM`.

### Notes
- DB login needs `UPDATE` on `dbo.WindowActivity`.
- Upgrade steps: `server-python/README.md`, *Upgrading to 2026.10.26.5*.

## 2026.10.25.4

Read API for Home Assistant (server side).

### Added
- `GET /activity` — newest web and window activity merged into one timeline, with `kind` (`web`/`windows`/`both`), `user` (repeatable), `computer`, `limit` (default `FBL_ACTIVITY_DEFAULT_LIMIT`, capped at `FBL_ACTIVITY_MAX_LIMIT`), and a `summary` with the newest record of each kind.
- `GET /users` — accounts that have data, with `last_seen` and `excluded`, for the Home Assistant account picker.
- Separate reader token `FBL_READER_TOKEN`. It cannot write or read agent config; the agent token cannot read the activity API.

### Fixed
- `summary` on `GET /activity` reports the newest record of each kind even when `kind` leaves one kind out (previously it showed `null` for the omitted kind).

### Changed
- Agent token is `FBL_AGENT_TOKEN`. `FBL_API_TOKEN` is still accepted as an alias, so existing agents keep working.
- Read API returns local time with its offset and never includes URL query strings or fragments.
- Token comparison works on bytes, so a non-ASCII token returns `401` instead of `500`.

### Notes
- The DB login needs `SELECT` on `dbo.BrowsingHistory`, `dbo.WindowActivity` and `cfg.ExcludedUser` (see `server-python/README.md`).

## 2026.10.23.3

Follow-up to 2026.10.23.2: per-account separation and single recorder.

### Fixed
- Local resume cursor is now kept **per Windows account** (`cursor_<account>_<profile>.txt`). Previously all accounts on one PC shared `cursor_<profile>.txt`, so one account's cursor hid another account's visits (e.g. a second child's history was never sent).
- Corrupt or empty queue blocks are dropped with an `ERROR` log line instead of blocking the whole queue; empty payloads are never queued or posted.
- Everything that belongs to a Windows account is now per account: queue (`state\queue_<account>\`), Edge-history temp copy (`state\tmp_hist_<account>.db`), log (`monitor_log_<account>.txt`) and cursors. The only shared file is `settings.dat` (read-only). Two accounts running at the same time could previously share one log, and an agent could move another account's blocks into its own queue; both are removed.
- Edge-history temp copy is removed before each read, so a stale `-wal`/`-shm` from a previous Edge profile is never used.
- Single recorder per account: only the agent in the active console session records for its account. Ownership is an exclusive file lock (`state\owner_<account>.lock`), released automatically when the process ends. Fixes two sessions of one account writing the same queue, cursor and window rows at the same time.
- Queue blocks are written as `*.json.tmp` and renamed to `*.json` when complete, so a half-written block is never read (and never mistaken for a corrupt one and deleted).
- The old shared `state\queue\` folder is no longer read or moved; it can be deleted by hand after checking its contents.

### Docs
- Example account name in `docs/api.md`, `server-python/README.md` and `server-python/sql/settings.sql` is now the placeholder `LENOVO\<konto>` (was a test name).

## 2026.10.23.2

v2 agent (client).

### Added
- `client-powershell/monitor.ps1` — the v2 Windows agent:
  - Self-locating via `$PSScriptRoot` (no hard-coded `C:\boot`); `state/`, log and `settings.dat` live next to the script.
  - Talks only to the endpoint (`GET /config`, `POST /ingest`) — holds no database password, only the server URL + API token in `settings.dat`.
  - Reads new Edge visits in blocks of `block_size` from a copy of the `History` db; samples active-window activity; honors `enabled` (server-side exclusion) and all settings from `GET /config`.
  - Offline-safe queue in `state/queue/`: blocks are removed only after the server ACK; unreachable endpoint → retried next cycle (server idempotency prevents double-counting).
- `client-powershell/settings.example.dat` — template for the git-ignored `settings.dat`.

### Changed
- Agent window capture now records only the **active (foreground) window** by default, instead of every open window every cycle — far fewer, more meaningful rows. Controlled by the new `window_all` setting (`0` = active only [default], `1` = all windows, the old v1 behaviour).
- Agent now **drains backfill in consecutive blocks** until caught up, instead of one `block_size` block per `history_read_sec` cycle, so the initial catch-up finishes quickly.
- `window_sample_sec` is now honored as its own cadence (gated independently of pushes).

### Added (settings)
- New `window_all` setting (seeded `0` in `schema.sql`).
- `server-python/sql/settings.sql` — helper to tune settings from the database (incl. `window_all`, per-user overrides, exclusions) without touching the agent.

### Notes
- v1 agent is unaffected and can run in parallel from a different folder during testing.

## 2026.10.23.1

Initial v2 scaffold — endpoint-based architecture.

### Added
- Monorepo layout: `client-powershell/` (agent) and `server-python/` (service).
- `docs/api.md` — JSON contract for `GET /config` and `POST /ingest` (source of truth for both sides).
- `server-python/` — FastAPI ingest endpoint:
  - `GET /config` — returns per-user `enabled` flag, resolved settings, and `last_visit_id` per Edge profile.
  - `POST /ingest` — accepts a block of visits + window-activity rows, aggregates browsing history daily, writes window activity, advances resume state, updates device heartbeat. Idempotent: visits with `visit_id <= LastVisitId` are ignored, so retries never double-count.
  - Bearer-token authentication; no database password on the client.
  - Settings resolver with scope precedence `user > computer > global`.
- `server-python/sql/schema.sql` — MS SQL database `monitor` with schemas `dbo` (data) and `cfg` (configuration), all tables, and seeded default settings.
- `server-python/systemd/family-browse-log.service` — example unit for 24/7 run on netguard.
- GitHub Actions workflow to publish a release from `VERSION` on push to `main`.

### Notes
- v1 (standalone agent → `DB` database) is untouched and keeps running; v2 uses the separate `monitor` database.
