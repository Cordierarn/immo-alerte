# -*- coding: utf-8 -*-
"""Sites d'agences et de résidences : UN provider générique, piloté par
config.json (clé 'agences').

Pourquoi générique : les sites d'agences se ressemblent tous. Une carte par
bien, qui affiche loyer, type, pièces, surface et commune, et un lien vers
la fiche. Écrire un parseur par agence ne passerait pas à l'échelle : on
décrit donc chaque site en quelques lignes (URL, sélecteur CSS de la carte)
et les règles d'extraction sont communes.

Pourquoi ces agences-là : Bien'ici, qu'on scanne déjà, reprend le stock de
85 agences actives dans la zone sous 700 €. Les petites régies y sont
presque toutes. Ce sont les grands réseaux absents de Bien'ici (Century 21,
Oralia, Square Habitat...) qui apportent du stock qu'on ne voit pas ailleurs.

Chaque site déclare :
  nom      libellé de la source dans les notifications
  urls     une ou plusieurs pages de résultats ; '{page}' est remplacé par
           'page_debut' (1 par défaut), 2, 3... jusqu'à 'pages'. Une page
           au-delà du stock répond 404 ou vide : elle est simplement ignorée
  carte    sélecteur CSS d'une carte d'annonce
  lien     sélecteur du lien vers la fiche, dans la carte (défaut : a[href])
  base     préfixe des liens relatifs
  meuble   'carte'  : la carte dit « meublé » quand c'en est un
           'tous'   : le site ne loue que du meublé (résidences, Lodgis)
           'detail' : seule la fiche le dit ; on l'ouvre pour les candidats
                      qui passent tous les autres filtres, verdict en cache
  coloc    true si le site ne fait que de la colocation
  scrapfly {heures, cout_max} pour un site derrière Cloudflare/DataDome
           (Foncia, Laforêt) : mêmes garde-fous que les autres providers
           payants (créneaux, enveloppe, réserve du compte)

Les filtres d'URL de ces sites (prix max, type) sont appliqués en JavaScript
et ignorés d'un GET : on lit toutes les pages et on filtre en local.
"""

import json
import re
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

import zone as zone_mod
from providers_gratuits import _get, _get_tous, _slug

# « 634,58 € » / « 1 170 € » / « 560€ ». Le texte de la carte est joint par
# ' | ' : un compteur de photos (« 6 ») voisin du loyer reste ainsi séparé
# au lieu de fusionner en « 6 880 € ».
PRIX_RE = re.compile(r"(\d[\d\s  .]*)(?:,\d+)?\s?€")
# Century 21 écrit « 29 m » puis un « 2 » en exposant dans une autre balise
SURFACE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m(?:²|2|\s*\|\s*2)(?!\d)")
PIECES_RE = re.compile(r"(\d+)\s*(?:pièces?|pieces?|p\.)|\b[TF](\d)\b", re.I)
MEUBLE_RE = re.compile(r"(?<!non )(?<!non-)meubl", re.I)
TYPE_RE = re.compile(r"\b(appartement|studio|chambre|colocation|maison|duplex|loft|t\d|f\d)\b", re.I)
# la fiche Oralia / Square Habitat porte un champ « Meublé Non » : chercher
# le mot seul le prenait pour un oui (7 faux positifs sur 7)
CHAMP_MEUBLE_RE = re.compile(r"meubl[ée]e?s?\s*[:|]?\s*(oui|non)\b", re.I)
EXCLUS_RE = re.compile(r"\b(parking|garage|box|cave|local|bureau|commerce|terrain)\b", re.I)


def _texte(el):
    return el.get_text(" | ", strip=True)


def _prix(texte):
    """Premier montant plausible : '18 € / m²' suit le loyer chez ERA."""
    for m in PRIX_RE.finditer(texte):
        v = int(re.sub(r"\D", "", m.group(1)) or 0)
        if v >= 50:
            return v
    return None


