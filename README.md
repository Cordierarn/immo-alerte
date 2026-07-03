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
GitHub Actions (toutes les 15 min)
        │
        ├── Bien'ici  (API JSON interne)
        ├── PAP       (parsing HTML, 100% particuliers)
        │
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
   Actions → New repository secret* → nom `NTFY_TOPIC`
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

En local, renseigne `ntfy_topic` dans `config.json` ou exporte `NTFY_TOPIC`.

## Sources couvertes (et pourquoi pas les autres)

| Site | Statut | Raison |
|---|---|---|
| Bien'ici | ✅ intégré | API JSON interne accessible |
| PAP | ✅ intégré | HTML propre, pas de protection agressive |
| Leboncoin | ❌ | DataDome (anti-bot) : nécessite des proxys résidentiels payants |
| SeLoger | ❌ | idem DataDome |

Pour couvrir Leboncoin/SeLoger, le plus simple reste leurs alertes email
natives en parallèle.

## Notes

- **Usage personnel et modéré** : un passage par quart d'heure sur une recherche
  ciblée, pas d'extraction massive. Reste courtois avec les sites sources.
- Le cron GitHub Actions peut avoir quelques minutes de retard aux heures de
  pointe, c'est normal.
- Projet né d'une recherche de logement autour de Saint-Priest (69) —
  architecture inspirée de [Fredy](https://github.com/orangecoding/fredy) et
  [House-Alert](https://github.com/rbiou/House-Alert).
