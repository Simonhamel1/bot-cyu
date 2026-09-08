#!/usr/bin/env python3
"""
Le bot Discord : tout se pilote depuis Discord, plus besoin du terminal.

C'est le fichier a lancer au quotidien. Un seul processus fait les deux choses :

  * il PARLE  : le daemon d'assistant.py tourne dans un fil d'execution a part
                et envoie briefings, alertes et tableaux dans leurs salons ;
  * il ECOUTE : les commandes slash repondent la ou tu les tapes.

    /edt [aujourdhui|demain|semaine|lundi...]   ton emploi du temps, en texte
    /photo [jusqu_au]   ton emploi du temps en image, jusqu'a la date voulue
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
import sys
import threading
import time
import traceback
import warnings
from datetime import date, datetime, timedelta

# discord.py 2.7 deprecie TextInput(label=...) au profit de discord.ui.Label,
# qui n'existe pas avant 2.7. On garde la forme compatible avec les deux, et on
# tait l'avertissement : sinon il s'affiche a chaque lancement et donne
# l'impression que le bot est casse.
warnings.filterwarnings("ignore", message="label is deprecated",
                        category=DeprecationWarning)

import discord
from discord import app_commands

import assistant
import celcat
import config
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

JOURS_CHOIX = [
    app_commands.Choice(name="aujourd'hui", value="aujourdhui"),
    app_commands.Choice(name="demain", value="demain"),
    app_commands.Choice(name="cette semaine", value="semaine"),
    app_commands.Choice(name="la semaine prochaine", value="semaine_prochaine"),
] + [app_commands.Choice(name=j, value=j) for j in vue.JOURS]


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
async def reponse_edt(quand="aujourdhui"):
    cours, liste_devoirs = await _donnees()

    if quand in ("semaine", "semaine_prochaine"):
        lundi = celcat.semaine_de(date.today())
        if quand == "semaine_prochaine":
            lundi += timedelta(days=7)
        return f"Semaine du {lundi:%d/%m}", vue.bloc_semaine(cours, lundi), "cours"

    jour = date.today()
    if quand == "demain":
        jour += timedelta(days=1)
    elif quand in vue.JOURS:
        jour += timedelta(days=(vue.JOURS.index(quand) - jour.weekday()) % 7)
    corps = vue.bloc_journee(cours, liste_devoirs, jour,
                             avec_reveil=jour != date.today())
    return vue.jour_relatif(jour).capitalize(), corps, "cours"


async def reponse_prochain():
    cours, liste_devoirs = await _donnees()
    return "Prochain cours", vue.bloc_prochain(cours, liste_devoirs), "cours"


async def reponse_libre(jours=7):
    cours, _ = await _donnees()
    return ("Creneaux libres",
            vue.creneaux_libres(cours, jours=max(1, min(jours, 31))), "calme")


# --- /edt --------------------------------------------------------------------
@bot.tree.command(name="edt", description="Ton emploi du temps, en texte")
@app_commands.describe(quand="aujourd'hui par defaut")
@app_commands.choices(quand=JOURS_CHOIX)
async def cmd_edt(inter: discord.Interaction, quand: str = "aujourdhui"):
    await inter.response.defer()
    titre, corps, couleur = await reponse_edt(quand)
    await inter.followup.send(embed=_embed(titre, corps, couleur))


# --- /photo : l'emploi du temps en image -------------------------------------
async def _fabriquer_image(jusqu_au="", a_partir_de=""):
    """(fichier Discord, note) ou (None, message d'erreur).

    Tout le dessin se fait dans un fil : Pillow prend une seconde sur trois
    semaines, et une seconde de boucle asyncio bloquee, c'est un bot qui ne
    repond plus a personne."""
    cours, _ = await _donnees()

    def travail():
        debut = vue.lire_date(a_partir_de, cours, date.today())
        fin = vue.lire_date(jusqu_au, cours, debut + timedelta(days=6))
        if fin < debut:
            debut, fin = fin, debut
        # rendre() coupe deja a six semaines ; on le dit ici pour que
        # l'utilisateur ne croie pas a un bug.
        tronque = (fin - debut).days > 41
        chemin = img.rendre(cours, debut, fin)
        with open(chemin, "rb") as f:
            octets = io.BytesIO(f.read())
        note = f"Du {vue.jour_fr(debut)} au {vue.jour_fr(min(fin, debut + timedelta(days=41)))}."
        if tronque:
            note += " (limite a six semaines)"
        return octets, note

    try:
        octets, note = await asyncio.to_thread(travail)
    except img.PillowManquant as e:
        return None, str(e)
    except ValueError as e:
        return None, f"Date incomprise : {e}"
    return discord.File(octets, filename="emploi-du-temps.png"), note


@bot.tree.command(name="photo", description="Ton emploi du temps en image")
@app_commands.describe(
    jusqu_au="jusqu'a quand : 12/10 · dans 3 semaines (+21) · vendredi · demain",
    a_partir_de="a partir de quand (aujourd'hui par defaut)")
async def cmd_photo(inter: discord.Interaction, jusqu_au: str = "",
                    a_partir_de: str = ""):
    await inter.response.defer()
    fichier, note = await _fabriquer_image(jusqu_au, a_partir_de)
    if fichier is None:
        await inter.followup.send(f"❌ {note}", ephemeral=True)
        return
    await inter.followup.send(content=f"🗓️ {note}", file=fichier)


# --- /prochain ---------------------------------------------------------------
@bot.tree.command(name="prochain", description="Le prochain cours, et dans combien de temps")
async def cmd_prochain(inter: discord.Interaction):
    await inter.response.defer()
    titre, corps, couleur = await reponse_prochain()
    await inter.followup.send(embed=_embed(titre, corps, couleur))


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
        # On reconstruit la liste : le devoir raye disparait, son bouton aussi.
        liste = await asyncio.to_thread(dv.lire)
        restants = dv.actifs(liste)
        await inter.response.edit_message(
            embed=_embed("Devoirs", vue.bloc_devoirs(liste), "devoir"),
            view=VueDevoirs(restants) if restants else None)


class VueDevoirs(discord.ui.View):
    """Les boutons cessent de repondre au bout de 10 min, et au redemarrage du
    bot : c'est voulu, un vieux message ne doit pas rayer un devoir par
    accident. Il suffit de refaire /devoirs."""

    def __init__(self, liste):
        super().__init__(timeout=600)
        for d in liste[:5]:            # Discord limite a 5 boutons par ligne
            self.add_item(BoutonFait(d))


@bot.tree.command(name="devoirs", description="Ce qu'il te reste a faire")
async def cmd_devoirs(inter: discord.Interaction):
    await inter.response.defer()
    liste = await asyncio.to_thread(dv.lire)
    restants = dv.actifs(liste)
    await inter.followup.send(
        embed=_embed("Devoirs", vue.bloc_devoirs(liste), "devoir"),
        view=VueDevoirs(restants) if restants else None)


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
    liste = await asyncio.to_thread(dv.lire)
    await inter.response.send_message(
        embed=_embed("Devoirs", vue.bloc_devoirs(liste), "devoir"))


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
    """Les memes reponses que les commandes slash, en un clic.

    timeout=None et des custom_id fixes : le panneau reste vivant apres un
    redemarrage du bot, sans qu'on ait a le reposter."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _repondre(self, inter, fabrique):
        # Ephemere : le panneau est epingle et tout le monde clique dessus,
        # inutile de remplir le salon d'une reponse par clic.
        await inter.response.defer(ephemeral=True)
        titre, corps, couleur = await fabrique()
        await inter.followup.send(embed=_embed(titre, corps, couleur),
                                  ephemeral=True)

    @discord.ui.button(label="Aujourd'hui", emoji="📆",
                       style=discord.ButtonStyle.primary, custom_id="pan:auj")
    async def b_auj(self, inter: discord.Interaction, _b):
        await self._repondre(inter, lambda: reponse_edt("aujourdhui"))

    @discord.ui.button(label="Demain", emoji="🌙",
                       style=discord.ButtonStyle.secondary, custom_id="pan:demain")
    async def b_demain(self, inter: discord.Interaction, _b):
        await self._repondre(inter, lambda: reponse_edt("demain"))

    @discord.ui.button(label="La semaine", emoji="🗓️",
                       style=discord.ButtonStyle.secondary, custom_id="pan:semaine")
    async def b_semaine(self, inter: discord.Interaction, _b):
        await self._repondre(inter, lambda: reponse_edt("semaine"))

    @discord.ui.button(label="Prochain cours", emoji="⏭️",
                       style=discord.ButtonStyle.success, custom_id="pan:prochain")
    async def b_prochain(self, inter: discord.Interaction, _b):
        await self._repondre(inter, reponse_prochain)

    @discord.ui.button(label="Photo de la semaine", emoji="📸",
                       style=discord.ButtonStyle.primary, custom_id="pan:photo", row=1)
    async def b_photo(self, inter: discord.Interaction, _b):
        await inter.response.defer(ephemeral=True)
        fichier, note = await _fabriquer_image("+6")
        if fichier is None:
            await inter.followup.send(f"❌ {note}", ephemeral=True)
            return
        await inter.followup.send(content=f"🗓️ {note}", file=fichier, ephemeral=True)

    @discord.ui.button(label="Devoirs", emoji="📚",
                       style=discord.ButtonStyle.secondary, custom_id="pan:devoirs", row=1)
    async def b_devoirs(self, inter: discord.Interaction, _b):
        await inter.response.defer(ephemeral=True)
        liste = await asyncio.to_thread(dv.lire)
        restants = dv.actifs(liste)
        await inter.followup.send(
            embed=_embed("Devoirs", vue.bloc_devoirs(liste), "devoir"),
            view=VueDevoirs(restants) if restants else None, ephemeral=True)

    @discord.ui.button(label="Ajouter un devoir", emoji="➕",
                       style=discord.ButtonStyle.success, custom_id="pan:ajout", row=1)
    async def b_ajout(self, inter: discord.Interaction, _b):
        await inter.response.send_modal(ModaleDevoir())

    @discord.ui.button(label="Creneaux libres", emoji="🫧",
                       style=discord.ButtonStyle.secondary, custom_id="pan:libre", row=2)
    async def b_libre(self, inter: discord.Interaction, _b):
        await self._repondre(inter, lambda: reponse_libre(7))

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
        "**Voir**",
        "`/edt` — ton emploi du temps en texte : aujourd'hui, demain, la "
        "semaine, ou un jour precis",
        "`/photo jusqu_au:12/10` — **l'emploi du temps en image**, jusqu'a la "
        "date que tu veux",
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
        "**Ecrire une date**  (pour `/photo` comme pour un devoir)",
        "`12/10` · `12/10/2026` · `2026-10-12`",
        "`demain` · `lundi` · `apres-demain`",
        "`+21` — dans 21 jours",
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
        f"deplace, annule, changement de salle",
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
