"""Source de données multi-actif : websocket sortant + REST, bascule, backfill
(§4). Une seule vérité de prix : le 1m de chaque actif (BTCUSDT, ETHUSDT),
caché en parquet SEGMENTÉ PAR MOIS (chaque minute ne réécrit que ~2 Mo).
Les timeframes ≥ 15m sont ré-échantillonnées du 1m.

Détection de clôture par HORODATAGE (jamais « à la réception »). Reconnexion à
backoff exponentiel 1 s → 60 s ; backfill REST du trou à chaque reconnexion.
Ordre des sources : binance → kraken → coinbase → coingecko.
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

MINUTE_MS = 60_000
KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time"]

TF_MS = {
    "1m": 60_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
    "4h": 14_400_000, "12h": 43_200_000, "1D": 86_400_000,
}
TF_PANDAS = {
    "15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h", "12h": "12h", "1D": "24h",
}
# Mapping actif → identifiant par source REST.
SYMBOL_MAP = {
    "kraken":   {"BTCUSDT": "XBTUSD", "ETHUSDT": "ETHUSD"},
    "coinbase": {"BTCUSDT": "BTC-USD", "ETHUSDT": "ETH-USD"},
    "coingecko": {"BTCUSDT": "bitcoin", "ETHUSDT": "ethereum"},
}


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=KLINE_COLS).astype(
        {"open_time": "int64", "open": "float64", "high": "float64",
         "low": "float64", "close": "float64", "volume": "float64",
         "close_time": "int64"})


# ===========================================================================
# Cache OHLCV parquet — 1m segmenté par mois, autres TF en fichier unique (§4)
# ===========================================================================
def cache_path(symbol: str, timeframe: str) -> Path:
    return CONFIG.ohlcv_dir / f"{symbol}_{timeframe}.parquet"


def _seg_dir(symbol: str) -> Path:
    return CONFIG.ohlcv_dir / f"{symbol}_1m_segments"


def _seg_key(open_time_ms: int) -> str:
    return datetime.fromtimestamp(open_time_ms / 1000, timezone.utc).strftime("%Y-%m")


def _atomic_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def _segments(symbol: str) -> list[Path]:
    d = _seg_dir(symbol)
    return sorted(d.glob("*.parquet")) if d.exists() else []


def _is_segmented(symbol: str, timeframe: str) -> bool:
    return timeframe == "1m" and not cache_path(symbol, "1m").exists()


def load_ohlcv(symbol: str, timeframe: str = "1m") -> pd.DataFrame:
    if _is_segmented(symbol, timeframe):
        parts = [pd.read_parquet(p) for p in _segments(symbol)]
        if not parts:
            return _empty()
        return pd.concat(parts, ignore_index=True).sort_values("open_time").reset_index(drop=True)
    p = cache_path(symbol, timeframe)
    if not p.exists():
        return _empty()
    return pd.read_parquet(p).sort_values("open_time").reset_index(drop=True)


def load_ohlcv_tail(symbol: str, timeframe: str, n_rows: int) -> pd.DataFrame:
    if not _is_segmented(symbol, timeframe):
        return load_ohlcv(symbol, timeframe).tail(n_rows).reset_index(drop=True)
    parts, got = [], 0
    for p in reversed(_segments(symbol)):
        part = pd.read_parquet(p)
        parts.append(part)
        got += len(part)
        if got >= n_rows:
            break
    if not parts:
        return _empty()
    return pd.concat(list(reversed(parts)), ignore_index=True).sort_values(
        "open_time").tail(n_rows).reset_index(drop=True)


def save_ohlcv_atomic(df: pd.DataFrame, symbol: str, timeframe: str = "1m") -> None:
    df = df.sort_values("open_time").drop_duplicates("open_time").reset_index(drop=True)
    if _is_segmented(symbol, timeframe):
        existing = {p.stem for p in _segments(symbol)}
        for key, part in df.groupby(df["open_time"].map(_seg_key)):
            _atomic_parquet(part.reset_index(drop=True), _seg_dir(symbol) / f"{key}.parquet")
            existing.discard(key)
        for stale in existing:
            (_seg_dir(symbol) / f"{stale}.parquet").unlink(missing_ok=True)
        return
    _atomic_parquet(df, cache_path(symbol, timeframe))


def append_ohlcv(new_rows: pd.DataFrame, symbol: str, timeframe: str = "1m") -> None:
    if new_rows is None or new_rows.empty:
        return
    new_rows = new_rows[KLINE_COLS]
    if _is_segmented(symbol, timeframe):
        for key, part in new_rows.groupby(new_rows["open_time"].map(_seg_key)):
            seg = _seg_dir(symbol) / f"{key}.parquet"
            cur = pd.read_parquet(seg) if seg.exists() else _empty()
            merged = pd.concat([cur, part], ignore_index=True)
            merged = merged.sort_values("open_time").drop_duplicates("open_time").reset_index(drop=True)
            _atomic_parquet(merged, seg)
        return
    cur = load_ohlcv(symbol, timeframe)
    merged = pd.concat([cur, new_rows], ignore_index=True)
    save_ohlcv_atomic(merged.sort_values("open_time").drop_duplicates("open_time"), symbol, timeframe)


def last_open_time(symbol: str, timeframe: str = "1m") -> Optional[int]:
    if _is_segmented(symbol, timeframe):
        segs = _segments(symbol)
        if not segs:
            return None
        return int(pd.read_parquet(segs[-1], columns=["open_time"])["open_time"].max())
    df = load_ohlcv(symbol, timeframe)
    return int(df["open_time"].iloc[-1]) if not df.empty else None


def cache_stats(symbol: str, timeframe: str = "1m") -> tuple[int, Optional[int]]:
    import pyarrow.parquet as pq

    def _last(path: Path) -> Optional[int]:
        f = pq.ParquetFile(path)
        if f.metadata.num_rows == 0:
            return None
        rg = f.read_row_group(f.metadata.num_row_groups - 1, columns=["open_time"])
        return int(rg.column(0)[-1].as_py())

    if _is_segmented(symbol, timeframe):
        segs = _segments(symbol)
        if not segs:
            return 0, None
        return sum(pq.ParquetFile(p).metadata.num_rows for p in segs), _last(segs[-1])
    p = cache_path(symbol, timeframe)
    if not p.exists():
        return 0, None
    return int(pq.ParquetFile(p).metadata.num_rows), _last(p)


# ===========================================================================
# Continuité & ré-échantillonnage
# ===========================================================================
def find_gaps(df: pd.DataFrame, step_ms: int = MINUTE_MS) -> list[tuple[int, int]]:
    if df is None or len(df) < 2:
        return []
    times = df["open_time"].astype("int64").to_numpy()
    return [(int(p + step_ms), int(c - step_ms)) for p, c in zip(times[:-1], times[1:])
            if c - p > step_ms]


def count_duplicates(df: pd.DataFrame) -> int:
    return 0 if df is None or df.empty else int(df["open_time"].duplicated().sum())


def resample_1m(df_1m: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Agrège le 1m en `timeframe` (OHLCV, bornes UTC alignées sur l'époque).
    Ne renvoie que des bougies COMPLÈTES (zéro look-ahead)."""
    if timeframe == "1m":
        return df_1m.copy()
    if timeframe not in TF_PANDAS:
        raise ValueError(f"Timeframe non ré-échantillonnable : {timeframe}")
    if df_1m.empty:
        return _empty()
    d = df_1m.copy()
    d["ts"] = pd.to_datetime(d["open_time"], unit="ms", utc=True)
    d = d.set_index("ts").sort_index()
    agg = d.resample(TF_PANDAS[timeframe], label="left", closed="left", origin="epoch").agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum")).dropna(subset=["open"])
    out = agg.reset_index()
    out["open_time"] = (out["ts"].astype("int64") // 1_000_000).astype("int64")
    out["close_time"] = out["open_time"] + TF_MS[timeframe] - 1
    out = out[KLINE_COLS]
    last_1m_open = int(df_1m["open_time"].iloc[-1])
    complete = out["open_time"] + TF_MS[timeframe] - 1 <= last_1m_open + MINUTE_MS - 1
    return out[complete].reset_index(drop=True)


# ===========================================================================
# Sources REST (§4.2) — chacune expose fetch_1m(symbol, start_ms, end_ms)
# ===========================================================================
class RestSource:
    name = "base"

    def fetch_1m(self, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        raise NotImplementedError

    def server_time_ms(self) -> Optional[int]:
        return None

    @staticmethod
    def _get(url, params=None, timeout=20):
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()


class BinanceRest(RestSource):
    name = "binance"
    BASE = "https://api.binance.com"

    def server_time_ms(self):
        try:
            return int(self._get(f"{self.BASE}/api/v3/time")["serverTime"])
        except Exception:
            return None

    def fetch_1m(self, symbol, start_ms, end_ms):
        rows, cursor = [], start_ms
        while cursor <= end_ms:
            data = self._get(f"{self.BASE}/api/v3/klines",
                             {"symbol": symbol, "interval": "1m",
                              "startTime": cursor, "endTime": end_ms, "limit": 1000})
            if not data:
                break
            for k in data:
                rows.append((int(k[0]), float(k[1]), float(k[2]), float(k[3]),
                             float(k[4]), float(k[5]), int(k[6])))
            cursor = int(data[-1][0]) + MINUTE_MS
            if len(data) < 1000:
                break
            time.sleep(0.15)
        return pd.DataFrame(rows, columns=KLINE_COLS) if rows else _empty()


class KrakenRest(RestSource):
    name = "kraken"
    BASE = "https://api.kraken.com"

    def server_time_ms(self):
        try:
            return int(self._get(f"{self.BASE}/0/public/Time")["result"]["unixtime"]) * 1000
        except Exception:
            return None

    def fetch_1m(self, symbol, start_ms, end_ms):
        pair = SYMBOL_MAP["kraken"].get(symbol)
        if not pair:
            return _empty()
        data = self._get(f"{self.BASE}/0/public/OHLC",
                         {"pair": pair, "interval": 1, "since": start_ms // 1000})
        series = next((v for k, v in data.get("result", {}).items() if k != "last"), [])
        rows = []
        for c in series:
            ot = int(c[0]) * 1000
            if start_ms <= ot <= end_ms:
                rows.append((ot, float(c[1]), float(c[2]), float(c[3]),
                             float(c[4]), float(c[6]), ot + MINUTE_MS - 1))
        return pd.DataFrame(rows, columns=KLINE_COLS) if rows else _empty()


class CoinbaseRest(RestSource):
    name = "coinbase"
    BASE = "https://api.exchange.coinbase.com"

    def fetch_1m(self, symbol, start_ms, end_ms):
        product = SYMBOL_MAP["coinbase"].get(symbol)
        if not product:
            return _empty()
        rows, cursor = [], start_ms
        while cursor <= end_ms:
            window_end = min(cursor + 300 * MINUTE_MS, end_ms)
            data = self._get(f"{self.BASE}/products/{product}/candles",
                             {"granularity": 60,
                              "start": datetime.fromtimestamp(cursor / 1000, timezone.utc).isoformat(),
                              "end": datetime.fromtimestamp(window_end / 1000, timezone.utc).isoformat()})
            if not data:
                break
            for c in sorted(data, key=lambda x: x[0]):
                ot = int(c[0]) * 1000
                rows.append((ot, float(c[3]), float(c[2]), float(c[1]),
                             float(c[4]), float(c[5]), ot + MINUTE_MS - 1))
            cursor = window_end + MINUTE_MS
            time.sleep(0.2)
        return pd.DataFrame(rows, columns=KLINE_COLS) if rows else _empty()


class CoinGeckoRest(RestSource):
    name = "coingecko"
    BASE = "https://api.coingecko.com/api/v3"

    def fetch_1m(self, symbol, start_ms, end_ms):
        coin = SYMBOL_MAP["coingecko"].get(symbol)
        if not coin:
            return _empty()
        data = self._get(f"{self.BASE}/coins/{coin}/market_chart/range",
                         {"vs_currency": "usd", "from": start_ms // 1000, "to": end_ms // 1000})
        rows = []
        for ts_ms, price in data.get("prices", []):
            ot = (int(ts_ms) // MINUTE_MS) * MINUTE_MS
            rows.append((ot, price, price, price, price, 0.0, ot + MINUTE_MS - 1))
        df = pd.DataFrame(rows, columns=KLINE_COLS) if rows else _empty()
        return df.drop_duplicates("open_time")


_REGISTRY = {"binance": BinanceRest, "kraken": KrakenRest,
             "coinbase": CoinbaseRest, "coingecko": CoinGeckoRest}


# ===========================================================================
# Orchestrateur REST avec bascule (§4.2)
# ===========================================================================
class DataSource:
    def __init__(self, store=None, logger=None) -> None:
        self.store = store
        self.logger = logger
        order = [CONFIG.data_primary] + [s for s in CONFIG.data_fallbacks
                                         if s != CONFIG.data_primary]
        self.sources = [_REGISTRY[n]() for n in order if n in _REGISTRY]
        self.active = self.sources[0].name if self.sources else CONFIG.data_primary

    def _event(self, kind, level, msg, meta=None):
        if self.logger:
            getattr(self.logger, "info" if level == "info" else "warning")(msg)
        if self.store:
            self.store.add_event(kind, level, msg, meta)

    def fetch_1m(self, symbol, start_ms, end_ms):
        last_err = None
        for src in self.sources:
            try:
                df = src.fetch_1m(symbol, start_ms, end_ms)
                if not df.empty:
                    if src.name != self.active:
                        self._event("data_incident", "warning",
                                    f"Bascule source → {src.name}",
                                    {"from": self.active, "to": src.name})
                        self.active = src.name
                    if src.name == "coingecko":
                        self._event("data_incident", "warning",
                                    "Source dégradée (coingecko) : bougies close-only",
                                    {"source": "coingecko"})
                    return df
            except Exception as e:
                last_err = e
                if self.logger:
                    self.logger.warning(f"Source {src.name} ({symbol}) échec : {e!r}")
        if last_err and self.logger:
            self.logger.error(f"Toutes les sources REST ont échoué ({symbol}) : {last_err!r}")
        return _empty()

    def server_time_ms(self):
        for src in self.sources:
            t = src.server_time_ms()
            if t:
                return t
        return None

    def ensure_backfill(self, symbol, days, chunk_ms=12 * 3600 * 1000) -> int:
        now = _now_ms()
        last = last_open_time(symbol, "1m")
        start = (now - days * 86_400_000) if last is None else last + MINUTE_MS
        added, cursor = 0, start
        while cursor < now:
            end = min(cursor + chunk_ms, now)
            df = self.fetch_1m(symbol, cursor, end)
            if not df.empty:
                append_ohlcv(df, symbol, "1m")
                added += len(df)
                cursor = int(df["open_time"].iloc[-1]) + MINUTE_MS
            else:
                cursor = end + MINUTE_MS
        return added

    def fill_gaps(self, symbol, max_gaps=500) -> int:
        gaps = find_gaps(load_ohlcv(symbol, "1m"))
        if not gaps:
            return 0
        filled = 0
        for start, end in gaps[:max_gaps]:
            got = self.fetch_1m(symbol, start, end)
            if not got.empty:
                append_ohlcv(got, symbol, "1m")
                filled += len(got)
        if filled:
            self._event("data_incident", "info",
                        f"Trou(s) comblé(s) {symbol} : {filled} bougie(s)",
                        {"symbol": symbol, "filled": filled})
        return filled

    def poll_recent(self, symbol, lookback_min=180) -> pd.DataFrame:
        now = _now_ms()
        df = self.fetch_1m(symbol, now - lookback_min * MINUTE_MS, now)
        if df.empty:
            return df
        return df[df["open_time"] + MINUTE_MS <= now].reset_index(drop=True)


# ===========================================================================
# Flux websocket combiné Binance (BTC + ETH), §4.5
# ===========================================================================
class BinanceMultiStream:
    """Flux kline 1m COMBINÉ pour plusieurs actifs. Clôture par HORODATAGE.
    on_closed_candle(symbol, row). Reconnexion backoff 1 s → 60 s."""

    URL = "wss://stream.binance.com:9443/stream?streams={streams}"

    def __init__(self, symbols, on_closed_candle, on_reconnect=None,
                 on_status=None, logger=None) -> None:
        self.symbols = symbols
        self.on_closed_candle = on_closed_candle
        self.on_reconnect = on_reconnect
        self.on_status = on_status
        self.logger = logger
        self._stop = False
        self._forming: dict[str, dict] = {}
        self._last_emitted: dict[str, int] = {}
        self._last_msg_ms = 0

    def _finalize(self, symbol, force=False):
        row = self._forming.get(symbol)
        if row is None:
            return
        if force or _now_ms() >= row["close_time"]:
            if row["open_time"] > self._last_emitted.get(symbol, 0):
                self._last_emitted[symbol] = row["open_time"]
                self.on_closed_candle(symbol, dict(row))
            self._forming[symbol] = None

    def _on_message(self, _ws, message):
        self._last_msg_ms = _now_ms()
        try:
            payload = json.loads(message)
            data = payload.get("data", payload)
            k = data["k"]
            symbol = data["s"].upper()
        except (KeyError, json.JSONDecodeError, TypeError):
            return
        ot = int(k["t"])
        row = {"open_time": ot, "open": float(k["o"]), "high": float(k["h"]),
               "low": float(k["l"]), "close": float(k["c"]), "volume": float(k["v"]),
               "close_time": ot + MINUTE_MS - 1}
        prev = self._forming.get(symbol)
        if prev is not None and ot != prev["open_time"]:
            self._finalize(symbol, force=True)
        self._forming[symbol] = row
        self._finalize(symbol)

    def last_message_age_s(self) -> float:
        return float("inf") if not self._last_msg_ms else (_now_ms() - self._last_msg_ms) / 1000.0

    def stop(self):
        self._stop = True

    def run_forever(self):
        import websocket
        streams = "/".join(f"{s.lower()}@kline_1m" for s in self.symbols)
        url = self.URL.format(streams=streams)
        backoff = 1
        while not self._stop:
            connected = {"ok": False}

            def _on_open(_ws):
                connected["ok"] = True
                if self.on_status:
                    self.on_status("stream", "connected")
                if self.on_reconnect:
                    self.on_reconnect()

            def _on_error(_ws, err):
                if self.logger:
                    self.logger.warning(f"Websocket erreur : {err!r}")

            def _on_close(_ws, *_a):
                if self.on_status:
                    self.on_status("stream", "disconnected")

            ws = websocket.WebSocketApp(url, on_message=self._on_message,
                                        on_open=_on_open, on_error=_on_error,
                                        on_close=_on_close)
            try:
                ws.run_forever(ping_interval=180, ping_timeout=10)
            except Exception as e:
                if self.logger:
                    self.logger.warning(f"run_forever : {e!r}")
            if self._stop:
                break
            backoff = 1 if connected["ok"] else min(backoff * 2, 60)
            if self.logger:
                self.logger.warning(f"Reconnexion websocket dans {backoff}s")
            for _ in range(backoff * 10):
                if self._stop:
                    break
                time.sleep(0.1)
