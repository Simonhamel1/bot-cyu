#!/usr/bin/env python3
"""
Le suivi des changements d'emploi du temps : ne rien rater, ne rien inventer,
ne rien dire deux fois.

Ce que changements.py sait faire, c'est comparer deux etats. Ce module-ci s'en
sert et repond aux questions qui restent, celles qui font la difference entre
un bot qui crie au loup et un bot en qui on a confiance :

  * QUE COMPARER ?      la reference est ecrite sur DISQUE. Un redemarrage du
                        bot ne remet donc pas les compteurs a zero : ce qui a
                        bouge pendant qu'il etait eteint est annonce au
                        demarrage suivant, au lieu d'etre avale en silence ;

  * SUR QUELLE PLAGE ?  seulement la fenetre commune aux deux etats. Sans ca,
                        le lendemain, tous les cours sortis de l'horizon
                        passeraient pour des annulations ;

  * EST-CE VRAI ?       CELCAT sert parfois une reponse tronquee (maintenance,
                        session a moitie expiree). Un emploi du temps qui perd
                        d'un coup la moitie de ses cours n'est pas cru : on
                        garde l'ancienne reference et on attend la lecture
                        suivante. Trois lectures d'affilee dans le meme sens
                        finissent par etre acceptees — un semestre peut
                        vraiment se terminer ;

  * L'AI-JE DEJA DIT ?  chaque changement annonce laisse sa signature dans un
                        journal. Un cours qui bouge, revient, et rebouge ne
                        genere qu'une annonce par etat reellement nouveau.

Le journal sert aussi a `/actu` : il garde les cours avant et apres, donc on
sait redessiner les changements des derniers jours sans redemander a CELCAT.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import celcat
import changements as chg
import config

FICHIER_REFERENCE = config.DONNEES / "reference.json"
FICHIER_JOURNAL = config.DONNEES / "actu.json"

# On garde un mois d'historique : de quoi repondre a « qu'est-ce qui a bouge
# depuis la rentree ? » sans laisser le fichier grossir indefiniment.
RETENTION_JOURS = 31

# En dessous de cette proportion de cours conserves, on soupconne une reponse
# tronquee plutot qu'un vrai vidage de l'emploi du temps.
SEUIL_PLAUSIBILITE = 0.5
# ... sauf si la meme chose se repete : au bout de trois lectures identiques,
# c'est que l'emploi du temps a vraiment change.
SUSPICIONS_AVANT_ACCEPTATION = 3


# --- Serialisation d'un cours ------------------------------------------------
def cours_en_dict(c):
    return {"debut": c.debut.isoformat(), "fin": c.fin.isoformat() if c.fin else "",
            "titre": c.titre, "module": c.module, "salle": c.salle,
            "prof": c.prof, "categorie": c.categorie, "campus": c.campus}


def dict_en_cours(d):
    debut = celcat._dt(d.get("debut"))
    if debut is None:
        return None
    return celcat.Cours(
        debut=debut, fin=celcat._dt(d.get("fin")), titre=d.get("titre", ""),
        module=d.get("module", ""), salle=d.get("salle", ""), prof=d.get("prof", ""),
        categorie=d.get("categorie", ""), campus=d.get("campus", ""))


def _lire_json(chemin, defaut):
    try:
        data = json.loads(chemin.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return defaut
    return data if isinstance(data, type(defaut)) else defaut


def _ecrire_json(chemin, data):
    config.preparer_dossiers()
    chemin.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# --- La reference ------------------------------------------------------------
def _horizon():
    """Le dernier jour que la lecture en cours a demande a CELCAT."""
    return date.today() + timedelta(days=config.HORIZON_JOURS)


def lire_reference():
    """(liste de Cours, jusqu'ou elle allait, quand elle a ete prise)."""
    data = _lire_json(FICHIER_REFERENCE, {})
    cours = [c for c in (dict_en_cours(d) for d in data.get("cours", [])) if c]
    fin = data.get("jusqu_au")
    try:
        fin = date.fromisoformat(str(fin)[:10])
    except (TypeError, ValueError):
        fin = max((c.jour for c in cours), default=None)
    return cours, fin, celcat._dt(data.get("le"))


def ecrire_reference(cours, suspicions=0, jusqu_au=None):
    """La reference, ET jusqu'ou elle etait renseignee.

    Garder l'horizon est indispensable : demain, la lecture ira un jour plus
    loin, et sans cette borne le jour gagne ressemblerait a une fournee de
    cours ajoutes."""
    _ecrire_json(FICHIER_REFERENCE, {
        "le": datetime.now().isoformat(timespec="seconds"),
        "jusqu_au": str(jusqu_au or _horizon()),
        "suspicions": int(suspicions),
        "cours": [cours_en_dict(c) for c in cours if c.est_cours]})


def _suspicions_gardees():
    return int(_lire_json(FICHIER_REFERENCE, {}).get("suspicions") or 0)


# --- Le journal --------------------------------------------------------------
def signature(ch):
    """L'identite d'un changement, stable d'une lecture a l'autre.

    Elle contient les deux cotes : un cours qui part de 8 h a 9 h puis revient
    a 8 h produit deux signatures differentes, donc deux annonces — ce qui est
    bien le comportement voulu, on veut savoir qu'il est revenu."""
    brut = json.dumps([ch.type,
                       ch.avant.cle() if ch.avant else None,
                       ch.apres.cle() if ch.apres else None],
                      ensure_ascii=False, default=str)
    return hashlib.sha1(brut.encode("utf-8")).hexdigest()[:16]


def journal_lire():
    return _lire_json(FICHIER_JOURNAL, [])


def journal_ajouter(liste):
    """Enregistre des changements annonces, et purge ce qui est trop vieux."""
    if not liste:
        return
    journal = journal_lire()
    maintenant = datetime.now().isoformat(timespec="seconds")
    for ch in liste:
        journal.append({
            "signature": signature(ch), "type": ch.type, "le": maintenant,
            "avant": cours_en_dict(ch.avant) if ch.avant else None,
            "apres": cours_en_dict(ch.apres) if ch.apres else None})
    limite = (datetime.now() - timedelta(days=RETENTION_JOURS)).isoformat()
    journal = [e for e in journal if str(e.get("le", "")) >= limite]
    _ecrire_json(FICHIER_JOURNAL, journal[-800:])


def signatures_connues():
    return {e.get("signature") for e in journal_lire()}


def historique(jours=7):
    """Les changements des `jours` derniers jours, en objets Changement.

    Sert a `/actu` : on redessine l'image des changements recents sans
    redemander quoi que ce soit a CELCAT."""
    limite = (datetime.now() - timedelta(days=max(1, jours))).isoformat()
    sortie = []
    for e in journal_lire():
        if str(e.get("le", "")) < limite:
            continue
        avant = dict_en_cours(e["avant"]) if e.get("avant") else None
        apres = dict_en_cours(e["apres"]) if e.get("apres") else None
        if avant is None and apres is None:
            continue
        if e.get("type") not in chg.ORDRE:
            continue
        sortie.append(chg.Changement(e["type"], avant, apres))
    return sorted(sortie, key=lambda ch: (chg.ORDRE.index(ch.type), ch.cours.debut))


def quand_dernier():
    """Quand un changement a-t-il ete annonce pour la derniere fois ?"""
    journal = journal_lire()
    return celcat._dt(journal[-1]["le"]) if journal else None


# --- Le rapport d'une observation --------------------------------------------
@dataclass
class Rapport:
    changements: list = field(default_factory=list)
    publication: dict | None = None      # « l'emploi du temps est sorti »
    suspect: str = ""                    # donnees jugees invraisemblables
    pendant_absence: bool = False        # detecte au demarrage, pas en direct
    connus: int = 0
    fenetre: tuple | None = None

    def __bool__(self):
        return bool(self.changements or self.publication)

    @property
    def urgents(self):
        return chg.urgents(self.changements)


def _fenetre_commune(fin_reference):
    """(debut, fin) des jours comparables entre les deux etats.

    On ne compare jamais le passe — CELCAT reecrit son historique sans que ca
    change quoi que ce soit pour toi — ni au-dela du plus court des deux
    horizons : sinon, le jour gagne chaque matin au bout de la fenetre
    ressemblerait a des cours ajoutes, et le jour perdu a des annulations."""
    fin = min(fin_reference or _horizon(), _horizon())
    debut = date.today()
    return (debut, fin) if fin >= debut else None


def _restreindre(cours, fenetre):
    debut, fin = fenetre
    return [c for c in cours if c.est_cours and debut <= c.jour <= fin]


def plausibilite(avant, apres):
    """Un message si le nouvel etat a l'air tronque, sinon une chaine vide."""
    if not avant:
        return ""
    if not apres:
        return (f"CELCAT ne renvoie plus aucun cours alors qu'on en connaissait "
                f"{len(avant)} : réponse probablement tronquée.")
    part = len(apres) / len(avant)
    if part < SEUIL_PLAUSIBILITE:
        return (f"CELCAT ne renvoie plus que {len(apres)} cours sur les "
                f"{len(avant)} connus ({part:.0%}) : réponse probablement "
                f"tronquée.")
    return ""


class Suivi:
    """La memoire des changements, entre deux lectures et entre deux lancements.

    Le daemon en garde un ; chaque lecture de CELCAT passe par observer().
    """

    def __init__(self):
        self.reference, self.fin_reference, self.capture = lire_reference()
        self.suspicions = _suspicions_gardees()
        self.premiere_observation = True

    def demarrage_a_froid(self):
        """Aucune reference : la premiere lecture ne peut rien annoncer."""
        return not self.reference

    def observer(self, cours, forcer=False):
        """Compare `cours` a la reference et rend un Rapport.

        `forcer` accepte des donnees jugees invraisemblables : c'est ce que
        fait /rafraichir quand on sait, soi, que l'emploi du temps a vraiment
        ete vide.
        """
        rapport = Rapport(connus=len([c for c in cours if c.est_cours]))
        vrais = [c for c in cours if c.est_cours]

        if not self.reference:
            # Premier lancement : on prend l'etat actuel pour reference sans
            # rien annoncer. Annoncer 47 « ajouts » au premier demarrage serait
            # du bruit pur.
            ecrire_reference(vrais)
            self.reference, self.fin_reference = vrais, _horizon()
            self.premiere_observation = False
            return rapport

        fenetre = _fenetre_commune(self.fin_reference)
        if fenetre is None:
            return rapport
        rapport.fenetre = fenetre
        avant = _restreindre(self.reference, fenetre)
        apres = _restreindre(vrais, fenetre)

        souci = "" if forcer else plausibilite(avant, apres)
        if souci and self.suspicions + 1 < SUSPICIONS_AVANT_ACCEPTATION:
            # On ne touche pas a la reference : la prochaine lecture aura donc
            # la meme chance de retomber sur ses pieds.
            self.suspicions += 1
            ecrire_reference(self.reference, self.suspicions, self.fin_reference)
            rapport.suspect = souci
            return rapport
        self.suspicions = 0

        rapport.pendant_absence = self.premiere_observation
        self.premiere_observation = False

        sortie = chg.premiere_publication(avant, apres)
        if sortie:
            rapport.publication = sortie
        else:
            connues = signatures_connues()
            liste = [ch for ch in chg.comparer(avant, apres)
                     if signature(ch) not in connues]
            rapport.changements = liste
            journal_ajouter(liste)

        ecrire_reference(vrais)
        self.reference, self.fin_reference = vrais, _horizon()
        return rapport
