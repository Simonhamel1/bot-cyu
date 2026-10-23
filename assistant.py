#!/usr/bin/env python3
"""
L'assistant : la ligne de commande, et le daemon qui envoie tout sur Discord.

    python assistant.py config         verifier config.yaml, sans rien envoyer
    python assistant.py salons         cree les salons Discord (une seule fois)
    python assistant.py init           un message de test dans chaque salon
    python assistant.py aujourdhui     la journee, dans le terminal
    python assistant.py demain
    python assistant.py semaine
    python assistant.py prochain       le prochain cours, et dans combien de temps
    python assistant.py libre          les creneaux libres a venir
    python assistant.py image --jusqu-au 12/10     l'emploi du temps en PNG
    python assistant.py photo --jour 12/10         une journee en PNG
    python assistant.py actu --jours 7             ce qui a bouge, en PNG
    python assistant.py tableaux       reecrire #statut et #edt tout de suite
    python assistant.py devoir add "DM 2" --matiere maths --pour prochain:maths
    python assistant.py devoir list
    python assistant.py devoir fait 3
    python assistant.py devoir suppr 3
    python assistant.py ics            export .ics (cours + devoirs)
    python assistant.py daemon         la boucle de fond

Options : --discord pour envoyer au lieu d'afficher, --hors-ligne pour lire le
cache sans se connecter a CELCAT.

Ce que le daemon envoie, et ou :

    #annonces   briefing du matin, briefing du soir, recap du dimanche
    #devoirs    echeances, relance de fin de journee
    #alertes    cours deplace ou annule qui touche aujourd'hui ou demain
    #edt        le tableau de la semaine, reecrit sur place
    #statut     le panneau d'etat, reecrit sur place
    #logs       demarrages, erreurs, retablissements

Il n'envoie PLUS de rappel avant chaque cours : c'etait du bruit. Les deux
briefings disent tout. Si tu en veux quand meme, remplis
`notifications.avant_cours_minutes` dans config.yaml.

En pratique tu lanceras plutot bot.py, qui fait tourner ce daemon ET repond aux
commandes Discord dans le meme processus.

Aussi dans ce fichier :

    La surveillance du webmail                    (ex-uptime.py)
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

import requests

import affichage
import config
import edt
import image
import promo


# Certains terminaux servent un stdout en cp1252 : sans ca, un simple print
# d'un intitule accentue fait planter le script.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass


# --- Envoi -------------------------------------------------------------------
def envoyer(titre, lignes, couleur="info", ping=False, canal="logs"):
    """Un message Discord. Ne leve jamais : un souci reseau ne doit pas tuer un
    daemon qui tourne depuis trois semaines."""
    return affichage.envoyer(titre, lignes, couleur=couleur, ping=ping, canal=canal)


def envoyer_photo(titre, lignes, dessin, couleur="cours", ping=False,
                  canal="annonces", secours=None):
    """Le message avec sa photo, ou le message tout court si ca echoue.

    `dessin` est une fonction sans argument qui rend le chemin du PNG. On
    l'appelle ici, a l'interieur du filet : une police manquante ou un disque
    plein ne doit jamais faire sauter un briefing.

    `lignes` accompagne la photo, `secours` la remplace quand il n'y a pas de
    photo — la legende d'une image et un message autonome ne disent pas la
    meme chose.
    """
    if config.IMAGES and image.DISPONIBLE:
        try:
            chemin = dessin()
            if affichage.envoyer_image(chemin, titre, lignes, canal=canal,
                                       couleur=couleur, ping=ping):
                return True
            print("[!] photo non publiee, envoi du texte seul", flush=True)
        except Exception as e:                      # noqa: BLE001 - filet volontaire
            print(f"[!] dessin impossible ({type(e).__name__}: {e}), "
                  f"envoi du texte seul", flush=True)
    return envoyer(titre, secours if secours is not None else lignes,
                   couleur=couleur, ping=ping, canal=canal)


def sortir(titre, lignes, vers_discord, couleur="info", canal="annonces"):
    """Meme contenu vers Discord ou vers le terminal, selon --discord."""
    if vers_discord:
        ok = envoyer(titre, lignes, couleur=couleur, canal=canal)
        print("envoye." if ok else "echec de l'envoi.", flush=True)
    else:
        print(f"\n=== {affichage.sans_markdown(titre)} ===")
        print(affichage.sans_markdown("\n".join(lignes)) + "\n")


# --- Etat du daemon (anti-doublon) -------------------------------------------
def lire_etat():
    return config.lire_json(config.FICHIER_ETAT, {})


def deja_envoye(etat, cle):
    return cle in (etat.get("envoyes") or {})


def marquer(etat, cle):
    """Memorise un envoi et purge ce qui date de plus de 30 jours, pour que le
    fichier ne gonfle pas indefiniment."""
    envoyes = etat.setdefault("envoyes", {})
    envoyes[cle] = datetime.now().isoformat(timespec="seconds")
    limite = (datetime.now() - timedelta(days=30)).isoformat()
    etat["envoyes"] = {k: v for k, v in envoyes.items() if v >= limite}
    config.ecrire_json(config.FICHIER_ETAT, etat)


def _du(moment, maintenant, fenetre_min=5):
    """Vrai si `moment` vient de passer. La fenetre evite qu'un daemon relance
    a 20 h envoie le briefing de 7 h du matin."""
    ecart = (maintenant - moment).total_seconds() / 60
    return 0 <= ecart < fenetre_min


def en_silence(maintenant=None):
    """Sommes-nous dans les heures de silence de config.yaml ?

    Seules les alertes urgentes passent outre. La plage traverse minuit
    (23:00 -> 06:30), d'ou la comparaison en deux morceaux."""
    if not config.SILENCE_DE or not config.SILENCE_A:
        return False
    maintenant = maintenant or datetime.now()
    debut = affichage.hhmm(config.SILENCE_DE, (23, 0))
    fin = affichage.hhmm(config.SILENCE_A, (6, 30))
    courant = (maintenant.hour, maintenant.minute)
    if debut <= fin:
        return debut <= courant < fin
    return courant >= debut or courant < fin


