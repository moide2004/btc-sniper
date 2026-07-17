"""
crm_logic.py — Logique PURE, sans réseau ni dépendance à un secret.

Regroupe tout ce qui est testable hors-ligne :
  - normalisation de noms / extraction de domaine / clé de déduplication ;
  - parsing du seed CSV ;
  - génération des formules Dashboard en locale fr_FR (séparateur ';', 0/1) ;
  - détection d'onglet réellement vide (piège gspread 6 -> [[]]).
"""
from __future__ import annotations

import csv
import re
import unicodedata
from pathlib import Path

import config

# --------------------------------------------------------------------------
# Normalisation & déduplication (clé = nom normalisé + domaine) — Phase 1.6
# --------------------------------------------------------------------------
_LEGAL_SUFFIXES = {
    "srl", "srls", "spa", "sp", "spzoo", "sp z o o", "zoo", "gmbh", "sa",
    "sas", "sarl", "ltd", "llc", "inc", "co", "srl.", "group", "groupe",
    "holding", "holdings", "company", "technology", "technologies", "paper",
    "nonwoven", "nonwovens", "industrial", "industries", "sl",
}


def strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def normalize_name(name: str) -> str:
    """Minuscule, sans accents, sans ponctuation, suffixes juridiques retirés."""
    if not name:
        return ""
    s = strip_accents(name).lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    tokens = [t for t in s.split() if t and t not in _LEGAL_SUFFIXES]
    return " ".join(tokens).strip()


def extract_domain(site_web: str = "", email: str = "") -> str:
    """Domaine nu (sans www) depuis le site web, sinon depuis l'e-mail."""
    src = (site_web or "").strip()
    if src:
        src = re.sub(r"^https?://", "", src, flags=re.I)
        src = src.split("/")[0]
        src = re.sub(r"^www\.", "", src, flags=re.I)
        if src:
            return src.lower()
    email = (email or "").strip()
    if "@" in email:
        return email.split("@")[-1].strip().lower()
    return ""


def dedup_key(name: str, site_web: str = "", email: str = "") -> str:
    """Clé de déduplication : « nom normalisé | domaine »."""
    return f"{normalize_name(name)}|{extract_domain(site_web, email)}"


def _name_match(a: str, b: str) -> bool:
    """Noms « identiques » : égalité normalisée OU inclusion d'ensembles de mots
    (gère « PAW (PAW Decor Collection) » vs « PAW Decor Collection »)."""
    if not a or not b:
        return False
    if a == b:
        return True
    sa, sb = set(a.split()), set(b.split())
    return bool(sa) and bool(sb) and (sa <= sb or sb <= sa)


def is_duplicate(candidate: dict, existing: list[dict]) -> dict | None:
    """
    Renvoie la fiche existante en doublon, sinon None. Clé = nom normalisé + domaine.
    Doublon si :
      - même domaine non vide (signal fort : même entreprise), OU
      - noms « identiques » (égalité ou inclusion de mots) ET domaines compatibles.
    """
    cname = normalize_name(candidate.get("nom", ""))
    cdom = extract_domain(candidate.get("site_web", ""), candidate.get("email", ""))
    if not cname and not cdom:
        return None
    for row in existing:
        rdom = extract_domain(row.get("site_web", ""), row.get("email", ""))
        if cdom and rdom and cdom == rdom:
            return row  # même domaine -> même entreprise
        rname = normalize_name(row.get("nom", ""))
        if _name_match(cname, rname) and (not cdom or not rdom or cdom == rdom):
            return row
    return None


# --------------------------------------------------------------------------
# Seed CSV
# --------------------------------------------------------------------------
def read_seed(path: Path | None = None) -> tuple[list[str], list[dict]]:
    """Lit le seed. Renvoie (en-têtes, lignes en dict). Données NON modifiées."""
    path = path or config.SEED_CSV
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f, delimiter=config.SEED_DELIMITER)
        rows = [r for r in reader]
    headers = rows[0]
    data = [dict(zip(headers, r + [""] * (len(headers) - len(r)))) for r in rows[1:]]
    return headers, data


# --------------------------------------------------------------------------
# Détection d'onglet réellement vide (piège gspread 6 : [[]] pour vide) — 1.3
# --------------------------------------------------------------------------
def is_really_empty(values) -> bool:
    """True si la feuille ne contient aucune cellule non vide.
    gspread 6 renvoie [[]] (voire [['']]) pour une feuille vierge : on teste
    le CONTENU réel, pas seulement « la liste est non vide »."""
    if not values:
        return True
    for row in values:
        for cell in row:
            if str(cell).strip() != "":
                return False
    return True


