"""Tickets de trade par étage de décision (§5.8).

Case candidate = EV nette prudente > 0 ET n ≥ 200 ET verdict coûts favorable
(portée par le drapeau `candidate` des tables) ET non disqualifiée par le
walk-forward (§5.12).

Ticket complet : direction long OU short (§5.14) ; entrée ; SL = −1×ATR ;
TP aux trois RR avec p̂/intervalle/EV chacun ; taille = min(Kelly/4 sur
p_prudent ; 1,5 %) × m_GARCH, m = min(1,5 ; vol_cible/vol_prévue), GARCH(1,1)
quotidien (arch), vol cible = médiane 1 an ; capital saisi (défaut 3 000 $) ;
bloc réalisme ; invalidation : état quitté OU 3 bougies.

Choix d'ingénierie documentés (§9) :
  * vol_prévue = prévision σ quotidienne à 1 pas du GARCH(1,1) ; vol_cible =
    médiane 1 an de la vol quotidienne EWMA (λ=0,94, cohérent §4 v1.5) ;
  * si arch/GARCH indisponible ou échoue → m = 1,0 + badge « garch
    indisponible » (le ticket reste émis, jamais surdimensionné).
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from .config import CONFIG

DECISION_STAGES = ("1h", "4h", "1D")   # §5.9 : étages de DÉCISION
RISK_CAP_SINGLE = 0.015                # 1,5 % par position (§5.8)
INVALIDATION_BARS = 3                  # §5.8


def kelly_quarter(p: float, rr: float) -> float:
    """Kelly/4 sur p_prudent (§5.8). Kelly plein : f* = p − (1−p)/RR."""
    if rr <= 0 or not (0 < p < 1):
        return 0.0
    return max(0.0, (p - (1 - p) / rr) / 4.0)


def ewma_vol(daily_close: np.ndarray, lam: float = 0.94) -> np.ndarray:
    """Vol quotidienne EWMA (λ=0,94, §4 v1.5) sur rendements log."""
    r = np.diff(np.log(daily_close))
    if len(r) == 0:
        return np.array([])
    var = np.empty(len(r))
    var[0] = r[0] ** 2 if r[0] != 0 else np.var(r) or 1e-8
    for i in range(1, len(r)):
        var[i] = lam * var[i - 1] + (1 - lam) * r[i] ** 2
    return np.sqrt(var)


def garch_multiplier(daily_close: np.ndarray) -> tuple[float, bool]:
    """m = min(1,5 ; vol_cible/vol_prévue) (§5.8). Retourne (m, garch_ok)."""
    try:
        c = np.asarray(daily_close, dtype="float64")
        if len(c) < 100:
            return 1.0, False
        r = 100.0 * np.diff(np.log(c))  # arch préfère des % pour la stabilité
        from arch import arch_model
        am = arch_model(r, vol="GARCH", p=1, q=1, mean="Zero")
        res = am.fit(disp="off", show_warning=False)
        fc = res.forecast(horizon=1, reindex=False)
        vol_prev = float(np.sqrt(fc.variance.values[-1, 0])) / 100.0  # σ quotidien
        if not (vol_prev > 0):
            return 1.0, False
        sig = ewma_vol(c)
        window = sig[-365:] if len(sig) >= 365 else sig
        vol_cible = float(np.median(window))
        m = min(1.5, vol_cible / vol_prev)
        return max(0.0, m), True
    except Exception:
        return 1.0, False


def build_ticket(
    stage: str,
    state: str,
    direction: str,
    dir_block: dict,
    tf_table: dict,
    daily_close: np.ndarray,
    capital: Optional[float] = None,
    ts_utc: str = "",
) -> Optional[dict]:
    """Fabrique un ticket complet depuis une case candidate (§5.8).
    Retourne None si la case n'a pas de meilleur RR exploitable."""
    best = dir_block.get("best")
    if best is None:
        return None
    capital = CONFIG.capital_usd if capital is None else capital
    entry = tf_table["close"]
    a = tf_table["atr"]
    if not (a and a == a and a > 0):
        return None

    sign = 1.0 if direction == "long" else -1.0
    sl = entry - sign * a  # SL = −1×ATR (§5.8)

    tps = []
    for b in dir_block["barrieres"]:
        tps.append({
            "rr": b["rr"],
            "prix": entry + sign * b["rr"] * a,
            "p_hat": b["p_hat"], "n": b["n"], "wilson": b["wilson"],
            "ev_nette": b["ev_nette"], "ev_nette_prudente": b["ev_nette_prudente"],
        })

    p_prudent = best["posterior"]["p_prudent"]
    kq = kelly_quarter(p_prudent, best["rr"])
    m, garch_ok = garch_multiplier(daily_close)
    size_pct = min(kq, RISK_CAP_SINGLE) * m
    size_usd = size_pct * capital

    return {
        "ts_utc": ts_utc, "stage": stage, "etat": state, "direction": direction,
        "entree": entry, "sl": sl, "atr": a, "tps": tps,
        "rr_retenu": best["rr"],                      # meilleure EV prudente (§5.11)
        "p_annonce": best["p_hat"], "n": best["n"],
        "wilson": best["wilson"], "p_prudent": p_prudent,
        "ev_nette_prudente": best["ev_nette_prudente"],
        "cost_r": tf_table["cost_r"],
        "taille": {"kelly_quart": kq, "plafond": RISK_CAP_SINGLE, "m_garch": m,
                   "garch_ok": garch_ok, "risque_pct": size_pct,
                   "risque_usd": size_usd, "capital": capital},
        "realisme": tf_table["realisme"],
        "invalidation": {"etat": state, "bougies_max": INVALIDATION_BARS},
        "funding_integre": False,
    }
