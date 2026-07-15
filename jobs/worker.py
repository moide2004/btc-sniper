"""Always-on task : worker événementiel sur clôtures 1m (§2.1, §7.3).

IDEMPOTENT au redémarrage : PythonAnywhere relance automatiquement une
always-on task qui plante ; la reprise doit être invisible.

Séquence de démarrage (reprise, §7.3) :
  1. relire l'état persistant (cache 1m, heartbeat) ;
  2. combler les bougies manquantes depuis le dernier point connu (REST) ;
  3. contrôle de continuité ;
  4. recalculer les états courants ;
  5. reprendre le flux (websocket primaire) — événement « worker redémarré ».

Un SEUL écrivain (§7.1) : au sein du worker, toutes les écritures du cache 1m
passent par le thread principal ; le thread websocket ne fait que déposer les
bougies closes dans une file.

Chemins d'ingestion :
  * PRIMAIRE  : websocket Binance (BinanceKlineStream) ;
  * REPLI     : sondage REST (DataSource.poll_recent) si le flux est muet
                > PRIMARY_SILENCE_SECONDS (§7.4) ou ne se connecte pas.
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
    BinanceKlineStream, DataSource, append_ohlcv, count_duplicates,
    find_gaps, last_open_time, load_ohlcv, MINUTE_MS,
)
from core.logging_setup import get_logger  # noqa: E402
from core.states import current_state  # noqa: E402
from core.store import Store, utc_now_ms  # noqa: E402

log = get_logger("worker")

# Délai de grâce avant de basculer en REST si le websocket ne connecte jamais.
CONNECT_GRACE_S = 25
# Fréquence des contrôles de continuité / comblage de trous (en ticks).
MAINTENANCE_EVERY = 20


class Worker:
    def __init__(self) -> None:
        CONFIG.ensure_dirs()
        self.store = Store()
        self.ds = DataSource(store=self.store, logger=log)
        self.candle_q: "queue.Queue[dict]" = queue.Queue()
        self.stream = BinanceKlineStream(
            on_closed_candle=self.candle_q.put,
            on_reconnect=self._on_stream_reconnect,
            on_status=self._on_stream_status,
            logger=log,
        )
        self.cycle = 0
        self.started_ms = utc_now_ms()
        self.active_source = self.ds.active
        self.ingest_mode = "stream"       # stream | rest
        self._stop = threading.Event()
        self._switched_to_rest = False

    # ----- Callbacks websocket ---------------------------------------------
    def _on_stream_reconnect(self) -> None:
        # À chaque (re)connexion : combler le trou REST (§7.4).
        try:
            filled = self.ds.fill_gaps()
            if filled:
                log.info(f"Backfill post-reconnexion : {filled} bougie(s)")
        except Exception as e:
            log.warning(f"Backfill post-reconnexion échoué : {e!r}")

    def _on_stream_status(self, _kind: str, status: str) -> None:
        log.info(f"Websocket {status}")
        if status == "connected":
            self.active_source = "binance"
            if self.ingest_mode == "rest":
                self.ingest_mode = "stream"
                self._switched_to_rest = False
                self.store.add_event("data_incident", "info",
                                     "Retour au flux websocket primaire (Binance)")

    # ----- Reprise (§7.3) ---------------------------------------------------
    def startup_recovery(self) -> None:
        log.info("Démarrage worker : reprise de l'état persistant")
        last = last_open_time("1m")
        if last is None:
            log.info(f"Cache 1m vide → backfill initial {CONFIG.backfill_days} j")
            added = self.ds.ensure_backfill(CONFIG.backfill_days)
            log.info(f"Backfill initial : {added} bougie(s) 1m")
        else:
            added = self.ds.ensure_backfill(CONFIG.backfill_days)
            if added:
                log.info(f"Comblé depuis le dernier point : {added} bougie(s)")

        filled = self.ds.fill_gaps()
        df = load_ohlcv("1m")
        gaps = find_gaps(df)
        dups = count_duplicates(df)
        log.info(f"Continuité 1m : {len(df)} bougies, {len(gaps)} trou(s) "
                 f"restant(s), {dups} doublon(s), comblés={filled}")

        self.active_source = self.ds.active  # reflète une éventuelle bascule au backfill
        self._recompute_states(df)
        self.store.add_event(
            "worker", "info", "Worker (re)démarré — reprise effectuée",
            {"bars_1m": int(len(df)), "gaps": len(gaps), "filled_on_start": filled},
        )
        self.store.write_heartbeat(self.cycle, self.active_source, "ok")

    def _recompute_states(self, df_1m) -> None:
        try:
            st = current_state(df_1m.tail(500), "1m")
            self.store.set_kv("state_1m", st.label)
            log.info(f"État courant 1m : {st.label} (RSI={st.rsi})")
        except Exception as e:
            log.warning(f"Recalcul d'état échoué : {e!r}")

    # ----- Ingestion --------------------------------------------------------
    def _drain_stream_candles(self) -> int:
        n = 0
        rows = []
        while not self.candle_q.empty():
            rows.append(self.candle_q.get_nowait())
            n += 1
        if rows:
            import pandas as pd
            append_ohlcv(pd.DataFrame(rows), "1m")
            log.info(f"{n} bougie(s) 1m closes reçues (flux)")
        return n

    def _rest_fallback_poll(self) -> int:
        got = self.ds.poll_recent(lookback_min=180)
        if got.empty:
            return 0
        before = last_open_time("1m") or 0
        append_ohlcv(got, "1m")
        after = last_open_time("1m") or 0
        added = int((after - before) // MINUTE_MS) if after > before else 0
        self.active_source = self.ds.active
        if added:
            log.info(f"Repli REST ({self.active_source}) : {added} bougie(s) closes")
        return added

    # ----- Boucle principale ------------------------------------------------
    def run(self) -> None:
        self._install_signals()
        self.startup_recovery()

        stream_thread = threading.Thread(target=self.stream.run_forever, daemon=True)
        stream_thread.start()

        last_beat = 0.0
        while not self._stop.is_set():
            self.cycle += 1
            now = time.time()

            self._drain_stream_candles()

            stream_age = self.stream.last_message_age_s()
            grace_over = (utc_now_ms() - self.started_ms) / 1000 > CONNECT_GRACE_S
            stream_healthy = stream_age < CONFIG.primary_silence_seconds

            status = "ok"
            if not stream_healthy and grace_over:
                # Bascule vers le repli REST (§7.4).
                if not self._switched_to_rest:
                    self._switched_to_rest = True
                    self.ingest_mode = "rest"
                    self.store.add_event(
                        "data_incident", "warning",
                        "Flux primaire muet → ingestion REST de repli",
                        {"stream_age_s": None if stream_age == float("inf") else round(stream_age, 1)},
                    )
                    log.warning("Bascule ingestion → REST de repli")
                self._rest_fallback_poll()
                status = "degraded"

            # Maintenance périodique : continuité, comblage, purge cloche.
            if self.cycle % MAINTENANCE_EVERY == 0:
                try:
                    self.ds.fill_gaps()
                    self.store.prune_events(90)
                except Exception as e:
                    log.warning(f"Maintenance : {e!r}")

            # Heartbeat toutes les HEARTBEAT_SECONDS (§7.3).
            if now - last_beat >= CONFIG.heartbeat_seconds:
                self.store.write_heartbeat(self.cycle, self.active_source, status)
                last_beat = now

            self._stop.wait(min(5, CONFIG.heartbeat_seconds))

        log.info("Arrêt propre du worker")
        self.stream.stop()
        self.store.close()

    # ----- Signaux ----------------------------------------------------------
    def _install_signals(self) -> None:
        def _handler(_signum, _frame):
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
