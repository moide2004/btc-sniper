"""Paper trading §8 P4 — simulation de positions VIRTUELLES (aucun ordre réel).

Deux usages du MÊME moteur de fill (règles §2 identiques, zéro divergence) :
  • replay HISTORIQUE par flux (actif × TF × direction) → statistiques immédiates
    (n, taux, profit factor, espérance R, t-stat, drawdown) qui alimentent la P5 ;
  • gestion LIVE forward : le worker ouvre une position au fill réel puis l'avance
    bougie 1m par bougie 1m (Livre).

Fill fidèle au BRIEF :
  • Entrée = open de la bougie d'analyse suivante (déjà porté par Setup.entry).
  • Gestion sur le 1m (granularité la plus fine → ordre intra-bougie respecté).
  • DOUBLE BARRIÈRE pessimiste : si SL et TP touchables dans la même bougie 1m,
    le SL l'emporte (comme la couche §3).
  • BREAK-EVEN : dès que le prix atteint beTrigger·stopDist en faveur, le SL
    est ramené à l'entrée (jamais élargi). Le BE ne s'applique qu'aux bougies
    SUIVANTES (une bougie qui touche l'ancien SL est une perte, pas un BE).
  • Coût aller-retour en R = 2·fee·entrée / stopDist (taker par défaut).

R (unité de risque) : TP → +rr, BE → 0, SL → −1, moins le coût. PnL USD = R·risk_usd.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .config import CONFIG
from .fibonacci import Setup
from .proba import HORIZON_1M


# ===========================================================================
# Fill élémentaire (partagé historique ⇄ live)
# ===========================================================================
@dataclass
class Fill:
    reason: str          # "tp" | "sl" | "be" | "censored"
    r_gross: Optional[float]   # +rr | 0 | -1 | None(censuré)
    exit_idx: Optional[int]    # index 1m de sortie (None si censuré)


def resolve_fill(entry: float, sl0: float, tp: float, direction: str,
                 stop_dist: float, be_trigger: float,
                 highs: np.ndarray, lows: np.ndarray, start_idx: int) -> Fill:
    """Rejoue une position sur le 1m depuis start_idx. Break-even pessimiste."""
    n = highs.size
    end = min(start_idx + HORIZON_1M, n)
    sl = sl0
    be_done = False
    be_level = (entry + be_trigger * stop_dist) if direction == "long" \
        else (entry - be_trigger * stop_dist)
    for j in range(start_idx, end):
        hi, lo = highs[j], lows[j]
        if direction == "long":
            hit_sl, hit_tp = lo <= sl, hi >= tp
        else:
            hit_sl, hit_tp = hi >= sl, lo <= tp
        if hit_sl:                                  # SL prioritaire (ex æquo → SL)
            return Fill("be" if be_done else "sl", 0.0 if be_done else -1.0, j)
        if hit_tp:
            return Fill("tp", None, j)              # r_gross=rr rempli par l'appelant
        if not be_done:                             # BE armé pour les bougies suivantes
            if (direction == "long" and hi >= be_level) or \
               (direction == "short" and lo <= be_level):
                sl, be_done = entry, True
    return Fill("censored", None, None)


# ===========================================================================
# Replay historique d'un flux (actif × TF × direction)
# ===========================================================================
@dataclass
class Trade:
    entry_time_ms: int
    exit_time_ms: Optional[int]
    direction: str
    reason: str
    r_net: float             # résultat net de coût, en R
    risk_usd: float
    pnl_usd: float


def simulate_flux(setups: list[Setup], ot_1m: np.ndarray, highs: np.ndarray,
                  lows: np.ndarray, rr: float, be_trigger: Optional[float] = None,
                  fee: Optional[float] = None,
                  risk_usd: Optional[float] = None) -> list[Trade]:
    """Rejoue tous les setups d'un flux (déjà d'une seule direction). Chronologique.
    Positions indépendantes (mesure de l'edge PROPRE au flux ; le budget partagé
    §5.4 gouverne le forward live, pas la mesure d'edge par flux de la P5)."""
    be_trigger = CONFIG.be_trigger if be_trigger is None else be_trigger
    fee = CONFIG.fee_taker if fee is None else fee
    trades: list[Trade] = []
    for s in sorted(setups, key=lambda x: x.entry_time_ms):
        idx = int(np.searchsorted(ot_1m, s.entry_time_ms, side="left"))
        if idx >= ot_1m.size:
            continue
        r_usd = risk_usd if risk_usd is not None else _risk_usd(s.direction)
        f = resolve_fill(s.entry, s.sl, s.tp(rr), s.direction, s.stop_dist,
                         be_trigger, highs, lows, idx)
        if f.reason == "censored":
            continue
        r_gross = rr if f.reason == "tp" else f.r_gross
        cost = 2 * fee * s.entry / s.stop_dist
        r_net = r_gross - cost
        trades.append(Trade(
            entry_time_ms=s.entry_time_ms,
            exit_time_ms=int(ot_1m[f.exit_idx]) if f.exit_idx is not None else None,
            direction=s.direction, reason=f.reason, r_net=r_net, risk_usd=r_usd,
            pnl_usd=r_net * r_usd))
    return trades


def _risk_usd(direction: str) -> float:
    factor = CONFIG.short_risk_factor if direction == "short" else 1.0
    return CONFIG.capital_usd * CONFIG.risk_pct * factor


# ===========================================================================
# Statistiques d'un flux (base de la P5)
# ===========================================================================
def max_drawdown_r(r_series: list[float]) -> float:
    """Drawdown maximal (en R) de la courbe d'équité cumulée. ≥ 0."""
    peak = cum = 0.0
    mdd = 0.0
    for r in r_series:
        cum += r
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return mdd


def t_stat(r: np.ndarray) -> Optional[float]:
    """t de Student de la moyenne des R (H0 : espérance nulle)."""
    if r.size < 2:
        return None
    sd = float(r.std(ddof=1))
    if sd == 0:
        return None
    return float(r.mean() / (sd / math.sqrt(r.size)))


def profit_factor(r: np.ndarray) -> Optional[float]:
    gains = float(r[r > 0].sum())
    losses = float(-r[r < 0].sum())
    if losses == 0:
        return None if gains == 0 else float("inf")
    return gains / losses


def flux_summary(trades: list[Trade]) -> dict:
    """Résumé mesuré d'un flux (jamais un chiffre sans n)."""
    n = len(trades)
    if n == 0:
        return {"n": 0, "wins": 0, "winrate": None, "profit_factor": None,
                "expectancy_r": None, "sum_r": 0.0, "t_stat": None,
                "max_dd_r": 0.0, "pnl_usd": 0.0}
    r = np.array([t.r_net for t in trades], dtype="float64")
    wins = int((r > 0).sum())
    pf = profit_factor(r)
    return {
        "n": n, "wins": wins, "winrate": wins / n,
        "profit_factor": None if pf is None else (None if math.isinf(pf) else pf),
        "expectancy_r": float(r.mean()), "sum_r": float(r.sum()),
        "t_stat": t_stat(r), "max_dd_r": max_drawdown_r([t.r_net for t in trades]),
        "pnl_usd": float(sum(t.pnl_usd for t in trades)),
    }


def mc_drawdown_p95(r_series: list[float], n_iter: int = 2000, seed: int = 12345) -> Optional[float]:
    """Drawdown p95 par bootstrap (rééchantillonnage avec remise de l'ordre des
    trades). Sert au critère P5 (DD MC p95×1,25 < 30 %). None si trop peu de trades."""
    if len(r_series) < 10:
        return None
    rng = np.random.default_rng(seed)
    arr = np.asarray(r_series, dtype="float64")
    dds = np.empty(n_iter)
    for i in range(n_iter):
        perm = rng.permutation(arr)
        dds[i] = max_drawdown_r(list(perm))
    return float(np.quantile(dds, 0.95))


# ===========================================================================
# Position LIVE (forward) — état sérialisable, avancée bougie par bougie
# ===========================================================================
def new_live_position(symbol: str, tf: str, setup: Setup, sizing, rr: float) -> dict:
    """Payload d'une position ouverte (avant gestion). risk_usd via sizing (§2.6)."""
    return {
        "symbol": symbol, "timeframe": tf, "direction": setup.direction,
        "entry": setup.entry, "sl0": setup.sl, "sl": setup.sl, "tp": setup.tp(rr),
        "stop_dist": setup.stop_dist, "rr": rr, "be_trigger": CONFIG.be_trigger,
        "be_done": False, "entry_time_ms": setup.entry_time_ms,
        "risk_usd": (sizing.risk_usd if sizing else _risk_usd(setup.direction)),
        "size_units": (sizing.size_units if sizing else None),
        "last_1m_ms": setup.entry_time_ms - 1, "atr": setup.atr,
    }


def advance_live_position(pos: dict, bars: list[tuple]) -> Optional[dict]:
    """Avance une position sur de NOUVELLES bougies 1m `bars`=[(open_ms,high,low)].
    Modifie pos en place (sl/be_done/last_1m_ms). Retourne un dict de CLÔTURE
    {reason,r_net,pnl_usd,exit_time_ms,exit_price} si sortie, sinon None."""
    d, sd = pos["direction"], pos["stop_dist"]
    be_level = (pos["entry"] + pos["be_trigger"] * sd) if d == "long" \
        else (pos["entry"] - pos["be_trigger"] * sd)
    for open_ms, hi, lo in bars:
        if open_ms <= pos["last_1m_ms"]:
            continue
        pos["last_1m_ms"] = open_ms
        if d == "long":
            hit_sl, hit_tp = lo <= pos["sl"], hi >= pos["tp"]
        else:
            hit_sl, hit_tp = hi >= pos["sl"], lo <= pos["tp"]
        if hit_sl:
            reason = "be" if pos["be_done"] else "sl"
            r_gross = 0.0 if pos["be_done"] else -1.0
            return _close(pos, reason, r_gross, open_ms, pos["sl"])
        if hit_tp:
            return _close(pos, "tp", pos["rr"], open_ms, pos["tp"])
        if not pos["be_done"]:
            if (d == "long" and hi >= be_level) or (d == "short" and lo <= be_level):
                pos["sl"], pos["be_done"] = pos["entry"], True
    return None


def _close(pos: dict, reason: str, r_gross: float, exit_ms: int, exit_price: float) -> dict:
    cost = 2 * CONFIG.fee_taker * pos["entry"] / pos["stop_dist"]
    r_net = r_gross - cost
    return {"reason": reason, "r_net": r_net, "pnl_usd": r_net * pos["risk_usd"],
            "exit_time_ms": exit_ms, "exit_price": exit_price}
