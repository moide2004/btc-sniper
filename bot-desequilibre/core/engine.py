"""Orchestrateur §2+§3 : relie le détecteur Fibonacci, la couche probabiliste,
le dimensionnement et le Store.

  • recompute_proba_tables() — LOURD, 1×/jour (tâche 00:10) : pour chaque actif ×
    TF × direction, rejoue l'historique et écrit le bloc §3 dans `probas`.
  • live_scan() — LÉGER, appelé par le worker : sur CHAQUE bougie d'analyse qui
    vient de se clôturer (15m→1D), cherche un setup §2 ; si valide, émet un
    TICKET annoté avec la dernière table §3 (cloche interne). Idempotent : une
    bougie n'est scannée qu'une fois (clé config_kv).

Rien n'exécute d'ordre. Aucune notification externe. UTC partout.
"""
from __future__ import annotations

import time
from typing import Optional

from .config import CONFIG
from .data_source import (
    TF_MS, load_ohlcv, load_ohlcv_tail, resample_1m,
)
from .fibonacci import FibParams, latest_setup
from .proba import annotate, build_proba
from .risk import OpenPosition, RiskBook, monthly_corr, size_position
from .store import Store

DIRECTIONS = ("long", "short")


# ===========================================================================
# Recalcul nocturne des tables probabilistes (§3)
# ===========================================================================
def recompute_proba_tables(store: Store, logger=None) -> dict:
    """Reconstruit les blocs §3 pour tous (actif, TF, direction). Renvoie un
    résumé {nb_blocs, nb_setups}. À lancer dans la tâche quotidienne."""
    params = FibParams()
    tfs = CONFIG.analysis_timeframes
    n_blocks = n_setups = 0
    for sym in CONFIG.symbols:
        df_1m = load_ohlcv(sym, "1m")
        if df_1m.empty:
            continue
        for tf in tfs:
            df_tf = resample_1m(df_1m, tf)
            if df_tf.empty:
                continue
            for direction in DIRECTIONS:
                if direction == "short" and not CONFIG.activer_shorts:
                    continue
                res = build_proba(sym, tf, direction, df_tf, df_1m, params)
                store.record_proba(sym, tf, direction, res.payload)
                n_blocks += 1
                n_setups += res.payload.get("n_setups", 0)
                if logger:
                    logger.info(f"§3 {sym} {tf} {direction} : "
                                f"{res.payload.get('n_setups', 0)} setup(s)")
        del df_1m
    store.prune_probas(keep_per_key=3)
    # Corrélation BTC/ETH (si les deux actifs présents), pour §5.4 + affichage.
    if len(CONFIG.symbols) >= 2:
        c = monthly_corr(load_ohlcv(CONFIG.symbols[0], "1m"),
                         load_ohlcv(CONFIG.symbols[1], "1m"))
        if c is not None:
            store.set_kv("corr_btc_eth", f"{c:.4f}")
    return {"n_blocks": n_blocks, "n_setups": n_setups}


# ===========================================================================
# Scan live : bougies d'analyse fraîchement clôturées → tickets
# ===========================================================================
def _last_closed_open(now_ms: int, tf: str) -> int:
    """open_time de la dernière bougie `tf` totalement clôturée avant `now_ms`."""
    step = TF_MS[tf]
    return (now_ms // step) * step - step


def _emit_ticket(store: Store, sym: str, tf: str, setup, corr: Optional[float],
                 logger=None) -> Optional[int]:
    sizing = size_position(setup.entry, setup.stop_dist, setup.direction)
    proba = store.latest_proba(sym, tf, setup.direction) or {}
    note = annotate(proba, CONFIG.rr_mult)
    book = RiskBook(corr=corr)
    fit = book.evaluate(OpenPosition(sym, setup.direction,
                                     sizing.risk_usd if sizing else 0.0))
    payload = {
        "direction": setup.direction, "rr_mult": CONFIG.rr_mult,
        "entry_ref": setup.entry, "entry_is_proxy": setup.entry_is_proxy,
        "sl": setup.sl, "tp": setup.tp(CONFIG.rr_mult), "stop_dist": setup.stop_dist,
        "amplitude": setup.amplitude, "atr": setup.atr, "fib": setup.fib,
        "signal_open_ms": setup.signal_open_ms, "signal_close_ms": setup.signal_close_ms,
        "entry_time_ms": setup.entry_time_ms,
        "sizing": None if sizing is None else {
            "risk_usd": sizing.risk_usd, "size_units": sizing.size_units,
            "notional_usd": sizing.notional_usd, "risk_pct_capital": sizing.risk_pct_capital},
        "proba": note, "budget": fit,
    }
    tid = store.emit_ticket(sym, tf, payload)
    lvl = "info" if note.get("solide") else "info"
    store.add_event("ticket", lvl,
                    f"Ticket {sym} {tf} {setup.direction.upper()} — "
                    f"{note.get('annotation')} (n={note.get('n')}, "
                    f"p̂={_fmt(note.get('p_hat'))}, EV_prud={_fmt(note.get('ev_prudent_taker'))})",
                    {"ticket_id": tid, "annotation": note.get("annotation")})
    if logger:
        logger.info(f"Ticket #{tid} {sym} {tf} {setup.direction} ({note.get('annotation')})")
    return tid


def _fmt(x) -> str:
    return "—" if x is None else f"{x:.3f}"


def live_scan(store: Store, now_ms: int, logger=None) -> int:
    """Émet un ticket par bougie d'analyse nouvellement clôturée avec setup §2.
    Renvoie le nombre de tickets émis. Robuste : une erreur par (actif,TF) est
    isolée et n'interrompt pas le reste."""
    corr_kv = store.get_kv("corr_btc_eth")
    corr = float(corr_kv) if corr_kv else None
    emitted = 0
    params = FibParams()
    need_rows = (params.fib_lookback + params.atr_period + 5)
    for sym in CONFIG.symbols:
        for tf in CONFIG.analysis_timeframes:
            try:
                lco = _last_closed_open(now_ms, tf)
                key = f"last_scan_{sym}_{tf}"
                if store.get_kv(key) == str(lco):
                    continue
                tail_1m = load_ohlcv_tail(sym, "1m", need_rows * (TF_MS[tf] // 60_000) + 5)
                if tail_1m.empty:
                    continue
                df_tf = resample_1m(tail_1m, tf)
                store.set_kv(key, str(lco))         # marqué scanné même sans setup
                if df_tf.empty or int(df_tf["open_time"].iloc[-1]) != lco:
                    continue                        # bougie pas encore disponible
                setup = latest_setup(df_tf, params)
                if setup is None:
                    continue
                if setup.direction == "short" and not CONFIG.activer_shorts:
                    continue
                _emit_ticket(store, sym, tf, setup, corr, logger)
                emitted += 1
            except Exception as e:                  # isolation par (actif, TF)
                if logger:
                    logger.warning(f"live_scan {sym} {tf} : {e!r}")
    return emitted
