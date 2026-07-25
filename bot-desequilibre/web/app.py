"""Mini-app Flask — LECTURE SEULE de moteur.db (§6). Dual-actif.

P1 : auth obligatoire (hash en env), endpoints JSON avec generated_at, bandeau
d'âge des données + ⚠ si heartbeat worker > 120 s. Les 4 vues complètes et la
cloche riche arrivent en P3 ; l'ossature d'endpoints est en place.
NaN/Infinity → null (sinon JSON.parse du navigateur rejette la réponse).
"""
from __future__ import annotations

import hashlib
import math
import sys
from functools import wraps
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import (  # noqa: E402
    Flask, jsonify as _flask_jsonify, redirect, render_template, request,
    session, url_for,
)

from core.config import CONFIG  # noqa: E402
from core.data_source import cache_stats  # noqa: E402
from core.store import Store, utc_now_iso  # noqa: E402

STALE_AFTER_S = 120
app = Flask(__name__)
app.secret_key = CONFIG.flask_secret_key or "dev-insecure-change-me"


def _clean(o):
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def jsonify(obj=None, **kw):
    return _flask_jsonify(_clean(obj if obj is not None else kw))


def _read_store() -> Store:
    try:
        return Store(read_only=True)
    except Exception:
        return Store(path=":memory:", read_only=False)


def _check_password(pw: str) -> bool:
    stored = CONFIG.web_password_hash
    if not stored:
        return False
    if stored.startswith(("pbkdf2:", "scrypt:", "argon2:")):
        try:
            from werkzeug.security import check_password_hash
            return check_password_hash(stored, pw)
        except Exception:
            return False
    return hashlib.sha256(pw.encode("utf-8")).hexdigest() == stored


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **k):
        if not session.get("auth"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "auth_required"}), 401
            return redirect(url_for("login", next=request.path))
        return fn(*a, **k)
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


@app.route("/")
@login_required
def index():
    return render_template("index.html", symbols=", ".join(CONFIG.symbols))


@app.route("/api/health")
@login_required
def api_health():
    with _read_store() as st:
        hb = st.get_heartbeat()
        age = st.heartbeat_age_seconds()
        unread = st.unread_count()
    par_actif = {}
    for sym in CONFIG.symbols:
        n, last = cache_stats(sym, "1m")
        par_actif[sym] = {"bars_1m": n, "last_1m_open_ms": last}
    return jsonify({
        "generated_at": utc_now_iso(), "symbols": CONFIG.symbols,
        "worker": hb, "heartbeat_age_s": None if age is None else round(age, 1),
        "worker_stale": (age is None) or (age > STALE_AFTER_S),
        "stale_threshold_s": STALE_AFTER_S,
        "source": hb["source"] if hb else None,
        "par_actif": par_actif, "unread_events": unread,
    })


@app.route("/api/evenements")
@login_required
def api_evenements():
    since = int(request.args.get("since_id", 0))
    with _read_store() as st:
        events = st.list_events(limit=100, since_id=since)
        unread = st.unread_count()
    return jsonify({"generated_at": utc_now_iso(), "unread": unread, "events": events})


@app.route("/api/livre")
@login_required
def api_livre():
    import json as _json
    with _read_store() as st:
        age = st.heartbeat_age_seconds()
        open_pos = st.list_open_positions()
        open_risk = st.open_risk_usd()
        bt_raw = st.get_kv("backtest")
        journal = st.list_journal(limit=60)
    positions = [{"id": p["id"], "opened_utc": p["opened_utc"], "symbol": p["symbol"],
                  "timeframe": p["timeframe"], **{k: p["payload"].get(k) for k in
                  ("direction", "entry", "sl", "tp", "risk_usd", "be_done", "rr")}}
                 for p in open_pos]
    backtest = _json.loads(bt_raw) if bt_raw else None
    cap = CONFIG.capital_usd or 1.0
    return jsonify({
        "generated_at": utc_now_iso(),
        "worker_stale": (age is None) or (age > STALE_AFTER_S),
        "capital_usd": CONFIG.capital_usd,
        "risque_ouvert_pct": 100.0 * open_risk / cap, "plafond_pct": CONFIG.risk_cap_pct,
        "params": {"capital_usd": CONFIG.capital_usd,
                   "risk_pct": CONFIG.risk_pct,
                   "short_risk_factor": CONFIG.short_risk_factor},
        "positions": positions, "backtest": backtest, "journal": journal,
    })


# ----- Journal PERSONNEL (seule écriture web : table dédiée journal_perso) ---
def _write_store() -> Store:
    return Store(read_only=False)


def _f_or_none(v):
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