# --- Les changements d'emploi du temps ---------------------------------------
# Un soupcon de donnees tronquees se repete a chaque cycle tant que CELCAT
# n'est pas revenu a la normale : on ne le signale qu'une fois par heure.
_DERNIER_SOUPCON = [None, None]


def signaler_soupcon(message):
    precedent, quand = _DERNIER_SOUPCON
    if precedent == message and quand and \
            (datetime.now() - quand).total_seconds() < 3600:
        return
    _DERNIER_SOUPCON[0], _DERNIER_SOUPCON[1] = message, datetime.now()
    print(f"[!] {message}", flush=True)
    envoyer("Lecture CELCAT douteuse",
            [message,
             "**Rien n'a été annoncé** : l'ancienne référence est conservée. "
             "Si l'emploi du temps a vraiment été vidé, ce sera confirmé à la "
             "troisième lecture identique."],
            couleur="devoir", canal="logs")


def resume_changements(liste):
    """La legende sous la photo : le compte par type, pas un doublon de l'image."""
    lignes = []
    for type_ in edt.ORDRE:
        combien = sum(1 for ch in liste if ch.type == type_)
        if not combien:
            continue
        icone = edt.ENTETES[type_][0]
        lignes.append(f"{icone} **{combien}** {edt.libelle_changements(type_, combien).lower()}")
    jours = sorted({j for ch in liste for j in ch.jours})
    if jours:
        lignes.append("")
        lignes.append("-# Concerne : " + ", ".join(
            affichage.jour_fr(j, court=True) for j in jours[:6])
            + (f" et {len(jours) - 6} autres jours" if len(jours) > 6 else ""))
    return lignes


def publier_actu(rapport, cours, aujourd=None, demarrage=False):
    """Annonce ce que la derniere lecture a revele. Rend ce qui a ete annonce.

    Trois cas, et un seul message par cas : l'emploi du temps sort pour la
    premiere fois, des cours ont bouge, ou les donnees sont douteuses — auquel
    cas on ne dit rien de faux, on dit qu'on doute.
    """
    aujourd = aujourd or date.today()

    if rapport.suspect:
        signaler_soupcon(rapport.suspect)
        return 0

    if rapport.publication:
        sortie = rapport.publication
        fin = min(sortie["au"], sortie["du"] + timedelta(days=41))
        envoyer_photo(
            "📢 L'emploi du temps est sorti",
            [f"**{len(sortie['cours'])} cours** publiés, du "
             f"{affichage.jour_fr(sortie['du'])} au {affichage.jour_fr(sortie['au'])}."],
            lambda: image.rendre(cours, sortie["du"], fin,
                                 config.DONNEES / "publication.png",
                                 titre="L'emploi du temps est sorti"),
            couleur="cours", ping=True, canal="annonces",
            secours=[f"**{len(sortie['cours'])} cours** publiés, du "
                     f"{affichage.jour_fr(sortie['du'])} au {affichage.jour_fr(sortie['au'])}.", ""]
                    + affichage.bloc_semaine(cours, edt.semaine_de(sortie["du"])))
        print(f"{datetime.now():%H:%M} | publication : {len(sortie['cours'])} cours",
              flush=True)
        return len(sortie["cours"])

    liste = rapport.changements
    if not liste:
        return 0

    urgent = bool(rapport.urgents)
    # Etre prevenu quand un cours bouge, c'est tout l'interet du bot : par
    # defaut on mentionne a chaque changement, pas seulement pour demain.
    ping = urgent or config.PING_CHANGEMENTS
    # Sauf les jours qu'on a exclus (le week-end, typiquement) : le changement
    # est alors publie dans #edt sans mention. On ne supprime jamais
    # l'information — un cours annule reste annonce, il ne fait que sonner un
    # jour ou l'autre.
    if config.JOURS_ALERTES and aujourd.weekday() not in config.JOURS_ALERTES:
        ping = False
    titre = edt.titre(liste)
    legende = resume_changements(liste)
    if demarrage or rapport.pendant_absence:
        legende = ["-# Détecté au redémarrage : ça a bougé pendant que "
                   "l'assistant était éteint.", ""] + legende
    texte, _ = edt.bloc_changements(liste, aujourd)

    envoyer_photo(
        titre, legende,
        lambda: image.rendre_changements(liste, config.DONNEES / "changements.png",
                                         titre=titre,
                                         sous_titre="détecté "
                                                    f"{datetime.now():le %d/%m à %H:%M}"),
        couleur=edt.couleur(liste, aujourd), ping=ping,
        canal="alertes" if ping else "edt", secours=texte)
    print(f"{datetime.now():%H:%M} | {len(liste)} changements EDT annonces",
          flush=True)
    return len(liste)


# --- Daemon ------------------------------------------------------------------
def cours_a_rappeler(jc):
    """Les cours qui meritent un rappel : tous, ou seulement les premiers d'un
    bloc qui s'enchaine, sinon on est notifie a chaque intercours."""
    if not config.SEULEMENT_PREMIER_COURS_DU_BLOC:
        return jc
    garde = []
    for i, c in enumerate(jc):
        veille = jc[i - 1] if i else None
        colle = veille and veille.fin and (c.debut - veille.fin).total_seconds() / 60 <= 20
        if not colle:
            garde.append(c)
    return garde


