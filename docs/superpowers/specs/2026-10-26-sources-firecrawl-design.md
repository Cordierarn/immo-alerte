# Plus de sources pour la veille logement, enquêtées avec Firecrawl

*Conception — 26 octobre 2026*

## Intention

Recevoir **le plus de notifications possible** d'annonces qui respectent les
critères actuels (T1/T2 meublé ≤ 650 €, ou coloc ≤ 650 €), situées à
**30-45 min maximum en transports** du 292-312 Cours du 3e Millénaire,
Saint-Priest (Parc Technologique, 45.7147, 4.9303), **et proches de Lyon**.
La zone a été ramenée le 26 octobre 2026 à 15 communes : Lyon 1er à 9e,
Villeurbanne, Vaulx-en-Velin, Décines-Charpieu, Saint-Priest, Bron, Vénissieux.

Plusieurs sites ont été écartés par le passé pour des raisons que Firecrawl
sait lever : résultats chargés en JavaScript (Orpi, Guy Hoquet, Crous), URL de
recherche introuvable (Stéphane Plaza). D'autres sources déjà branchées ne
lisent qu'une partie de la zone (SeLoger : ni Villeurbanne, ni Vaulx-en-Velin,
ni Décines ; Century 21 : pas Décines ; Foncia et Laforêt : Lyon +
Villeurbanne seulement).

**Firecrawl sert à l'enquête, Scrapfly (ou l'accès direct gratuit) au
fonctionnement quotidien.** Firecrawl n'est jamais appelé par le workflow.

## Objectifs

- Augmenter le nombre d'annonces **neuves** (jamais vues par une autre source)
  notifiées par semaine, sur les veilles `logement` et `coloc`.
- Chaque source ajoutée ou élargie est justifiée par une mesure, consignée dans
  le README comme les décisions précédentes (« Écartés après mesure »...).

## Hors périmètre

- Appeler Firecrawl depuis GitHub Actions (aucune clé Firecrawl dans les secrets).
- Modifier les critères (prix, meublé, pièces, mots exclus).
- La veille `parking` (Saint-Étienne, sans rapport avec cette adresse).
- Les sites à connexion obligatoire (Studapart, Lokaviz).
- Réécrire les providers existants au-delà de ce que l'élargissement exige.

## À éviter absolument

- Une notification hors des 15 communes.
- Deux notifications pour la même annonce.
- Une source qui casse en silence : 0 annonce sans le journaliser.
- Un dépassement de l'enveloppe Scrapfly ou une entame de la réserve du compte.
- **Commiter un `seen*.json` modifié par un passage local** : les annonces
  seraient marquées vues sans que le téléphone ait été notifié.

## Contraintes

| Sujet | Contrainte |
|---|---|
| Zone | **Fixée** : les 15 communes de `config.json`, ni ajout ni retrait. `zone.py` filtre au code postal : une source qui lit une page départementale reste dans la zone. |
| Firecrawl | 1 000 crédits/mois. Plafond de la campagne d'enquête : **600 crédits**. |
| Scrapfly (essais) | Plafond des appels d'essai pendant l'enquête : **2 000 crédits**, avec `--dev` (cache) dès qu'une page est relue. |
| Scrapfly (production) | Enveloppe 165 000, consommation ~24 000/mois depuis la réduction de zone (SeLoger passé de 7 à 4 URLs). Les nouvelles sources peuvent ajouter **jusqu'à +60 000 crédits/mois**. Chaque source payante a ses `heures` et son `cout_max`. |
| Chemin d'accès | Par ordre de préférence : accès direct gratuit > Scrapfly sans escalade (1 cr) > Scrapfly ASP résidentiel (25-35 cr) > `render_js` (+5). |
| Exécution | GitHub Actions, Python, `requests` + BeautifulSoup. Aucune nouvelle dépendance sans raison. |
| Dépôt public | Aucun secret dans `config.json` ni dans la doc. |

## Approches envisagées

1. **Enquête site par site, mesure avant branchement** — retenue.
   On ne branche que ce qui apporte des annonces absentes des autres sources.
   Plus lent, mais chaque crédit et chaque site à maintenir est justifié.
2. **Élargir les sources déjà branchées** — retenue, fondue dans la même liste.
   Gain sûr (le parseur existe), plafonné par le stock de ces sites.
