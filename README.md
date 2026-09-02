# RadioBeat Monitor

Contactless breathing, heart rate and irregular-rhythm monitoring using ordinary
WiFi signals — no wearable, no camera, no contact with the person at all.

**[Live demo →](https://adhvi29.github.io/radiobeat-monitor/)**

The demo runs on synthetic data so you can see the interface without hardware.
With the sensors attached it shows real measurements from the room.

> **Not a medical device.** This is a student research prototype. It cannot
> diagnose atrial fibrillation or any other condition and must never be used to
> make a medical decision. Anyone worried about their heart rhythm should see a
> doctor.

---

## How it works

Two cheap ESP32 boards talk to each other over WiFi. As a chest rises and falls,
it changes the radio waves travelling between them by a tiny amount. That change
shows up in the **Channel State Information** (CSI) the radio already computes
for every packet — 64 subcarriers of amplitude and phase, about 40 times a
second.

The processing chain, in order:

| Stage | Why it is there |
|---|---|
| Phase sanitisation | Removes the per-packet offset and slope the radio adds |
| Static clutter removal | Walls and furniture reflect far more strongly than a chest; subtracting the average reflection leaves only what moved |
| Uniform resampling | Packets arrive unevenly; the ESP32's own microsecond clock is used to put them on a regular grid |
| SNR-weighted subcarrier selection | Averaging all 64 buries the signal — the responsive ones are kept and weighted |
| Harmonic regression | Breathing and its harmonics are fitted and subtracted, which is what separates the heartbeat from a breathing echo |
| Fused estimation | A harmonic-sum spectrum and an autocorrelation must agree before a rate is trusted |
| Rhythm analysis | Beat times → RR intervals → RMSSD, pNN50, entropy, Poincaré |

## Detecting irregular rhythm

Atrial fibrillation shows up as *irregularly irregular* beat timing. The monitor
measures RMSSD, coefficient of variation, pNN50 and Shannon entropy of the RR
intervals, and flags irregularity when at least three cross clinical screening
thresholds.

The hard part is not detecting irregularity — it is **refusing to cry wolf**.
Noisy beat detection produces intervals that look exactly like AFib, so a verdict
is only given when:

- the beat-derived heart rate agrees within 12% with the independent spectral estimate
- signal quality is adequate and there is no movement
- at least 12 beats were found
- RMSSD and CV stay within physiologically possible bounds
- the pattern persists across **five consecutive windows**

Fail any of those and it says *"cannot assess"* with the reason, never
*"atrial fibrillation"*.

## Interference from fans and air conditioning

A fan is the biggest real-world threat: an oscillating fan sweeps at roughly
0.1–0.25 Hz, right inside the breathing band.

Machines identify themselves without any calibration step. A fan appears in
almost every window at a rock-steady frequency and constant amplitude; a person's
rate always wanders by a few percent. Anything that steady is treated as
machinery and regressed out of the signal along with its harmonics — so the fan
is *removed* rather than allowed to win, and the person underneath is recovered.

When a machine harmonic lands within 0.02 Hz of the pulse the two are
mathematically inseparable over a 30-second window, and the interface says so
instead of reporting a confident wrong number.

---

## Running it

Static demo, no hardware:

```bash
cd docs && python -m http.server 8000
```

With sensors attached:

```bash
pip install pyserial numpy scipy
python serve.py          # http://localhost:8770
```

`serve.py` reads CSI from the receiver over USB, runs the analysis, streams
results to the browser, and records sessions to a local SQLite file. It auto-
detects the serial baud rate and falls back to demo data when no board is found.

To reach it from a phone, use the computer's LAN address on the same network
(e.g. `http://192.168.1.20:8770`).

### Hardware

- **Transmitter** — any ESP32 dev board, acting as a WiFi access point
- **Receiver** — ESP32 with CSI enabled, connected over USB

Firmware lives in the main RadioBeat project.

### Optional accounts

Sign-in and cloud-synced session summaries are available via Supabase but are
off by default — the monitor works fully without an account, storing everything
locally. See [SUPABASE_SETUP.md](SUPABASE_SETUP.md).

---

## Honest limitations

- **Heartbeat is at the edge of what one antenna can do.** Breathing is reliable;
  the pulse is clearest when the person is still and clearest of all during a
  breath-hold.
- **Two people breathing at the same rate merge into one peak** and cannot be
  separated with a single antenna.
- **Loose bedsheets moving in airflow** are broadband and random, so unlike a fan
  they cannot be learned and filtered out.
- Position matters: the person should be between the two boards, ideally within
  about a metre.

## Licence

MIT
