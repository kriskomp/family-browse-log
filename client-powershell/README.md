# client-powershell — the agent

The Windows agent that runs on each child's PC. It reads Microsoft Edge history and
active-window activity, and pushes both to the `server-python/` service over the
JSON contract in [`../docs/api.md`](../docs/api.md).

## Status

This folder is the **reference home** for the agent. The current working agent is the
**v1** `monitor.ps1`, which still writes directly to MS SQL and is kept running
**locally** on the PCs — it is intentionally **not** committed here yet.

The v2 agent (talking to the endpoint instead of the database) will be adapted from
v1 and placed here. Until then, this folder holds only this note so the monorepo
makes the client/server split explicit.

## Planned behaviour (v2)

1. On start: `GET /config` with `computer` + `user` → receive `enabled`, `settings`,
   and `last_visit_id` per Edge profile. If `enabled` is `false`, idle.
2. Loop: read new Edge visits (`visit_id > last_visit_id`) in blocks of
   `block_size`, plus active-window samples; `POST /ingest` each block.
3. Advance the local resume point only after the server ACK.
4. Buffer locally when the server is unreachable (e.g. off the home network without
   Tailscale) and retry later.

The agent holds only the server URL and an API token (in `settings.dat`,
git-ignored) — **no database password**.
