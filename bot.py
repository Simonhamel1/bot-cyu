#!/usr/bin/env python3
"""
Le bot Discord : tout se pilote depuis Discord, plus besoin du terminal.

C'est le fichier a lancer au quotidien. Un seul processus fait les deux choses :

  * il PARLE  : le daemon d'assistant.py tourne dans un fil d'execution a part
                et envoie briefings, alertes et tableaux dans leurs salons ;
  * il ECOUTE : les commandes slash repondent la ou tu les tapes.

    /edt [quand]        ton emploi du temps EN PHOTO : aujourd'hui, demain,
                        « 12/10 », « lundi », « la semaine », « +14 »...
    /photo [du] [au]    une periode entiere en photo
    /actu [jours]       ce qui a bouge dans l'emploi du temps, en photo
    /prochain           le prochain cours et dans combien de temps
    /devoirs            ce qu'il reste a faire, un bouton « fait » par devoir
    /devoir             un formulaire pour en ajouter un
    /fait, /supprimer   rayer ou retirer un devoir (avec autocompletion)
    /stats [quand]      ce que pese ta semaine : heures, matieres, trous
    /meteo [jours]      le temps, et s'il faut un parapluie pour ton trajet
    /libre              tes creneaux libres
    /statut             l'assistant tourne-t-il, fraicheur des donnees
    /rafraichir         relire CELCAT tout de suite
    /panneau            epingle le panneau de boutons dans un salon
    /ics                le fichier .ics a importer dans ton agenda
    /help               l'aide, construite depuis ta config

Comment les reponses sont faites
--------------------------------
Chaque reponse est une CARTE (interface.py) : un bloc colore qui contient le
titre, la photo, le texte, et ses propres boutons. Une journee porte
« ◀ hier · demain ▶ », les stats « semaine precedente · suivante » : on
navigue en cliquant, le message se met a jour sur place, et le salon ne se
remplit pas.

Ces boutons sont PERSISTANTS : leur identifiant contient l'action et ses
arguments (« cyu:edt:j:2026-10-12 »), et BoutonCyu sait la rejouer a partir de
la. Un message d'il y a un mois marche encore apres dix redemarrages, sans
rien garder en memoire.

Lancement :

    pip install -r requirements.txt
    python assistant.py salons       # une seule fois, cree les sept salons
    python bot.py

Les commandes slash ne demandent PAS l'intent « contenu des messages ». En
revanche le bot doit avoir ete invite avec le scope applications.commands,
sinon aucune commande n'apparait dans Discord.
"""

from __future__ import annotations

import asyncio
import io
import re
import sys
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from uuid import uuid4

import discord
from discord import app_commands

import actu
import assistant
import celcat
import config
import changements as chg
import devoirs as dv
import image as img
import interface as ui
import meteo
import notif
import predictions as pr
import stats
import statut as st
import vue

# Le daemon tourne-t-il dans le meme processus que le bot ? Mets False si tu
# preferes lancer `python assistant.py daemon` de ton cote.
AVEC_DAEMON = True

# L'heure de demarrage, affichee dans /statut et dans le panneau.
DEMARRAGE = datetime.now()

# La forme de TOUS nos identifiants de composants : « cyu:action:arg1:arg2 ».
TEMPLATE = r"cyu:(?P<action>[a-z]+)(?::(?P<args>.*))?"

# Une periode de grille ne depasse jamais six semaines : au-dela l'image
# devient une bande illisible, et CELCAT n'a de toute facon rien de plus.
GRILLE_MAX_JOURS = 41

# Combien de devoirs par page dans la liste interactive. Chaque devoir prend
# trois composants Discord (section, texte, bouton) : six, c'est la marge.
DEVOIRS_PAR_PAGE = 6
# Une prediction en prend quatre (texte, rangee, deux boutons de vote).
PREDICTIONS_PAR_PAGE = 5
# /clear : au-dela, les messages de plus de 14 jours (supprimes un par un)
# feraient durer l'operation au-dela de ce que Discord accorde.
CLEAR_MAX = 300

TYPES_DEVOIR = [
    ("Devoir", "devoir", "exercices, lecture, a rendre en cours", "📝"),
    ("DM", "dm", "un devoir maison, note", "🏠"),
    ("Projet", "projet", "un rendu de groupe ou un livrable", "🧩"),
    ("Révision", "revision", "a reviser avant une date", "📖"),
    ("Examen", "examen", "un partiel, un DS, une soutenance", "🎓"),
]

SEMAINE_CHOIX = [
    app_commands.Choice(name="cette semaine", value=0),
    app_commands.Choice(name="la semaine prochaine", value=1),
    app_commands.Choice(name="dans deux semaines", value=2),
    app_commands.Choice(name="la semaine passée", value=-1),
    app_commands.Choice(name="tout l'emploi du temps connu", value=99),
]

AFFICHAGE_CHOIX = [
    app_commands.Choice(name="photo", value="photo"),
    app_commands.Choice(name="texte", value="texte"),
]

# Ce que /edt propose pendant la frappe. Ce ne sont QUE des suggestions : le
# champ reste libre, donc « 12/10 » ou « +21 » marchent sans figurer ici.
SUGGESTIONS = [
    ("aujourd'hui", ""),
    ("demain", "demain"),
    ("après-demain", "apres-demain"),
    ("cette semaine", "semaine"),
    ("la semaine prochaine", "semaine prochaine"),
    ("les 7 prochains jours", "+7"),
    ("les 14 prochains jours", "+14"),
    ("le mois qui vient", "+28"),
] + [(j, j) for j in vue.JOURS]

RE_JOURS = re.compile(r"^\+?(\d{1,3})\s*j?$")


# --- Lire « quand » ----------------------------------------------------------
def periode(texte, cours=None):
    """« quand » -> (debut, fin, mode). Leve ValueError si c'est incomprehensible.

    mode vaut "jour" (une journee en detail) ou "grille" (plusieurs jours, les
    jours a la verticale). Une date seule donne une journee : demander
    « /edt 12/10 », c'est vouloir voir le 12 octobre, pas trois semaines
    autour.
    """
    brut = celcat.normaliser(texte)
    auj = date.today()

    if not brut or brut in ("aujourd'hui", "aujourdhui", "auj", "ce jour", "today"):
        return auj, auj, "jour"
    if brut in ("demain", "dem"):
        return auj + timedelta(days=1), auj + timedelta(days=1), "jour"
    if brut in ("apres-demain", "apres demain", "surlendemain"):
        return auj + timedelta(days=2), auj + timedelta(days=2), "jour"
    if brut in ("hier",):
        return auj - timedelta(days=1), auj - timedelta(days=1), "jour"

    if "semaine prochaine" in brut or brut in ("prochaine semaine", "semaine+1", "s+1"):
        lundi = celcat.semaine_de(auj) + timedelta(days=7)
        return lundi, lundi + timedelta(days=6), "grille"
    if brut.startswith("semaine") or brut in ("cette semaine", "la semaine",
                                              "semaine en cours", "s"):
        lundi = celcat.semaine_de(auj)
        return lundi, lundi + timedelta(days=6), "grille"
    if brut in ("mois", "ce mois", "le mois", "mois prochain"):
        return auj, auj + timedelta(days=27), "grille"

    m = RE_JOURS.match(brut)
    if m:
        jours = min(int(m.group(1)), GRILLE_MAX_JOURS)
        return auj, auj + timedelta(days=jours), ("jour" if jours == 0 else "grille")

    jour = vue.lire_date(texte, cours or [], auj)
    return jour, jour, "jour"


def _date(texte):
    """« 2026-10-12 » -> date. Ce que les identifiants de boutons transportent."""
    return date.fromisoformat(str(texte)[:10])


# --- Les donnees et le dessin, jamais sur la boucle asyncio ------------------
async def _donnees():
    """Lecture du cache dans un fil : ne jamais bloquer la boucle du bot sur du
    disque ou du reseau, sinon le bot parait fige pour tout le monde."""
    return await asyncio.to_thread(
        lambda: (celcat.charger(hors_ligne=True)[0], dv.lire()))


async def _rendu(fabrique, nom="image"):
    """(discord.File, "") ou (None, message d'erreur).

    Tout le dessin part dans un fil : Pillow prend une seconde sur trois
    semaines, et une seconde de boucle asyncio bloquee, c'est un bot qui ne
    repond plus a personne.

    Chaque rendu ecrit dans son propre fichier temporaire : deux personnes qui
    tapent /edt en meme temps ne doivent pas se voler leur image. Et chaque
    fichier Discord recoit un nom unique : la carte le reference par son nom.
    """
    if not config.IMAGES:
        return None, "les images sont désactivées dans config.yaml (affichage.images)"
    if not img.DISPONIBLE:
        return None, "Pillow n'est pas installé sur le serveur (`pip install pillow`)"

    def travail():
        chemin = config.DONNEES / f".rendu-{uuid4().hex[:8]}.png"
        try:
            fabrique(chemin)
            return chemin.read_bytes()
        finally:
            chemin.unlink(missing_ok=True)

    try:
        octets = await asyncio.to_thread(travail)
    except img.PillowManquant as e:
        return None, str(e)
    except OSError as e:
        return None, f"impossible d'écrire l'image : {e}"
    return discord.File(io.BytesIO(octets), filename=f"{nom}-{uuid4().hex[:6]}.png"), ""


# --- Le fil du daemon --------------------------------------------------------
def lancer_daemon():
    """Le daemon dans un fil a part. Il fait du reseau bloquant (requests), donc
    il ne peut pas vivre dans la boucle asyncio de discord.py sans la geler.

    S'il tombe, on le relance : un bot qui repond encore aux commandes mais qui
    a cesse d'envoyer les briefings serait le pire des cas, silencieux."""
    while True:
        try:
            assistant.daemon()
        except Exception:
            traceback.print_exc()
            notif.envoyer("Le daemon a planté",
                          ["Il repart dans 60 s. Détails dans la console.",
                           f"```{traceback.format_exc()[-600:]}```"],
                          couleur="alerte", ping=True, canal="alertes")
            time.sleep(60)


# --- Le bot ------------------------------------------------------------------
class Assistant(discord.Client):
    def __init__(self):
        # Intents par defaut : les commandes slash n'ont pas besoin de lire le
        # contenu des messages, donc rien a activer dans le portail.
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self._commandes_publiees = False

    async def setup_hook(self):
        # Les boutons et menus sont reconstruits a partir de leur identifiant :
        # c'est ce qui les fait survivre aux redemarrages sans rien stocker.
        self.add_dynamic_items(BoutonCyu, MenuCyu)
        if AVEC_DAEMON:
            threading.Thread(target=lancer_daemon, daemon=True,
                             name="daemon-cyu").start()
            print("[i] daemon démarré dans un fil à part")

    async def on_ready(self):
        print(f"[i] connecté comme {self.user}", flush=True)
        if not self._commandes_publiees:
            await self._publier_commandes()
            self._commandes_publiees = True

    async def _publier_commandes(self):
        """Publier les commandes sur UN serveur est instantane ; en global,
        Discord peut mettre jusqu'a une heure a les propager.

        On n'a pas besoin que tu renseignes serveur_id : a ce stade le bot est
        connecte, donc il sait sur quels serveurs il se trouve. On publie sur
        tous. serveur_id, s'il est rempli, l'emporte."""
        if config.SERVEUR_ID.isdigit():
            cibles = [discord.Object(id=int(config.SERVEUR_ID))]
        else:
            cibles = list(self.guilds)

        if not cibles:
            await self.tree.sync()
            print("[!] le bot n'est sur aucun serveur : commandes publiées en "
                  "global (jusqu'à 1 h de délai). Invite-le avec le scope "
                  "applications.commands.")
            return

        for serveur in cibles:
            try:
                self.tree.copy_global_to(guild=serveur)
                await self.tree.sync(guild=serveur)
                nom = getattr(serveur, "name", serveur.id)
                print(f"[i] commandes publiées sur « {nom} » (immédiat)")
            except discord.Forbidden:
                print(f"[!] pas le droit de publier les commandes sur "
                      f"{getattr(serveur, 'name', serveur.id)} : il manque le "
                      f"scope applications.commands dans l'invitation du bot.")


