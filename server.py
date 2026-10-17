#!/usr/bin/env python3
"""Ranges — graphiques multi-timeframes des coins Hyperliquid.

Le serveur ne fait que servir la page (protégée par APP_PASSWORD) : les bougies sont chargées
directement par le navigateur depuis l'API Hyperliquid, et mises à jour en temps réel par WebSocket.

Les dessins (lignes, rectangles, positions…) sont enregistrés par coin dans DATA_DIR/drawings.json.

Variables d'environnement : PORT (8000), APP_PASSWORD (vide = pas de mot de passe), DATA_DIR (./data).
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
    print(f"Ranges sur http://localhost:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
