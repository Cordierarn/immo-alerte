# -*- coding: utf-8 -*-
"""
Providers passant par Scrapfly : Leboncoin et SeLoger (DataDome).

Principe commun d'économie : UNE seule requête par passage et par site.
Les deux sites embarquent l'intégralité des résultats de recherche dans un
JSON présent dans le HTML initial — prix, surface, ville, titre, description.
Donc :
  - pas de render_js (+5 crédits inutiles),
  - pas de pagination : trié par date décroissante, la page 1 suffit pour une
    veille qui tourne toutes les 10 minutes,
  - pas d'appel sur la page de détail : tout est déjà dans la recherche,
  - une recherche par RAYON couvre les 6 communes en une requête au lieu de 6.

C'est ce dernier point qui divise la facture par six : l'URL de recherche est
paramétrable dans config.json justement pour englober toute la zone.
"""

import json
import re

from bs4 import BeautifulSoup

from scrapfly_client import scrape, ScrapflyKO


# ---------------------------------------------------------------- Utilitaires

def _next_data(html):
    """Récupère le JSON __NEXT_DATA__ des pages Next.js (Leboncoin)."""
    m = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _jsonld(html):
    """Tous les blocs application/ld+json d'une page."""
    blocs = []
    for m in re.finditer(
            r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
            html, re.S):
        try:
            blocs.append(json.loads(m.group(1)))
        except json.JSONDecodeError:
            continue
    return blocs


def _dump_debug(nom, html, log):
    """Sauve le HTML une fois pour ajuster le parsing sans rebrûler de crédits."""
    from pathlib import Path
    p = Path(__file__).parent / f"debug_{nom}.html"
    p.write_text(html, encoding="utf-8")
    log(f"  {nom}: parsing KO, HTML sauvegardé dans {p.name} "
        f"(relance avec --dev pour itérer sur le cache, 0 crédit)")


# ---------------------------------------------------------------- Leboncoin

def _attr(ad, cle):
    for a in ad.get("attributes") or []:
        if a.get("key") == cle:
            return a.get("value")
    return None


def provider_leboncoin(cfg, sf_cfg, garder, log, dev=False):
    """Leboncoin — la seule source réellement irremplaçable (particuliers).

    DataDome : ASP obligatoire, ~25-30 crédits par appel. On n'en fait qu'un.
    """
    p = (sf_cfg.get("providers") or {}).get("leboncoin") or {}
    if not p.get("enabled"):
        return []
    url = p.get("search_url")
    if not url:
        log("  Leboncoin: 'search_url' absente de config.json, provider ignoré")
        return []

    res = scrape(url, provider="leboncoin", cfg=sf_cfg, log=log,
                 asp=True, render_js=False, country="fr",
                 cadence=p.get("cadence"),
                 cost_budget=p.get("cout_max"),
                 # en dev on rejoue la même requête à volonté pour 0 crédit
                 cache=dev, cache_ttl=3600,
                 ignorer_cadence=dev)

    data = _next_data(res["content"])
    if not data:
        _dump_debug("leboncoin", res["content"], log)
        return []

    pp = (data.get("props") or {}).get("pageProps") or {}
    ads = ((pp.get("searchData") or {}).get("ads")
           or ((pp.get("initialProps") or {}).get("searchData") or {}).get("ads")
           or [])

    results = []
    for ad in ads:
        titre = (ad.get("subject") or "").strip()
        desc = ad.get("body") or ""
        if not garder(titre, desc):
            continue

        prix = None
        pl = ad.get("price")
        if isinstance(pl, list) and pl:
            prix = pl[0]
        elif isinstance(pl, (int, float)):
            prix = int(pl)
        if prix is None or prix > cfg["prix_max"]:
            continue

        # Leboncoin applique bien 'furnished' et 'rooms' de l'URL, mais IGNORE
        # 'real_estate_type' : on reçoit des maisons (1) et des bureaux/locaux
        # (5) au milieu des appartements. On refiltre donc en local, c'est
        # gratuit et ça évite les notifications parasites.
        if _attr(ad, "real_estate_type") != "2":
            continue
        pieces = _attr(ad, "rooms")
        if pieces and cfg.get("pieces_max") and int(pieces) > cfg["pieces_max"]:
            continue
        if cfg.get("meuble") and _attr(ad, "furnished") not in (None, "1"):
            continue

        surface = _attr(ad, "square")
        loc = ad.get("location") or {}
        results.append({
            "id": f"leboncoin-{ad.get('list_id')}",
            "titre": titre[:90],
            "prix": prix,
            "surface": float(surface) if surface else None,
            "ville": loc.get("city", ""),
            "url": ad.get("url") or f"https://www.leboncoin.fr/ad/locations/{ad.get('list_id')}",
            "source": "Leboncoin",
        })
    return results


