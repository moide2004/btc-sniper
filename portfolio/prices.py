"""Recuperation des prix en temps reel via l'API publique CoinGecko (gratuite).

Deux modes :
- par identifiant CoinGecko (ex. 'bitcoin') pour les coins natifs / symboles ;
- par adresse de contrat (ex. un token ERC-20 ou SPL) pour les tokens.
"""

import logging

import requests

from . import config

log = logging.getLogger("portfolio.prices")

COINGECKO = "https://api.coingecko.com/api/v3"
SESSION = requests.Session()

# Correspondances sures pour les symboles les plus courants (evite les
# collisions de symboles dans la liste complete de CoinGecko).
CURATED = {
    "btc": "bitcoin", "eth": "ethereum", "sol": "solana", "bnb": "binancecoin",
    "xrp": "ripple", "ada": "cardano", "doge": "dogecoin", "avax": "avalanche-2",
    "dot": "polkadot", "matic": "matic-network", "pol": "matic-network",
    "ltc": "litecoin", "link": "chainlink", "atom": "cosmos", "near": "near",
    "trx": "tron", "usdt": "tether", "usdc": "usd-coin", "dai": "dai",
    "busd": "binance-usd", "tusd": "true-usd", "fdusd": "first-digital-usd",
    "wbtc": "wrapped-bitcoin", "weth": "weth", "arb": "arbitrum", "op": "optimism",
    "shib": "shiba-inu", "uni": "uniswap", "aave": "aave", "ldo": "lido-dao",
    "bgb": "bitget-token",
}

# Chaines pour l'endpoint token_price de CoinGecko.
CHAIN_PLATFORM = {
    "ethereum": "ethereum",
    "solana": "solana",
}

_symbol_index = None  # cache symbole -> id (premiere correspondance)


def _coins_list_index():
    """Telecharge une fois la liste CoinGecko et indexe symbole -> id."""
    global _symbol_index
    if _symbol_index is not None:
        return _symbol_index
    _symbol_index = {}
    try:
        r = SESSION.get(COINGECKO + "/coins/list", timeout=20)
        r.raise_for_status()
        for coin in r.json():
            sym = (coin.get("symbol") or "").lower()
            if sym and sym not in _symbol_index:
                _symbol_index[sym] = coin.get("id")
    except (requests.RequestException, ValueError) as e:
        log.warning("Liste CoinGecko indisponible : %s", e)
    return _symbol_index


def symbol_to_id(symbol):
    """Convertit un symbole (ex. 'BTC') en id CoinGecko (ex. 'bitcoin')."""
    s = (symbol or "").lower()
    if s in CURATED:
        return CURATED[s]
    return _coins_list_index().get(s)


def prices_by_ids(ids):
    """Prix pour une liste d'ids CoinGecko -> {id: prix} dans VS_CURRENCY."""
    ids = sorted({i for i in ids if i})
    if not ids:
        return {}
    out = {}
    # Decoupage par lots pour ne pas faire d'URL trop longue.
    for i in range(0, len(ids), 200):
        batch = ids[i:i + 200]
        try:
            r = SESSION.get(
                COINGECKO + "/simple/price",
                params={"ids": ",".join(batch), "vs_currencies": config.VS_CURRENCY},
                timeout=20,
            )
            r.raise_for_status()
            for cid, val in r.json().items():
                out[cid] = float(val.get(config.VS_CURRENCY, 0) or 0)
        except (requests.RequestException, ValueError) as e:
            log.warning("Prix par id indisponibles : %s", e)
    return out


def prices_by_contracts(chain, addresses):
    """Prix de tokens par adresse de contrat -> {adresse_min: prix}."""
    platform = CHAIN_PLATFORM.get(chain)
    addresses = sorted({a for a in addresses if a})
    if not platform or not addresses:
        return {}
    out = {}
    for i in range(0, len(addresses), 100):
        batch = addresses[i:i + 100]
        try:
            r = SESSION.get(
                COINGECKO + "/simple/token_price/" + platform,
                params={
                    "contract_addresses": ",".join(batch),
                    "vs_currencies": config.VS_CURRENCY,
                },
                timeout=20,
            )
            r.raise_for_status()
            for addr, val in r.json().items():
                out[addr.lower()] = float(val.get(config.VS_CURRENCY, 0) or 0)
        except (requests.RequestException, ValueError) as e:
            log.warning("Prix par contrat indisponibles (%s) : %s", chain, e)
    return out
