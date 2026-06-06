"""Lecture de la configuration depuis les variables d'environnement / .env.

Aucun secret n'est ecrit en dur ici : tout vient du fichier .env (non
versionne) ou des variables d'environnement de l'hebergeur.
"""

import os

# Charge le .env place a la racine du projet, s'il existe.
try:
    from dotenv import load_dotenv
    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(_ROOT, ".env"))
except ImportError:
    pass


def _split(value):
    """Transforme 'a, b ,c' en ['a', 'b', 'c'] (vide -> [])."""
    return [x.strip() for x in (value or "").split(",") if x.strip()]


# --- Securite du tableau de bord ------------------------------------------- #
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "")

# --- Exchanges (cles API en LECTURE SEULE) --------------------------------- #
# Chaque exchange est optionnel : s'il n'y a pas de cle, il est ignore.
EXCHANGES = {}

_bitget_key = os.environ.get("BITGET_API_KEY", "")
if _bitget_key:
    EXCHANGES["bitget"] = {
        "apiKey": _bitget_key,
        "secret": os.environ.get("BITGET_API_SECRET", ""),
        "password": os.environ.get("BITGET_API_PASSWORD", ""),
    }

_binance_key = os.environ.get("BINANCE_API_KEY", "")
if _binance_key:
    EXCHANGES["binance"] = {
        "apiKey": _binance_key,
        "secret": os.environ.get("BINANCE_API_SECRET", ""),
    }

_bybit_key = os.environ.get("BYBIT_API_KEY", "")
if _bybit_key:
    EXCHANGES["bybit"] = {
        "apiKey": _bybit_key,
        "secret": os.environ.get("BYBIT_API_SECRET", ""),
    }

# --- Donnees de marche (endpoints PUBLICS, aucune cle) --------------------- #
# Symboles a suivre (perpetuels USDT), separes par des virgules.
MARKET_SYMBOLS = _split(os.environ.get("MARKET_SYMBOLS", "BTCUSDT")) or ["BTCUSDT"]
# Exchanges pour les donnees de marche (publiques).
MARKET_EXCHANGES = _split(os.environ.get("MARKET_EXCHANGES", "bybit,bitget")) or ["bybit", "bitget"]

# Hotes Bybit a essayer dans l'ordre (bascule automatique en cas de blocage).
# api.bytick.com est le miroir officiel, utile si api.bybit.com est filtre.
BYBIT_HOSTS = _split(os.environ.get("BYBIT_HOSTS", "")) or [
    "api.bybit.com", "api.bytick.com",
]
# Hostname CCXT pour les soldes Bybit (ex. "bytick.com" si bybit.com bloque).
BYBIT_HOSTNAME = os.environ.get("BYBIT_HOSTNAME", "")

# Devise de reference pour la valorisation.
VS_CURRENCY = os.environ.get("VS_CURRENCY", "usd").lower()

# Duree de cache des donnees du portefeuille (secondes).
CACHE_TTL = int(os.environ.get("CACHE_TTL", "60"))
