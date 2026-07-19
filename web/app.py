"""Mini-app Flask — LECTURE SEULE de moteur.db (§7.1).

P1 livre le socle web : authentification obligatoire (§6.4), endpoints JSON
avec `generated_at`, bandeau d'âge des données + passage en ⚠ si le heartbeat
du worker dépasse 120 s (§6.3, §7.3). Les 4 vues complètes (§6.1) et la cloche
riche arrivent en P3 ; l'ossature d'endpoints est déjà en place.

Aucune écriture ici, SAUF la table dédiée web_actions (acquittement/lu-non-lu),
non exposée en P1.
"""
from __future__ import annotations

import hashlib
import sys
from functools import wraps
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import (  # noqa: E402
    Flask, jsonify, redirect, render_template, request, session, url_for,
)

from core.config import CONFIG  # noqa: E402
from core.data_source import last_open_time, load_ohlcv  # noqa: E402
from core.store import Store, utc_now_iso  # noqa: E402

# Seuil de fraîcheur du heartbeat (§6.3, §7.3).
STALE_AFTER_S = 120

app = Flask(__name__)
app.secret_key = CONFIG.flask_secret_key or "dev-insecure-change-me"


def _read_store() -> Store:
    """Connexion SQLite en LECTURE SEULE (§7.1)."""
    return Store(read_only=True)


# --------------------------------------------------------------------------
# Authentification (§6.4) — mot de passe unique, hash en variable d'env.
# --------------------------------------------------------------------------
def _check_password(password: str) -> bool:
    stored = CONFIG.web_password_hash
    if not stored:
        return False
    # werkzeug (fourni par flask) si le hash en a la forme ; sinon sha256 hex.
    if stored.startswith(("pbkdf2:", "scrypt:", "argon2:")):
        try:
            from werkzeug.security import check_password_hash
            return check_password_hash(stored, password)
        except Exception:
            return False
    return hashlib.sha256(password.encode("utf-8")).hexdigest() == stored


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("auth"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "auth_required"}), 401
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if _check_password(request.form.get("password", "")):
            session["auth"] = True
            return redirect(request.args.get("next") or url_for("index"))
        error = "Mot de passe invalide."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------
# Vues
# --------------------------------------------------------------------------
@app.route("/")
@login_required
def index():
    return render_template("index.html", symbol=CONFIG.symbol)


# --------------------------------------------------------------------------
# Endpoints JSON — chacun renvoie generated_at (§6.3)
# --------------------------------------------------------------------------
@app.route("/api/health")
@login_required
def api_health():
    with _read_store() as st:
        hb = st.get_heartbeat()
        age = st.heartbeat_age_seconds()
        unread = st.unread_count()
    last_1m = last_open_time("1m")
    df = load_ohlcv("1m")
    payload = {
        "generated_at": utc_now_iso(),
        "symbol": CONFIG.symbol,
        "worker": hb,
        "heartbeat_age_s": None if age is None else round(age, 1),
        "worker_stale": (age is None) or (age > STALE_AFTER_S),
        "stale_threshold_s": STALE_AFTER_S,
        "source": hb["source"] if hb else None,
        "bars_1m": int(len(df)),
        "last_1m_open_ms": last_1m,
        "unread_events": unread,
    }
    return jsonify(payload)


@app.route("/api/evenements")
@login_required
def api_evenements():
    since = int(request.args.get("since_id", 0))
    with _read_store() as st:
        events = st.list_events(limit=100, since_id=since)
        unread = st.unread_count()
    return jsonify({"generated_at": utc_now_iso(), "unread": unread, "events": events})


@app.route("/api/evenements/lu", methods=["POST"])
@login_required
def api_marquer_lu():
    # Seule écriture autorisée côté web (§7.1) : marquage lu, table dédiée.
    st = Store(read_only=False)
    try:
        n = st.mark_events_read()
    finally:
        st.close()
    return jsonify({"ok": True, "marques_lus": n})


@app.route("/api/synthese")
@login_required
def api_synthese():
    # Synthèse bayésienne (§5.6) + référence neutre (§5.7) — snapshot quotidien.
    import json
    with _read_store() as st:
        raw = st.get_kv("synthese_latest")
    if not raw:
        return jsonify({"generated_at": utc_now_iso(),
                        "note": "Synthèse pas encore calculée — lancer daily_update.py."})
    data = json.loads(raw)
    data["served_at"] = utc_now_iso()
    return jsonify(data)


@app.route("/api/livre")
@login_required
def api_livre():
    # P1 : ossature. Positions/tickets/CUSUM arrivent en P4.
    with _read_store() as st:
        hb = st.get_heartbeat()
        age = st.heartbeat_age_seconds()
    return jsonify({
        "generated_at": utc_now_iso(),
        "worker_stale": (age is None) or (age > STALE_AFTER_S),
        "risque_ouvert_pct": 0.0, "plafond_pct": 3.0,
        "positions": [], "tickets_actifs": [], "tickets_bloques": [],
        "note": "Livre alimenté en P4 (paper trading).",
    })


@app.route("/api/probas")
@login_required
def api_probas():
    # Matrice des 11 timeframes (§5) — snapshot calculé par le cycle quotidien.
    import json
    with _read_store() as st:
        raw = st.get_kv("matrix_latest")
    if not raw:
        return jsonify({"generated_at": utc_now_iso(), "timeframes": [],
                        "note": "Matrice pas encore calculée — lancer daily_update.py."})
    data = json.loads(raw)
    data["served_at"] = utc_now_iso()
    return jsonify(data)


if __name__ == "__main__":
    # Développement local uniquement ; en production = WSGI PythonAnywhere.
    app.run(host="127.0.0.1", port=5000, debug=False)
