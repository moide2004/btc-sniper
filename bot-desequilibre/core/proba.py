"""Couche probabiliste §3 — MESURE, ne prédit pas.

Par actif × timeframe × direction : on rejoue TOUS les setups §2 de l'historique
et on résout leur issue en DOUBLE BARRIÈRE (TP avant SL, sans chevauchement) sur
la granularité 1m (la plus fine disponible → ordre intra-bougie respecté). Pour
chacun des 3 rrMult :

  • p̂ = k/n           (issues résolues seulement)
  • Intervalle de Wilson 95 %
  • Postérieure Beta(5+k, 5+n−k) → p_prudent = quantile 25 %
  • EV nette par R, coûts TAKER **et** MAKER (aller-retour)
  • Réalisme : plus longue série de pertes (k_max), CVaR 99 % par trade (R)

« sans chevauchement » : si TP et SL sont touchables dans la MÊME bougie 1m, on
tranche en faveur du SL (pessimiste). Jamais un p̂ sans n ni intervalle (§ invariants).

Walk-forward : EV_test / EV_train (≥0,5 sain · 0,2–0,5 fragile · <0,2 overfit).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from scipy.stats import beta as _beta

from .config import CONFIG
from .fibonacci import FibParams, Setup, detect_setups

# Bornes de calcul (purement techniques, PAS une barrière de temps stratégique) :
# on cherche l'issue TP/SL dans au plus HORIZON_1M bougies 1m après l'entrée.
# Au-delà → issue « censurée », EXCLUE de n (jamais comptée en gain/perte).
HORIZON_1M = 90 * 1440           # 90 jours de 1m ; TP/SL se résout presque toujours avant


# ===========================================================================
# Statistiques élémentaires
# ===========================================================================
def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    phat = k / n
    denom = 1.0 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def beta_prudent(k: int, n: int, q: float = 0.25) -> float:
    """Quantile q de Beta(5+k, 5+n−k) — estimation PRUDENTE de p (q=0,25)."""
    if n < 0 or k < 0:
        return 0.0
    return float(_beta.ppf(q, 5 + k, 5 + n - k))


def max_losing_streak(outcomes: list[int]) -> int:
    """Plus longue série consécutive de pertes (0) dans l'ordre chronologique."""
    best = cur = 0
    for o in outcomes:
        cur = cur + 1 if o == 0 else 0
        best = max(best, cur)
    return best


def cvar99(pnls_r: np.ndarray) -> Optional[float]:
    """CVaR 99 % : moyenne du pire 1 % des PnL par trade (en R). None si vide."""
    if pnls_r.size == 0:
        return None
    thresh = np.quantile(pnls_r, 0.01)
    tail = pnls_r[pnls_r <= thresh]
    return float(tail.mean()) if tail.size else float(pnls_r.min())


# ===========================================================================
# Résolution double-barrière sur le 1m
# ===========================================================================
def _resolve_one(entry_idx: int, tp: float, sl: float, direction: str,
                 highs: np.ndarray, lows: np.ndarray) -> Optional[int]:
    """1 = TP d'abord (gain), 0 = SL d'abord ou ex æquo (perte), None = censuré."""
    end = min(entry_idx + HORIZON_1M, highs.size)
    if entry_idx >= end:
        return None
    h = highs[entry_idx:end]
    lo = lows[entry_idx:end]
    if direction == "long":
        tp_hit = h >= tp
        sl_hit = lo <= sl
    else:
        tp_hit = lo <= tp
        sl_hit = h >= sl
    i_tp = int(np.argmax(tp_hit)) if tp_hit.any() else None
    i_sl = int(np.argmax(sl_hit)) if sl_hit.any() else None
    if i_tp is None and i_sl is None:
        return None
    if i_sl is None:
        return 1
    if i_tp is None:
        return 0
    return 1 if i_tp < i_sl else 0          # ex æquo (i_tp == i_sl) → SL (pessimiste)


