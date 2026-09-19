#!/usr/bin/env python3
"""
L'allure du bot : une seule facon de repondre, pour toutes les commandes.

Discord sait aujourd'hui afficher bien mieux qu'un embed : un CONTENEUR avec
une barre de couleur, du texte mis en forme, une image integree au meme bloc,
un trait de separation, et des boutons ou des menus juste en dessous. C'est
ce qu'on appelle les « components v2 », et c'est ce que ce module construit.

Ce qu'on y gagne, concretement :

  * la photo et son titre sont UN SEUL bloc, pas un texte suivi d'une piece
    jointe qu'on lit deux fois ;
  * chaque reponse porte ses propres boutons : « ◀ hier · demain ▶ » sous une
    journee, « semaine precedente · suivante » sous les stats. On navigue au
    lieu de retaper ;
  * ces boutons SURVIVENT aux redemarrages du bot : leur identifiant contient
    tout ce qu'il faut pour rejouer l'action (« cyu:edt:j:2026-10-12 »), donc
    un message d'il y a trois semaines marche encore.

Tout le monde passe par carte() : bot.py ne connait ni Container ni
TextDisplay, il donne un titre, des lignes, une couleur, eventuellement une
image et des boutons, et recoit une vue prete a envoyer.

Limites Discord a garder en tete (elles sont appliquees ici, pas ailleurs) :
40 composants par message, 4000 caracteres de texte au total, 5 boutons par
rangee, 25 options par menu.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import discord

import notif

# Un prefixe pour tous nos identifiants de composants : un bouton d'un autre
# bot sur le meme serveur ne doit jamais tomber chez nous par accident.
PREFIXE = "cyu"

# La longueur maximale d'un bloc de texte, un peu sous la limite Discord pour
# laisser la place au titre et au pied.
TEXTE_MAX = 3600


def couleur(nom):
    """La couleur Discord d'un type de message : la meme que les embeds du
    daemon (notif.COULEURS), pour que tout ait l'air de venir du meme endroit."""
    return discord.Colour(notif.COULEURS.get(nom, notif.COULEURS["info"]))


def identifiant(action, *args):
    """« cyu:edt:j:2026-10-12 » — l'identifiant d'un bouton, qui dit tout ce
    qu'il faut pour rejouer l'action apres un redemarrage."""
    morceaux = [PREFIXE, action] + [str(a) for a in args if a is not None]
    return ":".join(morceaux)[:100]      # limite Discord sur un custom_id


@dataclass
class Bouton:
    """Ce que bot.py sait d'un bouton : un libelle, une action, un style."""
    libelle: str
    action: str
    args: tuple = ()
    style: str = "gris"          # gris · bleu · vert · rouge
    emoji: str | None = None
    inactif: bool = False
    # Pose par la fabrique quand deux boutons d'une meme carte visent la meme
    # action : « ◀ » depuis demain et « Aujourd'hui » menent au meme jour, et
    # Discord refuse deux identifiants identiques dans un message. Le suffixe
    # commence par « _ » : bot.py l'ignore en relisant les arguments.
    suffixe: str = ""

    @property
    def custom_id(self):
        base = identifiant(self.action, *self.args)
        return (base + self.suffixe)[:100] if self.suffixe else base


STYLES = {
    "gris": discord.ButtonStyle.secondary,
    "bleu": discord.ButtonStyle.primary,
    "vert": discord.ButtonStyle.success,
    "rouge": discord.ButtonStyle.danger,
}


@dataclass
class Menu:
    """Un menu deroulant : une action, un texte d'invite, des options."""
    action: str
    invite: str
    options: list = field(default_factory=list)   # [(libelle, valeur, description, emoji)]
    args: tuple = ()
    plusieurs: bool = False

    @property
    def custom_id(self):
        return identifiant(self.action, *self.args)


def fichier_image(chemin, nom=None):
    """Un discord.File a partir d'un PNG sur disque, avec un nom UNIQUE.

    Le nom sert de reference (« attachment://... ») dans la carte. Deux images
    du meme nom dans un meme message se disputeraient la place : on suffixe."""
    chemin = Path(chemin)
    nom = nom or f"{chemin.stem}-{uuid4().hex[:6]}.png"
    return discord.File(io.BytesIO(chemin.read_bytes()), filename=nom)


