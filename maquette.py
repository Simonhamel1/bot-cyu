#!/usr/bin/env python3
"""
La maquette pedagogique : les UE, les EC, les ECTS et les coefficients.

D'ou viennent ces chiffres
--------------------------
D'un seul fichier : le tableau M3C de la promo (« Modalites de controle des
connaissances et des competences »), un classeur Excel pose a la racine du
projet. Il n'est PAS recopie dans le code : quand l'ecole en publie une
nouvelle version, tu remplaces le fichier et /ects suit.

Pourquoi un parseur maison
--------------------------
Un .xlsx est une archive ZIP de fichiers XML. Lire trois colonnes ne vaut pas
d'ajouter openpyxl ou pandas aux dependances d'un bot Discord : zipfile et
ElementTree sont dans la bibliotheque standard, et tiennent en 80 lignes.

Ce que le module NE fait PAS
----------------------------
Il ne calcule aucune moyenne et ne connait aucune note : la maquette dit ce
que chaque matiere PESE, pas ce que quelqu'un y a obtenu. Les onglets
« MOYENNE S1 / S2 » du classeur sont donc ignores.

La structure rendue :

    Maquette          le classeur entier : un titre, une annee, des semestres
      Semestre        « Semestre 1 », ses UE, ses totaux d'heures et d'ECTS
        UE            une unite d'enseignement : un code, un intitule, ses ECTS
          EC          une matiere : heures, ECTS, coefficient, controle
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

import config

# L'espace de noms de SpreadsheetML, present sur chaque balise du XML d'Excel.
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# Le fichier cherche a la racine, dans l'ordre : ce que dit config.yaml, puis
# tout classeur dont le nom commence par « M3C ». Le second cas est celui de
# tout le monde : on depose le fichier de l'ecole, sans rien regler.
MOTIF = "M3C*.xlsx"

# Un en-tete de semestre, et rien d'autre : « Semestre 1 », pas « Semestre 2 :
# le PDF source affiche… », qui est une note de bas de tableau.
RE_SEMESTRE = re.compile(r"^semestres?\s*\d+\s*$", re.IGNORECASE)


class MaquetteIntrouvable(RuntimeError):
    """Aucun classeur M3C n'a ete trouve, ou il est illisible."""


# --- Le modele ---------------------------------------------------------------
@dataclass
class EC:
    """Une matiere (« element constitutif ») : ce qu'elle pese et comment elle
    est evaluee."""
    code: str = ""                  # EC1, SAE1…
    intitule: str = ""
    langue: str = ""
    cm: float = 0.0
    td: float = 0.0
    tp: float = 0.0
    autre: float = 0.0
    ects: float = 0.0               # deduit du coefficient (voir _repartir)
    coef: float = 0.0
    seuil: float | None = None      # la note minimale exigee dans l'EC
    controle: str = ""              # ET, CC, CC + ET…
    epreuve: str = ""               # E, O, E ou O…
    regle: str = ""                 # « 25% + 75% »
    controle2: str = ""             # la 2e session
    epreuve2: str = ""
    regle2: str = ""

    @property
    def heures(self):
        return self.cm + self.td + self.tp + self.autre

    @property
    def evalue(self):
        """Un EC « non evalue » (la LV2, la remediation) ne compte pas : il a
        zero coefficient et pas de note seuil."""
        return self.controle.lower() != "non évalué" and self.coef > 0

    @property
    def sae(self):
        return self.code.upper().startswith("SAE")


@dataclass
class UE:
    """Une unite d'enseignement : le bloc qui porte les ECTS."""
    code: str = ""
    intitule: str = ""
    ects: float = 0.0
    coef: float = 0.0
    ecs: list = field(default_factory=list)

    @property
    def heures(self):
        return sum(e.heures for e in self.ecs)

    @property
    def cm(self):
        return sum(e.cm for e in self.ecs)

    @property
    def td(self):
        return sum(e.td for e in self.ecs)

    @property
    def tp(self):
        return sum(e.tp for e in self.ecs)

    @property
    def autre(self):
        return sum(e.autre for e in self.ecs)


