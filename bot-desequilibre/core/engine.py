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

import json
import time
from typing import Optional

from .config import CONFIG
from .data_source import (
    TF_MS, load_ohlcv, load_ohlcv_tail, resample_1m,
)
from .fibonacci import FibParams, Setup, detect_setups, latest_setup
from .notify import send_push
from .paper import (
    advance_live_position, flux_summary, mc_drawdown_p95, new_live_position,
    simulate_flux,
)
from .proba import annotate, build_proba
from .risk import OpenPosition, RiskBook, monthly_corr, size_position
from .store import Store, utc_now_iso, utc_now_ms

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
    # Push téléphone (optionnel, amendement BRIEF 2026-07-25) — best-effort.
    send_push(
        f"Ticket {sym} {tf} {setup.direction.upper()} — {note.get('annotation')}",
        f"Entrée ≈ {setup.entry:.2f} · SL {setup.sl:.2f} · TP {setup.tp(CONFIG.rr_mult):.2f}\n"
        f"p̂={_fmt(note.get('p_hat'))} · p_prudent={_fmt(note.get('p_prudent'))} "
        f"· n={note.get('n')} · EV_prud={_fmt(note.get('ev_prudent_taker'))}",
        priority="high" if note.get("solide") else "default",
        tags="rotating_light" if note.get("solide") else "bell")
    if logger:
        logger.info(f"Ticket #{tid} {sym} {tf} {setup.direction} ({note.get('annotation')})")
    return tid


def _fmt(x) -> str:
    return "—" if x is None else f"{x:.3f}"


# ===========================================================================
# P4 — Replay HISTORIQUE par flux (résultats immédiats, base de la P5)
# ===========================================================================
def recompute_backtest(store: Store, logger=None) -> dict:
    """Rejoue TOUT l'historique en paper trading, flux par flux (actif × TF ×
    direction), pour CHAQUE profil rrMult de la grille §3 (le rr vivant est
    toujours inclus). Stocke par profil : résumés de flux + bilan go/no-go +
    agrégat portefeuille. Les champs de premier niveau restent ceux du rr
    vivant (compatibilité). À lancer dans la tâche quotidienne (après §3)."""
    params = FibParams()
    rr_live = CONFIG.rr_mult
    grid = list(dict.fromkeys(list(CONFIG.rr_grid) + [rr_live]))
    prof: dict[str, dict] = {f"{rr:.2f}": {"fluxes": [], "trades": []} for rr in grid}
    for sym in CONFIG.symbols:
        df_1m = load_ohlcv(sym, "1m")
        if df_1m.empty:
            continue
        ot = df_1m["open_time"].to_numpy(dtype="int64")
        highs = df_1m["high"].to_numpy(dtype="float64")
        lows = df_1m["low"].to_numpy(dtype="float64")
        for tf in CONFIG.analysis_timeframes:
            df_tf = resample_1m(df_1m, tf)
            if df_tf.empty:
                continue
            setups = detect_setups(df_tf, params)      # indépendants du rr
            for direction in DIRECTIONS:
                if direction == "short" and not CONFIG.activer_shorts:
                    continue
                flux_setups = [s for s in setups if s.direction == direction]
                pb = store.latest_proba(sym, tf, direction) or {}
                for rr in grid:
                    key = f"{rr:.2f}"
                    trades = simulate_flux(flux_setups, ot, highs, lows, rr)
                    summ = flux_summary(trades)
                    summ["mc_dd_p95_r"] = mc_drawdown_p95([t.r_net for t in trades])
                    summ.update({"symbol": sym, "timeframe": tf,
                                 "direction": direction, "n_setups": len(flux_setups)})
                    # Rétention + statut walk-forward de la table §3 AU MÊME rr.
                    wf = pb.get("walk_forward", {}).get(key, {})
                    summ["retention"] = wf.get("retention")
                    summ["wf_status"] = wf.get("status")
                    summ["verdict"] = _p5_verdict(summ, wf.get("retention"),
                                                  wf.get("status"))
                    prof[key]["fluxes"].append(summ)
                    prof[key]["trades"].extend(trades)
                    if logger and rr == rr_live:
                        logger.info(f"backtest {sym} {tf} {direction} : n={summ['n']} "
                                    f"PF={_fmt(summ['profit_factor'])} "
                                    f"verdict={summ['verdict']['statut']}")
        del df_1m
    profiles = {}
    for key, p in prof.items():
        p["trades"].sort(key=lambda t: (t.exit_time_ms or t.entry_time_ms))
        port = flux_summary(p["trades"])
        bilan = {"go": 0, "no-go": 0, "insuffisant": 0}
        for f in p["fluxes"]:
            bilan[f["verdict"]["statut"]] = bilan.get(f["verdict"]["statut"], 0) + 1
        profiles[key] = {"rr": float(key), "fluxes": p["fluxes"], "bilan": bilan,
                         "portfolio": {**port,
                                       "equity_usd": CONFIG.capital_usd + port["pnl_usd"]}}
    live_key = f"{rr_live:.2f}"
    result = {"generated_at": utc_now_iso(), "capital_usd": CONFIG.capital_usd,
              "rr": rr_live, "profiles": profiles, **profiles[live_key]}
    result["rr"] = rr_live
    store.set_kv("backtest", json.dumps(result))
    return {"n_fluxes": len(profiles[live_key]["fluxes"]),
            "n_trades": len(prof[live_key]["trades"]),
            "bilan": profiles[live_key]["bilan"],
            "rr_profiles": sorted(profiles.keys(), key=float)}


