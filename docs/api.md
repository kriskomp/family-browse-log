# API contract — family-browse-log

This file is the **single source of truth** for the HTTP contract between the
`client-powershell/` agent and the `server-python/` service. Both sides must follow it.

- Base URL (example): `http://netguard.local:8080`
- All requests require header: `Authorization: Bearer <token>`
- All request/response bodies are JSON (UTF-8).
- Timestamps on the wire are **Unix epoch seconds in UTC** (integers). The server
  converts to `Europe/Warsaw` when deriving the calendar `Day` for aggregation.
- JSON field names are short and lowercase; they map to the MS SQL columns noted below.

---

## `GET /config`

Called by the agent on start and periodically afterwards.

**Query parameters**

| Param | Example | Maps to |
|---|---|---|
| `computer` | `LENOVO` | `Computer` |
| `user` | `LENOVO\<konto>` | `UserAccount` |

**Response `200`**

```json
{
  "enabled": true,
  "settings": {
    "push_interval_sec": 30,
    "window_sample_sec": 30,
    "history_read_sec": 120,
    "block_size": 500,
    "max_backfill": 3000,
    "idle_limit_sec": 300,
    "window_all": 0
  },
  "last_visit_id": {
    "Default": 259750,
    "Profile 1": 0
  }
}
```

- `enabled` — `false` when this user (optionally on this computer) is listed in
  `cfg.ExcludedUser`. The agent then idles and sends nothing. When `false`, the server
  also returns empty `settings` and `last_visit_id`.
- `settings` — resolved with scope precedence **user > computer > global**
  (from `cfg.Setting`). All values are integers. Keys:
  - `push_interval_sec` — how often the agent flushes queued blocks.
  - `window_sample_sec` — how often the active window is sampled.
  - `history_read_sec` — how often Edge history is read.
  - `block_size` — max visits per `POST /ingest` block.
  - `max_backfill` — on first run, how many newest visits to backfill (`0` = all).
  - `idle_limit_sec` — no window sampling after this many idle seconds.
  - `window_all` — `0` = record only the active (foreground) window [default],
    `1` = record every open window (the old v1 behaviour).
- If the agent cannot reach `GET /config`, it keeps its last known settings (or the
  built-in defaults) and buffers data offline until the server is reachable again.
- `last_visit_id` — map `EdgeProfile -> LastVisitId` (from `dbo.IngestState`).
  A profile the server has never seen returns `0` (or is simply absent); the agent
  treats a missing profile as `0`.

**Errors**: `401` invalid/missing token.

---

## `POST /ingest`

Called by the agent to deliver one block of data (up to `block_size` visits, plus
any window-activity rows gathered since the last push). One profile per request.

**Request body**

```json
{
  "computer": "LENOVO",
  "user": "LENOVO\\<konto>",
  "profile": "Default",
  "visits": [
    { "visit_id": 259751, "url": "https://example.com/", "title": "Example", "ts": 1760000000 }
  ],
  "windows": [
    { "ts": 1760000000, "session_min": 12, "program": "msedge", "window": "Example - Edge" }
  ]
}
```

| JSON | Maps to | Notes |
|---|---|---|
| `computer` | `Computer` | |
| `user` | `UserAccount` | |
| `profile` | `EdgeProfile` | |
| `visits[].visit_id` | drives `IngestState.LastVisitId` | Edge `visits.id`, strictly increasing |
| `visits[].url` | `BrowsingHistory.Url` | truncated to 2048 |
| `visits[].title` | `BrowsingHistory.Title` | truncated to 1024 |
| `visits[].ts` | → `Day`, `FirstVisit`, `LastVisit` | epoch seconds UTC |
| `windows[].ts` | `WindowActivity.EventTime` / `EndTime` | epoch seconds UTC; samples are grouped per window and session, see *Window grouping* |
| `windows[].session_min` | — | ignored since 2026.10.26.5; the server computes `SessionMinutes` itself. The agent may send `0`. |
| `windows[].program` | `WindowActivity.Program` | |
| `windows[].window` | `WindowActivity.WindowTitle` | truncated to 2048 |

Both `visits` and `windows` may be empty arrays (e.g. a pure heartbeat, or a push
that only carries window activity).