@app.route("/api/journal-perso", methods=["GET", "POST"])
@login_required
def api_journal_perso():
    if request.method == "GET":
        with _read_store() as st:
            rows = st.list_journal_perso()
        return jsonify({"generated_at": utc_now_iso(), "trades": rows})
    d = request.get_json(silent=True) or {}
    symbol = str(d.get("symbol") or "").strip().upper()[:20]
    if not symbol:
        return jsonify({"error": "actif requis"}), 400
    direction = str(d.get("direction") or "").strip().lower()
    if direction not in ("long", "short", ""):
        return jsonify({"error": "sens invalide"}), 400
    entry = _f_or_none(d.get("entry"))
    sl = _f_or_none(d.get("sl"))
    tp = _f_or_none(d.get("tp"))
    ratio = _f_or_none(d.get("ratio"))
    # TP AUTO : si pas de TP mais un ratio (ex. 3 → 1:3), TP = entrée ± ratio×stop.
    if tp is None and ratio and ratio > 0 and entry is not None and sl is not None \
            and direction in ("long", "short"):
        stop = abs(entry - sl)
        if stop > 0:
            tp = entry + ratio * stop if direction == "long" else entry - ratio * stop
    with _write_store() as st:
        jid = st.add_journal_perso(
            symbol=symbol, timeframe=str(d.get("timeframe") or "").strip()[:8] or None,
            direction=direction or None, entry=entry, sl=sl, tp=tp,
            size_units=_f_or_none(d.get("size_units")),
            note=str(d.get("note") or "").strip()[:500] or None,
            capital_usd=_f_or_none(d.get("capital_usd")),
            risk_pct=_f_or_none(d.get("risk_pct")))
    return jsonify({"ok": True, "id": jid, "tp": tp})


@app.route("/api/journal-perso/<int:jid>/cloture", methods=["POST"])
@login_required
def api_journal_perso_close(jid: int):
    d = request.get_json(silent=True) or {}
    exit_price = _f_or_none(d.get("exit_price"))
    if exit_price is None:
        return jsonify({"error": "prix de sortie requis"}), 400
    with _write_store() as st:
        res = st.close_journal_perso(jid, exit_price)
    if res is None:
        return jsonify({"error": "ligne introuvable ou déjà clôturée"}), 404
    return jsonify({"ok": True, **res})


@app.route("/api/journal-perso/<int:jid>/supprimer", methods=["POST"])
@login_required
def api_journal_perso_delete(jid: int):
    with _write_store() as st:
        ok = st.delete_journal_perso(jid)
    return jsonify({"ok": ok}) if ok else (jsonify({"error": "introuvable"}), 404)


@app.route("/api/probas")
@login_required
def api_probas():
    """Matrice §3 : dernier bloc de chaque (actif, TF, direction), résumé au
    rrMult vivant + grille complète des rr en payload."""
    rr_key = f"{CONFIG.rr_mult:.2f}"
    with _read_store() as st:
        rows = st.all_latest_probas()
        corr = st.get_kv("corr_btc_eth")
    cases = []
    for r in rows:
        pl = r["payload"]
        blk = (pl.get("rr") or {}).get(rr_key, {})
        cases.append({
            "symbol": r["symbol"], "timeframe": r["timeframe"],
            "direction": r["direction"], "ts_utc": r["ts_utc"],
            "n_setups": pl.get("n_setups", 0),
            "n": blk.get("n"), "k": blk.get("k"), "p_hat": blk.get("p_hat"),
            "p_prudent": blk.get("p_prudent"), "wilson": blk.get("wilson"),
            "ev_prudent_taker": blk.get("ev_prudent_taker"),
            "ev_point_taker": blk.get("ev_point_taker"),
            "ev_prudent_maker": blk.get("ev_prudent_maker"),
            "k_max": blk.get("k_max"), "cvar99_r": blk.get("cvar99_r"),
            "wf": (pl.get("walk_forward") or {}).get(rr_key, {}),
            "rr_grid": pl.get("rr", {}),
        })
    return jsonify({"generated_at": utc_now_iso(), "rr_live": CONFIG.rr_mult,
                    "corr_btc_eth": float(corr) if corr else None, "cases": cases,
                    "note": "Chiffres MESURÉS (double barrière), pas des prédictions."})


@app.route("/api/tickets")
@login_required
def api_tickets():
    with _read_store() as st:
        tickets = st.list_tickets(limit=100)
    return jsonify({"generated_at": utc_now_iso(), "tickets": tickets,
                    "note": "Aucun ordre réel. Analyste : il signale, il n'exécute jamais."})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
