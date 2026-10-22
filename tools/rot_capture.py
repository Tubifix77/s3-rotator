"""R0's guided recording: Potluck's RSSI tee from one or more boards, with every step of the plan marked.

    <IDF python> tools/rot_capture.py --port A=COM3 --port B=COM4 --out captures/r0-2026-10-23.jsonl
                                      [--rounds 5] [--hold-s 30] [--settle-s 10] [--baseline-s 60]
                                      [--no-lift] [--no-full-turn] [--seed N] [--baud 115200]
    <IDF python> tools/rot_capture.py --port A=COM3 --port B=COM4 --out captures/check.jsonl --listen-s 60

With --listen-s there is no plan: it records for that long, then prints each link's frame rate and level
(flagging one too strong to measure) and any restart. That is the check for C's placement, and, run for
30 minutes, for a power bank.

Each board runs a Potluck image built with CONFIG_POT_RSSI_TEE (the rssi-tee variant), which prints one
{"t":"rssi",...} line per frame it hears from a peer. The ports are opened with DTR and RTS low, as
Potluck's console_log.py does, so opening them does not reset the boards. pyserial comes with ESP-IDF's
Python environment.

Only the tee's lines are kept. Every other console line is counted and dropped, because a board's log
can name the Wi-Fi networks it uses.

The program walks the plan (rot_plan.py). For each step it says how to turn R and waits for Enter (your
hands are off R by then). It then waits --settle-s, so you can walk to the stand spot and the links can
settle, and holds for the step's hold time while frames are recorded. A beep marks the hold's end. At a prompt, "x" discards the previous hold (someone walked past, R
slipped) and "q" ends the session; Ctrl-C ends it too. A partial session can still be analysed.

Output, one JSON object per line, in order of arrival:
  {"t":"meta", "host_ts", "tool", "ports": {label: device}, "settings": {...}, "plan": [...]}
  {"host_ts", "port", "line"}        a tee line as the board printed it (Potluck's raw-tee shape)
  {"t":"step", "host_ts", "phase", "id", "round", "kind", "from", "to"}
        phase: prompt (instruction shown), handled (Enter: hands off), hold_start, hold_end
  {"t":"discard", "host_ts", "id"}   the hold of step id is not to be used
  {"t":"summary", "host_ts", "ports": {label: {"rssi": n, "other": n}}, "holds_done": n}
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rot_plan import describe, make_plan  # noqa: E402

TOOL = "rot_capture 0.1"
TEE_PREFIX = '{"t":"rssi"'
PREFLIGHT_S = 5.0
HANDLING_GUESS_S = 10.0  # for the duration estimate only


class PortStats:
    """Counts per port and per peer, shared by the reader threads and the prompts."""

    def __init__(self, labels):
        self.lock = threading.Lock()
        self.rssi = {label: 0 for label in labels}
        self.other = {label: 0 for label in labels}
        self.peers = defaultdict(lambda: [0, 0])  # (label, peer) -> [frames, sum of rssi]

    def tee(self, label: str, frame: dict) -> None:
        with self.lock:
            self.rssi[label] += 1
            p = self.peers[(label, frame.get("peer"))]
            p[0] += 1
            p[1] += frame.get("rssi", 0)

    def dropped(self, label: str) -> None:
        with self.lock:
            self.other[label] += 1

    def snapshot(self):
        with self.lock:
            return dict(self.rssi), dict(self.other), {k: list(v) for k, v in self.peers.items()}


def reader(label, ser, out_q, stats, stop) -> None:
    buf = b""
    try:
        while not stop.is_set():
            buf += ser.read(max(1, ser.in_waiting))
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                ts = time.time()
                line = raw.decode("utf-8", "replace").strip()
                i = line.find(TEE_PREFIX)
                if i < 0:
                    stats.dropped(label)
                    continue
                line = line[i:]
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    stats.dropped(label)
                    continue
                stats.tee(label, frame)
                out_q.put({"host_ts": ts, "port": label, "line": line})
    finally:
        ser.close()


def writer(f, in_q) -> None:
    while (rec := in_q.get()) is not None:
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")


def parse_ports(specs: list[str]) -> dict[str, str]:
    ports: dict[str, str] = {}
    for spec in specs:
        label, sep, dev = spec.partition("=")
        if not sep or not label or not dev:
            raise SystemExit(f"--port {spec!r}: expected LABEL=DEVICE, e.g. A=COM3")
        if label in ports:
            raise SystemExit(f"--port label {label!r} given twice")
        ports[label] = dev
    return ports


def countdown(seconds: float, what: str, stats: PortStats) -> None:
    end = time.time() + seconds
    last_counts, last_t = stats.snapshot()[0], time.time()
    rates = ""
    while (left := end - time.time()) > 0:
        now = time.time()
        if now - last_t >= 2.0:
            counts = stats.snapshot()[0]
            rates = "  ".join(f"{label} {(counts[label] - last_counts[label]) / (now - last_t):.0f}/s"
                              for label in counts)
            last_counts, last_t = counts, now
        print(f"\r  {what}: {left:5.1f} s   {rates}   ", end="", flush=True)
        time.sleep(min(0.25, left))
    print(f"\r  {what}: done{' ' * 48}")


def preflight(stats: PortStats) -> bool:
    print(f"Listening {PREFLIGHT_S:.0f} s for the tee ...")
    time.sleep(PREFLIGHT_S)
    rssi, other, peers = stats.snapshot()
    for label in rssi:
        print(f"  {label}: {rssi[label]} tee lines, {other[label]} other lines")
        for (port, peer), (n, total) in sorted((kv for kv in peers.items() if kv[0][0] == label),
                                               key=lambda kv: str(kv[0][1])):
            if isinstance(peer, int):
                print(f"      peer {peer} (node-{peer:04x}): {n / PREFLIGHT_S:.1f} frames/s, mean {total / n:.1f} dBm")
    return all(rssi.values())


def make_emit(out_q: queue.Queue):
    # The record type is positional-only, so a step's own "kind" field can travel in **fields.
    def emit(record: str, /, **fields) -> None:
        out_q.put({"t": record, "host_ts": time.time(), **fields})
    return emit


def run_plan(plan: list[dict], settle_s: float, stats: PortStats, emit, holds_done: list[int]) -> None:
    minutes = sum(s["hold_s"] + settle_s + HANDLING_GUESS_S for s in plan) / 60
    print(f"\nPlan: {len(plan)} steps, about {minutes:.0f} minutes. Degrees are clockwise seen from above.\n"
          "At each prompt: do what it says, press Enter, walk to the stand spot and wait for the beep.\n"
          "x + Enter discards the previous hold, q + Enter stops.\n")
    last_hold = None
    for step in plan:
        info = {k: step[k] for k in ("id", "round", "kind", "from", "to")}
        print(f"[{step['id'] + 1}/{len(plan)}, round {step['round']}] {describe(step)}")
        emit("step", phase="prompt", **info)
        while (ans := input("> ").strip().lower()) == "x":
            if last_hold is None:
                print("  nothing to discard")
                continue
            emit("discard", id=last_hold)
            print(f"  hold {last_hold} discarded")
            last_hold = None
        if ans == "q":
            return
        emit("step", phase="handled", **info)
        countdown(settle_s, "settling", stats)
        emit("step", phase="hold_start", **info)
        countdown(step["hold_s"], "holding, hands off", stats)
        emit("step", phase="hold_end", **info)
        print("\a", end="", flush=True)  # the beep that calls you back from the stand spot
        last_hold = step["id"]
        holds_done[0] += 1
    if input("Plan complete. x + Enter discards the last hold, Enter finishes. > ").strip().lower() == "x" \
            and last_hold is not None:
        emit("discard", id=last_hold)


def listen_report(path: str, seconds: float) -> str:
    """For --listen-s: each link's frame rate and level, and every restart seen, by rot_analyze's own rules."""
    from rot_analyze import SATURATION_DBM, load

    cap = load(path)
    lines = ["", "Links (heartbeat beacons only, relayed and off-channel frames left out):"]
    for name, (ts, rssi, _) in sorted(cap.series.items()):
        level = sum(rssi) / len(rssi)
        note = "  TOO STRONG: move these two boards further apart" if level > SATURATION_DBM else ""
        lines.append(f"  {name}: {len(ts) / seconds:5.1f} frames/s, mean {level:6.1f} dBm, "
                     f"{min(rssi)}..{max(rssi)}{note}")
    lines.append(f"Restarts: {len(cap.resets) or 'none'}")
    lines += [f"  {node} {what} at {time.strftime('%H:%M:%S', time.localtime(ts))}" for ts, node, what in cap.resets]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="R0's guided recording of Potluck's RSSI tee.")
    ap.add_argument("--port", action="append", required=True, metavar="LABEL=DEVICE",
                    help="a board to record, e.g. A=COM3; repeat for each board")
    ap.add_argument("--out", required=True, help="capture file to create (never overwritten)")
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--hold-s", type=float, default=30.0)
    ap.add_argument("--settle-s", type=float, default=10.0,
                    help="after Enter: time to walk to the stand spot and for the links to settle")
    ap.add_argument("--baseline-s", type=float, default=60.0)
    ap.add_argument("--no-lift", action="store_true", help="leave out the lift control in each round")
    ap.add_argument("--no-full-turn", action="store_true", help="leave out the final full-turn control")
    ap.add_argument("--seed", type=int, help="plan order seed (default: the clock)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--listen-s", type=float, metavar="SECONDS",
                    help="no plan: only record for this long, then report each link's level and any restart "
                         "(placement and power-bank checks)")
    args = ap.parse_args(argv)

    ports = parse_ports(args.port)
    seed = args.seed if args.seed is not None else int(time.time())
    plan = make_plan(rounds=args.rounds, hold_s=args.hold_s, baseline_s=args.baseline_s,
                     lift=not args.no_lift, full_turn=not args.no_full_turn, seed=seed)

    import serial  # pyserial, from ESP-IDF's Python environment

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        f = open(out_path, "x", encoding="utf-8", buffering=1)
    except FileExistsError:
        raise SystemExit(f"{out_path} exists; captures are never overwritten")

    serials = {}
    for label, dev in ports.items():
        s = serial.Serial()
        s.port, s.baudrate, s.timeout = dev, args.baud, 0.05
        s.dtr = False
        s.rts = False
        try:
            s.open()
        except serial.SerialException as e:
            for opened in serials.values():
                opened.close()
            f.close()
            out_path.unlink()
            raise SystemExit(f"{label}={dev}: {e}")
        serials[label] = s

    q: queue.Queue = queue.Queue()
    stop = threading.Event()
    stats = PortStats(ports)
    w = threading.Thread(target=writer, args=(f, q), daemon=True)
    w.start()
    readers = [threading.Thread(target=reader, args=(label, s, q, stats, stop), daemon=True)
               for label, s in serials.items()]
    for t in readers:
        t.start()

    emit = make_emit(q)
    emit("meta", tool=TOOL, ports=ports, plan=[] if args.listen_s is not None else plan,
         settings={"rounds": args.rounds, "hold_s": args.hold_s, "settle_s": args.settle_s,
                   "baseline_s": args.baseline_s, "seed": seed, "baud": args.baud, "listen_s": args.listen_s})
    holds_done = [0]  # counted by run_plan, read after an interrupted plan too
    try:
        if not preflight(stats):
            ans = input("A port printed no tee lines (not the rssi-tee image, or wrong baud?). Continue? [y/N] ")
            if ans.strip().lower() != "y":
                return 1
        if args.listen_s is not None:
            countdown(args.listen_s, "listening, hands off", stats)
        else:
            run_plan(plan, args.settle_s, stats, emit, holds_done)
    except (KeyboardInterrupt, EOFError):
        print("\nStopped.")
    finally:
        rssi, other, _ = stats.snapshot()
        emit("summary", ports={label: {"rssi": rssi[label], "other": other[label]} for label in rssi},
             holds_done=holds_done[0])
        stop.set()
        for t in readers:
            t.join(timeout=2)
        q.put(None)
        w.join(timeout=5)
        f.close()
        print(f"Wrote {out_path}: {holds_done[0]} holds; "
              + ", ".join(f"{label} {rssi[label]} tee lines" for label in rssi))
    if args.listen_s is not None:
        print(listen_report(str(out_path), args.listen_s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
