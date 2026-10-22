"""Synthetic R0 captures for testing rot_analyze.py: a known orientation effect, noise, drift and handling.

    python tools/rot_sim.py out.jsonl [--effect-db 4] [--noise-db 1] [--drift-db 1] [--reseat-db 0.4]
                                      [--rounds 5] [--seed 1]

Two fixed boards A and B record; R is turned (it is board C, and the node ids are the bench cell's:
A 0x6300, B 0x7368, C 0x8160). A<-R and B<-R carry the orientation effect, an antenna pattern of
effect_db * cos(angle - phase) with the links 90° apart in phase. A<-B and B<-A carry only the room's
drift. A few frames are relayed, off the home channel, or not beacons, so the filters have work to do. Each placement of R (every step, lifts included) adds a small re-seating
offset, and while R is handled its links take a hand's absorption and scatter. Nothing here is a
measurement: it exists so the analysis can be checked against a known answer.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rot_plan import make_plan  # noqa: E402

PEERS = {"A": 0x6300, "B": 0x7368, "R": 0x8160}
BASE_DBM = {("A", "R"): -52.0, ("B", "R"): -58.0, ("A", "B"): -55.0, ("B", "A"): -54.0}
PHASE_DEG = {("A", "R"): 0.0, ("B", "R"): 90.0}
T0 = 1_790_000_000.0


def simulate(path: str, rounds: int = 5, effect_db: float = 4.0, noise_db: float = 1.0, drift_db: float = 1.0,
             reseat_db: float = 0.4, hand_db: float = 6.0, rate_hz: float = 10.0, loss: float = 0.02,
             hold_s: float = 30.0, settle_s: float = 5.0, baseline_s: float = 60.0, handle_s: float = 4.0,
             discard: tuple[int, ...] = (), restart_s: float | None = None, seed: int = 1) -> None:
    rng = random.Random(seed)
    plan = make_plan(rounds=rounds, hold_s=hold_s, baseline_s=baseline_s, seed=seed)
    records: list[dict] = [{"t": "meta", "host_ts": T0, "tool": "rot_sim", "ports": {"A": "sim", "B": "sim"},
                            "plan": plan, "settings": {"effect_db": effect_db, "seed": seed}}]
    segments = []  # (start, end, orientation, {link: re-seat offset}, handled)
    t = T0 + 5.0
    for step in plan:
        info = {k: step[k] for k in ("id", "round", "kind", "from", "to")}
        records.append({"t": "step", "host_ts": t, "phase": "prompt", **info})
        segments.append((t, t + handle_s, step["to"], {}, True))
        t += handle_s
        records.append({"t": "step", "host_ts": t, "phase": "handled", **info})
        offsets = {link: rng.gauss(0.0, reseat_db) for link in PHASE_DEG}
        segments.append((t, t + settle_s + step["hold_s"], step["to"], offsets, False))
        t += settle_s
        records.append({"t": "step", "host_ts": t, "phase": "hold_start", **info})
        t += step["hold_s"]
        records.append({"t": "step", "host_ts": t, "phase": "hold_end", **info})
        if step["id"] in discard:
            records.append({"t": "discard", "host_ts": t + 0.5, "id": step["id"]})
    end = t

    seconds = int(end - T0) + 2
    walk_sd = drift_db / math.sqrt(seconds)
    for (rx, tx), base in BASE_DBM.items():
        drift, level = [], 0.0
        for _ in range(seconds):
            drift.append(level)
            level += rng.gauss(0.0, walk_sd)
        seg_i, seq = 0, 0
        restart_at = T0 + restart_s if restart_s is not None and tx == "R" else None
        tf = T0 + rng.random() / rate_hz
        while tf < end:
            tf += (1.0 + rng.uniform(-0.1, 0.1)) / rate_hz
            while seg_i + 1 < len(segments) and segments[seg_i][1] <= tf:
                seg_i += 1
            seq = (seq + 1) & 0xFFFF
            if restart_at is not None and tf >= restart_at:
                seq, restart_at = 0, None  # R rebooted: its beacon count starts again
            if rng.random() < loss:
                continue
            start, stop, orient, offsets, handled = segments[seg_i]
            rssi = base + drift[int(tf - T0)] + rng.gauss(0.0, noise_db)
            if (rx, tx) in PHASE_DEG:
                rssi += effect_db * math.cos(math.radians(orient - PHASE_DEG[(rx, tx)]))
                rssi += offsets.get((rx, tx), 0.0)
                if handled and start <= tf < stop:
                    rssi += rng.gauss(-hand_db, hand_db)
            frame = {"t": "rssi", "us": int((tf - T0) * 1e6) & 0xFFFFFFFF, "peer": PEERS[tx], "rssi": round(rssi),
                     "nf": -96, "sig": 0, "rate": 0, "kind": "beacon", "seq": seq, "rel": 0, "ch": 6, "away": 0}
            u = rng.random()
            if u < 0.01:  # a relayed frame: its RSSI is the relay's, never R's
                frame.update(rel=1, rssi=-90)
            elif u < 0.02:  # heard off the home channel
                frame.update(away=1, ch=1, rssi=frame["rssi"] - 8)
            elif u < 0.04:  # a probe or HELLO, at another rate
                frame.update(kind="unicast", seq=0, rate=11, rssi=frame["rssi"] + 3)
            records.append({"host_ts": tf, "port": rx, "line": json.dumps(frame, separators=(",", ":"))})
    records.sort(key=lambda rec: rec["host_ts"])
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out")
    ap.add_argument("--effect-db", type=float, default=4.0)
    ap.add_argument("--noise-db", type=float, default=1.0)
    ap.add_argument("--drift-db", type=float, default=1.0)
    ap.add_argument("--reseat-db", type=float, default=0.4)
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)
    simulate(args.out, rounds=args.rounds, effect_db=args.effect_db, noise_db=args.noise_db,
             drift_db=args.drift_db, reseat_db=args.reseat_db, seed=args.seed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
