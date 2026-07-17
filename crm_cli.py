#!/usr/bin/env python3
"""
crm_cli.py — Exécute les commandes du CRM (Phase 1.6) en ligne de commande.

En session Claude, ces commandes se disent en langage naturel
(« ajoute fournisseur X », « marque X contacté le 12/03 », « qui relancer ? »,
« stats », « devis reçu de X : … ») et appellent les mêmes fonctions.

Exemples :
    python crm_cli.py add "Nouvelle Usine" --pays Chine --site usine.cn
    python crm_cli.py contacted "Pierrot Srl" --date 12/03
    python crm_cli.py devis "García de Pou" --infos "0,041 EXW, MOQ 300000"
    python crm_cli.py relance
    python crm_cli.py stats
"""
from __future__ import annotations

import argparse
import json

import config
from crm import CRM
from sheets_backend import GoogleSheetsBackend


def get_crm() -> CRM:
    config.load_env()
    key = config.env("GOOGLE_SERVICE_ACCOUNT_JSON", required=True)
    url = config.env("GSHEET_URL", required=True)
    return CRM(GoogleSheetsBackend(key, url).connect(), author="cli")


def main():
    p = argparse.ArgumentParser(description="Commandes CRM Fournisseurs Airlaid")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="ajoute fournisseur <nom>")
    a.add_argument("nom")
    a.add_argument("--pays", default="")
    a.add_argument("--site", default="")
    a.add_argument("--email", default="")
    a.add_argument("--source", default="manuel")

    c = sub.add_parser("contacted", help="marque <nom> contacté [le JJ/MM]")
    c.add_argument("nom")
    c.add_argument("--date", default=None)

    d = sub.add_parser("devis", help="devis reçu de <nom> : <infos>")
    d.add_argument("nom")
    d.add_argument("--infos", default="")

    sub.add_parser("relance", help="qui relancer ?")
    sub.add_parser("stats", help="stats")

    args = p.parse_args()
    crm = get_crm()

    if args.cmd == "add":
        res = crm.add_supplier({"nom": args.nom, "pays": args.pays,
                                "site_web": args.site, "email": args.email},
                               source=args.source)
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    elif args.cmd == "contacted":
        print(json.dumps(crm.mark_contacted(args.nom, args.date),
                         ensure_ascii=False, indent=2))
    elif args.cmd == "devis":
        print(json.dumps(crm.devis_recu(args.nom, args.infos),
                         ensure_ascii=False, indent=2, default=str))
    elif args.cmd == "relance":
        rows = crm.who_to_relance()
        if not rows:
            print("— personne à relancer —")
        for r in rows:
            print(f"  {r['date_relance']}  {r['nom']} ({r['pays']}) [{r['statut']}]")
    elif args.cmd == "stats":
        print(json.dumps(crm.stats(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
