#!/usr/bin/env python3
"""
Ce que la promo garde sur disque : les devoirs, les rappels, les predictions, les sondages, les anniversaires, les examens en evenements Discord, et le quiz Pokemon.

Sommaire :

    Les devoirs                                   (ex-devoirs.py)
    Les rappels                                   (ex-rappels.py)
    Les predictions                               (ex-predictions.py)
    Les sondages                                  (ex-sondages.py)
    Les anniversaires                             (ex-anniversaires.py)
    Les examens en evenements Discord             (ex-evenements.py)
    Le quiz Pokemon                               (ex-pokemon.py)
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import random
import re
import unicodedata
from datetime import date, datetime, timedelta

import discord

import affichage
import config
import edt


# ════════════════════════════════════════════════════════════════════════════
#  LES DEVOIRS  (ex-devoirs.py)
# ════════════════════════════════════════════════════════════════════════════
# Le carnet de devoirs, et surtout son lien avec l'emploi du temps.
#
# L'idee centrale : une echeance ne s'ecrit pas toujours en date. « a rendre au
# prochain cours de maths » est plus naturel, et c'est justement ce que le script
# sait resoudre, puisqu'il connait ton emploi du temps.
#
#     resoudre_echeance("prochain:maths", cours)  ->  "2026-09-08T10:15"
#
# Un devoir est un simple dictionnaire, stocke en JSON :
#
#     {"id": 3, "titre": "DM 2", "matiere": "maths",
#      "echeance": "2026-09-08T10:15", "type": "dm", "fait": false,
#      "note": "exercices 4 a 9", "cree_le": "2026-09-01T18:22:07"}


JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


# ─── Lecture et ecriture ─────────────────────────────────────────────────────
def lire_devoirs():
    return config.lire_json(config.FICHIER_DEVOIRS, [])


def ecrire_devoirs(liste):
    config.ecrire_json(config.FICHIER_DEVOIRS, liste)


@config.exclusif(config.FICHIER_DEVOIRS)
def ajouter_devoirs(titre, matiere, echeance, type_="devoir", note=""):
    liste = lire_devoirs()
    devoir = {
        "id": max((int(x.get("id", 0)) for x in liste), default=0) + 1,
        "titre": titre.strip(),
        "matiere": matiere.strip(),
        "echeance": echeance,
        "type": type_,
        "fait": False,
        "note": note.strip(),
        "cree_le": datetime.now().isoformat(timespec="seconds"),
    }
    liste.append(devoir)
    ecrire_devoirs(liste)
    return devoir


@config.exclusif(config.FICHIER_DEVOIRS)
def marquer_fait(ident):
    """True si le devoir existait, False sinon."""
    liste = lire_devoirs()
    for d in liste:
        if str(d.get("id")) == str(ident):
            d["fait"] = True
            ecrire_devoirs(liste)
            return True
    return False


@config.exclusif(config.FICHIER_DEVOIRS)
def supprimer_devoirs(ident):
    """Le devoir supprime, ou None s'il n'existait pas."""
    liste = lire_devoirs()
    for i, d in enumerate(liste):
        if str(d.get("id")) == str(ident):
            liste.pop(i)
            ecrire_devoirs(liste)
            return d
    return None


# ─── Echeances ───────────────────────────────────────────────────────────────
def echeance_dt(d):
    """Un devoir sans heure est du pour la fin de la journee, pas pour minuit :
    sinon il apparait en retard des le matin meme."""
    brut = str(d.get("echeance") or "")
    if not brut:
        return None
    val = edt._dt(brut)
    if val is None:
        try:
            val = datetime.combine(date.fromisoformat(brut[:10]), datetime.min.time())
        except ValueError:
            return None
    if len(brut) <= 10:
        val = val.replace(hour=23, minute=59)
    return val


def jours_restants(d, ref=None):
    ech = echeance_dt(d)
    return None if ech is None else (ech.date() - (ref or date.today())).days


def actifs(liste=None, horizon_jours=None):
    """Les devoirs non faits, tries par urgence. horizon_jours=None -> tous."""
    sortie = [d for d in (liste if liste is not None else lire_devoirs()) if not d.get("fait")]
    if horizon_jours is not None:
        sortie = [d for d in sortie
                  if (r := jours_restants(d)) is not None and r <= horizon_jours]
    return sorted(sortie, key=lambda d: (echeance_dt(d) or datetime.max))


# ─── Le lien avec les cours ──────────────────────────────────────────────────
def cles_matiere(matiere):
    """Mots qui rattachent un devoir a un cours : la matiere, ses alias, et sa
    forme sans pluriel — sans ca, « maths » ne trouverait pas « MATH101 »."""
    base = edt.normaliser(matiere)
    if not base:
        return []
    cles = {base}
    if base.endswith("s") and len(base) > 4:
        cles.add(base[:-1])
    for canon, variantes in (config.MATIERES or {}).items():
        noms = {edt.normaliser(canon)} | {edt.normaliser(v) for v in (variantes or [])}
        if cles & noms:
            cles |= noms
    return [c for c in cles if len(c) >= 3]


def cours_correspond(cours, cles):
    cible = edt.normaliser(f"{cours.module} {cours.titre}")
    return any(k in cible for k in cles)


def du_cours(cours, liste=None):
    """Les devoirs a rendre pour ce cours precis : meme matiere, echeance ce
    jour-la. C'est ce qui fait apparaitre « a rendre : DM 2 » sous le cours."""
    sortie = []
    for d in (liste if liste is not None else lire_devoirs()):
        if d.get("fait"):
            continue
        ech = echeance_dt(d)
        if ech is None or ech.date() != cours.jour:
            continue
        if cours_correspond(cours, cles_matiere(d.get("matiere", ""))):
            sortie.append(d)
    return sortie


def prochain_cours_matiere(cours, matiere, apres=None):
    apres = apres or datetime.now()
    cles = cles_matiere(matiere)
    if not cles:
        return None
    return next((c for c in cours
                 if c.debut > apres and c.est_cours and cours_correspond(c, cles)), None)


RE_JJMM = re.compile(r"^(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?$")


