#!/usr/bin/env python3
"""
Le calendrier d'alternance : quand on est en cours, quand on est en entreprise.

D'ou viennent ces dates
-----------------------
D'un seul fichier : le calendrier officiel de la promo, le PDF pose a la
racine du projet. Il n'est PAS recopie dans le code : quand l'ecole en publie
une nouvelle version, tu remplaces le fichier et /calendrier suit.

Pourquoi un parseur maison
--------------------------
Ce PDF-la n'a aucun texte : les mois, les jours et les numeros de semaine ont
ete convertis en courbes a l'export, et `extract_text()` d'une bibliotheque
PDF ne rend rien du tout. Ce qui reste lisible, c'est la COULEUR des cases --
et c'est justement la seule chose qui nous interesse, puisque la legende du
document dit ce que chaque couleur veut dire :

    bleu nuit  en entreprise        vert   examens
    orange     formation au centre  vert pale  rattrapages

Lire des rectangles colores ne vaut donc pas d'ajouter pdfplumber ou PyMuPDF
aux dependances d'un bot Discord : un flux PDF est du zlib (stdlib) et une
suite d'operateurs de dessin, et l'interprete tient en une centaine de lignes
-- la meme raison qui fait lire le classeur M3C avec zipfile dans maquette.py.

Comment on retrouve les dates sans texte
----------------------------------------
La grille est reguliere : treize colonnes de mois, trente et une lignes de
jours. On sait donc que telle case est « la 3e ligne de la 5e colonne », mais
pas de quel mois ni de quelle annee part le tableau -- c'est ecrit dans le
titre, en courbes, illisible.

On le DEDUIT, en essayant les annees plausibles et en gardant celle qui colle
(voir _caler) : une annee scolaire laisse une empreinte assez precise pour ca.
Aucune case de cours ne tombe un samedi, et les cases grises tombent pile sur
les jours feries francais de l'annee. Une seule annee candidate satisfait les
deux, et si deux se valent, on le dit au lieu d'inventer.

Ce que le module NE fait PAS
----------------------------
Il ne connait aucun horaire ni aucune salle : le calendrier dit dans quel
MONDE on est un jour donne (l'ecole ou l'entreprise), CELCAT dit ce qu'on y
fait. Les deux se completent, ils ne se remplacent pas.

La structure rendue :

    Calendrier        l'annee entiere : des jours, des periodes, une source
      Jour            une date et sa nature
      Periode         un bloc continu de meme nature : « du 31 aout au
                      18 septembre, formation », week-ends compris
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import config

# Le fichier cherche a la racine, dans l'ordre : ce que dit config.yaml, puis
# tout PDF dont le nom parle de calendrier ou d'alternance. Le second cas est
# celui de tout le monde : on depose le fichier de l'ecole, sans rien regler.
MOTIFS = ("calendrier*.pdf", "*alternance*.pdf")

# Les natures d'une journee. L'ordre compte : il sert a trier les legendes.
ENTREPRISE = "entreprise"
FORMATION = "formation"
EXAMEN = "examen"
RATTRAPAGE = "rattrapage"
FERIE = "ferie"
WEEKEND = "weekend"
HORS = "hors"

# Ce que dit la legende du PDF, couleur par couleur. Les teintes sont celles
# du document officiel ; la comparaison est tolerante (voir _nature_couleur),
# pour qu'un reexport qui decale une teinte d'un point ne casse pas tout.
COULEURS_PDF = {
    (0.0, 0.0, 0.501961): ENTREPRISE,
    (1.0, 0.4, 0.0): FORMATION,
    (0.607843, 0.733333, 0.34902): EXAMEN,
    (0.921569, 0.945098, 0.870588): RATTRAPAGE,
    (0.501961, 0.501961, 0.501961): FERIE,
}

# Le nom de chaque nature, tel qu'il s'affiche.
NOMS = {
    ENTREPRISE: "En entreprise",
    FORMATION: "Formation au centre",
    EXAMEN: "Examens",
    RATTRAPAGE: "Rattrapages",
    FERIE: "Férié",
    WEEKEND: "Week-end",
    HORS: "Hors alternance",
}

# L'emoji de chaque nature, pour le repli en texte.
EMOJIS = {
    ENTREPRISE: "🏢",
    FORMATION: "🎓",
    EXAMEN: "📝",
    RATTRAPAGE: "♻️",
    FERIE: "🎉",
    WEEKEND: "🛌",
    HORS: "⬜",
}

# Les natures qui comptent comme « a l'ecole » : celles qu'on annonce.
ECOLE = (FORMATION, EXAMEN, RATTRAPAGE)

MOIS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
           "août", "septembre", "octobre", "novembre", "décembre"]
MOIS_COURTS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.",
               "août", "sept.", "oct.", "nov.", "déc."]
JOURS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


class CalendrierIntrouvable(RuntimeError):
    """Aucun PDF de calendrier n'a ete trouve, ou il est illisible."""


