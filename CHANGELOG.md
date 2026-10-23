# Changelog

All notable changes to this project are documented here.
Versions are date-based: `YYYY.MM.DD.N`.

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
