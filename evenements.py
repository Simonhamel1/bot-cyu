#!/usr/bin/env python3
"""
Les examens en EVENEMENTS DISCORD.

Discord a un calendrier a lui : le bandeau « evenements » en haut du serveur,
avec un compte a rebours, une cloche « interesse » et un rappel envoye par
Discord lui-meme un quart d'heure avant. Un examen y a exactement sa place.

Toutes les heures, le bot compare les examens connus (CELCAT et le carnet de
devoirs, via stats.examens) aux evenements qu'il a deja crees, et cree, met a
jour ou retire ce qu'il faut. Il ne touche qu'aux evenements qu'il a crees
lui-meme : leurs identifiants sont ici, sur disque.

Ce module calcule ce qui DEVRAIT exister ; bot.py parle a Discord.

    {"celcat:2026-10-12:econometrie": {"id": "9876", "empreinte": "..."}}
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

import config
import stats
from celcat import normaliser

FICHIER = config.FICHIER_EVENEMENTS

# Un examen qui commence dans moins de deux minutes n'est plus a creer :
# Discord refuse un evenement dans le passe, et la marge evite de le rater
# de justesse.
MARGE = timedelta(minutes=2)


def lire():
    try:
        data = json.loads(FICHIER.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def ecrire(data):
    config.preparer_dossiers()
    FICHIER.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _cle(e):
    return f"{e['source']}:{e['quand']:%Y-%m-%d}:{normaliser(e['matiere'] or e['titre'])[:40]}"


def _nom(e):
    titre = str(e["titre"] or e["matiere"] or "Examen").strip()
    matiere = str(e["matiere"] or "").strip()
    if matiere and normaliser(matiere) not in normaliser(titre):
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
    for e in stats.examens(cours, liste_devoirs or [], maintenant):
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
