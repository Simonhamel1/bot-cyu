#!/usr/bin/env python3
"""
Ce que ta semaine pese vraiment : les chiffres que l'emploi du temps cache.

Un emploi du temps repond a « ou dois-je etre a 10h ». Il ne repond pas a
« est-ce que cette semaine est chargee », « ou passe mon temps », « combien
d'heures je perds entre deux cours ». Ce module compte, et ne fait que compter :

    semaine()      tout ce qu'on peut dire d'une semaine : total, matieres,
                   trous, jour le plus lourd, amplitude, distanciel ;
    comparer()     l'ecart avec la semaine d'avant, quand on la connait ;
    archiver()     garde le total de la semaine sur disque, pour que la
                   comparaison marche encore quand CELCAT a oublie le passe ;
    bloc()         la meme chose en texte, quand Pillow manque.

Une regle vaut pour tout le fichier : on ne compte que ce qui est un COURS.
Les feries et les fermetures que CELCAT publie des septembre gonfleraient
sinon les totaux d'heures qui n'existent pas (`c.est_cours`).

Les durees sont en MINUTES partout, et converties en heures seulement a
l'affichage : additionner des « 1h30 » est le meilleur moyen de se tromper.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import celcat
import config
import vue

FICHIER = config.FICHIER_SEMAINES

# On garde une annee scolaire d'historique : au-dela, personne ne compare.
SEMAINES_GARDEES = 45


# Les mots qui disent le TYPE d'une seance et pas la matiere. Ils sont traites
# selon leur place, parce que le francais ne les traite pas pareil :
#
#   * CM, TD, TP, DS       jamais un nom de matiere, ou qu'ils soient ;
#   * EXAMEN, CONTROLE...  annoncent la matiere qui suit (« Examen Statistiques »)
#                          ou la terminent : les deux se retirent ;
#   * PROJET               commence de VRAIS intitules (« Projet integrateur ») :
#                          on ne le retire qu'a la fin.
#
# Sans cette distinction, « Projet integrateur » devient « integrateur », et une
# matiere disparait du graphique sous un nom que personne ne reconnait.
CODES_COURTS = {"CM", "TD", "TP", "DS"}
MOTS_EXAMEN = {"EXAMEN", "EXAM", "CONTROLE", "CONTRÔLE", "SOUTENANCE", "PARTIEL"}
MOTS_FIN = CODES_COURTS | MOTS_EXAMEN | {"PROJET"}


def _marqueur(mot, debut):
    """Ce mot-la est-il un type de seance, a cette place-la ?"""
    mot = mot.strip(" -–·,;()").upper()
    return mot in (CODES_COURTS | MOTS_EXAMEN if debut else MOTS_FIN)


def _nom_matiere(c):
    """Le libelle sous lequel regrouper un cours.

    Deux problemes a regler ici, et un seul endroit pour les regler :

      * le type colle au titre. CELCAT ecrit « Corporate finance CM » et
        « Corporate finance TD » : sans nettoyage, ce sont deux matieres, et le
        graphique montre douze barres la ou il y en a six ;
      * les alias de config.yaml. Si `matieres:` dit que « anglais » couvre
        LANG201, alors LANG201 s'appelle Anglais, ici comme ailleurs.
    """
    brut = (c.titre or c.module or "").strip()
    for canon, variantes in (config.MATIERES or {}).items():
        noms = {celcat.normaliser(canon)}
        noms |= {celcat.normaliser(v) for v in (variantes or []) if v}
        cible = celcat.normaliser(f"{c.module} {c.titre}")
        if any(n and n in cible for n in noms):
            return str(canon).strip().capitalize()

    mots = brut.replace("-", " ").split()
    while mots and _marqueur(mots[0], debut=True):
        mots.pop(0)
    while mots and _marqueur(mots[-1], debut=False):
        mots.pop()
    propre = " ".join(mots).strip(" -–·,;")
    return propre or brut or "sans intitulé"


@dataclass(frozen=True)
class Part:
    """Une matiere, et la place qu'elle prend dans la semaine."""
    nom: str
    minutes: float
    seances: int
    par_type: dict = field(default_factory=dict)   # "CM" -> minutes

    @property
    def heures(self):
        return self.minutes / 60

    def part_de(self, total):
        return (self.minutes / total * 100) if total else 0

    @property
    def types(self):
        """Les types presents, du plus lourd au plus leger."""
        return sorted(self.par_type.items(), key=lambda kv: -kv[1])


