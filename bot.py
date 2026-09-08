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
    /devoirs            ce qu'il reste a faire, avec un bouton « fait »
    /devoir             ouvre un formulaire pour en ajouter un
    /libre              tes creneaux libres
    /statut             l'assistant tourne-t-il, fraicheur des donnees
    /rafraichir         relire CELCAT tout de suite
    /panneau            epingle le panneau de boutons dans #commandes
    /ics                le fichier .ics a importer dans ton agenda

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
import warnings
from datetime import date, datetime, timedelta
from uuid import uuid4

# discord.py 2.7 deprecie TextInput(label=...) au profit de discord.ui.Label,
# qui n'existe pas avant 2.7. On garde la forme compatible avec les deux, et on
# tait l'avertissement : sinon il s'affiche a chaque lancement et donne
# l'impression que le bot est casse.
warnings.filterwarnings("ignore", message="label is deprecated",
                        category=DeprecationWarning)

import discord
from discord import app_commands

import actu
import assistant
import celcat
import config
import changements as chg
import devoirs as dv
import image as img
import notif
import statut as st
import vue

# Le daemon tourne-t-il dans le meme processus que le bot ? Mets False si tu
# preferes lancer `python assistant.py daemon` de ton cote.
AVEC_DAEMON = True

# L'heure de demarrage, affichee dans /statut et dans le panneau.
DEMARRAGE = datetime.now()

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
        jours = int(m.group(1))
        return auj, auj + timedelta(days=jours), ("jour" if jours == 0 else "grille")

    jour = vue.lire_date(texte, cours or [], auj)
    return jour, jour, "jour"


def _embed(titre, corps, couleur="info", pied=None):
    """Meme allure que les messages du daemon : un seul endroit decide."""
    return discord.Embed.from_dict(notif.embed(titre, corps, couleur, pied))


