"""Surveillance des Fair Value Gaps (daily, weekly, monthly) sur tous les perps Hyperliquid.

Un FVG se forme sur 3 bougies clôturées qui se suivent :
  - haussier : bas de la 3e > haut de la 1re → zone [haut 1re, bas 3e], retestée quand le prix redescend sur le haut ;
  - baissier : haut de la 3e < bas de la 1re → zone [haut 3e, bas 1re], retestée quand le prix remonte sur le bas.
On alerte UNE fois, au premier contact (une mèche suffit) après la création. Ensuite le FVG est consommé.

Weekly (semaines du lundi) et monthly (mois calendaires) sont construits à partir des bougies journalières.
Le contact est vu de deux façons : prix en direct toutes les 10 s, et bougie du jour (haut/bas) relue toutes les
~10 min par coin, pour ne pas rater une mèche rapide.
"""
import datetime, json, os, threading, time, traceback, urllib.request

HL = "https://api.hyperliquid.xyz/info"
DAY = 86400
RULES = {  # taille minimale de la zone (%) et ancienneté maximale (jours)
    "D": {"min_pct": 0.5, "max_age": 183, "label": "daily"},
    "W": {"min_pct": 1.0, "max_age": 4 * 365, "label": "weekly"},
    "M": {"min_pct": 2.0, "max_age": 4 * 365, "label": "monthly"},
}
REFRESH_EVERY = 600     # bougies journalières relues toutes les 10 min par coin
MIDS_EVERY = 10         # prix en direct toutes les 10 s


# ---------- API Hyperliquid (requêtes espacées : limite de 1200 de « poids » par minute) ----------

_hl_lock = threading.Lock()
_next_call = [0.0]


