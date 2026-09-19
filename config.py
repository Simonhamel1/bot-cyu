#!/usr/bin/env python3
"""
Le chargeur de configuration. Il lit `config.yaml` et rien d'autre.

Tu n'as plus a ouvrir de fichier Python pour regler quoi que ce soit : tout se
passe dans config.yaml, a cote. Ce module se contente de le lire, de completer
ce que tu n'as pas rempli avec les valeurs de config.example.yaml, et d'exposer
le tout sous forme de constantes que le reste du projet utilise.

Pourquoi deux fichiers YAML :

    config.example.yaml   documente TOUTES les options, part sur GitHub, et
                          sert de valeurs par defaut ;
    config.yaml           ne contient que ce que tu veux changer, plus les
                          secrets, et reste sur ta machine (.gitignore).

Consequence pratique : quand une nouvelle option apparait dans une version du
projet, ton config.yaml n'a pas besoin d'etre mis a jour, la valeur par defaut
est prise dans l'exemple.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parent
FICHIER_CONFIG = RACINE / "config.yaml"
FICHIER_EXEMPLE = RACINE / "config.example.yaml"


class ConfigInvalide(RuntimeError):
    """config.yaml manquant, mal ecrit, ou incoherent."""


try:
    import yaml
except ImportError:                                          # pragma: no cover
    print("[X] il manque PyYAML.  pip install pyyaml", file=sys.stderr)
    raise


# --- Lecture et fusion -------------------------------------------------------
def _lire_yaml(chemin):
    if not chemin.exists():
        return {}
    try:
        data = yaml.safe_load(chemin.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigInvalide(
            f"{chemin.name} est mal ecrit : {e}\n"
            f"En YAML l'indentation compte, et elle se fait avec des espaces, "
            f"jamais des tabulations.")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigInvalide(f"{chemin.name} doit commencer par des sections "
                             f"(discord:, cyu:, ...), pas par une liste.")
    return data


def _fusionner(defauts, perso):
    """Les valeurs de `perso` l'emportent, section par section.

    Une cle absente de config.yaml garde la valeur de l'exemple ; une cle
    presente mais vide (null) aussi, SAUF pour les listes et les dicts, ou tu
    as le droit de vouloir explicitement du vide : c'est exactement le cas de
    `avant_cours_minutes: []`.
    """
    sortie = dict(defauts)
    for cle, valeur in (perso or {}).items():
        if isinstance(valeur, dict) and isinstance(sortie.get(cle), dict):
            sortie[cle] = _fusionner(sortie[cle], valeur)
        elif valeur is None and cle in sortie and not isinstance(sortie[cle], (list, dict)):
            continue                       # « cle: » toute seule = laisse par defaut
        else:
            sortie[cle] = valeur
    return sortie


def charger_brut():
    exemple = _lire_yaml(FICHIER_EXEMPLE)
    if not FICHIER_CONFIG.exists():
        raise ConfigInvalide(
            "config.yaml est introuvable.\n"
            "    cp config.example.yaml config.yaml\n"
            "puis remplis bot_token, user et password.")
    return _fusionner(exemple, _lire_yaml(FICHIER_CONFIG))


BRUT = charger_brut()


def _section(nom):
    valeur = BRUT.get(nom)
    return valeur if isinstance(valeur, dict) else {}


_discord = _section("discord")
_cyu = _section("cyu")
_moi = _section("moi")
_notif = _section("notifications")
_aff = _section("affichage")
_meteo = _section("meteo")
_classe = _section("classe")


# --- Le fuseau horaire, avant tout le reste ----------------------------------
# Toutes les heures du projet (« briefing_matin: 08:00 », « demain 9h », les
# horodatages des images) sont lues et ecrites avec datetime.now(), c'est-a-dire
# a l'heure DU SERVEUR. Or un serveur loue est presque toujours en UTC : « 08:00 »
# y part alors a 10 h a Paris l'ete, 9 h l'hiver, et personne ne comprend
# pourquoi le briefing du matin arrive en retard.
#
# Plutot que de reprendre deux cents appels a datetime.now(), on regle le fuseau
# DU PROCESSUS au demarrage : tout le projet parle des lors l'heure de Paris,
# ou celle que tu mets ici, sans que rien d'autre ne change. Le passage a
# l'heure d'ete se fait tout seul, c'est la zone qui le sait.
FUSEAU = str(BRUT.get("fuseau") or "Europe/Paris").strip()


def _appliquer_fuseau():
    """Rend le fuseau effectif pour tout le processus. Sans effet sur Windows,
    ou tzset n'existe pas : le bot tourne sous systemd, sur Linux."""
    if not FUSEAU:
        return ""
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(FUSEAU)                   # leve si le nom n'existe pas
    except Exception as e:                 # noqa: BLE001 - un nom de zone faux
        print(f"[!] fuseau « {FUSEAU} » inconnu ({e}) : le bot garde celui du "
              f"serveur. Un nom valide ressemble a « Europe/Paris ».",
              file=sys.stderr)
        return ""
    import os
    import time as _time
    os.environ["TZ"] = FUSEAU
    if hasattr(_time, "tzset"):
        _time.tzset()
    return FUSEAU