def _p5_verdict(summ: dict, retention: Optional[float] = None,
                wf_status: Optional[str] = None) -> dict:
    """Critère go/no-go P5 PAR FLUX (§8), 5 conditions cumulatives :
      1) n ≥ solide_min_n (sinon « insuffisant », on ne juge pas) ;
      2) PF net > 1,15 ;
      3) t ≥ 1,5 ;
      4) DD Monte-Carlo p95 × 1,25 < 30 % du capital (1 R = risk_pct du capital) ;
      5) DÉGRADATION PROGRESSIVE (walk-forward non « overfit »/« disqualifié »)
         ET RÉTENTION EV_test/EV_train ≥ 0,5 (§3).
    Un flux peut échouer et être désactivé — c'est un résultat de recherche."""
    n = summ.get("n", 0)
    pf, t = summ.get("profit_factor"), summ.get("t_stat")
    mc = summ.get("mc_dd_p95_r")
    dd_pct = None if mc is None else mc * CONFIG.risk_pct * 1.25 * 100.0
    crit_n = n >= CONFIG.solide_min_n
    crit_pf = pf is not None and pf > 1.15
    crit_t = t is not None and t >= 1.5
    crit_dd = dd_pct is not None and dd_pct < 30.0
    crit_ret = retention is not None and retention >= 0.5
    crit_degr = wf_status not in (None, "overfit", "disqualifie", "insuffisant")
    crits = {"n_ok": crit_n, "pf_ok": crit_pf, "t_ok": crit_t, "dd_ok": crit_dd,
             "ret_ok": crit_ret, "degr_ok": crit_degr,
             "dd_capital_pct": dd_pct, "retention": retention, "wf_status": wf_status}
    if not crit_n:
        return {"statut": "insuffisant", "go": False,
                "raison": f"n<{CONFIG.solide_min_n}", **crits}
    go = bool(crit_pf and crit_t and crit_dd and crit_ret and crit_degr)
    return {"statut": "go" if go else "no-go", "go": go, **crits}


