#!/usr/bin/env python3
"""
Le panneau de statut : un seul message, reecrit en boucle, dans #statut.

L'idee : ne plus jamais avoir a se demander « est-ce que le bot tourne
encore ? ». Le salon #statut contient UN message, toujours le meme, toujours a
jour, qui repond a la question sans qu'on tape quoi que ce soit :

    ● En ligne depuis 3 jours
    ⏭️ Prochain cours : VBA, dans 1 h 20, FER FT 210
    🔄 CELCAT : lu il y a 4 min
    📬 Webmail : joignable
    📚 3 devoirs en attente, le plus urgent demain

Le message est reecrit toutes les `rafraichir_tableaux_minutes` minutes, donc
il ne genere aucune notification : Discord ne previent pas sur une
modification. C'est justement pour ca que ce salon est separe des annonces.

L'etat du webmail vient d'uptime.py, qui tourne dans un autre processus : les
deux se parlent par un petit fichier JSON, pas par une variable partagee.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import celcat
import config
import devoirs as dv
import image
import notif
import vue
from celcat import _dt

CLE_MESSAGE = "panneau-statut"
FICHIER_WEBMAIL = config.DONNEES / "webmail.json"


# --- L'etat du webmail, partage entre processus ------------------------------
def enregistrer_webmail(up, detail=""):
    """Appele par uptime.py a chaque verification."""
    config.preparer_dossiers()
    FICHIER_WEBMAIL.write_text(json.dumps(
        {"up": bool(up), "detail": str(detail),
         "le": datetime.now().isoformat(timespec="seconds")},
        ensure_ascii=False), encoding="utf-8")


def lire_webmail():
    try:
        data = json.loads(FICHIER_WEBMAIL.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (ValueError, OSError):
        return None


# --- Les elements du panneau -------------------------------------------------
def _duree_depuis(depuis, maintenant=None):
    ecart = ((maintenant or datetime.now()) - depuis).total_seconds() / 60
    if ecart < 60 * 24:
        return vue.duree_fr(ecart)
    jours = int(ecart // (60 * 24))
    return f"{jours} jour" + ("s" if jours > 1 else "")


def _ligne_cours(cours, maintenant):
    actuel = celcat.en_cours(cours, maintenant)
    if actuel:
        reste = (actuel.fin - maintenant).total_seconds() / 60 if actuel.fin else 0
        return (f"▶️ **En cours** — {actuel.titre} · {actuel.ou}"
                + (f" · fini dans {vue.duree_fr(reste)}" if reste > 0 else ""))
    suivant = celcat.prochain(cours, maintenant)
    if not suivant:
        return "⏭️ **Prochain cours** — aucun de prevu 🎉"
    return (f"⏭️ **Prochain cours** — {suivant.titre} · "
            f"**{vue.compte_a_rebours(suivant.debut, maintenant)}** "
            f"({vue.jour_relatif(suivant.jour, maintenant.date())} "
            f"`{suivant.creneau}`) · {suivant.ou}")


def _ligne_celcat(echecs=0):
    age = celcat.age_cache()
    if age is None:
        return "🔄 **CELCAT** — aucune donnee encore lue ⚠️"
    if echecs:
        return (f"🔄 **CELCAT** — ⚠️ {echecs} echec"
                f"{'s' if echecs > 1 else ''} d'affilee, dernieres donnees "
                f"il y a {vue.duree_longue(age)}")
    etat = "🟢" if age < 60 else ("🟠" if age < 60 * 6 else "🔴")
    return f"🔄 **CELCAT** — {etat} lu il y a {vue.duree_longue(age)}"


def _ligne_webmail():
    etat = lire_webmail()
    if not etat:
        return "📬 **Webmail** — non surveille (`python uptime.py`)"
    vu = _dt(etat.get("le"))
    age = f" il y a {vue.duree_longue((datetime.now() - vu).total_seconds() / 60)}" if vu else ""
    if etat.get("up"):
        return f"📬 **Webmail** — 🟢 joignable{age}"
    return (f"📬 **Webmail** — 🔴 injoignable{age}\n"
            f"{vue.RETRAIT}{str(etat.get('detail'))[:120]}")


def _ligne_devoirs(liste_devoirs=None):
    restants = dv.actifs(liste_devoirs)
    if not restants:
        return "📚 **Devoirs** — rien en attente 😌"
    retard = [d for d in restants if (r := dv.jours_restants(d)) is not None and r < 0]
    urgence = ""
    if retard:
        urgence = f" · 🔴 **{len(retard)} en retard**"
    else:
        proche = dv.jours_restants(restants[0])
        if proche == 0:
            urgence = " · 🔥 un pour aujourd'hui"
        elif proche == 1:
            urgence = " · ⚠️ un pour demain"
        elif proche is not None:
            urgence = f" · le plus proche dans {proche} j"
    return f"📚 **Devoirs** — {len(restants)} en attente{urgence}"


def _prochain_rendez_vous(maintenant):
    """Le prochain envoi automatique prevu : dit ce que le bot va faire.

    Sans ca, un salon silencieux est ambigu — panne, ou simplement rien a
    dire ? La ligne leve le doute."""
    candidats = []
    for jour in (maintenant.date(), maintenant.date() + timedelta(days=1)):
        candidats.append((vue.a_heure(jour, config.BRIEFING_MATIN, (7, 0)),
                          "briefing du matin"))
        candidats.append((vue.a_heure(jour, config.BRIEFING_SOIR, (20, 0)),
                          "briefing du soir"))
        if jour.weekday() == config.RECAP_SEMAINE_JOUR:
            candidats.append((vue.a_heure(jour, config.RECAP_SEMAINE_HEURE, (18, 0)),
                              "recap de la semaine"))
    a_venir = sorted((q, n) for q, n in candidats if q > maintenant)
    if not a_venir:
        return None
    quand, nom = a_venir[0]
    return f"📅 **Prochain envoi** — {nom}, {quand:%H:%M} " \
           f"({vue.compte_a_rebours(quand, maintenant)})"


# --- Le panneau complet ------------------------------------------------------
def _ligne_meteo():
    """Le temps qu'il fait, en une ligne. "" si la meteo est coupee ou muette.

    Le panneau est reecrit toutes les dix minutes : le bulletin vient du cache
    de meteo.py, jamais d'un appel reseau par reecriture."""
    if not config.METEO_ACTIVE:
        return ""
    try:
        import meteo
        courte = meteo.ligne_courte()
    except Exception as e:                      # noqa: BLE001 - filet volontaire
        print(f"[!] meteo absente du panneau : {e}", flush=True)
        return ""
    return f"🌡️ **Dehors** — {courte} à {config.METEO_LIEU}" if courte else ""


