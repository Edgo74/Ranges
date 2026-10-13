# Range dans le range — détecteur live

Repère sur Hyperliquid un **range serré** dans lequel se forme un **petit range collé à un bord**
(petit range en haut → le prix va souvent chercher le bas, et inversement).

- Scan automatique toutes les 5 minutes, juste après la clôture des bougies (5m et 15m, 30 coins les plus échangés).
- Interface : liste des signaux (en cours / réussis / ratés), graphique en barres OHLC, boutons **Oui / Non**.
- Tes jugements sont enregistrés sur le serveur ; « Exporter jugements » télécharge un fichier avec les signaux
  et leurs graphiques, pour affiner la détection.
- ↻ Rafraîchir (un graphique) et « Tout rafraîchir » (relance le scan).

## Lancer en local

```bash
python3 server.py            # http://localhost:8000
```

Aucune dépendance à installer (Python 3.9+). Pour protéger par mot de passe : `APP_PASSWORD=... python3 server.py`.

## Déployer sur Railway

1. Pousse ce dossier sur un repo GitHub **privé**.
2. Railway → New Project → Deploy from GitHub repo.
3. Dans le service : **Add Volume** monté sur `/data`.
4. Variables : `APP_PASSWORD` (mot de passe de l'interface) et `DATA_DIR=/data`.
5. Settings → Networking → **Generate Domain**.
6. Vérifie que **App Sleeping / Serverless est désactivé** (sinon le scan s'arrête quand personne ne regarde).

Au premier démarrage, le serveur rattrape les 2 derniers jours (~3 min), puis scanne toutes les 5 minutes.

## Réglages

| Variable | Défaut | Rôle |
|---|---|---|
| `APP_PASSWORD` | (vide) | mot de passe de l'interface — obligatoire en ligne |
| `DATA_DIR` | `./data` | où est stockée la base (signaux + jugements) |
| `COINS` | `30` | nombre de coins (les plus gros volumes) |
| `TFS` | `5m,15m` | timeframes scannés |
| `SCAN_EVERY_MIN` | `5` | fréquence du scan |
| `BACKFILL_DAYS` | `2` | historique rattrapé au premier démarrage |
| `KEEP_DAYS` | `30` | durée de conservation des signaux |

Les paramètres du motif (touches, tolérances, tailles des ranges) sont en haut de `detect.py`.

## Script ponctuel (sans serveur)

```bash
python3 detect.py --days 3 --only AVAX,BTC --tf 15m   # puis ouvrir index.html
```