@dataclass
class Semestre:
    """Un semestre : ses UE, et la ligne de total du classeur."""
    nom: str = ""
    ues: list = field(default_factory=list)
    cm: float = 0.0
    td: float = 0.0
    tp: float = 0.0
    autre: float = 0.0
    ects: float = 0.0

    @property
    def numero(self):
        """« Semestre 2 » -> 2. Sert aux boutons et a /ects semestre:2."""
        chiffres = "".join(c for c in self.nom if c.isdigit())
        return int(chiffres) if chiffres else 0

    @property
    def heures(self):
        return self.cm + self.td + self.tp + self.autre

    @property
    def matieres(self):
        """Tous les EC du semestre, a plat."""
        return [e for u in self.ues for e in u.ecs]


@dataclass
class Maquette:
    titre: str = ""
    annee: str = ""
    semestres: list = field(default_factory=list)
    source: Path | None = None
    notes: list = field(default_factory=list)   # les reserves du classeur

    @property
    def entete(self):
        """« ING2 · FISA Mathématiques - Maths-Finance · 2026-2027 ».

        La ligne 2 du classeur est une phrase administrative (« Année
        universitaire : … | Type de diplôme… : … | Niveau : … ») : on n'en
        garde que les valeurs, dans l'ordre ou on les cherche.
        """
        valeurs = []
        for morceau in self.annee.split("|"):
            _, _, valeur = morceau.partition(":")
            valeur = (valeur or morceau).strip()
            if valeur:
                valeurs.append(valeur)
        if len(valeurs) == 3:               # annee, parcours, niveau
            valeurs = [valeurs[2], valeurs[1], valeurs[0]]
        return " · ".join(valeurs)

    def semestre(self, numero):
        for s in self.semestres:
            if s.numero == int(numero):
                return s
        return None

    @property
    def ects(self):
        return sum(s.ects for s in self.semestres)

    @property
    def reserves(self):
        """Les notes du classeur qui avouent un doute sur un chiffre.

        Le fichier M3C de la promo a ete reconstitue a partir d'un PDF, et son
        auteur a ecrit noir sur blanc quelles repartitions horaires sont des
        hypotheses. Afficher ces heures comme certaines serait mentir : /ects
        remonte donc la reserve avec le tableau.
        """
        mots = ("hypothèse", "hypothèses", "à vérifier", "ne concordent pas",
                "reconstitution")
        return [n for n in self.notes
                if any(m in n.lower() for m in mots)]


# --- Lire un .xlsx sans dependance -------------------------------------------
def _chaines(archive):
    """La table des chaines partagees : Excel n'ecrit chaque texte qu'une fois
    et les cellules y renvoient par leur indice."""
    try:
        brut = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    return ["".join(t.text or "" for t in si.iter(NS + "t"))
            for si in ET.fromstring(brut).iter(NS + "si")]


def _colonne(reference):
    """« AB12 » -> « AB ». La lettre de colonne, sans le numero de ligne."""
    return "".join(c for c in reference if c.isalpha())


def _lignes(archive, chaines, feuille="xl/worksheets/sheet1.xml"):
    """La feuille en dictionnaires {colonne: valeur}, dans l'ordre des lignes.

    Les cellules vides n'existent pas dans le XML : une ligne rendue ici n'a
    donc que ses colonnes remplies, ce qui est exactement ce qu'on veut —
    « la colonne TP est vide » se lit `"G" not in ligne`.
    """
    feuille_xml = ET.fromstring(archive.read(feuille))
    sortie = []
    for ligne in feuille_xml.iter(NS + "row"):
        cellules = {}
        for c in ligne.iter(NS + "c"):
            valeur = c.find(NS + "v")
            if valeur is None or valeur.text is None:
                continue
            if c.get("t") == "s":
                texte = chaines[int(valeur.text)]
            elif c.get("t") == "str":
                texte = valeur.text
            else:
                texte = valeur.text
            cellules[_colonne(c.get("r", ""))] = texte
        if cellules:
            sortie.append(cellules)
    return sortie


def _nombre(ligne, colonne, defaut=0.0):
    try:
        return float(str(ligne.get(colonne, "")).replace(",", "."))
    except (TypeError, ValueError):
        return defaut