FUSEAU_APPLIQUE = _appliquer_fuseau()


def controle_heure():
    """Le bot est-il a l'heure de `fuseau` ? Rend (ok, lignes d'explication).

    A verifier sur un serveur, pas sur sa machine : une machine de bureau est
    a l'heure du pays, un serveur loue est en UTC, et rien ne le signale. Le
    symptome est silencieux et deroutant — le briefing de 8 h arrive a 10 h,
    et tout le reste avec.

    On compare ce que voit le bot (datetime.now(), c'est-a-dire l'heure du
    PROCESSUS) avec l'heure qu'il est vraiment dans le fuseau voulu. Un ecart
    de plus d'une minute ne peut venir que du fuseau.
    """
    from datetime import datetime, timezone
    import time as _time

    zone = FUSEAU or "Europe/Paris"
    lignes = []
    try:
        from zoneinfo import ZoneInfo
        vraie = datetime.now(ZoneInfo(zone))
    except Exception as e:                     # noqa: BLE001 - zone inconnue
        return False, [f"Fuseau « {zone} » illisible : {e}",
                       "Sur Debian/Ubuntu : sudo apt install tzdata"]

    vue_bot = datetime.now()
    ecart = round((vue_bot - vraie.replace(tzinfo=None)).total_seconds() / 60)
    ok = abs(ecart) < 1

    large = max(22, len(zone) + 9)
    for libelle, valeur in (
            ("Heure du bot", f"{vue_bot:%d/%m/%Y %H:%M:%S}"),
            (f"Heure a {zone}", f"{vraie:%d/%m/%Y %H:%M:%S}"),
            ("Heure UTC", f"{datetime.now(timezone.utc):%d/%m/%Y %H:%M:%S}"),
            ("Fuseau du systeme", "/".join(dict.fromkeys(_time.tzname))),
            ("fuseau: du config.yaml", (FUSEAU or "(vide — heure du serveur)")
             + ("" if FUSEAU_APPLIQUE else "   [NON APPLIQUE]"))):
        lignes.append(f"{libelle:<{large}} {valeur}")

    if ok:
        lignes.append("")
        lignes.append(f"OK — le bot est bien a l'heure de {zone}. Les horaires de "
                      f"config.yaml (briefings, silence, rappels) sont donc "
                      f"ceux que tu lis.")
        return True, lignes

    sens = "en avance" if ecart > 0 else "en retard"
    lignes.append("")
    lignes.append(f"PROBLEME — le bot est {abs(ecart)} min {sens} sur {zone}.")
    # Une heure reglee dans config.yaml est lue a l'heure DU BOT : pour savoir
    # quand elle tombe vraiment, on retire l'ecart. Le dire en clair vaut mieux
    # qu'un nombre de minutes, qu'il faut sinon appliquer de tete et a l'envers.
    from datetime import timedelta
    exemple = vue_bot.replace(hour=8, minute=0, second=0, microsecond=0) \
        - timedelta(minutes=ecart)
    lignes.append(f"Un briefing regle sur 08:00 partirait a {exemple:%H:%M} "
                  f"heure de {zone}.")
    if not FUSEAU:
        lignes.append("Cause : `fuseau:` est vide dans config.yaml, donc le bot "
                      "suit l'heure du serveur.")
        lignes.append('Corrige : mets  fuseau: "Europe/Paris"  dans config.yaml, '
                      'puis redemarre le bot.')
    elif not FUSEAU_APPLIQUE:
        lignes.append("Cause : le fuseau n'a pas pu etre applique (voir le message "
                      "au demarrage).")
    else:
        lignes.append("Le fuseau est pourtant applique : l'horloge du serveur "
                      "elle-meme est fausse.")
        lignes.append("Corrige :  sudo timedatectl set-ntp true")
    return False, lignes


