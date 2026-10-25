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
```

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

# newest 5 web + window items
curl -s "$BASE/activity?kind=both&limit=5" -H "Authorization: Bearer $READER"

# web only, two accounts (repeat user=)
curl -s "$BASE/activity?kind=web&user=LEGION%5Cvvojc&user=LEGION%5Cstani" -H "Authorization: Bearer $READER"

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
