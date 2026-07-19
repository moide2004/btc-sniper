"""Banc de vérification P3 — synthèse bayésienne + cloche (acquittement).

Déterministe, SANS réseau. Démontre :
  1. Empilement bayésien (§5.6) : des échelles haussières donnent P(hausse) > 0,5,
     plafonné à 85 %, contributions présentes et triées.
  2. Neutralité : que des p̂ = 0,5 donnent une synthèse ≈ 0,5.
  3. Cloche : marquage lu = écriture web autorisée (§7.1), journalisée dans
     web_actions, compteur de non-lus remis à zéro.

Lancer :  python tests/test_p3.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="moteur_p3_")
os.environ["MOTEUR_DB"] = str(Path(_TMP) / "moteur.db")
os.environ["OHLCV_DIR"] = str(Path(_TMP) / "ohlcv")
os.environ["LOG_DIR"] = str(Path(_TMP) / "logs")
os.environ["BACKUP_DIR"] = str(Path(_TMP) / "backups")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import CONFIG  # noqa: E402
from core.stack import StackEntry, stack  # noqa: E402
from core.store import Store  # noqa: E402

CONFIG.ensure_dirs()
PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  {'✓' if cond else '✗'} {name}" + (f" — {detail}" if detail else ""))


def test_stack_bullish():
    print("[1] Empilement bayésien — échelles haussières")
    entries = [StackEntry(tf, p, 500) for tf, p in
               [("1h", 0.58), ("4h", 0.60), ("1D", 0.62), ("1W", 0.57)]]
    s = stack(entries)
    check("P(hausse) > 0.5", s["p_up"] > 0.5, f"{s['p_up']:.3f}")
    check("P(hausse) ≤ plafond 0.85", s["p_up"] <= 0.85 + 1e-9, f"{s['p_up']:.3f}")
    check("4 échelles combinées", s["n_timeframes"] == 4)
    check("contributions présentes et triées",
          len(s["contributions"]) == 4 and
          abs(s["contributions"][0]["contribution"]) >= abs(s["contributions"][-1]["contribution"]))


def test_stack_cap():
    print("[2] Plafond 85 %")
    entries = [StackEntry(tf, 0.95, 500) for tf in ["1h", "4h", "1D", "1W", "2W"]]
    s = stack(entries)
    check("plafonné à 0.85", abs(s["p_up"] - 0.85) < 1e-9, f"{s['p_up']:.3f}")
    check("drapeau capped = True", s["capped"] is True)


def test_stack_neutral():
    print("[3] Neutralité (p̂ = 0.5)")
    entries = [StackEntry(tf, 0.5, 500) for tf in ["1h", "4h", "1D"]]
    s = stack(entries)
    check("synthèse ≈ 0.5", abs(s["p_up"] - 0.5) < 1e-9, f"{s['p_up']:.4f}")


def test_bell_ack():
    print("[4] Cloche — marquage lu (écriture web autorisée §7.1)")
    st = Store()
    st.add_event("candidate", "info", "case devenue candidate — test")
    st.add_event("data_incident", "warning", "bascule — test")
    check("2 non-lus au départ", st.unread_count() == 2)
    n = st.mark_events_read()
    check("2 marqués lus", n == 2)
    check("0 non-lu après", st.unread_count() == 0)
    wa = st.conn.execute("SELECT action FROM web_actions ORDER BY id DESC LIMIT 1").fetchone()
    check("action journalisée dans web_actions", wa is not None and wa["action"] == "mark_read")
    st.close()


def main():
    print(f"Répertoire de test : {_TMP}\n")
    test_stack_bullish()
    test_stack_cap()
    test_stack_neutral()
    test_bell_ack()
    print(f"\nRésultat : {PASS} réussis, {FAIL} échoués")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