def bloc(cours, liste_devoirs=None, demarrage=None, echecs=0, maintenant=None):
    """Les lignes du panneau de statut."""
    maintenant = maintenant or datetime.now()
    lignes = []

    if demarrage:
        lignes.append(f"● **En ligne** depuis {_duree_depuis(demarrage, maintenant)}")
    else:
        lignes.append("● **En ligne**")
    lignes.append("")

    lignes.append(_ligne_cours(cours, maintenant))

    jc = celcat.du_jour(cours, maintenant.date())
    if jc:
        finis = sum(1 for c in jc if c.fin and maintenant >= c.fin)
        restant = sum(c.minutes for c in jc if not (c.fin and maintenant >= c.fin))
        lignes.append(f"📆 **Aujourd'hui** — {finis}/{len(jc)} cours faits"
                      + (f" · {vue.duree_fr(restant)} restantes" if restant else " · fini 🎉"))
    else:
        lignes.append("📆 **Aujourd'hui** — aucun cours")

    ligne = _ligne_meteo()
    if ligne:
        lignes.append(ligne)

    lignes += ["", _ligne_devoirs(liste_devoirs), "",
               _ligne_celcat(echecs), _ligne_webmail()]

    rdv = _prochain_rendez_vous(maintenant)
    if rdv:
        lignes += ["", rdv]

    return lignes