bot = Assistant()


# --- Les composants persistants ----------------------------------------------
# Un bouton ou un menu n'est qu'un identifiant. Quand quelqu'un clique, Discord
# nous rend cet identifiant, on le decoupe, et on rejoue l'action. Rien n'est
# garde en memoire cote bot : c'est pour ca que ca marche apres un redemarrage.
def _decouper_args(texte):
    # Un morceau qui commence par « _ » est un suffixe de dedoublonnage pose
    # par interface.py, pas un argument (voir interface.Bouton.suffixe).
    return [a for a in str(texte or "").split(":") if a != "" and not a.startswith("_")]


class BoutonCyu(discord.ui.DynamicItem[discord.ui.Button], template=TEMPLATE):
    def __init__(self, b: ui.Bouton):
        super().__init__(discord.ui.Button(
            label=b.libelle[:80], style=ui.STYLES.get(b.style, ui.STYLES["gris"]),
            custom_id=b.custom_id, emoji=b.emoji, disabled=b.inactif))
        self.action, self.args = b.action, [str(a) for a in b.args]

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(ui.Bouton(item.label or "", match["action"],
                             tuple(_decouper_args(match["args"]))))

    async def callback(self, inter: discord.Interaction):
        await agir(inter, self.action, self.args)


class MenuCyu(discord.ui.DynamicItem[discord.ui.Select], template=TEMPLATE):
    def __init__(self, m: ui.Menu):
        super().__init__(ui._menu_simple(m))
        self.action, self.args = m.action, [str(a) for a in m.args]

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        # On garde le menu tel que Discord nous le rend (avec ses options et
        # les valeurs choisies) : le reconstruire les perdrait.
        menu = cls.__new__(cls)
        discord.ui.DynamicItem.__init__(menu, item)
        menu.action, menu.args = match["action"], _decouper_args(match["args"])
        return menu

    async def callback(self, inter: discord.Interaction):
        await agir(inter, self.action, self.args, list(self.item.values))


def carte(*args, **kw):
    """interface.carte(), avec nos composants persistants injectes."""
    kw.setdefault("fabrique_bouton", BoutonCyu)
    kw.setdefault("fabrique_menu", MenuCyu)
    return ui.carte(*args, **kw)


def carte_composee(*args, **kw):
    kw.setdefault("fabrique_bouton", BoutonCyu)
    kw.setdefault("fabrique_menu", MenuCyu)
    return ui.carte_composee(*args, **kw)


# --- Envoyer ou remplacer ----------------------------------------------------
async def repondre(inter, vue, fichiers=(), ephemere=False):
    """La reponse a une commande ou a un bouton du panneau.

    REGLE DE L'API DISCORD, apprise a nos depens : apres un `defer`, un
    followup ne peut porter QUE le drapeau « ephemere » — jamais celui des
    cartes nouvelle generation. Envoyer une carte par `followup.send` apres
    un defer est donc refuse, silencieusement pour l'utilisateur (« l'app ne
    repond pas »). La carte doit passer par la MODIFICATION de la reponse
    originale, c'est-a-dire du message « reflechit… », qui l'accepte. Le
    caractere ephemere, lui, est herite du defer : inutile de le redire.

    Sans defer prealable, on repond directement, et la c'est `ephemere` qui
    decide.
    """
    fichiers = [f for f in fichiers if f is not None]
    if inter.response.is_done():
        await inter.edit_original_response(view=vue, attachments=fichiers)
    else:
        await inter.response.send_message(view=vue, files=fichiers, ephemeral=ephemere)


async def remplacer(inter, vue, fichiers=()):
    """Le message qui porte le bouton, reecrit sur place : c'est la navigation
    « hier / demain » sans encombrer le salon."""
    fichiers = [f for f in fichiers if f is not None]
    if inter.response.is_done():
        await inter.edit_original_response(view=vue, attachments=fichiers)
    else:
        await inter.response.edit_message(view=vue, attachments=fichiers)


# =============================================================================
# Les vues : chaque fonction rend (carte, [fichiers]). Les commandes slash,
# les boutons du panneau et les boutons de navigation appellent exactement
# les memes fonctions — les trois ne peuvent donc pas diverger.
# =============================================================================
def _b(libelle, action, *args, style="gris", emoji=None, inactif=False):
    return ui.Bouton(libelle, action, tuple(args), style, emoji, inactif)


# --- Une journee -------------------------------------------------------------
async def vue_jour(jour, texte=False):
    cours, liste_devoirs = await _donnees()
    auj = date.today()
    titre = vue.jour_relatif(jour, auj).capitalize()
    if titre.lower() != vue.jour_fr(jour):
        titre += f" — {vue.jour_fr(jour)}"

    navigation = [
        _b("◀", "edt", "j", jour - timedelta(days=1)),
        _b("Aujourd'hui", "edt", "j", auj, style="bleu" if jour != auj else "gris",
           inactif=jour == auj),
        _b("▶", "edt", "j", jour + timedelta(days=1)),
        _b("La semaine", "edt", "s", celcat.semaine_de(jour), emoji="🗓️"),
    ]

    if texte:
        corps = vue.bloc_journee(cours, liste_devoirs, jour, avec_reveil=jour != auj)
        navigation.append(_b("En photo", "edt", "j", jour, emoji="📷"))
        return carte(titre, corps, "cours", boutons=navigation), []

    fichier, souci = await _rendu(
        lambda chemin: img.rendre_jour(cours, liste_devoirs, jour, chemin),
        nom=f"jour-{jour:%Y-%m-%d}")
    if fichier is None:
        corps = vue.bloc_journee(cours, liste_devoirs, jour, avec_reveil=jour != auj)
        return carte(titre, corps, "cours", boutons=navigation, pied=souci), []

    legende = assistant.legende_jour(cours, liste_devoirs, jour)
    navigation.append(_b("Texte", "edt", "t", jour, emoji="📝"))
    return carte(titre, legende, "cours", image=fichier, boutons=navigation), [fichier]


# --- Une periode, les jours a la verticale -----------------------------------
async def vue_grille(debut, fin, texte=False):
    cours, liste_devoirs = await _donnees()
    if fin < debut:
        debut, fin = fin, debut
    fin = min(fin, debut + timedelta(days=GRILLE_MAX_JOURS))
    portee = (fin - debut).days
    saut = timedelta(days=7 if portee <= 7 else portee + 1)

    auj = date.today()
    cette = celcat.semaine_de(auj)
    est_semaine = debut.weekday() == 0 and portee == 6
    if est_semaine:
        titre = f"Semaine du {debut:%d/%m}"
        if debut == cette:
            titre = "Cette semaine"
        elif debut == cette + timedelta(days=7):
            titre = "La semaine prochaine"
    else:
        titre = f"Du {vue.jour_fr(debut, court=True)} au {vue.jour_fr(fin, court=True)}"

    navigation = [
        _b("◀", "edt", "g", debut - saut, fin - saut),
        _b("Cette semaine", "edt", "s", cette, style="bleu",
           inactif=est_semaine and debut == cette),
        _b("▶", "edt", "g", debut + saut, fin + saut),
        _b("Aujourd'hui", "edt", "j", auj, emoji="📆"),
    ]

    if texte:
        _, corps, _ = _grille_texte(cours, debut, fin)
        navigation.append(_b("En photo", "edt", "g", debut, fin, emoji="📷"))
        return carte(titre, corps, "cours", boutons=navigation), []

    fichier, souci = await _rendu(
        lambda chemin: img.rendre(cours, debut, fin, chemin), nom="edt")
    if fichier is None:
        _, corps, _ = _grille_texte(cours, debut, fin)
        return carte(titre, corps, "cours", boutons=navigation, pied=souci), []

    semaine_ = stats.semaine(cours, celcat.semaine_de(debut)) if est_semaine else None
    sous = (f"{vue.duree_fr(semaine_.total_minutes)} de cours · "
            f"{semaine_.seances} séances · {semaine_.jours_travailles} jours"
            if semaine_ and not semaine_.vide else
            f"{vue.jour_fr(debut)} → {vue.jour_fr(fin)}")
    navigation.append(_b("Texte", "edt", "x", debut, fin, emoji="📝"))
    if est_semaine:
        navigation = [navigation, [_b("Ce que pèse la semaine", "stats", debut, emoji="📊")]]
    return carte(titre, [], "cours", sous_titre=sous, image=fichier,
                 boutons=navigation), [fichier]


def _grille_texte(cours, debut, fin):
    if fin - debut <= timedelta(days=7):
        lundi = celcat.semaine_de(debut)
        return f"Semaine du {lundi:%d/%m}", vue.bloc_semaine(cours, lundi), "cours"
    lignes, lundi = [], celcat.semaine_de(debut)
    while lundi <= fin:
        lignes += [f"**Semaine du {lundi:%d/%m}**"] + \
                  vue.bloc_semaine(cours, lundi, detail=False) + [""]
        lundi += timedelta(days=7)
    return "Période", lignes, "cours"


async def vue_edt(quand="", affichage="photo"):
    """Le point d'entree de /edt : une journee ou une grille selon `quand`."""
    cours, _ = await _donnees()
    debut, fin, mode = periode(quand, cours)
    if mode == "jour":
        return await vue_jour(debut, texte=affichage == "texte")
    return await vue_grille(debut, fin, texte=affichage == "texte")


# --- Le prochain cours -------------------------------------------------------
async def vue_prochain():
    cours, liste_devoirs = await _donnees()
    boutons = [_b("Aujourd'hui", "edt", "j", date.today(), emoji="📆"),
               _b("Demain", "edt", "j", date.today() + timedelta(days=1), emoji="🌙"),
               _b("Actualiser", "pan", "prochain", emoji="🔄")]
    fichier, souci = await _rendu(
        lambda chemin: img.rendre_prochain(cours, liste_devoirs, chemin), nom="prochain")
    corps = vue.bloc_prochain(cours, liste_devoirs)
    if fichier is None:
        return carte("Prochain cours", corps, "cours", boutons=boutons, pied=souci), []
    suivant = celcat.prochain(cours)
    sous = (f"{suivant.titre} · {vue.compte_a_rebours(suivant.debut)}"
            if suivant else "rien de prévu")
    return carte("Prochain cours", [], "cours", sous_titre=sous, image=fichier,
                 boutons=boutons), [fichier]


