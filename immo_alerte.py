# -*- coding: utf-8 -*-
"""
immo-alerte : veille logement personnelle (Saint-Priest & alentours).

Architecture inspirée de Fredy (providers + dédoublonnage + notifications)
et de House-Alert (critères utilisateur, exécution périodique).

Providers :
  - Bien'ici : API JSON interne (fiable, pas de protection anti-bot agressive)
  - PAP      : parsing HTML (100% particuliers, pas de frais d'agence)

Usage :
  python immo_alerte.py            # un passage (à planifier toutes les 15 min)
  python immo_alerte.py --init     # premier passage : mémorise l'existant sans notifier
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).parent
CONFIG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
SEEN_FILE = BASE / "seen.json"
LOG_FILE = BASE / "alertes.log"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept-Language": "fr-FR,fr;q=0.9"}


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_seen():
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    return set()


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(sorted(seen)), encoding="utf-8")


def texte_exclu(texte):
    t = (texte or "").lower()
    return any(m in t for m in CONFIG["mots_exclus"])


def est_chambre(titre):
    """Les 'chambres à louer' sont de la coloc déguisée."""
    return (titre or "").strip().lower().startswith("chambre")


# ---------------------------------------------------------------- Bien'ici

def bienici_zone_ids():
    """Résout les zoneIds Bien'ici pour chaque ville configurée."""
    ids = []
    for ville in CONFIG["villes"]:
        try:
            r = requests.get("https://res.bienici.com/suggest.json",
                             params={"q": ville}, headers=UA, timeout=15)
            r.raise_for_status()
            for s in r.json():
                if s.get("type") == "city" and s.get("zoneIds"):
                    ids.extend(s["zoneIds"])
                    break
        except Exception as e:
            log(f"  Bien'ici suggest KO pour {ville}: {e}")
        time.sleep(0.5)
    return ids


def provider_bienici():
    zone_ids = bienici_zone_ids()
    if not zone_ids:
        log("  Bien'ici: aucune zone résolue, provider ignoré")
        return []
    filters = {
        "size": 60, "from": 0, "page": 1,
        "filterType": "rent",
        "propertyType": ["flat"],
        "maxPrice": CONFIG["prix_max"],
        "maxRooms": CONFIG.get("pieces_max", 2),
        "onTheMarket": [True],
        "sortBy": "publicationDate", "sortOrder": "desc",
        "zoneIdsByTypes": {"zoneIds": zone_ids},
    }
    if CONFIG.get("meuble"):
        filters["isFurnished"] = True
    r = requests.get("https://www.bienici.com/realEstateAds.json",
                     params={"filters": json.dumps(filters)}, headers=UA, timeout=20)
    r.raise_for_status()
    ads = r.json().get("realEstateAds", [])
    results = []
    for ad in ads:
        titre = ad.get("title") or f"{ad.get('propertyType','')} {ad.get('roomsQuantity','?')}p"
        desc = ad.get("description", "")
        if texte_exclu(titre) or texte_exclu(desc):
            continue
        if CONFIG.get("exclure_coloc") and est_chambre(titre):
            continue
        prix = ad.get("price")
        if prix is None or prix > CONFIG["prix_max"]:
            continue
        results.append({
            "id": f"bienici-{ad['id']}",
            "titre": titre.strip(),
            "prix": prix,
            "surface": ad.get("surfaceArea"),
            "ville": ad.get("city", ""),
            "url": f"https://www.bienici.com/annonce/{ad['id']}",
            "source": "Bien'ici",
        })
    return results


# ---------------------------------------------------------------- PAP

# Codes géo PAP (g-codes) des villes de la zone
PAP_GEO = {
    "Saint-Priest": "g35662",
    "Bron": "g35406",
    "Vénissieux": "g35718",
    "Mions": "g35551",
    "Corbas": "g35443",
    "Chassieu": "g35426",
}


def pap_geo_code(ville):
    """g-code connu, sinon tentative via l'autocomplete PAP."""
    if ville in PAP_GEO:
        return PAP_GEO[ville]
    try:
        r = requests.get("https://www.pap.fr/json/ac-geo",
                         params={"q": ville}, headers=UA, timeout=15)
        for item in r.json():
            if item.get("name", "").lower().startswith(ville.lower()):
                return f"g{item['id']}"
    except Exception:
        pass
    return None