def _texte(ligne, colonne):
    return str(ligne.get(colonne, "") or "").strip()


# --- Du tableau au modele ----------------------------------------------------
def _repartir(ue):
    """Donne a chaque EC sa part des ECTS de l'UE, au prorata du coefficient.

    Le classeur ne porte les ECTS que sur la ligne de l'UE : « UE1 vaut 10 »,
    et en dessous quatre matieres de coefficient 3, 3, 2, 2. La part de chacune
    se deduit donc du coefficient — et dans cette maquette les deux coincident
    exactement, la somme des coefficients d'une UE valant ses ECTS.
    """
    total = sum(e.coef for e in ue.ecs)
    if total <= 0:
        return
    for e in ue.ecs:
        part = ue.ects * e.coef / total
        # Les ECTS ne vont jamais plus fin que le demi-point : arrondir evite
        # d'afficher « 2.4999999999 » sur une division qui tombe juste.
        e.ects = round(part * 2) / 2


def _lire_feuille(lignes):
    """Le tableau M3C -> des semestres. La forme du classeur est la seule
    grammaire : colonne A = le code (« Semestre 1 », « UE1 », « EC3 »)."""
    semestres, notes = [], []
    semestre = ue = None
    legende = False
    for ligne in lignes:
        code = _texte(ligne, "A")
        intitule = _texte(ligne, "B")
        haut = code.upper()

        if haut.startswith("LÉGENDE") or haut.startswith("LEGENDE"):
            legende = True
            continue
        if legende:
            # Tout ce qui suit « Legende » explique le tableau : les sigles, la
            # note seuil, et les reserves de celui qui a reconstitue le fichier.
            # On le garde tel quel : ces avertissements appartiennent aux
            # chiffres, et les cacher serait les presenter comme certains.
            #
            # Ce bloc passe AVANT la detection des semestres, et la detection
            # elle-meme exige « Semestre <n> » seul : sans ces deux gardes, une
            # note qui commence par « Semestre 2 : le PDF source affiche… »
            # ouvre un troisieme semestre, vide, dans le tableau.
            if code:
                notes.append(code)
            continue
        if RE_SEMESTRE.match(code):
            semestre = Semestre(nom=code)
            semestres.append(semestre)
            ue = None
            continue
        if semestre is None:
            continue

        if haut.startswith("TOTAL"):
            semestre.cm = _nombre(ligne, "E")
            semestre.td = _nombre(ligne, "F")
            semestre.tp = _nombre(ligne, "G")
            semestre.autre = _nombre(ligne, "H")
            semestre.ects = _nombre(ligne, "I")
            continue

        if haut.startswith("UE"):
            ue = UE(code=code, intitule=intitule, ects=_nombre(ligne, "I"),
                    coef=_nombre(ligne, "J"))
            semestre.ues.append(ue)
            continue

        if ue is not None and intitule:
            ue.ecs.append(EC(
                code=code, intitule=intitule, langue=_texte(ligne, "D"),
                cm=_nombre(ligne, "E"), td=_nombre(ligne, "F"),
                tp=_nombre(ligne, "G"), autre=_nombre(ligne, "H"),
                coef=_nombre(ligne, "J"),
                seuil=_nombre(ligne, "K", None) if "K" in ligne else None,
                controle=_texte(ligne, "L"), epreuve=_texte(ligne, "M"),
                regle=_texte(ligne, "N"), controle2=_texte(ligne, "O"),
                epreuve2=_texte(ligne, "P"), regle2=_texte(ligne, "Q")))

    for s in semestres:
        for u in s.ues:
            _repartir(u)
        # Un classeur sans ligne de total reste exploitable : on additionne.
        if not s.ects:
            s.ects = sum(u.ects for u in s.ues)
        if not s.heures:
            s.cm = sum(u.cm for u in s.ues)
            s.td = sum(u.td for u in s.ues)
            s.tp = sum(u.tp for u in s.ues)
            s.autre = sum(u.autre for u in s.ues)
    return semestres, notes


