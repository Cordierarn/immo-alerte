# Nouvelles sources enquêtées avec Firecrawl — plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Plus de notifications d'annonces neuves sur les veilles `logement` et `coloc`, en ajoutant ou élargissant des sources dont l'apport a été mesuré.

**Architecture:** Un outil de mesure (`mesure_source.py`) dit combien d'annonces d'une source candidate passent les filtres et sont inconnues de `seen*.json`, avec la règle exacte de `passage()`. Firecrawl sert à l'enquête depuis la session de dev ; ce qui est branché tourne en accès direct ou via Scrapfly, par la config `agences` quand c'est possible, sinon par un provider dédié.

**Tech Stack:** Python 3, `requests`, BeautifulSoup, Scrapfly (`scrapfly_client.py`), Firecrawl CLI v1.26 (dev uniquement). Pas de framework de test dans le projet : les vérifications sont des commandes avec leur sortie attendue.

**Spec:** `docs/superpowers/specs/2026-10-26-sources-firecrawl-design.md`

## Global Constraints

- Zone **fixée** : les 15 communes de `config.json` (Lyon 1er à 9e, Villeurbanne, Vaulx-en-Velin, Décines-Charpieu, Saint-Priest, Bron, Vénissieux), ni ajout ni retrait.
- Critères inchangés : prix, meublé, pièces, mots exclus.
- Firecrawl n'est jamais appelé par le workflow ; plafond de la campagne : **600 crédits** Firecrawl.
- Appels Scrapfly d'essai : plafond **2 000 crédits**, `--dev` (cache 1 h) dès qu'une page est relue.
- Nouvelles sources en production : **+60 000 crédits Scrapfly/mois** au plus ; chaque source payante a ses `heures` et son `cout_max`.
- Chemin d'accès, par préférence : direct gratuit > Scrapfly 1 cr > Scrapfly ASP 25-35 cr > `render_js` (+5).
- Aucune nouvelle dépendance de production ; aucun secret dans le dépôt (public).
- **Avant chaque commit** : `git checkout seen.json seen_coloc.json seen_parking.json geo_cache.json budget.json`. Un `seen*.json` modifié en local et commité marquerait des annonces vues sans notification.
- Commits sur `main` (le workflow y commite à chaque passage), style `ajout:` / `maj:` / `fix:` / `doc:`, en français.
- Veille `parking` hors périmètre ; sites à connexion obligatoire hors périmètre.
- Stock existant d'une nouvelle source : notifié, sauf si la mesure en compte **plus de 20** (décision au point d'étape).

## Review Focus

1. **Annonce d'une commune retirée** (Caluire 69300, Meyzieu 69330...) renvoyée par une source départementale : elle doit être écartée par `zone.py`. Test dans la tâche 1, étape 3.
2. **Bien marqué « Meublé : Non » ou « non meublé »** : il ne doit jamais passer pour un meublé. Test dans la tâche 1, étape 3, et contrôle des parseurs dédiés à la tâche 6, étape 2.
3. **Page servie en 200 mais vide** (mur de cookies, challenge, structure changée) : il faut un journal « AUCUNE carte reconnue », jamais un « 0 retenue » muet. Contrôle à la tâche 6, étape 3.
4. **Même logement sur deux sites avec une surface arrondie différemment** (29,5 / 30 m²) : l'empreinte ne le rapproche pas, d'où une double notification. On l'accepte (règle du projet : rater coûte plus cher que doubler), mais la liste des neuves est relue à l'œil avant chaque branchement (tâche 2, étape 5).
5. **Escalade de coût Scrapfly au-delà de `cout_max`** : la requête échoue sans être facturée et c'est journalisé ; `cout_max` = coût mesuré + 30 %. Réglé à la tâche 4, étape 1, et à la tâche 6, étape 1.

---

### Task 1: Outil de mesure `mesure_source.py`

**Files:**
- Modify: `immo_alerte.py:497-509` (boucle `nouvelles` de `passage()`)
- Create: `mesure_source.py`

