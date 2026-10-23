#!/usr/bin/env python3
"""
Lire et ecrire les fichiers JSON de donnees/, sans jamais les perdre.

Tout le projet gardait ses donnees de la meme facon :

    FICHIER.write_text(json.dumps(data))

Deux problemes, que ce module regle une fois pour toutes :

  * l'ecriture n'est pas ATOMIQUE. Un bot coupe au milieu (redemarrage du
    service, coupure de courant) laisse un fichier a moitie ecrit ; la lecture
    suivante echoue, rend une liste vide sans rien dire, et le prochain ajout
    ecrase tout le carnet de devoirs avec une liste d'un seul element. On
    ecrit donc dans un fichier temporaire, puis on le met a la place d'un
    coup (os.replace) — et on garde l'ancienne version en `.bak`, relue si
    jamais le fichier principal est illisible ;

  * deux fils peuvent LIRE-MODIFIER-ECRIRE le meme fichier en meme temps :
    deux votes simultanes sur une prediction, le daemon et le bot. Le second
    ecrase le premier, un vote disparait. `@exclusif(FICHIER)` met la
    fonction sous le verrou du fichier : les lectures et ecritures qu'elle
    fait sont alors vues comme une seule operation.

Le verrou vaut pour le processus : c'est le cas du bot (daemon et commandes
dans le meme). uptime.py, qui tourne a part, n'ecrit que son propre fichier.
"""

from __future__ import annotations

import copy
import functools
import json
import os
import shutil
import threading
import time
from pathlib import Path

import config

_VERROUS: dict[str, threading.RLock] = {}
_VERROU_DES_VERROUS = threading.Lock()


def verrou(chemin):
    """Le verrou d'un fichier. Reentrant : une fonction sous @exclusif peut
    appeler lire() et ecrire(), qui le reprennent sans se bloquer."""
    cle = str(Path(chemin).resolve())
    with _VERROU_DES_VERROUS:
        return _VERROUS.setdefault(cle, threading.RLock())


def exclusif(chemin):
    """Decorateur : toute la fonction s'execute sous le verrou du fichier."""
    def decorer(fonction):
        @functools.wraps(fonction)
        def enveloppe(*args, **kw):
            with verrou(chemin):
                return fonction(*args, **kw)
        return enveloppe
    return decorer


def _secours(chemin):
    chemin = Path(chemin)
    return chemin.with_name(chemin.name + ".bak")


def _charger(chemin):
    return json.loads(Path(chemin).read_text(encoding="utf-8"))


def lire(chemin, defaut):
    """Le contenu du fichier, ou une copie de `defaut` s'il n'existe pas, s'il
    est illisible, ou s'il n'a pas le type de `defaut` (liste, dict).

    Un fichier present mais illisible n'est pas « vide » : c'est une ecriture
    interrompue, ou une main malheureuse. On relit alors la version
    precedente (.bak) plutot que de repartir de rien."""
    chemin = Path(chemin)
    with verrou(chemin):
        try:
            data = _charger(chemin)
        except FileNotFoundError:
            return copy.deepcopy(defaut)
        except (ValueError, OSError) as e:
            try:
                data = _charger(_secours(chemin))
                print(f"[!] {chemin.name} illisible ({type(e).__name__}) : "
                      f"relu depuis {_secours(chemin).name}", flush=True)
            except (ValueError, OSError):
                print(f"[!] {chemin.name} illisible et sans copie de secours : "
                      f"on repart de zero", flush=True)
                return copy.deepcopy(defaut)
    return data if isinstance(data, type(defaut)) else copy.deepcopy(defaut)


def ecrire(chemin, data, indent=1):
    """Ecriture atomique : le fichier est soit l'ancien, soit le nouveau,
    jamais un melange des deux."""
    chemin = Path(chemin)
    with verrou(chemin):
        config.preparer_dossiers()
        chemin.parent.mkdir(parents=True, exist_ok=True)
        tmp = chemin.with_name(chemin.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        # La copie de secours n'est faite que d'une version LISIBLE : apres
        # une ecriture interrompue, recopier le fichier abime ecraserait le
        # seul bon exemplaire qui reste.
        try:
            _charger(chemin)
            shutil.copyfile(chemin, _secours(chemin))
        except (ValueError, OSError):
            pass
        # Sous Windows, os.replace echoue si un autre processus a le fichier
        # ouvert a cet instant (un antivirus, un editeur) : on reessaie un peu.
        for essai in range(5):
            try:
                os.replace(tmp, chemin)
                return
            except PermissionError:
                if essai == 4:
                    raise
                time.sleep(0.05 * (essai + 1))
