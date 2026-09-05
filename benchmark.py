"""
RadioBeat accuracy benchmark.

Generates physically-grounded synthetic CSI with a KNOWN heart and breathing
rate, pushes it through the real analysis pipeline, and reports the error.

Why this exists: live readings can only ever be compared against a reference
pulse a human has to measure. This measures the algorithm itself, so a change
can be shown to help or hurt before it ever reaches hardware.

    python benchmark.py            # full sweep
    python benchmark.py quick      # one condition
"""
import sys
import time
import numpy as np

import radiobeat_core as core

# --- physical constants -------------------------------------------------------
LAMBDA = 0.125          # 2.4 GHz wavelength, metres
D_BREATH = 0.0050       # chest displacement from breathing, ~5 mm
D_HEART = 0.00045       # from the heartbeat, ~0.45 mm -- an order smaller
N_SUB = 64
FS_PKT = 42.0           # packets/sec, matching the real receiver at 115200 baud


def synth(hr_bpm, br_pm, seconds=45.0, snr_db=12.0, seed=0,
          drift=True, agc=True):
    """Build CSI the way the radio would actually see it.

    A chest moving by d(t) changes the reflected path by 2*d(t), which rotates
    that path's phase by 4*pi*d/lambda. Breathing moves ~5 mm and the heartbeat
    ~0.45 mm, so the heart term is roughly 11x smaller before any noise -- which
    is the whole difficulty of the problem, and the reason this is worth
    simulating honestly rather than adding a clean sine wave.
    """
    rng = np.random.default_rng(seed)
    n = int(seconds * FS_PKT)
    t = np.arange(n) / FS_PKT

    f_br, f_hr = br_pm / 60.0, hr_bpm / 60.0
    if drift:                                   # real rates wander a few percent
        f_br_t = f_br * (1 + 0.04 * np.sin(2 * np.pi * 0.011 * t))
        f_hr_t = f_hr * (1 + 0.03 * np.sin(2 * np.pi * 0.017 * t))
    else:
        f_br_t = np.full(n, f_br)
        f_hr_t = np.full(n, f_hr)

    # phase-integrate: sin(2*pi*f(t)*t) would be a chirp, not a wandering tone
    ph_br = 2 * np.pi * np.cumsum(f_br_t) / FS_PKT
    ph_hr = 2 * np.pi * np.cumsum(f_hr_t) / FS_PKT
    disp = D_BREATH * np.sin(ph_br) + D_HEART * np.sin(ph_hr)
    rot = np.exp(1j * 4 * np.pi * disp / LAMBDA)          # dynamic path rotation

    Z = np.empty((n, N_SUB), dtype=complex)
    for k in range(N_SUB):
        # static clutter dominates; a fraction of the energy is the moving path
        clutter = (rng.normal(0, 1) + 1j * rng.normal(0, 1)) * 3.0
        frac = 0.10 + 0.25 * rng.random()                 # per-subcarrier coupling
        dyn = abs(clutter) * frac * np.exp(1j * rng.uniform(0, 2 * np.pi))
        Z[:, k] = clutter + dyn * rot

    sig = np.abs(Z).std()
    Z += (rng.normal(0, 1, Z.shape) + 1j * rng.normal(0, 1, Z.shape)) \
        * sig * 10 ** (-snr_db / 20)

    if agc:                                     # the radio rescales each packet
        Z *= (1.0 + 0.35 * rng.standard_normal((n, 1)))

    # guard/null subcarriers, exactly as the hardware reports them
    for k in list(range(0, 4)) + [32] + list(range(60, 64)):
        Z[:, k] = 0

    # the receiver sends int8 I/Q, so quantise -- this noise floor is real
    scale = 110.0 / np.abs(Z).max()
    Z = (np.round(Z.real * scale).clip(-127, 127)
         + 1j * np.round(Z.imag * scale).clip(-127, 127))

    jitter = rng.normal(0, 0.004, n)            # packets do not arrive evenly
    ts = t + np.cumsum(np.abs(jitter)) * 0.0 + jitter
    return np.sort(ts), Z


def run_once(hr, br, **kw):
    """Feed synthetic packets through the real pipeline and read the result."""
    ts, Z = synth(hr, br, **kw)
    with core.lock:
        core.buf.clear()
        for i in range(len(ts)):
            core.buf.append((float(ts[i]), Z[i]))
    core.hr_track.__init__(tol=0.12, patience=6)
    core.br_track.__init__(tol=0.04, patience=4)
    core.rhythm_gate.__init__()
    core.lines_auto.reset()
    r = core.analyze()
    if r is None:
        return None
    return dict(hr=r["f_h"] * 60.0, br=r["f_b"] * 60.0,
                conf=r["conf_h"], quality=r["quality"])


def sweep():
    print("=" * 68)
    print("  RadioBeat algorithm accuracy — synthetic CSI, known ground truth")
    print("=" * 68)
    conditions = [
        ("easy   (SNR 18 dB)", 18.0),
        ("normal (SNR 12 dB)", 12.0),
        ("hard   (SNR  6 dB)", 6.0),
    ]
    truths = [(62, 14), (75, 16), (88, 18), (101, 13), (55, 11)]

    for label, snr in conditions:
        hr_err, br_err, got = [], [], 0
        for i, (hr, br) in enumerate(truths):
            out = run_once(hr, br, snr_db=snr, seed=100 + i)
            if out is None:
                continue
            got += 1
            hr_err.append(abs(out["hr"] - hr))
            br_err.append(abs(out["br"] - br))
        if not got:
            print("  %-20s no result" % label)
            continue
        print("  %-20s  HR err %5.1f BPM   BR err %4.1f /min   (%d trials)"
              % (label, float(np.mean(hr_err)), float(np.mean(br_err)), got))
    print()
    print("  Breathing should land within ~1/min. The heartbeat is the hard one:")
    print("  its chest movement is ~11x smaller than breathing before any noise.")


def quick():
    out = run_once(72, 15, snr_db=12.0, seed=7)
    print("truth  HR 72.0  BR 15.0")
    if out is None:
        print("result none")
    else:
        print("got    HR %.1f  BR %.1f   (err %.1f / %.1f)"
              % (out["hr"], out["br"], abs(out["hr"] - 72), abs(out["br"] - 15)))


if __name__ == "__main__":
    time.sleep(0.3)                 # let the serial thread settle if it started
    (quick if "quick" in sys.argv else sweep)()