def _pieces(texte):
    if re.search(r"\bstudio\b", texte, re.I):
        return 1
    m = PIECES_RE.search(texte)
    return int(m.group(1) or m.group(2)) if m else None


def _ident(site, href):
    """Identifiant stable : le dernier segment numérique du lien, à défaut
    le lien entier (sans domaine)."""
    nums = re.findall(r"\d{4,}", href)
    cle = nums[-1] if nums else re.sub(r"^https?://[^/]+", "", href).strip("/")
    return f"{_slug(site['nom'])}-{cle}"


def _meuble_sur_fiche(url, log):
    """Ouvre la fiche une fois et met le verdict en cache (geo_cache.json) :
    un bien ne change pas de mobilier, et refaire l'appel à chaque passage
    multiplierait les requêtes par le nombre d'annonces."""
    connu = zone_mod.cache_lire("meuble", url)
    if connu is not None:
        return connu
    html = _get(url)
    if html is None:
        return None
    soup = BeautifulSoup(html, "html.parser")
    # menus et pieds de page parlent tous de « location meublée »
    for t in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        t.decompose()
    texte = soup.get_text(" ", strip=True)
    champ = CHAMP_MEUBLE_RE.search(texte)
    verdict = champ.group(1).lower() == "oui" if champ else bool(MEUBLE_RE.search(texte))
    zone_mod.cache_ecrire("meuble", url, verdict)
    return verdict


def _commune_nommee(zone, texte):
    """Lodgis écrit « Lyon 7° » sans code postal : un segment de la carte
    qui EST le nom d'une commune de la zone donne la ville (et donc une
    empreinte comparable à celle des autres sites)."""
    plats = [zone_mod._norm(seg) for seg in texte.split(" | ")]
    # « Lyon » tout court figure aussi sur la carte : l'arrondissement d'abord
    for c in sorted(zone.communes, key=lambda c: c.cp_only or c.nom_exact):
        if any(p in c.noms_norm for p in plats):
            return c.nom
    return ""


def _cartes(html, site, cfg, zone, garder, log):
    soup = BeautifulSoup(html, "html.parser")
    cartes = soup.select(site["carte"])
    results = []
    for c in cartes:
        a = c if c.name == "a" and c.get("href") else c.select_one(site.get("lien") or "a[href]")
        if not a or not a.get("href"):
            continue
        url = urljoin(site.get("base") or "", a["href"])
        texte = _texte(c)
        if CHAMP_MEUBLE_RE.search(texte) and CHAMP_MEUBLE_RE.search(texte).group(1).lower() == "non" \
                and cfg.get("meuble"):
            continue
        if EXCLUS_RE.search(texte) and not TYPE_RE.search(texte):
            continue

        prix = _prix(texte)
        if prix is None or prix > cfg["prix_max"]:
            continue
        if not zone.accepte(adresse=texte):
            continue
        pieces = _pieces(texte)
        if pieces and cfg.get("pieces_max") and pieces > cfg["pieces_max"]:
            continue

        # le titre est le premier segment court qui nomme un type de bien :
        # la description, plus loin, en nomme aussi mais fait 300 caractères
        titre = next((s for s in texte.split(" | ") if len(s) <= 90 and TYPE_RE.search(s)),
                     texte[:90])
        coloc = True if site.get("coloc") else None
        if not garder(titre, texte, coloc=coloc):
            continue

        if cfg.get("meuble"):
            mode = site.get("meuble", "carte")
            if mode == "carte" and not MEUBLE_RE.search(texte):
                continue
            if mode == "detail" and not MEUBLE_RE.search(texte) \
                    and _meuble_sur_fiche(url, log) is False:
                continue

        cps = [cp for cp in zone_mod.CP_RE.findall(texte) if cp in zone.cps]
        ms = SURFACE_RE.search(texte)
        ville = zone.par_cp(cps[0]).nom if cps else _commune_nommee(zone, texte)
        results.append({
            "id": _ident(site, url),
            "titre": titre[:90],
            "prix": prix,
            "surface": float(ms.group(1).replace(",", ".")) if ms else None,
            "ville": ville,
            "url": url,
            "source": site["nom"],
        })
    return results, len(cartes)