def resoudre_echeance(txt, cours, matiere=""):
    """Traduit ce que tu tapes en date ISO.

    Accepte : 2026-09-12 · 12/09 · 12/09/2026 · demain · apres-demain · lundi ·
    +3 (dans 3 jours) · prochain (prochain cours de la matiere du devoir) ·
    prochain:maths (prochain cours de maths, avec son heure exacte).
    """
    brut = (txt or "").strip()
    if not brut:
        return None
    bas = edt.normaliser(brut)
    today = date.today()

    if bas.startswith("prochain"):
        cible = brut.split(":", 1)[1].strip() if ":" in brut else matiere
        cible = re.sub(r"(?i)^\s*(cours\s+de\s+)?", "", cible).strip()
        c = prochain_cours_matiere(cours, cible)
        if c is None:
            raise ValueError(
                f"aucun cours a venir ne correspond a « {cible or '?'} ». "
                f"Ajoute un alias dans config.py > MATIERES, ou donne une date.")
        return c.debut.isoformat(timespec="minutes")

    if bas in ("aujourd'hui", "aujourdhui", "auj", "ce soir"):
        return today.isoformat()
    if bas == "demain":
        return (today + timedelta(days=1)).isoformat()
    if bas in ("apres-demain", "apres demain"):
        return (today + timedelta(days=2)).isoformat()

    if bas in JOURS:
        ecart = (JOURS.index(bas) - today.weekday()) % 7 or 7
        return (today + timedelta(days=ecart)).isoformat()

    m = re.match(r"^\+?(\d+)\s*j?$", bas)
    if m:
        return (today + timedelta(days=int(m.group(1)))).isoformat()

    m = RE_JJMM.match(bas)
    if m:
        j, mo, an = int(m.group(1)), int(m.group(2)), m.group(3)
        an = int(an) + (2000 if an and len(an) == 2 else 0) if an else today.year
        try:
            d = date(an, mo, j)
        except ValueError:
            raise ValueError(f"date invalide : {brut}")
        # Sans annee, une date deja passee vise l'annee suivante.
        if not m.group(3) and d < today:
            d = d.replace(year=d.year + 1)
        return d.isoformat()

    val = edt._dt(brut)
    if val:
        return val.isoformat(timespec="minutes") if len(brut) > 10 else val.date().isoformat()
    raise ValueError(f"echeance incomprise : « {brut} » (essaie 12/09, demain, "
                     f"lundi, +3, ou prochain:maths)")


# ════════════════════════════════════════════════════════════════════════════
#  LES RAPPELS  (ex-rappels.py)
# ════════════════════════════════════════════════════════════════════════════
# Les rappels : « rappelle-moi demain 9 h de rendre le TP ».
#
# /rappel pose un rappel, le bot le poste a l'heure dite dans le salon ou il a
# ete pose, en mentionnant celui qui l'a demande (ou tout le salon, si c'est
# un rappel pour la promo : « @here rendu du projet a 23 h 59 »).
#
# Ce module ne fait que deux choses : garder les rappels sur disque, et lire
# « quand » dans ce que les gens tapent. Le bot fait le reste.
#
# La grammaire de « quand », la plus tolerante possible :
#
#     dans 30 min · dans 2 h · dans 1h30 · dans 3 jours · dans une semaine
#     18h · 18h30 · 18:30                 aujourd'hui, ou demain si c'est passe
#     demain 9h · lundi 14h · 12/10 8h30  un jour, et une heure
#     demain · lundi · 12/10 · +3         un jour seul : 9 h du matin
#     ce soir · demain matin · lundi midi  soir = 20 h, midi = 12 h, matin = 9 h
#     prochain:maths                      juste avant le prochain cours de maths
#
# Un rappel :
#
#     {"id": 3, "texte": "...", "quand": "2026-10-12T09:00", "auteur_id": "1234",
#      "auteur": "Simon", "salon_id": "5678", "qui": "moi" | "here",
#      "cree_le": "...", "essais": 0}


FICHIER_RAPPELS = config.FICHIER_RAPPELS

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
def lire_rappels():
    return config.lire_json(FICHIER_RAPPELS, [])


def ecrire_rappels(liste):
    config.ecrire_json(FICHIER_RAPPELS, liste)


def _trouver_rappels(liste, ident):
    return next((r for r in liste if str(r.get("id")) == str(ident)), None)


@config.exclusif(FICHIER_RAPPELS)
def ajouter_rappels(texte, quand, auteur_id, auteur, salon_id, qui="moi"):
    liste = lire_rappels()
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
    ecrire_rappels(liste)
    return r


@config.exclusif(FICHIER_RAPPELS)
def supprimer_rappels(ident, par_id):
    """Rend (rappel, "ok" | "absent" | "pas_auteur")."""
    liste = lire_rappels()
    r = _trouver_rappels(liste, ident)
    if r is None:
        return None, "absent"
    if str(r.get("auteur_id")) != str(par_id):
        return r, "pas_auteur"
    liste.remove(r)
    ecrire_rappels(liste)
    return r, "ok"


@config.exclusif(FICHIER_RAPPELS)
def retirer_rappels(ident):
    """Le rappel est parti : on l'oublie."""
    liste = lire_rappels()
    r = _trouver_rappels(liste, ident)
    if r is not None:
        liste.remove(r)
        ecrire_rappels(liste)
    return r


@config.exclusif(FICHIER_RAPPELS)
def echec(ident):
    """Un envoi rate de plus. Rend True si le rappel est abandonne."""
    liste = lire_rappels()
    r = _trouver_rappels(liste, ident)
    if r is None:
        return True
    r["essais"] = int(r.get("essais") or 0) + 1
    abandonne = r["essais"] >= ESSAIS_MAX
    if abandonne:
        liste.remove(r)
    ecrire_rappels(liste)
    return abandonne


def quand(r):
    try:
        return datetime.fromisoformat(str(r.get("quand") or ""))
    except ValueError:
        return None


def de(auteur_id, liste=None):
    """Les rappels a venir de quelqu'un, du plus proche au plus lointain."""
    liste = liste if liste is not None else lire_rappels()
    miens = [r for r in liste if str(r.get("auteur_id")) == str(auteur_id)]
    return sorted(miens, key=lambda r: quand(r) or datetime.max)


def echus(maintenant=None, liste=None):
    """Ceux dont l'heure est passee : a envoyer maintenant."""
    maintenant = maintenant or datetime.now()
    liste = liste if liste is not None else lire_rappels()
    return sorted((r for r in liste if (q := quand(r)) is not None and q <= maintenant),
                  key=lambda r: quand(r))


# --- Lire « quand » ----------------------------------------------------------
def lire_moment(txt, cours=None, maintenant=None):
    """Ce que quelqu'un tape -> un datetime dans le futur. ValueError sinon."""
    maintenant = maintenant or datetime.now()
    brut = edt.normaliser(txt)
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
    # Les plus longs d'abord : « apres midi » finit aussi par « midi ».
    for mot, hm in sorted(MOMENTS.items(), key=lambda kv: -len(kv[0])):
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
        iso = resoudre_echeance(brut, cours or [], "")
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


# ════════════════════════════════════════════════════════════════════════════
#  LES PREDICTIONS  (ex-predictions.py)
# ════════════════════════════════════════════════════════════════════════════
# Les predictions : un jeu entre vous, pas une fonction de l'assistant.
#
# Quelqu'un ecrit « Kevin va valider l'annee » ou « le cours de VBA de jeudi va
# sauter », tout le monde vote pour ou contre, et l'auteur tranche le jour venu.
# Un classement dit qui avait raison : les prophetes (ceux dont les predictions
# se realisent) et les parieurs (ceux qui votent juste).
#
# Ce module ne fait que garder les donnees sur disque et compter. Il ne sait
# rien de Discord : bot.py lui passe des identifiants et des noms d'utilisateur,
# et affiche ce qu'il rend.
#
# Une prediction :
#
#     {"id": 3, "texte": "...", "auteur_id": "1234", "auteur": "Simon",
#      "cree_le": "...", "echeance": "2026-06-30" ou "", "echeance_texte": "...",
#      "mise": "un kebab", "votes": {"5678": {"choix": "oui", "nom": "Lea"}},
#      "resultat": null | "oui" | "non", "tranche_le": "..."}
#
# Le vote est un toggle : revoter la meme chose retire le vote, voter l'autre
# chose le change. Seul l'auteur tranche ou supprime sa prediction — c'est son
# pari, et c'est lui qui devra assumer le kebab.


