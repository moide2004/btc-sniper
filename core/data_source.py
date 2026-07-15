"""Source de données : websocket sortant + REST, bascule, backfill (§2.3, §7.4).

Une seule vérité de prix : le 1m (§2.3). Toutes les timeframes ≤ 1D en sont
ré-échantillonnées (implémenté ici : `resample_1m`). Les klines 1m sont
cachées en parquet (§7.2) avec écriture atomique (fichier temporaire puis
renommage).

Ingestion — deux backends, même sortie (une bougie 1m CLÔTURÉE) :
  * StreamBackend  : websocket Binance (kline 1m), reconnexion à backoff
                     exponentiel 1 s → 60 s, backfill REST du trou à chaque
                     reconnexion (§7.4). C'est le chemin PRIMAIRE.
  * PollBackend    : sondage REST de la source active. Chemin de REPLI, utilisé
                     quand la primaire est muette > 5 min, ou quand le
                     websocket ne se connecte pas.

Détection de clôture par horodatage (§7.4) : une bougie d'ouverture T est
close quand l'horloge dépasse T + durée — JAMAIS « à la réception ».

Ordre des sources REST : primaire puis replis (§2.3) : binance → kraken →
coinbase → coingecko. La bascule émet un événement dans la cloche.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
import requests

from .config import CONFIG
from .store import Store

MINUTE_MS = 60_000
KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time"]

# Durée d'une bougie par timeframe, en millisecondes.
TF_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "12h": 43_200_000, "1D": 86_400_000,
}
# Règle pandas de ré-échantillonnage (bornes gauche, label gauche).
TF_PANDAS = {
    "5m": "5min", "15m": "15min", "30m": "30min",
    "1h": "1h", "4h": "4h", "12h": "12h", "1D": "1D",
}


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _empty_klines() -> pd.DataFrame:
    df = pd.DataFrame(columns=KLINE_COLS)
    return df.astype(
        {"open_time": "int64", "open": "float64", "high": "float64",
         "low": "float64", "close": "float64", "volume": "float64",
         "close_time": "int64"}
    )


# ===========================================================================
# Cache OHLCV parquet (écriture atomique, §7.2)
# ===========================================================================
def cache_path(timeframe: str = "1m", symbol: Optional[str] = None) -> Path:
    symbol = symbol or CONFIG.symbol
    return CONFIG.ohlcv_dir / f"{symbol}_{timeframe}.parquet"


def load_ohlcv(timeframe: str = "1m") -> pd.DataFrame:
    p = cache_path(timeframe)
    if not p.exists():
        return _empty_klines()
    df = pd.read_parquet(p)
    return df.sort_values("open_time").reset_index(drop=True)


def save_ohlcv_atomic(df: pd.DataFrame, timeframe: str = "1m") -> None:
    """Écrit le parquet via fichier temporaire + os.replace (renommage
    atomique — §7.2 : toute écriture fichier = temp puis renommage)."""
    p = cache_path(timeframe)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".tmp.{os.getpid()}")
    df = df.sort_values("open_time").drop_duplicates("open_time").reset_index(drop=True)
    df.to_parquet(tmp, index=False)
    os.replace(tmp, p)


def append_ohlcv(new_rows: pd.DataFrame, timeframe: str = "1m") -> pd.DataFrame:
    """Fusionne de nouvelles bougies dans le cache (dédup sur open_time)."""
    if new_rows is None or new_rows.empty:
        return load_ohlcv(timeframe)
    cur = load_ohlcv(timeframe)
    merged = pd.concat([cur, new_rows[KLINE_COLS]], ignore_index=True)
    merged = merged.sort_values("open_time").drop_duplicates("open_time").reset_index(drop=True)
    save_ohlcv_atomic(merged, timeframe)
    return merged


def last_open_time(timeframe: str = "1m") -> Optional[int]:
    df = load_ohlcv(timeframe)
    if df.empty:
        return None
    return int(df["open_time"].iloc[-1])


# ===========================================================================
# Contrôle de continuité (§7.4)
# ===========================================================================
def find_gaps(df: pd.DataFrame, step_ms: int = MINUTE_MS) -> list[tuple[int, int]]:
    """Retourne la liste des trous [start_ms, end_ms] (ouvertures manquantes,
    bornes incluses) dans une série supposée régulière."""
    if df is None or len(df) < 2:
        return []
    times = df["open_time"].astype("int64").to_numpy()
    gaps: list[tuple[int, int]] = []
    for prev, cur in zip(times[:-1], times[1:]):
        if cur - prev > step_ms:
            gaps.append((int(prev + step_ms), int(cur - step_ms)))
    return gaps


def count_duplicates(df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    return int(df["open_time"].duplicated().sum())


# ===========================================================================
# Ré-échantillonnage 1m -> timeframe supérieure (§2.3)
# ===========================================================================
def resample_1m(df_1m: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Agrège des bougies 1m en `timeframe` (OHLCV). Bornes UTC alignées sur
    l'époque. Ne renvoie que des bougies COMPLÈTES (zéro look-ahead)."""
    if timeframe == "1m":
        return df_1m.copy()
    if timeframe not in TF_PANDAS:
        raise ValueError(f"Timeframe non ré-échantillonnable depuis 1m : {timeframe}")
    if df_1m.empty:
        return _empty_klines()

    d = df_1m.copy()
    d["ts"] = pd.to_datetime(d["open_time"], unit="ms", utc=True)
    d = d.set_index("ts").sort_index()
    rule = TF_PANDAS[timeframe]
    agg = d.resample(rule, label="left", closed="left", origin="epoch").agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum"),
    ).dropna(subset=["open"])

    out = agg.reset_index()
    out["open_time"] = (out["ts"].astype("int64") // 1_000_000).astype("int64")
    out["close_time"] = out["open_time"] + TF_MS[timeframe] - 1
    out = out[KLINE_COLS]

    # Ne garder que les bougies dont l'intervalle complet est couvert par du 1m.
    last_1m_open = int(df_1m["open_time"].iloc[-1])
    complete = out["open_time"] + TF_MS[timeframe] - 1 <= last_1m_open + MINUTE_MS - 1
    return out[complete].reset_index(drop=True)


# ===========================================================================
# Sources REST (§2.3) — chacune expose fetch_1m(start_ms, end_ms)
# ===========================================================================
class RestSource:
    name = "base"

    def fetch_1m(self, start_ms: int, end_ms: int) -> pd.DataFrame:
        raise NotImplementedError

    def server_time_ms(self) -> Optional[int]:
        return None

    @staticmethod
    def _get(url: str, params: Optional[dict] = None, timeout: int = 20):
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()


class BinanceRest(RestSource):
    name = "binance"
    BASE = "https://api.binance.com"

    def server_time_ms(self) -> Optional[int]:
        try:
            return int(self._get(f"{self.BASE}/api/v3/time")["serverTime"])
        except Exception:
            return None

    def fetch_1m(self, start_ms: int, end_ms: int) -> pd.DataFrame:
        rows = []
        cursor = start_ms
        while cursor <= end_ms:
            data = self._get(
                f"{self.BASE}/api/v3/klines",
                {"symbol": CONFIG.symbol, "interval": "1m",
                 "startTime": cursor, "endTime": end_ms, "limit": 1000},
            )
            if not data:
                break
            for k in data:
                rows.append((int(k[0]), float(k[1]), float(k[2]), float(k[3]),
                             float(k[4]), float(k[5]), int(k[6])))
            cursor = int(data[-1][0]) + MINUTE_MS
            if len(data) < 1000:
                break
            time.sleep(0.15)  # courtoisie / rate limit
        return pd.DataFrame(rows, columns=KLINE_COLS) if rows else _empty_klines()


class KrakenRest(RestSource):
    name = "kraken"
    BASE = "https://api.kraken.com"
    PAIR = "XBTUSD"

    def server_time_ms(self) -> Optional[int]:
        try:
            return int(self._get(f"{self.BASE}/0/public/Time")["result"]["unixtime"]) * 1000
        except Exception:
            return None

    def fetch_1m(self, start_ms: int, end_ms: int) -> pd.DataFrame:
        # Kraken OHLC : `since` en secondes, ~720 dernières bougies max (repli
        # pour trous récents, pas pour backfill profond).
        data = self._get(
            f"{self.BASE}/0/public/OHLC",
            {"pair": self.PAIR, "interval": 1, "since": start_ms // 1000},
        )
        result = data.get("result", {})
        series = next((v for k, v in result.items() if k != "last"), [])
        rows = []
        for c in series:
            ot = int(c[0]) * 1000
            if ot < start_ms or ot > end_ms:
                continue
            rows.append((ot, float(c[1]), float(c[2]), float(c[3]),
                         float(c[4]), float(c[6]), ot + MINUTE_MS - 1))
        return pd.DataFrame(rows, columns=KLINE_COLS) if rows else _empty_klines()


class CoinbaseRest(RestSource):
    name = "coinbase"
    BASE = "https://api.exchange.coinbase.com"
    PRODUCT = "BTC-USD"

    def fetch_1m(self, start_ms: int, end_ms: int) -> pd.DataFrame:
        rows = []
        cursor = start_ms
        while cursor <= end_ms:
            window_end = min(cursor + 300 * MINUTE_MS, end_ms)  # 300 bougies max
            data = self._get(
                f"{self.BASE}/products/{self.PRODUCT}/candles",
                {"granularity": 60,
                 "start": datetime.fromtimestamp(cursor / 1000, timezone.utc).isoformat(),
                 "end": datetime.fromtimestamp(window_end / 1000, timezone.utc).isoformat()},
            )
            if not data:
                break
            # Coinbase renvoie [time, low, high, open, close, volume], desc.
            for c in sorted(data, key=lambda x: x[0]):
                ot = int(c[0]) * 1000
                rows.append((ot, float(c[3]), float(c[2]), float(c[1]),
                             float(c[4]), float(c[5]), ot + MINUTE_MS - 1))
            cursor = window_end + MINUTE_MS
            time.sleep(0.2)
        return pd.DataFrame(rows, columns=KLINE_COLS) if rows else _empty_klines()


class CoinGeckoRest(RestSource):
    name = "coingecko"
    BASE = "https://api.coingecko.com/api/v3"

    def fetch_1m(self, start_ms: int, end_ms: int) -> pd.DataFrame:
        # Dernier repli : uniquement des prix (close). Bougies synthétiques
        # o=h=l=c, volume 0 — un badge « source dégradée » est émis en amont.
        data = self._get(
            f"{self.BASE}/coins/bitcoin/market_chart/range",
            {"vs_currency": "usd", "from": start_ms // 1000, "to": end_ms // 1000},
        )
        rows = []
        for ts_ms, price in data.get("prices", []):
            ot = (int(ts_ms) // MINUTE_MS) * MINUTE_MS
            rows.append((ot, price, price, price, price, 0.0, ot + MINUTE_MS - 1))
        df = pd.DataFrame(rows, columns=KLINE_COLS) if rows else _empty_klines()
        return df.drop_duplicates("open_time")


_SOURCE_REGISTRY = {
    "binance": BinanceRest, "kraken": KrakenRest,
    "coinbase": CoinbaseRest, "coingecko": CoinGeckoRest,
}


# ===========================================================================
# Orchestrateur REST avec bascule (§7.4)
# ===========================================================================
class DataSource:
    """Backfill et sondage REST avec bascule automatique entre sources.
    Journalise les bascules et incidents dans la cloche (§6.2) via un Store."""

    def __init__(self, store: Optional[Store] = None, logger=None) -> None:
        self.store = store
        self.logger = logger
        order = [CONFIG.data_primary] + [
            s for s in CONFIG.data_fallbacks if s != CONFIG.data_primary
        ]
        self.sources = [_SOURCE_REGISTRY[n]() for n in order if n in _SOURCE_REGISTRY]
        self.active = self.sources[0].name if self.sources else CONFIG.data_primary

    def _event(self, kind: str, level: str, msg: str, meta: Optional[dict] = None) -> None:
        if self.logger:
            getattr(self.logger, "info" if level == "info" else "warning")(msg)
        if self.store:
            self.store.add_event(kind, level, msg, meta)

    def fetch_1m(self, start_ms: int, end_ms: int) -> pd.DataFrame:
        """Essaie chaque source dans l'ordre jusqu'à obtenir des données ;
        bascule + événement si la source active change."""
        last_err: Optional[Exception] = None
        for src in self.sources:
            try:
                df = src.fetch_1m(start_ms, end_ms)
                if not df.empty:
                    if src.name != self.active:
                        self._event("data_incident", "warning",
                                    f"Bascule source de données → {src.name}",
                                    {"from": self.active, "to": src.name})
                        self.active = src.name
                    if src.name == "coingecko":
                        self._event("data_incident", "warning",
                                    "Source dégradée (coingecko) : funding non "
                                    "intégré, bougies close-only", {"source": "coingecko"})
                    return df
            except Exception as e:  # réseau, 4xx/5xx, parsing
                last_err = e
                if self.logger:
                    self.logger.warning(f"Source {src.name} en échec : {e!r}")
                continue
        if last_err and self.logger:
            self.logger.error(f"Toutes les sources REST ont échoué : {last_err!r}")
        return _empty_klines()

    def server_time_ms(self) -> Optional[int]:
        for src in self.sources:
            t = src.server_time_ms()
            if t:
                return t
        return None

    # ----- Backfill & comblage de trous ------------------------------------
    def ensure_backfill(self, days: int, chunk_ms: int = 12 * 3600 * 1000) -> int:
        """Garantit ~`days` jours de 1m dans le cache, depuis le dernier point
        connu (reprise idempotente, §7.3). Retourne le nombre de bougies ajoutées."""
        now = _now_ms()
        start_target = now - days * 86_400_000
        last = last_open_time("1m")
        start = start_target if last is None else last + MINUTE_MS
        added = 0
        cursor = start
        while cursor < now:
            end = min(cursor + chunk_ms, now)
            df = self.fetch_1m(cursor, end)
            if not df.empty:
                append_ohlcv(df, "1m")
                added += len(df)
                cursor = int(df["open_time"].iloc[-1]) + MINUTE_MS
            else:
                cursor = end + MINUTE_MS
        return added

    def fill_gaps(self, max_gaps: int = 500) -> int:
        """Comble les trous du cache 1m via REST (§7.4). Retourne le nombre de
        bougies récupérées."""
        df = load_ohlcv("1m")
        gaps = find_gaps(df)
        if not gaps:
            return 0
        filled = 0
        for start, end in gaps[:max_gaps]:
            got = self.fetch_1m(start, end)
            if not got.empty:
                append_ohlcv(got, "1m")
                filled += len(got)
        if filled:
            self._event("data_incident", "info",
                        f"Trou(s) comblé(s) : {filled} bougie(s) 1m récupérée(s)",
                        {"gaps": len(gaps), "filled": filled})
        return filled

    def poll_recent(self, lookback_min: int = 120) -> pd.DataFrame:
        """Sondage REST des bougies récentes (chemin de repli d'ingestion).
        Renvoie uniquement les bougies CLÔTURÉES (open_time + 1m ≤ maintenant)."""
        now = _now_ms()
        df = self.fetch_1m(now - lookback_min * MINUTE_MS, now)
        if df.empty:
            return df
        closed = df[df["open_time"] + MINUTE_MS <= now]
        return closed.reset_index(drop=True)


# ===========================================================================
# Backend websocket Binance (chemin PRIMAIRE d'ingestion, §7.4)
# ===========================================================================
class BinanceKlineStream:
    """Flux kline 1m Binance via websocket sortant. Reconnexion à backoff
    exponentiel 1 s → 60 s. À chaque (re)connexion, un callback de backfill
    comble le trou. Clôture détectée par HORODATAGE, pas par le flag reçu."""

    URL = "wss://stream.binance.com:9443/ws/{sym}@kline_1m"

    def __init__(
        self,
        on_closed_candle: Callable[[dict], None],
        on_reconnect: Optional[Callable[[], None]] = None,
        on_status: Optional[Callable[[str, str], None]] = None,
        logger=None,
    ) -> None:
        self.on_closed_candle = on_closed_candle
        self.on_reconnect = on_reconnect
        self.on_status = on_status
        self.logger = logger
        self._stop = False
        self._forming: Optional[dict] = None  # bougie en cours (open_time -> row)
        self._last_msg_ms = 0

    def _finalize_if_closed(self, force: bool = False) -> None:
        """Émet la bougie en cours si l'horloge a dépassé sa clôture."""
        if self._forming is None:
            return
        row = self._forming
        if force or _now_ms() >= row["close_time"]:
            self.on_closed_candle(dict(row))
            self._forming = None

    def _on_message(self, _ws, message: str) -> None:
        self._last_msg_ms = _now_ms()
        try:
            k = json.loads(message)["k"]
        except (KeyError, json.JSONDecodeError):
            return
        ot = int(k["t"])
        row = {
            "open_time": ot, "open": float(k["o"]), "high": float(k["h"]),
            "low": float(k["l"]), "close": float(k["c"]), "volume": float(k["v"]),
            "close_time": ot + MINUTE_MS - 1,
        }
        # Nouvelle bougie détectée -> la précédente est close (par horodatage).
        if self._forming is not None and ot != self._forming["open_time"]:
            self._finalize_if_closed(force=True)
        self._forming = row
        self._finalize_if_closed()

    def last_message_age_s(self) -> float:
        if not self._last_msg_ms:
            return float("inf")
        return (_now_ms() - self._last_msg_ms) / 1000.0

    def stop(self) -> None:
        self._stop = True

    def run_forever(self) -> None:
        """Boucle de connexion avec backoff. Bloquant : à lancer dans un thread."""
        import websocket  # import tardif : ne casse pas les imports sans la lib

        backoff = 1
        url = self.URL.format(sym=CONFIG.symbol.lower())
        while not self._stop:
            connected = {"ok": False}

            def _on_open(_ws):
                connected["ok"] = True
                if self.on_status:
                    self.on_status("stream", "connected")
                if self.on_reconnect:
                    self.on_reconnect()  # backfill du trou (§7.4)

            def _on_error(_ws, err):
                if self.logger:
                    self.logger.warning(f"Websocket erreur : {err!r}")

            def _on_close(_ws, *_a):
                if self.on_status:
                    self.on_status("stream", "disconnected")

            ws = websocket.WebSocketApp(
                url, on_message=self._on_message, on_open=_on_open,
                on_error=_on_error, on_close=_on_close,
            )
            try:
                ws.run_forever(ping_interval=180, ping_timeout=10)
            except Exception as e:
                if self.logger:
                    self.logger.warning(f"Websocket run_forever : {e!r}")
            if self._stop:
                break
            backoff = 1 if connected["ok"] else min(backoff * 2, 60)
            if self.logger:
                self.logger.warning(f"Reconnexion websocket dans {backoff}s")
            for _ in range(backoff * 10):
                if self._stop:
                    break
                time.sleep(0.1)
