#!/usr/bin/env python3
"""
L'emploi du temps : la lecture CELCAT, les changements (detection et suivi), ce que pese une semaine, et la meteo du trajet.

Sommaire :

    CELCAT                                        (ex-celcat.py)
    Les changements d'emploi du temps             (ex-changements.py)
    Le suivi des changements                      (ex-actu.py)
    Ce que pese une semaine                       (ex-stats.py)
    La meteo                                      (ex-meteo.py)
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from getpass import getpass

import requests

from . import affichage
from . import config
from . import promo


# ════════════════════════════════════════════════════════════════════════════
#  CELCAT  (ex-celcat.py)
# ════════════════════════════════════════════════════════════════════════════
# Tout ce qui touche a l'emploi du temps CELCAT : se connecter, lire les cours,
# les transformer en objets exploitables, et garder un cache sur disque.
#
# CELCAT n'est PAS derriere le SSO SAML casse de mail.etu.cyu.fr : c'est un
# formulaire LDAP classique (POST /LdapLogin/Logon), donc un script peut s'y
# connecter avec les identifiants de config.yaml.
#
# Le cache sert a deux choses : les commandes du bot repondent instantanement
# sans refaire un aller-retour CELCAT, et l'assistant continue de fonctionner
# quand le reseau de l'ecole tombe.


HEADERS = {"User-Agent": "Mozilla/5.0 assistant-cyu/2.0"}
TIMEOUT = 30


class EchecConnexion(RuntimeError):
    """Login refuse ou page de login inattendue. Distinct d'une panne reseau :
    on ne veut pas annoncer « emploi du temps modifie » alors qu'en fait le mot
    de passe est faux."""


# --- Outils de texte ---------------------------------------------------------
def normaliser(txt):
    """minuscules, sans accents : pour comparer des intitules a la main."""
    plat = unicodedata.normalize("NFD", str(txt or ""))
    plat = "".join(c for c in plat if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", plat).strip().lower()


def _texte(brut):
    """Le champ description de CELCAT est du HTML avec des entites numeriques
    (`Indisponibilit&#233;`). On enleve les balises ET on decode les entites,
    sinon les messages Discord sont illisibles."""
    plat = re.sub(r"<[^>]+>", " ", str(brut or ""))
    return re.sub(r"\s+", " ", html.unescape(plat)).strip()


def _lignes_html(brut):
    """La description CELCAT est du HTML avec des <br/> : on veut les lignes."""
    txt = re.sub(r"(?i)<\s*br\s*/?>|</\s*(p|div|li)\s*>", "\n", str(brut or ""))
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = html.unescape(txt)
    return [re.sub(r"\s+", " ", l).strip() for l in txt.split("\n") if l.strip()]


def _dt(valeur):
    try:
        return datetime.fromisoformat(str(valeur)[:19])
    except (TypeError, ValueError):
        return None


# --- Le modele : un cours ----------------------------------------------------
# Format reel d'une description CELCAT a CY Tech, ligne par ligne :
#
#     CM a distance                      <- la categorie
#     ING2 IF App S3 Groupe 1            <- ton groupe, constant, inutile
#     Equity markets & trading           <- nom long du cours (parfois absent)
#     FER FT 104-106 SALLE DE TD 40p     <- la vraie salle
#     BOULU-RESHEF BEATRICE              <- le prof
#     Equity Market DIZC3EMT             <- matiere + code du module
#
# La derniere ligne est la plus fiable pour le titre : elle est toujours la, et
# elle porte le code du module qui sert a rattacher les devoirs. Le champ
# `sites` de CELCAT contient le CAMPUS ("Site du Port"), pas la salle : s'y
# fier afficherait "Site du Port" a la place du numero de salle.

RE_TYPE = re.compile(r"\b(CM|TD|TP|EXAMEN|EXAM|CONTROLE|DS|PROJET|SOUTENANCE)\b", re.I)
# Le code module en fin de titre : "Equity Market DIZC3EMT" -> DIZC3EMT.
RE_CODE = re.compile(r"\s([A-Z][A-Z0-9]{5,})$")
# Une ligne de salle : elle nomme un type de local, ou porte un numero.
RE_SALLE = re.compile(r"(?i)\b(salle|amphi\w*|labo|batiment|bat\.)\b|\d{3}")
# Un nom de prof : au moins un mot tout en majuscules, aucun chiffre.
RE_PROF = re.compile(r"^(?=.*\b[A-ZÀ-ÖØ-Þ]{3,}\b)[^\d]{4,60}$")
# La ligne de groupe, a jeter : elle ne dit rien qu'on ne sache deja.
RE_GROUPE = re.compile(r"(?i)\bgroupe?\b|\bgr\.?\s*\d|\bpromo(tion)?\b")
# La queue descriptive d'une salle : "FER FT 104-106 SALLE DE TD 40p" ->
# "FER FT 104-106". Le type de salle et sa capacite n'aident pas a la trouver.
RE_QUEUE_SALLE = re.compile(r"(?i)\s+(salle\s+de\s+\w+|salle|amphi\w*)\s*\d*\s*p?$")
# Un cours a distance : pas la peine de prendre le train.
RE_DISTANCE = re.compile(r"(?i)\ba\s+distance\b|\bdistanciel\b|\bvisio\b|\bteams\b|\bzoom\b")
# Un examen : merite un traitement a part dans l'affichage.
RE_EXAMEN = re.compile(r"(?i)\bexam\w*\b|\bpartiel\b|\bcontrole\b|\bds\b|\bsoutenance\b")

# Categories qui ne sont pas des cours : CELCAT publie les jours feries et les
# fermetures bien avant l'emploi du temps. Sans cette distinction, l'assistant
# annoncerait « l'emploi du temps est sorti » pour un 25 decembre.
NON_COURS = {"indisponibilite", "ferie", "vacances", "fermeture"}

# Un pictogramme par type de seance : c'est ce qui rend la journee lisible
# d'un coup d'oeil, sans avoir a lire les intitules.
ICONES = {"CM": "📖", "TD": "✏️", "TP": "🔧", "EXAMEN": "📝", "EXAM": "📝",
          "CONTROLE": "📝", "DS": "📝", "PROJET": "🧩", "SOUTENANCE": "🎤"}


@dataclass(frozen=True)
class Cours:
    debut: datetime
    fin: datetime | None
    titre: str
    module: str          # le code CELCAT, ex. DIZA3VBA
    salle: str
    prof: str
    categorie: str       # CM, TD, CM a distance...
    campus: str = ""     # le champ `sites`, ex. "Site du Port"

    @property
    def jour(self):
        return self.debut.date()

    @property
    def minutes(self):
        return (self.fin - self.debut).total_seconds() / 60 if self.fin else 0

    @property
    def creneau(self):
        return f"{self.debut:%H:%M}-{self.fin:%H:%M}" if self.fin else f"{self.debut:%H:%M}"

    @property
    def type_court(self):
        m = RE_TYPE.search(f"{self.categorie} {self.titre}")
        return m.group(1).upper() if m else ""

    @property
    def icone(self):
        if self.a_distance:
            return "💻"
        return ICONES.get(self.type_court, "📚")

    @property
    def a_distance(self):
        """Cours en visio : la salle ne sert a rien, et le trajet non plus.

        On normalise avant de chercher : CELCAT ecrit « CM a distance » avec un
        accent, et un motif sans accent ne le trouverait jamais."""
        return bool(RE_DISTANCE.search(normaliser(self.categorie)))

    @property
    def est_examen(self):
        return bool(RE_EXAMEN.search(normaliser(f"{self.categorie} {self.titre}")))

    @property
    def est_cours(self):
        """Un ferie ou une fermeture n'est pas un cours."""
        return normaliser(self.categorie) not in NON_COURS

    @property
    def ou(self):
        """Ou se rendre, en un mot : la salle, ou « a distance »."""
        return "a distance" if self.a_distance else (self.salle or self.campus or "salle inconnue")

    def cle(self):
        """Identite exacte, pour reperer le moindre changement.

        TOUT ce qui peut changer et qui t'interesse doit etre dedans : un champ
        oublie ici, c'est un changement que le comparateur ne verra jamais.
        L'heure de fin y est donc, sinon un cours raccourci d'une heure passe
        inapercu ; le prof aussi, sinon un remplacement passe inapercu.

        On n'utilise pas l'id CELCAT : il est regenere a chaque republication de
        l'emploi du temps, donc tous les cours passeraient pour nouveaux a la
        moindre modification."""
        return (self.debut.isoformat(), self.fin.isoformat() if self.fin else "",
                self.titre, self.salle, self.prof, self.module)

    def empreinte(self):
        """Identite du COURS, independamment de son heure et de sa salle.

        C'est la cle du rapprochement : quand un creneau disparait et qu'un
        autre apparait avec la meme empreinte, ce n'est pas une annulation
        suivie d'un ajout, c'est un DEPLACEMENT — et c'est ce qu'on veut
        pouvoir dire, plutot que d'afficher deux lignes sans rapport.

        On prend le code module quand il existe (stable, unique), sinon le
        titre normalise. Le type de seance en fait partie : un CM deplace et un
        TD ajoute le meme jour sur la meme matiere sont deux choses
        differentes."""
        return (normaliser(self.module) or normaliser(self.titre), self.type_court)

    def ligne(self, avec_prof=True, avec_icone=True):
        """Une ligne prete a poster dans Discord."""
        tete = f"{self.icone} " if avec_icone else ""
        bouts = [self.type_court, self.ou]
        if avec_prof and self.prof:
            bouts.append(self.prof.title())
        suite = " · ".join(b for b in bouts if b)
        return f"`{self.creneau}` {tete}**{self.titre}**" + (f"\n⠀⠀⠀⠀⠀⠀⠀⠀└ {suite}" if suite else "")

    def ligne_courte(self):
        """Une seule ligne, sans retour : pour les listes denses et les diffs."""
        bouts = [b for b in (self.type_court, self.ou) if b]
        return f"`{self.creneau}` **{self.titre}**" + (f" · {' · '.join(bouts)}" if bouts else "")