def _outcomes_for_rr(setups: list[Setup], rr: float, ot_1m: np.ndarray,
                     highs: np.ndarray, lows: np.ndarray) -> tuple[list[int], np.ndarray, np.ndarray]:
    """Issues (0/1, censurés exclus) + coût R (taker) + coût R (maker) par trade."""
    outcomes: list[int] = []
    cost_t: list[float] = []
    cost_m: list[float] = []
    for s in setups:
        idx = int(np.searchsorted(ot_1m, s.entry_time_ms, side="left"))
        res = _resolve_one(idx, s.tp(rr), s.sl, s.direction, highs, lows)
        if res is None:
            continue
        outcomes.append(res)
        # coût aller-retour rapporté au risque : 2·fee·entrée / stopDist
        cost_t.append(2 * CONFIG.fee_taker * s.entry / s.stop_dist)
        cost_m.append(2 * CONFIG.fee_maker * s.entry / s.stop_dist)
    return outcomes, np.asarray(cost_t), np.asarray(cost_m)


def _ev(p: float, rr: float, cost_r: float) -> float:
    """EV par R : p·rr − (1−p)·1 − coût."""
    return p * rr - (1 - p) - cost_r


def _rr_block(setups: list[Setup], rr: float, ot_1m, highs, lows) -> dict:
    outcomes, ct, cm = _outcomes_for_rr(setups, rr, ot_1m, highs, lows)
    n = len(outcomes)
    k = int(sum(outcomes))
    if n == 0:
        return {"rr": rr, "n": 0, "k": 0, "p_hat": None, "wilson": [None, None],
                "p_prudent": None, "ev_point_taker": None, "ev_prudent_taker": None,
                "ev_point_maker": None, "ev_prudent_maker": None,
                "k_max": 0, "cvar99_r": None}
    p_hat = k / n
    p_prud = beta_prudent(k, n)
    cost_t = float(np.median(ct)) if ct.size else 0.0
    cost_m = float(np.median(cm)) if cm.size else 0.0
    # PnL par trade en R (avec coût taker) pour le réalisme.
    pnls = np.array([(rr if o == 1 else -1.0) for o in outcomes]) - ct
    return {
        "rr": rr, "n": n, "k": k, "p_hat": p_hat,
        "wilson": list(wilson_interval(k, n)), "p_prudent": p_prud,
        "cost_taker_r": cost_t, "cost_maker_r": cost_m,
        "ev_point_taker": _ev(p_hat, rr, cost_t),
        "ev_prudent_taker": _ev(p_prud, rr, cost_t),
        "ev_point_maker": _ev(p_hat, rr, cost_m),
        "ev_prudent_maker": _ev(p_prud, rr, cost_m),
        "k_max": max_losing_streak(outcomes),
        "cvar99_r": cvar99(pnls),
    }


# ===========================================================================
# Walk-forward (§3) — rétention EV_test / EV_train
# ===========================================================================
def walk_forward(setups: list[Setup], rr: float, ot_1m, highs, lows,
                 train_frac: float = 0.7) -> dict:
    if len(setups) < 10:
        return {"status": "insuffisant", "retention": None, "n_train": len(setups), "n_test": 0}
    setups = sorted(setups, key=lambda s: s.entry_time_ms)
    cut = int(len(setups) * train_frac)
    tr = _rr_block(setups[:cut], rr, ot_1m, highs, lows)
    te = _rr_block(setups[cut:], rr, ot_1m, highs, lows)
    ev_tr, ev_te = tr["ev_point_taker"], te["ev_point_taker"]
    if ev_tr is None or ev_te is None or tr["n"] == 0 or te["n"] == 0:
        return {"status": "insuffisant", "retention": None,
                "n_train": tr["n"], "n_test": te["n"]}
    if ev_tr <= 0:
        return {"status": "disqualifie", "retention": None, "ev_train": ev_tr,
                "ev_test": ev_te, "n_train": tr["n"], "n_test": te["n"],
                "raison": "EV_train ≤ 0"}
    ret = ev_te / ev_tr
    status = "sain" if ret >= 0.5 else "fragile" if ret >= 0.2 else "overfit"
    return {"status": status, "retention": ret, "ev_train": ev_tr, "ev_test": ev_te,
            "n_train": tr["n"], "n_test": te["n"]}


