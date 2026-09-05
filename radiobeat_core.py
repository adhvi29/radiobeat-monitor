"""
RadioBeat core — signal processing shared by the Qt dashboard and the
web server. Importing this starts the serial reader thread.

Pipeline (per update):
  raw I/Q -> phase sanitisation -> STATIC CLUTTER REMOVAL (the walls and
  furniture reflect far more strongly than a chest does, so the average
  reflection is subtracted and only the moving part is kept)
          -> 4 feature families per subcarrier (AGC-free amplitude,
             sanitised phase, dynamic magnitude, clutter projection)
          -> uniform resample on the device's own clock
          -> breathing: SNR-weighted subcarrier selection + PCA
          -> harmonic regression removes breathing + 8 harmonics
          -> heartbeat: SNR-weighted selection + PCA
          -> fused estimate (harmonic-sum spectrum + autocorrelation)
             cross-checked against two half-window estimates
          -> temporal tracker
"""

import sys
import csv
import time
import threading
import serial
import numpy as np
from collections import deque
from scipy.signal import butter, sosfiltfilt, find_peaks
import pyqtgraph as pg
from pyqtgraph.Qt import QtWidgets, QtCore

# ── config ────────────────────────────────────────────────────────────────────
PORT        = "COM4"
BAUD_TRY    = [921600, 115200, 460800]   # auto-detected at startup
N_BYTES     = 128             # LLTF: 64 subcarriers x (imag, real)
N_SUB       = 64
FS          = 40              # uniform grid after resampling (Hz)
WIN_SEC     = 30              # analysis window — 30 s => ~2 BPM resolution
MIN_SEC     = 12              # start showing results after this much data
NFFT        = 4096            # zero-padded for a fine frequency grid

F_LO_B, F_HI_B = 0.12, 0.60   # breathing =  7..36 breaths/min
F_LO_H, F_HI_H = 0.75, 2.50   # heartbeat = 45..150 BPM
N_BREATH_HARM  = 8            # breathing harmonics to regress out
LOG_PATH       = "radiobeat_log.csv"

sos_breath = butter(4, [F_LO_B, F_HI_B], btype="band", fs=FS, output="sos")
sos_heart  = butter(4, [F_LO_H, F_HI_H], btype="band", fs=FS, output="sos")

# ESP32 LLTF layout is subcarriers 0..31 then -32..-1; reorder to ascending.
REORDER = np.r_[32:64, 0:32]
SC_IDX  = np.arange(-32, 32, dtype=float)

buf  = deque(maxlen=12000)    # (device_time_s, complex[64])
lock = threading.Lock()
stats = {"pkts": 0, "rate": 0.0, "bad": 0, "baud": 0, "last_pkt": 0.0}


# ── serial reader thread ──────────────────────────────────────────────────────
def open_serial():
    """Try each baud and keep the one that actually yields CSI3 lines, so a
    firmware baud change never silently blanks the dashboard again."""
    while True:
        for baud in BAUD_TRY:
            try:
                ser = serial.Serial(PORT, baud, timeout=0.5)
                ser.dtr = False
                ser.rts = False
                time.sleep(0.3)
                ser.reset_input_buffer()
            except Exception as e:
                print(f"  cannot open {PORT} @ {baud}: {e}")
                time.sleep(0.5)
                continue
            sniff, t_end = b"", time.time() + 2.5
            while time.time() < t_end:
                sniff += ser.read(ser.in_waiting or 1)
                if b"CSI3," in sniff:
                    print(f"Serial locked: {PORT} @ {baud}")
                    stats["baud"] = baud
                    return ser
            ser.close()
            print(f"  no CSI3 at {baud}, trying next...")
        print("No CSI found at any baud. Is the board powered and flashed?")
        time.sleep(2.0)