FICHIER_PREDICTIONS = config.DONNEES / "predictions.json"


# --- Lecture et ecriture -----------------------------------------------------
def lire_predictions():
    return config.lire_json(FICHIER_PREDICTIONS, [])


def ecrire_predictions(liste):
    config.ecrire_json(FICHIER_PREDICTIONS, liste)


def _trouver_predictions(liste, ident):
    return next((p for p in liste if str(p.get("id")) == str(ident)), None)


trouver = _trouver_predictions


@config.exclusif(FICHIER_PREDICTIONS)
def ajouter_predictions(texte, auteur_id, auteur, echeance="", echeance_texte="", mise=""):
    liste = lire_predictions()
    p = {
        "id": max((int(x.get("id", 0)) for x in liste), default=0) + 1,
        "texte": texte.strip()[:300],
        "auteur_id": str(auteur_id),
        "auteur": str(auteur)[:60],
        "cree_le": datetime.now().isoformat(timespec="seconds"),
        "echeance": echeance or "",
        "echeance_texte": (echeance_texte or "").strip()[:60],
        "mise": (mise or "").strip()[:80],
        "votes": {},
        "resultat": None,
        "tranche_le": "",
    }
    liste.append(p)
    ecrire_predictions(liste)
    return p


@config.exclusif(FICHIER_PREDICTIONS)
def voter(ident, user_id, nom, choix):
    """Vote « oui » ou « non ». Le meme vote deux fois = plus de vote.

    Rend la prediction, ou None si elle n'existe pas ou est deja tranchee."""
    liste = lire_predictions()
    p = _trouver_predictions(liste, ident)
    if p is None or p.get("resultat"):
        return None
    votes = p.setdefault("votes", {})
    actuel = (votes.get(str(user_id)) or {}).get("choix")
    if actuel == choix:
        votes.pop(str(user_id), None)
    else:
        votes[str(user_id)] = {"choix": choix, "nom": str(nom)[:60]}
    ecrire_predictions(liste)
    return p


@config.exclusif(FICHIER_PREDICTIONS)
def trancher(ident, resultat, par_id):
    """L'auteur dit si c'est arrive (« oui ») ou non (« non »).

    Rend (prediction, "ok" | "absente" | "pas_auteur" | "deja")."""
    liste = lire_predictions()
    p = _trouver_predictions(liste, ident)
    if p is None:
        return None, "absente"
    if str(p.get("auteur_id")) != str(par_id):
        return p, "pas_auteur"
    if p.get("resultat"):
        return p, "deja"
    p["resultat"] = resultat
    p["tranche_le"] = datetime.now().isoformat(timespec="seconds")
    ecrire_predictions(liste)
    return p, "ok"


@config.exclusif(FICHIER_PREDICTIONS)
def supprimer_predictions(ident, par_id):
    """Rend (prediction, "ok" | "absente" | "pas_auteur")."""
    liste = lire_predictions()
    p = _trouver_predictions(liste, ident)
    if p is None:
        return None, "absente"
    if str(p.get("auteur_id")) != str(par_id):
        return p, "pas_auteur"
    liste.remove(p)
    ecrire_predictions(liste)
    return p, "ok"


# --- Lire ce qu'il y a -------------------------------------------------------
def ouvertes(liste=None):
    liste = liste if liste is not None else lire_predictions()
    return [p for p in liste if not p.get("resultat")]


def closes(liste=None):
    liste = liste if liste is not None else lire_predictions()
    return sorted((p for p in liste if p.get("resultat")),
                  key=lambda p: p.get("tranche_le", ""), reverse=True)


def comptes(p):
    """(oui, non) : combien de votes de chaque cote."""
    votes = p.get("votes") or {}
    oui = sum(1 for v in votes.values() if v.get("choix") == "oui")
    return oui, len(votes) - oui


def noms(p, choix, maxi=4):
    """Les prenoms de ceux qui ont vote `choix`, tronques a `maxi`."""
    liste = [v.get("nom", "?") for v in (p.get("votes") or {}).values()
             if v.get("choix") == choix]
    if len(liste) > maxi:
        return ", ".join(liste[:maxi]) + f" +{len(liste) - maxi}"
    return ", ".join(liste)


def echeance_date(p):
    try:
        return date.fromisoformat(str(p.get("echeance") or "")[:10])
    except ValueError:
        return None


def en_retard(p, aujourd=None):
    """Une prediction ouverte dont l'echeance est passee : a trancher."""
    ech = echeance_date(p)
    return bool(ech) and not p.get("resultat") and ech < (aujourd or date.today())


# --- Le classement -----------------------------------------------------------
def classement_predictions(liste=None):
    """(prophetes, parieurs, stats).

    prophetes : [(nom, realisees, tentees)] — les auteurs, par predictions
                qui se sont realisees ;
    parieurs  : [(nom, bons, votes)] — les votants, par votes justes ;
    stats     : {"ouvertes", "tranchees", "realisees", "reussite_votes"}.
    """
    liste = liste if liste is not None else lire_predictions()
    auteurs, votants = {}, {}
    bons_total, votes_total = 0, 0
    for p in liste:
        a = auteurs.setdefault(p.get("auteur_id"), {"nom": p.get("auteur"), "ok": 0, "n": 0})
        resultat = p.get("resultat")
        if resultat:
            a["n"] += 1
            a["ok"] += resultat == "oui"
            for uid, v in (p.get("votes") or {}).items():
                w = votants.setdefault(uid, {"nom": v.get("nom"), "ok": 0, "n": 0})
                w["n"] += 1
                juste = v.get("choix") == resultat
                w["ok"] += juste
                bons_total += juste
                votes_total += 1
    prophetes = sorted(((a["nom"], a["ok"], a["n"]) for a in auteurs.values() if a["n"]),
                       key=lambda t: (-t[1], t[2], t[0]))
    parieurs = sorted(((w["nom"], w["ok"], w["n"]) for w in votants.values()),
                      key=lambda t: (-t[1], t[2], t[0]))
    tranchees = [p for p in liste if p.get("resultat")]
    stats = {
        "ouvertes": len(ouvertes(liste)),
        "tranchees": len(tranchees),
        "realisees": sum(1 for p in tranchees if p["resultat"] == "oui"),
        "reussite_votes": (bons_total / votes_total * 100) if votes_total else None,
    }
    return prophetes, parieurs, stats


# ════════════════════════════════════════════════════════════════════════════
#  LES SONDAGES  (ex-sondages.py)
# ════════════════════════════════════════════════════════════════════════════
# Les sondages : ceux de Discord, les vrais, avec leurs barres qui se
# remplissent en direct — pas des reactions a compter a la main.
#
# /sondage (ou le bouton du panneau) pose une question et des reponses ; Discord
# fait le reste. Ce module lit ce que les gens tapent (« 🍕 Pizza ; 🍔 Burger »
# ou une reponse par ligne), construit le sondage, et retient ceux du bot pour
# en annoncer le resultat quand ils se terminent — sinon le resultat reste
# enfoui trois ecrans plus haut.
#
# Les limites sont celles de Discord : 300 caracteres de question, 10 reponses
# de 55 caracteres, de 1 heure a 32 jours.


