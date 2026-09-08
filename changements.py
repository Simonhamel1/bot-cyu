#!/usr/bin/env python3
"""
Comparer deux versions de l'emploi du temps, et dire ce qui a VRAIMENT change.

Le probleme que ce module resout
--------------------------------
CELCAT ne publie pas de « modifications » : il republie l'emploi du temps en
entier, avec des identifiants d'evenements regeneres a chaque fois. Comparer
naivement deux versions donne donc, pour un simple cours decale d'une heure :

    Disparu  : mardi 08:00-09:30 Corporate finance
    Nouveau  : mardi 09:00-10:30 Corporate finance

Deux lignes sans rapport apparent, a toi de faire le rapprochement a l'oeil. Et
un changement de salle ressort exactement pareil, alors que ca n'a rien a voir.

Ce module rapproche les deux cotes par EMPREINTE (le module et le type de
seance, voir Cours.empreinte) et sait alors dire, en une ligne :

    🕘 Corporate finance passe de 08:00 a 09:00, mardi 9 (+1 h)
    🚪 VBA change de salle : FER FT 104 → FER FT 210

Le rapprochement se fait du plus sur au moins sur : meme creneau d'abord, meme
jour ensuite, meme semaine enfin. Un cours ne peut etre apparie qu'une fois.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import vue

# Les types de changement, du plus au moins important. L'ordre sert a trier
# l'affichage : ce qui te fait rater un cours passe en premier.
ANNULE = "annule"
DEPLACE_JOUR = "deplace_jour"
DEPLACE_HEURE = "deplace_heure"
SALLE = "salle"
PROF = "prof"
DUREE = "duree"
AJOUTE = "ajoute"

ORDRE = [ANNULE, DEPLACE_JOUR, DEPLACE_HEURE, SALLE, DUREE, PROF, AJOUTE]

ENTETES = {
    ANNULE:        ("❌", "Annulé"),
    DEPLACE_JOUR:  ("📅", "Déplacé à un autre jour"),
    DEPLACE_HEURE: ("🕘", "Déplacé dans la journée"),
    SALLE:         ("🚪", "Changement de salle"),
    DUREE:         ("⏱️", "Durée modifiée"),
    PROF:          ("👤", "Changement d'intervenant"),
    AJOUTE:        ("➕", "Ajouté"),
}

# Le meme libelle au pluriel : « 3 changements de salle » ne s'ecrit pas en
# ajoutant un s a la fin de « Changement de salle ».
PLURIELS = {
    ANNULE:        "Annulés",
    DEPLACE_JOUR:  "Déplacés à un autre jour",
    DEPLACE_HEURE: "Déplacés dans la journée",
    SALLE:         "Changements de salle",
    DUREE:         "Durées modifiées",
    PROF:          "Changements d'intervenant",
    AJOUTE:        "Ajoutés",
}


def libelle(type_, combien=1):
    """Le nom d'un type de changement, accorde au nombre."""
    return PLURIELS[type_] if combien > 1 else ENTETES[type_][1]


# Un changement de salle ou de prof dans trois semaines ne merite pas de te
# mentionner ; une annulation demain, si. Ce qui suit dit ce qui est « grave »
# independamment de la date.
GRAVES = {ANNULE, DEPLACE_JOUR, DEPLACE_HEURE}


@dataclass(frozen=True)
class Changement:
    type: str
    avant: object | None      # un Cours, ou None pour un ajout
    apres: object | None      # un Cours, ou None pour une annulation

    @property
    def cours(self):
        """Le cours a considerer : le nouveau s'il existe, sinon l'ancien."""
        return self.apres or self.avant

    @property
    def jours(self):
        """Les dates concernees : deux pour un deplacement d'un jour a l'autre."""
        return {c.jour for c in (self.avant, self.apres) if c is not None}


