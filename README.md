# RadioBeat Monitor

The monitoring app for [RadioBeat](https://github.com/adhvi29/radiobeat) —
contactless breathing, heart rate and irregular-rhythm detection from ordinary
WiFi signals.

| | |
|---|---|
| **Live demo** | [adhvi29.github.io/radiobeat-monitor](https://adhvi29.github.io/radiobeat-monitor/) |
| **Project write-up** | [adhvi29.github.io/radiobeat](https://adhvi29.github.io/radiobeat/) |

The demo runs on synthetic data so the interface can be explored without
hardware. With the sensors attached it shows real measurements from the room.

## Running it

```bash
pip install pyserial numpy scipy
python serve.py          # http://localhost:8770
```

`serve.py` reads WiFi Channel State Information from an ESP32 receiver over USB,
runs the signal processing, streams results to the browser and records sessions
locally. With no board connected it serves demo data instead.

Optional Supabase accounts: [SUPABASE_SETUP.md](SUPABASE_SETUP.md).

> **Not a medical device.** A student research prototype. It cannot diagnose
> atrial fibrillation or any other condition. Anyone worried about their heart
> rhythm should see a doctor.

MIT licence.