FICHIER_SONDAGES = config.FICHIER_SONDAGES

# Les durees proposees, en heures. Discord accepte de 1 a 768.
DUREES = [("1 heure", 1), ("6 heures", 6), ("12 heures", 12), ("24 heures", 24),
          ("3 jours", 72), ("1 semaine", 168), ("2 semaines", 336)]

# Des jeux de reponses tout prets, proposes pendant la frappe.
MODELES = [
    ("Oui / Non", "👍 Oui ; 👎 Non"),
    ("Je viens / En retard / Absent", "✅ Je viens ; ⏰ En retard ; ❌ Absent"),
    ("Lundi → Vendredi", "Lundi ; Mardi ; Mercredi ; Jeudi ; Vendredi"),
    ("Matin / Midi / Aprem / Soir", "🌅 Matin ; ☀️ Midi ; 🌇 Aprem ; 🌙 Soir"),
    ("😍 🙂 😐 😴", "😍 ; 🙂 ; 😐 ; 😴"),
    ("De 1 à 5", "1 ; 2 ; 3 ; 4 ; 5"),
]

QUESTION_MAX, REPONSE_MAX, REPONSES_MAX = 300, 55, 10

# Un emoji en tete d'une reponse : « 🍕 Pizza » -> emoji 🍕, texte « Pizza ».
# Les emojis Unicode habituels, avec leur eventuel selecteur de variante, ton
# de peau ou sequence ZWJ ; et les emojis personnalises du serveur <:nom:id>.
_EMOJI = (r"(?:[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF"
          r"\u25A0-\u25FF\u2190-\u21FF\u3030\u303D\u3297\u3299\u00A9\u00AE\u2122\u2139]"
          r"[\uFE0F\U0001F3FB-\U0001F3FF]*(?:\u200D[\U0001F000-\U0001FAFF\u2600-\u27BF]"
          r"\uFE0F?)*)")
RE_EMOJI_TETE = re.compile(rf"^(<a?:\w+:\d+>|{_EMOJI}|[0-9#*]\uFE0F?\u20E3)\s*")


def reponses(texte):
    """« 🍕 Pizza ; 🍔 Burger » ou une reponse par ligne
    -> [(emoji ou None, texte)]. ValueError si c'est inutilisable."""
    brut = str(texte or "").strip()
    if not brut:
        brut = MODELES[0][1]
    morceaux = [m.strip() for m in re.split(r"[;\n|]+|\s+/\s+", brut) if m.strip()]
    sortie, vus = [], set()
    for m in morceaux:
        emoji, txt = None, m
        e = RE_EMOJI_TETE.match(m)
        if e:
            emoji, txt = e.group(1), m[e.end():].strip()
        if not txt:                             # « 😍 » tout seul
            emoji, txt = None, m
        txt = txt[:REPONSE_MAX]
        cle = (emoji, txt.lower())
        if cle in vus:
            continue
        vus.add(cle)
        sortie.append((emoji, txt))
    if len(sortie) < 1:
        raise ValueError("il faut au moins une réponse")
    if len(sortie) > REPONSES_MAX:
        raise ValueError(f"Discord s'arrête à {REPONSES_MAX} réponses, "
                         f"il y en a {len(sortie)}")
    return sortie


def construire(question, texte_reponses, heures=None, plusieurs=False):
    """Un discord.Poll pret a envoyer. ValueError si la saisie ne va pas."""
    question = str(question or "").strip()
    if not question:
        raise ValueError("il manque la question")
    heures = int(heures or config.SONDAGE_DUREE_HEURES)
    heures = min(768, max(1, heures))
    sondage = discord.Poll(question=question[:QUESTION_MAX],
                           duration=timedelta(hours=heures), multiple=bool(plusieurs))
    for emoji, txt in reponses(texte_reponses):
        sondage.add_answer(text=txt, emoji=emoji)
    return sondage


def duree_texte(heures):
    for nom, h in DUREES:
        if h == heures:
            return nom
    if heures % 24 == 0 and heures >= 48:
        return f"{heures // 24} jours"
    return f"{heures} h"


# --- Les sondages du bot, en cours -------------------------------------------
def lire_sondages():
    return config.lire_json(FICHIER_SONDAGES, [])


def ecrire_sondages(liste):
    config.ecrire_json(FICHIER_SONDAGES, liste)


@config.exclusif(FICHIER_SONDAGES)
def suivre(message_id, salon_id, fin, question, auteur_id):
    """Retenir un sondage pour en annoncer le resultat a la fin."""
    liste = [s for s in lire_sondages() if str(s.get("message_id")) != str(message_id)]
    liste.append({"message_id": str(message_id), "salon_id": str(salon_id),
                  "fin": fin.isoformat(timespec="minutes"), "question": question[:300],
                  "auteur_id": str(auteur_id),
                  "cree_le": datetime.now().isoformat(timespec="seconds")})
    ecrire_sondages(liste)


@config.exclusif(FICHIER_SONDAGES)
def oublier(message_id):
    liste = lire_sondages()
    reste = [s for s in liste if str(s.get("message_id")) != str(message_id)]
    if len(reste) != len(liste):
        ecrire_sondages(reste)


# Un sondage fini mais pas encore clos par Discord n'est pas relu plus
# souvent que ca.
RELECTURE = timedelta(minutes=5)


def a_relever(maintenant=None, liste=None):
    """Les sondages dont la fin est passee : a aller lire. Ceux qu'on vient
    de relire sans succes attendent leur tour."""
    maintenant = maintenant or datetime.now()
    liste = liste if liste is not None else lire_sondages()
    sortie = []
    for s in liste:
        try:
            fin = datetime.fromisoformat(str(s.get("fin") or ""))
            lu = datetime.fromisoformat(s["lu_le"]) if s.get("lu_le") else None
        except (ValueError, TypeError):
            continue
        if fin <= maintenant and (lu is None or maintenant - lu >= RELECTURE):
            sortie.append(s)
    return sortie


@config.exclusif(FICHIER_SONDAGES)
def marquer_lecture(message_id, maintenant=None):
    liste = lire_sondages()
    for s in liste:
        if str(s.get("message_id")) == str(message_id):
            s["lu_le"] = (maintenant or datetime.now()).isoformat(timespec="seconds")
    ecrire_sondages(liste)


def perime(s, maintenant=None, heures=6):
    """Un sondage qu'on n'arrive pas a relire depuis des heures : on lache."""
    maintenant = maintenant or datetime.now()
    try:
        fin = datetime.fromisoformat(str(s.get("fin") or ""))
    except ValueError:
        return True
    return maintenant - fin > timedelta(hours=heures)


# --- Le resultat -------------------------------------------------------------
def _barre(part, largeur=12):
    plein = int(round(part * largeur))
    return "▰" * plein + "▱" * (largeur - plein)


