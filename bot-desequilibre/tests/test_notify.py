"""Banc de vérification — push ntfy (amendement BRIEF 2026-07-25). SANS réseau.

Démontre : désactivé par défaut (topic vide → aucun appel HTTP) ; activé →
POST vers l'URL du sujet avec titre/priorité/tags ; panne réseau silencieuse
(False, jamais d'exception vers le worker) ; en-tête Title épuré en latin-1.

Lancer :  python tests/test_notify.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="botdes_ntfy_")
os.environ["MOTEUR_DB"] = str(Path(_TMP) / "moteur.db")
os.environ["OHLCV_DIR"] = str(Path(_TMP) / "ohlcv")
os.environ["LOG_DIR"] = str(Path(_TMP) / "logs")
os.environ["BACKUP_DIR"] = str(Path(_TMP) / "backups")
os.environ["NTFY_TOPIC"] = ""            # désactivé au départ

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import CONFIG  # noqa: E402
from core.notify import send_push  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  {'✓' if cond else '✗'} {name}" + (f" — {detail}" if detail else ""))


class FakeRequests:
    def __init__(self, ok=True, boom=False):
        self.calls = []
        self.ok = ok
        self.boom = boom

    def post(self, url, data=None, headers=None, timeout=None):
        if self.boom:
            raise ConnectionError("réseau coupé")
        self.calls.append({"url": url, "data": data, "headers": headers,
                           "timeout": timeout})
        class R:  # noqa: N801
            ok = self.ok
        return R()


def main():
    print(f"Répertoire de test : {_TMP}\n")

    print("[1] Désactivé par défaut")
    fake = FakeRequests()
    check("topic vide → False, AUCUN appel HTTP",
          send_push("t", "m", requests=fake) is False and fake.calls == [])

    print("[2] Activé → envoi correct")
    CONFIG.ntfy_topic = "sujet-secret-123"
    CONFIG.ntfy_url = "https://ntfy.sh"
    fake = FakeRequests()
    ok = send_push("Ticket BTCUSDT 4h LONG — solide",
                   "Entrée ≈ 60000 · SL 58000", priority="high",
                   tags="rotating_light", requests=fake)
    c = fake.calls[0]
    check("envoi réussi", ok is True and len(fake.calls) == 1)
    check("URL = ntfy.sh/<topic>", c["url"] == "https://ntfy.sh/sujet-secret-123")
    check("titre + priorité + tags posés",
          c["headers"]["Priority"] == "high" and c["headers"]["Tags"] == "rotating_light"
          and c["headers"]["Title"].startswith("Ticket BTCUSDT"))
    check("timeout court (non bloquant)", c["timeout"] is not None and c["timeout"] <= 10)
    check("titre épuré en latin-1 (p̂ toléré)",
          send_push("p̂=0.60 · éval", "corps", requests=FakeRequests()) is True)

    print("[3] Panne réseau silencieuse")
    check("exception réseau → False, rien ne remonte",
          send_push("t", "m", requests=FakeRequests(boom=True)) is False)
    CONFIG.ntfy_topic = ""

    print(f"\nRésultat : {PASS} réussis, {FAIL} échoués")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
