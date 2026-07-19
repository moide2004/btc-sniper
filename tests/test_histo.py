"""Banc de vérification — historique profond natif (§2.3 « avant : natif »).

Déterministe, SANS réseau. Démontre :
  1. combine_native_recent : le natif ne vit QUE avant le 1m (vérité de prix),
     ordre et dédup corrects, aucun chevauchement.
  2. resample src_ms : 1h→4h == 1m→4h (agrégation par partition exacte).
  3. Absence de natif → le système fonctionne comme avant (repli propre).

Lancer :  python tests/test_histo.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="moteur_histo_")
os.environ["MOTEUR_DB"] = str(Path(_TMP) / "moteur.db")
os.environ["OHLCV_DIR"] = str(Path(_TMP) / "ohlcv")
os.environ["LOG_DIR"] = str(Path(_TMP) / "logs")
os.environ["BACKUP_DIR"] = str(Path(_TMP) / "backups")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from core.config import CONFIG  # noqa: E402
from core.data_source import (  # noqa: E402
    MINUTE_MS, TF_MS, combine_native_recent, load_native, resample_1m,
)

CONFIG.ensure_dirs()
PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  {'✓' if cond else '✗'} {name}" + (f" — {detail}" if detail else ""))


def bars(start_ms, n, tf_ms, price0=100.0):
    rows = []
    p = price0
    for i in range(n):
        ot = start_ms + i * tf_ms
        c = p + (1 if i % 2 == 0 else -0.4)
        rows.append((ot, p, max(p, c) + 0.2, min(p, c) - 0.2, c, 1.0,
                     ot + tf_ms - 1))
        p = c
    return pd.DataFrame(rows, columns=["open_time", "open", "high", "low",
                                       "close", "volume", "close_time"])


def test_combine():
    print("[1] Raccord natif + récent")
    H = TF_MS["1h"]
    t0 = 1_500_000_000_000 - (1_500_000_000_000 % 86_400_000)
    native = bars(t0, 100, H)                       # 100 h de natif
    recent = bars(t0 + 90 * H, 50, H, price0=200)   # recouvre les 10 dernières
    out = combine_native_recent(native, recent)
    check("longueur = 90 natif + 50 récent", len(out) == 140, f"{len(out)}")
    check("aucun doublon d'open_time", out["open_time"].is_unique)
    check("strictement croissant", out["open_time"].is_monotonic_increasing)
    junction = out[out["open_time"] == t0 + 90 * H]
    check("à la jonction, le RÉCENT (1m) fait foi",
          len(junction) == 1 and float(junction["open"].iloc[0]) == 200.0)
    check("natif seul si récent vide",
          len(combine_native_recent(native, native.iloc[0:0])) == 100)
    check("récent seul si natif vide",
          len(combine_native_recent(native.iloc[0:0], recent)) == 50)


def test_src_ms():
    print("[2] Agrégation par partition : 1m→4h == (1m→1h)→4h")
    t0 = 1_600_000_000_000 - (1_600_000_000_000 % 86_400_000)
    df_1m = bars(t0, 2 * 1440, MINUTE_MS)  # 2 jours de 1m
    direct = resample_1m(df_1m, "4h")
    via_1h = resample_1m(resample_1m(df_1m, "1h"), "4h", src_ms=TF_MS["1h"])
    same = (len(direct) == len(via_1h)
            and (direct["open"] == via_1h["open"]).all()
            and (direct["high"] == via_1h["high"]).all()
            and (direct["low"] == via_1h["low"]).all()
            and (direct["close"] == via_1h["close"]).all()
            and (abs(direct["volume"] - via_1h["volume"]) < 1e-9).all())
    check("OHLCV identiques (tolérance ~0)", same,
          f"{len(direct)} vs {len(via_1h)} barres")
    # Sans src_ms, le contrôle de complétude sous-estimerait la couverture 1h :
    naive = resample_1m(resample_1m(df_1m, "1h"), "4h")
    check("src_ms indispensable (sinon dernière barre 4h perdue)",
          len(naive) < len(via_1h), f"{len(naive)} < {len(via_1h)}")


def test_fallback():
    print("[3] Sans cache natif → repli propre")
    check("load_native vide (fichier absent)", load_native("1h").empty)
    rec = bars(1_600_000_000_000, 10, TF_MS["1h"])
    out = combine_native_recent(load_native("1h"), rec)
    check("combinaison == récent seul", len(out) == 10)


def main():
    print(f"Répertoire de test : {_TMP}\n")
    test_combine()
    test_src_ms()
    test_fallback()
    print(f"\nRésultat : {PASS} réussis, {FAIL} échoués")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
