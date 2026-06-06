"""BTC Sniper - bot d'alertes de trading pour BTCUSDT (perp Bybit).

Analyse multi-timeframe (1W / 1D / 4H) avec confluence d'indicateurs
(structure de marche, order blocks, FVG, CVD, volume profile, RSI, funding,
open interest), calcule un score de conviction et envoie les signaux
(entree / SL / TP1 / TP2) sur Telegram. Le bot suit ensuite la position
ouverte et notifie quand TP1, TP2 ou SL sont atteints.

NB : ce script envoie uniquement des alertes, il ne passe aucun ordre.

Configuration via variables d'environnement (voir .env.example) :
    TELEGRAM_TOKEN   token du bot Telegram (obligatoire)
    TELEGRAM_CHATID  id du chat destinataire (obligatoire)
    SYMBOL           paire (defaut BTCUSDT)
    SCAN_INTERVAL    secondes entre deux scans (defaut 14400 = 4h)
    MIN_SCORE        score minimum pour un signal (defaut 82)
    MIN_RR           ratio risque/recompense minimum (defaut 3.0)
    RISK_PCT         risque par trade en % pour l'affichage (defaut 3.0)
"""

import logging
import os
import sys
import time
from datetime import datetime

import requests

try:
    from zoneinfo import ZoneInfo  # Python 3.9+ (stdlib, pas de dependance)
except ImportError:  # pragma: no cover - fallback pour les vieux Python
    from backports.zoneinfo import ZoneInfo

# Charge automatiquement les secrets depuis un fichier .env s'il existe
# (et si python-dotenv est installe). Le .env n'est jamais versionne.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _env_float(name, default):
    """Lit une variable d'environnement et la convertit en float."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logging.warning("Variable %s invalide (%r), valeur par defaut %s utilisee",
                        name, raw, default)
        return default


TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHATID = os.environ.get("TELEGRAM_CHATID", "")
PARIS_TZ = ZoneInfo("Europe/Paris")
SYMBOL = os.environ.get("SYMBOL", "BTCUSDT")
SCAN_INTERVAL = int(_env_float("SCAN_INTERVAL", 14400))
MIN_SCORE = _env_float("MIN_SCORE", 82)
MIN_RR = _env_float("MIN_RR", 3.0)
RISK_PCT = _env_float("RISK_PCT", 3.0)

BYBIT_BASE = "https://api.bybit.com"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%d/%m %H:%M:%S",
)
log = logging.getLogger("btc_sniper")

# Une seule session HTTP reutilisee pour toutes les requetes (connexions
# persistantes -> plus rapide et plus robuste).
SESSION = requests.Session()


# --------------------------------------------------------------------------- #
# Communication / recuperation des donnees
# --------------------------------------------------------------------------- #
def send_telegram(msg):
    """Envoie un message texte via l'API Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHATID:
        log.warning("Telegram non configure - message non envoye :\n%s", msg)
        return
    url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
    try:
        r = SESSION.post(
            url,
            json={"chat_id": TELEGRAM_CHATID, "text": msg},
            timeout=10,
        )
        log.info("[TG] %s", r.status_code)
    except requests.RequestException as e:
        log.error("[TG ERROR] %s", e)


