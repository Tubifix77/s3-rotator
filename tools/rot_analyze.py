"""R0 analysis: does turning R on the spot move its radio links, and can the turn's direction be told back?

    python tools/rot_analyze.py captures/r0.jsonl --rotor <R> [--rotor ...] [--out report.md]
                                [--csv holds.csv] [--min-frames 20] [--perms 500] [--seed 1]

Input: rot_capture.py's file (or rot_sim.py's). Only heartbeat beacons are used (--all-kinds for every
frame: probes, HELLOs and app traffic may go at other rates). Frames that were relayed (rel=1) or heard
off the home channel (away=1) are left out. A link is "<receiving port><-<sending peer, hex>".
A restart is seen when a recorder's clock (us) or a sender's beacon count (seq) goes back without
wrapping; a hold with a restart inside it is left out, and every restart is listed.

--rotor names R: a port label (the links R receives on, when R is cabled) or a peer id (the links others
hear R on) as the tee prints it (decimal), or as 0x... / node-... in hex. Repeat it for both. Links that
do not involve R are controls: they see the room's drift, never R's orientation. Without --rotor every
link counts as R's and drift is not checked.

Per hold and link: frames, mean and standard deviation of RSSI, channel. Then four questions:

  A. Per link: do the positions' means differ by more than the same position's spread across rounds?
     (spread of position means / pooled within-position SD, and the one-way F)
  B. Position: leave-one-round-out nearest-centroid classification of the hold means on R's links.
     "raw" uses them as measured; "centred" first removes each round's own average, which takes slow
     drift out but needs the whole round, so it is a diagnostic, not something a live dial could do.
  C. Turn versus handling: the RMS change of R's links between consecutive holds, for real turns
     against lifts and full turns (handled, not turned), and against the control links' change.
  D. Direction: knowing where R was, the change of R's links is matched against the change expected
     for each possible turn (from centroids of the other rounds). Change cancels slow drift, so this is
     what a live dial would do. "tracked" chains it from the baseline's known 0°, using its own
     estimates, as a dial would.

B and D get a permutation p: the same analysis with the positions shuffled within each round.
The verdict applies the criteria fixed in README.md before the first recording (constants below).
Every number comes from the given file only.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import gzip
import json
import math
import random
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

# R0's criteria, fixed before the first recording (README.md, "Criteria"). Change only with the reason recorded there.
T1_TURN_FRACTION = 0.9   # share of real turns whose change must clear the threshold below
T1_CONTROL_FACTOR = 2.0  # threshold: the largest no-turn change, and at least this x the control links' median change
T2_ACCURACY = 0.8        # position: leave-one-round-out accuracy ...
T3_ACCURACY = 0.9        # direction: share of quarter turns whose direction is called right ...
P_MAX = 0.01             # ... each with a permutation p below this
MIN_ROUNDS = 3
MIN_NO_TURN = 3
LINK_COVERAGE = 0.9      # a link is a feature when it has enough frames in this share of the holds
SATURATION_DBM = -25.0   # Potluck saw desk-range links saturate at -4 to -20 dBm (its M0-LOG)


@dataclass
class LinkStat:
    n: int
    mean: float
    std: float
    ch: int


@dataclass
class Hold:
    id: int
    round: int
    kind: str
    frm: int
    to: int
    t0: float
    t1: float
    stats: dict[str, LinkStat] = field(default_factory=dict)


@dataclass
class Capture:
    links: dict[str, tuple[str, int]]  # link -> (port, peer)
    series: dict[str, tuple[list[float], list[int], list[int]]]  # link -> host_ts, rssi, ch in time order
    holds: list[Hold]
    discarded: list[int]
    counts: Counter
    meta: dict | None
    radio: dict[str, Counter]  # link -> (sig, rate) -> frames
    resets: list[tuple[float, str, str]]  # host_ts, node, what


def link_name(port: str, peer: int) -> str:
    return f"{port}<-{peer:04x}"


def wrapped(last: int, now: int, modulus: int) -> bool:
    """A counter that went back by wrapping round (last near the top, now near zero), not by a restart."""
    return last > modulus - modulus // 64 and now < modulus // 64


def load(path: str, all_kinds: bool = False) -> Capture:
    raw: dict[str, list[tuple[float, int, int]]] = {}
    links: dict[str, tuple[str, int]] = {}
    radio: dict[str, Counter] = {}
    resets: list[tuple[float, str, str]] = []
    last_us: dict[str, int] = {}
    last_seq: dict[str, int] = {}
    counts: Counter = Counter()
    starts: dict[int, dict] = {}
    ends: dict[int, dict] = {}
    discarded: set[int] = set()
    meta = None
    with (gzip.open(path, "rt", encoding="utf-8") if str(path).endswith(".gz") else open(path, encoding="utf-8")) as f:
        for text in f:
            if not text.strip():
                continue
            try:
                rec = json.loads(text)
            except json.JSONDecodeError:
                counts["bad_records"] += 1
                continue
            if "line" in rec:
                try:
                    fr = json.loads(rec["line"])
                except json.JSONDecodeError:
                    counts["bad_lines"] += 1
                    continue
                if fr.get("t") != "rssi":
                    counts["other_lines"] += 1
                    continue
                port, ts, peer = rec["port"], float(rec["host_ts"]), int(fr["peer"])
                us = fr.get("us")
                if us is not None:
                    if port in last_us and us < last_us[port] and not wrapped(last_us[port], us, 1 << 32):
                        resets.append((ts, port, "recorder restarted (its clock went back)"))
                    last_us[port] = us
                if fr.get("rel"):
                    counts["relayed"] += 1
                elif fr.get("away"):
                    counts["away"] += 1
                elif fr.get("kind") != "beacon" and not all_kinds:
                    counts["not_beacon"] += 1
                else:
                    name = link_name(port, peer)
                    seq = fr.get("seq")
                    if fr.get("kind") == "beacon" and seq is not None:
                        last = last_seq.get(name)
                        if last is not None and seq < last and not wrapped(last, seq, 1 << 16):
                            resets.append((ts, f"{peer:04x}", f"restarted (its beacon count went back, heard on {port})"))
                        last_seq[name] = seq
                    links[name] = (port, peer)
                    raw.setdefault(name, []).append((ts, int(fr["rssi"]), int(fr.get("ch", 0))))
                    radio.setdefault(name, Counter())[(fr.get("sig"), fr.get("rate"))] += 1
                    counts["frames"] += 1
            elif rec.get("t") == "step":
                if rec.get("phase") == "hold_start":
                    starts[rec["id"]] = rec
                elif rec.get("phase") == "hold_end":
                    ends[rec["id"]] = rec
            elif rec.get("t") == "discard" and rec.get("id") is not None:
                discarded.add(rec["id"])
            elif rec.get("t") == "meta":
                meta = rec
    series = {}
    for name, rows in raw.items():
        rows.sort()
        series[name] = ([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows])
    holds = []
    for hid, s in starts.items():
        if hid not in ends:
            counts["unfinished_holds"] += 1
        elif hid in discarded:
            continue
        elif any(s["host_ts"] <= ts <= ends[hid]["host_ts"] for ts, _, _ in resets):
            counts["holds_with_restart"] += 1
        else:
            holds.append(Hold(hid, s["round"], s["kind"], s["from"], s["to"], s["host_ts"], ends[hid]["host_ts"]))
    holds.sort(key=lambda h: h.t0)
    return Capture(links, series, holds, sorted(discarded & set(starts)), counts, meta, radio, resets)


def measure(cap: Capture, min_frames: int) -> None:
    min_frames = max(2, min_frames)
    for h in cap.holds:
        for name, (ts, rssi, ch) in cap.series.items():
            i0, i1 = bisect.bisect_left(ts, h.t0), bisect.bisect_right(ts, h.t1)
            if i1 - i0 < min_frames:
                continue
            vals = rssi[i0:i1]
            h.stats[name] = LinkStat(len(vals), statistics.fmean(vals), statistics.stdev(vals),
                                     Counter(ch[i0:i1]).most_common(1)[0][0])


def parse_peer(tok: str) -> int | None:
    t = tok.strip().lower()
    try:
        if t.startswith("node-"):
            return int(t[5:], 16)
        if t.startswith("0x"):
            return int(t, 16)
        return int(t)
    except ValueError:
        return None


def assign_roles(cap: Capture, tokens: list[str]) -> dict[str, str]:
    if not tokens:
        return {name: "rotor" for name in cap.links}
    ports = {p for p, _ in cap.links.values()}
    peers = {pe for _, pe in cap.links.values()}
    rot_ports, rot_peers = set(), set()
    for tok in tokens:
        if tok in ports:
            rot_ports.add(tok)
            continue
        peer = parse_peer(tok)
        if peer not in peers:
            raise ValueError(f"--rotor {tok!r} is neither a port ({', '.join(sorted(ports))}) nor a peer seen "
                             f"({', '.join(f'{p} = 0x{p:04x}' for p in sorted(peers))})")
        rot_peers.add(peer)
    return {name: "rotor" if port in rot_ports or peer in rot_peers else "control"
            for name, (port, peer) in cap.links.items()}


def separation(holds: list[Hold], link: str) -> dict | None:
    groups: dict[int, list[float]] = {}
    for h in holds:
        if link in h.stats:
            groups.setdefault(h.to, []).append(h.stats[link].mean)
    n, g = sum(len(v) for v in groups.values()), len(groups)
    if g < 2 or n - g < 1:
        return None
    means = {o: statistics.fmean(v) for o, v in groups.items()}
    grand = statistics.fmean(x for v in groups.values() for x in v)
    ssb = sum(len(v) * (means[o] - grand) ** 2 for o, v in groups.items())
    ssw = sum((x - means[o]) ** 2 for o, v in groups.items() for x in v)
    within = math.sqrt(ssw / (n - g))
    spread = max(means.values()) - min(means.values())
    return {"holds": n, "positions": g, "means": means, "spread": spread, "within_sd": within,
            "ratio": spread / within if within > 0 else math.inf,
            "F": (ssb / (g - 1)) / (ssw / (n - g)) if ssw > 0 else math.inf,
            "noise": statistics.median(h.stats[link].std for h in holds if link in h.stats)}


# Items are (round, position, vector) in time order; vectors hold the feature links' means.
Item = tuple[int, int, list[float]]


def items_of(holds: list[Hold], names: list[str]) -> list[Item]:
    return [(h.round, h.to, [h.stats[n].mean for n in names]) for h in holds if all(n in h.stats for n in names)]


def centred(items: list[Item]) -> list[Item]:
    positions = {lab for _, lab, _ in items}
    out: list[Item] = []
    for r in sorted({r for r, _, _ in items if r >= 1}):
        group = [i for i in items if i[0] == r]
        by: dict[int, list[list[float]]] = {}
        for _, lab, v in group:
            by.setdefault(lab, []).append(v)
        if set(by) != positions:
            continue  # a round that misses a position cannot be centred without bias
        dims = len(group[0][2])
        centre = [statistics.fmean(statistics.fmean(v[k] for v in vs) for vs in by.values()) for k in range(dims)]
        out += [(r, lab, [x - c for x, c in zip(v, centre)]) for _, lab, v in group]
    return out


def centroids(items: list[Item]) -> dict[int, list[float]]:
    by: dict[int, list[list[float]]] = {}
    for _, lab, v in items:
        by.setdefault(lab, []).append(v)
    return {lab: [statistics.fmean(col) for col in zip(*vs)] for lab, vs in by.items()}


def sqdist(a: list[float], b: list[float]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b))


def loro(items: list[Item]) -> tuple[float | None, int, Counter]:
    correct = total = 0
    confusion: Counter = Counter()
    for r in sorted({r for r, _, _ in items if r >= 1}):
        cents = centroids([i for i in items if i[0] != r])
        for _, lab, v in (i for i in items if i[0] == r):
            pred = min(cents, key=lambda o: sqdist(v, cents[o])) if cents else None
            confusion[(lab, pred)] += 1
            total += 1
            correct += pred == lab
    return (correct / total if total else None), total, confusion


def direction(items: list[Item], tracked: bool) -> tuple[float | None, int, Counter, float | None, int]:
    """Infer each step's turn from the change of R's links, knowing (or, tracked, estimating) where R was.

    Returns: accuracy over every step, steps, confusion (actual turn, inferred turn), accuracy over
    quarter turns (90 or 270), quarter turns.
    """
    correct = total = q_correct = q_total = 0
    confusion: Counter = Counter()
    cents_by_round: dict[frozenset, dict[int, list[float]]] = {}
    estimate = items[0][1] if items else None
    for prev, cur in zip(items, items[1:]):
        if cur[0] < 1:
            continue
        skip = frozenset({prev[0], cur[0]})
        if skip not in cents_by_round:
            cents_by_round[skip] = centroids([i for i in items if i[0] not in skip])
        cents = cents_by_round[skip]
        frm = estimate if tracked else prev[1]
        actual = (cur[1] - prev[1]) % 360
        if frm not in cents or not cents:
            inferred_to = None
        else:
            step = [b - a for a, b in zip(prev[2], cur[2])]
            inferred_to = min(cents, key=lambda c: sqdist(step, [y - x for x, y in zip(cents[frm], cents[c])]))
        inferred = None if inferred_to is None else (inferred_to - frm) % 360
        if tracked:
            ok = inferred_to == cur[1]
            estimate = inferred_to if inferred_to is not None else estimate
        else:
            ok = inferred == actual
        confusion[(actual, inferred)] += 1
        total += 1
        correct += ok
        if actual in (90, 270):
            q_total += 1
            q_correct += ok
    return ((correct / total) if total else None, total, confusion,
            (q_correct / q_total) if q_total else None, q_total)


def permute(items: list[Item], rng: random.Random) -> list[Item]:
    labels: dict[int, list[int]] = {}
    for r, lab, _ in items:
        labels.setdefault(r, []).append(lab)
    for labs in labels.values():
        rng.shuffle(labs)
    taken: Counter = Counter()
    out = []
    for r, _, v in items:
        out.append((r, labels[r][taken[r]], v))
        taken[r] += 1
    return out


def perm_p(items: list[Item], score, observed: float, perms: int, seed: int) -> float:
    rng = random.Random(seed)
    ge = sum(1 for _ in range(perms) if (score(permute(items, rng)) or 0.0) >= observed)
    return (ge + 1) / (perms + 1)


def rms(xs: list[float]) -> float | None:
    return math.sqrt(statistics.fmean(x * x for x in xs)) if xs else None


def steps(holds: list[Hold], roles: dict[str, str]) -> list[dict]:
    out = []
    for prev, cur in zip(holds, holds[1:]):
        if cur.kind == "baseline":
            continue
        common = [n for n in cur.stats if n in prev.stats]
        d = {n: cur.stats[n].mean - prev.stats[n].mean for n in common}
        out.append({"id": cur.id, "kind": cur.kind, "from": prev.to, "to": cur.to, "net": (cur.to - prev.to) % 360,
                    "gap_s": cur.t0 - prev.t1, "deltas": d,
                    "rotor_rms": rms([d[n] for n in common if roles[n] == "rotor"]),
                    "control_rms": rms([d[n] for n in common if roles[n] == "control"])})
    return out


def judge_turns(st: list[dict]) -> dict:
    turns = [s for s in st if s["net"] != 0 and s["rotor_rms"] is not None]
    quiet = [s for s in st if s["net"] == 0 and s["rotor_rms"] is not None]
    ctl = [s["control_rms"] for s in st if s["control_rms"] is not None]
    res = {"turns": len(turns), "quiet": len(quiet), "ok": None}
    if not turns or not quiet:
        return res
    handling = max(s["rotor_rms"] for s in quiet)
    drift = T1_CONTROL_FACTOR * statistics.median(ctl) if ctl else None
    threshold = max(handling, drift or 0.0)
    cleared = sum(s["rotor_rms"] > threshold for s in turns)
    res.update(handling_max=handling, control_median=statistics.median(ctl) if ctl else None, threshold=threshold,
               cleared=cleared, fraction=cleared / len(turns), turn_median=statistics.median(s["rotor_rms"] for s in turns),
               turn_min=min(s["rotor_rms"] for s in turns))
    res["ok"] = res["fraction"] >= T1_TURN_FRACTION
    return res


def analyze(path: str, rotor: list[str] = (), min_frames: int = 20, perms: int = 500, seed: int = 1,
            all_kinds: bool = False) -> dict:
    cap = load(path, all_kinds)
    measure(cap, min_frames)
    roles = assign_roles(cap, list(rotor))
    names = sorted(cap.links)
    holds = cap.holds
    cover = {n: sum(n in h.stats for h in holds) / len(holds) if holds else 0.0 for n in names}
    features = [n for n in names if roles[n] == "rotor" and cover[n] >= LINK_COVERAGE]
    items = items_of(holds, features) if features else []

    position = {}
    for variant, its in (("raw", items), ("centred", centred(items))):
        acc, tested, conf = loro(its)
        if acc is None:
            continue
        tested_labels = Counter(lab for r, lab, _ in its if r >= 1)
        position[variant] = {"accuracy": acc, "tested": tested, "confusion": conf,
                             "chance": max(tested_labels.values()) / tested,
                             "p": perm_p(its, lambda x: loro(x)[0], acc, perms, seed)}

    turning = {}
    for variant, tracked in (("known start", False), ("tracked", True)):
        acc, total, conf, q_acc, q_total = direction(items, tracked)
        if acc is None:
            continue
        turning[variant] = {"accuracy": acc, "steps": total, "confusion": conf, "quarter_accuracy": q_acc,
                            "quarter_turns": q_total,
                            "p": perm_p(items, lambda x, t=tracked: direction(x, t)[3], q_acc, perms, seed)
                            if q_acc is not None else None}

    st = steps(holds, roles)
    t1 = judge_turns(st)
    rounds = len({h.round for h in holds if h.round >= 1})
    channels = {n: sorted({h.stats[n].ch for h in holds if n in h.stats}) for n in names}
    result = {
        "path": path, "counts": dict(cap.counts), "holds": holds, "discarded": cap.discarded, "roles": roles,
        "links": names, "cover": cover, "features": features, "channels": channels,
        "frames": {n: len(cap.series[n][0]) for n in names},
        "level": {n: statistics.fmean(cap.series[n][1]) for n in names},
        "radio": cap.radio, "resets": cap.resets,
        "separation": {n: separation(holds, n) for n in names},
        "position": position, "direction": turning, "steps": st, "t1": t1, "rounds": rounds,
        "has_control": any(r == "control" for r in roles.values()), "perms": perms,
    }
    result["verdict"] = verdict(result)
    return result


def passes(block: dict | None, acc_key: str, threshold: float) -> bool:
    return bool(block) and block.get(acc_key) is not None and block[acc_key] >= threshold and \
        block.get("p") is not None and block["p"] < P_MAX


def verdict(r: dict) -> list[str]:
    t1 = r["t1"]
    if r["rounds"] < MIN_ROUNDS or t1["quiet"] < MIN_NO_TURN or not r["features"]:
        return [f"NOT ENOUGH DATA to judge: {r['rounds']} rounds (need {MIN_ROUNDS}), {t1['quiet']} no-turn "
                f"controls (need {MIN_NO_TURN}), {len(r['features'])} usable links of R."]
    lines = []
    if not r["has_control"]:
        lines.append("No --rotor given, so there are no control links: the room's drift is not checked.")
    if not t1["ok"]:
        lines.append(f"KILL (T1 fails): only {t1['cleared']} of {t1['turns']} turns moved R's links more than "
                     f"handling without turning or the room's drift ({t1['threshold']:.2f} dB). Turning R is "
                     "not seen in this setup.")
        return lines
    lines.append(f"T1 passes: {t1['cleared']} of {t1['turns']} turns cleared {t1['threshold']:.2f} dB, "
                 "the most that handling without turning (or the room) moved R's links.")
    d_known, d_tracked = r["direction"].get("known start"), r["direction"].get("tracked")
    if passes(d_tracked, "quarter_accuracy", T3_ACCURACY):
        lines.append("T3 passes, tracked: from the baseline alone, the dial followed R and told clockwise from "
                     "counter-clockwise. This is a working direction sensor in this setup.")
    elif passes(d_known, "quarter_accuracy", T3_ACCURACY):
        lines.append("T3 passes only with the start position known: direction is in the signal, but chained "
                     "tracking loses its place. Needs a way to re-anchor (a home position).")
    else:
        lines.append("T3 fails: a turn is seen but its direction is not told reliably.")
    pos_raw, pos_c = r["position"].get("raw"), r["position"].get("centred")
    if passes(pos_raw, "accuracy", T2_ACCURACY):
        lines.append("T2 passes: absolute position is told back across the session.")
    elif passes(pos_c, "accuracy", T2_ACCURACY):
        lines.append("T2 passes only centred: positions are distinct, but drift moves them over the session.")
    else:
        lines.append("T2 fails: absolute position is not told back.")
    return lines


def fmt(x, nd=2) -> str:
    if x is None:
        return "-"
    if isinstance(x, float) and math.isinf(x):
        return "inf"
    return f"{x:.{nd}f}" if isinstance(x, float) else str(x)


def confusion_table(conf: Counter, labels: list, title_row: str, title_col: str) -> list[str]:
    cols = sorted({p for _, p in conf if p is not None}.union(labels), key=lambda v: (v is None, v)) + \
        ([None] if any(p is None for _, p in conf) else [])
    out = [f"| {title_row} \\ {title_col} | " + " | ".join("none" if c is None else str(c) for c in cols) + " |",
           "|---" * (len(cols) + 1) + "|"]
    for lab in labels:
        out.append(f"| {lab} | " + " | ".join(str(conf.get((lab, c), 0) or "") for c in cols) + " |")
    return out


def render(r: dict) -> str:
    c = r["counts"]
    out = ["# R0 analysis", "", f"Every number here is computed from `{r['path']}` only.", "", "## Verdict", ""]
    out += [f"- {line}" for line in r["verdict"]]
    out += ["", "## Input", "",
            f"- frames used: {c.get('frames', 0)}; left out: {c.get('relayed', 0)} relayed, {c.get('away', 0)} off "
            f"home channel, {c.get('not_beacon', 0)} not heartbeat beacons, "
            f"{c.get('other_lines', 0) + c.get('bad_lines', 0)} other or bad lines",
            f"- holds: {len(r['holds'])} in {r['rounds']} rounds; discarded: {len(r['discarded'])}; "
            f"left out for a restart during the hold: {c.get('holds_with_restart', 0)}; "
            f"unfinished: {c.get('unfinished_holds', 0)}",
            f"- restarts seen: {len(r['resets'])}" + "".join(f"\n  - {node} {what} at {time.strftime('%H:%M:%S', time.localtime(ts))}"
                                                            for ts, node, what in r["resets"]),
            f"- R's links used as features: {', '.join(r['features']) or 'none'}", "",
            "| link | role | frames | mean dBm | in holds | channels | sig/rate |", "|---|---|---|---|---|---|---|"]
    for n in r["links"]:
        chs = r["channels"][n]
        rates = ", ".join(f"{s}/{rt}: {k}" for (s, rt), k in r["radio"][n].most_common())
        level = r["level"][n]
        out.append(f"| {n} | {r['roles'][n]} | {r['frames'][n]} | {level:.1f}"
                   f"{' (near saturation: move the boards apart)' if level > SATURATION_DBM else ''} | "
                   f"{r['cover'][n]:.0%} | {', '.join(map(str, chs))}"
                   f"{' (RSSI differs per channel!)' if len(chs) > 1 else ''} | {rates} |")

    out += ["", "## A. Per link: positions against their own repeatability", "",
            "Spread: the largest difference between two positions' mean RSSI. Within SD: one position's spread "
            "across rounds (drift and re-seating included). Noise: the median SD inside one hold.", "",
            "| link | role | spread dB | within SD dB | ratio | F | noise dB | means per position |",
            "|---|---|---|---|---|---|---|---|"]
    for n in sorted(r["links"], key=lambda n: (r["roles"][n] != "rotor", n)):
        s = r["separation"][n]
        if s is None:
            out.append(f"| {n} | {r['roles'][n]} | not enough holds | | | | | |")
            continue
        means = ", ".join(f"{o}°: {m:.1f}" for o, m in sorted(s["means"].items()))
        out.append(f"| {n} | {r['roles'][n]} | {fmt(s['spread'])} | {fmt(s['within_sd'])} | {fmt(s['ratio'], 1)} | "
                   f"{fmt(s['F'], 1)} | {fmt(s['noise'])} | {means} |")

    out += ["", "## B. Position, leave one round out", ""]
    if not r["position"]:
        out.append("Not computed: no usable links of R, or no rounds.")
    for variant, b in r["position"].items():
        labels = sorted({lab for lab, _ in b["confusion"]})
        out += [f"**{variant}**: {b['accuracy']:.0%} of {b['tested']} holds (majority guess {b['chance']:.0%}), "
                f"permutation p = {b['p']:.3f} ({r['perms']} shuffles)", ""]
        out += confusion_table(b["confusion"], labels, "actual", "called") + [""]

    out += ["## C. Turn against handling", "",
            "RMS change of R's links between consecutive holds; control: the same for links without R.", ""]
    t1 = r["t1"]
    if t1["ok"] is None:
        out.append(f"Not computed: {t1['turns']} turns, {t1['quiet']} no-turn controls.")
    else:
        out += [f"- no-turn controls (lift, full turn): largest {fmt(t1['handling_max'])} dB over {t1['quiet']}",
                f"- control links: median {fmt(t1['control_median'])} dB",
                f"- turns: smallest {fmt(t1['turn_min'])} dB, median {fmt(t1['turn_median'])} dB over {t1['turns']}",
                f"- threshold {fmt(t1['threshold'])} dB, cleared by {t1['cleared']} of {t1['turns']} "
                f"({t1['fraction']:.0%}; T1 needs {T1_TURN_FRACTION:.0%})"]
    out += ["", "| step | kind | from | to | turn | gap s | R's links dB | controls dB |",
            "|---|---|---|---|---|---|---|---|"]
    for s in r["steps"]:
        out.append(f"| {s['id']} | {s['kind']} | {s['from']} | {s['to']} | {s['net']} | {s['gap_s']:.0f} | "
                   f"{fmt(s['rotor_rms'])} | {fmt(s['control_rms'])} |")

    out += ["", "## D. Direction", "",
            "Turn in degrees clockwise: 90 is a quarter turn clockwise, 270 a quarter turn counter-clockwise, "
            "0 no turn. 'known start' is told where R was; 'tracked' only knows the baseline's position.", ""]
    if not r["direction"]:
        out.append("Not computed: no usable links of R.")
    for variant, d in r["direction"].items():
        out += [f"**{variant}**: every step {d['accuracy']:.0%} of {d['steps']}; quarter turns "
                f"{fmt(d['quarter_accuracy'] * 100 if d['quarter_accuracy'] is not None else None, 0)}% of "
                f"{d['quarter_turns']}, permutation p = {fmt(d['p'], 3)}", ""]
        labels = sorted({a for a, _ in d["confusion"]})
        out += confusion_table(d["confusion"], labels, "actual turn", "inferred") + [""]
    return "\n".join(out) + "\n"


def write_csv(r: dict, path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["hold", "round", "kind", "from", "to", "t0", "t1", "link", "role", "frames", "mean_dbm", "sd_db", "ch"])
        for h in r["holds"]:
            for n in r["links"]:
                s = h.stats.get(n)
                if s:
                    w.writerow([h.id, h.round, h.kind, h.frm, h.to, f"{h.t0:.3f}", f"{h.t1:.3f}", n, r["roles"][n],
                                s.n, f"{s.mean:.3f}", f"{s.std:.3f}", s.ch])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="R0 analysis of a rot_capture.py recording.")
    ap.add_argument("capture")
    ap.add_argument("--rotor", action="append", default=[], help="R as a port label or peer id; repeatable")
    ap.add_argument("--out", help="write the report here as well (markdown)")
    ap.add_argument("--csv", help="per hold and link statistics")
    ap.add_argument("--min-frames", type=int, default=20, help="fewer frames than this in a hold: link missing there")
    ap.add_argument("--perms", type=int, default=500)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--all-kinds", action="store_true",
                    help="use every frame from a peer, not only its heartbeat beacons")
    args = ap.parse_args(argv)
    try:
        r = analyze(args.capture, args.rotor, args.min_frames, args.perms, args.seed, args.all_kinds)
    except ValueError as e:
        raise SystemExit(str(e))
    text = render(r)
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    if args.csv:
        write_csv(r, args.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
