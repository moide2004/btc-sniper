"""Lecture des soldes de wallets via leur ADRESSE PUBLIQUE uniquement.

- MetaMask / Ethereum : solde natif ETH via JSON-RPC public.
- Phantom / Solana    : solde natif SOL + tous les tokens SPL via RPC public.

Aucune cle privee ni phrase secrete n'est requise : on lit des donnees
publiques sur la blockchain.

Note : l'enumeration automatique des tokens ERC-20 d'une adresse Ethereum
necessite un service d'indexation (cle API). Elle n'est pas incluse dans
cette v1 ; seul l'ETH natif est lu. Cote Solana, les tokens SPL sont lus
automatiquement (le RPC public le permet sans cle).
"""

import logging

import requests

from . import config

log = logging.getLogger("portfolio.wallets")

SESSION = requests.Session()
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def _rpc(url, method, params):
    """Appel JSON-RPC generique."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    r = SESSION.post(url, json=payload, timeout=20)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise ValueError(str(data["error"]))
    return data.get("result")


def fetch_evm():
    """Solde natif ETH de chaque adresse MetaMask configuree."""
    holdings = []
    errors = []
    for addr in config.EVM_ADDRESSES:
        try:
            result = _rpc(config.EVM_RPC_URL, "eth_getBalance", [addr, "latest"])
            wei = int(result, 16)
            eth = wei / 1e18
            if eth > 0:
                holdings.append({
                    "source": "MetaMask",
                    "symbol": "ETH",
                    "amount": eth,
                    "kind": "native",
                    "coingecko_id": "ethereum",
                    "label": "ETH (" + addr[:6] + "…" + addr[-4:] + ")",
                })
        except (requests.RequestException, ValueError) as e:
            log.warning("Erreur EVM %s : %s", addr, e)
            errors.append("MetaMask " + addr[:8] + "… : " + str(e))
    return holdings, errors


def fetch_solana():
    """Solde natif SOL + tokens SPL de chaque adresse Phantom configuree."""
    holdings = []
    errors = []
    for addr in config.SOLANA_ADDRESSES:
        short = addr[:4] + "…" + addr[-4:]
        # SOL natif
        try:
            result = _rpc(config.SOLANA_RPC_URL, "getBalance", [addr])
            lamports = result.get("value", 0) if isinstance(result, dict) else result
            sol = float(lamports) / 1e9
            if sol > 0:
                holdings.append({
                    "source": "Phantom",
                    "symbol": "SOL",
                    "amount": sol,
                    "kind": "native",
                    "coingecko_id": "solana",
                    "label": "SOL (" + short + ")",
                })
        except (requests.RequestException, ValueError) as e:
            log.warning("Erreur Solana SOL %s : %s", addr, e)
            errors.append("Phantom " + short + " (SOL) : " + str(e))
        # Tokens SPL
        try:
            result = _rpc(
                config.SOLANA_RPC_URL,
                "getTokenAccountsByOwner",
                [addr, {"programId": TOKEN_PROGRAM_ID}, {"encoding": "jsonParsed"}],
            )
            for acc in (result or {}).get("value", []):
                try:
                    info = acc["account"]["data"]["parsed"]["info"]
                    mint = info["mint"]
                    amt = float(info["tokenAmount"]["uiAmount"] or 0)
                except (KeyError, TypeError, ValueError):
                    continue
                if amt > 0:
                    holdings.append({
                        "source": "Phantom",
                        "symbol": mint[:4].upper(),
                        "amount": amt,
                        "kind": "token",
                        "chain": "solana",
                        "contract": mint,
                        "label": "SPL " + mint[:4] + "…" + mint[-4:] + " (" + short + ")",
                    })
        except (requests.RequestException, ValueError) as e:
            log.warning("Erreur Solana SPL %s : %s", addr, e)
            errors.append("Phantom " + short + " (SPL) : " + str(e))
    return holdings, errors


def fetch_all():
    """Tous les avoirs de tous les wallets configures."""
    holdings = []
    errors = []
    h, e = fetch_evm()
    holdings += h
    errors += e
    h, e = fetch_solana()
    holdings += h
    errors += e
    return holdings, errors
