"""Agregateur de portefeuille multi-comptes (style CoinStats, perso).

Lit en LECTURE SEULE les soldes de plusieurs sources (exchanges via cle API
lecture seule, wallets via adresse publique), recupere les prix en temps reel
et calcule la valeur totale du portefeuille.

Aucune cle privee ni phrase secrete n'est jamais utilisee.
"""
