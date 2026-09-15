#!/usr/bin/env python3
"""
Le rendu en image : tout ce que Discord affiche mal en texte, dessine en PNG.

Pourquoi des images partout
---------------------------
Un embed Discord n'a ni colonnes, ni couleurs, ni alignement fiable : une
journee de cours en texte est une liste, alors que c'est une FORME — des blocs,
des trous, une amplitude. Ce module dessine donc :

    rendre()              une periode : les jours a la VERTICALE, les heures
                          a l'horizontale, un bloc colore par cours ;
    rendre_jour()         une journee en detail : trous, salles, profs,
                          devoirs a rendre, heure de lever ;
    rendre_changements()  ce qui a bouge : avant  ➜  apres, ligne par ligne ;
    rendre_devoirs()      le carnet de devoirs, trie par urgence ;
    rendre_prochain()     le prochain cours, en grand.

Les jours sont a la verticale (une ligne par jour) et jamais en colonnes : sur
un telephone, cinq colonnes etroites obligent a zoomer, cinq lignes larges se
lisent d'un coup.

Tout est dessine au double de la taille finale puis reduit : les textes
sortent nets meme sur un ecran dense, sans avoir a charger une police par
taille.

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


# --- Le theme ----------------------------------------------------------------
# Volontairement proche du theme sombre de Discord : l'image doit avoir l'air
# d'appartenir au salon, pas d'etre une capture d'ecran venue d'ailleurs.
FOND = (17, 18, 21)
CARTE = (30, 31, 36)
CARTE_HAUTE = (38, 40, 46)
TRAIT = (48, 51, 58)
TRAIT_FORT = (66, 70, 80)
TEXTE = (238, 240, 245)
TEXTE_MOYEN = (176, 182, 194)
TEXTE_FAIBLE = (132, 139, 152)
ACCENT = (88, 101, 242)
ROUGE = (237, 66, 69)
VERT = (59, 165, 93)
ORANGE = (219, 145, 38)

# Une couleur par type de seance, la meme que la grille en barres de vue.py :
# une seule grammaire de couleurs a apprendre pour tout le projet.
#
# Les teintes ne sont pas choisies a l'oeil : elles passent le test de
# separation pour les trois formes de daltonisme ET pour une vision normale,
# sur ce fond sombre. C'est ce qui a fait bouger le TP du turquoise (14, 132,
# 168) vers le bleu ci-dessous : cote a cote avec le vert des TD, les deux
# etaient trop proches pour etre distingues de facon fiable.
COULEURS = {
    "CM": (59, 102, 214), "TD": (35, 140, 85), "TP": (14, 140, 196),
    "EXAMEN": (196, 52, 58), "EXAM": (196, 52, 58), "DS": (196, 52, 58),
    "CONTROLE": (196, 52, 58), "SOUTENANCE": (196, 52, 58),
    "PROJET": (139, 86, 196), "": (176, 122, 30),
}
COULEUR_DISTANCE = (176, 64, 140)

# La meteo est dessinee, jamais ecrite en emoji : sur un serveur ou seule
# DejaVu est installee, un emoji devient un carre vide (voir _nettoyer).
SOLEIL = (240, 190, 64)
NUAGE = (154, 162, 178)
NUAGE_SOMBRE = (104, 112, 130)
PLUIE = (86, 158, 232)
NEIGE = (214, 230, 245)
ECLAIR = (246, 204, 74)
BROUILLARD = (138, 146, 162)

# Tout est dessine a cette echelle puis reduit : c'est ce qui donne des
# caracteres nets au lieu de l'escalier habituel de Pillow.
ECHELLE = 2

MARGE = 24
RAYON = 14

# La grille : une ligne par jour, le temps de gauche a droite.
GOUTTIERE = 112                 # la colonne des jours, a gauche
LARGEUR_HEURE = 88              # un pouce de temps
HAUTEUR_JOUR = 82               # une ligne de jour
HAUTEUR_JOUR_VIDE = 46          # un jour sans cours prend moins de place
ENTETE_HEURES = 30
ESPACE_LIGNE = 8

# Polices : la premiere trouvee gagne. Linux d'abord (le serveur ou tourne le
# bot), puis Windows, puis macOS.
POLICES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]
POLICES_GRASSES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]

_CACHE_POLICES = {}


def _police(taille, gras=False):
    """Une police a la taille demandee, ou celle de secours de Pillow.

    Le cache evite de relire le fichier de police a chaque ligne de texte :
    une image de trois semaines en demande plusieurs centaines."""
    cle = (int(taille), bool(gras))
    if cle in _CACHE_POLICES:
        return _CACHE_POLICES[cle]
    for chemin in (POLICES_GRASSES if gras else POLICES):
        try:
            _CACHE_POLICES[cle] = ImageFont.truetype(chemin, int(taille))
            return _CACHE_POLICES[cle]
        except (OSError, ValueError):
            continue
    # Police bitmap de secours : laide et sans accents, mais ca n'echoue pas
    # sur un serveur nu ou personne n'a installe fonts-dejavu.
    _CACHE_POLICES[cle] = ImageFont.load_default()
    return _CACHE_POLICES[cle]


# Une police sans emoji dessine un rectangle vide a la place : sur un serveur
# ou seule DejaVu est installee, un titre plein d'emoji devient une rangee de
# petits carres. On enleve donc, une fois pour toutes, ce que la police ne sait
# pas dessiner — que le caractere vienne de nous ou d'un intitule CELCAT.
_CACHE_GLYPHES = {}


def _dessinable(car):
    if car in _CACHE_GLYPHES:
        return _CACHE_GLYPHES[car]
    police = _police(24)
    try:
        masque = bytes(bytearray(police.getmask(car)))
        vide = bytes(bytearray(police.getmask("")))
        ok = masque != vide or car.isspace()
    except (OSError, ValueError, TypeError):
        ok = True
    _CACHE_GLYPHES[car] = ok
    return ok


def _nettoyer(texte):
    """Le texte sans les caracteres que la police ne sait pas dessiner."""
    texte = str(texte)
    if texte.isascii():
        return texte
    return "".join(c for c in texte if c.isascii() or _dessinable(c)).strip() or texte.strip()


def _eclaircir(rgb, facteur=0.35):
    return tuple(int(v + (255 - v) * facteur) for v in rgb)


def _assombrir(rgb, facteur=0.35):
    return tuple(int(v * (1 - facteur)) for v in rgb)


def _melanger(a, b, part):
    return tuple(int(x + (y - x) * part) for x, y in zip(a, b))


def _texte_sur(fond):
    """Noir ou blanc, selon ce qui se lit le mieux sur `fond`.

    Sans ca, une pastille jaune ecrite en blanc est illisible, et c'est
    justement la couleur des changements d'horaire."""
    luminance = 0.299 * fond[0] + 0.587 * fond[1] + 0.114 * fond[2]
    return (18, 18, 20) if luminance > 150 else (255, 255, 255)


def _couleur_cours(c):
    if c.a_distance:
        return COULEUR_DISTANCE
    return COULEURS.get(c.type_court, COULEURS[""])