def _bybit_get(path, params, timeout=15):
    """Appel GET sur l'API Bybit, renvoie la liste 'result.list' ou []."""
    try:
        r = SESSION.get(BYBIT_BASE + path, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json().get("result", {}).get("list", [])
    except (requests.RequestException, ValueError) as e:
        log.error("[BYBIT ERROR] %s -> %s", path, e)
        return []


def get_candles(interval, limit=100):
    """Recupere les bougies Bybit, ordre chronologique ascendant."""
    raw = _bybit_get(
        "/v5/market/kline",
        {"category": "linear", "symbol": SYMBOL, "interval": interval, "limit": limit},
    )
    candles = []
    for c in raw:
        try:
            candles.append({
                "open":   float(c[1]),
                "high":   float(c[2]),
                "low":    float(c[3]),
                "close":  float(c[4]),
                "volume": float(c[5]),
            })
        except (ValueError, IndexError, TypeError):
            continue
    candles.reverse()
    return candles


def get_market_data():
    """Prix courant + taux de funding."""
    raw = _bybit_get(
        "/v5/market/tickers",
        {"category": "linear", "symbol": SYMBOL},
        timeout=10,
    )
    if not raw:
        return {}
    t = raw[0]
    try:
        return {
            "price":   float(t.get("lastPrice", 0)),
            "funding": float(t.get("fundingRate", 0)),
        }
    except (ValueError, TypeError):
        return {}


def get_oi():
    """Open Interest courant + variation en % sur la derniere periode 4h."""
    raw = _bybit_get(
        "/v5/market/open-interest",
        {"category": "linear", "symbol": SYMBOL, "intervalTime": "4h", "limit": 3},
        timeout=10,
    )
    if len(raw) >= 2:
        try:
            oi_now = float(raw[0].get("openInterest", 0))
            oi_prev = float(raw[1].get("openInterest", 0))
        except (ValueError, TypeError):
            return 0, 0
        change = ((oi_now - oi_prev) / oi_prev * 100) if oi_prev > 0 else 0
        return oi_now, round(change, 3)
    return 0, 0


def get_funding_history():
    """Dernier funding + moyenne sur les 6 dernieres valeurs."""
    raw = _bybit_get(
        "/v5/market/funding/history",
        {"category": "linear", "symbol": SYMBOL, "limit": 6},
        timeout=10,
    )
    if raw:
        try:
            rates = [float(x.get("fundingRate", 0)) for x in raw]
        except (ValueError, TypeError):
            return 0, 0
        avg = sum(rates) / len(rates)
        return round(rates[0], 6), round(avg, 6)
    return 0, 0


# --------------------------------------------------------------------------- #
# Indicateurs techniques
# --------------------------------------------------------------------------- #
def calc_ema(candles, period):
    if len(candles) < period:
        return None
    closes = [c["close"] for c in candles]
    k = 2.0 / (period + 1)
    e = sum(closes[:period]) / period
    for p in closes[period:]:
        e = p * k + e * (1 - k)
    return round(e, 2)


def calc_atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, period + 1):
        c = candles[-i]
        p = candles[-i - 1]
        trs.append(max(
            c["high"] - c["low"],
            abs(c["high"] - p["close"]),
            abs(c["low"] - p["close"]),
        ))
    return sum(trs) / period


def calc_rsi(candles, period=14):
    if len(candles) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        d = candles[-i]["close"] - candles[-i - 1]["close"]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains) / period
    al = sum(losses) / period
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag / al), 2)


def calc_structure(candles, lookback=20):
    if len(candles) < lookback:
        return "UNDEFINED", 0
    mid = lookback // 2
    h = [c["high"] for c in candles[-lookback:]]
    l = [c["low"] for c in candles[-lookback:]]
    hf, hs = max(h[:mid]), max(h[mid:])
    lf, ls = min(l[:mid]), min(l[mid:])
    if hs > hf and ls > lf:
        return "UPTREND", 1
    if hs < hf and ls < lf:
        return "DOWNTREND", -1
    if hs > hf and ls < lf:
        return "CHoCH_BEAR", -2
    if hs < hf and ls > lf:
        return "CHoCH_BULL", 2
    return "RANGE", 0