def reader():
    ser = open_serial()
    linebuf = b""
    last_report, count = time.monotonic(), 0
    while True:
        try:
            linebuf += ser.read(ser.in_waiting or 1)
            while b"\n" in linebuf:
                raw, linebuf = linebuf.split(b"\n", 1)
                line = raw.decode("ascii", "ignore").strip()
                if not line.startswith("CSI3,"):
                    continue
                parts = line.split(",", 2)
                if len(parts) != 3:
                    continue
                try:
                    ts = int(parts[1]) * 1e-6          # device clock, seconds
                    data = bytes.fromhex(parts[2])
                except ValueError:
                    stats["bad"] += 1
                    continue
                if len(data) != N_BYTES:
                    stats["bad"] += 1
                    continue

                iq = np.frombuffer(data, dtype=np.int8).astype(np.float32)
                z = (iq[1::2] + 1j * iq[0::2])[REORDER]   # ascending subcarrier

                with lock:
                    buf.append((ts, z))
                count += 1
                stats["pkts"] += 1
                stats["last_pkt"] = time.monotonic()

            now = time.monotonic()
            if now - last_report >= 1.0:
                stats["rate"] = count / (now - last_report)
                count, last_report = 0, now
        except Exception as e:
            print("Reader error:", e)
            time.sleep(0.1)


threading.Thread(target=reader, daemon=True).start()


# ── signal-processing helpers ─────────────────────────────────────────────────
def spectra(X):
    """Column-wise magnitude spectrum, Hann-windowed and zero-padded."""
    w = np.hanning(X.shape[0])[:, None]
    S = np.abs(np.fft.rfft((X - X.mean(axis=0)) * w, n=NFFT, axis=0))
    return np.fft.rfftfreq(NFFT, 1.0 / FS), S


def parabolic(f, S, i):
    """Sub-bin peak location via parabolic interpolation."""
    if 1 <= i < len(S) - 1:
        a, b, c = S[i - 1], S[i], S[i + 1]
        den = a - 2 * b + c
        if den != 0:
            return f[i] + 0.5 * (a - c) / den * (f[1] - f[0])
    return f[i]


def band_snr(S, band):
    """Peak-to-median ratio inside a band, per column."""
    return S[band].max(axis=0) / (np.median(S[band], axis=0) + 1e-12)


def select_columns(snr, frac=0.30, floor=8):
    """Keep the most responsive columns and weight them by how good they are,
    instead of averaging every subcarrier (which buries the signal in noise)."""
    n = max(floor, int(frac * len(snr)))
    sel = np.argsort(snr)[::-1][:n]
    w = snr[sel] - snr[sel].min()
    w = w / (w.max() + 1e-12) + 0.15          # never fully discard a keeper
    return sel, w


def combine(M, w):
    """Weighted PCA: the one pattern the good subcarriers agree on."""
    Mw = (M - M.mean(axis=0)) * w
    U, S, _ = np.linalg.svd(Mw, full_matrices=False)
    v = U[:, 0] * S[0]
    if v[np.argmax(np.abs(v))] < 0:
        v = -v
    return v


def remove_periodics(X, t, sources, n_harm=N_BREATH_HARM):
    """Least-squares fit of DC + linear drift + every listed periodic source
    and its harmonics, then subtract. Used for breathing AND for fan/AC lines:
    removing a machine in the time domain is far cleaner than notching the
    spectrum, because it takes out the harmonics too."""
    sources = [f for f in sources if f and f > 0]
    if not sources:
        return X
    cols = [np.ones_like(t), t]
    for f0 in sources:
        for k in range(1, n_harm + 1):
            fk = f0 * k
            if fk >= FS / 2:
                break
            cols.append(np.sin(2 * np.pi * fk * t))
            cols.append(np.cos(2 * np.pi * fk * t))
    A = np.column_stack(cols)
    coef, *_ = np.linalg.lstsq(A, X, rcond=None)
    return X - A @ coef


def top_peaks(f, S, lo, hi, n=4, gap=0.035):
    """The n strongest, well-separated peaks inside a band."""
    band = (f >= lo) & (f <= hi)
    if not band.any():
        return []
    fb, sb = f[band], S[band]
    out = []
    for i in np.argsort(sb)[::-1]:
        if len(out) >= n:
            break
        if sb[i] < 0.20 * sb.max():
            break
        if all(abs(fb[i] - g) >= gap for g, _ in out):
            # Sub-bin refine: raw bin centres jitter by a whole bin width,
            # which would masquerade as frequency wander and hide a machine.
            gi = int(np.argmin(np.abs(f - fb[i])))
            out.append((float(parabolic(f, S, gi)), float(sb[i])))
    return out


