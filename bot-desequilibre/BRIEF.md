# BRIEF — Bot Déséquilibré (Fibonacci breakout, BTC + ETH)

Analyste de trading multi-timeframes (**il signale, il n'exécute jamais**).
La conception de la stratégie est fournie par l'humain ; le rôle de l'ingénieur
est l'ingénierie et la validation, pas la réinvention. Aucune règle de trading ni
choix logistique n'est modifié sans question préalable justifiée par un
**mécanisme de marché** (§9). Projet éducatif ; rien n'est un conseil financier.

## Invariants
- **Aucun ordre réel**, jamais. **Aucune notification externe** — cloche interne
  seule.
- Jamais un p̂ sans **n** ni **intervalle** ; jamais une EV sans **coûts**.
- **UTC partout.** Un seul écrivain (worker) ; la web app lit.
- **15m = plancher** d'analyse ; le 1m sert à la collecte/backfill/fills.
- Dual-actif **BTC + ETH**, capital et budget de risque **partagés** (§5.4).

## Stratégie (fidèle — §2)
Niveaux Fibonacci dynamiques sur `fibLookback` (défaut 50) : 23,6/38,2/50/61,8/
78,6 %. **Long** : clôture > 50 % ET > 23,6 % (buffer `bufferATR`). **Short** :
clôture < 50 % ET < 78,6 % (miroir ; `activerShorts`, `shortRiskFactor`=0,75).
Signal à la clôture, entrée à l'open suivant, `pyramiding=0`. **SL** : long sous
78,6 %, short au-dessus de 23,6 % ; rejet si stopDist > `stopMaxATR`×ATR (3),
plancher `stopMinATR`×ATR (0,5) ; **break-even** à `beTrigger` ; jamais élargi.
**TP** : entrée ± `rrMult`×stopDist (1,5). **Taille** : capital×`riskPct`/stopDist.
Filtres : `minAmpATR` (amplitude), `bufferATR` (distance à la cassure). Aucun
autre paramètre/indicateur sans accord (§9.1).

## Couche probabiliste (§3)
Par actif × timeframe × direction : p̂ double barrière (TP avant SL, sans
chevauchement) aux 3 rrMult ; Wilson 95 % ; Beta(5+k,5+n−k) → p_prudent q25 ;
EV nette (taker ET maker) ; réalisme (k_max, CVaR99). **Aucune case barrée** :
le chiffre parle. Ticket émis dès qu'un setup §2 est valide, ANNOTÉ « solide »
(EV prudente > 0 ET n ≥ 200) ou « spéculatif ». Walk-forward nocturne : rétention
EV_test/EV_train (≥0,5 sain ; 0,2–0,5 fragile ; <0,2 overfit → disqualifiée).

## Données & hébergement (§4)
eu.pythonanywhere.com (Binance répond en EU ; sinon Kraken primaire). Binance ws
klines 1m + REST backfill BTC+ETH ; replis Kraken→Coinbase→CoinGecko.
Profondeur : 15m/30m 2 ans ; 1h 4 ans ; 4h/12h/1D maximum. Always-on idempotent ;
recalcul lourd 1×/jour (00:10 UTC) ; worker incrémental. Deps épinglées.

## Budget de risque partagé (§5.4)
Capital unique. 2e position de MÊME direction (tous actifs/TF) comptée 1,5× ;
**plafond risque ouvert 4 %** ; corrélation BTC/ETH mensuelle, ×2 si ρ>0,8 ;
**interdiction des positions opposées** entre actifs corrélés.

## Phases (ordre strict — §8)
P0 géoblocage (ping) · P1 données & socle ✓ · **P2 Fibonacci + probas** (← livré)
· P3 mini-app 4 vues · P4 paper trading (60 j ou 100 trades) · P5 bilan go/no-go
**par flux** (PF net > 1,15 ET t ≥ 1,5 ET DD MC p95×1,25 < 30 % ET dégradation
progressive ET rétention ≥ 0,5). Un flux peut échouer et être désactivé — résultat
de recherche. Exécution réelle HORS PÉRIMÈTRE.