# --- Le rapprochement --------------------------------------------------------
def _apparier(partis, venus):
    """(paires, restes_partis, restes_venus).

    Trois passes, de la plus sure a la moins sure. Un cours apparie sort du jeu
    tout de suite : sans ca, deux seances de la meme matiere le meme jour
    pourraient s'apparier en croix et produire un message absurde.
    """
    paires = []
    libres_partis = list(partis)
    libres_venus = list(venus)

    def passe(compatible, distance):
        for parti in list(libres_partis):
            candidats = [v for v in libres_venus
                         if v.empreinte() == parti.empreinte() and compatible(parti, v)]
            if not candidats:
                continue
            venu = min(candidats, key=lambda v: distance(parti, v))
            paires.append((parti, venu))
            libres_partis.remove(parti)
            libres_venus.remove(venu)

    # 1. Meme creneau exact : ce n'est donc ni un deplacement ni une annulation,
    #    c'est la salle, le prof ou la duree qui a bouge.
    passe(lambda a, b: a.debut == b.debut, lambda a, b: 0)

    # 2. Meme jour : le cours a ete decale dans la journee.
    passe(lambda a, b: a.jour == b.jour,
          lambda a, b: abs((b.debut - a.debut).total_seconds()))

    # 3. A moins de dix jours : le cours a change de jour. Au-dela, c'est plus
    #    vraisemblablement une seance differente du meme module, et l'annoncer
    #    comme un deplacement induirait en erreur.
    passe(lambda a, b: abs((b.debut - a.debut).days) <= 10,
          lambda a, b: abs((b.debut - a.debut).total_seconds()))

    return paires, libres_partis, libres_venus


def _qualifier(avant, apres):
    """Le type de changement d'une paire deja appariee, ou None si identique."""
    if avant.jour != apres.jour:
        return DEPLACE_JOUR
    if avant.debut != apres.debut:
        return DEPLACE_HEURE
    if (avant.salle or "") != (apres.salle or ""):
        return SALLE
    if avant.minutes != apres.minutes:
        return DUREE
    if (avant.prof or "") != (apres.prof or ""):
        return PROF
    return None


def comparer(avant, apres):
    """La liste des Changement entre deux etats de l'emploi du temps.

    `avant` et `apres` sont des dicts {cle: Cours} ou des iterables de Cours.
    """
    def index(source):
        valeurs = source.values() if isinstance(source, dict) else source
        return {c.cle(): c for c in valeurs if c.est_cours}

    a, b = index(avant), index(apres)
    partis = [c for k, c in a.items() if k not in b]
    venus = [c for k, c in b.items() if k not in a]
    if not partis and not venus:
        return []

    paires, restes_partis, restes_venus = _apparier(partis, venus)

    sortie = [Changement(t, x, y) for x, y in paires
              if (t := _qualifier(x, y)) is not None]
    sortie += [Changement(ANNULE, c, None) for c in restes_partis]
    sortie += [Changement(AJOUTE, None, c) for c in restes_venus]

    return sorted(sortie, key=lambda ch: (ORDRE.index(ch.type), ch.cours.debut))


# --- Mise en forme -----------------------------------------------------------
def _quand(c):
    return f"{vue.jour_fr(c.jour, court=True)} `{c.creneau}`"


def _decalage(avant, apres):
    """« +1 h », « -30 min » : de combien le cours a bouge, signe compris."""
    minutes = (apres.debut - avant.debut).total_seconds() / 60
    signe = "+" if minutes > 0 else "-"
    return f"{signe}{vue.duree_fr(minutes)}"


def decrire(ch):
    """Une a deux lignes decrivant un changement, en markdown Discord."""
    c = ch.cours
    tete = f"**{c.titre}**" + (f" ({c.type_court})" if c.type_court else "")

    if ch.type == ANNULE:
        return [f"{tete} — {_quand(ch.avant)}"]

    if ch.type == AJOUTE:
        return [f"{tete} — {_quand(ch.apres)} · {ch.apres.ou}"]

    if ch.type == DEPLACE_JOUR:
        return [f"{tete}",
                f"⠀⠀{vue.jour_fr(ch.avant.jour, court=True)} `{ch.avant.creneau}`"
                f"  ➜  **{vue.jour_fr(ch.apres.jour, court=True)}** "
                f"`{ch.apres.creneau}` · {ch.apres.ou}"]

    if ch.type == DEPLACE_HEURE:
        return [f"{tete} — {vue.jour_fr(ch.apres.jour, court=True)}",
                f"⠀⠀`{ch.avant.creneau}`  ➜  **`{ch.apres.creneau}`**  "
                f"({_decalage(ch.avant, ch.apres)}) · {ch.apres.ou}"]

    if ch.type == SALLE:
        return [f"{tete} — {_quand(ch.apres)}",
                f"⠀⠀{ch.avant.ou or 'salle inconnue'}  ➜  **{ch.apres.ou}**"]

    if ch.type == DUREE:
        return [f"{tete} — {vue.jour_fr(ch.apres.jour, court=True)}",
                f"⠀⠀`{ch.avant.creneau}`  ➜  **`{ch.apres.creneau}`**  "
                f"({vue.duree_fr(ch.avant.minutes)} ➜ {vue.duree_fr(ch.apres.minutes)})"]

    if ch.type == PROF:
        return [f"{tete} — {_quand(ch.apres)}",
                f"⠀⠀{ch.avant.prof.title() or 'inconnu'}  ➜  "
                f"**{ch.apres.prof.title() or 'inconnu'}**"]

    return [f"{tete} — {_quand(c)}"]