def depuis_event(ev):
    """Evenement CELCAT brut -> Cours, ou None si inexploitable."""
    debut = _dt(ev.get("start"))
    if debut is None:
        return None
    fin = _dt(ev.get("end"))
    categorie = _texte(ev.get("eventCategory"))
    campus = " ".join(ev.get("sites") or []).strip()

    lignes = _lignes_html(ev.get("description"))
    if lignes and normaliser(lignes[0]) == normaliser(categorie):
        lignes = lignes[1:]                     # la categorie, deja connue
    if not categorie and lignes:
        categorie, lignes = lignes[0], lignes[1:]
    lignes = [l for l in lignes if not RE_GROUPE.search(l)]

    if not lignes:
        # Ferie, fermeture, ou format inattendu : on ne devine rien de plus.
        return Cours(debut=debut, fin=fin, titre=categorie or "Cours", module="",
                     salle="", prof="", categorie=categorie, campus=campus)

    # La derniere ligne porte "Nom du cours CODEMODULE".
    titre = lignes[-1]
    # Le code extrait du titre (DIZA3VBA) est prefere a celui du champ
    # `modules`, qui arrive sous une forme verbeuse : DIZA3VBA(DI02I2-260).
    module = " ".join(ev.get("modules") or []).strip()
    m = RE_CODE.search(titre)
    if m:
        module = m.group(1)
        titre = titre[:m.start()].strip()

    reste = lignes[:-1]
    prof = next((l for l in reversed(reste)
                 if RE_PROF.match(l) and not RE_SALLE.search(l)), "")
    salle = next((l for l in reversed(reste)
                  if l != prof and RE_SALLE.search(l)), "")
    salle = RE_QUEUE_SALLE.sub("", salle).strip()

    return Cours(debut=debut, fin=fin, titre=(titre or categorie or "Cours")[:120],
                 module=module, salle=salle, prof=prof, categorie=categorie,
                 campus=campus)


# --- Connexion ---------------------------------------------------------------
def identifiants():
    """(user, mot de passe), depuis config.yaml sinon demandes au clavier.

    Rien n'est envoye ailleurs qu'a celcat-calendar.cyu.fr."""
    user = config.CYU_USER
    mdp = config.CYU_PASS
    if not user:
        user = input("Identifiant CYU : ").strip()
    if not mdp:
        mdp = getpass("Mot de passe CYU (non affiche) : ")
    if not user or not mdp:
        raise EchecConnexion("identifiants vides : remplis cyu.user et "
                             "cyu.password dans config.yaml")
    return user, mdp