@dataclass(frozen=True)
class Journee:
    """Une journee : ce qu'elle contient, et ce qu'elle coute vraiment."""
    jour: date
    minutes: float
    seances: int
    debut: datetime | None
    fin: datetime | None
    trous: tuple = ()
    par_type: dict = field(default_factory=dict)   # "CM" -> minutes

    @property
    def amplitude(self):
        """Le temps entre le premier et le dernier cours : la vraie duree de
        la journee, celle qui fatigue, pas la somme des cours."""
        if not self.debut or not self.fin:
            return 0
        return (self.fin - self.debut).total_seconds() / 60

    @property
    def trous_minutes(self):
        return sum((b - a).total_seconds() / 60 for a, b in self.trous)

    @property
    def vide(self):
        return self.seances == 0


@dataclass(frozen=True)
class Semaine:
    """Tout ce qu'on sait d'une semaine, deja calcule."""
    lundi: date
    jours: tuple
    matieres: tuple
    total_minutes: float
    seances: int
    distanciel_minutes: float
    examens: tuple
    trous_minutes: float
    nb_trous: int

    @property
    def dimanche(self):
        return self.lundi + timedelta(days=6)

    @property
    def heures(self):
        return self.total_minutes / 60

    @property
    def jours_travailles(self):
        return sum(1 for j in self.jours if not j.vide)

    @property
    def moyenne_par_jour(self):
        return self.total_minutes / max(self.jours_travailles, 1)

    @property
    def jour_plein(self):
        """Le jour le plus charge. None si la semaine est vide."""
        pleins = [j for j in self.jours if not j.vide]
        return max(pleins, key=lambda j: j.minutes) if pleins else None

    @property
    def jour_leger(self):
        pleins = [j for j in self.jours if not j.vide]
        return min(pleins, key=lambda j: j.minutes) if pleins else None

    @property
    def part_distanciel(self):
        return (self.distanciel_minutes / self.total_minutes * 100
                if self.total_minutes else 0)

    @property
    def vide(self):
        return self.seances == 0

    @property
    def libelle(self):
        """« 13 au 19 octobre », ou « 29 sept. au 5 oct. » a cheval sur deux mois."""
        fin = self.dimanche
        if fin.month == self.lundi.month:
            return f"{self.lundi.day} au {fin.day} {vue.MOIS[fin.month - 1]}"
        return (f"{self.lundi.day} {vue.MOIS[self.lundi.month - 1]} "
                f"au {fin.day} {vue.MOIS[fin.month - 1]}")


def _type_de(c):
    """Le type sous lequel compter un cours. Un cours a distance compte comme
    tel avant tout : ce qui change ta journee, c'est de ne pas avoir a y aller,
    pas que ce soit un CM ou un TD."""
    return "à distance" if c.a_distance else (c.type_court or "autre")