# --- Le modele ---------------------------------------------------------------
@dataclass
class Periode:
    """Un bloc continu de meme nature : « du 31 aout au 18 septembre ».

    Les bornes sont des jours OUVRES : une periode de formation finit le
    vendredi, pas le dimanche qui suit. Le week-end appartient a la periode
    au sens ou il est dedans, mais il n'en est jamais le bord -- annoncer
    « formation jusqu'au dimanche 20 » serait faux.
    """
    nature: str
    debut: date
    fin: date

    @property
    def nom(self):
        return NOMS.get(self.nature, self.nature)

    @property
    def emoji(self):
        return EMOJIS.get(self.nature, "•")

    @property
    def jours(self):
        """Le nombre de jours calendaires, bornes comprises."""
        return (self.fin - self.debut).days + 1

    @property
    def ouvres(self):
        """Le nombre de jours ouvres : ce qui se compte vraiment."""
        return sum(1 for d in self.dates() if d.weekday() < 5)

    @property
    def semaines(self):
        """Le nombre de semaines entamees, pour « 3 semaines de cours »."""
        return max(1, round(self.ouvres / 5))

    def dates(self):
        d = self.debut
        while d <= self.fin:
            yield d
            d += timedelta(days=1)

    def contient(self, jour):
        return self.debut <= jour <= self.fin

    def chevauche(self, debut, fin):
        return self.debut <= fin and self.fin >= debut

    def __str__(self):
        return f"{self.nom} — {intervalle_fr(self.debut, self.fin)}"


@dataclass
class Calendrier:
    """Le PDF entier : chaque jour, sa nature, et les blocs qu'ils forment."""
    jours: dict = field(default_factory=dict)      # date -> nature
    periodes: list = field(default_factory=list)   # [Periode], dans l'ordre
    source: Path | None = None
    annee: str = ""                                # « 2026-2027 »

    # -- les bornes ----------------------------------------------------------
    @property
    def debut(self):
        return min(self.jours) if self.jours else None

    @property
    def fin(self):
        return max(self.jours) if self.jours else None

    def couvre(self, jour):
        return bool(self.jours) and self.debut <= jour <= self.fin

    # -- interroger un jour --------------------------------------------------
    def nature(self, jour):
        """La nature d'un jour. Hors du calendrier, ou sur une case vide, on
        rend WEEKEND si c'en est un et HORS sinon : ne jamais rendre None
        evite un `if` a chaque appel, et aucun des deux n'est un mensonge."""
        trouve = self.jours.get(jour)
        if trouve:
            return trouve
        return WEEKEND if jour.weekday() >= 5 else HORS

    def periode_de(self, jour):
        """La periode qui contient ce jour, ou None."""
        for p in self.periodes:
            if p.contient(jour):
                return p
        return None

    def entre(self, debut, fin):
        """Les periodes qui touchent la fenetre, dans l'ordre."""
        return [p for p in self.periodes if p.chevauche(debut, fin)]

    def suivante(self, depuis=None, natures=None):
        """La prochaine periode qui COMMENCE apres `depuis`.

        `natures` filtre : suivante(natures=ECOLE) rend le prochain retour a
        l'ecole, ce que tout le monde demande en premier."""
        depuis = depuis or date.today()
        for p in self.periodes:
            if p.debut > depuis and (natures is None or p.nature in natures):
                return p
        return None

    def jusqu_a(self, jour, natures):
        """Combien de jours avant la prochaine periode d'une de ces natures."""
        p = self.suivante(jour, natures)
        return (p.debut - jour).days if p else None

    # -- compter -------------------------------------------------------------
    def comptes(self, debut=None, fin=None):
        """{nature: nombre de jours ouvres} sur la fenetre.

        Les week-ends ne sont pas comptes : personne ne veut lire « 104 jours
        de week-end » dans le bilan de son annee."""
        debut = debut or self.debut
        fin = fin or self.fin
        sortie = {}
        d = debut
        while d and d <= fin:
            if d.weekday() < 5:
                n = self.nature(d)
                if n not in (WEEKEND, HORS):
                    sortie[n] = sortie.get(n, 0) + 1
            d += timedelta(days=1)
        return sortie

    def natures_presentes(self, debut=None, fin=None):
        """Les natures vues sur la fenetre, dans l'ordre de la legende."""
        vues = set(self.comptes(debut, fin))
        return [n for n in (FORMATION, EXAMEN, RATTRAPAGE, ENTREPRISE, FERIE)
                if n in vues]


