# 📘 Fiche complète — Mise en place sur PythonAnywhere

Ce document explique **ce que fait le projet** et **comment tout installer**, pas
à pas, de zéro. Aucune connaissance technique préalable n'est supposée.

---

## 1. Ce que contient le projet

Le projet a **deux outils indépendants** (tu peux utiliser l'un, l'autre, ou les deux) :

| Outil | Fichier | Rôle |
|---|---|---|
| 🤖 **Bot d'alertes** | `btc_sniper.py` | Analyse BTC toutes les 4h et envoie des signaux d'achat/vente sur **Telegram** |
| 📊 **Tableau de bord** | `dashboard.py` | Page web qui montre **tes portefeuilles** (Bitget/Bybit) + des **données de marché** en temps réel (OI, long/short, CVD, funding…) |

> 🔒 **Tout est en lecture seule.** Aucun outil ne peut trader ni retirer de
> l'argent. On utilise des clés API « lecture » et jamais de phrase secrète.

---

## 2. Comment ça marche (en bref)

```
                ┌────────────────────────────────────────────┐
                │                PythonAnywhere                 │
                │                                                │
  Bitget API ──▶│  dashboard.py ──▶ page web protégée (HTTPS)   │◀── ton navigateur
  Bybit  API ──▶│  btc_sniper.py ──▶ messages Telegram          │──▶ ton téléphone
  CoinGecko  ──▶│                                                │
                └────────────────────────────────────────────┘
```

- **`dashboard.py`** lit tes soldes (clés API lecture seule) + les données de
  marché publiques (Bybit, Bitget) + les prix (CoinGecko), et affiche tout sur
  une page web protégée par mot de passe.
- **`btc_sniper.py`** tourne en boucle, analyse le marché, et t'écrit sur Telegram.

Détail des fichiers :

| Fichier / dossier | Ce qu'il fait |
|---|---|
| `portfolio/exchanges.py` | Lit les soldes via CCXT (Bitget/Bybit) |
| `portfolio/markets.py` | OI, ratio long/short, CVD, funding, prix (Bybit + Bitget) |
| `portfolio/prices.py` | Prix des actifs via CoinGecko |
| `portfolio/aggregate.py` | Additionne tout, calcule la valeur totale |
| `dashboard.py` + `templates/` | La page web (connexion + affichage) |
| `.env` | **Tes secrets** (clés, mots de passe) — privé, jamais sur GitHub |

---

## 3. Avant de commencer — ce qu'il te faut