# la liste des sites est commune à toutes les recherches : elle vit à la
# racine de config.json, chaque recherche ne fait que l'activer
CONFIG = json.loads((Path(__file__).parent / "config.json").read_text(encoding="utf-8"))

# pages lues pendant ce passage : logement et coloc parcourent les mêmes
# sites, inutile de télécharger deux fois les 36 pages d'Oralia
_LUES = {}


def _lire(urls):
    manquantes = [u for u in urls if u not in _LUES]
    for u, html in zip(manquantes, _get_tous(manquantes)):
        _LUES[u] = html
    return [_LUES[u] for u in urls]


def _lire_scrapfly(urls, site, log):
    """Même lecture, via Scrapfly. Le créneau est vérifié sur la première
    URL seulement (les suivantes font partie du même passage), et une page
    déjà lue par une autre recherche ne coûte rien."""
    from scrapfly_client import BudgetEpuise, ScrapflyKO, TropTot, scrape
    sf = dict(CONFIG.get("scrapfly") or {})
    if not sf.get("enabled"):
        return None
    opts = site.get("scrapfly") or {}
    pages = []
    for i, u in enumerate(urls):
        if u not in _LUES:
            try:
                _LUES[u] = scrape(u, provider=f"agence:{_slug(site['nom'])}", cfg=sf, log=log,
                                  asp=True, render_js=False, country="fr",
                                  cadence={"heures": opts["heures"]} if opts.get("heures") else None,
                                  cost_budget=opts.get("cout_max"),
                                  ignorer_cadence=i > 0)["content"]
            except TropTot:
                return None
            except (BudgetEpuise, ScrapflyKO) as e:
                log(f"  {site['nom']}: {e}")
                _LUES[u] = None
        pages.append(_LUES[u])
    return pages


def _pages(site):
    n = max(1, int(site.get("pages", 1)))
    # Century 21 répond 410 à « page-1 » : sa page 1 est l'URL nue, déclarée
    # à part, et la pagination commence à 2
    debut = int(site.get("page_debut", 1))
    for u in site["urls"]:
        if "{page}" in u:
            yield [u.replace("{page}", str(i)) for i in range(debut, n + 1)]
        else:
            yield [u]


def provider_agences(cfg, zone, garder, log):
    """Tous les sites d'agences déclarés dans config.json, pour une
    recherche qui l'active ('agences': true)."""
    if not cfg.get("agences"):
        return []
    sites = CONFIG.get("agences") or []
    results = []
    for site in sites:
        if not site.get("enabled", True):
            continue
        trouves, vues, echecs, attente = [], 0, 0, False
        groupes = list(_pages(site))
        if site.get("scrapfly"):
            # un seul contrôle de créneau pour tout le site
            groupes = [[u for g in groupes for u in g]]
        for urls in groupes:
            lues = _lire_scrapfly(urls, site, log) if site.get("scrapfly") else _lire(urls)
            if lues is None:
                attente = True
                continue
            for html in lues:
                if html is None:
                    echecs += 1
                    continue
                r, nb = _cartes(html, site, cfg, zone, garder, log)
                vues += nb
                trouves.extend(r)
        # on ne déduplique que sur l'id : une même carte peut figurer sur
        # deux pages si le stock bouge pendant la lecture
        uniques = {a["id"]: a for a in trouves}
        if attente and not vues:
            log(f"  {site['nom']}: pas encore l'heure (créneau Scrapfly)")
            continue
        log(f"  {site['nom']}: {len(uniques)} retenue(s) sur {vues} carte(s)"
            + (f", {echecs} page(s) injoignable(s)" if echecs else ""))
        if not vues and not echecs:
            log(f"  {site['nom']}: AUCUNE carte reconnue — sélecteur '{site['carte']}' à revoir")
        results.extend(uniques.values())
    return results


PROVIDERS_AGENCES = (
    ("Agences", provider_agences),
)
