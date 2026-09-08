#!/usr/bin/env python3
"""
La mise en forme : transformer des cours et des devoirs en texte lisible.

Toutes les fonctions rendent une liste de lignes en markdown Discord. Le
terminal reutilise les memes, en passant par sans_markdown(). Un seul endroit
decide donc de l'allure des messages, que ce soit le daemon, le bot ou la ligne
de commande qui les affiche.

Deux principes tenus partout ici :

  * l'heure d'abord. Une ligne commence toujours par son creneau, en chasse
    fixe, pour que la colonne des heures soit droite d'un bout a l'autre du
    message ;
  * l'indentation se fait avec U+2800 (braille vide), pas avec des espaces.
    Discord mange les espaces multiples en debut de ligne, pas celui-la.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import config
import celcat
import devoirs as dv

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
    brut = dv.resoudre_echeance(txt, cours or [], "")
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
    ech = dv.echeance_dt(d)
    reste = dv.jours_restants(d)
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
    liste = liste if liste is not None else dv.lire()
    restants = dv.actifs(liste, horizon)
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
    jc = celcat.du_jour(cours, jour)

    if not jc:
        autres = celcat.non_cours_du_jour(cours, jour)
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

        for d in dv.du_cours(c, liste_devoirs):
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
    actuel = celcat.en_cours(cours, maintenant)
    suivant = celcat.prochain(cours, maintenant)

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

    liees = dv.du_cours(suivant, liste_devoirs)
    if liees:
        lignes += ["", "📌 **A rendre pour ce cours**"] + \
                  [ligne_devoir(d, avec_id=False) for d in liees]

    # Ce qui suit dans la meme journee : sans ca, il faut refaire /edt.
    apres = [c for c in celcat.du_jour(cours, suivant.jour) if c.debut > suivant.debut]
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
    par_jour = {j: celcat.du_jour(cours, j) for j in dates}
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
            autres = celcat.non_cours_du_jour(cours, j)
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
            morceau, courant, texte = [], None, ""
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
        jc = celcat.du_jour(cours, jour)
        minutes = sum(c.minutes for c in jc)
        total += minutes
        if not jc:
            if i < 5 or celcat.non_cours_du_jour(cours, jour):
                autres = celcat.non_cours_du_jour(cours, jour)
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
    lundi = lundi or celcat.semaine_de(date.today())
    lignes = bloc_semaine(cours, lundi)

    suivant = celcat.prochain(cours)
    if suivant:
        lignes += ["", f"⏭️ **Prochain** — `{suivant.creneau}` "
                       f"{jour_relatif(suivant.jour)} · {suivant.titre} · "
                       f"{suivant.ou} ({compte_a_rebours(suivant.debut)})"]

    restants = dv.actifs(liste_devoirs, 7)
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
        for c in celcat.du_jour(cours, jour):
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
        ech = dv.echeance_dt(d)
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
