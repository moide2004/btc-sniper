"""Lecture des soldes des exchanges via CCXT (cles API en LECTURE SEULE).

CCXT unifie l'acces a 100+ exchanges. On n'appelle QUE fetch_balance (lecture).
Aucune fonction de trading ou de retrait n'est utilisee.
"""

import logging

try:
    import ccxt
except ImportError:  # pragma: no cover
    ccxt = None

from . import config

log = logging.getLogger("portfolio.exchanges")


def fetch_all():
    """Renvoie la liste des avoirs de tous les exchanges configures.

    Chaque avoir : {source, symbol, amount, kind='exchange'}.
    Renvoie (holdings, errors).
    """
    holdings = []
    errors = []
    if not config.EXCHANGES:
        return holdings, errors
    if ccxt is None:
        errors.append("Module 'ccxt' non installe (pip install ccxt)")
        return holdings, errors

    for name, creds in config.EXCHANGES.items():
        try:
            klass = getattr(ccxt, name)
            client = klass({**creds, "enableRateLimit": True})
            # Bybit : permet d'utiliser le miroir (bytick.com) si bybit.com
            # est bloque geographiquement.
            if name == "bybit" and config.BYBIT_HOSTNAME:
                try:
                    client.hostname = config.BYBIT_HOSTNAME
                    client.urls["api"] = client.describe()["urls"]["api"]
                except Exception:  # pragma: no cover - selon version de ccxt
                    pass
            balance = client.fetch_balance()
            totals = balance.get("total", {}) or {}
            label = name.capitalize()
            for symbol, amount in totals.items():
                try:
                    amt = float(amount)
                except (TypeError, ValueError):
                    continue
                if amt and amt > 0:
                    holdings.append({
                        "source": label,
                        "symbol": symbol,
                        "amount": amt,
                        "kind": "exchange",
                    })
        except Exception as e:  # CCXT leve des exceptions variees
            log.warning("Erreur exchange %s : %s", name, e)
            errors.append(name.capitalize() + " : " + str(e))
    return holdings, errors