def calc_order_blocks(candles, lookback=20):
    if len(candles) < lookback + 3:
        return None, None, None, None
    price = candles[-1]["close"]
    atr_val = calc_atr(candles)
    if not atr_val:
        return None, None, None, None
    ob_bull = None
    ob_bear = None
    for i in range(3, min(lookback, len(candles) - 2)):
        c = candles[-i]
        next2 = candles[-i + 1:-i + 3]
        if not next2:
            continue
        vol_ma = sum(cc["volume"] for cc in candles[-i - 3:-i]) / 3 if i >= 3 else 0
        if vol_ma == 0:
            continue
        if c["close"] < c["open"] and c["volume"] > vol_ma * 1.3:
            move_up = max(cc["high"] for cc in next2) - c["close"]
            if move_up > atr_val * 1.2:
                dist = abs(price - (c["high"] + c["low"]) / 2)
                if ob_bull is None or dist < abs(price - (ob_bull["high"] + ob_bull["low"]) / 2):
                    ob_bull = {"high": round(c["high"], 0), "low": round(c["low"], 0)}
        if c["close"] > c["open"] and c["volume"] > vol_ma * 1.3:
            move_dn = c["close"] - min(cc["low"] for cc in next2)
            if move_dn > atr_val * 1.2:
                dist = abs(price - (c["high"] + c["low"]) / 2)
                if ob_bear is None or dist < abs(price - (ob_bear["high"] + ob_bear["low"]) / 2):
                    ob_bear = {"high": round(c["high"], 0), "low": round(c["low"], 0)}
    ob_bull_sig = ob_bull if ob_bull and price >= ob_bull["low"] and price <= ob_bull["high"] + atr_val else None
    ob_bear_sig = ob_bear if ob_bear and price <= ob_bear["high"] and price >= ob_bear["low"] - atr_val else None
    return ob_bull, ob_bear, ob_bull_sig, ob_bear_sig


def calc_fvg(candles, lookback=10):
    if len(candles) < lookback:
        return [], []
    price = candles[-1]["close"]
    fvg_bull = []
    fvg_bear = []
    for i in range(2, min(lookback, len(candles) - 1)):
        c1 = candles[-i - 1]
        c3 = candles[-i + 1]
        if c3["low"] > c1["high"]:
            fvg_bull.append({"top": round(c3["low"], 0), "bot": round(c1["high"], 0)})
        if c3["high"] < c1["low"]:
            fvg_bear.append({"top": round(c1["low"], 0), "bot": round(c3["high"], 0)})
    fvg_bull_a = [f for f in fvg_bull if price >= f["bot"] and price <= f["top"]]
    fvg_bear_a = [f for f in fvg_bear if price >= f["bot"] and price <= f["top"]]
    return fvg_bull_a[:2], fvg_bear_a[:2]


def calc_cvd(candles, period=14):
    if len(candles) < period + 5:
        return None, None
    deltas = []
    for c in candles[-period:]:
        rng = c["high"] - c["low"]
        if rng == 0:
            deltas.append(0)
            continue
        br = (c["close"] - c["low"]) / rng
        sr = (c["high"] - c["close"]) / rng
        deltas.append((br - sr) * c["volume"])
    cvd_now = sum(deltas)
    cvd_prev = sum(deltas[:-5])
    price_up = candles[-1]["close"] > candles[-period]["close"]
    div = None
    if price_up and cvd_now < cvd_prev * 0.72:
        div = "BEARISH"
    elif not price_up and cvd_now > cvd_prev * 1.28:
        div = "BULLISH"
    return "BUY" if cvd_now > 0 else "SELL", div


def calc_vp(candles, bins=20):
    if len(candles) < 15:
        return None, None, None
    hi = max(c["high"] for c in candles)
    lo = min(c["low"] for c in candles)
    if hi == lo:
        return None, None, None
    bsz = (hi - lo) / bins
    profile = [0.0] * bins
    for c in candles:
        idx = max(0, min(bins - 1, int(((c["high"] + c["low"]) / 2 - lo) / bsz)))
        profile[idx] += c["volume"]
    pi = profile.index(max(profile))
    poc = round(lo + pi * bsz + bsz / 2, 0)
    tot = sum(profile)
    acc = 0
    iu = id_ = vi = vl = pi
    while acc < tot * 0.70:
        up = profile[iu + 1] if iu + 1 < bins else 0
        dn = profile[id_ - 1] if id_ - 1 >= 0 else 0
        if up >= dn and iu + 1 < bins:
            iu += 1
            acc += up
            vi = iu
        elif id_ - 1 >= 0:
            id_ -= 1
            acc += dn
            vl = id_
        else:
            break
    return poc, round(lo + vi * bsz + bsz, 0), round(lo + vl * bsz, 0)