def _journee(cours, jour):
    """Le detail d'une journee : bornes, trous, volume."""
    jc = [c for c in celcat.du_jour(cours, jour) if c.est_cours]
    if not jc:
        return Journee(jour=jour, minutes=0, seances=0, debut=None, fin=None)

    minutes = sum(c.minutes for c in jc)
    debut = min(c.debut for c in jc)
    fin = max((c.fin or c.debut) for c in jc)

    # Un trou, c'est du temps mort ENTRE deux cours. On avance un curseur au
    # lieu de comparer des cours deux a deux : sinon deux cours qui se
    # chevauchent (CELCAT en produit) inventent un trou negatif.
    trous, curseur = [], None
    for c in jc:
        if curseur is not None and c.debut > curseur:
            creux = (c.debut - curseur).total_seconds() / 60
            if creux >= config.TROU_MINUTES:
                trous.append((curseur, c.debut))
        curseur = max(curseur or c.debut, c.fin or c.debut)

    par_type = {}
    for c in jc:
        type_ = _type_de(c)
        par_type[type_] = par_type.get(type_, 0) + c.minutes

    return Journee(jour=jour, minutes=minutes, seances=len(jc), debut=debut,
                   fin=fin, trous=tuple(trous), par_type=par_type)


def semaine(cours, lundi=None):
    """Tous les chiffres d'une semaine, calcules en une passe."""
    lundi = lundi or celcat.semaine_de(date.today())
    if isinstance(lundi, datetime):
        lundi = lundi.date()

    jours = tuple(_journee(cours, lundi + timedelta(days=i)) for i in range(7))
    dans_la_semaine = [c for c in cours
                       if c.est_cours and lundi <= c.jour <= lundi + timedelta(days=6)]

    par_matiere = {}
    for c in dans_la_semaine:
        nom = _nom_matiere(c)
        entree = par_matiere.setdefault(nom, {"minutes": 0.0, "seances": 0, "types": {}})
        entree["minutes"] += c.minutes
        entree["seances"] += 1
        type_ = _type_de(c)
        entree["types"][type_] = entree["types"].get(type_, 0) + c.minutes

    matieres = tuple(sorted(
        (Part(nom=nom, minutes=v["minutes"], seances=v["seances"], par_type=v["types"])
         for nom, v in par_matiere.items()),
        key=lambda p: (-p.minutes, p.nom)))

    return Semaine(
        lundi=lundi, jours=jours, matieres=matieres,
        total_minutes=sum(c.minutes for c in dans_la_semaine),
        seances=len(dans_la_semaine),
        distanciel_minutes=sum(c.minutes for c in dans_la_semaine if c.a_distance),
        examens=tuple(c for c in dans_la_semaine if c.est_examen),
        trous_minutes=sum(j.trous_minutes for j in jours),
        nb_trous=sum(len(j.trous) for j in jours),
    )


# --- Le bilan : tout l'emploi du temps connu ---------------------------------
@dataclass(frozen=True)
class Bilan:
    """Le poids de tout ce que CELCAT connait, matiere par matiere."""
    debut: date
    fin: date
    matieres: tuple
    total_minutes: float
    seances: int
    semaines: tuple          # (lundi, minutes, seances), une par semaine couverte
    distanciel_minutes: float
    examens: tuple

    @property
    def vide(self):
        return self.seances == 0

    @property
    def heures(self):
        return self.total_minutes / 60

    @property
    def nb_semaines(self):
        """Les semaines qui ont au moins un cours : une semaine de vacances ne
        doit pas faire baisser la moyenne."""
        return sum(1 for _, m, _ in self.semaines if m)

    @property
    def moyenne_semaine(self):
        return self.total_minutes / max(self.nb_semaines, 1)

    @property
    def part_distanciel(self):
        return (self.distanciel_minutes / self.total_minutes * 100
                if self.total_minutes else 0)

    @property
    def semaine_pleine(self):
        pleines = [s for s in self.semaines if s[1]]
        return max(pleines, key=lambda s: s[1]) if pleines else None

    @property
    def libelle(self):
        return (f"du {vue.jour_fr(self.debut, court=True)} "
                f"au {vue.jour_fr(self.fin, court=True)}")