**Response `200` (ACK)**

```json
{
  "ok": true,
  "last_visit_id": 259751,
  "accepted_visits": 1,
  "skipped_visits": 0,
  "accepted_windows": 1
}
```

- `last_visit_id` — the new resume point for this `(computer, user, profile)`.
  The agent stores this **only after receiving the ACK**, then continues with the
  next block. This makes a last-moment logout safe: the next session resumes from
  the last acknowledged block.
- **Idempotency**: the server ignores any visit whose `visit_id <= LastVisitId`
  currently stored, so re-sending a block that was already applied (after an
  uncertain ACK) never double-counts. Reported as `skipped_visits`.
- If the user is excluded, the server accepts the request but writes nothing and
  returns the unchanged `last_visit_id`.

**Errors**: `401` invalid/missing token · `422` malformed body.

---

## Server-side write behaviour

For a single `/ingest` call, inside one DB transaction:

1. Drop incoming visits with `visit_id <= LastVisitId`.
2. Aggregate the rest by `(Day, Url)`: `VisitCount`, earliest `FirstVisit`,
   latest `LastVisit` (and its `Title`). Upsert into `dbo.BrowsingHistory`.
3. Fold `windows` samples into `dbo.WindowActivity`, one row per window per session
   (see *Window grouping* below).
4. Advance `dbo.IngestState.LastVisitId` to the max seen (if greater).
5. Update `cfg.Device.LastSeen` (heartbeat) for this computer.

### Window grouping

The agent sends one sample every `window_sample_sec` (30 s), each with its own time
(also when it waited in the offline queue). The server keeps **one row per window per
session** of the account. A window is:

- **a browser** (`FBL_WINDOW_GROUP_BY_PROGRAM`, default `msedge,chrome,firefox,brave,opera`,
  case-insensitive): the **program alone**. Its title changes with every tab, new page or
  tab count ("… i jeszcze 4 strony"), so it would split constantly; the row keeps the
  **latest** title. Which pages were visited is in `dbo.BrowsingHistory`.
- **any other program**: `Program` + `WindowTitle` (so different games, documents or
  Store apps hosted by one process, e.g. `ApplicationFrameHost`, stay apart).

| Column | Meaning |
|---|---|
| `SessionStart` | first sample of the session; all rows of one session share it |
| `EventTime` | first time the window was in front in that session (**from**) |
| `EndTime` | last time it was in front (**to**) |
| `WindowTitle` | for browsers the latest title, otherwise the window title |
| `ActiveSeconds` | total time in front: `window_sample_sec` per sample |
| `SessionMinutes` | `ActiveSeconds / 60` — grows while the window is used |

- **Switching windows or browser tabs never adds duplicate rows**: coming back to a window in the same
  session updates its row (`EndTime`, `ActiveSeconds`). Two windows used alternately for
  10 minutes get about 5 minutes each, while both show `from`/`to` spanning the 10 minutes.
- **A session ends** when the account sends no sample of any window for longer than
  `idle_limit_sec + 2 * window_sample_sec` (default 360 s): PC off, asleep, logged off.
  Reading or watching without touching mouse/keyboard (the agent pauses sampling after
  `idle_limit_sec`) does not split the session; the paused time is not counted.
- **Gaps are measured between sample times, not arrival times.** A connection outage,
  a server restart or a backlog sent later never starts a new session.
- **Re-sent blocks** (ACK lost): samples not newer than the account's newest `EndTime` are
  skipped and not counted in `accepted_windows`. This relies on the agent sending its queue
  oldest first, which it does (queue file names are UTC timestamps, so even turning the clock
  back at the end of DST cannot reorder them).
- Samples of one request are processed oldest first, in the same transaction as the rest
  of `/ingest`.

Data written before 2026.10.26.5 (no `SessionStart`) is merged once by
`server-python/sql/migrate_2026.10.26.5.sql`.

---|---|
| `EventTime` | first sample of the group (**from**) |
| `EndTime` | last sample of the group (**to**) |
| `SessionMinutes` | whole minutes between them, `floor((EndTime - EventTime) / 60 s)`; grows while the window stays in front |

A sample **extends** the newest row of that `(Computer, UserAccount)` when all hold:

