"""
crm.py — Opérations métier du CRM au-dessus d'un backend (Google Sheets).

Commandes reconnues (Phase 1.6) :
  - ajoute fournisseur <nom>
  - marque <nom> contacté [le JJ/MM]   (statut + date_contact + date_relance J+7)
  - devis reçu de <nom> : <infos>
  - qui relancer ?
  - stats

Règles permanentes :
  - déduplication avant tout ajout (clé = nom normalisé + domaine) ;
  - NE JAMAIS écraser un champ saisi à la main (statut, notes, dates, prix) ;
  - tout ajout / modification est journalisé dans l'onglet Log.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import config
from crm_logic import is_duplicate, normalize_name


def today_iso() -> str:
    return date.today().isoformat()


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_jjmm(s: str | None) -> str:
    """« 12/03 » ou « 12/03/2026 » -> ISO. Vide -> aujourd'hui."""
    if not s:
        return today_iso()
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%d/%m", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s, fmt).date()
            if fmt == "%d/%m":
                d = d.replace(year=date.today().year)
            return d.isoformat()
        except ValueError:
            continue
    return today_iso()


class CRM:
    def __init__(self, backend, author: str = "claude"):
        self.b = backend
        self.author = author
        self._headers: list[str] | None = None

    # ---- accès données ---------------------------------------------------
    def headers(self) -> list[str]:
        if self._headers is None:
            vals = self.b.ws(config.TAB_FOURNISSEURS).get_all_values()
            self._headers = vals[0] if vals and vals[0] else list(config.COLUMNS)
        return self._headers

    def rows(self) -> list[dict]:
        return self.b.read_records(config.TAB_FOURNISSEURS)

    def _row_number(self, nom: str) -> int | None:
        """Numéro de ligne (1-based, en-tête = 1) du fournisseur, ou None."""
        target = normalize_name(nom)
        for i, r in enumerate(self.rows(), start=2):
            if normalize_name(r.get("nom", "")) == target:
                return i
        return None

    def find(self, nom: str) -> dict | None:
        target = normalize_name(nom)
        for r in self.rows():
            if normalize_name(r.get("nom", "")) == target:
                return r
        return None

    # ---- journalisation --------------------------------------------------
    def log(self, action: str, cible: str, detail: str = ""):
        self.b.append_row(config.TAB_LOG,
                          [now_stamp(), action, cible, detail, self.author],
                          raw=True)

    # ---- ajout (avec déduplication) -------------------------------------
    def add_supplier(self, data: dict, source: str = "manuel") -> dict:
        """Ajoute un fournisseur si non-doublon. Renvoie un dict de résultat.
        Nouveau = statut « À contacter » + source + date d'ajout."""
        nom = (data.get("nom") or "").strip()
        if not nom:
            return {"status": "error", "message": "nom manquant"}

        existing = self.rows()
        dup = is_duplicate(data, existing)
        if dup:
            self.log("ajout ignoré (doublon)", nom,
                     f"correspond à « {dup.get('nom')} »")
            return {"status": "duplicate", "match": dup.get("nom"), "row": dup}

        record = {k: (data.get(k, "") or "") for k in self.headers()}
        record["nom"] = nom
        if not record.get("statut"):
            record["statut"] = config.DEFAULT_STATUS
        record["source"] = data.get("source") or source
        record["date_ajout"] = today_iso()
        if not record.get("id"):
            record["id"] = self._next_id(existing)

        self.b.append_row(config.TAB_FOURNISSEURS,
                          [record.get(h, "") for h in self.headers()])
        self.log("ajout fournisseur", nom,
                 f"statut={record['statut']} source={record['source']}")
        return {"status": "added", "record": record}

    def _next_id(self, existing: list[dict]) -> str:
        nums = []
        for r in existing:
            rid = str(r.get("id", ""))
            tail = rid.split("-")[-1]
            if tail.isdigit():
                nums.append(int(tail))
        return f"NEW-{(max(nums) + 1) if nums else 1:02d}"

    # ---- mise à jour d'une fiche (sans écraser le manuel) ----------------
    def _update_fields(self, nom: str, updates: dict,
                       protect: bool = True) -> bool:
        """Écrit `updates` sur la ligne du fournisseur.
        Si protect=True, n'écrase PAS un champ déjà rempli à la main."""
        rn = self._row_number(nom)
        if rn is None:
            return False
        current = self.rows()[rn - 2]
        payload = []
        headers = self.headers()
        for key, val in updates.items():
            if key not in headers:
                continue
            if protect and str(current.get(key, "")).strip():
                continue  # champ déjà saisi -> on ne touche pas
            col = config.col_letter(key) if key in config.COLUMNS else None
            if col is None:
                # colonne hors schéma connu : retrouver par en-tête
                idx = headers.index(key)
                col = self._idx_to_letter(idx)
            payload.append((f"{col}{rn}", val))
        for a1, val in payload:
            raw = any(k in config.TEXT_COLUMNS for k in [self._letter_to_key(a1)])
            self.b.write_rows(config.TAB_FOURNISSEURS, a1, [[val]], raw=raw)
        return True

    @staticmethod
    def _idx_to_letter(idx: int) -> str:
        letters, n = "", idx + 1
        while n:
            n, rem = divmod(n - 1, 26)
            letters = chr(65 + rem) + letters
        return letters

    def _letter_to_key(self, a1: str) -> str:
        import re
        m = re.match(r"([A-Z]+)", a1)
        if not m:
            return ""
        letters = m.group(1)
        idx = 0
        for ch in letters:
            idx = idx * 26 + (ord(ch) - 64)
        idx -= 1
        headers = self.headers()
        return headers[idx] if idx < len(headers) else ""

    # ---- commandes -------------------------------------------------------
    def mark_contacted(self, nom: str, when: str | None = None) -> dict:
        d = parse_jjmm(when)
        relance = (datetime.fromisoformat(d).date() + timedelta(days=7)).isoformat()
        rn = self._row_number(nom)
        if rn is None:
            return {"status": "not_found", "nom": nom}
        # statut + dates : on écrit même si présent (action explicite),
        # SAUF si le statut manuel est « en aval » — on reste simple : on pose.
        self.b.write_rows(config.TAB_FOURNISSEURS,
                          f"{config.col_letter('statut')}{rn}", [["Contacté"]])
        self.b.write_rows(config.TAB_FOURNISSEURS,
                          f"{config.col_letter('date_contact')}{rn}", [[d]])
        self.b.write_rows(config.TAB_FOURNISSEURS,
                          f"{config.col_letter('date_relance')}{rn}", [[relance]])
        self.log("marqué contacté", nom, f"le {d}, relance {relance}")
        return {"status": "ok", "nom": nom, "date_contact": d, "date_relance": relance}

    def devis_recu(self, nom: str, infos: str = "") -> dict:
        rn = self._row_number(nom)
        if rn is None:
            # fournisseur inconnu -> nouvelle ligne
            res = self.add_supplier({"nom": nom, "statut": "Devis reçu",
                                     "notes": f"⚠ à valider — {infos}"},
                                    source="devis")
            self.log("devis reçu (nouveau fournisseur)", nom, infos)
            return {"status": "added_with_devis", "nom": nom, "detail": res}
        self.b.write_rows(config.TAB_FOURNISSEURS,
                          f"{config.col_letter('statut')}{rn}", [["Devis reçu"]])
        # notes : on APPEND (ne pas écraser le manuel)
        cur = self.rows()[rn - 2].get("notes", "")
        note = (cur + " | " if cur else "") + f"⚠ à valider — {infos}"
        self.b.write_rows(config.TAB_FOURNISSEURS,
                          f"{config.col_letter('notes')}{rn}", [[note]], raw=True)
        self.log("devis reçu", nom, infos)
        return {"status": "ok", "nom": nom}

    def who_to_relance(self, horizon_days: int = 3) -> list[dict]:
        limit = date.today() + timedelta(days=horizon_days)
        out = []
        for r in self.rows():
            dr = (r.get("date_relance") or "").strip()
            if not dr:
                continue
            try:
                d = datetime.fromisoformat(dr[:10]).date()
            except ValueError:
                continue
            if d <= limit:
                out.append({"nom": r.get("nom"), "pays": r.get("pays"),
                            "statut": r.get("statut"), "date_relance": dr})
        return sorted(out, key=lambda x: x["date_relance"])

    def stats(self) -> dict:
        rows = self.rows()
        by_status: dict[str, int] = {}
        by_country: dict[str, int] = {}
        for r in rows:
            by_status[r.get("statut", "")] = by_status.get(r.get("statut", ""), 0) + 1
            by_country[r.get("pays", "")] = by_country.get(r.get("pays", ""), 0) + 1
        return {
            "total": len(rows),
            "par_statut": dict(sorted(by_status.items())),
            "par_pays": dict(sorted(by_country.items(), key=lambda x: -x[1])),
            "devis_recus": by_status.get("Devis reçu", 0),
            "a_relancer": len(self.who_to_relance()),
        }

    def delete_supplier(self, nom: str) -> bool:
        """Supprime la ligne d'un fournisseur (utilisé par le test 1.7)."""
        rn = self._row_number(nom)
        if rn is None:
            return False
        sid = self.b.sheet_id(config.TAB_FOURNISSEURS)
        self.b.batch([{"deleteDimension": {"range": {
            "sheetId": sid, "dimension": "ROWS",
            "startIndex": rn - 1, "endIndex": rn}}}])
        self.log("suppression fournisseur", nom, "")
        return True