def connexion(user, mdp):
    """Session authentifiee sur CELCAT.

    Deux etapes : le GET pose le cookie antiforgery ASP.NET et livre le jeton
    __RequestVerificationToken cache dans le formulaire ; le POST renvoie les
    deux ensemble. Sans le jeton, CELCAT rejette silencieusement le login.
    """
    s = requests.Session()
    s.headers.update(HEADERS)

    page = s.get(f"{config.CELCAT_BASE}/LdapLogin", timeout=TIMEOUT)
    page.raise_for_status()
    jeton = re.search(
        r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', page.text)
    if not jeton:
        raise EchecConnexion(
            "jeton __RequestVerificationToken introuvable : le formulaire de "
            "login CELCAT a change, il faut mettre a jour connexion()")

    rep = s.post(
        f"{config.CELCAT_BASE}/LdapLogin/Logon",
        data={"Name": user, "Password": mdp,
              "__RequestVerificationToken": jeton.group(1)},
        timeout=TIMEOUT, allow_redirects=True,
    )
    # CELCAT renvoie 200 sur la page de login en cas d'echec, pas une 401 :
    # on se fie a l'URL finale et a la presence du champ mot de passe.
    if "LdapLogin" in rep.url or 'type="password"' in rep.text:
        raise EchecConnexion("login refuse : verifie cyu.user et cyu.password "
                             "dans config.yaml")
    return s


def evenements(session, debut, fin):
    """Evenements bruts sur la periode. Leve EchecConnexion si la session a
    expire (CELCAT sert alors la page de login au lieu du JSON)."""
    rep = session.post(
        f"{config.CELCAT_BASE}/Home/GetCalendarData",
        data={
            "start": debut.isoformat(),
            "end": fin.isoformat(),
            "resType": config.RES_TYPE,
            "calView": "month",
            "federationIds[]": config.FEDERATION_ID,
            "colourScheme": "3",
        },
        headers={"X-Requested-With": "XMLHttpRequest"},
        timeout=TIMEOUT,
    )
    rep.raise_for_status()
    try:
        data = rep.json()
    except ValueError:
        raise EchecConnexion(
            f"reponse non-JSON de GetCalendarData (HTTP {rep.status_code}, "
            f"{len(rep.text)} octets) : session expiree ou endpoint different")
    return data if isinstance(data, list) else data.get("events", [])


class Session:
    """Session CELCAT qui se reconnecte toute seule quand elle expire."""

    def __init__(self):
        self.user, self.mdp = identifiants()
        self.session = connexion(self.user, self.mdp)

    def evenements(self, debut, fin):
        try:
            return evenements(self.session, debut, fin)
        except EchecConnexion:
            self.session = connexion(self.user, self.mdp)
            return evenements(self.session, debut, fin)


# --- Cache sur disque --------------------------------------------------------
def ecrire_cache(events, debut, fin):
    config.preparer_dossiers()
    config.FICHIER_CACHE.write_text(json.dumps(
        {"maj": datetime.now().isoformat(timespec="seconds"),
         "debut": debut.isoformat(), "fin": fin.isoformat(), "events": events},
        ensure_ascii=False), encoding="utf-8")


def lire_cache():
    try:
        return json.loads(config.FICHIER_CACHE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def age_cache():
    """Age du cache en minutes, ou None s'il n'y en a pas. Sert au statut."""
    cache = lire_cache()
    maj = _dt((cache or {}).get("maj"))
    return None if maj is None else (datetime.now() - maj).total_seconds() / 60


def _construire(events):
    liste = [c for c in (depuis_event(e) for e in events) if c is not None]
    return sorted(liste, key=lambda c: c.debut)


def charger(debut=None, fin=None, hors_ligne=False, session=None):
    """(liste de Cours, origine).

    Se rabat sur le cache si CELCAT est injoignable, pour qu'un `/edt` marche
    encore dans le train sans reseau.
    """
    debut = debut or date.today()
    fin = fin or (date.today() + timedelta(days=config.HORIZON_JOURS))

    if not hors_ligne:
        try:
            client = session or Session()
            events = client.evenements(debut, fin)
            ecrire_cache(events, debut, fin)
            return _construire(events), "celcat"
        except (EchecConnexion, requests.RequestException) as e:
            print(f"[!] CELCAT indisponible ({type(e).__name__}: {e}) -> cache",
                  flush=True)

    cache = lire_cache()
    if not cache:
        return [], "aucune donnee"
    return _construire(cache.get("events", [])), f"cache de {str(cache.get('maj', ''))[11:16]}"


# --- Interrogation d'une liste de cours --------------------------------------
def du_jour(cours, jour):
    """Les vrais cours d'une journee, feries exclus, dans l'ordre."""
    return [c for c in cours if c.jour == jour and c.est_cours]


def non_cours_du_jour(cours, jour):
    """Ce qui occupe la journee sans etre un cours : ferie, fermeture."""
    return [c for c in cours if c.jour == jour and not c.est_cours]


def prochain(cours, maintenant=None):
    """Le prochain cours a venir, ou None. La base de /prochain et du statut."""
    maintenant = maintenant or datetime.now()
    return next((c for c in cours if c.est_cours and c.debut > maintenant), None)


def en_cours(cours, maintenant=None):
    """Le cours en train de se derouler, ou None.

    Utile pour ne pas afficher « prochain cours dans 10 min » quand on est
    justement assis dedans depuis une heure."""
    maintenant = maintenant or datetime.now()
    return next((c for c in cours
                 if c.est_cours and c.debut <= maintenant < (c.fin or c.debut)), None)


def semaine_de(jour):
    """Le lundi de la semaine qui contient `jour`."""
    return jour - timedelta(days=jour.weekday())


# ════════════════════════════════════════════════════════════════════════════
#  LES CHANGEMENTS D'EMPLOI DU TEMPS  (ex-changements.py)
# ════════════════════════════════════════════════════════════════════════════
# Comparer deux versions de l'emploi du temps, et dire ce qui a VRAIMENT change.
#
# Le probleme que ce module resout
# --------------------------------
# CELCAT ne publie pas de « modifications » : il republie l'emploi du temps en
# entier, avec des identifiants d'evenements regeneres a chaque fois. Comparer
# naivement deux versions donne donc, pour un simple cours decale d'une heure :
#
#     Disparu  : mardi 08:00-09:30 Corporate finance
#     Nouveau  : mardi 09:00-10:30 Corporate finance
#
# Deux lignes sans rapport apparent, a toi de faire le rapprochement a l'oeil. Et
# un changement de salle ressort exactement pareil, alors que ca n'a rien a voir.
#
# Ce module rapproche les deux cotes par EMPREINTE (le module et le type de
# seance, voir Cours.empreinte) et sait alors dire, en une ligne :
#
#     🕘 Corporate finance passe de 08:00 a 09:00, mardi 9 (+1 h)
#     🚪 VBA change de salle : FER FT 104 → FER FT 210
#
# Le rapprochement se fait du plus sur au moins sur : meme creneau d'abord, meme
# jour ensuite, meme semaine enfin. Un cours ne peut etre apparie qu'une fois.


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


def libelle_changements(type_, combien=1):
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


def comparer_changements(avant, apres):
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
    return f"{affichage.jour_fr(c.jour, court=True)} `{c.creneau}`"


def _decalage(avant, apres):
    """« +1 h », « -30 min » : de combien le cours a bouge, signe compris."""
    minutes = (apres.debut - avant.debut).total_seconds() / 60
    signe = "+" if minutes > 0 else "-"
    return f"{signe}{affichage.duree_fr(minutes)}"


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
                f"⠀⠀{affichage.jour_fr(ch.avant.jour, court=True)} `{ch.avant.creneau}`"
                f"  ➜  **{affichage.jour_fr(ch.apres.jour, court=True)}** "
                f"`{ch.apres.creneau}` · {ch.apres.ou}"]

    if ch.type == DEPLACE_HEURE:
        return [f"{tete} — {affichage.jour_fr(ch.apres.jour, court=True)}",
                f"⠀⠀`{ch.avant.creneau}`  ➜  **`{ch.apres.creneau}`**  "
                f"({_decalage(ch.avant, ch.apres)}) · {ch.apres.ou}"]

    if ch.type == SALLE:
        return [f"{tete} — {_quand(ch.apres)}",
                f"⠀⠀{ch.avant.ou or 'salle inconnue'}  ➜  **{ch.apres.ou}**"]

    if ch.type == DUREE:
        return [f"{tete} — {affichage.jour_fr(ch.apres.jour, court=True)}",
                f"⠀⠀`{ch.avant.creneau}`  ➜  **`{ch.apres.creneau}`**  "
                f"({affichage.duree_fr(ch.avant.minutes)} ➜ {affichage.duree_fr(ch.apres.minutes)})"]

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


def bloc_changements(liste, aujourd=None):
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
        lignes.append(f"{icone} **{libelle_changements(type_, len(groupe))}{compte}**")
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


# ════════════════════════════════════════════════════════════════════════════
#  LE SUIVI DES CHANGEMENTS  (ex-actu.py)
# ════════════════════════════════════════════════════════════════════════════
# Le suivi des changements d'emploi du temps : ne rien rater, ne rien inventer,
# ne rien dire deux fois.
#
# Ce que changements.py sait faire, c'est comparer deux etats. Ce module-ci s'en
# sert et repond aux questions qui restent, celles qui font la difference entre
# un bot qui crie au loup et un bot en qui on a confiance :
#
#   * QUE COMPARER ?      la reference est ecrite sur DISQUE. Un redemarrage du
#                         bot ne remet donc pas les compteurs a zero : ce qui a
#                         bouge pendant qu'il etait eteint est annonce au
#                         demarrage suivant, au lieu d'etre avale en silence ;
#
#   * SUR QUELLE PLAGE ?  seulement la fenetre commune aux deux etats. Sans ca,
#                         le lendemain, tous les cours sortis de l'horizon
#                         passeraient pour des annulations ;
#
#   * EST-CE VRAI ?       CELCAT sert parfois une reponse tronquee (maintenance,
#                         session a moitie expiree). Un emploi du temps qui perd
#                         d'un coup la moitie de ses cours n'est pas cru : on
#                         garde l'ancienne reference et on attend la lecture
#                         suivante. Trois lectures d'affilee dans le meme sens
#                         finissent par etre acceptees — un semestre peut
#                         vraiment se terminer ;
#
#   * L'AI-JE DEJA DIT ?  chaque changement annonce laisse sa signature dans un
#                         journal. Un cours qui bouge, revient, et rebouge ne
#                         genere qu'une annonce par etat reellement nouveau.
#
# Le journal sert aussi a `/actu` : il garde les cours avant et apres, donc on
# sait redessiner les changements des derniers jours sans redemander a CELCAT.


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
    debut = _dt(d.get("debut"))
    if debut is None:
        return None
    return Cours(
        debut=debut, fin=_dt(d.get("fin")), titre=d.get("titre", ""),
        module=d.get("module", ""), salle=d.get("salle", ""), prof=d.get("prof", ""),
        categorie=d.get("categorie", ""), campus=d.get("campus", ""))


_lire_json = config.lire_json
_ecrire_json = config.ecrire_json


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
    return cours, fin, _dt(data.get("le"))


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


@config.exclusif(FICHIER_JOURNAL)
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


def historique_actu(jours=7):
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
        if e.get("type") not in ORDRE:
            continue
        sortie.append(Changement(e["type"], avant, apres))
    return sorted(sortie, key=lambda ch: (ORDRE.index(ch.type), ch.cours.debut))


def quand_dernier():
    """Quand un changement a-t-il ete annonce pour la derniere fois ?"""
    journal = journal_lire()
    return _dt(journal[-1]["le"]) if journal else None


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
        return urgents(self.changements)


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

        sortie = premiere_publication(avant, apres)
        if sortie:
            rapport.publication = sortie
        else:
            connues = signatures_connues()
            liste = [ch for ch in comparer_changements(avant, apres)
                     if signature(ch) not in connues]
            rapport.changements = liste
            journal_ajouter(liste)

        ecrire_reference(vrais)
        self.reference, self.fin_reference = vrais, _horizon()
        return rapport


# ════════════════════════════════════════════════════════════════════════════
#  CE QUE PESE UNE SEMAINE  (ex-stats.py)
# ════════════════════════════════════════════════════════════════════════════
# Ce que ta semaine pese vraiment : les chiffres que l'emploi du temps cache.
#
# Un emploi du temps repond a « ou dois-je etre a 10h ». Il ne repond pas a
# « est-ce que cette semaine est chargee », « ou passe mon temps », « combien
# d'heures je perds entre deux cours ». Ce module compte, et ne fait que compter :
#
#     semaine()      tout ce qu'on peut dire d'une semaine : total, matieres,
#                    trous, jour le plus lourd, amplitude, distanciel ;
#     comparer()     l'ecart avec la semaine d'avant, quand on la connait ;
#     archiver()     garde le total de la semaine sur disque, pour que la
#                    comparaison marche encore quand CELCAT a oublie le passe ;
#     bloc()         la meme chose en texte, quand Pillow manque.
#
# Une regle vaut pour tout le fichier : on ne compte que ce qui est un COURS.
# Les feries et les fermetures que CELCAT publie des septembre gonfleraient
# sinon les totaux d'heures qui n'existent pas (`c.est_cours`).
#
# Les durees sont en MINUTES partout, et converties en heures seulement a
# l'affichage : additionner des « 1h30 » est le meilleur moyen de se tromper.


FICHIER_STATS = config.FICHIER_SEMAINES

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
        noms = {normaliser(canon)}
        noms |= {normaliser(v) for v in (variantes or []) if v}
        cible = normaliser(f"{c.module} {c.titre}")
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
            return f"{self.lundi.day} au {fin.day} {affichage.MOIS[fin.month - 1]}"
        return (f"{self.lundi.day} {affichage.MOIS[self.lundi.month - 1]} "
                f"au {fin.day} {affichage.MOIS[fin.month - 1]}")


def _type_de(c):
    """Le type sous lequel compter un cours. Un cours a distance compte comme
    tel avant tout : ce qui change ta journee, c'est de ne pas avoir a y aller,
    pas que ce soit un CM ou un TD."""
    return "à distance" if c.a_distance else (c.type_court or "autre")


def _journee(cours, jour):
    """Le detail d'une journee : bornes, trous, volume."""
    jc = [c for c in du_jour(cours, jour) if c.est_cours]
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
    lundi = lundi or semaine_de(date.today())
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
        return (f"du {affichage.jour_fr(self.debut, court=True)} "
                f"au {affichage.jour_fr(self.fin, court=True)}")


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

    semaines, lundi = [], semaine_de(debut)
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
    lignes = [f"**{affichage.duree_fr(b.total_minutes)} de cours** {b.libelle} · "
              f"{b.seances} séances · {b.nb_semaines} semaines",
              f"↳ **{affichage.duree_fr(b.moyenne_semaine)}** par semaine en moyenne"]
    pleine = b.semaine_pleine
    if pleine:
        lignes.append(f"↳ semaine la plus chargée : **du {pleine[0]:%d/%m}**, "
                      f"{affichage.duree_fr(pleine[1])}")
    if b.distanciel_minutes:
        lignes.append(f"↳ {affichage.duree_fr(b.distanciel_minutes)} à distance "
                      f"({b.part_distanciel:.0f} %)")
    if b.examens:
        lignes.append(f"↳ ⚠️ {len(b.examens)} examen{'s' if len(b.examens) > 1 else ''}")
    lignes += ["", "**Heures par matière**"]
    for p in b.matieres[:12]:
        lignes.append(f"`{_barre(p.part_de(b.total_minutes))}` "
                      f"**{affichage.duree_fr(p.minutes)}** · {p.nom} "
                      f"({p.part_de(b.total_minutes):.0f} %, {p.seances} séance"
                      f"{'s' if p.seances > 1 else ''})")
    if len(b.matieres) > 12:
        reste = sum(p.minutes for p in b.matieres[12:])
        lignes.append(f"`{_barre(reste / b.total_minutes * 100)}` "
                      f"**{affichage.duree_fr(reste)}** · {len(b.matieres) - 12} autres")
    return lignes


# --- L'historique ------------------------------------------------------------
# CELCAT ne sert que l'avenir : la semaine passee sort de l'horizon et devient
# impossible a recalculer. On garde donc le total au passage, une ligne par
# semaine — quelques octets pour une comparaison qui, sinon, n'existerait pas.
def _lire_historique():
    return config.lire_json(FICHIER_STATS, {})


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
        "trous": round(s.trous_minutes),
        "jours": s.jours_travailles,
    }
    ancienne = data.get(s.lundi.isoformat()) or {}
    if all(ancienne.get(k) == v for k, v in entree.items()):
        return False

    data[s.lundi.isoformat()] = dict(entree,
                                     le=datetime.now().isoformat(timespec="seconds"))
    for vieux in sorted(data)[:-SEMAINES_GARDEES]:
        data.pop(vieux, None)
    try:
        config.ecrire_json(FICHIER_STATS, data)
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
    lundis = sorted({semaine_de(c.jour) for c in cours
                     if c.est_cours and semaine_de(c.jour) >= aujourd})
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
        phrase += f" — la semaine entière faisait {affichage.duree_fr(complet)}"
    return phrase + "."


