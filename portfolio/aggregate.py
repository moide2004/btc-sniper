"""Assemble tous les avoirs, les valorise et calcule les totaux."""

import logging
import time
from datetime import datetime, timezone

from . import config, exchanges, prices

log = logging.getLogger("portfolio.aggregate")

_cache = {"data": None, "ts": 0}


def _value_holdings(holdings):
    """Ajoute price_usd et value_usd a chaque avoir."""
    # 1) Prix des avoirs identifies par symbole / coingecko_id.
    ids_needed = set()
    for h in holdings:
        if h["kind"] == "token":
            continue
        cid = h.get("coingecko_id") or prices.symbol_to_id(h["symbol"])
        h["_id"] = cid
        if cid:
            ids_needed.add(cid)
    id_prices = prices.prices_by_ids(ids_needed)

    # 2) Prix des tokens par contrat, regroupes par chaine.
    by_chain = {}
    for h in holdings:
        if h["kind"] == "token":
            by_chain.setdefault(h["chain"], set()).add(h["contract"].lower())
    contract_prices = {}
    for chain, addrs in by_chain.items():
        contract_prices[chain] = prices.prices_by_contracts(chain, addrs)

    # 3) Valorisation.
    for h in holdings:
        if h["kind"] == "token":
            price = contract_prices.get(h["chain"], {}).get(h["contract"].lower(), 0)
        else:
            price = id_prices.get(h.get("_id"), 0)
        h["price_usd"] = price
        h["value_usd"] = price * h["amount"]
        h.pop("_id", None)
    return holdings


def build_portfolio():
    """Recupere, valorise et agrege l'ensemble du portefeuille."""
    holdings = []
    errors = []

    h, e = exchanges.fetch_all()
    holdings += h
    errors += e

    holdings = _value_holdings(holdings)
    holdings.sort(key=lambda x: x.get("value_usd", 0), reverse=True)

    total = sum(h.get("value_usd", 0) for h in holdings)
    by_source = {}
    for h in holdings:
        by_source[h["source"]] = by_source.get(h["source"], 0) + h.get("value_usd", 0)

    return {
        "total_usd": total,
        "by_source": by_source,
        "holdings": holdings,
        "errors": errors,
        "vs_currency": config.VS_CURRENCY,
        "updated": datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y %H:%M:%S"),
    }


def get_portfolio(force=False):
    """Version avec cache (evite de marteler les APIs a chaque rafraichissement)."""
    now = time.time()
    if not force and _cache["data"] and (now - _cache["ts"] < config.CACHE_TTL):
        return _cache["data"]
    data = build_portfolio()
    _cache["data"] = data
    _cache["ts"] = now
    return data
