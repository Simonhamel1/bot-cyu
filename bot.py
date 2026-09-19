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
    /comparer, /examens la semaine contre la precedente ; le compte a rebours
    /prediction, /parier  le jeu de la promo : on parie, on vote, on tranche
    /sondage            un vrai sondage Discord, resultat annonce a la fin
    /rappel, /rappels   « demain 9h : rendre le TP », le bot te mentionne
    /anniversaire(s)    le tien, et les prochains de la promo
    /meteo [jours]      le temps, et s'il faut un parapluie pour ton trajet
    /libre              tes creneaux libres
    /ects [semestre]    la maquette : UE, matieres, ECTS, coefficients
    /statut             l'assistant tourne-t-il, fraicheur des donnees
    /rafraichir         relire CELCAT tout de suite
    /panneau            epingle le panneau de boutons dans un salon
    /ics                le fichier .ics a importer dans ton agenda
    /clear              vider un salon (les epingles et le panneau restent)
    /help               l'aide, construite depuis ta config

Et il fait des choses tout seul, en plus du daemon : il poste les rappels a
l'heure dite, souhaite les anniversaires, annonce le resultat des sondages,
fait le point des predictions le dimanche, et tient a jour les EVENEMENTS
Discord du serveur avec les examens (voir la boucle `tic`, en bas).

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
    python assistant.py salons       # une seule fois, cree les salons
    python bot.py

Les commandes slash ne demandent PAS l'intent « contenu des messages ». En
revanche le bot doit avoir ete invite avec le scope applications.commands,
sinon aucune commande n'apparait dans Discord.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import sys
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from uuid import uuid4

import discord
from discord import app_commands
from discord.ext import tasks

import actu
import anniversaires as an
import assistant
import celcat
import config
import changements as chg
import devoirs as dv
import evenements as ev
import image as img
import interface as ui
import maquette as mq
import meteo
import notif
import predictions as pr
import rappels as rp
import sondages as sd
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

DUREE_CHOIX = [app_commands.Choice(name=nom, value=h) for nom, h in sd.DUREES]

QUI_CHOIX = [
    app_commands.Choice(name="moi seulement", value="moi"),
    app_commands.Choice(name="tout le salon (@here)", value="here"),
]

# Ce que /rappel propose pendant la frappe. Le champ reste libre.
SUGGESTIONS_RAPPEL = ["dans 1h", "dans 2h", "ce soir", "demain 9h", "demain 18h",
                      "lundi 8h", "vendredi 12h", "dans 3 jours"]

# Les actions qui repondent TOUJOURS en prive, meme depuis une carte publique :
# la liste de TES rappels n'a rien a faire a la place d'un message de tous.
ACTIONS_PRIVEES = ("rap", "rapsel", "anniv")

# Meme chose, mais pour un seul bouton d'une action : (action, 1er argument).
# Telecharger la maquette ne concerne que celui qui clique, alors que passer
# du semestre 1 au semestre 2 est une navigation ordinaire, qui a le droit de
# reecrire la carte publique.
SOUS_ACTIONS_PRIVEES = {("ects", "dl")}

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
        self._heure_signalee = False

    async def setup_hook(self):
        # Les boutons et menus sont reconstruits a partir de leur identifiant :
        # c'est ce qui les fait survivre aux redemarrages sans rien stocker.
        # UNE SEULE classe ici : le registre est indexe par motif, et nos deux
        # fabriques partagent le leur (voir ComposantCyu).
        self.add_dynamic_items(ComposantCyu)
        if AVEC_DAEMON:
            threading.Thread(target=lancer_daemon, daemon=True,
                             name="daemon-cyu").start()
            print("[i] daemon démarré dans un fil à part")
        # Ce que le bot fait de lui-meme (rappels, anniversaires, sondages,
        # evenements) : une boucle asyncio, pas un fil, puisqu'elle parle a
        # Discord par le client.
        tic.start()

    async def on_ready(self):
        print(f"[i] connecté comme {self.user}", flush=True)
        await self._controler_heure()
        await self._poser_son_nom()
        if not self._commandes_publiees:
            await self._publier_commandes()
            self._commandes_publiees = True

    async def _poser_son_nom(self):
        """Le surnom du bot sur le serveur, depuis config.yaml.

        Une application Discord garde le nom sous lequel on l'a creee — souvent
        celui d'un projet precedent. Ce nom-la se change dans le portail
        developpeur, et Discord n'en accepte que deux par heure ; le SURNOM de
        serveur, lui, est libre et immediat, et c'est celui que la promo lit
        dans chaque message.
        """
        voulu = config.NOM_BOT.strip()[:32]
        if not voulu:
            return
        for serveur in self.guilds:
            moi = serveur.me
            if moi is None or moi.nick == voulu:
                continue
            try:
                await moi.edit(nick=voulu)
                print(f"[i] surnom « {voulu} » posé sur {serveur.name}", flush=True)
            except discord.Forbidden:
                print(f"[!] pas le droit « Changer de pseudo » sur {serveur.name} : "
                      f"le bot reste « {moi.display_name} ».", flush=True)
            except discord.HTTPException as e:
                print(f"[!] surnom refusé sur {serveur.name} : {e}", flush=True)

    async def _controler_heure(self):
        """Une horloge fausse ne se voit nulle part : elle deplace juste toutes
        les heures du bot, sans erreur ni trace. On la verifie au demarrage, la
        seule fois ou quelqu'un regarde la console."""
        ok, lignes = config.controle_heure()
        from datetime import datetime
        if ok:
            print(f"[i] heure : {datetime.now():%d/%m %H:%M} "
                  f"({config.FUSEAU_APPLIQUE or 'heure du serveur'})", flush=True)
            return
        print("[!] " + "\n    ".join(lignes), flush=True)
        if self._heure_signalee:
            return
        self._heure_signalee = True
        await asyncio.to_thread(
            notif.envoyer, "Le bot n'est pas à l'heure",
            ["Tous les horaires de `config.yaml` sont donc décalés : briefings, "
             "heures de silence, rappels.", "```" + "\n".join(lignes) + "```"],
            "devoir", False, "logs")

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


class ComposantCyu(discord.ui.DynamicItem[discord.ui.Item], template=TEMPLATE):
    """Le rattrapage des messages que le bot ne tient plus en memoire.

    Tant qu'une carte vient d'etre envoyee, discord.py garde sa vue et sait a
    quel bouton appartient un clic. Passe un redemarrage, cette vue n'existe
    plus : le clic est alors confie au REGISTRE des composants dynamiques,
    alimente par `add_dynamic_items`.

    Et ce registre est un dictionnaire INDEXE PAR LE MOTIF. BoutonCyu et
    MenuCyu partagent le meme (« cyu:action:args »), donc y inscrire les deux
    n'en gardait qu'un — le dernier. Tous les clics sur un bouton d'un vieux
    message partaient chez MenuCyu, qui lisait `self.item.values` sur un
    bouton, echouait, et ne repondait jamais : « L'interaction a echoue »,
    sans une ligne dans la console du bot (discord.py journalise et passe).

    D'ou cette classe unique, la seule inscrite au registre : elle accepte le
    composant tel que Discord le rend, bouton ou menu, et lit ses valeurs
    seulement s'il en a. BoutonCyu et MenuCyu continuent de CONSTRUIRE les
    cartes — elles seules savent poser un style, un emoji ou des options.
    """

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        composant = cls.__new__(cls)
        discord.ui.DynamicItem.__init__(composant, item)
        composant.action = match["action"]
        composant.args = _decouper_args(match["args"])
        return composant

    async def callback(self, inter: discord.Interaction):
        # `values` n'existe que sur un menu : un bouton n'a rien choisi.
        valeurs = list(getattr(self.item, "values", None) or ())
        await agir(inter, self.action, self.args, valeurs)


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
async def _salon(ident):
    """Un salon par son identifiant, meme s'il n'est pas dans le cache (juste
    apres le demarrage, par exemple). Leve discord.HTTPException s'il n'existe
    pas ou que le bot ne le voit pas."""
    salon = bot.get_channel(int(ident))
    if salon is None:
        salon = await bot.fetch_channel(int(ident))
    return salon


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


