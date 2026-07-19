"""Exécuteur virtuel — le banc de validation (§5.11).

Fills simulés sur flux 1m RÉEL :
  * entrée : limite touchée (contact), sinon expiration à l'invalidation ;
  * SL : contact → perte 1 R × slippage (×1,3 long / ×1,5 short, §5.11, §5.14) ;
  * TP : au contact → gain +RR ;
  * coûts appliqués (cost_r du ticket, en R).
Aucun ordre réel, jamais (§9).

Journal complet par trade : horodatage, étage, état, direction, p annoncé,
RR retenu (= meilleure EV prudente), taille, issue en R.

Carte de verdict par étage + globale (§5.11) : n, p̂ + Wilson, posterior +
p_prudent, EV réalisée, σ_R, t-stat, série max vs attendue, Monte Carlo 10 000
permutations (DD médian, p95), profit factor, Brier.

Résolution intra-bougie ambiguë (SL et TP touchés dans la même bougie 1m) :
issue DÉFAVORABLE (stop d'abord) — même convention conservatrice que la
mesure double barrière (§5.1).
"""
from __future__ import annotations

import json
import math
from typing import Optional

import numpy as np

from .execution import entry_improvement_r, is_filled, limit_price
from .proba_engine import beta_posterior, wilson_interval
from .store import Store, utc_now_iso

SLIP_LONG = 1.3    # §5.11
SLIP_SHORT = 1.5   # §5.11, §5.14
MC_PERMUTATIONS = 10_000


# ===========================================================================
# Persistance (tickets / positions / journal via Store, payload JSON)
# ===========================================================================
def _rows(store: Store, table: str) -> list[dict]:
    out = []
    for r in store.conn.execute(f"SELECT * FROM {table} ORDER BY id"):
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        out.append(d)
    return out


