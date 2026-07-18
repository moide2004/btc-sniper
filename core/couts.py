"""Coûts et verdicts par timeframe (§5.5).

Taker 0,10 % (défaut) et maker 0,03 % affichés en parallèle. c% = coût/σ_bougie.
Verdicts :
  > 25 %  → « NON TRADABLE — zone d'exécution »
  10–25 % → « exiger RR ≥ 1,5 »
  < 10 %  → « coûts secondaires »
Les seuils ne sont pas modifiés sans accord (§9).
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import CONFIG


def roundtrip_fraction(fee: float) -> float:
    """Coût aller-retour en fraction de prix (entrée + sortie)."""
    return 2.0 * fee


def cost_in_r(fee: float, price: float, atr: float) -> float:
    """Coût aller-retour exprimé en R (1 R = 1×ATR). Sert à l'EV (§5.3)."""
    if atr is None or atr <= 0 or price <= 0:
        return float("nan")
    return roundtrip_fraction(fee) * price / atr


@dataclass
class CostVerdict:
    fee: float
    cost_pct_sigma: float   # c% = coût/σ_bougie
    verdict: str
    level: str              # secondaire | rr15 | non_tradable


def _verdict(cost_pct: float) -> tuple[str, str]:
    if cost_pct != cost_pct:  # NaN
        return "indéterminé (σ inconnu)", "inconnu"
    if cost_pct > 0.25:
        return "NON TRADABLE — zone d'exécution", "non_tradable"
    if cost_pct >= 0.10:
        return "exiger RR ≥ 1,5", "rr15"
    return "coûts secondaires", "secondaire"


def cost_verdict(sigma_bougie: float, fee: float | None = None) -> CostVerdict:
    fee = CONFIG.fee_taker if fee is None else fee
    if sigma_bougie is None or sigma_bougie != sigma_bougie or sigma_bougie <= 0:
        return CostVerdict(fee, float("nan"), "indéterminé (σ inconnu)", "inconnu")
    cost_pct = roundtrip_fraction(fee) / sigma_bougie
    label, level = _verdict(cost_pct)
    return CostVerdict(fee, cost_pct, label, level)


def both_verdicts(sigma_bougie: float) -> dict:
    """Taker et maker affichés en parallèle (§5.5)."""
    taker = cost_verdict(sigma_bougie, CONFIG.fee_taker)
    maker = cost_verdict(sigma_bougie, CONFIG.fee_maker)
    return {
        "taker": {"fee": taker.fee, "cost_pct_sigma": taker.cost_pct_sigma,
                  "verdict": taker.verdict, "level": taker.level},
        "maker": {"fee": maker.fee, "cost_pct_sigma": maker.cost_pct_sigma,
                  "verdict": maker.verdict, "level": maker.level},
    }
