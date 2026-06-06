"""Donnees de marche en temps reel pour Bybit et Bitget (perpetuels USDT).

Recupere, par exchange et par symbole, via les endpoints PUBLICS (aucune cle) :
- prix, variation 24h, funding ;
- open interest (+ variation) ;
- ratio long/short des comptes (% long, % short, ratio) ;
- CVD (Cumulative Volume Delta) approxime a partir des bougies ;
- volume 24h.

Note : le CVD est ici une APPROXIMATION calculee depuis les bougies (position
de la cloture dans la meche x volume). Un vrai CVD necessite le detail des
trades agressifs, non fourni par ces endpoints publics.
"""

import logging
import time

import requests

from . import config

log = logging.getLogger("portfolio.markets")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0"})

BITGET = "https://api.bitget.com"

# Memoire du dernier OI vu (pour estimer la variation cote Bitget).
_prev_oi = {}

# Memoire de l'hote Bybit qui repond (evite de retester les hotes morts).
_bybit_host = {"ok": None}


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _cvd_from_candles(candles, period=14):
    """CVD approxime : somme sur 'period' bougies de la pression d'achat/vente.

    candles : liste de dicts {high, low, close, volume}, ordre ascendant.
    Renvoie (valeur_cvd, direction 'BUY'/'SELL') ou (None, None).
    """
    if len(candles) < period:
        return None, None
    deltas = []
    for c in candles[-period:]:
        rng = c["high"] - c["low"]
        if rng == 0:
            deltas.append(0.0)
            continue
        buy = (c["close"] - c["low"]) / rng
        sell = (c["high"] - c["close"]) / rng
        deltas.append((buy - sell) * c["volume"])
    cvd = sum(deltas)
    return cvd, ("BUY" if cvd >= 0 else "SELL")


# --------------------------------------------------------------------------- #
# Bybit
# --------------------------------------------------------------------------- #
def _bybit_list(path, params):
    """GET Bybit avec bascule automatique entre hotes (bybit.com -> bytick.com)."""
    hosts = list(config.BYBIT_HOSTS)
    # On essaie d'abord l'hote connu comme fonctionnel.
    if _bybit_host["ok"] in hosts:
        hosts.remove(_bybit_host["ok"])
        hosts.insert(0, _bybit_host["ok"])
    last_err = None
    for host in hosts:
        try:
            r = SESSION.get("https://" + host + path, params=params, timeout=12)
            r.raise_for_status()
            data = r.json()
            if str(data.get("retCode", 0)) not in ("0", "None"):
                raise ValueError(data.get("retMsg", "erreur Bybit"))
            _bybit_host["ok"] = host
            return data.get("result", {}).get("list", []) or []
        except (requests.RequestException, ValueError) as e:
            last_err = e
            continue
    raise last_err if last_err else RuntimeError("Bybit injoignable")


def bybit_market(symbol):
    row = {"exchange": "Bybit", "symbol": symbol, "error": None}
    cat = {"category": "linear", "symbol": symbol}
    try:
        tk = _bybit_list("/v5/market/tickers", cat)
        t = tk[0] if tk else {}
        row["price"] = _f(t.get("lastPrice"))
        row["change_24h"] = _f(t.get("price24hPcnt")) * 100
        row["funding"] = _f(t.get("fundingRate"))
        row["oi"] = _f(t.get("openInterest"))
        row["oi_usd"] = _f(t.get("openInterestValue"))
        row["volume_24h_usd"] = _f(t.get("turnover24h"))

        oi_hist = _bybit_list("/v5/market/open-interest",
                              {**cat, "intervalTime": "5min", "limit": 2})
        if len(oi_hist) >= 2:
            now_oi = _f(oi_hist[0].get("openInterest"))
            prev_oi = _f(oi_hist[1].get("openInterest"))
            row["oi_change"] = ((now_oi - prev_oi) / prev_oi * 100) if prev_oi else None
        else:
            row["oi_change"] = None

        ar = _bybit_list("/v5/market/account-ratio",
                         {**cat, "period": "5min", "limit": 1})
        if ar:
            row["long_pct"] = _f(ar[0].get("buyRatio")) * 100
            row["short_pct"] = _f(ar[0].get("sellRatio")) * 100
            row["ls_ratio"] = (row["long_pct"] / row["short_pct"]) if row["short_pct"] else None
        else:
            row["long_pct"] = row["short_pct"] = row["ls_ratio"] = None

        kl = _bybit_list("/v5/market/kline", {**cat, "interval": "5", "limit": 50})
        candles = []
        for c in reversed(kl):  # Bybit renvoie du plus recent au plus ancien
            candles.append({"high": _f(c[2]), "low": _f(c[3]),
                            "close": _f(c[4]), "volume": _f(c[5])})
        row["cvd"], row["cvd_dir"] = _cvd_from_candles(candles)
    except (requests.RequestException, ValueError, IndexError, KeyError) as e:
        log.warning("Bybit %s : %s", symbol, e)
        row["error"] = str(e)
    return row


