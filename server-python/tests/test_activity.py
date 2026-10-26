"""Unit tests for app/activity.py (standard library only).

Run from server-python/:  python3 -m unittest discover -s tests -v
"""
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.activity import (can_extend, clamp_limit, clean_url, local_iso, merge_activity,
                          merge_gap_sec, minutes_between)

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

class WindowGrouping(unittest.TestCase):
    G = {"start": datetime(2026, 10, 26, 18, 45, 0), "end": datetime(2026, 10, 26, 18, 45, 30),
         "program": "msedge", "window": "Chess"}

    def test_minutes_between_floors(self):
        a = datetime(2026, 10, 26, 18, 45, 0)
        self.assertEqual(minutes_between(a, datetime(2026, 10, 26, 18, 45, 59)), 0)
        self.assertEqual(minutes_between(a, datetime(2026, 10, 26, 18, 46, 0)), 1)
        self.assertEqual(minutes_between(a, datetime(2026, 10, 26, 18, 57, 30)), 12)
        self.assertEqual(minutes_between(a, datetime(2026, 10, 26, 18, 44, 0)), 0)

    def test_gap(self):
        self.assertEqual(merge_gap_sec(30), 120)
        self.assertEqual(merge_gap_sec(60), 180)

    def test_extends_same_window(self):
        t = datetime(2026, 10, 26, 18, 46, 0)
        self.assertTrue(can_extend(self.G, t, "msedge", "Chess", 120))

    def test_new_group_on_other_title_program_or_gap(self):
        t = datetime(2026, 10, 26, 18, 46, 0)
        self.assertFalse(can_extend(self.G, t, "msedge", "Other", 120))
        self.assertFalse(can_extend(self.G, t, "chrome", "Chess", 120))
        self.assertFalse(can_extend(self.G, datetime(2026, 10, 26, 18, 50, 0), "msedge", "Chess", 120))
        self.assertFalse(can_extend(None, t, "msedge", "Chess", 120))

    def test_resent_sample_inside_group_is_extend(self):
        self.assertTrue(can_extend(self.G, datetime(2026, 10, 26, 18, 45, 0), "msedge", "Chess", 120))

    def test_sample_before_group_is_new(self):
        self.assertFalse(can_extend(self.G, datetime(2026, 10, 26, 18, 44, 0), "msedge", "Chess", 120))

    def test_none_and_empty_equal(self):
        g = dict(self.G, program=None, window=None)
        self.assertTrue(can_extend(g, datetime(2026, 10, 26, 18, 45, 30), "", "", 120))


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
