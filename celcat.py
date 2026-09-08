#!/usr/bin/env python3
"""
Tout ce qui touche a l'emploi du temps CELCAT : se connecter, lire les cours,
les transformer en objets exploitables, et garder un cache sur disque.

CELCAT n'est PAS derriere le SSO SAML casse de mail.etu.cyu.fr : c'est un
formulaire LDAP classique (POST /LdapLogin/Logon), donc un script peut s'y
connecter avec les identifiants de config.yaml.

Le cache sert a deux choses : les commandes du bot repondent instantanement
sans refaire un aller-retour CELCAT, et l'assistant continue de fonctionner
quand le reseau de l'ecole tombe.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from getpass import getpass

import requests

import config

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