def _s(n):
    """Le pluriel, en une lettre."""
    return "s" if n > 1 else ""


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
    if config.EVENEMENTS_EXAMENS:
        boutons.append(_b("Événements Discord", "evenements", emoji="📅"))
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
    if config.EVENEMENTS_EXAMENS:
        lignes.append("-# 📅 Ils sont aussi dans les **événements du serveur** : clique "
                      "sur la cloche « intéressé » et Discord te rappelle chacun.")
    teinte = ("alerte" if premier["jours"] <= 1 else
              "devoir" if premier["jours"] <= 7 else "info")
    sous = (f"{len(liste)} à venir · le premier {vue.jour_relatif(premier['quand'].date())}")
    return carte("Examens — compte à rebours", lignes, teinte, sous_titre=sous,
                 boutons=boutons), []


# --- La maquette : ce que chaque matiere pese --------------------------------
def _semestre_courant():
    """Le semestre en cours : S1 de septembre a janvier, S2 de fevrier a aout.

    Les vraies dates de bascule changent chaque annee et ne sont dans aucun
    fichier qu'on lise. Ce decoupage grossier ne sert qu'a choisir le semestre
    qui s'ouvre en premier ; les deux boutons sont la pour l'autre.
    """
    return 1 if date.today().month in (9, 10, 11, 12, 1) else 2


def _quoi_ects(quoi):
    """« 1 », « 2 » ou « annee ». Tout le reste retombe sur le semestre en cours,
    y compris un bouton d'un vieux message qui aurait porte autre chose."""
    quoi = str(quoi or "").strip().lower()
    if quoi in ("annee", "année", "an", "tout"):
        return "annee"
    return quoi if quoi in ("1", "2") else str(_semestre_courant())


def _sans_maquette(souci):
    """Il n'y a pas de classeur M3C : on dit lequel, et ou le poser.

    Une commande qui repond « erreur » sans dire quoi faire est une commande
    morte : ici l'utilisateur a besoin d'un nom de fichier et d'un dossier.
    """
    return carte(
        "Maquette",
        [f"Je n'ai pas trouvé la maquette de la promo. {souci}",
         "",
         "**Comment la mettre en place**",
         "Dépose le classeur M3C de l'année (le fichier `.xlsx` publié par "
         "l'école) à la racine du bot, à côté de `bot.py`. Tout nom qui "
         "commence par `M3C` est reconnu.",
         "-# Un autre emplacement ? `classe.maquette:` dans `config.yaml` "
         "prend un chemin explicite.",
         "-# Le fichier est relu à chaque changement : une nouvelle version "
         "remplace l'ancienne sans redémarrer le bot."],
        "alerte"), []


def _boutons_ects(quoi, m=None):
    """Les quatre boutons de /ects. Celui qu'on regarde est en bleu — un
    bouton actif qui renvoie a la page affichee doit au moins se signaler."""
    boutons = []
    for s in (m.semestres if m else []):
        n = str(s.numero)
        boutons.append(_b(s.nom, "ects", n, style="bleu" if quoi == n else "gris",
                          emoji="📘"))
    boutons.append(_b("L'année", "ects", "annee",
                      style="bleu" if quoi == "annee" else "gris", emoji="🎓"))
    boutons.append(_b("Télécharger", "ects", "dl", quoi, style="vert", emoji="⬇️"))
    return boutons


def _image_ects(m, quoi):
    """La fonction de dessin de la vue demandee, prete pour _rendu()."""
    if quoi == "annee":
        return lambda chemin: img.rendre_ects_annee(m, chemin)
    return lambda chemin: img.rendre_ects(m, int(quoi), chemin)


def _nom_ects(quoi):
    """Le nom du fichier tel qu'il arrive dans les telechargements de celui qui
    clique : « maquette-semestre-1.png » se retrouve, « image-a3f9.png » non."""
    return "maquette-annee" if quoi == "annee" else f"maquette-semestre-{quoi}"


async def vue_ects(quoi=""):
    """La maquette en photo : les UE, les matieres, les ECTS, les coefficients.

    Huit colonnes de chiffres ne tiennent pas en Markdown sur un telephone —
    d'ou l'image. Le texte reste le repli quand Pillow manque, et il dit alors
    la meme chose, en moins dense.
    """
    try:
        m = await asyncio.to_thread(mq.charger)
    except (mq.MaquetteIntrouvable, OSError) as e:
        return _sans_maquette(e)

    quoi = _quoi_ects(quoi)
    boutons = _boutons_ects(quoi, m)
    if quoi == "annee":
        titre = "Maquette de l'année"
        sous = (f"{m.entete} · {mq.nombre_fr(m.ects)} ECTS · "
                f"{len(m.semestres)} semestres")
    else:
        s = m.semestre(int(quoi))
        if s is None:
            return _sans_maquette(f"Le semestre {quoi} n'y figure pas.")
        evaluees = [e for e in s.matieres if e.evalue]
        titre = f"Maquette — {s.nom}"
        sous = (f"{mq.nombre_fr(s.ects)} ECTS · {len(s.ues)} UE · "
                f"{len(evaluees)} matières · {mq.nombre_fr(s.heures)} h de cours")

    # Le classeur dit lui-meme quand un chiffre est une hypothese : on le
    # repete sous le tableau. Des heures reconstituees presentees comme
    # certaines seraient pires que pas d'heures du tout.
    pied = ("certains volumes horaires du classeur sont des hypothèses, "
            "à vérifier sur le fichier officiel" if m.reserves else None)

    fichier, souci = await _rendu(_image_ects(m, quoi), nom=_nom_ects(quoi))
    if fichier is None:
        # Sans image, le texte : les UE et leurs matieres, sans les colonnes
        # de chiffres qui ne survivent pas a un ecran etroit.
        lignes = []
        for s in (m.semestres if quoi == "annee" else [m.semestre(int(quoi))]):
            lignes += mq.bloc_semestre(s, detail=quoi != "annee") + [""]
        return carte(titre, lignes, "info", sous_titre=sous, boutons=boutons,
                     pied=souci), []
    return carte(titre, [], "info", sous_titre=sous, image=fichier,
                 boutons=boutons, pied=pied), [fichier]


async def vue_ects_telechargement(quoi=""):
    """Le bouton « Télécharger » : la photo en piece jointe, et le classeur.

    Deux fichiers pour deux usages : le PNG se renvoie a quelqu'un dans une
    conversation, le classeur s'ouvre dans Excel ou LibreOffice pour trier,
    filtrer, ou recopier une colonne. Rendre l'un sans l'autre obligerait a
    retourner chercher le fichier a la main.
    """
    try:
        m = await asyncio.to_thread(mq.charger)
    except (mq.MaquetteIntrouvable, OSError) as e:
        return _sans_maquette(e)

    quoi = _quoi_ects(quoi)
    quelle = "l'année entière" if quoi == "annee" else f"le semestre {quoi}"
    de_quelle = "de l'année entière" if quoi == "annee" else f"du semestre {quoi}"
    fichiers, lignes = [], []

    fichier, souci = await _rendu(_image_ects(m, quoi), nom=_nom_ects(quoi))
    if fichier is not None:
        # Un nom fixe, sans le suffixe anti-collision de _rendu : ce fichier
        # part dans les telechargements de quelqu'un, il doit y etre relisible.
        fichier.filename = f"{_nom_ects(quoi)}.png"
        fichiers.append(fichier)
        lignes.append(f"**{fichier.filename}** — le tableau {de_quelle}, en image.")
    else:
        lignes.append(f"⚠️ L'image n'a pas pu être faite : {souci}")

    source = m.source
    if source is not None:
        try:
            octets = await asyncio.to_thread(source.read_bytes)
        except OSError as e:
            lignes.append(f"⚠️ Le classeur n'a pas pu être lu : {e}")
        else:
            # Discord refuse au-dela de 10 Mo sur un serveur non booste, et
            # refuse tout le message avec : mieux vaut l'image seule.
            if len(octets) > 9_000_000:
                lignes.append(f"-# Le classeur `{source.name}` fait "
                              f"{len(octets) // 1_000_000} Mo : trop lourd pour "
                              f"Discord, va le chercher sur le serveur du bot.")
            else:
                fichiers.append(discord.File(io.BytesIO(octets), filename=source.name))
                lignes.append(f"**{source.name}** — le classeur source, à ouvrir "
                              f"dans Excel ou LibreOffice.")

    lignes.append("-# Ces fichiers ne sont visibles que par toi. Clique sur l'un "
                  "d'eux pour l'enregistrer.")
    vue_ = carte("Maquette — à télécharger", lignes, "calme",
                 sous_titre=f"{m.entete} · {quelle}",
                 telechargements=fichiers,
                 boutons=[_b("Revoir le tableau", "ects", quoi, style="bleu",
                             emoji="📊")])
    return vue_, fichiers


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
    # Dans un fil, comme partout ailleurs : ce calcul parcourt tout l'emploi
    # du temps, et une boucle asyncio bloquee, c'est un bot qui ne repond plus
    # a personne — y compris aux boutons des autres.
    s = await asyncio.to_thread(stats.semaine, cours, cette)
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


