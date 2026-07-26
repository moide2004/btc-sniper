"""Banc de vérification — BOT 3 laboratoire VuManChu/WaveTrend. SANS réseau.

Démontre : WaveTrend fini après warmup et centré ; détection SANS look-ahead
(entrée = open suivant) ; cohérence tendance/zone (long ⇒ MM20>MM50 et wt1<−53
au signal) ; recompute_backtest3 → kv backtest3 avec profils rr ; scan live →
état marché + ticket bot=3 idempotent ; séparation des tickets Bot 1/2/3.

Lancer :  python tests/test_bot3.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="botdes_b3_")
os.environ["MOTEUR_DB"] = str(Path(_TMP) / "moteur.db")
os.environ["OHLCV_DIR"] = str(Path(_TMP) / "ohlcv")
os.environ["LOG_DIR"] = str(Path(_TMP) / "logs")
os.environ["BACKUP_DIR"] = str(Path(_TMP) / "backups")
os.environ["SYMBOLS"] = "BTCUSDT"
os.environ["RR_GRID"] = "1.0,1.5"
os.environ["VMC_TIMEFRAMES"] = "1h"
os.environ["VMC_OS_LEVEL"] = "10"   # seuil abaissé pour EXERCER la mécanique (prod : 53)
os.environ["FLASK_SECRET_KEY"] = "test-secret-key"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from core.config import CONFIG  # noqa: E402
from core.data_source import MINUTE_MS, TF_MS, resample_1m, save_ohlcv_atomic  # noqa: E402
from core.engine import bot3_live_scan, recompute_backtest3  # noqa: E402
from core.store import Store  # noqa: E402
from core.wavetrend import (  # noqa: E402
    WaveTrendParams, detect_wavetrend_setups, latest_wavetrend_setup, wavetrend,
)

CONFIG.ensure_dirs()
PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  {'✓' if cond else '✗'} {name}" + (f" — {detail}" if detail else ""))


def synth_1m(n=60 * 1440, base=60000, seed=11):
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    close = base * (1 + 0.4 * t / n) + base * 0.06 * np.sin(t / 2200) \
        + np.cumsum(rng.normal(0, base * 0.0006, n))
    op = np.concatenate([[close[0]], close[:-1]])
    hi = np.maximum(op, close) + np.abs(rng.normal(0, base * 0.0008, n))
    lo = np.minimum(op, close) - np.abs(rng.normal(0, base * 0.0008, n))
    ot = 1_600_000_000_000 + t * MINUTE_MS
    return pd.DataFrame({"open_time": ot, "open": op, "high": hi, "low": lo,
                         "close": close, "volume": 1.0, "close_time": ot + MINUTE_MS - 1})


def main():
    print(f"Répertoire de test : {_TMP}\n")
    df_1m = synth_1m()
    save_ohlcv_atomic(df_1m, "BTCUSDT", "1m")
    df_tf = resample_1m(df_1m, "1h")

    print("[1] Indicateur WaveTrend")
    wt1, wt2 = wavetrend(df_tf)
    ok_fin = np.isfinite(wt1[60:]).all() and np.isfinite(wt2[60:]).all()
    check("wt1/wt2 finis après warmup", bool(ok_fin))
    check("oscillateur centré (moyenne |wt1| raisonnable)",
          abs(float(np.nanmean(wt1[60:]))) < 60, f"{np.nanmean(wt1[60:]):.1f}")

    print("[2] Détection VuManChu")
    params = WaveTrendParams()
    setups = detect_wavetrend_setups(df_tf, params)
    check("des setups détectés", len(setups) > 0, f"{len(setups)} setups")
    close = df_tf["close"].to_numpy("float64")
    ma_f = pd.Series(close).rolling(params.ma_fast).mean().to_numpy()
    ma_s = pd.Series(close).rolling(params.ma_slow).mean().to_numpy()
    check("long ⇒ tendance haussière ET wt1 < −seuil au signal",
          all((ma_f[s.idx] > ma_s[s.idx] and wt1[s.idx] < -params.os_level)
              for s in setups if s.direction == "long"))
    check("short ⇒ tendance baissière ET wt1 > +seuil au signal",
          all((ma_f[s.idx] < ma_s[s.idx] and wt1[s.idx] > params.os_level)
              for s in setups if s.direction == "short"))
    opens = df_tf["open"].to_numpy("float64")
    non_proxy = [s for s in setups if not s.entry_is_proxy]
    check("entrée = OPEN de la bougie suivante (zéro look-ahead)",
          all(abs(s.entry - opens[s.idx + 1]) < 1e-9 for s in non_proxy))

    print("[3] Backtest Bot 3 + scan live + séparation")
    st = Store()
    res = recompute_backtest3(st)
    check("flux générés", res["n_fluxes"] == 2, str(res))
    bt = json.loads(st.get_kv("backtest3"))
    check("profils rr + stratégie documentée",
          set(bt["profiles"].keys()) == {"1.00", "1.50"}
          and bt["strategie"].startswith("vumanchu"))

    s_last = setups[-1]
    df_cut = df_1m[df_1m["open_time"] <= s_last.signal_close_ms].reset_index(drop=True)
    save_ohlcv_atomic(df_cut, "BTCUSDT", "1m")
    lat = latest_wavetrend_setup(resample_1m(df_cut, "1h"), params)
    check("latest retrouve le signal (proxy)",
          lat is not None and lat.direction == s_last.direction and lat.entry_is_proxy)
    now = s_last.signal_open_ms + TF_MS["1h"] + 60_000
    n_t = bot3_live_scan(st, now)
    check("1 ticket Bot 3 émis puis idempotent",
          n_t == 1 and bot3_live_scan(st, now) == 0)
    mkt = json.loads(st.get_kv("bot3_market"))
    check("état marché stocké (wt1/wt2/tendance)",
          len(mkt["states"]) >= 1 and "wt1" in mkt["states"][0])
    st.close()

    from web.app import app
    app.config["TESTING"] = True
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["auth"] = True
    j3 = c.get("/api/bot3").get_json()
    check("/api/bot3 : backtest + ticket bot=3",
          j3["backtest"]["strategie"].startswith("vumanchu")
          and len(j3["tickets"]) == 1 and j3["tickets"][0]["bot"] == "3")
    check("tickets Bot 1 non pollués (aucun bot marqué)",
          all(not t.get("bot") for t in c.get("/api/tickets").get_json()["tickets"]))
    check("tickets Bot 2 non pollués par le 3",
          all(t.get("bot") == "2" for t in c.get("/api/bot2").get_json()["tickets"]))

    print(f"\nRésultat : {PASS} réussis, {FAIL} échoués")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
