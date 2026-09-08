#!/usr/bin/env python3
"""
Le carnet de devoirs, et surtout son lien avec l'emploi du temps.

L'idee centrale : une echeance ne s'ecrit pas toujours en date. « a rendre au
prochain cours de maths » est plus naturel, et c'est justement ce que le script
sait resoudre, puisqu'il connait ton emploi du temps.

    resoudre_echeance("prochain:maths", cours)  ->  "2026-09-08T10:15"

Un devoir est un simple dictionnaire, stocke en JSON :

    {"id": 3, "titre": "DM 2", "matiere": "maths",
     "echeance": "2026-09-08T10:15", "type": "dm", "fait": false,
     "note": "exercices 4 a 9", "cree_le": "2026-09-01T18:22:07"}
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

import config
from celcat import _dt, normaliser

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


# ─── Lecture et ecriture ─────────────────────────────────────────────────────
def lire():
    try:
        data = json.loads(config.FICHIER_DEVOIRS.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (ValueError, OSError):
        return []


def ecrire(liste):
    config.preparer_dossiers()
    config.FICHIER_DEVOIRS.write_text(
        json.dumps(liste, ensure_ascii=False, indent=1), encoding="utf-8")


def ajouter(titre, matiere, echeance, type_="devoir", note=""):
    liste = lire()
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
    ecrire(liste)
    return devoir


def marquer_fait(ident):
    """True si le devoir existait, False sinon."""
    liste = lire()
    for d in liste:
        if str(d.get("id")) == str(ident):
            d["fait"] = True
            ecrire(liste)
            return True
    return False


def supprimer(ident):
    """Le devoir supprime, ou None s'il n'existait pas."""
    liste = lire()
    for i, d in enumerate(liste):
        if str(d.get("id")) == str(ident):
            liste.pop(i)
            ecrire(liste)
            return d
    return None


# ─── Echeances ───────────────────────────────────────────────────────────────
def echeance_dt(d):
    """Un devoir sans heure est du pour la fin de la journee, pas pour minuit :
    sinon il apparait en retard des le matin meme."""
    brut = str(d.get("echeance") or "")
    if not brut:
        return None
    val = _dt(brut)
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
    sortie = [d for d in (liste if liste is not None else lire()) if not d.get("fait")]
    if horizon_jours is not None:
        sortie = [d for d in sortie
                  if (r := jours_restants(d)) is not None and r <= horizon_jours]
    return sorted(sortie, key=lambda d: (echeance_dt(d) or datetime.max))


# ─── Le lien avec les cours ──────────────────────────────────────────────────
def cles_matiere(matiere):
    """Mots qui rattachent un devoir a un cours : la matiere, ses alias, et sa
    forme sans pluriel — sans ca, « maths » ne trouverait pas « MATH101 »."""
    base = normaliser(matiere)
    if not base:
        return []
    cles = {base}
    if base.endswith("s") and len(base) > 4:
        cles.add(base[:-1])
    for canon, variantes in (config.MATIERES or {}).items():
        noms = {normaliser(canon)} | {normaliser(v) for v in (variantes or [])}
        if cles & noms:
            cles |= noms
    return [c for c in cles if len(c) >= 3]


def cours_correspond(cours, cles):
    cible = normaliser(f"{cours.module} {cours.titre}")
    return any(k in cible for k in cles)


def du_cours(cours, liste=None):
    """Les devoirs a rendre pour ce cours precis : meme matiere, echeance ce
    jour-la. C'est ce qui fait apparaitre « a rendre : DM 2 » sous le cours."""
    sortie = []
    for d in (liste if liste is not None else lire()):
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
    bas = normaliser(brut)
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

    val = _dt(brut)
    if val:
        return val.isoformat(timespec="minutes") if len(brut) > 10 else val.date().isoformat()
    raise ValueError(f"echeance incomprise : « {brut} » (essaie 12/09, demain, "
                     f"lundi, +3, ou prochain:maths)")