async def _donnees():
    """Lecture du cache dans un fil : ne jamais bloquer la boucle du bot sur du
    disque ou du reseau, sinon le bot parait fige pour tout le monde."""
    return await asyncio.to_thread(
        lambda: (celcat.charger(hors_ligne=True)[0], dv.lire()))


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
            notif.envoyer("Le daemon a plante",
                          ["Il repart dans 60 s. Details dans la console.",
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
        # Le panneau survit aux redemarrages : ses boutons ont des identifiants
        # fixes, donc Discord sait a qui les rendre meme apres un an.
        self.add_view(VuePanneau())
        if AVEC_DAEMON:
            threading.Thread(target=lancer_daemon, daemon=True,
                             name="daemon-cyu").start()
            print("[i] daemon demarre dans un fil a part")

    async def on_ready(self):
        print(f"[i] connecte comme {self.user}", flush=True)
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
            print("[!] le bot n'est sur aucun serveur : commandes publiees en "
                  "global (jusqu'a 1 h de delai). Invite-le avec le scope "
                  "applications.commands.")
            return

        for serveur in cibles:
            try:
                self.tree.copy_global_to(guild=serveur)
                await self.tree.sync(guild=serveur)
                nom = getattr(serveur, "name", serveur.id)
                print(f"[i] commandes publiees sur « {nom} » (immediat)")
            except discord.Forbidden:
                print(f"[!] pas le droit de publier les commandes sur "
                      f"{getattr(serveur, 'name', serveur.id)} : il manque le "
                      f"scope applications.commands dans l'invitation du bot.")


bot = Assistant()


# --- Les reponses, partagees entre les commandes et les boutons --------------
# Chaque fonction rend (titre, lignes, couleur) : les commandes slash et les
# boutons du panneau appellent exactement le meme code, donc les deux ne
# peuvent pas diverger.
async def reponse_prochain():
    cours, liste_devoirs = await _donnees()
    return "Prochain cours", vue.bloc_prochain(cours, liste_devoirs), "cours"


async def reponse_libre(jours=7):
    cours, _ = await _donnees()
    return ("Creneaux libres",
            vue.creneaux_libres(cours, jours=max(1, min(jours, 31))), "calme")


# --- Le rendu en image, partage par toutes les commandes ---------------------
async def _rendu(fabrique, nom="emploi-du-temps.png"):
    """(discord.File, "") ou (None, message d'erreur).

    Tout le dessin part dans un fil : Pillow prend une seconde sur trois
    semaines, et une seconde de boucle asyncio bloquee, c'est un bot qui ne
    repond plus a personne.

    Chaque rendu ecrit dans son propre fichier temporaire : deux personnes qui
    tapent /edt en meme temps ne doivent pas se voler leur image.
    """
    if not config.IMAGES:
        return None, "les images sont desactivees dans config.yaml (affichage.images)"

    def travail():
        chemin = config.DONNEES / f".rendu-{uuid4().hex[:8]}.png"
        try:
            fabrique(chemin)
            return io.BytesIO(chemin.read_bytes())
        finally:
            chemin.unlink(missing_ok=True)

    try:
        octets = await asyncio.to_thread(travail)
    except img.PillowManquant as e:
        return None, str(e)
    except OSError as e:
        return None, f"impossible d'ecrire l'image : {e}"
    return discord.File(octets, filename=nom), ""


# --- /edt : l'emploi du temps, en photo --------------------------------------
async def _envoyer_edt(inter, quand="", affichage="photo", ephemere=False):
    cours, liste_devoirs = await _donnees()
    try:
        debut, fin, mode = periode(quand, cours)
    except ValueError as e:
        await inter.followup.send(
            f"❌ {e}\nEssaie `12/10`, `lundi`, `demain`, `la semaine`, `+14`.",
            ephemeral=True)
        return

    # Sans Pillow, ou images desactivees : on repond quand meme, en texte.
    if affichage == "texte" or not img.DISPONIBLE or not config.IMAGES:
        titre, corps, couleur = await reponse_edt_texte(debut, fin, mode)
        await inter.followup.send(embed=_embed(titre, corps, couleur),
                                  ephemeral=ephemere)
        return

    if mode == "jour":
        fichier, souci = await _rendu(
            lambda chemin: img.rendre_jour(cours, liste_devoirs, debut, chemin),
            nom=f"{debut:%Y-%m-%d}.png")
        note = vue.jour_relatif(debut).capitalize()
    else:
        fichier, souci = await _rendu(
            lambda chemin: img.rendre(cours, debut, fin, chemin))
        note = f"Du {vue.jour_fr(debut)} au {vue.jour_fr(fin)}"
    if fichier is None:
        await inter.followup.send(f"❌ {souci}", ephemeral=True)
        return
    await inter.followup.send(content=f"🗓️ **{note}**", file=fichier,
                              ephemeral=ephemere)


async def reponse_edt_texte(debut, fin, mode):
    """La meme chose en texte, pour `affichage:texte` et sans Pillow."""
    cours, liste_devoirs = await _donnees()
    if mode == "grille":
        if fin - debut <= timedelta(days=7):
            return (f"Semaine du {celcat.semaine_de(debut):%d/%m}",
                    vue.bloc_semaine(cours, celcat.semaine_de(debut)), "cours")
        lignes = []
        lundi = celcat.semaine_de(debut)
        while lundi <= fin:
            lignes += [f"**Semaine du {lundi:%d/%m}**"] + \
                      vue.bloc_semaine(cours, lundi, detail=False) + [""]
            lundi += timedelta(days=7)
        return (f"Du {vue.jour_fr(debut, court=True)} au "
                f"{vue.jour_fr(fin, court=True)}", lignes, "cours")
    corps = vue.bloc_journee(cours, liste_devoirs, debut,
                             avec_reveil=debut != date.today())
    return vue.jour_relatif(debut).capitalize(), corps, "cours"


@bot.tree.command(name="edt", description="Ton emploi du temps en photo")
@app_commands.describe(
    quand="une date (12/10), un jour (lundi), demain, la semaine, +14… "
          "— aujourd'hui par defaut",
    affichage="photo par defaut ; texte si tu preferes copier-coller")
@app_commands.choices(affichage=AFFICHAGE_CHOIX)
async def cmd_edt(inter: discord.Interaction, quand: str = "",
                  affichage: str = "photo"):
    await inter.response.defer()
    await _envoyer_edt(inter, quand, affichage)


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


# --- /photo : une periode entiere --------------------------------------------
@bot.tree.command(name="photo",
                  description="Une periode entiere en photo (les jours a la verticale)")
@app_commands.describe(
    du="a partir de quand (aujourd'hui par defaut)",
    au="jusqu'a quand : 12/10 · +21 · vendredi (dans 6 jours par defaut)")
async def cmd_photo(inter: discord.Interaction, du: str = "", au: str = ""):
    await inter.response.defer()
    cours, _ = await _donnees()
    try:
        debut = vue.lire_date(du, cours, date.today())
        fin = vue.lire_date(au, cours, debut + timedelta(days=6))
    except ValueError as e:
        await inter.followup.send(f"❌ Date incomprise : {e}", ephemeral=True)
        return
    if fin < debut:
        debut, fin = fin, debut
    tronque = (fin - debut).days > 41

    fichier, souci = await _rendu(lambda chemin: img.rendre(cours, debut, fin, chemin))
    if fichier is None:
        await inter.followup.send(f"❌ {souci}", ephemeral=True)
        return
    note = (f"Du {vue.jour_fr(debut)} au "
            f"{vue.jour_fr(min(fin, debut + timedelta(days=41)))}")
    if tronque:
        note += " _(limité à six semaines)_"
    await inter.followup.send(content=f"🗓️ **{note}**", file=fichier)


# --- /actu : ce qui a bouge --------------------------------------------------
@bot.tree.command(name="actu",
                  description="Ce qui a change dans ton emploi du temps")
@app_commands.describe(jours="sur combien de jours regarder en arriere (7 par defaut)")
async def cmd_actu(inter: discord.Interaction, jours: int = 7):
    await inter.response.defer()
    jours = max(1, min(jours, actu.RETENTION_JOURS))
    liste = await asyncio.to_thread(actu.historique, jours)
    if not liste:
        dernier = await asyncio.to_thread(actu.quand_dernier)
        depuis = (f" Le dernier remonte au {dernier:%d/%m à %H:%M}."
                  if dernier else "")
        await inter.followup.send(
            embed=_embed("Rien n'a bougé",
                         [f"Aucun changement d'emploi du temps depuis "
                          f"{jours} jours.{depuis}"], "calme"))
        return

    fichier, souci = await _rendu(
        lambda chemin: img.rendre_changements(
            liste, chemin, titre="Ce qui a changé",
            sous_titre=f"sur les {jours} derniers jours"),
        nom="changements.png")
    if fichier is None:
        lignes, _ = await asyncio.to_thread(chg.bloc, liste)
        await inter.followup.send(embed=_embed(chg.titre(liste), lignes,
                                               chg.couleur(liste)))
        return
    await inter.followup.send(file=fichier)


# --- /prochain ---------------------------------------------------------------
async def _envoyer_prochain(inter, ephemere=False):
    cours, liste_devoirs = await _donnees()
    fichier, souci = await _rendu(
        lambda chemin: img.rendre_prochain(cours, liste_devoirs, chemin),
        nom="prochain.png")
    if fichier is None:
        titre, corps, couleur = await reponse_prochain()
        await inter.followup.send(embed=_embed(titre, corps, couleur),
                                  ephemeral=ephemere)
        return
    await inter.followup.send(file=fichier, ephemeral=ephemere)


@bot.tree.command(name="prochain", description="Le prochain cours, et dans combien de temps")
async def cmd_prochain(inter: discord.Interaction):
    await inter.response.defer()
    await _envoyer_prochain(inter)


# --- /devoirs, avec un bouton par devoir -------------------------------------
class BoutonFait(discord.ui.Button):
    def __init__(self, devoir):
        super().__init__(label=f"✅ {devoir['titre'][:60]}",
                         style=discord.ButtonStyle.secondary,
                         custom_id=f"fait-{devoir['id']}")
        self.devoir_id = devoir["id"]

    async def callback(self, inter: discord.Interaction):
        ok = await asyncio.to_thread(dv.marquer_fait, self.devoir_id)
        if not ok:
            await inter.response.send_message(
                f"Le devoir #{self.devoir_id} n'existe plus.", ephemeral=True)
            return
        # On redessine la liste : le devoir raye disparait, son bouton aussi.
        await inter.response.defer()
        liste = await asyncio.to_thread(dv.lire)
        restants = dv.actifs(liste)
        fichier, _ = await _rendu(
            lambda chemin: img.rendre_devoirs(liste, chemin), nom="devoirs.png")
        vue_boutons = VueDevoirs(restants) if restants else None
        if fichier is None:
            await inter.edit_original_response(
                embed=_embed("Devoirs", vue.bloc_devoirs(liste), "devoir"),
                view=vue_boutons)
            return
        await inter.edit_original_response(content="📚 **Devoirs**", embed=None,
                                           attachments=[fichier], view=vue_boutons)


class VueDevoirs(discord.ui.View):
    """Les boutons cessent de repondre au bout de 10 min, et au redemarrage du
    bot : c'est voulu, un vieux message ne doit pas rayer un devoir par
    accident. Il suffit de refaire /devoirs."""

    def __init__(self, liste):
        super().__init__(timeout=600)
        for d in liste[:5]:            # Discord limite a 5 boutons par ligne
            self.add_item(BoutonFait(d))


async def _envoyer_devoirs(inter, ephemere=False):
    liste = await asyncio.to_thread(dv.lire)
    restants = dv.actifs(liste)
    vue_boutons = VueDevoirs(restants) if restants else None
    fichier, _ = await _rendu(lambda chemin: img.rendre_devoirs(liste, chemin),
                              nom="devoirs.png")
    if fichier is None:
        await inter.followup.send(
            embed=_embed("Devoirs", vue.bloc_devoirs(liste), "devoir"),
            view=vue_boutons, ephemeral=ephemere)
        return
    await inter.followup.send(content="📚 **Devoirs**", file=fichier,
                              view=vue_boutons, ephemeral=ephemere)


@bot.tree.command(name="devoirs", description="Ce qu'il te reste a faire")
async def cmd_devoirs(inter: discord.Interaction):
    await inter.response.defer()
    await _envoyer_devoirs(inter)


# --- /devoir : un formulaire -------------------------------------------------
class ModaleDevoir(discord.ui.Modal, title="Nouveau devoir"):
    titre = discord.ui.TextInput(
        label="Quoi ?", placeholder="DM 2, exercices 4 a 9", max_length=100)
    matiere = discord.ui.TextInput(
        label="Matiere", placeholder="maths", required=False, max_length=40)
    pour = discord.ui.TextInput(
        label="Pour quand ?", required=False, max_length=40,
        placeholder="prochain:maths · demain · lundi · 12/09 · +3")
    note = discord.ui.TextInput(
        label="Details", style=discord.TextStyle.paragraph,
        required=False, max_length=400)

    async def on_submit(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True)
        cours, _ = await _donnees()
        try:
            echeance = dv.resoudre_echeance(str(self.pour), cours, str(self.matiere))
        except ValueError as e:
            await inter.followup.send(f"❌ {e}", ephemeral=True)
            return
        d = await asyncio.to_thread(dv.ajouter, str(self.titre), str(self.matiere),
                                    echeance, "devoir", str(self.note))
        await inter.followup.send(
            embed=_embed("Devoir ajoute", vue.ligne_devoir(d), "devoir"),
            ephemeral=False)


@bot.tree.command(name="devoir", description="Ajouter un devoir")
async def cmd_devoir(inter: discord.Interaction):
    await inter.response.send_modal(ModaleDevoir())


@bot.tree.command(name="fait", description="Marquer un devoir comme fait")
@app_commands.describe(numero="le numero affiche par /devoirs, sans le #")
async def cmd_fait(inter: discord.Interaction, numero: int):
    ok = await asyncio.to_thread(dv.marquer_fait, numero)
    if not ok:
        await inter.response.send_message(f"❌ aucun devoir #{numero}.",
                                          ephemeral=True)
        return
    await inter.response.defer()
    await _envoyer_devoirs(inter)


# --- /libre ------------------------------------------------------------------
@bot.tree.command(name="libre", description="Tes creneaux libres a venir")
@app_commands.describe(jours="nombre de jours a regarder (7 par defaut)")
async def cmd_libre(inter: discord.Interaction, jours: int = 7):
    await inter.response.defer()
    titre, corps, couleur = await reponse_libre(jours)
    await inter.followup.send(embed=_embed(titre, corps, couleur))


# --- /statut -----------------------------------------------------------------
@bot.tree.command(name="statut", description="L'assistant tourne-t-il bien ?")
async def cmd_statut(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    cours, liste_devoirs = await _donnees()
    vivant = any(t.name == "daemon-cyu" and t.is_alive()
                 for t in threading.enumerate())

    lignes = list(st.bloc(cours, liste_devoirs, DEMARRAGE))
    lignes += ["", "**Le daemon**",
               ("🟢 actif" if vivant else "🔴 ARRETE")
               + ("" if AVEC_DAEMON else " (AVEC_DAEMON = False dans bot.py)"),
               "", "**Les salons**"]
    for canal in config.CANAUX:
        mode, cible = notif.destination(canal)
        ou = f"<#{cible}>" if mode == "bot" else "webhook"
        if not notif.salon_configure(canal):
            ou += " ⚠️ non configure, retombe sur le secours"
        lignes.append(f"`{canal:10s}` {ou}")

    await inter.followup.send(embed=_embed("État de l'assistant", lignes, "statut"))


# --- /rafraichir -------------------------------------------------------------
@bot.tree.command(name="rafraichir",
                  description="Relire CELCAT maintenant et remettre les tableaux a jour")
async def cmd_rafraichir(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)

    def travail():
        cours, origine = celcat.charger()
        liste_devoirs = dv.lire()
        st.publier(cours, liste_devoirs, DEMARRAGE)
        st.publier_tableau_edt(cours, liste_devoirs)
        return cours, origine

    cours, origine = await asyncio.to_thread(travail)
    vrais = [c for c in cours if c.est_cours]
    await inter.followup.send(
        embed=_embed("Donnees rafraichies",
                     [f"Source : **{origine}** · {len(vrais)} cours connus.",
                      "Le tableau de #edt et le panneau de #statut viennent "
                      "d'etre reecrits."],
                     "calme"),
        ephemeral=True)


# --- /ics --------------------------------------------------------------------
@bot.tree.command(name="ics", description="Le fichier .ics a importer dans ton agenda")
async def cmd_ics(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)

    def travail():
        cours, _ = celcat.charger(hors_ligne=True)
        liste_devoirs = dv.lire()
        chemin = config.DONNEES / "cyu.ics"
        vue.exporter_ics([c for c in cours if c.est_cours], liste_devoirs, chemin)
        with open(chemin, "rb") as f:
            return io.BytesIO(f.read())

    octets = await asyncio.to_thread(travail)
    await inter.followup.send(
        content="Importe-le dans Google Agenda ou l'appli de ton telephone.",
        file=discord.File(octets, filename="cyu.ics"), ephemeral=True)


# --- Le panneau de boutons, epingle dans #commandes --------------------------
class VuePanneau(discord.ui.View):
    """Les memes reponses que les commandes slash, en un clic — et en photo.

    timeout=None et des custom_id fixes : le panneau reste vivant apres un
    redemarrage du bot, sans qu'on ait a le reposter.
    """

    def __init__(self):
        super().__init__(timeout=None)

    async def _texte(self, inter, fabrique):
        # Ephemere : le panneau est epingle et tout le monde clique dessus,
        # inutile de remplir le salon d'une reponse par clic.
        await inter.response.defer(ephemeral=True)
        titre, corps, couleur = await fabrique()
        await inter.followup.send(embed=_embed(titre, corps, couleur), ephemeral=True)

    async def _photo(self, inter, quand):
        await inter.response.defer(ephemeral=True)
        await _envoyer_edt(inter, quand, "photo", ephemere=True)

    @discord.ui.button(label="Aujourd'hui", emoji="📆",
                       style=discord.ButtonStyle.primary, custom_id="pan:auj")
    async def b_auj(self, inter: discord.Interaction, _b):
        await self._photo(inter, "")

    @discord.ui.button(label="Demain", emoji="🌙",
                       style=discord.ButtonStyle.secondary, custom_id="pan:demain")
    async def b_demain(self, inter: discord.Interaction, _b):
        await self._photo(inter, "demain")

    @discord.ui.button(label="La semaine", emoji="🗓️",
                       style=discord.ButtonStyle.secondary, custom_id="pan:semaine")
    async def b_semaine(self, inter: discord.Interaction, _b):
        await self._photo(inter, "semaine")

    @discord.ui.button(label="Trois semaines", emoji="📸",
                       style=discord.ButtonStyle.secondary, custom_id="pan:photo")
    async def b_photo(self, inter: discord.Interaction, _b):
        await self._photo(inter, "+20")

    @discord.ui.button(label="Prochain cours", emoji="⏭️",
                       style=discord.ButtonStyle.success, custom_id="pan:prochain", row=1)
    async def b_prochain(self, inter: discord.Interaction, _b):
        await inter.response.defer(ephemeral=True)
        await _envoyer_prochain(inter, ephemere=True)

    @discord.ui.button(label="Ce qui a changé", emoji="🔔",
                       style=discord.ButtonStyle.primary, custom_id="pan:actu", row=1)
    async def b_actu(self, inter: discord.Interaction, _b):
        await inter.response.defer(ephemeral=True)
        liste = await asyncio.to_thread(actu.historique, 7)
        if not liste:
            await inter.followup.send("Rien n'a bougé ces 7 derniers jours. 👌",
                                      ephemeral=True)
            return
        fichier, souci = await _rendu(
            lambda chemin: img.rendre_changements(
                liste, chemin, titre="Ce qui a changé",
                sous_titre="sur les 7 derniers jours"), nom="changements.png")
        if fichier is None:
            lignes, _u = chg.bloc(liste)
            await inter.followup.send(embed=_embed(chg.titre(liste), lignes,
                                                   chg.couleur(liste)), ephemeral=True)
            return
        await inter.followup.send(file=fichier, ephemeral=True)

    @discord.ui.button(label="Devoirs", emoji="📚",
                       style=discord.ButtonStyle.secondary, custom_id="pan:devoirs", row=1)
    async def b_devoirs(self, inter: discord.Interaction, _b):
        await inter.response.defer(ephemeral=True)
        await _envoyer_devoirs(inter, ephemere=True)

    @discord.ui.button(label="Ajouter un devoir", emoji="➕",
                       style=discord.ButtonStyle.success, custom_id="pan:ajout", row=2)
    async def b_ajout(self, inter: discord.Interaction, _b):
        await inter.response.send_modal(ModaleDevoir())

    @discord.ui.button(label="Creneaux libres", emoji="🫧",
                       style=discord.ButtonStyle.secondary, custom_id="pan:libre", row=2)
    async def b_libre(self, inter: discord.Interaction, _b):
        await self._texte(inter, lambda: reponse_libre(7))

    @discord.ui.button(label="Rafraichir", emoji="🔄",
                       style=discord.ButtonStyle.secondary, custom_id="pan:refresh", row=2)
    async def b_refresh(self, inter: discord.Interaction, _b):
        await inter.response.defer(ephemeral=True)

        def travail():
            cours, origine = celcat.charger()
            liste_devoirs = dv.lire()
            st.publier(cours, liste_devoirs, DEMARRAGE)
            st.publier_tableau_edt(cours, liste_devoirs)
            return origine, len([c for c in cours if c.est_cours])

        origine, combien = await asyncio.to_thread(travail)
        await inter.followup.send(
            f"🔄 Relu depuis **{origine}** — {combien} cours. "
            f"Tableaux mis a jour.", ephemeral=True)


@bot.tree.command(name="panneau",
                  description="Epingler le panneau de boutons dans ce salon")
async def cmd_panneau(inter: discord.Interaction):
    corps = [
        "Tout ce que fait l'assistant, en un clic. Les reponses ne sont "
        "visibles que par toi.",
        "",
        "Tu peux aussi taper les commandes : `/edt` `/photo` `/prochain` "
        "`/devoirs` `/devoir` `/libre` `/statut` `/help`",
    ]
    await inter.response.send_message(
        embed=_embed("Panneau de l'assistant", corps, "info",
                     pied="ce panneau reste actif apres un redemarrage"),
        view=VuePanneau())
    # Epingler tout de suite : le panneau doit rester en haut du salon meme
    # quand tu y tapes vingt commandes a la suite.
    try:
        message = await inter.original_response()
        await message.pin()
    except discord.HTTPException:
        pass


# --- /help -------------------------------------------------------------------
def texte_aide():
    """L'aide est construite depuis config.yaml, pas ecrite en dur : si tu
    changes l'heure d'un briefing, /help dit la nouvelle heure sans qu'on y
    touche."""
    jour_recap = vue.JOURS[config.RECAP_SEMAINE_JOUR % 7]

    lignes = [
        "**Voir — tout sort en photo**",
        "`/edt` — **ta journee en photo**. Avec une date : `/edt 12/10`, "
        "`/edt lundi`, `/edt demain`",
        "`/edt quand:la semaine` — la semaine entiere, **les jours a la "
        "verticale**. Aussi : `+14`, `la semaine prochaine`",
        "`/edt affichage:texte` — la meme chose en texte, si tu veux "
        "copier-coller",
        "`/photo du:12/10 au:31/10` — une periode precise, en photo",
        "`/actu` — **ce qui a change** dans l'emploi du temps, en photo",
        "`/prochain` — le prochain cours, la salle, et dans combien de temps",
        "`/libre` — tes creneaux libres",
        "",
        "**Les devoirs**",
        "`/devoirs` — ce qu'il te reste a faire, avec un bouton ✅ par devoir",
        "`/devoir` — ouvre un formulaire pour en ajouter un",
        "`/fait 3` — raye le devoir numero 3",
        "",
        "**Le reste**",
        "`/statut` — l'assistant tourne-t-il, fraicheur des donnees, salons",
        "`/rafraichir` — relire CELCAT tout de suite",
        "`/panneau` — epingler le panneau de boutons dans un salon",
        "`/ics` — le fichier a importer dans ton agenda",
        "",
        "**Ecrire une date**  (pour `/edt` comme pour un devoir)",
        "`12/10` · `12/10/2026` · `2026-10-12`",
        "`demain` · `lundi` · `apres-demain`",
        "`+21` — les 21 prochains jours",
        "`la semaine` · `la semaine prochaine`",
        "`prochain:vba` — ton prochain cours de VBA, avec son heure exacte",
        "",
        "**Ce qui arrive tout seul, sans rien taper**",
        f"`{config.BRIEFING_MATIN}` **#annonces** — la journee entiere, et ce "
        f"qu'il faut rendre aujourd'hui",
        f"`{config.BRIEFING_SOIR}` **#annonces** — demain, l'heure de lever, "
        f"les echeances qui approchent",
        f"`{jour_recap} {config.RECAP_SEMAINE_HEURE}` **#annonces** — la "
        f"semaine qui vient et sa charge de travail",
    ]
    if config.AVANT_COURS_MINUTES:
        minutes = ", ".join(str(m) for m in config.AVANT_COURS_MINUTES)
        lignes.append(f"`{minutes} min avant chaque cours` — la salle et le prof")
    if config.PREMIER_COURS_MINUTES:
        lignes.append(f"`{config.PREMIER_COURS_MINUTES} min avant le premier "
                      f"cours du jour` — l'heure de partir")
    if config.RELANCE_DEVOIRS:
        lignes.append("`fin de journee` **#devoirs** — « tu as eu quoi, des "
                      "devoirs a noter ? »")
    lignes += [
        f"`toutes les {config.VERIF_EDT_MINUTES} min` **#alertes** — cours "
        f"deplace, annule, changement de salle, "
        + ("**avec une mention**" if config.PING_CHANGEMENTS else "sans mention"),
        f"`toutes les {config.RAFRAICHIR_TABLEAUX_MINUTES} min` **#edt** et "
        f"**#statut** — les deux tableaux vivants, reecrits sur place (aucune "
        f"notification)",
    ]
    return lignes


@bot.tree.command(name="help", description="Toutes les commandes de l'assistant")
async def cmd_help(inter: discord.Interaction):
    # Ephemere : l'aide n'a d'interet que pour celui qui la demande, inutile
    # d'encombrer le salon.
    await inter.response.send_message(
        embed=_embed("Assistant CYU", texte_aide(), "info",
                     pied="Tape /panneau pour avoir tout en boutons"),
        ephemeral=True)


# --- Erreurs -----------------------------------------------------------------
@bot.tree.error
async def en_cas_d_erreur(inter: discord.Interaction, err: app_commands.AppCommandError):
    """Sans ca, une commande qui echoue laisse juste « L'application ne repond
    pas » a l'ecran, et rien dans la console."""
    traceback.print_exception(type(err), err, err.__traceback__)
    message = f"❌ {type(err.__cause__ or err).__name__} : {err}"
    try:
        if inter.response.is_done():
            await inter.followup.send(message[:1900], ephemeral=True)
        else:
            await inter.response.send_message(message[:1900], ephemeral=True)
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
              "developpeur Discord > ton application > Bot > Reset Token.")
        sys.exit(1)
    try:
        bot.run(config.BOT_TOKEN, log_handler=None)
    except discord.LoginFailure:
        print("[X] jeton refuse par Discord : il est faux ou a ete regenere.")
        sys.exit(1)
    except discord.PrivilegedIntentsRequired as e:
        print(f"[X] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
