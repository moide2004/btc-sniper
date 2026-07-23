"""Indicateurs de base pour le moteur §2 : True Range, ATR de Wilder, niveaux
Fibonacci. Purs (aucun réseau, aucun état) — testables au banc.

ORIENTATION FIBONACCI (imposée par la géométrie du SL, §2) : les pourcentages
sont mesurés en RETRACEMENT DEPUIS LE HAUT. Sur une fenêtre de `lookback`
bougies, H = plus-haut, L = plus-bas, R = H − L ; le niveau p vaut

        level(p) = H − p·R

donc 0 % = H (haut), 100 % = L (bas), 50 % = milieu. Ainsi 23,6 % est PROCHE DU
HAUT et 78,6 % PROCHE DU BAS. C'est la seule orientation qui rend le SR cohérent :
« SL long sous 78,6 % » place bien le stop SOUS l'entrée, « SL short au-dessus de
23,6 % » place bien le stop AU-DESSUS. Voir core/fibonacci.py.
"""
from __future__ import annotations

import pandas as pd

FIB_RATIOS = (0.236, 0.382, 0.5, 0.618, 0.786)


def true_range(df: pd.DataFrame) -> pd.Series:
    """True Range de Wilder : max(H−L, |H−C₋₁|, |L−C₋₁|)."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low),
                    (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    return tr


def atr_wilder(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR de Wilder (lissage RMA = ewm α=1/period). NaN tant que < `period`."""
    tr = true_range(df)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def fib_levels(high: float, low: float) -> dict[str, float]:
    """Niveaux Fibonacci en retracement depuis le haut : level(p) = H − p·R."""
    rng = high - low
    return {f"{r:.3f}": high - r * rng for r in FIB_RATIOS}