class LineTracker:
    """Learns which spectral peaks are machines WITHOUT needing an empty room.

    A fan is present in almost every window and its frequency barely drifts.
    A person comes and goes and their rate always wanders. Tracking every
    candidate peak over time separates the two automatically."""

    MATCH     = 0.020  # Hz — same peak across updates
    MIN_OBS   = 60     # updates before a track may be judged (~30 s)
    STEADY    = 0.025  # frequency wander below this => machine.
                       # Measured: a fan sits near 1.8%, breathing near 2.9%.
    AMP_STEADY = 0.35  # a machine's strength barely varies; breathing depth does
    PRESENCE  = 0.80   # a machine is in essentially every window
    MAX_LINES = 3      # never blame more than this on machinery
    STALE     = 150    # drop a track unseen for this many updates

    def __init__(self):
        self.tracks, self.tick = [], 0

    def reset(self):
        self.tracks, self.tick = [], 0

    def observe(self, peaks):
        """peaks: list of (frequency, amplitude)."""
        self.tick += 1
        for f, a in peaks:
            best, bd = None, 1e9
            for tr in self.tracks:
                d = abs(tr["f"] - f)
                if d < bd:
                    best, bd = tr, d
            if best is not None and bd < self.MATCH:
                best["hist"].append(f)
                best["amp"].append(a)
                best["f"] = 0.9 * best["f"] + 0.1 * f
                best["last"], best["hits"] = self.tick, best["hits"] + 1
            else:
                self.tracks.append(dict(f=f, hist=deque([f], maxlen=300),
                                        amp=deque([a], maxlen=300),
                                        last=self.tick, born=self.tick, hits=1))
        self.tracks = [t for t in self.tracks
                       if self.tick - t["last"] < self.STALE]

    def mechanical(self):
        """A machine is (1) almost always present, (2) frequency-steady and
        (3) amplitude-steady. A person fails at least one of the three."""
        scored = []
        for t in self.tracks:
            if len(t["hist"]) < self.MIN_OBS:
                continue
            age = max(1, self.tick - t["born"])
            fa, aa = np.array(t["hist"]), np.array(t["amp"])
            f_rel = fa.std() / (fa.mean() + 1e-9)
            a_rel = aa.std() / (aa.mean() + 1e-9)
            if (t["hits"] / age >= self.PRESENCE and f_rel < self.STEADY
                    and a_rel < self.AMP_STEADY):
                scored.append((f_rel, float(fa.mean())))
        scored.sort()                       # steadiest first
        return sorted(f for _, f in scored[:self.MAX_LINES])


def harmonic_score(f, S, lo, hi, weights=(1.0, 0.55, 0.30)):
    """A real pulse has harmonics. Scoring f by S(f)+w2*S(2f)+w3*S(3f) stops
    the 2nd harmonic being mistaken for the fundamental."""
    band = (f >= lo) & (f <= hi)
    cand = f[band]
    sc = np.zeros(len(cand))
    for k, w in enumerate(weights, start=1):
        sc += w * np.interp(cand * k, f, S, left=0.0, right=0.0)
    return cand, sc


def autocorr_estimate(x, lo, hi):
    """Independent time-domain estimate — periodicity the FFT cannot fake."""
    x = x - x.mean()
    n = len(x)
    if n < 4:
        return 0.0, 0.0
    ac = np.correlate(x, x, "full")[n - 1:]
    if ac[0] <= 0:
        return 0.0, 0.0
    ac = ac / ac[0]
    lo_lag, hi_lag = max(2, int(FS / hi)), min(n - 2, int(FS / lo))
    if hi_lag <= lo_lag + 2:
        return 0.0, 0.0
    seg = ac[lo_lag:hi_lag + 1]
    i = int(np.argmax(seg))
    lag = lo_lag + i
    if 1 <= i < len(seg) - 1:
        a, b, c = seg[i - 1], seg[i], seg[i + 1]
        den = a - 2 * b + c
        if den != 0:
            lag += 0.5 * (a - c) / den
    return (FS / lag if lag > 0 else 0.0), float(seg[i])


def peak_of(f, S, lo, hi):
    band = (f >= lo) & (f <= hi)
    if not band.any():
        return 0.0, 0.0
    seg = np.where(band, S, 0.0)
    i = int(np.argmax(seg))
    return parabolic(f, S, i), seg[i] / (np.median(S[band]) + 1e-12)


