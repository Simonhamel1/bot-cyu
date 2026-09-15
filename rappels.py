#!/usr/bin/env python3
"""
Les rappels : « rappelle-moi demain 9 h de rendre le TP ».

/rappel pose un rappel, le bot le poste a l'heure dite dans le salon ou il a
ete pose, en mentionnant celui qui l'a demande (ou tout le salon, si c'est
un rappel pour la promo : « @here rendu du projet a 23 h 59 »).

Ce module ne fait que deux choses : garder les rappels sur disque, et lire
« quand » dans ce que les gens tapent. Le bot fait le reste.

La grammaire de « quand », la plus tolerante possible :

    dans 30 min · dans 2 h · dans 1h30 · dans 3 jours · dans une semaine
    18h · 18h30 · 18:30                 aujourd'hui, ou demain si c'est passe
    demain 9h · lundi 14h · 12/10 8h30  un jour, et une heure
    demain · lundi · 12/10 · +3         un jour seul : 9 h du matin
    ce soir · demain matin · lundi midi  soir = 20 h, midi = 12 h, matin = 9 h
    prochain:maths                      juste avant le prochain cours de maths

Un rappel :

    {"id": 3, "texte": "...", "quand": "2026-10-12T09:00", "auteur_id": "1234",
     "auteur": "Simon", "salon_id": "5678", "qui": "moi" | "here",
     "cree_le": "...", "essais": 0}
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

import config
import devoirs as dv
from celcat import normaliser

FICHIER = config.FICHIER_RAPPELS

# Un rappel qui n'a pas pu partir (salon supprime, droits retires) est
# retente quelques fois, pas indefiniment.
ESSAIS_MAX = 3

# L'heure d'un rappel pose « pour lundi » sans preciser.
HEURE_PAR_DEFAUT = (9, 0)
MOMENTS = {"matin": (9, 0), "midi": (12, 0), "aprem": (15, 0),
           "apres-midi": (15, 0), "apres midi": (15, 0), "soir": (20, 0)}

RE_DANS = re.compile(
    r"^dans\s+(\d+)\s*(min|mn|minutes?|m|h|heures?|j|jours?|sem|semaines?|s)?"
    r"(?:\s*(?:et\s*)?(\d+)\s*(?:min|mn|minutes?|m)?)?$")
RE_HEURE = re.compile(
    r"^(?P<jour>.*?)\s*(?:a\s+)?(?P<h>\d{1,2})\s*(?:h|:)\s*(?P<m>\d{2})?\s*$")


# --- Lecture et ecriture -----------------------------------------------------
def lire():
    try:
        data = json.loads(FICHIER.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (ValueError, OSError):
        return []


def ecrire(liste):
    config.preparer_dossiers()
    FICHIER.write_text(json.dumps(liste, ensure_ascii=False, indent=1),
                       encoding="utf-8")


def _trouver(liste, ident):
    return next((r for r in liste if str(r.get("id")) == str(ident)), None)


def ajouter(texte, quand, auteur_id, auteur, salon_id, qui="moi"):
    liste = lire()
    r = {
        "id": max((int(x.get("id", 0)) for x in liste), default=0) + 1,
        "texte": str(texte).strip()[:400],
        "quand": quand.isoformat(timespec="minutes"),
        "auteur_id": str(auteur_id),
        "auteur": str(auteur)[:60],
        "salon_id": str(salon_id),
        "qui": "here" if qui == "here" else "moi",
        "cree_le": datetime.now().isoformat(timespec="seconds"),
        "essais": 0,
    }
    liste.append(r)
    ecrire(liste)
    return r


def supprimer(ident, par_id):
    """Rend (rappel, "ok" | "absent" | "pas_auteur")."""
    liste = lire()
    r = _trouver(liste, ident)
    if r is None:
        return None, "absent"
    if str(r.get("auteur_id")) != str(par_id):
        return r, "pas_auteur"
    liste.remove(r)
    ecrire(liste)
    return r, "ok"


def retirer(ident):
    """Le rappel est parti : on l'oublie."""
    liste = lire()
    r = _trouver(liste, ident)
    if r is not None:
        liste.remove(r)
        ecrire(liste)
    return r


def echec(ident):
    """Un envoi rate de plus. Rend True si le rappel est abandonne."""
    liste = lire()
    r = _trouver(liste, ident)
    if r is None:
        return True
    r["essais"] = int(r.get("essais") or 0) + 1
    abandonne = r["essais"] >= ESSAIS_MAX
    if abandonne:
        liste.remove(r)
    ecrire(liste)
    return abandonne


