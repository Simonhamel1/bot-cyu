#!/usr/bin/env python3
"""
Les sondages : ceux de Discord, les vrais, avec leurs barres qui se
remplissent en direct — pas des reactions a compter a la main.

/sondage (ou le bouton du panneau) pose une question et des reponses ; Discord
fait le reste. Ce module lit ce que les gens tapent (« 🍕 Pizza ; 🍔 Burger »
ou une reponse par ligne), construit le sondage, et retient ceux du bot pour
en annoncer le resultat quand ils se terminent — sinon le resultat reste
enfoui trois ecrans plus haut.

Les limites sont celles de Discord : 300 caracteres de question, 10 reponses
de 55 caracteres, de 1 heure a 32 jours.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import discord

import config

FICHIER = config.FICHIER_SONDAGES

# Les durees proposees, en heures. Discord accepte de 1 a 768.
DUREES = [("1 heure", 1), ("6 heures", 6), ("12 heures", 12), ("24 heures", 24),
          ("3 jours", 72), ("1 semaine", 168), ("2 semaines", 336)]

# Des jeux de reponses tout prets, proposes pendant la frappe.
MODELES = [
    ("Oui / Non", "👍 Oui ; 👎 Non"),
    ("Je viens / En retard / Absent", "✅ Je viens ; ⏰ En retard ; ❌ Absent"),
    ("Lundi → Vendredi", "Lundi ; Mardi ; Mercredi ; Jeudi ; Vendredi"),
    ("Matin / Midi / Aprem / Soir", "🌅 Matin ; ☀️ Midi ; 🌇 Aprem ; 🌙 Soir"),
    ("😍 🙂 😐 😴", "😍 ; 🙂 ; 😐 ; 😴"),
    ("De 1 à 5", "1 ; 2 ; 3 ; 4 ; 5"),
]

QUESTION_MAX, REPONSE_MAX, REPONSES_MAX = 300, 55, 10

# Un emoji en tete d'une reponse : « 🍕 Pizza » -> emoji 🍕, texte « Pizza ».
# Les emojis Unicode habituels, avec leur eventuel selecteur de variante, ton
# de peau ou sequence ZWJ ; et les emojis personnalises du serveur <:nom:id>.
_EMOJI = (r"(?:[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF"
          r"\u25A0-\u25FF\u2190-\u21FF\u3030\u303D\u3297\u3299\u00A9\u00AE\u2122\u2139]"
          r"[\uFE0F\U0001F3FB-\U0001F3FF]*(?:\u200D[\U0001F000-\U0001FAFF\u2600-\u27BF]"
          r"\uFE0F?)*)")
RE_EMOJI_TETE = re.compile(rf"^(<a?:\w+:\d+>|{_EMOJI}|[0-9#*]\uFE0F?\u20E3)\s*")


def reponses(texte):
    """« 🍕 Pizza ; 🍔 Burger » ou une reponse par ligne
    -> [(emoji ou None, texte)]. ValueError si c'est inutilisable."""
    brut = str(texte or "").strip()
    if not brut:
        brut = MODELES[0][1]
    morceaux = [m.strip() for m in re.split(r"[;\n|]+|\s+/\s+", brut) if m.strip()]
    sortie, vus = [], set()
    for m in morceaux:
        emoji, txt = None, m
        e = RE_EMOJI_TETE.match(m)
        if e:
            emoji, txt = e.group(1), m[e.end():].strip()
        if not txt:                             # « 😍 » tout seul
            emoji, txt = None, m
        txt = txt[:REPONSE_MAX]
        cle = (emoji, txt.lower())
        if cle in vus:
            continue
        vus.add(cle)
        sortie.append((emoji, txt))
    if len(sortie) < 1:
        raise ValueError("il faut au moins une réponse")
    if len(sortie) > REPONSES_MAX:
        raise ValueError(f"Discord s'arrête à {REPONSES_MAX} réponses, "
                         f"il y en a {len(sortie)}")
    return sortie


