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

import re
import time
import unicodedata
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
        if cfg.get("exclure_coloc") and ("COLOCATION" in badges or "/colocation/" in href):
            continue

        titre = lien.get_text(" ", strip=True)
        desc = (c.select_one("p.description").get_text(" ", strip=True)
                if c.select_one("p.description") else "")
        if not garder(titre, desc):
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
        ms = re.search(r"([\d,.]+)\s*m", bloc)
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


PROVIDERS_GRATUITS = (
    ("Immojeune", provider_immojeune),
    ("ParuVendu", provider_paruvendu),
)
