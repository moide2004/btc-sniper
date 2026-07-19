"""Backfill UNIQUE de l'historique 1m profond (2017 → début du cache actuel).

À lancer UNE FOIS à la main :
    python jobs/backfill_1m_profond.py

~3,6 millions de bougies (≈ 3 800 requêtes REST Binance) : compter 15 à 30
minutes. RÉSUMABLE : si la console se ferme ou que ça coupe, relancer la même
commande — les mois déjà téléchargés sont sautés.

Après ce backfill, lancer `python jobs/daily_update.py` pour reconstruire
toutes les échelles (5m/15m/30m gagnent alors aussi leurs 9 ans).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import CONFIG  # noqa: E402
from core.data_source import backfill_deep_1m, cache_stats  # noqa: E402
from core.logging_setup import get_logger  # noqa: E402
from core.store import Store  # noqa: E402

log = get_logger("backfill_profond")


def main() -> None:
    CONFIG.ensure_dirs()
    n_avant, _ = cache_stats("1m")
    log.info(f"=== Backfill 1m profond : début ({n_avant} bougies en cache) ===")
    t0 = time.time()
    store = Store()
    try:
        added = backfill_deep_1m(logger=log, store=store)
    finally:
        store.close()
    n_apres, _ = cache_stats("1m")
    log.info(f"=== Backfill 1m profond : fin — +{added} bougies en "
             f"{(time.time() - t0) / 60:.1f} min ({n_apres} au total) ===")
    if added == 0 and n_apres == n_avant:
        log.info("Rien à ajouter : l'historique profond est déjà complet.")


if __name__ == "__main__":
    main()