def bilan(cours, debut=None, fin=None):
    """Tout ce que CELCAT connait entre deux dates (par defaut : tout).

    C'est la reponse a « combien d'heures de VBA ce semestre ? » — que la
    vue par semaine ne peut pas donner. Les bornes reelles sont celles des
    cours trouves, pas celles demandees : on ne dessine pas de semaines vides
    aux deux bouts.
    """
    vrais = [c for c in cours if c.est_cours
             and (debut is None or c.jour >= debut)
             and (fin is None or c.jour <= fin)]
    if not vrais:
        auj = date.today()
        return Bilan(debut or auj, fin or auj, (), 0.0, 0, (), 0.0, ())

    debut = min(c.jour for c in vrais)
    fin = max(c.jour for c in vrais)

    par_matiere = {}
    for c in vrais:
        nom = _nom_matiere(c)
        entree = par_matiere.setdefault(nom, {"minutes": 0.0, "seances": 0, "types": {}})
        entree["minutes"] += c.minutes
        entree["seances"] += 1
        type_ = _type_de(c)
        entree["types"][type_] = entree["types"].get(type_, 0) + c.minutes
    matieres = tuple(sorted(
        (Part(nom=nom, minutes=v["minutes"], seances=v["seances"], par_type=v["types"])
         for nom, v in par_matiere.items()),
        key=lambda p: (-p.minutes, p.nom)))

    semaines, lundi = [], celcat.semaine_de(debut)
    while lundi <= fin:
        de_la_semaine = [c for c in vrais if lundi <= c.jour <= lundi + timedelta(days=6)]
        semaines.append((lundi, sum(c.minutes for c in de_la_semaine), len(de_la_semaine)))
        lundi += timedelta(days=7)

    return Bilan(
        debut=debut, fin=fin, matieres=matieres,
        total_minutes=sum(c.minutes for c in vrais), seances=len(vrais),
        semaines=tuple(semaines),
        distanciel_minutes=sum(c.minutes for c in vrais if c.a_distance),
        examens=tuple(c for c in vrais if c.est_examen),
    )


def par_type_semaine(cours, lundi):
    """{type: minutes} pour une semaine : ce que la barre d'une semaine empile."""
    sortie = {}
    for c in cours:
        if c.est_cours and lundi <= c.jour <= lundi + timedelta(days=6):
            type_ = _type_de(c)
            sortie[type_] = sortie.get(type_, 0) + c.minutes
    return sortie


def bloc_bilan(b):
    """Le bilan en texte, quand Pillow manque."""
    if b.vide:
        return ["CELCAT ne connaît aucun cours pour l'instant."]
    lignes = [f"**{vue.duree_fr(b.total_minutes)} de cours** {b.libelle} · "
              f"{b.seances} séances · {b.nb_semaines} semaines",
              f"↳ **{vue.duree_fr(b.moyenne_semaine)}** par semaine en moyenne"]
    pleine = b.semaine_pleine
    if pleine:
        lignes.append(f"↳ semaine la plus chargée : **du {pleine[0]:%d/%m}**, "
                      f"{vue.duree_fr(pleine[1])}")
    if b.distanciel_minutes:
        lignes.append(f"↳ {vue.duree_fr(b.distanciel_minutes)} à distance "
                      f"({b.part_distanciel:.0f} %)")
    if b.examens:
        lignes.append(f"↳ ⚠️ {len(b.examens)} examen{'s' if len(b.examens) > 1 else ''}")
    lignes += ["", "**Heures par matière**"]
    for p in b.matieres[:12]:
        lignes.append(f"`{_barre(p.part_de(b.total_minutes))}` "
                      f"**{vue.duree_fr(p.minutes)}** · {p.nom} "
                      f"({p.part_de(b.total_minutes):.0f} %, {p.seances} séance"
                      f"{'s' if p.seances > 1 else ''})")
    if len(b.matieres) > 12:
        reste = sum(p.minutes for p in b.matieres[12:])
        lignes.append(f"`{_barre(reste / b.total_minutes * 100)}` "
                      f"**{vue.duree_fr(reste)}** · {len(b.matieres) - 12} autres")
    return lignes