def construire(question, texte_reponses, heures=None, plusieurs=False):
    """Un discord.Poll pret a envoyer. ValueError si la saisie ne va pas."""
    question = str(question or "").strip()
    if not question:
        raise ValueError("il manque la question")
    heures = int(heures or config.SONDAGE_DUREE_HEURES)
    heures = min(768, max(1, heures))
    sondage = discord.Poll(question=question[:QUESTION_MAX],
                           duration=timedelta(hours=heures), multiple=bool(plusieurs))
    for emoji, txt in reponses(texte_reponses):
        sondage.add_answer(text=txt, emoji=emoji)
    return sondage


def duree_texte(heures):
    for nom, h in DUREES:
        if h == heures:
            return nom
    if heures % 24 == 0 and heures >= 48:
        return f"{heures // 24} jours"
    return f"{heures} h"


# --- Les sondages du bot, en cours -------------------------------------------
def lire():
    try:
        data = json.loads(FICHIER.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (ValueError, OSError):
        return []


def ecrire(liste):
    config.preparer_dossiers()
    FICHIER.write_text(json.dumps(liste, ensure_ascii=False, indent=1),
                       encoding="utf-8")


def suivre(message_id, salon_id, fin, question, auteur_id):
    """Retenir un sondage pour en annoncer le resultat a la fin."""
    liste = [s for s in lire() if str(s.get("message_id")) != str(message_id)]
    liste.append({"message_id": str(message_id), "salon_id": str(salon_id),
                  "fin": fin.isoformat(timespec="minutes"), "question": question[:300],
                  "auteur_id": str(auteur_id),
                  "cree_le": datetime.now().isoformat(timespec="seconds")})
    ecrire(liste)


def oublier(message_id):
    liste = lire()
    reste = [s for s in liste if str(s.get("message_id")) != str(message_id)]
    if len(reste) != len(liste):
        ecrire(reste)


# Un sondage fini mais pas encore clos par Discord n'est pas relu plus
# souvent que ca.
RELECTURE = timedelta(minutes=5)


def a_relever(maintenant=None, liste=None):
    """Les sondages dont la fin est passee : a aller lire. Ceux qu'on vient
    de relire sans succes attendent leur tour."""
    maintenant = maintenant or datetime.now()
    liste = liste if liste is not None else lire()
    sortie = []
    for s in liste:
        try:
            fin = datetime.fromisoformat(str(s.get("fin") or ""))
            lu = datetime.fromisoformat(s["lu_le"]) if s.get("lu_le") else None
        except (ValueError, TypeError):
            continue
        if fin <= maintenant and (lu is None or maintenant - lu >= RELECTURE):
            sortie.append(s)
    return sortie


def marquer_lecture(message_id, maintenant=None):
    liste = lire()
    for s in liste:
        if str(s.get("message_id")) == str(message_id):
            s["lu_le"] = (maintenant or datetime.now()).isoformat(timespec="seconds")
    ecrire(liste)


def perime(s, maintenant=None, heures=6):
    """Un sondage qu'on n'arrive pas a relire depuis des heures : on lache."""
    maintenant = maintenant or datetime.now()
    try:
        fin = datetime.fromisoformat(str(s.get("fin") or ""))
    except ValueError:
        return True
    return maintenant - fin > timedelta(hours=heures)


# --- Le resultat -------------------------------------------------------------
def _barre(part, largeur=12):
    plein = int(round(part * largeur))
    return "▰" * plein + "▱" * (largeur - plein)


def resultat(sondage):
    """(lignes, gagnant) a partir d'un discord.Poll termine.

    gagnant est le texte de la reponse en tete, ou None en cas d'egalite ou
    sans aucun vote."""
    reponses_ = list(sondage.answers)
    total = sum(a.vote_count for a in reponses_)
    reponses_.sort(key=lambda a: -a.vote_count)
    lignes = []
    medailles = ["🥇", "🥈", "🥉"]
    for i, a in enumerate(reponses_):
        part = (a.vote_count / total) if total else 0
        tete = medailles[i] if i < 3 and a.vote_count else "▫️"
        emoji = f"{a.emoji} " if a.emoji else ""
        lignes.append(f"{tete} {emoji}**{a.text}** — {a.vote_count} voix · "
                      f"{part * 100:.0f} %\n-# {_barre(part)}")
    if not total:
        return ["Personne n'a voté. 🫥"], None
    if len(reponses_) > 1 and reponses_[0].vote_count == reponses_[1].vote_count:
        gagnant = None
    else:
        gagnant = reponses_[0].text
    return lignes, gagnant
