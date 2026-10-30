"""Unit tests for app/activity.py (standard library only).

Run from server-python/:  python3 -m unittest discover -s tests -v
"""
import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.config import parse_program_list
from app.activity import (build_persons, build_usage, format_duration, resolve_person,
                          resolve_range, slugify, user_part,
                          clamp_limit, clean_url, group_key, group_samples, local_iso, mark_active,
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
    def web(self, t, url="https://a.pl/x?y=1", title="T", visits=1, user="LEGION\\anna"):
        return {"time": t, "url": url, "title": title, "visits": visits,
                "user": user, "computer": "LEGION"}

    def win(self, t, program="msedge", window="W", user="LEGION\\anna"):
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
                                    "user": "LEGION\\anna", "computer": "LEGION",
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


class BrowserGrouping(unittest.TestCase):
    BR = frozenset({"msedge", "chrome"})

    def test_program_list(self):
        self.assertEqual(parse_program_list(" msedge, Chrome,,firefox "),
                         frozenset({"msedge", "chrome", "firefox"}))
        self.assertEqual(parse_program_list(""), frozenset())

    def test_key(self):
        self.assertEqual(group_key("msedge", "A", self.BR), ("msedge", None))
        self.assertEqual(group_key("MSEdge", "B", self.BR), ("msedge", None))
        self.assertEqual(group_key("notepad++", "n", self.BR), ("notepad++", "n"))
        self.assertEqual(group_key(None, "", self.BR), (None, None))

    def test_browser_tabs_one_row_latest_title(self):
        samples = [(T(21, 0), "msedge", "CloudBeaver i jeszcze 4 strony"),
                   (T(21, 0, 30), "msedge", "Claude i jeszcze 4 strony"),
                   (T(21, 1), "notepad++", "n"),
                   (T(21, 1, 30), "msedge", "Proxmox i jeszcze 5 stron")]
        rows = {}
        touched, _ = group_samples(None, None, rows, samples, 360, 30, self.BR)
        self.assertEqual(len(touched), 2)
        edge = rows[("msedge", None)]
        self.assertEqual(edge["window"], "Proxmox i jeszcze 5 stron")
        self.assertEqual(edge["secs"], 90)
        self.assertEqual((edge["start"], edge["end"]), (T(21, 0), T(21, 1, 30)))

    def test_other_programs_still_split_by_title(self):
        samples = [(T(21, 0), "javaw", "Minecraft"), (T(21, 0, 30), "javaw", "Other game")]
        touched, _ = group_samples(None, None, {}, samples, 360, 30, self.BR)
        self.assertEqual(len(touched), 2)

    def test_without_list_browser_splits(self):
        samples = [(T(21, 0), "msedge", "A"), (T(21, 0, 30), "msedge", "B")]
        touched, _ = group_samples(None, None, {}, samples, 360, 30)
        self.assertEqual(len(touched), 2)


PEOPLE = [
    {"person": "Anna", "computer": None, "username": "anna", "excluded": False},
    {"person": "Anna", "computer": None, "username": "ania", "excluded": False},
    {"person": "Rodzic", "computer": None, "username": "parent", "excluded": True},
    {"person": "Rodzic", "computer": None, "username": "p2", "excluded": True},
    {"person": "Ola", "computer": "PC2", "username": "x", "excluded": False},
    {"person": "Ala", "computer": None, "username": "x", "excluded": False},
]


class Persons(unittest.TestCase):
    def test_slug_and_user_part(self):
        self.assertEqual(slugify("\u0141ukasz"), "lukasz")
        self.assertEqual(slugify("\u0141ukasz \u017b\u00f3\u0142w"), "lukasz-zolw")
        self.assertEqual(slugify("   "), "unknown")
        self.assertEqual(user_part("LEGION\\anna"), "anna")
        self.assertEqual(user_part("anna"), "anna")

    def test_links_logins_across_pcs_and_case(self):
        a = resolve_person("LEGION", "LEGION\\Anna", PEOPLE)
        b = resolve_person("LENOVO", "LENOVO\\ania", PEOPLE)
        self.assertEqual((a["id"], a["name"]), ("anna", "Anna"))
        self.assertEqual(b["id"], "anna")

    def test_computer_specific_row_wins(self):
        self.assertEqual(resolve_person("PC2", "PC2\\x", PEOPLE)["name"], "Ola")
        self.assertEqual(resolve_person("PC1", "PC1\\x", PEOPLE)["name"], "Ala")

    def test_unmapped_is_its_own_person(self):
        who = resolve_person("PC1", "PC1\\Nowy", PEOPLE)
        self.assertEqual((who["id"], who["name"], who["excluded"]), ("nowy", "Nowy", False))

    def test_excluded_when_any_row_is(self):
        self.assertTrue(resolve_person("PC1", "PC1\\parent", PEOPLE)["excluded"])
        self.assertTrue(resolve_person("PC1", "PC1\\p2", PEOPLE)["excluded"])
        self.assertEqual(resolve_person("PC1", "PC1\\p2", PEOPLE)["id"], "rodzic")

    def test_build_persons(self):
        accounts = [("LEGION", "LEGION\\anna", datetime(2026, 10, 28, 10, 0)),
                    ("LENOVO", "LENOVO\\ania", datetime(2026, 10, 28, 12, 0)),
                    ("LENOVO", "LENOVO\\parent", None)]
        out = build_persons(accounts, PEOPLE, lambda u, c: u == "LEGION\\anna", TZ)
        self.assertEqual([p["id"] for p in out], ["anna"])          # parent hidden
        anna = out[0]
        self.assertEqual(len(anna["accounts"]), 2)
        self.assertEqual(anna["last_seen"], "2026-10-28T12:00:00+01:00")
        self.assertEqual([a["recording_excluded"] for a in anna["accounts"]], [True, False])
        shown = build_persons(accounts, PEOPLE, lambda u, c: False, TZ, include_excluded=True)
        self.assertEqual([p["id"] for p in shown], ["anna", "rodzic"])

    def test_person_in_activity_items(self):
        win = [{"start": datetime(2026, 10, 28, 9), "time": datetime(2026, 10, 28, 10),
                "minutes": 1, "program": "p", "window": "w", "user": "LENOVO\\ania",
                "computer": "LENOVO"}]
        items, _ = merge_activity([], win, TZ, 5, person_of=lambda c, u: resolve_person(c, u, PEOPLE))
        self.assertEqual(items[0]["person"], "Anna")


class Durations(unittest.TestCase):
    def test_format(self):
        for sec, text in [(0, "0 min"), (30, "< 1 min"), (60, "1 min"), (2700, "45 min"),
                          (4500, "1 h 15 min"), (7200, "2 h"), (93600, "26 h")]:
            self.assertEqual(format_duration(sec), text, sec)

    def test_range(self):
        today = date(2026, 10, 28)
        self.assertEqual(resolve_range(None, None, None, today), (today, today))
        self.assertEqual(resolve_range("2026-10-01", None, None, today),
                         (date(2026, 10, 1), date(2026, 10, 1)))
        self.assertEqual(resolve_range(None, "2026-10-01", "2026-10-07", today),
                         (date(2026, 10, 1), date(2026, 10, 7)))
        self.assertEqual(resolve_range(None, "2026-10-20", None, today),
                         (date(2026, 10, 20), today))

    def test_range_errors(self):
        today = date(2026, 10, 28)
        for args in [("2026-10-01", "2026-10-02", None), (None, "2026-10-09", "2026-10-01"),
                     ("nonsense", None, None), (None, "2025-01-01", "2026-10-28")]:
            with self.assertRaises(ValueError):
                resolve_range(*args, today)


D1, D2 = date(2026, 10, 27), date(2026, 10, 28)


def U(comp, user, prog, day, sec):
    return {"computer": comp, "user": user, "program": prog, "day": day, "seconds": sec}


class Usage(unittest.TestCase):
    ACC = [("LEGION", "LEGION\\anna", None), ("LENOVO", "LENOVO\\ania", None),
           ("LENOVO", "LENOVO\\parent", None), ("LENOVO", "LENOVO\\idle", None)]
    ROWS = [U("LEGION", "LEGION\\anna", "msedge", D2, 3600),
            U("LENOVO", "LENOVO\\ania", "MSEdge", D2, 900),
            U("LENOVO", "LENOVO\\ania", "javaw", D2, 600),
            U("LENOVO", "LENOVO\\ania", "notepad++", D1, 300),
            U("LENOVO", "LENOVO\\parent", "msedge", D2, 99999)]

    def usage(self, **kw):
        kw.setdefault("programs_limit", 10)
        return build_usage(self.ROWS, self.ACC, PEOPLE, D2, D2, **kw)

    def by_id(self, out):
        return {p["id"]: p for p in out}

    def test_one_person_over_two_pcs(self):
        anna = self.by_id(self.usage())["anna"]
        self.assertEqual(anna["total"], {"seconds": 5100, "text": "1 h 25 min"})
        self.assertEqual([(c["computer"], c["seconds"]) for c in anna["computers"]],
                         [("LEGION", 3600), ("LENOVO", 1500)])
        self.assertEqual(anna["programs"][0]["program"], "msedge")      # case-insensitive merge
        self.assertEqual(anna["programs"][0]["seconds"], 4500)
        self.assertEqual(anna["per_day"], [{"date": "2026-10-28", "seconds": 5100,
                                            "text": "1 h 25 min"}])

    def test_excluded_hidden_and_shown(self):
        self.assertNotIn("rodzic", self.by_id(self.usage()))
        shown = self.by_id(self.usage(include_excluded=True))
        self.assertEqual(shown["rodzic"]["total"]["seconds"], 99999)

    def test_person_without_time_has_zeros(self):
        idle = self.by_id(self.usage())["idle"]
        self.assertEqual(idle["total"], {"seconds": 0, "text": "0 min"})
        self.assertEqual(idle["programs"], [])
        self.assertIsNone(idle["programs_other"])

    def test_top_n_and_other(self):
        anna = self.by_id(self.usage(programs_limit=1))["anna"]
        self.assertEqual([p["program"] for p in anna["programs"]], ["msedge"])
        self.assertEqual(anna["programs_other"], {"count": 1, "seconds": 600, "text": "10 min"})
        all_ = self.by_id(self.usage(programs_limit=0))["anna"]
        self.assertEqual(len(all_["programs"]), 2)
        self.assertIsNone(all_["programs_other"])

    def test_range_days_and_averages(self):
        out = self.by_id(build_usage(self.ROWS, self.ACC, PEOPLE, D1, D2))
        anna = out["anna"]
        self.assertEqual([d["date"] for d in anna["per_day"]], ["2026-10-27", "2026-10-28"])
        self.assertEqual(anna["per_day"][0]["seconds"], 300)
        self.assertEqual(anna["active_days"], 2)
        self.assertEqual(anna["total"]["seconds"], 5400)
        self.assertEqual(anna["average"]["per_active_day"]["seconds"], 2700)
        self.assertEqual(anna["average"]["per_day"]["seconds"], 2700)
        idle = out["idle"]
        self.assertEqual(idle["active_days"], 0)
        self.assertEqual(idle["average"]["per_active_day"]["seconds"], 0)

    def test_average_over_calendar_days(self):
        d3 = date(2026, 10, 29)
        out = self.by_id(build_usage(self.ROWS, self.ACC, PEOPLE, D2, d3))
        self.assertEqual(out["anna"]["active_days"], 1)
        self.assertEqual(out["anna"]["average"]["per_active_day"]["seconds"], 5100)
        self.assertEqual(out["anna"]["average"]["per_day"]["seconds"], 2550)

    def test_filters(self):
        only = self.usage(person_ids={"anna"})
        self.assertEqual([p["id"] for p in only], ["anna"])
        on_pc = self.by_id(self.usage(computer="LENOVO"))["anna"]
        self.assertEqual(on_pc["total"]["seconds"], 1500)
        self.assertEqual([c["computer"] for c in on_pc["computers"]], ["LENOVO"])


class UsageHostPrograms(unittest.TestCase):
    ACC = [("PC", "PC\\anna", None)]
    HOSTS = frozenset({"applicationframehost"})

    def rows(self):
        return [U("PC", "PC\\anna", "ApplicationFrameHost", D2, 600) | {"window": "Pasjans"},
                U("PC", "PC\\anna", "applicationframehost", D2, 300) | {"window": "Pasjans"},
                U("PC", "PC\\anna", "ApplicationFrameHost", D2, 120) | {"window": "Odtwarzacz"},
                U("PC", "PC\\anna", "ApplicationFrameHost", D2, 60) | {"window": ""},
                U("PC", "PC\\anna", "msedge", D2, 200) | {"window": "Strona"}]

    def test_host_apps_listed_by_title(self):
        anna = build_usage(self.rows(), self.ACC, [], D2, D2, host_programs=self.HOSTS)[0]
        self.assertEqual([(p["program"], p.get("host"), p["seconds"]) for p in anna["programs"]],
                         [("Pasjans", "ApplicationFrameHost", 900),
                          ("msedge", None, 200),
                          ("Odtwarzacz", "ApplicationFrameHost", 120),
                          ("ApplicationFrameHost", None, 60)])   # no title: stays the host
        self.assertEqual(anna["total"]["seconds"], 1280)

    def test_without_hosts_lumped_by_program(self):
        anna = build_usage(self.rows(), self.ACC, [], D2, D2)[0]
        self.assertEqual([(p["program"], p["seconds"]) for p in anna["programs"]],
                         [("ApplicationFrameHost", 1080), ("msedge", 200)])
        self.assertNotIn("host", anna["programs"][0])

    def test_browser_titles_never_split_programs(self):
        rows = [U("PC", "PC\\anna", "msedge", D2, 60) | {"window": "A"},
                U("PC", "PC\\anna", "msedge", D2, 60) | {"window": "B"}]
        anna = build_usage(rows, self.ACC, [], D2, D2, host_programs=self.HOSTS)[0]
        self.assertEqual([(p["program"], p["seconds"]) for p in anna["programs"]],
                         [("msedge", 120)])


class UsageIgnoreTitles(unittest.TestCase):
    ACC = [("PC", "PC\\anna", None)]

    def test_exact_title_left_out(self):
        rows = [U("PC", "PC\\anna", "ApplicationFrameHost", D2, 600) | {"window": "Microsoft Family Safety"},
                U("PC", "PC\\anna", "ApplicationFrameHost", D2, 120) | {"window": "microsoft family safety "},
                U("PC", "PC\\anna", "msedge", D2, 300) | {"window": "Strona - Microsoft Family Safety"}]
        ign = frozenset({"microsoft family safety"})
        anna = build_usage(rows, self.ACC, [], D2, D2, ignore_titles=ign)[0]
        self.assertEqual(anna["total"]["seconds"], 300)     # browser title containing it is kept
        self.assertEqual([p["program"] for p in anna["programs"]], ["msedge"])

    def test_default_keeps_everything(self):
        rows = [U("PC", "PC\\anna", "ApplicationFrameHost", D2, 600) | {"window": "Microsoft Family Safety"}]
        anna = build_usage(rows, self.ACC, [], D2, D2)[0]
        self.assertEqual(anna["total"]["seconds"], 600)


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
