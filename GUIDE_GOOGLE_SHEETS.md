# Guide pas à pas — Google Sheets (Option A)

Objectif : permettre au script `setup_crm.py` d'écrire dans votre feuille
Google. On crée un « service account » (robot Google) et on partage la feuille
avec lui. ~10 minutes, une seule fois.

> Vous ne me donnerez QUE le **chemin** du fichier JSON (jamais son contenu)
> et l'**URL** de la feuille. La clé reste sur votre PC et est git-ignorée.

---

## 1. Projet Google Cloud + APIs

1. Ouvrez <https://console.cloud.google.com/> (connectez-vous avec votre compte).
2. En haut, créez un projet (ex. `crm-airlaid`) ou sélectionnez-en un.
3. Activez les 2 APIs (menu **APIs & Services → Library**, cherchez et cliquez **Enable**) :
   - **Google Sheets API**
   - **Google Drive API**

## 2. Créer le service account

4. **APIs & Services → Credentials → Create credentials → Service account**.
5. Nom : `crm-airlaid-bot`. Cliquez **Create and continue**, puis **Done**
   (aucun rôle nécessaire).
6. Dans la liste, cliquez le service account → onglet **Keys** →
   **Add key → Create new key → JSON → Create**.
7. Un fichier `.json` se télécharge. **Rangez-le dans un dossier sûr**
   (PAS dans ce dépôt git ; ou dans un sous-dossier `keys/`, déjà ignoré).
   Notez son **chemin absolu** (ex. `C:\Users\vous\keys\crm-airlaid.json`
   ou `/home/vous/keys/crm-airlaid.json`).
8. Ouvrez ce JSON, repérez la ligne `"client_email"` :
   c'est l'adresse du robot, du type
   `crm-airlaid-bot@crm-airlaid.iam.gserviceaccount.com`. **Copiez-la.**

## 3. Créer et partager la feuille

9. Sur <https://sheets.google.com>, créez une feuille vierge nommée
   **`CRM Fournisseurs Airlaid`**.
10. Bouton **Partager** → collez l'adresse du robot (`client_email`) →
    rôle **Éditeur** → **Envoyer** (décochez la notification par e-mail).
11. Copiez l'**URL** de la feuille (la barre d'adresse,
    `https://docs.google.com/spreadsheets/d/......../edit`).

## 4. Renseigner `.env`

```bash
cp .env.example .env
```
Puis éditez `.env` :
```
GOOGLE_SERVICE_ACCOUNT_JSON=/chemin/absolu/vers/crm-airlaid.json
GSHEET_URL=https://docs.google.com/spreadsheets/d/......../edit
```

## 5. Installer et lancer

```bash
pip install -r requirements.txt
python setup_crm.py
```

Le script :
- crée les onglets **Fournisseurs / Dashboard / Log** ;
- importe les 53 fournisseurs du seed (données intactes) ;
- applique validation des statuts, couleurs, filtre auto, formats ;
- construit le Dashboard (compteurs statut/pays, à relancer, top 10, devis) ;
- exécute le **test obligatoire 1.7** et l'affiche.

Réinstallation propre (purge des règles de couleur, pas d'empilement) :
```bash
python setup_crm.py --force
```

---

### Dépannage rapide
- `403 / PERMISSION_DENIED` → la feuille n'est pas partagée en **Éditeur**
  avec le `client_email`, ou une des 2 APIs n'est pas activée.
- `SpreadsheetNotFound` → mauvaise `GSHEET_URL`.
- `FileNotFoundError` → mauvais chemin dans `GOOGLE_SERVICE_ACCOUNT_JSON`.
