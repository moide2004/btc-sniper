# BRIEF — Moteur de probabilités BTC (v3)

Instrument de mesure et de validation probabiliste pour BTC. La **conception est
figée** ; le travail est de l'**ingénierie**. Aucune définition statistique,
aucun seuil, aucun budget ni choix logistique n'est modifié sans question
préalable justifiée par un mécanisme (§9 du cahier des charges).

## Invariants (non négociables)
- Jamais un p̂ sans **n** ni **intervalle** ; jamais une **EV** sans **coûts**.
- Le moteur ne passe **JAMAIS** d'ordre réel (passer / préparer / brancher).
- **Aucune notification externe** (Telegram, e-mail, push, webhook). Le seul
  canal est la **cloche interne** de la mini-app.
- **UTC partout** (données, clôtures, journaux, affichage).
- Une **seule vérité de prix** : le 1m ; toutes les timeframes ≤ 1D en sont
  ré-échantillonnées ; 1W/2W/1M agrégées du 1D.
- **Un seul écrivain** : le worker écrit `moteur.db` ; la web app lit (sauf la
  table `web_actions` : acquittement d'alarme, marquage lu/non-lu).
- Le 1m/5m n'est **jamais** promu étage de décision. Pas de positions opposées
  simultanées.

## Contraintes d'hébergement (PythonAnywhere payant)
- **Always-on task** = `jobs/worker.py`, relancée automatiquement si elle plante
  → doit être **idempotente** au redémarrage (reprise propre depuis l'état
  persistant).
- **Asymétrie websocket** : le worker ouvre des ws **sortants** (Binance) ; la
  web app **ne peut pas** servir de ws au navigateur → le « live » = **polling
  HTTP** (`fetch` sur endpoints JSON). Aucune tentative de ws/SSE côté web app.
- Sources : Binance (ws klines 1m + REST backfill) ; replis REST Kraken →
  Coinbase → CoinGecko, bascule automatique.
- Recalcul **lourd 1×/jour** (tâche planifiée) ; le worker ne fait que
  l'incrémental ; RAM disciplinée (lecture par morceaux, dataframes libérés).
- Dépendances épinglées (`requirements.txt`), rien d'autre sans accord.

## Phases (ordre strict)
- **P1 — Données & socle** : store SQLite (WAL), data_source (ws + backfill +
  bascule), worker minimal + heartbeat, README d'installation PythonAnywhere,
  tâche planifiée 00:10 UTC, WSGI web app. Tests : (a) tuer le worker → relance
  + reprise sans trou ; (b) couper le ws → reconnexion + backfill ; (c) 48 h
  sans bougie 1m manquante.  ← **phase courante**
- **P2 — Moteur** : tables statistiques + tests unitaires synthétiques ;
  walk-forward. Test : ré-échantillonnage 1m→4h == 4h natif (tolérance ~0).
- **P3 — Mini-app live** : 4 vues + cloche + polling + auth.
- **P4 — Paper trading** : 60 j OU 100 trades sans intervention ; carte de
  verdict globale.
- **P5 — Bilan** : go/no-go par étage. Exécution réelle HORS PÉRIMÈTRE.

## Cadre
Projet éducatif. Des fréquences historiques ne sont pas des promesses. Rien
n'est un conseil financier.

> Le cahier des charges complet (définitions §4–§5, logistique §7, vues §6)
> fait foi. Ce fichier en rappelle les invariants pour le développement.