def _txt(source, cle, defaut=""):
    valeur = source.get(cle, defaut)
    return "" if valeur is None else str(valeur).strip()


def _entier(source, cle, defaut):
    try:
        return int(source.get(cle, defaut))
    except (TypeError, ValueError):
        return defaut


def _bool(source, cle, defaut):
    valeur = source.get(cle, defaut)
    return defaut if valeur is None else bool(valeur)


def _liste_entiers(source, cle):
    valeur = source.get(cle) or []
    if isinstance(valeur, (int, float)):
        valeur = [valeur]
    sortie = []
    for v in valeur:
        try:
            sortie.append(int(v))
        except (TypeError, ValueError):
            pass
    return sorted(set(sortie), reverse=True)


def _flottant(source, cle, defaut):
    try:
        return float(str(source.get(cle, defaut)).replace(",", "."))
    except (TypeError, ValueError):
        return defaut


def _optionnel_entier(source, cle):
    """None reste None : c'est la difference entre « desactive » et « zero »."""
    valeur = source.get(cle)
    if valeur is None or str(valeur).strip().lower() in ("", "null", "non", "false"):
        return None
    try:
        return int(valeur)
    except (TypeError, ValueError):
        return None


# --- Discord -----------------------------------------------------------------
BOT_TOKEN = _txt(_discord, "bot_token")
CATEGORIE_ID = _txt(_discord, "categorie_id")
SERVEUR_ID = _txt(_discord, "serveur_id")
MENTION = _txt(_discord, "mention")
MENTIONS_ACTIVES = _bool(_discord, "mentions_actives", True)
WEBHOOK_SECOURS = _txt(_discord, "webhook_secours")

# L'ordre compte : c'est celui de la creation des salons et de l'affichage
# dans /statut.
CANAUX = ("annonces", "edt", "devoirs", "alertes", "statut", "commandes", "logs",
          "predictions")

# Les salons qui n'ont de sens QUE par le bot (des cartes a boutons, qu'un
# webhook ne sait pas porter). Vides, ils ne retombent pas sur le secours :
# la fonction se passe simplement de salon dedie.
CANAUX_BOT_SEULEMENT = ("predictions",)

_salons_bruts = _discord.get("salons") or {}
SALONS = {canal: _txt(_salons_bruts, canal) for canal in CANAUX}


# Le nom que le bot porte SUR LE SERVEUR. Le nom global (celui du portail
# developpeur, « funding bot » tant qu'on ne l'a pas change) ne se change pas
# depuis le code sans limite : Discord n'en accepte que deux par heure. Le
# surnom de serveur, lui, est libre, immediat, et c'est celui que tout le monde
# voit dans les messages et les mentions.
NOM_BOT = _txt(_discord, "nom", "")


def salon_bot(canal):
    """L'identifiant (int) du salon `canal` quand c'est bien un salon poste par
    le bot, sinon None : un webhook ne peut pas porter de boutons."""
    valeur = str(SALONS.get(canal) or "").strip()
    return int(valeur) if valeur.isdigit() else None


def salon_configure_bot(canal):
    return salon_bot(canal) is not None


# Un salon inconnu dans config.yaml est presque toujours une faute de frappe :
# le signaler tout de suite evite de chercher pendant une heure pourquoi rien
# n'arrive dans le salon en question.
for _inconnu in sorted(set(_salons_bruts) - set(CANAUX)):
    print(f"[!] config.yaml : salon « {_inconnu} » inconnu, ignore. "
          f"Attendus : {', '.join(CANAUX)}", file=sys.stderr)