# --------------------------------------------------------------------------
# Formules Dashboard — locale fr_FR : séparateur ';' et 0/1 (jamais FALSE/TRUE)
# Noms de fonctions en anglais (acceptés par l'API Sheets, affichés localisés).
# --------------------------------------------------------------------------
SEP = ";"  # séparateur d'arguments imposé par la locale fr_FR (Phase 1.3)


def _f(sheet: str, key: str) -> str:
    """Référence colonne entière, ex. Fournisseurs!J:J."""
    c = config.col_letter(key)
    return f"{sheet}!{c}:{c}"


def _fr(sheet: str, key: str, r1: int, r2: int) -> str:
    """Référence plage bornée, ex. Fournisseurs!T2:T2000."""
    c = config.col_letter(key)
    return f"{sheet}!{c}{r1}:{c}{r2}"


def formula_count_status(status: str) -> str:
    rng = _f(config.TAB_FOURNISSEURS, "statut")
    return f'=COUNTIF({rng}{SEP}"{status}")'


def formula_count_country(country: str) -> str:
    rng = _f(config.TAB_FOURNISSEURS, "pays")
    return f'=COUNTIF({rng}{SEP}"{country}")'


def formula_count_devis() -> str:
    rng = _f(config.TAB_FOURNISSEURS, "statut")
    return f'=COUNTIF({rng}{SEP}"Devis reçu")'


def formula_total() -> str:
    rng = _f(config.TAB_FOURNISSEURS, "nom")
    return f"=COUNTA({rng})-1"  # -1 pour l'en-tête


def formula_a_relancer(r1: int = 2, r2: int | None = None) -> str:
    """Noms des fournisseurs dont date_relance <= aujourd'hui + 3 jours.
    Produit AND via multiplication (1/0), conforme à la locale fr_FR."""
    r2 = r2 or config.PROVISIONED_ROWS
    nom = _fr(config.TAB_FOURNISSEURS, "nom", r1, r2)
    dr = _fr(config.TAB_FOURNISSEURS, "date_relance", r1, r2)
    # AND exprimé par multiplication (1/0) — pas de séparateur d'arguments ici.
    cond = f'({dr}<>"")*({dr}<=TODAY()+3)'
    return f'=IFERROR(FILTER({nom}{SEP}{cond}){SEP}"— aucun —")'


def formula_top_score_names(r1: int = 2, r2: int | None = None, limit: int = 10) -> str:
    r2 = r2 or config.PROVISIONED_ROWS
    nom = _fr(config.TAB_FOURNISSEURS, "nom", r1, r2)
    score = _fr(config.TAB_FOURNISSEURS, "score", r1, r2)
    flt_nom = f'FILTER({nom}{SEP}{score}<>"")'
    flt_score = f'FILTER({score}{SEP}{score}<>"")'
    sorted_ = f"SORT({flt_nom}{SEP}{flt_score}{SEP}0)"  # 0 = décroissant
    return f"=IFERROR(ARRAY_CONSTRAIN({sorted_}{SEP}{limit}{SEP}1){SEP}\"—\")"


def formula_top_score_values(r1: int = 2, r2: int | None = None, limit: int = 10) -> str:
    r2 = r2 or config.PROVISIONED_ROWS
    score = _fr(config.TAB_FOURNISSEURS, "score", r1, r2)
    flt_score = f'FILTER({score}{SEP}{score}<>"")'
    sorted_ = f"SORT({flt_score}{SEP}{flt_score}{SEP}0)"
    return f"=IFERROR(ARRAY_CONSTRAIN({sorted_}{SEP}{limit}{SEP}1){SEP}\"—\")"


def all_dashboard_formulas() -> list[str]:
    """Toutes les formules générées — utilisé par les tests (aucune virgule
    comme séparateur d'arguments, aucun FALSE/TRUE)."""
    out = [formula_count_devis(), formula_total(),
           formula_a_relancer(), formula_top_score_names(),
           formula_top_score_values()]
    out += [formula_count_status(s) for s in config.ALL_STATUSES]
    out += [formula_count_country(c) for c in ("Italie", "Chine", "Pologne")]
    return out
