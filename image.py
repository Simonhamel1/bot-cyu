#!/usr/bin/env python3
"""
Dessiner l'emploi du temps en image : une vraie grille, comme sur papier.

Le texte Discord est parfait pour « c'est quoi mon prochain cours », mais pour
« a quoi ressemblent mes trois prochaines semaines », rien ne vaut une image :
on voit la forme des journees d'un seul regard.

    python assistant.py image --jusqu-au 12/10

    /photo jusqu_au:12/10          dans Discord

Le rendu est une colonne par jour, une ligne par heure, un bloc colore par
cours. Au-dela d'une semaine, les semaines s'empilent les unes sous les autres
plutot que de s'etaler en largeur : un PNG de 30 colonnes serait illisible sur
un telephone, ce qui est justement la ou on le regarde.

Seule dependance : Pillow (`pip install pillow`). Si elle manque, le reste du
projet continue de marcher : c'est le seul module qui l'importe.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import celcat
import config
import vue

try:
    from PIL import Image, ImageDraw, ImageFont
    DISPONIBLE = True
except ImportError:                                          # pragma: no cover
    DISPONIBLE = False


class PillowManquant(RuntimeError):
    """Pillow n'est pas installe : `pip install pillow`."""


# --- Reglages du dessin ------------------------------------------------------
FOND = (30, 31, 34)             # le gris de Discord en theme sombre
PANNEAU = (43, 45, 49)
GRILLE = (58, 60, 66)
GRILLE_FORTE = (78, 81, 89)
TEXTE = (242, 243, 245)
TEXTE_FAIBLE = (150, 155, 165)
AUJOURDHUI = (88, 101, 242)

# Une couleur par type de seance. Meme code couleur que la grille en barres
# de vue.py, pour ne pas avoir a reapprendre deux langages.
COULEURS = {
    "CM": (67, 96, 223), "TD": (45, 145, 88), "TP": (0, 145, 190),
    "EXAMEN": (192, 57, 60), "EXAM": (192, 57, 60), "DS": (192, 57, 60),
    "CONTROLE": (192, 57, 60), "SOUTENANCE": (192, 57, 60),
    "PROJET": (140, 80, 180), "": (176, 122, 30),
}
COULEUR_DISTANCE = (170, 60, 140)

MARGE = 26
GOUTTIERE = 62                  # la colonne des heures, a gauche
LARGEUR_JOUR = 196
HAUTEUR_HEURE = 66
ENTETE_JOUR = 40
ESPACE_SEMAINES = 30

# Polices : la premiere trouvee gagne. Windows d'abord (ta machine), puis les
# chemins Linux classiques (le serveur ou tourne le bot), puis macOS.
POLICES = [
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]
POLICES_GRASSES = [
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
]


def _police(taille, gras=False):
    """Une police de la taille demandee, ou celle de secours de Pillow.

    La police de secours est une bitmap minuscule et sans accents : le rendu
    est laid mais lisible, et surtout ca n'echoue pas sur un serveur nu."""
    for chemin in (POLICES_GRASSES if gras else POLICES):
        try:
            return ImageFont.truetype(chemin, taille)
        except (OSError, ValueError):
            continue
    return ImageFont.load_default()


def _tronquer(draw, texte, police, largeur):
    """Le texte coupe pour tenir dans `largeur`, avec un vrai caractere « … »."""
    if draw.textlength(texte, font=police) <= largeur:
        return texte
    while texte and draw.textlength(texte + "…", font=police) > largeur:
        texte = texte[:-1]
    return (texte.rstrip() + "…") if texte else ""


def _couleur(c):
    if c.a_distance:
        return COULEUR_DISTANCE
    return COULEURS.get(c.type_court, COULEURS[""])


def _eclaircir(rgb, facteur=0.35):
    return tuple(int(v + (255 - v) * facteur) for v in rgb)


# --- Une semaine -------------------------------------------------------------
def _jours_visibles(cours, lundi, debut, fin):
    """Les jours de cette semaine a dessiner : ceux dans la plage demandee.

    Le week-end n'apparait que s'il porte quelque chose : deux colonnes vides
    par semaine, ce serait 40 % de l'image pour rien."""
    jours = []
    for i in range(7):
        j = lundi + timedelta(days=i)
        if j < debut or j > fin:
            continue
        if i >= 5 and not celcat.du_jour(cours, j):
            continue
        jours.append(j)
    return jours


