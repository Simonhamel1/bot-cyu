#!/usr/bin/env bash
#
# A LANCER SUR LE SERVEUR, dans le dossier du depot.
#
#     git clone https://github.com/<toi>/<depot>.git bot-cyu
#     cd bot-cyu
#     # deposer config.yaml ici (voir plus bas), puis :
#     ./installer.sh
#
# Et pour chaque mise a jour ensuite :
#
#     cd ~/bot-cyu && git pull && ./installer.sh
#
# Ce que le script fait, dans l'ordre :
#   1. verifie que config.yaml est present et rempli
#   2. verifie que les modules compilent
#   3. installe / met a jour les dependances
#   4. controle le fuseau horaire du serveur
#   5. installe le service systemd et (re)demarre le bot
#
# Il est idempotent : le relancer ne casse rien, ne cree pas de doublon, et ne
# touche jamais a config.yaml ni au dossier donnees/ (cache + carnet de devoirs).
#
# Options :
#   --avec-uptime    installe aussi la surveillance du webmail (uptime.py)
#   --sans-uptime    desinstalle cette surveillance
#   --sans-demarrer  tout preparer sans lancer le service
#
set -euo pipefail

SERVICE="assistant-cyu"
SERVICE_UPTIME="uptime-cyu"
UNITES="$HOME/.config/systemd/user"

AVEC_UPTIME=""       # "" = on garde l'etat actuel
DEMARRER=1

for arg in "$@"; do
    case "$arg" in
        --avec-uptime)   AVEC_UPTIME=1 ;;
        --sans-uptime)   AVEC_UPTIME=0 ;;
        --sans-demarrer) DEMARRER=0 ;;
        -h|--help)
            # tout l'en-tete en commentaire, jusqu'a la premiere ligne de code
            sed -n '2,$p' "$0" | sed -n '/^#/!q; s/^# \{0,1\}//; p'
            exit 0 ;;
        *)
            echo "[X] option inconnue : $arg   (--help pour la liste)"
            exit 1 ;;
    esac
done

ICI="$(cd "$(dirname "$0")" && pwd)"
cd "$ICI"

echo "== dossier : $ICI =="

# ─── 1. config.yaml ──────────────────────────────────────────────────────────
# Il n'est PAS dans le depot (.gitignore) : il contient le jeton du bot et le
# mot de passe CYU. Un git pull ne l'apportera donc jamais, c'est voulu.
echo
echo "== verification de config.yaml =="
if [ ! -f config.yaml ]; then
    cat <<AIDE
[X] config.yaml est introuvable.

    Il ne passe pas par git (il contient tes secrets). Depose-le depuis ta
    machine, une seule fois :

        scp config.yaml $(whoami)@$(hostname -I 2>/dev/null | awk '{print $1}'):$ICI/config.yaml

    Ou, a defaut, pars de l'exemple et remplis-le :

        cp config.example.yaml config.yaml && nano config.yaml
AIDE
    exit 1
fi

# On demande a config.py lui-meme : lui seul connait les valeurs par defaut de
# config.example.yaml, donc lui seul sait ce qui manque vraiment.
python3 -c "
import sys, config
trous = config.manquants()
if trous:
    print('[X] a remplir dans config.yaml :')
    for t in trous:
        print('    - ' + t)
    sys.exit(1)
print('   config remplie.')
"

# config.yaml contient des mots de passe : personne d'autre n'a a le lire.
chmod 600 config.yaml

