"""Banc de vérification P3 — endpoints web des 4 vues. Déterministe, SANS réseau.

Démontre : auth exigée (401 sans session) ; /api/probas expose la matrice §3
(cases triables actif×TF×direction avec p̂/Wilson/EV) ; /api/tickets expose les
tickets annotés ; /api/health et /api/livre répondent ; la page rend les 4 vues.
NaN/Infinity → null (jamais de JSON invalide pour le navigateur).

Lancer :  python tests/test_p3.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="botdes_p3_")
os.environ["MOTEUR_DB"] = str(Path(_TMP) / "moteur.db")
os.environ["OHLCV_DIR"] = str(Path(_TMP) / "ohlcv")
os.environ["LOG_DIR"] = str(Path(_TMP) / "logs")
os.environ["BACKUP_DIR"] = str(Path(_TMP) / "backups")
os.environ["SYMBOLS"] = "BTCUSDT,ETHUSDT"
os.environ["FLASK_SECRET_KEY"] = "test-secret-key"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.store import Store  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  {'✓' if cond else '✗'} {name}" + (f" — {detail}" if detail else ""))


def seed():
    st = Store()
    st.record_proba("BTCUSDT", "4h", "long", {
        "n_setups": 300,
        "rr": {"1.50": {"n": 250, "k": 150, "p_hat": 0.6, "p_prudent": 0.55,
                        "wilson": [0.54, 0.66], "ev_prudent_taker": 0.12,
                        "ev_point_taker": 0.2, "ev_prudent_maker": 0.15,
                        "k_max": 5, "cvar99_r": -1.02}},
        "walk_forward": {"1.50": {"status": "sain", "retention": 0.7}}})
    st.set_kv("corr_btc_eth", "0.83")
    st.emit_ticket("BTCUSDT", "4h", {
        "direction": "long", "entry_ref": 60000.0, "sl": 58000.0, "tp": 63000.0,
        "entry_is_proxy": True, "stop_dist": 2000.0,
        "sizing": {"risk_usd": 45.0, "size_units": 0.0225, "notional_usd": 1350.0,
                   "risk_pct_capital": 1.5},
        "proba": {"annotation": "solide", "solide": True, "n": 250, "p_hat": 0.6,
                  "p_prudent": 0.55, "ev_prudent_taker": 0.12, "wf_status": "sain"}})
    st.close()


def client():
    from web.app import app
    app.config["TESTING"] = True
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["auth"] = True
    return c, app


def test_auth():
    print("[1] Authentification exigée")
    from web.app import app
    anon = app.test_client()
    check("GET / sans session → redirection login", anon.get("/").status_code in (301, 302))
    check("GET /api/probas sans session → 401", anon.get("/api/probas").status_code == 401)


def test_probas():
    print("[2] /api/probas — matrice §3")
    c, _ = client()
    r = c.get("/api/probas")
    check("HTTP 200", r.status_code == 200)
    j = r.get_json()
    check("rr_live exposé", j.get("rr_live") == 1.5, str(j.get("rr_live")))
    check("corrélation BTC/ETH exposée", abs((j.get("corr_btc_eth") or 0) - 0.83) < 1e-6)
    case = next((x for x in j["cases"] if x["symbol"] == "BTCUSDT"
                 and x["timeframe"] == "4h" and x["direction"] == "long"), None)
    check("case BTC 4h long présente", case is not None)
    check("p̂ + Wilson + EV renseignés",
          case and case["p_hat"] == 0.6 and case["wilson"] == [0.54, 0.66]
          and case["ev_prudent_taker"] == 0.12)


def test_tickets():
    print("[3] /api/tickets — tickets annotés")
    c, _ = client()
    r = c.get("/api/tickets")
    check("HTTP 200", r.status_code == 200)
    tks = r.get_json()["tickets"]
    check("ticket exposé avec annotation solide",
          len(tks) == 1 and tks[0]["proba"]["annotation"] == "solide")
    check("niveaux entrée/SL/TP présents",
          tks[0]["entry_ref"] == 60000.0 and tks[0]["sl"] == 58000.0 and tks[0]["tp"] == 63000.0)


def test_health_livre_index():
    print("[4] /api/health · /api/livre · page 4 vues")
    c, _ = client()
    check("/api/health 200", c.get("/api/health").status_code == 200)
    lv = c.get("/api/livre").get_json()
    check("plafond §5.4 exposé", lv.get("plafond_pct") == 4.0, str(lv.get("plafond_pct")))
    html = c.get("/").get_data(as_text=True)
    check("page rend les 4 onglets",
          all(w in html for w in ("Santé", "Matrice", "Tickets", "Livre")))


def main():
    print(f"Répertoire de test : {_TMP}\n")
    seed()
    test_auth()
    test_probas()
    test_tickets()
    test_health_livre_index()
    print(f"\nRésultat : {PASS} réussis, {FAIL} échoués")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
