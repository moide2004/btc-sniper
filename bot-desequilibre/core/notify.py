"""Notification push OPTIONNELLE vers le téléphone via ntfy.sh.

AMENDEMENT du BRIEF approuvé par l'humain (2026-07-25) : l'invariant « aucune
notification externe » est levé UNIQUEMENT pour l'émission de tickets, sur
demande explicite. Désactivée par défaut (NTFY_TOPIC vide = aucun envoi).

Best-effort : jamais bloquant (timeout court), aucune exception propagée au
worker, aucun secret journalisé. Le sujet (topic) fait office de clé : le
choisir long et imprévisible.
"""
from __future__ import annotations

import requests as _requests_mod

from .config import CONFIG


def _h(s: str) -> str:
    """Les en-têtes HTTP doivent être latin-1 ; remplace le reste (ex. p̂)."""
    return s.encode("latin-1", "replace").decode("latin-1")


def send_push(title: str, message: str, priority: str = "default",
              tags: str = "bell", requests=_requests_mod) -> bool:
    """Envoie une notification si NTFY_TOPIC est configuré. Renvoie True si
    l'envoi a réussi, False sinon (désactivé, réseau en panne, etc.)."""
    topic = CONFIG.ntfy_topic
    if not topic:
        return False
    try:
        r = requests.post(
            f"{CONFIG.ntfy_url.rstrip('/')}/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": _h(title), "Priority": priority, "Tags": tags},
            timeout=8)
        return bool(getattr(r, "ok", False))
    except Exception:
        return False
