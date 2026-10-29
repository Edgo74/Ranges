# Ranges — graphiques multi-timeframes Hyperliquid

Outil de recherche visuelle des ranges : pour chaque coin, **4 graphiques côte à côte (15 min, 1 H, 4 H, 1 D)**.

- À gauche : les **30 coins avec le plus de volume sur 24 h sur Hyperliquid** (perps tradables uniquement),
  avec prix et variation ; la recherche couvre tous les perps Hyperliquid.
- Vue **Barres** (OHLC) ou **Bougies**, au choix (touche B).
- Glisser un graphique vers la gauche charge l'historique plus ancien (jusqu'aux 5000 dernières bougies
  que garde Hyperliquid par timeframe).
- Mise à jour en temps réel (WebSocket Hyperliquid).
- Raccourcis : ↑ ↓ changer de coin · 1–4 agrandir un graphique · Échap revenir à la grille.
- **Tiroir 1D · 1W · 1M** : onglet sur le bord droit (ou touche W), par-dessus les 4 graphiques, élargissable en tirant
  son bord. Le 1W (semaines du lundi) et le 1M (mois calendaires) sont construits à partir des bougies journalières.
- **Outils de dessin** : ligne horizontale (H), ligne de tendance (T), rectangle/range avec sa hauteur en % (R),
  position longue (L) et courte (S) avec objectif, stop, % et R:R, mesure % / prix / bougies / durée (M),
  6 couleurs, Suppr pour effacer. Un dessin apparaît sur les 4 timeframes du coin et il est enregistré
  sur le serveur (`DATA_DIR/drawings.json`), donc retrouvé sur tous tes appareils.

Les bougies sont chargées directement par le navigateur depuis l'API publique Hyperliquid ;
le serveur ne fait que servir la page derrière un mot de passe.

## Alertes FVG (Telegram)

Le serveur surveille en continu **tous les perps Hyperliquid** et repère les Fair Value Gaps **4h, daily, weekly
(semaines du lundi) et monthly (mois calendaires)** :

- FVG haussier : bas de la 3e bougie au-dessus du haut de la 1re (zone entre les deux) ; baissier : l'inverse.
- Tailles minimales : 1 % 4h · 0,5 % daily · 1 % weekly · 2 % monthly. Ancienneté max : 2 semaines 4h · 6 mois daily ·
  4 ans weekly/monthly.
- 4h : visible sur le site pour tous les coins, mais **alerte Telegram seulement pour les 30 coins les plus échangés**
  (sinon ~70 messages par jour).
- **Alerte au premier retour du prix sur la zone** (une mèche suffit), une seule fois : le FVG est ensuite consommé.
- Délai : prix vérifié toutes les 10 s, bougie du jour relue toutes les ~10 min (pour les mèches rapides).
- Onglet **FVG** du site : FVG intacts triés par distance au prix, historique des alertes ; case « FVG » pour
  afficher les zones sur les graphiques.

Mise en route : variable `TELEGRAM_BOT_TOKEN` (token donné par @BotFather), puis envoyer `/start` au bot.
Commandes du bot : `/status`, `/4h off|on`, `/daily off|on`, `/weekly off|on`, `/monthly off|on`.

## Lancer en local

```bash
python3 server.py            # http://localhost:8000
```

Aucune dépendance (Python 3.9+). Mot de passe : `APP_PASSWORD=... python3 server.py`.

## Railway

Déployé depuis ce repo. Variables : `APP_PASSWORD`, `DATA_DIR=/data` (le volume garde dessins, état des FVG et liaison Telegram) et `TELEGRAM_BOT_TOKEN`.

L'ancien détecteur automatique de signaux est dans l'historique Git (commit `cefee1e`).