# ===========================================================================
# P4 — Paper trading FORWARD (positions virtuelles live)
# ===========================================================================
def _tail_arrays(sym: str, since_ms: int):
    """Charge le 1m de `sym` couvrant [since_ms, now] (+ marge). Renvoie
    (ot, opens, highs, lows) numpy, ou None si vide."""
    minutes = max(3 * 1440, (utc_now_ms() - since_ms) // 60_000 + 10)
    minutes = min(minutes, 90 * 1440)
    df = load_ohlcv_tail(sym, "1m", int(minutes))
    if df.empty:
        return None
    return (df["open_time"].to_numpy("int64"), df["open"].to_numpy("float64"),
            df["high"].to_numpy("float64"), df["low"].to_numpy("float64"))


def _rebuild_position(tk: dict, entry_real: float) -> Optional[Setup]:
    """Reconstruit un Setup au fill RÉEL (open de la bougie d'entrée) avec la règle
    §2 (stop depuis la ligne fib stockée). Renvoie None si géométrie rejetée."""
    d, fib, atr = tk["direction"], tk.get("fib", {}), tk.get("atr", 0.0)
    sl_line = fib.get("0.786") if d == "long" else fib.get("0.236")
    if sl_line is None or atr <= 0:
        return None
    stop_raw = (entry_real - sl_line) if d == "long" else (sl_line - entry_real)
    if stop_raw <= 0 or stop_raw > CONFIG.stop_max_atr * atr:
        return None
    stop_eff = max(stop_raw, CONFIG.stop_min_atr * atr)
    sl = entry_real - stop_eff if d == "long" else entry_real + stop_eff
    return Setup(idx=-1, direction=d, signal_open_ms=tk.get("signal_open_ms", 0),
                 signal_close_ms=tk.get("signal_close_ms", 0),
                 entry_time_ms=tk["entry_time_ms"], entry=entry_real, sl=sl,
                 stop_dist=stop_eff, amplitude=tk.get("amplitude", 0.0), atr=atr,
                 fib=fib, entry_is_proxy=False)


def paper_step(store: Store, now_ms: int, logger=None) -> dict:
    """Un pas de paper trading forward : avance les positions ouvertes sur le 1m
    (BE + double barrière → clôture + journal), puis ouvre les tickets récents
    éligibles (fill réel + budget §5.4). Idempotent (une position par ticket)."""
    opened = closed = 0
    open_pos = store.list_open_positions()
    syms_needed = {p["symbol"] for p in open_pos}
    tickets = [t for t in store.list_tickets(limit=60)
               if not store.get_kv(f"paper_done_{t['id']}")]
    syms_needed |= {t["symbol"] for t in tickets}
    if not syms_needed:
        return {"opened": 0, "closed": 0}

    since = now_ms
    for p in open_pos:
        since = min(since, int(p["payload"].get("last_1m_ms", now_ms)))
    for t in tickets:
        since = min(since, int(t.get("entry_time_ms", now_ms)))
    arrays = {}
    for sym in syms_needed:
        a = _tail_arrays(sym, since)
        if a is not None:
            arrays[sym] = a

    # 1) Avancer / clôturer les positions ouvertes.
    for p in open_pos:
        a = arrays.get(p["symbol"])
        if a is None:
            continue
        ot, _op, hi, lo = a
        pos = p["payload"]
        mask = ot > pos.get("last_1m_ms", 0)
        bars = list(zip(ot[mask].tolist(), hi[mask].tolist(), lo[mask].tolist()))
        close = advance_live_position(pos, bars)
        if close is None:
            store.update_position(p["id"], pos)
        else:
            pos.update({"close": close})
            store.close_position(p["id"], pos)
            store.add_journal(p["symbol"], p["timeframe"], {
                "event": "close", "position_id": p["id"], "direction": pos["direction"],
                **close, "entry": pos["entry"], "risk_usd": pos["risk_usd"]})
            store.add_event("paper", "info",
                            f"Position {p['symbol']} {p['timeframe']} "
                            f"{pos['direction'].upper()} clôturée ({close['reason']}, "
                            f"{close['r_net']:+.2f} R, {close['pnl_usd']:+.0f}$)",
                            {"position_id": p["id"], "reason": close["reason"]})
            closed += 1

    # 2) Ouvrir les tickets éligibles au fill réel + budget §5.4.
    corr_kv = store.get_kv("corr_btc_eth")
    corr = float(corr_kv) if corr_kv else None
    book = RiskBook(corr=corr)
    live = [OpenPosition(q["symbol"], q["payload"]["direction"],
                         float(q["payload"].get("risk_usd", 0.0)))
            for q in store.list_open_positions()]
    for t in tickets:
        a = arrays.get(t["symbol"])
        if a is None:
            continue
        ot, opens, _hi, _lo = a
        idx = int((ot == t["entry_time_ms"]).argmax())
        if not (ot.size and ot[idx] == t["entry_time_ms"]):
            continue                                   # bougie d'entrée pas encore là
        setup = _rebuild_position(t, float(opens[idx]))
        if setup is None:
            store.set_kv(f"paper_done_{t['id']}", "rejected")
            continue
        sizing = size_position(setup.entry, setup.stop_dist, setup.direction)
        cand = OpenPosition(t["symbol"], setup.direction,
                            sizing.risk_usd if sizing else 0.0)
        fit = book.evaluate(cand, live)
        if not fit["allowed"]:
            store.set_kv(f"paper_done_{t['id']}", "budget")
            store.add_event("paper", "info",
                            f"Ticket {t['symbol']} {t['timeframe']} non ouvert — {fit['raison']}",
                            {"ticket_id": t["id"]})
            continue
        pos = new_live_position(t["symbol"], t["timeframe"], setup, sizing, CONFIG.rr_mult)
        pid = store.open_position(t["symbol"], t["timeframe"], pos)
        store.set_kv(f"paper_done_{t['id']}", f"open:{pid}")
        live.append(cand)
        store.add_journal(t["symbol"], t["timeframe"], {
            "event": "open", "position_id": pid, "direction": setup.direction,
            "entry": setup.entry, "sl": setup.sl, "tp": setup.tp(CONFIG.rr_mult),
            "risk_usd": pos["risk_usd"]})
        store.add_event("paper", "info",
                        f"Position OUVERTE {t['symbol']} {t['timeframe']} "
                        f"{setup.direction.upper()} @ {setup.entry:.2f} "
                        f"(SL {setup.sl:.2f}, TP {setup.tp(CONFIG.rr_mult):.2f})",
                        {"position_id": pid})
        opened += 1
    if logger and (opened or closed):
        logger.info(f"Paper trading : {opened} ouverte(s), {closed} clôturée(s)")
    return {"opened": opened, "closed": closed}


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
