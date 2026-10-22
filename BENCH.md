# R0 bench setup

Step by step, for the person at the bench. The hardware facts come from the Potluck session (2026-10-22, also in Potluck's `captures/for-s3-rotator-2026-10-22.md`); "advice" marks what is judgement, not measurement.

**Ready:** the RSSI tee is live on all three boards: image fc2ff21-86d6fd, firmware counter 71, package 107. A (COM3) and B (COM4) print the readings.

**Today's desk placement can't be used.** A 20-second listen on 2026-10-22 read B←C −8 dBm, A↔B −17 dBm and A←C −27 dBm (`captures/check-2026-10-22-desk.jsonl`). That's far too strong to measure: at desk range the receivers saturate. C has to move away from A and B.

**The layout** (a to-scale floor plan of the house was drawn for this, and is kept private). Positions are in metres from the house's left and top walls:

| | from the left wall | from the top wall | note |
|---|---|---|---|
| A | 8.2 m | 3.6 m | at the front of the PC, its wires still plugged in |
| B | 8.4 m | 2.55 m | on USB to the PC, halfway between the PC and the homelab PC (about 1 m from each) |
| C | 5.9 m | 3.5 m | on the pivot, on wall power |
| you, during holds | 2.2 m | 6.8 m | outside every line between the boards |

The links come out at A↔C 2.3 m, B↔C 2.7 m and A↔B 1.1 m. **Why B is on a cable (decided 2026-10-22):** a cable gives every frame B hears, with tools that already work. The hub's radio telemetry today comes only every 10 s, and passive-sensor's 2 s averages didn't arrive when asked for. Radio telemetry (which would add C's own view of A and B) is for a later R0b, with Potluck's session. The spots are read off your sketch (house 9 × 18 m), so they're good to about ±0.5 m; the distances are what matter, so check them with a tape measure.

## 1. Before you move anything

Photograph or mark where **C** sits now and which way it faces. After R0 it goes back exactly there, for passive-sensor.

## 2. Place A and B (the recorders)

- **A goes to the front of the PC.** Its three wires go to the PC's CP2102 (the hub's link to the whole cell, COM6, which nobody opens), and it records on COM3. Move it gently so those wires stay plugged in.
- **B goes to its spot in the table above,** halfway between the PC and the homelab PC, about 1 m from A, on USB to the PC (COM4). Put it on something, not on the floor.
- Mark which way A and B face, and don't touch them until R0 is over.

## 3. Build the pivot base for C

The board (ESP32-S3-DevKitC-1) is 62.7 × 25.4 mm.

- **Find the antenna.** It's the end of the board away from the two USB-C sockets: the part of the module beyond its metal can, about 25–30 mm from the board's centre.
- **Pivot under the board's centre**, so C turns the way a knob would. (Advice: a second run with the pivot under the antenna is the fallback if R0 is borderline.)
- **Wood or plastic only.** No metal near the antenna end, including screws. Espressif recommends at least 15 mm of clear space around the antenna in every direction. The antenna sweeps a circle of about 30 mm radius as C turns, so keep roughly 45 mm around the pivot clear at board height.
- **Height:** C at least 10 cm above the desk or floor it stands on (advice).
- **Power bank and a short cable at the USB end, under the board,** strapped to the turning part so they turn with C. Neither may come within 15 mm of the antenna's circle.
- **Marks:** 0/90/180/270° on the fixed part, clockwise seen from above, and a pointer on the turning part. Choose what 0° means (suggestion: the antenna end pointing at A) and write it down.

## 4. Choose the power bank

C needs a steady 5 V supply. Its radio never sleeps, so it draws a steady current. Espressif's datasheet gives 95 mA receiving and 355 mA peaks transmitting at 3.3 V; the draw at the USB plug has not been measured (Potluck's guess is 100–200 mA). Many banks switch off at low current, and in Potluck's only power-bank run the bank rebooted the board every 20–30 s.

- Prefer a bank with an always-on or low-current mode.
- If you have an inline USB meter, note the mA after a minute.
- The real test is step 6: 30 minutes with no restart.

## 5. Place C

- **At its spot in the table above:** 2–3 m from both A and B, with nothing in the way of either, and **not on the line between A and B** (the three form a triangle).
- Not behind the PC tower or a wall. The spot keeps about 1 m from the bathroom wall's corner.
- **On wall power** (chosen 2026-10-22 instead of a power bank): run the cable straight down through the pivot's middle, or tape it under the board with slack below, so a turn twists it rather than swinging it round.
- Expect about −40 to −50 dBm on C's links (Potluck's advice, from a 2.5 m reading of −44/−48 dBm).

## 6. Check placement and power bank in one go (30 minutes)

C on its bank on the pivot at 0°, hands off, nobody walking between the boards:

```powershell
<IDF python> tools\rot_capture.py --port A=COM3 --port B=COM4 --out captures\check-bank.jsonl --listen-s 1800
```

At the end it lists every link. You want:
- no **TOO STRONG** next to any link (all below −25 dBm). If A↔B is still too strong, move B further away;
- about 10 or more frames/s on each;
- **Restarts: none.** A restart means the bank isn't fit: try another one.

For a quick placement check, `--listen-s 60` does the same in a minute.

## 7. Run R0 (about 23 minutes)

```powershell
<IDF python> tools\rot_capture.py --port A=COM3 --port B=COM4 --out captures\r0-2026-10-23.jsonl
```

For each step:
1. Turn C as the prompt says.
2. Press Enter at the PC.
3. Walk to the stand spot within the 10 s the program waits.
4. Stay there until the beep, 30 s later.

You sit at the PC right next to A, so staying at the desk would put you on A's links. A person between two boards drops a link by several dB, more than the effect R0 looks for.
- If someone walks past during a hold, type `x` at the next prompt to throw that hold away.
- passive-sensor's LEDs and Home Assistant will show "present" the whole time. That's expected and harmless.

Then tell Claude in the s3-rotator session; it runs the analysis.

## 8. Afterwards

1. Put C back where step 1 recorded, facing the same way, on its usual power.
2. Tell Claude. It asks Potluck to roll the tee back, which restarts all three boards. **Leave the room for about 2 minutes** while they restart, so passive-sensor relearns an empty room.