# --- Lire le PDF : les rectangles colores ------------------------------------
# Un contenu de page PDF est une suite d'operandes et d'operateurs en notation
# postfixee (« 1 0 0 rg » = « prends du rouge »). On n'interprete ici que ce
# qui sert a poser un aplat de couleur, et on ignore tout le reste -- le
# texte, les images, les courbes. C'est volontairement incomplet : ce n'est pas
# un lecteur de PDF, c'est un lecteur de cases coloriees.
_NOMBRE = r"[-+]?\d*\.?\d+"
_JETON = re.compile(rb"(/[^\s/\[\]<>(){}]+|" + _NOMBRE.encode() +
                    rb"|[A-Za-z*'\"]+)")

# Les operateurs qui remplissent le chemin courant.
_REMPLIR = ("f", "f*", "B", "B*", "b", "b*")


def _produit(m, n):
    """Le produit de deux matrices 2x3 du PDF : appliquer m PUIS n."""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + b * C, a * B + b * D,
            c * A + d * C, c * B + d * D,
            e * A + f * C + E, e * B + f * D + F)


def _point(m, x, y):
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def _bornes(points):
    """Les bornes du sous-chemin s'il est un rectangle a cotes droits, sinon
    None. Quatre sommets, deux abscisses, deux ordonnees : tout le reste --
    un triangle, une courbe, la fleche d'une legende -- est ecarte ici."""
    if not points or None in points:
        return None
    if len(points) > 1 and points[0] == points[-1]:
        points = points[:-1]
    if len(points) != 4:
        return None
    xs = sorted({round(p[0], 3) for p in points})
    ys = sorted({round(p[1], 3) for p in points})
    if len(xs) != 2 or len(ys) != 2:
        return None
    return (xs[0], ys[0], xs[1], ys[1])


