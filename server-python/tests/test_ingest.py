"""Tests for the visit write path (app/ingest.py, app/visits.py, the visit helpers in app/db.py).

No database and no pymssql needed: a small in-memory fake plays the SQL statements the write
path sends. It mirrors the behaviour the design relies on (a row is inserted only if it does
not exist, OUTPUT returns the inserted row, ...), so these tests check the LOGIC; the SQL text
itself is checked against a real server (see server-python/README.md, upgrade section).

Run from server-python/:  python3 -m unittest discover -s tests -v
"""
import sys
import types
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

# db.py imports pymssql at module level; the tests never connect, so a stub is enough
sys.modules.setdefault("pymssql", types.ModuleType("pymssql"))

from app import db                                                  # noqa: E402
from app.ingest import process_ingest                               # noqa: E402
from app.models import IngestRequest                                # noqa: E402
from app.visits import (ZERO_HASH, is_before_cutoff, local_epoch,  # noqa: E402
                        order_visits, url_hash)

TZ = ZoneInfo("Europe/Warsaw")
C, U, P = "LEGION", "LEGION\\vvojc", "Default"


class FakeCursor:
    """Plays the statements of the write path against dictionaries."""

    def __init__(self, st: "FakeConn") -> None:
        self.st = st
        self.rows: list = []

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def execute(self, sql, params=None):
        st = self.st
        self.rows = []
        p = params
        if "SELECT LastVisitId FROM dbo.IngestState" in sql:
            v = st.ingest_state.get(tuple(p))
            self.rows = [(v,)] if v is not None else []
        elif "SELECT VisitTs FROM dbo.BrowsingVisit" in sql:          # cutoff marker
            ts = [k[3] for k in st.visits if k[:3] == tuple(p) and k[4] == ZERO_HASH]
            self.rows = [(ts[0],)] if ts else []
        elif "SELECT TOP 1 1 FROM dbo.BrowsingVisit" in sql:
            self.rows = [(1,)] if any(k[:3] == tuple(p) for k in st.visits) else []
        elif "SELECT MAX(LastVisit) FROM dbo.BrowsingHistory" in sql and "GROUP BY" not in sql:
            lasts = [h["last"] for k, h in st.history.items() if k[:3] == tuple(p)]
            self.rows = [(max(lasts) if lasts else None,)]
        elif "SELECT CASE WHEN OBJECT_ID" in sql:
            self.rows = [(1,)]
        elif "SELECT EdgeProfile, MAX(VisitTs) FROM dbo.BrowsingVisit" in sql:
            best: dict = {}
            for k in st.visits:
                if k[:2] == tuple(p):
                    best[k[2]] = max(best.get(k[2], 0), k[3])
            self.rows = list(best.items())
        elif "SELECT EdgeProfile, MAX(LastVisit) FROM dbo.BrowsingHistory" in sql:
            best = {}
            for k, h in st.history.items():
                if k[:2] == tuple(p):
                    best[k[2]] = max(best.get(k[2]) or h["last"], h["last"])
            self.rows = list(best.items())
        elif "INSERT INTO dbo.BrowsingVisit" in sql:
            key = (p["c"], p["u"], p["p"], p["ts"], p["h"])
            if key not in st.visits:
                st.visits.add(key)
                self.rows = [(p["ts"],)]
        elif "DELETE TOP (500) FROM dbo.BrowsingVisit" in sql:
            for key in [k for k in st.visits
                        if k[:3] == (p["c"], p["u"], p["p"]) and k[3] < p["before"]
                        and k[4] != p["zero"]]:
                st.visits.discard(key)
        elif "UPDATE dbo.BrowsingHistory" in sql:
            key = (p["c"], p["u"], p["p"], p["day"], p["url"])
            h = st.history.get(key)
            if h is None:
                st.history[key] = {"count": p["cnt"], "first": p["first"], "last": p["last"]}
            else:
                h["count"] += p["cnt"]
                h["first"] = min(h["first"], p["first"])
                h["last"] = max(h["last"], p["last"])
        elif "UPDATE dbo.IngestState" in sql:
            st.ingest_state[(p["c"], p["u"], p["p"])] = p["v"]
        elif "UPDATE dbo.AgentStatus" in sql:
            st.status[(p["c"], p["u"], p["p"])] = dict(p)
        elif "UPDATE cfg.AgentCommand" in sql:
            st.acked.append(p["id"])
        elif "UPDATE cfg.Device" in sql:
            pass
        else:  # pragma: no cover - a statement the fake does not know is a test bug
            raise AssertionError("unexpected SQL: " + sql[:80])


