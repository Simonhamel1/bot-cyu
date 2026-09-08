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
CANAUX = ("annonces", "edt", "devoirs", "alertes", "statut", "commandes", "logs")

_salons_bruts = _discord.get("salons") or {}
SALONS = {canal: _txt(_salons_bruts, canal) for canal in CANAUX}

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
BRIEFING_SOIR = _txt(_notif, "briefing_soir", "20:00")
RECAP_SEMAINE_JOUR = _entier(_notif, "recap_semaine_jour", 6) % 7
RECAP_SEMAINE_HEURE = _txt(_notif, "recap_semaine_heure", "18:00")

AVANT_COURS_MINUTES = _liste_entiers(_notif, "avant_cours_minutes")
PREMIER_COURS_MINUTES = _optionnel_entier(_notif, "premier_cours_minutes")
SEULEMENT_PREMIER_COURS_DU_BLOC = _bool(_notif, "seulement_premier_du_bloc", True)

RELANCE_DEVOIRS = _bool(_notif, "relance_devoirs", True)
RELANCE_DEVOIRS_APRES_MINUTES = _entier(_notif, "relance_devoirs_apres_minutes", 20)
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


# --- Matieres ----------------------------------------------------------------
MATIERES = BRUT.get("matieres") if isinstance(BRUT.get("matieres"), dict) else {}


# --- Fichiers (rien a regler) ------------------------------------------------
DONNEES = RACINE / "donnees"

FICHIER_DEVOIRS = DONNEES / "devoirs.json"      # ton carnet de devoirs
FICHIER_CACHE = DONNEES / "edt_cache.json"      # dernier emploi du temps connu
FICHIER_ETAT = DONNEES / "etat.json"            # ce qui a deja ete envoye


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

    lignes = [
        f"config.yaml : {FICHIER_CONFIG}",
        "",
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
        if not valeur:
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
    lignes += [
        "",
        f"  briefing matin       {BRIEFING_MATIN}",
        f"  briefing soir        {BRIEFING_SOIR}",
        f"  rappels avant cours  {rappels}",
        f"  rappel premier cours {premier}",
        f"  silence              {SILENCE_DE or '-'} -> {SILENCE_A or '-'}",
    ]
    return lignes