# ─── 2. Les modules compilent ────────────────────────────────────────────────
python3 -m py_compile ./*.py
rm -rf __pycache__
echo "   les modules compilent."

# ─── 3. Dependances ──────────────────────────────────────────────────────────
echo
echo "== dependances =="
# Ubuntu 24 / Debian 12 refusent pip hors environnement isole (PEP 668).
# --break-system-packages avec --user installe dans ~/.local, jamais dans les
# paquets systeme : le nom fait peur, rien n'est casse.
pip3 install --user --break-system-packages --quiet -r requirements.txt \
    || pip3 install --user --quiet -r requirements.txt

python3 -c "
import requests, discord, yaml
print(f'   requests OK, discord.py {discord.__version__}, PyYAML {yaml.__version__}')
try:
    import PIL
    print(f'   Pillow {PIL.__version__} : les reponses en photo sont disponibles')
except ImportError:
    print('   [!] Pillow absent : tout marche, mais tout sortira en texte')
"

# Sans police TTF systeme, Pillow retombe sur sa bitmap de secours : les
# images sortent alors laides et sans accents. Un paquet de 3 Mo suffit.
# ls renvoie une erreur des qu'UN chemin manque : on compte les lignes plutot
# que de tester son code de sortie, sinon on avertirait a tort.
if ! ls /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf \
        /usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf \
        /usr/share/fonts/TTF/DejaVuSans.ttf 2>/dev/null | grep -q .; then
    echo "   [!] aucune police TTF systeme : les images sortiront sans accents."
    echo "       sudo apt install -y fonts-dejavu-core"
fi

# ─── 4. Fuseau horaire ───────────────────────────────────────────────────────
# Le code lit l'heure locale du serveur, sans conversion. Un serveur en UTC
# enverrait le briefing de 07:00 a 09:00 heure de Paris.
echo
echo "== fuseau horaire =="
FUSEAU="$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo inconnu)"
if [ "$FUSEAU" = "Europe/Paris" ]; then
    echo "   $FUSEAU — les horaires de config.yaml sont bien de l'heure de Paris."
else
    cat <<AVERTISSEMENT
   [!] fuseau du serveur : $FUSEAU

   Les horaires de config.yaml (briefing 07:00, silence 23:00...) sont lus en
   heure LOCALE du serveur. Tant qu'il n'est pas sur Europe/Paris, tes
   briefings partiront decales. A corriger une fois :

       sudo timedatectl set-timezone Europe/Paris
       systemctl --user restart $SERVICE
AVERTISSEMENT
fi

# ─── 5. Service systemd ──────────────────────────────────────────────────────
echo
echo "== service systemd =="
mkdir -p "$UNITES"

# Les fichiers .service du depot pointent vers %h/bot-cyu. On y substitue le
# vrai chemin, quel que soit le nom du dossier clone.
installer_unite() {
    local source="$1" nom="$2"
    sed -e "s|%h/bot-cyu|$ICI|g" "$source" > "$UNITES/$nom.service"
    echo "   $UNITES/$nom.service"
}

installer_unite assistant-cyu.service "$SERVICE"

# uptime : par defaut on garde l'etat actuel du serveur.
if [ -z "$AVEC_UPTIME" ]; then
    if [ -f "$UNITES/$SERVICE_UPTIME.service" ]; then AVEC_UPTIME=1; else AVEC_UPTIME=0; fi
fi
if [ "$AVEC_UPTIME" = "1" ]; then
    installer_unite uptime-cyu.service "$SERVICE_UPTIME"
else
    if [ -f "$UNITES/$SERVICE_UPTIME.service" ]; then
        systemctl --user disable --now "$SERVICE_UPTIME" >/dev/null 2>&1 || true
        rm -f "$UNITES/$SERVICE_UPTIME.service"
        echo "   surveillance webmail retiree."
    else
        echo "   surveillance webmail non installee (--avec-uptime pour l'ajouter)."
    fi
fi

systemctl --user daemon-reload

# Pour que le service tourne meme quand tu n'es pas connecte en SSH.
loginctl enable-linger "$USER" 2>/dev/null || \
    echo "   [!] 'loginctl enable-linger' a echoue : le bot s'arretera a la
       deconnexion SSH. Lance-le en root : sudo loginctl enable-linger $USER"

if [ "$DEMARRER" = "0" ]; then
    echo
    echo "== termine (service installe, pas demarre : --sans-demarrer) =="
    echo "    systemctl --user start $SERVICE"
    exit 0
fi

systemctl --user enable "$SERVICE" >/dev/null 2>&1 || true
systemctl --user restart "$SERVICE"
if [ "$AVEC_UPTIME" = "1" ]; then
    systemctl --user enable "$SERVICE_UPTIME" >/dev/null 2>&1 || true
    systemctl --user restart "$SERVICE_UPTIME"
fi

sleep 4
echo
systemctl --user --no-pager status "$SERVICE" | head -12

# Un service qui redemarre en boucle a cause d'un jeton invalide passerait
# inapercu sans ca.
if ! systemctl --user is-active --quiet "$SERVICE"; then
    echo
    echo "[X] le service n'est pas actif. Les 30 dernieres lignes de log :"
    journalctl --user -u "$SERVICE" -n 30 --no-pager
    exit 1
fi

echo
cat <<FIN
== termine ==
Le bot tourne. Dans #commandes : /panneau pour epingler les boutons.

    Logs en direct :
        journalctl --user -u $SERVICE -f

    Arreter / relancer :
        systemctl --user stop $SERVICE
        systemctl --user restart $SERVICE

    Mettre a jour :
        cd $ICI && git pull && ./installer.sh

ATTENTION : ne fais pas tourner le bot ici ET sur ta machine en meme temps, tu
recevrais chaque briefing en double.
FIN
