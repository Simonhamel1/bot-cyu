#!/usr/bin/env python3
"""
Tout ce qui met en forme et envoie : le texte (vue), les cartes Discord (interface), l'aiguillage vers les salons (notif), et les tableaux vivants de #statut et #edt.

Sommaire :

    La mise en forme du texte                     (ex-vue.py)
    Les cartes Discord                            (ex-interface.py)
    L'aiguillage Discord                          (ex-notif.py)
    Les tableaux vivants (#statut, #edt)          (ex-statut.py)
"""

from __future__ import annotations

import io
import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import discord
import requests

import config
import edt
import image
import promo


# ════════════════════════════════════════════════════════════════════════════
#  LA MISE EN FORME DU TEXTE  (ex-vue.py)
# ════════════════════════════════════════════════════════════════════════════
# La mise en forme : transformer des cours et des devoirs en texte lisible.
#
# Toutes les fonctions rendent une liste de lignes en markdown Discord. Le
# terminal reutilise les memes, en passant par sans_markdown(). Un seul endroit
# decide donc de l'allure des messages, que ce soit le daemon, le bot ou la ligne
# de commande qui les affiche.
#
# Deux principes tenus partout ici :
#
#   * l'heure d'abord. Une ligne commence toujours par son creneau, en chasse
#     fixe, pour que la colonne des heures soit droite d'un bout a l'autre du
#     message ;
#   * l'indentation se fait avec U+2800 (braille vide), pas avec des espaces.
#     Discord mange les espaces multiples en debut de ligne, pas celui-la.


JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
JOURS_COURTS = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
MOIS = ["janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet",
        "aout", "septembre", "octobre", "novembre", "decembre"]

# Discord mange les espaces en debut de ligne, mais garde celui-la.
BLANC = "⠀"
RETRAIT = BLANC * 4


# --- Petits formateurs -------------------------------------------------------
def hhmm(txt, defaut=(0, 0)):
    m = re.match(r"^\s*(\d{1,2})\s*[:hH]\s*(\d{2})?\s*$", str(txt or ""))
    return (int(m.group(1)), int(m.group(2) or 0)) if m else defaut


def a_heure(jour, txt, defaut=(0, 0)):
    """Un datetime pour « ce jour-la, a telle heure »."""
    h, m = hhmm(txt, defaut)
    return datetime.combine(jour, datetime.min.time()).replace(hour=h, minute=m)


def jour_fr(d, court=False):
    if court:
        return f"{JOURS_COURTS[d.weekday()]}. {d:%d/%m}"
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]}"


def jour_relatif(d, ref=None):
    """« aujourd'hui », « demain », sinon le jour en toutes lettres.

    C'est ce qui rend un titre de message immediatement comprehensible : on lit
    « demain » beaucoup plus vite que « mercredi 10 septembre »."""
    ref = ref or date.today()
    ecart = (d - ref).days
    if ecart == 0:
        return "aujourd'hui"
    if ecart == 1:
        return "demain"
    if ecart == -1:
        return "hier"
    if 2 <= ecart <= 6:
        return JOURS[d.weekday()]
    return jour_fr(d)


def duree_fr(minutes):
    h, m = divmod(int(round(abs(minutes))), 60)
    if h and m:
        return f"{h} h {m:02d}"
    return f"{h} h" if h else f"{m} min"


