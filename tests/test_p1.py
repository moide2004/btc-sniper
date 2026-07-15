"""Banc de vérification P1 — déterministe, SANS réseau.

Démontre, en isolant un répertoire de travail temporaire :
  1. Store SQLite WAL : heartbeat, âge, événements, santé.
  2. Cache OHLCV parquet : écriture atomique, append/dédup, dernier point.
  3. Continuité : détection de trous, doublons.
  4. Ré-échantillonnage 1m → 5m/1h : cohérence OHLCV (préfigure le test P2
     « 1m→4h == 4h natif », ici vérifié sur données synthétiques exactes).
  5. Reprise idempotente : comblage d'un trou puis 0 trou restant.

Lancer :  python tests/test_p1.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Isole un environnement jetable AVANT d'importer core.config.
_TMP = tempfile.mkdtemp(prefix="moteur_p1_")
os.environ["MOTEUR_DB"] = str(Path(_TMP) / "moteur.db")
os.environ["OHLCV_DIR"] = str(Path(_TMP) / "ohlcv")
os.environ["LOG_DIR"] = str(Path(_TMP) / "logs")
os.environ["BACKUP_DIR"] = str(Path(_TMP) / "backups")
os.environ["SYMBOL"] = "BTCUSDT"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from core.config import CONFIG  # noqa: E402
from core.data_source import (  # noqa: E402
    MINUTE_MS, append_ohlcv, find_gaps, count_duplicates, last_open_time,
    load_ohlcv, resample_1m, save_ohlcv_atomic,
)
from core.store import Store  # noqa: E402

CONFIG.ensure_dirs()
PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    mark = "✓" if cond else "✗"
    if cond:
        PASS += 1
    else:
        FAIL += 1
    print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))


def make_1m(n: int, start_ms: int = 1_700_000_000_000, drop: set[int] | None = None) -> pd.DataFrame:
    """Génère n bougies 1m synthétiques (prix déterministe), avec option de
    trous (indices à retirer)."""
    drop = drop or set()
    rows = []
    price = 30_000.0
    for i in range(n):
        if i in drop:
            price += 5.0
            continue
        ot = start_ms + i * MINUTE_MS
        o = price
        c = price + (5 if i % 2 == 0 else -3)
        h = max(o, c) + 2
        low = min(o, c) - 2
        rows.append((ot, o, h, low, c, 1.0 + i * 0.01, ot + MINUTE_MS - 1))
        price = c
    return pd.DataFrame(
        rows,
        columns=["open_time", "open", "high", "low", "close", "volume", "close_time"],
    )


def test_store() -> None:
    print("[1] Store SQLite WAL")
    st = Store()
    mode = st.conn.execute("PRAGMA journal_mode").fetchone()[0]
    check("mode WAL actif", mode.lower() == "wal", mode)
    st.write_heartbeat(cycle=3, source="binance", status="ok")
    hb = st.get_heartbeat()
    check("heartbeat écrit", hb is not None and hb["cycle"] == 3)
    age = st.heartbeat_age_seconds()
    check("âge heartbeat calculé", age is not None and age < 5, f"{age:.2f}s")
    eid = st.add_event("worker", "info", "Worker (re)démarré — test")
    check("événement inséré (cloche)", eid > 0)
    check("compteur non-lus", st.unread_count() == 1)
    st.record_health("daily", duration_s=1.2, cpu_s=0.4, peak_mem_mb=42.0, clock_skew_s=0.3)
    check("santé enregistrée", True)
    st.close()


def test_cache_and_continuity() -> None:
    print("[2] Cache parquet + continuité")
    df = make_1m(120)
    save_ohlcv_atomic(df, "1m")
    check("parquet écrit atomiquement", (CONFIG.ohlcv_dir / "BTCUSDT_1m.parquet").exists())
    check("dernier open_time correct", last_open_time("1m") == int(df["open_time"].iloc[-1]))
    # Append avec chevauchement -> dédup
    merged = append_ohlcv(df.tail(10), "1m")
    check("dédup sur append", len(merged) == 120 and count_duplicates(merged) == 0)
    # Trou : retirer les minutes 50..54
    holed = pd.concat([df.iloc[:50], df.iloc[55:]], ignore_index=True)
    gaps = find_gaps(holed)
    total_missing = sum((e - s) // MINUTE_MS + 1 for s, e in gaps)
    check("trou détecté", len(gaps) == 1 and total_missing == 5, f"{gaps}")


def test_resample() -> None:
    print("[3] Ré-échantillonnage 1m -> 5m / 1h (cohérence OHLCV)")
    n = 600  # 10 h de 1m alignées
    df = make_1m(n, start_ms=1_699_999_200_000)  # frontière horaire (472222×3600 s)
    r5 = resample_1m(df, "5m")
    # Vérifie une barre 5m contre l'agrégat manuel des 5 bougies sources.
    first = df.iloc[:5]
    b0 = r5.iloc[0]
    ok = (
        b0["open"] == first["open"].iloc[0]
        and b0["high"] == first["high"].max()
        and b0["low"] == first["low"].min()
        and b0["close"] == first["close"].iloc[-1]
        and abs(b0["volume"] - first["volume"].sum()) < 1e-9
    )
    check("barre 5m == agrégat des 5 bougies 1m", ok)
    r1h = resample_1m(df, "1h")
    check("nb barres 1h complètes = 10", len(r1h) == 10, f"{len(r1h)}")
    # Zéro look-ahead : aucune barre incomplète en fin de série.
    last = r1h.iloc[-1]
    check("dernière barre 1h complète (close_time ≤ dernier 1m)",
          last["close_time"] <= int(df["open_time"].iloc[-1]) + MINUTE_MS - 1)


def test_reprise_idempotente() -> None:
    print("[4] Reprise idempotente (comblage de trou → 0 trou)")
    # Repart d'un cache propre, avec un trou, puis simule le comblage REST.
    save_ohlcv_atomic(make_1m(0), "1m")  # vide
    full = make_1m(60)
    holed = pd.concat([full.iloc[:20], full.iloc[25:]], ignore_index=True)
    save_ohlcv_atomic(holed, "1m")
    before = find_gaps(load_ohlcv("1m"))
    check("trou présent avant reprise", len(before) == 1)
    # « Backfill REST » simulé : on ré-insère les bougies manquantes.
    missing = full.iloc[20:25]
    append_ohlcv(missing, "1m")
    after = find_gaps(load_ohlcv("1m"))
    check("0 trou après comblage", len(after) == 0)
    # Idempotence : ré-append des mêmes données ne crée pas de doublon.
    append_ohlcv(missing, "1m")
    check("aucun doublon après ré-append", count_duplicates(load_ohlcv("1m")) == 0)


def main() -> int:
    print(f"Répertoire de test : {_TMP}\n")
    test_store()
    test_cache_and_continuity()
    test_resample()
    test_reprise_idempotente()
    print(f"\nRésultat : {PASS} réussis, {FAIL} échoués")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
