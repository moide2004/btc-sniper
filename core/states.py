"""États : Régime × Extension (§4).

En P1, ce module fournit le strict nécessaire à la REPRISE (§7.3 : « recalcule
les états courants ») : indicateurs sans look-ahead (SMA200 du dernier jour
COMPLÉTÉ, RSD(14), momentum 26). La cartographie complète en cases + la zone
de volatilité v1.5 (n ≥ 200 par sous-case) relèvent de P2 et ne sont pas
activées ici sans validation (§9).

Zéro look-ahead : tous les indicateurs n'utilisent que des bougies CLÔTURÉES.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=window).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI de Wilder (§4 : RSI(14) de la timeframe)."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - 100 / (1 + rs)
    out[avg_loss == 0] = 100.0
    return out


def momentum(close: pd.Series, period: int = 26) -> pd.Series:
    """Momentum simple : close / close[-period] - 1 (régime ≥ 1W, §4)."""
    return close / close.shift(period) - 1.0


def extension_label(rsi_value: Optional[float]) -> str:
    if rsi_value is None or pd.isna(rsi_value):
        return "inconnu"
    if rsi_value < 30:
        return "survendu"
    if rsi_value > 70:
        return "surachete"
    return "neutre"


@dataclass
class CurrentState:
    timeframe: str
    regime: str          # bull | bear | inconnu
    extension: str       # survendu | neutre | surachete | inconnu
    rsi: Optional[float]
    close: Optional[float]
    ref: str             # "sma200" (≤1D) | "momentum26" (≥1W)

    @property
    def label(self) -> str:
        return f"{self.regime}/{self.extension}"


# Timeframes dont le régime vient de la SMA200 QUOTIDIENNE (≤ 1D, §4) vs
# celles dont le régime vient du momentum 26 périodes (≥ 1W).
MOMENTUM_TF = {"1W", "2W", "1M"}


def daily_sma200_ref(daily_df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Prépare la référence de régime ≤ 1D : (close_time, SMA200) du 1D.
    Le régime d'une bougie utilise la SMA200 du DERNIER JOUR COMPLÉTÉ
    (zéro look-ahead, §4)."""
    if daily_df is None or daily_df.empty:
        return np.array([], dtype="int64"), np.array([], dtype="float64")
    d = daily_df.sort_values("open_time").reset_index(drop=True)
    sma = d["close"].astype("float64").rolling(200, min_periods=200).mean()
    return d["close_time"].to_numpy("int64"), sma.to_numpy("float64")


def _regime_le_1d(df: pd.DataFrame, daily_ref: tuple[np.ndarray, np.ndarray]) -> np.ndarray:
    """Régime bull/bear par barre via SMA200 du dernier jour complété."""
    day_ct, day_sma = daily_ref
    n = len(df)
    out = np.full(n, "inconnu", dtype=object)
    if len(day_ct) == 0:
        return out
    ot = df["open_time"].to_numpy("int64")
    close = df["close"].to_numpy("float64")
    # dernier jour dont close_time < open_time de la barre
    idx = np.searchsorted(day_ct, ot, side="left") - 1
    for i in range(n):
        j = idx[i]
        if j < 0 or np.isnan(day_sma[j]):
            continue
        out[i] = "bull" if close[i] > day_sma[j] else "bear"
    return out


def _regime_ge_1w(df: pd.DataFrame) -> np.ndarray:
    """Régime bull/bear par barre via momentum 26 périodes (§4)."""
    mom = momentum(df["close"].astype("float64"), 26).to_numpy("float64")
    out = np.full(len(df), "inconnu", dtype=object)
    out[mom > 0] = "bull"
    out[mom < 0] = "bear"
    out[np.isnan(mom)] = "inconnu"
    return out


def _extension_series(df: pd.DataFrame) -> np.ndarray:
    r = rsi(df["close"].astype("float64")).to_numpy("float64")
    out = np.full(len(df), "inconnu", dtype=object)
    out[r < 30] = "survendu"
    out[(r >= 30) & (r <= 70)] = "neutre"
    out[r > 70] = "surachete"
    out[np.isnan(r)] = "inconnu"
    return out


def state_labels(
    df: pd.DataFrame, timeframe: str, daily_ref: tuple[np.ndarray, np.ndarray]
) -> tuple[np.ndarray, str]:
    """Étiquette chaque barre par son état « régime/extension » (§4) et renvoie
    (labels, état_courant)."""
    if df is None or df.empty:
        return np.array([], dtype=object), "inconnu/inconnu"
    regime = _regime_ge_1w(df) if timeframe in MOMENTUM_TF else _regime_le_1d(df, daily_ref)
    ext = _extension_series(df)
    labels = np.array([f"{r}/{e}" for r, e in zip(regime, ext)], dtype=object)
    return labels, str(labels[-1])


def current_state(df: pd.DataFrame, timeframe: str, use_momentum: bool = False) -> CurrentState:
    """État courant d'une timeframe à partir de ses bougies CLÔTURÉES.

    ≤ 1D : régime = clôture > SMA200 quotidienne du dernier jour complété.
           Ici, faute d'accès direct au 1D dans ce module minimal, on utilise
           la SMA200 de la timeframe fournie comme proxy régime — le calcul
           régime « SMA200 quotidienne » exact sera câblé en P2 avec le 1D.
    ≥ 1W : régime = momentum 26 périodes.
    """
    if df is None or df.empty:
        return CurrentState(timeframe, "inconnu", "inconnu", None, None,
                            "momentum26" if use_momentum else "sma200")
    close = df["close"].astype("float64").reset_index(drop=True)
    last_close = float(close.iloc[-1])
    r = rsi(close)
    rsi_v = float(r.iloc[-1]) if not pd.isna(r.iloc[-1]) else None

    if use_momentum:
        mom = momentum(close)
        m = mom.iloc[-1]
        regime = "inconnu" if pd.isna(m) else ("bull" if m > 0 else "bear")
        ref = "momentum26"
    else:
        s = sma(close, 200)
        sv = s.iloc[-1]
        regime = "inconnu" if pd.isna(sv) else ("bull" if last_close > sv else "bear")
        ref = "sma200"

    return CurrentState(timeframe, regime, extension_label(rsi_v), rsi_v, last_close, ref)
