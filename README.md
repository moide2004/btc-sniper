# CRM Fournisseurs Airlaid — ArcaneLogistics

Suivi des fournisseurs (serviette airlaid pocket « kangourou »,
réf. `SRV-AIRLAID-KGR-001`), relève des devis par e-mail (lecture seule) et
génération de **brouillons** de réponse — **jamais d'envoi automatique**.

> 🔴 **Règle d'or** : aucun envoi d'e-mail, aucun SMTP. La seule écriture
> e-mail autorisée est le dépôt de brouillons via IMAP APPEND (`\Draft`).

## Avancement

- ✅ **Phase 1 — CRM** (Google Sheets, Option A) : onglets Fournisseurs /
  Dashboard / Log, import des 53 fournisseurs, statuts + couleurs + validation,
  déduplication, journalisation, test 1.7.
- ⏳ Phase 2 — Relève des devis (IONOS, lecture seule)
- ⏳ Phase 3 — Brouillons de réponse (IMAP APPEND)
- ⏳ Phase 4 — Pipeline « prépare les réponses » + tests bout en bout

## Démarrage (Phase 1)

1. Configurer Google Sheets : voir **[GUIDE_GOOGLE_SHEETS.md](GUIDE_GOOGLE_SHEETS.md)**.
2. `cp .env.example .env` puis renseigner `GOOGLE_SERVICE_ACCOUNT_JSON` et `GSHEET_URL`.
3. `pip install -r requirements.txt`
4. `python setup_crm.py`  (ajouter `--force` pour une réinstallation propre)

## Commandes courantes

```bash
python crm_cli.py add "Nouvelle Usine" --pays Chine --site usine.cn
python crm_cli.py contacted "Pierrot Srl" --date 12/03
python crm_cli.py devis "García de Pou" --infos "0,041 EXW, MOQ 300000"
python crm_cli.py relance
python crm_cli.py stats
```

## Tests (hors-ligne, sans réseau)

```bash
python tests/test_all.py
```

Détails d'architecture et règles permanentes : voir **[CLAUDE.md](CLAUDE.md)**.
