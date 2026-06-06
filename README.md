# BTC Sniper

Bot d'**alertes** de trading pour BTCUSDT (perpetuels Bybit). Il analyse le
marche en multi-timeframe (1W / 1D / 4H) via une confluence d'indicateurs
(structure de marche, order blocks, FVG, CVD, volume profile, RSI, funding,
open interest), calcule un score de conviction, et envoie les signaux
(entree / SL / TP1 / TP2) sur Telegram. Il suit ensuite la position et
notifie quand TP1, TP2 ou SL sont atteints.

> ⚠️ Ce script **n'execute aucun ordre** : il envoie uniquement des alertes.
> Ce n'est pas un conseil financier. Les seuils et ponderations sont des
> choix heuristiques a backtester avant tout usage reel.

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Les secrets ne sont **plus codes en dur**. Copiez le modele et remplissez-le :

```bash
cp .env.example .env
# editez .env : TELEGRAM_TOKEN et TELEGRAM_CHATID
```

| Variable | Defaut | Role |
|---|---|---|
| `TELEGRAM_TOKEN` | — | Token du bot Telegram (obligatoire) |
| `TELEGRAM_CHATID` | — | Chat destinataire (obligatoire) |
| `SYMBOL` | `BTCUSDT` | Paire tradee |
| `SCAN_INTERVAL` | `14400` | Secondes entre deux scans (4h) |
| `MIN_SCORE` | `82` | Score minimum pour un signal |
| `MIN_RR` | `3.0` | Ratio risque/recompense minimum |
| `RISK_PCT` | `3.0` | Risque par trade en % (affichage) |

## Lancement

```bash
set -a; source .env; set +a
python3 btc_sniper.py
```

Aucune cle API Bybit n'est requise (endpoints publics en lecture seule).