def historique_stats(n=8):
    """Les n dernieres semaines archivees : [(lundi, minutes), ...]."""
    data = _lire_historique()
    sortie = []
    for cle in sorted(data)[-n:]:
        try:
            sortie.append((date.fromisoformat(cle), float(data[cle]["minutes"])))
        except (ValueError, KeyError, TypeError):
            continue
    return sortie


def comparer_stats(s, cours=None):
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


# --- Comparer deux semaines --------------------------------------------------
@dataclass(frozen=True)
class Cote:
    """Une semaine reduite a ses chiffres, avec l'ORIGINE de ces chiffres.

    C'est ce que la comparaison manipule : peu importe que la semaine vienne
    des cours reels ou de l'archive, tant qu'on sait le dire.
    """
    lundi: date
    minutes: float
    seances: int
    matieres: dict            # nom -> minutes
    trous: float | None       # None : l'archive ne le savait pas encore
    jours: int | None
    origine: str              # calculee · archivee · partielle · ""

    @property
    def vide(self):
        return self.seances == 0


def cote_semaine(cours, lundi, aujourd=None):
    """Les chiffres d'une semaine, d'ou qu'ils viennent.

    Dans l'ordre : les cours reels si la semaine est ENTIERE dans ce qu'on a,
    l'archive sinon, les cours partiels en dernier recours — et on le dit.
    """
    aujourd = aujourd or date.today()
    s = semaine(cours, lundi)
    entiere = lundi >= aujourd or (
        bool(cours) and min(c.jour for c in cours) <= lundi)
    if not s.vide and entiere:
        return Cote(lundi, s.total_minutes, s.seances,
                    {p.nom: p.minutes for p in s.matieres}, s.trous_minutes,
                    s.jours_travailles, "calculée")
    archive = _lire_historique().get(lundi.isoformat())
    if archive:
        try:
            return Cote(lundi, float(archive["minutes"]), int(archive.get("seances", 0)),
                        {k: float(v) for k, v in (archive.get("matieres") or {}).items()},
                        archive.get("trous"), archive.get("jours"), "archivée")
        except (TypeError, ValueError):
            pass
    if not s.vide:
        return Cote(lundi, s.total_minutes, s.seances,
                    {p.nom: p.minutes for p in s.matieres}, s.trous_minutes,
                    s.jours_travailles, "partielle")
    return Cote(lundi, 0.0, 0, {}, None, None, "")


