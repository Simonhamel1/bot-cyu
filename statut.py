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


def publier_tableau_edt(cours, liste_devoirs=None, lundi=None):
    """Reecrit le tableau de la semaine dans #edt.

    Meme principe que le panneau de statut : le salon ne contient qu'un
    message, celui de la semaine en cours, et il est toujours juste."""
    lundi = lundi or celcat.semaine_de(date.today())
    return notif.epingler(
        "tableau-edt", f"Semaine du {lundi:%d/%m}",
        vue.tableau_semaine(cours, lundi, liste_devoirs),
        couleur="cours", canal="edt",
        pied="mis a jour tout seul · /edt pour un jour precis")