def _aplats(contenu):
    """[(couleur, x0, y0, x1, y1)] : chaque rectangle plein du flux.

    On suit la pile d'etat graphique (q/Q) et la matrice courante (cm) pour
    rendre des coordonnees de page, la couleur de remplissage (rg, g, k), et
    les chemins (m, l, re, h) jusqu'a l'operateur qui les remplit."""
    ctm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    pile, couleur, sortie = [], (0.0, 0.0, 0.0), []
    chemins, courant, operandes = [], [], []

    for jeton in _JETON.finditer(contenu):
        brut = jeton.group(1)
        try:
            operandes.append(float(brut))
            continue
        except ValueError:
            pass
        op = brut.decode("latin-1")

        if op == "q":
            pile.append((ctm, couleur))
        elif op == "Q":
            if pile:
                ctm, couleur = pile.pop()
        elif op == "cm" and len(operandes) >= 6:
            ctm = _produit(tuple(operandes[-6:]), ctm)
        elif op == "rg" and len(operandes) >= 3:
            couleur = tuple(round(v, 6) for v in operandes[-3:])
        elif op == "g" and len(operandes) >= 1:
            couleur = (round(operandes[-1], 6),) * 3
        elif op == "k" and len(operandes) >= 4:
            c, m_, y, n = operandes[-4:]
            couleur = tuple(round((1 - min(1.0, v + n)), 6) for v in (c, m_, y))
        elif op == "m" and len(operandes) >= 2:
            if courant:
                chemins.append(courant)
            courant = [_point(ctm, operandes[-2], operandes[-1])]
        elif op == "l" and len(operandes) >= 2:
            courant.append(_point(ctm, operandes[-2], operandes[-1]))
        elif op in ("c", "v", "y"):
            # Une courbe de Bezier : le sous-chemin n'est plus un rectangle.
            courant.append(None)
        elif op == "re" and len(operandes) >= 4:
            x, y, larg, haut = operandes[-4:]
            if courant:
                chemins.append(courant)
            chemins.append([_point(ctm, x, y), _point(ctm, x + larg, y),
                            _point(ctm, x + larg, y + haut), _point(ctm, x, y + haut)])
            courant = []
        elif op == "h":
            if courant:
                chemins.append(courant)
                courant = []
        elif op in _REMPLIR:
            if courant:
                chemins.append(courant)
            for chemin in chemins:
                boite = _bornes(chemin)
                if boite:
                    sortie.append((couleur,) + boite)
            chemins, courant = [], []
        elif op in ("n", "S", "s"):
            # Un chemin trace ou abandonne : rien a remplir.
            chemins, courant = [], []
        operandes = []
    return sortie


def _flux(brut):
    """Les flux de contenu decompresses du PDF.

    On ecarte les objets qui portent un /Subtype (les images, les polices) :
    decompresser un JPEG de 300 ko pour y chercher des operateurs de dessin
    coute cher et ne rend jamais rien."""
    sortie = []
    for entete, donnees in re.findall(
            rb"\d+ 0 obj\s*(<<.*?>>)\s*stream\r?\n(.*?)endstream", brut, re.S):
        if b"/Subtype" in entete or b"/Image" in entete:
            continue
        if b"FlateDecode" in entete:
            try:
                sortie.append(zlib.decompress(donnees))
            except zlib.error:
                continue
        elif b"/Filter" not in entete:
            sortie.append(donnees)
    return sortie


def _nature_couleur(couleur):
    """La nature que designe une couleur, ou None.

    La comparaison est tolerante : un PDF reexporte par un autre outil decale
    volontiers une teinte de quelques millemes, et refuser un orange a
    (0.999, 0.401, 0.0) rendrait le module inutile pour rien."""
    meilleure, ecart_min = None, 0.12
    for teinte, nature in COULEURS_PDF.items():
        ecart = sum(abs(a - b) for a, b in zip(couleur, teinte))
        if ecart < ecart_min:
            meilleure, ecart_min = nature, ecart
    return meilleure


# --- Retrouver la grille ------------------------------------------------------
def _grouper(valeurs, tolerance):
    """Des coordonnees voisines ramenees a une seule : les cases d'une meme
    colonne ne sont pas au pixel pres dans le PDF."""
    groupes = []
    for v in sorted(valeurs):
        if groupes and v - groupes[-1][-1] <= tolerance:
            groupes[-1].append(v)
        else:
            groupes.append([v])
    return [sum(g) / len(g) for g in groupes]


