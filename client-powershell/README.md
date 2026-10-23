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
   database (so a running Edge doesn't block it). It drains the whole backlog in
   consecutive blocks on first run, then only the increment.
3. **Windows**: it samples the active (foreground) window when the user is active.
   Set `window_all=1` in `cfg.Setting` to record every open window instead (v1 style).
4. **Push**: every `push_interval_sec` it sends queued blocks via `POST /ingest`.
5. **Offline-safe**: each block is written to the account's own queue
   (`state\queue_<account>\`) first and removed only
   after the server ACKs. If the endpoint is unreachable (e.g. off the home network),
   blocks stay queued and are retried next cycle. The server is idempotent, so a
   retried block never double-counts. A corrupt or empty block is dropped (logged)
   so it cannot block the rest of the queue.
6. **Per account — separate files for everything**: several Windows accounts may use
   one PC and one `state\` folder, and each account's agent keeps its own state:

   | What | File / place | Notes |
   |---|---|---|
   | Edge history cursor | `state\cursor_<account>_<profile>.txt` | one per account and Edge profile |
   | Edge history copy (temp) | `state\tmp_hist_<account>.db` | read copy of that account's `History` |
   | Queue of blocks | `state\queue_<account>\*.json` | history visits **and** window (program) rows |
   | Log | `monitor_log_<account>.txt` (next to the script) | one log per account |
   | Program / window list | no file of its own | sampled in memory by that account's agent and sent in its queue |

   The database separates accounts by the `UserAccount` column (and `Computer`), so
   the data of one account never mixes with another's. `<account>` is the Windows
   `DOMAIN\user` name with non-alphanumeric characters replaced by `_`
   (e.g. `LEGION_vvojc`).

   **The only shared file is `settings.dat`**, read-only configuration with no user data.

7. **One agent per account**: a second agent for the same account in the same logon
   session exits at once (logged as `INFO`), so two instances never share a queue or cursor.
   Different accounts are not affected.

   The old shared `state\queue\` folder from earlier versions is no longer read or moved.
   Nothing sends its blocks any more, so check its contents and delete it by hand.

## Relationship to v1

The v1 agent (writing directly to SQL, in `C:\boot`) is separate and can keep running
during testing. This v2 agent talks only to the endpoint. Run them from different
folders so their `state/` and logs don't mix.
