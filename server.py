#!/usr/bin/env python3
"""Ranges — graphiques multi-timeframes des coins Hyperliquid.

Le serveur ne fait que servir la page (protégée par APP_PASSWORD) : les bougies sont chargées
directement par le navigateur depuis l'API Hyperliquid, et mises à jour en temps réel par WebSocket.

Variables d'environnement : PORT (8000), APP_PASSWORD (vide = pas de mot de passe).
"""
import base64, hmac, json, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", 8000))
PASSWORD = os.environ.get("APP_PASSWORD", "")


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
        path = self.path.split("?")[0]
        if path == "/health":
            return self.send(200, json.dumps({"ok": True}).encode(), "application/json")
        if not self.authed():
            return
        if path in ("/", "/index.html"):
            with open(os.path.join(ROOT, "web", "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        self.send(404, b"introuvable", "text/plain; charset=utf-8")


if __name__ == "__main__":
    print(f"Ranges sur http://localhost:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