def _barre_votes(oui, non, largeur=12):
    total = oui + non
    if not total:
        return ""
    plein = int(round(oui / total * largeur))
    return "▰" * plein + "▱" * (largeur - plein) + f" {oui / total * 100:.0f} % y croient"


async def vue_prediction_seule(ident):
    """UNE prediction, comme un sondage : sa carte a elle, ses boutons de
    vote, reecrite sur place a chaque clic. C'est ce que /parier poste dans
    #predictions, et ce qui reste quand elle est tranchee."""
    liste = await asyncio.to_thread(pr.lire)
    p = pr.trouver(liste, ident)
    if p is None:
        return ui.erreur(f"La prédiction #{ident} n'existe plus.", "Introuvable"), []
    oui, non = pr.comptes(p)
    ech = pr.echeance_date(p)
    sous = [f"par **{p['auteur']}**"]
    try:
        sous.append(f"posée le {date.fromisoformat(str(p.get('cree_le', ''))[:10]):%d/%m}")
    except ValueError:
        pass
    if ech:
        sous.append(f"d'ici le {vue.jour_fr(ech, court=True)}")
    elif p.get("echeance_texte"):
        sous.append(f"d'ici « {p['echeance_texte']} »")
    if p.get("mise"):
        sous.append(f"mise : {p['mise']}")

    lignes = [f"### « {p['texte']} »", ""]
    if oui or non:
        lignes += [f"👍 **{oui}** — {pr.noms(p, 'oui', 6) or '—'}",
                   f"👎 **{non}** — {pr.noms(p, 'non', 6) or '—'}",
                   f"-# {_barre_votes(oui, non)}"]
    else:
        lignes.append("-# personne n'a encore voté — à toi")

    resultat = p.get("resultat")
    if resultat:
        justes = pr.noms(p, resultat, 8)
        quand = str(p.get("tranche_le", ""))[:10]
        try:
            quand = f"le {date.fromisoformat(quand):%d/%m}"
        except ValueError:
            quand = ""
        lignes += ["", f"**Tranchée {quand} par {p['auteur']}** — "
                       + (f"avaient raison : {justes}" if justes
                          else "personne n'avait misé du bon côté")]
        titre = ("✅ Réalisée" if resultat == "oui" else "❌ Ratée") + f" — prédiction #{p['id']}"
        boutons = [[_b("Toutes les prédictions", "pred", "page", 0, emoji="🔮"),
                    _b("Classement", "pred", "classement", emoji="🏆"),
                    _b("Parier", "pred", "ajout", style="vert", emoji="🎲")]]
        teinte = "cours" if resultat == "oui" else "alerte"
        pied = "tranchée : les votes sont figés"
    else:
        if pr.en_retard(p):
            lignes += ["", f"⏰ **L'échéance est passée** — {p['auteur']}, tranche : ✔️ ou ✖️"]
        titre = f"🔮 Prédiction #{p['id']}"
        boutons = [[_b(f"Oui ({oui})", "pred", "oui", p["id"], "solo", style="vert", emoji="👍"),
                    _b(f"Non ({non})", "pred", "non", p["id"], "solo", style="rouge", emoji="👎"),
                    _b("C'est arrivé", "pred", "ok", p["id"], "solo", emoji="✔️"),
                    _b("Raté", "pred", "ko", p["id"], "solo", emoji="✖️")],
                   [_b("Toutes les prédictions", "pred", "page", 0, emoji="🔮"),
                    _b("Classement", "pred", "classement", emoji="🏆"),
                    _b("Parier", "pred", "ajout", style="vert", emoji="🎲")]]
        teinte = "devoir" if pr.en_retard(p) else "info"
        pied = "tout le monde vote, seul l'auteur tranche (✔️ ✖️)"
    return carte(titre, lignes, teinte, sous_titre=" · ".join(sous),
                 boutons=boutons, pied=pied), []


async def publier_prediction(inter, p):
    """La carte d'une prediction fraiche : dans #predictions si ce salon est
    configure et qu'on n'y est pas deja, sinon la ou on est. Repond a
    l'interaction dans les deux cas."""
    vue_, _ = await vue_prediction_seule(p["id"])
    salon_id = config.salon_bot("predictions")
    ici = inter.channel.id if inter.channel is not None else None
    if salon_id and salon_id != ici:
        try:
            salon = await _salon(salon_id)
            message = await salon.send(view=vue_)
        except discord.HTTPException as e:
            print(f"[!] impossible de poster dans #predictions ({salon_id}) : {e}",
                  flush=True)
        else:
            await inter.response.send_message(view=carte(
                "Prédiction postée 🔮",
                [f"Elle est dans <#{salon_id}>, avec ses boutons de vote : "
                 f"{message.jump_url}",
                 "-# Tout le monde vote 👍 👎, et c'est toi qui tranches le jour venu."],
                "info", boutons=[_b("Toutes les prédictions", "pred", "page", 0, emoji="🔮")],
                pied=False), ephemeral=True)
            return
    await inter.response.send_message(view=vue_)


async def _apres_tranchage(p, etat, inter):
    """Apres un ✔️ / ✖️ : le refus a celui qui a clique si ce n'est pas son
    pari, ou l'annonce dans #predictions quand ca s'est passe ailleurs.
    Toujours apres un defer : un followup, en texte."""
    if inter is None or p is None:
        return
    if etat == "pas_auteur":
        await inter.followup.send(f"Seul·e **{p['auteur']}** peut trancher la prédiction "
                                  f"#{p['id']} : c'est son pari.", ephemeral=True)
        return
    if etat == "deja":
        await inter.followup.send(f"La prédiction #{p['id']} est déjà tranchée.",
                                  ephemeral=True)
        return
    if etat != "ok":
        return
    salon_id = config.salon_bot("predictions")
    if not salon_id or (inter.channel is not None and inter.channel.id == salon_id):
        return
    realisee = p.get("resultat") == "oui"
    justes = pr.noms(p, p.get("resultat"), 8)
    try:
        salon = await _salon(salon_id)
        await salon.send(view=carte(
            ("✅ Réalisée" if realisee else "❌ Ratée") + f" — prédiction #{p['id']}",
            [f"« {p['texte']} » — de **{p['auteur']}**",
             f"-# avaient raison : {justes}" if justes
             else "-# personne n'avait misé du bon côté"],
            "cours" if realisee else "alerte",
            boutons=[_b("Classement", "pred", "classement", emoji="🏆"),
                     _b("Parier", "pred", "ajout", style="vert", emoji="🎲")],
            pied=False))
    except discord.HTTPException as e:
        print(f"[!] annonce dans #predictions impossible : {e}", flush=True)


