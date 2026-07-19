"""Livre multi-étages et budget de risque agrégé (§5.9).

Étages de DÉCISION : 1h, 4h, 1D (12h mesuré non décisionnel ; 1m→30m ne
décident jamais). Règles :
  * 2e/3e position de MÊME direction → leur risque compte 1,5× au budget ;
  * plafond de risque ouvert TOTAL : 3 % (refus « bloqué par budget ») ;
  * corrélation P&L inter-étages mensuelle affichée, multiplicateur 2× sur le
    risque pondéré si ρ > 0,7 ;
  * INTERDICTION des positions opposées simultanées
    (refus « bloqué : direction opposée ouverte »).
"""
from __future__ import annotations

from itertools import combinations

TOTAL_RISK_CAP = 0.03      # 3 % (§5.9)
SAME_DIR_WEIGHT = 1.5      # 2e/3e position même direction (§5.9)
CORR_THRESHOLD = 0.7       # ρ > 0,7 → ×2 (§5.9)
CORR_MULTIPLIER = 2.0


def monthly_stage_correlations(journal_rows: list[dict]) -> dict:
    """Corrélation des P&L mensuels entre étages (affichée, §5.9).
    `journal_rows` : [{ts_utc, stage, r_result}, ...]. ρ calculée par paire
    d'étages sur les mois communs (≥ 3 mois requis, sinon None)."""
    by = {}
    for row in journal_rows:
        month = str(row.get("ts_utc", ""))[:7]  # AAAA-MM
        stage = row.get("stage")
        if not month or stage is None:
            continue
        by.setdefault(stage, {}).setdefault(month, 0.0)
        by[stage][month] += float(row.get("r_result", 0.0))

    out = {}
    for a, b in combinations(sorted(by.keys()), 2):
        common = sorted(set(by[a]) & set(by[b]))
        if len(common) < 3:
            out[f"{a}~{b}"] = None
            continue
        xa = [by[a][m] for m in common]
        xb = [by[b][m] for m in common]
        ma = sum(xa) / len(xa)
        mb = sum(xb) / len(xb)
        cov = sum((p - ma) * (q - mb) for p, q in zip(xa, xb))
        va = sum((p - ma) ** 2 for p in xa)
        vb = sum((q - mb) ** 2 for q in xb)
        out[f"{a}~{b}"] = (cov / (va * vb) ** 0.5) if va > 0 and vb > 0 else None
    return out


def weighted_open_risk(open_positions: list[dict], correlations: dict) -> float:
    """Risque ouvert pondéré (§5.9) : les positions au-delà de la première dans
    une même direction pèsent 1,5× ; le tout ×2 si une corrélation inter-étages
    dépasse 0,7."""
    total = 0.0
    seen_dir_count: dict[str, int] = {}
    # ordre d'ouverture : la 1re position d'une direction pèse 1×, les suivantes 1,5×
    for pos in sorted(open_positions, key=lambda p: p.get("opened_utc", "")):
        d = pos["direction"]
        seen_dir_count[d] = seen_dir_count.get(d, 0) + 1
        weight = 1.0 if seen_dir_count[d] == 1 else SAME_DIR_WEIGHT
        total += float(pos["risque_pct"]) * weight
    high_corr = any(v is not None and v > CORR_THRESHOLD for v in correlations.values())
    if high_corr:
        total *= CORR_MULTIPLIER
    return total


def check_new_position(
    ticket: dict, open_positions: list[dict], correlations: dict,
) -> tuple[bool, str]:
    """Vérifie si un ticket peut ouvrir une position. Retourne (autorisé, motif).
    Motifs exacts du cahier des charges (§5.9)."""
    direction = ticket["direction"]
    opposite = "short" if direction == "long" else "long"
    if any(p["direction"] == opposite for p in open_positions):
        return False, "bloqué : direction opposée ouverte"

    hypothetical = open_positions + [{
        "direction": direction,
        "risque_pct": ticket["taille"]["risque_pct"],
        "opened_utc": "9999-12-31T23:59:59",  # la nouvelle arrive en dernier
    }]
    risk = weighted_open_risk(hypothetical, correlations)
    if risk > TOTAL_RISK_CAP + 1e-12:
        return False, "bloqué par budget"
    return True, "ok"
