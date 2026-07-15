"""Chargement de la configuration depuis .env (§7.8).

Lecture unique au démarrage. Aucun secret en dur, aucun secret journalisé.
Un mini-parseur .env est fourni pour ne dépendre d'aucune bibliothèque
supplémentaire (§2.6 : dépendances épinglées, rien d'autre sans accord).
"""
from __future__ import annotations

import os
from pathlib import Path

# Racine du projet = parent du dossier core/
ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Injecte les paires KEY=VALUE de .env dans os.environ (sans écraser
    l'environnement déjà défini, qui reste prioritaire)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(ROOT / ".env")


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _path(key: str, default: str) -> Path:
    p = Path(_get(key, default))
    return p if p.is_absolute() else ROOT / p


class Config:
    """Vue typée de la configuration. Instanciée une fois (voir CONFIG)."""

    def __init__(self) -> None:
        self.symbol = _get("SYMBOL", "BTCUSDT")
        self.backfill_days = int(_get("BACKFILL_DAYS", "730"))

        self.db_path = _path("MOTEUR_DB", "data/moteur.db")
        self.ohlcv_dir = _path("OHLCV_DIR", "data/ohlcv")
        self.log_dir = _path("LOG_DIR", "logs")
        self.backup_dir = _path("BACKUP_DIR", "backups")

        self.capital_usd = float(_get("CAPITAL_USD", "3000"))
        self.fee_taker = float(_get("FEE_TAKER", "0.0010"))
        self.fee_maker = float(_get("FEE_MAKER", "0.0003"))

        self.heartbeat_seconds = int(_get("HEARTBEAT_SECONDS", "30"))
        self.primary_silence_seconds = int(_get("PRIMARY_SILENCE_SECONDS", "300"))

        self.data_primary = _get("DATA_PRIMARY", "binance").strip().lower()
        self.data_fallbacks = [
            s.strip().lower()
            for s in _get("DATA_FALLBACKS", "kraken,coinbase,coingecko").split(",")
            if s.strip()
        ]

        self.web_password_hash = _get("WEB_PASSWORD_HASH", "")
        self.flask_secret_key = _get("FLASK_SECRET_KEY", "")

    def ensure_dirs(self) -> None:
        for d in (self.db_path.parent, self.ohlcv_dir, self.log_dir, self.backup_dir):
            d.mkdir(parents=True, exist_ok=True)


CONFIG = Config()
