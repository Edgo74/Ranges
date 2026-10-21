#!/usr/bin/env python3
"""Détecteur « range dans le range » sur Hyperliquid.

Motif recherché :
  1. Un grand range horizontal : un bord touché par au moins 4 bougies, l'autre par au moins 2,
     et le prix a fait l'aller-retour (haut → bas → haut, ou l'inverse).
  2. Puis un petit range (au moins 4 bougies, serré) collé à un BORD du grand range — pas au milieu.
  3. Signal : petit range en haut → le prix va chercher le bas (short). En bas → va chercher le haut (long).

Chaque signal est rejoué sur la suite pour savoir s'il a marché :
  ✓ le bord opposé est touché avant que le prix dépasse l'autre bord de stop_mult × la hauteur du range.

Usage : python3 detect.py [--days 3] [--coins 30] [--tf 5m,15m]
Puis ouvrir index.html.
"""
import argparse, json, os, time, urllib.request

HL = "https://api.hyperliquid.xyz/info"
ROOT = os.path.dirname(os.path.abspath(__file__))

# ---------- paramètres du motif (c'est ici qu'on règle) ----------
P = {
    # grand range : un range SERRÉ (quelques hauteurs de bougie), pas une grande vague
    "touch_tol": 0.15,        # une bougie « touche » un bord si elle arrive à moins de 15 % de la hauteur du range
    "edge_min_touches": 4,    # un des deux bords : au moins 4 bougies
    "other_min_touches": 2,   # l'autre bord : au moins 2 bougies
    "range_min_bars": 8,      # durée du grand range (en bougies)
    "range_max_bars": 40,
    "range_min_atr": 1.5,     # hauteur du grand range : entre 1,5 et 6 fois la taille moyenne d'une bougie
    "range_max_atr": 6.0,
    "max_breaks": 1,          # nb de bougies tolérées qui dépassent un bord (mèches isolées)
    # petit range : les dernières bougies restent dans la moitié d'un bord et touchent ce bord
    "inner_min_bars": 4,
    "inner_max_bars": 25,
    "inner_min_touches": 4,   # au moins 4 bougies du petit range touchent le bord du grand range
    "inner_min_far_touches": 2,  # le petit range est horizontal : son bord intérieur est touché au moins 2 fois (pas une montée)
    "inner_far": 0.4,         # aucune bougie du petit range ne descend sous 40 % de la hauteur (petit range en haut)
    "before_inner_bars": 2,   # au moins 2 bougies du grand range avant le petit
    "outcome_bars": 120,      # nb de bougies max pour juger si le signal a marché
    # contexte 4h : même règle de range (4 bougies sur un bord, 2 sur l'autre, allers-retours)
    "h4_max_atr": 8.0,        # un range 4h peut être plus large (jusqu'à 8 bougies 4h moyennes de haut)
    "h4_edge_zone": 0.35,     # « bord haut » du range 4h = 35 % du haut ; « bord bas » = 35 % du bas
    "h4_outcome_bars": 12,    # on juge le bord opposé du 4h sur 12 bougies 4h (48 h)
    "stop_mult": 0.5,         # échec si le prix dépasse le bord du petit range de 50 % de la hauteur du grand range
}


