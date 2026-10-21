#!/usr/bin/env python3
"""Version live du détecteur « range dans le range ».

- Scanne les coins les plus échangés de Hyperliquid toutes les 5 minutes, juste après la clôture des bougies.
- Garde les signaux et tes votes Oui/Non dans une base SQLite (DATA_DIR/ranges.db).
- Sert l'interface sur http://localhost:8000 (mot de passe : APP_PASSWORD).

Variables d'environnement : PORT, APP_PASSWORD, DATA_DIR, COINS (nb de coins, 30), TFS ("5m,15m"),
SCAN_EVERY_MIN (5), BACKFILL_DAYS (2, au premier démarrage), KEEP_DAYS (30).
"""
import base64, hmac, json, os, sqlite3, threading, time, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import detect as D

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", 8000))
PASSWORD = os.environ.get("APP_PASSWORD", "")
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(ROOT, "data"))
N_COINS = int(os.environ.get("COINS", 30))
TFS = os.environ.get("TFS", "5m,15m").split(",")
EVERY = int(os.environ.get("SCAN_EVERY_MIN", 5)) * 60
BACKFILL_DAYS = float(os.environ.get("BACKFILL_DAYS", 2))
KEEP_DAYS = float(os.environ.get("KEEP_DAYS", 30))
RESCAN_DAYS = 1.0  # à chaque scan on rejoue les dernières 24 h (les signaux en cours sont mis à jour)

os.makedirs(DATA_DIR, exist_ok=True)

# ---------- base de données ----------