def vue_recap_predictions(liste):
    """Le point du dimanche dans #predictions. Rend (carte, [auteurs a
    mentionner]) : ceux qui ont une prediction a trancher."""
    ouvertes = pr.ouvertes(liste)
    retard = [p for p in ouvertes if pr.en_retard(p)]
    prophetes, parieurs, chiffres = pr.classement(liste)
    lignes, mentions = [], []
    if retard:
        lignes += ["### ⏰ À trancher",
                   "-# l'échéance est passée : l'auteur dit si c'est arrivé (✔️ ✖️)"]
        for p in retard[:8]:
            lignes.append(f"• **#{p['id']}** « {p['texte'][:80]} » — <@{p['auteur_id']}>")
            mentions.append(str(p["auteur_id"]))
        lignes.append("")
    if ouvertes:
        lignes.append(f"### 🔮 {len(ouvertes)} en jeu")
        for p in sorted(ouvertes, key=lambda p: -sum(pr.comptes(p)))[:5]:
            oui, non = pr.comptes(p)
            lignes.append(f"• « {p['texte'][:80]} » — {p['auteur']} · 👍 {oui} · 👎 {non}")
        lignes.append("")
    if prophetes or parieurs:
        lignes.append("### 🏆 Le classement")
        for m, (nom, ok, n) in zip(("🥇", "🥈", "🥉"), prophetes[:3]):
            lignes.append(f"{m} **{nom}** — {ok}/{n} réalisée{'s' if ok > 1 else ''}")
        if parieurs:
            nom, ok, n = parieurs[0]
            lignes.append(f"🎯 Meilleur parieur : **{nom}** — {ok}/{n} vote"
                          f"{'s' if n > 1 else ''} juste{'s' if ok > 1 else ''}")
    if not lignes:
        lignes = ["Rien en jeu cette semaine. Quelqu'un ose ? 🎲"]
    boutons = [_b("Parier", "pred", "ajout", style="vert", emoji="🎲"),
               _b("Toutes les prédictions", "pred", "page", 0, emoji="🔮"),
               _b("Classement", "pred", "classement", emoji="🏆")]
    sous = (f"{chiffres['ouvertes']} en jeu · {chiffres['tranchees']} "
            f"tranchée{'s' if chiffres['tranchees'] > 1 else ''}")
    return carte("Le point du dimanche", lignes, "info", sous_titre=sous,
                 boutons=boutons, pied="on vote, l'auteur tranche, le classement juge"), mentions


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
        p = await asyncio.to_thread(pr.ajouter, texte, inter.user.id,
                                    inter.user.display_name, echeance, echeance_texte, mise)
        # Publique, et sur sa propre carte : une prediction est faite pour
        # etre vue et votee, comme un sondage.
        await publier_prediction(inter, p)


# --- Les sondages ------------------------------------------------------------
async def creer_sondage(inter, question, texte_reponses, heures, plusieurs):
    """Un vrai sondage Discord, envoye en reponse a l'interaction, et retenu
    pour en annoncer le resultat a la fin (voir _tic_sondages)."""
    try:
        sondage = sd.construire(question, texte_reponses, heures, plusieurs)
    except ValueError as e:
        await inter.response.send_message(view=ui.erreur(str(e), "Sondage impossible"),
                                          ephemeral=True)
        return
    heures = min(768, max(1, int(heures or config.SONDAGE_DUREE_HEURES)))
    fin = datetime.now() + timedelta(hours=heures)
    contenu = (f"📊 **Sondage** de {inter.user.mention} · {sd.duree_texte(heures)}, "
               f"se termine {discord.utils.format_dt(fin, 'R')}"
               + (" · plusieurs réponses possibles" if plusieurs else ""))
    # Un sondage ne peut pas voyager dans une carte (regle Discord) : c'est un
    # message ordinaire, et Discord dessine le sondage en dessous.
    await inter.response.send_message(contenu, poll=sondage,
                                      allowed_mentions=discord.AllowedMentions.none())
    try:
        message = await inter.original_response()
    except discord.HTTPException:
        return
    await asyncio.to_thread(sd.suivre, message.id, inter.channel_id, fin,
                            sondage.question, inter.user.id)


class ModaleSondage(discord.ui.Modal, title="Un sondage"):
    question = discord.ui.Label(
        text="La question",
        component=discord.ui.TextInput(max_length=sd.QUESTION_MAX,
                                       placeholder="On mange où jeudi ?"))
    reponses = discord.ui.Label(
        text="Les réponses",
        description="une par ligne, ou séparées par « ; » — vide = 👍 Oui / 👎 Non",
        component=discord.ui.TextInput(style=discord.TextStyle.paragraph, required=False,
                                       max_length=600,
                                       placeholder="🍕 Pizza ; 🍔 Burger ; 🥗 Salade"))
    duree = discord.ui.Label(
        text="Durée",
        component=discord.ui.Select(
            options=[discord.SelectOption(label=nom, value=str(h),
                                          default=h == config.SONDAGE_DUREE_HEURES)
                     for nom, h in sd.DUREES],
            min_values=0, max_values=1))
    plusieurs = discord.ui.Label(
        text="Plusieurs réponses par personne ?",
        component=discord.ui.Select(
            options=[discord.SelectOption(label="Non, une seule", value="non", default=True),
                     discord.SelectOption(label="Oui, plusieurs", value="oui")],
            min_values=0, max_values=1))

    async def on_submit(self, inter: discord.Interaction):
        heures = int((self.duree.component.values or [str(config.SONDAGE_DUREE_HEURES)])[0])
        plusieurs = (self.plusieurs.component.values or ["non"])[0] == "oui"
        await creer_sondage(inter, str(self.question.component.value),
                            str(self.reponses.component.value or ""), heures, plusieurs)


# --- Les rappels -------------------------------------------------------------
async def poser_rappel(inter, quand, texte, qui="moi"):
    cours, _ = await _donnees()
    try:
        moment = rp.lire_moment(quand, cours)
    except ValueError as e:
        await inter.response.send_message(view=ui.erreur(str(e), "Quand ?"), ephemeral=True)
        return
    texte = str(texte or "").strip()
    if not texte:
        await inter.response.send_message(
            view=ui.erreur("Un rappel de quoi ? Il manque le texte."), ephemeral=True)
        return
    salon_id = inter.channel_id
    r = await asyncio.to_thread(rp.ajouter, texte, moment, inter.user.id,
                                inter.user.display_name, salon_id, qui)
    lignes = [f"**{texte}**",
              f"-# {vue.jour_fr(moment.date())} à **{moment:%H:%M}** · "
              f"{vue.compte_a_rebours(moment)} · "
              + ("tout le salon sera prévenu (@here)" if qui == "here"
                 else "toi seul seras mentionné")]
    # Un rappel pour tout le monde s'annonce a tout le monde ; le tien ne
    # regarde que toi.
    await inter.response.send_message(view=carte(
        "Rappel posé ⏰", lignes, "devoir", sous_titre=f"ici, dans <#{salon_id}>",
        boutons=[_b("Mes rappels", "rap", "liste", emoji="📋"),
                 _b("Annuler celui-ci", "rap", "suppr", r["id"], emoji="🗑️")],
        pied=False), ephemeral=qui != "here")


async def vue_rappels(user_id, nom=""):
    liste = await asyncio.to_thread(rp.de, user_id)
    boutons = [_b("Nouveau rappel", "rap", "ajout", style="vert", emoji="⏰"),
               _b("Actualiser", "rap", "liste", emoji="🔄")]
    if not liste:
        return carte("Tes rappels", ["Aucun rappel en attente.",
                                     "-# `/rappel quand:demain 9h texte:rendre le TP` "
                                     "— ou le bouton ci-dessous."],
                     "calme", boutons=boutons), []
    lignes, options = [], []
    for r in liste[:15]:
        q = rp.quand(r)
        quand = (f"{vue.jour_fr(q.date(), court=True)} {q:%H:%M} · {vue.compte_a_rebours(q)}"
                 if q else "?")
        ou = f" · <#{r['salon_id']}>" if str(r.get("salon_id", "")).isdigit() else ""
        qui = " · @here" if r.get("qui") == "here" else ""
        lignes.append(f"⏰ **{r['texte']}**\n-# #{r['id']} · {quand}{ou}{qui}")
        options.append((f"#{r['id']} {r['texte']}"[:100], str(r["id"]), quand[:100], "🗑️"))
    menu = ui.Menu("rapsel", "Annuler un rappel…", options[:25])
    return carte("Tes rappels", lignes, "devoir",
                 sous_titre=f"{len(liste)} en attente" + (f" — {nom}" if nom else ""),
                 boutons=boutons, menus=[menu], pied="toi seul vois cette liste"), []


