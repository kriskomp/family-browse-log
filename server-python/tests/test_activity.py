"""Unit tests for app/activity.py (standard library only).

Run from server-python/:  python3 -m unittest discover -s tests -v
"""
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.activity import (clamp_limit, clean_url, group_samples, local_iso, mark_active,
                          merge_activity, merge_gap_sec, session_gap_sec)

TZ = ZoneInfo("Europe/Warsaw")


class CleanUrl(unittest.TestCase):
    def test_query_and_fragment_removed(self):
        self.assertEqual(
            clean_url("https://login.live.com/oauth20_authorize.srf?client_id=1&code=SECRET#x"),
            "https://login.live.com/oauth20_authorize.srf")

    def test_plain_url_unchanged(self):
        self.assertEqual(clean_url("https://github.com/kriskomp"), "https://github.com/kriskomp")

    def test_about_blank_and_empty(self):
        self.assertEqual(clean_url("about:blank"), "about:blank")
        self.assertEqual(clean_url(None), "")
        self.assertEqual(clean_url(""), "")

    def test_root_path(self):
        self.assertEqual(clean_url("https://example.com/?a=1"), "https://example.com/")


class ClampLimit(unittest.TestCase):
    def test_default_when_missing(self):
        self.assertEqual(clamp_limit(None, 20, 200), 20)

    def test_capped_at_maximum(self):
        self.assertEqual(clamp_limit(5000, 20, 200), 200)

    def test_requested_value_used(self):
        self.assertEqual(clamp_limit(7, 20, 200), 7)

    def test_zero_or_negative_rejected(self):
        with self.assertRaises(ValueError):
            clamp_limit(0, 20, 200)

    def test_default_never_above_maximum(self):
        self.assertEqual(clamp_limit(None, 500, 200), 200)


class LocalIso(unittest.TestCase):
    def test_winter_offset(self):
        self.assertEqual(local_iso(datetime(2026, 1, 15, 12, 0), TZ), "2026-01-15T12:00:00+01:00")

    def test_summer_offset(self):
        self.assertEqual(local_iso(datetime(2026, 7, 15, 12, 0), TZ), "2026-07-15T12:00:00+02:00")


class MergeActivity(unittest.TestCase):
    def web(self, t, url="https://a.pl/x?y=1", title="T", visits=1, user="LEGION\\vvojc"):
        return {"time": t, "url": url, "title": title, "visits": visits,
                "user": user, "computer": "LEGION"}

    def win(self, t, program="msedge", window="W", user="LEGION\\vvojc"):
        return {"time": t, "program": program, "window": window,
                "user": user, "computer": "LEGION"}

    def test_newest_first_across_both_kinds(self):
        web = [self.web(datetime(2026, 10, 20, 10, 0)), self.web(datetime(2026, 10, 20, 8, 0))]
        win = [self.win(datetime(2026, 10, 20, 9, 0)), self.win(datetime(2026, 10, 20, 11, 0))]
        items, _ = merge_activity(web, win, TZ, limit=10)
        self.assertEqual([i["time"] for i in items], [
            "2026-10-20T11:00:00+02:00", "2026-10-20T10:00:00+02:00",
            "2026-10-20T09:00:00+02:00", "2026-10-20T08:00:00+02:00"])
        self.assertEqual([i["type"] for i in items], ["window", "web", "window", "web"])

    def test_limit_applied_to_merged_list(self):
        web = [self.web(datetime(2026, 10, 20, h, 0)) for h in (10, 8)]
        win = [self.win(datetime(2026, 10, 20, h, 0)) for h in (9, 7)]
        items, _ = merge_activity(web, win, TZ, limit=2)
        self.assertEqual([i["time"] for i in items],
                         ["2026-10-20T10:00:00+02:00", "2026-10-20T09:00:00+02:00"])

    def test_url_is_cleaned_in_web_items(self):
        items, _ = merge_activity([self.web(datetime(2026, 10, 20, 10, 0))], [], TZ, 5)
        self.assertEqual(items[0]["url"], "https://a.pl/x")
        self.assertNotIn("?", items[0]["url"])

    def test_web_item_fields(self):
        items, _ = merge_activity([self.web(datetime(2026, 10, 20, 10, 0), title=None, visits=3)],
                                  [], TZ, 5)
        self.assertEqual(items[0], {"type": "web", "time": "2026-10-20T10:00:00+02:00",
                                    "user": "LEGION\\vvojc", "computer": "LEGION",
                                    "url": "https://a.pl/x", "title": "", "visits": 3})

    def test_window_item_fields(self):
        items, _ = merge_activity([], [self.win(datetime(2026, 10, 20, 10, 0), program=None)],
                                  TZ, 5)
        self.assertEqual(items[0]["type"], "window")
        self.assertEqual(items[0]["program"], "")
        self.assertEqual(items[0]["window"], "W")

    def test_summary_last_times(self):
        web = [self.web(datetime(2026, 10, 20, 8, 0)), self.web(datetime(2026, 10, 20, 10, 0))]
        _, summary = merge_activity(web, [], TZ, 1)
        self.assertEqual(summary["last_web_at"], "2026-10-20T10:00:00+02:00")
        self.assertIsNone(summary["last_window_at"])

    def test_summary_is_independent_of_limit(self):
        # limit truncates the list, but the summary still reports the newest of each kind
        web = [self.web(datetime(2026, 10, 20, 10, 0))]
        win = [self.win(datetime(2026, 10, 20, 11, 0))]
        items, summary = merge_activity(web, win, TZ, 1)
        self.assertEqual(len(items), 1)
        self.assertEqual(summary["last_window_at"], "2026-10-20T11:00:00+02:00")
        self.assertEqual(summary["last_web_at"], "2026-10-20T10:00:00+02:00")

    def test_empty(self):
        self.assertEqual(merge_activity([], [], TZ, 5), ([], {"last_web_at": None, "last_window_at": None}))


