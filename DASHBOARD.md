# Tableau de bord portefeuille (style CoinStats, perso)

Agrège en **lecture seule** tes comptes crypto (exchanges + wallets) et affiche
la valeur totale en temps réel sur une page web protégée par mot de passe.

## Sources supportées (v1)

| Source | Connexion | Ce qui est lu |
|---|---|---|
| **Bitget** (et Binance, Bybit) | Clé API **lecture seule** | Tous les soldes |
| **MetaMask** (Ethereum) | **Adresse publique** | ETH natif* |
| **Phantom** (Solana) | **Adresse publique** | SOL + tous les tokens SPL |

\* L'énumération automatique des tokens ERC-20 d'une adresse Ethereum
nécessite un service d'indexation (clé API) — prévu pour une prochaine version.

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
   - `EVM_ADDRESSES` : ton/tes adresses MetaMask publiques (séparées par virgules)
   - `SOLANA_ADDRESSES` : ton/tes adresses Phantom publiques

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
