#!/usr/bin/env python3
"""
« Qui est ce Pokemon ? » : le quiz de la promo.

Le bot montre une SILHOUETTE et propose quatre noms. Le premier qui clique
juste marque ; les autres jouent quand meme, pour leur score. Ceux qui veulent
le mode difficile tapent le nom eux-memes, et ca vaut plus cher.

Ce module ne fait que garder les donnees sur disque, tirer au sort et compter.
Il ne sait rien de Discord : bot.py lui passe des identifiants et des noms
d'utilisateur, et affiche ce qu'il rend -- exactement comme predictions.py.

Pourquoi des boutons, et pas le nom tape dans le salon
------------------------------------------------------
Lire ce que les gens ecrivent demande l'intent « contenu des messages », un
droit privilegie a cocher dans le portail Discord. Tout le reste du projet
s'en passe (voir bot.Assistant), et l'activer pour un jeu ferait lire TOUS les
messages du serveur par le bot. Quatre boutons suffisent, et le formulaire
« je tape le nom » rend le mode difficile a ceux qui le cherchent, sans rien
demander de plus.

D'ou viennent les Pokemon
-------------------------
De PokeAPI, en deux temps et une seule fois :

  * les NOMS francais des 1025 especes, un fichier CSV telecharge une fois et
    garde dans donnees/ ;
  * les IMAGES, a la demande : seules celles deja tirees finissent en cache,
    redimensionnees. Personne n'a besoin des 1025 artworks sur son disque.

Sans reseau et sans cache, le quiz le dit au lieu de tomber en panne.

Un quiz :

    {"id": "a3f9c1b2", "espece": 25, "choix": [25, 6, 94, 143],
     "ouvert_le": "...", "ferme_le": "...", "revele": false,
     "salon_id": "...", "message_id": "...",
     "reponses": {"1234": {"nom": "Simon", "choix": 6, "juste": false,
                           "tape": false, "points": 0, "quand": "..."}}}
"""

from __future__ import annotations

import csv
import io
import json
import random
import re
import unicodedata
from datetime import datetime, timedelta

import config

FICHIER = config.DONNEES / "pokemon.json"
CATALOGUE = config.DONNEES / "pokemon-noms.json"
IMAGES = config.DONNEES / "pokemon"

# Les noms de toutes les especes, dans toutes les langues, en UN fichier. Les
# interroger une par une ferait 1025 requetes ; ce CSV en fait une seule.
URL_NOMS = ("https://raw.githubusercontent.com/PokeAPI/pokeapi/master/"
            "data/v2/csv/pokemon_species_names.csv")
LANGUE_FR = "5"

# L'illustration officielle, bien plus lisible en silhouette que le petit
# sprite de jeu (96 pixels, illisible des qu'on l'agrandit).
URL_IMAGE = ("https://raw.githubusercontent.com/PokeAPI/sprites/master/"
             "sprites/pokemon/other/official-artwork/{id}.png")

# Les bornes de chaque generation : de quoi limiter le tirage a ce que les
# gens connaissent vraiment. Au-dela de la 3e, il faut etre courageux.
GENERATIONS = {
    1: (1, 151), 2: (152, 251), 3: (252, 386), 4: (387, 493), 5: (494, 649),
    6: (650, 721), 7: (722, 809), 8: (810, 905), 9: (906, 1025),
}

# Ce que rapporte une bonne reponse. Le premier a trouver prend une prime :
# sans elle, rien ne distingue celui qui cherche de celui qui attend.
POINTS_JUSTE = 3
PRIME_PREMIER = 2
PRIME_TAPE = 2                  # avoir tape le nom au lieu de cliquer
PRIME_SERIE = 1                 # a partir de SERIE_MINIMUM bonnes d'affilee
SERIE_MINIMUM = 3

# Comment on nomme quelqu'un dont Discord ne nous a pas donne le nom.
ANONYME = "quelqu'un"

