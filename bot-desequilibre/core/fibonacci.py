"""Moteur §2 — détection de setups « déséquilibre » sur cassure de Fibonacci.

FIDÈLE au BRIEF (§2). Aucune règle inventée. Signal à la CLÔTURE d'une bougie
d'analyse (TF ≥ 15m) ; entrée à l'OPEN de la bougie suivante (pyramiding 0).

Géométrie (voir core/indicators.py) : sur `fib_lookback` bougies, H = plus-haut,
L = plus-bas, R = H − L, level(p) = H − p·R (0 %=haut, 100 %=bas).

  • LONG  : close > level(50 %) ET close > level(23,6 %) + bufferATR·ATR
            (cassure PAR LE HAUT ; 23,6 % est proche du haut → nouveau plus-haut)
      SL  : level(78,6 %)  (« sous 78,6 % », proche du bas → stop sous l'entrée)
  • SHORT : close < level(50 %) ET close < level(78,6 %) − bufferATR·ATR   (miroir)
      SL  : level(23,6 %)  (« au-dessus de 23,6 % »)

  • stopDist = |entrée − SL| ; REJET si stopDist > stopMaxATR·ATR ; PLANCHER
    stopMinATR·ATR (le stop n'est jamais plus serré que le plancher).
  • TP(rr) = entrée ± rr·stopDist.
  • Filtres : amplitude R ≥ minAmpATR·ATR ; bufferATR = distance à la cassure.
  • Taille : capital·riskPct / stopDist (short : ×shortRiskFactor). Voir core/risk.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from .config import CONFIG
from .indicators import FIB_RATIOS, atr_wilder, fib_levels


@dataclass
class Setup:
    idx: int                       # index de la bougie SIGNAL dans df_tf
    direction: str                 # "long" | "short"
    signal_open_ms: int
    signal_close_ms: int
    entry_time_ms: int             # open_time de la bougie d'ENTRÉE (signal+1)
    entry: float                   # prix d'entrée (open suivant ; proxy=close si live)
    sl: float                      # stop-loss effectif (après plancher)
    stop_dist: float               # |entrée − SL| effectif
    amplitude: float               # R = H − L
    atr: float
    fib: dict[str, float] = field(default_factory=dict)
    entry_is_proxy: bool = False   # True si entrée = close (bougie suivante absente)

    def tp(self, rr: float) -> float:
        return self.entry + rr * self.stop_dist if self.direction == "long" \
            else self.entry - rr * self.stop_dist


class FibParams:
    """Copie figée des paramètres §2 (permet l'injection au banc de test)."""

    def __init__(self, **kw) -> None:
        self.fib_lookback = kw.get("fib_lookback", CONFIG.fib_lookback)
        self.buffer_atr = kw.get("buffer_atr", CONFIG.buffer_atr)
        self.activer_shorts = kw.get("activer_shorts", CONFIG.activer_shorts)
        self.stop_max_atr = kw.get("stop_max_atr", CONFIG.stop_max_atr)
        self.stop_min_atr = kw.get("stop_min_atr", CONFIG.stop_min_atr)
        self.min_amp_atr = kw.get("min_amp_atr", CONFIG.min_amp_atr)
        self.atr_period = kw.get("atr_period", CONFIG.atr_period)


def _build_setup(df, i, direction, atr_i, high_w, low_w, params) -> Optional[Setup]:
    """Construit un Setup VALIDE au bar i (déjà filtré direction/cassure), ou None."""
    levels = fib_levels(high_w, low_w)
    sl_line = levels["0.786"] if direction == "long" else levels["0.236"]
    n = len(df)
    if i + 1 < n:                                   # entrée = open de la bougie suivante
        entry = float(df["open"].iloc[i + 1])
        entry_time = int(df["open_time"].iloc[i + 1])
        proxy = False
    else:                                           # live : bougie suivante pas encore ouverte
        entry = float(df["close"].iloc[i])
        entry_time = int(df["close_time"].iloc[i]) + 1
        proxy = True
    stop_raw = (entry - sl_line) if direction == "long" else (sl_line - entry)
    if stop_raw <= 0:
        return None                                 # géométrie incohérente (garde)
    if stop_raw > params.stop_max_atr * atr_i:
        return None                                 # REJET §2 : stop trop large
    stop_eff = max(stop_raw, params.stop_min_atr * atr_i)   # PLANCHER §2
    sl = (entry - stop_eff) if direction == "long" else (entry + stop_eff)
    return Setup(idx=i, direction=direction,
                 signal_open_ms=int(df["open_time"].iloc[i]),
                 signal_close_ms=int(df["close_time"].iloc[i]),
                 entry_time_ms=entry_time, entry=entry, sl=sl, stop_dist=stop_eff,
                 amplitude=high_w - low_w, atr=atr_i, fib=levels, entry_is_proxy=proxy)


def detect_setups(df_tf: pd.DataFrame, params: Optional[FibParams] = None) -> list[Setup]:
    """Tous les setups §2 valides d'une série TF (historique). Sans look-ahead :
    H/L et ATR au bar i n'utilisent que les bougies ≤ i ; l'entrée est en i+1."""
    params = params or FibParams()
    lb, buf = params.fib_lookback, params.buffer_atr
    n = len(df_tf)
    if n < lb + 1:
        return []
    atr = atr_wilder(df_tf, params.atr_period).to_numpy()
    high = df_tf["high"].to_numpy()
    low = df_tf["low"].to_numpy()
    close = df_tf["close"].to_numpy()
    # Plus-haut/plus-bas glissants sur `lb` bougies (fenêtre incluant i).
    roll_h = pd.Series(high).rolling(lb).max().to_numpy()
    roll_l = pd.Series(low).rolling(lb).min().to_numpy()

    out: list[Setup] = []
    for i in range(lb - 1, n):
        atr_i = atr[i]
        if not np.isfinite(atr_i) or atr_i <= 0:
            continue
        H, L = roll_h[i], roll_l[i]
        R = H - L
        if R < params.min_amp_atr * atr_i:          # filtre amplitude
            continue
        lv = fib_levels(H, L)
        c = close[i]
        long_ok = c > lv["0.500"] and c > lv["0.236"] + buf * atr_i
        short_ok = (params.activer_shorts and c < lv["0.500"]
                    and c < lv["0.786"] - buf * atr_i)
        if long_ok:
            s = _build_setup(df_tf, i, "long", atr_i, H, L, params)
            if s:
                out.append(s)
        elif short_ok:
            s = _build_setup(df_tf, i, "short", atr_i, H, L, params)
            if s:
                out.append(s)
    return out


def latest_setup(df_tf: pd.DataFrame, params: Optional[FibParams] = None) -> Optional[Setup]:
    """Setup §2 sur la DERNIÈRE bougie close (usage LIVE) ou None. L'entrée est un
    proxy (=close) : elle sera figée à l'open réel de la bougie suivante en P4."""
    params = params or FibParams()
    n = len(df_tf)
    if n < params.fib_lookback + 1:
        return None
    i = n - 1
    atr_i = atr_wilder(df_tf, params.atr_period).to_numpy()[i]
    if not np.isfinite(atr_i) or atr_i <= 0:
        return None
    H = float(df_tf["high"].iloc[i - params.fib_lookback + 1:i + 1].max())
    L = float(df_tf["low"].iloc[i - params.fib_lookback + 1:i + 1].min())
    if (H - L) < params.min_amp_atr * atr_i:
        return None
    lv = fib_levels(H, L)
    c = float(df_tf["close"].iloc[i])
    if c > lv["0.500"] and c > lv["0.236"] + params.buffer_atr * atr_i:
        return _build_setup(df_tf, i, "long", atr_i, H, L, params)
    if (params.activer_shorts and c < lv["0.500"]
            and c < lv["0.786"] - params.buffer_atr * atr_i):
        return _build_setup(df_tf, i, "short", atr_i, H, L, params)
    return None


# Rétro-compat lisibilité : clés de niveaux disponibles.
LEVEL_KEYS = [f"{r:.3f}" for r in FIB_RATIOS]
