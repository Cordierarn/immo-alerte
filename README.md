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
GitHub Actions (toutes les 5 min)
        │
        ├── GRATUIT ─ Bien'ici   (API JSON interne)
        │             PAP        (parsing HTML, 100% particuliers)
        │
        ├── PAYANT ── Leboncoin  (Scrapfly + ASP, DataDome)
        │             SeLoger    (Scrapfly + ASP, désactivé par défaut)
        │                  ▲
        │                  └── 3 garde-fous : cadence horaire, enveloppe
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
| `villes` | Liste des communes surveillées |
| `prix_max` | Loyer maximum (charges comprises selon les annonces) |
| `meuble` | `true` = meublés uniquement |
| `exclure_coloc` | Écarte colocations et chambres chez l'habitant |
| `pieces_max` | Nombre de pièces maximum (T1/T2 = `2`) |
| `mots_exclus` | Mots-clés qui disqualifient une annonce |

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
| PAP | ✅ intégré | 0 | HTML propre, pas de protection agressive |
| Leboncoin | ✅ Scrapfly | ~30 cr/appel | DataDome → ASP. Source la plus riche en particuliers |
| SeLoger | ⚙️ Scrapfly, off | ~30 cr/appel | DataDome. Stock très redondant avec Bien'ici |
| Logic-Immo | ❌ | — | Même groupe (Aviv) et même stock que SeLoger : payer deux fois |
| Avendrealouer | ❌ | — | DataDome aussi, apport marginal sur la zone |
| ParuVendu / Ouest-France Immo | ❌ | — | Quasi rien en location sur l'est lyonnais |

Le raisonnement : sur une recherche T1/T2 meublé ≤ 650 € autour de Saint-Priest,
**Leboncoin est la seule source qui apporte du stock qu'on n'a pas ailleurs**
(bailleurs particuliers). Les portails d'agences se recopient largement entre
eux, et Bien'ici — gratuit — en couvre déjà l'essentiel. D'où l'ordre de
priorité : Leboncoin d'abord, le reste seulement s'il reste du budget.

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

1. **Cadence horaire** — les annonces sortent en journée ouvrée. On scanne
   toutes les 5 min de 9 h à 19 h en semaine, 30 min le soir et le week-end,
   2 h la nuit. Aucune annonce utile perdue, ~60 % de crédits économisés.
2. **Enveloppe mensuelle** — `budget.json` compte les crédits dépensés par ce
   projet ; au-delà de `budget_mensuel`, plus aucun appel.
3. **Réserve du compte** — chaque réponse renvoie le crédit restant du compte
   (tous projets confondus). En dessous de `reserve_autre_projet`, on coupe :
   c'est ce qui garantit que la veille immo n'assèche pas l'autre projet.

Projection avec la config par défaut : **~96 600 crédits/mois** pour Leboncoin
seul (48 % du quota), ~117 700 avec SeLoger activé (59 %).

### Réglages

Tout est dans la section `scrapfly` de [`config.json`](config.json) :

| Clé | Rôle |
|---|---|
| `enabled` | Coupe-circuit global des providers payants |
| `budget_mensuel` | Crédits max que ce projet peut consommer dans le mois |
| `reserve_autre_projet` | Crédits du compte à ne jamais entamer |
| `cout_max_par_requete` | `cost_budget` Scrapfly : plafond dur par appel |
| `cadence` | Intervalle mini en secondes (`pleine` / `creuse` / `nuit`) |
| `providers.*.search_url` | URL de recherche, **une seule pour toute la zone** |
| `providers.*.cadence` | Cadence spécifique à un site (SeLoger tourne au ralenti) |

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