# --------------------------------------------------------------------------- #
# Scoring & gestion du risque
# --------------------------------------------------------------------------- #
def calc_score(
    struct_1w, dir_1w,
    struct_1d, dir_1d,
    struct_4h, dir_4h,
    ob_bull_sig, ob_bear_sig,
    fvg_bull_1d, fvg_bear_1d,
    cvd_trend_4h, cvd_div_4h,
    cvd_trend_1d, cvd_div_1d,
    poc, vah, val,
    rsi_1w, rsi_1d, rsi_4h,
    funding, funding_avg,
    oi_change, price,
):
    lp = sp = tp = 0
    rl = []
    rs = []
    tp += 25
    if dir_1w == 2:
        lp += 25
        rl.append("CHoCH BULL 1W - retournement majeur haussier")
    elif dir_1w == 1:
        lp += 20
        rl.append("UPTREND 1W - tendance haussiere long terme")
    elif dir_1w == -2:
        sp += 25
        rs.append("CHoCH BEAR 1W - retournement majeur baissier")
    elif dir_1w == -1:
        sp += 20
        rs.append("DOWNTREND 1W - tendance baissiere long terme")
    else:
        lp += 12
        sp += 12
    tp += 20
    if dir_1d == 2:
        lp += 20
        rl.append("CHoCH BULL 1D - cassure de structure journaliere")
    elif dir_1d == 1:
        lp += 15
        rl.append("UPTREND 1D confirme")
    elif dir_1d == -2:
        sp += 20
        rs.append("CHoCH BEAR 1D - cassure de structure journaliere")
    elif dir_1d == -1:
        sp += 15
        rs.append("DOWNTREND 1D confirme")
    else:
        lp += 10
        sp += 10
    tp += 10
    if dir_4h == 2:
        lp += 10
        rl.append("CHoCH BULL 4H - confirmation entree")
    elif dir_4h == 1:
        lp += 7
    elif dir_4h == -2:
        sp += 10
        rs.append("CHoCH BEAR 4H - confirmation entree")
    elif dir_4h == -1:
        sp += 7
    else:
        lp += 5
        sp += 5
    tp += 15
    if ob_bull_sig:
        lp += 15
        rl.append("Order Block haussier 1D " + str(ob_bull_sig["low"]) + "-" + str(ob_bull_sig["high"]))
    elif ob_bear_sig:
        sp += 15
        rs.append("Order Block baissier 1D " + str(ob_bear_sig["low"]) + "-" + str(ob_bear_sig["high"]))
    else:
        lp += 7
        sp += 7
    tp += 10
    if fvg_bull_1d:
        lp += 10
        rl.append("FVG haussier 1D " + str(fvg_bull_1d[0]["bot"]) + "-" + str(fvg_bull_1d[0]["top"]))
    elif fvg_bear_1d:
        sp += 10
        rs.append("FVG baissier 1D " + str(fvg_bear_1d[0]["bot"]) + "-" + str(fvg_bear_1d[0]["top"]))
    else:
        lp += 5
        sp += 5
    tp += 10
    if cvd_div_4h == "BULLISH":
        lp += 10
        rl.append("CVD DIVERGENCE BULL 4H - smart money achete")
    elif cvd_div_4h == "BEARISH":
        sp += 10
        rs.append("CVD DIVERGENCE BEAR 4H - smart money vend")
    elif cvd_trend_4h == "BUY":
        lp += 6
    elif cvd_trend_4h == "SELL":
        sp += 6
    else:
        lp += 3
        sp += 3
    if cvd_div_1d == "BULLISH":
        tp += 5
        lp += 5
        rl.append("CVD DIVERGENCE BULL 1D - confirmation HTF")
    elif cvd_div_1d == "BEARISH":
        tp += 5
        sp += 5
        rs.append("CVD DIVERGENCE BEAR 1D - confirmation HTF")
    if poc and val and vah and price:
        tp += 5
        if abs(price - val) < abs(price - vah):
            lp += 5
            rl.append("Prix proche VAL " + str(val))
        else:
            sp += 5
            rs.append("Prix proche VAH " + str(vah))
    if rsi_1w:
        if rsi_1w < 30:
            tp += 5
            lp += 5
            rl.append("RSI 1W survendu " + str(rsi_1w))
        elif rsi_1w > 70:
            tp += 5
            sp += 5
            rs.append("RSI 1W sursachete " + str(rsi_1w))
    if rsi_1d:
        if rsi_1d < 32:
            tp += 3
            lp += 3
            rl.append("RSI 1D survendu " + str(rsi_1d))
        elif rsi_1d > 68:
            tp += 3
            sp += 3
            rs.append("RSI 1D sursachete " + str(rsi_1d))
    if abs(funding) > 0.002:
        tp += 5
        if funding < -0.003:
            lp += 5
            rl.append("Funding TRES NEGATIF " + str(round(funding * 100, 4)) + "%")
        elif funding < -0.001:
            lp += 3
        elif funding > 0.004:
            sp += 5
            rs.append("Funding TRES POSITIF " + str(round(funding * 100, 4)) + "%")
        elif funding > 0.002:
            sp += 3
    if abs(oi_change) > 2:
        tp += 3
        if oi_change > 5:
            if lp >= sp:
                lp += 3
                rl.append("OI hausse forte +" + str(round(oi_change, 1)) + "%")
            else:
                sp += 3
        elif oi_change < -5:
            if lp >= sp:
                lp += 2
            else:
                sp += 2
    if tp == 0:
        return 50, "LONG", "INSUFFISANTE", []
    ls = round((lp / tp) * 100)
    ss = round((sp / tp) * 100)
    if ls >= ss:
        score = ls
        direction = "LONG"
        reasons = rl
    else:
        score = ss
        direction = "SHORT"
        reasons = rs
    if score >= 90:
        conviction = "MAXIMALE"
    elif score >= 85:
        conviction = "TRES FORTE"
    elif score >= 82:
        conviction = "FORTE"
    else:
        conviction = "INSUFFISANTE"
    return score, direction, conviction, reasons


