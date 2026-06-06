#!/usr/bin/env bash
# Script d'installation simplifie pour PythonAnywhere (ou tout serveur Linux).
# Usage :  bash setup.sh
#
# Il installe les dependances et cree le fichier secret .env en te
# demandant simplement ton token Telegram. Le .env reste prive (jamais
# envoye sur GitHub).

set -e
cd "$(dirname "$0")"

echo "=============================================="
echo "  Installation de BTC Sniper"
echo "=============================================="
echo

echo "[1/3] Installation des dependances..."
pip install --user -r requirements.txt
echo "      OK"
echo

if [ -f .env ]; then
    echo "[2/3] Un fichier .env existe deja - on le garde."
else
    echo "[2/3] Creation du fichier secret .env"
    echo "      (ton token reste prive, il n'ira jamais sur GitHub)"
    echo
    read -r -p "  Colle ton token Telegram (de @BotFather) : " TG_TOKEN
    read -r -p "  Ton chat id [par defaut 6130878748] : " TG_CHATID
    TG_CHATID="${TG_CHATID:-6130878748}"
    printf 'TELEGRAM_TOKEN=%s\nTELEGRAM_CHATID=%s\n' "$TG_TOKEN" "$TG_CHATID" > .env
    chmod 600 .env
    echo "      .env cree."
fi
echo

echo "[3/3] Termine !"
echo
echo "Pour tester maintenant :"
echo "    python3 $(pwd)/btc_sniper.py"
echo
echo "Pour le faire tourner 24h/24 sur PythonAnywhere :"
echo "  onglet Tasks -> Always-on tasks -> commande :"
echo "    python3 $(pwd)/btc_sniper.py"
echo
