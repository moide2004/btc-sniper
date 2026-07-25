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

    print("[2] Ajout + liste")
    r = c.post("/api/journal-perso", json={
        "symbol": "btcusdt", "timeframe": "4h", "direction": "long",
        "entry": 60000, "sl": 58000, "tp": 63000, "size_units": 0.05,
        "note": "cassure 4h propre"})
    check("ajout accepté", r.status_code == 200 and r.get_json()["ok"])
    jid = r.get_json()["id"]
    rows = c.get("/api/journal-perso").get_json()["trades"]
    check("listé, symbole normalisé en majuscules",
          len(rows) == 1 and rows[0]["symbol"] == "BTCUSDT" and rows[0]["closed_utc"] is None)

    print("[3] Clôture — calcul du R et du PnL")
    r = c.post(f"/api/journal-perso/{jid}/cloture", json={"exit_price": 63000})
    j = r.get_json()
    # long : (63000-60000)/(60000-58000) = 1.5 R ; PnL = 1.5 × 0.05 × 2000 = 150 $
    check("R = +1,50", abs(j["r_result"] - 1.5) < 1e-9, str(j["r_result"]))
    check("PnL = +150 $", abs(j["pnl_usd"] - 150.0) < 1e-9, str(j["pnl_usd"]))
    check("re-clôture refusée (404)",
          c.post(f"/api/journal-perso/{jid}/cloture", json={"exit_price": 64000}).status_code == 404)

    r2 = c.post("/api/journal-perso", json={"symbol": "ETHUSDT", "direction": "short",
                                            "entry": 3000, "sl": 3100, "size_units": 1.0})
    jid2 = r2.get_json()["id"]
    j2 = c.post(f"/api/journal-perso/{jid2}/cloture", json={"exit_price": 2850}).get_json()
    # short : (3000-2850)/100 = 1.5 R ; PnL = 1.5 × 1.0 × 100 = 150 $
    check("short : R = +1,50, PnL = +150 $",
          abs(j2["r_result"] - 1.5) < 1e-9 and abs(j2["pnl_usd"] - 150.0) < 1e-9)

    print("[4] Suppression + isolation")
    check("suppression OK",
          c.post(f"/api/journal-perso/{jid}/supprimer").get_json()["ok"])
    check("1 ligne restante", len(c.get("/api/journal-perso").get_json()["trades"]) == 1)
    lv = c.get("/api/livre").get_json()
    check("journal du BOT non pollué (table isolée)", lv["journal"] == [])

    print(f"\nRésultat : {PASS} réussis, {FAIL} échoués")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
