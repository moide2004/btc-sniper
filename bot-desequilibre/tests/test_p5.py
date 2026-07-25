"""Banc de vérification P5 — bilan go/no-go par flux. Déterministe, SANS réseau.

Démontre : les 5 critères cumulatifs du verdict (§8 P5) — n, PF, t, DD MC,
dégradation progressive + rétention walk-forward — et l'agrégat « bilan » du
backtest (comptes go / no-go / insuffisant).

Lancer :  python tests/test_p5.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="botdes_p5_")
os.environ["MOTEUR_DB"] = str(Path(_TMP) / "moteur.db")
os.environ["OHLCV_DIR"] = str(Path(_TMP) / "ohlcv")
os.environ["LOG_DIR"] = str(Path(_TMP) / "logs")
os.environ["BACKUP_DIR"] = str(Path(_TMP) / "backups")
os.environ["SYMBOLS"] = "BTCUSDT"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import CONFIG  # noqa: E402
from core.engine import _p5_verdict  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  {'✓' if cond else '✗'} {name}" + (f" — {detail}" if detail else ""))


def summ(n=300, pf=1.5, t=2.0, mc_dd=8.0):
    return {"n": n, "profit_factor": pf, "t_stat": t, "mc_dd_p95_r": mc_dd}


def test_verdict():
    print("[1] Verdict — cas nominal GO (5 critères réunis)")
    v = _p5_verdict(summ(), retention=0.7, wf_status="sain")
    check("statut go", v["statut"] == "go" and v["go"])
    check("détail des critères exposé",
          all(v[k] for k in ("n_ok", "pf_ok", "t_ok", "dd_ok", "ret_ok", "degr_ok")))
    # DD capital : 8 R × 1,5 % × 1,25 = 15 % < 30 %.
    check("DD capital converti", abs(v["dd_capital_pct"] - 8 * CONFIG.risk_pct * 1.25 * 100) < 1e-9,
          f"{v['dd_capital_pct']:.1f}%")

    print("[2] Chaque critère fait échouer seul")
    check("n insuffisant → insuffisant",
          _p5_verdict(summ(n=100), 0.7, "sain")["statut"] == "insuffisant")
    check("PF ≤ 1,15 → no-go", _p5_verdict(summ(pf=1.10), 0.7, "sain")["statut"] == "no-go")
    check("t < 1,5 → no-go", _p5_verdict(summ(t=1.2), 0.7, "sain")["statut"] == "no-go")
    big_dd = 30.0 / (CONFIG.risk_pct * 1.25 * 100) + 1  # dépasse 30 % du capital
    check("DD MC trop grand → no-go",
          _p5_verdict(summ(mc_dd=big_dd), 0.7, "sain")["statut"] == "no-go")
    check("rétention < 0,5 → no-go", _p5_verdict(summ(), 0.3, "fragile")["statut"] == "no-go")
    check("walk-forward overfit → no-go (dégradation NON progressive)",
          _p5_verdict(summ(), 0.7, "overfit")["statut"] == "no-go")
    check("rétention absente → no-go (jamais un go sans preuve)",
          _p5_verdict(summ(), None, None)["statut"] == "no-go")
    v = _p5_verdict(summ(), 0.55, "fragile")
    check("fragile mais rétention ≥ 0,5 → go (critères §8 stricts)",
          v["statut"] == "go", v["statut"])


def test_bilan_agregat():
    print("[3] Agrégat bilan (comptage des statuts)")
    stats = [_p5_verdict(summ(), 0.7, "sain"),
             _p5_verdict(summ(pf=0.9), 0.7, "sain"),
             _p5_verdict(summ(n=10), 0.7, "sain")]
    bilan = {"go": 0, "no-go": 0, "insuffisant": 0}
    for v in stats:
        bilan[v["statut"]] += 1
    check("1 go / 1 no-go / 1 insuffisant",
          bilan == {"go": 1, "no-go": 1, "insuffisant": 1}, str(bilan))


def main():
    print(f"Répertoire de test : {_TMP}\n")
    test_verdict()
    test_bilan_agregat()
    print(f"\nRésultat : {PASS} réussis, {FAIL} échoués")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
