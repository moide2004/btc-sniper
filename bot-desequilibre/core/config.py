"""Chargement de la configuration depuis .env (§4). Lecture unique au démarrage.

Multi-actif : SYMBOLS est une LISTE (BTCUSDT, ETHUSDT). Mini-parseur .env sans
dépendance ajoutée. Aucun secret en dur, aucun secret journalisé.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(ROOT / ".env")


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _path(key: str, default: str) -> Path:
    p = Path(_get(key, default))
    return p if p.is_absolute() else ROOT / p


class Config:
    def __init__(self) -> None:
        self.symbols = [s.strip().upper() for s in
                        _get("SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()]
        self.backfill_days = int(_get("BACKFILL_DAYS", "730"))

        self.db_path = _path("MOTEUR_DB", "data/moteur.db")
        self.ohlcv_dir = _path("OHLCV_DIR", "data/ohlcv")
        self.log_dir = _path("LOG_DIR", "logs")
        self.backup_dir = _path("BACKUP_DIR", "backups")

        self.capital_usd = float(_get("CAPITAL_USD", "3000"))
        self.fee_taker = float(_get("FEE_TAKER", "0.0010"))
        self.fee_maker = float(_get("FEE_MAKER", "0.0003"))
        self.risk_pct = float(_get("RISK_PCT", "0.015"))
        self.short_risk_factor = float(_get("SHORT_RISK_FACTOR", "0.75"))

        # --- Stratégie §2 (paramètres NOMMÉS au BRIEF ; défauts épinglés) -----
        # Les valeurs sans défaut explicite au BRIEF sont marquées (†) : à
        # confirmer par l'humain (§9.1). Aucun paramètre/indicateur en plus.
        self.fib_lookback = int(_get("FIB_LOOKBACK", "50"))          # §2 défaut 50
        self.buffer_atr = float(_get("BUFFER_ATR", "0.10"))          # (†) distance à la cassure
        self.activer_shorts = _get("ACTIVER_SHORTS", "1").strip().lower() in ("1", "true", "yes", "oui")
        self.stop_max_atr = float(_get("STOP_MAX_ATR", "3.0"))       # §2 défaut 3
        self.stop_min_atr = float(_get("STOP_MIN_ATR", "0.5"))       # §2 défaut 0,5
        self.be_trigger = float(_get("BE_TRIGGER", "1.0"))           # (†) break-even en R (P4)
        self.rr_mult = float(_get("RR_MULT", "1.5"))                 # §2 défaut 1,5 (rrMult vivant)
        self.rr_grid = [float(x) for x in _get("RR_GRID", "1.0,1.5,2.0").split(",") if x.strip()]  # (†) « 3 rrMult » §3
        self.min_amp_atr = float(_get("MIN_AMP_ATR", "1.0"))         # (†) amplitude minimale
        self.atr_period = int(_get("ATR_PERIOD", "14"))              # (†) ATR standard de Wilder
        self.analysis_timeframes = [s.strip() for s in
                                    _get("ANALYSIS_TIMEFRAMES", "15m,30m,1h,4h,12h,1D").split(",") if s.strip()]
        self.solide_min_n = int(_get("SOLIDE_MIN_N", "200"))         # §3 « solide » si n ≥ 200
        self.risk_cap_pct = float(_get("RISK_CAP_PCT", "4.0"))       # §5.4 plafond risque ouvert
        self.corr_high = float(_get("CORR_HIGH", "0.8"))             # §5.4 seuil ρ élevé

        self.heartbeat_seconds = int(_get("HEARTBEAT_SECONDS", "30"))
        self.primary_silence_seconds = int(_get("PRIMARY_SILENCE_SECONDS", "300"))

        self.data_primary = _get("DATA_PRIMARY", "binance").strip().lower()
        self.data_fallbacks = [s.strip().lower() for s in
                               _get("DATA_FALLBACKS", "kraken,coinbase,coingecko").split(",")
                               if s.strip()]

        self.web_password_hash = _get("WEB_PASSWORD_HASH", "")
        self.flask_secret_key = _get("FLASK_SECRET_KEY", "")

        # Push téléphone via ntfy (amendement BRIEF 2026-07-25, tickets seuls).
        # Vide = désactivé. Le topic est un secret : long et imprévisible.
        self.ntfy_topic = _get("NTFY_TOPIC", "").strip()
        self.ntfy_url = _get("NTFY_URL", "https://ntfy.sh").strip()

    def ensure_dirs(self) -> None:
        for d in (self.db_path.parent, self.ohlcv_dir, self.log_dir, self.backup_dir):
            d.mkdir(parents=True, exist_ok=True)


CONFIG = Config()
