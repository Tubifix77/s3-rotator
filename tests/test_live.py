"""Tests for rot_live.py's engine: settings, the trend rules, the tracker and the needle, without boards.

    python -m unittest discover -s tests
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import rot_live  # noqa: E402

# R0's position averages and spreads (README, Results), as load_calibration would give them
CALIB = {0: {"A": [-45.3, 5.0], "B": [-39.2, 2.9]}, 90: {"A": [-51.0, 6.7], "B": [-40.6, 3.8]},
         180: {"A": [-38.9, 2.6], "B": [-32.8, 1.6]}, 270: {"A": [-47.6, 2.4], "B": [-32.6, 2.5]}}


def settings(**changes) -> dict:
    return rot_live.apply_settings(copy.deepcopy(rot_live.DEFAULTS), changes)


class SettingsTest(unittest.TestCase):
    def test_every_setting_can_be_changed_and_is_checked(self):
        s = settings(short_n=12, before_n=8, gap_n=3, settle=0.5, still=2.0, drift_follow=0.1, fallback="stay",
                     now_n=30, now_spread=False, now_smooth=0.3, calib_s=5)
        self.assertEqual((s["short_n"], s["gap_n"], s["fallback"], s["now_spread"]), (12, 3, "stay", False))
        for bad in ({"short_n": 0}, {"still": 99}, {"fallback": "guess"}, {"nonsense": 1},
                    {"rules": [{"from": 45, "to": 90, "A": ["<", 1], "B": ["<", 1]}]},
                    {"rules": [{"from": 0, "to": 90, "A": ["~", 1], "B": ["<", 1]}]}):
            with self.assertRaises(ValueError, msg=bad):
                settings(**bad)

    def test_defaults_are_r0s_rules(self):
        self.assertEqual(len(rot_live.DEFAULTS["rules"]), 12)
        self.assertEqual(rot_live.DEFAULTS["short_n"], 5)


class TrendRuleTest(unittest.TestCase):
    def test_r0s_average_changes_land_where_they_should(self):
        s = settings()
        for frm in rot_live.POSITIONS:
            for to in rot_live.POSITIONS:
                if to == frm:
                    continue
                d_a = CALIB[to]["A"][0] - CALIB[frm]["A"][0]
                d_b = CALIB[to]["B"][0] - CALIB[frm]["B"][0]
                self.assertEqual(rot_live.trend_rule(CALIB, s, frm, d_a, d_b)[0], to, f"{frm} -> {to}")

    def test_small_changes_are_no_turn_and_unmatched_ones_fall_back(self):
        s = settings()
        self.assertEqual(rot_live.trend_rule(CALIB, s, 90, 1.0, -2.0), (90, "still"))
        empty = settings(rules=[])
        self.assertEqual(rot_live.trend_rule(CALIB, empty, 0, -2.3, 6.6), (270, "nearest"))
        self.assertEqual(rot_live.trend_rule(CALIB, settings(rules=[], fallback="stay"), 0, -2.3, 6.6), (0, "stay"))


class TrackerTest(unittest.TestCase):
    def feed(self, tracker, s, levels, steps):
        series = {"A": [], "B": []}
        t = 0.0
        for (a, b), n in zip(levels, steps):
            for _ in range(n):
                series["A"].append(a)
                series["B"].append(b)
                t += 0.1
                tracker.update(t, series, CALIB, s)
        return tracker

    def test_a_settled_turn_from_0_to_270_is_seen_once(self):
        s = settings()
        tr = self.feed(rot_live.Tracker(0), s, [(-45, -39), (-47, -33)], [40, 40])
        self.assertEqual(tr.pos, 270)
        self.assertEqual(len(tr.moves), 1)
        self.assertEqual(tr.moves[0]["why"], "rule 3")

    def test_wider_windows_still_find_the_turn(self):
        s = settings(short_n=20, before_n=30, gap_n=10)
        tr = self.feed(rot_live.Tracker(0), s, [(-45, -39), (-47, -33)], [100, 100])
        self.assertEqual((tr.pos, len(tr.moves)), (270, 1))

    def test_a_steady_reading_is_never_a_move(self):
        tr = self.feed(rot_live.Tracker(180), settings(), [(-39, -33)], [300])
        self.assertEqual((tr.pos, len(tr.moves), tr.moving), (180, 0, False))


class NeedleTest(unittest.TestCase):
    def test_the_needle_points_at_the_matching_position(self):
        for o, links in CALIB.items():
            probs, angle, conf = rot_live.where_now(CALIB, {k: v[0] for k, v in links.items()})
            self.assertEqual(max(probs, key=probs.get), o)
            self.assertAlmostEqual(sum(probs.values()), 1.0)
            self.assertTrue(0.0 <= conf <= 1.0)


class EngineTest(unittest.TestCase):
    def line(self, peer, rssi, kind="beacon", rel=0, away=0):
        return json.dumps({"t": "rssi", "us": 1, "peer": peer, "rssi": rssi, "kind": kind, "rel": rel, "away": away})

    def test_only_cs_beacons_feed_the_dial_and_state_is_json(self):
        e = rot_live.Engine(copy.deepcopy(CALIB), settings(), 0x8160, {"A": "COM3", "B": "COM4"})
        for i in range(30):
            for port, rssi in (("A", -45), ("B", -39)):
                e.add({"host_ts": 100 + i * 0.1, "port": port, "line": self.line(0x8160, rssi)})
            e.add({"host_ts": 100 + i * 0.1, "port": "A", "line": self.line(0x8160, -90, rel=1)})
            e.add({"host_ts": 100 + i * 0.1, "port": "A", "line": self.line(0x8160, -90, kind="unicast")})
            e.add({"host_ts": 100 + i * 0.1, "port": "A", "line": self.line(0x7368, -27)})
        self.assertEqual(set(e.series["A"]), {-45})
        self.assertIn("A:7368", e.series)
        e.tick()
        state = json.loads(e.snapshot)
        self.assertEqual(state["now_avg"]["A"], -45)
        self.assertEqual(max(state["probs"], key=state["probs"].get), "0")


if __name__ == "__main__":
    unittest.main()