def duree_longue(minutes):
    """Comme duree_fr, mais bascule en jours au-dela de deux jours.

    « lu il y a 152 h » ne veut rien dire pour un lecteur ; « lu il y a
    6 jours » se comprend sans calculer."""
    minutes = abs(minutes)
    if minutes < 60 * 48:
        return duree_fr(minutes)
    jours = int(minutes // (60 * 24))
    return f"{jours} jours"


def compte_a_rebours(cible, ref=None):
    """« dans 12 min », « dans 3 h 20 », « dans 2 jours ». Jamais une date.

    Utilise partout ou la question est « c'est dans combien de temps ? » :
    le prochain cours, le message de statut, les echeances proches."""
    ref = ref or datetime.now()
    minutes = (cible - ref).total_seconds() / 60
    if minutes < 0:
        return f"il y a {duree_fr(-minutes)}"
    if minutes < 1:
        return "maintenant"
    if minutes < 60:
        return f"dans {int(minutes)} min"
    if minutes < 60 * 20:
        return f"dans {duree_fr(minutes)}"
    jours = int(minutes // (60 * 24))
    return f"dans {jours} jour" + ("s" if jours > 1 else "")


def lire_date(txt, cours=None, defaut=None):
    """« 12/10 », « demain », « lundi », « +7 », « 2026-10-12 » -> une date.

    Meme grammaire que l'echeance d'un devoir : une seule facon d'ecrire une
    date dans tout le projet, qu'on parle d'un rendu ou d'une plage d'emploi
    du temps. Leve ValueError si c'est incomprehensible.
    """
    txt = str(txt or "").strip()
    if not txt:
        return defaut
    brut = promo.resoudre_echeance(txt, cours or [], "")
    return date.fromisoformat(str(brut)[:10])


def sans_markdown(txt):
    """Le terminal n'a pas de markdown : on enleve juste le bruit."""
    return re.sub(r"\*\*|__|~~|`", "", str(txt)).replace(BLANC, " ")


def heure_lever(premier_cours):
    """(lever, depart) conseilles pour arriver a l'heure au premier cours.

    Un cours a distance ne demande pas de trajet : compter 45 min de RER pour
    un cours en visio ferait lever une heure trop tot."""
    trajet = 0 if premier_cours.a_distance else config.TRAJET_MINUTES
    depart = premier_cours.debut - timedelta(minutes=trajet)
    return depart - timedelta(minutes=config.PREPARATION_MINUTES), depart


# --- Devoirs -----------------------------------------------------------------
def ligne_devoir(d, avec_id=True):
    """Une ligne de devoir, avec son urgence lisible d'un coup d'oeil."""
    ech = promo.echeance_dt(d)
    reste = promo.jours_restants(d)
    if reste is None:
        quand, urgence = "sans echeance", ""
    elif reste < 0:
        quand, urgence = f"**EN RETARD de {-reste} j**", " 🔴"
    elif reste == 0:
        quand, urgence = "**pour aujourd'hui**", " 🔥"
    elif reste == 1:
        quand, urgence = "**pour demain**", " ⚠️"
    elif reste <= 3:
        quand, urgence = f"dans {reste} j ({jour_fr(ech.date(), court=True)})", " 🟠"
    else:
        quand, urgence = f"dans {reste} j ({jour_fr(ech.date(), court=True)})", ""
    if ech is not None and ech.strftime("%H:%M") not in ("23:59", "00:00"):
        quand += f" a {ech:%H:%M}"

    tete = f"`#{d['id']}` " if avec_id else "• "
    matiere = f"**{d['matiere']}** · " if d.get("matiere") else ""
    barre = "~~" if d.get("fait") else ""
    note = f"\n{RETRAIT}↳ {d['note']}" if d.get("note") else ""
    return f"{tete}{barre}{matiere}{d['titre']}{barre} — {quand}{urgence}{note}"


def bloc_devoirs(liste=None, titre="📚 Devoirs", horizon=None, vide=None):
    liste = liste if liste is not None else promo.lire_devoirs()
    restants = promo.actifs(liste, horizon)
    if not restants:
        return [f"**{titre}**", vide or "Rien en attente. 😌"]
    return [f"**{titre}**"] + [ligne_devoir(d) for d in restants]


# --- Une journee -------------------------------------------------------------
def _etat_cours(c, maintenant):
    """Le pictogramme de progression d'un cours dans la journee en cours.

    Voir d'un coup d'oeil ou on en est vaut mieux que relire les heures : le
    cours en cours est marque, ceux qui sont passes sont grises."""
    if c.fin and maintenant >= c.fin:
        return "fini"
    if c.debut <= maintenant:
        return "encours"
    return "avenir"


def bloc_journee(cours, liste_devoirs, jour, avec_reveil=False, maintenant=None):
    """Les lignes d'une journee : cours, trous, devoirs rattaches, heure de lever."""
    maintenant = maintenant or datetime.now()
    jc = edt.du_jour(cours, jour)

    if not jc:
        autres = edt.non_cours_du_jour(cours, jour)
        motif = f" — {autres[0].titre}" if autres else ""
        return [f"**{jour_fr(jour).capitalize()}**",
                f"Aucun cours{motif}. 🎉"]

    total = sum(c.minutes for c in jc)
    amplitude = (jc[-1].fin or jc[-1].debut) - jc[0].debut
    lignes = [
        f"**{jour_fr(jour).capitalize()}**",
        f"`{jc[0].debut:%H:%M}` → `{(jc[-1].fin or jc[-1].debut):%H:%M}` · "
        f"{len(jc)} cours · {duree_fr(total)} de cours"
        + (f" sur {duree_fr(amplitude.total_seconds() / 60)} de presence"
           if amplitude.total_seconds() / 60 - total >= config.TROU_MINUTES else ""),
        "",
    ]

    salles = {c.ou for c in jc if not c.a_distance}
    if len(jc) > 1 and len(salles) == 1 and salles != {"salle inconnue"}:
        lignes.append(f"📍 Tout se passe en **{salles.pop()}**.")
        lignes.append("")

    est_aujourdhui = jour == maintenant.date()
    precedent = None
    for c in jc:
        if precedent and precedent.fin:
            creux = (c.debut - precedent.fin).total_seconds() / 60
            if creux >= config.TROU_MINUTES:
                lignes.append(f"{RETRAIT}⏸ **{duree_fr(creux)}** de trou "
                              f"({precedent.fin:%H:%M} → {c.debut:%H:%M})")

        etat = _etat_cours(c, maintenant) if est_aujourdhui else "avenir"
        if etat == "fini":
            # Barre : ce qui est derriere toi ne doit plus accrocher l'oeil.
            lignes.append(f"`{c.creneau}` ~~{c.titre}~~")
        else:
            marque = "▶️ " if etat == "encours" else ""
            detail = " · ".join(b for b in (c.type_court, c.ou,
                                            c.prof.title() if c.prof else "") if b)
            lignes.append(f"`{c.creneau}` {marque}{c.icone} **{c.titre}**")
            if detail:
                lignes.append(f"{RETRAIT}└ {detail}")
            if etat == "avenir" and est_aujourdhui:
                lignes[-1] += f"  ·  _{compte_a_rebours(c.debut, maintenant)}_"

        for d in promo.du_cours(c, liste_devoirs):
            lignes.append(f"{RETRAIT}📌 **a rendre** : {d['titre']}")
        precedent = c

    if avec_reveil:
        lever, depart = heure_lever(jc[0])
        if jc[0].a_distance:
            lignes += ["", f"💻 Premier cours a distance : reveil **{lever:%H:%M}**, "
                       f"pas de trajet."]
        else:
            lignes += ["", f"🔔 Lever **{lever:%H:%M}** · depart **{depart:%H:%M}** "
                       f"· cours a {jc[0].debut:%H:%M}"]
    return lignes


# --- Le prochain cours -------------------------------------------------------
def bloc_prochain(cours, liste_devoirs=None, maintenant=None):
    """Le cours en cours et/ou le prochain : la reponse a « et maintenant ? »."""
    maintenant = maintenant or datetime.now()
    actuel = edt.en_cours(cours, maintenant)
    suivant = edt.prochain(cours, maintenant)

    lignes = []
    if actuel:
        reste = (actuel.fin - maintenant).total_seconds() / 60 if actuel.fin else 0
        lignes += [
            f"▶️ **En ce moment — {actuel.titre}**",
            f"`{actuel.creneau}` · {actuel.ou}"
            + (f" · {actuel.prof.title()}" if actuel.prof else ""),
            f"{RETRAIT}fini dans **{duree_fr(reste)}**" if reste > 0 else "",
            "",
        ]

    if not suivant:
        lignes.append("Plus aucun cours prevu dans l'horizon connu. 🎉")
        return [l for l in lignes if l != ""] or ["Rien de prevu. 🎉"]

    ecart = jour_relatif(suivant.jour, maintenant.date())
    lignes += [
        f"⏭️ **Prochain — {suivant.titre}**",
        f"`{suivant.creneau}` {ecart} · **{compte_a_rebours(suivant.debut, maintenant)}**",
        f"{RETRAIT}{suivant.icone} {suivant.type_court or 'cours'} · **{suivant.ou}**"
        + (f" · {suivant.prof.title()}" if suivant.prof else ""),
    ]

    if not suivant.a_distance and not actuel:
        lever, depart = heure_lever(suivant)
        if depart > maintenant:
            lignes.append(f"{RETRAIT}🚶 partir a **{depart:%H:%M}** "
                          f"({compte_a_rebours(depart, maintenant)})")

    liees = promo.du_cours(suivant, liste_devoirs)
    if liees:
        lignes += ["", "📌 **A rendre pour ce cours**"] + \
                  [ligne_devoir(d, avec_id=False) for d in liees]

    # Ce qui suit dans la meme journee : sans ca, il faut refaire /edt.
    apres = [c for c in edt.du_jour(cours, suivant.jour) if c.debut > suivant.debut]
    if apres:
        lignes += ["", f"**Ensuite {ecart}**"] + \
                  [f"`{c.creneau}` {c.titre} · {c.ou}" for c in apres[:4]]
    return lignes


# --- La grille de la semaine -------------------------------------------------
# Un caractere = 30 minutes, donc deux caracteres = une heure, et l'etiquette
# "08" fait pile deux caracteres : la colonne des heures tombe juste sans avoir
# a compter les espaces. C'est toute l'astuce de l'alignement.
PLEIN = "█"
TROU = "·"
VIDE = " "

# Couleurs ANSI comprises par Discord dans un bloc ```ansi.
ANSI = {"CM": "[0;34m", "TD": "[0;32m", "TP": "[0;36m",
        "EXAMEN": "[0;31m", "EXAM": "[0;31m", "DS": "[0;31m",
        "CONTROLE": "[0;31m", "PROJET": "[0;35m",
        "SOUTENANCE": "[0;31m", "": "[0;33m"}
ANSI_DISTANCE = "[0;35m"
ANSI_GRIS = "[0;30m"
ANSI_RESET = "[0m"


def _bornes_grille(jc_semaine):
    """(heure de debut, heure de fin) de la grille, en heures entieres.

    Les bornes de config.yaml sont un minimum, pas un maximum : un cours a 7 h
    ou un examen qui finit a 20 h doit rester visible, sinon la grille ment.
    """
    debut = hhmm(config.JOURNEE_DEBUT, (8, 0))[0]
    fin = hhmm(config.JOURNEE_FIN, (19, 0))[0]
    for c in jc_semaine:
        debut = min(debut, c.debut.hour)
        if c.fin:
            fin = max(fin, c.fin.hour + (1 if c.fin.minute else 0))
    return debut, max(fin, debut + 1)


def grille_semaine(cours, lundi, jours=None, couleurs=None):
    """L'emploi du temps de la semaine en barres, dans un bloc de code.

    Rendu (une barre = 30 min) :

        ═══ 08  10  12  14  16  18
        lun ····████··████
        mar     ██████··██████
        mer ─ libre ─

    C'est la vue qui manquait : on voit la forme de sa semaine — les journees
    chargees, les trous, le jour ou on commence tard — sans lire une seule
    heure.
    """
    jours = jours or config.GRILLE_JOURS
    couleurs = config.GRILLE_COULEURS if couleurs is None else couleurs

    dates = [lundi + timedelta(days=i) for i in range(jours)]
    par_jour = {j: edt.du_jour(cours, j) for j in dates}
    tous = [c for liste in par_jour.values() for c in liste]
    if not tous:
        return []

    h0, h1 = _bornes_grille(tous)
    creneaux = (h1 - h0) * 2

    def colonne(moment):
        """Le numero de case d'un instant, arrondi vers l'exterieur du cours."""
        return (moment.hour - h0) * 2 + (1 if moment.minute >= 30 else 0)

    entete = "".join(f"{h:02d}" if (h - h0) % 2 == 0 else "  " for h in range(h0, h1))
    lignes = [f"═══ {entete}"]

    for j in dates:
        jc = par_jour[j]
        etiquette = f"{JOURS_COURTS[j.weekday()]} "
        if not jc:
            autres = edt.non_cours_du_jour(cours, j)
            lignes.append(f"{etiquette} ─ {autres[0].titre.lower() if autres else 'libre'} ─")
            continue

        cases = [VIDE] * creneaux
        for c in jc:
            a = max(0, colonne(c.debut))
            b = min(creneaux, colonne(c.fin) if c.fin else a + 2)
            marque = c.type_court if not c.a_distance else "DIST"
            for i in range(a, max(b, a + 1)):
                cases[i] = (marque, c)
        # Les trous entre le premier et le dernier cours : c'est le temps
        # d'attente sur place, pas du temps libre.
        pleins = [i for i, v in enumerate(cases) if v != VIDE]
        for i in range(pleins[0], pleins[-1]):
            if cases[i] == VIDE:
                cases[i] = TROU

        if couleurs:
            courant, texte = None, ""
            for case in cases:
                if isinstance(case, tuple):
                    code = ANSI_DISTANCE if case[0] == "DIST" else ANSI.get(case[0], ANSI[""])
                    car = PLEIN
                elif case == TROU:
                    code, car = ANSI_GRIS, TROU
                else:
                    code, car = None, VIDE
                if code != courant:
                    texte += (ANSI_RESET if courant else "") + (code or "")
                    courant = code
                texte += car
            texte += ANSI_RESET if courant else ""
            lignes.append(f"{etiquette} {texte}".rstrip())
        else:
            plat = "".join(PLEIN if isinstance(c, tuple) else c for c in cases)
            lignes.append(f"{etiquette} {plat}".rstrip())

    langage = "ansi" if couleurs else ""
    return [f"```{langage}"] + lignes + ["```"]


def legende_grille():
    """Ce que veulent dire les couleurs. Une seule ligne, sous la grille."""
    if config.GRILLE_COULEURS:
        return ["-# 🟦 CM · 🟩 TD · 🟧 TP · 🟪 a distance · 🟥 examen · `·` trou"]
    return ["-# `█` cours · `·` trou sur place"]


def bloc_semaine(cours, lundi, detail=True):
    """La semaine : la grille, puis une ligne par jour avec les matieres."""
    lignes = []
    if config.GRILLE_SEMAINE:
        grille = grille_semaine(cours, lundi)
        if grille:
            lignes += grille + legende_grille() + [""]

    total = 0
    jours_travailles = 0
    for i in range(7):
        jour = lundi + timedelta(days=i)
        jc = edt.du_jour(cours, jour)
        minutes = sum(c.minutes for c in jc)
        total += minutes
        if not jc:
            if i < 5 or edt.non_cours_du_jour(cours, jour):
                autres = edt.non_cours_du_jour(cours, jour)
                libelle = autres[0].titre if autres else "libre"
                lignes.append(f"**{JOURS_COURTS[jour.weekday()]}. {jour:%d/%m}** — {libelle}")
            continue
        jours_travailles += 1
        if detail:
            # Le titre, pas le code du module : « Corporate finance » se lit,
            # pas « DIXA5COF(DI02J3-262) ».
            matieres = []
            for c in jc:
                brut = c.titre or c.module
                court = brut if len(brut) <= 24 else brut[:23].rstrip() + "…"
                if court not in matieres:
                    matieres.append(court)
            lignes.append(
                f"**{JOURS_COURTS[jour.weekday()]}. {jour:%d/%m}** "
                f"`{jc[0].debut:%H:%M}→{(jc[-1].fin or jc[-1].debut):%H:%M}` "
                f"({duree_fr(minutes)}) · " + ", ".join(matieres))

    if total:
        moyenne = total / max(jours_travailles, 1)
        lignes.append("")
        lignes.append(f"**Total : {duree_fr(total)}** sur {jours_travailles} jours "
                      f"({duree_fr(moyenne)} par jour travaille).")
    return lignes


def tableau_semaine(cours, lundi=None, liste_devoirs=None):
    """Le message du salon #edt : la semaine complete, remise a jour tout seule.

    Volontairement autonome : c'est le seul message qu'on peut consulter sans
    rien taper, il doit donc contenir la grille, le detail, et ce qui arrive
    ensuite."""
    lundi = lundi or edt.semaine_de(date.today())
    lignes = bloc_semaine(cours, lundi)

    suivant = edt.prochain(cours)
    if suivant:
        lignes += ["", f"⏭️ **Prochain** — `{suivant.creneau}` "
                   f"{jour_relatif(suivant.jour)} · {suivant.titre} · "
                   f"{suivant.ou} ({compte_a_rebours(suivant.debut)})"]

    restants = promo.actifs(liste_devoirs, 7)
    if restants:
        lignes += ["", f"📚 **{len(restants)} devoir"
                   f"{'s' if len(restants) > 1 else ''} sous 7 jours** — "
                   + ", ".join(d["titre"][:28] for d in restants[:4])]
    return lignes


# --- Creneaux libres ---------------------------------------------------------
def creneaux_libres(cours, jours=7, depuis=None):
    """Les trous exploitables des N prochains jours : revisions, sport, sieste."""
    depuis = depuis or datetime.now()
    mini = config.CRENEAU_LIBRE_MINUTES
    lignes = []
    for delta in range(jours):
        jour = depuis.date() + timedelta(days=delta)
        borne_debut = max(a_heure(jour, config.JOURNEE_DEBUT, (8, 0)), depuis)
        borne_fin = a_heure(jour, config.JOURNEE_FIN, (19, 0))
        if borne_debut >= borne_fin:
            continue

        libres = []
        curseur = borne_debut
        for c in edt.du_jour(cours, jour):
            if c.debut > curseur and (c.debut - curseur).total_seconds() / 60 >= mini:
                libres.append((curseur, min(c.debut, borne_fin)))
            curseur = max(curseur, c.fin or c.debut)
        if borne_fin > curseur and (borne_fin - curseur).total_seconds() / 60 >= mini:
            libres.append((curseur, borne_fin))

        libres = [(a, b) for a, b in libres
                  if b > a and (b - a).total_seconds() / 60 >= mini]
        if libres:
            total = sum((b - a).total_seconds() / 60 for a, b in libres)
            detail = " · ".join(
                f"`{a:%H:%M}-{b:%H:%M}` ({duree_fr((b - a).total_seconds() / 60)})"
                for a, b in libres)
            lignes.append(f"**{jour_fr(jour, court=True)}** ({duree_fr(total)}) "
                          f"{detail}")
    return lignes or ["Aucun creneau libre significatif. Courage. 🫠"]


# --- Export .ics -------------------------------------------------------------
def _ics_txt(t):
    return re.sub(r"[\r\n]+", " ", str(t or "")).replace("\\", "\\\\") \
        .replace(",", "\\,").replace(";", "\\;")


def exporter_ics(cours, liste_devoirs, chemin):
    """Fichier .ics importable dans Google Agenda ou l'appli du telephone.

    Les heures sont ecrites en heure locale flottante (pas de Z) : CELCAT donne
    des heures locales, les convertir en UTC ferait decaler l'ete et l'hiver.
    """
    lignes = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//assistant-cyu//FR",
              "CALSCALE:GREGORIAN", "X-WR-CALNAME:CYU"]
    for c in cours:
        fin = c.fin or (c.debut + timedelta(hours=1))
        lieu = " ".join(x for x in (c.salle, c.prof) if x)
        lignes += [
            "BEGIN:VEVENT",
            f"UID:cours-{abs(hash(c.cle()))}@assistant-cyu",
            f"DTSTAMP:{datetime.now():%Y%m%dT%H%M%S}",
            f"DTSTART:{c.debut:%Y%m%dT%H%M%S}",
            f"DTEND:{fin:%Y%m%dT%H%M%S}",
            f"SUMMARY:{_ics_txt((c.type_court + ' ' if c.type_court else '') + c.titre)}",
            f"LOCATION:{_ics_txt(lieu)}",
            f"DESCRIPTION:{_ics_txt(c.module)}",
            "END:VEVENT",
        ]
    for d in liste_devoirs:
        ech = promo.echeance_dt(d)
        if ech is None or d.get("fait"):
            continue
        titre = "[A rendre] " + d["titre"] + (f" - {d['matiere']}" if d.get("matiere") else "")
        lignes += [
            "BEGIN:VEVENT",
            f"UID:devoir-{d['id']}@assistant-cyu",
            f"DTSTAMP:{datetime.now():%Y%m%dT%H%M%S}",
            f"DTSTART:{ech - timedelta(minutes=30):%Y%m%dT%H%M%S}",
            f"DTEND:{ech:%Y%m%dT%H%M%S}",
            f"SUMMARY:{_ics_txt(titre)}",
            f"DESCRIPTION:{_ics_txt(d.get('note', ''))}",
            "BEGIN:VALARM", "TRIGGER:-P1D", "ACTION:DISPLAY",
            f"DESCRIPTION:{_ics_txt('Demain : ' + d['titre'])}", "END:VALARM",
            "END:VEVENT",
        ]
    lignes.append("END:VCALENDAR")
    # newline="" est indispensable : sans lui, Windows retraduit chaque \n en
    # \r\n et le fichier se retrouve avec des \r\r\n que certains agendas
    # refusent d'importer.
    with open(chemin, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(lignes) + "\r\n")
    return chemin


# ════════════════════════════════════════════════════════════════════════════
#  LES CARTES DISCORD  (ex-interface.py)
# ════════════════════════════════════════════════════════════════════════════
# L'allure du bot : une seule facon de repondre, pour toutes les commandes.
#
# Discord sait aujourd'hui afficher bien mieux qu'un embed : un CONTENEUR avec
# une barre de couleur, du texte mis en forme, une image integree au meme bloc,
# un trait de separation, et des boutons ou des menus juste en dessous. C'est
# ce qu'on appelle les « components v2 », et c'est ce que ce module construit.
#
# Ce qu'on y gagne, concretement :
#
#   * la photo et son titre sont UN SEUL bloc, pas un texte suivi d'une piece
#     jointe qu'on lit deux fois ;
#   * chaque reponse porte ses propres boutons : « ◀ hier · demain ▶ » sous une
#     journee, « semaine precedente · suivante » sous les stats. On navigue au
#     lieu de retaper ;
#   * ces boutons SURVIVENT aux redemarrages du bot : leur identifiant contient
#     tout ce qu'il faut pour rejouer l'action (« cyu:edt:j:2026-10-12 »), donc
#     un message d'il y a trois semaines marche encore.
#
# Tout le monde passe par carte() : bot.py ne connait ni Container ni
# TextDisplay, il donne un titre, des lignes, une couleur, eventuellement une
# image et des boutons, et recoit une vue prete a envoyer.
#
# Limites Discord a garder en tete (elles sont appliquees ici, pas ailleurs) :
# 40 composants par message, 4000 caracteres de texte au total, 5 boutons par
# rangee, 25 options par menu.


# Un prefixe pour tous nos identifiants de composants : un bouton d'un autre
# bot sur le meme serveur ne doit jamais tomber chez nous par accident.
PREFIXE = "cyu"

# La longueur maximale d'un bloc de texte, un peu sous la limite Discord pour
# laisser la place au titre et au pied.
TEXTE_MAX = 3600


def couleur(nom):
    """La couleur Discord d'un type de message : la meme que les embeds du
    daemon (notif.COULEURS), pour que tout ait l'air de venir du meme endroit."""
    return discord.Colour(COULEURS.get(nom, COULEURS["info"]))


def identifiant(action, *args):
    """« cyu:edt:j:2026-10-12 » — l'identifiant d'un bouton, qui dit tout ce
    qu'il faut pour rejouer l'action apres un redemarrage."""
    morceaux = [PREFIXE, action] + [str(a) for a in args if a is not None]
    return ":".join(morceaux)[:100]      # limite Discord sur un custom_id


@dataclass
class Bouton:
    """Ce que bot.py sait d'un bouton : un libelle, une action, un style."""
    libelle: str
    action: str
    args: tuple = ()
    style: str = "gris"          # gris · bleu · vert · rouge
    emoji: str | None = None
    inactif: bool = False
    # Pose par la fabrique quand deux boutons d'une meme carte visent la meme
    # action : « ◀ » depuis demain et « Aujourd'hui » menent au meme jour, et
    # Discord refuse deux identifiants identiques dans un message. Le suffixe
    # commence par « _ » : bot.py l'ignore en relisant les arguments.
    suffixe: str = ""

    @property
    def custom_id(self):
        base = identifiant(self.action, *self.args)
        return (base + self.suffixe)[:100] if self.suffixe else base


STYLES = {
    "gris": discord.ButtonStyle.secondary,
    "bleu": discord.ButtonStyle.primary,
    "vert": discord.ButtonStyle.success,
    "rouge": discord.ButtonStyle.danger,
}


@dataclass
class Menu:
    """Un menu deroulant : une action, un texte d'invite, des options."""
    action: str
    invite: str
    options: list = field(default_factory=list)   # [(libelle, valeur, description, emoji)]
    args: tuple = ()
    plusieurs: bool = False

    @property
    def custom_id(self):
        return identifiant(self.action, *self.args)


def fichier_image(chemin, nom=None):
    """Un discord.File a partir d'un PNG sur disque, avec un nom UNIQUE.

    Le nom sert de reference (« attachment://... ») dans la carte. Deux images
    du meme nom dans un meme message se disputeraient la place : on suffixe."""
    chemin = Path(chemin)
    nom = nom or f"{chemin.stem}-{uuid4().hex[:6]}.png"
    return discord.File(io.BytesIO(chemin.read_bytes()), filename=nom)


def _decouper(lignes):
    """Des lignes -> des blocs de texte sous la limite, coupes entre deux
    lignes et jamais au milieu d'une."""
    blocs, courant = [], ""
    for ligne in lignes:
        ligne = str(ligne)
        if len(courant) + len(ligne) + 1 > TEXTE_MAX:
            if courant:
                blocs.append(courant)
            courant = ligne[:TEXTE_MAX]
        else:
            courant = f"{courant}\n{ligne}" if courant else ligne
    if courant:
        blocs.append(courant)
    return blocs


def _pied(note=""):
    texte = f"assistant CYU · {datetime.now():%d/%m à %H:%M}"
    return f"-# {note} · {texte}" if note else f"-# {texte}"


class Carte(discord.ui.LayoutView):
    """Une reponse du bot : un conteneur colore, et tout ce qu'il contient.

    timeout=None : les boutons ne sont jamais desactives par le temps. Ils
    sont rejoues par bot.py a partir de leur identifiant, donc un vieux
    message reste utilisable — c'est tout l'interet.
    """

    def __init__(self):
        super().__init__(timeout=None)

    async def on_error(self, inter, error, item):
        """Sans ca, un bouton qui echoue affiche « L'interaction a echoue »
        et rien d'autre, nulle part."""
        import traceback
        traceback.print_exception(type(error), error, error.__traceback__)
        texte = f"❌ {type(error).__name__} : {error}"[:1900]
        try:
            if inter.response.is_done():
                await inter.followup.send(texte, ephemeral=True)
            else:
                await inter.response.send_message(texte, ephemeral=True)
        except discord.HTTPException:
            pass


def _dedoublonner(rangees, menus=()):
    """Rend unique l'identifiant de chaque bouton d'une carte.

    Discord : « Component custom id cannot be duplicated ». Deux boutons qui
    font la meme chose (la fleche et le raccourci) ont le droit d'exister
    tous les deux ; ils n'ont pas le droit de porter le meme nom.
    """
    vus = {m.custom_id for m in menus}
    n = 0
    for r in rangees:
        for b in r:
            b.suffixe = ""
            while b.custom_id in vus:
                n += 1
                b.suffixe = f":_{n}"
            vus.add(b.custom_id)


def _rangee(boutons, fabrique_bouton):
    rangee = discord.ui.ActionRow()
    for b in boutons[:5]:
        rangee.add_item(fabrique_bouton(b))
    return rangee


def carte(titre, lignes=(), teinte="info", sous_titre="", image=None,
          boutons=(), menus=(), pied=None, fabrique_bouton=None,
          fabrique_menu=None, telechargements=()):
    """La reponse standard du bot.

    titre / sous_titre  la tete du bloc ;
    lignes              le corps, en Markdown Discord, une ligne par element ;
    teinte              info · cours · devoir · alerte · calme · statut ;
    image               un discord.File deja construit (fichier_image()), ou
                        rien : la carte marche aussi bien en texte seul ;
    telechargements     des discord.File presentes en PIECES JOINTES, avec leur
                        nom et leur taille, et un bouton de telechargement.
                        Une image affichee dans la carte se telecharge deja en
                        cliquant dessus ; ceci sert a ce que Discord n'affiche
                        pas — un classeur, un .ics — et a offrir le fichier
                        source a cote de l'image qui en est tiree ;
    boutons             une liste de Bouton, ou une liste de listes (une par
                        rangee) ;
    menus               une liste de Menu ;
    pied                une note de bas de carte (« ce panneau reste actif… »).

    fabrique_bouton / fabrique_menu : bot.py fournit ici la fonction qui
    transforme un Bouton en composant Discord PERSISTANT (voir bot.BoutonCyu).
    Sans elles, on cree des composants ordinaires — utile pour tester le
    module tout seul.

    Rend la Carte (a passer en `view=`) ; le fichier image, s'il y en a un,
    est a passer en `files=[image]` dans le meme envoi.
    """
    fabrique_bouton = fabrique_bouton or _bouton_simple
    fabrique_menu = fabrique_menu or _menu_simple

    conteneur = discord.ui.Container(accent_colour=couleur(teinte))
    tete = f"## {titre}"
    if sous_titre:
        tete += f"\n-# {sous_titre}"
    conteneur.add_item(discord.ui.TextDisplay(tete))

    if image is not None:
        galerie = discord.ui.MediaGallery()
        galerie.add_item(media=f"attachment://{image.filename}",
                         description=str(titre)[:200])
        conteneur.add_item(galerie)

    lignes = [l for l in (lignes or ()) if l is not None]
    if lignes:
        if image is not None:
            conteneur.add_item(discord.ui.Separator())
        for bloc in _decouper(lignes)[:4]:
            conteneur.add_item(discord.ui.TextDisplay(bloc))

    # Les pieces jointes, sous le texte : Discord les affiche avec leur nom,
    # leur taille et un bouton « telecharger ».
    for f in telechargements or ():
        if f is not None:
            conteneur.add_item(discord.ui.File(f"attachment://{f.filename}"))

    # Les rangees de boutons : une liste plate = une seule rangee. Cinq au
    # plus : c'est la limite de Discord par message, et au-dela le panneau
    # deviendrait de toute facon un mur de boutons.
    rangees = list(boutons) if boutons and isinstance(boutons[0], (list, tuple)) \
        else ([list(boutons)] if boutons else [])
    _dedoublonner(rangees, menus)
    if rangees or menus:
        conteneur.add_item(discord.ui.Separator())
    for m in menus:
        rangee = discord.ui.ActionRow()
        rangee.add_item(fabrique_menu(m))
        conteneur.add_item(rangee)
    for r in rangees[:5]:
        if r:
            conteneur.add_item(_rangee(list(r), fabrique_bouton))

    if pied is not False:
        conteneur.add_item(discord.ui.TextDisplay(_pied(pied or "")))

    vue = Carte()
    vue.add_item(conteneur)
    return vue


def erreur(message, titre="Ça n'a pas marché"):
    """Une petite carte rouge, sans boutons ni pied."""
    conteneur = discord.ui.Container(accent_colour=couleur("alerte"))
    conteneur.add_item(discord.ui.TextDisplay(f"**{titre}**\n{message}"[:TEXTE_MAX]))
    vue = Carte()
    vue.add_item(conteneur)
    return vue


# --- Sans bot.py : des composants ordinaires ---------------------------------
def _bouton_simple(b):
    return discord.ui.Button(label=b.libelle[:80], style=STYLES.get(b.style, STYLES["gris"]),
                             custom_id=b.custom_id, emoji=b.emoji, disabled=b.inactif)


def _menu_simple(m):
    options = [discord.SelectOption(label=str(l)[:100], value=str(v)[:100],
                                    description=(str(d)[:100] if d else None),
                                    emoji=e)
               for l, v, d, e in m.options[:25]]
    return discord.ui.Select(custom_id=m.custom_id, placeholder=m.invite[:150],
                             options=options, min_values=1,
                             max_values=len(options) if m.plusieurs else 1)


def sections(elements, fabrique_bouton=None):
    """Des « sections » : un texte a gauche, un bouton a droite, l'un sous
    l'autre. C'est la forme d'une liste de devoirs : chaque ligne porte son
    propre bouton « fait ».

    elements : [(texte_markdown, Bouton | None), ...]
    Rend une liste de composants a ajouter dans un conteneur.
    """
    fabrique_bouton = fabrique_bouton or _bouton_simple
    sortie = []
    for texte, bouton in elements:
        if bouton is None:
            sortie.append(discord.ui.TextDisplay(texte[:TEXTE_MAX]))
            continue
        sortie.append(discord.ui.Section(discord.ui.TextDisplay(texte[:TEXTE_MAX]),
                                         accessory=fabrique_bouton(bouton)))
    return sortie


def carte_composee(titre, composants, teinte="info", sous_titre="", boutons=(),
                   menus=(), pied=None, fabrique_bouton=None, fabrique_menu=None):
    """Comme carte(), mais le corps est une liste de composants deja faits
    (sections(), par exemple) au lieu de lignes de texte."""
    fabrique_bouton = fabrique_bouton or _bouton_simple
    fabrique_menu = fabrique_menu or _menu_simple

    conteneur = discord.ui.Container(accent_colour=couleur(teinte))
    tete = f"## {titre}"
    if sous_titre:
        tete += f"\n-# {sous_titre}"
    conteneur.add_item(discord.ui.TextDisplay(tete))
    for c in composants:
        conteneur.add_item(c)

    rangees = list(boutons) if boutons and isinstance(boutons[0], (list, tuple)) \
        else ([list(boutons)] if boutons else [])
    _dedoublonner(rangees, menus)
    if rangees or menus:
        conteneur.add_item(discord.ui.Separator())
    for m in menus:
        rangee = discord.ui.ActionRow()
        rangee.add_item(fabrique_menu(m))
        conteneur.add_item(rangee)
    for r in rangees[:5]:
        if r:
            conteneur.add_item(_rangee(list(r), fabrique_bouton))
    if pied is not False:
        conteneur.add_item(discord.ui.TextDisplay(_pied(pied or "")))

    vue = Carte()
    vue.add_item(conteneur)
    return vue


# ════════════════════════════════════════════════════════════════════════════
#  L'AIGUILLAGE DISCORD  (ex-notif.py)
# ════════════════════════════════════════════════════════════════════════════
# Aiguillage Discord : qui envoie quoi, et dans quel salon.
#
# Un seul module sait poster sur Discord ; tous les autres passent par ici.
# Changer de salon se fait donc a un seul endroit : la section `salons` de
# config.yaml.
#
# Deux facons de poster, au choix, salon par salon :
#
#   * un IDENTIFIANT DE SALON (que des chiffres) -> poste via ton bot, qui doit
#     etre sur le serveur et avoir un jeton dans config.yaml ;
#   * une URL DE WEBHOOK -> aucun droit a donner, mais un webhook = un salon.
#
# Deux facons de poster, aussi, selon la duree de vie du message :
#
#   * envoyer()   un message de plus dans le salon. Pour tout ce qui est un
#                 evenement : un briefing, une alerte, une echeance ;
#   * epingler()  UN message, toujours le meme, reecrit sur place. Pour tout ce
#                 qui est un etat : le tableau de l'emploi du temps, le panneau
#                 de statut. Le salon ne se remplit pas, et l'information est
#                 toujours a la premiere place.
#
# Tu n'as pas a chercher les identifiants a la main :
#
#     python assistant.py salons
#
# cree les salons dans la categorie et ECRIT LEURS IDENTIFIANTS DANS
# config.yaml. Relancer la commande ne cree pas de doublon.


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


def _attente_429(r, defaut=2.0):
    """Le retry_after d'une 429, borne a 10 s. Une 429 servie par Cloudflare
    est une page HTML, pas du JSON : on ne doit pas planter dessus."""
    try:
        return min(float(r.json().get("retry_after", defaut)), 10)
    except (ValueError, AttributeError, TypeError):
        return defaut


def _requete(methode, url, entetes, charge=None):
    """Une requete Discord qui ne leve jamais : une panne de Discord ne doit pas
    tuer un daemon qui tourne depuis trois semaines.

    Rend (reponse ou None). Une 429 est retentee une fois, c'est tout : si
    Discord nous limite deux fois de suite, insister aggrave le probleme.
    """
    try:
        r = requests.request(methode, url, json=charge, headers=entetes, timeout=15)
        if r.status_code == 429:
            time.sleep(_attente_429(r))
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


def _requete_fichier(methode, url, entetes, charge, chemin, essai=0):
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
    if r.status_code == 429 and essai < 1:
        time.sleep(_attente_429(r))
        return _requete_fichier(methode, url, entetes, charge, chemin, essai + 1)
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


@config.exclusif(FICHIER_MESSAGES)
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
    """Message texte brut : ce dont la surveillance du webmail a besoin."""
    contenu, autorisees = _mention(ping)
    texte = f"{contenu} {msg}".strip() if contenu else str(msg)
    return _poster(canal, {"content": texte[:1990], "allowed_mentions": autorisees})


# --- Messages vivants : un seul message, reecrit sur place -------------------
def _lire_messages():
    return config.lire_json(FICHIER_MESSAGES, {})


def _ecrire_messages(data):
    config.ecrire_json(FICHIER_MESSAGES, data)


def _url_message(mode, cible, message_id):
    if mode == "bot":
        return f"{API}/channels/{cible}/messages/{message_id}"
    return f"{cible}/messages/{message_id}"


@config.exclusif(FICHIER_MESSAGES)
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


@config.exclusif(FICHIER_MESSAGES)
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
        attente = _attente_429(r, 5.0)
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


# ════════════════════════════════════════════════════════════════════════════
#  LES TABLEAUX VIVANTS (#STATUT, #EDT)  (ex-statut.py)
# ════════════════════════════════════════════════════════════════════════════
# Le panneau de statut : un seul message, reecrit en boucle, dans #statut.
#
# L'idee : ne plus jamais avoir a se demander « est-ce que le bot tourne
# encore ? ». Le salon #statut contient UN message, toujours le meme, toujours a
# jour, qui repond a la question sans qu'on tape quoi que ce soit :
#
#     ● En ligne depuis 3 jours
#     ⏭️ Prochain cours : VBA, dans 1 h 20, FER FT 210
#     🔄 CELCAT : lu il y a 4 min
#     📬 Webmail : joignable
#     📚 3 devoirs en attente, le plus urgent demain
#
# Le message est reecrit toutes les `rafraichir_tableaux_minutes` minutes, donc
# il ne genere aucune notification : Discord ne previent pas sur une
# modification. C'est justement pour ca que ce salon est separe des annonces.
#
# L'etat du webmail vient de `assistant.py uptime`, qui tourne dans un autre processus : les
# deux se parlent par un petit fichier JSON, pas par une variable partagee.


CLE_MESSAGE = "panneau-statut"
FICHIER_WEBMAIL = config.DONNEES / "webmail.json"


# --- L'etat du webmail, partage entre processus ------------------------------
def enregistrer_webmail(up, detail=""):
    """Appele par `assistant.py uptime` a chaque verification."""
    config.preparer_dossiers()
    FICHIER_WEBMAIL.write_text(json.dumps(
        {"up": bool(up), "detail": str(detail),
         "le": datetime.now().isoformat(timespec="seconds")},
        ensure_ascii=False), encoding="utf-8")


def lire_webmail():
    try:
        data = json.loads(FICHIER_WEBMAIL.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (ValueError, OSError):
        return None


# --- Les elements du panneau -------------------------------------------------
def _duree_depuis(depuis, maintenant=None):
    ecart = ((maintenant or datetime.now()) - depuis).total_seconds() / 60
    if ecart < 60 * 24:
        return duree_fr(ecart)
    jours = int(ecart // (60 * 24))
    return f"{jours} jour" + ("s" if jours > 1 else "")


def _ligne_cours(cours, maintenant):
    actuel = edt.en_cours(cours, maintenant)
    if actuel:
        reste = (actuel.fin - maintenant).total_seconds() / 60 if actuel.fin else 0
        return (f"▶️ **En cours** — {actuel.titre} · {actuel.ou}"
                + (f" · fini dans {duree_fr(reste)}" if reste > 0 else ""))
    suivant = edt.prochain(cours, maintenant)
    if not suivant:
        return "⏭️ **Prochain cours** — aucun de prevu 🎉"
    return (f"⏭️ **Prochain cours** — {suivant.titre} · "
            f"**{compte_a_rebours(suivant.debut, maintenant)}** "
            f"({jour_relatif(suivant.jour, maintenant.date())} "
            f"`{suivant.creneau}`) · {suivant.ou}")


def _ligne_celcat(echecs=0):
    age = edt.age_cache()
    if age is None:
        return "🔄 **CELCAT** — aucune donnee encore lue ⚠️"
    if echecs:
        return (f"🔄 **CELCAT** — ⚠️ {echecs} echec"
                f"{'s' if echecs > 1 else ''} d'affilee, dernieres donnees "
                f"il y a {duree_longue(age)}")
    etat = "🟢" if age < 60 else ("🟠" if age < 60 * 6 else "🔴")
    return f"🔄 **CELCAT** — {etat} lu il y a {duree_longue(age)}"


def _ligne_webmail():
    etat = lire_webmail()
    if not etat:
        return "📬 **Webmail** — non surveille (`python assistant.py uptime`)"
    vu = edt._dt(etat.get("le"))
    age = f" il y a {duree_longue((datetime.now() - vu).total_seconds() / 60)}" if vu else ""
    if etat.get("up"):
        return f"📬 **Webmail** — 🟢 joignable{age}"
    return (f"📬 **Webmail** — 🔴 injoignable{age}\n"
            f"{RETRAIT}{str(etat.get('detail'))[:120]}")


def _ligne_devoirs(liste_devoirs=None):
    restants = promo.actifs(liste_devoirs)
    if not restants:
        return "📚 **Devoirs** — rien en attente 😌"
    retard = [d for d in restants if (r := promo.jours_restants(d)) is not None and r < 0]
    urgence = ""
    if retard:
        urgence = f" · 🔴 **{len(retard)} en retard**"
    else:
        proche = promo.jours_restants(restants[0])
        if proche == 0:
            urgence = " · 🔥 un pour aujourd'hui"
        elif proche == 1:
            urgence = " · ⚠️ un pour demain"
        elif proche is not None:
            urgence = f" · le plus proche dans {proche} j"
    return f"📚 **Devoirs** — {len(restants)} en attente{urgence}"


def _prochain_rendez_vous(maintenant):
    """Le prochain envoi automatique prevu : dit ce que le bot va faire.

    Sans ca, un salon silencieux est ambigu — panne, ou simplement rien a
    dire ? La ligne leve le doute."""
    candidats = []
    for jour in (maintenant.date(), maintenant.date() + timedelta(days=1)):
        candidats.append((a_heure(jour, config.BRIEFING_MATIN, (7, 0)),
                          "briefing du matin"))
        candidats.append((a_heure(jour, config.BRIEFING_SOIR, (20, 0)),
                          "briefing du soir"))
        if jour.weekday() == config.RECAP_SEMAINE_JOUR:
            candidats.append((a_heure(jour, config.RECAP_SEMAINE_HEURE, (18, 0)),
                              "recap de la semaine"))
    a_venir = sorted((q, n) for q, n in candidats if q > maintenant)
    if not a_venir:
        return None
    quand, nom = a_venir[0]
    return f"📅 **Prochain envoi** — {nom}, {quand:%H:%M} " \
    f"({compte_a_rebours(quand, maintenant)})"


# --- Le panneau complet ------------------------------------------------------
def _ligne_meteo():
    """Le temps qu'il fait, en une ligne. "" si la meteo est coupee ou muette.

    Le panneau est reecrit toutes les dix minutes : le bulletin vient du cache
    de meteo.py, jamais d'un appel reseau par reecriture."""
    if not config.METEO_ACTIVE:
        return ""
    try:
        courte = edt.ligne_courte()
    except Exception as e:                      # noqa: BLE001 - filet volontaire
        print(f"[!] meteo absente du panneau : {e}", flush=True)
        return ""
    return f"🌡️ **Dehors** — {courte} à {config.METEO_LIEU}" if courte else ""


def bloc(cours, liste_devoirs=None, demarrage=None, echecs=0, maintenant=None):
    """Les lignes du panneau de statut."""
    maintenant = maintenant or datetime.now()
    lignes = []

    if demarrage:
        lignes.append(f"● **En ligne** depuis {_duree_depuis(demarrage, maintenant)}")
    else:
        lignes.append("● **En ligne**")
    lignes.append("")

    lignes.append(_ligne_cours(cours, maintenant))

    jc = edt.du_jour(cours, maintenant.date())
    if jc:
        finis = sum(1 for c in jc if c.fin and maintenant >= c.fin)
        restant = sum(c.minutes for c in jc if not (c.fin and maintenant >= c.fin))
        lignes.append(f"📆 **Aujourd'hui** — {finis}/{len(jc)} cours faits"
                      + (f" · {duree_fr(restant)} restantes" if restant else " · fini 🎉"))
    else:
        lignes.append("📆 **Aujourd'hui** — aucun cours")

    ligne = _ligne_meteo()
    if ligne:
        lignes.append(ligne)

    lignes += ["", _ligne_devoirs(liste_devoirs), "",
               _ligne_celcat(echecs), _ligne_webmail()]

    rdv = _prochain_rendez_vous(maintenant)
    if rdv:
        lignes += ["", rdv]

    return lignes


def publier(cours, liste_devoirs=None, demarrage=None, echecs=0):
    """Reecrit le panneau dans #statut. Ne notifie personne : c'est une
    modification de message, pas un nouveau message."""
    couleur = "alerte" if echecs >= 3 else "statut"
    return epingler(
        CLE_MESSAGE, "État de l'assistant",
        bloc(cours, liste_devoirs, demarrage, echecs),
        couleur=couleur, canal="statut",
        pied=f"mis a jour toutes les {config.RAFRAICHIR_TABLEAUX_MINUTES} min")


def _resume_semaine(cours, liste_devoirs, lundi):
    """Les deux ou trois lignes qui accompagnent l'image dans #edt.

    L'image dit la forme de la semaine ; ces lignes disent ce qu'une image ne
    peut pas dire — dans combien de temps est le prochain cours, et ce qu'il
    faut rendre."""
    lignes = []
    suivant = edt.prochain(cours)
    if suivant:
        lignes.append(f"⏭️ **Prochain** — `{suivant.creneau}` "
                      f"{jour_relatif(suivant.jour)} · {suivant.titre} · "
                      f"{suivant.ou} ({compte_a_rebours(suivant.debut)})")
    restants = promo.actifs(liste_devoirs, 7)
    if restants:
        lignes.append(f"📚 **{len(restants)} devoir"
                      f"{'s' if len(restants) > 1 else ''} sous 7 jours** — "
                      + ", ".join(d["titre"][:28] for d in restants[:4]))
    total = sum(c.minutes for c in cours
                if c.est_cours and lundi <= c.jour <= lundi + timedelta(days=6))
    if total:
        lignes.append(f"-# {duree_fr(total)} de cours cette semaine · "
                      f"`/edt` pour un jour precis, `/actu` pour ce qui a bougé")
    return lignes


def publier_tableau_edt(cours, liste_devoirs=None, lundi=None):
    """Reecrit le tableau de la semaine dans #edt.

    Meme principe que le panneau de statut : le salon ne contient qu'un
    message, celui de la semaine en cours, et il est toujours juste. Depuis
    que l'image existe, ce message EST l'image : c'est la vue qu'on regarde
    dix fois par jour, elle merite mieux qu'une liste.

    Retombe sur le tableau en texte si Pillow manque ou si le dessin echoue :
    un salon vide serait pire qu'un tableau moins joli.
    """
    lundi = lundi or edt.semaine_de(date.today())
    titre = f"Semaine du {lundi:%d/%m}"

    if config.IMAGES and config.TABLEAU_EDT_IMAGE and image.DISPONIBLE:
        try:
            chemin = image.rendre(cours, lundi, lundi + timedelta(days=6),
                                  config.DONNEES / "tableau-edt.png", titre=titre)
            ok = epingler_image(
                "tableau-edt", chemin, titre,
                _resume_semaine(cours, liste_devoirs, lundi),
                couleur="cours", canal="edt",
                pied=f"mis a jour toutes les {config.RAFRAICHIR_TABLEAUX_MINUTES} min")
            if ok:
                return True
            print("[!] image de #edt non publiee, retour au tableau texte",
                  flush=True)
        except (image.PillowManquant, OSError, ValueError) as e:
            print(f"[!] dessin de #edt impossible ({e}), retour au texte", flush=True)

    return epingler(
        "tableau-edt", titre,
        tableau_semaine(cours, lundi, liste_devoirs),
        couleur="cours", canal="edt",
        pied="mis a jour tout seul · /edt pour un jour precis")