def calc_sl_tp(direction, price, atr_4h, atr_1d, ob_bull, ob_bear, fvg_bull, fvg_bear, val, vah, poc, min_rr=3.0):
    if not atr_4h or not atr_1d:
        return None, None, None, None
    if direction == "LONG":
        sl_opts = [price - atr_1d * 1.5]
        if val:
            sl_opts.append(val - atr_4h * 0.5)
        if ob_bull:
            sl_opts.append(ob_bull["low"] - atr_4h * 0.2)
        if fvg_bull:
            sl_opts.append(fvg_bull[0]["bot"] - atr_4h * 0.2)
        sl = round(max(sl_opts), 0)
        risk = price - sl
        if risk <= 0:
            sl = round(price - atr_1d * 1.5, 0)
            risk = atr_1d * 1.5
        tp1_opts = [price + risk * 2]
        if poc and poc > price:
            tp1_opts.append(poc)
        if vah and vah > price:
            tp1_opts.append(vah)
        tp1 = round(min(tp1_opts), 0)
        tp2_opts = [price + risk * min_rr]
        if ob_bear:
            tp2_opts.append(ob_bear["low"])
        if fvg_bear:
            tp2_opts.append(fvg_bear[0]["bot"])
        tp2 = round(min(tp2_opts), 0)
    else:
        sl_opts = [price + atr_1d * 1.5]
        if vah:
            sl_opts.append(vah + atr_4h * 0.5)
        if ob_bear:
            sl_opts.append(ob_bear["high"] + atr_4h * 0.2)
        if fvg_bear:
            sl_opts.append(fvg_bear[0]["top"] + atr_4h * 0.2)
        sl = round(min(sl_opts), 0)
        risk = sl - price
        if risk <= 0:
            sl = round(price + atr_1d * 1.5, 0)
            risk = atr_1d * 1.5
        tp1_opts = [price - risk * 2]
        if poc and poc < price:
            tp1_opts.append(poc)
        if val and val < price:
            tp1_opts.append(val)
        tp1 = round(max(tp1_opts), 0)
        tp2_opts = [price - risk * min_rr]
        if ob_bull:
            tp2_opts.append(ob_bull["high"])
        if fvg_bull:
            tp2_opts.append(fvg_bull[0]["top"])
        tp2 = round(max(tp2_opts), 0)
    reward = abs(price - tp2)
    rr_actual = round(reward / risk, 1) if risk > 0 else 0
    return sl, tp1, tp2, rr_actual


