"""Banc de vérification P2 — moteur statistique. Déterministe, SANS réseau.

Démontre :
  1. Incertitude : Wilson 95 % (valeur connue) ; Beta posterior p_prudent < moyenne.
  2. Récupération d'un p CONNU : une série construite à 70 % de hausses redonne
     p̂ = 0,70 avec l'intervalle qui le contient (test d'acceptation §8).
  3. Double barrière : tendance strictement haussière → long ~100 % gagnant.
  4. Ré-échantillonnage 1m→4h == agrégat natif (tolérance ~0, test §8 P2).
  5. Bout-en-bout : compute_matrix renvoie 11 timeframes, 2 directions chacune,
     jamais un p̂ sans n/intervalle ni une EV sans coûts.

Lancer :  python tests/test_p2.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="moteur_p2_")
os.environ["MOTEUR_DB"] = str(Path(_TMP) / "moteur.db")
os.environ["OHLCV_DIR"] = str(Path(_TMP) / "ohlcv")
os.environ["LOG_DIR"] = str(Path(_TMP) / "logs")
os.environ["BACKUP_DIR"] = str(Path(_TMP) / "backups")
os.environ["SYMBOL"] = "BTCUSDT"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from core.config import CONFIG  # noqa: E402
from core.proba_engine import (  # noqa: E402
    atr, beta_posterior, build_measure, compute_matrix, double_barrier_counts,
    fixed_horizon_counts, wilson_interval,
)
from core.data_source import MINUTE_MS, resample_1m, save_ohlcv_atomic  # noqa: E402
from core.states import daily_sma200_ref  # noqa: E402

CONFIG.ensure_dirs()
PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    mark = "✓" if cond else "✗"
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))


def klines(rows):
    return pd.DataFrame(rows, columns=["open_time", "open", "high", "low",
                                       "close", "volume", "close_time"])


def test_uncertainty():
    print("[1] Incertitude (Wilson, Beta)")
    w = wilson_interval(70, 100)
    check("Wilson centre ≈ 0.693", abs(w.center - 0.6926) < 1e-3, f"{w.center:.4f}")
    check("Wilson demi-largeur ≈ 0.088", abs(w.half - 0.0884) < 1e-3, f"{w.half:.4f}")
    check("Wilson contient 0.70", w.low < 0.70 < w.high, f"[{w.low:.3f},{w.high:.3f}]")
    post = beta_posterior(70, 100)
    check("Beta p_prudent < moyenne", post.p_prudent < post.mean,
          f"q25={post.p_prudent:.3f} < moy={post.mean:.3f}")
    check("Beta moyenne raisonnable", 0.6 < post.mean < 0.75, f"{post.mean:.3f}")


def test_known_p():
    print("[2] Récupération d'un p CONNU (70 % de hausses)")
    H = 10
    # 100 fenêtres non chevauchantes, 70 montantes / 30 descendantes.
    ups = [True] * 70 + [False] * 30
    rng_order = ups  # ordre déterministe (pas d'aléa)
    checkpoints = [100.0]
    for u in rng_order:
        checkpoints.append(checkpoints[-1] + (1.0 if u else -1.0))
    # Construit 1 bougie par pas ; close aux multiples de H = checkpoints.
    close = []
    for i in range(len(checkpoints) - 1):
        a, b = checkpoints[i], checkpoints[i + 1]
        for j in range(H):
            close.append(a + (b - a) * j / H)
    close.append(checkpoints[-1])
    s = pd.Series(close)
    k, n = fixed_horizon_counts(s, H)
    check("n = 100 fenêtres", n == 100, f"n={n}")
    check("p̂ = 0.70 exact", abs(k / n - 0.70) < 1e-9, f"{k}/{n}")
    w = wilson_interval(k, n)
    check("intervalle contient 0.70", w.low < 0.70 < w.high, f"[{w.low:.3f},{w.high:.3f}]")


def test_double_barrier():
    print("[3] Double barrière — tendance haussière → long gagnant")
    rows = []
    price = 100.0
    for i in range(300):
        ot = i * MINUTE_MS
        o = price
        c = price + 1.0            # hausse régulière
        h = c + 0.1
        low = o - 0.1             # ne descend jamais sous l'entrée précédente
        rows.append((ot, o, h, low, c, 1.0, ot + MINUTE_MS - 1))
        price = c
    df = klines(rows)
    a = atr(df)
    k, n, dur = double_barrier_counts(df, a, rr=1.0, direction="long")
    check("long presque 100 % gagnant", n > 0 and k / n > 0.95, f"{k}/{n}")
    check("durée médiane mesurée (funding §5.14)", dur == dur and dur >= 1, f"{dur} bougies")
    ks, ns, _ = double_barrier_counts(df, a, rr=1.0, direction="short")
    check("short presque 0 % gagnant", ns > 0 and ks / ns < 0.05, f"{ks}/{ns}")


def test_resample_4h():
    print("[4] Ré-échantillonnage 1m→4h == agrégat natif (tolérance ~0)")
    # 2 bougies 4h alignées = 480 bougies 1m, départ sur frontière 4h.
    start = 1_699_999_200_000  # multiple de 4h (14400 s * n)
    start -= start % (4 * 3600 * 1000)
    rows = []
    p = 100.0
    for i in range(480):
        ot = start + i * MINUTE_MS
        o = p
        c = p + (0.5 if i % 3 == 0 else -0.2)
        h = max(o, c) + 0.3
        low = min(o, c) - 0.3
        rows.append((ot, o, h, low, c, 1.0 + i, ot + MINUTE_MS - 1))
        p = c
    df1m = klines(rows)
    r4h = resample_1m(df1m, "4h")
    check("2 bougies 4h complètes", len(r4h) == 2, f"{len(r4h)}")
    first = df1m.iloc[:240]
    b = r4h.iloc[0]
    ok = (b["open"] == first["open"].iloc[0] and b["high"] == first["high"].max()
          and b["low"] == first["low"].min() and b["close"] == first["close"].iloc[-1]
          and abs(b["volume"] - first["volume"].sum()) < 1e-6)
    check("bougie 4h == agrégat exact des 240 bougies 1m", ok)


def test_measure_completeness():
    print("[5] Jamais un p̂ sans n/intervalle, ni une EV sans coûts")
    m = build_measure(120, 200, rr=1.5, cost_r=0.05)
    d = m.to_dict()
    check("n présent", d["n"] == 200)
    check("intervalle Wilson présent", "low" in d["wilson"] and "high" in d["wilson"])
    check("p_prudent présent", "p_prudent" in d["posterior"])
    check("coût pris en compte dans EV", d["ev_nette"] < d["ev_brute"],
          f"nette={d['ev_nette']:.3f} < brute={d['ev_brute']:.3f}")


def test_matrix_end_to_end():
    print("[6] Bout-en-bout : compute_matrix (30 jours synthétiques)")
    days = 30
    n = days * 1440
    start = 1_600_000_000_000
    start -= start % (24 * 3600 * 1000)
    p = 30000.0
    rows = []
    for i in range(n):
        ot = start + i * MINUTE_MS
        o = p
        c = p * (1 + (0.0004 if (i // 300) % 2 == 0 else -0.0003))
        h = max(o, c) * 1.0006
        low = min(o, c) * 0.9994
        rows.append((ot, o, h, low, c, 1.0, ot + MINUTE_MS - 1))
        p = c
    df1m = klines(rows)
    save_ohlcv_atomic(df1m, "1m")
    for tf in ["5m", "15m", "30m", "1h", "4h", "12h", "1D"]:
        save_ohlcv_atomic(resample_1m(df1m, tf), tf)
    from core.data_source import resample_from_1d, load_ohlcv
    df1d = load_ohlcv("1D")
    for tf in ["1W", "2W", "1M"]:
        save_ohlcv_atomic(resample_from_1d(df1d, tf), tf)

    daily_ref = daily_sma200_ref(load_ohlcv("1D"))
    matrix = compute_matrix(load_ohlcv, daily_ref, CONFIG.fee_taker)
    check("11 timeframes calculées", len(matrix) == 11, f"{len(matrix)}")

    # [7] Funding directionnel (§5.14) : +F short / −F long si taux positif.
    matrix_f = compute_matrix(load_ohlcv, daily_ref, CONFIG.fee_taker,
                              funding_annualized=0.10)  # +10 %/an
    s = next((tf for tf in matrix_f if not tf.get("insuffisant")
              and tf["long"]["barrieres"] and tf["long"]["barrieres"][0]["n"] > 0), None)
    check("[7] case avec funding calculée", s is not None)
    if s:
        bl = s["long"]["barrieres"][0]
        bs = s["short"]["barrieres"][0]
        check("[7] funding_r long négatif (payé)", bl["funding_r"] < 0,
              f"{bl['funding_r']:.5f}")
        check("[7] funding_r short positif (reçu)", bs["funding_r"] > 0,
              f"{bs['funding_r']:.5f}")
        check("[7] badge funding intégré", s["long"]["funding_integre"] is True)
        sans = next(tf for tf in matrix if tf["timeframe"] == s["timeframe"])
        check("[7] sans taux → F=0 + badge non intégré",
              sans["long"]["barrieres"][0]["funding_r"] == 0.0
              and sans["long"]["funding_integre"] is False)
    have_dirs = all(("long" in tf and "short" in tf) or tf.get("insuffisant") for tf in matrix)
    check("chaque case a long + short", have_dirs)
    # vérifie qu'une case type porte n, intervalle, EV+coûts
    sample = next((tf for tf in matrix if not tf.get("insuffisant")), None)
    check("case type exploitable", sample is not None)
    if sample:
        b = sample["long"]["horizon_fixe"]
        check("p̂ accompagné de n et Wilson", "n" in b and "wilson" in b)
        check("verdict de coûts présent", "taker" in sample["couts"])
        # §5.1 : les TROIS horizons mesurés, toutes timeframes.
        hz = sample["long"]["horizons"]
        check("3 horizons présents (5/10/20)", set(hz.keys()) == {"5", "10", "20"})
        check("chaque horizon complet (p̂, n, Wilson, posterior)",
              all(all(k in hz[h] for k in ("p_hat", "n", "wilson", "posterior"))
                  for h in hz))
        check("H5 a ~2× plus d'échantillons que H10",
              hz["5"]["n"] > hz["10"]["n"] * 1.5,
              f"n5={hz['5']['n']} vs n10={hz['10']['n']}")
        check("horizon_fixe == horizons['10']",
              b["p_hat"] == hz["10"]["p_hat"] and b["n"] == hz["10"]["n"])


def main():
    print(f"Répertoire de test : {_TMP}\n")
    test_uncertainty()
    test_known_p()
    test_double_barrier()
    test_resample_4h()
    test_measure_completeness()
    test_matrix_end_to_end()
    print(f"\nRésultat : {PASS} réussis, {FAIL} échoués")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