def resultat(sondage):
    """(lignes, gagnant) a partir d'un discord.Poll termine.

    gagnant est le texte de la reponse en tete, ou None en cas d'egalite ou
    sans aucun vote."""
    reponses_ = list(sondage.answers)
    total = sum(a.vote_count for a in reponses_)
    reponses_.sort(key=lambda a: -a.vote_count)
    lignes = []
    medailles = ["🥇", "🥈", "🥉"]
    for i, a in enumerate(reponses_):
        part = (a.vote_count / total) if total else 0
        tete = medailles[i] if i < 3 and a.vote_count else "▫️"
        emoji = f"{a.emoji} " if a.emoji else ""
        lignes.append(f"{tete} {emoji}**{a.text}** — {a.vote_count} voix · "
                      f"{part * 100:.0f} %\n-# {_barre(part)}")
    if not total:
        return ["Personne n'a voté. 🫥"], None
    if len(reponses_) > 1 and reponses_[0].vote_count == reponses_[1].vote_count:
        gagnant = None
    else:
        gagnant = reponses_[0].text
    return lignes, gagnant


# ════════════════════════════════════════════════════════════════════════════
#  LES ANNIVERSAIRES  (ex-anniversaires.py)
# ════════════════════════════════════════════════════════════════════════════
# Les anniversaires de la promo.
#
# Chacun donne le sien avec /anniversaire (« 12/10 », « 12 octobre », et
# l'annee si on veut l'age), et le matin du jour J le bot le souhaite dans
# #annonces en mentionnant la personne. /anniversaires dit qui est le prochain.
#
# Sur disque, un dictionnaire par identifiant Discord :
#
#     {"1234": {"nom": "Simon", "jour": 12, "mois": 10, "annee": 2005 | null}}


FICHIER_ANNIVERSAIRES = config.FICHIER_ANNIVERSAIRES

RE_NUM = re.compile(r"^(\d{1,2})\s*[/.\-]\s*(\d{1,2})(?:\s*[/.\-]\s*(\d{2,4}))?$")
RE_MOTS = re.compile(r"^(\d{1,2})(?:er)?\s+([a-z]+)(?:\s+(\d{4}))?$")


# --- Lecture et ecriture -----------------------------------------------------
def lire_anniversaires():
    return config.lire_json(FICHIER_ANNIVERSAIRES, {})


def ecrire_anniversaires(data):
    config.ecrire_json(FICHIER_ANNIVERSAIRES, data)


@config.exclusif(FICHIER_ANNIVERSAIRES)
def definir(user_id, nom, jour, mois, annee=None):
    data = lire_anniversaires()
    data[str(user_id)] = {"nom": str(nom)[:60], "jour": int(jour), "mois": int(mois),
                          "annee": int(annee) if annee else None}
    ecrire_anniversaires(data)
    return data[str(user_id)]


@config.exclusif(FICHIER_ANNIVERSAIRES)
def retirer_anniversaires(user_id):
    data = lire_anniversaires()
    if data.pop(str(user_id), None) is None:
        return False
    ecrire_anniversaires(data)
    return True


