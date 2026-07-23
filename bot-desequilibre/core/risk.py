"""Dimensionnement (§2.6) et BUDGET DE RISQUE PARTAGÉ BTC+ETH (§5.4).

Capital unique partagé. Le risque d'une position = capital·riskPct (×
shortRiskFactor en short), réparti par la distance de stop. Règles de portefeuille :
  • plafond de risque ouvert : risk_cap_pct (défaut 4 %) ;
  • 2ᵉ position de MÊME direction (tous actifs/TF) comptée ×1,5 ;
  • corrélation BTC/ETH mensuelle ; ×2 sur le risque compté si ρ > corr_high ;
  • INTERDICTION de positions OPPOSÉES entre actifs corrélés (ρ > corr_high).

En P2 aucune position n'est ouverte (paper trading = P4) : ce module CALCULE la
taille et ÉVALUE si un ticket TIENDRAIT dans le budget (annotation informative).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from .config import CONFIG


@dataclass
class Sizing:
    risk_usd: float          # montant risqué (déjà ×shortRiskFactor si short)
    size_units: float        # quantité d'actif
    notional_usd: float      # exposition = size·entrée
    risk_pct_capital: float  # risk_usd / capital, en %


def size_position(entry: float, stop_dist: float, direction: str,
                  capital: Optional[float] = None) -> Optional[Sizing]:
    """Taille §2.6 : capital·riskPct / stopDist (short : ×shortRiskFactor)."""
    if stop_dist <= 0 or entry <= 0:
        return None
    capital = CONFIG.capital_usd if capital is None else capital
    factor = CONFIG.short_risk_factor if direction == "short" else 1.0
    risk_usd = capital * CONFIG.risk_pct * factor
    size = risk_usd / stop_dist
    return Sizing(risk_usd=risk_usd, size_units=size, notional_usd=size * entry,
                  risk_pct_capital=100.0 * risk_usd / capital)


def monthly_corr(df1_1m: pd.DataFrame, df2_1m: pd.DataFrame, days: int = 30) -> Optional[float]:
    """Corrélation des rendements JOURNALIERS des deux actifs sur `days` jours.
    Renvoie None si l'historique commun est insuffisant."""
    if df1_1m.empty or df2_1m.empty:
        return None

    def _daily(df):
        s = pd.Series(df["close"].to_numpy(),
                      index=pd.to_datetime(df["open_time"].to_numpy(), unit="ms", utc=True))
        return s.resample("1D").last().pct_change().dropna()

    a, b = _daily(df1_1m), _daily(df2_1m)
    joined = pd.concat([a, b], axis=1, join="inner").dropna().tail(days)
    if len(joined) < 5:
        return None
    c = joined.iloc[:, 0].corr(joined.iloc[:, 1])
    return None if pd.isna(c) else float(c)


@dataclass
class OpenPosition:
    symbol: str
    direction: str
    risk_usd: float


class RiskBook:
    """Évalue si un nouveau ticket tient dans le budget partagé (§5.4)."""

    def __init__(self, capital: Optional[float] = None, corr: Optional[float] = None) -> None:
        self.capital = CONFIG.capital_usd if capital is None else capital
        self.corr = corr
        self.cap_pct = CONFIG.risk_cap_pct

    def _counted_risk(self, positions: list[OpenPosition], new: OpenPosition) -> float:
        """Risque COMPTÉ (pondéré) du portefeuille + le nouveau ticket."""
        total = 0.0
        same_dir_seen = 0
        high_corr = self.corr is not None and abs(self.corr) > CONFIG.corr_high
        for p in positions + [new]:
            w = 1.0
            same_dir_seen += 1 if p.direction == new.direction else 0
            if p.direction == new.direction and same_dir_seen > 1:
                w *= 1.5                       # 2ᵉ position de même direction
            if high_corr:
                w *= 2.0                       # actifs très corrélés
            total += p.risk_usd * w
        return total

    def evaluate(self, new: OpenPosition, positions: Optional[list[OpenPosition]] = None) -> dict:
        positions = positions or []
        high_corr = self.corr is not None and abs(self.corr) > CONFIG.corr_high
        # Interdiction des positions opposées entre actifs corrélés.
        if high_corr:
            for p in positions:
                if p.symbol != new.symbol and p.direction != new.direction:
                    return {"allowed": False, "raison": "positions opposées interdites (ρ élevé)",
                            "counted_pct": None, "corr": self.corr}
        counted = self._counted_risk(positions, new)
        counted_pct = 100.0 * counted / self.capital
        allowed = counted_pct <= self.cap_pct
        return {"allowed": allowed, "counted_pct": counted_pct,
                "cap_pct": self.cap_pct, "corr": self.corr,
                "raison": None if allowed else f"plafond {self.cap_pct}% dépassé"}
