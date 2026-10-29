#!/usr/bin/env python3
"""Ranges — graphiques multi-timeframes des coins Hyperliquid.

Le serveur ne fait que servir la page (protégée par APP_PASSWORD) : les bougies sont chargées
directement par le navigateur depuis l'API Hyperliquid, et mises à jour en temps réel par WebSocket.

Les dessins (lignes, rectangles, positions…) sont enregistrés par coin dans DATA_DIR/drawings.json.

Surveille aussi les FVG daily / weekly / monthly de tous les perps et envoie une alerte Telegram au premier
retour du prix (voir fvg.py, telegram.py).

Variables d'environnement : PORT (8000), APP_PASSWORD (vide = pas de mot de passe), DATA_DIR (./data),
TELEGRAM_BOT_TOKEN (token du bot), TELEGRAM_CHAT_ID (facultatif : sinon envoyer /start au bot),
SITE_URL (facultatif : lien dans les alertes ; sur Railway, le domaine public est détecté tout seul).
"""
import base64, hmac, json, os, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", 8000))
PASSWORD = os.environ.get("APP_PASSWORD", "")
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(ROOT, "data"))
DRAWINGS = os.path.join(DATA_DIR, "drawings.json")
os.makedirs(DATA_DIR, exist_ok=True)
lock = threading.Lock()

from fvg import FVGWatcher, RULES  # noqa: E402
from telegram import Telegram  # noqa: E402

SITE_URL = os.environ.get("SITE_URL") or (f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}" if os.environ.get("RAILWAY_PUBLIC_DOMAIN") else "")
watcher = FVGWatcher(DATA_DIR, SITE_URL)


def status_text():
    st = watcher.status
    n = sum(len(v) for v in watcher.intact.values())
    by = {k: sum(1 for v in watcher.intact.values() for f in v if f["tf"] == k) for k in RULES}
    load = "prêt" if st["ready"] else f"chargement {st['coinsLoaded']}/{st['coins']} coins"
    return (f"📊 Surveillance FVG : {load}\n{n} FVG intacts (4h {by['H4']} · daily {by['D']} · weekly {by['W']} · monthly {by['M']})\n"
            f"{len(watcher.alerts)} alerte(s) envoyée(s)")


def read_drawings():
    try:
        with open(DRAWINGS) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def write_drawings(coin, items):
    with lock:
        data = read_drawings()
        if items:
            data[coin] = items
        else:
            data.pop(coin, None)
        tmp = DRAWINGS + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, DRAWINGS)  # écriture atomique : jamais de fichier à moitié écrit


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def authed(self):
        if not PASSWORD:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                if hmac.compare_digest(base64.b64decode(h[6:]).decode().split(":", 1)[1], PASSWORD):
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="ranges"')
        self.end_headers()
        self.wfile.write("Mot de passe requis".encode())
        return False

    def send(self, code, data, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/health":
            return self.send(200, json.dumps({"ok": True}).encode(), "application/json")
        if not self.authed():
            return
        if url.path in ("/", "/index.html"):
            with open(os.path.join(ROOT, "web", "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if url.path == "/api/drawings":
            coin = parse_qs(url.query).get("coin", [""])[0]
            return self.send(200, json.dumps(read_drawings().get(coin, [])).encode(), "application/json")
        if url.path == "/api/fvg":
            coin = parse_qs(url.query).get("coin", [""])[0] or None
            return self.send(200, json.dumps(watcher.snapshot(coin), separators=(",", ":")).encode(), "application/json")
        self.send(404, b"introuvable", "text/plain; charset=utf-8")

    def do_PUT(self):
        url = urlparse(self.path)
        if not self.authed():
            return
        if url.path == "/api/drawings":
            coin = parse_qs(url.query).get("coin", [""])[0]
            n = int(self.headers.get("Content-Length") or 0)
            items = json.loads(self.rfile.read(n) or b"[]")
            if not coin or not isinstance(items, list):
                return self.send(400, b'{"error":"requete invalide"}', "application/json")
            write_drawings(coin, items)
            return self.send(200, b'{"ok":true}', "application/json")
        self.send(404, b"introuvable", "text/plain; charset=utf-8")


if __name__ == "__main__":
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if token:
        watcher.telegram = Telegram(token, DATA_DIR, os.environ.get("TELEGRAM_CHAT_ID", ""), status_text)
        watcher.telegram.start()
        print("Telegram : " + ("relié" if watcher.telegram.chat_id else "envoie /start au bot pour recevoir les alertes"), flush=True)
    else:
        print("Telegram : pas de TELEGRAM_BOT_TOKEN, alertes visibles seulement sur le site", flush=True)
    watcher.start()
    print(f"Ranges sur http://localhost:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
