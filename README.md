# Moteur de probabilités BTC

Instrument de mesure et de validation probabiliste multi-timeframes pour BTC.

**État des phases :**
- **P1 — Données & socle** ✅ : cache 1m parquet, SQLite WAL, websocket Binance +
  replis REST, worker always-on idempotent, heartbeat, cycle quotidien, web app
  de lecture (auth, polling).
- **P2 — Moteur statistique** ✅ : probabilités conditionnelles (horizon fixe +
  double barrière, deux sens), Wilson/Beta, EV nette (prudente), verdicts de
  coûts, matrice 11 timeframes.
- **P3 — Mini-app live** ✅ : synthèse bayésienne (κ=0,6, plafond 85 %),
  référence neutre, Vue 2 (détail états × directions), cloche avec badge +
  marquage lu.
- **P4 — Paper trading** ✅ (code) : tickets (§5.8), livre multi-étages 1h/4h/1D
  (§5.9), exécution limites (§5.10), exécuteur virtuel + journal + cartes de
  verdict (§5.11), walk-forward (§5.12), surveillance CUSUM + acquittement
  (§5.13), funding directionnel (§5.14). La période d'observation (60 j ou
  100 trades) court d'elle-même (suivi dans l'app).
- **v1.5 — Dimension volatilité** ✅ : percentile 1 an de vol EWMA (λ=0,94) en
  3 zones, activée case par case si chaque sous-case garde n ≥ 200 (§4) ;
  affichée en Vue 2, utilisée par les décisions quand active.
- **P5 — Bilan** ⏳ : à l'issue de la période P4 (métriques déjà suivies dans
  la carte « Période de validation »).

> Aucune notification externe. Aucun ordre réel, jamais. Voir `BRIEF.md`.

---

## 1. Architecture (rappel)

```
core/     store.py · config.py · data_source.py · states.py · logging_setup.py
jobs/     worker.py (always-on)  · daily_update.py (planifié 00:10 UTC)
web/      app.py (Flask, LECTURE SEULE) · templates/ · static/
data/     moteur.db (SQLite WAL)  · ohlcv/ (caches parquet par timeframe)
logs/     rotatifs 10 Mo × 5      · backups/ (instantanés quotidiens, 14 j)
```

- **Un seul écrivain** : le worker écrit `moteur.db`, la web app lit (mode `ro`).
- **Une seule vérité de prix** : le 1m ; les TF ≤ 1D en sont ré-échantillonnées.
- **UTC partout.**

---

## 2. Installation locale (test avant PythonAnywhere)

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # puis éditer .env (§4)
```

Générer les secrets pour `.env` :

```bash
python -c "import secrets; print('FLASK_SECRET_KEY=' + secrets.token_hex(32))"
python -c "from werkzeug.security import generate_password_hash as g; print('WEB_PASSWORD_HASH=' + g('CHOISIR_UN_MOT_DE_PASSE'))"
```

Lancer en local :

```bash
python jobs/worker.py           # worker (ingestion + heartbeat) — Ctrl-C pour arrêter
python jobs/daily_update.py     # cycle quotidien (à la demande)
flask --app web/app.py run      # web app sur http://127.0.0.1:5000
```

---

## 3. Déploiement PythonAnywhere (payant) — pas à pas

### 3.1 Récupérer le code

Onglet **Consoles → Bash** :

```bash
git clone https://github.com/moide2004/btc-sniper.git moteur-proba-btc
cd moteur-proba-btc
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 3.2 Configurer `.env`

Éditer `.env` (onglet **Files** ou `nano .env`). Renseigner au minimum
`WEB_PASSWORD_HASH` et `FLASK_SECRET_KEY` (voir §2 pour les générer), et adapter
`CAPITAL_USD`, `BACKFILL_DAYS` si besoin. **Aucun secret en dur, jamais commité.**

### 3.3 Always-on task (le worker)

Onglet **Tasks → Always-on tasks → Add**. Commande **exacte** :

```
/home/VOTRE_USER/moteur-proba-btc/.venv/bin/python /home/VOTRE_USER/moteur-proba-btc/jobs/worker.py
```

PythonAnywhere relance automatiquement cette tâche si elle plante ; le worker est
idempotent (reprise sans trou). Le premier démarrage backfill ~`BACKFILL_DAYS`
jours de 1m (peut durer quelques minutes selon la source).

### 3.4 Tâche planifiée (recalcul quotidien 00:10 UTC)

Onglet **Tasks → Scheduled tasks**. Heure **00:10 UTC**, commande **exacte** :

```
/home/VOTRE_USER/moteur-proba-btc/.venv/bin/python /home/VOTRE_USER/moteur-proba-btc/jobs/daily_update.py
```

> PythonAnywhere planifie en UTC : régler l'heure sur **00:10**.

### 3.5 Web app (WSGI)

Onglet **Web → Add a new web app → Manual configuration → Python 3.11**.

- **Virtualenv** : `/home/VOTRE_USER/moteur-proba-btc/.venv`
- **Source code** : `/home/VOTRE_USER/moteur-proba-btc`
- **WSGI configuration file** (bouton du même nom) : remplacer le contenu par :

