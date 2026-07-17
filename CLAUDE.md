# CLAUDE.md — CRM Fournisseurs Airlaid (ArcaneLogistics)

Projet : centrale d'achat française. Produit principal : serviette airlaid
pocket porte-couverts « kangourou », réf. **SRV-AIRLAID-KGR-001**.

---

## 🔴 RÈGLE D'OR ABSOLUE (valable partout, toutes phases)

**AUCUN ENVOI D'E-MAIL, AUCUN CODE SMTP dans tout le projet.**
La seule écriture e-mail autorisée est le dépôt de **brouillons via IMAP APPEND**
avec le flag `\Draft`. **Ne jamais supprimer ni déplacer un mail reçu.**

---

## 🟢 RÈGLE PERMANENTE — TENUE DU CRM (Phase 1.6)

Ce projet contient le CRM fournisseurs (Google Sheets — URL dans `.env`,
variable `GSHEET_URL`). Dans **toute session future**, dès que des fournisseurs
sont identifiés (recherche web, e-mails, documents, listes collées),
**les AJOUTER au tracker sans qu'on te le demande**.

- **Déduplication avant ajout** (clé = nom normalisé + domaine). Voir
  `crm_logic.is_duplicate`. Même domaine = même entreprise.
- **Nouveau** = statut `À contacter` + `source` + `date_ajout`.
- **Ne JAMAIS écraser** les champs saisis à la main (statut, notes, dates, prix).
  Les mises à jour automatiques n'écrasent qu'un champ vide, sinon elles
  ajoutent à la suite (ex. notes).
- **Log** chaque ajout / modification (onglet `Log`).

## Commandes à reconnaître (langage naturel → fonctions `crm.py`)

| Commande | Effet | Fonction |
|---|---|---|
| `ajoute fournisseur <nom>` | ajoute (avec dédup) | `CRM.add_supplier` |
| `marque <nom> contacté [le JJ/MM]` | statut `Contacté` + `date_contact` + `date_relance` J+7 | `CRM.mark_contacted` |
| `devis reçu de <nom> : <infos>` | statut `Devis reçu` + note `⚠ à valider` | `CRM.devis_recu` |
| `qui relancer ?` | fournisseurs `date_relance ≤ aujourd'hui + 3 j` | `CRM.who_to_relance` |
| `stats` | compteurs par statut / pays, devis, à relancer | `CRM.stats` |

En CLI : `python crm_cli.py {add|contacted|devis|relance|stats}`.

---

## Statuts (validation par liste déroulante)

`À sourcer` · `À contacter` · `Contacté` · `Relancé` · `Devis reçu` ·
`Échantillons` · `Qualifié` · `Écarté` · `À vérifier` · `Substrat` ·
`Réserve` · `Benchmark`

Statut hérité `Réserve ouate` (ligne NA-53) : **accepté** (présent dans la
validation, sans alerte) mais **jamais proposé** pour une nouvelle fiche.

Couleurs : vert = `Qualifié`, rouge = `Écarté`, bleu = `Devis reçu`,
beige = `Réserve` / `Réserve ouate` (voir `config.STATUS_HEX`).

---

## Fiche produit — SRV-AIRLAID-KGR-001 (source unique de vérité)

- Grammage **55 g/m² ±5 %** · format ouvert **40×40 cm ±5 mm** ·
  format fermé **≈ 20×10 cm**
- Couleur **sable / ivoire** · gaufrage **toile de lin**
- Contact alimentaire **CE 1935/2004** · qualité **≥ García de Pou**
- 1re commande : **1 000 000 pcs** = 800 000 imprimées logo 1 couleur
  + 200 000 neutres
- **Golden sample obligatoire avant production** (échantillon teinte + BAT imprimé)
- Livraison : **Arles (13200), France**

⚠️ Pour répondre aux fournisseurs (Phase 3), n'utiliser QUE ces valeurs
(voir `config.PRODUCT`). Toute info absente → écrire `[À COMPLÉTER : …]` dans
le brouillon et le signaler. **Ne JAMAIS inventer.**

---

## Architecture / fichiers

| Fichier | Rôle |
|---|---|
| `config.py` | Constantes métier, schéma colonnes, statuts, couleurs, fiche produit. Aucun secret. |
| `crm_logic.py` | Logique pure : normalisation, dédup, seed, formules fr_FR, détection onglet vide. |
| `sheets_backend.py` | Backend Google Sheets (gspread) : tous les appels réseau. |
| `crm.py` | Opérations CRM (add/contacted/devis/relance/stats/log). |
| `setup_crm.py` | **Phase 1** : construit les 3 onglets + importe le seed + test 1.7. |
| `crm_cli.py` | Commandes en ligne de commande. |
| `tests/test_all.py` | Tests hors-ligne (98 assertions). |
| `fournisseurs_seed.csv` | 53 fournisseurs (séparateur `;`). |

Leçons d'installation intégrées (Phase 1.3) : formules fr_FR en `;` + `0/1` ;
détection réelle d'onglet vide (gspread 6 → `[[]]`) ; téléphones/ids en format
TEXTE ; filtre auto + mises en forme couvrant les lignes FUTURES ;
réinstallation `--force` = purge des règles de couleur avant recréation.

---

## Statut d'avancement

- [x] **Phase 1 — CRM** (Google Sheets, Option A) + test 1.7
- [ ] Phase 2 — Relève des devis (IONOS, lecture seule) — `fetch_devis.py`
- [ ] Phase 3 — Brouillons de réponse (IMAP APPEND) — `draft_replies.py`
- [ ] Phase 4 — Pipeline « prépare les réponses » + tests bout en bout

<!-- RÈGLES DE RÉPONSE (Phase 4.1 « règle de réponse : … ») : ajouter ci-dessous -->