def detect_beats(wave, tu, f_hr):
    """Locate individual heartbeats in the filtered waveform.

    Peak timing is refined between samples by parabolic interpolation — at
    40 Hz one sample is 25 ms, and AFib turns on differences of ~50 ms, so
    whole-sample timing would not be good enough."""
    if f_hr <= 0 or len(wave) < 8:
        return np.array([])
    w = (wave - wave.mean()) / (wave.std() + 1e-9)
    min_gap = max(2, int(0.60 * FS / f_hr))       # beats cannot crowd closer
    idx, _ = find_peaks(w, distance=min_gap, prominence=0.50)
    if len(idx) < 3:
        return np.array([])

    times, amps = [], w[idx]
    for i in idx:
        off = 0.0
        if 1 <= i < len(w) - 1:
            a, b, c = w[i - 1], w[i], w[i + 1]
            den = a - 2 * b + c
            if den != 0:
                off = 0.5 * (a - c) / den
        times.append(tu[i] + off / FS)
    times = np.array(times)

    # Drop spurious extra peaks. A false beat creates an impossibly short
    # interval, which would inflate RMSSD and fake an arrhythmia — so when two
    # beats sit too close together, keep only the stronger one.
    keep = list(range(len(times)))
    while len(keep) > 3:
        rr = np.diff(times[keep])
        med = np.median(rr)
        bad = [j for j, v in enumerate(rr) if v < 0.60 * med]
        if not bad:
            break
        j = bad[0]
        a, b = keep[j], keep[j + 1]
        keep.remove(a if amps[a] < amps[b] else b)
    return times[keep]


def hrv_metrics(beats):
    """Standard heart-rate-variability measures computed from beat times."""
    if len(beats) < 6:
        return None
    rr = np.diff(beats)                            # seconds
    rr = rr[(rr > 0.3) & (rr < 2.0)]               # physiologically possible
    if len(rr) < 5:
        return None
    d = np.diff(rr)
    hist, _ = np.histogram(rr, bins=8)
    pr = hist / hist.sum()
    pr = pr[pr > 0]
    ent = -np.sum(pr * np.log(pr)) / np.log(len(pr)) if len(pr) > 1 else 0.0
    sd1 = np.sqrt(np.var(d) / 2) * 1000
    sd2 = np.sqrt(max(2 * np.var(rr) - np.var(d) / 2, 0)) * 1000
    return dict(rr=rr, n=len(rr),
                mean_hr=60.0 / rr.mean(),
                rmssd=float(np.sqrt(np.mean(d ** 2)) * 1000),   # ms
                cv=float(rr.std() / (rr.mean() + 1e-9)),
                pnn50=float(np.mean(np.abs(d) > 0.05) * 100),   # %
                entropy=float(ent), sd1=float(sd1), sd2=float(sd2))


def classify_rhythm(m, spectral_hr, quality, motion):
    """Score rhythm irregularity — but only when the beats can be trusted.

    The trap: noisy beat detection produces irregular intervals that look
    exactly like AFib. So the beats must first agree with the independent
    spectral heart rate. If they do not, the honest answer is 'cannot tell',
    not 'atrial fibrillation'."""
    if m is None:
        return dict(state="no beats", trust=False, score=0, reasons=[])

    agree = (spectral_hr > 0 and
             abs(m["mean_hr"] - spectral_hr) / spectral_hr < 0.12)
    trust = agree and quality >= 2 and m["n"] >= 12 and not motion
    if not trust:
        why = ("beats disagree with spectrum" if not agree else
               "weak signal" if quality < 2 else
               "too few beats" if m["n"] < 12 else "motion")
        return dict(state=f"cannot assess ({why})", trust=False, score=0,
                    reasons=[])

    # Physiological sanity. Even severe AFib does not produce RMSSD much past
    # ~300 ms or CV past ~0.30 — beyond that the beat detection has failed and
    # the honest answer is "too noisy", never "atrial fibrillation".
    if m["rmssd"] > 300 or m["cv"] > 0.30:
        return dict(state="cannot assess (beat timing too noisy)", trust=False,
                    score=0, reasons=[])

    # Thresholds from the AFib screening literature (RMSSD / pNN50 / entropy).
    reasons = []
    score = 0
    if m["rmssd"] > 90:
        score += 1; reasons.append(f"RMSSD {m['rmssd']:.0f}ms > 90")
    if m["cv"] > 0.12:
        score += 1; reasons.append(f"CV {m['cv']:.2f} > 0.12")
    if m["pnn50"] > 45:
        score += 1; reasons.append(f"pNN50 {m['pnn50']:.0f}% > 45")
    if m["entropy"] > 0.85:
        score += 1; reasons.append(f"entropy {m['entropy']:.2f} > 0.85")

    if score >= 3:
        state = "IRREGULAR — AFib-like"
    elif score == 2:
        state = "borderline"
    else:
        state = "regular rhythm"
    return dict(state=state, trust=True, score=score, reasons=reasons)


