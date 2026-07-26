"""BOT 3 (laboratoire) — stratégie « VUMANCHU / WAVETREND » : le cœur de
VuManChu Cipher B est l'oscillateur WaveTrend (LazyBear). Formule canonique :

    ap  = (high + low + close) / 3
    esa = EMA(ap, n1)                 # canal
    d   = EMA(|ap − esa|, n1)
    ci  = (ap − esa) / (0.015 · d)
    wt1 = EMA(ci, n2)                 # ligne rapide (tci)
    wt2 = SMA(wt1, smooth)            # ligne lente

Règles ÉCRITES (proposées le 2026-07-26, validées « vumanchu ») :
  • Tendance : SMA(ma_fast) vs SMA(ma_slow) — longs seuls au-dessus, shorts
    seuls en dessous (même filtre que le Bot 2, comparaison équitable).
  • LONG  : croisement HAUSSIER wt1>wt2 (précédent wt1≤wt2) pendant que
    wt1 < −os_level (« point vert en survente », défaut 53).
  • SHORT : croisement BAISSIER pendant que wt1 > +os_level (miroir).
  • Signal à la CLÔTURE ; entrée à l'OPEN suivant (zéro look-ahead).
  • SL/TP/BE/coûts : identiques aux autres bots (extrême récent borné ATR,
    rr × stop, break-even, frais taker). Même juge (verdict P5).
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from .config import CONFIG
from .fibonacci import Setup
from .indicators import atr_wilder
from .pullback import _build


class WaveTrendParams:
    def __init__(self, **kw) -> None:
        self.n1 = kw.get("n1", CONFIG.vmc_n1)
        self.n2 = kw.get("n2", CONFIG.vmc_n2)
        self.smooth = kw.get("smooth", CONFIG.vmc_smooth)
        self.os_level = kw.get("os_level", CONFIG.vmc_os_level)
        self.ma_fast = kw.get("ma_fast", CONFIG.vmc_ma_fast)
        self.ma_slow = kw.get("ma_slow", CONFIG.vmc_ma_slow)
        self.swing_lookback = kw.get("swing_lookback", CONFIG.vmc_swing)
        self.stop_max_atr = kw.get("stop_max_atr", CONFIG.stop_max_atr)
        self.stop_min_atr = kw.get("stop_min_atr", CONFIG.stop_min_atr)
        self.atr_period = kw.get("atr_period", CONFIG.atr_period)


def ema(arr: np.ndarray, period: int) -> np.ndarray:
    """EMA récursive standard (alpha = 2/(period+1))."""
    out = np.empty_like(arr, dtype="float64")
    alpha = 2.0 / (period + 1.0)
    out[0] = arr[0]
    for i in range(1, arr.size):
        out[i] = out[i - 1] + alpha * (arr[i] - out[i - 1])
    return out


def wavetrend(df_tf: pd.DataFrame, n1: int = 10, n2: int = 21,
              smooth: int = 4) -> tuple[np.ndarray, np.ndarray]:
    """(wt1, wt2) — formule canonique LazyBear/VuManChu. NaN si d nul."""
    ap = ((df_tf["high"] + df_tf["low"] + df_tf["close"]) / 3.0).to_numpy("float64")
    esa = ema(ap, n1)
    d = ema(np.abs(ap - esa), n1)
    with np.errstate(divide="ignore", invalid="ignore"):
        ci = np.where(d > 0, (ap - esa) / (0.015 * d), 0.0)
    wt1 = ema(ci, n2)
    wt2 = pd.Series(wt1).rolling(smooth).mean().to_numpy()
    return wt1, wt2


def _warmup(params: WaveTrendParams) -> int:
    return max(params.ma_slow, params.n1 + params.n2 + params.smooth,
               params.atr_period) + 2


def _arrays(df_tf: pd.DataFrame, params: WaveTrendParams):
    close = df_tf["close"].to_numpy("float64")
    ma_f = pd.Series(close).rolling(params.ma_fast).mean().to_numpy()
    ma_s = pd.Series(close).rolling(params.ma_slow).mean().to_numpy()
    wt1, wt2 = wavetrend(df_tf, params.n1, params.n2, params.smooth)
    atr = atr_wilder(df_tf, params.atr_period).to_numpy()
    return close, ma_f, ma_s, wt1, wt2, atr


def _sig_at(i, ma_f, ma_s, wt1, wt2, params) -> Optional[str]:
    if not (np.isfinite(wt1[i]) and np.isfinite(wt2[i]) and np.isfinite(wt1[i - 1])
            and np.isfinite(wt2[i - 1]) and np.isfinite(ma_f[i]) and np.isfinite(ma_s[i])):
        return None
    cross_up = wt1[i - 1] <= wt2[i - 1] and wt1[i] > wt2[i]
    cross_dn = wt1[i - 1] >= wt2[i - 1] and wt1[i] < wt2[i]
    if ma_f[i] > ma_s[i] and cross_up and wt1[i] < -params.os_level:
        return "long"
    if ma_f[i] < ma_s[i] and cross_dn and wt1[i] > params.os_level:
        return "short"
    return None


def detect_wavetrend_setups(df_tf: pd.DataFrame,
                            params: Optional[WaveTrendParams] = None) -> list[Setup]:
    """Tous les setups VuManChu/WaveTrend d'une série TF. Zéro look-ahead."""
    params = params or WaveTrendParams()
    n = len(df_tf)
    if n < _warmup(params) + 1:
        return []
    _, ma_f, ma_s, wt1, wt2, atr = _arrays(df_tf, params)
    out: list[Setup] = []
    for i in range(_warmup(params), n):
        if not (np.isfinite(atr[i]) and atr[i] > 0):
            continue
        d = _sig_at(i, ma_f, ma_s, wt1, wt2, params)
        if d:
            s = _build(df_tf, i, d, atr[i], params)
            if s:
                out.append(s)
    return out