# --- Trouver le fichier ------------------------------------------------------
def fichier():
    """Le classeur M3C, ou None. config.yaml d'abord, sinon le premier trouve.

    On regarde a la racine du projet puis dans donnees/ : deposer le fichier
    a cote du bot doit suffire, sans rien configurer.
    """
    choisi = getattr(config, "MAQUETTE_FICHIER", "")
    if choisi:
        # Rendu meme s'il n'existe pas : c'est charger() qui dira son nom.
        # Retourner None ici ferait repondre « aucun fichier trouve » a
        # quelqu'un qui en a justement designe un, et qui s'est trompe d'une
        # lettre.
        chemin = Path(choisi)
        return chemin if chemin.is_absolute() else config.RACINE / chemin
    for dossier in (config.RACINE, config.DONNEES):
        trouves = sorted(p for p in dossier.glob(MOTIF) if not p.name.startswith("~$"))
        if trouves:
            return trouves[0]
    return None


_CACHE = {}


def charger(chemin=None):
    """La maquette, lue une fois puis gardee en memoire.

    Le cache est indexe sur la date de modification du fichier : remplacer le
    classeur par une nouvelle version suffit, sans redemarrer le bot.
    """
    chemin = Path(chemin) if chemin else fichier()
    if chemin is None:
        raise MaquetteIntrouvable("Aucun classeur M3C dans le dossier du bot.")
    if not chemin.exists():
        raise MaquetteIntrouvable(f"Le fichier `{chemin.name}` est introuvable.")

    cle = (str(chemin), chemin.stat().st_mtime_ns)
    if cle in _CACHE:
        return _CACHE[cle]

    try:
        with zipfile.ZipFile(chemin) as archive:
            lignes = _lignes(archive, _chaines(archive))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        raise MaquetteIntrouvable(
            f"{chemin.name} n'est pas un classeur Excel lisible ({e}).") from e

    semestres, notes = _lire_feuille(lignes)
    if not semestres:
        raise MaquetteIntrouvable(
            f"{chemin.name} ne contient aucun semestre : la colonne A doit "
            f"porter « Semestre 1 », « UE1 », « EC1 »…")

    titre = _texte(lignes[0], "A") if lignes else ""
    annee = _texte(lignes[1], "A") if len(lignes) > 1 else ""
    m = Maquette(titre=titre, annee=annee, semestres=semestres, source=chemin,
                 notes=notes)
    _CACHE.clear()                      # une seule version en memoire
    _CACHE[cle] = m
    return m


def disponible():
    """Y a-t-il un classeur a lire ? Sert a n'afficher le bouton que si oui.

    On regarde si le FICHIER est la, sans l'ouvrir : cette fonction est
    appelee par vue_panneau(), qui est synchrone et tourne sur la boucle du
    bot. Y parser une archive ZIP ferait attendre tout le monde pour decider
    d'afficher un bouton. Si le fichier est present mais illisible, /ects le
    dira lui-meme, avec le detail.
    """
    chemin = fichier()
    try:
        return chemin is not None and chemin.exists()
    except OSError:
        return False


# --- Le repli en texte -------------------------------------------------------
def nombre_fr(valeur):
    """« 31.5 » -> « 31,5 » et « 24.0 » -> « 24 » : des heures qui se lisent."""
    if valeur is None:
        return ""
    if float(valeur) == int(valeur):
        return str(int(valeur))
    return f"{float(valeur):.1f}".replace(".", ",")


def bloc_semestre(s, detail=True):
    """Le semestre en lignes Markdown : ce que voit Discord quand Pillow
    manque, et ce qui reste lisible sur un telephone."""
    lignes = [f"**{s.nom}** — {nombre_fr(s.ects)} ECTS · "
              f"{nombre_fr(s.heures)} h de cours"]
    for u in s.ues:
        lignes.append("")
        lignes.append(f"**{u.code} · {u.intitule}** — {nombre_fr(u.ects)} ECTS")
        if not detail:
            continue
        for e in u.ecs:
            bout = []
            if e.evalue:
                bout.append(f"{nombre_fr(e.ects)} ECTS")
            if e.heures:
                bout.append(f"{nombre_fr(e.heures)} h")
            if e.controle:
                bout.append(e.controle)
            marque = "·" if e.evalue else "◦"
            lignes.append(f"-# {marque} {e.intitule} — {' · '.join(bout)}")
    return lignes