1. Un **compte PythonAnywhere payant** (le plan « Hacker » à ~5$/mois suffit ;
   il débloque l'accès internet complet + une tâche always-on).
2. Tes comptes **Bitget** et/ou **Bybit**.
3. (Optionnel, pour le bot d'alertes) un **bot Telegram** (voir étape 6).

---

## 4. Récupérer le code sur PythonAnywhere

1. Connecte-toi à PythonAnywhere.
2. Onglet **Consoles** → ouvre une console **Bash**.
3. Tape :
   ```bash
   git clone https://github.com/moide2004/btc-sniper.git
   cd btc-sniper
   pip install --user -r requirements.txt
   ```

---

## 5. Créer tes clés API en LECTURE SEULE

### 🏦 Bitget
1. Connecte-toi à Bitget → **API Management** (Gestion des API).
2. **Create API** → choisis **System-generated** (clé système).
3. **Permissions : coche UNIQUEMENT « Read-only » (Lecture seule).**
   Ne coche **jamais** « Trade » ni « Withdraw ».
4. Tu obtiens **3 valeurs** : `API Key`, `Secret Key`, et une **Passphrase**
   (le mot de passe que tu choisis). Note les trois.

### 🏦 Bybit
1. Connecte-toi à Bybit → **API** → **Create New Key**.
2. Choisis **System-generated API Keys**.
3. **Permissions : uniquement « Read-Only ».** Pas de trading, pas de retrait.
4. Tu obtiens `API Key` et `Secret`.

> 💡 Astuce sécurité : si l'exchange propose de **restreindre par adresse IP**,
> tu peux y mettre l'IP de ta web app PythonAnywhere (affichée dans l'onglet Web).

---

## 6. (Optionnel) Créer le bot Telegram — pour `btc_sniper.py`

1. Sur Telegram, ouvre **@BotFather** → `/newbot` → suis les instructions.
   Tu obtiens un **TOKEN**.
2. Pour ton **chat id** : écris un message à ton bot, puis ouvre dans un
   navigateur `https://api.telegram.org/bot<TON_TOKEN>/getUpdates` et repère
   le champ `"chat":{"id": ...}`.

---

## 7. Remplir le fichier secret `.env`

Dans la console Bash (toujours dans le dossier `btc-sniper`) :
```bash
cp .env.example .env
nano .env
```
Remplis ce dont tu as besoin (laisse vide ce que tu n'utilises pas) :

```ini
# --- Tableau de bord ---
DASHBOARD_PASSWORD=choisis_un_mot_de_passe_solide
FLASK_SECRET_KEY=colle_ici_une_valeur_aleatoire

# Bitget (lecture seule)
BITGET_API_KEY=...
BITGET_API_SECRET=...
BITGET_API_PASSWORD=...

# Bybit (lecture seule)
BYBIT_API_KEY=...
BYBIT_API_SECRET=...

# --- Bot d'alertes (optionnel) ---
TELEGRAM_TOKEN=...
TELEGRAM_CHATID=...
```

Pour générer `FLASK_SECRET_KEY`, tape dans la console :
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```
Sauvegarde nano : `Ctrl+O`, `Entrée`, puis `Ctrl+X`.

> ℹ️ Les **données de marché** (OI, long/short, CVD…) sont **publiques** :
> elles fonctionnent sans aucune clé.

---

## 8. Mettre en ligne le TABLEAU DE BORD (page web)

1. Onglet **Web** → **Add a new web app**.
2. Choisis **Flask** → la version de **Python 3.x**.
3. PythonAnywhere crée un fichier WSGI. Clique sur son lien (section *Code*) et
   **remplace tout son contenu** par :
   ```python
   import sys
   path = "/home/TONPSEUDO/btc-sniper"   # remplace TONPSEUDO
   if path not in sys.path:
       sys.path.insert(0, path)

   from dashboard import app as application
   ```
4. Dans la section **Virtualenv / Working directory**, mets le **Working
   directory** sur `/home/TONPSEUDO/btc-sniper`.
5. Clique sur le gros bouton vert **Reload**.
6. Ouvre l'adresse de ta web app (ex. `https://tonpseudo.pythonanywhere.com`).
   Tu arrives sur la page de connexion → entre ton `DASHBOARD_PASSWORD`. ✅

> 🔐 **HTTPS** est fourni automatiquement par PythonAnywhere, donc la connexion
> est chiffrée.

---

## 9. (Optionnel) Faire tourner le BOT D'ALERTES 24h/24

1. Onglet **Tasks** → section **Always-on tasks**.
2. Colle cette commande (remplace `TONPSEUDO`) :
   ```
   python3 /home/TONPSEUDO/btc-sniper/btc_sniper.py
   ```
3. Clique sur **Create**. Le bot tourne en continu.

---

## 10. Si Bybit est bloqué (erreur 403)

Bybit filtre certaines régions/IP. Le code essaie déjà **automatiquement** le
miroir `api.bytick.com`. Si malgré tout Bybit reste bloqué :

1. Dans `.env`, force le miroir pour les données ET les soldes :
   ```ini
   BYBIT_HOSTS=api.bytick.com,api.bybit.com
   BYBIT_HOSTNAME=bytick.com
   ```
2. Recharge la web app (bouton **Reload**).
3. Si ça ne suffit pas (blocage géographique strict), il faudra héberger sur un
   serveur situé dans une région autorisée (ex. un petit VPS en Europe). Le code
   est prêt pour ça — seul l'hébergeur change. **Bitget, lui, n'est pas
   concerné** et fonctionne partout.

---

## 11. Rappel sécurité (à garder en tête)

- ✅ Clés API **lecture seule** uniquement (jamais trade/retrait).
- ✅ Wallets : **adresse publique** seulement, **jamais** de phrase secrète.
- ✅ Secrets dans `.env` (privé) — jamais dans le code, jamais sur GitHub.
- ✅ Page web protégée par **mot de passe** + **HTTPS**.
- ⚠️ Ton ancien token Telegram (avant sécurisation) doit être **régénéré** via
  @BotFather, car il a figuré en clair dans l'historique.

---

## 12. Résumé express

```bash
# 1. Récupérer le code
git clone https://github.com/moide2004/btc-sniper.git && cd btc-sniper
pip install --user -r requirements.txt

# 2. Configurer les secrets
cp .env.example .env && nano .env

# 3. Tableau de bord  -> onglet Web (WSGI vers "from dashboard import app as application")
# 4. Bot d'alertes    -> onglet Tasks (always-on: python3 .../btc_sniper.py)
```

Page web = ton « CoinStats perso ». Bot = tes alertes Telegram. 🎯