class PaperTrader:
    """Gère tickets en attente, positions ouvertes et journal. Un seul écrivain :
    instancié UNIQUEMENT dans le worker (§7.1).

    `can_open(ticket_payload)` : re-validation au moment du FILL (budget,
    positions opposées, §5.9) — fournie par le worker. `on_trade_closed(entry)` :
    callback par trade clos (mise à jour CUSUM, §5.13)."""

    def __init__(self, store: Store, can_open=None, on_trade_closed=None) -> None:
        self.store = store
        self.can_open = can_open
        self.on_trade_closed = on_trade_closed

    # ----- lectures ---------------------------------------------------------
    def pending_tickets(self) -> list[dict]:
        return [t for t in _rows(self.store, "tickets")
                if t["payload"].get("status") == "pending"]

    def open_positions(self) -> list[dict]:
        return [p["payload"] | {"row_id": p["id"]}
                for p in _rows(self.store, "positions")
                if p["payload"].get("status") == "open"]

    def journal(self, stage: Optional[str] = None) -> list[dict]:
        rows = [j["payload"] for j in _rows(self.store, "journal")]
        return [r for r in rows if stage is None or r.get("stage") == stage]

    # ----- émission d'un ticket (appelé par le worker à la clôture d'étage) --
    def emit_ticket(self, ticket: dict, blocked_reason: Optional[str] = None) -> int:
        ticket = dict(ticket)
        if blocked_reason:
            ticket["status"] = "blocked"
            ticket["motif_blocage"] = blocked_reason
        else:
            ticket["status"] = "pending"
            ticket["limite"] = limit_price(ticket["entree"], ticket["atr"],
                                           ticket["direction"])
            ticket["bougies_attente"] = 0
        with self.store.conn:
            cur = self.store.conn.execute(
                "INSERT INTO tickets (ts_utc, stage, payload) VALUES (?,?,?)",
                (ticket.get("ts_utc") or utc_now_iso(), ticket["stage"],
                 json.dumps(ticket)))
            rid = int(cur.lastrowid)
        kind = "ticket_bloque" if blocked_reason else "ticket_emis"
        msg = (f"Ticket {ticket['stage']} {ticket['direction']} — {blocked_reason}"
               if blocked_reason else
               f"Ticket émis : {ticket['stage']} {ticket['direction']} "
               f"(état {ticket['etat']}, RR {ticket['rr_retenu']})")
        self.store.add_event(kind, "warning" if blocked_reason else "info", msg,
                             {"ticket_id": rid})
        return rid

    def _update_ticket(self, row_id: int, payload: dict) -> None:
        with self.store.conn:
            self.store.conn.execute("UPDATE tickets SET payload = ? WHERE id = ?",
                                    (json.dumps(payload), row_id))

    # ----- flux 1m : fills, SL/TP -------------------------------------------
    def on_1m_candle(self, candle: dict) -> None:
        """À chaque bougie 1m CLOSE : tente les fills des limites, puis les
        sorties SL/TP des positions ouvertes."""
        self._try_fills(candle)
        self._try_exits(candle)

    def _try_fills(self, candle: dict) -> None:
        for t in self.pending_tickets():
            p = t["payload"]
            if is_filled(candle, p["limite"], p["direction"]):
                # Re-validation au fill (§5.9) : le livre a pu changer depuis
                # l'émission (position opposée ouverte, budget consommé).
                if self.can_open is not None:
                    ok, motif = self.can_open(p)
                    if not ok:
                        p["status"] = "expired"
                        p["motif_expiration"] = motif
                        self._update_ticket(t["id"], p)
                        self.store.add_event(
                            "ticket_bloque", "warning",
                            f"Fill refusé {p['stage']} {p['direction']} — {motif}",
                            {"ticket_id": t["id"]})
                        continue
                fill = p["limite"]  # limite touchée → exécutée à son prix
                impr = entry_improvement_r(p["entree"], fill, p["atr"], p["direction"])
                sign = 1.0 if p["direction"] == "long" else -1.0
                pos = {
                    "status": "open", "stage": p["stage"], "etat": p["etat"],
                    "direction": p["direction"], "ticket_id": t["id"],
                    "opened_utc": utc_now_iso(),
                    "entree_marche": p["entree"], "fill": fill, "atr": p["atr"],
                    "sl": fill - sign * p["atr"],
                    "tp": fill + sign * p["rr_retenu"] * p["atr"],
                    "rr": p["rr_retenu"], "p_annonce": p["p_annonce"],
                    "ev_annonce": p["ev_nette_prudente"],  # μ0 du CUSUM (§5.13)
                    "cost_r": p["cost_r"],
                    "risque_pct": p["taille"]["risque_pct"],
                    "risque_usd": p["taille"]["risque_usd"],
                    "kpi_amelioration_r": impr,
                }
                # UNE transaction : position créée ET ticket consommé ensemble
                # (§7.3 : un crash entre les deux dupliquerait la position).
                p["status"] = "filled"
                with self.store.conn:
                    self.store.conn.execute(
                        "INSERT INTO positions (opened_utc, stage, payload) VALUES (?,?,?)",
                        (pos["opened_utc"], pos["stage"], json.dumps(pos)))
                    self.store.conn.execute(
                        "UPDATE tickets SET payload = ? WHERE id = ?",
                        (json.dumps(p), t["id"]))
                self.store.add_event(
                    "ticket_execute", "info",
                    f"Ticket exécuté : {p['stage']} {p['direction']} @ {fill:.2f} "
                    f"(amélioration {impr:+.2f}R)", {"ticket_id": t["id"]})

    def _try_exits(self, candle: dict) -> None:
        low, high = float(candle["low"]), float(candle["high"])
        for pos in self.open_positions():
            rid = pos.pop("row_id")
            if pos["direction"] == "long":
                hit_sl, hit_tp = low <= pos["sl"], high >= pos["tp"]
                slip = SLIP_LONG
            else:
                hit_sl, hit_tp = high >= pos["sl"], low <= pos["tp"]
                slip = SLIP_SHORT
            if not (hit_sl or hit_tp):
                continue
            if hit_sl:  # conservateur : stop prioritaire en cas d'ambiguïté
                r = -1.0 * slip - pos["cost_r"]
                issue = "sl"
            else:
                r = pos["rr"] - pos["cost_r"]
                issue = "tp"
            pos["status"] = "closed"
            pos["issue"] = issue
            pos["r_result"] = r
            pos["closed_utc"] = utc_now_iso()
            # UNE transaction : clôture de position ET ligne de journal (§7.3).
            entry = self._journal_entry(pos)
            with self.store.conn:
                self.store.conn.execute(
                    "UPDATE positions SET payload = ?, closed_utc = ? WHERE id = ?",
                    (json.dumps(pos), pos["closed_utc"], rid))
                self.store.conn.execute(
                    "INSERT INTO journal (ts_utc, stage, payload) VALUES (?,?,?)",
                    (entry["ts_utc"], entry["stage"], json.dumps(entry)))
            if self.on_trade_closed is not None:
                self.on_trade_closed(entry)  # CUSUM par trade (§5.13)
            self.store.add_event(
                "position_close", "info",
                f"Position {pos['stage']} {pos['direction']} close : {issue.upper()} "
                f"({r:+.2f}R)", {"position_id": rid})

    @staticmethod
    def _journal_entry(pos: dict) -> dict:
        return {
            "ts_utc": pos["closed_utc"], "stage": pos["stage"], "etat": pos["etat"],
            "direction": pos["direction"], "p_annonce": pos["p_annonce"],
            "ev_annonce": pos.get("ev_annonce"),
            "rr_retenu": pos["rr"], "taille_pct": pos["risque_pct"],
            "issue": pos["issue"], "r_result": pos["r_result"],
            "kpi_amelioration_r": pos.get("kpi_amelioration_r", 0.0),
        }

    # ----- clôture d'étage : invalidation (état quitté OU 3 bougies, §5.8) --
    def on_stage_close(self, stage: str, current_state: str) -> None:
        for t in self.pending_tickets():
            p = t["payload"]
            if p["stage"] != stage:
                continue
            p["bougies_attente"] = p.get("bougies_attente", 0) + 1
            reason = None
            # v1.5 : un ticket de sous-case porte « état [vol zone] » ; l'inva-
            # lidation « état quitté » se juge sur l'état PARENT (la zone de
            # vol, percentile 1 an, ne bouge qu'à l'échelle du jour).
            etat_parent = p["etat"].split(" [")[0]
            if etat_parent != current_state:
                reason = "état quitté"
            elif p["bougies_attente"] >= p["invalidation"]["bougies_max"]:
                reason = "3 bougies sans exécution"
            if reason:
                p["status"] = "expired"
                p["motif_expiration"] = reason
                self._update_ticket(t["id"], p)
                self.store.add_event(
                    "ticket_expire", "info",
                    f"Ticket expiré : {p['stage']} {p['direction']} — {reason}",
                    {"ticket_id": t["id"]})
            else:
                self._update_ticket(t["id"], p)


