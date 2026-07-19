"""Empilement bayésien (§5.6) et référence neutre (§5.7).

ln O = κ·Σ ln LR_i, κ = 0,6, plafond 85 % ; contributions par timeframe
affichées.

Estimation des LR_i (« estimés sur l'historique », §5.6) : chaque échelle vote
avec la MOYENNE DU POSTERIOR Beta(5+k, 5+n−k) — le même amortisseur que §5.2 —
et non le p̂ brut. Mécanisme : un p̂ brut de 100 % à n=3 donnerait des cotes
quasi infinies et écraserait la synthèse ; le posterior le ramène à ~62 % et
laisse les échelles à grand n voter à leur juste poids (n=3 → prior dominant ;
n=50 000 → p̂ quasi inchangé).
Décision utilisateur du 2026-07-20 (§9) : « modifier P(hausse) de manière
cohérente avec la réalité statistique du marché ».

κ, le plafond 85 % et la forme du produit restent ceux du cahier des charges.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

KAPPA = 0.6
CAP = 0.85          # plafond (et plancher symétrique 1−CAP)
EPS = 1e-6
PRIOR_A = 5.0       # prior Beta(5,5) — identique à §5.2 (proba_engine)
PRIOR_B = 5.0


@dataclass
class StackEntry:
    timeframe: str
    p_up: float
    n: int


def _posterior_mean(p_hat: float, n: int) -> float:
    """Moyenne du posterior Beta(5+k, 5+n−k) avec k = p̂·n (§5.2)."""
    k = p_hat * n
    return (PRIOR_A + k) / (PRIOR_A + PRIOR_B + n)


def stack(entries: list[StackEntry], kappa: float = KAPPA, cap: float = CAP) -> dict:
    """Combine les probabilités directionnelles des échelles en une proba
    globale P(hausse). Retourne proba, log-odds et contributions par échelle."""
    contributions = []
    ln_o = 0.0
    used = 0
    for e in entries:
        if e.n <= 0 or e.p_up != e.p_up:  # n nul ou NaN
            continue
        p_vote = _posterior_mean(e.p_up, e.n)
        p = min(max(p_vote, EPS), 1 - EPS)
        lr = p / (1 - p)
        contrib = kappa * math.log(lr)
        ln_o += contrib
        used += 1
        contributions.append({
            "timeframe": e.timeframe, "p_up": e.p_up, "p_vote": p_vote,
            "n": e.n, "lr": lr, "contribution": contrib,
        })
    if used == 0:
        return {"p_up": float("nan"), "ln_odds": float("nan"),
                "n_timeframes": 0, "contributions": [], "capped": False}

    odds = math.exp(ln_o)
    prob = odds / (1 + odds)
    capped = prob > cap or prob < (1 - cap)
    prob = min(max(prob, 1 - cap), cap)  # plafond 85 % / plancher 15 %
    # contributions triées par poids absolu décroissant (les plus influentes d'abord)
    contributions.sort(key=lambda c: abs(c["contribution"]), reverse=True)
    return {
        "p_up": prob, "ln_odds": ln_o, "n_timeframes": used,
        "contributions": contributions, "capped": capped, "kappa": kappa,
    }


def lognormal_reference(daily_close, horizon_days: int) -> dict:
    """Référence neutre (§5.7) : P(S_T > S_0) log-normale, μ estimé sur
    l'historique disponible (≈ 4 ans si présents). Sert de repère à côté du
    conditionnel pour les échelles ≥ 1D."""
    import numpy as np
    c = np.asarray(daily_close, dtype="float64")
    if len(c) < 30:
        return {"p_up": float("nan"), "mu_daily": float("nan"), "sigma_daily": float("nan")}
    logret = np.diff(np.log(c))
    mu = float(np.mean(logret))
    sigma = float(np.std(logret))
    if sigma <= 0:
        return {"p_up": float("nan"), "mu_daily": mu, "sigma_daily": sigma}
    # P(S_T > S_0) = P(somme des rendements > 0), ~ Normale(μ·H, σ²·H)
    from scipy.stats import norm
    z = (mu * horizon_days) / (sigma * math.sqrt(horizon_days))
    return {"p_up": float(norm.cdf(z)), "mu_daily": mu, "sigma_daily": sigma,
            "horizon_days": horizon_days}