```python
import sys
path = "/home/VOTRE_USER/moteur-proba-btc"
if path not in sys.path:
    sys.path.insert(0, path)

# config.py lit .env au démarrage (auth + secret de session).
from web.app import app as application  # noqa: E402
```

- **Environment variables** : `core/config.py` lit automatiquement le `.env`
  du dossier projet ; renseigner au minimum `WEB_PASSWORD_HASH` et
  `FLASK_SECRET_KEY`.
- Cliquer **Reload** (gros bouton vert) après chaque changement de code/config.

L'URL est `https://VOTRE_USER.pythonanywhere.com` — **protégée par mot de passe**
(§6.4). Le « live » se fait par polling (aucun websocket entrant).

---

## 4. Variables d'environnement (`.env`)

| Clé | Rôle | Défaut |
|---|---|---|
| `SYMBOL` | Instrument | `BTCUSDT` |
| `BACKFILL_DAYS` | Historique 1m au 1er démarrage | `730` |
| `CAPITAL_USD` | Capital de dimensionnement | `3000` |
| `FEE_TAKER` / `FEE_MAKER` | Coûts (verdicts §5.5) | `0.0010` / `0.0003` |
| `HEARTBEAT_SECONDS` | Cadence heartbeat | `30` |
| `PRIMARY_SILENCE_SECONDS` | Seuil de bascule REST | `300` |
| `DATA_PRIMARY` / `DATA_FALLBACKS` | Ordre des sources | `binance` / `kraken,coinbase,coingecko` |
| `WEB_PASSWORD_HASH` | Hash du mot de passe web | *(obligatoire)* |
| `FLASK_SECRET_KEY` | Clé de session Flask | *(obligatoire)* |

---

## 5. Procédure de restauration (§7.7)

Sauvegardes quotidiennes dans `backups/AAAA-MM-JJ/moteur.db` (rétention 14 j,
instantané SQLite cohérent). Pour restaurer :

```bash
# 1. Arrêter le worker (Tasks → Always-on → Stop) pour libérer l'écrivain.
# 2. Remplacer la base par l'instantané choisi :
cp backups/AAAA-MM-JJ/moteur.db data/moteur.db
rm -f data/moteur.db-wal data/moteur.db-shm     # purge d'un éventuel WAL orphelin
# 3. Relancer le worker (Start). La reprise comble automatiquement le 1m manquant.
```

Le cache 1m (`data/ohlcv/*.parquet`) se reconstruit seul via backfill REST ;
il n'est pas indispensable de le sauvegarder.

---

## 6. Tests d'acceptation P1 (§8) — comment les démontrer

- **(a) Tuer le worker → relance auto + reprise sans trou.** Noter la dernière
  bougie 1m, tuer le process ; au redémarrage, `startup_recovery()` backfill le
  trou et journalise `Continuité 1m : … 0 trou(s)`, avec l'événement « Worker
  (re)démarré » dans la cloche.
- **(b) Couper le websocket → reconnexion + backfill.** Couper la connectivité
  du flux ; observer le backoff exponentiel (1 s → 60 s) dans `logs/worker.log`,
  puis le comblage REST du trou à la reconnexion (événement « Trou(s) comblé(s) »).
  À défaut de websocket (ex. IP géo-bloquée), le worker bascule en ingestion REST
  de repli après `PRIMARY_SILENCE_SECONDS` (événement de bascule).
- **(c) 48 h de stabilité, zéro bougie 1m manquante.** Après 48 h, l'endpoint
  `/api/health` (`bars_1m`) et le contrôle de continuité (à chaque maintenance et
  au cycle quotidien) doivent montrer 0 trou sur la fenêtre.

Un banc local sans réseau (`tests/test_p1.py`) démontre la logique de reprise,
de continuité, de bascule et de ré-échantillonnage de façon déterministe.

---

## 7. Lancer les tests locaux

```bash
source .venv/bin/activate
python tests/test_p1.py   # socle données (16 tests)
python tests/test_p2.py   # moteur statistique (21 tests)
python tests/test_p3.py   # synthèse bayésienne + cloche (11 tests)
python tests/test_p4.py   # paper trading, livre, CUSUM, walk-forward (30 tests)
```

---

## 8. Fonctionnement P4 (paper trading automatique)

À chaque clôture d'un étage de décision (1h, 4h, 1D), le worker :
1. invalide les tickets en attente (état quitté OU 3 bougies) ;
2. met à jour la surveillance (CUSUM, lecture toutes les 25 clôtures) —
   un étage « en enquête » n'émet plus de tickets jusqu'à l'acquittement
   MANUEL dans l'app (bouton « Acquitter », Vue Livre) ;
3. si la case (étage × état courant × direction) est candidate (EV nette
   prudente > 0, n ≥ 200, coûts favorables, non-overfit au walk-forward) et
   que le livre l'autorise (plafond 3 %, pas de position opposée), émet un
   ticket : entrée limite à −0,25×ATR, SL à −1×ATR, TP au meilleur RR.

Les fills sont simulés sur le flux 1m réel (SL avec slippage ×1,3 long /
×1,5 short, coûts appliqués). Chaque trade clos alimente le journal et les
cartes de verdict (t-stat, Monte Carlo 10 000 permutations, profit factor,
Brier), visibles dans la Vue Livre & journal.
