"""Tâche planifiée quotidienne (00:10 UTC) — recalcul LOURD 1×/jour (§2.5).

P1 (socle) réalise ici :
  * ré-échantillonnage 1m → toutes les timeframes ≤ 1D + agrégation 1W/2W/1M
    depuis le 1D (§2.3), caches parquet ;
  * contrôle de continuité + comblage des trous (§7.4) ;
  * mesure et journalisation du budget ressources : durée, CPU, pic mémoire
    (§7.6) — événement « performance » si durée > 15 min ;
  * vérification de dérive d'horloge contre l'exchange (§7.9) ;
  * sauvegarde cohérente de moteur.db + rétention 14 j (§7.7).

Les tables statistiques et le walk-forward (§5.11–5.12) seront branchés ici
en P2, sans toucher à cette ossature (§9).
"""
from __future__ import annotations

import resource
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json  # noqa: E402

from core.config import CONFIG  # noqa: E402
from core.data_source import (  # noqa: E402
    DataSource, count_duplicates, fetch_funding_annualized_7d, find_gaps, load_ohlcv,
    resample_1m, resample_from_1d, save_ohlcv_atomic, TF_MS,
)
from core.logging_setup import get_logger  # noqa: E402
from core.proba_engine import compute_all_tables, compute_matrix  # noqa: E402
from core.stack import StackEntry, lognormal_reference, stack  # noqa: E402
from core.states import daily_sma200_ref  # noqa: E402
from core.walkforward import disqualified_set, walkforward  # noqa: E402
from core.store import Store, utc_now_iso  # noqa: E402

log = get_logger("daily")

# Agrégations natives (≤1D depuis 1m ; 1W/2W/1M depuis 1D — §2.3).
RESAMPLE_FROM_1M = ["5m", "15m", "30m", "1h", "4h", "12h", "1D"]
RESAMPLE_FROM_1D = ["1W", "2W", "1M"]


