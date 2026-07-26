"""Banc de vérification — BOT 2 laboratoire trend-pullback. SANS réseau.

Démontre : RSI/Stoch RSI bornés et finis après warmup ; détection SANS
look-ahead (entrée = open de la bougie suivante) ; cohérence tendance/direction
(longs seulement si MM20>MM50 au signal) ; SL du bon côté ; rejet stop trop
large ; recompute_backtest2 → kv backtest2 avec profils rr + bilan + endpoint.

Lancer :  python tests/test_bot2.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="botdes_b2_")
os.environ["MOTEUR_DB"] = str(Path(_TMP) / "moteur.db")
os.environ["OHLCV_DIR"] = str(Path(_TMP) / "ohlcv")
os.environ["LOG_DIR"] = str(Path(_TMP) / "logs")
os.environ["BACKUP_DIR"] = str(Path(_TMP) / "backups")
os.environ["SYMBOLS"] = "BTCUSDT"
os.environ["RR_GRID"] = "1.0,1.5"
os.environ["PB_TIMEFRAMES"] = "1h"
os.environ["FLASK_SECRET_KEY"] = "test-secret-key"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from core.config import CONFIG  # noqa: E402
from core.data_source import MINUTE_MS, resample_1m, save_ohlcv_atomic  # noqa: E402
from core.engine import recompute_backtest2  # noqa: E402
from core.pullback import (  # noqa: E402
    PullbackParams, detect_pullback_setups, rsi_wilder, stoch_rsi_k,
)
from core.store import Store  # noqa: E402

CONFIG.ensure_dirs()
PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    PASS += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  {'✓' if cond else '✗'} {name}" + (f" — {detail}" if detail else ""))


def synth_1m(n=60 * 1440, base=60000, seed=5):
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    close = base * (1 + 0.5 * t / n) + base * 0.05 * np.sin(t / 1800) \
        + np.cumsum(rng.normal(0, base * 0.0005, n))
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

    print("[1] Indicateurs")
    close = df_tf["close"].to_numpy("float64")
    r = rsi_wilder(close)
    k = stoch_rsi_k(close)
    check("RSI borné [0,100]", np.nanmin(r) >= 0 and np.nanmax(r) <= 100)
    kk = k[~np.isnan(k)]
    check("Stoch RSI %K borné [0,100] et fini après warmup",
          kk.size > 0 and kk.min() >= 0 and kk.max() <= 100)

    print("[2] Détection trend-pullback")
    params = PullbackParams()
    setups = detect_pullback_setups(df_tf, params)
    check("des setups détectés", len(setups) > 0, f"{len(setups)} setups")
    ma_f = pd.Series(close).rolling(params.ma_fast).mean().to_numpy()
    ma_s = pd.Series(close).rolling(params.ma_slow).mean().to_numpy()
    ok_trend = all((s.direction == "long") == (ma_f[s.idx] > ma_s[s.idx]) for s in setups)
    check("direction cohérente avec la tendance MM au signal", ok_trend)
    opens = df_tf["open"].to_numpy("float64")
    ots = df_tf["open_time"].to_numpy("int64")
    non_proxy = [s for s in setups if not s.entry_is_proxy]
    check("entrée = OPEN de la bougie suivante (zéro look-ahead)",
          all(abs(s.entry - opens[s.idx + 1]) < 1e-9
              and s.entry_time_ms == int(ots[s.idx + 1]) for s in non_proxy))
    check("SL du bon côté + stop borné",
          all(((s.sl < s.entry) if s.direction == "long" else (s.sl > s.entry))
              and s.stop_dist <= params.stop_max_atr * s.atr + 1e-9 for s in setups))

    print("[3] Backtest Bot 2 + endpoint")
    st = Store()
    res = recompute_backtest2(st)
    check("flux générés (1h × long/short)", res["n_fluxes"] == 2, str(res))
    bt = json.loads(st.get_kv("backtest2"))
    check("profils rr présents", set(bt["profiles"].keys()) == {"1.00", "1.50"})
    check("stratégie et params documentés",
          "pullback" in bt["strategie"] and bt["params"]["ma_fast"] == 20)
    check("verdicts posés sur chaque flux",
          all("verdict" in f for f in bt["fluxes"]))
    st.close()

    from web.app import app
    app.config["TESTING"] = True
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["auth"] = True
    j = c.get("/api/bot2").get_json()
    check("/api/bot2 renvoie le backtest", j["backtest"]["strategie"].startswith("trend-pullback"))

    print(f"\nRésultat : {PASS} réussis, {FAIL} échoués")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