class RhythmGate:
    """An arrhythmia persists; measurement noise flickers. A verdict must hold
    for several consecutive windows before it is shown to a user, which is how
    real monitors avoid alarming on a single bad reading."""

    NEED_ON  = 5      # consecutive irregular windows before alerting
    NEED_OFF = 3      # consecutive calm windows before clearing

    def __init__(self):
        self.on = False
        self.hi = 0
        self.lo = 0

    def update(self, trust, score):
        if not trust:
            self.hi = 0
            self.lo += 1
            if self.lo >= self.NEED_OFF:
                self.on = False
            return self.on
        if score >= 3:
            self.hi += 1
            self.lo = 0
            if self.hi >= self.NEED_ON:
                self.on = True
        else:
            self.lo += 1
            self.hi = 0
            if self.lo >= self.NEED_OFF:
                self.on = False
        return self.on


def simulate_rr(kind, seconds=30.0, hr=78.0):
    """Clearly-labelled synthetic rhythm, so the alert path can be shown to an
    audience without waiting for a real arrhythmia."""
    rng = np.random.default_rng()
    base = 60.0 / hr
    rr, total = [], 0.0
    while total < seconds:
        if kind == "afib":
            # irregularly irregular: large random beat-to-beat jumps
            v = base * (1 + rng.normal(0, 0.22))
        else:
            # normal sinus with gentle respiratory modulation
            v = base * (1 + 0.035 * np.sin(2 * np.pi * 0.25 * total)
                        + rng.normal(0, 0.015))
        v = float(np.clip(v, 0.35, 1.8))
        rr.append(v); total += v
    return np.cumsum(np.array(rr))


class RoomProfile:
    """Learns the room's mechanical interference (fans, AC louvers) from an
    empty-room recording, then suppresses those exact frequencies."""

    LINE_BW = 0.030

    def __init__(self):
        self.lines, self.acc, self.n = [], None, 0
        self.active, self.t_end = False, 0.0

    def start(self, seconds=20.0):
        self.acc, self.n, self.lines = None, 0, []
        self.active, self.t_end = True, time.monotonic() + seconds

    def remaining(self):
        return max(0.0, self.t_end - time.monotonic()) if self.active else 0.0

    def feed(self, f, S):
        if not self.active:
            return
        self.acc = S.copy() if self.acc is None else self.acc + S
        self.n += 1
        if time.monotonic() >= self.t_end:
            self._finish(f)

    def _finish(self, f):
        self.active = False
        if self.acc is None or self.n == 0:
            return
        S = self.acc / self.n
        for lo, hi in ((F_LO_B, F_HI_B), (F_LO_H, F_HI_H)):
            band = (f >= lo) & (f <= hi)
            if not band.any():
                continue
            fb, sb = f[band], S[band]
            thr = 3.0 * np.median(sb)
            for i in np.argsort(sb)[::-1][:6]:
                if sb[i] < thr:
                    break
                if all(abs(fb[i] - x) > 0.03 for x in self.lines):
                    self.lines.append(float(fb[i]))
        print("Room profile learned. Interference lines (Hz):",
              [round(x, 3) for x in self.lines] or "none")

    def suppress(self, f, S):
        if not self.lines:
            return S
        out = S.copy()
        for f0 in self.lines:
            out[(f > f0 - self.LINE_BW) & (f < f0 + self.LINE_BW)] = 0.0
        return out


class Wander:
    """A motor holds its rate to a fraction of a percent; a human never does."""

    MECHANICAL = 0.008

    def __init__(self, n=150):
        self.h = deque(maxlen=n)

    def add(self, f):
        if f > 0:
            self.h.append(f)

    def rel(self):
        if len(self.h) < 40:
            return None
        a = np.array(self.h)
        return float(a.std() / (a.mean() + 1e-9))

    def is_mechanical(self):
        r = self.rel()
        return r is not None and r < self.MECHANICAL