def publier(cours, liste_devoirs=None, demarrage=None, echecs=0):
    """Reecrit le panneau dans #statut. Ne notifie personne : c'est une
    modification de message, pas un nouveau message."""
    couleur = "alerte" if echecs >= 3 else "statut"
    return notif.epingler(
        CLE_MESSAGE, "État de l'assistant",
        bloc(cours, liste_devoirs, demarrage, echecs),
        couleur=couleur, canal="statut",
        pied=f"mis a jour toutes les {config.RAFRAICHIR_TABLEAUX_MINUTES} min")


def _resume_semaine(cours, liste_devoirs, lundi):
    """Les deux ou trois lignes qui accompagnent l'image dans #edt.

    L'image dit la forme de la semaine ; ces lignes disent ce qu'une image ne
    peut pas dire — dans combien de temps est le prochain cours, et ce qu'il
    faut rendre."""
    lignes = []
    suivant = celcat.prochain(cours)
    if suivant:
        lignes.append(f"⏭️ **Prochain** — `{suivant.creneau}` "
                      f"{vue.jour_relatif(suivant.jour)} · {suivant.titre} · "
                      f"{suivant.ou} ({vue.compte_a_rebours(suivant.debut)})")
    restants = dv.actifs(liste_devoirs, 7)
    if restants:
        lignes.append(f"📚 **{len(restants)} devoir"
                      f"{'s' if len(restants) > 1 else ''} sous 7 jours** — "
                      + ", ".join(d["titre"][:28] for d in restants[:4]))
    total = sum(c.minutes for c in cours
                if c.est_cours and lundi <= c.jour <= lundi + timedelta(days=6))
    if total:
        lignes.append(f"-# {vue.duree_fr(total)} de cours cette semaine · "
                      f"`/edt` pour un jour precis, `/actu` pour ce qui a bougé")
    return lignes


def publier_tableau_edt(cours, liste_devoirs=None, lundi=None):
    """Reecrit le tableau de la semaine dans #edt.

    Meme principe que le panneau de statut : le salon ne contient qu'un
    message, celui de la semaine en cours, et il est toujours juste. Depuis
    que l'image existe, ce message EST l'image : c'est la vue qu'on regarde
    dix fois par jour, elle merite mieux qu'une liste.

    Retombe sur le tableau en texte si Pillow manque ou si le dessin echoue :
    un salon vide serait pire qu'un tableau moins joli.
    """
    lundi = lundi or celcat.semaine_de(date.today())
    titre = f"Semaine du {lundi:%d/%m}"

    if config.IMAGES and config.TABLEAU_EDT_IMAGE and image.DISPONIBLE:
        try:
            chemin = image.rendre(cours, lundi, lundi + timedelta(days=6),
                                  config.DONNEES / "tableau-edt.png", titre=titre)
            ok = notif.epingler_image(
                "tableau-edt", chemin, titre,
                _resume_semaine(cours, liste_devoirs, lundi),
                couleur="cours", canal="edt",
                pied=f"mis a jour toutes les {config.RAFRAICHIR_TABLEAUX_MINUTES} min")
            if ok:
                return True
            print("[!] image de #edt non publiee, retour au tableau texte",
                  flush=True)
        except (image.PillowManquant, OSError, ValueError) as e:
            print(f"[!] dessin de #edt impossible ({e}), retour au texte", flush=True)

    return notif.epingler(
        "tableau-edt", titre,
        vue.tableau_semaine(cours, lundi, liste_devoirs),
        couleur="cours", canal="edt",
        pied="mis a jour tout seul · /edt pour un jour precis")