# Un quiz sans reponse finit par se reveler tout seul.
DUREE_MINUTES = 3

# Ce qu'on garde : au-dela, les vieux quiz n'interessent plus personne et le
# fichier grossit pour rien. Les SCORES, eux, ne sont jamais effaces.
QUIZ_GARDES = 60
IMAGES_GARDEES = 400


class PokemonIndisponible(RuntimeError):
    """Pas de catalogue et pas de reseau : le quiz ne peut pas commencer."""


# --- Lecture et ecriture -----------------------------------------------------
def lire():
    try:
        data = json.loads(FICHIER.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"quiz": [], "scores": {}}
        data.setdefault("quiz", [])
        data.setdefault("scores", {})
        return data
    except (ValueError, OSError):
        return {"quiz": [], "scores": {}}


def ecrire(data):
    data["quiz"] = data.get("quiz", [])[:QUIZ_GARDES]
    FICHIER.parent.mkdir(parents=True, exist_ok=True)
    FICHIER.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                       encoding="utf-8")


# --- Le catalogue des especes -------------------------------------------------
_CACHE = {}


def catalogue(telecharger=True):
    """{numero: {"nom": "Pikachu", "genre": "Pokémon Souris"}}.

    Lu une fois puis garde en memoire. Le fichier n'est telecharge que s'il
    manque : les noms des Pokemon ne changent pas, et un quiz ne doit pas
    dependre du reseau a chaque partie.
    """
    if _CACHE:
        return _CACHE
    try:
        data = json.loads(CATALOGUE.read_text(encoding="utf-8"))
        if data:
            _CACHE.update({int(k): v for k, v in data.items()})
            return _CACHE
    except (ValueError, OSError):
        pass
    if not telecharger:
        raise PokemonIndisponible("Le catalogue des Pokémon n'a pas encore été "
                                  "téléchargé.")
    _CACHE.update(_telecharger_noms())
    return _CACHE


def _telecharger_noms():
    """Le CSV de PokeAPI, filtre sur le francais, ecrit dans donnees/."""
    try:
        import requests
    except ImportError as e:                                 # pragma: no cover
        raise PokemonIndisponible("Le module `requests` n'est pas installé.") from e
    try:
        reponse = requests.get(URL_NOMS, timeout=20)
        reponse.raise_for_status()
    except Exception as e:                      # noqa: BLE001 - reseau : tout peut arriver
        raise PokemonIndisponible(
            f"Impossible de télécharger la liste des Pokémon ({e}).") from e

    especes = {}
    for ligne in csv.DictReader(io.StringIO(reponse.text)):
        if ligne.get("local_language_id") != LANGUE_FR:
            continue
        try:
            numero = int(ligne["pokemon_species_id"])
        except (KeyError, ValueError):
            continue
        especes[numero] = {"nom": ligne.get("name", "").strip(),
                           "genre": (ligne.get("genus") or "").strip()}
    especes = {n: v for n, v in especes.items() if v["nom"]}
    if not especes:
        raise PokemonIndisponible("La liste téléchargée ne contient aucun nom "
                                  "français.")
    try:
        CATALOGUE.parent.mkdir(parents=True, exist_ok=True)
        CATALOGUE.write_text(json.dumps(especes, ensure_ascii=False, indent=0),
                             encoding="utf-8")
    except OSError:
        pass                    # tant pis pour le cache, le quiz marche quand meme
    return especes


def nom(numero):
    return catalogue(telecharger=False).get(int(numero), {}).get("nom", f"#{numero}")


def genre(numero):
    return catalogue(telecharger=False).get(int(numero), {}).get("genre", "")


def disponible():
    """Le jeu peut-il demarrer sans reseau ? Sert a n'afficher le bouton que
    si oui -- une fonction synchrone, appelee par le panneau."""
    try:
        return bool(catalogue(telecharger=False))
    except (PokemonIndisponible, OSError):
        return False


