# -*- coding: utf-8 -*-
"""Providers sans coût : Immojeune et ParuVendu.

Les deux servent leurs résultats dans le HTML initial, sans protection
anti-bot : un simple GET suffit, donc on peut interroger toute la zone à
chaque passage sans rien dépenser.

Ils sont complémentaires des sources déjà en place :
  - Immojeune  : meublé étudiant / jeune actif, exactement le segment visé
                 (petites surfaces, budget serré), absent des autres portails
  - ParuVendu  : beaucoup de particuliers, donc pas de frais d'agence

Découpage des requêtes, mesuré site par site :
  - ParuVendu accepte le code postal, donc un arrondissement de Lyon se
    demande directement (`lyon-69008`).
  - Immojeune ne connaît que la commune (`lyon-69`) : les neuf
    arrondissements se replient sur une seule requête, et c'est le filtrage
    par code postal de zone.py qui retient les bons.
"""

import json
import re
import time
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept-Language": "fr-FR,fr;q=0.9"}


def _slug(nom):
    """'Saint-Priest' -> 'saint-priest', 'Vénissieux' -> 'venissieux'."""
    t = unicodedata.normalize("NFKD", nom.lower()).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def _get(url):
    try:
        r = requests.get(url, headers=UA, timeout=20)
        return r.text if r.status_code == 200 else None
    except requests.RequestException:
        return None


def _get_tous(urls, workers=4):
    """Quelques requêtes en parallèle : la zone compte une trentaine de
    communes et deux sites, soit ~60 pages par passage. En séquentiel avec
    une pause de politesse on dépasserait le temps de job GitHub."""
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(_get, urls))


def _cibles(zone, avec_cp, slugs=None):
    """Les (libellé, url_fragment) à interroger, dédoublonnés.

    `avec_cp` distingue les deux schémas repérés : ParuVendu veut
    'commune-codepostal', Immojeune 'commune-departement'.
    """
    # une recherche peut imposer ses slugs : ParuVendu classe Saint-Etienne
    # parmi les grandes villes, ou seul le nom nu fonctionne, la ou les
    # communes de l'agglomeration lyonnaise exigent 'commune-codepostal'.
    # ParuVendu ne publiant aucune coordonnee, ce provider reste de toute
    # facon au niveau de la commune en recherche par rayon.
    if slugs:
        return [(s, s) for s in slugs]
    vues, cibles = set(), []
    for c in zone.communes:
        if c.cp_only or not c.cps:
            continue
        nom = getattr(c, "requete", None) or c.nom
        frag = f"{_slug(nom)}-{c.cps[0]}" if avec_cp else f"{_slug(nom)}-{c.cps[0][:2]}"
        if frag not in vues:
            vues.add(frag)
            cibles.append((c.nom, frag))
    return cibles


# ---------------------------------------------------------------- Immojeune

def _ij_cartes(html, cfg, zone, garder):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for c in soup.select("div.card"):
        lien = c.select_one("p.title a[href]")
        if not lien:
            continue
        href = lien["href"]
        badges = [b.get_text(" ", strip=True).upper() for b in c.select("span.badge")]

        # la colocation se lit sur le badge ET sur le chemin de l'annonce
        coloc = "COLOCATION" in badges or "/colocation/" in href

        titre = lien.get_text(" ", strip=True)
        desc = (c.select_one("p.description").get_text(" ", strip=True)
                if c.select_one("p.description") else "")
        if not garder(titre, desc, coloc=coloc):
            continue

        # '125 m² - 399 €' : surface et prix dans le même paragraphe
        bloc = next((p.get_text(" ", strip=True) for p in c.select("p")
                     if "€" in p.get_text()), "")
        mp = re.search(r"([\d\s ]+)\s*€", bloc)
        if not mp:
            continue
        prix = int(re.sub(r"\D", "", mp.group(1)))
        if prix > cfg["prix_max"]:
            continue
        ms = re.search(r"(\d+(?:[.,]\d+)?)\s*m", bloc)
        surface = float(ms.group(1).replace(",", ".")) if ms else None

        # badge de type : STUDIO/T1/T2... au-delà de pieces_max on écarte
        for b in badges:
            mt = re.fullmatch(r"T(\d+)", b)
            if mt and cfg.get("pieces_max") and int(mt.group(1)) > cfg["pieces_max"]:
                break
        else:
            geo = c.select_one("div.geo")
            adresse = geo.get_text(" ", strip=True) if geo else ""
            if not zone.accepte(adresse=adresse):
                continue
            mv = re.match(r"(\d{5})\s+(.*)", adresse)
            ident = re.search(r"_(\d+)\.html", href)
            results.append({
                "id": f"immojeune-{ident.group(1) if ident else href}",
                "titre": titre[:90],
                "prix": prix,
                "surface": surface,
                "ville": mv.group(2) if mv else adresse,
                "url": "https://www.immojeune.com" + href,
                "source": "Immojeune",
            })
    return results


