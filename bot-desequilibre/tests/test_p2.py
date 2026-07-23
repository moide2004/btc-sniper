"""Banc de vérification P2 — moteur Fibonacci (§2) + couche probabiliste (§3).
Déterministe, SANS réseau.

Démontre : géométrie Fibonacci (orientation imposée par le SL) ; détection de
setups §2 (filtres, rejet stop large, plancher) ; double barrière sur 1m (TP
avant SL, ex æquo → perte) ; Wilson / Beta q25 / EV / réalisme ; dimensionnement
(§2.6) et budget partagé (§5.4) ; Store probas/tickets ; orchestrateur (recalcul
§3 + scan live idempotent).

Lancer :  python tests/test_p2.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="botdes_p2_")
os.environ["MOTEUR_DB"] = str(Path(_TMP) / "moteur.db")
os.environ["OHLCV_DIR"] = str(Path(_TMP) / "ohlcv")
os.environ["LOG_DIR"] = str(Path(_TMP) / "logs")
os.environ["BACKUP_DIR"] = str(Path(_TMP) / "backups")
os.environ["SYMBOLS"] = "BTCUSDT"
os.environ["ANALYSIS_TIMEFRAMES"] = "15m"
os.environ["FIB_LOOKBACK"] = "5"
os.environ["ATR_PERIOD"] = "5"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from core.config import CONFIG  # noqa: E402
from core.data_source import resample_1m, save_ohlcv_atomic  # noqa: E402
from core.engine import live_scan, recompute_proba_tables  # noqa: E402
from core.fibonacci import FibParams, detect_setups, latest_setup  # noqa: E402
from core.indicators import atr_wilder, fib_levels  # noqa: E402
from core.proba import (  # noqa: E402
    _resolve_one, annotate, beta_prudent, build_proba, cvar99,
    max_losing_streak, wilson_interval,
)
from core.risk import OpenPosition, RiskBook, monthly_corr, size_position  # noqa: E402
from core.store import Store  # noqa: E402

CONFIG.ensure_dirs()
COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time"]
PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  {'✓' if cond else '✗'} {name}" + (f" — {detail}" if detail else ""))


def make_1m(n_bars, start=1_704_067_200_000, base0=100.0, drift=0.3):
    """1m synthétique : chaque bougie 15m a une grande amplitude intra (atr élevé
    ⇒ stop non rejeté) et des clôtures qui montent lentement (cassures LONG,
    issues TP gagnantes). 15 minutes par bougie 15m."""
    rows = []
    for b in range(n_bars):
        base = base0 + drift * b
        for m in range(15):
            i = b * 15 + m
            ot = start + i * 60_000
            o = c = base + 2.5
            h = l = base + 2.5
            if m == 0:
                l = base - 2.0            # mèche basse
            if m == 1:
                h = base + 3.0            # mèche haute (nouveau plus-haut)
            rows.append((ot, o, h, l, c, 1.0, ot + 59_999))
    return pd.DataFrame(rows, columns=COLS)


def test_indicators():
    print("[1] Indicateurs : ATR de Wilder + orientation Fibonacci")
    lv = fib_levels(110.0, 100.0)   # H=110, L=100, R=10
    check("50 % = milieu", abs(lv["0.500"] - 105.0) < 1e-9, f"{lv['0.500']}")
    check("23,6 % proche du HAUT > 78,6 % proche du BAS",
          lv["0.236"] > lv["0.500"] > lv["0.786"],
          f"{lv['0.236']:.2f} > {lv['0.500']:.2f} > {lv['0.786']:.2f}")
    df = make_1m(3)
    atr = atr_wilder(df, 5)
    check("ATR défini après période, positif",
          np.isfinite(atr.iloc[-1]) and atr.iloc[-1] > 0, f"{atr.iloc[-1]:.3f}")


def test_detect():
    print("[2] Détection de setups §2 (filtres, SL, plancher)")
    df_tf = resample_1m(make_1m(60), "15m")
    setups = detect_setups(df_tf)
    longs = [s for s in setups if s.direction == "long"]
    check("au moins un setup LONG détecté", len(longs) >= 1, f"{len(longs)} long(s)")
    ok_geom = all(s.sl < s.entry and s.tp(1.5) > s.entry for s in longs)
    check("géométrie long : SL < entrée < TP", ok_geom)
    ok_bounds = all(CONFIG.stop_min_atr * s.atr - 1e-6 <= s.stop_dist
                    <= CONFIG.stop_max_atr * s.atr + 1e-6 for s in longs)
    check("stopDist entre plancher et plafond ATR", ok_bounds)
    ok_amp = all(s.amplitude >= CONFIG.min_amp_atr * s.atr - 1e-6 for s in longs)
    check("amplitude ≥ minAmpATR·ATR (filtre)", ok_amp)
    check("aucun short dans une tendance haussière",
          all(s.direction == "long" for s in setups))
    ls = latest_setup(df_tf)
    check("latest_setup renvoie un long en tendance", ls is not None and ls.direction == "long")


def test_double_barrier():
    print("[3] Double barrière sur 1m (TP avant SL ; ex æquo → perte)")
    highs = np.array([1.0, 2.0, 10.0, 1.0]); lows = np.array([1.0, 0.5, 1.0, 1.0])
    check("TP touché avant SL → gain (1)",
          _resolve_one(0, tp=5.0, sl=0.4, direction="long", highs=highs, lows=lows) == 1)
    check("ex æquo même bougie → perte (0)",
          _resolve_one(0, tp=10.0, sl=0.0, direction="long",
                       highs=np.array([10.0]), lows=np.array([0.0])) == 0)
    check("aucune barrière → censuré (None)",
          _resolve_one(0, tp=5.0, sl=0.0, direction="long",
                       highs=np.array([1.0, 1.0]), lows=np.array([1.0, 1.0])) is None)


def test_stats():
    print("[4] Wilson · Beta q25 · réalisme")
    lo, hi = wilson_interval(50, 100)
    check("Wilson encadre p̂=0,5", 0 < lo < 0.5 < hi < 1, f"[{lo:.3f}, {hi:.3f}]")
    check("Wilson n=0 → [0,1]", wilson_interval(0, 0) == (0.0, 1.0))
    pp = beta_prudent(90, 100)
    check("Beta q25 prudent < p̂ élevé", pp < 0.9, f"{pp:.3f}")
    check("série de pertes max", max_losing_streak([0, 0, 1, 0, 0, 0, 1]) == 3)
    check("CVaR99 négatif sur pertes",
          cvar99(np.array([1.5, 1.5, 1.5, -1.0, 1.5])) < 0)


def test_build_proba():
    print("[5] Bloc §3 complet (actif × TF × direction)")
    df_1m = make_1m(80)
    df_tf = resample_1m(df_1m, "15m")
    res = build_proba("BTCUSDT", "15m", "long", df_tf, df_1m)
    blk = res.payload["rr"]["1.50"]
    check("n > 0 setups résolus", blk["n"] > 0, f"n={blk['n']}")
    check("p̂ = 1,0 (tendance : tous gagnants)", blk["p_hat"] == 1.0)
    check("p_prudent < p̂ (marge bayésienne)", blk["p_prudent"] < 1.0, f"{blk['p_prudent']:.3f}")
    check("EV prudente taker calculée", blk["ev_prudent_taker"] is not None)
    check("walk-forward renseigné", "1.50" in res.payload["walk_forward"])
    short = build_proba("BTCUSDT", "15m", "short", df_tf, df_1m)
    check("aucun setup short en tendance haussière", short.payload["n_setups"] == 0)


def test_annotate():
    print("[6] Annotation solide / spéculatif (§3)")
    base = {"n": 250, "k": 180, "p_hat": 0.72, "p_prudent": 0.66,
            "wilson": [0.6, 0.8], "ev_prudent_taker": 0.15, "ev_point_taker": 0.2,
            "ev_prudent_maker": 0.18, "k_max": 4, "cvar99_r": -1.0}
    solide = {"rr": {"1.50": base}, "walk_forward": {"1.50": {"status": "sain", "retention": 0.8}}}
    check("solide si EV_prud>0 ∧ n≥200 ∧ WF sain",
          annotate(solide, 1.5)["annotation"] == "solide")
    faible = {"rr": {"1.50": {**base, "n": 50}}, "walk_forward": {"1.50": {"status": "sain"}}}
    check("spéculatif si n < 200", annotate(faible, 1.5)["annotation"] == "spéculatif")
    over = {"rr": {"1.50": base}, "walk_forward": {"1.50": {"status": "overfit"}}}
    check("spéculatif si walk-forward overfit", annotate(over, 1.5)["annotation"] == "spéculatif")


def test_risk():
    print("[7] Dimensionnement §2.6 + budget partagé §5.4")
    sz = size_position(100.0, 2.0, "long")
    exp_risk = CONFIG.capital_usd * CONFIG.risk_pct
    check("risque = capital·riskPct", abs(sz.risk_usd - exp_risk) < 1e-6, f"{sz.risk_usd}")
    check("taille = risque / stopDist", abs(sz.size_units - exp_risk / 2.0) < 1e-6)
    szs = size_position(100.0, 2.0, "short")
    check("short : ×shortRiskFactor",
          abs(szs.risk_usd - exp_risk * CONFIG.short_risk_factor) < 1e-6)
    book = RiskBook(corr=0.9)
    ev = book.evaluate(OpenPosition("ETHUSDT", "short", exp_risk),
                       [OpenPosition("BTCUSDT", "long", exp_risk)])
    check("positions opposées interdites si ρ élevé", ev["allowed"] is False, ev["raison"])
    book2 = RiskBook(corr=0.1)
    ev2 = book2.evaluate(OpenPosition("ETHUSDT", "short", exp_risk),
                         [OpenPosition("BTCUSDT", "long", exp_risk)])
    check("opposées tolérées si ρ faible", ev2["allowed"] is True, f"{ev2['counted_pct']:.2f}%")
    # Corrélation : deux séries strictement croissantes → ρ ≈ 1.
    n = 40 * 24
    t = np.arange(n)
    ot = 1_704_067_200_000 + t * 3_600_000
    d1 = pd.DataFrame({"open_time": ot, "open": 100 + t, "high": 100 + t,
                       "low": 100 + t, "close": 100.0 + t, "volume": 1.0,
                       "close_time": ot + 59_999})
    d2 = pd.DataFrame({"open_time": ot, "open": 200 + 2 * t, "high": 200 + 2 * t,
                       "low": 200 + 2 * t, "close": 200.0 + 2 * t, "volume": 1.0,
                       "close_time": ot + 59_999})
    c = monthly_corr(d1, d2)
    check("corrélation mensuelle BTC/ETH élevée", c is not None and c > 0.8, f"ρ={c}")


def test_store_metier():
    print("[8] Store : probas + tickets")
    st = Store()
    st.record_proba("BTCUSDT", "15m", "long", {"n_setups": 3, "rr": {"1.50": {"n": 10}}})
    st.record_proba("BTCUSDT", "15m", "long", {"n_setups": 5, "rr": {"1.50": {"n": 20}}})
    lp = st.latest_proba("BTCUSDT", "15m", "long")
    check("latest_proba = dernier enregistrement", lp["n_setups"] == 5)
    check("all_latest_probas non vide", len(st.all_latest_probas()) == 1)
    tid = st.emit_ticket("BTCUSDT", "15m", {"direction": "long", "proba": {"annotation": "solide"}})
    tks = st.list_tickets()
    check("ticket relu avec ses champs",
          len(tks) == 1 and tks[0]["id"] == tid and tks[0]["direction"] == "long")
    for i in range(5):
        st.record_proba("BTCUSDT", "15m", "long", {"n_setups": i})
    st.prune_probas(keep_per_key=3)
    kept = st.conn.execute("SELECT COUNT(*) c FROM probas WHERE direction='long'").fetchone()["c"]
    check("prune_probas garde 3 par clé", kept == 3, f"{kept}")
    st.close()


def test_engine():
    print("[9] Orchestrateur : recalcul §3 + scan live idempotent")
    save_ohlcv_atomic(make_1m(80), "BTCUSDT", "1m")
    st = Store()
    summ = recompute_proba_tables(st)
    check("blocs §3 recalculés", summ["n_blocks"] >= 1, f"{summ['n_blocks']} blocs")
    check("table long alimentée",
          st.latest_proba("BTCUSDT", "15m", "long") is not None)
    now_ms = 1_704_067_200_000 + 80 * 15 * 60_000
    n1 = live_scan(st, now_ms)
    check("scan live émet un ticket", n1 == 1, f"{n1} ticket(s)")
    n2 = live_scan(st, now_ms)
    check("second scan idempotent (0 ticket)", n2 == 0)
    tks = st.list_tickets()
    check("ticket porte une annotation", tks and "annotation" in tks[0]["proba"])
    st.close()


def main():
    print(f"Répertoire de test : {_TMP}\n")
    test_indicators()
    test_detect()
    test_double_barrier()
    test_stats()
    test_build_proba()
    test_annotate()
    test_risk()
    test_store_metier()
    test_engine()
    print(f"\nRésultat : {PASS} réussis, {FAIL} échoués")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
