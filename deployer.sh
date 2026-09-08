#!/usr/bin/env bash
#
# Envoie le projet sur ton serveur et l'y fait tourner en permanence.
#
#     ./deployer.sh utilisateur@ip_du_serveur
#
# Ce que le script fait, dans l'ordre :
#   1. verifie que ta config locale est remplie
#   2. copie le dossier par SSH (rsync, donc seuls les fichiers changes partent)
#   3. installe les dependances sur le serveur
#   4. installe un service systemd qui redemarre le bot tout seul
#   5. affiche les logs pour que tu voies s'il demarre bien
#
# Relancer le script met a jour le code et redemarre le service. Le cache et le
# carnet de devoirs du serveur ne sont jamais ecrases.
#
set -euo pipefail

SERVEUR="${1:-}"
DOSSIER="${2:-mail_cyu}"
SERVICE="assistant-cyu"

if [ -z "$SERVEUR" ]; then
    cat <<'AIDE'
Usage : ./deployer.sh utilisateur@ip_du_serveur [dossier_distant]

Exemples :
    ./deployer.sh simon@192.168.1.42
    ./deployer.sh root@vps.exemple.fr assistant

Il te faut un acces SSH au serveur. Si tu tapes un mot de passe a chaque fois,
installe ta cle une bonne fois pour toutes :
    ssh-keygen -t ed25519          # si tu n'en as pas encore
    ssh-copy-id utilisateur@ip
AIDE
    exit 1
fi

ICI="$(cd "$(dirname "$0")" && pwd)"
cd "$ICI"

# ─── 1. La config locale est-elle prete ? ────────────────────────────────────
# On demande a config.py lui-meme : lui seul connait les valeurs par defaut de
# config.example.yaml, donc lui seul sait ce qui manque vraiment.
echo "== verification de config.yaml =="
if [ ! -f config.yaml ]; then
    echo "[X] config.yaml est introuvable."
    echo "    cp config.example.yaml config.yaml   puis remplis-le."
    exit 1
fi
python3 -c "
import sys, config
trous = config.manquants()
if trous:
    print('[X] a remplir dans config.yaml :')
    for t in trous:
        print('    - ' + t)
    sys.exit(1)
" || exit 1
python3 -m py_compile ./*.py
rm -rf __pycache__
echo "   config remplie, les modules compilent."

# ─── 2. Copie ────────────────────────────────────────────────────────────────
echo
echo "== copie vers $SERVEUR:~/$DOSSIER =="
# --exclude donnees : le cache et les devoirs du serveur restent intacts.
# config.yaml, lui, est bien copie : c'est la config du bot, secrets compris.
rsync -az --info=stats1 --delete \
      --exclude='__pycache__' \
      --exclude='donnees' \
      --exclude='.git' \
      --exclude='*.ics' \
      --exclude='*.png' \
      ./ "$SERVEUR:$DOSSIER/"

# ─── 3, 4, 5. Installation et service, sur le serveur ────────────────────────
echo
echo "== installation sur le serveur =="
ssh "$SERVEUR" DOSSIER="$DOSSIER" SERVICE="$SERVICE" 'bash -se' <<'DISTANT'
set -euo pipefail
cd "$HOME/$DOSSIER"

# Ubuntu 24 refuse pip hors environnement isole (PEP 668).
# --break-system-packages avec --user installe dans ~/.local, jamais dans les
# paquets systeme : le nom fait peur, rien n'est casse.
DEPS='requests discord.py>=2.3 PyYAML>=6.0 Pillow>=10.0'
pip3 install --user --break-system-packages --quiet $DEPS \
    || pip3 install --user --quiet $DEPS
python3 -c "
import requests, discord, yaml
print(f'   requests OK, discord.py {discord.__version__}, PyYAML {yaml.__version__}')
try:
    import PIL
    print(f'   Pillow {PIL.__version__} : /photo disponible')
except ImportError:
    print('   [!] Pillow absent : tout marche sauf /photo')
"

mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/$SERVICE.service" <<UNITE
[Unit]
Description=Assistant CYU (emploi du temps + devoirs sur Discord)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/$DOSSIER
ExecStart=/usr/bin/env python3 %h/$DOSSIER/bot.py
Restart=always
RestartSec=30
# Sans ca, les print() du script restent bloques dans un tampon et
# journalctl n'affiche rien avant plusieurs minutes.
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
UNITE

systemctl --user daemon-reload
systemctl --user enable "$SERVICE" >/dev/null 2>&1 || true
systemctl --user restart "$SERVICE"

# Pour que le service tourne meme quand tu n'es pas connecte en SSH.
loginctl enable-linger "$USER" 2>/dev/null || \
    echo "   [!] 'loginctl enable-linger' a echoue : le bot s'arretera a la
   deconnexion. Lance-le en root : sudo loginctl enable-linger $USER"

sleep 4
echo
systemctl --user --no-pager status "$SERVICE" | head -12
DISTANT

echo
echo "== termine =="
cat <<FIN
Le bot tourne sur $SERVEUR.

    Voir les logs en direct :
        ssh $SERVEUR journalctl --user -u $SERVICE -f

    Arreter / relancer :
        ssh $SERVEUR systemctl --user stop $SERVICE
        ssh $SERVEUR systemctl --user restart $SERVICE

    Mettre a jour apres une modification :
        ./deployer.sh $SERVEUR

ATTENTION : n'ai pas le bot qui tourne ici ET la-bas en meme temps, tu
recevrais chaque briefing en double. Arrete celui de ta machine.
FIN
