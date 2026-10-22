"""Live dial: Potluck's RSSI tee from A and B, read the way a dial would, drawn in a browser while you turn C.

    <IDF python> tools/rot_live.py --port A=COM3 --port B=COM4 [--rotor 0x8160]
                                   [--calib captures/r0-2026-10-23-holds.csv] [--settings file.json]
                                   [--out captures/live-....jsonl] [--http 8475]

Then open http://127.0.0.1:8475. Ports are opened reset-free (DTR/RTS low) in this one process; only the tee's
heartbeat beacons from C are used (relayed and off-channel frames are left out).

It's a research tool: every number below is a setting, changed live from the page (POST /settings), and
the defaults are R0's. With --out every tee line is journaled in rot_capture.py's raw shape, and so is
every settings change ({"t":"settings", ...}), so a session shows which settings gave which behaviour.

Two readings run side by side, both from the calibration: each position's mean and spread on A<-C and
B<-C, from R0's holds by default, editable, and replaceable by "C is at ... now" (the last `calib_s`).

- **Where is it now**: each position's likelihood from the average of the last `now_n` readings, with or
  without each position's own spread. The needle points at the likelihood-weighted mean angle and its
  length says how sure it is; `now_smooth` (0..1) smooths the needle (1 = no smoothing).
- **Trend rules**: the rule table, applied to changes. The recent average (last `short_n` readings) is
  compared with an earlier one (`before_n` readings, ending `gap_n` readings before the recent ones):
  while they differ by `settle` dB or more on either link, C is moving. Once it settles, the change since
  the last settled level (the anchor) is judged by the rules from the tracked position: under `still` dB
  on both links is no turn (and the anchor then closes `drift_follow` of the gap, following slow drift);
  otherwise the first matching rule row wins, and if none matches, `fallback` decides.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import queue
import statistics
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from rot_capture import PortStats, parse_ports, reader, writer  # noqa: E402

POSITIONS = (0, 90, 180, 270)
TICK_S = 0.2
HISTORY = 3000  # readings kept per link (about 5 minutes at 10/s)
OPS = {"<": lambda x, v: x < v, "<=": lambda x, v: x <= v, ">": lambda x, v: x > v,
       ">=": lambda x, v: x >= v, "any": lambda x, v: True}

# R0's rule table (README, 2026-10-23): first matching row for the tracked start wins.
R0_RULES = [
    {"from": 0, "to": 90, "A": ["<", -3.0], "B": ["<", 2.5]},
    {"from": 0, "to": 180, "A": [">", 2.0], "B": [">", 2.5]},
    {"from": 0, "to": 270, "A": ["<=", 2.0], "B": [">", 2.5]},
    {"from": 90, "to": 0, "A": [">", 3.0], "B": ["<", 4.7]},
    {"from": 90, "to": 180, "A": [">", 7.8], "B": [">", 4.7]},
    {"from": 90, "to": 270, "A": ["<=", 7.8], "B": [">", 4.7]},
    {"from": 180, "to": 270, "A": ["<", -3.0], "B": [">", -3.1]},
    {"from": 180, "to": 90, "A": ["<", -9.3], "B": ["<", -3.1]},
    {"from": 180, "to": 0, "A": [">=", -9.3], "B": ["<", -3.1]},
    {"from": 270, "to": 180, "A": [">", 3.0], "B": [">", -3.4]},
    {"from": 270, "to": 0, "A": [">", -0.6], "B": ["<", -3.4]},
    {"from": 270, "to": 90, "A": ["<=", -0.6], "B": ["<", -3.4]},
]

DEFAULTS = {
    "short_n": 5, "before_n": 5, "gap_n": 0,         # trend: recent readings, earlier readings, skipped between
    "settle": 1.0, "still": 3.0, "drift_follow": 0.02,
    "fallback": "nearest",                           # no rule matched: "nearest" expected change, or "stay"
    "now_n": 10, "now_spread": True, "now_smooth": 1.0,
    "calib_s": 3.0,
    "rules": R0_RULES,
}
LIMITS = {"short_n": (1, 600), "before_n": (1, 600), "gap_n": (0, 600), "now_n": (1, 600),
          "settle": (0.05, 30.0), "still": (0.05, 30.0), "drift_follow": (0.0, 1.0), "now_smooth": (0.01, 1.0),
          "calib_s": (0.5, 60.0)}


def load_calibration(path: str) -> dict[int, dict[str, list[float]]]:
    """Each position's [mean, SD] of the hold means on the rotor links, keyed by receiving port (A, B)."""
    values: dict[int, dict[str, list[float]]] = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["role"] == "rotor":
                port = row["link"].split("<-")[0]
                values.setdefault(int(row["to"]), {}).setdefault(port, []).append(float(row["mean_dbm"]))
    return {o: {port: [statistics.fmean(xs), max(1.0, statistics.stdev(xs)) if len(xs) > 1 else 3.0]
                for port, xs in by_port.items()} for o, by_port in values.items()}


