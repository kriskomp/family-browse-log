# server-python — the ingest service

FastAPI service that receives data from the agents and writes it to the MS SQL
database `monitor`. Runs 24/7 on `netguard`. Implements the contract in
[`../docs/api.md`](../docs/api.md).

## Layout

```
server-python/
├─ app/
│  ├─ main.py      # FastAPI app + routes (/config, /ingest, /health)
│  ├─ models.py    # request/response schemas
│  ├─ db.py        # pymssql access, settings resolver, exclusion check
│  ├─ ingest.py    # write path: aggregate + upsert in one transaction
│  └─ config.py    # service config from environment / .env
├─ sql/schema.sql  # database 'monitor' + schemas dbo/cfg + tables + seed
├─ systemd/family-browse-log.service
├─ requirements.txt
└─ .env.example
```

## Install (on netguard)

```bash
# 1) database (run once against your MS SQL server)
#    e.g. with sqlcmd / Azure Data Studio / any client:
#    sqlcmd -S docker,1433 -U <admin> -P <pwd> -i sql/schema.sql

# 2) service
cd /opt/family-browse-log/server-python
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env    # then edit .env with real DB creds + API token

# 3) run (dev)
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080

# 3b) run (service)
sudo cp systemd/family-browse-log.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now family-browse-log
```

The DB login only needs: `SELECT` on `cfg.*` and `dbo.*`, and `INSERT`/`UPDATE` on
`dbo.*` + `cfg.Device`. No agent ever holds this password.

The read API (`GET /activity`, `GET /users`) reads `dbo.BrowsingHistory`,
`dbo.WindowActivity` and `cfg.ExcludedUser` with the same login:

```sql
GRANT SELECT ON dbo.BrowsingHistory TO [<login>];
GRANT SELECT ON dbo.WindowActivity  TO [<login>];
GRANT SELECT ON cfg.ExcludedUser    TO [<login>];
GRANT SELECT ON cfg.Person          TO [<login>];   -- since 2026.10.28.6 (persons, usage)
```

## Upgrading to 2026.10.28.6 (persons and usage)

1. Run `sql/schema.sql` (idempotent): it creates `cfg.Person`. Grant `SELECT` on it to the
   read-API login (see above).
2. Pull the code and restart the service. Until `cfg.Person` exists the API still works
   (every account is its own person), so the order is not critical.
3. Optional `.env` key `FBL_USAGE_HOST_PROGRAMS` (default `ApplicationFrameHost`): host
   processes that `GET /usage` names by window title instead of by program.
4. Link accounts with `sql/persons.sql` (it ships with **example** names - put your own
   in; the file is a template, do not commit real names to a public repo). Changes apply
   at once, no restart.

New endpoints: `GET /persons`, `GET /usage`; `person` in `GET /users` / `GET /activity`
(`docs/api.md`, *Persons*, `GET /usage`).

## Upgrading to 2026.10.26.5 (window activity per session)

1. Run `sql/schema.sql` (idempotent). It adds `EndTime`, `SessionStart`, `ActiveSeconds`
   and the index `IX_WindowActivity_session` to `dbo.WindowActivity`.
2. Pull the code and restart the service.
3. Right after that, run `sql/migrate_2026.10.26.5.sql` once: preview with `@apply = 0`,
   then `@apply = 1`. It gives older rows a session and merges them into one row per
   window per session (browsers: one row per session). Keep `@browsers` in the script
   equal to `FBL_WINDOW_GROUP_BY_PROGRAM`. Needs SQL Server 2016+ (`STRING_SPLIT`,
   database compatibility level 130 or higher). Safe to re-run.
4. Update the agent (`client-powershell/monitor.ps1`): queue file names are now UTC.
   An older agent keeps working; it can only misorder its queue in the hour the clock
   is turned back.

Optional `.env` key: `FBL_WINDOW_GROUP_BY_PROGRAM` (default
`msedge,chrome,firefox,brave,opera`) — programs that get one row per session with the
latest title instead of one row per window title. Restart the service after a change.

The DB login needs `UPDATE` on `dbo.WindowActivity`. Grouping rules: `docs/api.md`,
*Window grouping*.

## Tuning settings (no code changes)

