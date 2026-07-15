"""Tâche planifiée quotidienne (00:10 UTC) — recalcul LOURD 1×/jour (§2.5).

P1 (socle) réalise ici :
  * ré-échantillonnage 1m → toutes les timeframes ≤ 1D + agrégation 1W/2W/1M
    depuis le 1D (§2.3), caches parquet ;
  * contrôle de continuité + comblage des trous (§7.4) ;
  * mesure et journalisation du budget ressources : durée, CPU, pic mémoire
    (§7.6) — événement « performance » si durée > 15 min ;
  * vérification de dérive d'horloge contre l'exchange (§7.9) ;
  * sauvegarde cohérente de moteur.db + rétention 14 j (§7.7).

Les tables statistiques et le walk-forward (§5.11–5.12) seront branchés ici
en P2, sans toucher à cette ossature (§9).
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
    DataSource, count_duplicates, find_gaps, load_ohlcv,
    resample_1m, save_ohlcv_atomic, TF_MS,
)
from core.logging_setup import get_logger  # noqa: E402
from core.store import Store  # noqa: E402

log = get_logger("daily")

# Agrégations natives (≤1D depuis 1m ; 1W/2W/1M depuis 1D — §2.3).
RESAMPLE_FROM_1M = ["5m", "15m", "30m", "1h", "4h", "12h", "1D"]


def rebuild_timeframes() -> dict[str, int]:
    """Reconstruit les caches ≤ 1D depuis le 1m. Retourne le nb de bougies/TF."""
    df_1m = load_ohlcv("1m")
    counts = {"1m": len(df_1m)}
    if df_1m.empty:
        log.warning("Cache 1m vide : rien à ré-échantillonner")
        return counts
    for tf in RESAMPLE_FROM_1M:
        agg = resample_1m(df_1m, tf)
        save_ohlcv_atomic(agg, tf)
        counts[tf] = len(agg)
        # Libération mémoire : dataframe intermédiaire non conservé (§2.5).
        del agg
    log.info("Ré-échantillonnage : " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return counts


def check_clock_skew(ds: DataSource, store: Store) -> float | None:
    """Dérive d'horloge vs horodatage serveur exchange (§7.9)."""
    server_ms = ds.server_time_ms()
    if server_ms is None:
        return None
    local_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    skew_s = (local_ms - server_ms) / 1000.0
    if abs(skew_s) > 5:
        store.add_event("data_incident", "warning",
                        f"Dérive d'horloge {skew_s:+.1f}s vs exchange (> 5s)",
                        {"skew_s": skew_s})
    return skew_s


def backup_db(store_path: Path) -> Path | None:
    """Instantané cohérent de moteur.db (API backup SQLite) + rétention 14 j (§7.7)."""
    if not store_path.exists():
        log.warning("moteur.db absent : pas de sauvegarde")
        return None
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest_dir = CONFIG.backup_dir / day
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "moteur.db"
    src = sqlite3.connect(store_path)
    try:
        with sqlite3.connect(dest) as bck:
            src.backup(bck)  # instantané cohérent, y compris WAL
    finally:
        src.close()
    log.info(f"Sauvegarde : {dest}")

    # Rétention 14 jours.
    cutoff = time.time() - 14 * 86400
    for d in CONFIG.backup_dir.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            log.info(f"Sauvegarde purgée : {d.name}")
    return dest


def main() -> None:
    CONFIG.ensure_dirs()
    t0 = time.time()
    cpu0 = resource.getrusage(resource.RUSAGE_SELF).ru_utime + \
        resource.getrusage(resource.RUSAGE_SELF).ru_stime
    store = Store()
    ds = DataSource(store=store, logger=log)

    log.info("=== Cycle quotidien : début ===")
    filled = ds.fill_gaps()
    if filled:
        log.info(f"Trous comblés avant recalcul : {filled}")

    counts = rebuild_timeframes()
    df_1m = load_ohlcv("1m")
    gaps = len(find_gaps(df_1m))
    dups = count_duplicates(df_1m)
    skew = check_clock_skew(ds, store)

    duration = time.time() - t0
    ru = resource.getrusage(resource.RUSAGE_SELF)
    cpu_s = (ru.ru_utime + ru.ru_stime) - cpu0
    # ru_maxrss : Ko sous Linux.
    peak_mem_mb = ru.ru_maxrss / 1024.0

    store.record_health("daily", duration_s=duration, cpu_s=cpu_s,
                        peak_mem_mb=peak_mem_mb, clock_skew_s=skew,
                        note=f"gaps={gaps} dups={dups} bars_1m={counts.get('1m', 0)}")
    log.info(f"Ressources : durée={duration:.1f}s cpu={cpu_s:.1f}s "
             f"pic_mem={peak_mem_mb:.0f}Mo skew={skew}")

    if duration > 15 * 60:  # §7.6
        store.add_event("performance", "warning",
                        f"Cycle quotidien lent : {duration/60:.1f} min (> 15 min)",
                        {"duration_s": duration})

    backup_db(CONFIG.db_path)
    store.add_event("worker", "info",
                    f"Cycle quotidien terminé ({duration:.0f}s)",
                    {"bars_1m": counts.get("1m", 0), "gaps": gaps})
    log.info("=== Cycle quotidien : fin ===")
    store.close()


if __name__ == "__main__":
    main()