def apply_settings(current: dict, changes: dict) -> dict:
    """The settings with `changes` merged in, each checked; raises ValueError naming what is wrong."""
    new = copy.deepcopy(current)
    for key, value in changes.items():
        if key not in DEFAULTS:
            raise ValueError(f"unknown setting {key!r}")
        if key in LIMITS:
            lo, hi = LIMITS[key]
            v = int(value) if isinstance(DEFAULTS[key], int) else float(value)
            if not lo <= v <= hi:
                raise ValueError(f"{key} must be between {lo} and {hi}")
            new[key] = v
        elif key == "fallback":
            if value not in ("nearest", "stay"):
                raise ValueError("fallback must be 'nearest' or 'stay'")
            new[key] = value
        elif key == "now_spread":
            new[key] = bool(value)
        elif key == "rules":
            rows = []
            for i, row in enumerate(value):
                try:
                    frm, to = int(row["from"]), int(row["to"])
                    conds = {k: [str(row[k][0]), float(row[k][1])] for k in ("A", "B")}
                except (KeyError, TypeError, ValueError, IndexError):
                    raise ValueError(f"rule {i + 1}: needs from, to, A [op, dB] and B [op, dB]")
                if frm not in POSITIONS or to not in POSITIONS:
                    raise ValueError(f"rule {i + 1}: from and to must be 0, 90, 180 or 270")
                if any(c[0] not in OPS for c in conds.values()):
                    raise ValueError(f"rule {i + 1}: an operator must be one of {', '.join(OPS)}")
                rows.append({"from": frm, "to": to, **conds})
            new[key] = rows
    return new


def where_now(calib: dict, reading: dict[str, float], spread: bool = True) -> tuple[dict[int, float], float, float]:
    """Likelihood per position, the needle's angle (deg) and its length (0..1)."""
    scores = {}
    for o, links in calib.items():
        s = 0.0
        for port, (mean, sd) in links.items():
            if port in reading:
                s += ((reading[port] - mean) / sd) ** 2 + 2 * math.log(sd) if spread else (reading[port] - mean) ** 2 / 9.0
        scores[o] = s
    best = min(scores.values())
    w = {o: math.exp(-(s - best) / 2) for o, s in scores.items()}
    total = sum(w.values())
    probs = {o: v / total for o, v in w.items()}
    x = sum(p * math.cos(math.radians(o)) for o, p in probs.items())
    y = sum(p * math.sin(math.radians(o)) for o, p in probs.items())
    return probs, math.degrees(math.atan2(y, x)) % 360, math.hypot(x, y)


