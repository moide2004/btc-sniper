# Guide d'exploitation — Moteur de probabilités BTC

> Version résumée de la fiche technique. Projet éducatif : des fréquences
> historiques ne sont pas des promesses ; rien n'est un conseil financier.
> Aucun ordre réel n'est jamais passé.

## Les 3 règles gravées
1. **Aucun ordre réel, jamais** — tout le trading est virtuel (paper).
2. **Jamais un chiffre nu** — chaque p̂ vient avec son n et son intervalle ;
   chaque EV est nette de coûts (et funding).
3. **Une fréquence historique n'est pas une promesse.**

## Lire le bandeau
- Pastille **verte « live »** = données fraîches ; orange quelques minutes aux
  heures rondes = calculs lourds (normal) ; orange persistant → relancer
  l'always-on task.
- `source` : binance (primaire) ou kraken/coinbase (repli automatique).

## Lire une carte de la Matrice
- **État** : régime (`bull`/`bear` vs SMA200 quotidienne ; momentum 26 pour
  ≥ 1W) / extension RSI (`survendu·neutre·surachete`) ; `n` = bougies
  historiques dans cet état.
- **p̂ [Wilson] · n** : fréquence historique + intervalle 95 %. Intervalle
  large = échantillon trop petit = méfiance.
- **EV nette prudente** (en R) : espérance au quantile 25 % du posterior,
  coûts et funding déduits. Candidate si EV > 0 ET n ≥ 200 ET coûts
  favorables ET walk-forward non-overfit.
- **Verdict de coûts** (c/σ) : > 25 % NON TRADABLE ; 10–25 % exiger RR ≥ 1,5 ;
  < 10 % coûts secondaires.
- **Badge walk-forward** : rétention EV_test/EV_train — ≥ 0,5 sain ;
  0,2–0,5 fragile ; < 0,2 overfit (case disqualifiée).
- **« 0 candidate » est une réponse valide**, pas une panne.

## Le détail (clic sur une carte)
Tous les états × directions ; 3 horizons (H5/H10/H20) ; EV des 3 RR (meilleur
en gras) ; zones de volatilité v1.5 (`↳ vol basse/moyenne/haute`, actives si
n ≥ 200 par sous-case) ; bloc réalisme (σ_bougie, CVaR99, k_max).

## La synthèse bayésienne
P(hausse) combinée des 11 échelles (κ=0,6, **plafond 85 %**), contributions
par échelle, référence neutre log-normale à côté.

## Livre & journal (paper trading)
Cycle : case candidate (clôture 1h/4h/1D) → contrôle du livre (plafond 3 %,
pas de positions opposées) → ticket (limite −0,25×ATR, SL −1×ATR, TP au
meilleur RR, taille = min(Kelly/4 prudent ; 1,5 %) × m_GARCH) → fill simulé
sur le 1m réel (slippage stops ×1,3 long / ×1,5 short, coûts) → journal.

Cartes de verdict : n, p̂+Wilson, EV réalisée, σ_R, **t-stat** (> 2 = sans
doute pas de la chance), série max vs attendue, **Monte Carlo 10 000
permutations** (DD médian/p95), profit factor, **Brier** (< 0,25 = mieux que
pile-ou-face).

**CUSUM** : compteur de déception vs l'EV validée ; alarme → étage « en
enquête », tickets suspendus jusqu'à **acquittement manuel** (bouton Vue
Livre). Avant d'acquitter : comparer la série perdante à la « série max
attendue ».

## La cloche
Seul canal d'alerte (90 j de rétention). Alarmes CUSUM = agir (examiner puis
acquitter) ; le reste (bascules de source, trous comblés, worker redémarré,
tickets) = information.

## Routine
- **Quotidien (2 min)** : bandeau vert ? non-lus ? candidates ? état changé ?
- **Hebdo (5 min)** : jauge P4 (jours/60, trades/100), cartes de verdict,
  détail de 2-3 échelles, marquer lu.
- **À chaque alarme** : examiner puis acquitter en conscience.

## P4 → P5
60 jours OU 100 trades (premier atteint), sans intervention ni raccourci.
À l'échéance (événement dans la cloche) : instruire le bilan P5 — verdict
go/no-go par étage (t-stat, rétention, DD Monte Carlo, Brier). Décision
humaine.

## Entretien
```bash
# journal du worker (diagnostic n°1)
tail -n 20 ~/moteur-proba-btc/logs/worker.log

# relancer le calcul quotidien à la main
cd ~/moteur-proba-btc && source .venv/bin/activate && python jobs/daily_update.py

# mise à jour de l'application (nouvelle livraison)
cd ~ && curl -L -o moteur.zip "https://codeload.github.com/moide2004/btc-sniper/zip/refs/heads/claude/btc-proba-engine-v3-9f3dii" \
  && unzip -o moteur.zip && cp -rf btc-sniper-*/. moteur-proba-btc/ && rm -rf btc-sniper-* moteur.zip
# puis : Tâches → ↻ (worker) et Web → Reload
```
Automatique : recalcul 00:10 UTC, sauvegardes 14 j (garde anti-corruption),
reprise après coupure (bougies + SL/TP rejoués), rotation des logs, purge de
la cloche, bascule de source.