# --- Les images ---------------------------------------------------------------
def image(numero):
    """Le chemin du PNG d'une espece, telecharge et mis en cache au besoin.

    L'image est gardee telle quelle : c'est image.py qui en tire la silhouette
    puis la revelation, a partir du meme fichier."""
    numero = int(numero)
    chemin = IMAGES / f"{numero}.png"
    if chemin.exists() and chemin.stat().st_size > 0:
        return chemin
    try:
        import requests
        reponse = requests.get(URL_IMAGE.format(id=numero), timeout=20)
        reponse.raise_for_status()
        octets = reponse.content
    except Exception as e:                      # noqa: BLE001 - reseau
        raise PokemonIndisponible(
            f"L'image de {nom(numero)} n'a pas pu être téléchargée ({e}).") from e
    try:
        IMAGES.mkdir(parents=True, exist_ok=True)
        chemin.write_bytes(octets)
        _purger_images()
    except OSError as e:
        raise PokemonIndisponible(f"Impossible d'écrire l'image ({e}).") from e
    return chemin


def _purger_images():
    """Le cache d'images ne grossit pas indefiniment : au-dela de la limite,
    les plus anciennement utilisees s'en vont. Elles se retelechargeront si
    elles ressortent au tirage."""
    try:
        fichiers = sorted(IMAGES.glob("*.png"), key=lambda p: p.stat().st_atime)
    except OSError:
        return
    for vieux in fichiers[:-IMAGES_GARDEES]:
        try:
            vieux.unlink()
        except OSError:
            pass


# --- Tirer un quiz -------------------------------------------------------------
def bornes(generation=None):
    """(premier, dernier) numero tirable. Sans generation, tout le dex."""
    if generation and int(generation) in GENERATIONS:
        return GENERATIONS[int(generation)]
    maxi = max(catalogue(telecharger=False) or [1])
    return 1, maxi


def tirer(generation=None, choix=4, eviter=()):
    """(espece, [choix melanges]) : un Pokemon a deviner et ses leurres.

    `eviter` sert a ne pas reposer la meme question deux fois de suite : rien
    n'use plus vite un quiz que de retomber sur le Pokemon d'il y a deux tours.
    """
    especes = catalogue(telecharger=False)
    premier, dernier = bornes(generation)
    plage = [n for n in especes if premier <= n <= dernier]
    if not plage:
        raise PokemonIndisponible("Aucun Pokémon dans cette génération.")

    frais = [n for n in plage if n not in set(eviter)] or plage
    espece = random.choice(frais)
    leurres = [n for n in plage if n != espece]
    random.shuffle(leurres)
    propositions = [espece] + leurres[:max(0, choix - 1)]
    random.shuffle(propositions)
    return espece, propositions


def ouvrir(espece, propositions, salon_id=None, minutes=None):
    """Le quiz enregistre, pret a recevoir des reponses."""
    maintenant = datetime.now()
    minutes = DUREE_MINUTES if minutes is None else minutes
    quiz = {
        "id": f"{random.getrandbits(32):08x}",
        "espece": int(espece),
        "choix": [int(n) for n in propositions],
        "ouvert_le": maintenant.isoformat(timespec="seconds"),
        "ferme_le": (maintenant + timedelta(minutes=minutes)).isoformat(
            timespec="seconds"),
        "revele": False,
        "salon_id": str(salon_id) if salon_id else "",
        "message_id": "",
        "reponses": {},
    }
    data = lire()
    data["quiz"].insert(0, quiz)
    ecrire(data)
    return quiz


def par_id(quiz_id, data=None):
    data = data or lire()
    for q in data.get("quiz", []):
        if q.get("id") == str(quiz_id):
            return q
    return None


def derniers(nombre=8):
    """Les especes recemment posees, pour ne pas les reposer tout de suite."""
    return [q.get("espece") for q in lire().get("quiz", [])[:nombre]]