def provider_immojeune(cfg, zone, garder, log):
    p = (cfg.get("gratuits") or {}).get("immojeune") or {}
    if not p.get("enabled", True):
        return []
    pages = max(1, int(p.get("pages_max", 3)))
    urls = []
    for _, frag in _cibles(zone, avec_cp=False):
        urls.append(f"https://www.immojeune.com/location-etudiant/{frag}.html")
        # la page 1 est '<commune>.html', les suivantes '<commune>/<n>'
        urls += [f"https://www.immojeune.com/location-etudiant/{frag}/{n}"
                 for n in range(2, pages + 1)]

    results, echecs = [], 0
    for html in _get_tous(urls):
        if html is None:
            echecs += 1
            continue
        results.extend(_ij_cartes(html, cfg, zone, garder))
    if echecs:
        log(f"  Immojeune: {echecs}/{len(urls)} page(s) injoignable(s)")
    return results


# ---------------------------------------------------------------- ParuVendu

def _pv_cartes(html, cfg, zone, garder):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for c in soup.select("div.blocAnnonce"):
        lien = c.select_one('a[href*="/immobilier/location/"]')
        if not lien:
            continue
        href = lien["href"]
        titre = (lien.get("title") or "").strip()
        texte = c.get_text(" ", strip=True)
        if not garder(titre, texte):
            continue

        # Le prix est isolé dans la plus petite balise contenant '€'. On ne
        # peut pas le chercher dans le texte de la carte : celui-ci commence
        # par « Voir d'autres photos 10 990 € », où 10 est un nombre de
        # photos qu'un regex glouton agrégerait au loyer.
        noeud = next((el for el in c.find_all(True)
                      if "€" in el.get_text()
                      and not any("€" in ch.get_text() for ch in el.find_all(True))), None)
        if not noeud:
            continue
        mp = re.search(r"([\d\s ]+)\s*€", noeud.get_text(" ", strip=True))
        if not mp:
            continue
        prix = int(re.sub(r"\D", "", mp.group(1)))
        if prix > cfg["prix_max"]:
            continue

        # 'Appartement - 3 pièce(s) - 61 m²' dans l'attribut title
        mpi = re.search(r"(\d+)\s*pièce", titre)
        if mpi and cfg.get("pieces_max") and int(mpi.group(1)) > cfg["pieces_max"]:
            continue
        ms = re.search(r"([\d,.]+)\s*m²", titre)
        surface = float(ms.group(1).replace(",", ".")) if ms else None

        # ParuVendu n'applique aucun filtre 'meublé' côté serveur : comme
        # pour PAP, on exige la mention explicite plutôt que de notifier des
        # locations vides à longueur de journée
        if cfg.get("meuble") and "meubl" not in texte.lower():
            continue

        # la carte affiche 'Bron (69)' : le département ne suffit pas à
        # trancher, c'est le nom qui est comparé à la zone
        # la carte ecrit « ... 61 m2 Bron (69) ». S'ancrer sur la surface est
        # precis pour un logement, mais une carte parking n'en affiche aucune
        # (« Parking / Garage Saint-Etienne (42) ») : sans repli, 29 annonces
        # sur 30 partaient avec une ville vide, donc hors zone.
        brut = c.get_text(" ", strip=True)
        mv = (re.search(r"m\s*2\s*(.+?)\s*\(\d{2}\)", brut)
              or re.search(r"([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’\-\s]{1,38})\s*\(\d{2}\)", brut))
        ville = mv.group(1).strip() if mv else ""
        if not zone.accepte(ville=ville):
            continue

        results.append({
            "id": f"paruvendu-{c.get('data-id') or href}",
            "titre": titre[:90],
            "prix": prix,
            "surface": surface,
            "ville": ville,
            "url": "https://www.paruvendu.fr" + href,
            "source": "ParuVendu",
        })
    return results