def _decouper(lignes):
    """Des lignes -> des blocs de texte sous la limite, coupes entre deux
    lignes et jamais au milieu d'une."""
    blocs, courant = [], ""
    for ligne in lignes:
        ligne = str(ligne)
        if len(courant) + len(ligne) + 1 > TEXTE_MAX:
            if courant:
                blocs.append(courant)
            courant = ligne[:TEXTE_MAX]
        else:
            courant = f"{courant}\n{ligne}" if courant else ligne
    if courant:
        blocs.append(courant)
    return blocs


def _pied(note=""):
    texte = f"assistant CYU · {datetime.now():%d/%m à %H:%M}"
    return f"-# {note} · {texte}" if note else f"-# {texte}"


class Carte(discord.ui.LayoutView):
    """Une reponse du bot : un conteneur colore, et tout ce qu'il contient.

    timeout=None : les boutons ne sont jamais desactives par le temps. Ils
    sont rejoues par bot.py a partir de leur identifiant, donc un vieux
    message reste utilisable — c'est tout l'interet.
    """

    def __init__(self):
        super().__init__(timeout=None)

    async def on_error(self, inter, error, item):
        """Sans ca, un bouton qui echoue affiche « L'interaction a echoue »
        et rien d'autre, nulle part."""
        import traceback
        traceback.print_exception(type(error), error, error.__traceback__)
        texte = f"❌ {type(error).__name__} : {error}"[:1900]
        try:
            if inter.response.is_done():
                await inter.followup.send(texte, ephemeral=True)
            else:
                await inter.response.send_message(texte, ephemeral=True)
        except discord.HTTPException:
            pass


def _dedoublonner(rangees, menus=()):
    """Rend unique l'identifiant de chaque bouton d'une carte.

    Discord : « Component custom id cannot be duplicated ». Deux boutons qui
    font la meme chose (la fleche et le raccourci) ont le droit d'exister
    tous les deux ; ils n'ont pas le droit de porter le meme nom.
    """
    vus = {m.custom_id for m in menus}
    n = 0
    for r in rangees:
        for b in r:
            b.suffixe = ""
            while b.custom_id in vus:
                n += 1
                b.suffixe = f":_{n}"
            vus.add(b.custom_id)


def _rangee(boutons, fabrique_bouton):
    rangee = discord.ui.ActionRow()
    for b in boutons[:5]:
        rangee.add_item(fabrique_bouton(b))
    return rangee


def carte(titre, lignes=(), teinte="info", sous_titre="", image=None,
          boutons=(), menus=(), pied=None, fabrique_bouton=None,
          fabrique_menu=None, telechargements=()):
    """La reponse standard du bot.

    titre / sous_titre  la tete du bloc ;
    lignes              le corps, en Markdown Discord, une ligne par element ;
    teinte              info · cours · devoir · alerte · calme · statut ;
    image               un discord.File deja construit (fichier_image()), ou
                        rien : la carte marche aussi bien en texte seul ;
    telechargements     des discord.File presentes en PIECES JOINTES, avec leur
                        nom et leur taille, et un bouton de telechargement.
                        Une image affichee dans la carte se telecharge deja en
                        cliquant dessus ; ceci sert a ce que Discord n'affiche
                        pas — un classeur, un .ics — et a offrir le fichier
                        source a cote de l'image qui en est tiree ;
    boutons             une liste de Bouton, ou une liste de listes (une par
                        rangee) ;
    menus               une liste de Menu ;
    pied                une note de bas de carte (« ce panneau reste actif… »).

    fabrique_bouton / fabrique_menu : bot.py fournit ici la fonction qui
    transforme un Bouton en composant Discord PERSISTANT (voir bot.BoutonCyu).
    Sans elles, on cree des composants ordinaires — utile pour tester le
    module tout seul.

    Rend la Carte (a passer en `view=`) ; le fichier image, s'il y en a un,
    est a passer en `files=[image]` dans le meme envoi.
    """
    fabrique_bouton = fabrique_bouton or _bouton_simple
    fabrique_menu = fabrique_menu or _menu_simple

    conteneur = discord.ui.Container(accent_colour=couleur(teinte))
    tete = f"## {titre}"
    if sous_titre:
        tete += f"\n-# {sous_titre}"
    conteneur.add_item(discord.ui.TextDisplay(tete))

    if image is not None:
        galerie = discord.ui.MediaGallery()
        galerie.add_item(media=f"attachment://{image.filename}",
                         description=str(titre)[:200])
        conteneur.add_item(galerie)

    lignes = [l for l in (lignes or ()) if l is not None]
    if lignes:
        if image is not None:
            conteneur.add_item(discord.ui.Separator())
        for bloc in _decouper(lignes)[:4]:
            conteneur.add_item(discord.ui.TextDisplay(bloc))

    # Les pieces jointes, sous le texte : Discord les affiche avec leur nom,
    # leur taille et un bouton « telecharger ».
    for f in telechargements or ():
        if f is not None:
            conteneur.add_item(discord.ui.File(f"attachment://{f.filename}"))

    # Les rangees de boutons : une liste plate = une seule rangee. Cinq au
    # plus : c'est la limite de Discord par message, et au-dela le panneau
    # deviendrait de toute facon un mur de boutons.
    rangees = list(boutons) if boutons and isinstance(boutons[0], (list, tuple)) \
        else ([list(boutons)] if boutons else [])
    _dedoublonner(rangees, menus)
    if rangees or menus:
        conteneur.add_item(discord.ui.Separator())
    for m in menus:
        rangee = discord.ui.ActionRow()
        rangee.add_item(fabrique_menu(m))
        conteneur.add_item(rangee)
    for r in rangees[:5]:
        if r:
            conteneur.add_item(_rangee(list(r), fabrique_bouton))

    if pied is not False:
        conteneur.add_item(discord.ui.TextDisplay(_pied(pied or "")))

    vue = Carte()
    vue.add_item(conteneur)
    return vue