class FakeConn:
    def __init__(self) -> None:
        self.visits: set = set()          # (computer, user, profile, VisitTs, UrlHash hex)
        self.history: dict = {}           # (c, u, p, day, url) -> {count, first, last}
        self.ingest_state: dict = {}
        self.status: dict = {}
        self.acked: list = []

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def count(self) -> int:
        return sum(h["count"] for h in self.history.values())


def visit(vid, url, ts):
    return {"visit_id": vid, "url": url, "title": "t", "ts": ts}


def request(visits, **extra):
    return IngestRequest(computer=C, user=U, profile=P, visits=visits, **extra)


def at(y, mo, d, h=12, mi=0, s=0):
    return local_epoch(datetime(y, mo, d, h, mi, s), TZ)


class VisitHelpers(unittest.TestCase):
    def test_url_hash_is_stable_and_hex(self):
        h = url_hash("https://example.com/")
        self.assertEqual(h, url_hash("https://example.com/"))
        self.assertEqual(len(h), 64)
        self.assertNotEqual(h, url_hash("https://example.com/a"))
        self.assertNotEqual(h, ZERO_HASH)

    def test_url_hash_survives_lone_surrogate(self):
        url_hash("https://example.com/\ud800")          # must not raise

    def test_local_epoch_round_trip_and_dst(self):
        self.assertEqual(local_epoch(datetime(2026, 1, 15, 12, 0, 0), TZ), 1768474800)   # CET, UTC+1
        self.assertEqual(local_epoch(datetime(2026, 7, 15, 12, 0, 0), TZ), 1784109600)   # CEST, UTC+2
        # the repeated hour when DST ends (26.10.2026 02:30) resolves to its first occurrence
        self.assertEqual(local_epoch(datetime(2026, 10, 25, 2, 30, 0), TZ),
                         local_epoch(datetime(2026, 10, 25, 2, 30, 0), TZ))

    def test_order_visits_oldest_first_id_breaks_ties(self):
        vs = [types.SimpleNamespace(ts=5, visit_id=9), types.SimpleNamespace(ts=3, visit_id=7),
              types.SimpleNamespace(ts=5, visit_id=2)]
        self.assertEqual([(v.ts, v.visit_id) for v in order_visits(vs)], [(3, 7), (5, 2), (5, 9)])

    def test_cutoff(self):
        self.assertTrue(is_before_cutoff(100, 100))
        self.assertTrue(is_before_cutoff(99, 100))
        self.assertFalse(is_before_cutoff(101, 100))
        self.assertFalse(is_before_cutoff(1, None))