class Tracker:
    """Holds a rate steady. A different rate must persist before it is adopted,
    and the displayed value is a median of recent estimates."""

    def __init__(self, tol, patience=6, hist=15):
        self.f, self.tol, self.patience = 0.0, tol, patience
        self.miss, self.locked = 0, False
        self.hist = deque(maxlen=hist)

    def update(self, cand, ok):
        if not ok or cand <= 0:
            self.miss += 1
            if self.miss >= self.patience + 4:
                self.locked = False
        elif self.f <= 0:
            self.f, self.miss = cand, 0
        elif abs(cand - self.f) < self.tol:
            self.f = 0.82 * self.f + 0.18 * cand
            self.miss, self.locked = 0, True
        else:
            self.miss += 1
            if self.miss >= self.patience:
                self.f, self.miss = cand, 0
        if self.f > 0 and self.locked:
            self.hist.append(self.f)
        return self.f

    def value(self):
        v = [x for x in self.hist if x > 0]
        return float(np.median(v)) if v else self.f


room = RoomProfile()
lines_auto = LineTracker()
rhythm_gate = RhythmGate()
sim_mode = {"kind": None}   # None | 'normal' | 'afib'  (clearly labelled demo)
wander_b, wander_h = Wander(), Wander()
hr_track = Tracker(tol=0.12, patience=6)
br_track = Tracker(tol=0.04, patience=4)
hr_history = deque(maxlen=400)

try:
    logfile = open(LOG_PATH, "w", newline="")
    logger = csv.writer(logfile)
    logger.writerow(["time_s", "breaths_per_min", "bpm", "hr_locked", "conf",
                     "rhythm", "rmssd_ms", "cv", "pnn50", "entropy", "n_beats"])
except Exception:
    logfile, logger = None, None
t0 = time.monotonic()


