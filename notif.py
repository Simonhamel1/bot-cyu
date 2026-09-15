#!/usr/bin/env python3
"""
Aiguillage Discord : qui envoie quoi, et dans quel salon.

Un seul module sait poster sur Discord ; tous les autres passent par ici.
Changer de salon se fait donc a un seul endroit : la section `salons` de
config.yaml.

Deux facons de poster, au choix, salon par salon :

  * un IDENTIFIANT DE SALON (que des chiffres) -> poste via ton bot, qui doit
    etre sur le serveur et avoir un jeton dans config.yaml ;
  * une URL DE WEBHOOK -> aucun droit a donner, mais un webhook = un salon.

Deux facons de poster, aussi, selon la duree de vie du message :

  * envoyer()   un message de plus dans le salon. Pour tout ce qui est un
                evenement : un briefing, une alerte, une echeance ;
  * epingler()  UN message, toujours le meme, reecrit sur place. Pour tout ce
                qui est un etat : le tableau de l'emploi du temps, le panneau
                de statut. Le salon ne se remplit pas, et l'information est
                toujours a la premiere place.

Tu n'as pas a chercher les identifiants a la main :

    python assistant.py salons

cree les salons dans la categorie et ECRIT LEURS IDENTIFIANTS DANS
config.yaml. Relancer la commande ne cree pas de doublon.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import requests

import config

API = "https://discord.com/api/v10"
UA = "assistant-cyu/2.0"

CANAUX = config.CANAUX

# Pour chaque type de message : (nom du salon cree, sujet affiche en tete).
SALONS = {
    "annonces":  ("annonces", "Briefing du matin, briefing du soir, recap du dimanche"),
    "edt":       ("emploi-du-temps", "La semaine en cours, remise a jour toute seule"),
    "devoirs":   ("devoirs", "Echeances, relance du soir, carnet de devoirs"),
    "alertes":   ("alertes", "Cours deplace ou annule, pannes CELCAT - le seul salon qui mentionne"),
    "statut":    ("statut", "Etat de l'assistant, prochain cours, fraicheur des donnees"),
    "commandes": ("commandes", "Tape tes commandes ici. Le panneau est epingle en haut."),
    "logs":      ("logs", "Demarrages, erreurs, heartbeats. A mettre en muet."),
    "predictions": ("predictions", "Vos predictions et vos sondages : qui va valider, "
                                   "quel cours va sauter. On vote, l'auteur tranche."),
}

COULEURS = {"info": 0x5865F2, "cours": 0x57F287, "devoir": 0xFEE75C,
            "alerte": 0xED4245, "calme": 0x99AAB5, "statut": 0x2B2D31}

# Les messages « vivants » (statut, tableau EDT) : leur identifiant est garde
# ici pour pouvoir les reecrire au lieu d'en reposter un a chaque fois.
FICHIER_MESSAGES = config.DONNEES / "messages.json"


def destination(canal):
    """(mode, cible) pour un type de message : ("webhook", url) ou ("bot", id).

    Repli : le salon demande, sinon le webhook de secours. On n'echoue jamais
    faute de configuration ; au pire tout arrive au meme endroit.
    """
    valeur = str(config.SALONS.get(canal) or "").strip()
    if not valeur:
        return "webhook", config.WEBHOOK_SECOURS
    if valeur.startswith("http"):
        return "webhook", valeur
    if valeur.isdigit():
        return "bot", valeur
    print(f"[!] salon '{canal}' invalide ({valeur[:40]}) : ni un identifiant de "
          f"salon ni une URL de webhook", flush=True)
    return "webhook", config.WEBHOOK_SECOURS


def salon_configure(canal):
    """Le salon a-t-il sa propre destination, ou retombe-t-il sur le secours ?"""
    return bool(str(config.SALONS.get(canal) or "").strip())


# --- Envoi -------------------------------------------------------------------
def _entetes(mode):
    if mode == "bot":
        return {"Authorization": f"Bot {config.BOT_TOKEN}", "User-Agent": UA}
    return {"User-Agent": UA}


def _requete(methode, url, entetes, charge=None):
    """Une requete Discord qui ne leve jamais : une panne de Discord ne doit pas
    tuer un daemon qui tourne depuis trois semaines.

    Rend (reponse ou None). Une 429 est retentee une fois, c'est tout : si
    Discord nous limite deux fois de suite, insister aggrave le probleme.
    """
    try:
        r = requests.request(methode, url, json=charge, headers=entetes, timeout=15)
        if r.status_code == 429:
            time.sleep(min(float(r.json().get("retry_after", 2)), 10))
            r = requests.request(methode, url, json=charge, headers=entetes, timeout=15)
        return r
    except requests.RequestException as e:
        print(f"[!] Discord injoignable ({methode} {url[:60]}) : {e}", flush=True)
        return None


def _poster(canal, charge, rendre_message=False):
    """Poste la charge utile. Rend True/False, ou le message JSON si demande."""
    mode, cible = destination(canal)
    if mode == "bot":
        if not config.BOT_TOKEN:
            print(f"[!] salon '{canal}' = {cible} mais discord.bot_token est vide "
                  f"dans config.yaml", flush=True)
            return None if rendre_message else False
        url = f"{API}/channels/{cible}/messages"
    else:
        if not cible:
            print(f"[!] salon '{canal}' vide et aucun webhook_secours dans "
                  f"config.yaml : le message est perdu.", flush=True)
            return None if rendre_message else False
        # ?wait=true : sans ca un webhook repond 204 sans corps, et on ne peut
        # donc pas recuperer l'identifiant du message pour le reecrire ensuite.
        url = cible + ("&" if "?" in cible else "?") + "wait=true"

    r = _requete("POST", url, _entetes(mode), charge)
    if r is None:
        return None if rendre_message else False
    if r.status_code >= 400:
        print(f"[!] Discord HTTP {r.status_code} sur '{canal}' : {r.text[:200]}",
              flush=True)
        return None if rendre_message else False
    if not rendre_message:
        return True
    try:
        return r.json()
    except ValueError:
        return None


def _requete_fichier(methode, url, entetes, charge, chemin):
    """POST ou PATCH multipart : un embed ET une image, en une seule requete.

    Discord veut du multipart des qu'il y a une piece jointe, pas du JSON : la
    charge utile part dans un champ `payload_json` a cote du fichier. C'est la
    seule forme de requete du module qui ne passe pas par _requete().
    """
    try:
        with open(chemin, "rb") as f:
            r = requests.request(
                methode, url, headers=entetes, timeout=60,
                data={"payload_json": json.dumps(charge)},
                files={"files[0]": (Path(chemin).name, f, "image/png")})
    except (requests.RequestException, OSError) as e:
        print(f"[!] envoi de l'image echoue : {e}", flush=True)
        return None
    if r.status_code == 429:
        time.sleep(min(float(r.json().get("retry_after", 2)), 10))
        return _requete_fichier(methode, url, entetes, charge, chemin)
    return r


def _charge_image(chemin, titre, corps, couleur, pied, ping):
    contenu, autorisees = _mention(ping)
    charge = {"content": contenu, "allowed_mentions": autorisees,
              "attachments": [{"id": 0, "filename": Path(chemin).name}]}
    if titre or corps:
        charge["embeds"] = [embed(titre or Path(chemin).stem, corps, couleur, pied)]
        # L'image est montree DANS l'embed : sinon Discord affiche l'embed puis
        # la piece jointe en dessous, et on lit deux fois la meme chose.
        charge["embeds"][0]["image"] = {"url": f"attachment://{Path(chemin).name}"}
    return charge


def envoyer_image(chemin, titre="", corps="", canal="commandes", couleur="cours",
                  ping=False, pied=None):
    """Poste une image (l'emploi du temps, les changements, les devoirs...).

    C'est la fonction que le daemon utilise partout ou le texte Discord ne
    rend pas justice au contenu, c'est-a-dire a peu pres partout.
    """
    chemin = Path(chemin)
    if not chemin.exists():
        print(f"[!] image introuvable : {chemin}", flush=True)
        return False

    mode, cible = destination(canal)
    if mode == "bot":
        if not config.BOT_TOKEN:
            return False
        url = f"{API}/channels/{cible}/messages"
    else:
        if not cible:
            return False
        url = cible

    r = _requete_fichier("POST", url, _entetes(mode),
                         _charge_image(chemin, titre, corps, couleur, pied, ping),
                         chemin)
    if r is None:
        return False
    if r.status_code >= 400:
        print(f"[!] Discord HTTP {r.status_code} sur '{canal}' (image) : "
              f"{r.text[:200]}", flush=True)
        return False
    return True


# Ancien nom, garde pour ne rien casser dans un script personnel.
envoyer_fichier = envoyer_image


def epingler_image(cle, chemin, titre, corps="", couleur="cours", canal="edt",
                   pied=None):
    """Comme epingler(), mais le message porte une IMAGE reecrite sur place.

    Discord remplace les pieces jointes d'un message modifie par celles qu'on
    redeclare : il suffit donc de renvoyer le PNG a chaque fois pour que le
    salon #edt contienne une seule image, toujours a jour, et ne notifie
    jamais.

    Si le message a ete supprime a la main, la reecriture echoue en 404 et on
    en poste un neuf — supprimer le message doit suffire a le regenerer.
    """
    chemin = Path(chemin)
    if not chemin.exists():
        return False
    mode, cible = destination(canal)
    if mode == "bot" and not config.BOT_TOKEN:
        return False
    if mode == "webhook" and not cible:
        return False

    charge = _charge_image(chemin, titre, corps, couleur, pied, ping=False)
    memoire = _lire_messages()
    connu = memoire.get(cle) or {}

    if connu.get("id") and str(connu.get("cible")) == str(cible):
        r = _requete_fichier("PATCH", _url_message(mode, cible, connu["id"]),
                             _entetes(mode), charge, chemin)
        if r is not None and r.status_code < 400:
            return True
        if r is not None and r.status_code != 404:
            print(f"[!] reecriture de '{cle}' impossible (HTTP {r.status_code}) : "
                  f"{r.text[:160]}", flush=True)

    url = (f"{API}/channels/{cible}/messages" if mode == "bot"
           else cible + ("&" if "?" in cible else "?") + "wait=true")
    r = _requete_fichier("POST", url, _entetes(mode), charge, chemin)
    if r is None or r.status_code >= 400:
        if r is not None:
            print(f"[!] Discord HTTP {r.status_code} sur '{canal}' (image "
                  f"epinglee) : {r.text[:200]}", flush=True)
        return False
    try:
        message = r.json()
    except ValueError:
        return False
    memoire[cle] = {"id": str(message.get("id")), "cible": str(cible),
                    "canal": canal,
                    "le": datetime.now().isoformat(timespec="seconds")}
    _ecrire_messages(memoire)
    if mode == "bot" and config.BOT_TOKEN:
        _requete("PUT", f"{API}/channels/{cible}/pins/{message['id']}",
                 _entetes("bot"))
    return True


def _mention(ping, utilisateurs=()):
    """(contenu, allowed_mentions). `utilisateurs` : des identifiants Discord a
    mentionner nommement (un anniversaire, un rappel) — ils passent meme quand
    `mentions_actives` est a false, puisqu'on ne parle pas de toi."""
    veut = ping and config.MENTION and config.MENTIONS_ACTIVES
    morceaux = [config.MENTION] if veut else []
    ids = [str(u) for u in utilisateurs if str(u).isdigit()]
    morceaux += [f"<@{u}>" for u in ids if f"<@{u}>" not in morceaux]
    autorisees = {"parse": ["users"] if veut else []}
    if ids:
        autorisees["users"] = ids[:100]
    return " ".join(morceaux), autorisees


def embed(titre, corps, couleur="info", pied=None, horodate=True):
    """L'embed utilise partout : meme allure dans le daemon et dans le bot."""
    texte = corps if isinstance(corps, str) else "\n".join(str(l) for l in corps)
    if len(texte) > 4000:                   # limite Discord sur la description
        texte = texte[:3960] + "\n[...]"
    sortie = {"title": str(titre)[:250], "description": texte,
              "color": COULEURS.get(couleur, COULEURS["info"]),
              "footer": {"text": (pied or "assistant CYU")[:2040]}}
    if horodate:
        sortie["timestamp"] = datetime.now().astimezone().isoformat()
    return sortie


def envoyer(titre, corps, couleur="info", ping=False, canal="logs", pied=None,
            utilisateurs=()):
    """Un embed Discord dans le salon du type `canal`."""
    contenu, autorisees = _mention(ping, utilisateurs)
    return _poster(canal, {"content": contenu, "allowed_mentions": autorisees,
                           "embeds": [embed(titre, corps, couleur, pied)]})


def simple(msg, ping=False, canal="logs"):
    """Message texte brut : ce dont uptime.py a besoin."""
    contenu, autorisees = _mention(ping)
    texte = f"{contenu} {msg}".strip() if contenu else str(msg)
    return _poster(canal, {"content": texte[:1990], "allowed_mentions": autorisees})


# --- Messages vivants : un seul message, reecrit sur place -------------------
def _lire_messages():
    try:
        data = json.loads(FICHIER_MESSAGES.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def _ecrire_messages(data):
    config.preparer_dossiers()
    FICHIER_MESSAGES.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                encoding="utf-8")


def _url_message(mode, cible, message_id):
    if mode == "bot":
        return f"{API}/channels/{cible}/messages/{message_id}"
    return f"{cible}/messages/{message_id}"


def epingler(cle, titre, corps, couleur="statut", canal="statut", pied=None,
             composants=None):
    """Ecrit UN message et le reecrit ensuite, au lieu d'en empiler.

    `cle` identifie le message a travers les redemarrages ("tableau-edt",
    "panneau-statut"). Son identifiant Discord est garde dans
    donnees/messages.json.

    Si le message a ete supprime a la main, la reecriture echoue en 404 : on en
    reposte alors un neuf, et on retient le nouvel identifiant. C'est le
    comportement voulu — supprimer le message doit suffire a le regenerer.

    Rend True si le salon a bien ete mis a jour.
    """
    mode, cible = destination(canal)
    charge = {"content": "", "allowed_mentions": {"parse": []},
              "embeds": [embed(titre, corps, couleur, pied)]}
    if composants is not None:
        charge["components"] = composants

    memoire = _lire_messages()
    connu = memoire.get(cle) or {}

    # On ne reecrit que si le message est bien la ou on croit : changer un
    # salon dans config.yaml doit reposter ailleurs, pas modifier l'ancien.
    if connu.get("id") and str(connu.get("cible")) == str(cible):
        r = _requete("PATCH", _url_message(mode, cible, connu["id"]),
                     _entetes(mode), charge)
        if r is not None and r.status_code < 400:
            return True
        if r is not None and r.status_code != 404:
            print(f"[!] reecriture de '{cle}' impossible (HTTP {r.status_code}) : "
                  f"{r.text[:160]}", flush=True)

    message = _poster(canal, charge, rendre_message=True)
    if not message:
        return False
    memoire[cle] = {"id": str(message.get("id")), "cible": str(cible),
                    "canal": canal,
                    "le": datetime.now().isoformat(timespec="seconds")}
    _ecrire_messages(memoire)

    # Epingler pour de vrai, quand c'est le bot qui poste : le message reste
    # accessible en un clic meme si le salon defile.
    if mode == "bot" and config.BOT_TOKEN:
        _requete("PUT", f"{API}/channels/{cible}/pins/{message['id']}",
                 _entetes("bot"))
    return True


def oublier_message(cle):
    """Force la creation d'un nouveau message au prochain epingler()."""
    memoire = _lire_messages()
    if memoire.pop(cle, None) is not None:
        _ecrire_messages(memoire)
        return True
    return False


# --- Creation des salons -----------------------------------------------------
class EchecDiscord(RuntimeError):
    """Probleme de configuration cote Discord (jeton, droits, mauvais id)."""


def _api(methode, chemin, **kw):
    if not config.BOT_TOKEN:
        raise EchecDiscord(
            "discord.bot_token est vide dans config.yaml. Portail developpeur "
            "Discord > ton application > Bot > Reset Token.")
    r = requests.request(methode, f"{API}{chemin}", timeout=20,
                         headers={"Authorization": f"Bot {config.BOT_TOKEN}",
                                  "User-Agent": UA}, **kw)
    if r.status_code == 401:
        raise EchecDiscord("jeton de bot refuse (401) : il est faux ou revoque.")
    if r.status_code == 403:
        raise EchecDiscord(
            "acces refuse (403) : le bot n'est pas sur ce serveur, ou il lui "
            "manque la permission « Gerer les salons ».")
    if r.status_code == 404:
        raise EchecDiscord(f"introuvable (404) sur {chemin} : verifie l'identifiant.")
    if r.status_code == 429:
        attente = float(r.json().get("retry_after", 5))
        raise EchecDiscord(f"Discord limite le debit, reessaie dans {attente:.0f} s.")
    if r.status_code >= 400:
        raise EchecDiscord(f"HTTP {r.status_code} sur {chemin} : {r.text[:300]}")
    return r.json() if r.text else {}


def creer_salons(categorie_id="", canaux=CANAUX):
    """Cree (ou retrouve) un salon par type de message dans la categorie.

    Idempotent : relancer ne cree pas de doublon, on reutilise un salon deja
    present dans la categorie qui porte le bon nom.

    Rend (id_du_serveur, {canal: (id_du_salon, "cree"|"reutilise")}).
    L'identifiant du serveur est deduit de la categorie : personne n'a a le
    chercher a la main.
    """
    cat = str(categorie_id or config.CATEGORIE_ID or "").strip()
    if not cat.isdigit():
        raise EchecDiscord(
            "discord.categorie_id manquant ou invalide dans config.yaml. Clic "
            "droit sur la categorie > Copier l'identifiant (mode developpeur).")

    info = _api("GET", f"/channels/{cat}")
    if info.get("type") != 4:
        raise EchecDiscord(
            f"l'identifiant {cat} n'est pas une categorie (type {info.get('type')}). "
            f"C'est peut-etre un salon : prends l'element parent.")
    guilde = str(info["guild_id"])

    presents = _api("GET", f"/guilds/{guilde}/channels")
    par_nom = {c["name"]: c for c in presents if str(c.get("parent_id")) == cat}

    resultat = {}
    for position, canal in enumerate(canaux):
        nom, sujet = SALONS[canal]
        if nom in par_nom:
            resultat[canal] = (str(par_nom[nom]["id"]), "reutilise")
            continue
        cree = _api("POST", f"/guilds/{guilde}/channels",
                    json={"name": nom, "type": 0, "parent_id": cat,
                          "topic": sujet, "position": position})
        resultat[canal] = (str(cree["id"]), "cree")
        time.sleep(0.7)      # Discord limite fortement la creation de salons
    return guilde, resultat