def _suite_reguliere(valeurs, minimum=6):
    """La plus longue suite de valeurs regulierement espacees.

    C'est ce qui separe la GRILLE du reste de la page : les treize colonnes de
    mois sont a pas constant, les quatre cases de la legende, en bas, ne le
    sont pas. Plutot que de deviner ou s'arrete le tableau par des seuils de
    position, on garde la plus longue regularite -- un tableau, c'est
    exactement ca."""
    if len(valeurs) < minimum:
        return valeurs
    ecarts = [round(b - a, 1) for a, b in zip(valeurs, valeurs[1:])]
    if not ecarts:
        return valeurs
    pas = sorted(ecarts)[len(ecarts) // 2]          # l'ecart median
    if pas <= 0:
        return valeurs
    meilleure, courante = [], [valeurs[0]]
    for precedent, valeur in zip(valeurs, valeurs[1:]):
        if abs((valeur - precedent) - pas) <= max(1.5, pas * 0.25):
            courante.append(valeur)
        else:
            if len(courante) > len(meilleure):
                meilleure = courante
            courante = [valeur]
    return courante if len(courante) > len(meilleure) else meilleure


def _cases(aplats):
    """Les cases coloriees du tableau : [(colonne, ligne, nature)].

    La taille d'une case n'est pas connue d'avance -- elle depend de l'echelle
    du PDF. On prend la taille la plus frequente parmi les rectangles dont la
    couleur figure dans la legende : dans un calendrier, la case du calendrier
    est forcement ce qui se repete le plus."""
    colores = [(nature, x0, y0, x1, y1)
               for (couleur, x0, y0, x1, y1) in aplats
               if (nature := _nature_couleur(couleur))]
    if not colores:
        return [], [], []

    tailles = {}
    for _, x0, y0, x1, y1 in colores:
        cle = (round(x1 - x0), round(y1 - y0))
        tailles[cle] = tailles.get(cle, 0) + 1
    larg, haut = max(tailles, key=tailles.get)

    cases = [c for c in colores
             if abs(round(c[3] - c[1]) - larg) <= 1 and abs(round(c[4] - c[2]) - haut) <= 1]

    colonnes = _suite_reguliere(_grouper([c[1] for c in cases], larg / 2), 6)
    lignes = _suite_reguliere(_grouper([c[2] for c in cases], haut / 2), 20)
    if not colonnes or not lignes:
        return [], [], []

    def rang(valeur, reperes, tolerance):
        i = min(range(len(reperes)), key=lambda k: abs(reperes[k] - valeur))
        return i if abs(reperes[i] - valeur) <= tolerance else None

    grille = []
    for nature, x0, y0, _, _ in cases:
        col = rang(x0, colonnes, larg / 2)
        lig = rang(y0, lignes, haut / 2)
        if col is not None and lig is not None:
            grille.append((col, lig, nature))
    return grille, colonnes, lignes


# --- Caler la grille sur un vrai calendrier ----------------------------------
def _paques(an):
    """Le dimanche de Paques (algorithme de Meeus/Jones/Butcher)."""
    a, b, c = an % 19, an // 100, an % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    total = h + l - 7 * m + 114
    return date(an, total // 31, (total % 31) + 1)


def feries(an):
    """Les jours feries francais de l'annee civile."""
    p = _paques(an)
    return {
        date(an, 1, 1), date(an, 5, 1), date(an, 5, 8), date(an, 7, 14),
        date(an, 8, 15), date(an, 11, 1), date(an, 11, 11), date(an, 12, 25),
        p + timedelta(days=1),          # lundi de Paques
        p + timedelta(days=39),         # Ascension
        p + timedelta(days=50),         # lundi de Pentecote
    }


def _essayer(grille, colonnes, nb_lignes, premier_mois, premiere_annee, sens):
    """Un calage candidat : {date: nature}, et son score de vraisemblance.

    `sens` vaut +1 si la premiere ligne du tableau est le 1er du mois, et -1
    si c'est le 31 : selon l'outil qui a produit le PDF, l'axe vertical du
    document descend ou monte, et rien dans le fichier ne le dit franchement.
    On essaie les deux et on garde celui qui ressemble a un calendrier.

    Le score dit justement a quel point la grille lui RESSEMBLE :

      * une case posee sur un jour qui n'existe pas (le 31 d'un mois de 30)
        est redhibitoire -- le tableau ne serait pas cale du tout ;
      * une case de cours un samedi est tres suspecte ;
      * une case grise sur un vrai jour ferie francais est un tres bon signe,
        et c'est ce qui departage deux annees a une semaine pres.
    """
    jours, score = {}, 0
    mois_annee = []
    an, mo = premiere_annee, premier_mois
    for _ in range(len(colonnes)):
        mois_annee.append((an, mo))
        mo += 1
        if mo > 12:
            an, mo = an + 1, 1

    tous_feries = set()
    for an, _ in mois_annee:
        tous_feries |= feries(an)

    for col, lig, nature in grille:
        if col >= len(mois_annee):
            return None, -10_000
        an, mo = mois_annee[col]
        jour = lig + 1 if sens > 0 else nb_lignes - lig
        try:
            d = date(an, mo, jour)
        except ValueError:
            score -= 200                # un 31 fevrier : le calage est faux
            continue
        jours[d] = nature
        if nature == FERIE:
            score += 40 if d in tous_feries else -25
        elif d.weekday() >= 5:
            score -= 12                 # des cours le samedi : douteux
        else:
            score += 1
    return jours, score


def _caler(grille, colonnes, nb_lignes, annee_forcee=0):
    """Poser la grille sur de vraies dates : par quel mois et quelle annee
    commence-t-elle ?

    Le titre du PDF le dirait, mais il est en courbes -- illisible. On essaie
    donc les debuts plausibles et on garde le meilleur. Le calendrier scolaire
    laisse une empreinte tres particuliere (les feries, les week-ends), et une
    seule annee y repond : c'est plus sur que de lire une chaine de caracteres
    qui, de toute facon, n'est pas la.
    """
    aujourd_hui = date.today()
    if annee_forcee:
        annees = [annee_forcee]
    else:
        # Un calendrier d'alternance couvre l'annee scolaire en cours ; on
        # ratisse large autour, dans les deux sens.
        centre = aujourd_hui.year - (1 if aujourd_hui.month < 7 else 0)
        annees = [centre + n for n in range(-3, 4)]

    meilleur, meilleur_score, second = None, -10_000, -10_000
    for an in annees:
        for mois in (8, 9, 7, 1, 10):
            for sens in (1, -1):
                jours, score = _essayer(grille, colonnes, nb_lignes, mois, an, sens)
                if jours is None:
                    continue
                if score > meilleur_score:
                    meilleur, second, meilleur_score = \
                        (jours, an, mois), meilleur_score, score
                elif score > second:
                    second = score

    if meilleur is None:
        raise CalendrierIntrouvable(
            "Impossible de reconnaitre un calendrier dans ce PDF : aucune "
            "grille de mois n'y correspond.")
    # Une avance nette est exigee : si deux annees se valent, le calendrier
    # serait pose au hasard, et des dates fausses sont pires que pas de dates.
    if not annee_forcee and meilleur_score - second < 20:
        raise CalendrierIntrouvable(
            "Ce PDF ne permet pas de deviner son annee scolaire avec "
            "certitude. Precise-la dans config.yaml :\n"
            "    classe:\n      calendrier_annee: 2026")
    return meilleur


# --- Les periodes -------------------------------------------------------------
def _periodes(jours):
    """Les jours regroupes en blocs continus de meme nature.

    Deux subtilites, et elles comptent toutes les deux :

      * les WEEK-ENDS ne coupent pas un bloc. Trois semaines de cours d'affilee
        doivent s'annoncer « du 31 aout au 18 septembre », pas en trois
        morceaux de cinq jours separes par des samedis ;
      * un FERIE au milieu d'un bloc ne le coupe pas non plus (le 11 novembre
        tombe un mercredi de cours : la semaine reste une semaine de cours),
        mais un ferie qui separe deux natures DIFFERENTES devient un bloc a
        lui seul -- c'est une information, pas du bruit.
    """
    if not jours:
        return []
    ouvres = [d for d in sorted(jours) if d.weekday() < 5]
    if not ouvres:
        return []

    # Les feries absorbes par le bloc qui les entoure.
    natures = {}
    for i, d in enumerate(ouvres):
        n = jours[d]
        if n == FERIE:
            avant = jours.get(ouvres[i - 1]) if i > 0 else None
            apres = jours.get(ouvres[i + 1]) if i + 1 < len(ouvres) else None
            if avant == apres and avant not in (None, FERIE):
                n = avant
        natures[d] = n

    blocs, debut, nature = [], ouvres[0], natures[ouvres[0]]
    precedent = ouvres[0]
    for d in ouvres[1:]:
        # Un trou de plus d'un week-end signale une coupure dans le tableau.
        coupure = (d - precedent).days > 4
        if natures[d] != nature or coupure:
            blocs.append(Periode(nature, debut, precedent))
            debut, nature = d, natures[d]
        precedent = d
    blocs.append(Periode(nature, debut, precedent))
    return blocs


# --- Charger ------------------------------------------------------------------
def fichier():
    """Le PDF du calendrier, ou None. config.yaml d'abord, sinon le premier
    trouve a la racine puis dans donnees/."""
    choisi = getattr(config, "CALENDRIER_FICHIER", "")
    if choisi:
        # Rendu meme s'il n'existe pas : c'est charger() qui dira son nom.
        chemin = Path(choisi)
        return chemin if chemin.is_absolute() else config.RACINE / chemin
    for dossier in (config.RACINE, config.DONNEES):
        for motif in MOTIFS:
            trouves = sorted(p for p in dossier.glob(motif)
                             if not p.name.startswith("~$"))
            if trouves:
                return trouves[0]
    return None


_CACHE = {}


def charger(chemin=None):
    """Le calendrier, lu une fois puis garde en memoire.

    Le cache est indexe sur la date de modification du fichier : remplacer le
    PDF par une nouvelle version suffit, sans redemarrer le bot.
    """
    chemin = Path(chemin) if chemin else fichier()
    if chemin is None:
        raise CalendrierIntrouvable(
            "Aucun calendrier d'alternance dans le dossier du bot : dépose le "
            "PDF de l'école à la racine du projet.")
    if not chemin.exists():
        raise CalendrierIntrouvable(f"Le fichier `{chemin.name}` est introuvable.")

    cle = (str(chemin), chemin.stat().st_mtime_ns)
    if cle in _CACHE:
        return _CACHE[cle]

    try:
        brut = chemin.read_bytes()
    except OSError as e:
        raise CalendrierIntrouvable(f"{chemin.name} est illisible ({e}).") from e
    if not brut.startswith(b"%PDF"):
        raise CalendrierIntrouvable(f"{chemin.name} n'est pas un PDF.")

    aplats = []
    for contenu in _flux(brut):
        if b"rg" in contenu or b"re" in contenu:
            aplats += _aplats(contenu)

    grille, colonnes, lignes = _cases(aplats)
    if not grille:
        raise CalendrierIntrouvable(
            f"{chemin.name} ne contient aucune case coloriée reconnue. Les "
            f"couleurs attendues sont celles de la légende officielle : bleu "
            f"nuit, orange, vert, vert pâle.")

    jours, an, _mois = _caler(grille, colonnes, len(lignes),
                              int(getattr(config, "CALENDRIER_ANNEE", 0) or 0))
    cal = Calendrier(jours=jours, periodes=_periodes(jours), source=chemin,
                     annee=f"{an}-{an + 1}")
    _CACHE.clear()                      # une seule version en memoire
    _CACHE[cle] = cal
    return cal


def disponible():
    """Y a-t-il un PDF a lire ? Sert a n'afficher le bouton que si oui.

    On regarde si le FICHIER est la, sans l'ouvrir : cette fonction est
    appelee par le panneau, qui est synchrone et tourne sur la boucle du bot.
    Si le fichier est present mais illisible, /calendrier le dira lui-meme,
    avec le detail."""
    chemin = fichier()
    try:
        return chemin is not None and chemin.exists()
    except OSError:
        return False


# --- Dire une date ------------------------------------------------------------
def _quantieme(n):
    """« 1er », et « 2 », « 3 »… : le francais ne met l'ordinal qu'au premier."""
    return "1er" if n == 1 else str(n)


def date_fr(d, avec_jour=True, court=False):
    """« lundi 31 août » — la date comme on la dit."""
    mois = (MOIS_COURTS if court else MOIS_FR)[d.month - 1]
    nom = JOURS_FR[d.weekday()]
    tete = f"{nom[:3] if court else nom} " if avec_jour else ""
    return f"{tete}{_quantieme(d.day)} {mois}"


def intervalle_fr(debut, fin, court=False):
    """« du lundi 31 août au vendredi 18 septembre », sans repeter ce qui se
    repete : dans un meme mois, le mois ne se dit qu'une fois.

    Quand l'intervalle change d'annee, en revanche, l'annee se dit : « du
    31 août au 30 août » laisse croire a une coquille."""
    if debut == fin:
        return f"le {date_fr(debut, court=court)}"
    if debut.year != fin.year:
        return (f"du {date_fr(debut, court=court)} {debut.year} "
                f"au {date_fr(fin, court=court)} {fin.year}")
    if debut.month == fin.month:
        jour = JOURS_FR[debut.weekday()][:3] if court else JOURS_FR[debut.weekday()]
        jour_fin = JOURS_FR[fin.weekday()][:3] if court else JOURS_FR[fin.weekday()]
        mois = (MOIS_COURTS if court else MOIS_FR)[debut.month - 1]
        return (f"du {jour} {_quantieme(debut.day)} au "
                f"{jour_fin} {_quantieme(fin.day)} {mois}")
    return f"du {date_fr(debut, court=court)} au {date_fr(fin, court=court)}"


def duree_fr(jours):
    """« dans 3 jours », « demain », « aujourd'hui »."""
    if jours == 0:
        return "aujourd'hui"
    if jours == 1:
        return "demain"
    if jours < 0:
        return f"il y a {-jours} jour{'s' if -jours > 1 else ''}"
    if jours < 14:
        return f"dans {jours} jours"
    semaines = round(jours / 7)
    return f"dans {semaines} semaines"


def compte_fr(nombre, singulier, pluriel=None):
    pluriel = pluriel or singulier + "s"
    return f"{nombre} {singulier if nombre <= 1 else pluriel}"


# --- Le repli en texte --------------------------------------------------------
def bloc_periodes(cal, debut=None, fin=None, maxi=14, aujourd_hui=None):
    """Les periodes en lignes Markdown : ce que voit Discord quand Pillow
    manque, et ce qui reste lisible sur un telephone."""
    aujourd_hui = aujourd_hui or date.today()
    debut = debut or cal.debut
    fin = fin or cal.fin
    lignes = []
    for p in cal.entre(debut, fin)[:maxi]:
        if p.nature in (WEEKEND, HORS):
            continue
        marque = "**➜** " if p.contient(aujourd_hui) else ""
        detail = compte_fr(p.ouvres, "jour")
        if p.nature in (FORMATION, ENTREPRISE) and p.semaines > 1:
            detail = f"{compte_fr(p.semaines, 'semaine')} · {detail}"
        quand = ""
        if p.debut > aujourd_hui:
            quand = f" — {duree_fr((p.debut - aujourd_hui).days)}"
        lignes.append(f"{marque}{p.emoji} **{p.nom}** · {intervalle_fr(p.debut, p.fin)}"
                      f" · {detail}{quand}")
    return lignes


def bloc_resume(cal, jour=None):
    """Les deux ou trois lignes qui repondent a « et maintenant ? »."""
    jour = jour or date.today()
    lignes = []
    nature = cal.nature(jour)
    p = cal.periode_de(jour)

    if not cal.couvre(jour):
        quand = "n'a pas encore commencé" if cal.debut and jour < cal.debut \
            else "est terminée"
        lignes.append(f"⬜ L'année {cal.annee} {quand}.")
    elif nature == WEEKEND:
        suite = cal.periode_de(jour + timedelta(days=(7 - jour.weekday())))
        if suite:
            lignes.append(f"🛌 Week-end. Lundi, c'est **{suite.nom.lower()}**.")
        else:
            lignes.append("🛌 Week-end.")
    elif p is not None:
        reste = (p.fin - jour).days
        if reste == 0:
            lignes.append(f"{p.emoji} Aujourd'hui : **{p.nom}** — dernier jour.")
        else:
            lignes.append(f"{p.emoji} Aujourd'hui : **{p.nom}** — jusqu'au "
                          f"{date_fr(p.fin)} ({compte_fr(reste, 'jour')} "
                          f"restant{'s' if reste > 1 else ''}).")

    suivante = cal.suivante(jour, ECOLE if nature == ENTREPRISE else None)
    if suivante is not None:
        ecart = (suivante.debut - jour).days
        lignes.append(f"{suivante.emoji} Ensuite : **{suivante.nom}** "
                      f"{intervalle_fr(suivante.debut, suivante.fin)} — "
                      f"{duree_fr(ecart)}.")
    return lignes
