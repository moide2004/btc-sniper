"""Moteur statistique — probabilités, incertitude, espérance, réalisme (§5.1–5.4).

Formules implémentées TELLES QUELLES (§5, §9 : rien n'est modifié sans accord).

Toutes les mesures sont SANS CHEVAUCHEMENT et SANS look-ahead : une issue à
l'horizon H (ou jusqu'à barrière) n'utilise que des bougies postérieures à
l'entrée, et les échantillons ne se recouvrent pas.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np
import pandas as pd
from scipy.stats import beta as beta_dist

Z95 = 1.96  # quantile normal 95 % (§5.2)

# Durée d'une bougie (ms) — pour convertir la durée médiane en jours (§5.14).
TF_MS_ENGINE = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "12h": 43_200_000, "1D": 86_400_000,
    "1W": 604_800_000, "2W": 1_209_600_000, "1M": 2_592_000_000,
}


# ===========================================================================
# 5.2 — Incertitude
# ===========================================================================
@dataclass
class Wilson:
    center: float
    half: float
    low: float
    high: float


def wilson_interval(k: int, n: int, z: float = Z95) -> Wilson:
    """Intervalle de Wilson 95 % (§5.2).
    centre = (p̂ + z²/2n)/(1 + z²/n)
    demi-largeur = z·√(p̂(1−p̂)/n + z²/4n²)/(1 + z²/n)"""
    if n <= 0:
        return Wilson(float("nan"), float("nan"), float("nan"), float("nan"))
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return Wilson(center, half, max(0.0, center - half), min(1.0, center + half))


@dataclass
class BetaPosterior:
    mean: float
    std: float
    p_prudent: float  # quantile 25 %


def beta_posterior(k: int, n: int, prior_a: float = 5.0, prior_b: float = 5.0) -> BetaPosterior:
    """Posterior Beta(5+k, 5+n−k) (§5.2) : moyenne, écart-type, p_prudent = q25."""
    a = prior_a + k
    b = prior_b + (n - k)
    mean = a / (a + b)
    var = (a * b) / ((a + b) ** 2 * (a + b + 1))
    p_prudent = float(beta_dist.ppf(0.25, a, b))
    return BetaPosterior(mean, float(np.sqrt(var)), p_prudent)


# ===========================================================================
# Indicateurs de base
# ===========================================================================
def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR de Wilder (§5.1)."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


# ===========================================================================
# 5.1 — Probabilités d'issue
# ===========================================================================
def fixed_horizon_counts(
    close: pd.Series, horizon: int, mask: Optional[np.ndarray] = None, direction: str = "long"
) -> tuple[int, int]:
    """p̂ = fréquence de (clôture à H bougies > clôture actuelle) pour un long,
    (< pour un short), SANS chevauchement (§5.1, §5.14). Si `mask` (bool par
    barre) est fourni, seules les entrées où mask=True sont éligibles ; le
    non-chevauchement (espacement ≥ H) reste garanti."""
    c = close.to_numpy(dtype="float64")
    n_bars = len(c)
    k = n = 0
    i = 0
    while i + horizon < n_bars:
        if mask is None or mask[i]:
            up = c[i + horizon] > c[i]
            if (direction == "long" and up) or (direction == "short" and c[i + horizon] < c[i]):
                k += 1
            n += 1
            i += horizon  # saute l'horizon → sans chevauchement
        else:
            i += 1
    return k, n


def double_barrier_counts(
    df: pd.DataFrame,
    atr_series: pd.Series,
    rr: float,
    direction: str,
    mask: Optional[np.ndarray] = None,
) -> tuple[int, int, float]:
    """p̂ = fréquence de (+RR×ATR touché AVANT −1×ATR), SANS chevauchement,
    mesurée DANS LES DEUX SENS (§5.1, §5.14).

    long  : gain si high atteint entry+RR·ATR avant que low atteigne entry−ATR.
    short : gain si low atteint entry−RR·ATR avant que high atteigne entry+ATR.
    En cas d'ambiguïté (les deux barrières dans la même bougie) → issue
    défavorable (stop d'abord), choix conservateur.

    Retourne (k, n, durée de détention MÉDIANE en bougies) — la durée sert au
    funding directionnel (§5.14 : F = funding annualisé × durée médiane).
    """
    high = df["high"].to_numpy("float64")
    low = df["low"].to_numpy("float64")
    close = df["close"].to_numpy("float64")
    a = atr_series.to_numpy("float64")
    n_bars = len(close)
    k = n = 0
    durations: list[int] = []
    i = 0
    while i < n_bars - 1:
        if (mask is not None and not mask[i]) or np.isnan(a[i]) or a[i] <= 0:
            i += 1
            continue
        entry = close[i]
        if direction == "long":
            up, down = entry + rr * a[i], entry - a[i]
        else:  # short
            up, down = entry - rr * a[i], entry + a[i]
        j = i + 1
        resolved = False
        while j < n_bars:
            if direction == "long":
                hit_stop = low[j] <= down
                hit_target = high[j] >= up
            else:
                hit_stop = high[j] >= down
                hit_target = low[j] <= up
            if hit_stop:            # conservateur : stop prioritaire
                n += 1; resolved = True; break
            if hit_target:
                k += 1; n += 1; resolved = True; break
            j += 1
        if resolved:
            durations.append(j - i)
            i = j + 1              # sans chevauchement : reprend après résolution
        else:
            break                  # plus assez d'historique pour résoudre
    med = float(np.median(durations)) if durations else float("nan")
    return k, n, med


# ===========================================================================
# 5.3 — Espérance nette  &  5.4 — Réalisme
# ===========================================================================
def ev_net(p: float, rr: float, cost_r: float, funding_r: float = 0.0) -> float:
    """EV_R = p·RR − (1−p) − c(timeframe) ± F (§5.3, funding §5.14)."""
    return p * rr - (1 - p) - cost_r + funding_r


@dataclass
class Realism:
    sigma_bougie: float
    cvar99: float
    k_max: float


def realism_block(df: pd.DataFrame, timeframe: str, p_hat: float) -> Realism:
    """§5.4 : σ_bougie, CVaR 99 % empirique, k_max ≈ ln(100)/ln(1/(1−p̂))."""
    close = df["close"].astype("float64")
    logret = np.log(close / close.shift(1)).dropna()
    # σ quotidien = std des rendements log 1 an (√365) — approché sur l'historique
    sigma_daily = float(logret.std()) if len(logret) > 2 else float("nan")

    bars_per_day = {
        "1m": 1440, "5m": 288, "15m": 96, "30m": 48, "1h": 24, "4h": 6,
        "12h": 2, "1D": 1,
    }
    days_per_bar = {"1W": 7, "2W": 14, "1M": 30}
    if timeframe in bars_per_day:
        sigma_bougie = sigma_daily / np.sqrt(bars_per_day[timeframe])
    elif timeframe in days_per_bar:
        sigma_bougie = sigma_daily * np.sqrt(days_per_bar[timeframe])
    else:
        sigma_bougie = sigma_daily

    if len(logret) > 20:
        q = np.quantile(logret, 0.01)
        tail = logret[logret <= q]
        cvar99 = float(-tail.mean()) if len(tail) else float("nan")
    else:
        cvar99 = float("nan")

    if 0 < p_hat < 1:
        k_max = float(np.log(100) / np.log(1 / (1 - p_hat)))
    else:
        k_max = float("inf")
    return Realism(float(sigma_bougie), cvar99, k_max)


# ===========================================================================
# Assemblage d'une mesure complète (une case : tf × état × direction × méthode)
# ===========================================================================
@dataclass
class Measure:
    n: int
    k: int
    p_hat: float
    wilson: dict
    posterior: dict
    ev_brute: float
    cost_r: float
    ev_nette: float
    ev_nette_prudente: float

    def to_dict(self) -> dict:
        return asdict(self)


def build_measure(k: int, n: int, rr: float, cost_r: float, funding_r: float = 0.0) -> Measure:
    """Fabrique une mesure complète : p̂ jamais sans n/intervalle, EV jamais
    sans coûts (§9). L'EV qualifiante est l'EV nette au p_prudent (§5.3)."""
    p_hat = (k / n) if n > 0 else float("nan")
    w = wilson_interval(k, n)
    post = beta_posterior(k, n)
    ev_brute = ev_net(p_hat, rr, 0.0, funding_r) if n > 0 else float("nan")
    ev_nette = ev_net(p_hat, rr, cost_r, funding_r) if n > 0 else float("nan")
    ev_prud = ev_net(post.p_prudent, rr, cost_r, funding_r) if n > 0 else float("nan")
    return Measure(
        n=n, k=k, p_hat=p_hat,
        wilson={"center": w.center, "half": w.half, "low": w.low, "high": w.high},
        posterior={"mean": post.mean, "std": post.std, "p_prudent": post.p_prudent},
        ev_brute=ev_brute, cost_r=cost_r, ev_nette=ev_nette, ev_nette_prudente=ev_prud,
    )


# ===========================================================================
# Assemblage de la MATRICE (§5.8 candidates, §5.14 shorts) — sert la Vue 1
# ===========================================================================
HORIZON_DEFAULT = 10          # §5.1 (défaut 10)
RR_SET = (1.0, 1.5, 2.0)      # §5.1
N_MIN = 200                   # §5.8 : case candidate si n ≥ 200
SHORT_TF_ALLOWED = {"4h", "1D"}  # §5.14 : shorts autorisés sur 4h et 1D (v2)
ALL_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "12h", "1D", "1W", "2W", "1M"]


def _direction_block(
    df: pd.DataFrame, atr_series: pd.Series, mask: np.ndarray, direction: str,
    cost_r: float, cost_level: str, regime: str, timeframe: str,
    funding_ctx: Optional[dict] = None,
) -> dict:
    """`funding_ctx` (§5.14) : {"annualized": taux signé, "price_over_atr": x}
    ou None si le funding est indisponible → F = 0 + badge."""
    # Probabilité directionnelle à horizon fixe : MESURE D'ISSUE pure (p̂, n,
    # intervalle, posterior). Aucune EV publiée ici — une EV sans coûts
    # violerait l'invariant §9 ; les EV vivent dans les barrières (avec coûts).
    fh_k, fh_n = fixed_horizon_counts(df["close"], HORIZON_DEFAULT, mask, direction)
    fh_m = build_measure(fh_k, fh_n, rr=1.0, cost_r=0.0).to_dict()
    fh = {k: fh_m[k] for k in ("n", "k", "p_hat", "wilson", "posterior")}

    # Double barrière aux trois RR (§5.1). Funding (§5.14) :
    # F = funding moyen 7 j annualisé × durée de détention médiane, en R ;
    # +F short si funding positif (reçu), −F long (payé) ; signes inversés si
    # négatif — obtenu en gardant le SIGNE du taux : terme = ±taux×durée×prix/ATR.
    bars_per_day = 86_400_000 / TF_MS_ENGINE.get(timeframe, 86_400_000)
    barriers = []
    for rr in RR_SET:
        k, n, med_bars = double_barrier_counts(df, atr_series, rr, direction, mask)
        if funding_ctx is not None and med_bars == med_bars:
            dur_years = (med_bars / bars_per_day) / 365.0
            f_r = funding_ctx["annualized"] * dur_years * funding_ctx["price_over_atr"]
            funding_r = f_r if direction == "short" else -f_r
        else:
            funding_r = 0.0
        m = build_measure(k, n, rr=rr, cost_r=cost_r, funding_r=funding_r)
        barriers.append({"rr": rr, "duree_mediane_bougies": med_bars,
                         "funding_r": funding_r, **m.to_dict()})

    # Meilleure EV nette prudente parmi les RR respectant le verdict de coûts.
    def rr_ok(rr: float) -> bool:
        if cost_level == "non_tradable" or cost_level == "inconnu":
            return False
        if cost_level == "rr15":
            return rr >= 1.5
        return True  # secondaire

    eligibles = [b for b in barriers if rr_ok(b["rr"]) and b["n"] > 0]
    best = max(eligibles, key=lambda b: b["ev_nette_prudente"], default=None)

    # Short suspect en régime bull, et non autorisé hors 4h/1D en v2 (§5.14).
    short_suspect = direction == "short" and regime == "bull"
    short_blocked_tf = direction == "short" and timeframe not in SHORT_TF_ALLOWED

    candidate = bool(
        best is not None
        and best["ev_nette_prudente"] > 0
        and best["n"] >= N_MIN
        and not short_suspect
        and not short_blocked_tf
    )
    motifs = []
    if best is None:
        motifs.append("aucun RR compatible avec le verdict de coûts")
    else:
        if best["n"] < N_MIN:
            motifs.append(f"n < {N_MIN}")
        if best["ev_nette_prudente"] <= 0:
            motifs.append("EV nette prudente ≤ 0")
    if short_suspect:
        motifs.append("short suspect (régime bull)")
    if short_blocked_tf:
        motifs.append("short non autorisé hors 4h/1D (v2)")

    return {
        "direction": direction,
        "horizon_fixe": {"H": HORIZON_DEFAULT, **fh},
        "barrieres": barriers,
        "best": best,
        "candidate": candidate,
        "motifs": motifs,
        "funding_integre": funding_ctx is not None,  # badge (§5.14)
    }


def compute_timeframe(
    df: pd.DataFrame, timeframe: str, daily_ref, fee_taker: float,
    funding_annualized: float | None = None,
) -> dict:
    """Calcule la case complète d'une timeframe pour son ÉTAT COURANT, dans les
    deux directions (§5). Renvoie un dict JSON-sérialisable pour la matrice."""
    from .states import state_labels
    from .couts import both_verdicts, cost_in_r

    if df is None or len(df) < HORIZON_DEFAULT + 5:
        return {"timeframe": timeframe, "n_etat": 0, "insuffisant": True}

    atr_series = atr(df)
    labels, current = state_labels(df, timeframe, daily_ref)
    mask = (labels == current)
    n_etat = int(mask.sum())
    regime, _, extension = current.partition("/")

    close_last = float(df["close"].iloc[-1])
    atr_last = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else float("nan")
    cost_r = cost_in_r(fee_taker, close_last, atr_last)

    realism = realism_block(df, timeframe, 0.5)
    costs = both_verdicts(realism.sigma_bougie)
    cost_level = costs["taker"]["level"]

    fctx = None
    if funding_annualized is not None and atr_last == atr_last and atr_last > 0:
        fctx = {"annualized": funding_annualized, "price_over_atr": close_last / atr_last}
    long_b = _direction_block(df, atr_series, mask, "long", cost_r, cost_level,
                              regime, timeframe, fctx)
    short_b = _direction_block(df, atr_series, mask, "short", cost_r, cost_level,
                               regime, timeframe, fctx)

    return {
        "timeframe": timeframe,
        "etat": current, "regime": regime, "extension": extension,
        "n_etat": n_etat,
        "close": close_last, "atr": atr_last, "cost_r": cost_r,
        "realisme": {"sigma_bougie": realism.sigma_bougie, "cvar99": realism.cvar99,
                     "k_max": realism.k_max},
        "couts": costs,
        "long": long_b, "short": short_b,
        "candidate": bool(long_b["candidate"] or short_b["candidate"]),
    }


def compute_matrix(loader, daily_ref, fee_taker: float,
                   funding_annualized: float | None = None) -> list[dict]:
    """Calcule les 11 timeframes (§4). `loader(tf)` renvoie le DataFrame OHLCV."""
    out = []
    for tf in ALL_TIMEFRAMES:
        df = loader(tf)
        out.append(compute_timeframe(df, tf, daily_ref, fee_taker, funding_annualized))
    return out


# Les 6 états possibles (§4 : Régime × Extension).
ALL_STATES = [f"{r}/{e}" for r in ("bull", "bear")
              for e in ("survendu", "neutre", "surachete")]


def compute_timeframe_all_states(
    df: pd.DataFrame, timeframe: str, daily_ref, fee_taker: float,
    funding_annualized: float | None = None,
) -> dict:
    """Tables COMPLÈTES d'une timeframe : chaque état × chaque direction (§5,
    Vue 2). Sert aussi aux décisions du worker (l'état courant peut changer
    entre deux recalculs quotidiens). Renvoie un dict JSON-sérialisable."""
    from .states import state_labels
    from .couts import both_verdicts, cost_in_r

    if df is None or len(df) < HORIZON_DEFAULT + 5:
        return {"timeframe": timeframe, "insuffisant": True, "etats": {}}

    atr_series = atr(df)
    labels, current = state_labels(df, timeframe, daily_ref)

    close_last = float(df["close"].iloc[-1])
    atr_last = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else float("nan")
    cost_r = cost_in_r(fee_taker, close_last, atr_last)

    realism = realism_block(df, timeframe, 0.5)
    costs = both_verdicts(realism.sigma_bougie)
    cost_level = costs["taker"]["level"]

    fctx = None
    if funding_annualized is not None and atr_last == atr_last and atr_last > 0:
        fctx = {"annualized": funding_annualized, "price_over_atr": close_last / atr_last}

    # Dimension volatilité v1.5 (§4) : zones du percentile 1 an de vol EWMA.
    from .states import vol_zone_labels
    zone_labels, zone_current = vol_zone_labels(df, timeframe)
    VOL_ZONES = ("basse", "moyenne", "haute")

    etats: dict[str, dict] = {}
    for state in ALL_STATES:
        mask = (labels == state)
        n_etat = int(mask.sum())
        if n_etat == 0:
            continue
        regime = state.split("/")[0]
        blk = {
            "n_etat": n_etat,
            "long": _direction_block(df, atr_series, mask, "long", cost_r,
                                     cost_level, regime, timeframe, fctx),
            "short": _direction_block(df, atr_series, mask, "short", cost_r,
                                      cost_level, regime, timeframe, fctx),
        }
        # v1.5 : ACTIVÉE case par case seulement si CHAQUE sous-case (état ×
        # zone) garde n ≥ 200 (§4). Sinon la dimension reste inactive et la
        # case parente fait foi.
        sub_masks = {z: mask & (zone_labels == z) for z in VOL_ZONES}
        sub_ns = {z: int(m.sum()) for z, m in sub_masks.items()}
        blk["vol_active"] = all(v >= N_MIN for v in sub_ns.values())
        if blk["vol_active"]:
            blk["vol_zones"] = {}
            for z in VOL_ZONES:
                blk["vol_zones"][z] = {
                    "n_etat": sub_ns[z],
                    "long": _direction_block(df, atr_series, sub_masks[z], "long",
                                             cost_r, cost_level, regime,
                                             timeframe, fctx),
                    "short": _direction_block(df, atr_series, sub_masks[z], "short",
                                              cost_r, cost_level, regime,
                                              timeframe, fctx),
                }
        else:
            blk["vol_sous_cases_n"] = sub_ns  # motif d'inactivation affichable
        etats[state] = blk

    return {
        "timeframe": timeframe,
        "etat_courant": current,
        "vol_zone_courante": zone_current,
        "close": close_last, "atr": atr_last, "cost_r": cost_r,
        "realisme": {"sigma_bougie": realism.sigma_bougie, "cvar99": realism.cvar99,
                     "k_max": realism.k_max},
        "couts": costs,
        "etats": etats,
    }


def compute_all_tables(loader, daily_ref, fee_taker: float,
                       funding_annualized: float | None = None) -> dict:
    """Tables complètes des 11 timeframes (états × directions). LOURD : réservé
    au cycle quotidien (§2.5)."""
    return {tf: compute_timeframe_all_states(loader(tf), tf, daily_ref, fee_taker,
                                             funding_annualized)
            for tf in ALL_TIMEFRAMES}