# ===========================================================================
# Carte de verdict (§5.11)
# ===========================================================================
def _max_streak(outcomes: list[bool], value: bool) -> int:
    best = cur = 0
    for o in outcomes:
        cur = cur + 1 if o == value else 0
        best = max(best, cur)
    return best


def verdict_card(trades: list[dict]) -> dict:
    """Carte de verdict d'une liste de trades du journal (§5.11)."""
    n = len(trades)
    if n == 0:
        return {"n": 0}
    r = np.array([t["r_result"] for t in trades], dtype="float64")
    wins = [bool(t["r_result"] > 0) for t in trades]
    k = sum(wins)
    p_hat = k / n
    w = wilson_interval(k, n)
    post = beta_posterior(k, n)
    sigma_r = float(r.std(ddof=1)) if n > 1 else float("nan")
    ev = float(r.mean())
    t_stat = (ev / (sigma_r / math.sqrt(n))) if n > 1 and sigma_r > 0 else float("nan")

    # Série perdante max observée vs attendue ≈ ln(n)/ln(1/(1−p̂)).
    streak_obs = _max_streak(wins, False)
    if 0 < p_hat < 1:
        streak_att = math.log(n) / math.log(1 / (1 - p_hat)) if n > 1 else 1.0
    else:
        streak_att = float("nan")

    # Monte Carlo 10 000 permutations : distribution du drawdown max (§5.11).
    rng = np.random.default_rng(0)  # déterministe : même journal → même carte
    dds = np.empty(MC_PERMUTATIONS)
    for i in range(MC_PERMUTATIONS):
        perm = rng.permutation(r)
        eq = np.cumsum(perm)
        dds[i] = float(np.max(np.maximum.accumulate(eq) - eq))
    gains = float(r[r > 0].sum())
    pertes = float(-r[r < 0].sum())
    pf = gains / pertes if pertes > 0 else float("inf")
    brier = float(np.mean([(t["p_annonce"] - (1.0 if w_ else 0.0)) ** 2
                           for t, w_ in zip(trades, wins)]))
    return {
        "n": n, "k": k, "p_hat": p_hat,
        "wilson": {"low": w.low, "high": w.high},
        "posterior": {"mean": post.mean, "p_prudent": post.p_prudent},
        "ev_realisee": ev, "sigma_r": sigma_r, "t_stat": t_stat,
        "serie_perdante_max": streak_obs, "serie_attendue": streak_att,
        "mc_dd_median": float(np.median(dds)), "mc_dd_p95": float(np.quantile(dds, 0.95)),
        "profit_factor": pf, "brier": brier,
        "kpi_amelioration_moy_r": float(np.mean([t.get("kpi_amelioration_r", 0.0)
                                                 for t in trades])),
    }
