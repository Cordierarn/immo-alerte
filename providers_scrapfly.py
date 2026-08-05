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

def provider_seloger(cfg, sf_cfg, garder, log, dev=False):
    """SeLoger — désactivé par défaut (voir README : stock très redondant
    avec Bien'ici, qui est déjà couvert gratuitement).

    Extraction volontairement défensive : SeLoger change souvent de structure.
    On tente le JSON-LD (stable, normalisé) puis __NEXT_DATA__.
    """
    p = (sf_cfg.get("providers") or {}).get("seloger") or {}
    if not p.get("enabled"):
        return []
    url = p.get("search_url")
    if not url:
        log("  SeLoger: 'search_url' absente de config.json, provider ignoré")
        return []

    res = scrape(url, provider="seloger", cfg=sf_cfg, log=log,
                 asp=True, render_js=False, country="fr",
                 cadence=p.get("cadence"),
                 cache=dev, cache_ttl=3600,
                 ignorer_cadence=dev)

    annonces = []
    for bloc in _jsonld(res["content"]):
        items = bloc.get("itemListElement") if isinstance(bloc, dict) else None
        for it in items or []:
            item = it.get("item") if isinstance(it, dict) else None
            if isinstance(item, dict):
                annonces.append(item)
    if not annonces:
        data = _next_data(res["content"]) or {}
        pp = (data.get("props") or {}).get("pageProps") or {}
        annonces = pp.get("listings") or pp.get("items") or []
    if not annonces:
        _dump_debug("seloger", res["content"], log)
        return []

    results = []
    for a in annonces:
        titre = (a.get("name") or a.get("title") or "").strip()
        desc = a.get("description") or ""
        if not garder(titre, desc):
            continue

        offre = a.get("offers") or {}
        prix = offre.get("price") if isinstance(offre, dict) else None
        prix = prix or a.get("price")
        try:
            prix = int(float(prix))
        except (TypeError, ValueError):
            continue
        if prix > cfg["prix_max"]:
            continue

        adresse = a.get("address") or {}
        ville = adresse.get("addressLocality") if isinstance(adresse, dict) else ""
        lien = a.get("url") or a.get("@id") or ""
        ident = re.search(r"(\d{6,})", lien)
        results.append({
            "id": f"seloger-{ident.group(1) if ident else lien}",
            "titre": titre[:90],
            "prix": prix,
            "surface": a.get("floorSize", {}).get("value") if isinstance(a.get("floorSize"), dict) else None,
            "ville": ville or "",
            "url": lien if lien.startswith("http") else f"https://www.seloger.com{lien}",
            "source": "SeLoger",
        })
    return results


PROVIDERS_SCRAPFLY = (
    ("Leboncoin", provider_leboncoin),
    ("SeLoger", provider_seloger),
)