# --- La toile ----------------------------------------------------------------
class Toile:
    """Un dessin en coordonnees « logiques », rendu au double puis reduit.

    Toutes les methodes prennent des coordonnees a taille finale ; la mise a
    l'echelle est faite ici, une fois pour toutes. Le code de dessin reste
    donc lisible, et changer ECHELLE ne demande de toucher a rien.

    La hauteur passee au constructeur est un MAJORANT : on dessine, on retient
    jusqu'ou on est descendu, et `finir()` recadre. C'est ce qui permet
    d'ecrire des cartes a contenu variable sans calculer leur hauteur deux
    fois.
    """

    def __init__(self, largeur, hauteur, fond=FOND):
        if not DISPONIBLE:
            raise PillowManquant(
                "Pillow n'est pas installe, donc pas d'image possible.\n"
                "    pip install pillow")
        self.largeur = int(largeur)
        self.hauteur = int(hauteur)
        self.e = ECHELLE
        self.img = Image.new("RGB", (self.largeur * self.e, self.hauteur * self.e), fond)
        self.d = ImageDraw.Draw(self.img)

    # -- primitives ----------------------------------------------------------
    def _b(self, boite):
        e = self.e
        return [round(v * e) for v in boite]

    def rect(self, boite, fond=None, rayon=0, contour=None, epaisseur=1):
        boite = self._b(boite)
        if rayon:
            self.d.rounded_rectangle(boite, radius=round(rayon * self.e), fill=fond,
                                     outline=contour, width=round(epaisseur * self.e))
        else:
            self.d.rectangle(boite, fill=fond, outline=contour,
                             width=round(epaisseur * self.e))

    def ligne(self, boite, couleur, epaisseur=1):
        self.d.line(self._b(boite), fill=couleur, width=max(1, round(epaisseur * self.e)))

    def largeur_texte(self, texte, taille=14, gras=False):
        return self.d.textlength(_nettoyer(texte),
                                 font=_police(taille * self.e, gras)) / self.e

    def texte(self, x, y, texte, taille=14, gras=False, couleur=TEXTE,
              largeur_max=None, aligne="gauche"):
        """Ecrit une ligne. `largeur_max` tronque avec une vraie ellipse."""
        texte = _nettoyer(texte)
        if largeur_max is not None:
            texte = self.tronquer(texte, largeur_max, taille, gras)
        if aligne != "gauche":
            l = self.largeur_texte(texte, taille, gras)
            x = x - l if aligne == "droite" else x - l / 2
        self.d.text((round(x * self.e), round(y * self.e)), texte,
                    font=_police(taille * self.e, gras), fill=couleur)
        return x

    def tronquer(self, texte, largeur, taille=14, gras=False):
        texte = _nettoyer(texte)
        if self.largeur_texte(texte, taille, gras) <= largeur:
            return texte
        while texte and self.largeur_texte(texte + "…", taille, gras) > largeur:
            texte = texte[:-1]
        return (texte.rstrip() + "…") if texte else ""

    def envelopper(self, texte, largeur, taille=14, gras=False, lignes_max=3):
        """Le texte coupe en lignes qui tiennent dans `largeur`."""
        mots, lignes, courante = _nettoyer(texte).split(), [], ""
        for mot in mots:
            essai = f"{courante} {mot}".strip()
            if self.largeur_texte(essai, taille, gras) <= largeur or not courante:
                courante = essai
            else:
                lignes.append(courante)
                courante = mot
                if len(lignes) == lignes_max:
                    break
        if courante and len(lignes) < lignes_max:
            lignes.append(courante)
        if len(lignes) == lignes_max and mots:
            reste = " ".join(mots[sum(len(l.split()) for l in lignes):])
            if reste:
                lignes[-1] = self.tronquer(lignes[-1] + " " + reste, largeur, taille, gras)
        return lignes

    def polygone(self, points, fond=None, contour=None):
        """Un polygone en coordonnees logiques : l'eclair, les fleches."""
        plats = []
        for x, y in points:
            plats += [round(x * self.e), round(y * self.e)]
        self.d.polygon(plats, fill=fond, outline=contour)

    def disque(self, cx, cy, rayon, fond):
        """Un cercle plein. Pillow n'a pas de primitive « cercle » qui tienne
        compte de l'echelle : celle-ci si."""
        self.rect([cx - rayon, cy - rayon, cx + rayon, cy + rayon], fond=fond,
                  rayon=rayon)

    def pastille(self, x, y, texte, fond, couleur=None, taille=11, gras=True,
                 hauteur=19):
        """Une etiquette arrondie : « CM », « EXAMEN », « à distance »."""
        couleur = _texte_sur(fond) if couleur is None else couleur
        largeur = self.largeur_texte(texte, taille, gras) + 14
        self.rect([x, y, x + largeur, y + hauteur], fond=fond, rayon=hauteur / 2)
        self.texte(x + 7, y + (hauteur - taille) / 2 - 2, texte, taille, gras, couleur)
        return largeur

    # -- sortie --------------------------------------------------------------
    def finir(self, chemin, hauteur_utile=None):
        img = self.img
        if hauteur_utile:
            hauteur = min(self.hauteur, max(120, int(hauteur_utile)))
            img = img.crop((0, 0, self.largeur * self.e, hauteur * self.e))
        img = img.resize((img.width // self.e, img.height // self.e), Image.LANCZOS)
        chemin = Path(chemin)
        chemin.parent.mkdir(parents=True, exist_ok=True)
        img.save(chemin, "PNG", optimize=True)
        return chemin


# --- Elements communs --------------------------------------------------------
def _entete(toile, titre, sous_titre="", accent=ACCENT, y=MARGE):
    """Le bandeau du haut : une barre de couleur, un titre, une sous-ligne."""
    toile.rect([MARGE, y + 2, MARGE + 5, y + 42], fond=accent, rayon=3)
    toile.texte(MARGE + 18, y, titre, 25, True, TEXTE,
                largeur_max=toile.largeur - 2 * MARGE - 20)
    if sous_titre:
        toile.texte(MARGE + 18, y + 33, sous_titre, 14, False, TEXTE_FAIBLE,
                    largeur_max=toile.largeur - 2 * MARGE - 20)
        return y + 62
    return y + 50


def _pied(toile, y, note=""):
    """La ligne du bas : quand l'image a ete faite. Une image sans horodatage
    finit toujours par etre relue trois jours plus tard comme si elle etait
    d'aujourd'hui."""
    texte = f"assistant CYU · {datetime.now():%d/%m à %H:%M}"
    if note:
        texte = f"{note} · {texte}"
    toile.ligne([MARGE, y, toile.largeur - MARGE, y], TRAIT)
    toile.texte(MARGE, y + 10, texte, 12, False, TEXTE_FAIBLE)
    return y + 32


def _legende(toile, y, types):
    """Les pastilles de couleur sous une grille. Seulement celles utilisees :
    une legende qui explique des couleurs absentes est du bruit."""
    if not types:
        return y
    x = MARGE
    for nom, couleur in types:
        x += toile.pastille(x, y, nom, couleur) + 7
    return y + 26


def _types_presents(cours):
    vus, sortie = set(), []
    for c in cours:
        if c.a_distance:
            cle, couleur, nom = "dist", COULEUR_DISTANCE, "à distance"
        else:
            cle = c.type_court or "autre"
            couleur = _couleur_cours(c)
            nom = c.type_court or "autre"
        if cle not in vus:
            vus.add(cle)
            sortie.append((nom, couleur))
    return sortie


# --- La meteo, dessinee -------------------------------------------------------
def _nuage(toile, x, y, t, couleur=NUAGE):
    """Un nuage inscrit dans le carre (x, y, t) : trois bosses et un socle."""
    toile.disque(x + 0.34 * t, y + 0.54 * t, 0.19 * t, couleur)
    toile.disque(x + 0.54 * t, y + 0.45 * t, 0.25 * t, couleur)
    toile.disque(x + 0.73 * t, y + 0.57 * t, 0.17 * t, couleur)
    toile.rect([x + 0.20 * t, y + 0.56 * t, x + 0.84 * t, y + 0.74 * t],
               fond=couleur, rayon=0.09 * t)


def _soleil(toile, cx, cy, r, rayons=True):
    toile.disque(cx, cy, r, SOLEIL)
    if not rayons:
        return
    # Huit rayons : quatre droits, quatre en diagonale. Les diagonales sont
    # calculees, pas approchees a 0.7 — un rayon qui depasse se voit.
    from math import cos, pi, sin
    for i in range(8):
        a = i * pi / 4
        dx, dy = cos(a), sin(a)
        toile.ligne([cx + dx * r * 1.35, cy + dy * r * 1.35,
                     cx + dx * r * 1.95, cy + dy * r * 1.95], SOLEIL, 2)


def _gouttes(toile, x, y, t, couleur=PLUIE, n=3):
    for i in range(n):
        gx = x + (0.32 + i * 0.18) * t
        toile.ligne([gx, y + 0.78 * t, gx - 0.05 * t, y + 0.95 * t], couleur, 2.5)


def _icone_meteo(toile, x, y, t, code):
    """Le pictogramme d'un code WMO, dessine dans un carre de cote `t`.

    Pourquoi dessiner plutot qu'ecrire un emoji : la police de secours d'un
    serveur nu n'a aucun emoji, et _nettoyer() les retire donc de toutes les
    images. Un pictogramme vectoriel, lui, sort toujours.
    """
    code = int(code or 0)
    if code in (0,):
        _soleil(toile, x + 0.5 * t, y + 0.5 * t, 0.24 * t)
    elif code in (1, 2):
        _soleil(toile, x + 0.64 * t, y + 0.36 * t, 0.18 * t)
        _nuage(toile, x, y + 0.06 * t, t)
    elif code in (3,):
        _nuage(toile, x, y + 0.02 * t, t, NUAGE)
    elif code in (45, 48):
        for i in range(4):
            largeur = 0.72 if i % 2 == 0 else 0.56
            toile.rect([x + 0.14 * t, y + (0.32 + i * 0.13) * t,
                        x + (0.14 + largeur) * t, y + (0.38 + i * 0.13) * t],
                       fond=BROUILLARD, rayon=0.03 * t)
    elif code in (71, 73, 75, 77, 85, 86):
        _nuage(toile, x, y - 0.06 * t, t, NUAGE)
        for i in range(3):
            toile.disque(x + (0.34 + i * 0.18) * t, y + 0.84 * t, 0.045 * t, NEIGE)
    elif code in (95, 96, 99):
        _nuage(toile, x, y - 0.06 * t, t, NUAGE_SOMBRE)
        toile.polygone([(x + 0.54 * t, y + 0.66 * t), (x + 0.40 * t, y + 0.92 * t),
                        (x + 0.50 * t, y + 0.92 * t), (x + 0.42 * t, y + 1.10 * t),
                        (x + 0.66 * t, y + 0.84 * t), (x + 0.55 * t, y + 0.84 * t),
                        (x + 0.64 * t, y + 0.66 * t)], fond=ECLAIR)
    elif code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        fort = code in (65, 82, 63, 81)
        _nuage(toile, x, y - 0.06 * t, t, NUAGE_SOMBRE if fort else NUAGE)
        _gouttes(toile, x, y - 0.04 * t, t, n=4 if fort else 3)
    else:
        _nuage(toile, x, y + 0.02 * t, t, NUAGE)


def _marge_pluie(depart, arrivee=None):
    """Les minutes a retrancher a l'heure de depart parce qu'il pleut. 0 si la
    meteo est coupee, injoignable, ou s'il fait sec."""
    if not (config.METEO_ACTIVE and config.METEO_BRIEFING) or depart is None:
        return 0
    try:
        import meteo as meteo_
        return meteo_.trajet(depart, arrivee)[1]
    except Exception as e:     # large a dessein : une meteo ratee, ce n'est
        print(f"[!] marge meteo ignoree : {e}", flush=True)   # pas une image ratee
        return 0


def _bande_meteo(toile, y, largeur, jour_, depart=None, arrivee=None):
    """Le bandeau meteo : le temps du jour, et ce qu'il change a ton depart.

    Rend la nouvelle ordonnee. Sans bulletin — pas de reseau, meteo coupee — on
    ne dessine rien du tout et on rend `y` inchange : une carte « meteo
    indisponible » prendrait la place d'une information utile.
    """
    if not (config.METEO_ACTIVE and config.METEO_BRIEFING):
        return y
    try:
        import meteo as meteo_
    except ImportError:
        return y

    data = meteo_.bulletin()
    if not data:
        return y
    resume = meteo_.jour(jour_, data)
    creneaux = meteo_.fenetre(depart, arrivee or depart, data) if depart else []
    gene = meteo_.pire(creneaux)
    if resume is None and gene is None:
        return y

    hauteur = 70 if gene is not None else 54
    toile.rect([MARGE, y, largeur - MARGE, y + hauteur], fond=CARTE, rayon=12)
    # Le pictogramme illustre la ligne d'a cote, donc la JOURNEE — sauf s'il
    # pleut au moment ou tu sors : ce jour-la, c'est la pluie qu'on dessine,
    # meme si la journee est belle par ailleurs.
    _icone_meteo(toile, MARGE + 14, y + (hauteur - 40) / 2, 40,
                 gene.code if (gene is not None and (gene.mouille or resume is None))
                 else resume.code)

    x = MARGE + 68
    if resume is not None:
        toile.texte(x, y + 12, resume.detail(), 16, True, TEXTE,
                    largeur_max=largeur - x - MARGE - 20)
    if gene is not None:
        ligne = f"Au départ de {depart:%H:%M} : {gene.detail()}"
        phrase = meteo_.conseil(creneaux)
        if phrase:
            ligne += f" — {phrase}"
        toile.texte(x, y + (36 if resume is not None else 18), ligne, 13.5, False,
                    PLUIE if gene.mouille else TEXTE_MOYEN,
                    largeur_max=largeur - x - MARGE - 20)
    return y + hauteur + 12


# --- La grille : les jours a la verticale ------------------------------------
def _bornes(cours_visibles):
    """(heure de debut, heure de fin) de la grille, en heures entieres.

    Les bornes de config.yaml sont un minimum, pas un maximum : un examen a
    7 h ou un TP qui finit a 20 h doit rester dans le cadre."""
    h0 = vue.hhmm(config.JOURNEE_DEBUT, (8, 0))[0]
    h1 = vue.hhmm(config.JOURNEE_FIN, (19, 0))[0]
    for c in cours_visibles:
        h0 = min(h0, c.debut.hour)
        if c.fin:
            h1 = max(h1, c.fin.hour + (1 if c.fin.minute else 0))
    return h0, max(h1, h0 + 2)


def _sous_lignes(jc):
    """Repartit les cours d'une journee pour que deux cours qui se chevauchent
    ne se recouvrent pas a l'ecran. En pratique une seule sous-ligne suffit ;
    CELCAT publie parfois des doublons, et on ne veut pas les cacher."""
    lignes = []
    for c in jc:
        fin_c = c.fin or c.debut
        for ligne in lignes:
            if all(c.debut >= (o.fin or o.debut) or fin_c <= o.debut for o in ligne):
                ligne.append(c)
                break
        else:
            lignes.append([c])
    return lignes


def _jours_a_montrer(cours, lundi, debut, fin):
    """Les jours d'une semaine a dessiner. Le week-end n'apparait que s'il
    porte quelque chose : deux lignes vides par semaine, c'est de la place
    prise a la seule chose qu'on regarde."""
    jours = []
    for i in range(7):
        j = lundi + timedelta(days=i)
        if j < debut or j > fin:
            continue
        if i >= 5 and not celcat.du_jour(cours, j):
            continue
        jours.append(j)
    return jours


def _bloc_cours(toile, c, x0, x1, y0, y1):
    """Un cours dans la grille : bloc colore, heure, titre, salle."""
    fond = _couleur_cours(c)
    toile.rect([x0, y0, x1, y1], fond=_melanger(fond, (0, 0, 0), 0.12), rayon=8)
    toile.rect([x0, y0, x0 + 4, y1], fond=_eclaircir(fond, 0.5), rayon=2)
    if c.est_examen:
        toile.rect([x0, y0, x1, y1], rayon=8, contour=_eclaircir(fond, 0.7),
                   epaisseur=1.5)

    large = x1 - x0 - 18
    if large < 26:
        return
    y = y0 + 7
    hauteur = y1 - y0

    if large >= 74:
        toile.texte(x0 + 11, y, c.creneau if c.fin else f"{c.debut:%H:%M}",
                    11, True, _eclaircir(fond, 0.8), largeur_max=large)
        y += 15
    titre = c.titre or c.module or "Cours"
    if hauteur - (y - y0) >= 34 and large >= 110:
        for ligne in toile.envelopper(titre, large, 14, True, lignes_max=2):
            toile.texte(x0 + 11, y, ligne, 14, True, (255, 255, 255))
            y += 17
    else:
        toile.texte(x0 + 11, y, titre, 13, True, (255, 255, 255), largeur_max=large)
        y += 16
    if y1 - y >= 16 and large >= 96:
        detail = " · ".join(b for b in (c.type_court, c.ou) if b)
        toile.texte(x0 + 11, y, detail, 11.5, False, _eclaircir(fond, 0.75),
                    largeur_max=large)


def _dessiner_semaine(toile, y0, cours, jours, h0, h1, maintenant=None):
    """Une semaine : l'axe des heures en haut, puis une LIGNE PAR JOUR."""
    maintenant = maintenant or datetime.now()
    x_lane = MARGE + GOUTTIERE
    largeur_lane = (h1 - h0) * LARGEUR_HEURE
    x_fin = x_lane + largeur_lane

    hauteurs = []
    for j in jours:
        hauteurs.append(HAUTEUR_JOUR * max(1, len(_sous_lignes(celcat.du_jour(cours, j))))
                        if celcat.du_jour(cours, j) else HAUTEUR_JOUR_VIDE)
    hauteur_totale = ENTETE_HEURES + sum(h + ESPACE_LIGNE for h in hauteurs)

    toile.rect([MARGE, y0, x_fin + 10, y0 + hauteur_totale + 6], fond=CARTE, rayon=RAYON)

    # L'axe des heures, en haut, et ses lignes verticales sur toute la hauteur.
    for i in range(h1 - h0 + 1):
        x = x_lane + i * LARGEUR_HEURE
        toile.ligne([x, y0 + ENTETE_HEURES - 6, x, y0 + hauteur_totale],
                    TRAIT_FORT if i % 2 == 0 else TRAIT)
        if i < h1 - h0:
            toile.texte(x + 6, y0 + 8, f"{h0 + i:02d}h", 12, True, TEXTE_FAIBLE)
            demi = x + LARGEUR_HEURE / 2
            toile.ligne([demi, y0 + ENTETE_HEURES - 2, demi, y0 + hauteur_totale],
                        _melanger(CARTE, TRAIT, 0.45))

    aujourd = maintenant.date()
    y = y0 + ENTETE_HEURES
    for jour, hauteur in zip(jours, hauteurs):
        jc = celcat.du_jour(cours, jour)
        est_auj = jour == aujourd

        if est_auj:
            toile.rect([MARGE + 6, y - 3, x_fin + 4, y + hauteur + 3],
                       fond=_melanger(CARTE, ACCENT, 0.16), rayon=10)

        # La colonne des jours, a gauche.
        etiquette = vue.JOURS_COURTS[jour.weekday()].capitalize()
        toile.texte(MARGE + 14, y + 6, etiquette, 15, True,
                    ACCENT if est_auj else TEXTE)
        toile.texte(MARGE + 14, y + 25, f"{jour:%d/%m}", 12.5, False,
                    TEXTE_MOYEN if est_auj else TEXTE_FAIBLE)
        if jc:
            total = sum(c.minutes for c in jc)
            toile.texte(MARGE + 14, y + 43, vue.duree_fr(total), 11.5, False, TEXTE_FAIBLE)

        if not jc:
            autres = celcat.non_cours_du_jour(cours, jour)
            mot = autres[0].titre if autres else "libre"
            toile.texte(x_lane + 12, y + 12, mot.lower(), 13, False, TEXTE_FAIBLE)
        else:
            sous = _sous_lignes(jc)
            haut_sous = hauteur / len(sous)
            for index, ligne in enumerate(sous):
                yy = y + index * haut_sous
                for c in ligne:
                    debut = (c.debut.hour - h0) * 60 + c.debut.minute
                    fin = debut + max(c.minutes, 25)
                    x0 = x_lane + debut * LARGEUR_HEURE / 60
                    x1 = x_lane + fin * LARGEUR_HEURE / 60
                    _bloc_cours(toile, c, max(x0, x_lane + 1), min(x1, x_fin - 1),
                                yy + 2, yy + haut_sous - 4)

        # Le trait « maintenant » : sans lui, on cherche ou on en est.
        if est_auj and h0 <= maintenant.hour < h1:
            x = x_lane + ((maintenant.hour - h0) * 60 + maintenant.minute) * LARGEUR_HEURE / 60
            toile.ligne([x, y - 2, x, y + hauteur + 2], ROUGE, 1.5)
        y += hauteur + ESPACE_LIGNE

    return y0 + hauteur_totale + 6


# --- L'image d'une periode ---------------------------------------------------
def rendre(cours, debut=None, fin=None, chemin=None, titre=None, maintenant=None):
    """Dessine l'emploi du temps de `debut` a `fin`, et rend le chemin du PNG.

    Les semaines s'empilent ; une semaine sans le moindre cours est sautee.
    """
    debut = debut or date.today()
    fin = fin or (debut + timedelta(days=6))
    if fin < debut:
        debut, fin = fin, debut
    # Au-dela de six semaines l'image devient un mur : on coupe, et l'appelant
    # le dit a l'utilisateur.
    fin = min(fin, debut + timedelta(days=41))

    concernes = [c for c in cours if c.est_cours and debut <= c.jour <= fin]
    h0, h1 = _bornes(concernes)

    semaines = []
    lundi = celcat.semaine_de(debut)
    while lundi <= fin:
        jours = _jours_a_montrer(cours, lundi, debut, fin)
        if jours:
            semaines.append((lundi, jours))
        lundi += timedelta(days=7)

    largeur = MARGE * 2 + GOUTTIERE + (h1 - h0) * LARGEUR_HEURE + 10
    hauteur_max = 260 + sum(
        40 + ENTETE_HEURES + sum(
            HAUTEUR_JOUR * 2 + ESPACE_LIGNE for _ in jours)
        for _, jours in semaines) + 120

    toile = Toile(largeur, max(hauteur_max, 300))
    entete = titre or (f"Emploi du temps — {vue.jour_fr(debut, court=True)} "
                       f"au {vue.jour_fr(fin, court=True)}")
    total = sum(c.minutes for c in concernes)
    sous = (f"{len(concernes)} cours · {vue.duree_fr(total)}"
            if concernes else "aucun cours sur cette période")
    y = _entete(toile, entete, sous)

    for lundi, jours in semaines:
        if len(semaines) > 1:
            toile.texte(MARGE + 2, y, f"SEMAINE DU {lundi:%d/%m}", 12, True, TEXTE_FAIBLE)
            y += 20
        # Une semaine sans le moindre cours tient en une bande : cinq lignes
        # « libre » prendraient un tiers de l'image pour ne rien dire.
        if not any(celcat.du_jour(cours, j) for j in jours):
            toile.rect([MARGE, y, largeur - MARGE, y + 46], fond=CARTE, rayon=12)
            feries = [c.titre for j in jours for c in celcat.non_cours_du_jour(cours, j)]
            toile.texte(MARGE + 18, y + 14,
                        feries[0] if feries else "Aucun cours publié sur cette semaine",
                        14, False, TEXTE_FAIBLE)
            y += 64
            continue
        y = _dessiner_semaine(toile, y, cours, jours, h0, h1, maintenant) + 18

    if not semaines:
        toile.rect([MARGE, y, largeur - MARGE, y + 70], fond=CARTE, rayon=RAYON)
        toile.texte(MARGE + 20, y + 26, "Aucun cours sur cette période.",
                    16, True, TEXTE_MOYEN)
        y += 88

    y = _legende(toile, y, _types_presents(concernes))
    y = _pied(toile, y + 6)
    return toile.finir(chemin or (config.DONNEES / "edt.png"), y)


def rendre_semaine(cours, lundi=None, chemin=None):
    """Raccourci : la semaine qui contient `lundi` (celle en cours par defaut)."""
    lundi = lundi or celcat.semaine_de(date.today())
    return rendre(cours, lundi, lundi + timedelta(days=6), chemin,
                  titre=f"Semaine du {lundi:%d/%m}")


# --- L'image d'une journee ---------------------------------------------------
LARGEUR_JOURNEE = 860


def rendre_jour(cours, liste_devoirs=None, jour=None, chemin=None, maintenant=None):
    """Une journee en detail : un bandeau par cours, les trous entre eux.

    C'est la reponse a « c'est quoi ma journee ? » : les heures a gauche en
    colonne, les cours empiles a la verticale, et ce qui manque au texte —
    l'etat d'avancement, les trous, l'heure de lever.
    """
    import devoirs as dv

    jour = jour or date.today()
    maintenant = maintenant or datetime.now()
    jc = celcat.du_jour(cours, jour)
    liste_devoirs = liste_devoirs if liste_devoirs is not None else dv.lire()

    toile = Toile(LARGEUR_JOURNEE, 400 + 150 * max(len(jc), 1) + 200)
    titre = vue.jour_fr(jour).capitalize()
    relatif = vue.jour_relatif(jour, maintenant.date())
    if relatif != titre.lower():
        titre = f"{relatif.capitalize()} — {vue.jour_fr(jour)}"

    if not jc:
        autres = celcat.non_cours_du_jour(cours, jour)
        motif = autres[0].titre if autres else ""
        y = _entete(toile, titre, motif or "aucun cours", VERT)
        toile.rect([MARGE, y, LARGEUR_JOURNEE - MARGE, y + 88], fond=CARTE, rayon=RAYON)
        toile.texte(LARGEUR_JOURNEE / 2, y + 32, "Aucun cours", 20, True,
                    TEXTE_MOYEN, aligne="centre")
        y += 110
        y = _bloc_devoirs_carte(toile, y, liste_devoirs, jour)
        return toile.finir(chemin or (config.DONNEES / "jour.png"), _pied(toile, y + 6))

    total = sum(c.minutes for c in jc)
    fin_journee = jc[-1].fin or jc[-1].debut
    amplitude = (fin_journee - jc[0].debut).total_seconds() / 60
    sous = (f"{jc[0].debut:%H:%M} → {fin_journee:%H:%M} · {len(jc)} cours · "
            f"{vue.duree_fr(total)} de cours")
    if amplitude - total >= config.TROU_MINUTES:
        sous += f" sur {vue.duree_fr(amplitude)} de présence"
    y = _entete(toile, titre, sous)

    # L'heure de lever, en haut : c'est l'information qu'on cherche le soir.
    depart = None
    if jour >= maintenant.date():
        lever, depart = vue.heure_lever(jc[0])
        # La pluie recule l'heure de depart : c'est la seule chose que la meteo
        # a le droit de changer, et elle ne la change que quand il pleut.
        marge = 0
        if not jc[0].a_distance:
            marge = _marge_pluie(depart, jc[0].debut)
            depart -= timedelta(minutes=marge)
        if jour > maintenant.date() or lever > maintenant:
            toile.rect([MARGE, y, LARGEUR_JOURNEE - MARGE, y + 42], fond=CARTE_HAUTE,
                       rayon=10)
            if jc[0].a_distance:
                txt = (f"Premier cours à distance — lever conseillé "
                       f"{lever:%H:%M}, pas de trajet")
            else:
                txt = (f"Lever {lever:%H:%M}   ·   départ {depart:%H:%M}   ·   "
                       f"cours à {jc[0].debut:%H:%M}")
                if marge:
                    txt += f"   ·   +{marge} min de pluie"
            toile.texte(MARGE + 16, y + 12, txt, 14.5, True, TEXTE)
            y += 54

    y = _bande_meteo(toile, y, LARGEUR_JOURNEE, jour, depart, jc[0].debut)

    precedent = None
    for c in jc:
        if precedent and precedent.fin:
            creux = (c.debut - precedent.fin).total_seconds() / 60
            if creux >= config.TROU_MINUTES:
                toile.texte(MARGE + 96, y + 2,
                            f"⋯   {vue.duree_fr(creux)} de trou   "
                            f"({precedent.fin:%H:%M} → {c.debut:%H:%M})",
                            12.5, False, TEXTE_FAIBLE)
                y += 26

        fond = _couleur_cours(c)
        fini = jour < maintenant.date() or (c.fin and maintenant >= c.fin
                                            and jour == maintenant.date())
        encours = jour == maintenant.date() and c.debut <= maintenant < (c.fin or c.debut)
        liees = dv.du_cours(c, liste_devoirs)
        hauteur = 76 + (22 if c.prof else 0) + 22 * len(liees)

        couleur_carte = CARTE if not encours else _melanger(CARTE, fond, 0.22)
        toile.rect([MARGE, y, LARGEUR_JOURNEE - MARGE, y + hauteur],
                   fond=couleur_carte, rayon=12)
        toile.rect([MARGE, y, MARGE + 6, y + hauteur],
                   fond=_assombrir(fond, 0.5) if fini else fond, rayon=3)

        gris = _melanger(TEXTE, CARTE, 0.55)
        couleur_titre = gris if fini else TEXTE
        toile.texte(MARGE + 24, y + 14, f"{c.debut:%H:%M}", 20, True, couleur_titre)
        if c.fin:
            toile.texte(MARGE + 24, y + 40, f"{c.fin:%H:%M}", 14, False, TEXTE_FAIBLE)
            toile.texte(MARGE + 24, y + 60, vue.duree_fr(c.minutes), 11.5, False,
                        TEXTE_FAIBLE)

        x = MARGE + 108
        largeur_dispo = LARGEUR_JOURNEE - MARGE - 20 - x
        toile.texte(x, y + 12, c.titre or c.module, 17, True, couleur_titre,
                    largeur_max=largeur_dispo - 120)

        etat_x = LARGEUR_JOURNEE - MARGE - 16
        if encours:
            reste = (c.fin - maintenant).total_seconds() / 60 if c.fin else 0
            mot = f"▶  en cours · reste {vue.duree_fr(reste)}"
            l = toile.largeur_texte(mot, 12, True) + 14
            toile.pastille(etat_x - l, y + 12, mot, VERT, taille=12)
        elif fini:
            l = toile.largeur_texte("terminé", 12, True) + 14
            toile.pastille(etat_x - l, y + 12, "terminé", TRAIT_FORT, TEXTE_FAIBLE, 12)
        elif jour == maintenant.date():
            mot = vue.compte_a_rebours(c.debut, maintenant)
            l = toile.largeur_texte(mot, 12, True) + 14
            toile.pastille(etat_x - l, y + 12, mot, CARTE_HAUTE, TEXTE_MOYEN, 12)

        xx = x
        if c.type_court:
            xx += toile.pastille(xx, y + 40, c.type_court, fond) + 7
        if c.a_distance:
            xx += toile.pastille(xx, y + 40, "à distance", COULEUR_DISTANCE) + 7
        if not c.a_distance:
            toile.texte(xx + 2, y + 42, "▪ " + c.ou, 14,
                        True, TEXTE_MOYEN if not fini else TEXTE_FAIBLE,
                        largeur_max=largeur_dispo - (xx - x) - 10)

        yy = y + 66
        if c.prof:
            toile.texte(x, yy, "avec " + c.prof.title(), 12.5, False, TEXTE_FAIBLE,
                        largeur_max=largeur_dispo)
            yy += 22
        for d in liees:
            toile.texte(x, yy, "▪ à rendre : " + d["titre"], 12.5, True, ORANGE,
                        largeur_max=largeur_dispo)
            yy += 22

        y += hauteur + 10
        precedent = c

    y = _bloc_devoirs_carte(toile, y + 4, liste_devoirs, jour)
    return toile.finir(chemin or (config.DONNEES / "jour.png"), _pied(toile, y + 6))


def _bloc_devoirs_carte(toile, y, liste_devoirs, jour):
    """Le rappel des devoirs sous une journee : seulement ce qui presse."""
    import devoirs as dv

    restants = [d for d in dv.actifs(liste_devoirs)
                if (r := dv.jours_restants(d)) is None or r <= 7]
    if not restants:
        return y
    hauteur = 44 + 26 * min(len(restants), 5)
    toile.rect([MARGE, y, toile.largeur - MARGE, y + hauteur], fond=CARTE, rayon=12)
    toile.texte(MARGE + 18, y + 13, "À rendre sous 7 jours", 14.5, True, TEXTE)
    yy = y + 40
    for d in restants[:5]:
        reste = dv.jours_restants(d)
        if reste is None:
            quand, couleur = "sans échéance", TEXTE_FAIBLE
        elif reste < 0:
            quand, couleur = f"en retard de {-reste} j", ROUGE
        elif reste == 0:
            quand, couleur = "aujourd'hui", ROUGE
        elif reste == 1:
            quand, couleur = "demain", ORANGE
        else:
            quand, couleur = f"dans {reste} j", TEXTE_FAIBLE
        matiere = f"{d['matiere']} · " if d.get("matiere") else ""
        toile.texte(MARGE + 18, yy, f"• {matiere}{d['titre']}", 13, False, TEXTE_MOYEN,
                    largeur_max=toile.largeur - 2 * MARGE - 160)
        toile.texte(toile.largeur - MARGE - 18, yy, quand, 13, True, couleur,
                    aligne="droite")
        yy += 26
    return y + hauteur + 12


# --- L'image des changements -------------------------------------------------
LARGEUR_CARTE = 880

# Un liseret de couleur par type de changement : le rouge n'est pas la pour
# faire joli, il dit « tu vas rater quelque chose ».
COULEUR_CHANGEMENT = {
    "annule": ROUGE, "deplace_jour": (232, 128, 40), "deplace_heure": (232, 168, 40),
    "salle": (52, 152, 219), "duree": (149, 165, 166), "prof": (155, 120, 200),
    "ajoute": VERT,
}


def rendre_changements(liste, chemin=None, titre=None, sous_titre=""):
    """Les changements d'emploi du temps, un par ligne : avant ➜ apres.

    C'est la ou le texte Discord echouait le plus : deux creneaux et une
    fleche, ca demande des colonnes."""
    import changements as chg

    liste = list(liste)
    toile = Toile(LARGEUR_CARTE, 220 + 96 * max(len(liste), 1) + 140)
    presses = chg.urgents(liste)
    accent = ROUGE if presses else (ORANGE if any(
        c.type in chg.GRAVES for c in liste) else ACCENT)

    y = _entete(toile, titre or chg.titre(liste),
                sous_titre or f"{len(liste)} changement"
                              f"{'s' if len(liste) > 1 else ''} détecté"
                              f"{'s' if len(liste) > 1 else ''}", accent)

    if presses:
        toile.rect([MARGE, y, LARGEUR_CARTE - MARGE, y + 40], fond=_melanger(CARTE, ROUGE, 0.3),
                   rayon=10)
        jours = sorted({j for ch in presses for j in ch.jours})
        libelle = " et ".join(vue.jour_relatif(j) for j in jours[:2])
        toile.texte(MARGE + 16, y + 11, f"⚠   Ça touche {libelle}.", 15, True, TEXTE)
        y += 52

    for ch in liste[:14]:
        c = ch.cours
        couleur = COULEUR_CHANGEMENT.get(ch.type, ACCENT)
        _, mot = chg.ENTETES[ch.type]
        hauteur = 86
        toile.rect([MARGE, y, LARGEUR_CARTE - MARGE, y + hauteur], fond=CARTE, rayon=12)
        toile.rect([MARGE, y, MARGE + 6, y + hauteur], fond=couleur, rayon=3)

        x = MARGE + 22
        largeur_pastille = toile.pastille(x, y + 14, mot, couleur, taille=11.5)
        toile.texte(x + largeur_pastille + 10, y + 13, c.titre or c.module, 16, True,
                    TEXTE, largeur_max=LARGEUR_CARTE - 2 * MARGE - largeur_pastille - 60)

        avant, apres = ch.avant, ch.apres
        y2 = y + 44
        if ch.type == "annule":
            toile.texte(x, y2, f"{vue.jour_fr(avant.jour, court=True)}   "
                               f"{avant.creneau}   ·   {avant.ou}", 14, True,
                        _melanger(TEXTE, CARTE, 0.4))
            toile.ligne([x, y2 + 9, x + toile.largeur_texte(
                f"{vue.jour_fr(avant.jour, court=True)}   {avant.creneau}   ·   {avant.ou}",
                14, True), y2 + 9], TEXTE_FAIBLE, 1.5)
        elif ch.type == "ajoute":
            toile.texte(x, y2, f"{vue.jour_fr(apres.jour, court=True)}   "
                               f"{apres.creneau}   ·   {apres.ou}", 14, True, TEXTE)
        else:
            gauche, droite = _cotes_changement(ch)
            largeur_gauche = toile.largeur_texte(gauche, 14, False)
            toile.texte(x, y2, gauche, 14, False, TEXTE_FAIBLE)
            toile.texte(x + largeur_gauche + 14, y2, "➜", 15, True, couleur)
            toile.texte(x + largeur_gauche + 42, y2, droite, 14.5, True, TEXTE,
                        largeur_max=LARGEUR_CARTE - 2 * MARGE - largeur_gauche - 80)

        quand = apres or avant
        toile.texte(LARGEUR_CARTE - MARGE - 18, y + 15,
                    vue.jour_relatif(quand.jour), 12.5, True, TEXTE_FAIBLE,
                    aligne="droite")
        y += hauteur + 10

    if len(liste) > 14:
        toile.texte(MARGE + 4, y, f"… et {len(liste) - 14} autres changements",
                    13, False, TEXTE_FAIBLE)
        y += 26
    if not liste:
        toile.rect([MARGE, y, LARGEUR_CARTE - MARGE, y + 70], fond=CARTE, rayon=RAYON)
        toile.texte(MARGE + 20, y + 24, "Aucun changement.", 16, True, TEXTE_MOYEN)
        y += 88

    return toile.finir(chemin or (config.DONNEES / "changements.png"),
                       _pied(toile, y + 6))


def _cotes_changement(ch):
    """(avant, apres) en texte court, pour la ligne « ➜ » de la carte."""
    a, b = ch.avant, ch.apres
    if ch.type == "deplace_jour":
        return (f"{vue.jour_fr(a.jour, court=True)}  {a.creneau}",
                f"{vue.jour_fr(b.jour, court=True)}  {b.creneau} · {b.ou}")
    if ch.type == "deplace_heure":
        minutes = (b.debut - a.debut).total_seconds() / 60
        signe = "+" if minutes > 0 else "−"
        return (f"{vue.jour_fr(a.jour, court=True)}  {a.creneau}",
                f"{b.creneau}  ({signe}{vue.duree_fr(minutes)}) · {b.ou}")
    if ch.type == "salle":
        return (f"{vue.jour_fr(a.jour, court=True)} {a.creneau} · {a.ou or 'salle inconnue'}",
                f"{b.ou}")
    if ch.type == "duree":
        return (f"{a.creneau} ({vue.duree_fr(a.minutes)})",
                f"{b.creneau} ({vue.duree_fr(b.minutes)})")
    if ch.type == "prof":
        return (a.prof.title() or "inconnu", b.prof.title() or "inconnu")
    return (a.creneau if a else "", b.creneau if b else "")


# --- L'image des devoirs -----------------------------------------------------
def rendre_devoirs(liste_devoirs=None, chemin=None):
    """Le carnet de devoirs, trie par urgence, avec une barre de couleur."""
    import devoirs as dv

    liste_devoirs = liste_devoirs if liste_devoirs is not None else dv.lire()
    restants = dv.actifs(liste_devoirs)
    toile = Toile(LARGEUR_CARTE, 200 + 92 * max(len(restants), 1) + 120)

    faits = [d for d in liste_devoirs if d.get("fait")]
    y = _entete(toile, "Devoirs",
                f"{len(restants)} en attente · {len(faits)} fait"
                f"{'s' if len(faits) > 1 else ''}",
                ORANGE if restants else VERT)

    if not restants:
        toile.rect([MARGE, y, LARGEUR_CARTE - MARGE, y + 80], fond=CARTE, rayon=RAYON)
        toile.texte(LARGEUR_CARTE / 2, y + 28, "Rien en attente.", 18, True,
                    TEXTE_MOYEN, aligne="centre")
        return toile.finir(chemin or (config.DONNEES / "devoirs.png"),
                           _pied(toile, y + 100))

    for d in restants[:12]:
        reste = dv.jours_restants(d)
        ech = dv.echeance_dt(d)
        if reste is None:
            couleur, quand = TEXTE_FAIBLE, "sans échéance"
        elif reste < 0:
            couleur, quand = ROUGE, f"EN RETARD de {-reste} j"
        elif reste == 0:
            couleur, quand = ROUGE, "pour aujourd'hui"
        elif reste == 1:
            couleur, quand = ORANGE, "pour demain"
        elif reste <= 3:
            couleur, quand = (219, 176, 38), f"dans {reste} jours"
        else:
            couleur, quand = ACCENT, f"dans {reste} jours"

        hauteur = 74 + (20 if d.get("note") else 0)
        toile.rect([MARGE, y, LARGEUR_CARTE - MARGE, y + hauteur], fond=CARTE, rayon=12)
        toile.rect([MARGE, y, MARGE + 6, y + hauteur], fond=couleur, rayon=3)

        x = MARGE + 22
        toile.texte(x, y + 12, f"#{d['id']}", 13, True, TEXTE_FAIBLE)
        toile.texte(x + 38, y + 10, d["titre"], 16.5, True, TEXTE,
                    largeur_max=LARGEUR_CARTE - 2 * MARGE - 230)
        largeur = toile.largeur_texte(quand, 12, True) + 14
        toile.pastille(LARGEUR_CARTE - MARGE - 18 - largeur, y + 11, quand, couleur,
                       taille=12)

        bas = []
        if d.get("matiere"):
            bas.append(d["matiere"])
        if ech is not None:
            bas.append(vue.jour_fr(ech.date())
                       + (f" à {ech:%H:%M}" if ech.strftime("%H:%M") not in
                          ("23:59", "00:00") else ""))
        if d.get("type") and d["type"] != "devoir":
            bas.append(d["type"])
        toile.texte(x + 38, y + 40, "  ·  ".join(bas), 13, False, TEXTE_MOYEN,
                    largeur_max=LARGEUR_CARTE - 2 * MARGE - 60)
        if d.get("note"):
            toile.texte(x + 38, y + 60, "↳ " + d["note"], 12.5, False, TEXTE_FAIBLE,
                        largeur_max=LARGEUR_CARTE - 2 * MARGE - 60)
        y += hauteur + 10

    if len(restants) > 12:
        toile.texte(MARGE + 4, y, f"… et {len(restants) - 12} autres", 13, False,
                    TEXTE_FAIBLE)
        y += 26
    return toile.finir(chemin or (config.DONNEES / "devoirs.png"), _pied(toile, y + 6))


# --- L'image du prochain cours -----------------------------------------------
def rendre_prochain(cours, liste_devoirs=None, chemin=None, maintenant=None):
    """Le prochain cours en grand, plus ce qui suit dans la journee."""
    import devoirs as dv

    maintenant = maintenant or datetime.now()
    actuel = celcat.en_cours(cours, maintenant)
    suivant = celcat.prochain(cours, maintenant)
    toile = Toile(LARGEUR_CARTE, 720)

    if not actuel and not suivant:
        y = _entete(toile, "Prochain cours", "plus rien de prévu", VERT)
        toile.rect([MARGE, y, LARGEUR_CARTE - MARGE, y + 80], fond=CARTE, rayon=RAYON)
        toile.texte(LARGEUR_CARTE / 2, y + 28, "Aucun cours à venir.", 18, True,
                    TEXTE_MOYEN, aligne="centre")
        return toile.finir(chemin or (config.DONNEES / "prochain.png"),
                           _pied(toile, y + 100))

    vedette = actuel or suivant
    couleur = _couleur_cours(vedette)
    y = _entete(toile, "En ce moment" if actuel else "Prochain cours",
                vue.jour_relatif(vedette.jour, maintenant.date()).capitalize()
                + f" · {vedette.creneau}", couleur)

    hauteur = 150
    toile.rect([MARGE, y, LARGEUR_CARTE - MARGE, y + hauteur],
               fond=_melanger(CARTE, couleur, 0.16), rayon=14)
    toile.rect([MARGE, y, MARGE + 7, y + hauteur], fond=couleur, rayon=3)

    x = MARGE + 26
    toile.texte(x, y + 18, vedette.titre or vedette.module, 26, True, TEXTE,
                largeur_max=LARGEUR_CARTE - 2 * MARGE - 60)
    xx = x
    if vedette.type_court:
        xx += toile.pastille(xx, y + 56, vedette.type_court, couleur) + 8
    if vedette.a_distance:
        xx += toile.pastille(xx, y + 56, "à distance", COULEUR_DISTANCE) + 8
    if not vedette.a_distance:
        toile.texte(xx + 2, y + 57, "▪ " + vedette.ou, 15, True, TEXTE)
    ligne_bas = []
    if vedette.prof:
        ligne_bas.append("avec " + vedette.prof.title())
    ligne_bas.append(f"{vue.duree_fr(vedette.minutes)} de cours")
    toile.texte(x, y + 86, "     ".join(ligne_bas), 13.5, False, TEXTE_MOYEN,
                largeur_max=LARGEUR_CARTE - 2 * MARGE - 60)

    if actuel and actuel.fin:
        reste = (actuel.fin - maintenant).total_seconds() / 60
        info = f"fini dans {vue.duree_fr(reste)}"
    else:
        info = vue.compte_a_rebours(vedette.debut, maintenant)
    toile.texte(x, y + 112, info, 19, True, _eclaircir(couleur, 0.55))
    y += hauteur + 14

    # L'heure de partir : la seule chose qui change ce qu'on fait maintenant.
    if suivant and not actuel and not suivant.a_distance:
        _, depart = vue.heure_lever(suivant)
        if depart > maintenant:
            toile.rect([MARGE, y, LARGEUR_CARTE - MARGE, y + 40], fond=CARTE_HAUTE,
                       rayon=10)
            toile.texte(MARGE + 16, y + 11,
                        f"Partir à {depart:%H:%M}  ·  "
                        f"{vue.compte_a_rebours(depart, maintenant)}", 14.5, True, TEXTE)
            y += 52

    if actuel and suivant:
        y = _carte_suite(toile, y, "Ensuite", [suivant], maintenant)

    apres = [c for c in celcat.du_jour(cours, vedette.jour) if c.debut > vedette.debut]
    if actuel and suivant:
        apres = [c for c in apres if c.debut > suivant.debut]
    if apres:
        y = _carte_suite(toile, y, f"La suite de {vue.jour_relatif(vedette.jour)}",
                         apres[:4], maintenant)

    liees = dv.du_cours(vedette, liste_devoirs)
    if liees:
        hauteur = 40 + 24 * len(liees)
        toile.rect([MARGE, y, LARGEUR_CARTE - MARGE, y + hauteur], fond=CARTE, rayon=12)
        toile.texte(MARGE + 18, y + 12, "À rendre pour ce cours", 14, True, ORANGE)
        yy = y + 36
        for d in liees:
            toile.texte(MARGE + 18, yy, "• " + d["titre"], 13, False, TEXTE_MOYEN,
                        largeur_max=LARGEUR_CARTE - 2 * MARGE - 40)
            yy += 24
        y += hauteur + 10

    return toile.finir(chemin or (config.DONNEES / "prochain.png"), _pied(toile, y + 6))


def _carte_suite(toile, y, titre, liste, maintenant):
    hauteur = 40 + 28 * len(liste)
    toile.rect([MARGE, y, toile.largeur - MARGE, y + hauteur], fond=CARTE, rayon=12)
    toile.texte(MARGE + 18, y + 12, titre, 13.5, True, TEXTE_FAIBLE)
    yy = y + 36
    for c in liste:
        couleur = _couleur_cours(c)
        toile.rect([MARGE + 18, yy + 4, MARGE + 22, yy + 16], fond=couleur, rayon=2)
        toile.texte(MARGE + 32, yy, c.creneau, 13, True, TEXTE_MOYEN)
        toile.texte(MARGE + 132, yy, c.titre, 13.5, True, TEXTE,
                    largeur_max=toile.largeur - 2 * MARGE - 320)
        toile.texte(toile.largeur - MARGE - 18, yy, c.ou, 12.5, False, TEXTE_FAIBLE,
                    aligne="droite")
        yy += 28
    return y + hauteur + 10


# --- Le tableau de bord de la semaine ----------------------------------------
LARGEUR_STATS = 900

# L'ordre des types dans une barre empilee est FIXE, jamais celui du tri : une
# matiere qui perd son TP ne doit pas repeindre les autres. C'est la meme regle
# que pour la legende — une couleur appartient a un type, pas a un rang.
ORDRE_TYPES = ["CM", "TD", "TP", "PROJET", "EXAMEN", "EXAM", "DS", "CONTROLE",
               "SOUTENANCE", "à distance", "autre"]


def _couleur_type(nom):
    if nom == "à distance":
        return COULEUR_DISTANCE
    return COULEURS.get(str(nom).upper(), COULEURS[""])


def _tuile(toile, x, y, largeur, hauteur, titre, valeur, detail="",
           accent=ACCENT, detail_couleur=None):
    """Une tuile de chiffre : le libelle en petit, la valeur en grand.

    Le chiffre est la raison d'etre de la tuile : il est donc le plus gros
    element, et le libelle passe au-dessus en gris — l'inverse se lit deux fois
    moins vite.
    """
    toile.rect([x, y, x + largeur, y + hauteur], fond=CARTE, rayon=12)
    toile.rect([x, y + 14, x + 4, y + hauteur - 14], fond=accent, rayon=2)
    toile.texte(x + 16, y + 13, titre.upper(), 10.5, True, TEXTE_FAIBLE,
                largeur_max=largeur - 26)
    toile.texte(x + 15, y + 30, valeur, 26, True, TEXTE, largeur_max=largeur - 26)
    if detail:
        toile.texte(x + 16, y + hauteur - 24, detail, 12, False,
                    detail_couleur or TEXTE_MOYEN, largeur_max=largeur - 26)
    return hauteur


def _axe_heures(toile, x0, x1, y0, y1, maxi):
    """La grille de fond d'un graphique en barres : un trait toutes les heures
    rondes, et les heures ecrites sous l'axe.

    La grille est volontairement a peine visible : elle sert a comparer deux
    barres, pas a etre regardee. Le pas s'adapte pour ne jamais depasser huit
    traits, sinon la grille devient une texture.
    """
    if maxi <= 0:
        return
    pas = 60
    while maxi / pas > 8:
        pas += 60
    h = pas
    while h <= maxi:
        x = x0 + (h / maxi) * (x1 - x0)
        toile.ligne([x, y0, x, y1], TRAIT, 1)
        toile.texte(x, y1 + 6, f"{int(h / 60)} h", 10.5, False, TEXTE_FAIBLE,
                    aligne="centre")
        h += pas


def _barre_empilee(toile, x0, x1, y, hauteur, segments, maxi):
    """Une barre faite de segments colores, separes par un vrai trou.

    Le trou de 2 px entre deux segments n'est pas cosmetique : sans lui, deux
    couleurs voisines se touchent et l'oeil lit une seule barre degradee au
    lieu de deux parts. Les segments trop fins pour etre vus sont fusionnes
    dans le precedent plutot que dessines en traits de 1 px illisibles.
    """
    if maxi <= 0:
        return
    echelle = (x1 - x0) / maxi
    x = x0
    dessines = []
    for nom, minutes in segments:
        largeur = minutes * echelle
        if largeur < 6 and dessines:
            dessines[-1] = (dessines[-1][0], dessines[-1][1], dessines[-1][2] + largeur)
            continue
        dessines.append((nom, _couleur_type(nom), largeur))
    for i, (_, couleur, largeur) in enumerate(dessines):
        fin = x + largeur - (2 if i < len(dessines) - 1 else 0)
        toile.rect([x, y, max(fin, x + 3), y + hauteur], fond=couleur,
                   rayon=min(4, hauteur / 2))
        x += largeur


def rendre_stats(s, chemin=None, cours=None, avec_historique=True,
                 avertissement=""):
    """Le tableau de bord d'une semaine : combien, ou, et quand.

    `s` est un stats.Semaine deja calcule — ce module dessine, il ne compte
    pas. Trois etages, du plus resume au plus detaille : les tuiles de
    chiffres, la repartition par matiere, puis la charge jour par jour.
    """
    import stats as st

    toile = Toile(LARGEUR_STATS,
                  400 + 44 * max(len(s.matieres), 1) + 44 * 7 + 220)
    y = _entete(toile, "Ta semaine", f"{s.libelle} · {s.seances} séances")

    if s.vide:
        toile.rect([MARGE, y, LARGEUR_STATS - MARGE, y + 90], fond=CARTE, rayon=RAYON)
        toile.texte(LARGEUR_STATS / 2, y + 32, "Aucun cours cette semaine-là.",
                    19, True, TEXTE_MOYEN, aligne="centre")
        return toile.finir(chemin or (config.DONNEES / "stats.png"),
                           _pied(toile, y + 110))

    # --- Etage 1 : les quatre chiffres qu'on retient ------------------------
    ecart, origine = st.comparer(s, cours)
    if ecart is None:
        detail_total, couleur_total = f"sur {s.jours_travailles} jours", TEXTE_MOYEN
    elif abs(ecart) < 15:
        detail_total, couleur_total = "comme la semaine passée", TEXTE_MOYEN
    else:
        fleche = "▲" if ecart > 0 else "▼"
        detail_total = f"{fleche} {vue.duree_fr(abs(ecart))} vs S-1 ({origine})"
        couleur_total = ORANGE if ecart > 0 else VERT

    plein = s.jour_plein
    largeur_tuile = (LARGEUR_STATS - 2 * MARGE - 3 * 12) / 4
    tuiles = [
        ("Total de cours", vue.duree_fr(s.total_minutes), detail_total, ACCENT,
         couleur_total),
        ("Moyenne par jour", vue.duree_fr(s.moyenne_par_jour),
         f"{s.seances} séances au total", COULEURS["TD"], None),
        ("Temps de trou", vue.duree_fr(s.trous_minutes) if s.trous_minutes else "aucun",
         f"{s.nb_trous} créneau{'x' if s.nb_trous > 1 else ''}" if s.nb_trous
         else "journées compactes",
         ORANGE if s.trous_minutes >= 240 else COULEURS["TP"], None),
        ("Jour le plus lourd",
         vue.JOURS[plein.jour.weekday()].capitalize() if plein else "—",
         f"{vue.duree_fr(plein.minutes)} · {plein.debut:%H:%M}→{plein.fin:%H:%M}"
         if plein else "", COULEURS["EXAMEN"] if plein and plein.minutes >= 480
         else COULEURS["CM"], None),
    ]
    for i, (titre, valeur, detail, accent, couleur) in enumerate(tuiles):
        _tuile(toile, MARGE + i * (largeur_tuile + 12), y, largeur_tuile, 96,
               titre, valeur, detail, accent, couleur)
    y += 96 + 18

    # Ce que les chiffres ne peuvent pas dire, ecrit en toutes lettres et en
    # haut : une statistique incomplete doit s'annoncer avant d'etre lue.
    if avertissement:
        toile.rect([MARGE, y, LARGEUR_STATS - MARGE, y + 40], fond=CARTE, rayon=10)
        toile.rect([MARGE, y, MARGE + 5, y + 40], fond=ORANGE, rayon=3)
        toile.texte(MARGE + 18, y + 12, avertissement, 13, False, TEXTE_MOYEN,
                    largeur_max=LARGEUR_STATS - 2 * MARGE - 40)
        y += 52

    # Une ligne d'avertissement quand il y a un examen : ca ne se decouvre pas
    # au detour d'une barre de couleur.
    if s.examens:
        toile.rect([MARGE, y, LARGEUR_STATS - MARGE, y + 40], fond=CARTE, rayon=10)
        toile.rect([MARGE, y, MARGE + 5, y + 40], fond=COULEURS["EXAMEN"], rayon=3)
        jours_examens = ", ".join(
            f"{vue.JOURS_COURTS[c.jour.weekday()]}. {c.debut:%H:%M}"
            for c in s.examens[:4])
        toile.texte(MARGE + 18, y + 11,
                    f"{len(s.examens)} examen{'s' if len(s.examens) > 1 else ''} "
                    f"cette semaine — {jours_examens}", 14, True, TEXTE,
                    largeur_max=LARGEUR_STATS - 2 * MARGE - 40)
        y += 52

    # --- Etage 2 : ou passe le temps ---------------------------------------
    toile.texte(MARGE, y, "Où passe ton temps", 16, True, TEXTE)
    y += 28

    montrees = list(s.matieres[:9])
    reste = s.matieres[9:]
    maxi = max([p.minutes for p in montrees] +
               ([sum(p.minutes for p in reste)] if reste else []))

    x_label, x_bar = MARGE + 4, MARGE + 210
    x_fin = LARGEUR_STATS - MARGE - 86
    # Le pourcentage est colle a gauche des barres : l'intitule s'arrete avant,
    # sinon un nom long lui passe dessus.
    largeur_nom = x_bar - x_label - 52
    haut_zone = y
    for p in montrees:
        toile.texte(x_label, y + 3, p.nom, 13.5, False, TEXTE,
                    largeur_max=largeur_nom)
        toile.rect([x_bar, y + 4, x_fin, y + 22], fond=CARTE, rayon=4)
        y += 34
    if reste:
        toile.texte(x_label, y + 3, f"{len(reste)} autres matières", 13.5, False,
                    TEXTE_FAIBLE, largeur_max=largeur_nom)
        toile.rect([x_bar, y + 4, x_fin, y + 22], fond=CARTE, rayon=4)
        y += 34

    # La grille passe DERRIERE les barres : on la dessine avant, puis les
    # barres par-dessus. L'inverse hachure les couleurs.
    _axe_heures(toile, x_bar, x_fin, haut_zone + 2, y - 8, maxi)

    y = haut_zone
    for p in montrees:
        segments = [(nom, p.par_type[nom]) for nom in ORDRE_TYPES if nom in p.par_type]
        segments += [(nom, m) for nom, m in p.par_type.items() if nom not in ORDRE_TYPES]
        _barre_empilee(toile, x_bar, x_fin, y + 4, 18, segments, maxi)
        toile.texte(LARGEUR_STATS - MARGE - 4, y + 4, vue.duree_fr(p.minutes),
                    13.5, True, TEXTE, aligne="droite")
        toile.texte(x_bar - 10, y + 5, f"{p.part_de(s.total_minutes):.0f} %", 11.5,
                    False, TEXTE_FAIBLE, aligne="droite")
        y += 34
    if reste:
        minutes = sum(p.minutes for p in reste)
        _barre_empilee(toile, x_bar, x_fin, y + 4, 18, [("autre", minutes)], maxi)
        toile.texte(LARGEUR_STATS - MARGE - 4, y + 4, vue.duree_fr(minutes), 13.5,
                    True, TEXTE_MOYEN, aligne="droite")
        y += 34
    y += 24

    # --- Etage 3 : la charge jour par jour ---------------------------------
    toile.texte(MARGE, y, "Jour par jour", 16, True, TEXTE)
    toile.texte(LARGEUR_STATS - MARGE, y + 3,
                "barre pleine : cours   ·   barre claire : présence, trous compris",
                11.5, False, TEXTE_FAIBLE, aligne="droite")
    y += 28

    jours_montres = [j for j in s.jours if not j.vide or j.jour.weekday() < 5]
    maxi_jour = max([j.amplitude for j in jours_montres] + [60])
    # « 7 h 45  +1 h 15 » est deux fois plus large que « 6 h » : la zone des
    # barres s'arrete plus tot, sinon l'etiquette ecrit sur la barre pleine.
    x_fin = LARGEUR_STATS - MARGE - 140
    haut_zone = y
    for j in jours_montres:
        toile.rect([x_bar, y + 4, x_fin, y + 22], fond=CARTE, rayon=4)
        y += 34
    _axe_heures(toile, x_bar, x_fin, haut_zone + 2, y - 8, maxi_jour)

    y = haut_zone
    for j in jours_montres:
        etiquette = f"{vue.JOURS_COURTS[j.jour.weekday()]}. {j.jour:%d/%m}"
        toile.texte(x_label, y + 3, etiquette, 13.5, j.jour == date.today(),
                    TEXTE if not j.vide else TEXTE_FAIBLE)
        if j.vide:
            # « libre » serait un mensonge pour un jour que CELCAT ne sert
            # plus : on ne sait pas ce qu'il y avait, on le dit.
            toile.texte(x_bar + 8, y + 5,
                        "hors données" if (avertissement and j.jour < date.today())
                        else "libre", 12, False, TEXTE_FAIBLE)
            y += 34
            continue
        # La presence d'abord, en clair : les cours viennent se poser dessus, et
        # ce qui depasse EST le temps perdu. Deux mesures, une seule echelle.
        largeur_presence = (j.amplitude / maxi_jour) * (x_fin - x_bar)
        toile.rect([x_bar, y + 4, x_bar + max(largeur_presence, 4), y + 22],
                   fond=CARTE_HAUTE, rayon=4)
        segments = [(nom, j.par_type[nom]) for nom in ORDRE_TYPES if nom in j.par_type]
        segments += [(nom, m) for nom, m in j.par_type.items() if nom not in ORDRE_TYPES]
        _barre_empilee(toile, x_bar, x_fin, y + 4, 18,
                       segments or [("autre", j.minutes)], maxi_jour)
        detail = vue.duree_fr(j.minutes)
        if j.trous_minutes:
            detail += f"  +{vue.duree_fr(j.trous_minutes)}"
        toile.texte(LARGEUR_STATS - MARGE - 4, y + 4, detail, 13, True,
                    TEXTE, aligne="droite")
        y += 34
    y += 18

    # La legende ne montre que les types reellement presents dans la semaine.
    types = []
    vus = set()
    for p in s.matieres:
        for nom in ORDRE_TYPES + list(p.par_type):
            if nom in p.par_type and nom not in vus:
                vus.add(nom)
                types.append((nom, _couleur_type(nom)))
    y = _legende(toile, y, types)

    note = ""
    if avec_historique:
        passees = st.historique(5)
        if len(passees) >= 2:
            moyenne = sum(m for _, m in passees) / len(passees)
            note = (f"moyenne des {len(passees)} dernières semaines connues : "
                    f"{vue.duree_fr(moyenne)}")
    return toile.finir(chemin or (config.DONNEES / "stats.png"),
                       _pied(toile, y + 4, note))


# --- Le bilan : tout l'emploi du temps connu ---------------------------------
def rendre_bilan(b, chemin=None, cours=None):
    """Le poids de TOUT ce que CELCAT connait : heures par matiere, et la
    charge semaine par semaine. C'est la reponse a « combien d'heures de VBA
    ce semestre », que la vue d'une semaine ne peut pas donner.

    Meme grammaire que rendre_stats() : tuiles, barres empilees par type,
    grille recessive. Un lecteur qui connait l'une lit l'autre.
    """
    import stats as st

    toile = Toile(LARGEUR_STATS,
                  380 + 34 * max(len(b.matieres), 1) + 34 * max(len(b.semaines), 1) + 200)
    y = _entete(toile, "Tout l'emploi du temps",
                f"{b.libelle} · {b.seances} séances · "
                f"{b.nb_semaines} semaine{'s' if b.nb_semaines > 1 else ''} avec cours")

    if b.vide:
        toile.rect([MARGE, y, LARGEUR_STATS - MARGE, y + 90], fond=CARTE, rayon=RAYON)
        toile.texte(LARGEUR_STATS / 2, y + 32, "CELCAT ne connaît aucun cours.",
                    19, True, TEXTE_MOYEN, aligne="centre")
        return toile.finir(chemin or (config.DONNEES / "bilan.png"),
                           _pied(toile, y + 110))

    # --- Etage 1 : les quatre chiffres --------------------------------------
    pleine = b.semaine_pleine
    plus_lourde = b.matieres[0]
    largeur_tuile = (LARGEUR_STATS - 2 * MARGE - 3 * 12) / 4
    tuiles = [
        ("Total de cours", vue.duree_fr(b.total_minutes),
         f"{b.seances} séances", ACCENT),
        ("Par semaine", vue.duree_fr(b.moyenne_semaine),
         f"la plus chargée : {vue.duree_fr(pleine[1])}" if pleine else "",
         COULEURS["TD"]),
        ("Matières", str(len(b.matieres)),
         f"la plus lourde : {plus_lourde.nom}"[:40], COULEURS["TP"]),
        ("À distance", vue.duree_fr(b.distanciel_minutes) if b.distanciel_minutes else "aucun",
         f"{b.part_distanciel:.0f} % du total" if b.distanciel_minutes else "tout en présentiel",
         COULEUR_DISTANCE),
    ]
    for i, (titre, valeur, detail, accent) in enumerate(tuiles):
        _tuile(toile, MARGE + i * (largeur_tuile + 12), y, largeur_tuile, 96,
               titre, valeur, detail, accent)
    y += 96 + 18

    if b.examens:
        toile.rect([MARGE, y, LARGEUR_STATS - MARGE, y + 40], fond=CARTE, rayon=10)
        toile.rect([MARGE, y, MARGE + 5, y + 40], fond=COULEURS["EXAMEN"], rayon=3)
        quand = ", ".join(f"{vue.jour_fr(c.jour, court=True)}" for c in b.examens[:5])
        toile.texte(MARGE + 18, y + 11,
                    f"{len(b.examens)} examen{'s' if len(b.examens) > 1 else ''} "
                    f"dans la période — {quand}", 14, True, TEXTE,
                    largeur_max=LARGEUR_STATS - 2 * MARGE - 40)
        y += 52

    # --- Etage 2 : les heures par matiere, en part du total -----------------
    toile.texte(MARGE, y, "Heures par matière", 16, True, TEXTE)
    toile.texte(LARGEUR_STATS - MARGE, y + 3,
                f"le pourcentage est la part du total de {vue.duree_fr(b.total_minutes)}",
                11.5, False, TEXTE_FAIBLE, aligne="droite")
    y += 28

    montrees = list(b.matieres[:12])
    reste = b.matieres[12:]
    maxi = max([p.minutes for p in montrees] +
               ([sum(p.minutes for p in reste)] if reste else []))
    x_label, x_bar = MARGE + 4, MARGE + 210
    x_fin = LARGEUR_STATS - MARGE - 86
    largeur_nom = x_bar - x_label - 52

    haut_zone = y
    for _ in montrees + ([None] if reste else []):
        toile.rect([x_bar, y + 4, x_fin, y + 22], fond=CARTE, rayon=4)
        y += 34
    _axe_heures(toile, x_bar, x_fin, haut_zone + 2, y - 8, maxi)

    y = haut_zone
    for p in montrees:
        toile.texte(x_label, y + 3, p.nom, 13.5, False, TEXTE, largeur_max=largeur_nom)
        segments = [(nom, p.par_type[nom]) for nom in ORDRE_TYPES if nom in p.par_type]
        segments += [(nom, m) for nom, m in p.par_type.items() if nom not in ORDRE_TYPES]
        _barre_empilee(toile, x_bar, x_fin, y + 4, 18, segments, maxi)
        toile.texte(x_bar - 10, y + 5, f"{p.part_de(b.total_minutes):.0f} %", 11.5,
                    False, TEXTE_FAIBLE, aligne="droite")
        toile.texte(LARGEUR_STATS - MARGE - 4, y + 4, vue.duree_fr(p.minutes),
                    13.5, True, TEXTE, aligne="droite")
        y += 34
    if reste:
        minutes = sum(p.minutes for p in reste)
        toile.texte(x_label, y + 3, f"{len(reste)} autres matières", 13.5, False,
                    TEXTE_FAIBLE, largeur_max=largeur_nom)
        _barre_empilee(toile, x_bar, x_fin, y + 4, 18, [("autre", minutes)], maxi)
        toile.texte(LARGEUR_STATS - MARGE - 4, y + 4, vue.duree_fr(minutes), 13.5,
                    True, TEXTE_MOYEN, aligne="droite")
        y += 34
    y += 24

    # --- Etage 3 : semaine par semaine --------------------------------------
    toile.texte(MARGE, y, "Semaine par semaine", 16, True, TEXTE)
    y += 28
    maxi_semaine = max([m for _, m, _ in b.semaines] + [60])
    cette = celcat.semaine_de(date.today())
    x_fin = LARGEUR_STATS - MARGE - 190       # place pour « 30 h 15 · 12 séances »
    haut_zone = y
    for _ in b.semaines:
        toile.rect([x_bar, y + 4, x_fin, y + 22], fond=CARTE, rayon=4)
        y += 34
    _axe_heures(toile, x_bar, x_fin, haut_zone + 2, y - 8, maxi_semaine)

    y = haut_zone
    for lundi, minutes, seances in b.semaines:
        etiquette = f"du {lundi:%d/%m} au {(lundi + timedelta(days=6)):%d/%m}"
        toile.texte(x_label, y + 3, etiquette, 13.5, lundi == cette,
                    TEXTE if minutes else TEXTE_FAIBLE)
        if not minutes:
            toile.texte(x_bar + 8, y + 5, "rien de prévu", 12, False, TEXTE_FAIBLE)
            y += 34
            continue
        par_type = st.par_type_semaine(cours or [], lundi) if cours else {}
        segments = [(nom, par_type[nom]) for nom in ORDRE_TYPES if nom in par_type]
        segments += [(nom, m) for nom, m in par_type.items() if nom not in ORDRE_TYPES]
        _barre_empilee(toile, x_bar, x_fin, y + 4, 18,
                       segments or [("autre", minutes)], maxi_semaine)
        toile.texte(LARGEUR_STATS - MARGE - 4, y + 4,
                    f"{vue.duree_fr(minutes)}  ·  {seances} séance{'s' if seances > 1 else ''}",
                    13, True, TEXTE, aligne="droite")
        y += 34
    y += 18

    types, vus = [], set()
    for p in b.matieres:
        for nom in ORDRE_TYPES + list(p.par_type):
            if nom in p.par_type and nom not in vus:
                vus.add(nom)
                types.append((nom, _couleur_type(nom)))
    y = _legende(toile, y, types)
    return toile.finir(chemin or (config.DONNEES / "bilan.png"),
                       _pied(toile, y + 4, "tout ce que CELCAT connaît à ce jour"))