class Ingest(unittest.TestCase):
    def setUp(self):
        self.db = FakeConn()

    def send(self, visits, **extra):
        return process_ingest(self.db, request(visits, **extra))

    def test_new_visits_are_counted_and_aggregated(self):
        t = at(2026, 11, 6)
        r = self.send([visit(1, "https://a/", t), visit(2, "https://a/", t + 5),
                       visit(3, "https://b/", t + 9)])
        self.assertEqual((r.accepted_visits, r.skipped_visits), (3, 0))
        self.assertEqual(self.db.count(), 3)
        self.assertEqual(len(self.db.history), 2)             # two URLs on one day

    def test_a_resent_block_is_not_counted_twice(self):
        t = at(2026, 11, 6)
        block = [visit(1, "https://a/", t), visit(2, "https://b/", t + 5)]
        self.send(block)
        r = self.send(block)
        self.assertEqual((r.accepted_visits, r.skipped_visits), (0, 2))
        self.assertEqual(self.db.count(), 2)

    def test_read_overlap_counts_only_what_is_new(self):
        t = at(2026, 11, 6)
        self.send([visit(1, "https://a/", t), visit(2, "https://b/", t + 60)])
        # the agent re-reads 15 minutes back: two known visits and one new
        r = self.send([visit(1, "https://a/", t), visit(2, "https://b/", t + 60),
                       visit(3, "https://c/", t + 120)])
        self.assertEqual((r.accepted_visits, r.skipped_visits), (1, 2))
        self.assertEqual(self.db.count(), 3)

    def test_same_page_visited_twice_at_different_times_counts_twice(self):
        t = at(2026, 11, 6)
        r = self.send([visit(1, "https://a/", t), visit(2, "https://a/", t + 30)])
        self.assertEqual(r.accepted_visits, 2)
        self.assertEqual(next(iter(self.db.history.values()))["count"], 2)

    def test_edge_restarting_its_ids_does_not_block_the_profile(self):
        """The 13-day outage: the stored resume id (207051) is higher than every new id."""
        t = at(2026, 11, 6)
        self.db.ingest_state[(C, U, P)] = 207051
        r = self.send([visit(5, "https://a/", t), visit(6, "https://b/", t + 10)])
        self.assertEqual((r.accepted_visits, r.skipped_visits), (2, 0))
        self.assertEqual(self.db.count(), 2)
        # the legacy resume id never goes down (older agents still read it)
        self.assertEqual(r.last_visit_id, 207051)
        self.assertEqual(self.db.ingest_state[(C, U, P)], 207051)

    def test_legacy_resume_id_still_advances_for_older_agents(self):
        t = at(2026, 11, 6)
        r = self.send([visit(10, "https://a/", t), visit(12, "https://b/", t + 1)])
        self.assertEqual(r.last_visit_id, 12)
        self.assertEqual(self.db.ingest_state[(C, U, P)], 12)

    def test_visits_counted_by_the_old_server_are_not_counted_again(self):
        """Data from before dbo.BrowsingVisit existed: up to its newest visit it counts as ingested."""
        newest = datetime(2026, 10, 24, 15, 55, 42)
        self.db.history[(C, U, P, newest.date(), "https://old/")] = {
            "count": 7, "first": newest, "last": newest}
        cut = local_epoch(newest, TZ)
        r = self.send([visit(1, "https://old/", cut - 300),      # counted before
                       visit(2, "https://old/", cut),            # counted before (same second)
                       visit(3, "https://new/", cut + 60)])      # new
        self.assertEqual((r.accepted_visits, r.skipped_visits), (1, 2))
        self.assertEqual(self.db.history[(C, U, P, newest.date(), "https://old/")]["count"], 7)
        # the cutoff is remembered as a marker row, so it still applies on the next request
        self.assertIn((C, U, P, cut, ZERO_HASH), self.db.visits)
        r = self.send([visit(1, "https://old/", cut - 100), visit(4, "https://new/", cut + 120)])
        self.assertEqual((r.accepted_visits, r.skipped_visits), (1, 1))

    def test_a_profile_without_older_data_has_no_cutoff(self):
        r = self.send([visit(1, "https://a/", at(2026, 11, 6))])
        self.assertEqual(r.accepted_visits, 1)
        self.assertFalse([k for k in self.db.visits if k[4] == ZERO_HASH])

    def test_old_rows_are_purged_but_the_cutoff_marker_stays(self):
        old = at(2025, 1, 1)
        self.db.visits.add((C, U, P, old, url_hash("https://x/")))
        self.db.visits.add((C, U, P, 1000, ZERO_HASH))
        self.send([visit(1, "https://a/", at(2026, 11, 6))])
        self.assertNotIn((C, U, P, old, url_hash("https://x/")), self.db.visits)
        self.assertIn((C, U, P, 1000, ZERO_HASH), self.db.visits)

    def test_agent_diagnostics_are_stored_per_profile(self):
        self.send([], agent_version="2026.11.06.8", diag=[
            {"profile": "Default", "history_mtime": at(2026, 11, 6, 18, 39),
             "max_visit_id": 99, "max_visit_ts": at(2026, 11, 6, 18, 36),
             "rows_read": 4, "error": None},
            {"profile": "Profile 1", "error": "copy failed"}])
        d = self.db.status[(C, U, "Default")]
        self.assertEqual((d["ver"], d["mid"], d["rows"]), ("2026.11.06.8", 99, 4))
        self.assertEqual(d["hm"], datetime(2026, 11, 6, 18, 39))
        self.assertEqual(d["vt"], datetime(2026, 11, 6, 18, 36))
        self.assertEqual(self.db.status[(C, U, "Profile 1")]["err"], "copy failed")

    def test_executed_commands_are_acknowledged(self):
        self.send([], acked=[4, 5])
        self.assertEqual(self.db.acked, [4, 5])

    def test_an_older_agent_without_the_new_fields_still_works(self):
        r = self.send([visit(1, "https://a/", at(2026, 11, 6))])
        self.assertEqual(r.accepted_visits, 1)
        self.assertEqual(self.db.status, {})


class ResumePoint(unittest.TestCase):
    """GET /config: `since` is where the agent resumes reading history."""

    def test_since_comes_from_visit_rows_else_from_the_old_history(self):
        st = FakeConn()
        newest = datetime(2026, 10, 24, 15, 55, 42)
        st.history[(C, U, "Default", newest.date(), "https://old/")] = {
            "count": 1, "first": newest, "last": newest}
        st.history[(C, U, "Profile 1", newest.date(), "https://old/")] = {
            "count": 1, "first": newest, "last": newest}
        st.visits.add((C, U, "Profile 1", 2000000000, url_hash("https://n/")))
        since = db.get_visit_since(st.cursor(), C, U, TZ)
        self.assertEqual(since["Default"], local_epoch(newest, TZ))      # fallback: old data
        self.assertEqual(since["Profile 1"], 2000000000)                  # per-visit rows win

    def test_since_is_empty_for_an_unknown_account(self):
        self.assertEqual(db.get_visit_since(FakeConn().cursor(), C, U, TZ), {})


if __name__ == "__main__":
    unittest.main()