def latest_wavetrend_setup(df_tf: pd.DataFrame,
                           params: Optional[WaveTrendParams] = None) -> Optional[Setup]:
    """Setup sur la DERNIÈRE bougie close (usage LIVE), ou None."""
    params = params or WaveTrendParams()
    n = len(df_tf)
    if n < _warmup(params) + 1:
        return None
    _, ma_f, ma_s, wt1, wt2, atr = _arrays(df_tf, params)
    i = n - 1
    if not (np.isfinite(atr[i]) and atr[i] > 0):
        return None
    d = _sig_at(i, ma_f, ma_s, wt1, wt2, params)
    return _build(df_tf, i, d, atr[i], params) if d else None


def market_state_wt(df_tf: pd.DataFrame,
                    params: Optional[WaveTrendParams] = None) -> Optional[dict]:
    """Photographie du marché à la dernière bougie close (affichage direct)."""
    params = params or WaveTrendParams()
    n = len(df_tf)
    if n < _warmup(params) + 1:
        return None
    close, ma_f, ma_s, wt1, wt2, _atr = _arrays(df_tf, params)
    i = n - 1
    if not (np.isfinite(wt1[i]) and np.isfinite(wt2[i])
            and np.isfinite(ma_f[i]) and np.isfinite(ma_s[i])):
        return None
    trend = "haussier" if ma_f[i] > ma_s[i] else "baissier" if ma_f[i] < ma_s[i] else "plat"
    return {"close": float(close[i]), "wt1": float(wt1[i]), "wt2": float(wt2[i]),
            "wt1_prev": float(wt1[i - 1]) if np.isfinite(wt1[i - 1]) else None,
            "trend": trend, "signal": _sig_at(i, ma_f, ma_s, wt1, wt2, params),
            "bar_open_ms": int(df_tf["open_time"].iloc[i]),
            "oversold": bool(wt1[i] < -params.os_level),
            "overbought": bool(wt1[i] > params.os_level)}