# ── main analysis ─────────────────────────────────────────────────────────────
def analyze():
    with lock:
        snap = list(buf)
    if len(snap) < MIN_SEC * 8:
        return None

    t = np.array([s[0] for s in snap])
    Z = np.array([s[1] for s in snap])

    t = t - t[0]
    keep = t >= (t[-1] - WIN_SEC)
    t, Z = t[keep], Z[keep]
    dur = t[-1] - t[0]
    if dur < MIN_SEC:
        return None
    t = t - t[0]

    amp = np.abs(Z)
    mean_amp = amp.mean(axis=0)
    valid = mean_amp > 0.15 * np.median(mean_amp[mean_amp > 0])
    if valid.sum() < 12:
        return None

    # Reject corrupted packets: those whose overall magnitude is a wild outlier.
    norms = np.linalg.norm(amp, axis=1)
    nmed = np.median(norms)
    nmad = np.median(np.abs(norms - nmed)) + 1e-9
    ok = np.abs(norms - nmed) < 6 * 1.4826 * nmad
    if ok.sum() < 0.5 * len(ok):
        ok[:] = True
    t, Z, amp = t[ok], Z[ok], amp[ok]
    if len(t) < MIN_SEC * 5:
        return None

    # Phase sanitisation: strip per-packet offset and slope across subcarriers.
    ph = np.unwrap(np.angle(Z), axis=1)
    phc = ph - ph.mean(axis=1, keepdims=True)
    x = SC_IDX - SC_IDX.mean()
    slope = (phc * x).sum(axis=1, keepdims=True) / (x @ x)
    phs = phc - slope * x

    # AGC removal: the radio rescales each packet, so divide it out.
    amp_n = amp / (np.linalg.norm(amp, axis=1, keepdims=True) + 1e-9)

    # STATIC CLUTTER REMOVAL. Walls and furniture reflect orders of magnitude
    # more strongly than a moving chest. Subtracting the average reflection
    # leaves only what moved — the single biggest sensitivity win here.
    Zs = amp_n * np.exp(1j * phs)
    clutter = Zs.mean(axis=0, keepdims=True)
    D = Zs - clutter
    dyn = np.abs(D)
    cdir = clutter / (np.abs(clutter) + 1e-12)
    proj = np.real(D * np.conj(cdir))      # displacement-linear component

    X = np.hstack([amp_n[:, valid], phs[:, valid],
                   dyn[:, valid], proj[:, valid]])
    X = (X - X.mean(axis=0)) / (X.std(axis=0) + 1e-9)
    med = np.median(X, axis=0)
    mad = np.median(np.abs(X - med), axis=0) + 1e-9
    X = np.clip(X, med - 5 * 1.4826 * mad, med + 5 * 1.4826 * mad)

    # Uniform resample using the device's own microsecond timestamps.
    m = int(dur * FS)
    if m < MIN_SEC * FS:
        return None
    tu = np.arange(m) / FS
    R = np.empty((m, X.shape[1]))
    for k in range(X.shape[1]):
        R[:, k] = np.interp(tu, t, X[:, k])

    f_pre, S_pre = spectra(R)
    room.feed(f_pre, S_pre.mean(axis=1))

    # ── learn the machines in the room, then delete them ─────────────────────
    # Every candidate peak is tracked over time. A fan or AC louvre shows up
    # in nearly every window at a rock-steady frequency; a person's rate
    # always wanders. So the machines identify themselves, no empty room
    # needed. They are then regressed out of the time series (fundamental +
    # harmonics), which is why the fan no longer BLOCKS detection: it is
    # removed rather than allowed to win the peak.
    Sm_pre = S_pre.mean(axis=1)
    lines_auto.observe(top_peaks(f_pre, Sm_pre, F_LO_B, F_HI_B, n=3) +
                       top_peaks(f_pre, Sm_pre, F_LO_H, F_HI_H, n=3))
    mech_lines = sorted(set(lines_auto.mechanical()) | set(room.lines))
    R = remove_periodics(R, tu, mech_lines, n_harm=5)

    f_ax, S_all = spectra(R)

    # ── breathing: pick the responsive subcarriers instead of averaging all ──
    band_b = (f_ax >= F_LO_B) & (f_ax <= F_HI_B)
    sel_b, w_b = select_columns(band_snr(S_all, band_b))
    Sb_avg = room.suppress(f_ax, (S_all[:, sel_b] * w_b).sum(axis=1) / w_b.sum())
    f_b, conf_b = peak_of(f_ax, Sb_avg, F_LO_B, F_HI_B)
    wander_b.add(f_b)
    br_track.update(f_b, conf_b >= 2.0)

    Bm = sosfiltfilt(sos_breath, R[:, sel_b], axis=0)
    wave_b = combine(Bm, w_b)

    people = []
    if conf_b >= 2.0:
        fseg, sseg = f_ax[band_b], Sb_avg[band_b]
        order = np.argsort(sseg)[::-1]
        top = sseg[order[0]]
        for i in order:
            if sseg[i] < 0.45 * top:
                break
            if all(abs(fseg[i] - pf) >= 0.05 for pf, _ in people):
                people.append((float(fseg[i]), float(sseg[i])))
            if len(people) >= 3:
                break

    # ── motion gate ──────────────────────────────────────────────────────────
    hi_band, in_band = f_ax > 4.0, (f_ax >= F_LO_H) & (f_ax <= F_HI_H)
    Sm = S_all.mean(axis=1)
    motion = hi_band.any() and in_band.any() and \
        Sm[hi_band].mean() > 1.8 * Sm[in_band].mean()

    # ── heartbeat ────────────────────────────────────────────────────────────
    Rc = remove_periodics(R, tu, [br_track.f if br_track.f > 0 else f_b])
    fh_ax, Sh = spectra(Rc)
    band_h = (fh_ax >= F_LO_H) & (fh_ax <= F_HI_H)
    sel_h, w_h = select_columns(band_snr(Sh, band_h))
    Sh_avg = room.suppress(fh_ax, (Sh[:, sel_h] * w_h).sum(axis=1) / w_h.sum())

    cand, score = harmonic_score(fh_ax, Sh_avg, F_LO_H, F_HI_H)
    f_fft = float(cand[int(np.argmax(score))]) if len(cand) else 0.0
    conf_h = Sh_avg[band_h].max() / (np.median(Sh_avg[band_h]) + 1e-12)
    wander_h.add(f_fft)

    Hm = sosfiltfilt(sos_heart, Rc[:, sel_h], axis=0)
    wave_h = combine(Hm, w_h)
    f_ac, ac_peak = autocorr_estimate(wave_h, F_LO_H, F_HI_H)

    # Half-window cross-check: a real pulse is present in both halves.
    half = len(tu) // 2
    halves = []
    for sl in (slice(0, half), slice(half, None)):
        seg = Rc[sl][:, sel_h]
        if seg.shape[0] > 4 * FS:
            fh2, Sh2 = spectra(seg)
            s2 = (Sh2 * w_h).sum(axis=1) / w_h.sum()
            halves.append(peak_of(fh2, s2, F_LO_H, F_HI_H)[0])
    consistent = (len(halves) == 2 and f_fft > 0 and
                  all(abs(h - f_fft) / max(f_fft, 1e-9) < 0.15 for h in halves))

    agree = f_fft > 0 and f_ac > 0 and abs(f_fft - f_ac) / max(f_fft, 1e-9) < 0.12
    if agree and consistent:
        f_h, quality = 0.5 * (f_fft + f_ac), 3
    elif agree:
        f_h, quality = 0.5 * (f_fft + f_ac), 2
    elif conf_h >= 3.0 and consistent:
        f_h, quality = f_fft, 1
    else:
        f_h, quality = f_fft, 0

    # If a machine harmonic sits on top of the pulse the two are not separable
    # over a 30 s window, so say so rather than reporting a confident wrong BPM.
    collide = any(abs(f0 * k - f_fft) < 0.02
                  for f0 in mech_lines for k in range(1, 6)) if f_fft > 0 else False

    mech_h, mech_b = wander_h.is_mechanical(), wander_b.is_mechanical()
    good = (not motion) and (not mech_h) and (quality >= 1) and \
        (conf_h >= 2.2 or ac_peak >= 0.30)
    hr_track.update(f_h, good)

    hr_now = hr_track.value()
    if hr_now > 0 and hr_track.locked:
        hr_history.append(hr_now * 60.0)

    # ── rhythm analysis: individual beats -> RR intervals -> irregularity ────
    if sim_mode["kind"]:
        beats = simulate_rr(sim_mode["kind"], seconds=dur)
        hrv = hrv_metrics(beats)
        rhythm = classify_rhythm(hrv, hrv["mean_hr"] if hrv else 0, 3, False)
        rhythm["state"] += "   [SIMULATED]"
        rhythm["sustained"] = rhythm["score"] >= 3
        rhythm["windows"] = 99
    else:
        beats = detect_beats(wave_h, tu, hr_now if hr_now > 0 else f_h)
        hrv = hrv_metrics(beats)
        rhythm = classify_rhythm(hrv, hr_now * 60.0, quality, motion)
        # A single irregular window is not an alert.
        sustained = rhythm_gate.update(rhythm["trust"], rhythm["score"])
        rhythm["sustained"] = sustained
        rhythm["windows"] = rhythm_gate.hi
        if rhythm["trust"] and rhythm["score"] >= 3 and not sustained:
            rhythm["state"] = "checking irregularity..."

    if logger:
        try:
            logger.writerow([f"{time.monotonic() - t0:.1f}",
                             f"{br_track.value() * 60:.1f}", f"{hr_now * 60:.1f}",
                             int(hr_track.locked), f"{conf_h:.2f}",
                             rhythm["state"],
                             f"{hrv['rmssd']:.1f}" if hrv else "",
                             f"{hrv['cv']:.3f}" if hrv else "",
                             f"{hrv['pnn50']:.1f}" if hrv else "",
                             f"{hrv['entropy']:.3f}" if hrv else "",
                             hrv["n"] if hrv else 0])
            logfile.flush()
        except Exception:
            pass

    return dict(tu=tu, wave_b=wave_b, fb=f_ax, Sb=Sb_avg, people=people,
                wave_h=wave_h, fh=fh_ax, Sh=Sh_avg, cand=cand, score=score,
                f_h=hr_now, locked=hr_track.locked, quality=quality,
                motion=motion, conf_h=conf_h, f_fft=f_fft, f_ac=f_ac,
                f_b=br_track.value(), dur=dur, mech_h=mech_h, mech_b=mech_b,
                wander_h=wander_h.rel(), lines=mech_lines, collide=collide,
                beats=beats, hrv=hrv, rhythm=rhythm,
                nsel=len(sel_h), consistent=consistent)


