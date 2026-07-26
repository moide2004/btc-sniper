"""BOT 2 (laboratoire) — stratégie « TREND-PULLBACK » : acheter le repli en
tendance haussière, vendre le rebond en tendance baissière.

Règles ÉCRITES (proposées à l'humain le 2026-07-26, validées « fais-le ») :
  • Tendance : SMA(ma_fast=20) > SMA(ma_slow=50) → LONGS seuls ; < → SHORTS seuls.
  • Entrée LONG : le %K du Stoch RSI (RSI 14 → stoch 14, lissage 3) re-croise
    AU-DESSUS de os_low (20) après avoir été en dessous, tendance haussière.
  • Entrée SHORT (miroir) : %K re-croise EN DESSOUS de os_high (80).
  • Signal à la CLÔTURE ; entrée à l'OPEN de la bougie suivante (zéro look-ahead).
  • SL : plus-bas (long) / plus-haut (short) des `swing_lookback` (10) dernières
    bougies ; REJET si stopDist > stop_max_atr·ATR ; PLANCHER stop_min_atr·ATR.
  • TP : rr × stopDist ; break-even : ceux du bot (CONFIG.be_trigger), via le
    même moteur de fill (core/paper.py). Coûts identiques. Mesure identique.

Ce module NE trade pas et n'émet PAS de tickets : c'est un banc d'essai jugé
par le même verdict P5 que le Bot Déséquilibré.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from .config import CONFIG
from .fibonacci import Setup
from .indicators import atr_wilder


class PullbackParams:
    def __init__(self, **kw) -> None:
        self.ma_fast = kw.get("ma_fast", CONFIG.pb_ma_fast)
        self.ma_slow = kw.get("ma_slow", CONFIG.pb_ma_slow)
        self.rsi_period = kw.get("rsi_period", CONFIG.pb_rsi_period)
        self.stoch_period = kw.get("stoch_period", CONFIG.pb_stoch_period)
        self.k_smooth = kw.get("k_smooth", CONFIG.pb_k_smooth)
        self.os_low = kw.get("os_low", CONFIG.pb_os_low)
        self.os_high = kw.get("os_high", CONFIG.pb_os_high)
        self.swing_lookback = kw.get("swing_lookback", CONFIG.pb_swing)
        self.stop_max_atr = kw.get("stop_max_atr", CONFIG.stop_max_atr)
        self.stop_min_atr = kw.get("stop_min_atr", CONFIG.stop_min_atr)
        self.atr_period = kw.get("atr_period", CONFIG.atr_period)


def rsi_wilder(close: np.ndarray, period: int = 14) -> np.ndarray:
    """RSI lissage de Wilder. Valeurs [0, 100] ; premiers points peu fiables
    (warmup), comme tout RSI."""
    delta = np.diff(close, prepend=close[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    ag = np.empty_like(close)
    al = np.empty_like(close)
    ag[0], al[0] = gain[0], loss[0]
    a = 1.0 / period
    for i in range(1, close.size):
        ag[i] = ag[i - 1] + a * (gain[i] - ag[i - 1])
        al[i] = al[i - 1] + a * (loss[i] - al[i - 1])
    out = np.empty_like(close)
    for i in range(close.size):
        if al[i] <= 0:
            out[i] = 100.0 if ag[i] > 0 else 50.0
        else:
            rs = ag[i] / al[i]
            out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def stoch_rsi_k(close: np.ndarray, rsi_period: int = 14, stoch_period: int = 14,
                k_smooth: int = 3) -> np.ndarray:
    """%K du Stoch RSI : stochastique du RSI, lissé SMA(k_smooth). NaN en warmup."""
    r = pd.Series(rsi_wilder(close, rsi_period))
    lo = r.rolling(stoch_period).min()
    hi = r.rolling(stoch_period).max()
    span = (hi - lo)
    k_raw = pd.Series(np.where(span > 0, 100.0 * (r - lo) / span, 50.0),
                      index=r.index)
    k_raw[span.isna()] = np.nan
    k = k_raw.rolling(k_smooth).mean().to_numpy()
    return np.clip(k, 0.0, 100.0)      # borne les résidus d'arrondi flottant


def _build(df, i, direction, atr_i, params) -> Optional[Setup]:
    n = len(df)
    lo_w = float(df["low"].iloc[max(0, i - params.swing_lookback + 1):i + 1].min())
    hi_w = float(df["high"].iloc[max(0, i - params.swing_lookback + 1):i + 1].max())
    if i + 1 < n:
        entry = float(df["open"].iloc[i + 1])
        entry_time = int(df["open_time"].iloc[i + 1])
        proxy = False
    else:
        entry = float(df["close"].iloc[i])
        entry_time = int(df["close_time"].iloc[i]) + 1
        proxy = True
    sl_line = lo_w if direction == "long" else hi_w
    stop_raw = (entry - sl_line) if direction == "long" else (sl_line - entry)
    if stop_raw <= 0 or stop_raw > params.stop_max_atr * atr_i:
        return None
    stop_eff = max(stop_raw, params.stop_min_atr * atr_i)
    sl = entry - stop_eff if direction == "long" else entry + stop_eff
    return Setup(idx=i, direction=direction,
                 signal_open_ms=int(df["open_time"].iloc[i]),
                 signal_close_ms=int(df["close_time"].iloc[i]),
                 entry_time_ms=entry_time, entry=entry, sl=sl, stop_dist=stop_eff,
                 amplitude=hi_w - lo_w, atr=atr_i, fib={}, entry_is_proxy=proxy)


def _warmup(params: PullbackParams) -> int:
    return max(params.ma_slow, params.rsi_period + params.stoch_period
               + params.k_smooth, params.atr_period) + 2


def latest_pullback_setup(df_tf: pd.DataFrame,
                          params: Optional[PullbackParams] = None) -> Optional[Setup]:
    """Setup trend-pullback sur la DERNIÈRE bougie close (usage LIVE), ou None."""
    params = params or PullbackParams()
    n = len(df_tf)
    if n < _warmup(params) + 1:
        return None
    close = df_tf["close"].to_numpy(dtype="float64")
    ma_f = pd.Series(close).rolling(params.ma_fast).mean().to_numpy()
    ma_s = pd.Series(close).rolling(params.ma_slow).mean().to_numpy()
    k = stoch_rsi_k(close, params.rsi_period, params.stoch_period, params.k_smooth)
    atr = atr_wilder(df_tf, params.atr_period).to_numpy()
    i = n - 1
    if not (np.isfinite(atr[i]) and atr[i] > 0 and np.isfinite(k[i])
            and np.isfinite(k[i - 1]) and np.isfinite(ma_f[i]) and np.isfinite(ma_s[i])):
        return None
    if ma_f[i] > ma_s[i] and k[i - 1] < params.os_low <= k[i]:
        return _build(df_tf, i, "long", atr[i], params)
    if ma_f[i] < ma_s[i] and k[i - 1] > params.os_high >= k[i]:
        return _build(df_tf, i, "short", atr[i], params)
    return None


def market_state(df_tf: pd.DataFrame,
                 params: Optional[PullbackParams] = None) -> Optional[dict]:
    """Photographie du marché à la dernière bougie close : tendance, %K, signal.
    Sert à l'affichage « analyse du marché » (aucune décision, juste l'état)."""
    params = params or PullbackParams()
    n = len(df_tf)
    if n < _warmup(params) + 1:
        return None
    close = df_tf["close"].to_numpy(dtype="float64")
    ma_f = pd.Series(close).rolling(params.ma_fast).mean().to_numpy()
    ma_s = pd.Series(close).rolling(params.ma_slow).mean().to_numpy()
    k = stoch_rsi_k(close, params.rsi_period, params.stoch_period, params.k_smooth)
    i = n - 1
    if not (np.isfinite(k[i]) and np.isfinite(k[i - 1])
            and np.isfinite(ma_f[i]) and np.isfinite(ma_s[i])):
        return None
    trend = "haussier" if ma_f[i] > ma_s[i] else "baissier" if ma_f[i] < ma_s[i] else "plat"
    signal = None
    if ma_f[i] > ma_s[i] and k[i - 1] < params.os_low <= k[i]:
        signal = "long"
    elif ma_f[i] < ma_s[i] and k[i - 1] > params.os_high >= k[i]:
        signal = "short"
    return {"close": float(close[i]), "ma_fast": float(ma_f[i]),
            "ma_slow": float(ma_s[i]), "k": float(k[i]), "k_prev": float(k[i - 1]),
            "trend": trend, "signal": signal,
            "bar_open_ms": int(df_tf["open_time"].iloc[i]),
            "oversold": bool(k[i] < params.os_low),
            "overbought": bool(k[i] > params.os_high)}


def detect_pullback_setups(df_tf: pd.DataFrame,
                           params: Optional[PullbackParams] = None) -> list[Setup]:
    """Tous les setups trend-pullback d'une série TF. Sans look-ahead : tous les
    indicateurs au bar i n'utilisent que les bougies ≤ i ; entrée en i+1."""
    params = params or PullbackParams()
    n = len(df_tf)
    warmup = max(params.ma_slow, params.rsi_period + params.stoch_period
                 + params.k_smooth, params.atr_period) + 2
    if n < warmup + 1:
        return []
    close = df_tf["close"].to_numpy(dtype="float64")
    ma_f = pd.Series(close).rolling(params.ma_fast).mean().to_numpy()
    ma_s = pd.Series(close).rolling(params.ma_slow).mean().to_numpy()
    k = stoch_rsi_k(close, params.rsi_period, params.stoch_period, params.k_smooth)
    atr = atr_wilder(df_tf, params.atr_period).to_numpy()
    out: list[Setup] = []
    for i in range(warmup, n):
        if not (np.isfinite(atr[i]) and atr[i] > 0 and np.isfinite(k[i])
                and np.isfinite(k[i - 1]) and np.isfinite(ma_f[i]) and np.isfinite(ma_s[i])):
            continue
        trend_up = ma_f[i] > ma_s[i]
        trend_dn = ma_f[i] < ma_s[i]
        long_sig = trend_up and k[i - 1] < params.os_low <= k[i]
        short_sig = trend_dn and k[i - 1] > params.os_high >= k[i]
        if long_sig:
            s = _build(df_tf, i, "long", atr[i], params)
            if s:
                out.append(s)
        elif short_sig:
            s = _build(df_tf, i, "short", atr[i], params)
            if s:
                out.append(s)
    return out
