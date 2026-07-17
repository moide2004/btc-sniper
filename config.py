"""
config.py — Constantes métier et chargement de l'environnement.
CRM Fournisseurs Airlaid — ArcaneLogistics.

Aucune donnée sensible ici : les secrets vivent dans .env (git-ignoré).
Ce module ne fait AUCUN appel réseau et ne dépend d'aucun secret pour
être importé : il sert aussi de source de vérité aux tests de logique pure.
"""
from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------
# Chemins
# --------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
SEED_CSV = ROOT / "fournisseurs_seed.csv"
SEED_DELIMITER = ";"  # le seed utilise le point-virgule comme séparateur

# --------------------------------------------------------------------------
# Environnement (.env)
# --------------------------------------------------------------------------
def load_env() -> None:
    """Charge .env si présent. Sans planter si python-dotenv absent."""
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:
        pass


def env(name: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.environ.get(name, default)
    if required and not val:
        raise SystemExit(
            f"❌ Variable d'environnement manquante : {name}. "
            f"Renseignez-la dans .env (voir .env.example)."
        )
    return val


# --------------------------------------------------------------------------
# Schéma de la feuille « Fournisseurs »
# En-têtes du CSV, dans l'ordre, + colonne de service `date_ajout`.
# (Les colonnes spécifiques aux devis — devise, coût cliché, colisage, CBM…
#  seront ajoutées en Phase 2, sans casser ce schéma : écritures par en-tête.)
# --------------------------------------------------------------------------
SEED_COLUMNS = [
    "id", "nom", "pays", "region", "ville", "site_web", "email", "telephone",
    "contact_nom", "statut", "tier", "score", "produit_match", "certifications",
    "moq", "prix_exw_eur", "incoterm", "delai", "date_contact", "date_relance",
    "source", "notes",
]
# Colonnes de service ajoutées par le CRM (au-delà du seed)
SERVICE_COLUMNS = ["date_ajout"]
COLUMNS = SEED_COLUMNS + SERVICE_COLUMNS

# Colonnes à forcer au format TEXTE (un 06… ou +39… ne doit pas perdre son zéro,
# et un id « IT-01 » ne doit pas être réinterprété). NB : `score` reste NUMÉRIQUE
# (le Dashboard le trie), `tier` est textuel car il contient V/R en plus des chiffres.
TEXT_COLUMNS = ["telephone", "id", "tier"]
# Colonnes de type DATE (écrites en ISO YYYY-MM-DD, interprétées comme dates).
DATE_COLUMNS = ["date_contact", "date_relance", "date_ajout"]


def col_index(key: str) -> int:
    """Index 0-based de la colonne `key` dans la feuille Fournisseurs."""
    return COLUMNS.index(key)


def col_letter(key: str) -> str:
    """Lettre de colonne A1 (ex. 'statut' -> 'J')."""
    n = col_index(key)
    letters = ""
    n += 1
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


# --------------------------------------------------------------------------
# Statuts (Phase 1.4)
# --------------------------------------------------------------------------
# Statuts PROPOSÉS pour toute nouvelle fiche (liste déroulante).
PROPOSED_STATUSES = [
    "À sourcer", "À contacter", "Contacté", "Relancé", "Devis reçu",
    "Échantillons", "Qualifié", "Écarté", "À vérifier", "Substrat",
    "Réserve", "Benchmark",
]
# Statut hérité (ligne NA-53) : ACCEPTÉ (donc dans la validation, pas d'alerte)
# mais JAMAIS proposé par le workflow pour une nouvelle fiche.
LEGACY_STATUSES = ["Réserve ouate"]
# Liste complète acceptée par la validation de données.
ALL_STATUSES = PROPOSED_STATUSES + LEGACY_STATUSES

DEFAULT_STATUS = "À contacter"  # statut d'un fournisseur nouvellement ajouté

# Couleurs par statut — fond clair, texte foncé lisible (Phase 1.4).
# vert = Qualifié, rouge = Écarté, bleu = Devis reçu, beige = Réserve/Réserve ouate.
STATUS_HEX = {
    "À sourcer":     "E0E0E0",  # gris
    "À contacter":   "FFF2CC",  # jaune pâle
    "Contacté":      "D9E1F2",  # bleu pâle
    "Relancé":       "FCE4D6",  # orange pâle
    "Devis reçu":    "9CC3E6",  # bleu
    "Échantillons":  "E4DFEC",  # lavande
    "Qualifié":      "A9D08E",  # vert
    "Écarté":        "F4A6A0",  # rouge
    "À vérifier":    "FFE599",  # ambre
    "Substrat":      "C6D9DC",  # cyan grisé
    "Réserve":       "EDE0CE",  # beige
    "Benchmark":     "B7D7C9",  # vert-bleu
    "Réserve ouate": "EDE0CE",  # beige (même famille que Réserve)
}


def hex_to_rgb01(hex_str: str) -> dict:
    """'A9D08E' -> {'red':.., 'green':.., 'blue':..} en 0..1 (format Sheets API)."""
    hex_str = hex_str.lstrip("#")
    r, g, b = (int(hex_str[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return {"red": round(r, 4), "green": round(g, 4), "blue": round(b, 4)}


# --------------------------------------------------------------------------
# Onglets
# --------------------------------------------------------------------------
TAB_FOURNISSEURS = "Fournisseurs"
TAB_DASHBOARD = "Dashboard"
TAB_LOG = "Log"

LOG_HEADERS = ["horodatage", "action", "cible", "detail", "auteur"]

# Nombre de lignes provisionnées (mises en forme/validation/filtre couvrent
# les lignes FUTURES, pas seulement les 53 du seed) — Phase 1.3.
PROVISIONED_ROWS = 2000

# --------------------------------------------------------------------------
# Fiche produit — SRV-AIRLAID-KGR-001 (CONTEXTE MÉTIER du prompt maître).
# Source unique de vérité pour répondre aux questions fournisseurs (Phase 3).
# NE JAMAIS inventer au-delà de ces valeurs.
# --------------------------------------------------------------------------
PRODUCT = {
    "ref": "SRV-AIRLAID-KGR-001",
    "designation": "Serviette airlaid pocket porte-couverts (« kangourou »)",
    "grammage": "55 g/m² ±5 %",
    "format_ouvert": "40×40 cm ±5 mm",
    "format_ferme": "≈ 20×10 cm",
    "couleur": "sable / ivoire",
    "gaufrage": "toile de lin",
    "contact_alimentaire": "CE 1935/2004",
    "qualite_ref": "≥ García de Pou",
    "commande_1": "1 000 000 pcs = 800 000 imprimées logo 1 couleur + 200 000 neutres",
    "golden_sample": "échantillon teinte + BAT imprimé exigés avant production",
    "livraison": "Arles (13200), France",
}

# Mots-clés multilingues de détection des devis (Phase 2.2).
DEVIS_KEYWORDS = [
    "devis", "quotation", "quote", "offer", "offre", "rfq", "price", "pricing",
    "oferta", "preventivo", "wycena", "报价", "报价单",
]
