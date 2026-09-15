#!/usr/bin/env python3
"""
Les predictions : un jeu entre vous, pas une fonction de l'assistant.

Quelqu'un ecrit « Kevin va valider l'annee » ou « le cours de VBA de jeudi va
sauter », tout le monde vote pour ou contre, et l'auteur tranche le jour venu.
Un classement dit qui avait raison : les prophetes (ceux dont les predictions
se realisent) et les parieurs (ceux qui votent juste).

Ce module ne fait que garder les donnees sur disque et compter. Il ne sait
rien de Discord : bot.py lui passe des identifiants et des noms d'utilisateur,
et affiche ce qu'il rend.

Une prediction :

    {"id": 3, "texte": "...", "auteur_id": "1234", "auteur": "Simon",
     "cree_le": "...", "echeance": "2026-06-30" ou "", "echeance_texte": "...",
     "mise": "un kebab", "votes": {"5678": {"choix": "oui", "nom": "Lea"}},
     "resultat": null | "oui" | "non", "tranche_le": "..."}

Le vote est un toggle : revoter la meme chose retire le vote, voter l'autre
chose le change. Seul l'auteur tranche ou supprime sa prediction — c'est son
pari, et c'est lui qui devra assumer le kebab.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import config

FICHIER = config.DONNEES / "predictions.json"


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
    return next((p for p in liste if str(p.get("id")) == str(ident)), None)


trouver = _trouver


def ajouter(texte, auteur_id, auteur, echeance="", echeance_texte="", mise=""):
    liste = lire()
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
    ecrire(liste)
    return p


def voter(ident, user_id, nom, choix):
    """Vote « oui » ou « non ». Le meme vote deux fois = plus de vote.

    Rend la prediction, ou None si elle n'existe pas ou est deja tranchee."""
    liste = lire()
    p = _trouver(liste, ident)
    if p is None or p.get("resultat"):
        return None
    votes = p.setdefault("votes", {})
    actuel = (votes.get(str(user_id)) or {}).get("choix")
    if actuel == choix:
        votes.pop(str(user_id), None)
    else:
        votes[str(user_id)] = {"choix": choix, "nom": str(nom)[:60]}
    ecrire(liste)
    return p


def trancher(ident, resultat, par_id):
    """L'auteur dit si c'est arrive (« oui ») ou non (« non »).

    Rend (prediction, "ok" | "absente" | "pas_auteur" | "deja")."""
    liste = lire()
    p = _trouver(liste, ident)
    if p is None:
        return None, "absente"
    if str(p.get("auteur_id")) != str(par_id):
        return p, "pas_auteur"
    if p.get("resultat"):
        return p, "deja"
    p["resultat"] = resultat
    p["tranche_le"] = datetime.now().isoformat(timespec="seconds")
    ecrire(liste)
    return p, "ok"


def supprimer(ident, par_id):
    """Rend (prediction, "ok" | "absente" | "pas_auteur")."""
    liste = lire()
    p = _trouver(liste, ident)
    if p is None:
        return None, "absente"
    if str(p.get("auteur_id")) != str(par_id):
        return p, "pas_auteur"
    liste.remove(p)
    ecrire(liste)
    return p, "ok"


# --- Lire ce qu'il y a -------------------------------------------------------
def ouvertes(liste=None):
    liste = liste if liste is not None else lire()
    return [p for p in liste if not p.get("resultat")]


def closes(liste=None):
    liste = liste if liste is not None else lire()
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
def classement(liste=None):
    """(prophetes, parieurs, stats).

    prophetes : [(nom, realisees, tentees)] — les auteurs, par predictions
                qui se sont realisees ;
    parieurs  : [(nom, bons, votes)] — les votants, par votes justes ;
    stats     : {"ouvertes", "tranchees", "realisees", "reussite_votes"}.
    """
    liste = liste if liste is not None else lire()
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
