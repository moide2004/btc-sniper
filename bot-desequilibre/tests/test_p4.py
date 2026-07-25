"""Banc de vérification P4 — paper trading. Déterministe, SANS réseau.

Démontre : fill double barrière pessimiste (TP/SL/BE/censuré) ; break-even qui
ramène le SL à l'entrée pour les bougies SUIVANTES ; simulate_flux (R net de
coûts) ; stats de flux (PF, t-stat, drawdown) ; position live avancée bougie par
bougie ; verdict go/no-go P5.

Lancer :  python tests/test_p4.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="botdes_p4_")
os.environ["MOTEUR_DB"] = str(Path(_TMP) / "moteur.db")
os.environ["OHLCV_DIR"] = str(Path(_TMP) / "ohlcv")
os.environ["LOG_DIR"] = str(Path(_TMP) / "logs")
os.environ["BACKUP_DIR"] = str(Path(_TMP) / "backups")
os.environ["SYMBOLS"] = "BTCUSDT,ETHUSDT"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from core.fibonacci import Setup  # noqa: E402
from core.paper import (  # noqa: E402
    Trade, advance_live_position, flux_summary, max_drawdown_r, new_live_position,
    profit_factor, resolve_fill, simulate_flux, t_stat,
)

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  {'✓' if cond else '✗'} {name}" + (f" — {detail}" if detail else ""))


def H(pairs):  # (highs, lows) depuis une liste [(hi,lo),...]
    return np.array([p[0] for p in pairs], "float64"), np.array([p[1] for p in pairs], "float64")


def test_resolve_fill():
    print("[1] Fill double barrière + break-even (long : e=100, sl=90, tp=120, be=110)")
    kw = dict(entry=100.0, sl0=90.0, tp=120.0, direction="long", stop_dist=10.0,
              be_trigger=1.0, start_idx=0)
    hi, lo = H([(105, 101), (121, 115)])
    f = resolve_fill(highs=hi, lows=lo, **kw)
    check("TP atteint", f.reason == "tp" and f.exit_idx == 1)

    hi, lo = H([(95, 88)])
    f = resolve_fill(highs=hi, lows=lo, **kw)
    check("SL atteint", f.reason == "sl" and f.r_gross == -1.0)

    hi, lo = H([(112, 101), (108, 99)])       # bar0 arme BE (≥110) ; bar1 retombe à 100
    f = resolve_fill(highs=hi, lows=lo, **kw)
    check("break-even (SL ramené à l'entrée)", f.reason == "be" and f.r_gross == 0.0)

    hi, lo = H([(112, 101), (108, 100.5)])    # BE armé bar0, mais ne retouche pas 100
    f = resolve_fill(highs=hi, lows=lo, **kw)
    check("censuré si aucune barrière touchée", f.reason == "censored")

    # Ex æquo dans la MÊME bougie → SL prioritaire (pessimiste).
    hi, lo = H([(120, 90)])
    f = resolve_fill(highs=hi, lows=lo, **kw)
    check("ex æquo TP/SL → SL (pessimiste)", f.reason == "sl")

    # Short miroir : e=100, sl=110, tp=80.
    hi, lo = H([(101, 79)])
    f = resolve_fill(entry=100.0, sl0=110.0, tp=80.0, direction="short",
                     stop_dist=10.0, be_trigger=1.0, start_idx=0, highs=hi, lows=lo)
    check("short TP atteint", f.reason == "tp")


def _setup(entry_ms, entry, sl, direction="long", stop=10.0):
    return Setup(idx=0, direction=direction, signal_open_ms=entry_ms - 60000,
                 signal_close_ms=entry_ms - 1, entry_time_ms=entry_ms, entry=entry,
                 sl=sl, stop_dist=stop, amplitude=30.0, atr=5.0, fib={}, entry_is_proxy=False)


def test_simulate_flux():
    print("[2] simulate_flux — R net de coûts, chronologique")
    ot = np.arange(0, 10) * 60000
    hi, lo = H([(100, 100), (105, 99), (121, 100), (100, 100),
                (100, 88), (100, 100), (100, 100), (100, 100), (100, 100), (100, 100)])
    # setup1 entrée à t=1 (idx1) → monte à 121 (TP, rr=2) ; setup2 entrée t=4 (idx4) → 88 (SL)
    s1 = _setup(60000, 100.0, 90.0)
    s2 = _setup(4 * 60000, 100.0, 90.0)
    trades = simulate_flux([s2, s1], ot, hi, lo, rr=2.0, be_trigger=5.0, fee=0.0, risk_usd=100.0)
    check("2 trades résolus, ordre chronologique",
          len(trades) == 2 and trades[0].entry_time_ms < trades[1].entry_time_ms)
    check("gagnant = +2 R, perdant = −1 R (fee=0)",
          abs(trades[0].r_net - 2.0) < 1e-9 and abs(trades[1].r_net + 1.0) < 1e-9)
    check("PnL USD = R × risque", trades[0].pnl_usd == 200.0 and trades[1].pnl_usd == -100.0)


def test_stats():
    print("[3] Statistiques de flux")
    r = np.array([2.0, -1.0, 2.0, -1.0, -1.0], "float64")
    check("profit factor = 4/3", abs(profit_factor(r) - (4.0 / 3.0)) < 1e-9)
    check("drawdown max (R)", abs(max_drawdown_r(list(r)) - 2.0) < 1e-9,
          f"{max_drawdown_r(list(r))}")
    check("t-stat calculé", t_stat(r) is not None)
    trades = [Trade(i, i + 1, "long", "tp" if x > 0 else "sl", x, 100.0, x * 100.0)
              for i, x in enumerate(r)]
    s = flux_summary(trades)
    check("résumé cohérent (n, wins, sum_r)",
          s["n"] == 5 and s["wins"] == 2 and abs(s["sum_r"] - 1.0) < 1e-9)
    check("flux vide → n=0 sans planter", flux_summary([])["n"] == 0)


def test_live_position():
    print("[4] Position live avancée bougie par bougie")
    s = _setup(0, 100.0, 90.0)
    pos = new_live_position("BTCUSDT", "4h", s, None, rr=2.0)
    pos["last_1m_ms"] = -1
    # bougies : monte vers 121 → TP
    bars = [(0, 105, 100), (60000, 121, 100)]
    close = advance_live_position(pos, bars)
    check("clôture TP détectée live", close is not None and close["reason"] == "tp")
    check("R net positif (coût taker déduit)", close["r_net"] > 1.9 and close["r_net"] < 2.0)

    # Nouvelle position : SL avant tout.
    pos2 = new_live_position("ETHUSDT", "1h", _setup(0, 100.0, 90.0), None, rr=2.0)
    pos2["last_1m_ms"] = -1
    c2 = advance_live_position(pos2, [(0, 100, 88)])
    check("clôture SL détectée live", c2 is not None and c2["reason"] == "sl" and c2["r_net"] < 0)


def main():
    print(f"Répertoire de test : {_TMP}\n")
    test_resolve_fill()
    test_simulate_flux()
    test_stats()
    test_live_position()
    print(f"\nRésultat : {PASS} réussis, {FAIL} échoués")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
