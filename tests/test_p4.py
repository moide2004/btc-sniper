"""Banc de vérification P4 — paper trading, livre, surveillance, walk-forward.

Déterministe, SANS réseau. Démontre :
  1. Dimensionnement (§5.8) : Kelly/4, plafond 1,5 %, m_GARCH borné à 1,5.
  2. Livre (§5.9) : positions opposées interdites, plafond 3 %, pondération
     1,5× même direction, multiplicateur corrélation.
  3. Exécution (§5.10) : limite à −0,25×ATR, fill au contact, KPI en R.
  4. Exécuteur virtuel (§5.11) : ticket → fill → TP/SL avec slippage et coûts,
     journal complet, carte de verdict (t-stat, Monte Carlo, Brier).
  5. Surveillance (§5.13) : CUSUM déclenche sur une série perdante, suspension,
     acquittement via web_actions consommé par le worker.
  6. Walk-forward (§5.12) : badges sain/fragile/overfit corrects.

Lancer :  python tests/test_p4.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="moteur_p4_")
os.environ["MOTEUR_DB"] = str(Path(_TMP) / "moteur.db")
os.environ["OHLCV_DIR"] = str(Path(_TMP) / "ohlcv")
os.environ["LOG_DIR"] = str(Path(_TMP) / "logs")
os.environ["BACKUP_DIR"] = str(Path(_TMP) / "backups")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from core.config import CONFIG  # noqa: E402
from core import monitoring  # noqa: E402
from core.decision import build_ticket, garch_multiplier, kelly_quarter  # noqa: E402
from core.execution import entry_improvement_r, limit_price  # noqa: E402
from core.paper_trader import PaperTrader, verdict_card  # noqa: E402
from core.risk_book import check_new_position, weighted_open_risk  # noqa: E402
from core.store import Store  # noqa: E402
from core.walkforward import _badge  # noqa: E402

CONFIG.ensure_dirs()
PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  {'✓' if cond else '✗'} {name}" + (f" — {detail}" if detail else ""))


def _dir_block(p=0.62, n=400, rr=1.5, ev_prud=0.12):
    from core.proba_engine import build_measure
    k = int(p * n)
    m = build_measure(k, n, rr=rr, cost_r=0.05).to_dict()
    return {"best": {"rr": rr, **m},
            "barrieres": [{"rr": r_, **build_measure(int(p * n), n, rr=r_, cost_r=0.05).to_dict()}
                          for r_ in (1.0, 1.5, 2.0)],
            "candidate": True, "motifs": []}


def _tf_table():
    return {"close": 50_000.0, "atr": 500.0, "cost_r": 0.05,
            "realisme": {"sigma_bougie": 0.01, "cvar99": 0.03, "k_max": 6.0}}


def test_sizing():
    print("[1] Dimensionnement (§5.8)")
    check("Kelly plein 0.6/RR1.5 → f*=0.3333 ; /4 = 0.0833",
          abs(kelly_quarter(0.6, 1.5) - (0.6 - 0.4 / 1.5) / 4) < 1e-12)
    check("Kelly négatif → 0", kelly_quarter(0.3, 1.0) == 0.0)
    rng = np.random.default_rng(2)
    close = 30_000 * np.exp(np.cumsum(rng.normal(0.0002, 0.02, 600)))
    m, ok = garch_multiplier(close)
    check("GARCH : m borné [0 ; 1,5]", 0.0 <= m <= 1.5, f"m={m:.3f} ok={ok}")
    t = build_ticket("4h", "bull/neutre", "long", _dir_block(), _tf_table(),
                     close, ts_utc="2026-01-01T00:00:00+00:00")
    check("ticket construit", t is not None)
    check("risque ≤ 1,5 % × m", t["taille"]["risque_pct"] <= 0.015 * 1.5 + 1e-12,
          f"{t['taille']['risque_pct']:.4f}")
    check("SL = entrée − ATR", abs(t["sl"] - (50_000 - 500)) < 1e-9)
    check("3 TP présents avec p̂/Wilson/EV",
          len(t["tps"]) == 3 and all("wilson" in tp and "ev_nette_prudente" in tp
                                     for tp in t["tps"]))


def test_risk_book():
    print("[2] Livre de risque (§5.9)")
    tk = {"direction": "short", "taille": {"risque_pct": 0.01}}
    open_pos = [{"direction": "long", "risque_pct": 0.01, "opened_utc": "2026-01-01"}]
    ok, motif = check_new_position(tk, open_pos, {})
    check("position opposée refusée", not ok and motif == "bloqué : direction opposée ouverte")

    tk2 = {"direction": "long", "taille": {"risque_pct": 0.012}}
    many = [{"direction": "long", "risque_pct": 0.012, "opened_utc": f"2026-01-0{i}"}
            for i in (1, 2)]
    ok2, motif2 = check_new_position(tk2, many, {})
    # risque pondéré : 0.012 + 1.5×0.012 + 1.5×0.012 = 0.048 > 3 %
    check("plafond 3 % appliqué (pondération 1,5×)", not ok2 and motif2 == "bloqué par budget")

    risk = weighted_open_risk(many, {})
    check("pondération même direction 1,5×", abs(risk - (0.012 + 1.5 * 0.012)) < 1e-12)
    risk2 = weighted_open_risk(many, {"1h~4h": 0.8})
    check("multiplicateur ×2 si ρ>0,7", abs(risk2 - 2 * risk) < 1e-12)


def test_execution():
    print("[3] Exécution (§5.10)")
    check("limite long = entrée − 0,25×ATR", limit_price(100.0, 4.0, "long") == 99.0)
    check("limite short = entrée + 0,25×ATR", limit_price(100.0, 4.0, "short") == 101.0)
    check("KPI amélioration = +0,25R", abs(entry_improvement_r(100.0, 99.0, 4.0, "long") - 0.25) < 1e-12)


def test_paper_trader():
    print("[4] Exécuteur virtuel (§5.11)")
    st = Store()
    trader = PaperTrader(st)
    close = 30_000 * np.exp(np.cumsum(np.random.default_rng(3).normal(0, 0.02, 400)))
    t = build_ticket("1h", "bull/neutre", "long", _dir_block(), _tf_table(), close,
                     ts_utc="2026-01-01T00:00:00+00:00")
    trader.emit_ticket(t)
    check("ticket en attente", len(trader.pending_tickets()) == 1)
    limite = trader.pending_tickets()[0]["payload"]["limite"]
    check("limite = 50000 − 125", abs(limite - 49_875.0) < 1e-9)

    # bougie qui touche la limite → fill
    trader.on_1m_candle({"open_time": 0, "open": 49_950, "high": 49_960,
                         "low": 49_870, "close": 49_900, "volume": 1,
                         "close_time": 59_999})
    check("fill exécuté", len(trader.open_positions()) == 1
          and len(trader.pending_tickets()) == 0)
    pos = trader.open_positions()[0]
    check("SL/TP ancrés au fill", abs(pos["sl"] - (49_875 - 500)) < 1e-9
          and abs(pos["tp"] - (49_875 + 1.5 * 500)) < 1e-9)

    # bougie qui touche le TP → clôture gagnante
    trader.on_1m_candle({"open_time": 60_000, "open": 50_000, "high": 50_700,
                         "low": 49_990, "close": 50_600, "volume": 1,
                         "close_time": 119_999})
    check("TP touché → position close", len(trader.open_positions()) == 0)
    j = trader.journal("1h")
    check("journal complet", len(j) == 1 and j[0]["issue"] == "tp"
          and abs(j[0]["r_result"] - (1.5 - 0.05)) < 1e-9
          and "p_annonce" in j[0] and "taille_pct" in j[0])

    # SL avec slippage : short → ×1,5
    t2 = build_ticket("4h", "bear/neutre", "short", _dir_block(), _tf_table(), close,
                      ts_utc="2026-01-01T04:00:00+00:00")
    trader.emit_ticket(t2)
    trader.on_1m_candle({"open_time": 120_000, "open": 50_100, "high": 50_130,
                         "low": 50_050, "close": 50_100, "volume": 1,
                         "close_time": 179_999})  # touche limite 50125
    check("fill short", len(trader.open_positions()) == 1)
    trader.on_1m_candle({"open_time": 180_000, "open": 50_200, "high": 50_700,
                         "low": 50_150, "close": 50_650, "volume": 1,
                         "close_time": 239_999})  # SL short = 50125+500=50625 touché
    j2 = trader.journal("4h")
    check("SL short : −1×1,5 − coûts", len(j2) == 1
          and abs(j2[0]["r_result"] - (-1.5 - 0.05)) < 1e-9, f"{j2[0]['r_result']:.3f}")

    card = verdict_card(trader.journal())
    check("carte de verdict complète",
          card["n"] == 2 and "t_stat" in card and "mc_dd_p95" in card
          and "brier" in card and "profit_factor" in card)
    st.close()


def test_monitoring():
    print("[5] Surveillance CUSUM (§5.13)")
    st = Store()
    # série perdante : 30 trades à −1,1 R, EV annoncée +0,1
    journal = []
    for i in range(30):
        monitoring.update_on_trade(st, "1h", -1.1, 0.1)
        journal.append({"r_result": -1.1, "stage": "1h",
                        "ts_utc": f"2026-01-{i % 28 + 1:02d}T00:00:00+00:00"})
    stt = monitoring.get_state(st, "1h")
    check("CUSUM a monté", stt["cusum"] > 3.0, f"{stt['cusum']:.2f}")
    # 25 clôtures → lecture → alarme (σ_R≈0 → h≈0 < CUSUM)
    for _ in range(25):
        monitoring.check_on_stage_close(st, "1h", journal)
    check("étage en enquête", monitoring.is_suspended(st, "1h"))
    # acquittement via web_actions (flux §7.1)
    st.add_web_action("ack_alarm", "1h")
    n = monitoring.process_web_actions(st)
    check("acquittement consommé par le worker", n == 1
          and not monitoring.is_suspended(st, "1h"))
    st.close()


def test_walkforward_badges():
    print("[6] Walk-forward — badges (§5.12)")
    check("rétention 0,8 → sain", _badge(0.8) == "sain")
    check("rétention 0,3 → fragile", _badge(0.3) == "fragile")
    check("rétention 0,1 → overfit", _badge(0.1) == "overfit")
    check("rétention None → n/a", _badge(None) == "n/a")


def main():
    print(f"Répertoire de test : {_TMP}\n")
    test_sizing()
    test_risk_book()
    test_execution()
    test_paper_trader()
    test_monitoring()
    test_walkforward_badges()
    print(f"\nRésultat : {PASS} réussis, {FAIL} échoués")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
