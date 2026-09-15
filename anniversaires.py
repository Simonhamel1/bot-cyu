#!/usr/bin/env python3
"""
Les anniversaires de la promo.

Chacun donne le sien avec /anniversaire (« 12/10 », « 12 octobre », et
l'annee si on veut l'age), et le matin du jour J le bot le souhaite dans
#annonces en mentionnant la personne. /anniversaires dit qui est le prochain.

Sur disque, un dictionnaire par identifiant Discord :

    {"1234": {"nom": "Simon", "jour": 12, "mois": 10, "annee": 2005 | null}}
"""

from __future__ import annotations

import json
import re
from datetime import date

import config
from celcat import normaliser
from vue import MOIS

FICHIER = config.FICHIER_ANNIVERSAIRES

RE_NUM = re.compile(r"^(\d{1,2})\s*[/.\-]\s*(\d{1,2})(?:\s*[/.\-]\s*(\d{2,4}))?$")
RE_MOTS = re.compile(r"^(\d{1,2})(?:er)?\s+([a-z]+)(?:\s+(\d{4}))?$")


# --- Lecture et ecriture -----------------------------------------------------
def lire():
    try:
        data = json.loads(FICHIER.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def ecrire(data):
    config.preparer_dossiers()
    FICHIER.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def definir(user_id, nom, jour, mois, annee=None):
    data = lire()
    data[str(user_id)] = {"nom": str(nom)[:60], "jour": int(jour), "mois": int(mois),
                          "annee": int(annee) if annee else None}
    ecrire(data)
    return data[str(user_id)]


def retirer(user_id):
    data = lire()
    if data.pop(str(user_id), None) is None:
        return False
    ecrire(data)
    return True


# --- Lire une date -----------------------------------------------------------
def lire_date(txt):
    """« 12/10 », « 12/10/2005 », « 12 octobre », « 1er mars 2004 »
    -> (jour, mois, annee ou None). ValueError sinon."""
    bas = normaliser(txt)
    m = RE_NUM.match(bas)
    if m:
        jour, mois, annee = int(m.group(1)), int(m.group(2)), m.group(3)
    else:
        m = RE_MOTS.match(bas)
        if not m:
            raise ValueError(f"je ne comprends pas « {str(txt).strip()} » : "
                             f"écris 12/10, 12/10/2005 ou 12 octobre")
        jour, annee = int(m.group(1)), m.group(3)
        mot = m.group(2)
        candidats = [i for i, nom in enumerate(MOIS, start=1)
                     if nom.startswith(mot[:3])]
        if len(candidats) != 1:
            raise ValueError(f"« {mot} » n'est pas un mois que je connais")
        mois = candidats[0]
    if annee:
        annee = int(annee)
        if annee < 100:
            annee += 2000 if annee <= date.today().year % 100 else 1900
        if not (1900 <= annee <= date.today().year):
            raise ValueError(f"{annee} : une année de naissance, vraiment ?")
    try:
        date(2024, mois, jour)                  # 2024 est bissextile : le 29/02 passe
    except ValueError:
        raise ValueError(f"le {jour}/{mois} n'existe pas")
    return jour, mois, (annee or None)


# --- Qui, et quand -----------------------------------------------------------
def prochaine_date(entree, ref=None):
    """La prochaine occurrence de cet anniversaire, aujourd'hui compris."""
    ref = ref or date.today()
    jour, mois = int(entree["jour"]), int(entree["mois"])
    for annee in (ref.year, ref.year + 1):
        try:
            d = date(annee, mois, jour)
        except ValueError:                      # 29 fevrier hors annee bissextile
            d = date(annee, 2, 28)
        if d >= ref:
            return d
    return date(ref.year + 1, mois, jour)


def age_le(entree, d):
    """L'age qu'on aura ce jour-la, si l'annee est connue."""
    annee = entree.get("annee")
    return (d.year - int(annee)) if annee else None


def du_jour(ref=None, data=None):
    """[(user_id, entree)] de ceux dont c'est l'anniversaire ce jour."""
    ref = ref or date.today()
    data = data if data is not None else lire()
    return [(uid, e) for uid, e in data.items() if prochaine_date(e, ref) == ref]


def prochains(n=10, ref=None, data=None):
    """[(date, user_id, entree)] les prochains anniversaires, du plus proche
    au plus lointain, aujourd'hui compris."""
    ref = ref or date.today()
    data = data if data is not None else lire()
    liste = [(prochaine_date(e, ref), uid, e) for uid, e in data.items()]
    liste.sort(key=lambda t: (t[0], normaliser(t[2].get("nom", ""))))
    return liste[:n]


def libelle(entree):
    """« 12 octobre », « 1er mars »."""
    jour = int(entree["jour"])
    return f"{jour}{'er' if jour == 1 else ''} {MOIS[int(entree['mois']) - 1]}"
