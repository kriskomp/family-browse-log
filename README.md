# family-browse-log

Self-hosted system for monitoring children's web browsing (Microsoft Edge history +
window activity) on Windows PCs, storing everything in a central MS SQL database.
A self-hosted alternative to Microsoft Family Safety.

This is a **monorepo with two parts**, intentionally kept side by side so the
language and role of each is obvious:

| Folder | Language | Role |
|---|---|---|
| [`client-powershell/`](client-powershell/) | PowerShell | **Client** — the agent that runs on each child's Windows PC. Reads Edge history + active-window activity and pushes it to the server. |
| [`server-python/`](server-python/) | Python (FastAPI) | **Service** — the ingest endpoint that runs 24/7 (on `netguard`). Holds configuration, receives data from agents, writes to MS SQL. |

The two parts talk only through a JSON HTTP contract documented in
[`docs/api.md`](docs/api.md) — that file is the single source of truth for both sides.

## How it works (v2)

```
 Windows PC (child)                         netguard (LXC, 24/7)              MS SQL
┌────────────────────┐   GET /config      ┌───────────────────┐   writes   ┌──────────┐
│ client-powershell  │ ─────────────────► │   server-python   │ ─────────► │ monitor  │
│  (monitor.ps1)     │   POST /ingest     │   (FastAPI)       │            │  db      │
└────────────────────┘ ◄───────────────── └───────────────────┘            └──────────┘
        agent              settings + ACK        endpoint
```

- The **agent is thin**: on start it asks the server "who am I and what are my
  settings", then pushes data in small blocks. It holds **no database password** —
  only the server URL and an API token.
- The **server owns everything else**: configuration (editable in the DB without
  touching the agent), per-user exclusions, deduplication, daily aggregation, and
  the resume state (`LastVisitId`).

## Database

All v2 tables live in a dedicated MS SQL database **`monitor`**, split by schema:

- **`dbo`** — incoming data: `BrowsingHistory`, `WindowActivity`, `IngestState`
- **`cfg`** — configuration: `Setting`, `ExcludedUser`, `Device`

See [`server-python/sql/schema.sql`](server-python/sql/schema.sql).

## Status

- **v1** (standalone PowerShell agent writing directly to SQL) is in use and is
  **not** part of this repo — it stays local and keeps running.
- **v2** (this repo) is the endpoint-based architecture. The server is built first;
  the agent in `client-powershell/` is adapted to the new contract separately.

## Versioning

Date-based: `YYYY.MM.DD.N` (date + Nth release that day), kept in
[`VERSION`](VERSION) and [`CHANGELOG.md`](CHANGELOG.md).