# --- Ce qui a bouge ----------------------------------------------------------
async def vue_actu(jours=7):
    jours = max(1, min(int(jours), actu.RETENTION_JOURS))
    liste = await asyncio.to_thread(actu.historique, jours)
    boutons = [_b(f"{n} jours", "actu", n, style="bleu" if n == jours else "gris",
                  inactif=n == jours) for n in (7, 14, 30)]
    if not liste:
        dernier = await asyncio.to_thread(actu.quand_dernier)
        corps = [f"Aucun changement d'emploi du temps depuis {jours} jours."]
        if dernier:
            corps.append(f"Le dernier remonte au **{dernier:%d/%m à %H:%M}**.")
        return carte("Rien n'a bougé", corps, "calme", boutons=boutons), []

    fichier, souci = await _rendu(
        lambda chemin: img.rendre_changements(
            liste, chemin, titre="Ce qui a changé",
            sous_titre=f"sur les {jours} derniers jours"), nom="changements")
    lignes, _ = await asyncio.to_thread(chg.bloc, liste)
    if fichier is None:
        return carte(chg.titre(liste), lignes, chg.couleur(liste),
                     boutons=boutons, pied=souci), []
    return carte(chg.titre(liste), [], chg.couleur(liste),
                 sous_titre=f"sur les {jours} derniers jours", image=fichier,
                 boutons=boutons), [fichier]


# --- Les devoirs -------------------------------------------------------------
def _quand_devoir(d):
    """« pour demain », « dans 5 j · lun. 21/09 » : l'urgence d'un devoir en
    quelques mots, et son pictogramme."""
    ech = dv.echeance_dt(d)
    reste = dv.jours_restants(d)
    if reste is None:
        return "sans échéance", ""
    if reste < 0:
        return f"**en retard de {-reste} j**", "🔴"
    if reste == 0:
        return "**pour aujourd'hui**", "🔥"
    if reste == 1:
        return "**pour demain**", "⚠️"
    quand = f"dans {reste} j · {vue.jour_fr(ech.date(), court=True)}"
    if ech.strftime("%H:%M") not in ("23:59", "00:00"):
        quand += f" à {ech:%H:%M}"
    return quand, ("🟠" if reste <= 3 else "")


def _texte_devoir(d):
    icone = {v: e for _, v, _, e in TYPES_DEVOIR}.get(d.get("type"), "📝")
    quand, urgence = _quand_devoir(d)
    tete = f"{icone} **{d['titre']}**"
    if d.get("matiere"):
        tete += f" · {d['matiere']}"
    lignes = [tete, f"-# #{d['id']} · {quand} {urgence}".rstrip()]
    if d.get("note"):
        lignes.append(f"-# ↳ {d['note'][:140]}")
    return "\n".join(lignes)


