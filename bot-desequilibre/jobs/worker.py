"""Always-on task : worker événementiel sur clôtures 1m, DUAL-ACTIF (§4, §8).

IDEMPOTENT au redémarrage : reprise invisible depuis l'état persistant. Un seul
écrivain : le thread websocket ne fait que déposer les bougies closes (symbole,
row) dans une file ; toutes les écritures cache/DB restent au thread principal.

Ingestion : flux websocket combiné Binance (BTC+ETH) primaire ; repli sondage
REST par actif si le flux est muet > PRIMARY_SILENCE_SECONDS.
"""
from __future__ import annotations

import queue
import signal
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import CONFIG  # noqa: E402
from core.data_source import (  # noqa: E402
    BinanceMultiStream, DataSource, append_ohlcv, count_duplicates, find_gaps,
    last_open_time, load_ohlcv, MINUTE_MS,
)
from core.engine import live_scan, paper_step  # noqa: E402
from core.logging_setup import get_logger  # noqa: E402
from core.store import Store, utc_now_ms  # noqa: E402

log = get_logger("worker")
CONNECT_GRACE_S = 25
MAINTENANCE_EVERY = 20


class Worker:
    def __init__(self) -> None:
        CONFIG.ensure_dirs()
        self.store = Store()
        self.ds = DataSource(store=self.store, logger=log)
        self.symbols = CONFIG.symbols
        self.candle_q: "queue.Queue[tuple]" = queue.Queue()
        self.ws_signal_q: "queue.Queue[tuple]" = queue.Queue()
        self.stream = BinanceMultiStream(
            self.symbols,
            on_closed_candle=lambda s, r: self.candle_q.put((s, r)),
            on_reconnect=lambda: self.ws_signal_q.put(("reconnect", None)),
            on_status=lambda k, st: self.ws_signal_q.put(("status", st)),
            logger=log)
        self.cycle = 0
        self.started_ms = utc_now_ms()
        self.active_source = self.ds.active
        self.ingest_mode = "stream"
        self._stop = threading.Event()
        self._switched_to_rest = False
        self._last_beat = 0.0

    # ----- Heartbeat (maintenu pendant les phases lourdes) -----------------
    def _beat_if_due(self, status="ok") -> None:
        now = time.time()
        if now - self._last_beat >= CONFIG.heartbeat_seconds:
            try:
                self.store.write_heartbeat(self.cycle, self.active_source, status)
                self._last_beat = now
            except Exception as e:
                log.error(f"Heartbeat : {e!r}")

    # ----- Signaux websocket (traités au thread principal, §4.5) -----------
    def _drain_ws_signals(self) -> None:
        while not self.ws_signal_q.empty():
            kind, status = self.ws_signal_q.get_nowait()
            if kind == "reconnect":
                for sym in self.symbols:
                    try:
                        filled = self.ds.fill_gaps(sym)
                        if filled:
                            log.info(f"Backfill post-reconnexion {sym} : {filled}")
                    except Exception as e:
                        log.warning(f"Backfill post-reconnexion {sym} : {e!r}")
            elif kind == "status":
                log.info(f"Websocket {status}")
                if status == "connected":
                    self.active_source = "binance"
                    if self.ingest_mode == "rest":
                        self.ingest_mode = "stream"
                        self._switched_to_rest = False
                        self.store.add_event("data_incident", "info",
                                             "Retour au flux websocket primaire (Binance)")

    # ----- Reprise (§8 P1) --------------------------------------------------
    def startup_recovery(self) -> None:
        log.info("Démarrage worker : reprise de l'état persistant")
        self._beat_if_due()
        totals = {}
        for sym in self.symbols:
            last = last_open_time(sym, "1m")
            if last is None:
                log.info(f"Cache 1m {sym} vide → backfill {CONFIG.backfill_days} j")
            added = self.ds.ensure_backfill(sym, CONFIG.backfill_days)
            self.ds.fill_gaps(sym)
            df = load_ohlcv(sym, "1m")
            gaps, dups = len(find_gaps(df)), count_duplicates(df)
            totals[sym] = len(df)
            log.info(f"Continuité 1m {sym} : {len(df)} bougies, {gaps} trou(s), "
                     f"{dups} doublon(s), +{added} au démarrage")
            self._beat_if_due()
        self.active_source = self.ds.active
        self.store.add_event("worker", "info", "Worker (re)démarré — reprise effectuée",
                             {"bars_1m": totals})
        self.store.write_heartbeat(self.cycle, self.active_source, "ok")

    # ----- Ingestion --------------------------------------------------------
    def _drain_stream_candles(self) -> None:
        rows_by_sym: dict[str, list] = {}
        while not self.candle_q.empty():
            sym, row = self.candle_q.get_nowait()
            rows_by_sym.setdefault(sym, []).append(row)
        for sym, rows in rows_by_sym.items():
            import pandas as pd
            append_ohlcv(pd.DataFrame(rows), sym, "1m")
            log.info(f"{len(rows)} bougie(s) 1m {sym} closes reçues (flux)")

    def _rest_fallback_poll(self) -> None:
        for sym in self.symbols:
            got = self.ds.poll_recent(sym, lookback_min=180)
            if got.empty:
                continue
            before = last_open_time(sym, "1m") or 0
            append_ohlcv(got, sym, "1m")
            self.active_source = self.ds.active
            new = got[got["open_time"] > before]
            if len(new):
                log.info(f"Repli REST ({self.active_source}) {sym} : {len(new)} bougie(s)")

    # ----- Boucle principale ------------------------------------------------
    def run(self) -> None:
        self._install_signals()
        self.startup_recovery()
        stream_thread = threading.Thread(target=self.stream.run_forever, daemon=True)
        stream_thread.start()
        while not self._stop.is_set():
            self.cycle += 1
            status = "ok"
            try:
                self._drain_ws_signals()
                self._drain_stream_candles()
                stream_age = self.stream.last_message_age_s()
                grace_over = (utc_now_ms() - self.started_ms) / 1000 > CONNECT_GRACE_S
                if stream_age >= CONFIG.primary_silence_seconds and grace_over:
                    if not self._switched_to_rest:
                        self._switched_to_rest = True
                        self.ingest_mode = "rest"
                        self.store.add_event("data_incident", "warning",
                                             "Flux primaire muet → ingestion REST de repli",
                                             {"stream_age_s": None if stream_age == float("inf")
                                              else round(stream_age, 1)})
                        log.warning("Bascule ingestion → REST de repli")
                    self._rest_fallback_poll()
                    status = "degraded"
                if self.cycle % MAINTENANCE_EVERY == 0:
                    for sym in self.symbols:
                        try:
                            self.ds.fill_gaps(sym)
                        except Exception as e:
                            log.warning(f"Maintenance {sym} : {e!r}")
                    self.store.prune_events(90)
                    self._beat_if_due(status)
                    try:                             # §2/§3 : setups → tickets
                        n = live_scan(self.store, utc_now_ms(), log)
                        if n:
                            log.info(f"Scan setups : {n} ticket(s) émis")
                    except Exception as e:
                        log.warning(f"Scan setups : {e!r}")
                    try:                             # §8 P4 : paper trading forward
                        paper_step(self.store, utc_now_ms(), log)
                    except Exception as e:
                        log.warning(f"Paper trading : {e!r}")
            except Exception as e:
                log.error(f"Cycle worker en échec (poursuite) : {e!r}")
                status = "degraded"
            self._beat_if_due(status)
            self._stop.wait(min(5, CONFIG.heartbeat_seconds))
        log.info("Arrêt propre du worker")
        self.stream.stop()
        self.store.close()

    def _install_signals(self) -> None:
        def _handler(_s, _f):
            log.info("Signal d'arrêt reçu")
            self._stop.set()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass


def main() -> None:
    Worker().run()


if __name__ == "__main__":
    main()