def ligne_meteo(jc, jour):
    """La meteo du trajet, en une ligne, pour la LEGENDE d'une photo.

    Une legende part dans la notification Discord, la photo non : si la
    journee commence sous la pluie, ca doit etre ecrit, pas seulement dessine.
    Liste vide quand il n'y a rien a dire — pas de reseau, meteo coupee, jour
    hors prevision, ou simplement un temps sans consequence.
    """
    if not (config.METEO_ACTIVE and config.METEO_BRIEFING) or not jc:
        return []
    try:
        premier = jc[0]
        resume = edt.jour(jour)
        if premier.a_distance:
            creneaux = edt.fenetre(premier.debut, premier.fin or premier.debut)
        else:
            _, depart = affichage.heure_lever(premier)
            creneaux = edt.fenetre(depart, premier.debut)
        gene = edt.pire(creneaux)
    except Exception as e:                      # noqa: BLE001 - filet volontaire
        print(f"[!] meteo ignoree dans la legende : {e}", flush=True)
        return []

    if resume is None and gene is None:
        return []
    if gene is not None and gene.mouille:
        phrase = edt.conseil(creneaux)
        return [f"{gene.emoji} **{gene.detail()}** au départ"
                + (f" — {phrase}." if phrase else ".")]
    if resume is not None:
        return [f"{resume.emoji} {resume.detail()}"]
    return [f"{gene.emoji} {gene.detail()} au départ"]


def legende_jour(cours, liste_devoirs, jour):
    """La legende sous la photo d'une journee.

    La photo dit deja tout ce qui est visuel ; la legende ne garde que ce qui
    doit apparaitre dans la NOTIFICATION Discord, ou l'image n'est pas
    visible : l'amplitude, les salles, et ce qu'il faut rendre.
    """
    jc = edt.du_jour(cours, jour)
    if not jc:
        autres = edt.non_cours_du_jour(cours, jour)
        return [f"Aucun cours{f' — {autres[0].titre}' if autres else ''}. 🎉"]

    fin = jc[-1].fin or jc[-1].debut
    lignes = [f"`{jc[0].debut:%H:%M}` → `{fin:%H:%M}` · {len(jc)} cours · "
              + ", ".join(dict.fromkeys(c.titre[:24] for c in jc))]
    salles = {c.ou for c in jc if not c.a_distance}
    if len(salles) == 1 and salles != {"salle inconnue"}:
        lignes.append(f"📍 Tout se passe en **{salles.pop()}**.")

    lignes += ligne_meteo(jc, jour)

    a_rendre = [d for d in promo.actifs(liste_devoirs)
                if (r := promo.jours_restants(d)) is not None and r <= 1]
    if a_rendre:
        lignes += ["", "📌 **À rendre**"] + \
                  [affichage.ligne_devoir(d, avec_id=False) for d in a_rendre]
    return lignes


def _corps_briefing_matin(cours, liste_devoirs, aujourd, maintenant):
    corps = affichage.bloc_journee(cours, liste_devoirs, aujourd, maintenant=maintenant)
    corps = _avec_meteo(corps, edt.du_jour(cours, aujourd), aujourd)
    urgents = promo.actifs(liste_devoirs, 0)
    if urgents:
        corps += ["", "🔥 **A rendre aujourd'hui**"] + \
                 [affichage.ligne_devoir(d) for d in urgents]
    demain = promo.actifs(liste_devoirs, 1)
    demain = [d for d in demain if d not in urgents]
    if demain:
        corps += ["", "⚠️ **Pour demain**"] + \
                 [affichage.ligne_devoir(d, avec_id=False) for d in demain]
    return corps, urgents


def _avec_meteo(corps, jc, jour):
    """La meteo en tete du briefing en texte. En tete, et pas en bas : c'est ce
    qui decide de ce qu'on met sur le dos avant de lire le reste."""
    lignes = ligne_meteo(jc, jour)
    return (lignes + [""] + corps) if lignes else corps


def _corps_briefing_soir(cours, liste_devoirs, demain):
    corps = affichage.bloc_journee(cours, liste_devoirs, demain, avec_reveil=True)
    corps = _avec_meteo(corps, edt.du_jour(cours, demain), demain)
    seuils = set(config.DEVOIRS_JOURS_AVANT)
    urgents = [d for d in promo.actifs(liste_devoirs)
               if (r := promo.jours_restants(d)) is not None
               and (r in seuils or r < 0)]
    if urgents:
        corps += ["", "⏳ **Echeances qui approchent**"] + \
                 [affichage.ligne_devoir(d) for d in urgents]
    return corps, urgents