def retenir_message(quiz_id, message_id, salon_id):
    """Ou le quiz a ete poste : la boucle en a besoin pour le reveler."""
    data = lire()
    quiz = par_id(quiz_id, data)
    if quiz is None:
        return
    quiz["message_id"] = str(message_id)
    quiz["salon_id"] = str(salon_id)
    ecrire(data)


# --- Repondre -------------------------------------------------------------------
def _sans_fioritures(texte):
    """« Dracaufeu » et « dracaufeu » sont le meme mot ; « M. Mime » aussi.

    On enleve les accents, la casse, et tout ce qui n'est pas une lettre ou un
    chiffre : personne ne doit perdre un point sur un trait d'union."""
    texte = unicodedata.normalize("NFD", str(texte or ""))
    texte = "".join(c for c in texte if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]", "", texte.lower())


def repondre(quiz_id, joueur_id, joueur, choix=None, tape=None):
    """Une reponse. Rend (etat, quiz, points).

    etat vaut :
      "juste"    bonne reponse ;
      "rate"     mauvaise reponse ;
      "deja"     ce joueur a deja repondu -- un quiz, une tentative, sinon il
                 suffirait de cliquer les quatre boutons ;
      "clos"     le quiz est deja revele ;
      "inconnu"  ce quiz n'existe plus.
    """
    data = lire()
    quiz = par_id(quiz_id, data)
    if quiz is None:
        return "inconnu", None, 0
    if quiz.get("revele"):
        return "clos", quiz, 0
    if str(joueur_id) in quiz.get("reponses", {}):
        return "deja", quiz, 0

    espece = int(quiz["espece"])
    if tape is not None:
        juste = _sans_fioritures(tape) == _sans_fioritures(nom(espece))
        choisi = espece if juste else None
    else:
        choisi = int(choix) if choix is not None else None
        juste = choisi == espece

    points = 0
    scores = data.setdefault("scores", {})
    fiche = scores.setdefault(str(joueur_id), {
        "nom": joueur, "points": 0, "bonnes": 0, "tentatives": 0,
        "serie": 0, "record": 0, "tapes": 0})
    fiche["nom"] = joueur or fiche.get("nom", "")
    fiche["tentatives"] = fiche.get("tentatives", 0) + 1

    if juste:
        premier = not any(r.get("juste") for r in quiz.get("reponses", {}).values())
        points = POINTS_JUSTE + (PRIME_PREMIER if premier else 0)
        if tape is not None:
            points += PRIME_TAPE
            fiche["tapes"] = fiche.get("tapes", 0) + 1
        fiche["serie"] = fiche.get("serie", 0) + 1
        if fiche["serie"] >= SERIE_MINIMUM:
            points += PRIME_SERIE
        fiche["record"] = max(fiche.get("record", 0), fiche["serie"])
        fiche["bonnes"] = fiche.get("bonnes", 0) + 1
        fiche["points"] = fiche.get("points", 0) + points
    else:
        fiche["serie"] = 0

    quiz.setdefault("reponses", {})[str(joueur_id)] = {
        "nom": joueur, "choix": choisi, "juste": juste,
        "tape": tape is not None, "points": points,
        "quand": datetime.now().isoformat(timespec="seconds")}
    ecrire(data)
    return ("juste" if juste else "rate"), quiz, points


def reveler(quiz_id):
    """Marquer le quiz comme termine. Rend le quiz, ou None."""
    data = lire()
    quiz = par_id(quiz_id, data)
    if quiz is None:
        return None
    quiz["revele"] = True
    ecrire(data)
    return quiz


def gagnant(quiz):
    """Le premier a avoir trouve, ou None."""
    justes = [(r.get("quand", ""), i, r)
              for i, r in (quiz or {}).get("reponses", {}).items() if r.get("juste")]
    return min(justes)[2] if justes else None