class ModaleRappel(discord.ui.Modal, title="Un rappel"):
    quand = discord.ui.Label(
        text="Quand ?",
        description="demain 9h · dans 2h · lundi 14h · 12/10 8h30 · ce soir",
        component=discord.ui.TextInput(max_length=40, placeholder="demain 9h"))
    texte = discord.ui.Label(
        text="De quoi ?",
        component=discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=400,
                                       placeholder="rendre le TP de VBA"))
    qui = discord.ui.Label(
        text="Qui prévenir ?",
        component=discord.ui.Select(
            options=[discord.SelectOption(label="Moi seulement", value="moi", default=True),
                     discord.SelectOption(label="Tout le salon (@here)", value="here")],
            min_values=0, max_values=1))

    async def on_submit(self, inter: discord.Interaction):
        await poser_rappel(inter, str(self.quand.component.value),
                           str(self.texte.component.value),
                           (self.qui.component.values or ["moi"])[0])


# --- Les anniversaires -------------------------------------------------------
async def vue_anniversaires(user_id=None):
    data = await asyncio.to_thread(an.lire)
    auj = date.today()
    liste = an.prochains(12, auj, data)
    boutons = [_b("Ajouter le mien", "anniv", "ajout", style="vert", emoji="🎂")]
    if user_id is not None and str(user_id) in data:
        boutons.append(_b("Retirer le mien", "anniv", "retirer", emoji="🗑️"))
    boutons.append(_b("Actualiser", "anniv", "liste", emoji="🔄"))
    pied = (f"le bot les souhaite à {config.ANNIVERSAIRES_HEURE} dans #annonces"
            if config.ANNIVERSAIRES else "les vœux automatiques sont désactivés")
    if not liste:
        return carte("Anniversaires", ["Personne n'a encore donné le sien.",
                                       "-# `/anniversaire quand:12/10` — et le bot le "
                                       "souhaite le jour J."],
                     "calme", boutons=boutons, pied=pied), []
    lignes = []
    for d, uid, e in liste:
        ecart = (d - auj).days
        age = an.age_le(e, d)
        quand = ("**AUJOURD'HUI** 🎉" if ecart == 0 else "**demain**" if ecart == 1
                 else f"dans {ecart} j")
        lignes.append(f"🎂 **{e['nom']}** — {an.libelle(e)} · {quand}"
                      + (f" · {age} ans" if age else ""))
    return carte("Anniversaires", lignes, "info",
                 sous_titre=f"{len(data)} dans la promo · les prochains",
                 boutons=boutons, pied=pied), []


async def definir_anniversaire(inter, texte):
    try:
        jour, mois, annee = an.lire_date(texte)
    except ValueError as e:
        await inter.response.send_message(view=ui.erreur(str(e), "Date incomprise"),
                                          ephemeral=True)
        return
    e = await asyncio.to_thread(an.definir, inter.user.id, inter.user.display_name,
                                jour, mois, annee)
    d = an.prochaine_date(e)
    ecart = (d - date.today()).days
    age = an.age_le(e, d)
    quand = "c'est aujourd'hui ! 🎉" if ecart == 0 else f"dans {ecart} jour{'s' if ecart > 1 else ''}"
    lignes = [f"**{e['nom']}** — {an.libelle(e)}, {quand}" + (f" ({age} ans)" if age else "")]
    if config.ANNIVERSAIRES:
        lignes.append(f"-# Le bot le souhaitera à {config.ANNIVERSAIRES_HEURE} dans #annonces.")
    await inter.response.send_message(view=carte(
        "Anniversaire noté 🎂", lignes, "info",
        boutons=[_b("Les prochains", "anniv", "liste", emoji="🎂")], pied=False))


class ModaleAnniversaire(discord.ui.Modal, title="Ton anniversaire"):
    quand = discord.ui.Label(
        text="Ta date de naissance",
        description="12/10 — ou 12/10/2005 pour que le bot dise ton âge",
        component=discord.ui.TextInput(max_length=30, placeholder="12/10/2005"))

    async def on_submit(self, inter: discord.Interaction):
        await definir_anniversaire(inter, str(self.quand.component.value))


# --- Les examens en evenements Discord ---------------------------------------
def _serveur():
    """Le serveur de la promo : celui d'un salon configure, sinon le seul ou
    le bot se trouve. None si on ne peut pas trancher."""
    for canal in ("commandes", "predictions", "annonces", "devoirs", "alertes", "edt"):
        ident = config.salon_bot(canal)
        salon = bot.get_channel(ident) if ident else None
        if salon is not None and getattr(salon, "guild", None) is not None:
            return salon.guild
    if config.SERVEUR_ID.isdigit():
        serveur = bot.get_guild(int(config.SERVEUR_ID))
        if serveur is not None:
            return serveur
    return bot.guilds[0] if len(bot.guilds) == 1 else None


