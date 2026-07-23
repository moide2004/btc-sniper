# Bot Déséquilibré — P1 + P2 (Socle données + moteur Fibonacci/probas, BTC + ETH)

Analyste de trading multi-timeframes fondé sur les cassures de Fibonacci
(**il signale, il n'exécute jamais**).

**Phase 1 livrée** : socle de données dual-actif (cache 1m parquet segmenté par
mois, SQLite WAL), source robuste (websocket combiné Binance BTC+ETH + backfill
REST + bascule Kraken/Coinbase/CoinGecko), worker always-on idempotent +
heartbeat, tâche quotidienne, mini-app web de lecture.

**Phase 2 livrée** : moteur §2 (détection de setups « déséquilibre » sur cassure
Fibonacci, sur **toutes** les timeframes d'analyse 15m→1D, pas seulement le 1m
qui n'est que le substrat) + couche probabiliste §3 (double barrière TP/SL
mesurée sur le 1m, Wilson 95 %, Beta q25, EV nette taker+maker, réalisme, walk-
forward). Émission de **tickets** annotés « solide »/« spéculatif » dès qu'un
setup est valide ; recalcul des tables la nuit (00:10 UTC).

> Aucune notification externe. Aucun ordre réel, jamais. Voir `BRIEF.md`.

## ⚠️ Point à confirmer (§9.1) — orientation Fibonacci
La géométrie du SL du BRIEF (« SL long **sous** 78,6 %, SL short **au-dessus** de
23,6 % ») n'est cohérente **que** si les pourcentages sont mesurés **en
retracement depuis le haut** : `level(p) = H − p·R` (0 %=haut, 100 %=bas), donc
**23,6 % est proche du haut** et **78,6 % proche du bas**. C'est l'orientation
implémentée (voir `core/indicators.py`). Un **long** est alors une cassure par le
haut (nouveau plus-haut) et son stop est placé bas (78,6 %). Si ton intention
était l'orientation inverse, dis-le : c'est un réglage isolé, sans toucher au
reste. Aucune autre règle/paramètre n'a été ajouté.

---

## 1. Architecture (rappel)
```
core/   store.py · config.py · data_source.py · logging_setup.py
jobs/   worker.py (always-on) · daily_update.py (planifié 00:10 UTC)
web/    app.py (Flask, LECTURE SEULE) · templates/ · static/
data/   moteur.db (SQLite WAL) · ohlcv/ (parquet ; 1m segmenté par mois/actif)
logs/   rotatifs 10 Mo × 5   · backups/ (instantanés quotidiens, 14 j)
```
- **Un seul écrivain** : le worker écrit `moteur.db`, la web app lit (mode `ro`).
- **Une seule vérité de prix** par actif : le 1m ; les TF ≥ 15m en découlent.
- **UTC partout.** 15m = plancher d'analyse (rien en dessous).

---

## 2. Installation locale (test avant PythonAnywhere)
```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # puis éditer (§4)
python -c "import secrets; print('FLASK_SECRET_KEY=' + secrets.token_hex(32))"
python -c "from werkzeug.security import generate_password_hash as g; print('WEB_PASSWORD_HASH=' + g('MON_MDP'))"
python jobs/worker.py            # worker (Ctrl-C pour arrêter)
python jobs/daily_update.py      # cycle quotidien
flask --app web/app.py run       # web app sur http://127.0.0.1:5000
```

---

## 3. Déploiement eu.pythonanywhere.com (payant) — pas à pas

### 3.0 P0 — Vérifier le géoblocage (console Bash, AVANT tout)
```bash
curl -s -o /dev/null -w "spot %{http_code}\n" https://api.binance.com/api/v3/ping
curl -s -o /dev/null -w "perp %{http_code}\n" https://fapi.binance.com/fapi/v1/ping
```
- **200 / 200** → Binance disponible (attendu sur les serveurs EU). Rien à changer.
- **451** → mettre `DATA_PRIMARY=kraken` dans `.env` : Kraken devient primaire
  (le websocket combiné Binance est alors inactif, le worker ingère par REST).

### 3.1 Récupérer le code + installer
```bash
git clone https://github.com/moide2004/btc-sniper.git bot && cd bot/bot-desequilibre
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 3.2 Configurer `.env`
Renseigner au minimum `WEB_PASSWORD_HASH` et `FLASK_SECRET_KEY` (voir §2).
`SYMBOLS=BTCUSDT,ETHUSDT` par défaut. Aucun secret en dur, jamais commité.

### 3.3 Always-on task (le worker)
Onglet **Tasks → Always-on tasks → Add**, commande exacte :
```
/home/VOTRE_USER/bot/bot-desequilibre/.venv/bin/python /home/VOTRE_USER/bot/bot-desequilibre/jobs/worker.py
```
Premier démarrage : backfill ~`BACKFILL_DAYS` jours de 1m pour BTC ET ETH.

### 3.4 Tâche planifiée (00:10 UTC)
Onglet **Tasks → Scheduled tasks**, heure **00:10**, commande exacte :
```
/home/VOTRE_USER/bot/bot-desequilibre/.venv/bin/python /home/VOTRE_USER/bot/bot-desequilibre/jobs/daily_update.py
```

### 3.5 Web app (WSGI)
Onglet **Web → Add a new web app → Manual configuration → Python 3.11**.
- **Source code** : `/home/VOTRE_USER/bot/bot-desequilibre`
- **Virtualenv** : `/home/VOTRE_USER/bot/bot-desequilibre/.venv`
- **WSGI file** : remplacer le contenu par :
```python
import sys
path = "/home/VOTRE_USER/bot/bot-desequilibre"
if path not in sys.path:
    sys.path.insert(0, path)
from web.app import app as application
```
- Renseigner les variables d'env (ou via `.env`, lu par `core/config.py`).
- Cliquer **Reload**. URL protégée par mot de passe ; « live » par polling.

---

## 4. Variables d'environnement (`.env`)
| Clé | Rôle | Défaut |
|---|---|---|
| `SYMBOLS` | Actifs analysés | `BTCUSDT,ETHUSDT` |
| `BACKFILL_DAYS` | Historique 1m au 1er démarrage | `730` |
| `CAPITAL_USD` | Capital PARTAGÉ BTC+ETH | `3000` |
| `RISK_PCT` / `SHORT_RISK_FACTOR` | Dimensionnement (§2.6) | `0.015` / `0.75` |
| `FEE_TAKER` / `FEE_MAKER` | Coûts affichés (§3) | `0.0010` / `0.0003` |
| `DATA_PRIMARY` / `DATA_FALLBACKS` | Ordre des sources | `binance` / `kraken,coinbase,coingecko` |
| `WEB_PASSWORD_HASH` / `FLASK_SECRET_KEY` | Sécurité web | *(obligatoires)* |

---

## 5. Restauration (§7.7 esprit)
Sauvegardes dans `backups/AAAA-MM-JJ/moteur.db` (14 j, garde anti-corruption).
Restaurer : arrêter le worker → `cp backups/AAAA-MM-JJ/moteur.db data/moteur.db`
→ `rm -f data/moteur.db-wal data/moteur.db-shm` → relancer le worker (le 1m se
recomble seul). Les caches parquet se reconstruisent via backfill.

---

## 6. Tests d'acceptation P1 (§8)
- **(a) Tuer le worker → relance auto + reprise sans trou** : Stop/Start
  l'always-on ; le log montre `Continuité 1m BTCUSDT/ETHUSDT : … 0 trou(s)` et
  l'événement « Worker (re)démarré ».
- **(b) Couper le websocket → reconnexion + backfill** : backoff 1 s→60 s dans
  `logs/worker.log`, comblage REST à la reconnexion. À défaut de ws (géo-blocage),
  bascule en ingestion REST de repli après `PRIMARY_SILENCE_SECONDS`.
- **(c) 48 h sans bougie 1m manquante sur les DEUX actifs** : `/api/health`
  (`par_actif`) + contrôle de continuité à chaque maintenance/cycle quotidien.

Banc local sans réseau : `python tests/test_p1.py` (17 tests) — démontre store,
isolation dual-actif, append segmenté, continuité, reprise idempotente.

## 7. Moteur §2/§3 (P2) — lecture

- **Endpoints web** : `/api/probas` (matrice actif×TF×direction : p̂, Wilson,
  p_prudent, EV taker/maker, k_max, CVaR99, walk-forward) et `/api/tickets`
  (setups vivants annotés). Le rendu 4 vues complet arrive en **P3**.
- **Recalcul manuel** des tables §3 (sinon automatique à 00:10 UTC) :
  `python jobs/daily_update.py`.
- **Tickets** : émis par le worker sur chaque bougie d'analyse close avec setup ;
  visibles via `/api/tickets` et la cloche interne (événements `ticket`).
- **Paramètres §2** (dans `.env`, défauts épinglés du BRIEF ; ceux sans défaut
  explicite sont marqués (†) dans `core/config.py`, à confirmer §9.1) :
  `FIB_LOOKBACK=50`, `RR_MULT=1.5`, `STOP_MAX_ATR=3`, `STOP_MIN_ATR=0.5`,
  `SHORT_RISK_FACTOR=0.75`, `RISK_PCT=0.015`, `BUFFER_ATR`, `MIN_AMP_ATR`,
  `RR_GRID=1.0,1.5,2.0`, `ATR_PERIOD`, `ANALYSIS_TIMEFRAMES=15m,30m,1h,4h,12h,1D`.

Banc local sans réseau : `python tests/test_p2.py` (41 tests) — géométrie
Fibonacci, détection §2, double barrière, Wilson/Beta/EV/réalisme, budget §5.4,
store probas/tickets, orchestrateur (recalcul + scan live idempotent).