def daemon():
    print("[i] demarrage du daemon", flush=True)
    demarrage = datetime.now()
    session = None
    try:
        session = edt.Session()
    except (edt.EchecConnexion, requests.RequestException) as e:
        print(f"[!] connexion CELCAT impossible au demarrage ({e}), "
              f"on demarre sur le cache", flush=True)

    etat = lire_etat()
    cours, origine = edt.charger(hors_ligne=session is None, session=session)
    liste_devoirs = promo.lire_devoirs()

    # La reference des changements vit sur le disque : ce qui a bouge pendant
    # que le bot etait eteint sera annonce a la premiere lecture, au lieu
    # d'etre avale en silence.
    suivi = edt.Suivi()
    if origine == "celcat":
        publier_actu(suivi.observer(cours), cours, date.today(), demarrage=True)

    rappels = (", ".join(f"{m} min" for m in config.AVANT_COURS_MINUTES)
               if config.AVANT_COURS_MINUTES else "aucun")
    envoyer("Assistant demarre",
            [f"Source : **{origine}** · "
             f"{len([c for c in cours if c.est_cours])} cours sur "
             f"{config.HORIZON_JOURS} jours.",
             f"Briefings **{config.BRIEFING_MATIN}** et **{config.BRIEFING_SOIR}** · "
             f"rappels avant cours : {rappels}.",
             f"Verification de l'emploi du temps toutes les "
             f"{config.VERIF_EDT_MINUTES} min."],
            couleur="calme", canal="logs")

    # Les deux tableaux vivants sont ecrits tout de suite : au redemarrage, ils
    # doivent redevenir justes sans attendre le prochain cycle.
    affichage.publier_tableau_edt(cours, liste_devoirs)
    affichage.publier(cours, liste_devoirs, demarrage)

    derniere_verif = datetime.now()
    dernier_tableau = datetime.now()
    echecs = 0

    while True:
        time.sleep(30)
        maintenant = datetime.now()
        aujourd = maintenant.date()
        demain = aujourd + timedelta(days=1)
        liste_devoirs = promo.lire_devoirs()

        # 1. Rafraichir l'emploi du temps et signaler les changements.
        if (maintenant - derniere_verif).total_seconds() >= config.VERIF_EDT_MINUTES * 60:
            derniere_verif = maintenant
            try:
                if session is None:
                    session = edt.Session()
                cours, origine = edt.charger(session=session)
                # charger() se rabat sur le cache quand CELCAT ne repond pas :
                # comparer le cache a lui-meme ne dirait rien, et pourrait
                # faire passer une lecture partielle pour un changement.
                if origine == "celcat":
                    publier_actu(suivi.observer(cours), cours, aujourd)

                if echecs >= 3:
                    envoyer("Assistant de nouveau operationnel",
                            "La connexion a CELCAT est retablie.",
                            couleur="calme", canal="logs")
                echecs = 0
            except (edt.EchecConnexion, requests.RequestException) as e:
                echecs += 1
                session = None
                print(f"{maintenant:%H:%M} | verif KO ({echecs}) : {e}", flush=True)
                if echecs == 3:          # une seule alerte, pas une par tour
                    envoyer("Assistant en panne de CELCAT",
                            [f"3 echecs d'affilee : `{e}`",
                             "Les briefings continuent sur le dernier emploi du "
                             "temps connu."],
                            couleur="alerte", ping=True, canal="alertes")

        # 2. Reecrire les deux tableaux vivants. Ni l'un ni l'autre ne notifie :
        #    ce sont des modifications de message, pas des envois.
        if (maintenant - dernier_tableau).total_seconds() >= \
                config.RAFRAICHIR_TABLEAUX_MINUTES * 60:
            dernier_tableau = maintenant
            affichage.publier(cours, liste_devoirs, demarrage, echecs)
            affichage.publier_tableau_edt(cours, liste_devoirs)
            # Le poids des semaines a venir est retenu au passage : c'est la
            # seule occasion de le faire, CELCAT ne rend jamais le passe.
            try:
                edt.archiver_horizon(cours, aujourd)
            except Exception as e:              # noqa: BLE001 - filet volontaire
                print(f"[!] archivage des semaines ignore : {e}", flush=True)

        jc = edt.du_jour(cours, aujourd)

        # 3. Briefing du matin. Les jours de la semaine ou il part se reglent
        #    dans config.yaml (briefing_matin_jours) ; vide = tous les jours.
        cle = f"matin:{aujourd}"
        if not deja_envoye(etat, cle) and \
                (not config.BRIEFING_MATIN_JOURS or
                 aujourd.weekday() in config.BRIEFING_MATIN_JOURS) and \
                (jc or not config.BRIEFING_SI_COURS) and \
                _du(affichage.a_heure(aujourd, config.BRIEFING_MATIN, (7, 0)), maintenant):
            corps, urgents = _corps_briefing_matin(cours, liste_devoirs, aujourd,
                                                   maintenant)
            envoyer_photo(
                f"☀️ {affichage.jour_fr(aujourd).capitalize()}",
                legende_jour(cours, liste_devoirs, aujourd),
                lambda: image.rendre_jour(cours, liste_devoirs, aujourd,
                                          config.DONNEES / "briefing-matin.png"),
                couleur="cours", ping=bool(urgents), canal="annonces",
                secours=corps)
            marquer(etat, cle)

        # 4. Rappels avant les cours. Desactives par defaut : voir config.yaml.
        for c in cours_a_rappeler(jc):
            for minutes in config.AVANT_COURS_MINUTES:
                cle = f"rappel:{c.debut.isoformat()}:{minutes}"
                if deja_envoye(etat, cle):
                    continue
                if _du(c.debut - timedelta(minutes=minutes), maintenant, fenetre_min=2):
                    liees = promo.du_cours(c, liste_devoirs)
                    corps = [c.ligne()]
                    if liees:
                        corps += ["", "📌 **A rendre pour ce cours**"] + \
                                 [affichage.ligne_devoir(d, avec_id=False) for d in liees]
                    envoyer(f"Dans {minutes} min : {c.titre}", corps,
                            couleur="cours", ping=bool(liees), canal="annonces")
                    marquer(etat, cle)

        # 5. Le premier cours de la journee, et lui seul : le seul rappel qui
        #    sert vraiment, celui qui te fait sortir de chez toi.
        if config.PREMIER_COURS_MINUTES and jc:
            premier = jc[0]
            cle = f"premier:{premier.debut.isoformat()}"
            moment = premier.debut - timedelta(minutes=config.PREMIER_COURS_MINUTES)
            if not deja_envoye(etat, cle) and _du(moment, maintenant, fenetre_min=3):
                _, depart = affichage.heure_lever(premier)
                envoyer(f"🚶 Premier cours dans {config.PREMIER_COURS_MINUTES} min",
                        [premier.ligne(),
                         "" if premier.a_distance else
                         f"Depart conseille : **{depart:%H:%M}**."],
                        couleur="cours", canal="annonces")
                marquer(etat, cle)

        # 6. Relance de fin de journee : « des devoirs a noter ? »
        #    A heure fixe si relance_devoirs_heure est rempli, sinon calee sur
        #    la fin du dernier cours.
        if config.RELANCE_DEVOIRS and jc and not en_silence(maintenant):
            cle = f"relance:{aujourd}"
            dernier = max((c.fin or c.debut) for c in jc)
            if config.RELANCE_DEVOIRS_HEURE:
                moment = affichage.a_heure(aujourd, config.RELANCE_DEVOIRS_HEURE, (18, 0))
            else:
                moment = dernier + timedelta(
                    minutes=config.RELANCE_DEVOIRS_APRES_MINUTES)
            if not deja_envoye(etat, cle) and _du(moment, maintenant, fenetre_min=10):
                # A heure fixe, un cours peut encore etre en cours : on ne
                # raconte pas que la journee est finie si elle ne l'est pas.
                entete = ("Journee finie. Tu as eu :" if dernier <= maintenant
                          else "Au programme aujourd'hui :")
                envoyer("📝 Des devoirs a noter ?",
                        [entete,
                         *[f"• {c.titre}" for c in jc],
                         "", "Tape `/devoir` pour en ajouter un."],
                        couleur="devoir", canal="devoirs")
                marquer(etat, cle)

        # 7. Briefing du soir : demain, et les echeances qui arrivent.
        cle = f"soir:{aujourd}"
        # « demain a-t-il des cours ? » decide de l'envoi quand
        # briefing_si_cours est mis : c'est ce qui fait taire le vendredi et le
        # samedi soir, sans toucher au dimanche soir, le seul a annoncer lundi.
        if not deja_envoye(etat, cle) and \
                (not config.BRIEFING_SOIR_JOURS or
                 aujourd.weekday() in config.BRIEFING_SOIR_JOURS) and \
                (edt.du_jour(cours, demain) or not config.BRIEFING_SI_COURS) and \
                _du(affichage.a_heure(aujourd, config.BRIEFING_SOIR, (20, 0)), maintenant):
            corps, urgents = _corps_briefing_soir(cours, liste_devoirs, demain)
            envoyer_photo(
                f"🌙 Demain — {affichage.jour_fr(demain)}",
                legende_jour(cours, liste_devoirs, demain),
                lambda: image.rendre_jour(cours, liste_devoirs, demain,
                                          config.DONNEES / "briefing-soir.png"),
                couleur="info",
                ping=any((r := promo.jours_restants(d)) is not None and r <= 1
                         for d in urgents),
                canal="annonces", secours=corps)
            marquer(etat, cle)

        # 8. Recap de la semaine.
        cle = f"semaine:{aujourd}"
        if maintenant.weekday() == config.RECAP_SEMAINE_JOUR and \
                not deja_envoye(etat, cle) and \
                _du(affichage.a_heure(aujourd, config.RECAP_SEMAINE_HEURE, (18, 0)), maintenant):
            lundi = aujourd + timedelta(days=(7 - aujourd.weekday()) % 7 or 7)
            corps = affichage.bloc_semaine(cours, lundi) + [""] + \
                affichage.bloc_devoirs(liste_devoirs, "📚 A faire cette semaine", horizon=7)
            envoyer_photo(
                f"🗓️ Semaine du {lundi:%d/%m}",
                affichage.bloc_devoirs(liste_devoirs, "📚 À faire cette semaine",
                                       horizon=7),
                lambda: image.rendre(cours, lundi, lundi + timedelta(days=6),
                                     config.DONNEES / "recap-semaine.png",
                                     titre=f"Semaine du {lundi:%d/%m}"),
                couleur="info", canal="annonces", secours=corps)

            # La semaine en chiffres, juste apres la grille : l'une dit quand,
            # l'autre dit combien. Les deux ensemble diraient trop.
            semaine_a_venir = edt.semaine(cours, lundi)
            if not semaine_a_venir.vide:
                envoyer_photo(
                    "📊 Ce que pèse la semaine",
                    [f"**{affichage.duree_fr(semaine_a_venir.total_minutes)}** de cours, "
                     f"{semaine_a_venir.seances} séances, "
                     f"{affichage.duree_fr(semaine_a_venir.trous_minutes)} de trous."],
                    lambda: image.rendre_stats(semaine_a_venir,
                                               config.DONNEES / "recap-stats.png",
                                               cours=cours),
                    couleur="info", canal="annonces",
                    secours=edt.bloc_stats(semaine_a_venir, cours))
            marquer(etat, cle)