def provider_paruvendu(cfg, zone, garder, log):
    p = (cfg.get("gratuits") or {}).get("paruvendu") or {}
    if not p.get("enabled", True):
        return []
    # px1 est le seul filtre serveur réellement pris en compte (mesuré :
    # nbp0 et meuble sont ignorés) ; le reste est refiltré en local
    chemin = p.get("chemin") or "appartement"
    urls = [f"https://www.paruvendu.fr/immobilier/recherche/location/{chemin}/"
            f"{frag}/?px1={cfg['prix_max']}"
            for _, frag in _cibles(zone, avec_cp=True, slugs=p.get("slugs"))]

    results, echecs = [], 0
    for html in _get_tous(urls):
        if html is None:
            echecs += 1
            continue
        results.extend(_pv_cartes(html, cfg, zone, garder))
    if echecs:
        log(f"  ParuVendu: {echecs}/{len(urls)} page(s) injoignable(s)")
    return results


# ---------------------------------------------------------------- Colocation
#
# Trois sites de chambres meublées, sans protection. Désactivés par défaut
# ('enabled' doit être explicite) : ils n'ont de sens que pour la veille
# coloc, et le logement les ignorerait de toute façon.

def _actif(cfg, cle):
    p = (cfg.get("gratuits") or {}).get(cle) or {}
    return p if p.get("enabled") else None


def _ha_listings(html):
    """HousingAnywhere hydrate sa page avec un JSON.parse("...") : on
    décode la chaîne, puis on cherche la liste 'listings' où qu'elle soit."""
    m = re.search(r"window\.__staticRouterHydrationData = JSON\.parse\(", html)
    if not m:
        return []
    try:
        chaine, _ = json.JSONDecoder().raw_decode(html[m.end():])
        data = json.loads(chaine)
    except (ValueError, TypeError):
        return []
    pile = [data]
    while pile:
        n = pile.pop()
        if isinstance(n, dict):
            if isinstance(n.get("listings"), list):
                return n["listings"]
            pile.extend(n.values())
        elif isinstance(n, list):
            pile.extend(n)
    return []


def provider_housinganywhere(cfg, zone, garder, log):
    """HousingAnywhere — chambres meublées à moyen terme, beaucoup en
    banlieue est (Bron, Vénissieux, Saint-Priest) autour de 450 €. Chaque
    annonce porte sa commune et ses coordonnées."""
    p = _actif(cfg, "housinganywhere")
    if not p:
        return []
    # l'alias anglais 'private-rooms' redirige vers ce chemin français
    url = p.get("url") or "https://housinganywhere.com/fr/s/Lyon--France/chambres-priv%C3%A9es"
    # le site renvoie un 404 de 134 octets à toute requête de `requests`,
    # quels que soient les en-têtes, mais sert urllib normalement
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
            html = r.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        html = None
    if html is None:
        log("  HousingAnywhere: page injoignable")
        return []
    results = []
    for a in _ha_listings(html):
        if a.get("propertyType") not in ("PRIVATE_ROOM", "SHARED_ROOM"):
            continue
        ville = a.get("city") or ""
        titre = f"Chambre {a.get('facility_bedroom_size') or '?'} m² - {ville}"
        if not garder(titre, "", coloc=True):
            continue
        prix = a.get("priceEUR") or a.get("minPrice")
        if not prix or prix > cfg["prix_max"]:
            continue
        geo = a.get("_geoloc") or {}
        if not zone.accepte(ville=ville, lat=geo.get("lat"), lon=geo.get("lng")):
            continue
        results.append({
            "id": f"housinganywhere-{a.get('id')}",
            "titre": titre,
            "prix": int(prix),
            # surface de la chambre : c'est celle qui distingue deux
            # chambres d'un même appartement
            "surface": float(a["facility_bedroom_size"]) if a.get("facility_bedroom_size") else None,
            "ville": ville,
            "url": "https://housinganywhere.com" + (a.get("listingPath") or ""),
            "source": "HousingAnywhere",
        })
    return results