Agent behaviour is driven by rows in `cfg.Setting`, served by `GET /config`. Change a
value in the database and agents pick it up on their next config refresh — the
PowerShell script is never touched. `sql/settings.sql` holds ready-to-run examples:

- `window_all` — `0` = record only the active window [default], `1` = all windows.
- per-user / per-computer overrides (precedence: `user > computer > global`).
- excluding an account via `cfg.ExcludedUser` (that user's agent then idles).

```sql
-- example: read history more often, globally
UPDATE cfg.Setting SET SettingValue='60'
 WHERE Scope='global' AND Computer IS NULL AND UserAccount IS NULL AND SettingKey='history_read_sec';
```

## Read API tokens (Home Assistant)

Two tokens, which must differ: `FBL_AGENT_TOKEN` (PCs) and `FBL_READER_TOKEN` (Home
Assistant). `FBL_API_TOKEN` still works as the agent token. Generate each with
`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.

## Tests

```bash
cd server-python
python3 -m unittest discover -s tests -v     # pure logic, no DB needed
```

## Read API smoke test (curl)

Set the tokens in your shell first (`READER` and `AGENT` hold the values from `.env`):

```bash
BASE=http://localhost:8080

# accounts for the picker
curl -s "$BASE/users" -H "Authorization: Bearer $READER"

# people (accounts of several PCs linked in cfg.Person)
curl -s "$BASE/persons" -H "Authorization: Bearer $READER"

# time in front of the computer: today, a day, a month; 3 top programs
curl -s "$BASE/usage" -H "Authorization: Bearer $READER"
curl -s "$BASE/usage?date=2026-10-27&programs=3" -H "Authorization: Bearer $READER"
curl -s "$BASE/usage?from=2026-10-01&to=2026-10-31" -H "Authorization: Bearer $READER"

# expect 422 (date together with from/to, or a bad date)
curl -s -o /dev/null -w "%{http_code}\n" "$BASE/usage?date=2026-10-27&from=2026-10-01" -H "Authorization: Bearer $READER"
curl -s -o /dev/null -w "%{http_code}\n" "$BASE/usage?date=nonsense" -H "Authorization: Bearer $READER"

# newest 5 web + window items
curl -s "$BASE/activity?kind=both&limit=5" -H "Authorization: Bearer $READER"

# web only, two accounts (repeat user=)
curl -s "$BASE/activity?kind=web&user=LEGION%5Canna&user=LEGION%5Cania" -H "Authorization: Bearer $READER"

# limit above the maximum is capped (expect "limit": 200)
curl -s "$BASE/activity?limit=5000" -H "Authorization: Bearer $READER" | head -c 200; echo

# expect 422 (limit must be >= 1)
curl -s -o /dev/null -w "%{http_code}\n" "$BASE/activity?limit=0" -H "Authorization: Bearer $READER"

# expect 401: the agent token does not read activity
curl -s -o /dev/null -w "%{http_code}\n" "$BASE/activity" -H "Authorization: Bearer $AGENT"

# expect 401: the reader token does not read agent config
curl -s -o /dev/null -w "%{http_code}\n" "$BASE/config?computer=LENOVO&user=x" -H "Authorization: Bearer $READER"
```

## Smoke test

```bash
# health (no auth) — checks env + DB connectivity
curl -s http://localhost:8080/health

# config (auth)
curl -s "http://localhost:8080/config?computer=LENOVO&user=LENOVO%5C<konto>" \
     -H "Authorization: Bearer <token>"

# ingest (auth)
curl -s -X POST http://localhost:8080/ingest \
     -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -d '{"computer":"LENOVO","user":"LENOVO\\<konto>","profile":"Default",
          "visits":[{"visit_id":1,"url":"https://example.com/","title":"Ex","ts":1760000000}],
          "windows":[]}'
```

Interactive API docs are at `http://localhost:8080/docs` (FastAPI built-in).

## Notes

- `pymssql` is used instead of `pyodbc` so no system ODBC driver is required.
- Timestamps arrive as epoch seconds (UTC); the server converts to
  `FBL_LOCAL_TZ` (default `Europe/Warsaw`) to pick the calendar `Day`.
- Idempotent ingest: visits with `visit_id <= LastVisitId` are skipped, so a retried
  block never double-counts.