class DstBoundary(unittest.TestCase):
    """2026-10-25 is the end of summer time in Warsaw (03:00 -> 02:00)."""
    def test_order_across_dst_change(self):
        web = [{"time": datetime(2026, 10, 25, 2, 30), "url": "https://a.pl/", "title": "before",
                "visits": 1, "user": "u", "computer": "c"}]
        win = [{"time": datetime(2026, 10, 25, 4, 0), "program": "p", "window": "after",
                "user": "u", "computer": "c"}]
        items, _ = merge_activity(web, win, TZ, 5)
        self.assertEqual(items[0]["time"], "2026-10-25T04:00:00+01:00")
        self.assertEqual(items[1]["time"], "2026-10-25T02:30:00+02:00")


class SummaryIndependentOfKind(unittest.TestCase):
    def test_kind_windows_still_reports_last_web(self):
        latest_web = [{"time": datetime(2026, 10, 20, 9, 0), "url": "https://a.pl/", "title": "t",
                       "visits": 1, "user": "u", "computer": "c"}]
        win = [{"time": datetime(2026, 10, 20, 10, 0), "program": "p", "window": "w",
                "user": "u", "computer": "c"}]
        items, summary = merge_activity([], win, TZ, 5, latest_web=latest_web)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["type"], "window")
        self.assertEqual(summary["last_web_at"], "2026-10-20T09:00:00+02:00")
        self.assertEqual(summary["last_window_at"], "2026-10-20T10:00:00+02:00")

    def test_kind_web_still_reports_last_window(self):
        web = [{"time": datetime(2026, 10, 20, 9, 0), "url": "https://a.pl/", "title": "t",
                "visits": 1, "user": "u", "computer": "c"}]
        latest_win = [{"time": datetime(2026, 10, 20, 11, 0), "program": "p", "window": "w",
                       "user": "u", "computer": "c"}]
        items, summary = merge_activity(web, [], TZ, 5, latest_window=latest_win)
        self.assertEqual([i["type"] for i in items], ["web"])
        self.assertEqual(summary["last_window_at"], "2026-10-20T11:00:00+02:00")

def T(h, m, sec=0):
    return datetime(2026, 10, 26, h, m, sec)


