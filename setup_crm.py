#!/usr/bin/env python3
"""
setup_crm.py — PHASE 1 : construit le CRM Fournisseurs Airlaid (Google Sheets).

Usage :
    python setup_crm.py            # installe / met à jour le CRM + test 1.7
    python setup_crm.py --force    # réinstallation : purge les règles de couleur
    python setup_crm.py --no-test  # saute le test obligatoire 1.7

Pré-requis (.env) : GOOGLE_SERVICE_ACCOUNT_JSON, GSHEET_URL.
Aucun envoi d'e-mail, aucun SMTP : ce script ne touche qu'à Google Sheets.
"""
from __future__ import annotations

import sys

import config
import crm_logic
from crm import CRM
from sheets_backend import GoogleSheetsBackend


def log(msg: str):
    print(msg, flush=True)


# --------------------------------------------------------------------------
# Construction de l'onglet Fournisseurs
# --------------------------------------------------------------------------
def build_fournisseurs(backend: GoogleSheetsBackend, force: bool):
    log("• Onglet « Fournisseurs »…")
    ncols = len(config.COLUMNS)
    backend.ensure_worksheet(config.TAB_FOURNISSEURS,
                             rows=config.PROVISIONED_ROWS, cols=ncols + 2)

    # 1) format TEXTE des colonnes sensibles AVANT toute écriture (zéros, ids).
    for key in config.TEXT_COLUMNS:
        backend.set_column_text_format(config.TAB_FOURNISSEURS, key)

    # 2) import du seed — en-têtes du CSV = colonnes, données NON modifiées.
    seed_headers, seed_rows = crm_logic.read_seed()
    if seed_headers != config.SEED_COLUMNS:
        raise SystemExit(f"❌ En-têtes du seed inattendus : {seed_headers}")
    block = [list(config.COLUMNS)]
    for r in seed_rows:
        block.append([r.get(k, "") for k in config.SEED_COLUMNS] +
                     ["" for _ in config.SERVICE_COLUMNS])
    backend.write_rows(config.TAB_FOURNISSEURS, "A1", block)
    log(f"  ↳ {len(seed_rows)} fournisseurs importés (données seed intactes).")

    # 3) mise en forme en-tête + gel de la 1re ligne.
    backend.format_header(config.TAB_FOURNISSEURS, ncols)

    # 4) validation des statuts (liste déroulante, lignes futures incluses).
    backend.set_status_validation(config.TAB_FOURNISSEURS, "statut")

    # 5) couleurs par statut — purge d'abord si réinstallation (pas d'empilement).
    purged = backend.purge_conditional_formats(config.TAB_FOURNISSEURS)
    if purged:
        log(f"  ↳ {purged} règle(s) de couleur existante(s) purgée(s) (--force).")
    backend.add_status_color_rules(config.TAB_FOURNISSEURS, "statut")

    # 6) filtre automatique couvrant les lignes futures + ajustement largeurs.
    backend.set_basic_filter(config.TAB_FOURNISSEURS, ncols)
    backend.autoresize_columns(config.TAB_FOURNISSEURS, ncols)
    log("  ↳ validation, couleurs, filtre auto et formats appliqués.")


# --------------------------------------------------------------------------
# Onglet Log — n'écrire les en-têtes QUE si réellement vide (piège gspread 6)
# --------------------------------------------------------------------------
def build_log(backend: GoogleSheetsBackend):
    log("• Onglet « Log »…")
    backend.ensure_worksheet(config.TAB_LOG, rows=config.PROVISIONED_ROWS,
                             cols=len(config.LOG_HEADERS) + 1)
    if backend.worksheet_is_empty(config.TAB_LOG):
        backend.write_rows(config.TAB_LOG, "A1", [config.LOG_HEADERS])
        backend.format_header(config.TAB_LOG, len(config.LOG_HEADERS))
        log("  ↳ en-têtes du Log écrits (onglet était réellement vide).")
    else:
        log("  ↳ Log déjà initialisé, en-têtes conservés (pas d'écrasement).")


