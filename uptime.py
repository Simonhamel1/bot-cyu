#!/usr/bin/env python3
"""
Surveille le webmail CYU et previent sur Discord quand il tombe ou revient.

Script independant du reste : il ne touche ni a CELCAT ni aux devoirs, il ne
partage que l'aiguillage Discord. A lancer a cote du bot si tu veux savoir
quand la boite mail est de nouveau joignable.

    python3 uptime.py

Les recaps « tout va bien » partent dans #logs, les pannes dans #alertes avec
mention. A chaque verification, l'etat est aussi ecrit dans
donnees/webmail.json : c'est ce que lit le panneau de #statut, qui tourne dans
l'autre processus.
"""

import time
from datetime import datetime
from urllib.parse import urlsplit

import requests

import config
import notif
import statut

TIMEOUT = 15
HEADERS = {"User-Agent": "Mozilla/5.0 uptime-monitor/1.0"}

# Pages d'erreur de la federation Shibboleth. L'IdP CYU les sert avec un corps
# HTML et un code 4xx, JAMAIS une 5xx : sans ce test, une panne de SSO passe
# pour un site en ligne.
ERREURS_SSO = (
    "Unable to Respond",          # version anglaise du template Shibboleth
    "Impossible de repondre",     # version francaise, sans accents
    "Impossible de répondre",     # version francaise, avec accents
    "stale request",              # requete SAML perimee
    "Web Login Service - Message Security Error",
)

# Ce qu'on doit trouver au bout de la chaine pour dire que la boite est
# joignable : le formulaire de connexion de l'IdP. C'est le plus loin qu'on
# puisse aller sans identifiants, donc c'est ca, « le service repond ».
# Si CYU refait son template de login, ce marqueur devient faux et declenche
# une fausse alerte : c'est la ligne a mettre a jour.
ATTENDU = 'type="password"'


def check():
    """(up: bool, detail: str). UP = on atteint l'ecran de connexion.

    Ce que testait la premiere version : « le serveur repond avec un code
    < 500 ». Insuffisant ici, parce que /mail n'est pas une page mais le debut
    d'une chaine SAML :

        mail.etu.cyu.fr/mail
          -> 302  sp.partage.renater.fr/ucp        (le SP, service Partage/RENATER)
          -> 302  idp.cyu.fr/idp/profile/SAML2/... (l'IdP, qui authentifie)

    Quand la federation est cassee, l'IdP repond HTTP 400 avec sa page « Unable
    to Respond ». 400 < 500, donc l'ancien test annoncait UP alors que la boite
    mail etait totalement inaccessible. On verifie donc trois choses : le code
    HTTP, l'absence de page d'erreur Shibboleth, et la presence du formulaire.
    """
    t0 = time.monotonic()
    try:
        # Pas de stream=True : il faut lire le corps pour distinguer une vraie
        # page de connexion d'une page d'erreur de la federation.
        r = requests.get(config.WEBMAIL_URL, headers=HEADERS, timeout=TIMEOUT)
    except requests.Timeout:
        return False, f"timeout > {TIMEOUT}s"
    except requests.RequestException as e:
        return False, f"{type(e).__name__}"

    ms = (time.monotonic() - t0) * 1000
    hote = urlsplit(r.url).netloc or "?"

    if r.status_code >= 400:
        faute = next((m for m in ERREURS_SSO if m in r.text), None)
        motif = f' - "{faute}"' if faute else ""
        return False, f"HTTP {r.status_code} sur {hote}{motif} ({ms:.0f} ms)"

    faute = next((m for m in ERREURS_SSO if m in r.text), None)
    if faute:
        return False, f'SSO casse sur {hote} - "{faute}" (HTTP {r.status_code})'

    if ATTENDU not in r.text:
        return False, (f"pas de formulaire de connexion sur {hote} "
                       f"(HTTP {r.status_code}, {len(r.text)} octets)")

    return True, f"HTTP {r.status_code} sur {hote} en {ms:.0f} ms"


def main():
    up, detail = check()
    statut.enregistrer_webmail(up, detail)
    notif.simple(f"Monitoring du webmail demarre sur {config.WEBMAIL_URL}\n"
                 f"Etat initial : {'OK' if up else 'INJOIGNABLE'} ({detail})",
                 canal="logs")
    print(f"start | {'UP' if up else 'DOWN'} | {detail}", flush=True)

    precedent = up
    n = 0          # essais depuis le dernier recap
    ok = 0         # essais reussis depuis le dernier recap

    while True:
        time.sleep(config.WEBMAIL_INTERVAL)

        up, detail = check()
        # Ecrit a chaque tour, meme sans changement : le panneau de #statut
        # affiche l'age de la derniere verification, donc il a besoin de
        # l'horodatage autant que de l'etat.
        statut.enregistrer_webmail(up, detail)
        n += 1
        if up:
            ok += 1
        print(f"{datetime.now():%H:%M:%S} | {'UP' if up else 'DOWN'} | {detail}",
              flush=True)

        # changement d'etat : la seule chose qui merite une mention
        if up and not precedent:
            notif.simple(f"Webmail de retour en ligne : {config.WEBMAIL_URL} "
                         f"({detail})", ping=True, canal="alertes")
        elif not up and precedent:
            notif.simple(f"Webmail injoignable : {config.WEBMAIL_URL} ({detail})",
                         ping=True, canal="alertes")
        precedent = up

        # recap periodique, dans les logs
        if n >= config.WEBMAIL_HEARTBEAT:
            notif.simple(f"Monitoring webmail toujours actif. {ok}/{n} essais OK "
                         f"sur la derniere periode ({detail})", canal="logs")
            n = 0
            ok = 0


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[i] arret.", flush=True)
