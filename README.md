# Assistant CYU

Un assistant qui lit ton emploi du temps CELCAT, tient ton carnet de devoirs, et
te parle sur Discord. Tu ne rouvres jamais CELCAT.

Toute la configuration — y compris les mots de passe — tient dans un seul
fichier : **`config.yaml`**. Aucun fichier Python n'a de valeur à modifier.

---

## Ce qu'il fait

### Il t'écrit tout seul

| Quand | Où | Quoi |
| --- | --- | --- |
| **8 h, en semaine** | `#annonces` | **la photo de la journée** : cours, salles, trous, **la météo de ton trajet**, ce qu'il faut rendre |
| **20 h** | `#annonces` | **la photo de demain**, l'heure de lever conseillée, les échéances |
| **dimanche 18 h** | `#annonces` | **la photo de la semaine** qui vient, puis **ce qu'elle pèse en chiffres** |
| **18 h** | `#devoirs` | « tu as eu Corporate finance et VBA, des devoirs à noter ? » |
| dès qu'un cours bouge | `#alertes` | **la photo du changement**, et **il te tague** |
| en permanence | `#edt` · `#statut` | l'image de la semaine et le panneau d'état, réécrits sur place, **sans notification** |

Il n'envoie **pas** de rappel 20 min avant chaque cours : c'était du bruit, les
deux briefings disent tout. Si tu en veux quand même, remplis
`notifications.avant_cours_minutes` dans `config.yaml`.

### Il détecte vraiment les cours déplacés

CELCAT ne publie pas de « modifications » : il republie tout, avec des
identifiants neufs. Un cours décalé d'une heure ressort donc, ailleurs, comme
une disparition suivie d'un ajout sans rapport.

L'assistant rapproche les deux côtés et dit ce qui s'est réellement passé :

```text
🕘 Déplacé dans la journée
   Corporate finance (CM) — mer. 09/09
   08:30-11:45  ➜  09:30-12:45  (+1 h) · FER FT 104-106

🚪 Changement de salle
   VBA for finance (TD) — jeu. 10/09 13:00-16:15
   FER FT 104-106  ➜  AMPHI B 210
```

