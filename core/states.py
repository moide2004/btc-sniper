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