async def synchroniser_evenements(serveur):
    """Cree, met a jour et retire les evenements Discord pour que le
    calendrier du serveur dise la meme chose que CELCAT et le carnet.
    Rend (crees, mis_a_jour, retires). Leve discord.Forbidden si le bot n'a
    pas « Gerer les evenements »."""
    cours, liste_devoirs = await _donnees()
    voulus = await asyncio.to_thread(ev.voulus, cours, liste_devoirs)
    connus = await asyncio.to_thread(ev.lire)
    a_creer, a_modifier, a_retirer = ev.plan(voulus, connus)
    crees = maj = retires = 0

    for cle in a_modifier:
        e = voulus[cle]
        try:
            obj = await serveur.fetch_scheduled_event(int(connus[cle]["id"]))
            await obj.edit(name=e["nom"], start_time=e["debut"], end_time=e["fin"],
                           location=e["lieu"], description=e["description"])
        except discord.NotFound:
            a_creer.append(cle)              # supprime a la main : on le refait
            continue
        connus[cle] = {"id": connus[cle]["id"], "empreinte": e["empreinte"]}
        maj += 1

    for cle in a_creer:
        e = voulus[cle]
        obj = await serveur.create_scheduled_event(
            name=e["nom"], start_time=e["debut"], end_time=e["fin"],
            entity_type=discord.EntityType.external,
            privacy_level=discord.PrivacyLevel.guild_only,
            location=e["lieu"], description=e["description"],
            reason="examen connu de l'assistant CYU")
        connus[cle] = {"id": str(obj.id), "empreinte": e["empreinte"]}
        crees += 1

    for cle in a_retirer:
        try:
            obj = await serveur.fetch_scheduled_event(int(connus[cle]["id"]))
            await obj.delete(reason="examen disparu de CELCAT ou du carnet")
        except discord.NotFound:
            pass                              # deja parti : c'est le but
        connus.pop(cle, None)
        retires += 1

    await asyncio.to_thread(ev.ecrire, connus)
    return crees, maj, retires


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
               + ("" if AVEC_DAEMON else " (AVEC_DAEMON = False dans bot.py)")]

    # L'heure : une horloge fausse decale tout sans rien casser, donc sans
    # jamais se signaler. C'est la ligne a lire avant d'accuser le daemon.
    heure_ok, heure_lignes = config.controle_heure()
    if heure_ok:
        lignes.append(f"**L'heure** — 🟢 {datetime.now():%H:%M} · "
                      f"{config.FUSEAU_APPLIQUE or 'heure du serveur'}")
    else:
        lignes.append("**L'heure** — 🔴 " + heure_lignes[-3]
                      + f"\n-# {heure_lignes[-1]}")
    try:
        n_r, n_s, n_e, n_a = await asyncio.to_thread(
            lambda: (len(rp.lire()), len(sd.lire()), len(ev.lire()), len(an.lire())))
        lignes.append(f"**La promo** — {n_r} rappel{_s(n_r)} en attente · {n_s} sondage"
                      f"{_s(n_s)} suivi{_s(n_s)} · {n_e} examen{_s(n_e)} en événements · "
                      f"{n_a} anniversaire{_s(n_a)} noté{_s(n_a)}"
                      + ("" if tic.is_running() else " · ⚠️ la boucle du bot est arrêtée"))
    except Exception as e:                      # noqa: BLE001 - filet volontaire
        lignes.append(f"**La promo** — ⚠️ `{type(e).__name__}: {e}`"[:300])
    lignes += ["", "**Les salons**"]
    for canal in config.CANAUX:
        mode, cible = notif.destination(canal)
        if canal in config.CANAUX_BOT_SEULEMENT:
            ou = (f"<#{cible}>" if config.salon_bot(canal)
                  else "— pas de salon dédié (les prédictions restent où on les pose)")
        else:
            ou = f"<#{cible}>" if mode == "bot" else "webhook"
            if not notif.salon_configure(canal):
                ou += " ⚠️ non configuré, retombe sur le secours"
        lignes.append(f"`{canal:11s}` {ou}")
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
             "`/examens` `/ects` `/sondage` `/rappel` — et `/help` pour tout voir"]
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
         _b("Parier", "pred", "ajout", style="vert", emoji="🎲"),
         _b("Sondage", "son", "ajout", style="vert", emoji="📊")],
        # La maquette n'apparait que si le classeur M3C est la : un bouton qui
        # ne sait que s'excuser n'a rien a faire sur le panneau.
        ([_b("ECTS et coefficients", "pan", "ects", emoji="📘")]
         if mq.disponible() else []),
        [_b("Rappel", "rap", "ajout", emoji="⏰"),
         _b("Mes rappels", "pan", "rappels", emoji="📋"),
         _b("Anniversaires", "pan", "anniv", emoji="🎂"),
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
        "`/ects` — **la maquette de la promo en photo** : chaque UE, chaque "
        "matière, ce qu'elle vaut en ECTS, son coefficient, ses heures et la "
        "façon dont elle est évaluée (contrôle continu ou examen terminal). "
        "Un bouton **⬇️ Télécharger** rend l'image et le classeur source",
        "`/prediction` — **vos prédictions** 🔮 : qui va valider l'année, quel cours "
        "va sauter… chacun vote 👍👎, l'auteur tranche, le classement juge. "
        "`/parier` pour en poser une. Les paris de l'assistant 🤖 (chiffres réels) "
        "sont derrière un bouton",
        "`/clear` — vider ce salon (les épinglés sont gardés) — droit « gérer les "
        "messages » requis",
        "",
        "### La promo",
        "`/sondage` — **un vrai sondage Discord** : `question:`, `reponses:` "
        "(« 🍕 Pizza ; 🍔 Burger », vide = Oui / Non), la durée, plusieurs réponses "
        "ou pas. **Le résultat est annoncé à la fin**",
        "`/rappel quand: texte:` — « demain 9h », « dans 2h », « lundi 14h »… le bot "
        "te mentionne ici à l'heure dite. `qui: tout le salon` pour un @here",
        "`/rappels` — tes rappels en attente, et de quoi en annuler un",
        "`/anniversaire quand:12/10` — le tien (avec l'année, il dira ton âge) · "
        "`/anniversaires` — les prochains de la promo",
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
    if config.ANNIVERSAIRES:
        lignes.append(f"`{config.ANNIVERSAIRES_HEURE}` **#annonces** — 🎂 joyeux "
                      f"anniversaire à qui l'a donné avec `/anniversaire`")
    if config.SONDAGE_RESULTATS:
        lignes.append("`à la fin de chaque sondage` — 📊 le résultat, en réponse au sondage")
    if config.EVENEMENTS_EXAMENS:
        lignes.append("`toutes les heures` — 📅 chaque examen (CELCAT ou carnet) devient "
                      "un **événement Discord** du serveur, avec sa cloche « intéressé »")
    if config.RECAP_PREDICTIONS and config.salon_bot("predictions"):
        lignes.append(f"`{jour_recap} {config.RECAP_SEMAINE_HEURE}` **#predictions** — "
                      f"🔮 le point : ce qu'il reste à trancher, et le classement")
    lignes.append("`à l'heure dite` — ⏰ chaque rappel posé avec `/rappel`, là où il "
                  "a été posé")
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


# Les boutons qui ouvrent un FORMULAIRE : un formulaire doit etre la premiere
# reponse a l'interaction, donc pas de defer, pas de carte, rien avant.
FORMULAIRES = {
    "dev": lambda args: ModaleDevoir(args[1] if len(args) > 1 else "devoir"),
    "pred": lambda args: ModalePrediction(),
    "son": lambda args: ModaleSondage(),
    "rap": lambda args: ModaleRappel(),
    "anniv": lambda args: ModaleAnniversaire(),
}


async def _agir(inter, args, valeurs, action):
    if args[:1] == ["ajout"] and action in FORMULAIRES:
        await inter.response.send_modal(FORMULAIRES[action](args))
        return
    if action == "clear":
        await _nettoyer(inter, args)
        return

    if action in ("pan", "jour"):
        await inter.response.defer(ephemeral=True, thinking=True)
        vue_, fichiers = await _vue_panneau(args, valeurs, inter)
        await repondre(inter, vue_, fichiers, ephemere=True)
        return

    # Ce qui ne regarde que celui qui clique (ses rappels, son anniversaire)
    # repond en prive, meme depuis une carte publique : on ne remplace pas un
    # message de tous par la liste de quelqu'un.
    if action in ACTIONS_PRIVEES or \
            (action, args[0] if args else "") in SOUS_ACTIONS_PRIVEES:
        await inter.response.defer(ephemeral=True, thinking=True)
        vue_, fichiers = await _vue_navigation(action, args, valeurs, inter)
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
    if quoi == "ects":
        return await vue_ects()
    if quoi == "comparer":
        return await vue_comparer(celcat.semaine_de(auj))
    if quoi == "prediction":
        return await vue_predictions(0)
    if quoi == "anniv":
        return await vue_anniversaires(inter.user.id if inter is not None else None)
    if quoi == "rappels":
        return await vue_rappels(inter.user.id if inter is not None else 0,
                                 inter.user.display_name if inter is not None else "")
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
    if action == "ects":
        if args[:1] == ["dl"]:
            return await vue_ects_telechargement(args[1] if len(args) > 1 else "")
        return await vue_ects(args[0] if args else "")
    if action == "paris":
        return await vue_paris_assistant()
    if action == "pred":
        quoi = args[0] if args else "page"
        # « solo » : le bouton est sur la carte d'UNE prediction (celle de
        # #predictions), pas sur la liste — on reecrit cette carte-la.
        solo = len(args) > 2 and args[2] == "solo"
        if quoi in ("oui", "non") and len(args) > 1:
            if inter is not None:
                await asyncio.to_thread(pr.voter, args[1], inter.user.id,
                                        inter.user.display_name, quoi)
            if solo:
                return await vue_prediction_seule(args[1])
            page = args[2] if len(args) > 2 else "0"
            return await vue_predictions(int(page) if page.isdigit() else 0)
        if quoi in ("ok", "ko") and len(args) > 1:
            if inter is not None:
                p, etat = await asyncio.to_thread(pr.trancher, args[1],
                                                  "oui" if quoi == "ok" else "non",
                                                  inter.user.id)
                await _apres_tranchage(p, etat, inter)
            if solo:
                return await vue_prediction_seule(args[1])
            return await vue_predictions(0)
        if quoi == "solo" and len(args) > 1:
            return await vue_prediction_seule(args[1])
        if quoi == "closes":
            return await vue_predictions_closes()
        if quoi == "classement":
            return await vue_classement()
        page = args[1] if quoi == "page" and len(args) > 1 else "0"
        return await vue_predictions(int(page) if page.lstrip("-").isdigit() else 0)
    if action == "predsel":
        for valeur in valeurs if inter is not None else []:
            op, _, ident = str(valeur).partition(":")
            if op in ("ok", "ko"):
                p, etat = await asyncio.to_thread(pr.trancher, ident,
                                                  "oui" if op == "ok" else "non",
                                                  inter.user.id)
                await _apres_tranchage(p, etat, inter)
            elif op == "suppr":
                p, etat = await asyncio.to_thread(pr.supprimer, ident, inter.user.id)
                if etat == "pas_auteur":
                    # Apres un defer, un followup n'accepte que du texte.
                    await inter.followup.send(
                        f"#{ident} n'est pas à toi : seul l'auteur supprime.", ephemeral=True)
        return await vue_predictions(0)
    if action == "evenements":
        if inter is not None and inter.guild is not None and config.EVENEMENTS_EXAMENS:
            try:
                crees, maj, retires = await synchroniser_evenements(inter.guild)
            except discord.Forbidden:
                texte = ("❌ Le bot n'a pas le droit **« Gérer les événements »** sur ce "
                         "serveur (Paramètres du serveur → Rôles → le rôle du bot).")
            else:
                texte = (f"📅 Événements Discord : **{crees}** créé{_s(crees)}, **{maj}** "
                         f"mis à jour, **{retires}** retiré{_s(retires)}."
                         + (" Tout était déjà à jour." if not (crees or maj or retires)
                            else ""))
            await inter.followup.send(texte, ephemeral=True)
        return await vue_examens()
    if action == "rap":
        quoi = args[0] if args else "liste"
        uid = inter.user.id if inter is not None else 0
        nom = inter.user.display_name if inter is not None else ""
        if quoi == "suppr" and len(args) > 1 and inter is not None:
            _, etat = await asyncio.to_thread(rp.supprimer, args[1], uid)
            if etat == "pas_auteur":
                await inter.followup.send("Ce rappel n'est pas à toi.", ephemeral=True)
        return await vue_rappels(uid, nom)
    if action == "rapsel":
        uid = inter.user.id if inter is not None else 0
        for ident in valeurs:
            await asyncio.to_thread(rp.supprimer, ident, uid)
        return await vue_rappels(uid, inter.user.display_name if inter is not None else "")
    if action == "anniv":
        quoi = args[0] if args else "liste"
        uid = inter.user.id if inter is not None else None
        if quoi == "retirer" and uid is not None:
            await asyncio.to_thread(an.retirer, uid)
        return await vue_anniversaires(uid)
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


@bot.tree.command(name="ects",
                  description="La maquette en photo : UE, matières, ECTS, coefficients, évaluation")
@app_commands.describe(
    semestre="lequel — le semestre en cours par défaut, ou l'année entière")
@app_commands.choices(semestre=[
    app_commands.Choice(name="Semestre 1", value="1"),
    app_commands.Choice(name="Semestre 2", value="2"),
    app_commands.Choice(name="L'année entière (les UE, sans le détail)", value="annee"),
])
async def cmd_ects(inter: discord.Interaction, semestre: str = ""):
    await _commande(inter, vue_ects(semestre))


@bot.tree.command(name="prediction",
                  description="Vos prédictions : qui va valider, quel cours va sauter… votez !")
async def cmd_prediction(inter: discord.Interaction):
    await _commande(inter, vue_predictions(0))


@bot.tree.command(name="parier", description="Poser une prédiction (un formulaire s'ouvre)")
async def cmd_parier(inter: discord.Interaction):
    await inter.response.send_modal(ModalePrediction())


@bot.tree.command(name="sondage",
                  description="Un vrai sondage Discord : une question, des réponses, tout le monde vote")
@app_commands.describe(
    question="la question",
    reponses="séparées par « ; » : 🍕 Pizza ; 🍔 Burger — vide = Oui / Non",
    duree=f"combien de temps il reste ouvert ({sd.duree_texte(config.SONDAGE_DUREE_HEURES)} par défaut)",
    plusieurs="chacun peut cocher plusieurs réponses")
@app_commands.choices(duree=DUREE_CHOIX)
async def cmd_sondage(inter: discord.Interaction, question: str, reponses: str = "",
                      duree: int = 0, plusieurs: bool = False):
    await creer_sondage(inter, question, reponses, duree or config.SONDAGE_DUREE_HEURES,
                        plusieurs)


@cmd_sondage.autocomplete("reponses")
async def auto_reponses(inter: discord.Interaction, saisie: str):
    """Des jeux de reponses tout prets ; ce que tu tapes reste valable."""
    bas = celcat.normaliser(saisie)
    sortie = []
    if saisie.strip():
        sortie.append(app_commands.Choice(name=f"➜ {saisie.strip()}"[:100],
                                          value=saisie.strip()[:100]))
    for nom, valeur in sd.MODELES:
        if not bas or bas in celcat.normaliser(nom) or bas in celcat.normaliser(valeur):
            sortie.append(app_commands.Choice(name=nom, value=valeur))
    return sortie[:25]


@bot.tree.command(name="rappel", description="Rappelle-moi : « demain 9h », « dans 2h », « lundi 14h »…")
@app_commands.describe(
    quand="demain 9h · dans 2h · lundi 14h · 12/10 8h30 · ce soir · prochain:maths",
    texte="de quoi te rappeler",
    qui="toi seul (par défaut), ou tout le salon avec @here")
@app_commands.choices(qui=QUI_CHOIX)
async def cmd_rappel(inter: discord.Interaction, quand: str, texte: str, qui: str = "moi"):
    await poser_rappel(inter, quand, texte, qui)


@cmd_rappel.autocomplete("quand")
async def auto_rappel_quand(inter: discord.Interaction, saisie: str):
    sortie = []
    if saisie.strip():
        try:
            moment = rp.lire_moment(saisie)
            sortie.append(app_commands.Choice(
                name=f"➜ {vue.jour_fr(moment.date())} à {moment:%H:%M}"[:100],
                value=saisie.strip()[:100]))
        except ValueError:
            pass
    bas = celcat.normaliser(saisie)
    for s in SUGGESTIONS_RAPPEL:
        if not bas or bas in celcat.normaliser(s):
            sortie.append(app_commands.Choice(name=s, value=s))
    return sortie[:25]


@bot.tree.command(name="rappels", description="Tes rappels en attente")
async def cmd_rappels(inter: discord.Interaction):
    await _commande(inter, vue_rappels(inter.user.id, inter.user.display_name),
                    ephemere=True)


@bot.tree.command(name="anniversaire",
                  description="Donner ton anniversaire : le bot le souhaite le jour J")
@app_commands.describe(
    quand="12/10 · 12/10/2005 (avec l'année, il dira ton âge) · 12 octobre",
    retirer="oui = te retirer de la liste")
async def cmd_anniversaire(inter: discord.Interaction, quand: str = "", retirer: bool = False):
    if retirer:
        ok = await asyncio.to_thread(an.retirer, inter.user.id)
        await inter.response.send_message(
            view=carte("Anniversaire retiré" if ok else "Rien à retirer",
                       ["Tu n'es plus dans la liste." if ok else "Tu n'y étais pas."],
                       "calme", pied=False), ephemeral=True)
        return
    if not quand.strip():
        await _commande(inter, vue_anniversaires(inter.user.id), ephemere=True)
        return
    await definir_anniversaire(inter, quand)


@bot.tree.command(name="anniversaires", description="Les prochains anniversaires de la promo")
async def cmd_anniversaires(inter: discord.Interaction):
    await _commande(inter, vue_anniversaires(inter.user.id))


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


# =============================================================================
# Ce que le bot fait tout seul : la boucle `tic`
# =============================================================================
# Le daemon d'assistant.py parle par webhooks et ne connait pas les boutons.
# Tout ce qui a besoin du CLIENT Discord (une carte a boutons dans un salon,
# relire un sondage, creer un evenement) passe par ici, toutes les 30 s.
# Chaque tache est independante : l'une qui echoue n'empeche pas les autres.
def _etat_bot():
    try:
        data = json.loads(config.FICHIER_ETAT_BOT.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def _deja(cle):
    return cle in (_etat_bot().get("envoyes") or {})


def _marquer(cle):
    """Memorise un envoi (anti-doublon a travers les redemarrages), et purge
    ce qui a plus de 30 jours."""
    etat = _etat_bot()
    envoyes = etat.setdefault("envoyes", {})
    envoyes[cle] = datetime.now().isoformat(timespec="seconds")
    limite = (datetime.now() - timedelta(days=30)).isoformat()
    etat["envoyes"] = {k: v for k, v in envoyes.items() if v >= limite}
    config.preparer_dossiers()
    config.FICHIER_ETAT_BOT.write_text(json.dumps(etat, ensure_ascii=False, indent=1),
                                       encoding="utf-8")


async def _tic_rappels(maintenant):
    """Les rappels dont l'heure est passee : postes la ou ils ont ete poses,
    en mentionnant qui il faut. Un salon disparu ne bloque pas les autres."""
    for r in await asyncio.to_thread(rp.echus, maintenant):
        here = r.get("qui") == "here"
        mention = "@here" if here else f"<@{r['auteur_id']}>"
        q = rp.quand(r)
        lignes = [f"{mention} — **{r['texte']}**",
                  f"-# rappel posé par {r['auteur']}" + (f" pour {q:%H:%M}" if q else "")]
        try:
            salon = await _salon(r["salon_id"])
            await salon.send(
                view=carte("⏰ Rappel", lignes, "devoir",
                           boutons=[_b("Nouveau rappel", "rap", "ajout", style="vert",
                                       emoji="⏰")], pied=False),
                allowed_mentions=discord.AllowedMentions(users=True, everyone=here))
        except discord.HTTPException as e:
            abandonne = await asyncio.to_thread(rp.echec, r["id"])
            print(f"[!] rappel #{r['id']} non envoyé ({e})"
                  f"{' — abandonné' if abandonne else ''}", flush=True)
            continue
        await asyncio.to_thread(rp.retirer, r["id"])


async def _tic_anniversaires(maintenant):
    if not config.ANNIVERSAIRES:
        return
    moment = vue.a_heure(maintenant.date(), config.ANNIVERSAIRES_HEURE, (8, 0))
    if not assistant._du(moment, maintenant, fenetre_min=10):
        return
    for uid, e in await asyncio.to_thread(an.du_jour, maintenant.date()):
        cle = f"anniv:{maintenant.date()}:{uid}"
        if _deja(cle):
            continue
        age = an.age_le(e, maintenant.date())
        corps = [f"C'est l'anniversaire de <@{uid}> aujourd'hui"
                 + (f" — **{age} ans** !" if age else " !"),
                 "", "Un petit mot, un gâteau à la pause, une pensée. 🎉"]
        # Par le daemon-notif (webhook ou bot, selon config) : c'est #annonces,
        # comme les briefings, et la mention part dans le contenu du message.
        ok = await asyncio.to_thread(notif.envoyer, "🎂 Joyeux anniversaire !", corps,
                                     "cours", False, "annonces", None, [uid])
        if ok:
            _marquer(cle)
            print(f"{maintenant:%H:%M} | anniversaire de {e.get('nom')} souhaité", flush=True)


async def _tic_sondages(maintenant):
    """Les sondages du bot dont la fin est passee : on relit le message, et si
    Discord a clos le sondage, on poste le resultat en reponse."""
    if not config.SONDAGE_RESULTATS:
        return
    for s in await asyncio.to_thread(sd.a_relever, maintenant):
        try:
            salon = await _salon(s["salon_id"])
            message = await salon.fetch_message(int(s["message_id"]))
        except discord.HTTPException:
            await asyncio.to_thread(sd.oublier, s["message_id"])     # supprime
            continue
        sondage = message.poll
        if sondage is None:
            await asyncio.to_thread(sd.oublier, s["message_id"])
            continue
        if not sondage.is_finalised():
            # Discord cloture avec un peu de retard (parfois une heure) : on
            # relira plus tard, pas a chaque tour, et pas eternellement.
            if sd.perime(s, maintenant):
                await asyncio.to_thread(sd.oublier, s["message_id"])
            else:
                await asyncio.to_thread(sd.marquer_lecture, s["message_id"], maintenant)
            continue
        lignes, gagnant = sd.resultat(sondage)
        if gagnant:
            lignes = [f"### 🏆 {gagnant}", ""] + lignes
        elif sondage.total_votes:
            lignes = ["### 🤝 Égalité", ""] + lignes
        n = sondage.total_votes
        try:
            await salon.send(
                view=carte("📊 Résultat du sondage", lignes, "info",
                           sous_titre=f"« {sondage.question} »",
                           boutons=[_b("Un autre sondage", "son", "ajout", style="vert",
                                       emoji="📊")],
                           pied=f"{n} vote{_s(n)}"),
                reference=message, mention_author=False)
        except discord.HTTPException as e:
            print(f"[!] résultat du sondage {s['message_id']} non posté : {e}", flush=True)
        await asyncio.to_thread(sd.oublier, s["message_id"])


async def _tic_recap_predictions(maintenant):
    """Le dimanche, dans #predictions : ce qu'il reste a trancher, et le
    classement. Meme jour et meme heure que le recap de la semaine."""
    if not config.RECAP_PREDICTIONS:
        return
    salon_id = config.salon_bot("predictions")
    if not salon_id or maintenant.weekday() != config.RECAP_SEMAINE_JOUR:
        return
    cle = f"recap-pred:{maintenant.date()}"
    moment = vue.a_heure(maintenant.date(), config.RECAP_SEMAINE_HEURE, (18, 0))
    if _deja(cle) or not assistant._du(moment, maintenant, fenetre_min=10):
        return
    liste = await asyncio.to_thread(pr.lire)
    if not liste:
        _marquer(cle)
        return
    vue_, mentions = vue_recap_predictions(liste)
    salon = await _salon(salon_id)
    await salon.send(view=vue_, allowed_mentions=discord.AllowedMentions(
        users=[discord.Object(id=int(u)) for u in mentions if str(u).isdigit()]))
    _marquer(cle)


_DERNIERE_SYNC = [None]
_EVENEMENTS_REFUSES = [None]


async def _tic_evenements(maintenant):
    """Toutes les heures : les examens dans les evenements du serveur."""
    if not config.EVENEMENTS_EXAMENS:
        return
    derniere = _DERNIERE_SYNC[0]
    if derniere is not None and maintenant - derniere < timedelta(hours=1):
        return
    _DERNIERE_SYNC[0] = maintenant
    serveur = _serveur()
    if serveur is None:
        return
    try:
        crees, maj, retires = await synchroniser_evenements(serveur)
    except discord.Forbidden:
        # Une fois par jour, pas a chaque heure : le message est le meme.
        if _EVENEMENTS_REFUSES[0] is None or \
                maintenant - _EVENEMENTS_REFUSES[0] > timedelta(hours=24):
            _EVENEMENTS_REFUSES[0] = maintenant
            print("[!] examens → événements : le bot n'a pas « Gérer les événements »",
                  flush=True)
            await asyncio.to_thread(
                notif.envoyer, "Examens en événements : refusé",
                ["Le bot n'a pas le droit **« Gérer les événements »** sur le serveur.",
                 "Donne-le-lui (Paramètres du serveur → Rôles → le rôle du bot), ou "
                 "mets `classe.evenements_examens: false` dans config.yaml."],
                "devoir", False, "logs")
        return
    if crees or maj or retires:
        print(f"{maintenant:%H:%M} | événements : {crees} créés, {maj} mis à jour, "
              f"{retires} retirés", flush=True)


TACHES = (("rappels", _tic_rappels), ("anniversaires", _tic_anniversaires),
          ("sondages", _tic_sondages), ("predictions", _tic_recap_predictions),
          ("evenements", _tic_evenements))


@tasks.loop(seconds=30)
async def tic():
    maintenant = datetime.now()
    for nom, tache in TACHES:
        try:
            await tache(maintenant)
        except Exception:                       # noqa: BLE001 - filet volontaire
            print(f"[!] tâche « {nom} » :", flush=True)
            traceback.print_exc()


@tic.before_loop
async def _avant_tic():
    await bot.wait_until_ready()


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
