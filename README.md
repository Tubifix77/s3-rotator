# s3-rotator

**A negative result: an ESP32-S3 can't be used as a dial just by turning it and reading its own Wi-Fi signal. The radio does carry the board's orientation, but not repeatably enough to use.**

The idea came from a chat about a Home Assistant knob. Leave out the rotary encoder, turn the board itself, and let the other boards notice the change in its radio signal. Put the board on a pivot and its PCB antenna faces the others differently, so the strength (RSSI) of the heartbeats they hear from it should change. If that change is repeatable, the board is its own dial.

The test ran on a three-board [Potluck](https://github.com/Tubifix77/potluck) cell, using Potluck's own ESP-NOW heartbeats as the signal. No firmware was written for it. Two fixed boards (A and B) recorded every heartbeat from the turned board (C), and the A↔B link served as the control for the room's own drift. The pass/fail criteria were fixed before the first recording.

## The result in short

| | |
|---|---|
| Does turning C change its signal? | **Yes, a lot:** about 8 dB per turn, against 0.1 dB of room drift |
| Does a position repeat when C is put back on it? | **No:** 3–4 dB spread, up to 19 dB range at the weakest positions |
| Pre-registered test (turns must beat handling) | **Failed:** 75 % of turns, 90 % needed |
| Best fair position guess (4 positions, chance ≈ 25–31 %) | **73 %** |
| Clockwise vs counter-clockwise from a known start | **67–83 %** (chance 50 %) |
| Live, slow turning by hand | **A fun toy:** the signal draws a loop on the A/B plane, but the hand, the cable and errors that pile up make it unusable |

**Why it fails:** the effect is real, but the error is just as big. Putting the board back on "the same" mark changes the signal by up to several dB. So does the USB cable taking a new lie, and so does a hand or a body nearby. The small changes in position this involves are a fair part of the 12.5 cm wavelength, and that alone changes the signal. At directions where the antenna is weak, a millimetre moves the reading a lot.

**Conclusion (the owner's, 2026-10-23): the theory is dead.** Don't build a dial from Wi-Fi signal strength.
- **BLE isn't expected to be better:** it uses the same antenna and the same 2.4 GHz band.
- **To bring the idea back, attach a real dial:** a rotary encoder, or a magnet and Hall sensor, on the ESP32-S3.
- Getting anything out of the radio would need an automated turntable, the same start position every time, and a model trained on exactly that task.

The rest of this page is the record as it was kept during the experiment.

## Results

**R0, 2026-10-23 00:08–00:35** (`captures/r0-2026-10-23.jsonl.gz`, report `captures/r0-2026-10-23-report.md`). The bench was as in [BENCH.md](BENCH.md), with C on wall power. All 27 holds were kept; nobody else was in the room apart from a cat.

- **Verdict by the fixed criteria: KILL (T1 fails).** 15 of 20 turns cleared 4.25 dB (75 %, needs 90 %). The threshold came from the full-turn control: after one full turn back to 180°, C's links read 4.25 dB different. The five lifts changed them by 0.7–2.6 dB.
- **Turning C clearly changes its signal.** The median change was 7.7 dB for a turn, against 0.08 dB on the control links (A↔B, which never moved by more than 0.26 dB).
- **The positions differ, but don't repeat well.**

  | link | 0° | 90° | 180° | 270° | one position's spread across rounds |
  |---|---|---|---|---|---|
  | A←C | −45.3 | −51.0 | −38.9 | −47.6 | SD 4.4 dB |
  | B←C | −39.2 | −40.6 | −32.8 | −32.6 | SD 2.8 dB |

  Position was told back for 69 % of holds against 31 % by guessing (p = 0.001). 180° and 270° were nearly always right; 0° and 90° were confused.
- **Direction:** tracked from the start, 11 of 12 quarter turns were called right (p = 0.001). From a known start, only 8 of 12 were. Twelve turns is a small sample, and tracking beating the known start points to luck, so this is a hint, not a pass. T3 isn't judged when T1 fails.
- **C restarted once, at 00:09:52, while it was lifted for step 1.** That points to its power cable or connector. No hold was affected, but a cable that can cut C's power when it's handled can also shift it slightly when it's re-seated.

**The cable confound (the owner's point, 2026-10-23):** the full turn mainly moved C's wall-power USB cable into a new lie. Its 4.25 dB is mostly the cable, not C's placement. The same holds for every real turn: the cable turned with C each time. So R0 measured board plus cable, and can't say how much of the ~8 dB is the board's antenna. The lifts, which hardly move the cable, are the cleanest look at re-placement alone: 0.7–2.6 dB, which 19 of 20 turns exceeded. That's an observation after the fact; it doesn't change R0's verdict.

**What it means:** the effect is real and large (turns ~8 dB, the room ~0.1 dB). What fails is repeatability: putting C back at "the same" mark varies by 3–4 dB. An R0b keeps the same criteria and fixes the mechanics: power that can't move or drop (a power bank on the pivot, or the cable through the axis with strain relief), click-stops at each position, and more rounds.

## The live dial (research tool, 2026-10-23)

This is for turning C slowly by hand while watching what the radio does. It is the owner's next step after R0.

```powershell
<IDF python> tools\rot_live.py --port A=COM3 --port B=COM4 --out captures\live-2026-10-23.jsonl
```

Then open http://127.0.0.1:8475. The page shows:
- **The dial:** a needle for "where is it now", and a ring marking where the trend rules think C is.
- **The A/B plane:** each position's calibration as an ellipse, and the live reading as a dot with a trail.
- **The readings** over the last minute.
- **How likely each position is.**
- **Every move the trend rules saw,** with the rule that decided it.

**Every number is a setting on the page:**
- the recent and earlier windows, and the gap between them;
- the settle, no-turn and drift values, and what happens when no rule matches;
- the needle's window, spread weighting and smoothing;
- every row of the rule table;
- each position's calibration, editable or measured with "C is at … now".

R0's values are the defaults, and "Restore R0 defaults" brings them back. With `--out`, every tee line and every settings change is journaled, so a session can be replayed and analysed. Only one program can hold COM3/COM4: stop the live dial (Ctrl+C) before running `rot_capture.py`.

**First smoke test (2026-10-23, `captures/live-2026-10-23-smoke.jsonl`), the owner's observations:**
1. **Movement and the person in the room disturb it most.** The hand on C while turning, and the body nearby.
2. **One bad settled reading can derail the trend rules.** A wrong anchor or a wrong start position makes every following guess wrong, even when the later readings are fine.

Ideas to try:
- let "where is it now" correct the tracked position when it's confident;
- require C to stay settled for longer before judging;
- use a median instead of a mean, so one odd reading counts less.

These aren't built yet.

**In the smoke test, C was turned in 45° steps.** The calibration and the rules only know 0/90/180/270, so a 45° position has no ellipse on the A/B plane. The needle between two positions is a weighted guess, not a measurement. Next: positions should be a setting (e.g. eight at 45°), each calibrated with "C is at … now". The antenna's pattern isn't a straight line between positions, so the 45° readings have to be measured.

**Slow sweeps (2026-10-23, `captures/live-2026-10-23-sweeps.jsonl`):** the owner turned C slowly counter-clockwise from 0° round to 90°. On the A/B plane the live trail went round in a loop: from 0°, past the 270° ellipse, and ended on its left side. So a continuous turn does draw a path in A/B space.

**The owner's conclusion:** a fun toy, but nothing usable without three things:
- an automated turntable, for repeatable, hand-free turns;
- a model trained for this task on that data;
- a known start position every time.

The project stopped here.

## Decisions so far

- **Wi-Fi only.** BLE would share the same antenna and band, so it brings no new orientation information. It would also cost Potluck RAM and radio time. Revisit only if Wi-Fi alone is close but not enough.
- **No CSI.** Potluck's heartbeats go at 1 Mbps 802.11b, and the S3 measures CSI only on OFDM frames. CSI would need a Potluck change (its parked PS-6).
- **No new firmware for R0.** It uses Potluck's `CONFIG_POT_RSSI_TEE`, so it measures exactly the frames a later Potluck app would see.
- **R0 runs at the default TX power (20 dBm, the top).** Potluck is adding `POT! txpower <qdbm>` on a board's console. It can only go lower than today. It isn't saved, so a restart silently returns to 20 dBm. Lowering it waits for an R0b, if R0's links come out near saturation or its result is borderline. R0's criteria aren't changed for it.
- **Direction is the question, not just "was it turned".** Display and graphics wait until the theory has held up.
- **Detect changes, don't rely on fingerprints.** passive-sensor's walk showed one empty room drifting 1.7–3.4 dB over a session, while the noise inside a 2 s window was about ±0.5 dB. So the direction test matches the change between holds, which cancels slow drift.

## The setup (facts from the Potluck session, 2026-10-22)

| board | node | port | role in R0 |
|---|---|---|---|
| A | 0x6300 | COM3 (CH343) | records, fixed. Never turned: it carries the frame link, relays the PC and runs the room fusion |
| B | 0x7368 | COM4 (CH343) | records, fixed. Never turned: it is the extender (house router and hotspot). Its console names the house networks, so only tee lines are kept |
| C | 0x8160 | none (power bank) | **R**, the board that is turned |

- **COM6 (CP2102) is the hub's frame link to A (GPIO17/18), not a console.** The hub holds it, so never open it.
- **The tee** went in as a firmware rollout to all three boards (`CONFIG_POT_RSSI_TEE=y` in the cluster file's `firmware.extra`, through the hub; every board restarted). The image also has `POT! txpower`. Rolling back is the same again; Potluck's session holds `keys/home.json.pre-tee` and rolls back when asked, after R0, as passive-sensor's empty-room restart.
- **passive-sensor runs in the cell** and reads presence from the same RSSIs. Turning C and your hands will read as "present"; its session says that's expected and harmless. It needs one thing, once, after both the rollout and R0 (its session, 2026-10-22):
  - **Before R0**, mark or photograph C's normal spot and orientation.
  - **After R0**, put C back there as closely as practical.
  - **Then, with nobody in the room**, restart all three boards (or re-apply the package) and keep the room empty for about 2 minutes. Each board learns its empty-room baselines in about 60 s after it starts and keeps no saved copy. Without this, a baseline that included you, or a link to C that has shifted by more than 2.5 dB, can hold "present" indefinitely.
  - passive-sensor would like R0's dB shift per orientation on C's links, for its LEDGER.
- **Board:** ESP32-S3-DevKitC-1 N16R8, ESP32-S3-WROOM-1 with the on-module PCB antenna (check that the can doesn't say -1U).

## Physical setup

Step by step in [BENCH.md](BENCH.md), from Potluck's hardware facts.
- **Placement:** A and B at least 50 cm apart at the desk; C on a pivot 2–3 m from both, off the A–B line.
- **The pivot base:** wood or plastic, pivot under the board's centre, the antenna kept 15 mm clear of everything.
- **Power:** a power bank that turns with C.
- **Checks:** a 30-minute listen covers both placement and power bank.

The analysis leaves out any hold in which a board restarted, and flags any link averaging above −25 dBm.

## Running R0

Record with ESP-IDF's Python, which has pyserial. Use **one** process for all ports: parallel console readers once reset board C.

```powershell
<IDF python> tools\rot_capture.py --port A=COM3 --port B=COM4 --out captures\r0-2026-10-23.jsonl
```

The default is a 60 s baseline at 0°, then 5 rounds of four turns plus a lift, then a full turn: 27 holds of 30 s, about 23 minutes. The script prompts each step. Do it, press Enter, and walk to the stand spot ([BENCH.md](BENCH.md)). It waits 10 s, then records for 30 s and beeps. `x` discards the previous hold (someone walked past), `q` stops early; a partial capture can still be analysed.

Then analyse (any Python 3.10+, standard library only):

```powershell
python tools\rot_analyze.py captures\r0-2026-10-23.jsonl.gz --rotor 0x8160 --out captures\r0-2026-10-23-report.md --csv captures\r0-2026-10-23-holds.csv
```

## Criteria

These were fixed before the first recording and are the constants at the top of `tools/rot_analyze.py`. Change them only with the reason written here.

| | question | passes when |
|---|---|---|
| **T1** | Does turning C move its links more than handling it without turning, and more than the room drifts? | ≥ 90 % of real turns change C's links (RMS over A←C, B←C) by more than the largest change after a lift or a full turn, and by more than 2× the control links' (A↔B) median change |
| **T3** | Is the direction told? | ≥ 90 % of quarter turns called correctly (clockwise vs counter-clockwise), permutation p < 0.01. "Tracked" (starting only from the baseline's known 0°) is the real dial; "known start" shows whether the signal has direction at all |
| **T2** | Is the absolute position told? | leave-one-round-out accuracy ≥ 80 %, p < 0.01. "Centred" (each round's average removed) is a diagnostic for drift only |

**Kill:** if T1 fails, the theory doesn't hold in this setup. Fewer than 3 rounds or 3 no-turn controls gives "not enough data", not a verdict.

## Tools

| file | what |
|---|---|
| `tools/rot_plan.py` | the plan: random order per round, a lift per round, a closing full turn |
| `tools/rot_capture.py` | guided recording. Ports opened with DTR/RTS low; only tee lines kept. `--listen-s` records with no plan and reports levels and restarts (placement and power-bank checks) |
| `tools/rot_analyze.py` | the report: per link (A), position (B), turn vs handling (C), direction (D), verdict |
| `tools/rot_live.py`, `tools/rot_live.html` | the live dial: trend rules and "where is it now" on the live tee, every setting adjustable from the page |
| `tools/rot_sim.py` | synthetic captures with a known effect, drift, handling, restarts and frames to filter. For tests only, never evidence |
| `tests/test_rot.py` | `python -m unittest discover -s tests` |

## What would have come next (not built)

If the theory had held, the detector would have become a small C++ core, host-tested against `rot_analyze.py`. It would run inside a Potluck external-component actor fed by `on_rx_sample`, and its position and direction would reach Home Assistant through Potluck's integration. A real-dial knob can use the same path to Home Assistant.
