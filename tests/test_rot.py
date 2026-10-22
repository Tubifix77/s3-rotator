"""Tests for R0's plan and analysis, against synthetic captures with a known answer.

    python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import rot_analyze  # noqa: E402
from rot_plan import POSITIONS, make_plan  # noqa: E402
from rot_sim import simulate  # noqa: E402

R = "0x8160"


class TempCaptures(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def sim(self, name: str, **kw) -> str:
        path = str(Path(self.dir.name) / f"{name}.jsonl")
        simulate(path, **kw)
        return path

    def analyze(self, path: str, rotor=(R,)) -> dict:
        return rot_analyze.analyze(path, list(rotor), perms=100)


class PlanTest(unittest.TestCase):
    def test_rounds_visit_every_position_once_with_one_lift(self):
        for seed in range(20):
            plan = make_plan(rounds=5, seed=seed)
            self.assertEqual(plan[0]["kind"], "baseline")
            self.assertEqual(plan[-1]["kind"], "full")
            for r in range(1, 6):
                steps = [s for s in plan if s["round"] == r and s["kind"] != "full"]
                self.assertEqual(sorted(s["to"] for s in steps if s["kind"] == "turn"), sorted(POSITIONS))
                self.assertEqual(sum(s["kind"] == "lift" for s in steps), 1)

    def test_steps_chain_and_every_turn_turns(self):
        for seed in range(20):
            plan = make_plan(rounds=5, seed=seed)
            for prev, cur in zip(plan, plan[1:]):
                self.assertEqual(cur["from"], prev["to"])
                self.assertEqual(cur["kind"] == "turn", cur["to"] != cur["from"])


class VerdictTest(TempCaptures):
    def test_strong_effect_is_seen_with_its_direction(self):
        r = self.analyze(self.sim("strong", effect_db=4.0))
        self.assertTrue(r["t1"]["ok"])
        self.assertGreaterEqual(r["position"]["raw"]["accuracy"], 0.9)
        self.assertGreaterEqual(r["direction"]["tracked"]["quarter_accuracy"], 0.9)
        self.assertLess(r["direction"]["tracked"]["p"], rot_analyze.P_MAX)
        self.assertTrue(any(line.startswith("T3 passes, tracked") for line in r["verdict"]))

    def test_no_effect_is_killed(self):
        r = self.analyze(self.sim("null", effect_db=0.0))
        self.assertFalse(r["t1"]["ok"])
        self.assertTrue(r["verdict"][0].startswith("KILL"))
        self.assertGreater(r["position"]["raw"]["p"], rot_analyze.P_MAX)

    def test_drift_hurts_raw_position_more_than_centred(self):
        r = self.analyze(self.sim("drift", effect_db=1.5, drift_db=3.0, seed=3))
        self.assertGreater(r["position"]["centred"]["accuracy"], r["position"]["raw"]["accuracy"])
        self.assertIn("T2 passes only centred: positions are distinct, but drift moves them over the session.",
                      r["verdict"])

    def test_control_links_carry_no_position(self):
        r = self.analyze(self.sim("strong", effect_db=4.0))
        for name, role in r["roles"].items():
            ratio = r["separation"][name]["ratio"]
            if role == "control":
                self.assertLess(ratio, 3.0, name)
            else:
                self.assertGreater(ratio, 5.0, name)

    def test_too_few_rounds_is_not_judged(self):
        r = self.analyze(self.sim("short", rounds=2))
        self.assertTrue(r["verdict"][0].startswith("NOT ENOUGH DATA"))


class InputTest(TempCaptures):
    def test_relayed_off_channel_and_non_beacon_frames_are_left_out(self):
        r = self.analyze(self.sim("strong"))
        for key in ("relayed", "away", "not_beacon"):
            self.assertGreater(r["counts"][key], 0, key)
        self.assertEqual(sorted(r["features"]), ["A<-8160", "B<-8160"])
        for name in r["links"]:
            self.assertEqual(set(r["radio"][name]), {(0, 0)}, name)
            self.assertEqual(r["channels"][name], [6], name)

    def test_discarded_hold_is_not_used(self):
        r = self.analyze(self.sim("discard", discard=(3,)))
        self.assertEqual(r["discarded"], [3])
        self.assertNotIn(3, [h.id for h in r["holds"]])

    def test_hold_with_a_restart_is_left_out(self):
        r = self.analyze(self.sim("restart", restart_s=40.0))  # inside the baseline hold
        self.assertEqual(r["counts"]["holds_with_restart"], 1)
        self.assertNotIn(0, [h.id for h in r["holds"]])
        self.assertTrue(r["resets"])
        self.assertTrue(all(node == "8160" for _, node, _ in r["resets"]))

    def test_listen_report_names_links_and_restarts(self):
        from rot_capture import listen_report

        text = listen_report(self.sim("listen", rounds=1, restart_s=40.0), 300.0)
        for name in ("A<-8160", "B<-8160", "A<-7368", "B<-6300"):
            self.assertIn(name, text)
        self.assertIn("8160 restarted", text)
        self.assertNotIn("TOO STRONG", text)

    def test_counter_wrap_is_not_a_restart(self):
        self.assertTrue(rot_analyze.wrapped(65530, 3, 1 << 16))
        self.assertFalse(rot_analyze.wrapped(30000, 5, 1 << 16))


class CaptureTest(TempCaptures):
    def test_run_plan_records_every_step_and_the_analysis_reads_them(self):
        import builtins
        import queue
        from unittest import mock

        import rot_capture

        q: queue.Queue = queue.Queue()
        plan = make_plan(rounds=1, seed=4)
        answers = iter(["", "x", ""] + [""] * len(plan))  # the second prompt discards the first hold
        with mock.patch.object(builtins, "input", lambda *_: next(answers)), \
                mock.patch.object(rot_capture, "countdown", lambda *a, **k: None), \
                mock.patch("sys.stdout"):
            done = [0]
            rot_capture.run_plan(plan, 0.0, None, rot_capture.make_emit(q), done)
        recs = []
        while not q.empty():
            recs.append(q.get())
        self.assertEqual(done[0], len(plan))
        steps = [r for r in recs if r["t"] == "step"]
        self.assertEqual(len(steps), 4 * len(plan))
        self.assertEqual({r["kind"] for r in steps}, {s["kind"] for s in plan})
        self.assertEqual([r["id"] for r in recs if r["t"] == "discard"], [0])
        path = Path(self.dir.name) / "capture.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
        cap = rot_analyze.load(str(path))
        self.assertEqual(sorted(h.id for h in cap.holds), [s["id"] for s in plan][1:])


class RotorTest(TempCaptures):
    def test_rotor_by_peer_id_in_every_spelling(self):
        path = self.sim("roles", rounds=1)
        cap = rot_analyze.load(path)
        expected = rot_analyze.assign_roles(cap, [R])
        for tok in ("node-8160", "33120", "0X8160"):
            self.assertEqual(rot_analyze.assign_roles(cap, [tok]), expected, tok)
        self.assertEqual({n for n, role in expected.items() if role == "rotor"}, {"A<-8160", "B<-8160"})

    def test_rotor_by_port_takes_the_links_it_receives(self):
        cap = rot_analyze.load(self.sim("roles", rounds=1))
        roles = rot_analyze.assign_roles(cap, ["A"])
        self.assertEqual({n for n, role in roles.items() if role == "rotor"}, {"A<-8160", "A<-7368"})

    def test_unknown_rotor_is_refused(self):
        cap = rot_analyze.load(self.sim("roles", rounds=1))
        with self.assertRaises(ValueError):
            rot_analyze.assign_roles(cap, ["0x1234"])


if __name__ == "__main__":
    unittest.main()
