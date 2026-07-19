"""Surveillance par étage (§5.13).

CUSUM sur les trades papier : S_t = max(0, S_{t−1} + (μ0 − k) − r_t),
μ0 = EV validée du trade (EV nette prudente de sa case au moment du ticket),
k = μ0/2, h = 4,5·σ_R. Lecture toutes les 25 clôtures d'étage. Alarme →
événement dans l'app + étage « en enquête » : tickets SUSPENDUS jusqu'à
acquittement MANUEL dans l'app (§6.2 — aucun canal externe).

Seuil d'arrêt glissant : moyenne des 50 derniers r comparée à EV − 2σ_R/√50.

Flux d'acquittement (un seul écrivain, §7.1) : la web app INSÈRE une action
`ack_alarm` dans web_actions ; le WORKER la consomme (process_web_actions) et
lève lui-même l'enquête. L'état de surveillance vit dans config_kv
(clé monitor_<stage>), écrit par le worker uniquement.
"""
from __future__ import annotations

import json
import math
from typing import Optional

import numpy as np

from .store import Store

READ_EVERY = 25          # lecture toutes les 25 clôtures (§5.13)
H_SIGMA = 4.5            # h = 4,5·σ_R (§5.13)
STOP_WINDOW = 50         # fenêtre 50 trades (§5.13)
MIN_TRADES_SIGMA = 10    # σ_R exige un minimum de trades


def _load(store: Store, stage: str) -> dict:
    raw = store.get_kv(f"monitor_{stage}")
    if raw:
        return json.loads(raw)
    return {"cusum": 0.0, "closes": 0, "en_enquete": False, "motif": None,
            "h": None, "seuil_arret": None, "moyenne_50": None}


def _save(store: Store, stage: str, state: dict) -> None:
    store.set_kv(f"monitor_{stage}", json.dumps(state))


def get_state(store: Store, stage: str) -> dict:
    return _load(store, stage)


def is_suspended(store: Store, stage: str) -> bool:
    return bool(_load(store, stage).get("en_enquete"))


def update_on_trade(store: Store, stage: str, r_result: float, mu0: float) -> None:
    """Met à jour le CUSUM à chaque trade CLOS de l'étage (§5.13)."""
    st = _load(store, stage)
    k = mu0 / 2.0
    st["cusum"] = max(0.0, st["cusum"] + (mu0 - k) - r_result)
    _save(store, stage, st)


def check_on_stage_close(store: Store, stage: str, journal_stage: list[dict]) -> None:
    """À chaque clôture d'étage : incrémente le compteur ; toutes les 25
    clôtures, lit le CUSUM contre h = 4,5·σ_R et le seuil d'arrêt glissant."""
    st = _load(store, stage)
    st["closes"] = st.get("closes", 0) + 1
    do_read = (st["closes"] % READ_EVERY == 0)

    r = np.array([t["r_result"] for t in journal_stage], dtype="float64")
    if len(r) >= MIN_TRADES_SIGMA:
        sigma_r = float(r.std(ddof=1))
        st["h"] = H_SIGMA * sigma_r
        # Seuil d'arrêt : EV VALIDÉE − 2σ_R/√50 (§5.13 : EV = μ0, l'EV validée —
        # moyenne des EV annoncées des trades, cohérent avec le μ0 du CUSUM).
        evs = [t.get("ev_annonce") for t in journal_stage
               if t.get("ev_annonce") is not None]
        ev_validee = float(np.mean(evs)) if evs else float(r.mean())
        last = r[-STOP_WINDOW:]
        st["moyenne_50"] = float(last.mean())
        st["seuil_arret"] = ev_validee - 2.0 * sigma_r / math.sqrt(STOP_WINDOW)
    else:
        sigma_r = None

    if do_read and not st.get("en_enquete"):
        if sigma_r is not None and st["h"] is not None and st["cusum"] > st["h"]:
            st["en_enquete"] = True
            st["motif"] = f"CUSUM {st['cusum']:.2f} > h {st['h']:.2f}"
            store.add_event("alarme_cusum", "error",
                            f"Alarme CUSUM étage {stage} — en enquête, tickets "
                            f"suspendus jusqu'à acquittement", {"stage": stage})
        elif (sigma_r is not None and st["seuil_arret"] is not None
              and len(r) >= STOP_WINDOW and st["moyenne_50"] < st["seuil_arret"]):
            st["en_enquete"] = True
            st["motif"] = (f"moyenne 50 trades {st['moyenne_50']:.2f} < seuil "
                           f"{st['seuil_arret']:.2f}")
            store.add_event("alarme_seuil", "error",
                            f"Seuil d'arrêt franchi étage {stage} — en enquête",
                            {"stage": stage})
    _save(store, stage, st)


def acknowledge(store: Store, stage: str) -> None:
    """Acquittement MANUEL (déclenché via web_actions, §7.1) : lève l'enquête
    et remet le CUSUM à zéro (redémarrage propre de la surveillance)."""
    st = _load(store, stage)
    st["en_enquete"] = False
    st["motif"] = None
    st["cusum"] = 0.0
    _save(store, stage, st)
    store.add_event("alarme_acquittee", "info",
                    f"Alarme étage {stage} acquittée — tickets réactivés",
                    {"stage": stage})


def process_web_actions(store: Store) -> int:
    """Consomme les actions web en attente (worker uniquement) : acquittement
    d'alarme ET marquage lu/non-lu (§7.1 : la web app ne fait que déposer dans
    la table dédiée). Curseur avancé par action (crash → au pire une action
    rejouée, idempotente). Retourne le nombre d'actions traitées."""
    last = int(store.get_kv("web_actions_cursor", "0") or 0)
    rows = store.conn.execute(
        "SELECT id, action, target FROM web_actions WHERE id > ? ORDER BY id",
        (last,)).fetchall()
    n_done = 0
    for row in rows:
        if row["action"] == "ack_alarm" and row["target"]:
            acknowledge(store, row["target"])
            n_done += 1
        elif row["action"] == "mark_read":
            up_to = int(row["target"]) if row["target"] else None
            store.mark_events_read(up_to)
            n_done += 1
        store.set_kv("web_actions_cursor", str(row["id"]))  # par action (§7.3)
    return n_done