# --- Creation des salons -----------------------------------------------------
def creer_salons(categorie=""):
    """Le bot cree les salons dans la categorie et ecrit leurs
    identifiants dans config.yaml. Idempotent."""
    try:
        guilde, salons = affichage.creer_salons(categorie)
    except affichage.EchecDiscord as e:
        print(f"[X] {e}")
        sys.exit(1)

    for canal, (ident, etat) in salons.items():
        print(f"  {etat:10s} #{affichage.SALONS[canal][0]:16s} {ident}  ({canal})")

    try:
        change = config.ecrire_salons({c: i for c, (i, _) in salons.items()},
                                      serveur=guilde,
                                      categorie=str(categorie or config.CATEGORIE_ID))
    except config.ConfigInvalide as e:
        print(f"\n[!] {e}")
        return

    print(f"\nServeur {guilde}. {len(change)} valeurs ecrites dans config.yaml.\n"
          f"Verifie avec : python assistant.py init")


def tester_salons():
    """Un message par salon : le seul moyen de verifier que chaque aiguillage
    tombe vraiment ou tu crois."""
    for canal in config.CANAUX:
        if canal in config.CANAUX_BOT_SEULEMENT and not config.salon_configure_bot(canal):
            print(f"-     {canal:10s} -> pas de salon dedie (facultatif)")
            continue
        sujet = affichage.SALONS[canal][1]
        mode, cible = affichage.destination(canal)
        ok = envoyer(f"Test — #{affichage.SALONS[canal][0]}",
                     [sujet + ".",
                      f"Ce salon recevra les messages de type `{canal}`."],
                     couleur="calme", canal=canal)
        visee = cible if mode == "bot" else "webhook ..." + str(cible)[-12:]
        marque = "" if affichage.salon_configure(canal) else "  (vide -> secours)"
        print(f"{'OK   ' if ok else 'ECHEC'} {canal:10s} -> {mode} {visee}{marque}")