def _bornes(cours_visibles):
    """(heure de debut, heure de fin) de la grille, en heures entieres."""
    h0 = vue.hhmm(config.JOURNEE_DEBUT, (8, 0))[0]
    h1 = vue.hhmm(config.JOURNEE_FIN, (19, 0))[0]
    for c in cours_visibles:
        h0 = min(h0, c.debut.hour)
        if c.fin:
            h1 = max(h1, c.fin.hour + (1 if c.fin.minute else 0))
    return h0, max(h1, h0 + 2)


def _dessiner_semaine(draw, x0, y0, cours, jours, h0, h1, polices):
    """Dessine une semaine et rend sa hauteur totale."""
    p_jour, p_heure, p_titre, p_detail = polices
    hauteur_grille = (h1 - h0) * HAUTEUR_HEURE
    largeur = GOUTTIERE + len(jours) * LARGEUR_JOUR
    aujourd = date.today()

    draw.rounded_rectangle(
        [x0, y0, x0 + largeur, y0 + ENTETE_JOUR + hauteur_grille],
        radius=12, fill=PANNEAU)

    # Les lignes d'heures, et leur etiquette dans la gouttiere.
    for i in range(h1 - h0 + 1):
        y = y0 + ENTETE_JOUR + i * HAUTEUR_HEURE
        draw.line([x0 + GOUTTIERE - 8, y, x0 + largeur - 8, y],
                  fill=GRILLE_FORTE if i % 2 == 0 else GRILLE, width=1)
        if i < h1 - h0:
            draw.text((x0 + GOUTTIERE - 14, y + 4), f"{h0 + i:02d}:00",
                      font=p_heure, fill=TEXTE_FAIBLE, anchor="ra")
            # La demi-heure, plus discrete : elle sert a situer un cours qui
            # commence a 9 h 30 sans avoir a compter les pixels.
            demi = y + HAUTEUR_HEURE // 2
            draw.line([x0 + GOUTTIERE - 8, demi, x0 + largeur - 8, demi],
                      fill=GRILLE, width=1)

    for index, jour in enumerate(jours):
        cx = x0 + GOUTTIERE + index * LARGEUR_JOUR
        if index:
            draw.line([cx - 4, y0 + ENTETE_JOUR, cx - 4,
                       y0 + ENTETE_JOUR + hauteur_grille], fill=GRILLE, width=1)

        etiquette = f"{vue.JOURS_COURTS[jour.weekday()]}. {jour:%d/%m}"
        couleur_jour = AUJOURDHUI if jour == aujourd else TEXTE
        if jour == aujourd:
            draw.rounded_rectangle(
                [cx, y0 + 6, cx + LARGEUR_JOUR - 10, y0 + ENTETE_JOUR - 8],
                radius=8, fill=(46, 51, 82))
            etiquette += "  •"
        draw.text((cx + 10, y0 + 12), etiquette, font=p_jour, fill=couleur_jour)

        for c in celcat.du_jour(cours, jour):
            debut_min = (c.debut.hour - h0) * 60 + c.debut.minute
            fin_min = debut_min + max(c.minutes, 30)
            haut = y0 + ENTETE_JOUR + debut_min * HAUTEUR_HEURE / 60
            bas = y0 + ENTETE_JOUR + fin_min * HAUTEUR_HEURE / 60
            gauche, droite = cx + 3, cx + LARGEUR_JOUR - 13

            fond = _couleur(c)
            draw.rounded_rectangle([gauche, haut, droite, bas], radius=8, fill=fond)
            # La barre plus claire a gauche : elle donne au bloc un point
            # d'accroche, meme quand il ne fait que 30 minutes de haut.
            draw.rounded_rectangle([gauche, haut, gauche + 4, bas], radius=2,
                                   fill=_eclaircir(fond, 0.55))

            interieur = droite - gauche - 20
            ty = haut + 5
            draw.text((gauche + 12, ty),
                      f"{c.debut:%H:%M} – {c.fin:%H:%M}" if c.fin else f"{c.debut:%H:%M}",
                      font=p_detail, fill=_eclaircir(fond, 0.85))
            ty += 15
            if bas - haut > 34:
                draw.text((gauche + 12, ty),
                          _tronquer(draw, c.titre, p_titre, interieur),
                          font=p_titre, fill=TEXTE)
                ty += 18
            if bas - haut > 56:
                detail = " · ".join(b for b in (c.type_court, c.ou) if b)
                draw.text((gauche + 12, ty),
                          _tronquer(draw, detail, p_detail, interieur),
                          font=p_detail, fill=_eclaircir(fond, 0.8))
                ty += 15
            if bas - haut > 76 and c.prof:
                draw.text((gauche + 12, ty),
                          _tronquer(draw, c.prof.title(), p_detail, interieur),
                          font=p_detail, fill=_eclaircir(fond, 0.7))

    return ENTETE_JOUR + hauteur_grille