def trend_rule(calib: dict, settings: dict, frm: int, d_a: float, d_b: float) -> tuple[int, str]:
    """Where a settled change of d_a, d_b from `frm` went, and why: 'still', 'rule N', 'nearest' or 'stay'."""
    if abs(d_a) < settings["still"] and abs(d_b) < settings["still"]:
        return frm, "still"
    for i, row in enumerate(settings["rules"]):
        if row["from"] == frm and OPS[row["A"][0]](d_a, row["A"][1]) and OPS[row["B"][0]](d_b, row["B"][1]):
            return row["to"], f"rule {i + 1}"
    if settings["fallback"] == "stay" or frm not in calib:
        return frm, "stay"

    def miss(o: int) -> float:  # the nearest expected change
        return ((d_a - (calib[o]["A"][0] - calib[frm]["A"][0])) ** 2
                + (d_b - (calib[o]["B"][0] - calib[frm]["B"][0])) ** 2)
    return min(calib, key=miss), "nearest"


class Tracker:
    """The trend rules on a live stream: wait for C to settle, compare with the last settled level, move."""

    def __init__(self, start: int = 0):
        self.pos = start
        self.anchor: dict[str, float] | None = None
        self.moving = False
        self.moves: deque = deque(maxlen=20)
        self.last = {}

    def update(self, t: float, series: dict[str, list[float]], calib: dict, s: dict) -> None:
        n_s, n_b, gap = s["short_n"], s["before_n"], s["gap_n"]
        if any(len(series.get(k, [])) < n_s + gap + n_b for k in ("A", "B")):
            return
        short = {k: statistics.fmean(series[k][-n_s:]) for k in ("A", "B")}
        # the earlier window ends `gap` readings before the recent one starts
        before = {k: statistics.fmean(series[k][-(n_s + gap + n_b):len(series[k]) - n_s - gap]) for k in ("A", "B")}
        self.last = {"short": short, "before": before}
        self.moving = any(abs(short[k] - before[k]) >= s["settle"] for k in short)
        if self.moving:
            return
        if self.anchor is None:
            self.anchor = short
            return
        d_a, d_b = short["A"] - self.anchor["A"], short["B"] - self.anchor["B"]
        to, why = trend_rule(calib, s, self.pos, d_a, d_b)
        if why == "still":
            self.anchor = {k: self.anchor[k] + s["drift_follow"] * (short[k] - self.anchor[k]) for k in short}
            return
        self.moves.appendleft({"t": t, "from": self.pos, "to": to, "dA": round(d_a, 2), "dB": round(d_b, 2),
                               "why": why})
        self.pos, self.anchor = to, short