def provider_pap():
    results = []
    for ville in CONFIG["villes"]:
        code = pap_geo_code(ville)
        if not code:
            continue
        url = (f"https://www.pap.fr/annonce/locations-appartement-"
               f"{ville.lower().replace(' ', '-').replace('é', 'e').replace('è', 'e')}-{code}"
               f"-jusqu-a-{CONFIG['prix_max']}-euros")
        try:
            r = requests.get(url, headers=UA, timeout=20)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select("a.item-title, div.search-list-item-alt a[href*='/annonces/']"):
                href = a.get("href", "")
                if not href or "/annonces/" not in href:
                    continue
                full_url = href if href.startswith("http") else "https://www.pap.fr" + href
                m = re.search(r"r(\d+)$", href)
                ad_id = m.group(1) if m else href
                bloc = a.get_text(" ", strip=True)
                parent_txt = a.find_parent().get_text(" ", strip=True) if a.find_parent() else bloc
                if texte_exclu(parent_txt) or est_chambre(bloc):
                    continue
                # PAP inclut des annonces "aux alentours" parfois très loin :
                # on ne garde que celles qui mentionnent une ville de la zone
                if not any(v.lower() in parent_txt.lower() for v in CONFIG["villes"]):
                    continue
                if CONFIG.get("meuble") and "meubl" not in parent_txt.lower():
                    # PAP ne filtre pas le meublé dans l'URL : on garde seulement
                    # les annonces qui mentionnent explicitement le meublé
                    continue
                pm = re.search(r"(\d[\d\s. ]*)\s*€", parent_txt)
                prix = int(re.sub(r"[^\d]", "", pm.group(1))) if pm else None
                if prix and prix > CONFIG["prix_max"]:
                    continue
                results.append({
                    "id": f"pap-{ad_id}",
                    "titre": bloc[:90],
                    "prix": prix,
                    "surface": None,
                    "ville": ville,
                    "url": full_url,
                    "source": "PAP",
                })
        except Exception as e:
            log(f"  PAP KO pour {ville}: {e}")
        time.sleep(1)
    return results


# ---------------------------------------------------------------- Notification

def notifier(annonce):
    surface = f" - {annonce['surface']:.0f}m2" if annonce.get("surface") else ""
    msg = (f"{annonce['prix']}EUR{surface} - {annonce['ville']}\n"
           f"{annonce['titre']}\n{annonce['url']}")
    log(f"NOUVEAU [{annonce['source']}] {msg.replace(chr(10), ' | ')}")
    # le topic vient d'abord de l'environnement (secret GitHub Actions),
    # sinon du config.json (usage local)
    topic = (os.environ.get("NTFY_TOPIC") or CONFIG.get("ntfy_topic", "")).strip()
    if topic:
        try:
            requests.post(f"https://ntfy.sh/{topic}",
                          data=msg.encode("utf-8"),
                          headers={"Title": f"Logement {annonce['prix']}EUR - {annonce['ville']}",
                                   "Priority": "high", "Tags": "house"},
                          timeout=15)
        except Exception as e:
            log(f"  ntfy KO: {e}")


# ---------------------------------------------------------------- Main

def main():
    init_mode = "--init" in sys.argv
    seen = load_seen()
    annonces = []
    for name, provider in (("Bien'ici", provider_bienici), ("PAP", provider_pap)):
        try:
            found = provider()
            log(f"{name}: {len(found)} annonces correspondant aux criteres")
            annonces.extend(found)
        except Exception as e:
            log(f"{name}: erreur provider: {e}")

    # dédoublonnage intra-passage (un même id peut sortir de deux sélecteurs) :
    # on garde la version avec le titre le plus riche
    uniques = {}
    for a in annonces:
        if a["id"] not in uniques or len(a["titre"]) > len(uniques[a["id"]]["titre"]):
            uniques[a["id"]] = a
    annonces = list(uniques.values())

    nouvelles = [a for a in annonces if a["id"] not in seen]
    for a in nouvelles:
        if not init_mode:
            notifier(a)
        seen.add(a["id"])
    save_seen(seen)

    if init_mode:
        log(f"Init: {len(nouvelles)} annonces memorisees (pas de notification). "
            f"Les prochaines executions n'alerteront que sur les NOUVELLES annonces.")
        for a in annonces:
            s = f" {a['surface']:.0f}m2" if a.get("surface") else ""
            print(f"  [{a['source']}] {a['prix']}EUR{s} - {a['ville']} - {a['titre'][:60]} - {a['url']}")
    else:
        log(f"Passage termine: {len(nouvelles)} nouvelle(s) annonce(s).")


if __name__ == "__main__":
    main()