Six cas distincts : annulé, déplacé un autre jour, déplacé dans la journée,
changement de salle, durée modifiée, changement d'intervenant. Le tout arrive
**en photo dans `#alertes`, et il te tague** — c'est tout l'intérêt d'avoir un
bot. (`ping_changements: false` dans `config.yaml` si tu ne veux la mention
que pour ce qui touche aujourd'hui ou demain.)

### Et il ne raconte pas n'importe quoi

Un bot qui crie au loup, on finit par le couper. Quatre garde-fous, dans
`actu.py` :

| Le piège | Ce qu'il fait |
| --- | --- |
| le bot redémarre | la référence est **sur le disque**, pas en mémoire : ce qui a bougé pendant qu'il était éteint est annoncé au démarrage suivant, pas avalé |
| l'horizon glisse d'un jour chaque matin | la comparaison est **bornée à la fenêtre commune** aux deux lectures — sinon le jour gagné passerait pour des ajouts, et le jour perdu pour des annulations |
| CELCAT répond à moitié | un emploi du temps qui perd d'un coup **plus de la moitié** de ses cours n'est pas cru : l'ancienne référence est gardée, rien n'est annoncé, et `#logs` le dit. Trois lectures identiques finissent par être acceptées — un semestre peut vraiment se terminer |
| un cours bouge, revient, rebouge | chaque changement annoncé laisse sa **signature** dans un journal : il n'est jamais annoncé deux fois |
| le semestre entier sort d'un coup | ce n'est pas « 47 ajouts » mais **« l'emploi du temps est sorti »**, avec sa photo |

Le journal garde un mois : c'est ce que `/actu` relit pour te redessiner ce
qui a bougé cette semaine, sans redemander quoi que ce soit à CELCAT.

### Il répond en photo, pas en liste

Un embed Discord n'a ni colonnes, ni couleurs, ni alignement : une journée en
texte est une liste, alors que c'est une **forme**. Tout ce qui gagne à être
vu est donc dessiné.

**Les jours sont à la verticale** — une ligne par jour, les heures de gauche à
droite. Sur un téléphone, cinq colonnes étroites obligent à zoomer ; cinq
lignes larges se lisent d'un coup. Le trait rouge marque l'heure qu'il est, la
ligne du jour est surlignée, une semaine sans cours tient en une bande.

```text
/edt                     ta journée, en photo
/edt 12/10               le 12 octobre
/edt lundi               lundi prochain
/edt quand:la semaine    la semaine, jours à la verticale
/edt quand:+14           les quinze prochains jours
/edt affichage:texte     la même chose en texte, pour copier-coller
```

Cinq images différentes, chacune faite pour sa question : **la journée**
(cours, trous, salles, profs, heure de lever, devoirs à rendre), **la
période** (la grille, semaine par semaine), **les changements** (avant ➜
après), **les devoirs** (triés par urgence) et **le prochain cours** (en
grand, avec l'heure de partir).

Sans Pillow installé, aucune commande ne casse : tout retombe sur le texte.

### Il te dit s'il faut un parapluie

Le briefing du matin ne dit pas le temps qu'il fait : il dit le temps qu'il
fera **à l'heure où tu sors**, ce qui n'est pas la même chose quand on part à
7 h 45 pour un cours à 8 h 30.

```text
☁️ Couvert · 13 à 22 °C
   Au départ de 07:35 : pluie · 7 °C · ressenti 4 °C · 2.4 mm — prends un parapluie
```

Et quand il pleut sur le trajet, **l'heure de départ conseillée recule toute
seule** de dix minutes (`meteo.marge_pluie_minutes`). C'est la seule chose que
la météo a le droit de changer.

Source : [Open-Meteo](https://open-meteo.com) — **ni compte, ni clé d'API,
rien à remplir**. Les coordonnées par défaut sont celles de Cergy ;
`meteo.latitude` / `meteo.longitude` dans `config.yaml` pour ailleurs.
`meteo.active: false` pour ne plus en entendre parler.

Le pictogramme des images est **dessiné**, pas écrit en emoji : sur un serveur
où seule DejaVu est installée, un emoji devient un carré vide.

### Il compte ce que ta semaine te coûte

`/stats` répond à ce que l'emploi du temps ne dit pas : est-ce que cette
semaine est chargée, où passe ton temps, combien tu perds entre deux cours.

```text
TOTAL DE COURS     MOYENNE PAR JOUR   TEMPS DE TROU      JOUR LE PLUS LOURD
22 h               4 h 24             9 h                Mardi
▲ 3 h vs S-1       9 séances          3 créneaux         8 h · 08:30→17:00

Où passe ton temps
Algorithmique  45 %  ████████████████████████  10 h
VBA            41 %  ██████████████████████     9 h
Anglais        14 %  ███████                    3 h
```

Les barres sont **empilées par type de séance** (CM, TD, TP, examen, à
distance), et la vue « jour par jour » superpose deux mesures sur la même
échelle : les heures de cours en plein, et **le temps de présence** — trous
compris — en clair derrière. L'écart entre les deux, c'est ce que la semaine
te coûte vraiment.

CELCAT ne sert que l'avenir : une semaine déjà commencée y est amputée de ses
premiers jours. L'assistant **archive donc le poids de chaque semaine à venir**
au passage (`donnees/semaines.json`), ce qui lui permet de comparer avec la
semaine d'avant — et quand le compte ne peut pas être complet, **il l'écrit
sur l'image** au lieu de laisser croire à un total exact.

### Il répond aux commandes — et chaque réponse a ses boutons

Chaque réponse est une **carte** : un bloc coloré qui contient le titre, la
photo, le texte, et **ses propres boutons**. Une journée porte
`◀ · Aujourd'hui · ▶ · La semaine · Texte`, les stats
`◀ · Cette semaine · ▶ · La grille`, la liste des devoirs un bouton **✅ Fait**
par ligne et un menu pour supprimer. On navigue en cliquant : le message se
réécrit sur place, le salon ne se remplit pas.

Ces boutons **survivent aux redémarrages** : leur identifiant contient tout ce
qu'il faut pour rejouer l'action (`cyu:edt:j:2026-10-12`), le bot ne garde
rien en mémoire. Un message d'il y a un mois marche encore.

| Commande | Effet |
| --- | --- |
| `/edt [quand]` | **l'emploi du temps en photo** : rien, une date (`12/10`), un jour (`lundi`), `demain`, `la semaine`, `+14`… |
| `/photo [du] [au]` | une période précise, en photo |
| `/actu [jours]` | **ce qui a changé** dans l'emploi du temps, en photo |
| `/prochain` | le prochain cours, la salle, et dans combien de temps |
| `/devoirs` | la liste interactive : **un bouton ✅ par devoir**, un menu pour supprimer, une page par six |
| `/devoir` | un formulaire pour en ajouter un — titre, matière, échéance, **type** (devoir, DM, projet, révision, examen), détails |
| `/fait` · `/supprimer` | rayer ou retirer un devoir, **la liste s'affiche pendant la frappe** |
| `/libre` | tes créneaux libres |
| `/stats [quand]` | **ce que pèse ta semaine** en photo : heures, matières, trous, jour le plus lourd. `quand: tout l'emploi du temps` — **toutes les heures par matière**, et semaine par semaine |
| `/meteo [jours]` | le temps qu'il fera, et **s'il faut un parapluie** pour ton trajet |
| `/comparer [quand]` | **cette semaine contre la précédente** : heures, séances, trous, devoirs à rendre, et ce qui bouge matière par matière |
| `/examens` | **compte à rebours** avant chaque examen — ceux de CELCAT et ceux de ton carnet réunis — et le temps libre pour réviser d'ici le premier |
| `/prediction` | les paris de l'assistant 🎲 : le cours qui va bouger, le jour où tu vas craquer, le jour du parapluie… des vrais chiffres, des fausses cotes |
| `/statut` | l'assistant tourne-t-il, fraîcheur des données, salons |
| `/rafraichir` | relire CELCAT tout de suite |
| `/panneau` | épingle un panneau de boutons et un menu « voir un jour de la semaine » — tout ça sans rien taper |
| `/ics` | le fichier à importer dans ton agenda |
| `/help` | l'aide, construite depuis ta config |

---

## Les sept salons

Un salon par type de message, pour régler les notifications Discord séparément.

| Salon | Contenu | Réglage conseillé |
| --- | --- | --- |
| `#annonces` | briefings matin / soir / dimanche | **tous les messages** |
| `#edt` | le tableau de la semaine + les changements sans urgence | mentions seulement |
| `#devoirs` | échéances, relance du soir | tous les messages |
| `#alertes` | cours déplacé ou annulé pour aujourd'hui / demain, pannes | **tous les messages** |
| `#statut` | le panneau d'état permanent | muet |
| `#commandes` | ton salon fourre-tout + le panneau de boutons | muet |
| `#logs` | démarrages, erreurs, heartbeats | **muet** |

`#edt` et `#statut` ne contiennent qu'**un seul message chacun**, réécrit en
place. Ils ne notifient donc jamais, et l'information y est toujours à jour et
toujours en haut. Supprime le message à la main et il se régénère.

Tu n'es obligé d'en créer aucun : tout salon laissé vide dans `config.yaml`
retombe sur `webhook_secours`.

---

## Installation

### 1. Les dépendances

```bash
pip install -r requirements.txt
```

**Sur un serveur Ubuntu 24 / Debian 12**, cette commande est refusée
(`error: externally-managed-environment`) : depuis PEP 668, ces distributions
interdisent à pip de toucher au Python du système. Deux façons de s'en sortir,
au choix :

```bash
# 1. dans ton dossier personnel — c'est ce que fait installer.sh
pip3 install --user --break-system-packages -r requirements.txt

# 2. dans un environnement isolé, si tu préfères
sudo apt install -y python3-venv
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# puis lance le bot avec .venv/bin/python bot.py, et remplace
# /usr/bin/env python3 par le chemin du venv dans les fichiers .service
```

Le nom de `--break-system-packages` fait peur, mais avec `--user` tout part
dans `~/.local` : les paquets système ne sont jamais touchés.

Le plus simple sur un serveur reste de ne rien taper de tout ça et de lancer
**`./installer.sh`**, qui s'en occupe.

`requests`, `discord.py`, `PyYAML`, et `Pillow` pour les images. Sans Pillow,
tout fonctionne, mais tout sort en texte. Sur un serveur nu, installe aussi une
police, sinon les accents disparaissent des images :

```bash
sudo apt install -y fonts-dejavu-core
```

### 2. Le bot Discord

Sur [discord.com/developers/applications](https://discord.com/developers/applications) :

1. Ton application → onglet **Bot** → **Reset Token** → copie le jeton
2. Onglet **OAuth2** → copie l'**Application ID**
3. Ouvre cette URL en remplaçant `TON_APP_ID` :

```text
https://discord.com/oauth2/authorize?client_id=TON_APP_ID&scope=bot+applications.commands&permissions=19456
```

Le `scope=bot+applications.commands` est indispensable. Sans
`applications.commands`, le bot rejoint le serveur mais **aucune commande slash
n'apparaît**. Si ton bot est déjà invité sans ce scope, réouvre simplement cette
URL : ça complète l'autorisation sans le faire quitter le serveur.

Permissions : voir les salons, gérer les salons, envoyer des messages, joindre
des fichiers, intégrer des liens, gérer les messages (pour épingler).

### 3. Remplir `config.yaml`

```bash
cp config.example.yaml config.yaml
```

Puis trois valeurs, c'est tout :

```yaml
discord:
  bot_token: "le_jeton_copie_a_l_etape_2"
cyu:
  user: "ton_identifiant_cyu"
  password: "ton_mot_de_passe_cyu"
```

Vérifie sans rien envoyer :

```bash
python assistant.py config
```

### 4. Les salons

Soit tu les crées à la main dans Discord et tu colles les identifiants (clic
droit sur le salon → Copier l'identifiant, mode développeur activé) dans
`config.yaml`. Soit tu laisses le bot les créer :

```bash
python assistant.py salons     # crée les 7 salons ET écrit les ids dans config.yaml
python assistant.py init       # un message de test dans chacun, pour vérifier
```

`salons` est idempotent : le relancer ne crée pas de doublon, et il ne touche
ni aux commentaires ni au reste de `config.yaml`.

### 5. Démarrer

```bash
python bot.py
```

Puis, dans `#commandes` : `/panneau` pour épingler les boutons.

---

## En ligne de commande

```bash
python assistant.py config                    # vérifier config.yaml
python assistant.py aujourdhui                # la journée
python assistant.py demain
python assistant.py semaine                   # la grille + le détail
python assistant.py prochain                  # le prochain cours
python assistant.py libre                     # les créneaux libres
python assistant.py image --jusqu-au 12/10    # le PNG d'une période
python assistant.py photo --jour 12/10        # le PNG d'une journée
python assistant.py actu --jours 7            # ce qui a bougé, en PNG
python assistant.py tableaux                  # réécrire #statut et #edt
python assistant.py ics                       # export agenda
python assistant.py daemon                    # la boucle de fond seule

python assistant.py devoir add "DM 2" -m maths -p prochain:maths
python assistant.py devoir list
python assistant.py devoir fait 3
```

Ajoute `--discord` pour envoyer au lieu d'afficher, `--hors-ligne` pour lire le
cache sans se connecter à CELCAT.

### Écrire une date

La même grammaire partout — pour `/edt` comme pour l'échéance d'un devoir :

```text
12/10 · 12/10/2026 · 2026-10-12
demain · lundi · apres-demain
+21                        les 21 prochains jours
la semaine · la semaine prochaine
prochain:vba               ton prochain cours de VBA, avec son heure exacte
```

---

## Surveiller le webmail

`uptime.py` est indépendant du reste : il vérifie que
[mail.etu.cyu.fr](https://mail.etu.cyu.fr/mail) répond, prévient dans
`#alertes` quand il tombe ou revient, et alimente la ligne « Webmail » du
panneau de `#statut`.

```bash
python uptime.py
```

Il ne teste pas seulement « le serveur répond ». Le webmail est derrière une
chaîne SAML (`mail.etu.cyu.fr` → `sp.partage.renater.fr` → `idp.cyu.fr`), et
quand la fédération casse, l'IdP renvoie **HTTP 400 avec une page d'erreur** —
donc un test naïf sur le code HTTP annonce « en ligne » alors que la boîte est
totalement inaccessible. On vérifie le code, l'absence de page d'erreur
Shibboleth, **et** la présence du formulaire de connexion.

---

## Déployer sur un serveur

Deux chemins, au choix. Les deux aboutissent au même service systemd.

### A. Par `git pull` (recommandé)

Le dépôt vit sur GitHub, le serveur le tire. **`config.yaml` ne passe jamais par
git** — il contient le jeton du bot et ton mot de passe CYU, il est dans
`.gitignore`. Tu le déposes une seule fois, à la main.

Sur le serveur, la première fois :

```bash
git clone https://github.com/<toi>/<depot>.git ~/bot-cyu
```

Depuis ta machine, envoie la config (une seule fois) :

```bash
scp config.yaml utilisateur@ip_du_serveur:~/bot-cyu/config.yaml
```

Puis, sur le serveur :

```bash
cd ~/bot-cyu && ./installer.sh
```

Et à chaque mise à jour, ensuite :

```bash
cd ~/bot-cyu && git pull && ./installer.sh
```

`installer.sh` vérifie que la config est remplie, installe les dépendances,
contrôle le fuseau horaire, pose le service systemd et redémarre le bot. Il est
idempotent : le relancer ne crée pas de doublon et ne touche **ni** à
`config.yaml` **ni** à `donnees/`.

```bash
./installer.sh --avec-uptime      # ajoute la surveillance du webmail
./installer.sh --sans-uptime      # la retire
./installer.sh --sans-demarrer    # tout préparer sans lancer le service
./installer.sh --help
```

### B. Par `deployer.sh` (depuis ta machine, sans GitHub)

```bash
./deployer.sh utilisateur@ip_du_serveur
```

Copie le projet en SSH (rsync, `config.yaml` compris), installe les dépendances,
met en place le service systemd et affiche les logs. Relancer le script met à
jour le code sans écraser le cache ni le carnet de devoirs.

### Le fuseau horaire du serveur

Les horaires de `config.yaml` (`briefing_matin: "08:00"`, `silence_de`, …) sont
lus en **heure locale du serveur**, sans conversion. Un VPS livré en UTC
enverrait donc le briefing de 8 h à 10 h heure de Paris. À régler une fois :

```bash
sudo timedatectl set-timezone Europe/Paris
systemctl --user restart assistant-cyu
```

`installer.sh` le vérifie et te prévient si ce n'est pas fait.

### Piloter le service

```bash
journalctl --user -u assistant-cyu -f      # les logs en direct
systemctl --user restart assistant-cyu
systemctl --user stop assistant-cyu
```

> Ne fais pas tourner le bot ici **et** là-bas en même temps : tu recevrais
> chaque briefing en double.

---

## Les fichiers

| Fichier | Rôle |
| --- | --- |
| `config.yaml` | **toute** la configuration et les secrets — jamais sur GitHub |
| `config.example.yaml` | toutes les options documentées, et les valeurs par défaut |
| `config.py` | lit le YAML, complète avec l'exemple, expose les constantes |
| `celcat.py` | connexion CELCAT, lecture des cours, cache disque |
| `changements.py` | rapproche deux versions de l'EDT et dit ce qui a bougé |
| `vue.py` | toute la mise en forme texte : journée, grille, devoirs, .ics |
| `image.py` | **tout le rendu en images** : la journée, la période, les changements, les devoirs, le prochain cours, le tableau de bord de la semaine (Pillow) |
| `actu.py` | le suivi des changements : référence sur disque, garde-fous, anti-doublon, journal d'un mois |
| `devoirs.py` | le carnet de devoirs et ses échéances « prochain cours de X » |
| `meteo.py` | la météo Open-Meteo : le temps de ton trajet, le parapluie, la marge de pluie |
| `stats.py` | ce que pèse une semaine : heures, matières, trous, archive des semaines |
| `notif.py` | l'aiguillage Discord : qui poste quoi, où, et quoi se réécrit |
| `statut.py` | le panneau de `#statut` et le tableau de `#edt` |
| `assistant.py` | le daemon et la ligne de commande |
| `interface.py` | l'allure du bot : les cartes (conteneur coloré, photo intégrée, boutons persistants) |
| `bot.py` | le bot Discord : commandes slash, navigation par boutons, panneau, formulaire |
| `uptime.py` | la surveillance du webmail |
| `installer.sh` | à lancer **sur le serveur** : dépendances, service, démarrage |
| `deployer.sh` | à lancer **depuis ta machine** : envoie tout en SSH, puis installe |
| `assistant-cyu.service` | l'unité systemd du bot |
| `uptime-cyu.service` | l'unité systemd de la surveillance webmail (optionnelle) |
| `requirements.txt` | les dépendances Python — **discord.py 2.7 au minimum** pour les cartes |
| `donnees/` | cache, devoirs, état — local, jamais sur GitHub |

---

## Sécurité

`config.yaml` contient le jeton du bot et ton mot de passe CYU. Il est dans
`.gitignore` : il ne part pas sur GitHub, même en dépôt privé. Le dossier
`donnees/` non plus.

Si le jeton du bot fuit : portail développeur Discord → Bot → **Reset Token**,
l'ancien devient aussitôt inutilisable.

Rien n'est envoyé ailleurs qu'à `celcat-calendar.cyu.fr`, `mail.etu.cyu.fr` et
`discord.com`.