def hl(body, weight=20):
    with _hl_lock:
        wait = _next_call[0] - time.time()
        if wait > 0:
            time.sleep(wait)
        out = None
        for i in range(3):
            try:
                req = urllib.request.Request(HL, json.dumps(body).encode(), {"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    out = json.load(r)
                break
            except Exception:
                time.sleep(2 + 3 * i)
        n = len(out) if isinstance(out, list) else 0
        _next_call[0] = time.time() + (weight + n / 60) * 60 / 900  # ~900 / min, marge pour le reste
        return out


# ---------- bougies et FVG ----------

def week_start(t):
    d = datetime.datetime.utcfromtimestamp(t)
    return int((datetime.datetime(d.year, d.month, d.day) - datetime.timedelta(days=d.weekday())).replace(tzinfo=datetime.timezone.utc).timestamp())


def month_start(t):
    d = datetime.datetime.utcfromtimestamp(t)
    return int(datetime.datetime(d.year, d.month, 1, tzinfo=datetime.timezone.utc).timestamp())


def next_month(t):
    d = datetime.datetime.utcfromtimestamp(t)
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return int(datetime.datetime(y, m, 1, tzinfo=datetime.timezone.utc).timestamp())


def aggregate(daily, kind):
    out = []
    for b in daily:
        t = week_start(b["t"]) if kind == "W" else month_start(b["t"])
        if out and out[-1]["t"] == t:
            o = out[-1]
            o["h"], o["l"], o["c"] = max(o["h"], b["h"]), min(o["l"], b["l"]), b["c"]
        else:
            out.append({"t": t, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]})
    return out


def period_end(t, kind):
    return t + DAY if kind == "D" else t + 7 * DAY if kind == "W" else next_month(t)


def find_fvgs(bars, kind, now):
    """FVG formés par 3 bougies CLÔTURÉES, filtrés par taille et ancienneté."""
    rule = RULES[kind]
    closed = [b for b in bars if period_end(b["t"], kind) <= now]
    out = []
    for i in range(2, len(closed)):
        a, m, c = closed[i - 2], closed[i - 1], closed[i]
        if now - m["t"] > rule["max_age"] * DAY:
            continue
        if c["l"] > a["h"]:
            d, bot, top = "bull", a["h"], c["l"]
        elif c["h"] < a["l"]:
            d, bot, top = "bear", c["h"], a["l"]
        else:
            continue
        pct = (top - bot) / bot * 100
        if pct < rule["min_pct"]:
            continue
        out.append({"tf": kind, "dir": d, "bot": bot, "top": top, "pct": round(pct, 2),
                    "t": m["t"], "t1": a["t"], "after": period_end(c["t"], kind)})
    return out


def first_touch(f, daily):
    """Premier jour (bougie journalière, y compris celle en cours) qui touche la zone après sa création."""
    for b in daily:
        if b["t"] < f["after"]:
            continue
        if (f["dir"] == "bull" and b["l"] <= f["top"]) or (f["dir"] == "bear" and b["h"] >= f["bot"]):
            return b["t"]
    return None


def fmt_px(v):
    return f"{v:,.1f}".replace(",", " ") if v >= 1000 else f"{v:.4f}" if v >= 1 else f"{v:.6g}"


# ---------- moteur ----------

class FVGWatcher:
    def __init__(self, data_dir, site_url=""):
        self.path = os.path.join(data_dir, "fvg_state.json")
        self.site_url = site_url.rstrip("/")
        self.lock = threading.Lock()
        self.daily = {}          # coin -> bougies journalières
        self.refreshed = {}      # coin -> dernière relecture
        self.intact = {}         # coin -> [FVG intacts]
        self.mids = {}
        self.coins = []
        self.status = {"startedAt": int(time.time()), "ready": False, "coinsLoaded": 0, "coins": 0, "lastMids": None, "error": None}
        st = self._load()
        self.seen = set(st.get("seen", []))        # FVG vus intacts au moins une fois (on n'alerte que ceux-là)
        self.alerted = set(st.get("alerted", []))  # FVG déjà touchés / alertés
        self.alerts = st.get("alerts", [])         # historique (récent en premier)
        self.telegram = None

    # --- persistance ---
    def _load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return {}

    def save(self):
        with self.lock:
            data = {"seen": sorted(self.seen), "alerted": sorted(self.alerted), "alerts": self.alerts[:500]}
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)

    @staticmethod
    def key(coin, f):
        return f"{coin}|{f['tf']}|{f['t']}|{f['dir']}"

    # --- boucles ---
    def start(self):
        threading.Thread(target=self._candles_loop, daemon=True).start()
        threading.Thread(target=self._mids_loop, daemon=True).start()

    def _coins(self):
        meta = hl({"type": "meta"}, weight=20)
        if meta:
            self.coins = [u["name"] for u in meta["universe"] if not u.get("isDelisted")]
            self.status["coins"] = len(self.coins)
        return self.coins

    def _candles_loop(self):
        last_meta = 0
        while True:
            try:
                if time.time() - last_meta > 3600 or not self.coins:
                    self._coins()
                    last_meta = time.time()
                # le coin le moins récemment relu passe en premier
                todo = sorted(self.coins, key=lambda c: self.refreshed.get(c, 0))
                coin = todo[0] if todo else None
                if coin and time.time() - self.refreshed.get(coin, 0) >= REFRESH_EVERY:
                    self._refresh(coin)
                    self.save()
                else:
                    time.sleep(2)
            except Exception as e:
                self.status["error"] = str(e)
                traceback.print_exc()
                time.sleep(10)

    def _refresh(self, coin):
        now = int(time.time())
        have = self.daily.get(coin)
        start = (have[-3]["t"] if have and len(have) > 3 else now - 5000 * DAY) * 1000
        raw = hl({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1d", "startTime": int(start), "endTime": now * 1000}})
        self.refreshed[coin] = time.time()
        if not isinstance(raw, list) or not raw:
            return
        fresh = [{"t": c["t"] // 1000, "o": float(c["o"]), "h": float(c["h"]), "l": float(c["l"]), "c": float(c["c"])} for c in raw]
        merged = {b["t"]: b for b in (have or [])}
        merged.update({b["t"]: b for b in fresh})
        daily = [merged[t] for t in sorted(merged)]
        self.daily[coin] = daily
        if not have:
            self.status["coinsLoaded"] = len(self.daily)
            if len(self.daily) >= len(self.coins):
                self.status["ready"] = True
        self._evaluate(coin, daily, now)

    def _evaluate(self, coin, daily, now):
        fvgs = find_fvgs(daily, "D", now) + find_fvgs(aggregate(daily, "W"), "W", now) + find_fvgs(aggregate(daily, "M"), "M", now)
        intact = []
        for f in fvgs:
            k = self.key(coin, f)
            if k in self.alerted:
                continue
            touched = first_touch(f, daily)
            if touched is None:
                intact.append(f)
                self.seen.add(k)
            elif k in self.seen:
                # vu intact auparavant, touché depuis → premier retour
                self._alert(coin, f, k, daily[-1]["c"], touched)
            else:
                self.alerted.add(k)  # déjà touché avant qu'on le surveille : ce n'est plus un « premier retour »
        with self.lock:
            self.intact[coin] = intact

    def _mids_loop(self):
        while True:
            try:
                mids = hl({"type": "allMids"}, weight=2)
                if isinstance(mids, dict):
                    self.mids = {k: float(v) for k, v in mids.items() if not k.startswith("@")}
                    self.status["lastMids"] = int(time.time())
                    hit = False
                    for coin, lst in list(self.intact.items()):
                        px = self.mids.get(coin)
                        if px is None:
                            continue
                        for f in list(lst):
                            if (f["dir"] == "bull" and px <= f["top"]) or (f["dir"] == "bear" and px >= f["bot"]):
                                self._alert(coin, f, self.key(coin, f), px, int(time.time()))
                                hit = True
                    if hit:
                        self.save()
            except Exception as e:
                self.status["error"] = str(e)
            time.sleep(MIDS_EVERY)

    # --- alerte ---
    def _alert(self, coin, f, k, px, touched_at):
        with self.lock:
            if k in self.alerted:
                return
            self.alerted.add(k)
            self.intact[coin] = [x for x in self.intact.get(coin, []) if self.key(coin, x) != k]
            a = {"coin": coin, **f, "price": px, "at": int(time.time()), "touchedDay": touched_at}
            self.alerts.insert(0, a)
        print(f"FVG {coin} {RULES[f['tf']]['label']} {f['dir']} touché à {px}", flush=True)
        if self.telegram and f["tf"] not in self.telegram.muted:
            self.telegram.send(self.message(a))

    def message(self, a):
        up = a["dir"] == "bull"
        created = datetime.datetime.utcfromtimestamp(a["t"]).strftime("%d/%m/%Y" if a["tf"] == "D" else "%m/%Y" if a["tf"] == "M" else "sem. du %d/%m/%Y")
        txt = (f"{'🟢' if up else '🔴'} <b>{a['coin']}</b> — premier retour sur un FVG <b>{RULES[a['tf']]['label']}</b> "
               f"{'haussier' if up else 'baissier'}\n"
               f"Zone {fmt_px(a['bot'])} → {fmt_px(a['top'])} ({a['pct']} %)\n"
               f"Créé : {created} · prix actuel {fmt_px(a['price'])}")
        if self.site_url:
            txt += f"\n{self.site_url}/?coin={a['coin']}"
        return txt

    # --- pour l'interface ---
    def snapshot(self, coin=None):
        with self.lock:
            rows = []
            for c, lst in self.intact.items():
                if coin and c != coin:
                    continue
                px = self.mids.get(c)
                for f in lst:
                    near = f["top"] if f["dir"] == "bull" else f["bot"]
                    dist = (px - near) / px * 100 * (1 if f["dir"] == "bull" else -1) if px else None
                    rows.append({"coin": c, **f, "price": px, "dist": round(dist, 2) if dist is not None else None})
            alerts = [a for a in self.alerts if not coin or a["coin"] == coin][:200]
        rows.sort(key=lambda r: (r["dist"] is None, r["dist"] if r["dist"] is not None else 0))
        st = dict(self.status, telegram=bool(self.telegram and self.telegram.chat_id))
        return {"intact": rows, "alerts": alerts, "status": st}