# --------------------------------------------------------------------------
# Onglet Dashboard
# --------------------------------------------------------------------------
def build_dashboard(backend: GoogleSheetsBackend, seed_rows: list[dict]):
    log("• Onglet « Dashboard »…")
    backend.ensure_worksheet(config.TAB_DASHBOARD, rows=200, cols=12)
    ws = backend.ws(config.TAB_DASHBOARD)
    ws.clear()

    cells: list[tuple[str, str]] = []  # (A1, valeur/formule)

    cells.append(("A1", "DASHBOARD — CRM Fournisseurs Airlaid"))
    cells.append(("A2", "(compteurs en direct : se recalculent à chaque édition)"))

    # -- Bloc 1 : compteurs par statut (A4:B…) --
    cells.append(("A4", "Compteurs par statut"))
    row = 5
    for s in config.ALL_STATUSES:
        cells.append((f"A{row}", s))
        cells.append((f"B{row}", crm_logic.formula_count_status(s)))
        row += 1
    total_row = row + 1
    cells.append((f"A{total_row}", "TOTAL fournisseurs"))
    cells.append((f"B{total_row}", crm_logic.formula_total()))
    cells.append((f"A{total_row + 1}", "Devis reçus"))
    cells.append((f"B{total_row + 1}", crm_logic.formula_count_devis()))

    # -- Bloc 2 : compteurs par pays (D4:E…) --
    cells.append(("D4", "Compteurs par pays"))
    countries: list[str] = []
    for r in seed_rows:
        p = (r.get("pays") or "").strip()
        if p and p not in countries:
            countries.append(p)
    row = 5
    for c in countries:
        cells.append((f"D{row}", c))
        cells.append((f"E{row}", crm_logic.formula_count_country(c)))
        row += 1

    # -- Bloc 3 : à relancer (G4, spill vertical) --
    cells.append(("G4", "À relancer (date_relance ≤ aujourd'hui + 3 j)"))
    cells.append(("G5", crm_logic.formula_a_relancer()))

    # -- Bloc 4 : top 10 par score (I4:J…, spill) --
    cells.append(("I4", "Top 10 par score"))
    cells.append(("I5", crm_logic.formula_top_score_names()))
    cells.append(("J5", crm_logic.formula_top_score_values()))

    # écriture cellule par cellule (USER_ENTERED pour interpréter les formules)
    for a1, val in cells:
        ws.update(a1, [[val]], value_input_option="USER_ENTERED")

    backend.format_header(config.TAB_DASHBOARD, 1)  # gèle la ligne titre
    log(f"  ↳ compteurs statut ({len(config.ALL_STATUSES)}), pays "
        f"({len(countries)}), à relancer, top 10 par score, nb devis.")


# --------------------------------------------------------------------------
# Test obligatoire 1.7
# --------------------------------------------------------------------------
def run_mandatory_test(crm: CRM):
    log("\n" + "=" * 60)
    log("TEST OBLIGATOIRE 1.7 — preuve de fonctionnement")
    log("=" * 60)
    name = "TEST SUPPLIER"
    data = {"nom": name, "pays": "France", "site_web": "test-supplier.example",
            "produit_match": "fiche de test"}

    r1 = crm.add_supplier(data, source="test")
    log(f"1) Ajout « {name} » : {r1['status']}")

    r2 = crm.add_supplier(data, source="test")
    ok_dedup = r2["status"] == "duplicate"
    log(f"2) Re-tentative d'ajout : {r2['status']} "
        f"-> déduplication {'OK (0 doublon)' if ok_dedup else 'ÉCHEC'}")

    r3 = crm.mark_contacted(name)
    log(f"3) Marqué contacté : statut=Contacté, "
        f"date_contact={r3.get('date_contact')}, "
        f"date_relance={r3.get('date_relance')}")

    log("4) 5 dernières lignes du Log :")
    log_rows = crm.b.ws(config.TAB_LOG).get_all_values()[-5:]
    for lr in log_rows:
        log("     " + " | ".join(lr))

    deleted = crm.delete_supplier(name)
    log(f"5) Suppression « {name} » : {'OK' if deleted else 'ÉCHEC'}")

    if not ok_dedup:
        raise SystemExit("❌ Test 1.7 échoué : la déduplication n'a pas fonctionné.")
    log("✅ Test 1.7 réussi.")


# --------------------------------------------------------------------------
def main():
    force = "--force" in sys.argv
    do_test = "--no-test" not in sys.argv

    config.load_env()
    key_path = config.env("GOOGLE_SERVICE_ACCOUNT_JSON", required=True)
    sheet_url = config.env("GSHEET_URL", required=True)

    log("Connexion à Google Sheets…")
    backend = GoogleSheetsBackend(key_path, sheet_url).connect()
    log(f"✓ Connecté au classeur : « {backend.ss.title} »")

    _, seed_rows = crm_logic.read_seed()
    build_fournisseurs(backend, force)
    build_log(backend)
    build_dashboard(backend, seed_rows)

    if do_test:
        run_mandatory_test(CRM(backend, author="setup"))

    log("\n✅ PHASE 1 terminée. Onglets Fournisseurs / Dashboard / Log prêts.")
    log(f"   Classeur : {sheet_url}")


if __name__ == "__main__":
    main()
