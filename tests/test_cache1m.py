"""Banc de vérification — cache 1m segmenté par mois (sobriété I/O, §2.5, §7.2).

Déterministe, SANS réseau. Démontre :
  1. Écriture/lecture segmentées : contenu identique à l'ancien format.
  2. Append : seuls les segments touchés changent ; dédup ; frontière de mois.
  3. last_open_time et load_ohlcv_tail : lecture des derniers segments seulement.
  4. Migration depuis l'ancien fichier unique : sans perte, bascule atomique,
     idempotente.
  5. cache_stats : comptes exacts via métadonnées.

Lancer :  python tests/test_cache1m.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="moteur_c1m_")
os.environ["MOTEUR_DB"] = str(Path(_TMP) / "moteur.db")
os.environ["OHLCV_DIR"] = str(Path(_TMP) / "ohlcv")
os.environ["LOG_DIR"] = str(Path(_TMP) / "logs")
os.environ["BACKUP_DIR"] = str(Path(_TMP) / "backups")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from core.config import CONFIG  # noqa: E402
from core.data_source import (  # noqa: E402
    MINUTE_MS, append_ohlcv, cache_path, cache_stats, load_ohlcv,
    load_ohlcv_tail, last_open_time, migrate_to_segments, save_ohlcv_atomic,
)

CONFIG.ensure_dirs()
PASS, FAIL = 0, 0
SEG_DIR = CONFIG.ohlcv_dir / "BTCUSDT_1m_segments"


def check(name, cond, detail=""):
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  {'✓' if cond else '✗'} {name}" + (f" — {detail}" if detail else ""))


def make_1m(start_ms, n):
    rows = []
    p = 30_000.0
    for i in range(n):
        ot = start_ms + i * MINUTE_MS
        c = p + (5 if i % 2 == 0 else -3)
        rows.append((ot, p, max(p, c) + 2, min(p, c) - 2, c, 1.0, ot + MINUTE_MS - 1))
        p = c
    return pd.DataFrame(rows, columns=["open_time", "open", "high", "low",
                                       "close", "volume", "close_time"])


# 2 jours autour d'une frontière de mois (31 jan → 1 fév 2024, UTC).
JAN31 = 1_706_659_200_000  # 2024-01-31 00:00 UTC


def test_write_read():
    print("[1] Écriture/lecture segmentées")
    df = make_1m(JAN31, 2 * 1440)  # chevauche janvier et février
    save_ohlcv_atomic(df, "1m")
    segs = sorted(SEG_DIR.glob("*.parquet"))
    check("2 segments (2024-01, 2024-02)",
          [s.stem for s in segs] == ["2024-01", "2024-02"],
          str([s.stem for s in segs]))
    back = load_ohlcv("1m")
    check("contenu identique après relecture",
          len(back) == len(df) and (back["open_time"].values == df["open_time"].values).all()
          and (back["close"].values == df["close"].values).all())


def test_append():
    print("[2] Append ciblé + dédup + frontière de mois")
    before_jan = (SEG_DIR / "2024-01.parquet").stat().st_mtime_ns
    new = make_1m(JAN31 + 2 * 1440 * MINUTE_MS, 3)  # 3 bougies de février
    append_ohlcv(new, "1m")
    check("segment janvier NON réécrit",
          (SEG_DIR / "2024-01.parquet").stat().st_mtime_ns == before_jan)
    check("total correct", len(load_ohlcv("1m")) == 2 * 1440 + 3)
    append_ohlcv(new, "1m")  # ré-append identique
    check("dédup au ré-append", len(load_ohlcv("1m")) == 2 * 1440 + 3)


def test_tail_and_last():
    print("[3] last_open_time + tail")
    df = load_ohlcv("1m")
    check("last_open_time == dernière bougie",
          last_open_time("1m") == int(df["open_time"].iloc[-1]))
    tail = load_ohlcv_tail("1m", 100)
    check("tail : 100 dernières exactes",
          len(tail) == 100
          and (tail["open_time"].values == df["open_time"].tail(100).values).all())
    n, last = cache_stats("1m")
    check("cache_stats exacts (métadonnées)",
          n == len(df) and last == int(df["open_time"].iloc[-1]))


def test_migration():
    print("[4] Migration depuis l'ancien fichier unique")
    # Reconstruit un environnement « ancien format » : fichier unique + pas de segments.
    df = load_ohlcv("1m")
    for s in SEG_DIR.glob("*.parquet"):
        s.unlink()
    SEG_DIR.rmdir()
    df.to_parquet(cache_path("1m"), index=False)
    check("ancien format en place", cache_path("1m").exists())
    ok = migrate_to_segments("1m")
    check("migration effectuée", ok)
    check("ancien fichier supprimé (bascule atomique)", not cache_path("1m").exists())
    back = load_ohlcv("1m")
    check("aucune perte à la migration",
          len(back) == len(df) and (back["open_time"].values == df["open_time"].values).all())
    check("migration idempotente (2e appel sans effet)",
          migrate_to_segments("1m") is False)


def main():
    print(f"Répertoire de test : {_TMP}\n")
    test_write_read()
    test_append()
    test_tail_and_last()
    test_migration()
    print(f"\nRésultat : {PASS} réussis, {FAIL} échoués")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