def devoirs_de_la_semaine(liste_devoirs, lundi):
    """Les devoirs a rendre cette semaine-la, faits ou non."""
    fin = lundi + timedelta(days=6)
    sortie = []
    for d in liste_devoirs or []:
        ech = promo.echeance_dt(d)
        if ech is not None and lundi <= ech.date() <= fin:
            sortie.append(d)
    return sortie


def _fleche(ecart, fmt):
    if ecart is None:
        return ""
    if abs(ecart) < 1e-9:
        return "＝ pareil"
    return f"{'▲' if ecart > 0 else '▼'} {'+' if ecart > 0 else '−'}{fmt(abs(ecart))}"


def bloc_comparaison(cette, avant, devoirs_cette, devoirs_avant):
    """« Cette semaine vs la precedente », en lignes Markdown."""
    def h(m):
        return affichage.duree_fr(m)

    lignes = []
    if cette.vide and avant.vide:
        return ["Aucune des deux semaines n'est connue. CELCAT ne sert que "
                "l'avenir, et l'archive se remplit au fil des semaines."]
    if cette.vide:
        lignes.append("⚠️ Cette semaine est vide dans ce que connaît l'assistant.")
    if avant.vide:
        lignes.append("⚠️ La semaine précédente n'est pas connue : CELCAT ne sert "
                      "que l'avenir, et l'assistant ne l'a pas encore archivée. "
                      "Il retient chaque semaine à venir au fil de l'eau : la "
                      "comparaison marchera d'ici une à deux semaines.")
    if avant.origine == "archivée":
        lignes.append("-# semaine précédente : d'après l'archive")
    if cette.origine == "partielle":
        lignes.append("-# cette semaine : jours passés non comptés")

    def ligne(nom, val_c, val_a, fmt):
        ecart = None if (val_c is None or val_a is None or avant.vide) else val_c - val_a
        texte = f"**{nom}** — **{fmt(val_c) if val_c is not None else '?'}**"
        if ecart is not None:
            texte += f"  {_fleche(ecart, fmt)}"
            if abs(ecart) > 1e-9:
                texte += f"  -# (contre {fmt(val_a)})"
        return texte

    lignes += ["",
               ligne("Cours", cette.minutes, avant.minutes, h),
               ligne("Séances", cette.seances, avant.seances, str),
               ligne("Jours avec cours", cette.jours, avant.jours, str),
               ligne("Trous", cette.trous, avant.trous, h),
               ligne("Devoirs à rendre", len(devoirs_cette), len(devoirs_avant), str)]

    noms = set(cette.matieres) | set(avant.matieres)
    if noms and not avant.vide:
        detail = []
        for nom in noms:
            c, a = cette.matieres.get(nom), avant.matieres.get(nom)
            if c is None:
                detail.append((a, f"• {nom} — plus au programme *(−{h(a)})*"))
            elif a is None:
                detail.append((c, f"• {nom} — **{h(c)}** *(nouveau)*"))
            elif abs(c - a) >= 1:
                detail.append((abs(c - a), f"• {nom} — **{h(c)}**  {_fleche(c - a, h)}"))
        if detail:
            lignes += ["", "**Par matière, ce qui bouge**"]
            lignes += [t for _, t in sorted(detail, key=lambda x: -x[0])[:8]]
    return lignes


