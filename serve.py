"""
RadioBeat monitor server.

    python serve.py                   ->  http://localhost:8770

Serves the user-facing monitor, streams live vitals over SSE, and records
sleep sessions to a local SQLite file. Falls back to clearly-labelled demo
data when no board is connected.
"""
import sys
import json
import time
import sqlite3
import threading
import http.server
import socketserver
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import numpy as np

PORT = 8770
HERE = Path(__file__).parent
WWW = HERE / "docs"
DB = HERE / "radiobeat.db"

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import radiobeat_core as core            # noqa: E402

_state = {"payload": None, "session": None, "last_alert": ""}
_lock = threading.Lock()

DEFAULT_SETTINGS = {
    "person": "Guest",
    "alert_afib": True,
    "alert_absent": False,
    "record_every_s": 5,
}


# -- storage ------------------------------------------------------------------
def db():
    c = sqlite3.connect(DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.executescript(
            "CREATE TABLE IF NOT EXISTS sessions("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " started REAL NOT NULL, ended REAL, person TEXT, note TEXT);"
            "CREATE TABLE IF NOT EXISTS readings("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, ts REAL,"
            " breath REAL, hr REAL, quality INTEGER, present INTEGER,"
            " motion INTEGER, rhythm TEXT, score INTEGER, rmssd REAL, cv REAL,"
            " pnn50 REAL, entropy REAL);"
            "CREATE TABLE IF NOT EXISTS alerts("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, ts REAL,"
            " kind TEXT, message TEXT);"
            "CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);"
            "CREATE INDEX IF NOT EXISTS ix_read ON readings(session_id, ts);"
        )


def get_settings():
    out = dict(DEFAULT_SETTINGS)
    with db() as c:
        for r in c.execute("SELECT k, v FROM settings"):
            try:
                out[r["k"]] = json.loads(r["v"])
            except Exception:
                pass
    return out


def save_settings(d):
    with db() as c:
        for k, v in d.items():
            if k in DEFAULT_SETTINGS:
                c.execute("INSERT INTO settings(k,v) VALUES(?,?) "
                          "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                          (k, json.dumps(v)))
    return get_settings()


# -- payload building ---------------------------------------------------------
def thin(a, n=180):
    a = np.asarray(a, dtype=float)
    if a.size == 0:
        return []
    if a.size <= n:
        return [round(float(v), 4) for v in a]
    idx = np.linspace(0, a.size - 1, n).astype(int)
    return [round(float(v), 4) for v in a[idx]]


def norm(a):
    a = np.asarray(a, dtype=float)
    m = np.abs(a).max() if a.size else 0
    return a / m if m > 0 else a


def demo_payload(t):
    n = 400
    tt = np.linspace(0, 20, n)
    br = 14.5 + 0.6 * np.sin(t / 7)
    hr = 68 + 3 * np.sin(t / 11)
    afib = (int(t) // 50) % 3 == 2
    m = core.hrv_metrics(core.simulate_rr("afib" if afib else "normal", 30.0, hr))
    return dict(
        ok=True, demo=True, present=True, motion=False, collide=False,
        breath=round(float(br), 1), hr=round(float(hr)), hr_locked=True,
        quality=3, people=1, machines=0, rate=0,
        breath_wave=thin(np.sin(2 * np.pi * br / 60 * tt)),
        heart_wave=thin(np.sin(2 * np.pi * hr / 60 * tt)
                        + 0.25 * np.sin(4 * np.pi * hr / 60 * tt)),
        rr=thin(m["rr"] * 1000, 120) if m else [],
        rhythm="IRREGULAR - AFib-like" if afib else "regular rhythm",
        rhythm_trust=True, rhythm_score=4 if afib else 1, sustained=afib,
        rmssd=round(m["rmssd"]) if m else 0,
        cv=round(m["cv"], 3) if m else 0,
        pnn50=round(m["pnn50"]) if m else 0,
        entropy=round(m["entropy"], 2) if m else 0,
        sim=None, ts=time.time())


def build_payload():
    r = core.analyze()
    if r is None:
        return dict(ok=False, demo=False,
                    reason="warming up - collecting signal",
                    rate=round(core.stats["rate"]), pkts=core.stats["pkts"],
                    ts=time.time())
    hrv, rh = r["hrv"], r["rhythm"]
    return dict(
        ok=True, demo=False,
        present=bool(r["people"]) or r["quality"] >= 1,
        motion=bool(r["motion"]), collide=bool(r["collide"]),
        breath=round(r["f_b"] * 60, 1), hr=round(r["f_h"] * 60),
        hr_locked=bool(r["locked"]), quality=int(r["quality"]),
        people=len(r["people"]), machines=len(r["lines"]),
        rate=round(core.stats["rate"]),
        breath_wave=thin(norm(r["wave_b"])),
        heart_wave=thin(norm(r["wave_h"])),
        rr=thin(hrv["rr"] * 1000, 120) if hrv else [],
        rhythm=rh["state"], rhythm_trust=bool(rh["trust"]),
        rhythm_score=int(rh["score"]), sustained=bool(rh.get("sustained")),
        rmssd=round(hrv["rmssd"]) if hrv else 0,
        cv=round(hrv["cv"], 3) if hrv else 0,
        pnn50=round(hrv["pnn50"]) if hrv else 0,
        entropy=round(hrv["entropy"], 2) if hrv else 0,
        sim=core.sim_mode["kind"], ts=time.time())


def record(p):
    """Persist a reading and raise alerts while a session is running."""
    sid = _state["session"]
    if not sid or not p.get("ok"):
        return
    with db() as c:
        c.execute("INSERT INTO readings(session_id,ts,breath,hr,quality,present,"
                  "motion,rhythm,score,rmssd,cv,pnn50,entropy) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (sid, p["ts"], p.get("breath"), p.get("hr"), p.get("quality"),
                   int(p.get("present", 0)), int(p.get("motion", 0)),
                   p.get("rhythm"), p.get("rhythm_score"), p.get("rmssd"),
                   p.get("cv"), p.get("pnn50"), p.get("entropy")))
        st = get_settings()
        kind = msg = None
        if p.get("sustained") and st.get("alert_afib"):
            kind = "afib"
            msg = "Irregular rhythm sustained across several windows"
        elif st.get("alert_absent") and not p.get("present"):
            kind = "absent"
            msg = "No one detected in range"
        if kind and _state["last_alert"] != kind:
            c.execute("INSERT INTO alerts(session_id,ts,kind,message) "
                      "VALUES(?,?,?,?)", (sid, p["ts"], kind, msg))
        _state["last_alert"] = kind or ""


def worker():
    t0 = time.time()
    last_rec = 0.0
    while True:
        try:
            p = (build_payload() if core.stats["pkts"] > 0
                 else demo_payload(time.time() - t0))
            _state["payload"] = p
            every = get_settings().get("record_every_s", 5)
            if time.time() - last_rec >= every:
                last_rec = time.time()
                record(p)
        except Exception as e:
            _state["payload"] = dict(ok=False, demo=False,
                                     reason="error: " + str(e), ts=time.time())
        time.sleep(1.0)


# -- http ---------------------------------------------------------------------
class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WWW), **kw)

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except Exception:
            return {}

    def do_POST(self):
        path = urlparse(self.path).path
        b = self._body()

        if path == "/api/session/start":
            with _lock:
                with db() as c:
                    cur = c.execute(
                        "INSERT INTO sessions(started,person,note) VALUES(?,?,?)",
                        (time.time(),
                         b.get("person") or get_settings().get("person"),
                         b.get("note")))
                    _state["session"] = cur.lastrowid
                    _state["last_alert"] = ""
            return self._json({"session": _state["session"]})

        if path == "/api/session/stop":
            sid = _state["session"]
            if sid:
                with db() as c:
                    c.execute("UPDATE sessions SET ended=? WHERE id=?",
                              (time.time(), sid))
                _state["session"] = None
            return self._json({"stopped": sid})

        if path == "/api/settings":
            return self._json(save_settings(b))

        if path.startswith("/api/sim/"):
            k = path.rsplit("/", 1)[-1]
            core.sim_mode["kind"] = None if k == "off" else k
            return self._json({"sim": core.sim_mode["kind"]})

        self._json({"error": "not found"}, 404)

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path

        if path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    p = _state["payload"]
                    if p is not None:
                        out = dict(p, session=_state["session"])
                        line = "data: " + json.dumps(out) + "\n\n"
                        self.wfile.write(line.encode())
                        self.wfile.flush()
                    time.sleep(1.0)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        if path == "/api/settings":
            return self._json(get_settings())

        if path == "/api/sessions":
            with db() as c:
                rows = c.execute(
                    "SELECT s.*, "
                    " (SELECT COUNT(*) FROM readings r WHERE r.session_id=s.id) n,"
                    " (SELECT COUNT(*) FROM alerts a WHERE a.session_id=s.id) alerts,"
                    " (SELECT AVG(hr) FROM readings r WHERE r.session_id=s.id"
                    "   AND r.hr>0) avg_hr,"
                    " (SELECT AVG(breath) FROM readings r WHERE r.session_id=s.id"
                    "   AND r.breath>0) avg_br"
                    " FROM sessions s ORDER BY s.started DESC LIMIT 50").fetchall()
            return self._json([dict(r) for r in rows])

        if path.startswith("/api/session/"):
            try:
                sid = int(path.rsplit("/", 1)[-1])
            except ValueError:
                return self._json({"error": "bad id"}, 400)
            with db() as c:
                s = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
                if not s:
                    return self._json({"error": "not found"}, 404)
                rd = c.execute("SELECT ts,breath,hr,quality,present,motion,score,"
                               "rmssd,cv,pnn50,entropy FROM readings "
                               "WHERE session_id=? ORDER BY ts", (sid,)).fetchall()
                al = c.execute("SELECT ts,kind,message FROM alerts "
                               "WHERE session_id=? ORDER BY ts", (sid,)).fetchall()
            return self._json({"session": dict(s),
                               "readings": [dict(r) for r in rd],
                               "alerts": [dict(a) for a in al]})

        if path == "/api/state":
            return self._json(_state["payload"] or {"ok": False})

        return super().do_GET()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    init_db()
    threading.Thread(target=worker, daemon=True).start()
    print("RadioBeat monitor -> http://localhost:" + str(PORT))
    print("   serving " + str(WWW))
    print("   demo data is served until the board starts streaming")
    with Server(("", PORT), Handler) as httpd:
        httpd.serve_forever()
