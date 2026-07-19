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

import math  # noqa: E402

from flask import (  # noqa: E402
    Flask, jsonify as _flask_jsonify, redirect, render_template, request,
    session, url_for,
)


def _clean(obj):
    """Remplace NaN/Infinity par null : Python les sérialise mais JSON.parse
    côté navigateur les REJETTE (la page resterait bloquée en chargement)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def jsonify(obj=None, **kwargs):
    """jsonify sûr pour le navigateur (NaN/Inf → null)."""
    payload = obj if obj is not None else kwargs
    return _flask_jsonify(_clean(payload))

from core.config import CONFIG  # noqa: E402
from core.data_source import last_open_time, load_ohlcv  # noqa: E402
from core.store import Store, utc_now_iso  # noqa: E402

# Seuil de fraîcheur du heartbeat (§6.3, §7.3).
STALE_AFTER_S = 120

app = Flask(__name__)
app.secret_key = CONFIG.flask_secret_key or "dev-insecure-change-me"


def _read_store() -> Store:
    """Connexion SQLite en LECTURE SEULE (§7.1). Tant que le worker n'a pas
    créé moteur.db (premier déploiement), renvoie une base vide en mémoire :
    l'app affiche « worker absent » au lieu d'une erreur 500."""
    try:
        return Store(read_only=True)
    except Exception:
        return Store(path=":memory:", read_only=False)


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
def _cache_1m_stats() -> tuple[int, int | None]:
    """(nb bougies, dernière open_time) via les MÉTADONNÉES parquet — sans
    charger le fichier entier à chaque poll (sobriété §2.5)."""
    from core.data_source import cache_path
    p = cache_path("1m")
    if not p.exists():
        return 0, None
    try:
        import pyarrow.parquet as pq
        f = pq.ParquetFile(p)
        n = int(f.metadata.num_rows)
        if n == 0:
            return 0, None
        last_rg = f.read_row_group(f.metadata.num_row_groups - 1,
                                   columns=["open_time"])
        return n, int(last_rg.column(0)[-1].as_py())
    except Exception:
        df = load_ohlcv("1m")  # repli : lecture complète
        return int(len(df)), (int(df["open_time"].iloc[-1]) if len(df) else None)


@app.route("/api/health")
@login_required
def api_health():
    with _read_store() as st:
        hb = st.get_heartbeat()
        age = st.heartbeat_age_seconds()
        unread = st.unread_count()
    bars_1m, last_1m = _cache_1m_stats()
    payload = {
        "generated_at": utc_now_iso(),
        "symbol": CONFIG.symbol,
        "worker": hb,
        "heartbeat_age_s": None if age is None else round(age, 1),
        "worker_stale": (age is None) or (age > STALE_AFTER_S),
        "stale_threshold_s": STALE_AFTER_S,
        "source": hb["source"] if hb else None,
        "bars_1m": bars_1m,
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
    # §7.1 : la web app DÉPOSE l'action dans la table dédiée web_actions ;
    # c'est le worker qui applique le marquage (un seul écrivain des events).
    with _read_store() as ro:
        row = ro.conn.execute("SELECT MAX(id) AS m FROM events").fetchone()
        up_to = int(row["m"] or 0)
    st = Store(read_only=False)
    try:
        st.add_web_action("mark_read", str(up_to))
    finally:
        st.close()
    return jsonify({"ok": True, "depose": True, "jusqu_a": up_to,
                    "note": "Marquage appliqué par le worker sous ~30 s."})


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
    # Vue 4 (§6.1) : positions papier, jauge de risque, tickets, verdicts,
    # surveillance. LECTURE seule — tout est produit par le worker.
    import json
    from core.risk_book import TOTAL_RISK_CAP, weighted_open_risk

    with _read_store() as st:
        hb_age = st.heartbeat_age_seconds()
        positions, tickets = [], []
        for r in st.conn.execute("SELECT id, payload FROM positions ORDER BY id DESC LIMIT 200"):
            p = json.loads(r["payload"])
            p["id"] = r["id"]
            positions.append(p)
        for r in st.conn.execute("SELECT id, payload FROM tickets ORDER BY id DESC LIMIT 100"):
            t = json.loads(r["payload"])
            t["id"] = r["id"]
            tickets.append(t)
        verdicts = json.loads(st.get_kv("verdicts_latest") or "{}")
        monitors = {}
        for stage in ("1h", "4h", "1D"):
            raw = st.get_kv(f"monitor_{stage}")
            monitors[stage] = json.loads(raw) if raw else None
        journal_n = st.conn.execute("SELECT COUNT(*) AS c FROM journal").fetchone()["c"]

    open_pos = [p for p in positions if p.get("status") == "open"]
    corr = verdicts.get("correlations", {}) if isinstance(verdicts, dict) else {}
    risk = weighted_open_risk(open_pos, corr or {})
    return jsonify({
        "generated_at": utc_now_iso(),
        "worker_stale": (hb_age is None) or (hb_age > STALE_AFTER_S),
        "risque_ouvert_pct": round(100 * risk, 3),
        "plafond_pct": 100 * TOTAL_RISK_CAP,
        "positions_ouvertes": open_pos,
        "positions_closes": [p for p in positions if p.get("status") == "closed"][:20],
        "tickets_actifs": [t for t in tickets if t.get("status") == "pending"],
        "tickets_bloques": [t for t in tickets
                            if t.get("status") in ("blocked", "expired")][:20],
        "verdicts": verdicts, "surveillance": monitors,
        "trades_journal": journal_n,
    })


@app.route("/api/detail/<tf>")
@login_required
def api_detail(tf):
    # Vue 2 (§6.1) : tous les états × directions d'une timeframe.
    import json
    with _read_store() as st:
        raw = st.get_kv("tables_latest")
    if not raw:
        return jsonify({"generated_at": utc_now_iso(),
                        "note": "Tables pas encore calculées — lancer daily_update.py."})
    data = json.loads(raw)
    table = data.get("timeframes", {}).get(tf)
    if table is None:
        return jsonify({"error": f"timeframe inconnue : {tf}"}), 404
    return jsonify({"generated_at": data.get("generated_at"),
                    "served_at": utc_now_iso(), "timeframe": tf, "table": table})


@app.route("/api/alarme/ack", methods=["POST"])
@login_required
def api_ack_alarm():
    # Acquittement d'alarme (§5.13, §7.1) : la web app DÉPOSE l'action dans
    # web_actions ; le worker la consomme et lève l'enquête lui-même.
    stage = (request.get_json(silent=True) or {}).get("stage", "")
    if stage not in ("1h", "4h", "1D"):
        return jsonify({"error": "étage invalide"}), 400
    st = Store(read_only=False)
    try:
        st.add_web_action("ack_alarm", stage)
    finally:
        st.close()
    return jsonify({"ok": True, "stage": stage,
                    "note": "Acquittement déposé — traité par le worker sous ~30 s."})


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