# --- Les examens -------------------------------------------------------------
def examens(cours, liste_devoirs=None, maintenant=None):
    """Tous les examens a venir, d'ou qu'ils viennent, tries par date.

    CELCAT en connait certains (categorie ou intitule « examen », « partiel »,
    « DS »...), ton carnet de devoirs en connait d'autres (type examen). Les
    deux se completent ; un doublon evident (meme jour, meme matiere) n'est
    garde qu'une fois, version CELCAT.
    """
    maintenant = maintenant or datetime.now()
    sortie = []
    for c in cours:
        if c.est_examen and c.est_cours and (c.fin or c.debut) >= maintenant:
            sortie.append({"quand": c.debut, "titre": c.titre or c.module,
                           "matiere": _nom_matiere(c), "ou": c.ou,
                           "source": "celcat", "duree": c.minutes})
    for d in liste_devoirs or []:
        if d.get("fait") or d.get("type") != "examen":
            continue
        ech = promo.echeance_dt(d)
        if ech is None or ech < maintenant:
            continue
        cle = (ech.date(), normaliser(d.get("matiere") or d.get("titre") or ""))
        if any((e["quand"].date(), normaliser(e["matiere"])) == cle for e in sortie):
            continue
        sortie.append({"quand": ech, "titre": d.get("titre") or "Examen",
                       "matiere": d.get("matiere") or "", "ou": "",
                       "source": "devoir", "duree": 0, "id": d.get("id"),
                       "note": d.get("note") or ""})
    sortie.sort(key=lambda e: e["quand"])
    for e in sortie:
        e["jours"] = (e["quand"].date() - maintenant.date()).days
    return sortie


def minutes_libres(cours, jusqu_a, depuis=None):
    """Le temps libre exploitable d'ici une date, EN SEMAINE : les trous et
    les demi-journees vides, entre journee_debut et journee_fin.

    Les week-ends sont exclus, a dessein : compter onze heures « libres » par
    samedi et dimanche donne un total enorme et inutile. Ce qu'on veut savoir,
    c'est ce qu'on peut reviser sans toucher a ses week-ends."""
    depuis = depuis or datetime.now()
    mini = config.CRENEAU_LIBRE_MINUTES
    total = 0.0
    jour = depuis.date()
    while jour <= jusqu_a.date():
        if jour.weekday() >= 5:
            jour += timedelta(days=1)
            continue
        borne_debut = max(affichage.a_heure(jour, config.JOURNEE_DEBUT, (8, 0)), depuis)
        borne_fin = min(affichage.a_heure(jour, config.JOURNEE_FIN, (19, 0)),
                        jusqu_a if jour == jusqu_a.date() else datetime.max)
        curseur = borne_debut
        for c in du_jour(cours, jour):
            if not c.est_cours:
                continue
            if c.debut > curseur:
                creux = (min(c.debut, borne_fin) - curseur).total_seconds() / 60
                if creux >= mini:
                    total += creux
            curseur = max(curseur, c.fin or c.debut)
        if borne_fin > curseur:
            creux = (borne_fin - curseur).total_seconds() / 60
            if creux >= mini:
                total += creux
        jour += timedelta(days=1)
    return total


nom_matiere = _nom_matiere       # expose pour bot.py (les paris)


# --- La version texte --------------------------------------------------------
def _barre(part, largeur=14):
    """Une barre en caracteres pleins, pour l'embed de secours."""
    plein = max(1, round(part / 100 * largeur)) if part > 0 else 0
    return "█" * plein + "·" * (largeur - plein)


def bloc_stats(s, cours=None):
    """La semaine en texte : ce que Discord affiche si Pillow manque."""
    if s.vide:
        return [f"**{s.libelle}** — aucun cours cette semaine-la. 🌴"]

    ecart, origine = comparer_stats(s, cours)
    lignes = [f"**{affichage.duree_fr(s.total_minutes)} de cours** sur "
              f"{s.jours_travailles} jour{'s' if s.jours_travailles > 1 else ''} "
              f"· {s.seances} séances"]
    if ecart is not None:
        signe = "+" if ecart > 0 else "−"
        lignes.append(f"↳ {signe}{affichage.duree_fr(abs(ecart))} par rapport à la "
                      f"semaine précédente ({origine})"
                      if ecart else "↳ exactement autant que la semaine précédente")

    plein = s.jour_plein
    if plein:
        lignes.append(f"↳ jour le plus chargé : **{affichage.jour_fr(plein.jour, court=True)}**, "
                      f"{affichage.duree_fr(plein.minutes)}")
    if s.trous_minutes:
        lignes.append(f"↳ {affichage.duree_fr(s.trous_minutes)} de trous sur "
                      f"{s.nb_trous} créneau{'x' if s.nb_trous > 1 else ''}")
    if s.distanciel_minutes:
        lignes.append(f"↳ {affichage.duree_fr(s.distanciel_minutes)} à distance "
                      f"({s.part_distanciel:.0f} %)")
    if s.examens:
        lignes.append(f"↳ ⚠️ {len(s.examens)} examen{'s' if len(s.examens) > 1 else ''} "
                      f"cette semaine")

    lignes.append("")
    lignes.append("**Où passe ton temps**")
    for p in s.matieres[:8]:
        lignes.append(f"`{_barre(p.part_de(s.total_minutes))}` "
                      f"**{affichage.duree_fr(p.minutes)}** · {p.nom} "
                      f"({p.seances} séance{'s' if p.seances > 1 else ''})")
    if len(s.matieres) > 8:
        reste = sum(p.minutes for p in s.matieres[8:])
        lignes.append(f"`{_barre(reste / s.total_minutes * 100)}` "
                      f"**{affichage.duree_fr(reste)}** · {len(s.matieres) - 8} autres")
    return lignes


# ════════════════════════════════════════════════════════════════════════════
#  LA METEO  (ex-meteo.py)
# ════════════════════════════════════════════════════════════════════════════
# La meteo, mais seulement celle qui change quelque chose a ta journee.
#
# Un bulletin meteo complet n'aide personne : ce qui compte, quand on a cours a
# 8h30, c'est s'il pleut A L'HEURE OU TU SORS, et s'il faut partir plus tot. Ce
# module ne repond donc qu'a trois questions :
#
#     creneau()      quel temps a telle heure precise (celle de ton depart) ;
#     jour()         le resume d'une journee : mini, maxi, pluie cumulee ;
#     bloc_depart()  les deux lignes a coller dans le briefing du matin, avec
#                    le retard a prevoir quand il pleut.
#
# Source : Open-Meteo. C'est le seul service serieux qui ne demande NI compte NI
# cle d'API : rien de plus a remplir dans config.yaml pour que ca marche, et rien
# qui puisse expirer dans six mois. Les coordonnees par defaut sont celles de
# Cergy ; `meteo.latitude` / `meteo.longitude` dans config.yaml pour ailleurs.
#
# Le reseau n'est jamais une raison de rater un briefing : TOUTES les fonctions
# publiques rendent None plutot que de lever, et l'appelant se contente alors de
# ne rien afficher. La derniere reponse connue est gardee sur disque, donc une
# panne d'Open-Meteo un matin ne coute rien : on ressort le bulletin d'hier soir
# plutot que de faire semblant de ne rien savoir.