# --- L'image complete --------------------------------------------------------
def rendre(cours, debut=None, fin=None, chemin=None, titre=None):
    """Dessine l'emploi du temps de `debut` a `fin` et rend le chemin du PNG.

    Les semaines s'empilent verticalement. Une semaine sans le moindre cours
    est sautee : sur trois semaines d'horizon, ca evite deux pages vides.
    """
    if not DISPONIBLE:
        raise PillowManquant(
            "Pillow n'est pas installe, donc pas d'image possible.\n"
            "    pip install pillow")

    debut = debut or date.today()
    fin = fin or (debut + timedelta(days=6))
    if fin < debut:
        debut, fin = fin, debut
    # Au-dela de six semaines, l'image devient un mur de pixels que personne ne
    # lit : on coupe, et l'appelant le dit a l'utilisateur.
    fin = min(fin, debut + timedelta(days=41))

    concernes = [c for c in cours if c.est_cours and debut <= c.jour <= fin]
    h0, h1 = _bornes(concernes)

    semaines = []
    lundi = celcat.semaine_de(debut)
    while lundi <= fin:
        jours = _jours_visibles(cours, lundi, debut, fin)
        if jours and any(celcat.du_jour(cours, j) for j in jours):
            semaines.append((lundi, jours))
        lundi += timedelta(days=7)

    polices = (_police(15, gras=True), _police(13), _police(14, gras=True),
               _police(12))
    p_titre_h, p_sous = _police(24, gras=True), _police(14)

    if not semaines:
        largeur, hauteur = 720, 220
    else:
        largeur = MARGE * 2 + GOUTTIERE + max(len(j) for _, j in semaines) * LARGEUR_JOUR
        hauteur = (MARGE + 76
                   + sum(ENTETE_JOUR + (h1 - h0) * HAUTEUR_HEURE + ESPACE_SEMAINES + 24
                         for _ in semaines)
                   + MARGE)

    img = Image.new("RGB", (int(largeur), int(hauteur)), FOND)
    draw = ImageDraw.Draw(img)

    entete = titre or (f"Emploi du temps — {vue.jour_fr(debut, court=True)} "
                       f"au {vue.jour_fr(fin, court=True)}")
    draw.text((MARGE, MARGE), entete, font=p_titre_h, fill=TEXTE)
    total = sum(c.minutes for c in concernes)
    draw.text((MARGE, MARGE + 34),
              f"{len(concernes)} cours · {vue.duree_fr(total)} · "
              f"genere le {datetime.now():%d/%m a %H:%M}",
              font=p_sous, fill=TEXTE_FAIBLE)

    y = MARGE + 76
    for lundi, jours in semaines:
        draw.text((MARGE, y), f"Semaine du {lundi:%d/%m}", font=p_sous,
                  fill=TEXTE_FAIBLE)
        y += 24
        y += _dessiner_semaine(draw, MARGE, y, cours, jours, h0, h1, polices)
        y += ESPACE_SEMAINES

    if not semaines:
        draw.text((MARGE, MARGE + 110),
                  "Aucun cours sur cette periode.", font=p_titre_h, fill=TEXTE_FAIBLE)

    chemin = Path(chemin or (config.DONNEES / "edt.png"))
    chemin.parent.mkdir(parents=True, exist_ok=True)
    img.save(chemin, "PNG", optimize=True)
    return chemin


def rendre_semaine(cours, lundi=None, chemin=None):
    """Raccourci : la semaine qui contient `lundi` (celle en cours par defaut)."""
    lundi = lundi or celcat.semaine_de(date.today())
    return rendre(cours, lundi, lundi + timedelta(days=6), chemin,
                  titre=f"Semaine du {lundi:%d/%m}")