3. **Tout brancher vite, laisser le dédoublonnage trier** — écartée.
   Des crédits payés pour des doublons et autant de sites susceptibles de
   casser, contraire à la règle du projet « mesurer avant de choisir ».

**Choix : 1 + 2**, en une seule liste de candidats classée par le même
indicateur (annonces neuves par semaine, rapportées au coût mensuel).

## Déroulé

### Phase 1 — Liste des candidats

- **Sites écartés à réexaminer** : Orpi, Guy Hoquet, Crous, Stéphane Plaza.
- **Nouveaux sites** trouvés par `firecrawl search` : grands réseaux absents
  de Bien'ici, résidences étudiantes et jeunes actifs (toutes meublées),
  sites de particuliers, opérateurs de coliving présents à Lyon.
  Un réseau dont les annonces figurent déjà sur Bien'ici est écarté d'office
  (même méthode que pour le choix des agences actuelles).
- **Élargissements** : SeLoger, Century 21, Foncia, Laforêt aux communes de
  la zone qu'ils ne lisent pas encore ; pages supplémentaires d'Immojeune si la
  page 3 contient encore du neuf.

### Phase 2 — Enquête, une fiche par candidat

Avec Firecrawl (`map` pour l'arborescence et les URLs de recherche, `scrape`
pour la page rendue), établir :

1. l'URL de recherche, la pagination, et si les filtres d'URL sont appliqués
   côté serveur ;
2. où vivent les données : HTML des cartes, JSON embarqué (`__NEXT_DATA__`,
   `__NUXT_DATA__`, JSON-LD), ou API JSON interne appelée par la page ;
3. le chemin d'accès le moins cher : essai en direct (`requests`), sinon **un**
   appel Scrapfly d'essai pour mesurer le coût réel ;
4. les champs disponibles : loyer, surface, pièces, code postal, meublé (sur la
   carte ou seulement sur la fiche), coloc ;
5. **la mesure** : nombre d'annonces sous critères dans la zone, et parmi elles
   combien sont absentes de `seen.json` / `seen_coloc.json` (ni l'identifiant,
   ni l'empreinte `emp:ville|prix|surface`). Le rythme de publication est
   estimé à partir des dates quand le site les affiche.

Les fiches de travail restent hors dépôt ; seule la conclusion entre dans le
README.

### Phase 3 — Classement et point d'étape

Un tableau classé : candidat, annonces neuves, rythme estimé, chemin d'accès,
coût mensuel, effort de branchement. **On le revoit ensemble avant de
brancher** ; puis on branche dans l'ordre du classement jusqu'à épuiser la
liste ou l'enveloppe de +60 000 crédits.

### Phase 4 — Branchement

- **Si les cartes HTML conviennent au provider générique** : une entrée dans la
  clé `agences` de `config.json`, sans code.
- **Sinon** : un provider dédié dans `providers_gratuits.py` ou
  `providers_scrapfly.py`, sur le modèle des existants (Figaro pour le JSON
  embarqué, Bien'ici pour une API interne).
- Chaque source : place dans `ORDRE_SOURCES` (agrégateurs en fin), journal
  « AUCUNE carte reconnue » en cas de page vide, respect de `--gratuit`.
- Un élargissement de source existante passe par sa configuration
  (`search_url`, `urls`).

**Stock existant d'une nouvelle source** : il est notifié (ce sont des
annonces jamais vues), sauf si la mesure en compte plus de 20, auquel cas on
décide au point d'étape s'il faut le mémoriser sans notifier.

## Vérification

- Pour chaque source branchée : un passage local
  `python immo_alerte.py --recherche=logement` (et `coloc`) qui journalise
  « N retenue(s) sur M carte(s) » avec N > 0 ou une raison claire, puis
  `git checkout seen*.json geo_cache.json budget.json` avant tout commit.
- Le nombre d'annonces retenues correspond à la mesure de la phase 2.
- Après une semaine en production : nombre de notifications par source, lu
  dans les journaux du workflow, comparé à la semaine précédente.

## Laissé à l'implémentation

- Les sélecteurs CSS et le détail des parseurs.
- L'ordre exact de branchement entre deux candidats proches au classement.
- La répartition des créneaux `heures` des nouvelles sources payantes.
