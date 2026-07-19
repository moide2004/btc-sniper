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

import json  # noqa: E402

from core import monitoring  # noqa: E402
from core.config import CONFIG  # noqa: E402
from core.couts import cost_in_r  # noqa: E402
from core.data_source import (  # noqa: E402
    BinanceKlineStream, DataSource, append_ohlcv, count_duplicates,
    find_gaps, last_open_time, load_ohlcv, resample_1m, MINUTE_MS, TF_MS,
)
from core.decision import DECISION_STAGES, build_ticket  # noqa: E402
from core.logging_setup import get_logger  # noqa: E402
from core.paper_trader import PaperTrader, verdict_card  # noqa: E402
from core.proba_engine import atr as atr_series  # noqa: E402
from core.risk_book import (  # noqa: E402
    check_new_position, monthly_stage_correlations, weighted_open_risk,
)
from core.states import current_state, daily_sma200_ref, state_labels  # noqa: E402
from core.store import Store, utc_now_iso, utc_now_ms  # noqa: E402

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
        self.ws_signal_q: "queue.Queue[tuple]" = queue.Queue()
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

        self._last_beat = 0.0  # horodatage du dernier battement émis

        # --- Moteur d'étages (P4 : §5.8–§5.13) ---
        self.trader = PaperTrader(
            self.store,
            can_open=self._can_open_position,
            on_trade_closed=self._on_trade_closed,
        )
        self._daily_ref_cache = None      # (ref, day_ms) — régime SMA200 (§4)
        self._daily_close_cache = None
        self._corr_cache: dict = {}

    # ----- Aides moteur d'étages -------------------------------------------
    def _beat_if_due(self, status: str = "ok") -> None:
        """Émet le battement s'il est dû (§7.3 : toutes les 30 s) — appelé
        AUSSI au milieu des phases lourdes pour que le bandeau ne passe pas
        faussement en ⚠ pendant les clôtures d'étage ou un rejeu."""
        now = time.time()
        if now - self._last_beat >= CONFIG.heartbeat_seconds:
            try:
                self.store.write_heartbeat(self.cycle, self.active_source, status)
                self._last_beat = now
            except Exception as e:
                log.error(f"Heartbeat : {e!r}")

    def _refresh_daily_ref(self, df_1m=None) -> None:
        """(Re)calcule la référence de régime quotidienne depuis le 1m —
        au démarrage puis à chaque nouveau jour UTC (zéro look-ahead §4).
        `df_1m` optionnel : réutilise un cache déjà chargé (évite une relecture
        du parquet complet)."""
        today_ms = (utc_now_ms() // 86_400_000) * 86_400_000
        if self._daily_ref_cache and self._daily_ref_cache[1] == today_ms:
            return
        own_load = df_1m is None
        if own_load:
            df_1m = load_ohlcv("1m")
        d1d = resample_1m(df_1m, "1D")
        self._daily_ref_cache = (daily_sma200_ref(d1d), today_ms)
        self._daily_close_cache = d1d["close"].to_numpy("float64") if not d1d.empty else None
        if own_load:
            del df_1m
        del d1d  # RAM disciplinée (§2.5)

    def _tables(self) -> dict:
        raw = self.store.get_kv("tables_latest")
        return json.loads(raw)["timeframes"] if raw else {}

    def _can_open_position(self, ticket_payload: dict) -> tuple[bool, str]:
        # §5.13 : un étage en enquête ne doit ouvrir AUCUNE position — le fill
        # d'un ticket resté en attente est refusé tant que l'alarme n'est pas
        # acquittée manuellement.
        if monitoring.is_suspended(self.store, ticket_payload["stage"]):
            return False, "bloqué : étage en enquête (alarme non acquittée)"
        self._corr_cache = monthly_stage_correlations(self.trader.journal())
        return check_new_position(ticket_payload, self.trader.open_positions(),
                                  self._corr_cache)

    def _on_trade_closed(self, entry: dict) -> None:
        mu0 = entry.get("ev_annonce") or 0.0
        monitoring.update_on_trade(self.store, entry["stage"],
                                   entry["r_result"], mu0)
        self._store_verdicts()

    def _store_verdicts(self) -> None:
        """Cartes de verdict par étage + globale (§5.11), stockées pour la web
        app (calcul côté worker : Monte Carlo trop lourd pour le polling)."""
        cards = {"global": verdict_card(self.trader.journal())}
        for stage in DECISION_STAGES:
            cards[stage] = verdict_card(self.trader.journal(stage))
        cards["correlations"] = self._corr_cache or \
            monthly_stage_correlations(self.trader.journal())
        self.store.set_kv("verdicts_latest", json.dumps(
            {"generated_at": utc_now_iso(), **cards}))

    def _stage_bars(self, stage: str, n_bars: int = 300, df_1m=None):
        if df_1m is None:
            df_1m = load_ohlcv("1m")
        need = n_bars * (TF_MS[stage] // MINUTE_MS)
        return resample_1m(df_1m.tail(need), stage)

    def _process_new_candles(self, rows: list[dict]) -> None:
        """Route chaque bougie 1m CLOSE (flux, repli REST ou REJEU post-panne)
        vers l'exécuteur virtuel, puis déclenche les clôtures d'étage détectées
        par horodatage. Un curseur persistant (config_kv) garantit qu'aucune
        bougie n'est traitée deux fois ni oubliée après un redémarrage (§7.3)."""
        cursor = int(self.store.get_kv("trader_1m_cursor", "0") or 0)
        stage_counts: dict[str, int] = {}
        last_ot = cursor
        for i, row in enumerate(sorted(rows, key=lambda r: r["open_time"])):
            ot = int(row["open_time"])
            if ot <= cursor:
                continue  # déjà traité (doublon du flux ou rejeu partiel)
            self.trader.on_1m_candle(row)
            last_ot = ot
            boundary = ot + MINUTE_MS
            for stage in DECISION_STAGES:
                if boundary % TF_MS[stage] == 0:
                    stage_counts[stage] = stage_counts.get(stage, 0) + 1
            if i % 500 == 499:
                self._beat_if_due()  # rejeu long : le battement continue (§7.3)
        if last_ot > cursor:
            self.store.set_kv("trader_1m_cursor", str(last_ot))
        if stage_counts:
            # UNE seule lecture du cache 1m pour toutes les clôtures du lot.
            df_1m = load_ohlcv("1m")
            self._refresh_daily_ref(df_1m)
            for stage, n_closes in stage_counts.items():
                self._beat_if_due()  # phase lourde : battement maintenu
                try:
                    self._on_stage_close(stage, n_closes, df_1m)
                except Exception as e:
                    log.error(f"Clôture d'étage {stage} : {e!r}")
            del df_1m

    def _replay_missed_candles(self) -> None:
        """Rejoue dans l'exécuteur les bougies écrites au cache pendant une
        panne (backfill) mais jamais routées vers lui — sinon des SL/TP
        survenus hors-ligne seraient perdus (§7.3)."""
        cursor = int(self.store.get_kv("trader_1m_cursor", "0") or 0)
        if cursor == 0:
            # premier démarrage : ne pas rejouer 2 ans d'historique — on part
            # du présent (le paper trading commence à la mise en service).
            last = last_open_time("1m") or 0
            self.store.set_kv("trader_1m_cursor", str(last))
            return
        df = load_ohlcv("1m")
        missed = df[df["open_time"] > cursor]
        del df
        if missed.empty:
            return
        log.info(f"Rejeu post-panne : {len(missed)} bougie(s) 1m vers l'exécuteur")
        self._process_new_candles(missed.to_dict("records"))

    def _on_stage_close(self, stage: str, n_closes: int = 1, df_1m=None) -> None:
        """À chaque clôture d'un étage de décision (1h/4h/1D) : invalidation,
        surveillance, puis décision (§5.8–§5.13). `n_closes` > 1 après une
        panne : les compteurs (3 bougies, cadence 25 lectures) rattrapent
        chaque clôture manquée ; la décision, elle, n'est prise qu'une fois,
        au présent."""
        self._refresh_daily_ref(df_1m)
        df = self._stage_bars(stage, df_1m=df_1m)
        if df is None or len(df) < 20:
            return
        labels, current = state_labels(df, stage, self._daily_ref_cache[0])

        # 1. Invalidation des tickets en attente (état quitté OU 3 bougies).
        for _ in range(max(1, n_closes)):
            self.trader.on_stage_close(stage, current)

        # 2. Surveillance (CUSUM + seuil, lecture toutes les 25 clôtures).
        for _ in range(max(1, n_closes)):
            monitoring.check_on_stage_close(self.store, stage,
                                            self.trader.journal(stage))
        self._store_verdicts()
        if monitoring.is_suspended(self.store, stage):
            return  # étage en enquête : tickets suspendus (§5.13)

        # 3. Décision : une seule exposition par étage à la fois.
        already = (any(t["payload"]["stage"] == stage for t in self.trader.pending_tickets())
                   or any(p["stage"] == stage for p in self.trader.open_positions()))
        if already:
            return
        tf_table = self._tables().get(stage)
        if not tf_table or tf_table.get("insuffisant"):
            return
        blk = tf_table.get("etats", {}).get(current)
        if not blk:
            return

        # Rafraîchit entrée/ATR/coût au prix LIVE (les probabilités restent
        # celles des tables quotidiennes — le prix d'entrée, lui, est actuel).
        live = dict(tf_table)
        a = atr_series(df)
        live["close"] = float(df["close"].iloc[-1])
        live["atr"] = float(a.iloc[-1]) if a.iloc[-1] == a.iloc[-1] else float("nan")
        live["cost_r"] = cost_in_r(CONFIG.fee_taker, live["close"], live["atr"])

        # v1.5 (§4) : si la dimension volatilité est ACTIVE pour cette case et
        # que la zone courante existe, la SOUS-CASE (état × zone) fait foi.
        zone = tf_table.get("vol_zone_courante")
        decision_blk = blk
        etat_decision = current
        if blk.get("vol_active") and zone in blk.get("vol_zones", {}):
            decision_blk = blk["vol_zones"][zone]
            etat_decision = f"{current} [vol {zone}]"

        for direction in ("long", "short"):
            d = decision_blk[direction]
            if not d.get("candidate"):
                continue
            ticket = build_ticket(stage, etat_decision, direction, d, live,
                                  self._daily_close_cache, ts_utc=utc_now_iso())
            if ticket is None:
                continue
            ok, motif = self._can_open_position(ticket)
            if not ok and self._same_recent_block(stage, current, direction, motif):
                break  # déjà signalé à la clôture précédente : pas de spam
            self.trader.emit_ticket(ticket, blocked_reason=None if ok else motif)
            break  # une exposition par étage

    def _same_recent_block(self, stage: str, etat: str, direction: str,
                           motif: str) -> bool:
        """Vrai si le DERNIER ticket de cet étage est déjà un blocage identique
        (même état, sens, motif) — évite un événement warning par clôture."""
        row = self.store.conn.execute(
            "SELECT payload FROM tickets WHERE stage = ? ORDER BY id DESC LIMIT 1",
            (stage,)).fetchone()
        if not row:
            return False
        p = json.loads(row["payload"])
        return (p.get("status") == "blocked" and p.get("etat") == etat
                and p.get("direction") == direction
                and p.get("motif_blocage") == motif)

    # ----- Callbacks websocket ---------------------------------------------
    # IMPORTANT (§7.1 interne) : ces callbacks s'exécutent dans le THREAD du
    # flux. Ils ne font que déposer un signal dans une file ; toutes les
    # écritures (parquet, SQLite) restent dans le thread principal.
    def _on_stream_reconnect(self) -> None:
        self.ws_signal_q.put(("reconnect", None))

    def _on_stream_status(self, _kind: str, status: str) -> None:
        self.ws_signal_q.put(("status", status))

    def _drain_ws_signals(self) -> None:
        """Traite (thread principal) les signaux déposés par le thread du flux."""
        while not self.ws_signal_q.empty():
            kind, status = self.ws_signal_q.get_nowait()
            if kind == "reconnect":
                try:
                    filled = self.ds.fill_gaps()
                    if filled:
                        log.info(f"Backfill post-reconnexion : {filled} bougie(s)")
                except Exception as e:
                    log.warning(f"Backfill post-reconnexion échoué : {e!r}")
            elif kind == "status":
                log.info(f"Websocket {status}")
                if status == "connected":
                    self.active_source = "binance"
                    if self.ingest_mode == "rest":
                        self.ingest_mode = "stream"
                        self._switched_to_rest = False
                        self.store.add_event(
                            "data_incident", "info",
                            "Retour au flux websocket primaire (Binance)")

    # ----- Reprise (§7.3) ---------------------------------------------------
    def startup_recovery(self) -> None:
        log.info("Démarrage worker : reprise de l'état persistant")
        # Battement immédiat : le bandeau montre le redémarrage plutôt qu'un
        # faux « worker absent » pendant un long backfill (§7.3).
        self._beat_if_due()
        last = last_open_time("1m")
        if last is None:
            log.info(f"Cache 1m vide → backfill initial {CONFIG.backfill_days} j")
            added = self.ds.ensure_backfill(CONFIG.backfill_days)
            log.info(f"Backfill initial : {added} bougie(s) 1m")
        else:
            added = self.ds.ensure_backfill(CONFIG.backfill_days)
            if added:
                log.info(f"Comblé depuis le dernier point : {added} bougie(s)")
        self._beat_if_due()

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
    def _drain_stream_candles(self) -> list[dict]:
        rows = []
        while not self.candle_q.empty():
            rows.append(self.candle_q.get_nowait())
        if rows:
            import pandas as pd
            append_ohlcv(pd.DataFrame(rows), "1m")
            log.info(f"{len(rows)} bougie(s) 1m closes reçues (flux)")
        return rows

    def _rest_fallback_poll(self) -> list[dict]:
        got = self.ds.poll_recent(lookback_min=180)
        if got.empty:
            return []
        before = last_open_time("1m") or 0
        append_ohlcv(got, "1m")
        self.active_source = self.ds.active
        new = got[got["open_time"] > before]
        if len(new):
            log.info(f"Repli REST ({self.active_source}) : {len(new)} bougie(s) closes")
        return new.to_dict("records")

    # ----- Boucle principale ------------------------------------------------
    def run(self) -> None:
        self._install_signals()
        self.startup_recovery()
        # Rejeu des bougies écrites au cache pendant l'arrêt : les SL/TP et
        # invalidations survenus hors-ligne sont rattrapés AVANT le direct (§7.3).
        try:
            self._replay_missed_candles()
        except Exception as e:
            log.error(f"Rejeu post-panne : {e!r}")

        stream_thread = threading.Thread(target=self.stream.run_forever, daemon=True)
        stream_thread.start()

        while not self._stop.is_set():
            self.cycle += 1
            status = "ok"
            try:
                self._drain_ws_signals()
                new_rows = self._drain_stream_candles()

                stream_age = self.stream.last_message_age_s()
                grace_over = (utc_now_ms() - self.started_ms) / 1000 > CONNECT_GRACE_S
                stream_healthy = stream_age < CONFIG.primary_silence_seconds

                if not stream_healthy and grace_over:
                    # Bascule vers le repli REST (§7.4).
                    if not self._switched_to_rest:
                        self._switched_to_rest = True
                        self.ingest_mode = "rest"
                        self.store.add_event(
                            "data_incident", "warning",
                            "Flux primaire muet → ingestion REST de repli",
                            {"stream_age_s": None if stream_age == float("inf")
                             else round(stream_age, 1)},
                        )
                        log.warning("Bascule ingestion → REST de repli")
                    new_rows.extend(self._rest_fallback_poll())
                    status = "degraded"

                # Moteur d'étages : fills/sorties 1m + clôtures d'étage.
                if new_rows:
                    try:
                        self._process_new_candles(new_rows)
                    except Exception as e:
                        log.error(f"Moteur d'étages : {e!r}")

                # Acquittements/marquages déposés par la web app (§7.1).
                try:
                    monitoring.process_web_actions(self.store)
                except Exception as e:
                    log.warning(f"web_actions : {e!r}")

                # Maintenance périodique : continuité, comblage, purge cloche.
                if self.cycle % MAINTENANCE_EVERY == 0:
                    try:
                        self.ds.fill_gaps()
                        self.store.prune_events(90)
                    except Exception as e:
                        log.warning(f"Maintenance : {e!r}")
            except Exception as e:
                # Un cycle raté ne doit pas tuer l'always-on : les bougies
                # perdues seront récupérées par backfill + rejeu (§7.3).
                log.error(f"Cycle worker en échec (poursuite) : {e!r}")
                status = "degraded"

            # Heartbeat toutes les HEARTBEAT_SECONDS (§7.3).
            self._beat_if_due(status)

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