# --- CYU ---------------------------------------------------------------------
CYU_USER = _txt(_cyu, "user")
# Pas de .strip() sur le mot de passe : il a le droit de finir par une espace.
CYU_PASS = str(_cyu.get("password") or "")
CELCAT_BASE = _txt(_cyu, "celcat_base", "https://celcat-calendar.cyu.fr").rstrip("/")
FEDERATION_ID = _txt(_cyu, "federation_id")
RES_TYPE = _txt(_cyu, "res_type", "104")
HORIZON_JOURS = max(1, _entier(_cyu, "horizon_jours", 28))

WEBMAIL_URL = _txt(_cyu, "webmail_url", "https://mail.etu.cyu.fr/mail")
WEBMAIL_INTERVAL = max(10, _entier(_cyu, "webmail_intervalle", 60))
WEBMAIL_HEARTBEAT = max(1, _entier(_cyu, "webmail_heartbeat", 60))


# --- Toi ---------------------------------------------------------------------
TRAJET_MINUTES = _entier(_moi, "trajet_minutes", 45)
PREPARATION_MINUTES = _entier(_moi, "preparation_minutes", 30)
JOURNEE_DEBUT = _txt(_moi, "journee_debut", "08:00")
JOURNEE_FIN = _txt(_moi, "journee_fin", "19:00")


# --- Notifications -----------------------------------------------------------
BRIEFING_MATIN = _txt(_notif, "briefing_matin", "07:00")
# Les jours ou le briefing du matin part : 0 = lundi ... 6 = dimanche.
# Liste vide (ou absente) = tous les jours, comme avant.
BRIEFING_MATIN_JOURS = sorted({j % 7 for j in _liste_entiers(_notif, "briefing_matin_jours")})
BRIEFING_SOIR = _txt(_notif, "briefing_soir", "20:00")
BRIEFING_SOIR_JOURS = sorted({j % 7 for j in _liste_entiers(_notif, "briefing_soir_jours")})
# Ne rien envoyer les jours sans cours : un briefing « demain, rien » est du
# bruit, et c'est ce bruit qui fait couper les notifications du bot.
BRIEFING_SI_COURS = _bool(_notif, "briefing_si_cours", False)
# Les jours ou un changement d'emploi du temps a le droit de MENTIONNER. Les
# autres jours il est quand meme publie, dans #edt, sans notification : on ne
# perd pas l'information, on perd la sonnerie.
JOURS_ALERTES = sorted({j % 7 for j in _liste_entiers(_notif, "jours_alertes")})
RECAP_SEMAINE_JOUR = _entier(_notif, "recap_semaine_jour", 6) % 7
RECAP_SEMAINE_HEURE = _txt(_notif, "recap_semaine_heure", "18:00")

AVANT_COURS_MINUTES = _liste_entiers(_notif, "avant_cours_minutes")
PREMIER_COURS_MINUTES = _optionnel_entier(_notif, "premier_cours_minutes")
SEULEMENT_PREMIER_COURS_DU_BLOC = _bool(_notif, "seulement_premier_du_bloc", True)

RELANCE_DEVOIRS = _bool(_notif, "relance_devoirs", True)
RELANCE_DEVOIRS_APRES_MINUTES = _entier(_notif, "relance_devoirs_apres_minutes", 20)
# Une heure fixe ("18:00") prend le pas sur « apres le dernier cours » ; vide,
# on garde le calage sur la fin des cours.
RELANCE_DEVOIRS_HEURE = _txt(_notif, "relance_devoirs_heure")
DEVOIRS_JOURS_AVANT = _liste_entiers(_notif, "devoirs_jours_avant") or [7, 3, 1, 0]

VERIF_EDT_MINUTES = max(5, _entier(_notif, "verif_edt_minutes", 15))
# Sous 5 minutes, Discord finit par limiter les modifications de message.
RAFRAICHIR_TABLEAUX_MINUTES = max(5, _entier(_notif, "rafraichir_tableaux_minutes", 10))

SILENCE_DE = _txt(_notif, "silence_de")
SILENCE_A = _txt(_notif, "silence_a")

