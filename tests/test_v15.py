"""Banc de vérification v1.5 — dimension volatilité (§4). Déterministe, sans réseau.

Démontre :
  1. Vol EWMA λ=0,94 : formule récursive exacte.
  2. Zones : une série alternant calme/tempête étiquette basse/haute correctement,
     sans look-ahead (seuils issus de l'année écoulée).
  3. Activation case par case : ACTIVE seulement si chaque sous-case (état ×
     zone) garde n ≥ 200 ; motif d'inactivation exposé sinon.
  4. Sous-cases : mesures complètes (p̂/n/Wilson/EV avec coûts) par zone.

Lancer :  python tests/test_v15.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="moteur_v15_")
os.environ["MOTEUR_DB"] = str(Path(_TMP) / "moteur.db")
os.environ["OHLCV_DIR"] = str(Path(_TMP) / "ohlcv")
os.environ["LOG_DIR"] = str(Path(_TMP) / "logs")
os.environ["BACKUP_DIR"] = str(Path(_TMP) / "backups")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from core.config import CONFIG  # noqa: E402
from core.data_source import MINUTE_MS  # noqa: E402
from core.proba_engine import compute_timeframe_all_states  # noqa: E402
from core.states import daily_sma200_ref, ewma_vol, vol_zone_labels  # noqa: E402

CONFIG.ensure_dirs()
PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  {'✓' if cond else '✗'} {name}" + (f" — {detail}" if detail else ""))


def klines_from_close(close, tf_ms=MINUTE_MS, start=1_600_000_000_000):
    start -= start % 86_400_000
    rows = []
    prev = close[0]
    for i, c in enumerate(close):
        ot = start + i * tf_ms
        h, l = max(prev, c) * 1.0005, min(prev, c) * 0.9995
        rows.append((ot, prev, h, l, c, 1.0, ot + tf_ms - 1))
        prev = c
    return pd.DataFrame(rows, columns=["open_time", "open", "high", "low",
                                       "close", "volume", "close_time"])


def test_ewma_formula():
    print("[1] Vol EWMA λ=0,94 — récursion exacte")
    close = pd.Series([100.0, 101.0, 100.5, 102.0, 101.0, 103.0,
                       102.5, 104.0, 103.0, 105.0, 104.0, 106.0])
    v = ewma_vol(close, lam=0.94)
    r = np.log(close).diff().to_numpy()
    var = r[1] ** 2
    for t in range(2, len(close)):
        var = 0.94 * var + 0.06 * r[t] ** 2
    check("dernière vol == récursion manuelle",
          abs(float(v.iloc[-1]) - np.sqrt(var)) < 1e-12,
          f"{float(v.iloc[-1]):.6f}")


def test_zones_no_lookahead():
    print("[2] Zones basse/haute sur série calme→tempête (1D, fenêtre 365)")
    rng = np.random.default_rng(11)
    # 600 jours : 500 calmes (σ=0,5 %) puis 100 agités (σ=4 %)
    rets = np.concatenate([rng.normal(0, 0.005, 500), rng.normal(0, 0.04, 100)])
    close = 30_000 * np.exp(np.cumsum(rets))
    df = klines_from_close(close, tf_ms=86_400_000)
    labels, current = vol_zone_labels(df, "1D")
    check("zone courante = haute (tempête récente)", current == "haute", current)
    # Discriminant : la tempête (σ ×8) doit être étiquetée « haute » massivement
    # vs son année écoulée calme. (Dans une période homogène, un percentile
    # RELATIF répartit ~1/3 par zone : c'est la définition du §4, pas un défaut.)
    storm = labels[520:600]
    known_storm = storm[storm != "inconnu"]
    check("tempête massivement « haute »",
          len(known_storm) > 0
          and (known_storm == "haute").sum() / len(known_storm) > 0.8,
          f"{(known_storm == 'haute').sum()}/{len(known_storm)}")
    check("début sans historique → inconnu", (labels[:60] == "inconnu").all())


def test_activation_rule():
    print("[3] Activation case par case (n ≥ 200 par sous-case, §4)")
    rng = np.random.default_rng(7)
    # 3 ans de 1h : vol alternant par blocs de ~1 mois → chaque zone bien peuplée
    n = 3 * 8760
    sig = np.where((np.arange(n) // 720) % 3 == 0, 0.001,
                   np.where((np.arange(n) // 720) % 3 == 1, 0.004, 0.012))
    close = 30_000 * np.exp(np.cumsum(rng.normal(0, 1, n) * sig))
    df = klines_from_close(close, tf_ms=3_600_000)
    daily_ref = daily_sma200_ref(klines_from_close(close[::24], tf_ms=86_400_000))
    t = compute_timeframe_all_states(df, "1h", daily_ref, CONFIG.fee_taker)
    check("zone courante exposée", t.get("vol_zone_courante") in
          ("basse", "moyenne", "haute"), t.get("vol_zone_courante"))
    actives = [s for s, b in t["etats"].items() if b.get("vol_active")]
    inactives = [s for s, b in t["etats"].items() if not b.get("vol_active")]
    check("au moins une case active la dimension", len(actives) > 0, str(actives))
    ok_rule = all(
        all(z["n_etat"] >= 200 for z in t["etats"][s]["vol_zones"].values())
        for s in actives)
    check("chaque sous-case active a n ≥ 200", ok_rule)
    ok_motif = all("vol_sous_cases_n" in t["etats"][s] for s in inactives)
    check("cases inactives exposent leurs n de sous-cases", ok_motif)
    if actives:
        zb = next(iter(t["etats"][actives[0]]["vol_zones"].values()))
        b = zb["long"]["barrieres"][0]
        check("sous-case : mesure complète (p̂, n, Wilson, EV, coûts)",
              all(k in b for k in ("p_hat", "n", "wilson", "ev_nette",
                                   "ev_nette_prudente")))


def main():
    print(f"Répertoire de test : {_TMP}\n")
    test_ewma_formula()
    test_zones_no_lookahead()
    test_activation_rule()
    print(f"\nRésultat : {PASS} réussis, {FAIL} échoués")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
