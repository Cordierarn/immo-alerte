<p align="center">
  <img src="logo.svg" width="160" alt="immo-alerte"/>
</p>

<h1 align="center">immo-alerte</h1>

<p align="center">
  <b>Veille logement personnelle, auto-hébergée et gratuite.</b><br/>
  Scanne les annonces de location toutes les 15 minutes et envoie une notification
  push dès qu'une annonce correspond à tes critères.
</p>

---

## Comment ça marche

```
GitHub Actions — 8h, 10h, 12h, 13h, 15h, 18h, 20h (heure de Paris)
        │
        ├── GRATUIT ─ Bien'ici   (API JSON interne)
        │             Immojeune  (meublé étudiant / jeune actif)
        │             ParuVendu  (particuliers)
        │
        ├── PAYANT ── Leboncoin  (Scrapfly + ASP, DataDome)
        │             PAP        (Scrapfly + ASP, Cloudflare)
        │             SeLoger    (Scrapfly + ASP, communes prioritaires)
        │                  ▲
        │                  └── 3 garde-fous : créneaux horaires, enveloppe
        │                      mensuelle, réserve de crédits du compte
        ▼
   filtres : villes, prix max, meublé, exclusion coloc/chambres
        │
        ▼
   dédoublonnage (seen.json, commité entre les runs)
        │
        ▼
   📱 notification push ntfy avec prix, surface, ville et lien direct
```