def a_reveler(maintenant=None):
    """Les quiz dont le temps est ecoule et que personne n'a clos.

    C'est la boucle du bot qui appelle ceci toutes les trente secondes : un
    quiz ne doit pas rester ouvert parce que le bot a redemarre entre-temps.
    """
    maintenant = maintenant or datetime.now()
    sortie = []
    for quiz in lire().get("quiz", []):
        if quiz.get("revele") or not quiz.get("message_id"):
            continue
        try:
            fin = datetime.fromisoformat(quiz.get("ferme_le", ""))
        except ValueError:
            continue
        if fin <= maintenant:
            sortie.append(quiz)
    return sortie


# --- Le classement ---------------------------------------------------------------
def classement(limite=10):
    """[(rang, fiche)] : les meilleurs, par points puis par reussite."""
    fiches = []
    for ident, f in lire().get("scores", {}).items():
        if not f.get("tentatives"):
            continue
        fiche = dict(f)
        fiche["id"] = ident
        fiche["reussite"] = round(100 * f.get("bonnes", 0) / f["tentatives"])
        fiches.append(fiche)
    fiches.sort(key=lambda f: (-f.get("points", 0), -f.get("reussite", 0),
                               f.get("nom", "")))
    return [(i + 1, f) for i, f in enumerate(fiches[:limite])]


def fiche(joueur_id):
    """Le score de quelqu'un, meme s'il n'a jamais joue."""
    brut = lire().get("scores", {}).get(str(joueur_id))
    if not brut:
        return {"nom": "", "points": 0, "bonnes": 0, "tentatives": 0,
                "serie": 0, "record": 0, "tapes": 0, "reussite": 0, "rang": None}
    fiche = dict(brut)
    fiche["reussite"] = round(100 * brut.get("bonnes", 0) /
                              max(1, brut.get("tentatives", 0)))
    fiche["rang"] = next((r for r, f in classement(999)
                          if f["id"] == str(joueur_id)), None)
    return fiche


def total_joue():
    """Combien de quiz ont ete poses : de quoi dire « la 42e question »."""
    return len(lire().get("quiz", []))


# --- Le repli en texte -------------------------------------------------------------
def bloc_classement(limite=10):
    """Le classement en lignes Markdown, quand l'image manque."""
    lignes = []
    for rang, f in classement(limite):
        medaille = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rang, f"**{rang}.**")
        detail = (f"{f.get('bonnes', 0)}/{f.get('tentatives', 0)} bonnes · "
                  f"{f.get('reussite', 0)} %")
        if f.get("record", 0) >= SERIE_MINIMUM:
            detail += f" · série record {f['record']}"
        lignes.append(f"{medaille} **{f.get('nom') or ANONYME}** — "
                      f"**{f.get('points', 0)} pts**\n-# {detail}")
    return lignes or ["Personne n'a encore joué. `/quiz` pour lancer la première "
                      "question."]


def bloc_resultat(quiz):
    """Ce qu'on affiche quand la reponse est revelee."""
    espece = int(quiz.get("espece", 0))
    lignes = [f"C'était **{nom(espece)}** — n°{espece}"
              + (f", {genre(espece).lower()}" if genre(espece) else "") + "."]
    reponses = quiz.get("reponses", {})
    justes = [r for r in reponses.values() if r.get("juste")]
    if not reponses:
        lignes.append("-# Personne n'a tenté sa chance.")
        return lignes
    if justes:
        premier = gagnant(quiz)
        noms = ", ".join(r.get("nom") or ANONYME for r in justes)
        lignes.append(f"✅ **{len(justes)}** sur **{len(reponses)}** ont trouvé — "
                      f"{noms}.")
        if premier:
            lignes.append(f"-# 🥇 {premier.get('nom') or ANONYME} a répondu "
                          f"le premier (+{premier.get('points', 0)} pts).")
    else:
        lignes.append(f"❌ Personne n'a trouvé, sur **{len(reponses)}** "
                      f"tentative{'s' if len(reponses) > 1 else ''}.")
    return lignes