class WindowSessions(unittest.TestCase):
    GAP, SAMPLE = 360, 30

    def run_(self, samples, last_end=None, session=None, rows=None):
        rows = {} if rows is None else rows
        touched, resent = group_samples(last_end, session, rows, samples, self.GAP, self.SAMPLE)
        return touched, resent, rows

    def test_gaps(self):
        self.assertEqual(merge_gap_sec(30), 120)
        self.assertEqual(merge_gap_sec(60), 180)
        self.assertEqual(session_gap_sec(30, 300), 360)
        self.assertEqual(session_gap_sec(30, 0), 120)

    def test_switching_back_updates_same_row(self):
        s = [(T(20, 25, 0), "msedge", "CB"), (T(20, 25, 30), "msedge", "CB"),
             (T(20, 26, 0), "explorer", "boot"), (T(20, 26, 30), "msedge", "CB"),
             (T(20, 36, 30), "msedge", "CB")]  # back after 10 min in another window
        # samples kept coming in Notepad++ meanwhile, so the session goes on
        mid = [(T(20, 27, 0) + (T(20, 0, 30) - T(20, 0, 0)) * i, "notepad++", "n")
               for i in range(19)]
        samples = s[:4] + mid + s[4:]
        touched, resent, rows = self.run_(samples)
        self.assertEqual(resent, 0)
        self.assertEqual(len(touched), 3)               # CB, boot, notepad - no duplicates
        cb = rows[("msedge", "CB")]
        self.assertEqual(cb["start"], T(20, 25, 0))
        self.assertEqual(cb["end"], T(20, 36, 30))
        self.assertEqual(cb["secs"], 4 * 30)            # only time in front
        self.assertEqual({r["session"] for r in touched}, {T(20, 25, 0)})

    def test_long_pause_starts_new_session(self):
        samples = [(T(20, 0), "msedge", "CB"), (T(20, 7), "msedge", "CB")]  # 7 min > 6 min
        touched, _, _ = self.run_(samples)
        self.assertEqual(len(touched), 2)
        self.assertEqual([r["session"] for r in touched], [T(20, 0), T(20, 7)])

    def test_idle_pause_below_gap_keeps_session(self):
        samples = [(T(20, 0), "msedge", "film"), (T(20, 5, 30), "msedge", "film")]  # 5.5 min idle
        touched, _, _ = self.run_(samples)
        self.assertEqual(len(touched), 1)
        self.assertEqual(touched[0]["secs"], 60)

    def test_continues_session_from_db(self):
        row = {"id": 7, "start": T(20, 0), "end": T(20, 1), "secs": 90,
               "session": T(19, 50), "program": "msedge", "window": "CB"}
        rows = {("msedge", "CB"): row}
        touched, _, _ = self.run_([(T(20, 3), "msedge", "CB")], T(20, 2), T(19, 50), rows)
        self.assertEqual(touched, [row])
        self.assertEqual(row["id"], 7)
        self.assertEqual(row["end"], T(20, 3))
        self.assertEqual(row["secs"], 120)

    def test_resent_block_is_skipped(self):
        row = {"id": 7, "start": T(20, 0), "end": T(20, 1), "secs": 90,
               "session": T(20, 0), "program": "msedge", "window": "CB"}
        rows = {("msedge", "CB"): row}
        touched, resent, _ = self.run_([(T(20, 0, 30), "msedge", "CB"), (T(20, 1), "msedge", "CB")],
                                       T(20, 1), T(20, 0), rows)
        self.assertEqual((touched, resent), ([], 2))
        self.assertEqual(row["secs"], 90)

    def test_delayed_queue_keeps_original_session(self):
        # agent was offline for an hour; samples arrive late but carry their own times
        row = {"id": 7, "start": T(18, 0), "end": T(18, 10), "secs": 600,
               "session": T(18, 0), "program": "msedge", "window": "CB"}
        rows = {("msedge", "CB"): row}
        late = [(T(18, 10, 30), "msedge", "CB"), (T(18, 11), "msedge", "CB")]
        touched, _, _ = self.run_(late, T(18, 10), T(18, 0), rows)
        self.assertEqual(touched, [row])
        self.assertEqual(row["secs"], 660)

    def test_unknown_session_from_old_data_starts_new(self):
        touched, _, _ = self.run_([(T(20, 1), "msedge", "CB")], T(20, 0), None, {})
        self.assertEqual(touched[0]["id"], None)
        self.assertEqual(touched[0]["session"], T(20, 1))

    def test_new_session_does_not_reuse_old_rows(self):
        row = {"id": 7, "start": T(18, 0), "end": T(18, 10), "secs": 600,
               "session": T(18, 0), "program": "msedge", "window": "CB"}
        rows = {("msedge", "CB"): row}
        touched, _, _ = self.run_([(T(20, 0), "msedge", "CB")], T(18, 10), T(18, 0), rows)
        self.assertIsNone(touched[0]["id"])
        self.assertEqual(row["secs"], 600)

    def test_none_and_empty_equal(self):
        touched, _, _ = self.run_([(T(20, 0), None, None), (T(20, 0, 30), "", "")])
        self.assertEqual(len(touched), 1)