# Etre mentionne des qu'un cours bouge, et pas seulement quand ca touche
# aujourd'hui ou demain : un cours annule la semaine prochaine merite aussi
# qu'on le sache tout de suite.
PING_CHANGEMENTS = _bool(_notif, "ping_changements", True)


# --- Affichage ---------------------------------------------------------------
TROU_MINUTES = _entier(_aff, "trou_minutes", 45)
CRENEAU_LIBRE_MINUTES = _entier(_aff, "creneau_libre_minutes", 60)
GRILLE_SEMAINE = _bool(_aff, "grille_semaine", True)
GRILLE_JOURS = min(7, max(1, _entier(_aff, "grille_jours", 5)))
# Discord colorise les blocs ```ansi. Si un client affiche des codes bizarres
# du genre [0;34m au lieu de couleurs, mets grille_couleurs: false.
GRILLE_COULEURS = _bool(_aff, "grille_couleurs", True)

# Les images. Sans Pillow, tout retombe automatiquement sur le texte : ces
# reglages servent a s'en passer volontairement, pas a reparer une panne.
IMAGES = _bool(_aff, "images", True)
# Le salon #edt contient l'image de la semaine, reecrite sur place.
TABLEAU_EDT_IMAGE = _bool(_aff, "tableau_edt_image", True)
# Les briefings du matin et du soir sont accompagnes de la photo de la journee.
BRIEFING_IMAGE = _bool(_aff, "briefing_image", True)


# --- Meteo -------------------------------------------------------------------
# Open-Meteo ne demande ni compte ni cle : il n'y a donc rien a remplir pour
# que ca marche, seulement des coordonnees a changer si tu n'es pas a Cergy.
METEO_ACTIVE = _bool(_meteo, "active", True)
METEO_LAT = _flottant(_meteo, "latitude", 49.0362)
METEO_LON = _flottant(_meteo, "longitude", 2.0631)
METEO_LIEU = _txt(_meteo, "lieu", "Cergy")
# En dessous de ce cumul horaire (mm), il pleut sans que ca se remarque.
METEO_SEUIL_PLUIE = _flottant(_meteo, "seuil_pluie", 0.2)
# Minutes ajoutees au trajet quand il pleut a l'heure du depart. 0 = jamais.
METEO_MARGE_PLUIE_MINUTES = _entier(_meteo, "marge_pluie_minutes", 10)
METEO_CACHE_MINUTES = max(10, _entier(_meteo, "cache_minutes", 30))
# La meteo dans le briefing du matin, et dans l'image de la journee.
METEO_BRIEFING = _bool(_meteo, "dans_le_briefing", True)


# --- La promo ----------------------------------------------------------------
# Les anniversaires : /anniversaire, et un message dans #annonces le jour J.
ANNIVERSAIRES = _bool(_classe, "anniversaires", True)
ANNIVERSAIRES_HEURE = _txt(_classe, "anniversaires_heure", "08:00")
# Les examens en evenements Discord (bandeau du serveur, cloche « interesse »).
EVENEMENTS_EXAMENS = _bool(_classe, "evenements_examens", True)
EVENEMENTS_HORIZON_JOURS = max(1, _entier(_classe, "evenements_horizon_jours", 60))
# Discord borne un sondage entre 1 heure et 32 jours.
SONDAGE_DUREE_HEURES = min(768, max(1, _entier(_classe, "sondage_duree_heures", 24)))
SONDAGE_RESULTATS = _bool(_classe, "sondage_resultats", True)
# Le dimanche dans #predictions : ce qu'il reste a trancher, et le classement.
RECAP_PREDICTIONS = _bool(_classe, "recap_predictions", True)
# Le classeur M3C de la promo, pour /ects. Vide = le premier « M3C*.xlsx »
# trouve a cote du bot : deposer le fichier suffit, sans rien regler ici.
MAQUETTE_FICHIER = _txt(_classe, "maquette", "")


# --- Matieres ----------------------------------------------------------------
MATIERES = BRUT.get("matieres") if isinstance(BRUT.get("matieres"), dict) else {}


