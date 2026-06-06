# Déploiement sur PythonAnywhere (mémo)

Procédure pour faire tourner BTC Sniper 24h/24 sur PythonAnywhere.

## Les commandes à taper

Sur PythonAnywhere : onglet **Consoles** → **Bash**, puis :

```bash
git clone https://github.com/moide2004/btc-sniper.git
cd btc-sniper
bash setup.sh
```

Le script `setup.sh` fait le reste tout seul :
- installe les dépendances (`requests`, `python-dotenv`) ;
- te demande ton token Telegram et crée le fichier secret `.env` (privé,
  jamais envoyé sur GitHub) ;
- affiche la commande exacte pour la tâche 24h/24.

## Tester une fois

```bash
python3 btc_sniper.py
```

Si le message « BTC SNIPER demarre » arrive sur Telegram → c'est bon.
Arrêter le test avec `Ctrl+C`.

## Faire tourner 24h/24

Onglet **Tasks** → **Always-on tasks** → coller (remplacer `tonpseudo`
par ton nom d'utilisateur PythonAnywhere) :

```
python3 /home/tonpseudo/btc-sniper/btc_sniper.py
```

Puis cliquer sur **Create**. Le bot tourne en permanence, téléphone éteint.

## Rappel sécurité

Le token Telegram ne vit que dans le fichier `.env`, présent uniquement sur
PythonAnywhere. Il n'est jamais dans le code ni sur GitHub (le `.gitignore`
exclut `.env`).
