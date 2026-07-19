"""Couche exécution 1m/5m (§5.10) — JAMAIS source de signal.

Ticket actif → limite échelonnée : prix visé = entrée − 0,25×ATR_étage (long)
ou entrée + 0,25×ATR_étage (short). Annulée à l'invalidation du ticket.
KPI journalisé : amélioration d'entrée en R vs marché (attendu +0,05 à +0,15R).

Interdit (§9) : créer, filtrer ou annuler un signal d'étage — ce module ne
fait que placer/constater des limites sur des tickets déjà émis.
"""
from __future__ import annotations

LIMIT_OFFSET_ATR = 0.25   # §5.10


def limit_price(entry: float, atr: float, direction: str) -> float:
    """Prix de la limite échelonnée (§5.10)."""
    sign = 1.0 if direction == "long" else -1.0
    return entry - sign * LIMIT_OFFSET_ATR * atr


def is_filled(candle_1m: dict, price: float, direction: str) -> bool:
    """La limite est-elle touchée par cette bougie 1m ? (contact suffit)."""
    if direction == "long":
        return float(candle_1m["low"]) <= price
    return float(candle_1m["high"]) >= price


def entry_improvement_r(entry_market: float, fill: float, atr: float,
                        direction: str) -> float:
    """KPI : amélioration d'entrée en R vs entrée marché (§5.10)."""
    if not (atr and atr > 0):
        return 0.0
    if direction == "long":
        return (entry_market - fill) / atr
    return (fill - entry_market) / atr
