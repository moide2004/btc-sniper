"""Banc de vérification P1 — socle données dual-actif. Déterministe, SANS réseau.

Démontre : Store WAL (heartbeat, âge, cloche, santé) ; cache 1m SEGMENTÉ par
mois pour BTC ET ETH (isolation entre actifs) ; continuité (trous, doublons) ;
ré-échantillonnage 1m→15m/1h cohérent ; reprise idempotente (comblage → 0 trou).

Lancer :  python tests/test_p1.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="botdes_p1_")
os.environ["MOTEUR_DB"] = str(Path(_TMP) / "moteur.db")
os.environ["OHLCV_DIR"] = str(Path(_TMP) / "ohlcv")
os.environ["LOG_DIR"] = str(Path(_TMP) / "logs")
os.environ["BACKUP_DIR"] = str(Path(_TMP) / "backups")
os.environ["SYMBOLS"] = "BTCUSDT,ETHUSDT"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from core.config import CONFIG  # noqa: E402
from core.data_source import (  # noqa: E402
    MINUTE_MS, append_ohlcv, cache_stats, count_duplicates, find_gaps,
    last_open_time, load_ohlcv, resample_1m, save_ohlcv_atomic,
)
from core.store import Store  # noqa: E402

CONFIG.ensure_dirs()
PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  {'✓' if cond else '✗'} {name}" + (f" — {detail}" if detail else ""))


def make_1m(n, start_ms=1_706_659_200_000, base=100.0):  # 2024-01-31 (frontière de mois)
    rows, p = [], base
    for i in range(n):
        ot = start_ms + i * MINUTE_MS
        c = p + (5 if i % 2 == 0 else -3)
        rows.append((ot, p, max(p, c) + 2, min(p, c) - 2, c, 1.0 + i * 0.01, ot + MINUTE_MS - 1))
        p = c
    return pd.DataFrame(rows, columns=["open_time", "open", "high", "low",
                                       "close", "volume", "close_time"])


def test_store():
    print("[1] Store SQLite WAL")
    st = Store()
    check("mode WAL actif", st.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal")
    # Mode NFS-sûr (PythonAnywhere) : DB_JOURNAL_MODE=delete appliqué.
    CONFIG.db_journal_mode = "delete"
    st2 = Store(path=Path(_TMP) / "moteur_delete.db")
    check("mode DELETE configurable (NFS)",
          st2.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete")
    st2.close()
    CONFIG.db_journal_mode = "invalide"
    st3 = Store(path=Path(_TMP) / "moteur_fallback.db")
    check("mode invalide → repli WAL",
          st3.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal")
    st3.close()
    CONFIG.db_journal_mode = "wal"
    st.write_heartbeat(3, "binance", "ok")
    check("heartbeat écrit", st.get_heartbeat()["cycle"] == 3)
    age = st.heartbeat_age_seconds()
    check("âge heartbeat calculé", age is not None and age < 5, f"{age:.2f}s")
    st.add_event("worker", "info", "Worker (re)démarré — test")
    check("événement inséré", st.unread_count() == 1)
    st.record_health("daily", duration_s=1.2, cpu_s=0.4, peak_mem_mb=42.0)
    check("santé enregistrée", True)
    st.close()


def test_dual_asset_isolation():
    print("[2] Cache 1m segmenté + ISOLATION entre actifs")
    save_ohlcv_atomic(make_1m(2 * 1440, base=60000), "BTCUSDT", "1m")   # ~2 jours, 2 mois
    save_ohlcv_atomic(make_1m(1440, base=3000), "ETHUSDT", "1m")
    seg_btc = CONFIG.ohlcv_dir / "BTCUSDT_1m_segments"
    check("BTC segmenté par mois", seg_btc.exists() and len(list(seg_btc.glob("*.parquet"))) == 2)
    nb, _ = cache_stats("BTCUSDT", "1m")
    ne, _ = cache_stats("ETHUSDT", "1m")
    check("comptes distincts par actif", nb == 2880 and ne == 1440, f"BTC={nb} ETH={ne}")
    check("prix non mélangés", abs(load_ohlcv("BTCUSDT", "1m")["close"].iloc[0] - 60005) < 10
          and abs(load_ohlcv("ETHUSDT", "1m")["close"].iloc[0] - 3005) < 10)


def test_append_continuity():
    print("[3] Append ciblé + continuité")
    before = (CONFIG.ohlcv_dir / "BTCUSDT_1m_segments" / "2024-01.parquet").stat().st_mtime_ns
    append_ohlcv(make_1m(3, start_ms=1_706_659_200_000 + 2 * 1440 * MINUTE_MS, base=60000),
                 "BTCUSDT", "1m")
    check("segment janvier NON réécrit (append ciblé)",
          (CONFIG.ohlcv_dir / "BTCUSDT_1m_segments" / "2024-01.parquet").stat().st_mtime_ns == before)
    df = load_ohlcv("BTCUSDT", "1m")
    check("dédup / continuité", count_duplicates(df) == 0 and len(df) == 2883)
    holed = pd.concat([df.iloc[:100], df.iloc[105:]], ignore_index=True)
    gaps = find_gaps(holed)
    check("trou détecté", len(gaps) == 1 and (gaps[0][1] - gaps[0][0]) // MINUTE_MS + 1 == 5)


def test_resample():
    print("[4] Ré-échantillonnage 1m→15m/1h (frontière alignée)")
    start = 1_704_067_200_000  # 2024-01-01 00:00 UTC (aligné 15m/1h/1D)
    df = make_1m(240, start_ms=start, base=100)  # 4 h
    r15 = resample_1m(df, "15m")
    first = df.iloc[:15]
    b = r15.iloc[0]
    check("barre 15m == agrégat des 15 bougies 1m",
          b["open"] == first["open"].iloc[0] and b["high"] == first["high"].max()
          and b["low"] == first["low"].min() and b["close"] == first["close"].iloc[-1])
    check("nb barres 1h complètes = 4", len(resample_1m(df, "1h")) == 4)


def test_worker_selfheal():
    print("[5bis] Worker : auto-réparation de la connexion base")
    from jobs.worker import Worker
    w = Worker.__new__(Worker)          # sans démarrer les threads
    w.store = Store()
    w.cycle, w.active_source = 1, "test"
    w._last_beat, w._beat_fails = 0.0, 0

    class DeadStore:
        def write_heartbeat(self, *a, **k):
            raise RuntimeError("disk I/O error simulé")
        def close(self):
            pass
    w.store.close()
    w.store = DeadStore()
    for _ in range(3):                  # 3 échecs consécutifs → reconnexion
        w._last_beat = 0.0
        w._beat_if_due()
    check("reconnexion déclenchée après 3 échecs",
          isinstance(w.store, Store), type(w.store).__name__)
    check("heartbeat réécrit après reconnexion",
          w.store.get_heartbeat() is not None and w._beat_fails == 0)
    w.store.close()


def test_reprise():
    print("[5] Reprise idempotente (comblage → 0 trou)")
    for s in (CONFIG.ohlcv_dir / "ETHUSDT_1m_segments").glob("*.parquet"):
        s.unlink()
    full = make_1m(60, base=3000)
    save_ohlcv_atomic(pd.concat([full.iloc[:20], full.iloc[25:]], ignore_index=True), "ETHUSDT", "1m")
    check("trou présent", len(find_gaps(load_ohlcv("ETHUSDT", "1m"))) == 1)
    append_ohlcv(full.iloc[20:25], "ETHUSDT", "1m")
    check("0 trou après comblage", len(find_gaps(load_ohlcv("ETHUSDT", "1m"))) == 0)
    append_ohlcv(full.iloc[20:25], "ETHUSDT", "1m")
    check("aucun doublon après ré-append", count_duplicates(load_ohlcv("ETHUSDT", "1m")) == 0)
    check("last_open_time correct", last_open_time("ETHUSDT", "1m") == int(full["open_time"].iloc[-1]))


def main():
    print(f"Répertoire de test : {_TMP}\n")
    test_store()
    test_dual_asset_isolation()
    test_append_continuity()
    test_resample()
    test_worker_selfheal()
    test_reprise()
    print(f"\nRésultat : {PASS} réussis, {FAIL} échoués")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