class Engine:
    def __init__(self, calib: dict, settings: dict, rotor: int, ports: dict[str, str], journal=None):
        self.lock = threading.Lock()
        self.calib = calib
        self.settings = settings
        self.rotor = rotor
        self.ports = ports
        self.journal = journal  # a queue for --out, or None
        self.series: dict[str, deque] = {}  # "A", "B" (C as heard there), "A:7368"-style controls
        self.times: dict[str, deque] = {}
        self.tracker = Tracker()
        self.needle: float | None = None
        self.snapshot = "{}"

    def note(self, kind: str, **fields) -> None:
        if self.journal is not None:
            self.journal.put({"t": kind, "host_ts": time.time(), **fields})

    def add(self, rec: dict) -> None:
        try:
            fr = json.loads(rec["line"])
        except (KeyError, json.JSONDecodeError):
            return
        if fr.get("t") != "rssi" or fr.get("rel") or fr.get("away") or fr.get("kind") != "beacon":
            return
        peer = int(fr["peer"])
        key = rec["port"] if peer == self.rotor else f"{rec['port']}:{peer:04x}"
        with self.lock:
            self.series.setdefault(key, deque(maxlen=HISTORY)).append(int(fr["rssi"]))
            self.times.setdefault(key, deque(maxlen=HISTORY)).append(rec["host_ts"])

    def tick(self) -> None:
        now = time.time()
        with self.lock:
            s = self.settings
            series = {k: list(v) for k, v in self.series.items()}
            times = {k: list(v) for k, v in self.times.items()}
            tr = self.tracker
            tr.update(now, series, self.calib, s)
            now_avg = {k: statistics.fmean(v[-s["now_n"]:]) for k, v in series.items() if v}
            reading = {k: now_avg[k] for k in ("A", "B") if k in now_avg}
            probs, angle, conf = where_now(self.calib, reading, s["now_spread"]) if len(reading) == 2 else ({}, 0.0, 0.0)
            if probs:
                if self.needle is None:
                    self.needle = angle
                else:  # smooth along the shorter way round
                    diff = (angle - self.needle + 540) % 360 - 180
                    self.needle = (self.needle + s["now_smooth"] * diff) % 360
            latest = {k: v[-1] for k, v in series.items() if v}
            rate = {k: sum(1 for t in ts if t > now - 2.0) / 2.0 for k, ts in times.items()}
            state = {
                "t": now, "ports": self.ports, "recording": self.journal is not None,
                "latest": latest, "now_avg": {k: round(v, 2) for k, v in now_avg.items()}, "rate": rate,
                "trend": {part: {k: round(v, 2) for k, v in vals.items()} for part, vals in tr.last.items()},
                "probs": {str(o): round(p, 4) for o, p in probs.items()},
                "angle": round(self.needle, 1) if self.needle is not None else None, "conf": round(conf, 3),
                "tracked": tr.pos, "moving": tr.moving,
                "anchor": {k: round(v, 2) for k, v in tr.anchor.items()} if tr.anchor else None,
                "moves": list(tr.moves), "settings": s, "calib": {str(o): v for o, v in self.calib.items()},
            }
            self.snapshot = json.dumps(state)

    def change_settings(self, changes: dict) -> None:
        with self.lock:
            self.settings = apply_settings(self.settings, changes)
            settings = self.settings
        self.note("settings", settings=settings)

    def set_calib(self, body: dict) -> None:
        with self.lock:
            for o, links in body.items():
                o = int(o)
                if o not in POSITIONS:
                    raise ValueError("positions are 0, 90, 180 and 270")
                for port, (mean, sd) in links.items():
                    if port not in ("A", "B") or float(sd) <= 0:
                        raise ValueError("calibration is A and B, each [mean dBm, SD > 0]")
                    self.calib.setdefault(o, {})[port] = [float(mean), float(sd)]
            calib = copy.deepcopy(self.calib)
        self.note("calib", calib=calib)

    def calibrate(self, pos: int) -> str:
        now = time.time()
        with self.lock:
            window = self.settings["calib_s"]
            for port in ("A", "B"):
                xs = [r for r, t in zip(self.series.get(port, []), self.times.get(port, [])) if t > now - window]
                if len(xs) < 5:
                    return f"too few readings from {port} in the last {window:g} s"
                self.calib.setdefault(pos, {}).setdefault(port, [0.0, 3.0])[0] = statistics.fmean(xs)
            if self.tracker.pos == pos:
                self.tracker.anchor = None
            calib = copy.deepcopy(self.calib)
        self.note("calib", calib=calib, measured=pos)
        return "ok"

    def reset(self, pos: int) -> None:
        with self.lock:
            self.tracker.pos, self.tracker.anchor = pos, None
            self.tracker.moves.clear()
        self.note("reset", pos=pos)


