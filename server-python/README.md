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

The DB login only needs: `SELECT` on `cfg.*`, and `INSERT`/`UPDATE` on
`dbo.*` + `cfg.Device`. No agent ever holds this password.

## Smoke test

```bash
# health (no auth) — checks env + DB connectivity
curl -s http://localhost:8080/health

# config (auth)
curl -s "http://localhost:8080/config?computer=LENOVO&user=LENOVO%5CDziecko" \
     -H "Authorization: Bearer <token>"

# ingest (auth)
curl -s -X POST http://localhost:8080/ingest \
     -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -d '{"computer":"LENOVO","user":"LENOVO\\Dziecko","profile":"Default",
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