async def vue_devoirs(page=0):
    """La liste interactive : un bouton « fait » par devoir, un menu pour en
    supprimer un, un bouton pour en ajouter."""
    liste = await asyncio.to_thread(dv.lire)
    restants = dv.actifs(liste)
    faits = [d for d in liste if d.get("fait")]
    pages = max(1, -(-len(restants) // DEVOIRS_PAR_PAGE))
    page = max(0, min(int(page), pages - 1))
    sous = (f"{len(restants)} en attente · {len(faits)} fait{'s' if len(faits) > 1 else ''}"
            + (f" · page {page + 1}/{pages}" if pages > 1 else ""))

    actions = [_b("Ajouter", "dev", "ajout", style="vert", emoji="➕"),
               _b("En photo", "dev", "photo", emoji="📷"),
               _b("Actualiser", "dev", "page", page, emoji="🔄")]
    if pages > 1:
        actions = [actions, [_b("◀", "dev", "page", page - 1, inactif=page == 0),
                             _b("▶", "dev", "page", page + 1, inactif=page >= pages - 1)]]

    if not restants:
        corps = ["Rien en attente. 😌",
                 "-# Tape `/devoir`, ou clique sur **Ajouter**."]
        return carte("Devoirs", corps, "devoir", sous_titre=sous, boutons=actions), []

    tranche = restants[page * DEVOIRS_PAR_PAGE:(page + 1) * DEVOIRS_PAR_PAGE]
    elements = [(_texte_devoir(d), _b("Fait", "dev", "fait", d["id"], style="vert",
                                      emoji="✅")) for d in tranche]
    corps = ui.sections(elements, fabrique_bouton=BoutonCyu)
    menu = ui.Menu("devsel", "Supprimer un devoir…",
                   [(f"#{d['id']} {d['titre']}"[:100], str(d["id"]),
                     (f"{d.get('matiere') or ''} {_quand_devoir(d)[0]}"
                      .replace("*", "").strip() or None), "🗑️")
                    for d in restants[:25]], args=("suppr",))
    teinte = "alerte" if any((dv.jours_restants(d) or 9) <= 0 for d in restants) \
        else "devoir"
    return carte_composee("Devoirs", corps, teinte, sous_titre=sous,
                          boutons=actions, menus=[menu]), []


async def vue_devoirs_photo():
    liste = await asyncio.to_thread(dv.lire)
    restants = dv.actifs(liste)
    boutons = [_b("La liste", "dev", "page", 0, style="bleu", emoji="📋"),
               _b("Ajouter", "dev", "ajout", style="vert", emoji="➕")]
    fichier, souci = await _rendu(
        lambda chemin: img.rendre_devoirs(liste, chemin), nom="devoirs")
    if fichier is None:
        return carte("Devoirs", vue.bloc_devoirs(liste)[1:], "devoir",
                     boutons=boutons, pied=souci), []
    return carte("Devoirs", [], "devoir",
                 sous_titre=f"{len(restants)} en attente", image=fichier,
                 boutons=boutons), [fichier]


def _choix_devoirs(saisie):
    """Les devoirs en attente, pour l'autocompletion de /fait et /supprimer."""
    bas = celcat.normaliser(saisie)
    sortie = []
    for d in dv.actifs():
        libelle = f"#{d['id']} {d['titre']}"
        if d.get("matiere"):
            libelle += f" · {d['matiere']}"
        quand, _ = _quand_devoir(d)
        libelle += f" — {quand.replace('*', '')}"
        if not bas or bas in celcat.normaliser(libelle):
            sortie.append(app_commands.Choice(name=libelle[:100], value=int(d["id"])))
    return sortie[:25]


# --- Les stats de la semaine -------------------------------------------------
async def vue_stats(lundi):
    cours, _ = await _donnees()
    lundi = celcat.semaine_de(lundi)
    semaine_ = await asyncio.to_thread(stats.semaine, cours, lundi)
    note = stats.avertissement(semaine_, cours)
    cette = celcat.semaine_de(date.today())
    navigation = [
        [_b("◀", "stats", lundi - timedelta(days=7)),
         _b("Cette semaine", "stats", cette, style="bleu", inactif=lundi == cette),
         _b("▶", "stats", lundi + timedelta(days=7)),
         _b("La grille", "edt", "s", lundi, emoji="🗓️")],
        [_b("Tout l'emploi du temps", "bilan", emoji="📚"),
         _b("Comparer à la précédente", "comparer", lundi, emoji="⚖️")],
    ]
    titre = f"Ta semaine — {semaine_.libelle}"

    if semaine_.vide:
        archive = stats.archive_de(lundi)
        corps = ["Aucun cours connu pour cette semaine-là."]
        if archive:
            corps.append(f"Elle pesait **{vue.duree_fr(archive)}** d'après l'archive, "
                         f"mais CELCAT n'en garde plus le détail.")
        elif lundi < cette:
            corps.append("CELCAT ne sert que l'avenir : une semaine passée n'est "
                         "comparable que si l'assistant tournait déjà.")
        return carte(titre, corps, "calme", boutons=navigation), []

    fichier, souci = await _rendu(
        lambda chemin: img.rendre_stats(semaine_, chemin, cours=cours,
                                        avertissement=note), nom="stats")
    if fichier is None:
        corps = await asyncio.to_thread(stats.bloc, semaine_, cours)
        if note:
            corps = [f"⚠️ *{note}*", ""] + corps
        return carte(titre, corps, "info", boutons=navigation, pied=souci), []
    sous = (f"{vue.duree_fr(semaine_.total_minutes)} de cours · "
            f"{semaine_.seances} séances · "
            f"{vue.duree_fr(semaine_.trous_minutes)} de trous")
    return carte(titre, [f"-# ⚠️ {note}"] if note else [], "info", sous_titre=sous,
                 image=fichier, boutons=navigation), [fichier]


async def vue_bilan():
    """Tout ce que CELCAT connait : les heures par matiere, et par semaine."""
    cours, _ = await _donnees()
    b = await asyncio.to_thread(stats.bilan, cours)
    boutons = [_b("Cette semaine", "stats", celcat.semaine_de(date.today()),
                  style="bleu", emoji="📊"),
               _b("La grille", "edt", "s", celcat.semaine_de(date.today()), emoji="🗓️"),
               _b("Actualiser", "bilan", emoji="🔄")]
    titre = "Tout l'emploi du temps"
    if b.vide:
        return carte(titre, ["CELCAT ne connaît aucun cours pour l'instant."],
                     "calme", boutons=boutons), []
    sous = (f"{b.libelle} · {vue.duree_fr(b.total_minutes)} de cours · "
            f"{b.seances} séances · {len(b.matieres)} matières")
    fichier, souci = await _rendu(
        lambda chemin: img.rendre_bilan(b, chemin, cours=cours), nom="bilan")
    if fichier is None:
        return carte(titre, stats.bloc_bilan(b), "info", boutons=boutons, pied=souci), []
    return carte(titre, [], "info", sous_titre=sous, image=fichier,
                 boutons=boutons, pied="tout ce que CELCAT connaît à ce jour"), [fichier]


# --- Cette semaine contre la precedente --------------------------------------
async def vue_comparer(lundi):
    cours, liste_devoirs = await _donnees()
    lundi = celcat.semaine_de(lundi)
    avant_l = lundi - timedelta(days=7)
    cette = await asyncio.to_thread(stats.cote_semaine, cours, lundi)
    avant = await asyncio.to_thread(stats.cote_semaine, cours, avant_l)
    dev_cette = stats.devoirs_de_la_semaine(liste_devoirs, lundi)
    dev_avant = stats.devoirs_de_la_semaine(liste_devoirs, avant_l)
    lignes = stats.bloc_comparaison(cette, avant, dev_cette, dev_avant)

    ref = celcat.semaine_de(date.today())
    titre = ("Cette semaine" if lundi == ref else f"Semaine du {lundi:%d/%m}") \
        + " vs la précédente"
    navigation = [
        [_b("◀", "comparer", avant_l),
         _b("Cette semaine", "comparer", ref, style="bleu", inactif=lundi == ref),
         _b("▶", "comparer", lundi + timedelta(days=7)),
         _b("Les stats", "stats", lundi, emoji="📊")],
        [_b("Tout l'emploi du temps", "bilan", emoji="📚")],
    ]
    if cette.vide or avant.vide:
        teinte = "calme"
    else:
        teinte = "devoir" if cette.minutes > avant.minutes else "cours"
    return carte(titre, lignes, teinte,
                 sous_titre=f"semaine du {lundi:%d/%m} contre celle du {avant_l:%d/%m}",
                 boutons=navigation), []


# --- Les examens : compte a rebours ------------------------------------------
async def vue_examens():
    cours, liste_devoirs = await _donnees()
    liste = await asyncio.to_thread(stats.examens, cours, liste_devoirs)
    boutons = [_b("Ajouter un examen", "dev", "ajout", "examen", style="vert", emoji="🎓"),
               _b("Créneaux libres", "libre", 14, emoji="🫧"),
               _b("Devoirs", "dev", "page", 0, emoji="📚"),
               _b("Actualiser", "examens", emoji="🔄")]
    if not liste:
        return carte("Examens", ["Aucun examen connu.",
                                 "-# CELCAT n'en annonce aucun et ton carnet n'en "
                                 "contient pas. Ajoute-les avec le bouton : le "
                                 "compte à rebours suit tout seul."],
                     "calme", boutons=boutons), []

    lignes = []
    for e in liste[:12]:
        j = e["jours"]
        if j == 0:
            quand, ico = "**AUJOURD'HUI**", "🔥"
        elif j == 1:
            quand, ico = "**DEMAIN**", "⚠️"
        elif j <= 7:
            quand, ico = f"**J-{j}**", "🟠"
        else:
            quand, ico = f"**J-{j}**", "📅"
        titre = e["matiere"] or e["titre"]
        detail = f"{vue.jour_fr(e['quand'].date(), court=True)} {e['quand']:%H:%M}"
        if e["ou"]:
            detail += f" · {e['ou']}"
        if e["source"] == "devoir":
            detail += " · de ton carnet"
        lignes.append(f"{ico} {quand} — **{titre}**\n-# {detail}")
    if len(liste) > 12:
        lignes.append(f"-# … et {len(liste) - 12} autres")

    premier = liste[0]
    libre = await asyncio.to_thread(stats.minutes_libres, cours, premier["quand"])
    lignes += ["", f"**D'ici le premier** ({vue.jour_relatif(premier['quand'].date())}) : "
                   f"**{vue.duree_fr(libre)}** de créneaux libres en semaine pour "
                   f"réviser, sans toucher aux week-ends."]
    teinte = ("alerte" if premier["jours"] <= 1 else
              "devoir" if premier["jours"] <= 7 else "info")
    sous = (f"{len(liste)} à venir · le premier {vue.jour_relatif(premier['quand'].date())}")
    return carte("Examens — compte à rebours", lignes, teinte, sous_titre=sous,
                 boutons=boutons), []


# --- Les paris de l'assistant ------------------------------------------------
async def vue_paris_assistant():
    """Des pronostics, tires de vraies donnees, presentes comme des paris.

    C'est de l'humour, mais chaque pari repose sur un chiffre reel : les
    changements du mois, la journee la plus lourde, la probabilite de pluie,
    l'echeance la plus proche. La « cote » est l'inverse de la probabilite.
    """
    cours, liste_devoirs = await _donnees()
    auj = date.today()
    cette = celcat.semaine_de(auj)
    paris = []                       # (emoji, titre, texte, probabilite)

    # Le cours qui va bouger : celui qui a le plus bouge ces 30 derniers jours.
    historique = await asyncio.to_thread(actu.historique, 30)
    compte = {}
    for ch in historique:
        c = ch.apres or ch.avant
        if c is not None:
            nom = stats.nom_matiere(c)
            compte[nom] = compte.get(nom, 0) + 1
    if compte:
        nom, n = max(compte.items(), key=lambda kv: kv[1])
        paris.append(("🔀", "Le cours qui va encore bouger",
                      f"**{nom}** — déjà {n} changement{'s' if n > 1 else ''} en 30 jours",
                      min(0.9, 0.3 + 0.15 * n)))
    else:
        paris.append(("🔀", "Le cours qui va bouger",
                      "Rien n'a bougé depuis 30 jours : l'assistant parie sur une "
                      "semaine calme", 0.15))

    # Le jour ou tu vas craquer : le plus lourd de la semaine.
    s = stats.semaine(cours, cette)
    plein = s.jour_plein
    if plein:
        texte = f"**{vue.jour_fr(plein.jour)}** — {vue.duree_fr(plein.minutes)} de cours"
        if plein.trous_minutes:
            texte += f", {vue.duree_fr(plein.trous_minutes)} de trous"
        paris.append(("🫠", "Le jour où tu vas craquer", texte, min(0.95, plein.minutes / 600)))

    # Le parapluie : le jour le plus arrose des quatre a venir.
    if config.METEO_ACTIVE:
        def meteo_():
            pire = None
            for n in range(4):
                j = meteo.jour(auj + timedelta(days=n))
                if j and (pire is None or j.probabilite > pire[1].probabilite):
                    pire = (auj + timedelta(days=n), j)
            return pire
        pire = await asyncio.to_thread(meteo_)
        if pire:
            jour_, j = pire
            paris.append(("☔", "Le jour du parapluie",
                          f"**{vue.jour_relatif(jour_).capitalize()}** — {j.texte}, "
                          f"{j.probabilite} % de pluie", max(0.05, j.probabilite / 100)))

    # Le devoir rendu a la derniere minute : le plus proche.
    restants = dv.actifs(liste_devoirs)
    if restants:
        d = restants[0]
        r = dv.jours_restants(d)
        texte = f"**{d['titre']}**" + (f" · {d['matiere']}" if d.get("matiere") else "")
        paris.append(("⏰", "Le devoir rendu à la dernière minute",
                      f"{texte} — {_quand_devoir(d)[0]}",
                      0.9 if (r is not None and r <= 1) else 0.6))

    # Le trou qui finira en sieste : le plus long de la semaine.
    trous = [(j, a, b) for j in s.jours for a, b in j.trous]
    if trous:
        j, a, b = max(trous, key=lambda t: t[2] - t[1])
        minutes = (b - a).total_seconds() / 60
        paris.append(("🛋️", "Le trou qui finira en sieste",
                      f"**{vue.jour_fr(j.jour, court=True)} {a:%H:%M}–{b:%H:%M}** — "
                      f"{vue.duree_fr(minutes)} à tuer", min(0.9, minutes / 240)))

    lignes = []
    for emoji, titre, texte, p in paris:
        cote = max(1.05, 1 / max(p, 0.05))
        lignes += [f"{emoji} **{titre}**", texte,
                   f"-# cote {cote:.2f} · {p * 100:.0f} % de chances selon l'assistant", ""]
    if not paris:
        lignes = ["Pas assez de données pour parier. Reviens quand CELCAT aura parlé."]
    boutons = [_b("Rejouer", "paris", style="bleu", emoji="🎲"),
               _b("Vos prédictions", "pred", "page", 0, emoji="🔮"),
               _b("Ce qui a changé", "actu", 7, emoji="🔔"),
               _b("Météo", "meteo", 0, emoji="🌦️")]
    return carte("Les paris de l'assistant", lignes, "info",
                 sous_titre="des vrais chiffres, des fausses cotes",
                 boutons=boutons,
                 pied="l'assistant n'a jamais raison, mais il a des chiffres"), []


# --- Vos predictions : le jeu ------------------------------------------------
def _texte_prediction(p):
    ech = pr.echeance_date(p)
    details = []
    if pr.en_retard(p):
        details.append("⏰ échéance dépassée — à trancher")
    elif ech:
        details.append(f"d'ici le {vue.jour_fr(ech, court=True)}")
    elif p.get("echeance_texte"):
        details.append(f"d'ici « {p['echeance_texte']} »")
    if p.get("mise"):
        details.append(f"mise : {p['mise']}")
    texte = f"**#{p['id']} · {p['auteur']}** — « {p['texte']} »"
    if details:
        texte += "\n-# " + " · ".join(details)
    pour, contre = pr.noms(p, "oui"), pr.noms(p, "non")
    if pour or contre:
        texte += "\n-# " + " · ".join(x for x in (f"👍 {pour}" if pour else "",
                                                  f"👎 {contre}" if contre else "") if x)
    else:
        texte += "\n-# personne n'a encore voté"
    return texte


async def vue_predictions(page=0):
    """Les predictions en jeu : deux boutons de vote par ligne, un menu pour
    trancher ou supprimer (l'auteur seulement), et de quoi en poser une."""
    liste = await asyncio.to_thread(pr.lire)
    en_jeu = pr.ouvertes(liste)
    # Celles a trancher d'abord, puis les plus recentes.
    en_jeu.sort(key=lambda p: (not pr.en_retard(p), -int(p.get("id", 0))))
    _, _, chiffres = pr.classement(liste)
    pages = max(1, -(-len(en_jeu) // PREDICTIONS_PAR_PAGE))
    page = max(0, min(int(page), pages - 1))

    sous = f"{chiffres['ouvertes']} en jeu · {chiffres['tranchees']} tranchée"
    sous += "s" if chiffres["tranchees"] > 1 else ""
    if chiffres["reussite_votes"] is not None:
        sous += f" · les parieurs ont raison à {chiffres['reussite_votes']:.0f} %"
    if pages > 1:
        sous += f" · page {page + 1}/{pages}"

    composants, options = [], []
    for p in en_jeu[page * PREDICTIONS_PAR_PAGE:(page + 1) * PREDICTIONS_PAR_PAGE]:
        oui, non = pr.comptes(p)
        composants.append(discord.ui.TextDisplay(_texte_prediction(p)[:ui.TEXTE_MAX]))
        rangee = discord.ui.ActionRow()
        rangee.add_item(BoutonCyu(_b(f"Oui ({oui})", "pred", "oui", p["id"], page,
                                     style="vert", emoji="👍")))
        rangee.add_item(BoutonCyu(_b(f"Non ({non})", "pred", "non", p["id"], page,
                                     style="rouge", emoji="👎")))
        composants.append(rangee)
        court = p["texte"][:60]
        options += [(f"✔ #{p['id']} — c'est arrivé", f"ok:{p['id']}", court, "✅"),
                    (f"✘ #{p['id']} — raté", f"ko:{p['id']}", court, "❌"),
                    (f"🗑 #{p['id']} — supprimer", f"suppr:{p['id']}", court, None)]
    if not en_jeu:
        composants = [discord.ui.TextDisplay(
            "Aucune prédiction en jeu. Lance-toi : **🔮 Parier**.\n"
            "-# « Kevin va valider l'année », « le cours de VBA de jeudi va "
            "sauter »… tout le monde vote, l'auteur tranche, le classement juge.")]

    actions = [[_b("Parier", "pred", "ajout", style="vert", emoji="🔮"),
                _b("Classement", "pred", "classement", emoji="🏆"),
                _b("Tranchées", "pred", "closes", emoji="📜"),
                _b("Les paris de l'assistant", "paris", emoji="🤖"),
                _b("Actualiser", "pred", "page", page, emoji="🔄")]]
    if pages > 1:
        actions.append([_b("◀", "pred", "page", page - 1, inactif=page == 0),
                        _b("▶", "pred", "page", page + 1, inactif=page >= pages - 1)])
    menus = [ui.Menu("predsel", "Trancher ou supprimer une prédiction (l'auteur seulement)…",
                     options[:25])] if options else []
    teinte = "devoir" if any(pr.en_retard(p) for p in en_jeu) else "info"
    return carte_composee("Prédictions", composants, teinte, sous_titre=sous,
                          boutons=actions, menus=menus,
                          pied="c'est vous qui pariez, l'auteur tranche"), []


async def vue_predictions_closes():
    liste = await asyncio.to_thread(pr.lire)
    closes = pr.closes(liste)
    boutons = [_b("En jeu", "pred", "page", 0, style="bleu", emoji="🔮"),
               _b("Classement", "pred", "classement", emoji="🏆")]
    if not closes:
        return carte("Prédictions tranchées", ["Aucune pour l'instant : les paris "
                                                "sont encore ouverts."], "calme",
                     boutons=boutons), []
    lignes = []
    for p in closes[:12]:
        oui, non = pr.comptes(p)
        justes = pr.noms(p, p["resultat"])
        quand = str(p.get("tranche_le", ""))[:10]
        try:
            quand = vue.jour_fr(date.fromisoformat(quand), court=True)
        except ValueError:
            pass
        lignes.append(f"{'✅' if p['resultat'] == 'oui' else '❌'} **#{p['id']} · "
                      f"{p['auteur']}** — « {p['texte']} »\n-# tranché {quand} · "
                      f"👍 {oui} · 👎 {non}"
                      + (f" · avaient raison : {justes}" if justes else ""))
    if len(closes) > 12:
        lignes.append(f"-# … et {len(closes) - 12} autres")
    return carte("Prédictions tranchées", lignes, "info",
                 sous_titre=f"{len(closes)} au total", boutons=boutons), []


async def vue_classement():
    liste = await asyncio.to_thread(pr.lire)
    prophetes, parieurs, chiffres = pr.classement(liste)
    boutons = [_b("En jeu", "pred", "page", 0, style="bleu", emoji="🔮"),
               _b("Tranchées", "pred", "closes", emoji="📜")]
    if not chiffres["tranchees"]:
        return carte("Classement", ["Rien à classer tant qu'aucune prédiction n'est "
                                    "tranchée."], "calme", boutons=boutons), []
    medailles = ["🥇", "🥈", "🥉", "4.", "5."]
    lignes = ["### 🔮 Les prophètes", "-# dont les prédictions se réalisent"]
    for m, (nom, ok, n) in zip(medailles, prophetes[:5]):
        lignes.append(f"{m} **{nom}** — {ok}/{n} réalisée{'s' if ok > 1 else ''}")
    lignes += ["", "### 🎯 Les parieurs", "-# qui votent juste"]
    for m, (nom, ok, n) in zip(medailles, parieurs[:5]):
        lignes.append(f"{m} **{nom}** — {ok}/{n} vote{'s' if n > 1 else ''} juste"
                      f"{'s' if ok > 1 else ''}")
    if not parieurs:
        lignes.append("personne n'a encore voté sur une prédiction tranchée")
    sous = (f"{chiffres['tranchees']} tranchée{'s' if chiffres['tranchees'] > 1 else ''} · "
            f"{chiffres['realisees']} réalisée{'s' if chiffres['realisees'] > 1 else ''}")
    return carte("Classement", lignes, "info", sous_titre=sous, boutons=boutons), []


class ModalePrediction(discord.ui.Modal, title="Une prédiction"):
    texte = discord.ui.Label(
        text="Ta prédiction",
        component=discord.ui.TextInput(
            style=discord.TextStyle.paragraph, max_length=300,
            placeholder="Kevin va valider l'année · le cours de VBA de jeudi va sauter"))
    pour = discord.ui.Label(
        text="D'ici quand ?",
        description="12/10 · +30 · vendredi — ou en toutes lettres (« la fin de "
                    "l'année ») — facultatif",
        component=discord.ui.TextInput(required=False, max_length=60))
    mise = discord.ui.Label(
        text="La mise", description="facultatif — un kebab, un café, l'honneur",
        component=discord.ui.TextInput(required=False, max_length=80))

    async def on_submit(self, inter: discord.Interaction):
        texte = str(self.texte.component.value).strip()
        pour = str(self.pour.component.value).strip()
        mise = str(self.mise.component.value).strip()
        echeance, echeance_texte = "", ""
        if pour:
            cours, _ = await _donnees()
            try:
                echeance = dv.resoudre_echeance(pour, cours, "") or ""
            except ValueError:
                echeance_texte = pour        # « la fin de l'annee » : on garde tel quel
        await asyncio.to_thread(pr.ajouter, texte, inter.user.id,
                                inter.user.display_name, echeance, echeance_texte, mise)
        # Publique : une prediction est faite pour etre vue, et votee.
        vue_, _ = await vue_predictions(0)
        await inter.response.send_message(view=vue_)


# --- /clear : vider un salon -------------------------------------------------
def _peut_nettoyer(inter):
    """Le droit « gerer les messages » dans CE salon, ou administrateur."""
    salon = inter.channel
    if inter.guild is None or not hasattr(salon, "permissions_for"):
        return False
    droits = salon.permissions_for(inter.user)
    return bool(droits.manage_messages or droits.administrator)


def _est_panneau(message):
    """Ce message est-il un panneau de l'assistant ? On le reconnait a ses
    boutons (« cyu:pan:… »), pas a son epingle : si le bot n'a pas eu le droit
    d'epingler, le panneau n'est pas epingle, et /clear l'effacerait."""
    def parcourir(composants):
        for c in composants or ():
            if str(getattr(c, "custom_id", "") or "").startswith("cyu:pan:"):
                return True
            if parcourir(getattr(c, "children", None)):
                return True
            accessoire = getattr(c, "accessory", None)
            if accessoire is not None and parcourir([accessoire]):
                return True
        return False
    return parcourir(getattr(message, "components", None))


def _droits_manquants(inter):
    """Ce qui manque AU BOT dans ce salon pour vider : la liste des droits,
    en francais, tels qu'ils s'appellent dans Discord. Vide si tout va bien."""
    moi = getattr(inter.guild, "me", None)
    if moi is None or not hasattr(inter.channel, "permissions_for"):
        return []
    droits = inter.channel.permissions_for(moi)
    manquants = []
    if not droits.manage_messages:
        manquants.append("Gérer les messages")
    if not droits.read_message_history:
        manquants.append("Lire l'historique des messages")
    return manquants


async def _nettoyer(inter, args):
    """La confirmation de /clear a ete cliquee : on vide, ou on annule."""
    if args[:1] != ["go"]:
        await inter.response.edit_message(
            view=ui.erreur("Rien n'a été supprimé.", "Annulé"), attachments=[])
        return
    if not _peut_nettoyer(inter):
        await inter.response.edit_message(
            view=ui.erreur("Il faut le droit « gérer les messages » dans ce salon."))
        return
    nombre = int(args[1]) if len(args) > 1 and args[1].isdigit() else 100
    nombre = max(1, min(nombre, CLEAR_MAX))
    manquants = _droits_manquants(inter)
    if manquants:
        await inter.response.edit_message(view=ui.erreur(
            "Il manque au **bot** (pas à toi) : " + ", ".join(f"« {d} »" for d in manquants)
            + ".\n-# Paramètres du serveur → Rôles → le rôle du bot → Permissions. "
              "Si ce salon a ses propres permissions, c'est là qu'il faut regarder "
              "(salon → Modifier → Permissions).",
            "Le bot n'a pas le droit"), attachments=[])
        return
    await inter.response.defer()
    if not hasattr(inter.channel, "purge"):
        await inter.edit_original_response(view=ui.erreur("Ce type de salon ne se vide pas."))
        return
    try:
        # Les epingles sont gardees, et le panneau aussi meme s'il n'est pas
        # epingle : les effacer serait le meilleur moyen de tout casser.
        supprimes = await inter.channel.purge(
            limit=nombre, check=lambda m: not m.pinned and not _est_panneau(m), bulk=True)
    except discord.Forbidden:
        await inter.edit_original_response(view=ui.erreur(
            "Discord a refusé : le bot n'a pas le droit « Gérer les messages » ou "
            "« Lire l'historique des messages » dans ce salon."))
        return
    n = len(supprimes)
    await inter.edit_original_response(view=carte(
        "Salon vidé", [f"**{n}** message{'s' if n > 1 else ''} supprimé{'s' if n > 1 else ''}.",
                       "-# Les messages épinglés et le panneau ont été gardés."
                       + (f" Il en restait peut-être plus que {nombre} : relance "
                          f"/clear." if n >= nombre else "")],
        "calme", pied=False))


# --- La meteo ----------------------------------------------------------------
async def vue_meteo(jours=0):
    jours = max(0, min(int(jours), 3))
    boutons = [_b(l, "meteo", n, style="bleu" if n == jours else "gris",
                  inactif=n == jours)
               for n, l in ((0, "Aujourd'hui"), (1, "Demain"), (2, "J+2"), (3, "J+3"))]
    if not config.METEO_ACTIVE:
        return carte("Météo désactivée", ["`meteo.active: false` dans config.yaml."],
                     "calme"), []

    cours, _ = await _donnees()
    quand = date.today() + timedelta(days=jours)
    jc = [c for c in celcat.du_jour(cours, quand) if c.est_cours]

    def travail():
        resume = meteo.jour(quand)
        if resume is None:
            return None, [], None, 0
        depart = None
        if jc and not jc[0].a_distance:
            _, depart = vue.heure_lever(jc[0])
        minuit = datetime.combine(quand, datetime.min.time())
        creneaux = meteo.fenetre(depart or minuit.replace(hour=8),
                                 jc[0].debut if jc else minuit.replace(hour=18))
        return resume, creneaux, depart, meteo.marge_pluie(creneaux)

    resume, creneaux, depart, marge = await asyncio.to_thread(travail)
    titre = f"Météo — {vue.jour_relatif(quand)}"
    if resume is None:
        return carte(titre, ["Open-Meteo est injoignable, ou le jour demandé sort "
                             "de la prévision (quatre jours au maximum)."],
                     "calme", boutons=boutons), []

    gene = meteo.pire(creneaux)
    lignes = [f"### {resume.resume()}"]
    if resume.lever:
        lignes.append(f"🌅 lever du soleil {resume.lever} · coucher {resume.coucher}")
    lignes.append("")
    if jc:
        if depart:
            heure = depart - timedelta(minutes=marge)
            lignes.append(f"**Ton trajet** — départ conseillé **{heure:%H:%M}** "
                          f"pour un cours à {jc[0].debut:%H:%M}"
                          + (f" · **+{marge} min** à cause de la pluie" if marge else ""))
        else:
            lignes.append("**Premier cours à distance** : pas de trajet.")
        if gene is not None:
            lignes.append(f"↳ {gene.resume()}")
        phrase = meteo.conseil(creneaux)
        if phrase:
            lignes.append(f"↳ **{phrase.capitalize()}.**")
    else:
        lignes.append("Aucun cours ce jour-là : la pluie ne te concerne pas. 🛋️")

    age = meteo.age_minutes()
    pied = f"Open-Meteo · bulletin lu il y a {vue.duree_fr(age)}" if age else "Open-Meteo"
    teinte = "alerte" if gene is not None and gene.mouille else "info"
    return carte(titre, lignes, teinte, sous_titre=f"à {config.METEO_LIEU}",
                 boutons=boutons, pied=pied), []


# --- Les creneaux libres -----------------------------------------------------
async def vue_libre(jours=7):
    jours = max(1, min(int(jours), 31))
    cours, _ = await _donnees()
    boutons = [_b(f"{n} jours", "libre", n, style="bleu" if n == jours else "gris",
                  inactif=n == jours) for n in (7, 14, 31)]
    return carte("Créneaux libres", vue.creneaux_libres(cours, jours=jours), "calme",
                 sous_titre=f"sur les {jours} prochains jours, au moins "
                            f"{config.CRENEAU_LIBRE_MINUTES} min d'affilée",
                 boutons=boutons), []


# --- L'etat de l'assistant ---------------------------------------------------
async def vue_statut():
    cours, liste_devoirs = await _donnees()
    vivant = any(t.name == "daemon-cyu" and t.is_alive() for t in threading.enumerate())
    # Le panneau d'etat est le dernier endroit qui a le droit de tomber : c'est
    # lui qu'on ouvre quand quelque chose ne va pas. Une ligne qui echoue est
    # remplacee par son erreur, et le reste s'affiche quand meme.
    try:
        lignes = list(await asyncio.to_thread(st.bloc, cours, liste_devoirs, DEMARRAGE))
    except Exception as e:                      # noqa: BLE001 - filet volontaire
        traceback.print_exc()
        lignes = [f"⚠️ le panneau d'état a échoué : `{type(e).__name__}: {e}`"[:300]]
    lignes += ["", "**Le daemon** — " + ("🟢 actif" if vivant else "🔴 ARRÊTÉ")
               + ("" if AVEC_DAEMON else " (AVEC_DAEMON = False dans bot.py)"),
               "", "**Les salons**"]
    for canal in config.CANAUX:
        mode, cible = notif.destination(canal)
        ou = f"<#{cible}>" if mode == "bot" else "webhook"
        if not notif.salon_configure(canal):
            ou += " ⚠️ non configuré, retombe sur le secours"
        lignes.append(f"`{canal:10s}` {ou}")
    boutons = [_b("Relire CELCAT", "pan", "refresh", style="bleu", emoji="🔄"),
               _b("Actualiser", "pan", "statut", emoji="🔁")]
    return carte("État de l'assistant", lignes, "alerte" if not vivant else "statut",
                 boutons=boutons), []


async def rafraichir():
    """Relire CELCAT et reecrire les deux tableaux vivants. Rend une carte."""
    def travail():
        cours, origine = celcat.charger()
        liste_devoirs = dv.lire()
        st.publier(cours, liste_devoirs, DEMARRAGE)
        st.publier_tableau_edt(cours, liste_devoirs)
        return origine, len([c for c in cours if c.est_cours])

    origine, combien = await asyncio.to_thread(travail)
    return carte("Données rafraîchies",
                 [f"Source : **{origine}** · {combien} cours connus.",
                  "Le tableau de #edt et le panneau de #statut viennent d'être réécrits."],
                 "calme", boutons=[_b("État de l'assistant", "pan", "statut", emoji="🩺")]), []


# --- Le panneau --------------------------------------------------------------
def vue_panneau():
    corps = ["Tout ce que fait l'assistant, en un clic. Les réponses ne sont "
             "visibles que par toi, et chacune a ses propres boutons pour "
             "naviguer.",
             "-# Tu peux aussi taper les commandes : `/edt` `/devoirs` `/stats` "
             "`/examens` `/meteo` — et `/help` pour tout voir"]
    menu = ui.Menu("jour", "Voir un jour de la semaine…",
                   [(j.capitalize(), str(i), None, "📆") for i, j in enumerate(vue.JOURS)]
                   + [("La semaine prochaine", "prochaine", None, "🗓️"),
                      ("Dans deux semaines", "deux", None, "🗓️")])
    boutons = [
        [_b("Aujourd'hui", "pan", "auj", style="bleu", emoji="📆"),
         _b("Demain", "pan", "demain", emoji="🌙"),
         _b("La semaine", "pan", "semaine", emoji="🗓️"),
         _b("Prochain cours", "pan", "prochain", style="vert", emoji="⏭️"),
         _b("Ce qui a changé", "pan", "actu", emoji="🔔")],
        [_b("Devoirs", "pan", "devoirs", emoji="📚"),
         _b("Ajouter un devoir", "dev", "ajout", style="vert", emoji="➕"),
         _b("Ma semaine en chiffres", "pan", "stats", emoji="📊"),
         _b("Météo", "pan", "meteo", emoji="🌦️"),
         _b("Créneaux libres", "pan", "libre", emoji="🫧")],
        [_b("Examens", "pan", "examens", emoji="🎓"),
         _b("Comparer", "pan", "comparer", emoji="⚖️"),
         _b("Prédictions", "pan", "prediction", emoji="🔮"),
         _b("Relire CELCAT", "pan", "refresh", emoji="🔄"),
         _b("État", "pan", "statut", emoji="🩺")],
    ]
    return carte("Assistant CYU", corps, "info", sous_titre="le panneau",
                 boutons=boutons, menus=[menu],
                 pied="ce panneau reste actif après un redémarrage")


# --- L'aide ------------------------------------------------------------------
def _jours_matin():
    """« en semaine », « lun/mar/jeu », ou rien du tout quand c'est tous les
    jours : l'aide dit ce que config.yaml dit, sans etre reecrite."""
    jours = config.BRIEFING_MATIN_JOURS
    if not jours:
        return ""
    if jours == [0, 1, 2, 3, 4]:
        return " en semaine"
    return " " + "/".join(vue.JOURS_COURTS[j] for j in jours)


def texte_aide():
    """L'aide est construite depuis config.yaml, pas ecrite en dur : si tu
    changes l'heure d'un briefing, /help dit la nouvelle heure sans qu'on y
    touche."""
    jour_recap = vue.JOURS[config.RECAP_SEMAINE_JOUR % 7]
    lignes = [
        "### Voir — tout sort en photo, avec des boutons pour naviguer",
        "`/edt` — **ta journée**. Avec une date : `/edt 12/10`, `/edt lundi`, "
        "`/edt demain`. Puis ◀ ▶ pour passer d'un jour à l'autre",
        "`/edt quand:la semaine` — la semaine entière, **les jours à la "
        "verticale**. Aussi : `+14`, `la semaine prochaine`",
        "`/edt affichage:texte` — la même chose en texte, pour copier-coller",
        "`/photo du:12/10 au:31/10` — une période précise",
        "`/actu` — **ce qui a changé** dans l'emploi du temps",
        "`/prochain` — le prochain cours, la salle, et dans combien de temps",
        "`/stats` — **ce que pèse ta semaine** : heures, matières, trous, jour "
        "le plus lourd. `quand: tout l'emploi du temps` pour le bilan de "
        "**toutes les heures par matière**",
        "`/meteo` — le temps, et **s'il faut un parapluie** pour ton trajet",
        "`/libre` — tes créneaux libres",
        "`/comparer` — **cette semaine contre la précédente** : heures, séances, "
        "trous, devoirs, et ce qui bouge par matière",
        "`/examens` — **compte à rebours** avant chaque examen, CELCAT et ton "
        "carnet réunis, avec le temps libre pour réviser d'ici là",
        "`/prediction` — **vos prédictions** 🔮 : qui va valider l'année, quel cours "
        "va sauter… chacun vote 👍👎, l'auteur tranche, le classement juge. "
        "`/parier` pour en poser une. Les paris de l'assistant 🤖 (chiffres réels) "
        "sont derrière un bouton",
        "`/clear` — vider ce salon (les épinglés sont gardés) — droit « gérer les "
        "messages » requis",
        "",
        "### Les devoirs",
        "`/devoirs` — la liste, **un bouton ✅ par devoir**, un menu pour supprimer",
        "`/devoir` — un formulaire pour en ajouter un",
        "`/fait` · `/supprimer` — avec la liste qui s'affiche pendant la frappe",
        "",
        "### Le reste",
        "`/statut` — l'assistant tourne-t-il, fraîcheur des données, salons",
        "`/rafraichir` — relire CELCAT tout de suite",
        "`/panneau` — épingler le panneau de boutons dans un salon",
        "`/ics` — le fichier à importer dans ton agenda",
        "",
        "### Écrire une date  (pour `/edt` comme pour un devoir)",
        "`12/10` · `12/10/2026` · `2026-10-12` · `demain` · `lundi` · `apres-demain`",
        "`+21` — les 21 prochains jours · `la semaine` · `la semaine prochaine`",
        "`prochain:vba` — ton prochain cours de VBA, avec son heure exacte",
        "",
        "### Ce qui arrive tout seul, sans rien taper",
        f"`{config.BRIEFING_MATIN}`{_jours_matin()} **#annonces** — la journée "
        f"entière, et ce qu'il faut rendre aujourd'hui",
        f"`{config.BRIEFING_SOIR}` **#annonces** — demain, l'heure de lever, "
        f"les échéances qui approchent",
        f"`{jour_recap} {config.RECAP_SEMAINE_HEURE}` **#annonces** — la "
        f"semaine qui vient, sa grille et **ce qu'elle pèse en chiffres**",
    ]
    if config.METEO_ACTIVE and config.METEO_BRIEFING:
        lignes.append(
            f"`avec chaque photo de journée` — **la météo à {config.METEO_LIEU}** "
            f"à l'heure de ton départ"
            + (f", et **+{config.METEO_MARGE_PLUIE_MINUTES} min** de départ "
               f"quand il pleut" if config.METEO_MARGE_PLUIE_MINUTES else ""))
    if config.AVANT_COURS_MINUTES:
        minutes = ", ".join(str(m) for m in config.AVANT_COURS_MINUTES)
        lignes.append(f"`{minutes} min avant chaque cours` — la salle et le prof")
    if config.PREMIER_COURS_MINUTES:
        lignes.append(f"`{config.PREMIER_COURS_MINUTES} min avant le premier "
                      f"cours du jour` — l'heure de partir")
    if config.RELANCE_DEVOIRS:
        quand = (f"`{config.RELANCE_DEVOIRS_HEURE}`" if config.RELANCE_DEVOIRS_HEURE
                 else "`fin de journée`")
        lignes.append(f"{quand} **#devoirs** — « tu as eu quoi, des devoirs à "
                      f"noter ? », les jours où tu as eu cours")
    lignes += [
        f"`toutes les {config.VERIF_EDT_MINUTES} min` **#alertes** — cours "
        f"déplacé, annulé, changement de salle, "
        + ("**avec une mention**" if config.PING_CHANGEMENTS else "sans mention"),
        f"`toutes les {config.RAFRAICHIR_TABLEAUX_MINUTES} min` **#edt** et "
        f"**#statut** — les deux tableaux vivants, réécrits sur place (aucune "
        f"notification)",
    ]
    return lignes


def vue_aide():
    return carte("Assistant CYU", texte_aide(), "info", sous_titre="toutes les commandes",
                 boutons=[_b("Le panneau", "pan", "panneau", style="bleu", emoji="🎛️")],
                 pied="l'aide suit config.yaml")


# =============================================================================
# Le formulaire d'ajout d'un devoir
# =============================================================================
class ModaleDevoir(discord.ui.Modal, title="Nouveau devoir"):
    titre = discord.ui.Label(
        text="Quoi ?",
        component=discord.ui.TextInput(placeholder="DM 2, exercices 4 à 9", max_length=100))
    matiere = discord.ui.Label(
        text="Matière", description="facultatif — sert à relier le devoir au bon cours",
        component=discord.ui.TextInput(placeholder="maths, vba, anglais…",
                                       required=False, max_length=40))
    pour = discord.ui.Label(
        text="Pour quand ?",
        description="prochain:maths · demain · lundi · 12/09 · +3 — vide = sans échéance",
        component=discord.ui.TextInput(placeholder="prochain:maths",
                                       required=False, max_length=40))
    type_ = discord.ui.Label(
        text="Type", description="devoir par défaut",
        component=discord.ui.Select(
            options=[discord.SelectOption(label=l, value=v, description=d, emoji=e,
                                          default=v == "devoir")
                     for l, v, d, e in TYPES_DEVOIR],
            min_values=0, max_values=1))
    note = discord.ui.Label(
        text="Détails", description="facultatif",
        component=discord.ui.TextInput(style=discord.TextStyle.paragraph,
                                       required=False, max_length=400))

    def __init__(self, type_defaut="devoir"):
        super().__init__()
        # Les composants sont copies par instance : on peut changer le choix
        # par defaut sans toucher aux autres formulaires ouverts.
        for option in self.type_.component.options:
            option.default = option.value == type_defaut
        if type_defaut == "examen":
            self.title = "Nouvel examen"

    async def on_submit(self, inter: discord.Interaction):
        # Pas de defer : la reponse decide si elle est publique (le devoir est
        # ajoute, tout le monde peut le voir) ou privee (une erreur de saisie).
        # Le travail tient largement dans les trois secondes accordees.
        titre = str(self.titre.component.value).strip()
        matiere = str(self.matiere.component.value).strip()
        pour = str(self.pour.component.value).strip()
        type_ = (self.type_.component.values or ["devoir"])[0]
        note = str(self.note.component.value).strip()

        cours, _ = await _donnees()
        try:
            echeance = dv.resoudre_echeance(pour, cours, matiere)
        except ValueError as e:
            await inter.response.send_message(
                view=ui.erreur(f"{e}\n-# Essaie `demain`, `lundi`, `12/10`, `+3` "
                               f"ou `prochain:{matiere or 'maths'}`.",
                               "Date incomprise"), ephemeral=True)
            return
        d = await asyncio.to_thread(dv.ajouter, titre, matiere, echeance, type_, note)
        restants = len(dv.actifs())
        await inter.response.send_message(
            view=carte("Devoir ajouté", [_texte_devoir(d)], "devoir",
                       sous_titre=f"{restants} en attente",
                       boutons=[_b("Voir la liste", "dev", "page", 0, style="bleu",
                                   emoji="📋"),
                                _b("En ajouter un autre", "dev", "ajout", style="vert",
                                   emoji="➕")]))


# =============================================================================
# Le dispatch : un clic = une action + ses arguments
# =============================================================================
# Deux familles d'actions :
#   * la NAVIGATION (edt, stats, meteo, actu, libre, dev) reecrit le message
#     qui porte le bouton — le salon ne bouge pas ;
#   * le PANNEAU (pan, jour) envoie un nouveau message, visible de toi seul —
#     le panneau, lui, doit rester intact.
async def agir(inter, action, args, valeurs=()):
    """Un clic. Tout ce qui echoue ici est MONTRE : en message prive a celui
    qui a clique (avec le code HTTP et l'action, pour pouvoir le rapporter),
    et en trace complete dans la console. Un bouton muet est pire qu'un
    bouton qui explique."""
    try:
        await _agir(inter, list(args), list(valeurs), action)
    except Exception as e:                      # noqa: BLE001 - filet volontaire
        print(f"[!] bouton {action}:{':'.join(str(a) for a in args)} :", flush=True)
        traceback.print_exc()
        texte = f"❌ **Ça n'a pas marché** — `{type(e).__name__}` : {str(e)[:700]}"
        if isinstance(e, discord.HTTPException):
            texte += f"\n-# HTTP {e.status} · code {e.code}"
        texte += f"\n-# action `{action}:{':'.join(str(a) for a in args)}` — envoie ce message à qui s'occupe du bot"
        try:
            if inter.response.is_done():
                await inter.followup.send(texte[:1900], ephemeral=True)
            else:
                await inter.response.send_message(texte[:1900], ephemeral=True)
        except discord.HTTPException:
            pass


async def _agir(inter, args, valeurs, action):
    if action == "dev" and args[:1] == ["ajout"]:
        await inter.response.send_modal(ModaleDevoir(args[1] if len(args) > 1 else "devoir"))
        return
    if action == "pred" and args[:1] == ["ajout"]:
        await inter.response.send_modal(ModalePrediction())
        return
    if action == "clear":
        await _nettoyer(inter, args)
        return

    if action in ("pan", "jour"):
        await inter.response.defer(ephemeral=True, thinking=True)
        vue_, fichiers = await _vue_panneau(args, valeurs, inter)
        await repondre(inter, vue_, fichiers, ephemere=True)
        return

    # Navigation sur une carte EPHEMERE (celle du panneau) : on ne modifie pas
    # ses pieces jointes en place, on repond par une nouvelle carte, elle
    # aussi ephemere. Seul celui qui clique la voit : rien ne s'encombre.
    ephemere = bool(inter.message is not None and inter.message.flags.ephemeral)
    if ephemere:
        await inter.response.defer(ephemeral=True, thinking=True)
        vue_, fichiers = await _vue_navigation(action, args, valeurs, inter)
        await repondre(inter, vue_, fichiers, ephemere=True)
        return

    # Navigation sur une carte publique : on accuse reception tout de suite
    # (l'image peut prendre une seconde), puis on reecrit le message sur place.
    await inter.response.defer()
    vue_, fichiers = await _vue_navigation(action, args, valeurs, inter)
    await remplacer(inter, vue_, fichiers)


async def _vue_panneau(args, valeurs, inter=None):
    quoi = (args or valeurs or ["auj"])[0]
    auj = date.today()
    if quoi == "auj":
        return await vue_jour(auj)
    if quoi == "demain":
        return await vue_jour(auj + timedelta(days=1))
    if quoi == "semaine":
        lundi = celcat.semaine_de(auj)
        return await vue_grille(lundi, lundi + timedelta(days=6))
    if quoi == "prochaine":
        lundi = celcat.semaine_de(auj) + timedelta(days=7)
        return await vue_grille(lundi, lundi + timedelta(days=6))
    if quoi == "deux":
        lundi = celcat.semaine_de(auj) + timedelta(days=14)
        return await vue_grille(lundi, lundi + timedelta(days=6))
    if quoi.isdigit():                      # un jour de la semaine, 0 = lundi
        return await vue_jour(celcat.semaine_de(auj) + timedelta(days=int(quoi)))
    if quoi == "prochain":
        return await vue_prochain()
    if quoi == "actu":
        return await vue_actu(7)
    if quoi == "devoirs":
        return await vue_devoirs(0)
    if quoi == "stats":
        return await vue_stats(celcat.semaine_de(auj))
    if quoi == "meteo":
        return await vue_meteo(0)
    if quoi == "libre":
        return await vue_libre(7)
    if quoi == "examens":
        return await vue_examens()
    if quoi == "comparer":
        return await vue_comparer(celcat.semaine_de(auj))
    if quoi == "prediction":
        return await vue_predictions(0)
    if quoi == "refresh":
        return await rafraichir()
    if quoi == "statut":
        return await vue_statut()
    if quoi == "help":
        return vue_aide(), []
    if quoi == "panneau":
        return vue_panneau(), []
    return ui.erreur(f"action inconnue : `{quoi}`"), []


async def _vue_navigation(action, args, valeurs, inter=None):
    """`inter` sert aux actions qui ont besoin de savoir QUI clique : voter,
    trancher, supprimer une prediction. None dans les tests : ces actions
    se contentent alors de reafficher la liste."""
    if action == "edt":
        mode = args[0] if args else "j"
        if mode == "j":
            return await vue_jour(_date(args[1]))
        if mode == "t":
            return await vue_jour(_date(args[1]), texte=True)
        if mode == "s":
            lundi = _date(args[1])
            return await vue_grille(lundi, lundi + timedelta(days=6))
        if mode == "g":
            return await vue_grille(_date(args[1]), _date(args[2]))
        if mode == "x":
            return await vue_grille(_date(args[1]), _date(args[2]), texte=True)
    if action == "stats":
        return await vue_stats(_date(args[0]))
    if action == "bilan":
        return await vue_bilan()
    if action == "comparer":
        return await vue_comparer(_date(args[0]))
    if action == "examens":
        return await vue_examens()
    if action == "paris":
        return await vue_paris_assistant()
    if action == "pred":
        quoi = args[0] if args else "page"
        if quoi in ("oui", "non") and len(args) > 1:
            if inter is not None:
                await asyncio.to_thread(pr.voter, args[1], inter.user.id,
                                        inter.user.display_name, quoi)
            page = args[2] if len(args) > 2 else "0"
            return await vue_predictions(int(page) if page.isdigit() else 0)
        if quoi == "closes":
            return await vue_predictions_closes()
        if quoi == "classement":
            return await vue_classement()
        page = args[1] if quoi == "page" and len(args) > 1 else "0"
        return await vue_predictions(int(page) if page.lstrip("-").isdigit() else 0)
    if action == "predsel":
        refus = []
        for valeur in valeurs if inter is not None else []:
            op, _, ident = str(valeur).partition(":")
            if op in ("ok", "ko"):
                _, etat = await asyncio.to_thread(pr.trancher, ident,
                                                  "oui" if op == "ok" else "non",
                                                  inter.user.id)
            elif op == "suppr":
                _, etat = await asyncio.to_thread(pr.supprimer, ident, inter.user.id)
            else:
                continue
            if etat == "pas_auteur":
                refus.append(f"#{ident} n'est pas à toi : seul l'auteur tranche ou supprime.")
            elif etat == "deja":
                refus.append(f"#{ident} est déjà tranchée.")
        if refus and inter is not None:
            # Apres un defer, un followup n'accepte que du texte (voir repondre()).
            await inter.followup.send("\n".join(refus), ephemeral=True)
        return await vue_predictions(0)
    if action == "meteo":
        return await vue_meteo(int(args[0]))
    if action == "actu":
        return await vue_actu(int(args[0]))
    if action == "libre":
        return await vue_libre(int(args[0]))
    if action == "dev":
        quoi = args[0] if args else "page"
        if quoi == "fait":
            await asyncio.to_thread(dv.marquer_fait, args[1])
            return await vue_devoirs(0)
        if quoi == "suppr":
            await asyncio.to_thread(dv.supprimer, args[1])
            return await vue_devoirs(0)
        if quoi == "photo":
            return await vue_devoirs_photo()
        # « ajout » est intercepte plus haut (il ouvre un formulaire) ; s'il
        # arrive quand meme ici, la liste est la reponse la moins surprenante.
        # Et un argument qui n'est pas un numero de page ne fait pas planter.
        page = args[1] if quoi == "page" and len(args) > 1 else "0"
        return await vue_devoirs(int(page) if page.lstrip("-").isdigit() else 0)
    if action == "devsel":
        quoi = args[0] if args else "suppr"
        for ident in valeurs:
            await asyncio.to_thread(dv.supprimer if quoi == "suppr" else dv.marquer_fait,
                                    ident)
        return await vue_devoirs(0)
    return ui.erreur(f"action inconnue : `{action}`"), []


# =============================================================================
# Les commandes slash
# =============================================================================
async def _commande(inter, coroutine, ephemere=False):
    """Le tronc commun : accuser reception, calculer, repondre."""
    await inter.response.defer(ephemeral=ephemere, thinking=True)
    try:
        vue_, fichiers = await coroutine
    except Exception:
        print(f"[!] {inter.command.name if inter.command else '?'} a échoué :",
              flush=True)
        traceback.print_exc()
        raise
    await repondre(inter, vue_, fichiers, ephemere=ephemere)


@bot.tree.command(name="edt", description="Ton emploi du temps en photo")
@app_commands.describe(
    quand="une date (12/10), un jour (lundi), demain, la semaine, +14… "
          "— aujourd'hui par défaut",
    affichage="photo par défaut ; texte si tu préfères copier-coller")
@app_commands.choices(affichage=AFFICHAGE_CHOIX)
async def cmd_edt(inter: discord.Interaction, quand: str = "",
                  affichage: str = "photo"):
    # La date est lue AVANT le defer : seule la premiere reponse peut etre
    # ephemere, et une erreur de saisie n'a rien a faire dans le salon.
    cours, _ = await _donnees()
    try:
        periode(quand, cours)
    except ValueError as e:
        await inter.response.send_message(
            view=ui.erreur(f"{e}\n-# Essaie `12/10`, `lundi`, `demain`, `la semaine`, "
                           f"`+14`.", "Date incomprise"), ephemeral=True)
        return
    await inter.response.defer(thinking=True)
    vue_, fichiers = await vue_edt(quand, affichage)
    await repondre(inter, vue_, fichiers)


@cmd_edt.autocomplete("quand")
async def auto_quand(inter: discord.Interaction, saisie: str):
    """Des suggestions, sans jamais fermer la porte : ce que tu tapes reste
    valable meme s'il ne figure pas dans la liste."""
    bas = celcat.normaliser(saisie)
    sortie = []
    if saisie.strip():
        try:
            debut, fin, mode = periode(saisie)
            libelle = (vue.jour_fr(debut) if mode == "jour" else
                       f"du {vue.jour_fr(debut, court=True)} au "
                       f"{vue.jour_fr(fin, court=True)}")
            sortie.append(app_commands.Choice(name=f"➜ {libelle}",
                                              value=saisie.strip()[:100]))
        except ValueError:
            pass
    for nom, valeur in SUGGESTIONS:
        if len(sortie) >= 25:
            break
        if not bas or bas in celcat.normaliser(nom) or bas in celcat.normaliser(valeur):
            sortie.append(app_commands.Choice(name=nom, value=valeur or "aujourd'hui"))
    return sortie[:25]


@bot.tree.command(name="photo",
                  description="Une période entière en photo (les jours à la verticale)")
@app_commands.describe(
    du="à partir de quand (aujourd'hui par défaut)",
    au="jusqu'à quand : 12/10 · +21 · vendredi (dans 6 jours par défaut)")
async def cmd_photo(inter: discord.Interaction, du: str = "", au: str = ""):
    cours, _ = await _donnees()
    try:
        debut = vue.lire_date(du, cours, date.today())
        fin = vue.lire_date(au, cours, debut + timedelta(days=6))
    except ValueError as e:
        await inter.response.send_message(view=ui.erreur(str(e), "Date incomprise"),
                                          ephemeral=True)
        return
    await inter.response.defer(thinking=True)
    vue_, fichiers = await vue_grille(debut, fin)
    await repondre(inter, vue_, fichiers)


@bot.tree.command(name="actu", description="Ce qui a changé dans ton emploi du temps")
@app_commands.describe(jours="sur combien de jours regarder en arrière (7 par défaut)")
async def cmd_actu(inter: discord.Interaction, jours: int = 7):
    await _commande(inter, vue_actu(jours))


@bot.tree.command(name="prochain", description="Le prochain cours, et dans combien de temps")
async def cmd_prochain(inter: discord.Interaction):
    await _commande(inter, vue_prochain())


@bot.tree.command(name="devoirs", description="Ce qu'il te reste à faire")
async def cmd_devoirs(inter: discord.Interaction):
    await _commande(inter, vue_devoirs(0))


@bot.tree.command(name="devoir", description="Ajouter un devoir")
async def cmd_devoir(inter: discord.Interaction):
    await inter.response.send_modal(ModaleDevoir())


@bot.tree.command(name="fait", description="Marquer un devoir comme fait")
@app_commands.describe(devoir="le devoir — la liste s'affiche pendant la frappe")
async def cmd_fait(inter: discord.Interaction, devoir: int):
    ok = await asyncio.to_thread(dv.marquer_fait, devoir)
    if not ok:
        await inter.response.send_message(view=ui.erreur(f"Aucun devoir #{devoir}."),
                                          ephemeral=True)
        return
    await _commande(inter, vue_devoirs(0))


@bot.tree.command(name="supprimer", description="Retirer un devoir de la liste")
@app_commands.describe(devoir="le devoir — la liste s'affiche pendant la frappe")
async def cmd_supprimer(inter: discord.Interaction, devoir: int):
    d = await asyncio.to_thread(dv.supprimer, devoir)
    if d is None:
        await inter.response.send_message(view=ui.erreur(f"Aucun devoir #{devoir}."),
                                          ephemeral=True)
        return
    await _commande(inter, vue_devoirs(0))


@cmd_fait.autocomplete("devoir")
@cmd_supprimer.autocomplete("devoir")
async def auto_devoir(inter: discord.Interaction, saisie: str):
    return await asyncio.to_thread(_choix_devoirs, saisie)


@bot.tree.command(name="stats", description="Ce que pèse ta semaine : heures, matières, trous")
@app_commands.describe(quand="quelle semaine regarder (cette semaine par défaut)")
@app_commands.choices(quand=SEMAINE_CHOIX)
async def cmd_stats(inter: discord.Interaction, quand: int = 0):
    if quand == 99:
        await _commande(inter, vue_bilan())
        return
    lundi = celcat.semaine_de(date.today()) + timedelta(days=7 * quand)
    await _commande(inter, vue_stats(lundi))


@bot.tree.command(name="comparer",
                  description="Cette semaine contre la précédente : heures, séances, devoirs")
@app_commands.describe(quand="quelle semaine comparer à celle d'avant (cette semaine par défaut)")
@app_commands.choices(quand=SEMAINE_CHOIX[:3])
async def cmd_comparer(inter: discord.Interaction, quand: int = 0):
    lundi = celcat.semaine_de(date.today()) + timedelta(days=7 * quand)
    await _commande(inter, vue_comparer(lundi))


@bot.tree.command(name="examens", description="Compte à rebours avant chaque examen")
async def cmd_examens(inter: discord.Interaction):
    await _commande(inter, vue_examens())


@bot.tree.command(name="prediction",
                  description="Vos prédictions : qui va valider, quel cours va sauter… votez !")
async def cmd_prediction(inter: discord.Interaction):
    await _commande(inter, vue_predictions(0))


@bot.tree.command(name="parier", description="Poser une prédiction (un formulaire s'ouvre)")
async def cmd_parier(inter: discord.Interaction):
    await inter.response.send_modal(ModalePrediction())


@bot.tree.command(name="clear", description="Vider ce salon — les messages épinglés sont gardés")
@app_commands.describe(nombre=f"combien de messages au plus (100 par défaut, {CLEAR_MAX} maximum)")
@app_commands.default_permissions(manage_messages=True)
async def cmd_clear(inter: discord.Interaction, nombre: int = 100):
    nombre = max(1, min(nombre, CLEAR_MAX))
    if not _peut_nettoyer(inter):
        await inter.response.send_message(
            view=ui.erreur("Il faut le droit « gérer les messages » dans ce salon."),
            ephemeral=True)
        return
    await inter.response.send_message(
        view=carte("Vider ce salon ?",
                   [f"Jusqu'à **{nombre}** messages de <#{inter.channel.id}> vont être "
                    f"supprimés. Les messages **épinglés** (panneau, tableaux) sont gardés.",
                    "-# Les messages de plus de 14 jours partent un par un : c'est "
                    "plus lent."],
                   "alerte",
                   boutons=[_b(f"Oui, supprimer jusqu'à {nombre}", "clear", "go", nombre,
                               style="rouge", emoji="🧹"),
                            _b("Annuler", "clear", "non", emoji="✋")],
                   pied=False),
        ephemeral=True)


@bot.tree.command(name="meteo", description="Le temps qu'il fera, et s'il faut un parapluie")
@app_commands.describe(jours="0 = aujourd'hui, 1 = demain (3 au maximum)")
async def cmd_meteo(inter: discord.Interaction, jours: int = 0):
    await _commande(inter, vue_meteo(jours))


@bot.tree.command(name="libre", description="Tes créneaux libres à venir")
@app_commands.describe(jours="nombre de jours à regarder (7 par défaut)")
async def cmd_libre(inter: discord.Interaction, jours: int = 7):
    await _commande(inter, vue_libre(jours))


@bot.tree.command(name="statut", description="L'assistant tourne-t-il bien ?")
async def cmd_statut(inter: discord.Interaction):
    await _commande(inter, vue_statut(), ephemere=True)


@bot.tree.command(name="rafraichir",
                  description="Relire CELCAT maintenant et remettre les tableaux à jour")
async def cmd_rafraichir(inter: discord.Interaction):
    await _commande(inter, rafraichir(), ephemere=True)


@bot.tree.command(name="ics", description="Le fichier .ics à importer dans ton agenda")
async def cmd_ics(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True, thinking=True)

    def travail():
        cours, _ = celcat.charger(hors_ligne=True)
        liste_devoirs = dv.lire()
        chemin = config.DONNEES / "cyu.ics"
        vrais = [c for c in cours if c.est_cours]
        vue.exporter_ics(vrais, liste_devoirs, chemin)
        return chemin.read_bytes(), len(vrais), len(dv.actifs(liste_devoirs))

    octets, nb_cours, nb_devoirs = await asyncio.to_thread(travail)
    fichier = discord.File(io.BytesIO(octets), filename="cyu.ics")
    conteneur = discord.ui.Container(accent_colour=ui.couleur("calme"))
    conteneur.add_item(discord.ui.TextDisplay(
        f"## Ton agenda\n-# {nb_cours} cours · {nb_devoirs} devoirs\n"
        "Importe-le dans Google Agenda ou dans l'appli de ton téléphone. "
        "Les devoirs y sont en événements d'une journée."))
    conteneur.add_item(discord.ui.File("attachment://cyu.ics"))
    vue_ = ui.Carte()
    vue_.add_item(conteneur)
    await inter.edit_original_response(view=vue_, attachments=[fichier])


@bot.tree.command(name="panneau", description="Épingler le panneau de boutons dans ce salon")
async def cmd_panneau(inter: discord.Interaction):
    await inter.response.send_message(view=vue_panneau())
    # Epingler tout de suite : le panneau doit rester en haut du salon meme
    # quand tu y tapes vingt commandes a la suite.
    try:
        message = await inter.original_response()
        await message.pin()
    except discord.HTTPException:
        # Sans « Gerer les messages », pas d'epingle : on le dit, sinon on
        # decouvre le probleme le jour ou le panneau a disparu.
        try:
            await inter.followup.send(
                "⚠️ Le panneau est posté mais **pas épinglé** : le bot n'a pas le droit "
                "« Gérer les messages » dans ce salon. Donne-le-lui (Paramètres du "
                "serveur → Rôles → le rôle du bot), puis refais `/panneau`.",
                ephemeral=True)
        except discord.HTTPException:
            pass


@bot.tree.command(name="help", description="Toutes les commandes de l'assistant")
async def cmd_help(inter: discord.Interaction):
    # Ephemere : l'aide n'a d'interet que pour celui qui la demande.
    await inter.response.send_message(view=vue_aide(), ephemeral=True)


# --- Erreurs -----------------------------------------------------------------
@bot.tree.error
async def en_cas_d_erreur(inter: discord.Interaction, err: app_commands.AppCommandError):
    """Sans ca, une commande qui echoue laisse juste « L'application ne repond
    pas » a l'ecran, et rien dans la console."""
    traceback.print_exception(type(err), err, err.__traceback__)
    cause = err.__cause__ or err
    texte = f"❌ **Ça n'a pas marché** — `{type(cause).__name__}` : {cause}"[:1800]
    try:
        if inter.response.is_done():
            # Apres un defer, un followup n'accepte que du texte : une carte
            # serait refusee (voir repondre()).
            await inter.followup.send(texte, ephemeral=True)
        else:
            await inter.response.send_message(view=ui.erreur(texte), ephemeral=True)
    except discord.HTTPException:
        pass


def main():
    config.preparer_dossiers()
    trous = config.manquants()
    if trous:
        print("[X] config.yaml incomplet :")
        for t in trous:
            print(f"    - {t}")
        sys.exit(1)
    if not config.BOT_TOKEN:
        print("[X] discord.bot_token est vide dans config.yaml. Portail "
              "développeur Discord > ton application > Bot > Reset Token.")
        sys.exit(1)
    try:
        bot.run(config.BOT_TOKEN, log_handler=None)
    except discord.LoginFailure:
        print("[X] jeton refusé par Discord : il est faux ou a été régénéré.")
        sys.exit(1)
    except discord.PrivilegedIntentsRequired as e:
        print(f"[X] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
