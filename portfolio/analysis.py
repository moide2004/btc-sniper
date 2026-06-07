"""Analyse de marche "BTC Sniper" pour affichage dans le tableau de bord.

Reutilise le cerveau du bot (btc_sniper.py) : structure multi-timeframe,
order blocks, FVG, CVD, RSI, funding, OI, score de conviction et niveaux
SL/TP. Renvoie un dictionnaire de chiffres precis a afficher.

Robustesse : les bougies sont recuperees sur Bybit, avec repli automatique
sur Bitget si Bybit est bloque (Bitget fonctionne partout).
"""

import logging
import os
import sys
import time

from . import config, markets

# Rend btc_sniper.py (a la racine du projet) importable.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import btc_sniper as bs  # noqa: E402

log = logging.getLogger("portfolio.analysis")

SYMBOL = "BTCUSDT"
_cache = {"data": None, "ts": 0}


def _to_candle(c, i_open=1):
    return {"open": float(c[i_open]), "high": float(c[i_open + 1]),
            "low": float(c[i_open + 2]), "close": float(c[i_open + 3]),
            "volume": float(c[i_open + 4])}


def _bybit_candles(interval, limit):
    raw = markets._bybit_list("/v5/market/kline",
                              {"category": "linear", "symbol": SYMBOL,
                               "interval": str(interval), "limit": limit})
    candles = [_to_candle(c) for c in raw]
    candles.reverse()  # Bybit : du plus recent au plus ancien
    return candles


def _bitget_candles(granularity, limit):
    data = markets._bitget_get("/api/v2/mix/market/candles",
                               {"symbol": SYMBOL, "productType": "usdt-futures",
                                "granularity": granularity, "limit": str(limit)})
    return [_to_candle(c) for c in (data or [])]  # Bitget : deja ascendant


def _load_candles():
    """Renvoie (c_1w, c_1d, c_4h, source) en essayant Bybit puis Bitget."""
    try:
        c1w = _bybit_candles("W", 52)
        c1d = _bybit_candles("D", 200)
        c4h = _bybit_candles("240", 200)
        if c1w and c1d and c4h:
            return c1w, c1d, c4h, "Bybit"
    except Exception as e:  # bloque / indisponible -> repli Bitget
        log.info("Bybit indisponible pour l'analyse, repli Bitget : %s", e)
    c1w = _bitget_candles("1W", 52)
    c1d = _bitget_candles("1D", 200)
    c4h = _bitget_candles("4H", 200)
    return c1w, c1d, c4h, "Bitget"


def build_analysis():
    out = {"symbol": SYMBOL, "error": None}
    try:
        c_1w, c_1d, c_4h, source = _load_candles()
        if not c_1w or not c_1d or not c_4h:
            out["error"] = "Bougies indisponibles"
            return out

        # Donnees de marche (prix, funding, variation d'OI) depuis la meme source.
        mrow = markets.bybit_market(SYMBOL) if source == "Bybit" else markets.bitget_market(SYMBOL)
        price = mrow.get("price") or c_4h[-1]["close"]
        funding = mrow.get("funding") or 0
        oi_change = mrow.get("oi_change") or 0

        # Indicateurs (memes parametres que le bot).
        atr_4h = bs.calc_atr(c_4h)
        atr_1d = bs.calc_atr(c_1d)
        rsi_1w = bs.calc_rsi(c_1w)
        rsi_1d = bs.calc_rsi(c_1d)
        rsi_4h = bs.calc_rsi(c_4h)
        struct_1w, dir_1w = bs.calc_structure(c_1w, 20)
        struct_1d, dir_1d = bs.calc_structure(c_1d, 30)
        struct_4h, dir_4h = bs.calc_structure(c_4h, 30)
        ob_bull_1d, ob_bear_1d, ob_bull_sig, ob_bear_sig = bs.calc_order_blocks(c_1d, 30)
        fvg_bull_1d, fvg_bear_1d = bs.calc_fvg(c_1d, 15)
        cvd_trend_4h, cvd_div_4h = bs.calc_cvd(c_4h, 14)
        cvd_trend_1d, cvd_div_1d = bs.calc_cvd(c_1d, 14)
        poc, vah, val = bs.calc_vp(c_1d, 20)

        score, direction, conviction, reasons = bs.calc_score(
            struct_1w, dir_1w, struct_1d, dir_1d, struct_4h, dir_4h,
            ob_bull_sig, ob_bear_sig, fvg_bull_1d, fvg_bear_1d,
            cvd_trend_4h, cvd_div_4h, cvd_trend_1d, cvd_div_1d,
            poc, vah, val, rsi_1w, rsi_1d, rsi_4h,
            funding, funding, oi_change, price,
        )

        sl, tp1, tp2, rr = bs.calc_sl_tp(
            direction, price, atr_4h, atr_1d,
            ob_bull_1d, ob_bear_1d, fvg_bull_1d, fvg_bear_1d,
            val, vah, poc, bs.MIN_RR,
        )

        # Conditions d'un vrai signal (comme dans le bot).
        aligned = not ((direction == "LONG" and dir_1w < 0)
                       or (direction == "SHORT" and dir_1w > 0))
        has_signal = (score >= bs.MIN_SCORE and conviction != "INSUFFISANTE"
                      and rr is not None and rr >= bs.MIN_RR and aligned)

        out.update({
            "source": source,
            "price": price,
            "score": score,
            "direction": direction,
            "conviction": conviction,
            "reasons": reasons[:6],
            "struct_1w": struct_1w, "struct_1d": struct_1d, "struct_4h": struct_4h,
            "rsi_1w": rsi_1w, "rsi_1d": rsi_1d, "rsi_4h": rsi_4h,
            "funding": funding, "oi_change": oi_change,
            "cvd_4h": cvd_trend_4h, "cvd_div_4h": cvd_div_4h,
            "cvd_1d": cvd_trend_1d, "cvd_div_1d": cvd_div_1d,
            "poc": poc, "vah": vah, "val": val,
            "sl": sl, "tp1": tp1, "tp2": tp2, "rr": rr,
            "min_score": bs.MIN_SCORE, "min_rr": bs.MIN_RR,
            "aligned": aligned, "has_signal": has_signal,
        })
    except Exception as e:
        log.warning("Analyse indisponible : %s", e)
        out["error"] = str(e)
    return out


def get_analysis(force=False):
    now = time.time()
    if not force and _cache["data"] and (now - _cache["ts"] < config.CACHE_TTL):
        return _cache["data"]
    data = build_analysis()
    _cache["data"] = data
    _cache["ts"] = now
    return data