db_lock = threading.Lock()
db = sqlite3.connect(os.path.join(DATA_DIR, "ranges.db"), check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS signals (
  key TEXT PRIMARY KEY, coin TEXT, tf TEXT, t INTEGER, result TEXT,
  data TEXT, chart TEXT, created_at INTEGER, updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS signals_t ON signals(t);
CREATE TABLE IF NOT EXISTS votes (key TEXT PRIMARY KEY, vote TEXT, at INTEGER);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
""")


def q(sql, args=(), one=False):
    with db_lock:
        cur = db.execute(sql, args)
        rows = cur.fetchall()
        db.commit()
    return (rows[0] if rows else None) if one else rows


def meta_get(k, default=None):
    r = q("SELECT v FROM meta WHERE k=?", (k,), one=True)
    return json.loads(r["v"]) if r else default


def meta_set(k, v):
    q("INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))


# ---------- scan ----------

status = {"running": False, "lastAt": None, "lastDuration": None, "nextAt": None, "error": None,
          "coins": [], "lastNew": 0}
scan_lock = threading.Lock()
wake = threading.Event()


def upsert(sig, chart, allow_new):
    """Insère un nouveau signal, ou met à jour le résultat/graphique d'un signal existant."""
    now = int(time.time())
    row = q("SELECT key, result FROM signals WHERE key=?", (sig["key"],), one=True)
    if row:
        if row["result"] == "en cours" or sig["out"]["result"] != row["result"]:
            q("UPDATE signals SET result=?, data=?, chart=?, updated_at=? WHERE key=?",
              (sig["out"]["result"], json.dumps(sig), json.dumps(chart), now, sig["key"]))
        return False
    if not allow_new:
        return False
    q("INSERT INTO signals(key, coin, tf, t, result, data, chart, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
      (sig["key"], sig["coin"], sig["tf"], sig["t"], sig["out"]["result"], json.dumps(sig), json.dumps(chart), now, now))
    return True


# ---------- bougies : cache mémoire + requêtes espacées (limite Hyperliquid : 1200 de « poids » / minute) ----------

series = {}          # (coin, tf) -> bougies clôturées
hl_lock = threading.Lock()
next_call = [0.0]


def hl_paced(body):
    with hl_lock:
        wait = next_call[0] - time.time()
        if wait > 0:
            time.sleep(wait)
        r = D.hl(body)
        n = len(r) if isinstance(r, list) else 0
        # poids d'une requête de bougies ≈ 20 + 1 par tranche de 60 bougies ; on vise ~1000 / minute
        next_call[0] = time.time() + (20 + n / 60) * 60 / 1000
        return r


def get_bars(coin, tf, days):
    """Bougies clôturées couvrant `days` (+ le contexte nécessaire au grand range). Ne télécharge que la fin."""
    tf_ms = D.TF_MS[tf]
    keep_ms = days * 86400e3 + (D.P["range_max_bars"] + 60) * tf_ms
    now = int(time.time() * 1000)
    cached = series.get((coin, tf), [])
    start = cached[-1]["t"] * 1000 - 2 * tf_ms if cached and cached[0]["t"] * 1000 <= now - keep_ms + 3 * tf_ms else now - keep_ms
    raw = hl_paced({"type": "candleSnapshot", "req": {"coin": coin, "interval": tf, "startTime": int(start), "endTime": now}}) or []
    fresh = [{"t": c["t"] // 1000, "o": float(c["o"]), "h": float(c["h"]), "l": float(c["l"]), "c": float(c["c"])} for c in raw]
    fresh = [b for b in fresh if (b["t"] * 1000 + tf_ms) <= now]  # bougies clôturées seulement
    merged = {b["t"]: b for b in cached}
    merged.update({b["t"]: b for b in fresh})
    bars = [merged[t] for t in sorted(merged) if t * 1000 >= now - keep_ms]
    series[(coin, tf)] = bars
    return bars


top_cache = {"at": 0, "coins": []}


def coins_to_scan():
    if time.time() - top_cache["at"] > 3600 or not top_cache["coins"]:
        top_cache.update(at=time.time(), coins=D.top_coins(N_COINS))
    return list(top_cache["coins"])


def scan_series(coin, tf, days, new_since, force=False):
    """Rejoue la détection sur une série ; un signal n'est AJOUTÉ que s'il est récent (pas un ancien qui réapparaît
    parce que la fenêtre a glissé)."""
    cached = series.get((coin, tf))
    tf_s = D.TF_MS[tf] / 1000
    if not force and cached and time.time() < cached[-1]["t"] + 2 * tf_s:
        return 0  # aucune nouvelle bougie clôturée depuis le dernier scan (ex. 15m entre deux clôtures)
    bars = get_bars(coin, tf, days)
    if len(bars) < 60:
        return 0
    new = 0
    for d in D.scan(bars, time.time() - days * 86400):
        sig, chart = D.build_signal(bars, d, coin, tf)
        new += upsert(sig, chart, allow_new=sig["t"] >= new_since)
    return new


def run_scan():
    if not scan_lock.acquire(blocking=False):
        return
    t0 = time.time()
    status.update(running=True, error=None)
    try:
        first = meta_get("lastScanAt") is None
        days = BACKFILL_DAYS if first else RESCAN_DAYS
        # nouveaux signaux acceptés : ceux postérieurs au dernier scan (moins une marge de 2 bougies 15m)
        new_since = 0 if first else meta_get("lastScanAt") - 1800
        coins = coins_to_scan()
        # on garde aussi les coins qui ont un signal en cours, même s'ils sortent du top
        for r in q("SELECT DISTINCT coin FROM signals WHERE result='en cours'"):
            if r["coin"] not in coins:
                coins.append(r["coin"])
        status["coins"] = coins
        new = 0
        for tf in TFS:
            for coin in coins:
                try:
                    new += scan_series(coin, tf, days, new_since)
                except Exception as e:
                    print(f"scan {coin} {tf}: {e}", flush=True)
        q("DELETE FROM signals WHERE t < ?", (int(time.time() - KEEP_DAYS * 86400),))
        meta_set("lastScanAt", int(t0))
        status.update(lastAt=int(t0), lastDuration=round(time.time() - t0, 1), lastNew=new)
        print(f"scan ok : {new} nouveau(x) signal(aux) en {time.time() - t0:.0f}s", flush=True)
    except Exception as e:
        status["error"] = str(e)
        traceback.print_exc()
    finally:
        status["running"] = False
        scan_lock.release()


def scheduler():
    while True:
        run_scan()
        # prochain scan : juste après la prochaine clôture de bougie 5m (+20 s de marge)
        nxt = (int(time.time()) // EVERY + 1) * EVERY + 20
        status["nextAt"] = nxt
        wake.wait(max(1, nxt - time.time()))
        wake.clear()


def refresh_one(key):
    row = q("SELECT coin, tf FROM signals WHERE key=?", (key,), one=True)
    if not row:
        return None
    scan_series(row["coin"], row["tf"], RESCAN_DAYS, new_since=time.time() - 1800, force=True)
    return q("SELECT * FROM signals WHERE key=?", (key,), one=True)


# ---------- HTTP ----------

def row_to_signal(r, votes):
    s = json.loads(r["data"])
    s.update(createdAt=r["created_at"], updatedAt=r["updated_at"], vote=votes.get(r["key"]))
    return s


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def authed(self):
        if not PASSWORD:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                pw = base64.b64decode(h[6:]).decode().split(":", 1)[1]
                if hmac.compare_digest(pw, PASSWORD):
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="ranges"')
        self.end_headers()
        self.wfile.write("Mot de passe requis".encode())
        return False

    def send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            return self.send(200, {"ok": True})
        if not self.authed():
            return
        if path in ("/", "/index.html"):
            with open(os.path.join(ROOT, "web", "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/signals":
            votes = {r["key"]: r["vote"] for r in q("SELECT key, vote FROM votes")}
            rows = q("SELECT key, data, created_at, updated_at FROM signals ORDER BY t DESC")
            return self.send(200, {"signals": [row_to_signal(r, votes) for r in rows], "status": status,
                                   "params": D.P, "now": int(time.time())})
        if path.startswith("/api/chart/"):
            key = path[len("/api/chart/"):].replace("%7C", "|")
            r = q("SELECT chart FROM signals WHERE key=?", (key,), one=True)
            return self.send(200, json.loads(r["chart"]) if r else [])
        if path == "/api/export":
            # tout ce qu'il faut pour analyser tes votes : signal, mesures, graphique
            votes = {r["key"]: r for r in q("SELECT * FROM votes")}
            out = []
            for r in q("SELECT * FROM signals WHERE key IN (SELECT key FROM votes)"):
                s = json.loads(r["data"])
                s.update(vote=votes[r["key"]]["vote"], votedAt=votes[r["key"]]["at"], chart=json.loads(r["chart"]))
                out.append(s)
            name = f"jugements-range-{time.strftime('%Y-%m-%d')}.json"
            return self.send(200, json.dumps({"params": D.P, "signals": out}, indent=1).encode(), "application/json",
                             {"Content-Disposition": f'attachment; filename="{name}"'})
        self.send(404, {"error": "introuvable"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self.authed():
            return
        if path == "/api/vote":
            b = self.body()
            if b.get("vote") in ("yes", "no"):
                q("INSERT INTO votes(key, vote, at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET vote=excluded.vote, at=excluded.at",
                  (b["key"], b["vote"], int(time.time())))
            else:
                q("DELETE FROM votes WHERE key=?", (b.get("key"),))
            return self.send(200, {"ok": True})
        if path == "/api/import-votes":
            # fichier exporté par la version locale : [{coin, tf, t, vote}, …]
            items = self.body()
            n = 0
            for it in items if isinstance(items, list) else []:
                if it.get("vote") in ("yes", "no"):
                    q("INSERT OR REPLACE INTO votes(key, vote, at) VALUES(?,?,?)",
                      (f"{it['coin']}|{it['tf']}|{it['t']}", it["vote"], int(time.time())))
                    n += 1
            return self.send(200, {"ok": True, "imported": n})
        if path == "/api/scan":
            threading.Thread(target=run_scan, daemon=True).start()
            return self.send(200, {"ok": True})
        if path == "/api/refresh":
            key = self.body().get("key")
            r = refresh_one(key)
            if not r:
                return self.send(404, {"error": "signal introuvable"})
            votes = {x["key"]: x["vote"] for x in q("SELECT key, vote FROM votes WHERE key=?", (key,))}
            return self.send(200, {"signal": row_to_signal(r, votes), "chart": json.loads(r["chart"])})
        self.send(404, {"error": "introuvable"})


if __name__ == "__main__":
    threading.Thread(target=scheduler, daemon=True).start()
    print(f"Détecteur live sur http://localhost:{PORT}  ({N_COINS} coins · {', '.join(TFS)} · scan toutes les {EVERY // 60} min)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