def provider_spotahome(cfg, zone, garder, log):
    """Spotahome — chambres meublées vérifiées. Le filtre budget est
    appliqué côté serveur ; la commune ne se lit que dans le texte."""
    p = _actif(cfg, "spotahome")
    if not p:
        return []
    url = p.get("url") or f"https://www.spotahome.com/fr/s/lyon/for-rent:rooms?budget=0-{cfg['prix_max']}"
    html = _get(url)
    if html is None:
        log("  Spotahome: page injoignable")
        return []
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for c in soup.select("div.l-list__item"):
        lien = c.select_one("a[href*='/for-rent:']")
        prix_el = c.select_one("[class*=price__amount]")
        if not lien or not prix_el:
            continue
        mid = re.search(r"/for-rent:[a-z]+/(\d+)", lien["href"])
        texte = c.get_text(" ", strip=True)
        titre = next((t for t in c.stripped_strings
                      if len(t) > 25 and re.search(r"chambre|studio|appartement", t, re.I)), texte[:90])
        if not garder(titre, texte, coloc=True):
            continue
        prix = int(re.sub(r"\D", "", prix_el.get_text()) or 0)
        if not prix or prix > cfg["prix_max"]:
            continue
        # recherche « Lyon » : la métropole entière, et seule la commune
        # citée dans le texte tranche. « à Lyon » / « Gerland, Lyon » vise la
        # ville ; « proche Lyon » une commune voisine, qui doit alors être
        # nommée (« à Villeurbanne ») pour être retenue.
        dans_lyon = re.search(r"(?:\bà|,)\s*Lyon\b", titre)
        if not zone.accepte(ville="Lyon" if dans_lyon else None, adresse=texte):
            continue
        ms = re.search(r"(\d+(?:[.,]\d+)?)\s*m²", titre)
        results.append({
            "id": f"spotahome-{mid.group(1) if mid else lien['href']}",
            "titre": titre[:90],
            "prix": prix,
            "surface": float(ms.group(1).replace(",", ".")) if ms else None,
            "ville": "",
            "url": "https://www.spotahome.com" + lien["href"],
            "source": "Spotahome",
        })
    return results


def _app_cartes(html, cfg, zone, garder):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for c in soup.select("li.results-list__item"):
        lien = next((a for a in c.select("a[href]")
                     if re.search(r"/colocations/.+/\d{6,}$", a["href"])), None)
        if not lien:
            continue
        texte = c.get_text(" ", strip=True)
        # « €770 - €920 » pour une résidence : la chambre la moins chère
        mp = re.search(r"€\s*([\d.  ]+)", texte)
        if not mp:
            continue
        prix = int(re.sub(r"\D", "", mp.group(1)))
        mv = re.search(r"\((\d{5})\s+([^)]+)\)", texte)
        cp, ville = (mv.group(1), mv.group(2)) if mv else (None, "")
        titre = next((t for t in c.stripped_strings if len(t) > 20 and "€" not in t
                      and "(" not in t and "Disponible" not in t), texte[:90])
        if not garder(titre, texte, coloc=True):
            continue
        if prix > cfg["prix_max"] or not zone.accepte(cp=cp, ville=ville):
            continue
        results.append({
            "id": f"appartager-{lien['href'].rsplit('/', 1)[-1]}",
            "titre": titre[:90],
            "prix": prix,
            "surface": None,
            "ville": ville,
            "url": "https://www.appartager.com" + lien["href"],
            "source": "Appartager",
        })
    return results


def provider_appartager(cfg, zone, garder, log):
    """Appartager — beaucoup de particuliers, 10 annonces par page."""
    p = _actif(cfg, "appartager")
    if not p:
        return []
    pages = max(1, int(p.get("pages_max", 3)))
    urls = []
    for slug in p.get("slugs") or ["lyon"]:
        urls.append(f"https://www.appartager.com/colocations/{slug}")
        urls += [f"https://www.appartager.com/colocations/{slug}/page{n}"
                 for n in range(2, pages + 1)]
    results, echecs = [], 0
    for html in _get_tous(urls):
        if html is None:
            echecs += 1
            continue
        results.extend(_app_cartes(html, cfg, zone, garder))
    if echecs:
        log(f"  Appartager: {echecs}/{len(urls)} page(s) injoignable(s)")
    return results


PROVIDERS_GRATUITS = (
    ("Immojeune", provider_immojeune),
    ("ParuVendu", provider_paruvendu),
    ("HousingAnywhere", provider_housinganywhere),
    ("Spotahome", provider_spotahome),
    ("Appartager", provider_appartager),
)