def quand(r):
    try:
        return datetime.fromisoformat(str(r.get("quand") or ""))
    except ValueError:
        return None


def de(auteur_id, liste=None):
    """Les rappels a venir de quelqu'un, du plus proche au plus lointain."""
    liste = liste if liste is not None else lire()
    miens = [r for r in liste if str(r.get("auteur_id")) == str(auteur_id)]
    return sorted(miens, key=lambda r: quand(r) or datetime.max)


def echus(maintenant=None, liste=None):
    """Ceux dont l'heure est passee : a envoyer maintenant."""
    maintenant = maintenant or datetime.now()
    liste = liste if liste is not None else lire()
    return sorted((r for r in liste if (q := quand(r)) is not None and q <= maintenant),
                  key=lambda r: quand(r))


# --- Lire « quand » ----------------------------------------------------------
def lire_moment(txt, cours=None, maintenant=None):
    """Ce que quelqu'un tape -> un datetime dans le futur. ValueError sinon."""
    maintenant = maintenant or datetime.now()
    brut = normaliser(txt)
    brut = re.sub(r"\b(un|une)\b", "1", brut)
    brut = re.sub(r"^(le|la|pour|a)\s+", "", brut).strip()
    if not brut:
        raise ValueError("dis-moi quand : « demain 9h », « dans 2h », « lundi 14h »…")

    # « dans 2 h », « dans 30 min », « dans 1h30 », « dans 3 jours »
    m = RE_DANS.match(brut)
    if m:
        n, unite, reste = int(m.group(1)), (m.group(2) or "min"), int(m.group(3) or 0)
        if unite.startswith("h"):
            delta = timedelta(hours=n, minutes=reste)
        elif unite.startswith("j"):
            delta = timedelta(days=n)
        elif unite.startswith("s"):
            delta = timedelta(weeks=n)
        else:
            delta = timedelta(minutes=n)
        if delta <= timedelta(0):
            raise ValueError("« dans 0 » : c'est maintenant, pas un rappel")
        return (maintenant + delta).replace(second=0, microsecond=0)

    # « ... matin / midi / soir » : un moment de la journee.
    heure = None
    for mot, hm in MOMENTS.items():
        if brut == mot or brut.endswith(" " + mot):
            heure = hm
            brut = brut[:-len(mot)].strip()
            break
    brut = re.sub(r"^(ce|cet|cette)\s*$", "", brut).strip()
    brut = re.sub(r"^(ce|cet|cette)\s+", "", brut).strip()

    # « ... 9h », « ... 9h30 », « ... 9:30 »
    m = RE_HEURE.match(brut) if heure is None else None
    if m:
        h, mn = int(m.group("h")), int(m.group("m") or 0)
        if not (0 <= h <= 23 and 0 <= mn <= 59):
            raise ValueError(f"« {m.group('h')}h{m.group('m') or ''} » n'est pas une heure")
        heure = (h, mn)
        brut = m.group("jour").strip()

    if not brut:
        if heure is None:
            raise ValueError("dis-moi quand : « demain 9h », « dans 2h », « lundi 14h »…")
        moment = datetime.combine(maintenant.date(), datetime.min.time()).replace(
            hour=heure[0], minute=heure[1])
        if moment <= maintenant:
            moment += timedelta(days=1)         # « 18h » a 19 h : c'est demain
        return moment

    try:
        iso = dv.resoudre_echeance(brut, cours or [], "")
    except ValueError:
        raise ValueError(f"je ne comprends pas « {txt.strip()} ». Essaie « demain 9h », "
                         f"« dans 2h », « lundi 14h », « 12/10 8h30 », « ce soir ».")
    if iso is None:
        raise ValueError("dis-moi quand")
    if len(iso) > 10 and heure is None:
        # « prochain:maths » : l'heure exacte du cours. Un rappel un quart
        # d'heure avant est plus utile qu'un rappel quand ca commence.
        moment = datetime.fromisoformat(iso) - timedelta(minutes=15)
    else:
        jour = date.fromisoformat(iso[:10])
        h, mn = heure or HEURE_PAR_DEFAUT
        moment = datetime.combine(jour, datetime.min.time()).replace(hour=h, minute=mn)
    if moment <= maintenant:
        raise ValueError(f"{moment:%d/%m à %H:%M}, c'est déjà passé")
    return moment
