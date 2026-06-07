# Tableau de bord portefeuille (style CoinStats, perso)

Agrège en **lecture seule** tes comptes crypto (exchanges + wallets) et affiche
la valeur totale en temps réel sur une page web protégée par mot de passe.

## Ce que montre le tableau de bord

**1. Analyse BTC Sniper** — le cerveau de ton bot, affiché à l'écran :
score de conviction, direction (LONG/SHORT), structure 1W/1D/4H, RSI, funding,
CVD, POC/VAH/VAL, et les niveaux **SL / TP1 / TP2** avec le ratio R/R.
Si Bybit est bloqué, l'analyse bascule automatiquement sur Bitget.

**2. Tes comptes (montant des portefeuilles)** — soldes via clé API lecture seule :

| Source | Connexion | Ce qui est lu |
|---|---|---|
| **Bitget** / **Bybit** (et Binance) | Clé API **lecture seule** | Tous les soldes |

**3. Données de marché en temps réel** — via endpoints **publics** (aucune clé) :

| Donnée | Bybit | Bitget |
|---|---|---|
| Prix, variation 24h, funding, volume | ✅ | ✅ |
| Open Interest (+ variation) | ✅ (intervalle 5 min) | ✅ (Δ depuis dernier refresh) |
| Ratio Long / Short des comptes | ✅ | ✅ |
| CVD (approximation depuis bougies) | ✅ | ✅ |

> Le **CVD** affiché est une **approximation** calculée depuis les bougies
> (position de la clôture dans la mèche × volume). Un CVD exact nécessiterait
> le détail des trades agressifs, non fourni par les endpoints publics.

## 🔒 Sécurité (rappel)

- **Exchanges** : génère des clés API en cochant **uniquement « lecture »** —
  jamais « trading » ni « retrait ».
- **Wallets** : on n'utilise que ton **adresse publique**. **JAMAIS** ta phrase
  secrète (12/24 mots) ni ta clé privée. Aucun outil légitime n'en a besoin
  pour lire un solde.
- Tous les secrets vivent dans `.env` (privé, jamais sur GitHub).
- La page web est **protégée par mot de passe** + **HTTPS** (PythonAnywhere).

## Configuration

1. Copie le modèle et remplis-le :
   ```bash
   cp .env.example .env
   ```
2. Renseigne au minimum :
   - `DASHBOARD_PASSWORD` : ton mot de passe pour la page web.
   - `FLASK_SECRET_KEY` : `python3 -c "import secrets; print(secrets.token_hex(32))"`
   - `BITGET_API_KEY` / `BITGET_API_SECRET` / `BITGET_API_PASSWORD` (lecture seule)
   - `BYBIT_API_KEY` / `BYBIT_API_SECRET` (lecture seule) pour les soldes Bybit
   - (optionnel) `MARKET_SYMBOLS` : ex. `BTCUSDT,ETHUSDT` — les données de
     marché sont publiques et ne nécessitent aucune clé.

## Lancer en local (pour tester)

```bash
pip install -r requirements.txt
python3 dashboard.py
```
Puis ouvre http://127.0.0.1:5000 et connecte-toi avec `DASHBOARD_PASSWORD`.

## Héberger sur PythonAnywhere (24h/24)

1. Onglet **Web** → **Add a new web app** → **Flask** → Python 3.x.
2. Dans la config de la web app, fais pointer le **WSGI** vers `dashboard.py`
   et l'objet `app` (variable `application = app`), et règle le
   **Source code / Working directory** sur le dossier `btc-sniper`.
3. Onglet **Files** → crée le fichier `.env` (avec tes clés et adresses).
4. Recharge la web app (bouton **Reload**). HTTPS est fourni automatiquement.

> Le bot d'alertes (`btc_sniper.py`) et le tableau de bord (`dashboard.py`)
> sont indépendants : tu peux faire tourner l'un, l'autre, ou les deux.