def urgents(liste, aujourd=None):
    """Les changements qui touchent aujourd'hui ou demain ET qui font rater un
    cours. C'est le seul cas ou l'assistant a le droit de te mentionner."""
    aujourd = aujourd or date.today()
    proches = {aujourd, aujourd + timedelta(days=1)}
    return [ch for ch in liste if ch.type in GRAVES and (ch.jours & proches)]


def bloc(liste, aujourd=None):
    """(lignes, urgent) prets a envoyer, ou ([], False) s'il n'y a rien.

    Les changements sont groupes par type, avec un en-tete par groupe : lire
    « 3 cours annules » d'un bloc vaut mieux que trois lignes melangees a des
    changements de salle.
    """
    if not liste:
        return [], False
    aujourd = aujourd or date.today()
    presses = urgents(liste, aujourd)

    lignes = []
    if presses:
        quand = sorted({j for ch in presses for j in ch.jours} &
                       {aujourd, aujourd + timedelta(days=1)})
        moment = " et ".join("aujourd'hui" if j == aujourd else "demain"
                             for j in quand)
        lignes += [f"⚠️ **Ça touche {moment}.**", ""]

    for type_ in ORDRE:
        groupe = [ch for ch in liste if ch.type == type_]
        if not groupe:
            continue
        icone = ENTETES[type_][0]
        compte = f" ({len(groupe)})" if len(groupe) > 1 else ""
        lignes.append(f"{icone} **{libelle(type_, len(groupe))}{compte}**")
        for ch in groupe[:8]:
            lignes += ["⠀⠀" + l if not l.startswith("⠀") else l
                       for l in decrire(ch)]
        if len(groupe) > 8:
            lignes.append(f"⠀⠀_… et {len(groupe) - 8} autres_")
        lignes.append("")

    return [l for l in lignes[:-1]], bool(presses)


def titre(liste):
    """Le titre du message, qui dit deja l'essentiel dans la notification."""
    if not liste:
        return "Emploi du temps inchange"
    if len(liste) == 1:
        ch = liste[0]
        _, mot = ENTETES[ch.type]
        return f"{mot} : {ch.cours.titre}"
    types = {ch.type for ch in liste}
    if types == {AJOUTE}:
        return f"{len(liste)} cours ajoutes"
    if types == {SALLE}:
        return f"{len(liste)} changements de salle"
    return f"{len(liste)} changements d'emploi du temps"


def couleur(liste, aujourd=None):
    """La couleur de l'embed : rouge seulement si ca te concerne tout de suite."""
    if urgents(liste, aujourd):
        return "alerte"
    if any(ch.type in GRAVES for ch in liste):
        return "devoir"          # jaune : important, mais pas pour tout de suite
    return "info"


# --- Premiere publication ----------------------------------------------------
def premiere_publication(avant, apres, seuil=6):
    """L'emploi du temps SORT-il pour la premiere fois sur une periode vide ?

    Sans ce test, la publication d'un semestre entier arriverait sous la forme
    d'un message « 47 cours ajoutes » illisible, alors que la vraie nouvelle est
    « l'emploi du temps est sorti ». C'est justement l'evenement que tu attends
    quand tu regardes trois semaines en avant.
    """
    anciens = {c.jour for c in (avant.values() if isinstance(avant, dict) else avant)
               if c.est_cours}
    nouveaux = [c for c in (apres.values() if isinstance(apres, dict) else apres)
                if c.est_cours and c.jour not in anciens]
    if len(nouveaux) < seuil:
        return None
    jours = sorted({c.jour for c in nouveaux})
    return {"cours": sorted(nouveaux, key=lambda c: c.debut),
            "jours": jours,
            "du": jours[0], "au": jours[-1]}