# ---------------------------------------------------------------- SeLoger

def _slg_txt(carte, testid):
    el = carte.select_one(f'[data-testid="{testid}"]')
    return el.get_text(" ", strip=True) if el else ""


def _slg_cartes(html, cfg, garder, log):
    """Parse les cartes d'une page de résultats SeLoger.

    Le JSON-LD de SeLoger ne contient que des agrégats (nombre d'annonces,
    fourchette de prix) : aucune annonce exploitable. Les données sont dans
    le HTML, sur des attributs data-testid nettement plus stables que les
    classes CSS générées.
    """
    soup = BeautifulSoup(html, "html.parser")
    cartes = soup.select('[data-testid="serp-core-classified-card-testid"]')
    results = []
    for c in cartes:
        adresse = _slg_txt(c, "cardmfe-description-box-address")
        brut = _slg_txt(c, "cardmfe-description-box-text-test-id")
        prix_txt = _slg_txt(c, "cardmfe-price-testid")
        faits = _slg_txt(c, "cardmfe-keyfacts-testid")
        desc = _slg_txt(c, "cardmfe-description-text-test-id")

        # le bloc texte commence par « 506€ /mois charges comprises » :
        # on retire le prix puis la mention, en deux temps (une alternance
        # non-greedy s'arreterait sur le premier marqueur seulement)
        titre = re.sub(r"^[^A-Za-z]*€\s*/?\s*mois\s*", "", brut)
        titre = re.sub(r"^\s*charges comprises\s*", "", titre).strip()
        titre = titre or faits or adresse
        if not garder(titre, desc):
            continue

        m = re.search(r"([\d\s ]+)€", prix_txt)
        if not m:
            continue
        prix = int(re.sub(r"\D", "", m.group(1)))
        if prix > cfg["prix_max"]:
            continue

        mp = re.search(r"(\d+)\s*pièces?", faits)
        if mp and cfg.get("pieces_max") and int(mp.group(1)) > cfg["pieces_max"]:
            continue

        ms = re.search(r"([\d,]+)\s*m", faits)
        surface = float(ms.group(1).replace(",", ".")) if ms else None

        mv = re.search(r"([^,]+?)\s*\((\d{5})\)", adresse)
        ville = mv.group(1).strip() if mv else adresse.split(",")[-1].strip()
        # SeLoger propose souvent des communes « aux alentours »
        if not any(v.lower() in adresse.lower() for v in cfg["villes"]):
            continue

        lien = ""
        a = c.select_one("a[href]")
        if a:
            lien = a["href"]
            if not lien.startswith("http"):
                lien = "https://www.seloger.com" + lien
        ident = re.search(r"(\d{6,})", lien)

        results.append({
            "id": f"seloger-{ident.group(1) if ident else lien}",
            "titre": titre[:90],
            "prix": prix,
            "surface": surface,
            "ville": ville,
            "url": lien,
            "source": "SeLoger",
        })
    return results, len(cartes)


def provider_seloger(cfg, sf_cfg, garder, log, dev=False):
    """SeLoger — une requête PAR COMMUNE.

    SeLoger a abandonné list.htm : le nouveau schéma d'URL n'accepte qu'une
    seule localité, et l'URL départementale est saturée de Lyon (mesuré :
    4 annonces sur 30 dans notre zone, toutes des colocations). Il faut donc
    une URL par commune, d'où 'search_url' qui accepte une liste.

    C'est le provider le plus cher du projet : 40 crédits par URL et par
    passage. Voir le README avant d'en ajouter.
    """
    p = (sf_cfg.get("providers") or {}).get("seloger") or {}
    if not p.get("enabled"):
        return []
    urls = p.get("search_url")
    urls = [urls] if isinstance(urls, str) else list(urls or [])
    if not urls:
        log("  SeLoger: 'search_url' absente de config.json, provider ignoré")
        return []

    results = []
    for i, url in enumerate(urls):
        # le créneau n'est vérifié que sur la première URL : les suivantes
        # font partie du même passage, il ne faut pas qu'elles se bloquent
        res = scrape(url, provider="seloger", cfg=sf_cfg, log=log,
                     asp=True, render_js=False, country="fr",
                     cadence=p.get("cadence"),
                     # cible plus chère que Leboncoin (40 vs 30) : plafond
                     # dédié plutôt que de relever le global pour tous
                     cost_budget=p.get("cout_max"),
                     cache=dev, cache_ttl=3600,
                     ignorer_cadence=dev or i > 0)
        trouves, nb_cartes = _slg_cartes(res["content"], cfg, garder, log)
        if not nb_cartes:
            _dump_debug(f"seloger{i}", res["content"], log)
        results.extend(trouves)
    return results


PROVIDERS_SCRAPFLY = (
    ("Leboncoin", provider_leboncoin),
    ("SeLoger", provider_seloger),
)
