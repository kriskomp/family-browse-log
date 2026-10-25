# Changelog

All notable changes to this project are documented here.
Versions are date-based: `YYYY.MM.DD.N`.

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
