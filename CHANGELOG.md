# Changelog

All notable changes to this project are documented here.
Versions are date-based: `YYYY.MM.DD.N`.

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