# --- Fichiers (rien a regler) ------------------------------------------------
DONNEES = RACINE / "donnees"

FICHIER_DEVOIRS = DONNEES / "devoirs.json"      # ton carnet de devoirs
FICHIER_CACHE = DONNEES / "edt_cache.json"      # dernier emploi du temps connu
FICHIER_ETAT = DONNEES / "etat.json"            # ce qui a deja ete envoye
FICHIER_METEO = DONNEES / "meteo.json"          # dernier bulletin Open-Meteo
FICHIER_SEMAINES = DONNEES / "semaines.json"    # le poids des semaines passees
FICHIER_RAPPELS = DONNEES / "rappels.json"      # les rappels poses avec /rappel
FICHIER_ANNIVERSAIRES = DONNEES / "anniversaires.json"
FICHIER_SONDAGES = DONNEES / "sondages.json"    # les sondages du bot en cours
FICHIER_EVENEMENTS = DONNEES / "evenements.json"  # examens -> evenements Discord
FICHIER_ETAT_BOT = DONNEES / "etat-bot.json"    # ce que le bot a deja envoye


def preparer_dossiers():
    """Cree le dossier de donnees au premier lancement."""
    DONNEES.mkdir(parents=True, exist_ok=True)


# --- Ecrire les identifiants de salons dans config.yaml ----------------------
def ecrire_salons(salons, serveur="", categorie=""):
    """Ecrit les identifiants de salons DANS config.yaml, commentaires intacts.

    On ne passe pas par yaml.dump() : il reecrirait tout le fichier et
    effacerait les commentaires, qui sont ici la moitie de la documentation. On
    fait donc une substitution ligne a ligne, uniquement sur les lignes qui ont
    exactement la forme attendue, et uniquement a l'interieur du bloc
    `salons:` pour les salons.

    Les valeurs ne sont pas relues a chaud : il faut relancer le programme.
    Rend la liste de ce qui a change.
    """
    texte = FICHIER_CONFIG.read_text(encoding="utf-8")
    change = []

    for cle, valeur in (("serveur_id", serveur), ("categorie_id", categorie)):
        if not valeur:
            continue
        motif = re.compile(rf'^(\s*{cle}:\s*)(.*)$', re.M)
        if motif.search(texte):
            texte = motif.sub(rf'\g<1>"{valeur}"', texte, count=1)
            change.append(f"{cle} = {valeur}")

    # Le bloc `salons:` seulement : ailleurs, une cle « edt: » pourrait exister
    # dans une autre section et se faire ecraser par erreur.
    debut = texte.find("\n  salons:")
    if debut == -1:
        raise ConfigInvalide(
            "impossible de trouver le bloc `salons:` dans config.yaml. "
            "Ajoute-le sous `discord:` (voir config.example.yaml), ou colle "
            "les identifiants a la main.")
    # Le bloc s'arrete a la premiere ligne moins indentee qui n'est pas vide.
    lignes = texte[debut + 1:].split("\n")
    fin_relative = len(lignes)
    for i, ligne in enumerate(lignes[1:], start=1):
        if ligne.strip() and not ligne.startswith("    "):
            fin_relative = i
            break
    bloc = "\n".join(lignes[:fin_relative])
    reste = "\n".join(lignes[fin_relative:])

    for canal, ident in (salons or {}).items():
        motif = re.compile(rf'^(\s+{canal}:\s*)("[^"]*"|\'[^\']*\'|\S*)(\s*(?:#.*)?)$', re.M)
        if motif.search(bloc):
            bloc = motif.sub(rf'\g<1>"{ident}"\g<3>', bloc, count=1)
            change.append(f"{canal} = {ident}")

    FICHIER_CONFIG.write_text(texte[:debut + 1] + bloc + "\n" + reste,
                              encoding="utf-8")
    return change