def rebuild_timeframes(store: Store | None = None) -> dict[str, int]:
    """Reconstruit les caches (§2.3) : là où le 1m existe, il est LA vérité de
    prix ; AVANT lui, historique NATIF (Binance, depuis 2017) pour les grosses
    échelles 1h/4h/12h/1D — puis 1W/2W/1M agrégées du 1D combiné.
    Retourne le nb de bougies/TF."""
    from core.data_source import (combine_native_recent, ensure_native_history,
                                  load_native)

    df_1m = load_ohlcv("1m")
    counts = {"1m": len(df_1m)}
    if df_1m.empty:
        log.warning("Cache 1m vide : rien à ré-échantillonner")
        return counts

    # Historique profond (téléchargé une fois ; échec → poursuite sur 1m seul).
    ensure_native_history(logger=log, store=store)

    # Échelles fines : 1m uniquement (le natif minute profond n'apporte rien
    # de plus aux n déjà énormes, et pèserait des millions de lignes).
    for tf in ("5m", "15m", "30m"):
        agg = resample_1m(df_1m, tf)
        save_ohlcv_atomic(agg, tf)
        counts[tf] = len(agg)
        del agg  # libération mémoire (§2.5)

    # 1h : natif profond + récent depuis le 1m.
    recent_1h = resample_1m(df_1m, "1h")
    combined_1h = combine_native_recent(load_native("1h"), recent_1h)
    save_ohlcv_atomic(combined_1h, "1h")
    counts["1h"] = len(combined_1h)
    del recent_1h

    # 4h / 12h : agrégés du 1h combiné (partition exacte : 1m→1h→4h ≡ 1m→4h
    # sur la période 1m ; natif 1h→4h avant).
    for tf in ("4h", "12h"):
        agg = resample_1m(combined_1h, tf, src_ms=TF_MS["1h"])
        save_ohlcv_atomic(agg, tf)
        counts[tf] = len(agg)
        del agg
    del combined_1h

    # 1D : natif profond + récent depuis le 1m.
    recent_1d = resample_1m(df_1m, "1D")
    combined_1d = combine_native_recent(load_native("1D"), recent_1d)
    save_ohlcv_atomic(combined_1d, "1D")
    counts["1D"] = len(combined_1d)
    del df_1m, recent_1d

    # 1W / 2W / 1M : agrégées du 1D combiné (§2.3).
    for tf in RESAMPLE_FROM_1D:
        agg = resample_from_1d(combined_1d, tf)
        save_ohlcv_atomic(agg, tf)
        counts[tf] = len(agg)
        del agg
    del combined_1d
    log.info("Ré-échantillonnage : " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return counts


def compute_and_store_matrix(store: Store) -> int:
    """Calcule la matrice des 11 timeframes (§5) + tables complètes (Vue 2 et
    décisions) + walk-forward (§5.12) + synthèse bayésienne (§5.6) + référence
    neutre (§5.7). Retourne le nombre de cases candidates (Vue 1)."""
    df_1d = load_ohlcv("1D")
    daily_ref = daily_sma200_ref(df_1d)

    # Funding perpétuel (§5.14) : moyen 7 j annualisé ; None → F=0 + badge.
    funding = fetch_funding_annualized_7d()
    store.set_kv("funding_latest", json.dumps({
        "generated_at": utc_now_iso(), "annualized": funding,
        "integre": funding is not None}))
    if funding is None:
        log.warning("Funding indisponible → F=0 + badge « funding non intégré »")
    else:
        log.info(f"Funding 7j annualisé : {funding:+.4%}")

    matrix = compute_matrix(load_ohlcv, daily_ref, CONFIG.fee_taker, funding)

    # Walk-forward (§5.12) : train = historique − 12 mois, test = 12 mois exclus.
    try:
        wf = walkforward(load_ohlcv, daily_sma200_ref, CONFIG.fee_taker)
        disq = disqualified_set(wf)
        store.set_kv("walkforward_latest", json.dumps(
            {"generated_at": utc_now_iso(), **wf}))
        log.info(f"Walk-forward : {len(wf['cases'])} cases, "
                 f"{len(disq)} disqualifiée(s) (overfit)")
    except Exception as e:
        wf, disq = {"cases": []}, set()
        log.error(f"Walk-forward échoué : {e!r}")

    # Tables complètes (tous états × directions) : Vue 2 + décisions du worker.
    tables = compute_all_tables(load_ohlcv, daily_ref, CONFIG.fee_taker, funding)
    # Badge walk-forward reporté sur chaque case des tables complètes.
    badge_by_case = {(c["timeframe"], c["etat"], c["direction"]): c["badge"]
                     for c in wf.get("cases", [])}
    for tf_name, tf_table in tables.items():
        for state, blk in tf_table.get("etats", {}).items():
            for direction in ("long", "short"):
                badge = badge_by_case.get((tf_name, state, direction), "n/a")
                blk[direction]["walkforward"] = badge
                if badge == "overfit" and blk[direction].get("candidate"):
                    blk[direction]["candidate"] = False
                    blk[direction]["motifs"].append("overfit ? (walk-forward)")
                # v1.5 : les sous-cases (état × zone de vol) héritent du badge
                # de la case parente — un parent overfit disqualifie ses zones.
                for zone_blk in blk.get("vol_zones", {}).values():
                    zone_blk[direction]["walkforward"] = badge
                    if badge == "overfit" and zone_blk[direction].get("candidate"):
                        zone_blk[direction]["candidate"] = False
                        zone_blk[direction]["motifs"].append(
                            "overfit ? (walk-forward, case parente)")
    store.set_kv("tables_latest", json.dumps({"generated_at": utc_now_iso(),
                                              "timeframes": tables}))

    # Vue 1 : reporter badge + disqualification sur la matrice servie.
    for tf_entry in matrix:
        if tf_entry.get("insuffisant"):
            continue
        for direction in ("long", "short"):
            key = (tf_entry["timeframe"], tf_entry["etat"], direction)
            badge = badge_by_case.get(key, "n/a")
            tf_entry[direction]["walkforward"] = badge
            if badge == "overfit" and tf_entry[direction].get("candidate"):
                tf_entry[direction]["candidate"] = False
                tf_entry[direction]["motifs"].append("overfit ? (walk-forward)")
        tf_entry["candidate"] = bool(tf_entry["long"]["candidate"]
                                     or tf_entry["short"]["candidate"])
    store.set_kv("matrix_latest", json.dumps({"generated_at": utc_now_iso(),
                                              "timeframes": matrix}))

    # Synthèse bayésienne : combine les probas directionnelles des échelles (§5.6).
    entries = []
    for tf in matrix:
        if tf.get("insuffisant"):
            continue
        fh = tf["long"]["horizon_fixe"]
        entries.append(StackEntry(tf["timeframe"], fh.get("p_hat", float("nan")),
                                  fh.get("n", 0)))
    synth = stack(entries)
    # Référence neutre log-normale à ~1 an (§5.7).
    ref = lognormal_reference(df_1d["close"].to_numpy("float64"), horizon_days=365) \
        if not df_1d.empty else {"p_up": float("nan")}
    store.set_kv("synthese_latest", json.dumps({
        "generated_at": utc_now_iso(), "synthese": synth, "reference_neutre": ref}))

    candidates = sum(1 for tf in matrix if tf.get("candidate"))
    log.info(f"Matrice calculée : {len(matrix)} timeframes, {candidates} candidate(s) ; "
             f"synthèse P(hausse)={synth.get('p_up')}")
    return candidates


def compute_bilan(store: Store) -> None:
    """Suivi de la période P4 et préparation du bilan P5 (§8).

    P4 se termine à « 60 jours OU 100 trades papier » (premier atteint), SANS
    raccourci (§9). Ce bilan est donc étiqueté « période en cours » tant que
    l'échéance n'est pas atteinte ; il expose les métriques du go/no-go
    (t-stat, rétention walk-forward, DD Monte Carlo, Brier) sans conclure —
    la décision finale reste humaine (P5)."""
    # Début de période : premier passage du bilan (persistant).
    debut = store.get_kv("p4_debut_utc")
    if not debut:
        debut = utc_now_iso()
        store.set_kv("p4_debut_utc", debut)
    jours = max(0, (datetime.now(timezone.utc)
                    - datetime.fromisoformat(debut)).days)

    trades = store.conn.execute("SELECT COUNT(*) AS c FROM journal").fetchone()["c"]
    verdicts = json.loads(store.get_kv("verdicts_latest") or "{}")
    wf = json.loads(store.get_kv("walkforward_latest") or "{}")

    par_etage = {}
    for stage in ("1h", "4h", "1D"):
        v = verdicts.get(stage) or {}
        badges = [c["badge"] for c in wf.get("cases", [])
                  if c["timeframe"] == stage]
        par_etage[stage] = {
            "n_trades": v.get("n", 0),
            "t_stat": v.get("t_stat"),
            "ev_realisee": v.get("ev_realisee"),
            "dd_mc_p95": v.get("mc_dd_p95"),
            "brier": v.get("brier"),
            "retention": {b: badges.count(b)
                          for b in ("sain", "fragile", "overfit", "n/a")},
        }

    terminee = jours >= 60 or trades >= 100
    store.set_kv("bilan_latest", json.dumps({
        "generated_at": utc_now_iso(),
        "periode": {"debut_utc": debut, "jours": jours, "jours_cible": 60,
                    "trades": trades, "trades_cible": 100,
                    "terminee": terminee},
        "par_etage": par_etage,
        "global": verdicts.get("global") or {},
        "note": ("Période P4 TERMINÉE — bilan P5 à instruire (décision humaine)."
                 if terminee else
                 "Période P4 en cours — métriques indicatives, aucune conclusion."),
    }))
    log.info(f"Bilan P4 : jour {jours}/60, {trades}/100 trades"
             + (" — PÉRIODE TERMINÉE" if terminee else ""))
    if terminee and not store.get_kv("p4_terminee_annoncee"):
        store.set_kv("p4_terminee_annoncee", "1")
        store.add_event("worker", "info",
                        "Période P4 atteinte (60 j ou 100 trades) — le bilan "
                        "P5 peut être instruit", {"jours": jours, "trades": trades})


def check_clock_skew(ds: DataSource, store: Store) -> float | None:
    """Dérive d'horloge vs horodatage serveur exchange (§7.9)."""
    server_ms = ds.server_time_ms()
    if server_ms is None:
        return None
    local_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    skew_s = (local_ms - server_ms) / 1000.0
    if abs(skew_s) > 5:
        store.add_event("data_incident", "warning",
                        f"Dérive d'horloge {skew_s:+.1f}s vs exchange (> 5s)",
                        {"skew_s": skew_s})
    return skew_s


def backup_db(store_path: Path) -> Path | None:
    """Instantané cohérent de moteur.db (API backup SQLite) + rétention 14 j (§7.7)."""
    if not store_path.exists():
        log.warning("moteur.db absent : pas de sauvegarde")
        return None
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest_dir = CONFIG.backup_dir / day
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "moteur.db"
    src = sqlite3.connect(store_path)
    try:
        # Garde d'intégrité : ne JAMAIS écraser une sauvegarde saine avec un
        # instantané d'une base corrompue (§7.7).
        try:
            ok = src.execute("PRAGMA quick_check;").fetchone()[0] == "ok"
        except sqlite3.DatabaseError:
            ok = False
        if not ok:
            log.error("Base corrompue détectée : sauvegarde du jour NON écrasée "
                      "— restaurer depuis backups/ (procédure README §5)")
            return None
        with sqlite3.connect(dest) as bck:
            src.backup(bck)  # instantané cohérent, y compris WAL
    finally:
        src.close()
    log.info(f"Sauvegarde : {dest}")

    # Rétention 14 jours.
    cutoff = time.time() - 14 * 86400
    for d in CONFIG.backup_dir.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            log.info(f"Sauvegarde purgée : {d.name}")
    return dest


def main() -> None:
    CONFIG.ensure_dirs()
    t0 = time.time()
    cpu0 = resource.getrusage(resource.RUSAGE_SELF).ru_utime + \
        resource.getrusage(resource.RUSAGE_SELF).ru_stime
    store = Store()
    ds = DataSource(store=store, logger=log)

    log.info("=== Cycle quotidien : début ===")
    try:
        from core.data_source import migrate_to_segments
        migrate_to_segments("1m", logger=log)
    except Exception as e:
        log.error(f"Migration segments : {e!r}")
    filled = ds.fill_gaps()
    if filled:
        log.info(f"Trous comblés avant recalcul : {filled}")

    counts = rebuild_timeframes(store)
    df_1m = load_ohlcv("1m")
    gaps = len(find_gaps(df_1m))
    dups = count_duplicates(df_1m)
    skew = check_clock_skew(ds, store)

    # Moteur statistique (§5) : calcul + stockage de la matrice.
    try:
        candidates = compute_and_store_matrix(store)
    except Exception as e:  # une erreur de calcul ne doit pas casser le cycle
        candidates = 0
        log.error(f"Calcul de la matrice échoué : {e!r}")
        store.add_event("worker", "error", f"Calcul matrice échoué : {e}")

    # Suivi de période P4 / préparation bilan P5 (§8).
    try:
        compute_bilan(store)
    except Exception as e:
        log.error(f"Bilan P4/P5 échoué : {e!r}")

    duration = time.time() - t0
    ru = resource.getrusage(resource.RUSAGE_SELF)
    cpu_s = (ru.ru_utime + ru.ru_stime) - cpu0
    # ru_maxrss : Ko sous Linux.
    peak_mem_mb = ru.ru_maxrss / 1024.0

    store.record_health("daily", duration_s=duration, cpu_s=cpu_s,
                        peak_mem_mb=peak_mem_mb, clock_skew_s=skew,
                        note=f"gaps={gaps} dups={dups} bars_1m={counts.get('1m', 0)}")
    log.info(f"Ressources : durée={duration:.1f}s cpu={cpu_s:.1f}s "
             f"pic_mem={peak_mem_mb:.0f}Mo skew={skew}")

    if duration > 15 * 60:  # §7.6
        store.add_event("performance", "warning",
                        f"Cycle quotidien lent : {duration/60:.1f} min (> 15 min)",
                        {"duration_s": duration})

    backup_db(CONFIG.db_path)
    store.add_event("worker", "info",
                    f"Cycle quotidien terminé ({duration:.0f}s)",
                    {"bars_1m": counts.get("1m", 0), "gaps": gaps})
    log.info("=== Cycle quotidien : fin ===")
    store.close()


if __name__ == "__main__":
    main()