# --- L'historique ------------------------------------------------------------
# CELCAT ne sert que l'avenir : la semaine passee sort de l'horizon et devient
# impossible a recalculer. On garde donc le total au passage, une ligne par
# semaine — quelques octets pour une comparaison qui, sinon, n'existerait pas.
def _lire_historique():
    try:
        data = json.loads(FICHIER.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def archiver(s):
    """Retient le total d'une semaine. Sans effet si elle est vide : une
    lecture CELCAT ratee ne doit pas archiver une semaine a zero heure.

    Rien n'est reecrit quand le total n'a pas bouge : le daemon appelle ceci a
    chaque tour, et un fichier reecrit toutes les quinze minutes pour rien
    finit par se faire remarquer.
    """
    if s.vide:
        return False
    data = _lire_historique()
    entree = {
        "minutes": round(s.total_minutes),
        "seances": s.seances,
        "matieres": {p.nom: round(p.minutes) for p in s.matieres},
    }
    ancienne = data.get(s.lundi.isoformat()) or {}
    if all(ancienne.get(k) == v for k, v in entree.items()):
        return False

    data[s.lundi.isoformat()] = dict(entree,
                                     le=datetime.now().isoformat(timespec="seconds"))
    for vieux in sorted(data)[:-SEMAINES_GARDEES]:
        data.pop(vieux, None)
    try:
        config.preparer_dossiers()
        FICHIER.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    except OSError as e:
        print(f"[!] historique des semaines non ecrit : {e}", flush=True)
        return False
    return True


def archiver_horizon(cours, aujourd=None):
    """Archive toutes les semaines ENTIEREMENT couvertes par `cours`.

    CELCAT ne sert que l'avenir : une semaine deja commencee est amputee de ses
    premiers jours dans ce qu'on a en main, et l'archiver donnerait un total
    faux. On n'archive donc qu'une semaine dont le lundi n'est pas encore
    passe — elle est alors complete.

    Le tour suivant la reecrit si l'emploi du temps a bouge, et le lundi venu
    elle se fige d'elle-meme : on a alors archive la derniere version connue
    d'une semaine complete. C'est exactement ce qu'on veut comparer.
    """
    aujourd = aujourd or date.today()
    if not cours:
        return 0
    lundis = sorted({celcat.semaine_de(c.jour) for c in cours
                     if c.est_cours and celcat.semaine_de(c.jour) >= aujourd})
    return sum(1 for lundi in lundis if archiver(semaine(cours, lundi)))


def archive_de(lundi):
    """Le total archive d'une semaine, en minutes. None si on ne l'a pas.

    Sert a rattraper ce que CELCAT ne sait plus dire : une semaine commencee
    est amputee de ses jours passes dans ce qu'on peut relire, alors que son
    archive, elle, a ete ecrite quand la semaine etait encore entiere.
    """
    entree = _lire_historique().get(lundi.isoformat())
    try:
        return float(entree["minutes"])
    except (KeyError, TypeError, ValueError):
        return None


def avertissement(s, cours=None, aujourd=None):
    """La phrase a afficher quand les chiffres ne peuvent pas etre complets.

    Vaut "" quand tout va bien. Une statistique fausse sans le dire est pire
    que pas de statistique du tout : CELCAT ne rend jamais les jours passes, et
    ca doit se lire sur l'image, pas se deviner.

    On ne le devine pas non plus : si `cours` contient deja un cours anterieur
    au lundi de la semaine, c'est que la lecture couvre le passe, et il n'y a
    donc rien a signaler. Avertir quand meme serait un mensonge dans l'autre
    sens.
    """
    aujourd = aujourd or date.today()
    if s.lundi >= aujourd:
        return ""
    if cours and min((c.jour for c in cours), default=aujourd) <= s.lundi:
        return ""
    manquants = min((aujourd - s.lundi).days, 7)
    complet = archive_de(s.lundi)
    debut = ("le premier jour de la semaine n'y est plus" if manquants == 1
             else f"les {manquants} premiers jours de la semaine n'y sont plus")
    phrase = f"CELCAT ne sert que l'avenir : {debut}, ce total est partiel"
    if complet and complet > s.total_minutes:
        phrase += f" — la semaine entière faisait {vue.duree_fr(complet)}"
    return phrase + "."


def historique(n=8):
    """Les n dernieres semaines archivees : [(lundi, minutes), ...]."""
    data = _lire_historique()
    sortie = []
    for cle in sorted(data)[-n:]:
        try:
            sortie.append((date.fromisoformat(cle), float(data[cle]["minutes"])))
        except (ValueError, KeyError, TypeError):
            continue
    return sortie


def comparer(s, cours=None):
    """L'ecart en minutes avec la semaine precedente, et d'ou vient le chiffre.

    Rend (ecart, origine) ou (None, ""). On prefere toujours un calcul sur les
    cours reels a une valeur archivee : l'archive peut dater d'avant un
    changement d'emploi du temps.
    """
    avant = s.lundi - timedelta(days=7)
    if cours:
        precedente = semaine(cours, avant)
        if not precedente.vide:
            return s.total_minutes - precedente.total_minutes, "calculée"
    archive = _lire_historique().get(avant.isoformat())
    if archive:
        try:
            return s.total_minutes - float(archive["minutes"]), "archivée"
        except (KeyError, TypeError, ValueError):
            pass
    return None, ""


# --- La version texte --------------------------------------------------------
def _barre(part, largeur=14):
    """Une barre en caracteres pleins, pour l'embed de secours."""
    plein = max(1, round(part / 100 * largeur)) if part > 0 else 0
    return "█" * plein + "·" * (largeur - plein)


def bloc(s, cours=None):
    """La semaine en texte : ce que Discord affiche si Pillow manque."""
    if s.vide:
        return [f"**{s.libelle}** — aucun cours cette semaine-la. 🌴"]

    ecart, origine = comparer(s, cours)
    lignes = [f"**{vue.duree_fr(s.total_minutes)} de cours** sur "
              f"{s.jours_travailles} jour{'s' if s.jours_travailles > 1 else ''} "
              f"· {s.seances} séances"]
    if ecart is not None:
        signe = "+" if ecart > 0 else "−"
        lignes.append(f"↳ {signe}{vue.duree_fr(abs(ecart))} par rapport à la "
                      f"semaine précédente ({origine})"
                      if ecart else "↳ exactement autant que la semaine précédente")

    plein = s.jour_plein
    if plein:
        lignes.append(f"↳ jour le plus chargé : **{vue.jour_fr(plein.jour, court=True)}**, "
                      f"{vue.duree_fr(plein.minutes)}")
    if s.trous_minutes:
        lignes.append(f"↳ {vue.duree_fr(s.trous_minutes)} de trous sur "
                      f"{s.nb_trous} créneau{'x' if s.nb_trous > 1 else ''}")
    if s.distanciel_minutes:
        lignes.append(f"↳ {vue.duree_fr(s.distanciel_minutes)} à distance "
                      f"({s.part_distanciel:.0f} %)")
    if s.examens:
        lignes.append(f"↳ ⚠️ {len(s.examens)} examen{'s' if len(s.examens) > 1 else ''} "
                      f"cette semaine")

    lignes.append("")
    lignes.append("**Où passe ton temps**")
    for p in s.matieres[:8]:
        lignes.append(f"`{_barre(p.part_de(s.total_minutes))}` "
                      f"**{vue.duree_fr(p.minutes)}** · {p.nom} "
                      f"({p.seances} séance{'s' if p.seances > 1 else ''})")
    if len(s.matieres) > 8:
        reste = sum(p.minutes for p in s.matieres[8:])
        lignes.append(f"`{_barre(reste / s.total_minutes * 100)}` "
                      f"**{vue.duree_fr(reste)}** · {len(s.matieres) - 8} autres")
    return lignes
