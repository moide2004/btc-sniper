# BRIEF — Stratégie Pine Script « TREND-PULLBACK BTC 4H »

> Continuation sur repli, bidirectionnelle filtrée par régime — TradingView.
> **La conception est terminée : le rôle de l'ingénieur est l'ingénierie, pas le
> design.** Aucune règle de trading n'est modifiée sans question préalable (§7).
> Projet éducatif ; un backtest ne préjuge pas du futur ; rien n'est un conseil
> financier.

## 1. Vue d'ensemble
- **Livrable A** — `strategy/TREND_PULLBACK_BTC_4H.pine` : stratégie Pine v5.
- **Livrable B** — `validation/validate.py` : validateur statistique du CSV
  « Liste des transactions » du Strategy Tester.
- Contrainte : Pine non exécutable ici. **L'humain est le banc de test** :
  écrire → coller dans TradingView → rapporter/exporter le CSV → analyser → itérer.
- Concept : le momentum donne la DIRECTION (on ne trade que le sens de la
  tendance de fond), le repli donne l'ENTRÉE (prix revenu sur sa moyenne courte
  puis repartant), le TP court donne le PROFIL (gagner souvent, petit). Les
  shorts n'existent qu'en régime baissier.

## 2. Spécification exacte (Livrable A)
Marché : BTCUSDT (Binance, spot ou perp). Graphique 4 h. Capital initial 3 000 USD.
Une seule position, `pyramiding = 0`.

### 2.1 Régime (quotidien, zéro look-ahead)
`smaD` = SMA 200 quotidienne du dernier jour COMPLÉTÉ (idiome non-repaint :
`request.security(..., "D", ta.sma(close,200)[1], lookahead=barmerge.lookahead_on)`).
Bull : clôture 4 h > smaD → LONGS seuls. Bear : < smaD → SHORTS seuls.
Jamais contre le régime ; jamais long et short simultanés.

### 2.2 Entrées (continuation sur repli)
`ema` = EMA(close, emaLen=20) 4 h. Long : bull ET crossover(close, ema) ET plat.
Short : bear ET crossunder ET plat. Signal à la clôture, exécution à l'open
suivant (défaut ; PAS de `process_orders_on_close` ni `calc_on_every_tick`).

### 2.3 Stop (structurel, borné)
Long : brut = lowest(low, swingN=10) − 0,25×ATR(14) ; stopDist = entrée − brut ;
borné : < 0,75×ATR → élargi à 0,75×ATR ; **> 2×ATR → PAS DE TRADE** (refus
journalisé par plotshape discret). Short : miroir avec highest(high, swingN)
+ 0,25×ATR. Stop JAMAIS élargi/retiré en cours de trade.

### 2.4 Sorties
1. TP fixe : entrée ± rrMult × stopDist (rrMult = 1,25).
2. Stop §2.3. 3. Time-stop : maxBars = 16 bougies 4 h.
4. Sortie de régime : inversion du régime → clôture immédiate.

### 2.5 Dimensionnement
qtyLong = (equity × riskPct)/stopDist, riskPct = 1,5 %.
qtyShort = qtyLong × shortRiskFactor (0,75, input) — le short BTC paie son vent
de face structurel en taille réduite.

### 2.6 Paramètres (inputs FR) et grille de sensibilité
| Paramètre | Défaut | Grille (§6) |
|---|---|---|
| emaLen | 20 | {15, 20, 25} |
| swingN | 10 | {8, 10, 12} |
| rrMult | 1,25 | {1,0 ; 1,25 ; 1,5} |
| atrLen | 14 | fixe |
| bornes stop | 0,75×ATR / 2×ATR | fixe |
| maxBars | 16 | {12, 16, 20} |
| riskPct | 1,5 % | 1,0 % si test de drawdown échoue |
| shortRiskFactor | 0,75 | fixe |

Aucun autre paramètre/filtre/indicateur sans accord explicite de l'humain.

## 3. Contraintes anti-biais (non négociables)
Zéro look-ahead ; entrées à l'open suivant ; pyramiding 0 ; commission percent
0,075 (taker + slippage forfaitaire) ; initial_capital 3000 USD ; overlay.
Interdits : moyenne à la baisse, augmentation après perte, stop retiré/élargi,
position contre régime. `alertcondition` long/short séparées (FR). Tracés :
EMA20, smaD, fond teinté par régime, marqueurs d'entrée + refus. Limite Pine :
le funding perp n'est pas modélisable → shorts légèrement optimistes (README +
verdict du validateur).

## 4. Livrable B — validateur
Entrée : CSV TradingView (CLI). Colonnes FR/EN auto-détectées ; si ambigu :
afficher et demander. **Cartes séparées : longs, shorts, global.**
R_unit = |médiane des pertes en %| (repli riskPct si < 5 pertes) ; r_i = pnl%/R_unit.
Chaque carte : n, k, p̂, Wilson 95 % ; Beta(4+k, 6+n−k) moyenne/σ/p_prudent q25 ;
EV, σ_R, t (≥2 validé ; 1–2 prometteur ; <1 non concluant) ; série de pertes
observée vs attendue ln(n)/ln(1/(1−p̂)) ; Monte Carlo 10 000 permutations → DD
max médian et p95 en R et % (au riskPct) ; **go/no-go : p95×1,25 < 30 %** sinon
recommander 1,0 % ; seuil EV − 2σ_R/√50 ; CUSUM (μ0=EV, k=μ0/2, h=4,5σ_R) ;
profit factor ; espérance mensuelle ; carte SHORT : rappel funding non modélisé.
Python 3, numpy+pandas+scipy uniquement. CSV synthétique fourni (150 longs
p=0,58 ; 60 shorts p=0,52 ; +1,25R/−1R bruités) et validé AVANT données réelles.

## 5. Formules (telles quelles)
Wilson 95 % (z=1,96) ; Beta(4+k, 6+n−k), p_prudent = q25 ; t = EV/(σ_R/√n) ;
série attendue ln(n)/ln(1/(1−p̂)) ; DD = min(cumsum − cummax) ; CUSUM
S_t = max(0, S_{t−1} + (μ0−k) − r_t), alarme si S_t > h.

## 6. Protocole de validation (ordre strict)
P1 Code (compile + validateur OK sur synthétique) → P2 Backtest max (≥150
trades ET ≥40 shorts sinon livre short « non évaluable ») → P3 Robustesse
(grille COMPLÈTE §2.6 — dégradation progressive attendue ; walk-forward manuel
2018–2022 vs 2023–auj., rétention 50–70 %). Go/no-go PAR LIVRE : PF net > 1,15
ET t ≥ 1,5 ET DD MC p95×1,25 < 30 % ET dégradation progressive. Livre short
échoue + long passe → livrer version long-only (`activerShorts` = false défaut).
Si go → paper trading (alertes + journal + Brier). Jamais de réel direct.

## 7. Protocole d'interaction
Pas de modification de logique de sa propre initiative (amélioration = question
+ MÉCANISME de marché, jamais « le backtest s'améliore ») ; grille entière
rapportée ; 3 cartes + UNE recommandation max ; refus polis : réel automatisé,
stops retirés/élargis, pyramidage, riskPct > 2 %, indicateurs « pour voir » ;
phases strictes ; éducatif, pas un conseil financier.