def hl(body):
    for i in range(4):
        try:
            req = urllib.request.Request(HL, json.dumps(body).encode(), {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            time.sleep(1 + i * 2)
    return None


def top_coins(n):
    meta, ctx = hl({"type": "metaAndAssetCtxs"})
    rows = [(float(c["dayNtlVlm"]), u["name"]) for u, c in zip(meta["universe"], ctx) if not u.get("isDelisted")]
    return [name for _, name in sorted(rows, reverse=True)[:n]]


TF_MS = {"1m": 60e3, "3m": 180e3, "5m": 300e3, "15m": 900e3, "30m": 1800e3, "1h": 3600e3, "4h": 14400e3}


def candles(coin, tf, days):
    # contexte : on charge un peu plus que la période scannée pour que les ranges du début soient complets
    need = days * 86400e3 + (P["range_max_bars"] + 50) * TF_MS[tf]
    end = int(time.time() * 1000)
    raw = hl({"type": "candleSnapshot", "req": {"coin": coin, "interval": tf, "startTime": int(end - need), "endTime": end}}) or []
    bars = [{"t": c["t"] // 1000, "o": float(c["o"]), "h": float(c["h"]), "l": float(c["l"]), "c": float(c["c"])} for c in raw]
    # la dernière bougie est en cours : on ne la juge pas
    return bars[:-1] if bars else bars


# ---------- détection ----------

def avg_bar(bars, e, n=50):
    s = max(0, e - n + 1)
    rs = [b["h"] - b["l"] for b in bars[s:e + 1]]
    return sum(rs) / len(rs) if rs else 0


def range_stats(win, tol_frac, max_breaks):
    """Bords (robustes aux mèches isolées), touches de chaque bord et nombre d'allers-retours."""
    highs = sorted((b["h"] for b in win), reverse=True)
    lows = sorted(b["l"] for b in win)
    H, L = highs[min(max_breaks, len(highs) - 1)], lows[min(max_breaks, len(lows) - 1)]
    hgt = H - L
    if hgt <= 0:
        return None
    tol = tol_frac * hgt
    if highs[0] > H + 2 * tol or lows[0] < L - 2 * tol:
        return None  # vraie cassure, pas une mèche
    top = [i for i, b in enumerate(win) if b["h"] >= H - tol]
    bot = [i for i, b in enumerate(win) if b["l"] <= L + tol]
    seq = sorted([(i, "T") for i in top] + [(i, "B") for i in bot])
    changes = sum(1 for x, y in zip(seq, seq[1:]) if x[1] != y[1])
    return {"H": H, "L": L, "hgt": hgt, "tol": tol, "top": top, "bot": bot, "changes": changes}


def is_range(r, min_edge, min_other):
    # au moins 4 bougies sur un bord, 2 sur l'autre, et haut → bas → haut (ou l'inverse)
    return r and max(len(r["top"]), len(r["bot"])) >= min_edge and min(len(r["top"]), len(r["bot"])) >= min_other \
        and r["changes"] >= 2


def detect_at(bars, e, atr):
    """Cherche le motif complet qui se termine à la bougie e (sans regarder le futur)."""
    res, H0, L0 = None, -1e18, 1e18
    for n in range(1, P["range_max_bars"] + 1):
        s = e - n + 1
        if s < 0:
            break
        H0, L0 = max(H0, bars[s]["h"]), min(L0, bars[s]["l"])
        if n < P["range_min_bars"] or H0 - L0 < P["range_min_atr"] * atr:
            continue
        r = range_stats(bars[s:e + 1], P["touch_tol"], P["max_breaks"])
        if not is_range(r, P["edge_min_touches"], P["other_min_touches"]):
            continue
        if not (P["range_min_atr"] * atr <= r["hgt"] <= P["range_max_atr"] * atr):
            continue
        H, L, hgt, tol = r["H"], r["L"], r["hgt"], r["tol"]
        if not (L - tol <= bars[e]["c"] <= H + tol):
            continue  # déjà cassé en clôture
        found = inner_at_edge(bars, s, e, r)
        if found:
            res = found  # on continue : une fenêtre plus longue (range plus établi) remplace la précédente
    return res


def inner_at_edge(bars, s, e, r):
    """Les dernières bougies restent collées à un bord du grand range (sans revenir vers le milieu)."""
    H, L, hgt, tol = r["H"], r["L"], r["hgt"], r["tol"]
    for side in ("top", "bottom"):
        k, touches, ih, il = 0, 0, -1e18, 1e18
        for i in range(e, max(s + P["before_inner_bars"], e - P["inner_max_bars"] + 1) - 1, -1):
            b = bars[i]
            inside = b["l"] >= L + P["inner_far"] * hgt if side == "top" else b["h"] <= H - P["inner_far"] * hgt
            if not inside:
                break
            k += 1
            touches += (b["h"] >= H - tol) if side == "top" else (b["l"] <= L + tol)
            ih, il = max(ih, b["h"]), min(il, b["l"])
        if k < P["inner_min_bars"] or touches < P["inner_min_touches"]:
            continue
        i0 = e - k + 1
        # horizontal : le bord intérieur du petit range est touché plusieurs fois (sinon c'est une montée/descente)
        zone = 0.25 * (ih - il)
        inner = bars[i0:e + 1]
        far = sum(1 for b in inner if (b["l"] <= il + zone if side == "top" else b["h"] >= ih - zone))
        if far < P["inner_min_far_touches"]:
            continue
        # avant le petit range, le grand range a touché ses deux bords
        if not any(j + s < i0 for j in r["top"]) or not any(j + s < i0 for j in r["bot"]):
            continue
        return {"s": s, "e": e, "H": H, "L": L, "tol": tol, "is": i0, "ih": ih, "il": il, "side": side,
                "touchesTop": len(r["top"]), "touchesBot": len(r["bot"]), "innerTouches": touches}
    return None


def outcome(bars, d):
    """Après le signal : bord opposé touché (✓) ou petit range cassé dans le mauvais sens (✗) ?"""
    e = d["e"]
    for j in range(e + 1, min(len(bars), e + 1 + P["outcome_bars"])):
        b = bars[j]
        if d["side"] == "top":
            stop, target = d["H"] + P["stop_mult"] * (d["H"] - d["L"]), d["L"]
            hit_stop, hit_target = b["h"] > stop, b["l"] <= target
        else:
            stop, target = d["L"] - P["stop_mult"] * (d["H"] - d["L"]), d["H"]
            hit_stop, hit_target = b["l"] < stop, b["h"] >= target
        if hit_stop and hit_target:
            return {"result": "ambigu", "bars": j - e, "t": b["t"]}
        if hit_target:
            return {"result": "win", "bars": j - e, "t": b["t"]}
        if hit_stop:
            return {"result": "loss", "bars": j - e, "t": b["t"]}
    return {"result": "en cours", "bars": len(bars) - 1 - e}


def scan(bars, since_t):
    out, cooldown_until = [], -1
    for e in range(P["range_min_bars"], len(bars)):
        if bars[e]["t"] < since_t or e <= cooldown_until:
            continue
        atr = avg_bar(bars, e)
        if atr <= 0:
            continue
        d = detect_at(bars, e, atr)
        if not d:
            continue
        d["out"] = outcome(bars, d)
        out.append(d)
        # un seul signal par setup : on attend qu'il soit résolu (au moins 20 bougies)
        cooldown_until = 10**9 if d["out"]["result"] == "en cours" else e + max(d["out"]["bars"], 20)
    return out


# ---------- contexte 4h ----------

def find_range(bars, e, max_atr):
    """Le plus long range valide qui se termine à la bougie e (sans petit range) : (start, stats) ou None."""
    atr = avg_bar(bars, e)
    if atr <= 0:
        return None
    best, H0, L0 = None, -1e18, 1e18
    for n in range(1, P["range_max_bars"] + 1):
        s = e - n + 1
        if s < 0:
            break
        H0, L0 = max(H0, bars[s]["h"]), min(L0, bars[s]["l"])
        if n < P["range_min_bars"] or H0 - L0 < P["range_min_atr"] * atr:
            continue
        r = range_stats(bars[s:e + 1], P["touch_tol"], P["max_breaks"])
        if not is_range(r, P["edge_min_touches"], P["other_min_touches"]):
            continue
        if not (P["range_min_atr"] * atr <= r["hgt"] <= max_atr * atr):
            continue
        if not (r["L"] - r["tol"] <= bars[e]["c"] <= r["H"] + r["tol"]):
            continue
        best = (s, r)
    return best


def context_4h(h4, sig_close_t, price, direction):
    """Où se trouve le signal dans le range 4h en place au moment du signal (aucune bougie future utilisée)."""
    step = TF_MS["4h"] / 1000
    closed = [i for i, b in enumerate(h4) if b["t"] + step <= sig_close_t]
    if not closed:
        return None
    e = closed[-1]
    found = find_range(h4, e, P["h4_max_atr"])
    if not found:
        return {"range": False}
    s, r = found
    H, L, hgt = r["H"], r["L"], r["hgt"]
    pos = (price - L) / hgt
    z = P["h4_edge_zone"]
    zone = "au-dessus" if pos > 1 else "en dessous" if pos < 0 else "haut" if pos >= 1 - z else "bas" if pos <= z else "milieu"
    # aligné : petit range en haut (short) dans le haut du range 4h → on vise aussi le bas du 4h ; et inversement
    aligned = (direction == "short" and zone == "haut") or (direction == "long" and zone == "bas")
    ctx = {"range": True, "H": H, "L": L, "tStart": h4[s]["t"], "tEnd": h4[e]["t"] + step, "pos": round(pos, 3),
           "zone": zone, "aligned": aligned, "touchesTop": len(r["top"]), "touchesBot": len(r["bot"])}
    ctx["out"] = outcome_4h(h4, e, ctx)
    return ctx


def outcome_4h(h4, e, ctx):
    """Le bord opposé du range 4h est-il pris (avant que le prix sorte de l'autre côté) ?"""
    if ctx["zone"] not in ("haut", "bas"):
        return None
    hgt = ctx["H"] - ctx["L"]
    for j in range(e + 1, min(len(h4), e + 1 + P["h4_outcome_bars"])):
        b = h4[j]
        if ctx["zone"] == "haut":
            hit_t, hit_s = b["l"] <= ctx["L"], b["h"] > ctx["H"] + P["stop_mult"] * hgt
        else:
            hit_t, hit_s = b["h"] >= ctx["H"], b["l"] < ctx["L"] - P["stop_mult"] * hgt
        if hit_t and hit_s:
            return {"result": "ambigu", "t": b["t"]}
        if hit_t:
            return {"result": "win", "t": b["t"]}
        if hit_s:
            return {"result": "loss", "t": b["t"]}
    return {"result": "en cours"}


def build_signal(bars, d, coin, tf, h4=None):
    """Signal + graphique (barres autour du setup, jusqu'à maintenant tant qu'il est en cours)."""
    s0 = max(0, d["s"] - 30)
    if d["out"]["result"] == "en cours":
        e1 = len(bars)
    else:
        e1 = min(len(bars), d["e"] + 1 + max(40, min(P["outcome_bars"], d["out"]["bars"] + 15)))
    h = d["H"] - d["L"]
    sig = {
        "key": f"{coin}|{tf}|{bars[d['e']]['t']}",
        "coin": coin, "tf": tf,
        "t": bars[d["e"]]["t"], "tStart": bars[d["s"]]["t"], "tInner": bars[d["is"]]["t"],
        "side": d["side"], "dir": "short" if d["side"] == "top" else "long",
        "H": d["H"], "L": d["L"], "ih": d["ih"], "il": d["il"], "tol": d["tol"],
        "target": d["L"] if d["side"] == "top" else d["H"],
        "stop": d["H"] + P["stop_mult"] * h if d["side"] == "top" else d["L"] - P["stop_mult"] * h,
        "touchesTop": d["touchesTop"], "touchesBot": d["touchesBot"], "innerTouches": d.get("innerTouches"),
        "rangeBars": d["e"] - d["s"] + 1, "innerBars": d["e"] - d["is"] + 1,
        "price": bars[d["e"]]["c"],
        "out": d["out"],
    }
    if h4:
        sig["h4"] = context_4h(h4, sig["t"] + TF_MS[tf] / 1000, sig["price"], sig["dir"])
    chart = [[b["t"], b["o"], b["h"], b["l"], b["c"]] for b in bars[s0:e1]]
    return sig, chart


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=3)
    ap.add_argument("--coins", type=int, default=30)
    ap.add_argument("--tf", default="5m,15m")
    ap.add_argument("--only", default="", help="liste de coins, ex. AVAX,BTC")
    a = ap.parse_args()
    coins = a.only.split(",") if a.only else top_coins(a.coins)
    since = time.time() - a.days * 86400
    signals, charts = [], {}
    for tf in a.tf.split(","):
        for coin in coins:
            bars = candles(coin, tf, a.days)
            if len(bars) < 100:
                continue
            found = scan(bars, since)
            for d in found:
                sig, chart = build_signal(bars, d, coin, tf)
                sig["id"] = len(signals) + 1
                charts[sig["id"]] = chart
                signals.append(sig)
            print(f"{tf:>4} {coin:<10} {len(found)} signal(s)", flush=True)
    with open(os.path.join(ROOT, "data.js"), "w") as f:
        f.write("window.SIGNALS=" + json.dumps(signals, separators=(",", ":")) + ";\n")
        f.write("window.CHARTS=" + json.dumps(charts, separators=(",", ":")) + ";\n")
        f.write("window.PARAMS=" + json.dumps(P) + ";\n")
        f.write(f"window.GENERATED={int(time.time() * 1000)};window.DAYS={a.days};\n")
    res = [s["out"]["result"] for s in signals]
    w, l = res.count("win"), res.count("loss")
    print(f"\n{len(signals)} signaux · ✓ {w} · ✗ {l} · en cours {res.count('en cours')} · ambigus {res.count('ambigu')}"
          + (f" · réussite {w / (w + l) * 100:.0f} %" if w + l else ""))
    print("→ ouvre index.html (version locale) ou lance server.py (version live)")


if __name__ == "__main__":
    main()