def make_handler(engine: Engine):
    page = (HERE / "rot_live.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the console for the engine
            pass

        def send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self.send(200, page, "text/html; charset=utf-8")
            elif self.path == "/state":
                self.send(200, engine.snapshot.encode())
            elif self.path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    while True:
                        self.wfile.write(f"data: {engine.snapshot}\n\n".encode())
                        self.wfile.flush()
                        time.sleep(TICK_S)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return
            else:
                self.send(404, b'{"error":"not found"}')

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except json.JSONDecodeError:
                return self.send(400, b'{"error":"the body is not JSON"}')
            try:
                if self.path == "/settings":
                    engine.change_settings(body)
                elif self.path == "/defaults":
                    engine.change_settings(DEFAULTS)
                elif self.path == "/calib":
                    engine.set_calib(body)
                elif self.path in ("/calibrate", "/reset"):
                    pos = body.get("pos")
                    if pos not in POSITIONS:
                        raise ValueError("pos must be 0, 90, 180 or 270")
                    if self.path == "/reset":
                        engine.reset(pos)
                    else:
                        result = engine.calibrate(pos)
                        if result != "ok":
                            return self.send(409, json.dumps({"error": result}).encode())
                else:
                    return self.send(404, b'{"error":"not found"}')
            except ValueError as e:
                return self.send(400, json.dumps({"error": str(e)}).encode())
            engine.tick()  # so the page's next read of /state already shows the change
            self.send(200, b'{"result":"ok"}')

    return Handler


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Live dial from Potluck's RSSI tee.")
    ap.add_argument("--port", action="append", required=True, metavar="LABEL=DEVICE")
    ap.add_argument("--rotor", default="0x8160", help="the turned board's node id (default C, 0x8160)")
    ap.add_argument("--calib", default=str(HERE.parent / "captures" / "r0-2026-10-23-holds.csv"),
                    help="a rot_analyze.py holds CSV to take each position's mean and spread from")
    ap.add_argument("--settings", help="a JSON file of settings to start from (keys as on the page)")
    ap.add_argument("--out", help="also journal every tee line and settings change here (never overwritten)")
    ap.add_argument("--http", type=int, default=8475)
    ap.add_argument("--baud", type=int, default=115200)
    args = ap.parse_args(argv)

    ports = parse_ports(args.port)
    if not {"A", "B"} <= set(ports):
        raise SystemExit("the rules read A<-C and B<-C: give --port A=... and --port B=...")
    calib = load_calibration(args.calib)
    settings = copy.deepcopy(DEFAULTS)
    if args.settings:
        try:
            settings = apply_settings(settings, json.loads(Path(args.settings).read_text(encoding="utf-8")))
        except ValueError as e:
            raise SystemExit(f"{args.settings}: {e}")
    rotor = int(args.rotor, 16) if args.rotor.lower().startswith("0x") else int(args.rotor)

    import serial  # pyserial, from ESP-IDF's Python environment

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
            raise SystemExit(f"{label}={dev}: {e}")
        serials[label] = s

    out_q, out_f = None, None
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            out_f = open(out_path, "x", encoding="utf-8", buffering=1)
        except FileExistsError:
            raise SystemExit(f"{out_path} exists; captures are never overwritten")
        out_q = queue.Queue()
        threading.Thread(target=writer, args=(out_f, out_q), daemon=True).start()
        out_q.put({"t": "meta", "host_ts": time.time(), "tool": "rot_live 0.2", "ports": ports, "plan": [],
                   "settings": settings, "calib": calib, "rotor": f"{rotor:04x}"})

    engine = Engine(calib, settings, rotor, ports, out_q)
    q: queue.Queue = queue.Queue()
    stop = threading.Event()
    stats = PortStats(ports)
    for label, s in serials.items():
        threading.Thread(target=reader, args=(label, s, q, stats, stop), daemon=True).start()

    def consume() -> None:
        while not stop.is_set():
            try:
                rec = q.get(timeout=0.5)
            except queue.Empty:
                continue
            engine.add(rec)
            if out_q is not None:
                out_q.put(rec)

    def ticker() -> None:
        while not stop.is_set():
            engine.tick()
            time.sleep(TICK_S)

    threading.Thread(target=consume, daemon=True).start()
    threading.Thread(target=ticker, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", args.http), make_handler(engine))
    server.daemon_threads = True
    print(f"Live dial on http://127.0.0.1:{args.http}  (Ctrl+C stops)" + (f"; journaling to {args.out}" if args.out else ""))
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        stop.set()
        server.server_close()
        if out_q is not None:
            out_q.put(None)
            time.sleep(0.5)
            out_f.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