# --- Ligne de commande -------------------------------------------------------
def construire_parseur():
    p = argparse.ArgumentParser(
        description="Assistant emploi du temps + devoirs CYU.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sous = p.add_subparsers(dest="commande")

    sous.add_parser("config", help="verifier config.yaml sans rien envoyer")
    sous.add_parser("heure", help="le bot est-il bien a l'heure francaise ?")
    sous.add_parser("uptime", help="surveiller le webmail (a lancer a part, en continu)")

    sp = sous.add_parser("salons", help="cree les salons Discord dans la categorie")
    sp.add_argument("--categorie", default="",
                    help="identifiant de la categorie (sinon celui de config.yaml)")

    for nom, aide in (("init", "un message de test dans chaque salon"),
                      ("aujourdhui", "la journee du jour"),
                      ("demain", "la journee de demain"),
                      ("semaine", "la semaine en cours"),
                      ("prochain", "le prochain cours"),
                      ("libre", "les creneaux libres"),
                      ("tableaux", "reecrire #statut et #edt maintenant"),
                      ("daemon", "boucle de fond : briefings, alertes, tableaux")):
        sp = sous.add_parser(nom, help=aide)
        sp.add_argument("--discord", action="store_true",
                        help="envoyer sur Discord au lieu d'afficher")
        sp.add_argument("--hors-ligne", dest="hors_ligne", action="store_true",
                        help="lire le cache sans se connecter a CELCAT")

    sp = sous.add_parser("image", help="dessiner l'emploi du temps en PNG")
    sp.add_argument("--jusqu-au", dest="jusqu_au", default="",
                    help="12/10 · +21 · vendredi · demain (dans 6 jours par defaut)")
    sp.add_argument("--a-partir-de", dest="depuis", default="",
                    help="aujourd'hui par defaut")
    sp.add_argument("--sortie", default=str(config.RACINE / "edt.png"))
    sp.add_argument("--discord", action="store_true",
                    help="poster l'image dans #annonces au lieu de l'ecrire ici")
    sp.add_argument("--hors-ligne", dest="hors_ligne", action="store_true")

    sp = sous.add_parser("photo", help="dessiner UNE journee en PNG")
    sp.add_argument("--jour", default="",
                    help="12/10 · lundi · demain (aujourd'hui par defaut)")
    sp.add_argument("--sortie", default=str(config.RACINE / "jour.png"))
    sp.add_argument("--discord", action="store_true",
                    help="poster l'image dans #annonces")
    sp.add_argument("--hors-ligne", dest="hors_ligne", action="store_true")

    sp = sous.add_parser("stats", help="ce que pese une semaine, en PNG")
    sp.add_argument("--semaine", type=int, default=0,
                    help="0 = cette semaine, 1 = la prochaine, -1 = la passee")
    sp.add_argument("--tout", action="store_true",
                    help="tout l'emploi du temps connu, matiere par matiere")
    sp.add_argument("--sortie", default=str(config.RACINE / "stats.png"))
    sp.add_argument("--discord", action="store_true",
                    help="poster l'image dans #annonces")
    sp.add_argument("--hors-ligne", dest="hors_ligne", action="store_true")

    sp = sous.add_parser("meteo", help="le temps qu'il fera, et ton trajet")
    sp.add_argument("--jours", type=int, default=0,
                    help="0 = aujourd'hui, 1 = demain (3 au maximum)")
    sp.add_argument("--discord", action="store_true")
    sp.add_argument("--hors-ligne", dest="hors_ligne", action="store_true")

    sp = sous.add_parser("actu", help="ce qui a change dans l'emploi du temps")
    sp.add_argument("--jours", type=int, default=7,
                    help="sur combien de jours regarder en arriere")
    sp.add_argument("--sortie", default=str(config.RACINE / "changements.png"))
    sp.add_argument("--discord", action="store_true",
                    help="poster l'image dans #alertes")

    sp = sous.add_parser("ics", help="exporter un fichier .ics")
    sp.add_argument("--sortie", default=str(config.RACINE / "cyu.ics"))
    sp.add_argument("--hors-ligne", dest="hors_ligne", action="store_true")

    dev = sous.add_parser("devoir", help="carnet de devoirs").add_subparsers(dest="action")

    a = dev.add_parser("add", help="ajouter un devoir")
    a.add_argument("titre")
    a.add_argument("--matiere", "-m", default="")
    a.add_argument("--pour", "-p", default="",
                   help="12/09 · demain · lundi · +3 · prochain:maths")
    a.add_argument("--type", "-t", dest="type_", default="devoir",
                   help="devoir · dm · projet · revision · examen")
    a.add_argument("--note", "-n", default="")
    a.add_argument("--hors-ligne", dest="hors_ligne", action="store_true")

    l = dev.add_parser("list", help="lister les devoirs")
    l.add_argument("--tout", action="store_true", help="inclure ceux deja faits")

    dev.add_parser("fait", help="marquer comme fait").add_argument("id")
    dev.add_parser("suppr", help="supprimer").add_argument("id")

    return p


def commande_devoir(args):
    action = getattr(args, "action", None)
    if action == "add":
        cours, _ = edt.charger(hors_ligne=getattr(args, "hors_ligne", False))
        try:
            echeance = promo.resoudre_echeance(args.pour, cours, args.matiere)
        except ValueError as e:
            print(f"[X] {e}")
            sys.exit(1)
        d = promo.ajouter_devoirs(args.titre, args.matiere, echeance, args.type_, args.note)
        print("Ajoute : " + affichage.sans_markdown(affichage.ligne_devoir(d)))
    elif action == "list":
        liste = promo.lire_devoirs() if args.tout else promo.actifs()
        if not liste:
            print("Rien en attente. 😌")
        for d in sorted(liste, key=lambda d: (promo.echeance_dt(d) or datetime.max)):
            print(affichage.sans_markdown(affichage.ligne_devoir(d)))
    elif action == "fait":
        print(f"devoir #{args.id} marque comme fait." if promo.marquer_fait(args.id)
              else f"[!] aucun devoir #{args.id}")
    elif action == "suppr":
        d = promo.supprimer_devoirs(args.id)
        print(f"« {d['titre']} » supprime." if d else f"[!] aucun devoir #{args.id}")
    else:
        print("Utilise : devoir add | list | fait | suppr")


def main():
    args = construire_parseur().parse_args()
    cmd = args.commande or "aujourdhui"
    config.preparer_dossiers()
    hors_ligne = getattr(args, "hors_ligne", False)
    vers_discord = getattr(args, "discord", False)

    if cmd == "config":
        print("\n".join(config.resume()))
        trous = config.manquants()
        if trous:
            print("\n[X] a remplir dans config.yaml :")
            for t in trous:
                print(f"    - {t}")
            sys.exit(1)
        print("\nTout est rempli.")
        return
    if cmd == "heure":
        # A lancer SUR LE SERVEUR, pas sur sa machine : c'est la-bas que
        # l'horloge est en UTC sans prevenir personne.
        ok, lignes = config.controle_heure()
        print("\n".join(lignes))
        sys.exit(0 if ok else 1)
    if cmd == "salons":
        creer_salons(args.categorie)
        return
    if cmd == "uptime":
        try:
            main_uptime()
        except KeyboardInterrupt:
            print("\n[i] arret.", flush=True)
        return
    if cmd == "init":
        tester_salons()
        return
    if cmd == "daemon":
        try:
            daemon()
        except KeyboardInterrupt:
            print("\n[i] arret.", flush=True)
        return
    if cmd == "devoir":
        commande_devoir(args)
        return

    cours, origine = edt.charger(hors_ligne=hors_ligne)
    liste_devoirs = promo.lire_devoirs()

    if cmd == "ics":
        vrais = [c for c in cours if c.est_cours]
        chemin = affichage.exporter_ics(vrais, liste_devoirs, args.sortie)
        print(f"Ecrit : {chemin} ({len(vrais)} cours, "
              f"{len(promo.actifs(liste_devoirs))} devoirs) — source : {origine}")
    elif cmd == "image":
        try:
            debut = affichage.lire_date(args.depuis, cours, date.today())
            fin = affichage.lire_date(args.jusqu_au, cours, debut + timedelta(days=6))
        except ValueError as e:
            print(f"[X] {e}")
            sys.exit(1)
        try:
            chemin = image.rendre(cours, debut, fin, args.sortie)
        except image.PillowManquant as e:
            print(f"[X] {e}")
            sys.exit(1)
        if args.discord:
            ok = affichage.envoyer_image(
                chemin, f"Emploi du temps — {affichage.jour_fr(debut, court=True)} "
                        f"au {affichage.jour_fr(fin, court=True)}", "", canal="annonces")
            print("envoye." if ok else "echec de l'envoi.")
        else:
            print(f"Ecrit : {chemin}  ({affichage.jour_fr(debut)} -> {affichage.jour_fr(fin)})")
    elif cmd == "photo":
        try:
            jour = affichage.lire_date(args.jour, cours, date.today())
        except ValueError as e:
            print(f"[X] {e}")
            sys.exit(1)
        try:
            chemin = image.rendre_jour(cours, liste_devoirs, jour, args.sortie)
        except image.PillowManquant as e:
            print(f"[X] {e}")
            sys.exit(1)
        if args.discord:
            ok = affichage.envoyer_image(chemin, affichage.jour_relatif(jour).capitalize(),
                                         legende_jour(cours, liste_devoirs, jour),
                                         canal="annonces")
            print("envoye." if ok else "echec de l'envoi.")
        else:
            print(f"Ecrit : {chemin}  ({affichage.jour_fr(jour)})")
    elif cmd == "actu":
        liste = edt.historique_actu(args.jours)
        if not liste:
            dernier = edt.quand_dernier()
            print(f"Rien n'a bouge depuis {args.jours} jours."
                  + (f" Dernier changement : {dernier:%d/%m a %H:%M}." if dernier else ""))
            return
        if args.discord:
            titre = edt.titre(liste)
            envoyer_photo(titre, resume_changements(liste),
                          lambda: image.rendre_changements(liste, args.sortie,
                                                           titre=titre),
                          couleur=edt.couleur(liste), canal="alertes",
                          ping=config.PING_CHANGEMENTS,
                          secours=edt.bloc_changements(liste)[0])
            print("envoye.")
            return
        lignes, _urgent = edt.bloc_changements(liste)
        print(f"\n=== {affichage.sans_markdown(edt.titre(liste))} ===")
        print(affichage.sans_markdown("\n".join(lignes)) + "\n")
        try:
            print(f"Image : {image.rendre_changements(liste, args.sortie)}")
        except image.PillowManquant:
            pass
    elif cmd == "stats" and args.tout:
        b = edt.bilan(cours)
        lignes = edt.bloc_bilan(b)
        if args.discord:
            envoyer_photo("📚 Tout l'emploi du temps", lignes[:1],
                          lambda: image.rendre_bilan(b, args.sortie, cours=cours),
                          couleur="info", canal="annonces", secours=lignes)
            print("envoye.")
            return
        print("\n=== Tout l'emploi du temps ===")
        print(affichage.sans_markdown("\n".join(lignes)) + "\n")
        try:
            print(f"Image : {image.rendre_bilan(b, args.sortie, cours=cours)}")
        except image.PillowManquant:
            pass
    elif cmd == "stats":
        lundi = edt.semaine_de(date.today()) + timedelta(days=7 * args.semaine)
        semaine_ = edt.semaine(cours, lundi)
        note = edt.avertissement(semaine_, cours)
        if semaine_.vide:
            print(f"Aucun cours connu pour la semaine du {lundi:%d/%m}.")
            return
        lignes = edt.bloc_stats(semaine_, cours)
        if args.discord:
            envoyer_photo(f"📊 Ta semaine — {semaine_.libelle}", lignes[:1],
                          lambda: image.rendre_stats(semaine_, args.sortie,
                                                     cours=cours, avertissement=note),
                          couleur="info", canal="annonces", secours=lignes)
            print("envoye.")
            return
        if note:
            print(f"[!] {note}")
        print(f"\n=== Ta semaine — {semaine_.libelle} ===")
        print(affichage.sans_markdown("\n".join(lignes)) + "\n")
        try:
            print(f"Image : {image.rendre_stats(semaine_, args.sortie, cours=cours, avertissement=note)}")
        except image.PillowManquant:
            pass
    elif cmd == "meteo":
        jour_ = date.today() + timedelta(days=max(0, min(args.jours, 3)))
        resume = edt.jour(jour_)
        if resume is None:
            print("Pas de meteo : Open-Meteo injoignable, ou jour hors prevision.")
            return
        jc = [c for c in edt.du_jour(cours, jour_) if c.est_cours]
        depart = affichage.heure_lever(jc[0])[1] if jc and not jc[0].a_distance else None
        lignes = [f"{affichage.jour_fr(jour_).capitalize()} a {config.METEO_LIEU}",
                  resume.resume()]
        lignes += edt.bloc_depart(depart, jc[0].debut if jc else None, jour_)[1:]
        sortir("Meteo", lignes, vers_discord, "info", canal="commandes")
    elif cmd == "tableaux":
        ok_statut = affichage.publier(cours, liste_devoirs)
        ok_edt = affichage.publier_tableau_edt(cours, liste_devoirs)
        print(f"#statut : {'ok' if ok_statut else 'echec'} · "
              f"#edt : {'ok' if ok_edt else 'echec'}")
    elif cmd == "prochain":
        sortir("Prochain cours", affichage.bloc_prochain(cours, liste_devoirs),
               vers_discord, "cours", canal="commandes")
    elif cmd in ("aujourdhui", "demain"):
        jour = date.today() + (timedelta(days=1) if cmd == "demain" else timedelta())
        lignes = affichage.bloc_journee(cours, liste_devoirs, jour,
                                        avec_reveil=cmd == "demain")
        lignes += [""] + affichage.bloc_devoirs(liste_devoirs, "📚 A faire", horizon=7)
        if vers_discord:
            envoyer_photo(affichage.jour_relatif(jour).capitalize(),
                          legende_jour(cours, liste_devoirs, jour),
                          lambda: image.rendre_jour(cours, liste_devoirs, jour),
                          canal="annonces", secours=lignes)
            print("envoye.")
        else:
            sortir(affichage.jour_relatif(jour).capitalize(), lignes, False, "cours")
    elif cmd == "semaine":
        lundi = edt.semaine_de(date.today())
        lignes = affichage.bloc_semaine(cours, lundi) + [""] + \
            affichage.bloc_devoirs(liste_devoirs, "📚 A faire", horizon=7)
        if vers_discord:
            envoyer_photo(f"Semaine du {lundi:%d/%m}",
                          affichage.bloc_devoirs(liste_devoirs, "📚 A faire", horizon=7),
                          lambda: image.rendre(cours, lundi,
                                               lundi + timedelta(days=6),
                                               titre=f"Semaine du {lundi:%d/%m}"),
                          canal="annonces", secours=lignes)
            print("envoye.")
        else:
            sortir(f"Semaine du {lundi:%d/%m}", lignes, False)
    elif cmd == "libre":
        sortir("Creneaux libres", affichage.creneaux_libres(cours), vers_discord, "calme")


# ════════════════════════════════════════════════════════════════════════════
#  LA SURVEILLANCE DU WEBMAIL  (ex-uptime.py)
# ════════════════════════════════════════════════════════════════════════════
# Surveille le webmail CYU et previent sur Discord quand il tombe ou revient.
#
# Script independant du reste : il ne touche ni a CELCAT ni aux devoirs, il ne
# partage que l'aiguillage Discord. A lancer a cote du bot si tu veux savoir
# quand la boite mail est de nouveau joignable.
#
#     python3 assistant.py uptime
#
# Les recaps « tout va bien » partent dans #logs, les pannes dans #alertes avec
# mention. A chaque verification, l'etat est aussi ecrit dans
# donnees/webmail.json : c'est ce que lit le panneau de #statut, qui tourne dans
# l'autre processus.


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


def main_uptime():
    up, detail = check()
    affichage.enregistrer_webmail(up, detail)
    affichage.simple(f"Monitoring du webmail demarre sur {config.WEBMAIL_URL}\n"
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
        affichage.enregistrer_webmail(up, detail)
        n += 1
        if up:
            ok += 1
        print(f"{datetime.now():%H:%M:%S} | {'UP' if up else 'DOWN'} | {detail}",
              flush=True)

        # changement d'etat : la seule chose qui merite une mention
        if up and not precedent:
            affichage.simple(f"Webmail de retour en ligne : {config.WEBMAIL_URL} "
                             f"({detail})", ping=True, canal="alertes")
        elif not up and precedent:
            affichage.simple(f"Webmail injoignable : {config.WEBMAIL_URL} ({detail})",
                             ping=True, canal="alertes")
        precedent = up

        # recap periodique, dans les logs
        if n >= config.WEBMAIL_HEARTBEAT:
            affichage.simple(f"Monitoring webmail toujours actif. {ok}/{n} essais OK "
                             f"sur la derniere periode ({detail})", canal="logs")
            n = 0
            ok = 0


if __name__ == "__main__":
    main()
