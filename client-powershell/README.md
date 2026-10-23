# client-powershell — the agent

The Windows agent that runs on each child's PC. It reads Microsoft Edge history and
active-window activity and pushes both to the `server-python/` service over the JSON
contract in [`../docs/api.md`](../docs/api.md).

It holds **no database password** — only the server URL and an API token.

## Self-locating

`monitor.ps1` resolves all its paths from its own folder (`$PSScriptRoot`):
`state/`, `monitor_log.txt` and `settings.dat` live next to the script. Put the
folder anywhere — no hard-coded `C:\boot`.

## Setup

1. Copy the script + `settings.example.dat` into a folder on the PC (e.g.
   `C:\fbl\` or wherever you like).
2. Create `settings.dat` next to `monitor.ps1` (it is git-ignored):

   ```powershell
   @('http://netguard.local:8080','<API_TOKEN>')
   #  [0] = endpoint base URL        [1] = API token (from the server's .env)
   ```

3. Run for a quick test:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\monitor.ps1
   ```

   (The script sleeps 20 s on start by design — it waits for the desktop. Logs go to
   `monitor_log.txt` in the same folder.)

4. For unattended running, use the same Scheduled Task approach as v1
   (logon trigger). The task's working/script path just points at wherever you put
   this folder.

## How it works

1. **On each cycle** it calls `GET /config` → receives `enabled`, the settings
   (`push_interval_sec`, `block_size`, …) and the authoritative `last_visit_id` per
   Edge profile. If `enabled` is `false` (user excluded on the server) it idles.
2. **History**: every `history_read_sec` it reads new Edge visits
   (`visit_id > cursor`) in blocks of `block_size` from a copy of the `History`
   database (so a running Edge doesn't block it).
3. **Windows**: it samples the active-window title when the user is active.
4. **Push**: every `push_interval_sec` it sends queued blocks via `POST /ingest`.
5. **Offline-safe**: each block is written to `state/queue/` first and removed only
   after the server ACKs. If the endpoint is unreachable (e.g. off the home network),
   blocks stay queued and are retried next cycle. The server is idempotent, so a
   retried block never double-counts.

## Relationship to v1

The v1 agent (writing directly to SQL, in `C:\boot`) is separate and can keep running
during testing. This v2 agent talks only to the endpoint. Run them from different
folders so their `state/` and logs don't mix.