# --- Verification au demarrage -----------------------------------------------
def manquants():
    """Les reglages indispensables encore vides, en clair.

    Appele par assistant.py et bot.py avant de demarrer : mieux vaut un message
    precis tout de suite qu'une pile d'exceptions dans dix minutes.
    """
    trous = []
    if not BOT_TOKEN and not WEBHOOK_SECOURS:
        trous.append("discord.bot_token — portail developpeur Discord > "
                     "ton application > Bot > Reset Token")
    if not CYU_USER:
        trous.append("cyu.user — ton identifiant CYU (ex. e-nomprenom)")
    if not CYU_PASS:
        trous.append("cyu.password — ton mot de passe CYU")
    if not FEDERATION_ID:
        trous.append("cyu.federation_id — le fid0= de ton URL CELCAT")
    return trous


def resume():
    """Une ligne par reglage important : ce qu'affiche `assistant.py config`."""
    def masque(valeur):
        valeur = str(valeur or "")
        if not valeur:
            return "VIDE"
        return valeur[:6] + "..." + valeur[-4:] if len(valeur) > 14 else "rempli"

    from datetime import datetime
    heure_ok, _ = controle_heure()
    lignes = [
        f"config.yaml : {FICHIER_CONFIG}",
        "",
        f"  fuseau               {FUSEAU_APPLIQUE or 'heure du serveur'} "
        f"— il est {datetime.now():%H:%M}"
        + ("" if heure_ok else "   [!] pas a l'heure : python assistant.py heure"),
        f"  bot_token            {masque(BOT_TOKEN)}",
        f"  webhook_secours      {'rempli' if WEBHOOK_SECOURS else 'VIDE'}",
        f"  cyu.user             {CYU_USER or 'VIDE'}",
        f"  cyu.password         {'rempli' if CYU_PASS else 'VIDE'}",
        f"  federation_id        {FEDERATION_ID or 'VIDE'}",
        f"  horizon              {HORIZON_JOURS} jours",
        "",
        "  salons",
    ]
    for canal in CANAUX:
        valeur = SALONS[canal]
        if not valeur and canal in CANAUX_BOT_SEULEMENT:
            etat = "vide -> pas de salon dedie"
        elif not valeur:
            etat = "vide -> webhook de secours"
        elif valeur.startswith("http"):
            etat = "webhook"
        elif valeur.isdigit():
            etat = valeur
        else:
            etat = f"INVALIDE ({valeur[:20]})"
        lignes.append(f"    {canal:10s} {etat}")

    rappels = (", ".join(f"{m} min" for m in AVANT_COURS_MINUTES)
               if AVANT_COURS_MINUTES else "aucun (briefings seulement)")
    premier = f"{PREMIER_COURS_MINUTES} min avant" if PREMIER_COURS_MINUTES else "desactive"
    noms_jours = ("lun", "mar", "mer", "jeu", "ven", "sam", "dim")
    jours_matin = (", ".join(noms_jours[j] for j in BRIEFING_MATIN_JOURS)
                   if BRIEFING_MATIN_JOURS else "tous les jours")
    if not RELANCE_DEVOIRS:
        relance = "desactivee"
    elif RELANCE_DEVOIRS_HEURE:
        relance = RELANCE_DEVOIRS_HEURE
    else:
        relance = f"{RELANCE_DEVOIRS_APRES_MINUTES} min apres le dernier cours"
    lignes += [
        "",
        f"  briefing matin       {BRIEFING_MATIN} ({jours_matin})",
        f"  briefing soir        {BRIEFING_SOIR}",
        f"  rappels avant cours  {rappels}",
        f"  rappel premier cours {premier}",
        f"  relance devoirs      {relance}",
        f"  silence              {SILENCE_DE or '-'} -> {SILENCE_A or '-'}",
        f"  meteo                " + (f"{METEO_LIEU} ({METEO_LAT:.4f}, {METEO_LON:.4f})"
                                      if METEO_ACTIVE else "desactivee"),
        f"  anniversaires        " + (f"a {ANNIVERSAIRES_HEURE} dans #annonces"
                                      if ANNIVERSAIRES else "desactives"),
        f"  examens -> evenements" + (f" oui, {EVENEMENTS_HORIZON_JOURS} jours en avant"
                                      if EVENEMENTS_EXAMENS else " non"),
        f"  sondages             {SONDAGE_DUREE_HEURES} h par defaut"
        + (", resultat annonce" if SONDAGE_RESULTATS else ""),
    ]
    return lignes