class ActiveFlag(unittest.TestCase):
    NOW = datetime(2026, 10, 26, 19, 0, 0)

    def row(self, end, user="u"):
        return {"computer": "c", "user": user, "time": end, "start": end, "minutes": 0,
                "program": "p", "window": "w"}

    def test_newest_recent_row_is_active(self):
        rows = [self.row(datetime(2026, 10, 26, 18, 59, 0))]
        mark_active(rows, self.NOW, lambda c, u: 120)
        self.assertTrue(rows[0]["active"])

    def test_old_row_is_not_active(self):
        rows = [self.row(datetime(2026, 10, 26, 18, 55, 0))]
        mark_active(rows, self.NOW, lambda c, u: 120)
        self.assertFalse(rows[0]["active"])

    def test_boundary_is_active(self):
        rows = [self.row(datetime(2026, 10, 26, 18, 58, 0))]
        mark_active(rows, self.NOW, lambda c, u: 120)
        self.assertTrue(rows[0]["active"])

    def test_only_newest_row_per_account(self):
        rows = [self.row(datetime(2026, 10, 26, 18, 59, 0)),
                self.row(datetime(2026, 10, 26, 18, 59, 30)),
                self.row(datetime(2026, 10, 26, 18, 59, 40), user="other")]
        mark_active(rows, self.NOW, lambda c, u: 120)
        self.assertEqual([r["active"] for r in rows], [False, True, True])

    def test_gap_per_account(self):
        rows = [self.row(datetime(2026, 10, 26, 18, 56, 0))]
        mark_active(rows, self.NOW, lambda c, u: 300)
        self.assertTrue(rows[0]["active"])

    def test_active_in_item(self):
        rows = [self.row(datetime(2026, 10, 26, 18, 59, 0))]
        mark_active(rows, self.NOW, lambda c, u: 120)
        items, _ = merge_activity([], rows, TZ, 5)
        self.assertIs(items[0]["active"], True)

    def test_item_defaults_inactive(self):
        items, _ = merge_activity([], [self.row(datetime(2026, 10, 26, 18, 0, 0))], TZ, 5)
        self.assertIs(items[0]["active"], False)


class WindowItemFields(unittest.TestCase):
    def test_from_to_minutes(self):
        win = [{"start": datetime(2026, 10, 26, 18, 45), "time": datetime(2026, 10, 26, 18, 57),
                "minutes": 12, "program": "msedge", "window": "Chess", "user": "u", "computer": "c"}]
        items, summary = merge_activity([], win, TZ, 5)
        it = items[0]
        self.assertEqual(it["from"], "2026-10-26T18:45:00+01:00")
        self.assertEqual(it["to"], "2026-10-26T18:57:00+01:00")
        self.assertEqual(it["time"], it["to"])
        self.assertEqual(it["minutes"], 12)
        self.assertEqual(summary["last_window_at"], it["to"])


if __name__ == "__main__":
    unittest.main()
