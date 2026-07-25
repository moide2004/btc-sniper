"""Banc de vérification — journal PERSONNEL (saisie manuelle web). SANS réseau.

Démontre : auth exigée ; ajout validé (actif requis, sens contrôlé) ; clôture
avec calcul du R par rapport au stop (long ET short) + PnL si taille ; ligne
déjà clôturée refusée ; suppression ; la table dédiée n'interfère pas avec le
journal du bot.

Lancer :  python tests/test_journal.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="botdes_jp_")
os.environ["MOTEUR_DB"] = str(Path(_TMP) / "moteur.db")
os.environ["OHLCV_DIR"] = str(Path(_TMP) / "ohlcv")
os.environ["LOG_DIR"] = str(Path(_TMP) / "logs")
os.environ["BACKUP_DIR"] = str(Path(_TMP) / "backups")
os.environ["SYMBOLS"] = "BTCUSDT,ETHUSDT"
os.environ["FLASK_SECRET_KEY"] = "test-secret-key"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  {'✓' if cond else '✗'} {name}" + (f" — {detail}" if detail else ""))


def client():
    from web.app import app
    app.config["TESTING"] = True
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["auth"] = True
    return c


def main():
    print(f"Répertoire de test : {_TMP}\n")

    print("[1] Auth + validation")
    from web.app import app
    anon = app.test_client()
    check("POST sans session → 401",
          anon.post("/api/journal-perso", json={"symbol": "BTCUSDT"}).status_code == 401)
    c = client()
    check("actif manquant → 400", c.post("/api/journal-perso", json={}).status_code == 400)
    check("sens invalide → 400", c.post("/api/journal-perso",
          json={"symbol": "BTCUSDT", "direction": "hausse"}).status_code == 400)

    print("[2] Ajout + liste (mode COMPTE : capital × risque%)")
    r = c.post("/api/journal-perso", json={
        "symbol": "btcusdt", "timeframe": "4h", "direction": "long",
        "entry": 60000, "sl": 58000, "tp": 63000,
        "capital_usd": 10000, "risk_pct": 1, "note": "cassure 4h propre"})
    check("ajout accepté", r.status_code == 200 and r.get_json()["ok"])
    jid = r.get_json()["id"]
    rows = c.get("/api/journal-perso").get_json()["trades"]
    check("listé, symbole normalisé en majuscules",
          len(rows) == 1 and rows[0]["symbol"] == "BTCUSDT" and rows[0]["closed_utc"] is None)
    check("capital et risque stockés",
          rows[0]["capital_usd"] == 10000 and rows[0]["risk_pct"] == 1)

    print("[3] Clôture — R et PnL = R × capital × risque%")
    r = c.post(f"/api/journal-perso/{jid}/cloture", json={"exit_price": 63000})
    j = r.get_json()
    # long : (63000-60000)/(60000-58000) = 1.5 R ; PnL = 1.5 × 10000 × 1% = 150 $
    check("R = +1,50", abs(j["r_result"] - 1.5) < 1e-9, str(j["r_result"]))
    check("PnL = +150 $ (compte 10k, mise 1 %)", abs(j["pnl_usd"] - 150.0) < 1e-9, str(j["pnl_usd"]))
    check("re-clôture refusée (404)",
          c.post(f"/api/journal-perso/{jid}/cloture", json={"exit_price": 64000}).status_code == 404)

    # Repli « taille en unités » toujours supporté (sans capital/risque).
    r2 = c.post("/api/journal-perso", json={"symbol": "ETHUSDT", "direction": "short",
                                            "entry": 3000, "sl": 3100, "size_units": 1.0})
    jid2 = r2.get_json()["id"]
    j2 = c.post(f"/api/journal-perso/{jid2}/cloture", json={"exit_price": 2850}).get_json()
    # short : (3000-2850)/100 = 1.5 R ; PnL = 1.5 × 1.0 × 100 = 150 $
    check("repli taille : R = +1,50, PnL = +150 $",
          abs(j2["r_result"] - 1.5) < 1e-9 and abs(j2["pnl_usd"] - 150.0) < 1e-9)

    print("[3bis] TP AUTO depuis le ratio (1:3)")
    r3 = c.post("/api/journal-perso", json={"symbol": "ETHUSDT", "direction": "short",
                                            "entry": 3000, "sl": 3100, "ratio": 3,
                                            "capital_usd": 10000, "risk_pct": 2})
    j3 = r3.get_json()
    # short : stop=100 → TP = 3000 − 3×100 = 2700
    check("TP calculé = 2700", j3.get("tp") == 2700.0, str(j3.get("tp")))
    jid3 = j3["id"]
    jc = c.post(f"/api/journal-perso/{jid3}/cloture", json={"exit_price": 2700}).get_json()
    # sortie au TP : R = +3 ; PnL = 3 × 10000 × 2% = 600 $
    check("clôture au TP : R = +3, PnL = +600 $ (10k, 2 %)",
          abs(jc["r_result"] - 3.0) < 1e-9 and abs(jc["pnl_usd"] - 600.0) < 1e-9)
    # Perte au SL : R = −1 ; PnL = −200 $ (10k, 2 %)
    r4 = c.post("/api/journal-perso", json={"symbol": "BTCUSDT", "direction": "long",
                                            "entry": 60000, "sl": 59000, "ratio": 3,
                                            "capital_usd": 10000, "risk_pct": 2})
    j4 = c.post(f"/api/journal-perso/{r4.get_json()['id']}/cloture",
                json={"exit_price": 59000}).get_json()
    check("clôture au SL : R = −1, PnL = −200 $",
          abs(j4["r_result"] + 1.0) < 1e-9 and abs(j4["pnl_usd"] + 200.0) < 1e-9)

    print("[4] Suppression + isolation")
    check("suppression OK",
          c.post(f"/api/journal-perso/{jid}/supprimer").get_json()["ok"])
    check("3 lignes restantes", len(c.get("/api/journal-perso").get_json()["trades"]) == 3)
    lv = c.get("/api/livre").get_json()
    check("journal du BOT non pollué (table isolée)", lv["journal"] == [])

    print(f"\nRésultat : {PASS} réussis, {FAIL} échoués")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
