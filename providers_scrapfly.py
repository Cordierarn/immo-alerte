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

import zone as zone_mod
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


def _tag(sf_cfg, nom):
    """Nom sous lequel la cadence de ce provider est suivie.

    Deux recherches peuvent viser le meme site : sans prefixe, l'appel
    Leboncoin du parking marquerait le creneau du logement comme servi, et
    l'une des deux veilles serait sautee un passage sur deux. Le logement
    garde le nom nu, pour ne pas repartir de zero sur budget.json.
    """
    return (sf_cfg.get("prefixe") or "") + nom


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


def provider_leboncoin(cfg, zone, sf_cfg, garder, log, dev=False):
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

    res = scrape(url, provider=_tag(sf_cfg, "leboncoin"), cfg=sf_cfg, log=log,
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
        # 2 = appartement, 4 = parking/garage.
        attendu = str(p.get("real_estate_type") or "2")
        if _attr(ad, "real_estate_type") != attendu:
            continue
        pieces = _attr(ad, "rooms")
        if pieces and cfg.get("pieces_max") and int(pieces) > cfg["pieces_max"]:
            continue
        if cfg.get("meuble") and _attr(ad, "furnished") not in (None, "1"):
            continue

        surface = _attr(ad, "square")
        loc = ad.get("location") or {}
        lat, lon = loc.get("lat"), loc.get("lng")
        # l'URL liste une trentaine de communes ; Leboncoin élargit parfois
        # aux alentours, et le zipcode de l'annonce fait foi. En recherche
        # par rayon, ce sont les coordonnees qui tranchent.
        if not zone.accepte(ville=loc.get("city"), cp=loc.get("zipcode"),
                            lat=lat, lon=lon):
            continue
        results.append({
            "id": f"leboncoin-{ad.get('list_id')}",
            "titre": titre[:90],
            "prix": prix,
            "surface": float(surface) if surface else None,
            "ville": loc.get("city", ""),
            "url": ad.get("url") or f"https://www.leboncoin.fr/ad/locations/{ad.get('list_id')}",
            "source": "Leboncoin",
            "distance": zone.distance(lat, lon),
        })
    return results


# ---------------------------------------------------------------- SeLoger

def _slg_txt(carte, testid):
    el = carte.select_one(f'[data-testid="{testid}"]')
    return el.get_text(" ", strip=True) if el else ""


def _slg_cartes(html, cfg, zone, garder, log):
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
        # SeLoger propose souvent des communes « aux alentours ». L'adresse
        # porte le code postal (« Lyon 8ème (69008) ») : il tranche seul, et
        # c'est le seul moyen fiable de distinguer les arrondissements.
        if not zone.accepte(ville=ville, adresse=adresse):
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


def provider_seloger(cfg, zone, sf_cfg, garder, log, dev=False):
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
        res = scrape(url, provider=_tag(sf_cfg, "seloger"), cfg=sf_cfg, log=log,
                     asp=True, render_js=False, country="fr",
                     cadence=p.get("cadence"),
                     # cible plus chère que Leboncoin (40 vs 30) : plafond
                     # dédié plutôt que de relever le global pour tous
                     cost_budget=p.get("cout_max"),
                     cache=dev, cache_ttl=3600,
                     ignorer_cadence=dev or i > 0)
        trouves, nb_cartes = _slg_cartes(res["content"], cfg, zone, garder, log)
        if not nb_cartes:
            _dump_debug(f"seloger{i}", res["content"], log)
        results.extend(trouves)
    return results


# ---------------------------------------------------------------- Logic-Immo

def _li_annonces(html, cfg, zone, garder):
    """Extrait les annonces Logic-Immo.

    Contrairement à Leboncoin et SeLoger, la structure de cette page n'a
    jamais pu être observée : le site est derrière DataDome, qui renvoie 403
    à toute requête directe, y compris sur une URL volontairement invalide.
    Impossible donc de valider un sélecteur sans dépenser des crédits.

    On lit par conséquent le JSON-LD, qui est la partie la plus stable et la
    plus standardisée d'un portail immobilier, avec un repli sur les cartes
    HTML. Si les deux échouent, le HTML est sauvegardé pour ajuster le
    parsing hors ligne, sans rebrûler d'appel.
    """
    results = []

    def ajouter(titre, desc, prix, surface, ville, cp, lien):
        if not lien or prix is None or prix > cfg["prix_max"]:
            return
        if not garder(titre, desc) or not zone.accepte(ville=ville, cp=cp, adresse=ville):
            return
        ident = re.search(r"(\d{6,})", lien)
        results.append({
            "id": f"logicimmo-{ident.group(1) if ident else lien}",
            "titre": (titre or "Annonce Logic-Immo")[:90],
            "prix": int(prix),
            "surface": float(surface) if surface else None,
            "ville": ville or "",
            "url": lien if lien.startswith("http") else "https://www.logic-immo.com" + lien,
            "source": "Logic-Immo",
        })

    def parcourir(noeud):
        """Le JSON-LD immobilier imbrique Offer, ItemList et Accommodation
        de façon très variable selon les portails : on descend partout."""
        if isinstance(noeud, list):
            for x in noeud:
                parcourir(x)
            return
        if not isinstance(noeud, dict):
            return
        offre = noeud.get("offers") or {}
        offre = offre[0] if isinstance(offre, list) and offre else offre
        prix = (noeud.get("price") or (offre or {}).get("price")
                if isinstance(offre, dict) else noeud.get("price"))
        lien = noeud.get("url") or (offre or {}).get("url") if isinstance(offre, dict) else noeud.get("url")
        adr = noeud.get("address") or {}
        if isinstance(adr, dict):
            ville, cp = adr.get("addressLocality"), adr.get("postalCode")
        else:
            ville, cp = (adr if isinstance(adr, str) else None), None
        taille = noeud.get("floorSize") or {}
        surface = taille.get("value") if isinstance(taille, dict) else taille
        if prix and lien:
            try:
                ajouter(noeud.get("name"), noeud.get("description"),
                        float(str(prix).replace(",", ".")), surface, ville, cp, str(lien))
            except (TypeError, ValueError):
                pass
        for v in noeud.values():
            parcourir(v)

    for bloc in _jsonld(html):
        parcourir(bloc)
    if results:
        return results

    # repli : toute ancre qui porte à la fois un loyer et une surface
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select("a[href]"):
        txt = a.get_text(" ", strip=True)
        mp = re.search(r"(\d[\d\s ]{1,6})\s*€", txt)
        ms = re.search(r"(\d+(?:[.,]\d+)?)\s*m", txt)
        if not (mp and ms):
            continue
        ajouter(txt, txt, int(re.sub(r"\D", "", mp.group(1))),
                ms.group(1).replace(",", "."), txt, None, a["href"])
    return results


def provider_logicimmo(cfg, zone, sf_cfg, garder, log, dev=False):
    """Logic-Immo — une seule requête pour toute la zone.

    Le site accepte une recherche large, là où SeLoger impose une URL par
    commune : à couverture égale il coûte donc ~8 fois moins cher, ce qui
    est la raison de sa présence ici.
    """
    p = (sf_cfg.get("providers") or {}).get("logicimmo") or {}
    if not p.get("enabled"):
        return []
    urls = p.get("search_url")
    urls = [urls] if isinstance(urls, str) else list(urls or [])
    if not urls:
        log("  Logic-Immo: 'search_url' absente de config.json, provider ignoré")
        return []

    results = []
    for i, url in enumerate(urls):
        res = scrape(url, provider=_tag(sf_cfg, "logicimmo"), cfg=sf_cfg, log=log,
                     asp=True, render_js=False, country="fr",
                     cadence=p.get("cadence"), cost_budget=p.get("cout_max"),
                     cache=dev, cache_ttl=3600,
                     ignorer_cadence=dev or i > 0)
        trouves = _li_annonces(res["content"], cfg, zone, garder)
        if not trouves:
            _dump_debug(f"logicimmo{i}", res["content"], log)
        results.extend(trouves)
    return results


# ---------------------------------------------------------------- PAP

def _pap_annonces(html, cfg, zone, garder, log):
    """Extrait les annonces d'une page de résultats PAP.

    Les cartes sont des `div.search-list-item-alt`, dont le lien de titre
    porte déjà prix, commune, nombre de pièces et surface. On part de la
    carte et non de l'ancre : deux ancres pointent vers la même annonce
    (photo et titre), et dédoublonner sur l'identifiant est plus sûr que de
    deviner laquelle porte le texte.
    """
    soup = BeautifulSoup(html, "html.parser")
    cartes = soup.select("div.search-list-item-alt")
    results, vus = [], set()
    for c in cartes:
        a = c.select_one("a[href*='/annonces/']")
        if not a:
            continue
        href = a.get("href", "")
        m = re.search(r"r(\d+)$", href)
        ident = m.group(1) if m else href
        if ident in vus:
            continue
        vus.add(ident)

        texte = c.get_text(" ", strip=True)
        titre = (c.select_one("a.item-title") or a).get_text(" ", strip=True)
        if not garder(titre, texte):
            continue
        if not zone.accepte(adresse=texte):
            continue
        if cfg.get("meuble") and "meubl" not in texte.lower():
            continue

        mp = re.search(r"(\d[\d\s .]*)\s*€", texte)
        if not mp:
            continue
        prix = int(re.sub(r"[^\d]", "", mp.group(1)))
        if prix > cfg["prix_max"]:
            continue
        mpi = re.search(r"(\d+)\s*pièces?", texte)
        if mpi and cfg.get("pieces_max") and int(mpi.group(1)) > cfg["pieces_max"]:
            continue
        ms = re.search(r"(\d+(?:[.,]\d+)?)\s*m", texte)
        surface = float(ms.group(1).replace(",", ".")) if ms else None

        mcp = zone_mod.CP_RE.search(texte)
        exacte = zone.par_cp(mcp.group(1)) if mcp else None
        results.append({
            "id": f"pap-{ident}",
            "titre": titre[:90],
            "prix": prix,
            "surface": surface,
            "ville": exacte.nom if exacte else "",
            "url": href if href.startswith("http") else "https://www.pap.fr" + href,
            "source": "PAP",
        })
    return results, len(cartes)


def provider_pap(cfg, zone, sf_cfg, garder, log, dev=False):
    """PAP — 100 % particuliers, donc aucun frais d'agence.

    Gratuit jusqu'à son passage derrière Cloudflare, qui renvoie désormais
    403 sur les pages comme sur l'autocomplete. Le provider a continué à
    tourner des semaines en annonçant « 0 annonce » sans rien signaler.

    Deux enseignements de la remise en service, qui expliquent la forme
    actuelle :

      - les g-codes codés en dur ne correspondaient plus aux communes
        attendues (`g35406`, censé être Bron, sert Charentay). On ne résout
        donc plus de code par commune : on interroge le **département**,
        `g433`, et le filtrage par code postal de zone.py fait le tri.
      - cette page départementale coûte 40 crédits contre 80 pour une page
        de commune, tout en couvrant les 33 communes d'un coup. Le stock de
        PAP sous 650 € tient largement sur une page.
    """
    p = (sf_cfg.get("providers") or {}).get("pap") or {}
    if not p.get("enabled"):
        return []
    urls = p.get("search_url")
    urls = [urls] if isinstance(urls, str) else list(urls or [])
    if not urls:
        log("  PAP: 'search_url' absente de config.json, provider ignoré")
        return []

    results = []
    for i, url in enumerate(urls):
        res = scrape(url, provider=_tag(sf_cfg, "pap"), cfg=sf_cfg, log=log,
                     asp=True, render_js=False, country="fr",
                     cadence=p.get("cadence"), cost_budget=p.get("cout_max"),
                     cache=dev, cache_ttl=3600,
                     ignorer_cadence=dev or i > 0)
        trouves, nb = _pap_annonces(res["content"], cfg, zone, garder, log)
        if not nb:
            _dump_debug(f"pap{i}", res["content"], log)
        results.extend(trouves)
    return results


PROVIDERS_SCRAPFLY = (
    ("Leboncoin", provider_leboncoin),
    ("PAP", provider_pap),
    ("SeLoger", provider_seloger),
    ("Logic-Immo", provider_logicimmo),
)
