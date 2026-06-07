"""Tableau de bord web du portefeuille (style CoinStats, perso).

Page protegee par mot de passe. A heberger sur PythonAnywhere (HTTPS fourni).

Lancement local :
    python3 dashboard.py
Sur PythonAnywhere : configurer une "Web app" pointant vers l'objet `app`.
"""

import hmac
import logging
import secrets
from functools import wraps

from flask import (Flask, redirect, render_template, request, session, url_for)

from portfolio import config
from portfolio.aggregate import get_portfolio
from portfolio.analysis import get_analysis
from portfolio.markets import get_markets

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")

app = Flask(__name__)
# Cle de signature des sessions : depuis .env, sinon aleatoire (sessions
# invalidees a chaque redemarrage si non definie -> definir FLASK_SECRET_KEY).
app.secret_key = config.FLASK_SECRET_KEY or secrets.token_hex(32)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("auth"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if not config.DASHBOARD_PASSWORD:
        return ("Configuration incomplete : definis DASHBOARD_PASSWORD dans .env",
                500)
    if request.method == "POST":
        given = request.form.get("password", "")
        # Comparaison a temps constant (evite les attaques par timing).
        if hmac.compare_digest(given, config.DASHBOARD_PASSWORD):
            session["auth"] = True
            return redirect(url_for("index"))
        error = "Mot de passe incorrect."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    force = request.args.get("refresh") == "1"
    data = get_portfolio(force=force)
    markets = get_markets(force=force)
    analysis = get_analysis(force=force)
    return render_template("dashboard.html", p=data, m=markets, a=analysis)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
