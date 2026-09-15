#!/usr/bin/env python3
"""
La meteo, mais seulement celle qui change quelque chose a ta journee.

Un bulletin meteo complet n'aide personne : ce qui compte, quand on a cours a
8h30, c'est s'il pleut A L'HEURE OU TU SORS, et s'il faut partir plus tot. Ce
module ne repond donc qu'a trois questions :

    creneau()      quel temps a telle heure precise (celle de ton depart) ;
    jour()         le resume d'une journee : mini, maxi, pluie cumulee ;
    bloc_depart()  les deux lignes a coller dans le briefing du matin, avec
                   le retard a prevoir quand il pleut.

Source : Open-Meteo. C'est le seul service serieux qui ne demande NI compte NI
cle d'API : rien de plus a remplir dans config.yaml pour que ca marche, et rien
qui puisse expirer dans six mois. Les coordonnees par defaut sont celles de
Cergy ; `meteo.latitude` / `meteo.longitude` dans config.yaml pour ailleurs.

Le reseau n'est jamais une raison de rater un briefing : TOUTES les fonctions
publiques rendent None plutot que de lever, et l'appelant se contente alors de
ne rien afficher. La derniere reponse connue est gardee sur disque, donc une
panne d'Open-Meteo un matin ne coute rien : on ressort le bulletin d'hier soir
plutot que de faire semblant de ne rien savoir.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import requests

import config

API = "https://api.open-meteo.com/v1/forecast"
FICHIER = config.FICHIER_METEO

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


def libelle(code):
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
        return libelle(self.code)[0]

    @property
    def texte(self):
        return libelle(self.code)[1]

    @property
    def mouille(self):
        """Faut-il un parapluie ? Le code WMO dit le type de temps, mais c'est
        la quantite qui tranche : un code « bruine » a 0.0 mm ne mouille
        personne, et 2 mm sous un code « couvert » si."""
        return self.pluie >= config.METEO_SEUIL_PLUIE or (
            libelle(self.code)[2] and self.probabilite >= 50)

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
        return libelle(self.code)[0]

    @property
    def texte(self):
        return libelle(self.code)[1]

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
        data = json.loads(FICHIER.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (ValueError, OSError):
        return None


def _ecrire_cache(data):
    try:
        config.preparer_dossiers()
        FICHIER.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
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