- same `Program` and same `WindowTitle` (another tab or title starts a new row),
- the pause since `EndTime` is at most `max(120, 3 * window_sample_sec)` seconds
  (a real break such as idle time, logoff or sleep starts a new row; a few missed
  samples do not),
- the sample is not older than `EventTime`.

Otherwise a new row is inserted. A sample that lies inside an existing group (the
agent re-sent a block whose ACK was lost) changes nothing, so retries never inflate
`SessionMinutes`. Samples of one request are processed oldest first, inside the same
transaction as the rest of `/ingest`.

A group in progress is updated on every push, so `EndTime` and `SessionMinutes` rise
live (one sample shows `0` minutes until the next one arrives).

Data written before 2026.10.26.5 (one row per sample, `EndTime IS NULL`) is merged once
by `server-python/sql/migrate_2026.10.26.5.sql`. Rows not migrated still display
correctly (`from = to`).

---

## Read API (Home Assistant)

Read-only endpoints for Home Assistant. They use a **separate reader token**
(`FBL_READER_TOKEN`), which cannot write or read agent configuration, and the agent
token cannot read these endpoints. Both return `401` for a wrong or missing token.

### `GET /users`

Accounts that have any data. Home Assistant uses it to fill the account picker
after the user enters the server address and reader token.

```json
{
  "users": [
    {"computer": "LEGION", "user": "LEGION\\vvojc", "last_seen": "2026-10-25T18:03:00+01:00", "excluded": false}
  ]
}
```

- `last_seen` — newest record of that account (web or window), local time with offset.
- `excluded` — `true` when the account is listed in `cfg.ExcludedUser` (it is still
  shown, so the picker is complete).

### `GET /activity`

Newest activity, merged into one timeline, newest first.

| Param | Default | Meaning |
|---|---|---|
| `kind` | `both` | `web`, `windows` or `both` |
| `user` | all | repeat the parameter to pick several accounts: `user=LEGION%5Cvvojc&user=LEGION%5Cstani` |
| `computer` | all | a single PC |
| `limit` | `FBL_ACTIVITY_DEFAULT_LIMIT` (20) | items to return; capped at `FBL_ACTIVITY_MAX_LIMIT` (200) |

Response:

```json
{
  "generated_at": "2026-10-25T18:03:12+01:00",
  "kind": "both",
  "limit": 5,
  "items": [
    {"type": "window", "time": "2026-10-25T18:02:40+01:00",
     "from": "2026-10-25T17:49:10+01:00", "to": "2026-10-25T18:02:40+01:00", "minutes": 13, "active": true,
     "user": "LEGION\\vvojc", "computer": "LEGION", "program": "msedge",
     "window": "Blokada reklam - Edge"},
    {"type": "web", "time": "2026-10-25T18:01:05+01:00", "user": "LEGION\\vvojc",
     "computer": "LEGION", "url": "https://github.com/kriskomp", "title": "kriskomp", "visits": 2}
  ],
  "summary": {
    "last_web_at": "2026-10-25T18:01:05+01:00",
    "last_window_at": "2026-10-25T18:02:40+01:00"
  }
}
```

- **URLs never include the query string or fragment** (`scheme://host/path` only).
  Query strings often hold login codes and session tokens.
- **Window items are rows of *Window grouping***: `from` = first time in front in that session,
  `to` = last time in front, `minutes` = `SessionMinutes` (total time in front, not `to - from`).
  `time` equals `to` (kept for compatibility) and is what the list is sorted by.
- `active` (window items only): `true` while the window is in front now, i.e. it is the
  newest row of that account and its `to` is not older than `max(120, 3 * window_sample_sec)` s
  (per account). Nothing is stored: when the PC is turned off no more samples arrive and the
  row simply stops being active; its `to` is the closing time (accurate to one sample).
- `time`, `from`, `to` are local time with its offset (the database stores local time; the offset
  is +01:00 in winter and +02:00 in summer).
- `summary` always holds the newest record of each kind, even when `limit` cuts the list
  or `kind` leaves one kind out. `null` means that kind has no data at all.
- A `limit` above the maximum is capped, and the response reports the value applied.
- `limit < 1` returns `422`.

**Errors**: `401` invalid/missing reader token · `422` bad parameters.
