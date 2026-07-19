"""Walk-forward automatique (§5.12) — contrôle qualité train/test.

Chaque nuit : tables sur historique − 12 mois (train), testées sur les 12 mois
exclus (test). Rétention = EV_test/EV_train par case :
  ≥ 0,5   → « sain »
  0,2–0,5 → « fragile »
  < 0,2   → badge « overfit ? », case DISQUALIFIÉE des candidates.
Les tables SERVIES restent celles de l'historique complet.

Choix d'ingénierie documentés (aucun seuil modifié, §9) :
  * la rétention est calculée sur l'EV nette au p̂ (pas au p_prudent : le
    rétrécissement bayésien dépend de n et biaiserait le ratio train/test) ;
  * le RR évalué en test est le meilleur RR retenu en train (pas de re-choix
    en test — zéro fuite d'information) ;
  * rétention définie seulement si EV_train > 0 (sinon la case n'était de
    toute façon pas candidate → badge « n/a »).
"""
from __future__ import annotations

from typing import Callable

import pandas as pd

from .proba_engine import ALL_TIMEFRAMES, compute_timeframe_all_states

MS_12_MONTHS = 365 * 24 * 3600 * 1000


def _badge(retention: float | None) -> str:
    if retention is None:
        return "n/a"
    if retention >= 0.5:
        return "sain"
    if retention >= 0.2:
        return "fragile"
    return "overfit"


def _best_train_case(block: dict) -> dict | None:
    """Meilleur RR retenu en train pour une direction (celui de `best`)."""
    return block.get("best")


def _ev_of_rr(block: dict, rr: float) -> float | None:
    for b in block.get("barrieres", []):
        if b["rr"] == rr and b["n"] > 0:
            return b["ev_nette"]
    return None


def walkforward(
    loader: Callable[[str], pd.DataFrame], daily_ref_fn, fee_taker: float,
) -> dict:
    """Calcule la rétention par case (timeframe × état × direction).

    `loader(tf)` renvoie l'OHLCV complet ; `daily_ref_fn(df_1d)` construit la
    référence de régime à partir d'un 1D TRONQUÉ (zéro look-ahead : la
    référence train n'utilise que le train).
    """
    df_1d_full = loader("1D")
    if df_1d_full is None or df_1d_full.empty:
        return {"cases": [], "note": "1D indisponible"}
    cutoff = int(df_1d_full["close_time"].iloc[-1]) - MS_12_MONTHS

    cases = []
    for tf in ALL_TIMEFRAMES:
        df = loader(tf)
        if df is None or df.empty:
            continue
        train = df[df["close_time"] <= cutoff].reset_index(drop=True)
        test = df[df["close_time"] > cutoff].reset_index(drop=True)
        if len(train) < 30 or len(test) < 10:
            continue
        d1d_train = df_1d_full[df_1d_full["close_time"] <= cutoff].reset_index(drop=True)
        ref_train = daily_ref_fn(d1d_train)
        ref_test = daily_ref_fn(df_1d_full)  # le test peut voir tout le passé

        t_train = compute_timeframe_all_states(train, tf, ref_train, fee_taker)
        t_test = compute_timeframe_all_states(test, tf, ref_test, fee_taker)

        for state, blk_train in t_train.get("etats", {}).items():
            blk_test = t_test.get("etats", {}).get(state)
            for direction in ("long", "short"):
                bt = _best_train_case(blk_train[direction])
                if bt is None:
                    continue
                ev_train = bt["ev_nette"]
                ev_test = (_ev_of_rr(blk_test[direction], bt["rr"])
                           if blk_test else None)
                if ev_train is None or ev_train <= 0 or ev_test is None:
                    retention = None
                else:
                    retention = ev_test / ev_train
                cases.append({
                    "timeframe": tf, "etat": state, "direction": direction,
                    "rr": bt["rr"], "ev_train": ev_train, "ev_test": ev_test,
                    "n_train": bt["n"],
                    "retention": retention, "badge": _badge(retention),
                })
    return {"cases": cases, "cutoff_ms": cutoff}


def disqualified_set(wf: dict) -> set[tuple[str, str, str]]:
    """Ensemble des cases (tf, état, direction) marquées « overfit » —
    disqualifiées des candidates (§5.12)."""
    return {(c["timeframe"], c["etat"], c["direction"])
            for c in wf.get("cases", []) if c["badge"] == "overfit"}