Aucun serveur à gérer : le scan tourne sur GitHub Actions (gratuit pour les
dépôts publics), la mémoire des annonces déjà vues est committée dans le dépôt
entre deux passages, et les notifications passent par [ntfy.sh](https://ntfy.sh)
(gratuit, sans compte).

## Critères

Tout se règle dans [`config.json`](config.json) :

| Clé | Rôle |
|---|---|
| `villes` | Zone surveillée : une entrée par commune (voir ci-dessous) |
| `prix_max` | Loyer maximum (charges comprises selon les annonces) |
| `meuble` | `true` = meublés uniquement |
| `exclure_coloc` | Écarte colocations et chambres chez l'habitant |
| `pieces_max` | Nombre de pièces maximum (T1/T2 = `2`) |
| `mots_exclus` | Mots-clés qui disqualifient une annonce (titre **et** description) |
| `mots_exclus_titre` | Mots qui ne disqualifient que dans le **titre** |

Le second existe à cause d'un faux positif coûteux : « bureau » sert à écarter
les locaux professionnels, mais dans une recherche de **meublés** il désigne
presque toujours un meuble — « lit, armoire, table, bureau ». Le chercher dans
les descriptions écartait en silence de bonnes annonces sur tous les providers
à la fois. Il est donc désormais limité au titre.

### La zone (`villes` + `zone.py`)

La zone couvre **33 communes à moins de 45 min en transports en commun** du
Parc Technologique de Saint-Priest (terminus du T2), Lyon 1er à 9e inclus.

Chaque entrée porte son ou ses codes postaux :

```json
{ "nom": "Lyon 8e", "cp": ["69008"], "prioritaire": true, "requete": "Lyon" }
```

| Clé | Rôle |
|---|---|
| `cp` | Codes postaux de la commune : **c'est la clé de rattachement** |
| `prioritaire` | Commune assez centrale pour justifier un appel SeLoger payant |
| `requete` | Nom à envoyer aux sites quand il diffère du nom affiché |
| `alias` | Autres noms acceptés (communes fusionnées) |
| `cp_only` | Reconnue au code postal seulement, jamais au nom |

Pourquoi un module dédié plutôt qu'une liste de noms : le test historique
`"Lyon 8e" in adresse` échouait sur trois des quatre écritures rencontrées
(`Lyon 8ème`, `Lyon 08`, `Lyon`). Le code postal est non ambigu et présent
presque partout, il est donc devenu la clé primaire ; le nom n'est qu'un repli
quand aucun code postal n'est disponible. Dès qu'un code postal est lisible, il
tranche seul — y compris pour **refuser**, ce qui écarte les communes « aux
alentours » que SeLoger, ParuVendu et Immojeune glissent dans leurs résultats.

Aucun portail ne connaît « Lyon 8e ». D'où `requete` : les neuf arrondissements
se replient sur une seule recherche « Lyon », et le filtrage par code postal
retient ensuite les bons. ParuVendu fait exception, il accepte le code postal
directement (`lyon-69008`).

`geo_cache.json` mémorise les identifiants de zone Bien'ici. Ils ne changent
jamais, et les redemander commune par commune dominait le temps d'exécution.
Seuls les succès sont mis en cache : figer un échec condamnerait la commune
pour toujours. Le fichier est commité par le workflow.

## Installation (fork)

1. **Fork** ce dépôt (public, pour les minutes Actions illimitées)
2. Ajuste `config.json` à ta recherche
3. Crée un topic privé ntfy (un nom improbable, ex. `immo-tonprenom-a8f3e2`)
   et ajoute-le en **secret** du dépôt : *Settings → Secrets and variables →
   Actions → New repository secret* → nom `NTFY_TOPIC`.
   Plusieurs topics ? Sépare-les par des virgules : chaque alerte part sur
   tous (utile pour deux téléphones, ou pour doubler un topic perdu).
4. Installe l'app [ntfy](https://ntfy.sh) ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) / [iOS](https://apps.apple.com/app/ntfy/id1625396347)) et abonne-toi à ton topic
5. Active les workflows dans l'onglet **Actions**, puis lance *Veille immo* →
   *Run workflow* une première fois

Le premier passage mémorise le stock existant ; les suivants n'alertent que
sur les **nouvelles** annonces.

### Usage local (optionnel)

```bash
pip install -r requirements.txt
python immo_alerte.py --init   # premier passage : mémorise sans notifier
python immo_alerte.py          # passages suivants (à planifier)
```

En local, mets un topic par ligne dans `topic.txt` (gitignoré), ou renseigne
`ntfy_topic` dans `config.json` (chaîne ou liste), ou exporte `NTFY_TOPIC`.
Les trois sources s'additionnent et sont dédoublonnées : une alerte part sur
tous les topics connus, et un appui sur la notification ouvre l'annonce.

## Sources couvertes

| Site | Statut | Coût | Raison |
|---|---|---|---|
| Bien'ici | ✅ intégré | 0 | API JSON interne accessible |
| Immojeune | ✅ intégré | 0 | Meublé étudiant / jeune actif : exactement le segment visé |
| ParuVendu | ✅ intégré | 0 | Beaucoup de particuliers, HTML servi tel quel |
| Leboncoin | ✅ Scrapfly | 30 cr/appel | DataDome → ASP. Source la plus riche en particuliers |
| PAP | ✅ Scrapfly | 40-80 cr/appel | Cloudflare. **Une seule requête départementale** (voir plus bas) |
| SeLoger | ✅ Scrapfly | 35 cr × 7 URL | DataDome. Une URL par commune + une pour Lyon entier |
| Logic-Immo | ❌ | — | Tourne sur la plateforme SeLoger : même stock, payé deux fois |
| Avendrealouer | ❌ | — | DataDome aussi, apport marginal sur la zone |
| Studapart / Lokaviz | ❌ | — | Connexion obligatoire avant toute recherche |

Le raisonnement : sur une recherche T1/T2 meublé ≤ 650 €, **Leboncoin reste la
seule source payante qui apporte du stock qu'on n'a pas ailleurs** (bailleurs
particuliers). Les portails d'agences se recopient largement entre eux, et
Bien'ici — gratuit — en couvre déjà l'essentiel. Immojeune et ParuVendu ont été
ajoutés parce qu'ils sont gratuits et couvrent deux angles morts : le meublé
étudiant d'un côté, les particuliers hors Leboncoin de l'autre.

Deux points mesurés qui contredisent des choix antérieurs :

- **ParuVendu n'est pas vide** sur l'est lyonnais (23 annonces retenues lors
  d'un passage de contrôle). L'évaluation précédente portait sur une URL de
  recherche invalide, qui renvoyait une page sans résultats.
- **PAP était muet, pas vide.** Le site répond `403` derrière un challenge
  Cloudflare, et le code avalait ce refus sans rien journaliser : le provider
  annonçait « 0 annonce » comme si la zone n'avait rien à offrir. Il est
  désormais passé sous Scrapfly, et tout refus est journalisé.

### Le cas Logic-Immo

Écarté après mesure, et non par principe. Deux appels réels ont montré que la
page d'accueil de Logic-Immo sert le composant de recherche **de SeLoger**
(`data-testid="refiner-form-test-id"`, classes `css-*`) et mentionne 32 fois
« seloger » : les deux sites appartiennent au groupe Aviv et partagent
désormais la même plateforme, donc le même stock. L'intégrer reviendrait à
payer deux fois les mêmes annonces.

S'y ajoute un obstacle technique : `/location-immobilier.php` redirige vers
`/?tab=rent`, une application monopage dont les résultats sont rendus côté
navigateur. Les récupérer imposerait `render_js` (+5 crédits) *et* une
rétro-ingénierie du schéma d'URL de recherche.

Le code du provider reste en place (`provider_logicimmo`) avec
`enabled: false`, au cas où les deux sites divergeraient à nouveau.

### Le cas PAP

PAP est passé derrière Cloudflare, qui répond `403` aux pages **comme à
l'autocomplete**. Le provider est donc devenu payant. Sa remise en service a
mis au jour deux choses :

- **Les g-codes codés en dur ne valaient plus rien.** `g35406`, commenté
  « Bron », sert en réalité les annonces de *Charentay* (69220), un village à
  40 km. PAP interrogeait donc les mauvaises communes bien avant le blocage.
- **La page départementale coûte moitié moins que la page communale** (40
  crédits contre 80) tout en couvrant les 33 communes d'un coup. Le stock de
  PAP sous 650 € tient largement sur une page.

D'où la forme actuelle : une seule URL, `g433` (Rhône), et c'est le filtrage
par code postal de `zone.py` qui fait le tri. Le coût observé varie de 40 à 80
crédits selon la difficulté du challenge, d'où un `cout_max` à 90.

## Budget Scrapfly

Plan Discovery : 200 000 crédits/mois, **plafond dur** (pas de dépassement
facturé, les scrapes échouent). Grille de coût :

| Élément | Crédits |
|---|---|
| Requête HTTP, proxy datacenter | 1 |
| Requête HTTP, proxy résidentiel | 25 |
| `render_js=true` (navigateur) | +5 |
| Réponse servie par le cache | **1** (mesuré ; la doc annonce 0) |
| Scrape échoué | 0 (sauf >30 % d'échecs/heure) |

Un appel Leboncoin passe par l'ASP, qui bascule sur du résidentiel : **30
crédits** mesurés en conditions réelles. Toute la configuration vise donc à
faire *un seul appel par site et par passage*.

À noter : Leboncoin applique bien `furnished` et `rooms` depuis l'URL mais
**ignore `real_estate_type`** — on reçoit maisons, bureaux et locaux au milieu
des appartements. Le refiltrage se fait en local, gratuitement. Idem pour les
demandes déguisées en offres (« Recherche studio sur Lyon » arrive avec
`ad_type=offer`) : seul le titre les trahit.

### Les trois garde-fous (`scrapfly_client.py`)

1. **Créneaux horaires** — sept passages par jour, à 8 h, 10 h, 12 h, 13 h,
   15 h, 18 h et 20 h (heure de Paris), un seul appel par créneau. Un run
   GitHub en retard rattrape le créneau échu au lieu de le perdre ; hors
   créneau, le passage s'arrête avant tout appel et ne coûte rien.
2. **Enveloppe mensuelle** — `budget.json` compte les crédits dépensés par ce
   projet ; au-delà de `budget_mensuel`, plus aucun appel.
3. **Réserve du compte** — chaque réponse renvoie le crédit restant du compte
   (tous projets confondus). En dessous de `reserve_autre_projet`, on coupe :
   c'est ce qui garantit que la veille immo n'assèche pas l'autre projet.

Projection : **~6 300 crédits/mois** pour Leboncoin (7 passages/jour, 30 cr),
**~22 050** pour SeLoger (3 passages/jour × 7 URL × 35 cr) et **~5 400** pour
PAP (3 passages/jour, ~60 cr en moyenne), soit **~33 750 au total, 20 % du
quota**.

L'élargissement de la zone à 33 communes n'a coûté que ~6 000 crédits/mois,
parce que le surcoût est concentré sur SeLoger, seul provider facturé à la
commune. Leboncoin fait tenir les 32 communes dans **une seule URL**, et les
trois providers gratuits couvrent la zone entière sans rien dépenser. C'est la
raison pour laquelle SeLoger est limité aux communes `prioritaire` : le passer
sur les 33 communes coûterait ~104 000 crédits/mois, soit les deux tiers du
quota pour un stock largement redondant avec Bien'ici.

La contrainte n'est donc toujours pas le budget mais la **réactivité** : une
annonce publiée à 10 h 05 n'est signalée qu'à 12 h. Ajouter une heure dans
`heures` coûte ~915 crédits/mois côté Leboncoin, ~8 500 côté SeLoger.

### Le cas SeLoger

SeLoger a abandonné `list.htm`. Le schéma actuel n'accepte **qu'une localité
par URL**, d'où `search_url` qui prend une liste. L'URL départementale a été
testée et écartée : sur 30 annonces de la page 1, 4 seulement étaient dans la
zone, toutes des colocations — la page est saturée par Lyon et Villeurbanne.

Son JSON-LD ne contient que des agrégats (nombre d'annonces, fourchette de
prix). Les données sont donc lues dans les cartes HTML, via les attributs
`data-testid` — nettement plus stables que les classes CSS générées.

Rendement observé : 4 annonces exploitables sur les 6 communes d'origine, contre
~20 pour Leboncoin. Beaucoup de colocations sous 650 € et un stock qui recoupe
Bien'ici, déjà gratuit. Si le budget devenait un sujet, c'est le premier à
désactiver.

C'est ce faible rendement, combiné à la facturation à la commune, qui justifie
de le restreindre aux communes `prioritaire` plutôt que de l'étendre à toute la
zone. Les deux URL d'arrondissement (`lyon-8eme-69008`, `lyon-3eme-69003`)
**n'ont pas pu être vérifiées** : DataDome bloquant tout accès direct, un slug
invalide ne se distingue pas d'un slug correct avant le premier appel réel. Si
elles sont fausses, le premier passage écrira `debug_seloger6.html` /
`debug_seloger7.html` et il suffira de corriger `search_url`.

### Réglages

Tout est dans la section `scrapfly` de [`config.json`](config.json) :

| Clé | Rôle |
|---|---|
| `enabled` | Coupe-circuit global des providers payants |
| `budget_mensuel` | Crédits max que ce projet peut consommer dans le mois |
| `reserve_autre_projet` | Crédits du compte à ne jamais entamer |
| `cout_max_par_requete` | `cost_budget` Scrapfly : plafond dur par appel |
| `heures` | Heures de passage (Paris), ex. `[8, 10, 12, 13, 15, 18, 20]` |
| `providers.*.search_url` | URL de recherche, **une seule pour toute la zone** |
| `providers.*.heures` | Créneaux spécifiques à un site (SeLoger : 8 h, 13 h, 18 h) |

Modifier `heures` suffit : le cron GitHub couvre déjà toute la plage 8 h–20 h,
c'est `config.json` qui décide des passages retenus. Le mode historique
`cadence` (intervalle variable en secondes) reste accepté si `heures` est absent.

### Mise en place

1. Récupère ta clé API Scrapfly et crée un **projet dédié** (le plan Discovery
   en autorise 2) : les dashboards de coût restent séparés de l'autre projet.
2. Ajoute-la en secret du dépôt : *Settings → Secrets and variables → Actions*
   → nom `SCRAPFLY_KEY`. En local, mets-la dans `scrapfly_key.txt` (gitignoré).
3. **Vérifie l'URL de recherche** : ouvre `providers.leboncoin.search_url` dans
   ton navigateur. Elle doit afficher exactement les annonces voulues, triées
   par date, pour les six communes en une seule page. C'est cette URL unique
   qui évite de payer six requêtes.
4. Premier essai en mode cache (les rejeux ne coûtent rien) :
   ```bash
   python immo_alerte.py --dev --init
   ```

### Commandes

```bash
python immo_alerte.py --budget    # crédits consommés ce mois, par site
python immo_alerte.py --gratuit   # passage sans aucun appel payant
python immo_alerte.py --dev       # appels mis en cache : rejeux à 0 crédit
```

Si un parsing casse, le HTML est écrit dans `debug_<site>.html` et le mode
`--dev` permet d'itérer dessus sans rebrûler de crédits.

## Notes

- **Usage personnel et modéré** : un passage par quart d'heure sur une recherche
  ciblée, pas d'extraction massive. Reste courtois avec les sites sources.
- Le cron GitHub Actions peut avoir quelques minutes de retard aux heures de
  pointe, c'est normal.
- Projet né d'une recherche de logement autour de Saint-Priest (69) —
  architecture inspirée de [Fredy](https://github.com/orangecoding/fredy) et
  [House-Alert](https://github.com/rbiou/House-Alert).