API = "https://api.open-meteo.com/v1/forecast"
FICHIER_METEO = config.FICHIER_METEO

# Les champs demandes a Open-Meteo. En demander moins ne va pas plus vite, en
# demander plus ne coute rien : la reponse tient dans quelques dizaines de ko.
HORAIRE = ("temperature_2m,apparent_temperature,precipitation,"
           "precipitation_probability,weather_code,wind_speed_10m")
QUOTIDIEN = ("temperature_2m_min,temperature_2m_max,precipitation_sum,"
             "precipitation_probability_max,weather_code,sunrise,sunset")

# Les codes WMO, groupes par ce qu'ils changent pour toi : (emoji, libelle,
# « est-ce mouille ? »). Le detail meteorologique ne sert a rien ici — savoir
# s'il faut un parapluie, si.
CODES = {
    0:  ("☀️", "ciel dégagé", False),
    1:  ("🌤️", "plutôt dégagé", False),
    2:  ("⛅", "nuages épars", False),
    3:  ("☁️", "couvert", False),
    45: ("🌫️", "brouillard", False),
    48: ("🌫️", "brouillard givrant", False),
    51: ("🌦️", "bruine légère", True),
    53: ("🌦️", "bruine", True),
    55: ("🌦️", "bruine dense", True),
    56: ("🌧️", "bruine verglaçante", True),
    57: ("🌧️", "bruine verglaçante", True),
    61: ("🌦️", "pluie faible", True),
    63: ("🌧️", "pluie", True),
    65: ("🌧️", "forte pluie", True),
    66: ("🌧️", "pluie verglaçante", True),
    67: ("🌧️", "pluie verglaçante", True),
    71: ("🌨️", "neige faible", True),
    73: ("🌨️", "neige", True),
    75: ("❄️", "forte neige", True),
    77: ("🌨️", "grains de neige", True),
    80: ("🌦️", "averses", True),
    81: ("🌧️", "averses", True),
    82: ("🌧️", "fortes averses", True),
    85: ("🌨️", "averses de neige", True),
    86: ("❄️", "fortes averses de neige", True),
    95: ("⛈️", "orage", True),
    96: ("⛈️", "orage et grêle", True),
    99: ("⛈️", "orage et grêle", True),
}


def libelle_meteo(code):
    """(emoji, texte, mouille) pour un code WMO, meme inconnu."""
    return CODES.get(int(code) if code is not None else -1, ("🌡️", "temps incertain", False))


@dataclass(frozen=True)
class Creneau:
    """Le temps a une heure precise."""
    quand: datetime
    temperature: float
    ressenti: float
    pluie: float             # mm sur l'heure
    probabilite: int         # % de chance de precipitations
    code: int
    vent: float              # km/h

    @property
    def emoji(self):
        return libelle_meteo(self.code)[0]

    @property
    def texte(self):
        return libelle_meteo(self.code)[1]

    @property
    def mouille(self):
        """Faut-il un parapluie ? Le code WMO dit le type de temps, mais c'est
        la quantite qui tranche : un code « bruine » a 0.0 mm ne mouille
        personne, et 2 mm sous un code « couvert » si."""
        return self.pluie >= config.METEO_SEUIL_PLUIE or (
            libelle_meteo(self.code)[2] and self.probabilite >= 50)

    @property
    def froid(self):
        return self.ressenti <= 5

    def detail(self):
        """Le temps, sans pictogramme : « pluie · 8 °C · ressenti 5 °C ».

        Les images dessinent leur propre pictogramme (la police d'un serveur
        n'a pas d'emoji), elles ont donc besoin du texte seul."""
        morceaux = [self.texte, f"{self.temperature:.0f} °C"]
        if abs(self.ressenti - self.temperature) >= 2:
            morceaux.append(f"ressenti {self.ressenti:.0f} °C")
        if self.pluie >= config.METEO_SEUIL_PLUIE:
            morceaux.append(f"{self.pluie:.1f} mm")
        elif self.probabilite >= 40:
            morceaux.append(f"{self.probabilite} % de pluie")
        if self.vent >= 35:
            morceaux.append(f"vent {self.vent:.0f} km/h")
        return " · ".join(morceaux)

    def resume(self):
        """Une ligne : « 🌧️ pluie · 8 °C · ressenti 5 °C »."""
        return f"{self.emoji} {self.detail()}"


@dataclass(frozen=True)
class Jour:
    """Le resume d'une journee entiere."""
    jour: date
    mini: float
    maxi: float
    pluie: float             # mm cumules
    probabilite: int
    code: int
    lever: str = ""
    coucher: str = ""

    @property
    def emoji(self):
        return libelle_meteo(self.code)[0]

    @property
    def texte(self):
        return libelle_meteo(self.code)[1]

    def detail(self):
        morceaux = [self.texte.capitalize(), f"{self.mini:.0f} à {self.maxi:.0f} °C"]
        if self.pluie >= config.METEO_SEUIL_PLUIE:
            morceaux.append(f"{self.pluie:.1f} mm de pluie")
        return " · ".join(morceaux)

    def resume(self):
        return f"{self.emoji} {self.detail()}"