# --------------------------------------------------------------------------- #
# Bitget
# --------------------------------------------------------------------------- #
def _bitget_get(path, params):
    r = SESSION.get(BITGET + path, params=params, timeout=12)
    r.raise_for_status()
    return r.json().get("data")


def bitget_market(symbol):
    row = {"exchange": "Bitget", "symbol": symbol, "error": None}
    pt = {"symbol": symbol, "productType": "usdt-futures"}
    try:
        data = _bitget_get("/api/v2/mix/market/ticker", pt)
        t = data[0] if data else {}
        row["price"] = _f(t.get("lastPr"))
        row["change_24h"] = _f(t.get("change24h")) * 100
        row["funding"] = _f(t.get("fundingRate"))
        row["oi"] = _f(t.get("holdingAmount"))
        row["oi_usd"] = row["oi"] * (row["price"] or _f(t.get("markPrice")))
        row["volume_24h_usd"] = _f(t.get("usdtVolume"))

        # Pas d'historique d'OI public chez Bitget : on estime la variation
        # par rapport au dernier OI vu lors d'un precedent rafraichissement.
        key = ("bitget", symbol)
        prev = _prev_oi.get(key)
        if prev and prev > 0:
            row["oi_change"] = (row["oi"] - prev) / prev * 100
        else:
            row["oi_change"] = None
        _prev_oi[key] = row["oi"]

        ls = _bitget_get("/api/v2/mix/market/account-long-short",
                         {**pt, "period": "5m"})
        if ls:
            last = ls[-1]  # le plus recent
            row["long_pct"] = _f(last.get("longAccountRatio")) * 100
            row["short_pct"] = _f(last.get("shortAccountRatio")) * 100
            row["ls_ratio"] = _f(last.get("longShortAccountRatio")) or None
        else:
            row["long_pct"] = row["short_pct"] = row["ls_ratio"] = None

        candles_raw = _bitget_get("/api/v2/mix/market/candles",
                                  {**pt, "granularity": "5m", "limit": "50"})
        candles = []
        for c in (candles_raw or []):  # Bitget renvoie du plus ancien au plus recent
            candles.append({"high": _f(c[2]), "low": _f(c[3]),
                            "close": _f(c[4]), "volume": _f(c[5])})
        row["cvd"], row["cvd_dir"] = _cvd_from_candles(candles)
    except (requests.RequestException, ValueError, IndexError, KeyError) as e:
        log.warning("Bitget %s : %s", symbol, e)
        row["error"] = str(e)
    return row


_FETCHERS = {"bybit": bybit_market, "bitget": bitget_market}
_cache = {"data": None, "ts": 0}


def build_markets():
    rows = []
    errors = []
    for symbol in config.MARKET_SYMBOLS:
        for ex in config.MARKET_EXCHANGES:
            fetch = _FETCHERS.get(ex)
            if not fetch:
                continue
            row = fetch(symbol)
            rows.append(row)
            if row.get("error"):
                errors.append(row["exchange"] + " " + symbol + " : " + row["error"])
    return {"rows": rows, "errors": errors}


def get_markets(force=False):
    now = time.time()
    if not force and _cache["data"] and (now - _cache["ts"] < config.CACHE_TTL):
        return _cache["data"]
    data = build_markets()
    _cache["data"] = data
    _cache["ts"] = now
    return data