# --- Lire une date -----------------------------------------------------------
def lire_date(txt):
    """« 12/10 », « 12/10/2005 », « 12 octobre », « 1er mars 2004 »
    -> (jour, mois, annee ou None). ValueError sinon."""
    bas = edt.normaliser(txt)
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
        candidats = [i for i, nom in enumerate(affichage.MOIS, start=1)
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
    data = data if data is not None else lire_anniversaires()
    return [(uid, e) for uid, e in data.items() if prochaine_date(e, ref) == ref]


def prochains(n=10, ref=None, data=None):
    """[(date, user_id, entree)] les prochains anniversaires, du plus proche
    au plus lointain, aujourd'hui compris."""
    ref = ref or date.today()
    data = data if data is not None else lire_anniversaires()
    liste = [(prochaine_date(e, ref), uid, e) for uid, e in data.items()]
    liste.sort(key=lambda t: (t[0], edt.normaliser(t[2].get("nom", ""))))
    return liste[:n]


def libelle(entree):
    """« 12 octobre », « 1er mars »."""
    jour = int(entree["jour"])
    return f"{jour}{'er' if jour == 1 else ''} {affichage.MOIS[int(entree['mois']) - 1]}"


# ════════════════════════════════════════════════════════════════════════════
#  LES EXAMENS EN EVENEMENTS DISCORD  (ex-evenements.py)
# ════════════════════════════════════════════════════════════════════════════
# Les examens en EVENEMENTS DISCORD.
#
# Discord a un calendrier a lui : le bandeau « evenements » en haut du serveur,
# avec un compte a rebours, une cloche « interesse » et un rappel envoye par
# Discord lui-meme un quart d'heure avant. Un examen y a exactement sa place.
#
# Toutes les heures, le bot compare les examens connus (CELCAT et le carnet de
# devoirs, via stats.examens) aux evenements qu'il a deja crees, et cree, met a
# jour ou retire ce qu'il faut. Il ne touche qu'aux evenements qu'il a crees
# lui-meme : leurs identifiants sont ici, sur disque.
#
# Ce module calcule ce qui DEVRAIT exister ; bot.py parle a Discord.
#
#     {"celcat:2026-10-12:econometrie": {"id": "9876", "empreinte": "..."}}


FICHIER_EVENEMENTS = config.FICHIER_EVENEMENTS

# Un examen qui commence dans moins de deux minutes n'est plus a creer :
# Discord refuse un evenement dans le passe, et la marge evite de le rater
# de justesse.
MARGE = timedelta(minutes=2)


def lire_evenements():
    return config.lire_json(FICHIER_EVENEMENTS, {})


def ecrire_evenements(data):
    config.ecrire_json(FICHIER_EVENEMENTS, data)


def _cle(e):
    return f"{e['source']}:{e['quand']:%Y-%m-%d}:{edt.normaliser(e['matiere'] or e['titre'])[:40]}"


def _nom(e):
    titre = str(e["titre"] or e["matiere"] or "Examen").strip()
    matiere = str(e["matiere"] or "").strip()
    if matiere and edt.normaliser(matiere) not in edt.normaliser(titre):
        titre = f"{titre} · {matiere}"
    return f"📝 {titre}"[:100]


def _description(e):
    lignes = []
    if e.get("duree"):
        h, m = divmod(int(e["duree"]), 60)
        lignes.append(f"Durée : {h} h" + (f" {m:02d}" if m else ""))
    if e.get("ou"):
        lignes.append(f"Où : {e['ou']}")
    lignes.append("D'après CELCAT." if e["source"] == "celcat"
                  else "Noté dans le carnet de devoirs (/devoir, type examen)."
                  + (" Heure non précisée : la journée entière." if e.get("sans_heure")
                     else ""))
    if e.get("note"):
        lignes.append("")
        lignes.append(str(e["note"])[:400])
    lignes += ["", "Clique sur la cloche pour que Discord te rappelle l'examen "
                   "un quart d'heure avant. — assistant CYU"]
    return "\n".join(lignes)[:1000]


def voulus(cours, liste_devoirs=None, maintenant=None):
    """{cle: evenement} — ce qui devrait exister sur Discord.

    Chaque evenement : nom, debut, fin (datetimes avec fuseau), lieu,
    description, empreinte (pour savoir s'il a change sans rien redemander a
    Discord), et commence (True si l'examen a deja commence : on ne le cree
    pas, on ne le modifie pas, on ne le retire pas — Discord s'en occupe).
    """
    maintenant = maintenant or datetime.now()
    horizon = maintenant + timedelta(days=config.EVENEMENTS_HORIZON_JOURS)
    sortie = {}
    for e in edt.examens(cours, liste_devoirs or [], maintenant):
        debut = e["quand"]
        if debut > horizon:
            continue
        duree = int(e.get("duree") or 0) or 120
        sans_heure = e["source"] == "devoir" and f"{debut:%H:%M}" in ("23:59", "00:00")
        if sans_heure:
            # Un examen note « pour le 12/10 » sans heure : la journee, plutot
            # qu'un evenement a 23 h 59.
            debut = debut.replace(hour=8, minute=0)
            duree = 10 * 60
        fin = debut + timedelta(minutes=duree)
        lieu = str(e.get("ou") or "").strip() or "CYU"
        if lieu == "a distance":
            lieu = "À distance"
        e = dict(e, sans_heure=sans_heure)
        nom, description = _nom(e), _description(e)
        empreinte = hashlib.sha1(
            f"{nom}|{debut:%Y-%m-%d %H:%M}|{fin:%Y-%m-%d %H:%M}|{lieu}|{description}"
            .encode("utf-8")).hexdigest()[:16]
        sortie[_cle(e)] = {
            "nom": nom, "debut": debut.astimezone(), "fin": fin.astimezone(),
            "lieu": lieu[:100], "description": description, "empreinte": empreinte,
            "commence": debut <= maintenant + MARGE,
        }
    return sortie


def plan(voulus_, connus):
    """(a_creer, a_modifier, a_retirer) : trois listes de cles.

    Un examen deja commence n'est jamais touche. Un evenement connu dont
    l'examen a disparu (annule, ou termine) est a retirer.
    """
    a_creer, a_modifier, a_retirer = [], [], []
    for cle, e in voulus_.items():
        if e["commence"]:
            continue
        connu = connus.get(cle)
        if connu is None:
            a_creer.append(cle)
        elif connu.get("empreinte") != e["empreinte"]:
            a_modifier.append(cle)
    for cle in connus:
        if cle not in voulus_:
            a_retirer.append(cle)
    return a_creer, a_modifier, a_retirer


# ════════════════════════════════════════════════════════════════════════════
#  LE QUIZ POKEMON  (ex-pokemon.py)
# ════════════════════════════════════════════════════════════════════════════
# « Qui est ce Pokemon ? » : le quiz de la promo.
#
# Le bot montre une SILHOUETTE et propose quatre noms. Le premier qui clique
# juste marque ; les autres jouent quand meme, pour leur score. Ceux qui veulent
# le mode difficile tapent le nom eux-memes, et ca vaut plus cher.
#
# Ce module ne fait que garder les donnees sur disque, tirer au sort et compter.
# Il ne sait rien de Discord : bot.py lui passe des identifiants et des noms
# d'utilisateur, et affiche ce qu'il rend -- exactement comme predictions.py.
#
# Pourquoi des boutons, et pas le nom tape dans le salon
# ------------------------------------------------------
# Lire ce que les gens ecrivent demande l'intent « contenu des messages », un
# droit privilegie a cocher dans le portail Discord. Tout le reste du projet
# s'en passe (voir bot.Assistant), et l'activer pour un jeu ferait lire TOUS les
# messages du serveur par le bot. Quatre boutons suffisent, et le formulaire
# « je tape le nom » rend le mode difficile a ceux qui le cherchent, sans rien
# demander de plus.
#
# D'ou viennent les Pokemon
# -------------------------
# De PokeAPI, en deux temps et une seule fois :
#
#   * les NOMS francais des 1025 especes, un fichier CSV telecharge une fois et
#     garde dans donnees/ ;
#   * les IMAGES, a la demande : seules celles deja tirees finissent en cache,
#     redimensionnees. Personne n'a besoin des 1025 artworks sur son disque.
#
# Sans reseau et sans cache, le quiz le dit au lieu de tomber en panne.
#
# Un quiz :
#
#     {"id": "a3f9c1b2", "espece": 25, "choix": [25, 6, 94, 143],
#      "ouvert_le": "...", "ferme_le": "...", "revele": false,
#      "salon_id": "...", "message_id": "...",
#      "reponses": {"1234": {"nom": "Simon", "choix": 6, "juste": false,
#                            "tape": false, "points": 0, "quand": "..."}}}


FICHIER_POKEMON = config.DONNEES / "pokemon.json"
CATALOGUE = config.DONNEES / "pokemon-noms.json"
IMAGES = config.DONNEES / "pokemon"

# Les noms de toutes les especes, dans toutes les langues, en UN fichier. Les
# interroger une par une ferait 1025 requetes ; ce CSV en fait une seule.
URL_NOMS = ("https://raw.githubusercontent.com/PokeAPI/pokeapi/master/"
            "data/v2/csv/pokemon_species_names.csv")
LANGUE_FR = "5"

# L'illustration officielle, bien plus lisible en silhouette que le petit
# sprite de jeu (96 pixels, illisible des qu'on l'agrandit).
URL_IMAGE = ("https://raw.githubusercontent.com/PokeAPI/sprites/master/"
             "sprites/pokemon/other/official-artwork/{id}.png")

# Les bornes de chaque generation : de quoi limiter le tirage a ce que les
# gens connaissent vraiment. Au-dela de la 3e, il faut etre courageux.
GENERATIONS = {
    1: (1, 151), 2: (152, 251), 3: (252, 386), 4: (387, 493), 5: (494, 649),
    6: (650, 721), 7: (722, 809), 8: (810, 905), 9: (906, 1025),
}

# Ce que rapporte une bonne reponse. Le premier a trouver prend une prime :
# sans elle, rien ne distingue celui qui cherche de celui qui attend.
POINTS_JUSTE = 3
PRIME_PREMIER = 2
PRIME_TAPE = 2                  # avoir tape le nom au lieu de cliquer
PRIME_SERIE = 1                 # a partir de SERIE_MINIMUM bonnes d'affilee
SERIE_MINIMUM = 3

# Comment on nomme quelqu'un dont Discord ne nous a pas donne le nom.
ANONYME = "quelqu'un"

# Un quiz sans reponse finit par se reveler tout seul.
DUREE_MINUTES = 3

# Ce qu'on garde : au-dela, les vieux quiz n'interessent plus personne et le
# fichier grossit pour rien. Les SCORES, eux, ne sont jamais effaces.
QUIZ_GARDES = 60
IMAGES_GARDEES = 400


class PokemonIndisponible(RuntimeError):
    """Pas de catalogue et pas de reseau : le quiz ne peut pas commencer."""


# --- Lecture et ecriture -----------------------------------------------------
def lire_pokemon():
    data = config.lire_json(FICHIER_POKEMON, {})
    data.setdefault("quiz", [])
    data.setdefault("scores", {})
    return data


def ecrire_pokemon(data):
    data["quiz"] = data.get("quiz", [])[:QUIZ_GARDES]
    config.ecrire_json(FICHIER_POKEMON, data)


# --- Le catalogue des especes -------------------------------------------------
_CACHE = {}


def catalogue(telecharger=True):
    """{numero: {"nom": "Pikachu", "genre": "Pokémon Souris"}}.

    Lu une fois puis garde en memoire. Le fichier n'est telecharge que s'il
    manque : les noms des Pokemon ne changent pas, et un quiz ne doit pas
    dependre du reseau a chaque partie.
    """
    if _CACHE:
        return _CACHE
    try:
        data = json.loads(CATALOGUE.read_text(encoding="utf-8"))
        if data:
            _CACHE.update({int(k): v for k, v in data.items()})
            return _CACHE
    except (ValueError, OSError):
        pass
    if not telecharger:
        raise PokemonIndisponible("Le catalogue des Pokémon n'a pas encore été "
                                  "téléchargé.")
    _CACHE.update(_telecharger_noms())
    return _CACHE


def _telecharger_noms():
    """Le CSV de PokeAPI, filtre sur le francais, ecrit dans donnees/."""
    try:
        import requests
    except ImportError as e:                                 # pragma: no cover
        raise PokemonIndisponible("Le module `requests` n'est pas installé.") from e
    try:
        reponse = requests.get(URL_NOMS, timeout=20)
        reponse.raise_for_status()
    except Exception as e:                      # noqa: BLE001 - reseau : tout peut arriver
        raise PokemonIndisponible(
            f"Impossible de télécharger la liste des Pokémon ({e}).") from e

    especes = {}
    for ligne in csv.DictReader(io.StringIO(reponse.text)):
        if ligne.get("local_language_id") != LANGUE_FR:
            continue
        try:
            numero = int(ligne["pokemon_species_id"])
        except (KeyError, ValueError):
            continue
        especes[numero] = {"nom": ligne.get("name", "").strip(),
                           "genre": (ligne.get("genus") or "").strip()}
    especes = {n: v for n, v in especes.items() if v["nom"]}
    if not especes:
        raise PokemonIndisponible("La liste téléchargée ne contient aucun nom "
                                  "français.")
    try:
        CATALOGUE.parent.mkdir(parents=True, exist_ok=True)
        CATALOGUE.write_text(json.dumps(especes, ensure_ascii=False, indent=0),
                             encoding="utf-8")
    except OSError:
        pass                    # tant pis pour le cache, le quiz marche quand meme
    return especes


def nom(numero):
    return catalogue(telecharger=False).get(int(numero), {}).get("nom", f"#{numero}")


def genre(numero):
    return catalogue(telecharger=False).get(int(numero), {}).get("genre", "")


def disponible():
    """Le jeu peut-il demarrer sans reseau ? Sert a n'afficher le bouton que
    si oui -- une fonction synchrone, appelee par le panneau."""
    try:
        return bool(catalogue(telecharger=False))
    except (PokemonIndisponible, OSError):
        return False


# --- Les images ---------------------------------------------------------------
def image_pokemon(numero):
    """Le chemin du PNG d'une espece, telecharge et mis en cache au besoin.

    L'image est gardee telle quelle : c'est image.py qui en tire la silhouette
    puis la revelation, a partir du meme fichier."""
    numero = int(numero)
    chemin = IMAGES / f"{numero}.png"
    if chemin.exists() and chemin.stat().st_size > 0:
        return chemin
    try:
        import requests
        reponse = requests.get(URL_IMAGE.format(id=numero), timeout=20)
        reponse.raise_for_status()
        octets = reponse.content
    except Exception as e:                      # noqa: BLE001 - reseau
        raise PokemonIndisponible(
            f"L'image de {nom(numero)} n'a pas pu être téléchargée ({e}).") from e
    try:
        IMAGES.mkdir(parents=True, exist_ok=True)
        chemin.write_bytes(octets)
        _purger_images()
    except OSError as e:
        raise PokemonIndisponible(f"Impossible d'écrire l'image ({e}).") from e
    return chemin


def _purger_images():
    """Le cache d'images ne grossit pas indefiniment : au-dela de la limite,
    les plus anciennement utilisees s'en vont. Elles se retelechargeront si
    elles ressortent au tirage."""
    try:
        fichiers = sorted(IMAGES.glob("*.png"), key=lambda p: p.stat().st_atime)
    except OSError:
        return
    for vieux in fichiers[:-IMAGES_GARDEES]:
        try:
            vieux.unlink()
        except OSError:
            pass


# --- Tirer un quiz -------------------------------------------------------------
def bornes(generation=None):
    """(premier, dernier) numero tirable. Sans generation, tout le dex."""
    if generation and int(generation) in GENERATIONS:
        return GENERATIONS[int(generation)]
    maxi = max(catalogue(telecharger=False) or [1])
    return 1, maxi


def tirer(generation=None, choix=4, eviter=()):
    """(espece, [choix melanges]) : un Pokemon a deviner et ses leurres.

    `eviter` sert a ne pas reposer la meme question deux fois de suite : rien
    n'use plus vite un quiz que de retomber sur le Pokemon d'il y a deux tours.
    """
    especes = catalogue(telecharger=False)
    premier, dernier = bornes(generation)
    plage = [n for n in especes if premier <= n <= dernier]
    if not plage:
        raise PokemonIndisponible("Aucun Pokémon dans cette génération.")

    frais = [n for n in plage if n not in set(eviter)] or plage
    espece = random.choice(frais)
    leurres = [n for n in plage if n != espece]
    random.shuffle(leurres)
    propositions = [espece] + leurres[:max(0, choix - 1)]
    random.shuffle(propositions)
    return espece, propositions


@config.exclusif(FICHIER_POKEMON)
def ouvrir(espece, propositions, salon_id=None, minutes=None):
    """Le quiz enregistre, pret a recevoir des reponses."""
    maintenant = datetime.now()
    minutes = DUREE_MINUTES if minutes is None else minutes
    quiz = {
        "id": f"{random.getrandbits(32):08x}",
        "espece": int(espece),
        "choix": [int(n) for n in propositions],
        "ouvert_le": maintenant.isoformat(timespec="seconds"),
        "ferme_le": (maintenant + timedelta(minutes=minutes)).isoformat(
            timespec="seconds"),
        "revele": False,
        "salon_id": str(salon_id) if salon_id else "",
        "message_id": "",
        "reponses": {},
    }
    data = lire_pokemon()
    data["quiz"].insert(0, quiz)
    ecrire_pokemon(data)
    return quiz


def par_id(quiz_id, data=None):
    data = data or lire_pokemon()
    for q in data.get("quiz", []):
        if q.get("id") == str(quiz_id):
            return q
    return None


def derniers(nombre=8):
    """Les especes recemment posees, pour ne pas les reposer tout de suite."""
    return [q.get("espece") for q in lire_pokemon().get("quiz", [])[:nombre]]


@config.exclusif(FICHIER_POKEMON)
def retenir_message(quiz_id, message_id, salon_id):
    """Ou le quiz a ete poste : la boucle en a besoin pour le reveler."""
    data = lire_pokemon()
    quiz = par_id(quiz_id, data)
    if quiz is None:
        return
    quiz["message_id"] = str(message_id)
    quiz["salon_id"] = str(salon_id)
    ecrire_pokemon(data)


# --- Repondre -------------------------------------------------------------------
def _sans_fioritures(texte):
    """« Dracaufeu » et « dracaufeu » sont le meme mot ; « M. Mime » aussi.

    On enleve les accents, la casse, et tout ce qui n'est pas une lettre ou un
    chiffre : personne ne doit perdre un point sur un trait d'union."""
    texte = unicodedata.normalize("NFD", str(texte or ""))
    texte = "".join(c for c in texte if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]", "", texte.lower())


@config.exclusif(FICHIER_POKEMON)
def repondre(quiz_id, joueur_id, joueur, choix=None, tape=None):
    """Une reponse. Rend (etat, quiz, points).

    etat vaut :
      "juste"    bonne reponse ;
      "rate"     mauvaise reponse ;
      "deja"     ce joueur a deja repondu -- un quiz, une tentative, sinon il
                 suffirait de cliquer les quatre boutons ;
      "clos"     le quiz est deja revele ;
      "inconnu"  ce quiz n'existe plus.
    """
    data = lire_pokemon()
    quiz = par_id(quiz_id, data)
    if quiz is None:
        return "inconnu", None, 0
    if quiz.get("revele"):
        return "clos", quiz, 0
    if str(joueur_id) in quiz.get("reponses", {}):
        return "deja", quiz, 0

    espece = int(quiz["espece"])
    if tape is not None:
        juste = _sans_fioritures(tape) == _sans_fioritures(nom(espece))
        choisi = espece if juste else None
    else:
        choisi = int(choix) if choix is not None else None
        juste = choisi == espece

    points = 0
    scores = data.setdefault("scores", {})
    fiche = scores.setdefault(str(joueur_id), {
        "nom": joueur, "points": 0, "bonnes": 0, "tentatives": 0,
        "serie": 0, "record": 0, "tapes": 0})
    fiche["nom"] = joueur or fiche.get("nom", "")
    fiche["tentatives"] = fiche.get("tentatives", 0) + 1

    if juste:
        premier = not any(r.get("juste") for r in quiz.get("reponses", {}).values())
        points = POINTS_JUSTE + (PRIME_PREMIER if premier else 0)
        if tape is not None:
            points += PRIME_TAPE
            fiche["tapes"] = fiche.get("tapes", 0) + 1
        fiche["serie"] = fiche.get("serie", 0) + 1
        if fiche["serie"] >= SERIE_MINIMUM:
            points += PRIME_SERIE
        fiche["record"] = max(fiche.get("record", 0), fiche["serie"])
        fiche["bonnes"] = fiche.get("bonnes", 0) + 1
        fiche["points"] = fiche.get("points", 0) + points
    else:
        fiche["serie"] = 0

    quiz.setdefault("reponses", {})[str(joueur_id)] = {
        "nom": joueur, "choix": choisi, "juste": juste,
        "tape": tape is not None, "points": points,
        "quand": datetime.now().isoformat(timespec="seconds")}
    ecrire_pokemon(data)
    return ("juste" if juste else "rate"), quiz, points


@config.exclusif(FICHIER_POKEMON)
def reveler(quiz_id):
    """Marquer le quiz comme termine. Rend le quiz, ou None."""
    data = lire_pokemon()
    quiz = par_id(quiz_id, data)
    if quiz is None:
        return None
    quiz["revele"] = True
    ecrire_pokemon(data)
    return quiz


def gagnant(quiz):
    """Le premier a avoir trouve, ou None."""
    justes = [(r.get("quand", ""), i, r)
              for i, r in (quiz or {}).get("reponses", {}).items() if r.get("juste")]
    return min(justes)[2] if justes else None


def a_reveler(maintenant=None):
    """Les quiz dont le temps est ecoule et que personne n'a clos.

    C'est la boucle du bot qui appelle ceci toutes les trente secondes : un
    quiz ne doit pas rester ouvert parce que le bot a redemarre entre-temps.
    """
    maintenant = maintenant or datetime.now()
    sortie = []
    for quiz in lire_pokemon().get("quiz", []):
        if quiz.get("revele") or not quiz.get("message_id"):
            continue
        try:
            fin = datetime.fromisoformat(quiz.get("ferme_le", ""))
        except ValueError:
            continue
        if fin <= maintenant:
            sortie.append(quiz)
    return sortie


# --- Le classement ---------------------------------------------------------------
def classement_pokemon(limite=10):
    """[(rang, fiche)] : les meilleurs, par points puis par reussite."""
    fiches = []
    for ident, f in lire_pokemon().get("scores", {}).items():
        if not f.get("tentatives"):
            continue
        fiche = dict(f)
        fiche["id"] = ident
        fiche["reussite"] = round(100 * f.get("bonnes", 0) / f["tentatives"])
        fiches.append(fiche)
    fiches.sort(key=lambda f: (-f.get("points", 0), -f.get("reussite", 0),
                               f.get("nom", "")))
    return [(i + 1, f) for i, f in enumerate(fiches[:limite])]


def fiche(joueur_id):
    """Le score de quelqu'un, meme s'il n'a jamais joue."""
    brut = lire_pokemon().get("scores", {}).get(str(joueur_id))
    if not brut:
        return {"nom": "", "points": 0, "bonnes": 0, "tentatives": 0,
                "serie": 0, "record": 0, "tapes": 0, "reussite": 0, "rang": None}
    fiche = dict(brut)
    fiche["reussite"] = round(100 * brut.get("bonnes", 0) /
                              max(1, brut.get("tentatives", 0)))
    fiche["rang"] = next((r for r, f in classement_pokemon(999)
                          if f["id"] == str(joueur_id)), None)
    return fiche


def total_joue():
    """Combien de quiz ont ete poses : de quoi dire « la 42e question »."""
    return len(lire_pokemon().get("quiz", []))


# --- Le repli en texte -------------------------------------------------------------
def bloc_classement(limite=10):
    """Le classement en lignes Markdown, quand l'image manque."""
    lignes = []
    for rang, f in classement_pokemon(limite):
        medaille = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rang, f"**{rang}.**")
        detail = (f"{f.get('bonnes', 0)}/{f.get('tentatives', 0)} bonnes · "
                  f"{f.get('reussite', 0)} %")
        if f.get("record", 0) >= SERIE_MINIMUM:
            detail += f" · série record {f['record']}"
        lignes.append(f"{medaille} **{f.get('nom') or ANONYME}** — "
                      f"**{f.get('points', 0)} pts**\n-# {detail}")
    return lignes or ["Personne n'a encore joué. `/quiz` pour lancer la première "
                      "question."]


def bloc_resultat(quiz):
    """Ce qu'on affiche quand la reponse est revelee."""
    espece = int(quiz.get("espece", 0))
    lignes = [f"C'était **{nom(espece)}** — n°{espece}"
              + (f", {genre(espece).lower()}" if genre(espece) else "") + "."]
    reponses = quiz.get("reponses", {})
    justes = [r for r in reponses.values() if r.get("juste")]
    if not reponses:
        lignes.append("-# Personne n'a tenté sa chance.")
        return lignes
    if justes:
        premier = gagnant(quiz)
        noms = ", ".join(r.get("nom") or ANONYME for r in justes)
        lignes.append(f"✅ **{len(justes)}** sur **{len(reponses)}** ont trouvé — "
                      f"{noms}.")
        if premier:
            lignes.append(f"-# 🥇 {premier.get('nom') or ANONYME} a répondu "
                          f"le premier (+{premier.get('points', 0)} pts).")
    else:
        lignes.append(f"❌ Personne n'a trouvé, sur **{len(reponses)}** "
                      f"tentative{'s' if len(reponses) > 1 else ''}.")
    return lignes
