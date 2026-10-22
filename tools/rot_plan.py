"""R0's recording plan: the orientations R is turned to, in which order, and the no-turn controls.

Orientation is in degrees, clockwise seen from above, read off marks on R's base. Each round visits
every position once in a random order (so drift is not confounded with position) and includes one
lift: R is lifted and put back at the same orientation, which is handling without rotation. The
session starts with a longer baseline hold and ends with a full turn, which handles and moves R
through every orientation but leaves it where it was.
"""

from __future__ import annotations

import random

POSITIONS = (0, 90, 180, 270)


def make_plan(rounds: int = 5, positions: tuple[int, ...] = POSITIONS, hold_s: float = 30.0,
              baseline_s: float = 60.0, lift: bool = True, full_turn: bool = True,
              seed: int | None = None) -> list[dict]:
    if len(positions) < 2:
        raise ValueError("need at least two positions")
    rng = random.Random(seed)
    steps: list[dict] = []

    def add(round_: int, kind: str, frm: int, to: int, hold: float) -> None:
        steps.append({"id": len(steps), "round": round_, "kind": kind, "from": frm, "to": to, "hold_s": hold})

    cur = positions[0]
    add(0, "baseline", cur, cur, baseline_s)
    for r in range(1, rounds + 1):
        order = list(positions)
        rng.shuffle(order)
        while order[0] == cur:  # the round's first step must be a real turn
            rng.shuffle(order)
        ops: list[tuple[str, int | None]] = [("turn", p) for p in order]
        if lift:
            ops.insert(rng.randint(0, len(ops)), ("lift", None))
        for kind, p in ops:
            to = cur if p is None else p
            add(r, kind, cur, to, hold_s)
            cur = to
    if full_turn:
        add(rounds, "full", cur, cur, hold_s)
    return steps


def describe(step: dict) -> str:
    frm, to, kind = step["from"], step["to"], step["kind"]
    if kind == "baseline":
        return f"Put C on the pivot at {to}°."
    if kind == "lift":
        return f"Lift C about 10 cm off the pivot and put it back at {to}°, without turning it."
    if kind == "full":
        return f"Turn C one full turn clockwise, back to {to}°."
    turn = (to - frm) % 360
    how = {90: "90° clockwise", 180: "half a turn", 270: "90° counter-clockwise"}.get(turn, f"{turn}° clockwise")
    return f"Turn C from {frm}° to {to}° ({how})."
