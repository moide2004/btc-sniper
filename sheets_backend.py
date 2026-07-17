"""
sheets_backend.py — Backend Google Sheets (Option A) via gspread + service account.

Encapsule TOUS les appels réseau à l'API Sheets. Le reste du CRM (crm.py) ne
connaît que cette interface, ce qui laisse la porte ouverte à un backend
Excel local (Option B) plus tard sans rien réécrire ailleurs.

Leçons d'installation intégrées (Phase 1.3) :
  - locale fr_FR : formules en ';' + 0/1 (gérées dans crm_logic) ;
  - onglet vierge : gspread 6 renvoie [[]] -> test de contenu réel ;
  - téléphones/ids en format TEXTE ;
  - filtre auto + mises en forme couvrant les lignes FUTURES ;
  - réinstallation : purge des règles de couleur avant recréation.
"""
from __future__ import annotations

import config
from crm_logic import is_really_empty


class GoogleSheetsBackend:
    def __init__(self, key_path: str, sheet_url: str):
        self.key_path = key_path
        self.sheet_url = sheet_url
        self._gc = None
        self._ss = None

    # ---- connexion -------------------------------------------------------
    def connect(self):
        import gspread
        from google.oauth2.service_account import Credentials

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(self.key_path, scopes=scopes)
        self._gc = gspread.authorize(creds)
        self._ss = self._gc.open_by_url(self.sheet_url)
        return self

    @property
    def ss(self):
        if self._ss is None:
            raise RuntimeError("Backend non connecté : appelez connect() d'abord.")
        return self._ss

    def sheet_id(self, title: str) -> int:
        return self.ws(title).id

    # ---- onglets ---------------------------------------------------------
    def ws(self, title: str):
        return self.ss.worksheet(title)

    def ensure_worksheet(self, title: str, rows: int, cols: int):
        try:
            return self.ss.worksheet(title)
        except Exception:
            return self.ss.add_worksheet(title=title, rows=rows, cols=cols)

    def worksheet_is_empty(self, title: str) -> bool:
        """Test de vacuité RÉEL (gère le [[]] de gspread 6)."""
        return is_really_empty(self.ws(title).get_all_values())

    # ---- lecture / écriture ---------------------------------------------
    def read_records(self, title: str) -> list[dict]:
        ws = self.ws(title)
        values = ws.get_all_values()
        if is_really_empty(values):
            return []
        headers = values[0]
        return [dict(zip(headers, r + [""] * (len(headers) - len(r)))) for r in values[1:]]

    def write_rows(self, title: str, start_a1: str, rows: list[list], raw: bool = False):
        ws = self.ws(title)
        ws.update(start_a1, rows,
                  value_input_option="RAW" if raw else "USER_ENTERED")

    def append_row(self, title: str, row: list, raw: bool = False):
        self.ws(title).append_row(
            row, value_input_option="RAW" if raw else "USER_ENTERED",
            table_range="A1")

    # ---- batch bas niveau ------------------------------------------------
    def batch(self, requests: list[dict]):
        if requests:
            self.ss.batch_update({"requests": requests})

    # ---- mises en forme / structure (Phase 1.3) --------------------------
    def set_column_text_format(self, title: str, col_key: str,
                               r1: int = 1, r2: int | None = None):
        """Force une colonne au format TEXTE (téléphones, ids…)."""
        r2 = r2 or config.PROVISIONED_ROWS
        sid = self.sheet_id(title)
        c = config.col_index(col_key)
        self.batch([{
            "repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": r1 - 1, "endRowIndex": r2,
                          "startColumnIndex": c, "endColumnIndex": c + 1},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT"}}},
                "fields": "userEnteredFormat.numberFormat",
            }
        }])

    def format_header(self, title: str, ncols: int):
        sid = self.sheet_id(title)
        self.batch([
            {"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                          "startColumnIndex": 0, "endColumnIndex": ncols},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": config.hex_to_rgb01("2F5496"),
                    "textFormat": {"bold": True,
                                   "foregroundColor": config.hex_to_rgb01("FFFFFF")}}},
                "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
            {"updateSheetProperties": {
                "properties": {"sheetId": sid,
                               "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount"}},
        ])

    def set_basic_filter(self, title: str, ncols: int, r2: int | None = None):
        """Filtre automatique couvrant les lignes FUTURES (pas juste le seed)."""
        r2 = r2 or config.PROVISIONED_ROWS
        sid = self.sheet_id(title)
        # On retire un éventuel filtre existant avant d'en poser un neuf.
        self.batch([{"clearBasicFilter": {"sheetId": sid}}])
        self.batch([{"setBasicFilter": {"filter": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": r2,
                      "startColumnIndex": 0, "endColumnIndex": ncols}}}}])

    def purge_conditional_formats(self, title: str):
        """Supprime TOUTES les règles de mise en forme conditionnelle de l'onglet
        (réinstallation --force : pas d'empilement de couleurs) — Phase 1.3."""
        sid = self.sheet_id(title)
        meta = self.ss.fetch_sheet_metadata()
        count = 0
        for sh in meta.get("sheets", []):
            if sh.get("properties", {}).get("sheetId") == sid:
                count = len(sh.get("conditionalFormats", []))
                break
        # On supprime toujours l'index 0 : la liste se décale à chaque retrait.
        self.batch([{"deleteConditionalFormatRule": {"sheetId": sid, "index": 0}}
                    for _ in range(count)])
        return count

    def add_status_color_rules(self, title: str, status_col_key: str,
                               r1: int = 2, r2: int | None = None):
        """Une règle de couleur par statut, couvrant les lignes FUTURES."""
        r2 = r2 or config.PROVISIONED_ROWS
        sid = self.sheet_id(title)
        c = config.col_index(status_col_key)
        rng = {"sheetId": sid, "startRowIndex": r1 - 1, "endRowIndex": r2,
               "startColumnIndex": c, "endColumnIndex": c + 1}
        reqs = []
        for status, hex_color in config.STATUS_HEX.items():
            reqs.append({"addConditionalFormatRule": {"index": 0, "rule": {
                "ranges": [rng],
                "booleanRule": {
                    "condition": {"type": "TEXT_EQ",
                                  "values": [{"userEnteredValue": status}]},
                    "format": {"backgroundColor": config.hex_to_rgb01(hex_color)}},
            }}})
        self.batch(reqs)

    def set_status_validation(self, title: str, status_col_key: str,
                              r1: int = 2, r2: int | None = None):
        """Liste déroulante des statuts. strict=False (afficher un avertissement,
        pas rejeter) : la valeur héritée « Réserve ouate » reste acceptée."""
        r2 = r2 or config.PROVISIONED_ROWS
        sid = self.sheet_id(title)
        c = config.col_index(status_col_key)
        values = [{"userEnteredValue": s} for s in config.ALL_STATUSES]
        self.batch([{"setDataValidation": {
            "range": {"sheetId": sid, "startRowIndex": r1 - 1, "endRowIndex": r2,
                      "startColumnIndex": c, "endColumnIndex": c + 1},
            "rule": {"condition": {"type": "ONE_OF_LIST", "values": values},
                     "showCustomUi": True, "strict": False}}}])

    def autoresize_columns(self, title: str, ncols: int):
        sid = self.sheet_id(title)
        self.batch([{"autoResizeDimensions": {"dimensions": {
            "sheetId": sid, "dimension": "COLUMNS",
            "startIndex": 0, "endIndex": ncols}}}])