# --------------------------------------------------------------------------- #
# Boucle de scan
# --------------------------------------------------------------------------- #
active_signal = {
    "direction": None,
    "entry":     None,
    "sl":        None,
    "tp1":       None,
    "tp2":       None,
    "score":     None,
    "conviction": None,
    "time":      None,
    "tp1_hit":   False,
    "rr":        None,
}


def scan():
    global active_signal
    now = datetime.now(PARIS_TZ)
    hhmm = now.strftime("%d/%m %H:%M")
    log.info("Scan BTC Sniper...")
    market = get_market_data()
    if not market:
        log.warning("Donnees indisponibles")
        return
    price = market.get("price", 0)
    funding = market.get("funding", 0)
    c_1w = get_candles("W", 52)
    c_1d = get_candles("D", 200)
    c_4h = get_candles("240", 200)
    if not c_1w or not c_1d or not c_4h:
        log.warning("Donnees bougies insuffisantes")
        return
    atr_4h = calc_atr(c_4h)
    atr_1d = calc_atr(c_1d)
    rsi_1w = calc_rsi(c_1w)
    rsi_1d = calc_rsi(c_1d)
    rsi_4h = calc_rsi(c_4h)
    struct_1w, dir_1w = calc_structure(c_1w, 20)
    struct_1d, dir_1d = calc_structure(c_1d, 30)
    struct_4h, dir_4h = calc_structure(c_4h, 30)
    ob_bull_1d, ob_bear_1d, ob_bull_sig, ob_bear_sig = calc_order_blocks(c_1d, 30)
    fvg_bull_1d, fvg_bear_1d = calc_fvg(c_1d, 15)
    cvd_trend_4h, cvd_div_4h = calc_cvd(c_4h, 14)
    cvd_trend_1d, cvd_div_1d = calc_cvd(c_1d, 14)
    poc, vah, val = calc_vp(c_1d, 20)
    funding_now, funding_avg = get_funding_history()
    oi, oi_change = get_oi()
    score, direction, conviction, reasons = calc_score(
        struct_1w, dir_1w,
        struct_1d, dir_1d,
        struct_4h, dir_4h,
        ob_bull_sig, ob_bear_sig,
        fvg_bull_1d, fvg_bear_1d,
        cvd_trend_4h, cvd_div_4h,
        cvd_trend_1d, cvd_div_1d,
        poc, vah, val,
        rsi_1w, rsi_1d, rsi_4h,
        funding_now, funding_avg,
        oi_change, price,
    )
    log.info("%s %s%% %s", direction, score, conviction)
    log.info("1W:%s 1D:%s 4H:%s", struct_1w, struct_1d, struct_4h)
    if active_signal["direction"]:
        d = active_signal["direction"]
        if not active_signal["tp1_hit"]:
            if d == "LONG" and price >= active_signal["tp1"]:
                active_signal["tp1_hit"] = True
                send_telegram("TP1 ATTEINT\nLONG " + str(active_signal["entry"]) + " -> " + str(active_signal["tp1"]) + "\nFerme 50% SL au BE " + str(active_signal["entry"]) + "\nVise TP2 " + str(active_signal["tp2"]))
            elif d == "SHORT" and price <= active_signal["tp1"]:
                active_signal["tp1_hit"] = True
                send_telegram("TP1 ATTEINT\nSHORT " + str(active_signal["entry"]) + " -> " + str(active_signal["tp1"]) + "\nFerme 50% SL au BE " + str(active_signal["entry"]) + "\nVise TP2 " + str(active_signal["tp2"]))
        if active_signal["tp1_hit"]:
            if d == "LONG" and price >= active_signal["tp2"]:
                send_telegram("TP2 ATTEINT - TRADE GAGNANT\nLONG " + str(active_signal["entry"]) + " -> " + str(active_signal["tp2"]) + "\nR:R 1:" + str(active_signal["rr"]) + "\nRecherche prochain setup...")
                active_signal["direction"] = None
                return
            elif d == "SHORT" and price <= active_signal["tp2"]:
                send_telegram("TP2 ATTEINT - TRADE GAGNANT\nSHORT " + str(active_signal["entry"]) + " -> " + str(active_signal["tp2"]) + "\nR:R 1:" + str(active_signal["rr"]) + "\nRecherche prochain setup...")
                active_signal["direction"] = None
                return
        if d == "LONG" and price <= active_signal["sl"]:
            send_telegram("SL TOUCHE\nLONG stope a " + str(active_signal["sl"]) + "\nEntree: " + str(active_signal["entry"]) + "\nRecherche prochain setup...")
            active_signal["direction"] = None
            return
        elif d == "SHORT" and price >= active_signal["sl"]:
            send_telegram("SL TOUCHE\nSHORT stope a " + str(active_signal["sl"]) + "\nEntree: " + str(active_signal["entry"]) + "\nRecherche prochain setup...")
            active_signal["direction"] = None
            return
        if atr_4h and not active_signal["tp1_hit"]:
            if abs(price - active_signal["sl"]) < atr_4h * 0.5:
                send_telegram("DANGER SL PROCHE\nPrix: " + str(round(price, 0)) + " SL: " + str(active_signal["sl"]))
        opp = "SHORT" if d == "LONG" else "LONG"
        if score >= 85 and direction == opp:
            send_telegram("SIGNAL INVALIDE\nDirection inversee: " + opp + " " + str(score) + "%\nFerme la position manuellement")
            active_signal["direction"] = None
        return
    if score < MIN_SCORE or conviction == "INSUFFISANTE":
        log.info("Pas de signal (%s%%)", score)
        return
    if direction == "LONG" and dir_1w < 0:
        log.info("1W baissier - LONG refuse")
        return
    if direction == "SHORT" and dir_1w > 0:
        log.info("1W haussier - SHORT refuse")
        return
    sl, tp1, tp2, rr_actual = calc_sl_tp(
        direction, price, atr_4h, atr_1d,
        ob_bull_1d, ob_bear_1d,
        fvg_bull_1d, fvg_bear_1d,
        val, vah, poc, MIN_RR,
    )
    if not sl or rr_actual < MIN_RR:
        log.info("RR insuffisant 1:%s", rr_actual)
        return
    active_signal["direction"] = direction
    active_signal["entry"] = round(price, 0)
    active_signal["sl"] = sl
    active_signal["tp1"] = tp1
    active_signal["tp2"] = tp2
    active_signal["score"] = score
    active_signal["conviction"] = conviction
    active_signal["time"] = hhmm
    active_signal["tp1_hit"] = False
    active_signal["rr"] = rr_actual
    sig_e = "LONG" if direction == "LONG" else "SHORT"
    conv_e = "🔥🔥🔥" if conviction == "MAXIMALE" else "🔥🔥" if conviction == "TRES FORTE" else "🔥"
    reasons_str = "\n".join(["- " + r for r in reasons[:6]])
    risk_usd = round(price * RISK_PCT / 100, 0)
    gain_tp2 = round(risk_usd * rr_actual, 0)
    ob_ctx = ""
    if ob_bull_1d:
        ob_ctx += "\nOB Bull 1D: " + str(ob_bull_1d["low"]) + "-" + str(ob_bull_1d["high"])
    if ob_bear_1d:
        ob_ctx += "\nOB Bear 1D: " + str(ob_bear_1d["low"]) + "-" + str(ob_bear_1d["high"])
    fvg_ctx = ""
    if fvg_bull_1d:
        fvg_ctx += "\nFVG Bull 1D: " + str(fvg_bull_1d[0]["bot"]) + "-" + str(fvg_bull_1d[0]["top"])
    if fvg_bear_1d:
        fvg_ctx += "\nFVG Bear 1D: " + str(fvg_bear_1d[0]["bot"]) + "-" + str(fvg_bear_1d[0]["top"])
    msg = (
        conv_e + " BTC SNIPER " + sig_e + " " + conv_e + "\n"
        "Score: " + str(score) + "% - " + conviction + "\n\n"
        "Entree: " + str(round(price, 0)) + "$\n"
        "SL: " + str(sl) + "$ (-" + str(round(abs(price - sl) / price * 100, 2)) + "%)\n"
        "TP1 50%: " + str(tp1) + "$ (R:R 1:2)\n"
        "TP2 50%: " + str(tp2) + "$ (R:R 1:" + str(rr_actual) + ")\n\n"
        "Risque 3%: -" + str(risk_usd) + "$ | Gain TP2: +" + str(gain_tp2) + "$\n\n"
        "CONFLUENCES\n" + reasons_str + "\n"
        + ob_ctx + fvg_ctx + "\n\n"
        "1W:" + struct_1w + " 1D:" + struct_1d + " 4H:" + struct_4h + "\n"
        "CVD 4H:" + str(cvd_trend_4h) + " " + (str(cvd_div_4h) if cvd_div_4h else "") + "\n"
        "CVD 1D:" + str(cvd_trend_1d) + " " + (str(cvd_div_1d) if cvd_div_1d else "") + "\n"
        "RSI 1W:" + str(rsi_1w) + " 1D:" + str(rsi_1d) + " 4H:" + str(rsi_4h) + "\n"
        "POC:" + str(poc) + " VAH:" + str(vah) + " VAL:" + str(val) + "\n"
        "Funding: " + str(round(funding_now * 100, 4)) + "%\n"
        "OI: " + str(round(oi / 1000000, 1)) + "M (" + str(round(oi_change, 2)) + "%)"
    )
    send_telegram(msg)
    log.info("[SIGNAL] BTC %s %s%% %s RR=1:%s", direction, score, conviction, rr_actual)


def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHATID:
        log.error(
            "TELEGRAM_TOKEN et TELEGRAM_CHATID doivent etre definis "
            "(variables d'environnement). Voir .env.example."
        )
        sys.exit(1)
    log.info("BOT BTC SNIPER DEMARRE")
    send_telegram(
        "BTC SNIPER demarre\n"
        "Scan toutes les 4 heures\n"
        "1W + 1D + 4H alignes obligatoire\n"
        "Score minimum 82%\n"
        "RR minimum 1:3 | Risque 3%"
    )
    while True:
        try:
            scan()
            log.info("Prochain scan dans %sh", round(SCAN_INTERVAL / 3600, 1))
            time.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt:
            log.info("Bot arrete.")
            send_telegram("BTC Sniper arrete")
            break
        except Exception as e:  # garde-fou : on ne veut jamais crasher la boucle
            log.exception("[ERREUR] %s", e)
            time.sleep(300)


if __name__ == "__main__":
    main()