**Interfaces:**
- Produces:
  - `immo_alerte.est_connue(annonce: dict, seen: set[str]) -> bool` : vrai si `annonce["id"] in seen`, ou si son empreinte est dans `seen` **sans** la marque de la même source (`marque_source`). C'est la règle actuelle de `passage()`, extraite telle quelle.
  - CLI `python mesure_source.py <recherche> <mode> [args]`, sans jamais écrire `seen*.json` :
    - `--agence FICHIER.json` : une entrée au format de la clé `agences`, lue par `providers_agences.provider_agences` (on remplace `providers_agences.CONFIG["agences"]` par `[site]` le temps de l'appel) ;
    - `--scrapfly CLE [URL ...]` : un provider de `PROVIDERS_SCRAPFLY` via `immo_alerte.providers_payants(cfg2, zone, garder, dev=True)`, où `cfg2["providers"] = {CLE: {**config_existante, "enabled": True, "search_url": URLs}}` (les URLs de la config si aucune n'est donnée) ;
    - `--gratuit NOM` : un provider de `PROVIDERS_GRATUITS` (nom tel qu'il figure dans le tuple) ;
    - `--annonces FICHIER.json` : une liste brute `[{"titre", "description", "prix", "surface", "pieces", "cp", "ville", "meuble": true|false|null, "coloc": true|false|null, "url"}]` issue de l'enquête. On applique `zone.accepte(cp=, ville=)`, `prix_max`, `pieces_max`, `meuble` (`false` écarte, `null` garde et compte « meublé inconnu »), puis `garder(titre, description, coloc)`. Les annonces sans `id` reçoivent `"id": url` et `"source": "candidat"`.
  - Sortie : `N retenue(s), K neuve(s)`, puis une ligne par neuve : `prix € | surface m² | ville | url`.

- [ ] **Step 1: Extraire `est_connue` dans `immo_alerte.py`**, juste après `marque_source`, et l'utiliser dans la boucle de `passage()` à la place de la condition en ligne (comportement inchangé).

- [ ] **Step 2: Vérifier `est_connue` sur les quatre cas de la règle**

Run:
```bash
python3 -c "
from immo_alerte import est_connue
a = {'id': 'x-1', 'prix': 600, 'surface': 25, 'ville': 'Lyon 8e', 'source': 'A'}
assert est_connue(a, {'x-1'})                                        # id connu
assert est_connue(a, {'emp:lyon8|600|25'})                          # jumeau d'un autre site
assert not est_connue(a, {'emp:lyon8|600|25', 'emp:lyon8|600|25@A'})  # même site : deux annonces
assert not est_connue({**a, 'surface': None}, {'emp:lyon8|600|25'})     # sans surface : pas de rapprochement
print('ok')"
```
Expected: `ok`.

- [ ] **Step 3: Écrire `mesure_source.py` et le tester en mode `--annonces`** avec un fichier de trois annonces (scratchpad) : une à Lyon 69003 à 600 € meublée, une à Caluire 69300 à 600 € meublée, une à Villeurbanne 69100 à 600 € avec `"meuble": false`.

Run: `python3 mesure_source.py logement --annonces $ENQUETE/test.json`
Expected: `1 retenue(s), 1 neuve(s)` ; seule l'annonce de Lyon 3e est listée.

- [ ] **Step 4: Contrôler les modes branchés sur une source existante**

Run: `python3 mesure_source.py logement --gratuit Immojeune` puis `python3 mesure_source.py logement --agence $ENQUETE/lodgis.json` (l'entrée Lodgis copiée depuis `config.json`).
Expected : des retenues > 0 et presque aucune neuve, puisque ces sources tournent déjà et que leurs identifiants sont dans `seen.json`. `git status` ne montre aucun `seen*.json` modifié.

- [ ] **Step 5: Commit**

```bash
git checkout seen.json seen_coloc.json seen_parking.json geo_cache.json budget.json
git add immo_alerte.py mesure_source.py
git commit -m "ajout: mesure_source.py, apport d'une source candidate avant branchement"
```

---

### Task 2: Enquête Firecrawl et mesure des candidats

**Files:**
- Modify: `.gitignore` (ajouter `.firecrawl/`)
- Hors dépôt : `$ENQUETE/` = `<scratchpad>/enquete/`, une fiche `<slug>.md` par candidat, plus `credits.txt`

**Interfaces:**
- Consumes: `mesure_source.py` (tâche 1), tous ses modes.
- Produces: `$ENQUETE/<slug>.md` avec ces rubriques fixes : `URL de recherche`, `Pagination`, `Filtres serveur`, `Données` (HTML / `__NEXT_DATA__` / `__NUXT_DATA__` / JSON-LD / API interne + URL), `Accès` (direct / Scrapfly + coût mesuré), `Champs` (loyer, surface, pièces, cp, meublé carte/fiche, coloc), `Mesure` (sortie de `mesure_source.py`), `Rythme` (dates visibles ?), `Branchement` (`agences` / provider dédié). Plus, pour un élargissement, la ligne de config proposée.

- [ ] **Step 1: Ajouter `.firecrawl/` au `.gitignore`** et noter les crédits de départ : `firecrawl --status` → `$ENQUETE/credits.txt`. Arrêter l'enquête dès que **600 crédits** sont consommés (relire `--status` toutes les 10 fiches).

- [ ] **Step 2: Compléter la liste des candidats par la recherche web**

Run, en sortie vers `$ENQUETE/recherche-<n>.json` :
```bash
firecrawl search "location appartement meublé Lyon agence immobilière réseau" --country FR --limit 20 --json
firecrawl search "résidence étudiante jeunes actifs Lyon Villeurbanne studio meublé" --country FR --limit 20 --json
firecrawl search "coliving Lyon chambre meublée" --country FR --limit 15 --json
firecrawl search "location studio Lyon particulier site annonces" --country FR --limit 15 --json
```
Liste de départ, complétée par ces résultats sans les doublons des sources déjà branchées (README, « Sources couvertes ») :
- écartés à revoir : Orpi, Guy Hoquet, Crous (trouverunlogement.lescrous.fr), Stéphane Plaza ;
- élargissements : SeLoger (Villeurbanne, Vaulx-en-Velin, Décines-Charpieu), Century 21 (Décines-Charpieu), Foncia et Laforêt (Vaulx-en-Velin, Décines, Bron, Vénissieux, Saint-Priest), Immojeune (`pages_max` 3 → 5).

- [ ] **Step 3: Enquêter sur chaque candidat**, par ordre de taille de stock présumée, et remplir sa fiche :
  - URL de recherche : `firecrawl map <domaine> --search "location lyon" --limit 50` ;
  - page rendue : `firecrawl scrape <url> --format markdown,links -o $ENQUETE/<slug>.json` ;
  - données embarquées : `firecrawl scrape <url> --format rawHtml -o $ENQUETE/<slug>.html`, puis `grep -o '__NEXT_DATA__\|__NUXT_DATA__\|application/ld+json\|/api/[a-z0-9/_-]*' $ENQUETE/<slug>.html | sort | uniq -c` ;
  - accès direct : `curl -s -o /dev/null -w '%{http_code}' -A "$(python3 -c 'import immo_alerte;print(immo_alerte.UA["User-Agent"])')" <url>`. Si ce n'est pas 200 avec les annonces dans le HTML, faire **un** appel Scrapfly d'essai (`asp=True`, `cache=True`) et noter le coût renvoyé.

- [ ] **Step 4: Mesurer chaque candidat accessible**
  - cartes HTML compatibles avec le provider générique : écrire l'entrée au format `agences` dans `$ENQUETE/<slug>-site.json`, puis `python3 mesure_source.py logement --agence $ENQUETE/<slug>-site.json` (et `coloc`) ;
  - élargissement SeLoger : `python3 mesure_source.py logement --scrapfly seloger <les 3 URLs>` ;
  - autres cas (JSON, API) : extraire les annonces de la sortie Firecrawl dans `$ENQUETE/<slug>-annonces.json` (format de la tâche 1), puis `--annonces`.

  Expected : chaque fiche a une ligne `Mesure` avec `N retenue(s), K neuve(s)` pour `logement` et `coloc`.

- [ ] **Step 5: Relire à l'œil les neuves de chaque candidat** (Review Focus 4) : un même prix dans la même ville avec 1 m² d'écart par rapport à une annonce connue est un doublon probable, à décompter dans la fiche.

- [ ] **Step 6: Commit** (`.gitignore` seul ; les fiches restent hors dépôt)

```bash
git checkout seen.json seen_coloc.json seen_parking.json geo_cache.json budget.json
git add .gitignore && git commit -m "maj: .firecrawl/ ignoré"
```

---

### Task 3: Classement et point d'étape

**Files:**
- Hors dépôt : `$ENQUETE/classement.md`

**Interfaces:**
- Consumes: les fiches de la tâche 2.
- Produces: la liste **validée par l'utilisateur** des candidats à brancher, dans l'ordre, avec pour chacun `agences` ou `dédié`, `heures`, `cout_max` et la décision sur son stock existant.

- [ ] **Step 1: Construire le tableau** : candidat | neuves logement | neuves coloc | rythme | accès | crédits/mois (= coût par page × pages × passages/jour × 30) | branchement | effort. Trier par neuves rapportées aux crédits/mois (les sources gratuites en tête à apport égal). Barrer ce qui ferait dépasser le cumul de +60 000 crédits/mois.

- [ ] **Step 2: Présenter le tableau à l'utilisateur et attendre son choix.** Signaler toute source dont le stock existant dépasse 20 annonces neuves (notifier ou mémoriser sans notifier ?). Ne rien brancher avant la réponse.

---

### Task 4: Élargissements retenus (config seule)

**Files:**
- Modify: `config.json` (clés `providers.seloger.search_url` des recherches concernées, `agences[*].urls`, `gratuits.immojeune.pages_max`)
- Modify: `README.md` (lignes SeLoger, Century 21, Foncia, Laforêt des tableaux « Sources couvertes » et « Les sites d'agences » ; projection de la section « Budget Scrapfly »)

**Interfaces:**
- Consumes: la liste validée de la tâche 3.

- [ ] **Step 1: Ajouter les URLs retenues dans `config.json`**, en ajustant `cout_max` au coût mesuré + 30 % si une nouvelle URL coûte plus que les existantes.

- [ ] **Step 2: Vérifier chaque élargissement avec la config réelle**

Run: `python3 mesure_source.py logement --scrapfly seloger` (sans URL, donc celles de la config), `python3 mesure_source.py logement --agence $ENQUETE/<site>-site.json` pour les agences, recopié depuis la nouvelle config.
Expected : les retenues incluent des annonces des nouvelles communes, et aucune annonce hors des 15 communes.

- [ ] **Step 3: Mettre à jour le README** (couverture, coût, et la mesure qui a justifié l'ajout, dans le ton « après mesure » des sections existantes).

- [ ] **Step 4: Commit**

```bash
git checkout seen.json seen_coloc.json seen_parking.json geo_cache.json budget.json
git add config.json README.md
git commit -m "maj: <source> élargie à <communes> (<K> annonces neuves mesurées)"
```

---

### Task 5: Sites retenus compatibles avec le provider générique

**Files:**
- Modify: `config.json` (clé `agences`, une entrée par site)
- Modify: `README.md` (tableau « Les sites d'agences » ; retirer le site de la liste « Écartés après mesure » s'il y figurait)

**Interfaces:**
- Consumes: `$ENQUETE/<slug>-site.json` (tâche 2), la liste validée (tâche 3).
- Produces: une entrée `agences` par site, avec les clés documentées en tête de `providers_agences.py` (`nom`, `urls`, `pages`, `carte`, `lien`, `base`, `meuble`, `coloc`, `scrapfly`).

- [ ] **Step 1: Ajouter l'entrée du site** (une à la fois), recopiée de la fiche.

- [ ] **Step 2: Lancer le passage complet de la recherche, sans Scrapfly sauf si le site en dépend**

Run: `python3 immo_alerte.py --recherche=logement --gratuit` (ou sans `--gratuit` avec `--dev` pour un site Scrapfly), sans `topic.txt` ni variable `NTFY_TOPIC` dans l'environnement : `notifier` journalise alors `AUCUN TOPIC configuré` et ne mémorise pas les neuves.
Expected : une ligne `<Nom>: N retenue(s) sur M carte(s)` avec M > 0, et N cohérent avec la mesure de la fiche. Aucune ligne `AUCUNE carte reconnue`.

- [ ] **Step 3: Mettre à jour le README, puis commit** (un commit par site)

```bash
git checkout seen.json seen_coloc.json seen_parking.json geo_cache.json budget.json
git add config.json README.md
git commit -m "ajout: <site> (<K> annonces neuves mesurées)"
```

---

### Task 6: Sites retenus nécessitant un provider dédié

Une itération de cette tâche par site, dans l'ordre du classement.

**Files:**
- Modify: `providers_gratuits.py` (accès direct) **ou** `providers_scrapfly.py` (Scrapfly) : un parseur `_<cle>_annonces` et un `provider_<cle>`, enregistrés dans `PROVIDERS_GRATUITS` ou `PROVIDERS_SCRAPFLY`
- Modify: `config.json` (bloc `gratuits.<cle>` ou `providers.<cle>` de `logement` et/ou `coloc`)
- Modify: `immo_alerte.py:150` (`ORDRE_SOURCES` : particuliers avant agences, agrégateurs en fin)
- Modify: `README.md` (tableau « Sources couvertes » et un paragraphe sur ce qui a débloqué le site)

**Interfaces:**
- Consumes: la fiche du site (où vivent les données), la liste validée.
- Produces, sur le modèle des providers voisins :
  - gratuit : `provider_<cle>(cfg, zone, garder, log) -> list[dict]` ;
  - Scrapfly : `provider_<cle>(cfg, zone, sf_cfg, garder, log, dev=False) -> list[dict]`, qui délègue à `_passage("<cle>", "<Nom>", _<cle>_annonces, cfg, zone, sf_cfg, garder, log, dev)` ;
  - parseur : `_<cle>_annonces(html, cfg, zone, garder) -> tuple[list[dict], int]` (annonces retenues, cartes vues) ;
  - chaque annonce : `{"id": "<cle>-<id du site>", "titre", "prix": int, "surface": float|None, "ville": nom de commune de la zone, "url", "source": "<Nom>"}`, plus `lat`/`lon` si le site les donne. Les JSON embarqués se lisent avec les helpers existants `_next_data`, `_nuxt_data`, `_jsonld` de `providers_scrapfly.py`.

- [ ] **Step 1: Écrire le parseur et le provider**, puis ajouter la config (`enabled`, `search_url`, et pour Scrapfly `heures` + `cout_max` = coût mesuré + 30 %) et la place dans `ORDRE_SOURCES`.

- [ ] **Step 2: Mesurer avec le vrai provider**

Run: `python3 mesure_source.py logement --scrapfly <cle>` ou `--gratuit <Nom>` (et `coloc` si activé).
Expected : `N retenue(s), K neuve(s)` proche de la fiche. Aucune retenue non meublée (vérifier les annonces dont le texte contient « non meublé » ou « Meublé : Non »), et aucune hors des 15 communes.

- [ ] **Step 3: Vérifier la page vide** : passer au parseur une page sans annonce (`'<html><body></body></html>'`).
Expected : `([], 0)`. Pour un provider Scrapfly, `_passage` écrit alors `debug_<cle>0.html` et le journalise. Pour un provider gratuit, il journalise lui-même `<Nom>: AUCUNE carte reconnue`.

- [ ] **Step 4: Mettre à jour le README, puis commit**

```bash
git checkout seen.json seen_coloc.json seen_parking.json geo_cache.json budget.json
git add providers_*.py immo_alerte.py config.json README.md
git commit -m "ajout: <site> via <accès> (<K> annonces neuves mesurées)"
```

---

### Task 7: Mise en production et bilan à une semaine

**Files:**
- Modify: `README.md` (bilan chiffré, section de la source concernée)

- [ ] **Step 1: Pousser, avec l'accord de l'utilisateur** : `git pull --rebase origin main && git push origin main`. Expected : le prochain run de *Veille immo* journalise chaque nouvelle source avec M > 0 cartes (`gh run view --log` sur le dernier run).

- [ ] **Step 2: Une semaine plus tard, compter les notifications par source** dans les journaux du workflow (`notifier` écrit `NOUVEAU [<Source>] ...` pour chaque envoi ; `seen.json` ne convient pas, il mémorise aussi les doublons reconnus) :

```bash
for id in $(gh run list --workflow=veille.yml --created ">=$(date -d '7 days ago' +%F)" --limit 100 --json databaseId -q '.[].databaseId'); do
  gh run view "$id" --log
done | grep -o 'NOUVEAU \[[^]]*\]' | sort | uniq -c | sort -rn
```
Expected : un compteur par source, où figurent les nouvelles sources. Le comparer à la mesure de la tâche 2 et à la semaine précédente (même commande avec `--created` décalé de 7 jours).

- [ ] **Step 3: Consigner le bilan dans le README et commit** (`doc: bilan à une semaine des nouvelles sources`). Une source à 0 neuve sur la semaine est signalée à l'utilisateur pour être désactivée (`enabled: false`), pas supprimée.
