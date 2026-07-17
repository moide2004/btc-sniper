#!/usr/bin/env python3
"""
Tests hors-ligne du CRM (Phase 1). Lançable tel quel :  python tests/test_all.py
Vérifie la logique pure ET le comportement de crm.py via un backend mémoire.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import crm_logic
from crm import CRM
from tests.mock_backend import MockBackend

PASSED = 0


def check(cond, label):
    global PASSED
    assert cond, f"ÉCHEC : {label}"
    PASSED += 1
    print(f"  ✓ {label}")


# --------------------------------------------------------------------------
def test_normalisation_dedup():
    print("\n[normalisation & déduplication]")
    check(crm_logic.normalize_name("Pierrot Srl") == "pierrot", "suffixe juridique retiré")
    check(crm_logic.normalize_name("García de Pou") == "garcia de pou", "accents retirés")
    check(crm_logic.extract_domain("https://www.pawdecor.com/x") == "pawdecor.com",
          "domaine depuis URL (sans www ni protocole)")
    check(crm_logic.extract_domain("", "office@pawdecor.com") == "pawdecor.com",
          "domaine depuis e-mail")
    existing = [{"nom": "PAW (PAW Decor Collection)", "site_web": "pawdecor.com",
                 "email": ""}]
    dup = crm_logic.is_duplicate({"nom": "paw decor collection",
                                  "site_web": "www.pawdecor.com"}, existing)
    check(dup is not None, "doublon détecté (nom+domaine)")
    nodup = crm_logic.is_duplicate({"nom": "Autre Usine", "site_web": "autre.com"},
                                   existing)
    check(nodup is None, "non-doublon correctement accepté")


def test_seed():
    print("\n[seed CSV]")
    headers, rows = crm_logic.read_seed()
    check(headers == config.SEED_COLUMNS, "en-têtes du seed == schéma attendu")
    check(len(rows) == 53, f"53 fournisseurs lus (obtenu {len(rows)})")
    check(rows[0]["id"] == "IT-01", "1re ligne = IT-01")
    check(rows[-1]["id"] == "NA-53", "dernière ligne = NA-53")
    # donnée non modifiée : téléphone avec zéro/plus conservé tel quel
    pierrot = next(r for r in rows if r["id"] == "IT-02")
    check(pierrot["telephone"] == "+39 041 635454", "téléphone conservé tel quel")
    check(rows[-1]["statut"] == "Réserve ouate", "statut hérité présent (NA-53)")


def test_empty_detection():
    print("\n[détection onglet vide — piège gspread 6]")
    check(crm_logic.is_really_empty([]) is True, "[] -> vide")
    check(crm_logic.is_really_empty([[]]) is True, "[[]] -> vide (gspread 6)")
    check(crm_logic.is_really_empty([[""]]) is True, "[['']] -> vide")
    check(crm_logic.is_really_empty([["horodatage"]]) is False, "en-tête -> non vide")


def test_formules_fr():
    print("\n[formules Dashboard — locale fr_FR]")
    formulas = crm_logic.all_dashboard_formulas()
    for f in formulas:
        # séparateur d'arguments = ';' ; jamais ',' comme séparateur.
        # (on tolère une virgule éventuelle DANS un libellé entre guillemets,
        #  mais nos statuts/pays n'en contiennent pas)
        check("," not in f, f"pas de virgule séparatrice dans : {f[:45]}…")
        check("FALSE" not in f and "TRUE" not in f, "pas de FALSE/TRUE littéral")
    check(any(";" in f for f in formulas), "séparateur ';' bien utilisé")
    check(crm_logic.formula_count_status("Qualifié") ==
          '=COUNTIF(Fournisseurs!J:J;"Qualifié")', "COUNTIF statut correct")
    check("Fournisseurs!L" in crm_logic.formula_top_score_names(),
          "top score cible bien la colonne score (L)")


def test_colonnes():
    print("\n[schéma colonnes]")
    check(config.col_letter("statut") == "J", "statut -> colonne J")
    check(config.col_letter("score") == "L", "score -> colonne L")
    check(config.col_letter("date_relance") == "T", "date_relance -> colonne T")
    check(config.col_letter("date_ajout") == "W", "date_ajout -> colonne W")
    for s in config.PROPOSED_STATUSES + config.LEGACY_STATUSES:
        check(s in config.STATUS_HEX, f"couleur définie pour statut « {s} »")


def test_crm_scenario_1_7():
    print("\n[scénario du test obligatoire 1.7 via backend mémoire]")
    backend = MockBackend({
        config.TAB_FOURNISSEURS: config.COLUMNS,
        config.TAB_LOG: config.LOG_HEADERS,
    })
    # pré-charge quelques fournisseurs du seed pour un contexte réaliste
    _, seed = crm_logic.read_seed()
    for r in seed[:5]:
        backend.append_row(config.TAB_FOURNISSEURS,
                           [r.get(k, "") for k in config.SEED_COLUMNS] + [""])
    crm = CRM(backend, author="test")

    r1 = crm.add_supplier({"nom": "TEST SUPPLIER", "pays": "France",
                           "site_web": "test-supplier.example"}, source="test")
    check(r1["status"] == "added", "1) ajout TEST SUPPLIER")
    check(r1["record"]["statut"] == "À contacter", "   statut par défaut = À contacter")
    check(r1["record"]["date_ajout"] != "", "   date_ajout renseignée")

    r2 = crm.add_supplier({"nom": "test supplier", "pays": "France",
                           "site_web": "www.test-supplier.example"}, source="test")
    check(r2["status"] == "duplicate", "2) re-ajout -> doublon (0 ajout)")

    r3 = crm.mark_contacted("TEST SUPPLIER", "12/03")
    check(r3["status"] == "ok", "3) marqué contacté")
    fiche = crm.find("TEST SUPPLIER")
    check(fiche["statut"] == "Contacté", "   statut -> Contacté")
    check(fiche["date_contact"] == "2026-03-12", "   date_contact = 2026-03-12")
    check(fiche["date_relance"] == "2026-03-19", "   date_relance = J+7")

    log_vals = backend.ws(config.TAB_LOG).get_all_values()
    check(len(log_vals) >= 4, "4) Log renseigné (ajout+doublon+contacté)")

    check(crm.delete_supplier("TEST SUPPLIER") is True, "5) suppression OK")
    check(crm.find("TEST SUPPLIER") is None, "   fournisseur bien supprimé")


def test_crm_commandes():
    print("\n[commandes CRM : devis reçu, qui relancer, stats]")
    backend = MockBackend({
        config.TAB_FOURNISSEURS: config.COLUMNS,
        config.TAB_LOG: config.LOG_HEADERS,
    })
    crm = CRM(backend, author="test")
    crm.add_supplier({"nom": "Usine A", "pays": "Chine", "site_web": "a.cn"})
    crm.add_supplier({"nom": "Usine B", "pays": "Italie", "site_web": "b.it"})

    # devis reçu d'un fournisseur connu : statut + note ⚠, sans écraser
    crm.devis_recu("Usine A", "0,041 €/pce EXW, MOQ 300000")
    a = crm.find("Usine A")
    check(a["statut"] == "Devis reçu", "devis reçu -> statut Devis reçu")
    check("à valider" in a["notes"], "note « ⚠ à valider » posée")

    # devis d'un inconnu -> nouvelle ligne
    res = crm.devis_recu("Usine Inconnue", "prix X")
    check(res["status"] == "added_with_devis", "devis d'un inconnu -> nouvelle ligne")

    # qui relancer : marque A contacté hier -> relance dépassée bientôt
    crm.mark_contacted("Usine B", "01/01")  # relance 2026-01-08 <= aujourd'hui+3
    relance = crm.who_to_relance()
    check(any(x["nom"] == "Usine B" for x in relance), "Usine B dans « à relancer »")

    st = crm.stats()
    check(st["devis_recus"] >= 1, "stats : devis_recus comptés")
    check(st["total"] >= 3, "stats : total cohérent")
    check("Chine" in st["par_pays"], "stats : ventilation par pays")


def test_protection_champ_manuel():
    print("\n[ne jamais écraser un champ saisi à la main]")
    backend = MockBackend({
        config.TAB_FOURNISSEURS: config.COLUMNS,
        config.TAB_LOG: config.LOG_HEADERS,
    })
    crm = CRM(backend, author="test")
    crm.add_supplier({"nom": "Usine C", "site_web": "c.cn",
                      "notes": "note manuelle importante"})
    crm.devis_recu("Usine C", "infos devis")
    c = crm.find("Usine C")
    check("note manuelle importante" in c["notes"], "note manuelle conservée")
    check("infos devis" in c["notes"], "info devis ajoutée à la suite")


def main():
    tests = [test_normalisation_dedup, test_seed, test_empty_detection,
             test_formules_fr, test_colonnes, test_crm_scenario_1_7,
             test_crm_commandes, test_protection_champ_manuel]
    for t in tests:
        t()
    print(f"\n{'=' * 50}\n✅ {PASSED} assertions OK — tous les tests passent.")


if __name__ == "__main__":
    main()