# ===========================================================================
# Construction du bloc probabiliste complet pour (actif, TF, direction)
# ===========================================================================
@dataclass
class ProbaResult:
    symbol: str
    timeframe: str
    direction: str
    payload: dict


def build_proba(symbol: str, timeframe: str, direction: str,
                df_tf: pd.DataFrame, df_1m: pd.DataFrame,
                params: Optional[FibParams] = None,
                rr_grid: Optional[list[float]] = None) -> ProbaResult:
    """Bloc §3 : setups §2 de `direction`, résolus en double barrière sur `df_1m`,
    aux 3 rrMult + walk-forward. Renvoie un payload sérialisable (aucun NaN brut)."""
    params = params or FibParams()
    rr_grid = rr_grid or CONFIG.rr_grid
    setups = [s for s in detect_setups(df_tf, params) if s.direction == direction]
    if df_1m.empty or not setups:
        payload = {"n_setups": len(setups), "rr": [], "walk_forward": {},
                   "note": "Aucun setup exploitable" if not setups else "1m vide"}
        return ProbaResult(symbol, timeframe, direction, payload)

    ot = df_1m["open_time"].to_numpy(dtype="int64")
    highs = df_1m["high"].to_numpy(dtype="float64")
    lows = df_1m["low"].to_numpy(dtype="float64")

    blocks = {f"{rr:.2f}": _rr_block(setups, rr, ot, highs, lows) for rr in rr_grid}
    wf = {f"{rr:.2f}": walk_forward(setups, rr, ot, highs, lows) for rr in rr_grid}
    payload = {"n_setups": len(setups), "rr": blocks, "walk_forward": wf,
               "params": {"fib_lookback": params.fib_lookback,
                          "buffer_atr": params.buffer_atr,
                          "min_amp_atr": params.min_amp_atr,
                          "atr_period": params.atr_period}}
    return ProbaResult(symbol, timeframe, direction, payload)


def annotate(payload: dict, rr_live: float) -> dict:
    """Annotation d'un ticket à partir du bloc §3 au rrMult vivant : « solide »
    (EV prudente taker > 0 ET n ≥ solide_min_n) sinon « spéculatif ». Joint la
    rétention walk-forward et le drapeau « disqualifié » (overfit)."""
    key = f"{rr_live:.2f}"
    blk = payload.get("rr", {}).get(key)
    wf = payload.get("walk_forward", {}).get(key, {})
    if not blk or blk.get("n", 0) == 0:
        return {"annotation": "spéculatif", "raison": "aucune donnée historique",
                "n": 0, "solide": False, "wf_status": wf.get("status")}
    ev_prud = blk.get("ev_prudent_taker")
    n = blk.get("n", 0)
    disq = wf.get("status") == "overfit" or wf.get("status") == "disqualifie"
    solide = (ev_prud is not None and ev_prud > 0 and n >= CONFIG.solide_min_n and not disq)
    return {
        "annotation": "solide" if solide else "spéculatif",
        "solide": solide, "n": n, "k": blk.get("k"),
        "p_hat": blk.get("p_hat"), "p_prudent": blk.get("p_prudent"),
        "wilson": blk.get("wilson"),
        "ev_prudent_taker": ev_prud, "ev_point_taker": blk.get("ev_point_taker"),
        "ev_prudent_maker": blk.get("ev_prudent_maker"),
        "k_max": blk.get("k_max"), "cvar99_r": blk.get("cvar99_r"),
        "wf_status": wf.get("status"), "wf_retention": wf.get("retention"),
        "disqualifie": disq,
    }
