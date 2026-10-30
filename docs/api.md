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
    {"computer": "LEGION", "user": "LEGION\\anna", "person": "Anna",
     "last_seen": "2026-10-25T18:03:00+01:00", "excluded": false}
  ]
}
```

- `last_seen` — newest record of that account (web or window), local time with offset.
- `excluded` — `true` when the account is listed in `cfg.ExcludedUser` (it is still
  shown, so the picker is complete). This is the *recording* switch; hiding a person in the
  read API is `cfg.Person.Excluded` (see *Persons*).
- `person` — the person this account belongs to (see *Persons*); the login itself when
  the account is not linked.

### Persons

A person links the accounts of one human across PCs: `LEGION\anna` and `LENOVO\ania`
are one **Anna**. The link lives in the database (`cfg.Person`, edited with
`server-python/sql/persons.sql`), so every client sees the same people.

- A row says: *login `UserName` (the part after the backslash, case-insensitive) belongs to
  `Person`*. With `Computer` empty it covers that login on every PC; with a computer it
  applies to that PC only and wins over a row without one.
- An account **without a row is its own person**, named after its login. Nothing has to be
  configured to get started.
- `Excluded = 1` hides the person in `GET /persons` and `GET /usage` (parents, tests);
  `include_excluded=true` shows them. It is a presentation switch, unrelated to
  `cfg.ExcludedUser` (which stops the agent from recording).
- The person `id` is the name without diacritics, lower case, dashes (`Łukasz` ->
  `lukasz`). Parameters named `person` accept the id or the name.

### `GET /persons`

People and their accounts, for the Home Assistant config flow (pick people, not accounts).

| Param | Default | Meaning |
|---|---|---|
| `include_excluded` | `false` | also persons marked `Excluded` |

```json
{
  "persons": [
    {"id": "anna", "name": "Anna", "excluded": false, "last_seen": "2026-10-28T12:00:00+01:00",
     "accounts": [
       {"computer": "LEGION", "user": "LEGION\\anna", "last_seen": "2026-10-28T10:00:00+01:00",
        "recording_excluded": false},
       {"computer": "LENOVO", "user": "LENOVO\\ania", "last_seen": "2026-10-28T12:00:00+01:00",
        "recording_excluded": false}
     ]}
  ]
}
```

### `GET /usage`

Time in front of the computer: total, per PC, per program and per day. Window rows are
summed by `ActiveSeconds`, so it is time with the keyboard or mouse in use (the agent stops
sampling after `idle_limit_sec` without input; reading or a film without touching the PC
is not counted). A row belongs to the day of its **last activity**, so a session running
past midnight counts fully on the day it ends.

| Param | Default | Meaning |
|---|---|---|
| `date` | today | one day, `YYYY-MM-DD` |
| `from`, `to` | — | inclusive range (a month = first to last day); a missing `to` is today, a missing `from` is `to`; at most 366 days |
| `person` | all | repeat for several people (id or name) |
| `computer` | all | one PC: time on that PC only |
| `programs` | `10` | top N programs per person, the rest in `programs_other`; `0` = all, max 100 |
| `include_excluded` | `false` | also persons marked `Excluded` |

`date` together with `from`/`to` returns `422`, as do bad dates or `from` after `to`.
Day boundaries are local time (`FBL_LOCAL_TZ`).

```json
{
  "generated_at": "2026-10-28T19:40:00+01:00",
  "period": {"from": "2026-10-28", "to": "2026-10-28", "days": 1},
  "programs_limit": 10,
  "persons": [
    {"id": "anna", "name": "Anna", "excluded": false,
     "total": {"seconds": 5100, "text": "1 h 25 min"},
     "computers": [{"computer": "LEGION", "seconds": 3600, "text": "1 h"},
                   {"computer": "LENOVO", "seconds": 1500, "text": "25 min"}],
     "programs": [{"program": "msedge", "seconds": 4500, "text": "1 h 15 min"},
                  {"program": "Solitaire & Casual Games", "host": "ApplicationFrameHost",
                   "seconds": 600, "text": "10 min"}],
     "programs_other": {"count": 1, "seconds": 600, "text": "10 min"},
     "per_day": [{"date": "2026-10-28", "seconds": 5100, "text": "1 h 25 min"}],
     "active_days": 1,
     "average": {"per_active_day": {"seconds": 5100, "text": "1 h 25 min"},
                 "per_day": {"seconds": 5100, "text": "1 h 25 min"}}}
  ]
}
```

- Every duration has exact `seconds` (for calculations and automations) and a readable
  `text`: `1 h 15 min`, `2 h`, `45 min`, `< 1 min` (never `75 min`).
- A known person with no time in the range is still listed, with zeros, so a dashboard
  shows `0 min` instead of a missing value.
- **Host processes** (`FBL_USAGE_HOST_PROGRAMS`, default `ApplicationFrameHost`, which runs
  every Microsoft Store app) are listed by **window title**, i.e. the app name: `Solitaire &
  Casual Games`, `Odtwarzacz multimedialny`. Such an entry has `"host": "ApplicationFrameHost"`.
  A host row with an empty title stays under the process name. Other programs, browsers
  included, are never split by title. Nothing changes in the stored data.
- **Ignored windows** (`FBL_USAGE_IGNORE_TITLES`, default `Microsoft Family Safety`, separated by `;`):
  rows whose window title equals one of these (exact, case-insensitive) are left out of `/usage`
  entirely, so technical windows do not show up as child activity. Browser titles that merely
  contain the phrase are kept. Only `/usage` is affected; the stored data is not changed.
- Programs are merged case-insensitively. `per_day` lists every day of the range
  (zeros included). `average.per_active_day` divides by the days with any time,
  `average.per_day` by all days of the range.
- Only rows written by 2026.10.26.5 or later carry `ActiveSeconds` (older rows were
  converted by the migration script).

### `GET /activity`

Newest activity, merged into one timeline, newest first.

| Param | Default | Meaning |
|---|---|---|
| `kind` | `both` | `web`, `windows` or `both` |
| `user` | all | repeat the parameter to pick several accounts: `user=LEGION%5Canna&user=LEGION%5Cania` |
| `person` | all | repeat to pick people (id or name); adds all their accounts. A person with no accounts returns no items |
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
     "person": "Anna", "user": "LEGION\\anna", "computer": "LEGION", "program": "msedge",
     "window": "Blokada reklam - Edge"},
    {"type": "web", "time": "2026-10-25T18:01:05+01:00", "person": "Anna", "user": "LEGION\\anna",
     "computer": "LEGION", "url": "https://github.com/kriskomp", "title": "kriskomp", "visits": 2}
  ],
  "summary": {
    "last_web_at": "2026-10-25T18:01:05+01:00",
    "last_window_at": "2026-10-25T18:02:40+01:00"
  }
}
```

- Every item has `person` (see *Persons*).
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
