"""Tâche planifiée quotidienne (00:10 UTC) — recalcul LOURD 1×/jour (§4.6).

P1 : ré-échantillonnage 1m → toutes les timeframes ≥ 15m pour CHAQUE actif,
contrôle de continuité + comblage, budget ressources (§4.6), dérive d'horloge,
sauvegarde cohérente de moteur.db (rétention 14 j). Les tables Fibonacci +
probas + walk-forward (§2, §3) seront branchées ici en P2.
"""
from __future__ import annotations

import resource
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import CONFIG  # noqa: E402
from core.data_source import (  # noqa: E402
    DataSource, count_duplicates, find_gaps, load_ohlcv, resample_1m,
    save_ohlcv_atomic,
)
from core.engine import (  # noqa: E402
    recompute_backtest, recompute_backtest2, recompute_proba_tables,
)
from core.logging_setup import get_logger  # noqa: E402
from core.store import Store  # noqa: E402

log = get_logger("daily")
RESAMPLE_TF = ["15m", "30m", "1h", "4h", "12h", "1D"]


def rebuild_timeframes() -> dict[str, dict[str, int]]:
    counts = {}
    for sym in CONFIG.symbols:
        df_1m = load_ohlcv(sym, "1m")
        c = {"1m": len(df_1m)}
        if not df_1m.empty:
            for tf in RESAMPLE_TF:
                agg = resample_1m(df_1m, tf)
                save_ohlcv_atomic(agg, sym, tf)
                c[tf] = len(agg)
                del agg
        counts[sym] = c
        log.info(f"Ré-échantillonnage {sym} : " + ", ".join(f"{k}={v}" for k, v in c.items()))
        del df_1m
    return counts


def check_clock_skew(ds: DataSource, store: Store):
    server_ms = ds.server_time_ms()
    if server_ms is None:
        return None
    skew_s = (int(datetime.now(timezone.utc).timestamp() * 1000) - server_ms) / 1000.0
    if abs(skew_s) > 5:
        store.add_event("data_incident", "warning",
                        f"Dérive d'horloge {skew_s:+.1f}s vs exchange (> 5s)",
                        {"skew_s": skew_s})
    return skew_s


def backup_db(store_path: Path):
    if not store_path.exists():
        log.warning("moteur.db absent : pas de sauvegarde")
        return None
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest_dir = CONFIG.backup_dir / day
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "moteur.db"
    src = sqlite3.connect(store_path)
    try:
        try:
            ok = src.execute("PRAGMA quick_check;").fetchone()[0] == "ok"
        except sqlite3.DatabaseError:
            ok = False
        if not ok:
            log.error("Base corrompue : sauvegarde du jour NON écrasée (README restauration)")
            return None
        with sqlite3.connect(dest) as bck:
            src.backup(bck)
    finally:
        src.close()
    log.info(f"Sauvegarde : {dest}")
    cutoff = time.time() - 14 * 86400
    for d in CONFIG.backup_dir.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
    return dest


def main() -> None:
    CONFIG.ensure_dirs()
    t0 = time.time()
    ru0 = resource.getrusage(resource.RUSAGE_SELF)
    cpu0 = ru0.ru_utime + ru0.ru_stime
    store = Store()
    ds = DataSource(store=store, logger=log)
    log.info("=== Cycle quotidien : début ===")

    for sym in CONFIG.symbols:
        filled = ds.fill_gaps(sym)
        if filled:
            log.info(f"Trous comblés {sym} avant recalcul : {filled}")
    counts = rebuild_timeframes()

    try:                                        # §2/§3 : tables probabilistes
        summ = recompute_proba_tables(store, logger=log)
        store.prune_tickets(120)
        log.info(f"Tables §3 recalculées : {summ['n_blocks']} bloc(s), "
                 f"{summ['n_setups']} setup(s) historiques")
        store.add_event("worker", "info",
                        f"Matrice Fibonacci recalculée ({summ['n_blocks']} blocs)",
                        summ)
    except Exception as e:
        log.error(f"Recalcul §3 en échec (poursuite) : {e!r}")
        store.add_event("performance", "warning", f"Recalcul §3 échoué : {e!r}")

    try:                                        # §8 P4 : replay historique par flux
        bt = recompute_backtest(store, logger=log)
        log.info(f"Backtest recalculé : {bt['n_fluxes']} flux, {bt['n_trades']} trades")
        store.add_event("worker", "info",
                        f"Backtest par flux recalculé ({bt['n_fluxes']} flux, "
                        f"{bt['n_trades']} trades)", bt)
    except Exception as e:
        log.error(f"Backtest en échec (poursuite) : {e!r}")
        store.add_event("performance", "warning", f"Backtest échoué : {e!r}")

    try:                                        # BOT 2 (laboratoire trend-pullback)
        b2 = recompute_backtest2(store, logger=log)
        log.info(f"Bot 2 recalculé : {b2['n_fluxes']} flux, {b2['n_trades']} trades, "
                 f"bilan {b2['bilan']}")
        store.add_event("worker", "info",
                        f"Bot 2 (pullback) recalculé ({b2['n_trades']} trades)", b2)
    except Exception as e:
        log.error(f"Bot 2 en échec (poursuite) : {e!r}")
        store.add_event("performance", "warning", f"Bot 2 échoué : {e!r}")

    gaps = {sym: len(find_gaps(load_ohlcv(sym, "1m"))) for sym in CONFIG.symbols}
    dups = {sym: count_duplicates(load_ohlcv(sym, "1m")) for sym in CONFIG.symbols}
    skew = check_clock_skew(ds, store)

    duration = time.time() - t0
    ru = resource.getrusage(resource.RUSAGE_SELF)
    cpu_s = (ru.ru_utime + ru.ru_stime) - cpu0
    store.record_health("daily", duration_s=duration, cpu_s=cpu_s,
                        peak_mem_mb=ru.ru_maxrss / 1024.0, clock_skew_s=skew,
                        note=f"gaps={gaps} dups={dups}")
    log.info(f"Ressources : durée={duration:.1f}s cpu={cpu_s:.1f}s "
             f"pic_mem={ru.ru_maxrss/1024:.0f}Mo skew={skew}")
    if duration > 15 * 60:
        store.add_event("performance", "warning",
                        f"Cycle quotidien lent : {duration/60:.1f} min (> 15 min)",
                        {"duration_s": duration})
    backup_db(CONFIG.db_path)
    store.add_event("worker", "info", f"Cycle quotidien terminé ({duration:.0f}s)",
                    {"counts": {s: c.get("1m", 0) for s, c in counts.items()}})
    log.info("=== Cycle quotidien : fin ===")
    store.close()


if __name__ == "__main__":
    main()