def erreur(message, titre="Ça n'a pas marché"):
    """Une petite carte rouge, sans boutons ni pied."""
    conteneur = discord.ui.Container(accent_colour=couleur("alerte"))
    conteneur.add_item(discord.ui.TextDisplay(f"**{titre}**\n{message}"[:TEXTE_MAX]))
    vue = Carte()
    vue.add_item(conteneur)
    return vue


# --- Sans bot.py : des composants ordinaires ---------------------------------
def _bouton_simple(b):
    return discord.ui.Button(label=b.libelle[:80], style=STYLES.get(b.style, STYLES["gris"]),
                             custom_id=b.custom_id, emoji=b.emoji, disabled=b.inactif)


def _menu_simple(m):
    options = [discord.SelectOption(label=str(l)[:100], value=str(v)[:100],
                                    description=(str(d)[:100] if d else None),
                                    emoji=e)
               for l, v, d, e in m.options[:25]]
    return discord.ui.Select(custom_id=m.custom_id, placeholder=m.invite[:150],
                             options=options, min_values=1,
                             max_values=len(options) if m.plusieurs else 1)


def sections(elements, fabrique_bouton=None):
    """Des « sections » : un texte a gauche, un bouton a droite, l'un sous
    l'autre. C'est la forme d'une liste de devoirs : chaque ligne porte son
    propre bouton « fait ».

    elements : [(texte_markdown, Bouton | None), ...]
    Rend une liste de composants a ajouter dans un conteneur.
    """
    fabrique_bouton = fabrique_bouton or _bouton_simple
    sortie = []
    for texte, bouton in elements:
        if bouton is None:
            sortie.append(discord.ui.TextDisplay(texte[:TEXTE_MAX]))
            continue
        sortie.append(discord.ui.Section(discord.ui.TextDisplay(texte[:TEXTE_MAX]),
                                         accessory=fabrique_bouton(bouton)))
    return sortie


def carte_composee(titre, composants, teinte="info", sous_titre="", boutons=(),
                   menus=(), pied=None, fabrique_bouton=None, fabrique_menu=None):
    """Comme carte(), mais le corps est une liste de composants deja faits
    (sections(), par exemple) au lieu de lignes de texte."""
    fabrique_bouton = fabrique_bouton or _bouton_simple
    fabrique_menu = fabrique_menu or _menu_simple

    conteneur = discord.ui.Container(accent_colour=couleur(teinte))
    tete = f"## {titre}"
    if sous_titre:
        tete += f"\n-# {sous_titre}"
    conteneur.add_item(discord.ui.TextDisplay(tete))
    for c in composants:
        conteneur.add_item(c)

    rangees = list(boutons) if boutons and isinstance(boutons[0], (list, tuple)) \
        else ([list(boutons)] if boutons else [])
    _dedoublonner(rangees, menus)
    if rangees or menus:
        conteneur.add_item(discord.ui.Separator())
    for m in menus:
        rangee = discord.ui.ActionRow()
        rangee.add_item(fabrique_menu(m))
        conteneur.add_item(rangee)
    for r in rangees[:5]:
        if r:
            conteneur.add_item(_rangee(list(r), fabrique_bouton))
    if pied is not False:
        conteneur.add_item(discord.ui.TextDisplay(_pied(pied or "")))

    vue = Carte()
    vue.add_item(conteneur)
    return vue
