"""Journalisation rotative par processus (§7.5) : 10 Mo × 5, dans logs/.

Niveaux INFO (cycles, bascules) / WARNING (trous, reconnexions) / ERROR.
Jamais de secret dans les logs.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import CONFIG


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    CONFIG.log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)sZ %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    fmt.converter = __import__("time").gmtime  # horodatage UTC (§2.4)

    fh = RotatingFileHandler(
        CONFIG.log_dir / f"{name}.log", maxBytes=10 * 1024 * 1024, backupCount=5
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.propagate = False
    return logger