# --- Le cache ----------------------------------------------------------------
def _lire_cache():
    try:
        data = json.loads(FICHIER_METEO.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (ValueError, OSError):
        return None


def _ecrire_cache(data):
    try:
        config.preparer_dossiers()
        FICHIER_METEO.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        print(f"[!] cache meteo non ecrit : {e}", flush=True)


def _frais(cache):
    """Le cache est-il assez recent pour qu'on s'en contente ?"""
    try:
        age = (datetime.now() - datetime.fromisoformat(cache["lu_le"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return False
    return 0 <= age < config.METEO_CACHE_MINUTES * 60


def age_minutes():
    """Depuis combien de minutes le bulletin en cache a-t-il ete lu ? None si
    on n'a jamais rien lu — c'est ce qu'affiche /statut."""
    cache = _lire_cache()
    try:
        return int((datetime.now() -
                    datetime.fromisoformat(cache["lu_le"])).total_seconds() / 60)
    except (KeyError, TypeError, ValueError):
        return None


# --- L'appel reseau ----------------------------------------------------------
def bulletin(force=False):
    """Le bulletin brut, du cache ou du reseau. None si on n'a jamais rien eu.

    Un echec reseau ne vide jamais le cache : mieux vaut une prevision de trois
    heures qu'aucune prevision.
    """
    if not config.METEO_ACTIVE:
        return None

    cache = _lire_cache()
    if cache and not force and _frais(cache):
        return cache

    params = {
        "latitude": config.METEO_LAT, "longitude": config.METEO_LON,
        "hourly": HORAIRE, "daily": QUOTIDIEN,
        "timezone": "auto", "forecast_days": 4,
    }
    try:
        r = requests.get(API, params=params, timeout=12,
                         headers={"User-Agent": "assistant-cyu/2.0"})
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[!] meteo indisponible ({type(e).__name__}: {e})", flush=True)
        return cache          # perime, mais toujours mieux que rien

    if not isinstance(data, dict) or "hourly" not in data:
        return cache
    data["lu_le"] = datetime.now().isoformat(timespec="seconds")
    _ecrire_cache(data)
    return data


def perime(cache=None):
    """Le bulletin affiche vient-il d'un cache trop vieux pour etre sur ?"""
    cache = cache if cache is not None else _lire_cache()
    if not cache:
        return True
    return not _frais(cache) and (age_minutes() or 0) > 180


# --- Lire le bulletin --------------------------------------------------------
def _serie(data, groupe, nom):
    valeurs = (data.get(groupe) or {}).get(nom)
    return valeurs if isinstance(valeurs, list) else []


def _nombre(valeur, defaut=0.0):
    try:
        return float(valeur)
    except (TypeError, ValueError):
        return defaut


def creneau(quand=None, data=None):
    """Le temps a l'heure `quand`, arrondie a l'heure pleine la plus proche.

    Rend None si l'heure demandee sort de la prevision (au-dela de quatre
    jours) ou si on n'a aucun bulletin.
    """
    data = data if data is not None else bulletin()
    if not data:
        return None
    quand = quand or datetime.now()
    cible = quand.replace(minute=0, second=0, microsecond=0)
    if quand.minute >= 30:
        cible += timedelta(hours=1)

    heures = _serie(data, "hourly", "time")
    try:
        i = heures.index(cible.strftime("%Y-%m-%dT%H:00"))
    except ValueError:
        return None

    def valeur(nom, defaut=0.0):
        serie = _serie(data, "hourly", nom)
        return _nombre(serie[i], defaut) if i < len(serie) else defaut

    return Creneau(
        quand=cible,
        temperature=valeur("temperature_2m"),
        ressenti=valeur("apparent_temperature", valeur("temperature_2m")),
        pluie=valeur("precipitation"),
        probabilite=int(valeur("precipitation_probability")),
        code=int(valeur("weather_code")),
        vent=valeur("wind_speed_10m"),
    )


def jour(quand=None, data=None):
    """Le resume de la journee `quand`. None si elle sort de la prevision."""
    data = data if data is not None else bulletin()
    if not data:
        return None
    quand = quand or date.today()
    if isinstance(quand, datetime):
        quand = quand.date()

    jours = _serie(data, "daily", "time")
    try:
        i = jours.index(quand.isoformat())
    except ValueError:
        return None

    def valeur(nom, defaut=0.0):
        serie = _serie(data, "daily", nom)
        return _nombre(serie[i], defaut) if i < len(serie) else defaut

    def texte(nom):
        serie = _serie(data, "daily", nom)
        brut = str(serie[i]) if i < len(serie) else ""
        return brut[11:16] if len(brut) >= 16 else ""

    return Jour(
        jour=quand,
        mini=valeur("temperature_2m_min"),
        maxi=valeur("temperature_2m_max"),
        pluie=valeur("precipitation_sum"),
        probabilite=int(valeur("precipitation_probability_max")),
        code=int(valeur("weather_code")),
        lever=texte("sunrise"), coucher=texte("sunset"),
    )


def fenetre(debut, fin, data=None):
    """Tous les creneaux horaires entre deux heures, bornes comprises.

    Sert a couvrir un trajet qui dure : partir au sec pour arriver trempe n'est
    pas ce qu'on appelle une bonne prevision.
    """
    data = data if data is not None else bulletin()
    if not data or fin < debut:
        return []
    sortie, curseur = [], debut.replace(minute=0, second=0, microsecond=0)
    while curseur <= fin:
        c = creneau(curseur, data)
        if c:
            sortie.append(c)
        curseur += timedelta(hours=1)
    return sortie


def pire(creneaux):
    """Le creneau le plus genant d'une liste : le plus mouille, sinon le plus
    froid. C'est celui-la qu'il faut annoncer, pas la moyenne."""
    if not creneaux:
        return None
    return max(creneaux, key=lambda c: (c.mouille, c.pluie, -c.ressenti))


# --- Ce que le briefing affiche ---------------------------------------------
def marge_pluie(creneaux):
    """Les minutes a ajouter au trajet quand il pleut. Zero s'il fait sec :
    une marge permanente serait vite ignoree, donc inutile."""
    if not config.METEO_MARGE_PLUIE_MINUTES:
        return 0
    return config.METEO_MARGE_PLUIE_MINUTES if any(c.mouille for c in creneaux) else 0


def trajet(depart, arrivee=None):
    """(creneaux, marge) pour un trajet : tout ce qu'il faut avant de sortir.

    La marge est calculee sur l'horaire NOMINAL, pas sur l'horaire deja
    decale : partir dix minutes plus tot ne doit pas relancer le calcul, sinon
    on reculerait l'heure de depart a chaque appel.
    """
    creneaux = fenetre(depart, arrivee or depart)
    return creneaux, marge_pluie(creneaux)


def conseil(creneaux):
    """La phrase a suivre : parapluie, manteau, rien du tout."""
    gene = pire(creneaux)
    if gene is None:
        return ""
    if gene.mouille and gene.temperature <= 2:
        return "prends de quoi ne pas glisser, ça tient au sol"
    if gene.mouille:
        return "prends un parapluie"
    if gene.froid:
        return "couvre-toi, il fait froid dehors"
    if gene.vent >= 45:
        return "ça souffle fort, oublie la capuche"
    if gene.temperature >= 28:
        return "prends de l'eau, il va faire chaud"
    return ""


def bloc_depart(depart=None, arrivee=None, jour_=None):
    """Les lignes meteo du briefing du matin. Liste vide = rien a dire.

    `depart` / `arrivee` encadrent le trajet ; sans eux on parle de la journee
    en general. On n'invente jamais une ligne « il fait beau » : quand il n'y a
    rien a signaler, on ne prend pas la place d'une ligne utile.
    """
    data = bulletin()
    if not data:
        return []

    lignes = []
    resume_jour = jour(jour_ or date.today(), data)
    if resume_jour:
        lignes.append(f"{resume_jour.emoji} **{resume_jour.texte.capitalize()}** · "
                      f"{resume_jour.mini:.0f} à {resume_jour.maxi:.0f} °C"
                      + (f" · {resume_jour.pluie:.1f} mm de pluie"
                         if resume_jour.pluie >= config.METEO_SEUIL_PLUIE else ""))

    if depart is not None:
        creneaux = fenetre(depart, arrivee or depart, data)
        gene = pire(creneaux)
        if gene is not None:
            quand = ("au départ" if not arrivee or arrivee <= depart
                     else "sur le trajet")
            lignes.append(f"↳ {quand} ({depart:%H:%M}) : {gene.resume()}")
        phrase = conseil(creneaux)
        if phrase:
            lignes.append(f"↳ **{phrase.capitalize()}.**")

    if perime(data):
        lignes.append("↳ *bulletin non rafraîchi, Open-Meteo est injoignable.*")
    return lignes


def ligne_courte(quand=None):
    """Une seule ligne, pour le panneau de statut : « 🌧️ 8 °C, pluie »."""
    c = creneau(quand)
    if c is None:
        return ""
    return f"{c.emoji} {c.temperature:.0f} °C, {c.texte}"
