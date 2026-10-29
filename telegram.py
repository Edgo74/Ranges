"""Bot Telegram : envoie les alertes et relie la conversation automatiquement.

Il suffit d'envoyer /start au bot : la conversation est enregistrée (DATA_DIR/telegram.json) et reçoit
ensuite les alertes. Une fois reliée, le bot refuse les autres conversations.
Commandes : /start (relier), /status (état), /4h, /daily, /weekly, /monthly suivis de off|on (couper un timeframe).
"""
import json, os, threading, time, urllib.parse, urllib.request


class Telegram:
    def __init__(self, token, data_dir, chat_id="", status_fn=None):
        self.token = token
        self.path = os.path.join(data_dir, "telegram.json")
        self.status_fn = status_fn
        saved = self._load()
        self.chat_id = chat_id or saved.get("chat_id")
        self.muted = set(saved.get("muted", []))  # timeframes coupés : "H4", "D", "W", "M"
        self.offset = 0

    def _load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return {}

    def _api(self, method, params, timeout=20):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = urllib.parse.urlencode(params).encode()
        with urllib.request.urlopen(url, data=data, timeout=timeout) as r:
            return json.load(r)

    def send(self, text, chat_id=None):
        chat_id = chat_id or self.chat_id
        if not chat_id:
            return False
        for i in range(3):
            try:
                self._api("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"})
                return True
            except Exception as e:
                print(f"Telegram : envoi impossible ({e})", flush=True)
                time.sleep(2 + 3 * i)
        return False

    def start(self):
        threading.Thread(target=self._poll, daemon=True).start()

    def _poll(self):
        while True:
            try:
                res = self._api("getUpdates", {"offset": self.offset, "timeout": 50}, timeout=60)
                for u in res.get("result", []):
                    self.offset = u["update_id"] + 1
                    msg = u.get("message") or {}
                    chat, text = str((msg.get("chat") or {}).get("id", "")), (msg.get("text") or "").strip()
                    if chat and text:
                        self._command(chat, text)
            except Exception:
                time.sleep(5)

    def _command(self, chat, text):
        cmd = text.split()[0].split("@")[0].lower()
        if self.chat_id and chat != str(self.chat_id):
            self.send("Ce bot est déjà relié à une autre conversation.", chat)
            return
        if cmd == "/start":
            if not self.chat_id:
                self.chat_id = chat
                self._save()
            self.send("✅ Alertes FVG activées ici.\nTu recevras un message au <b>premier retour</b> du prix sur un FVG "
                      "daily, weekly ou monthly (tous les perps Hyperliquid) et 4h (30 coins les plus échangés).\n"
                      "/status pour voir l'état · /4h off pour couper le 4h (idem /daily, /weekly, /monthly).", chat)
        elif cmd == "/status" and self.status_fn:
            off = ", ".join({"H4": "4h", "D": "daily", "W": "weekly", "M": "monthly"}[k] for k in sorted(self.muted))
            self.send(self.status_fn() + (f"\nCoupé : {off}" if off else ""), chat)
        elif cmd in ("/4h", "/daily", "/weekly", "/monthly"):
            tf = {"/4h": "H4", "/daily": "D", "/weekly": "W", "/monthly": "M"}[cmd]
            arg = text.split()[1].lower() if len(text.split()) > 1 else ""
            if arg not in ("on", "off"):
                self.send(f"Utilise {cmd} on ou {cmd} off.", chat)
                return
            (self.muted.discard if arg == "on" else self.muted.add)(tf)
            self._save()
            self.send(f"Alertes {cmd[1:]} {'réactivées ✅' if arg == 'on' else 'coupées 🔕'}.", chat)

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"chat_id": self.chat_id, "muted": sorted(self.muted)}, f)
        os.replace(tmp, self.path)
