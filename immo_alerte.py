# -*- coding: utf-8 -*-
"""
immo-alerte : veille logement personnelle (Saint-Priest & alentours).

Architecture inspirée de Fredy (providers + dédoublonnage + notifications)
et de House-Alert (critères utilisateur, exécution périodique).

Providers gratuits (aucun coût, à chaque passage) :
  - Bien'ici   : API JSON interne (pas de protection anti-bot agressive)
  - Immojeune  : meublé étudiant / jeune actif
  - ParuVendu  : beaucoup de particuliers

Providers payants via Scrapfly (anti-bot, cadence et budget encadrés) :
  - Leboncoin  : ~30 crédits/appel, la source qui apporte vraiment du stock
  - PAP        : 100% particuliers ; passé sous Scrapfly depuis que
                 Cloudflare bloque l'accès direct (une requête départementale)
  - SeLoger    : 35 crédits par commune, limité aux communes prioritaires
  - Logic-Immo : une requête pour toute la zone

Usage :
  python immo_alerte.py            # un passage (à planifier toutes les 15 min)
  python immo_alerte.py --init     # premier passage : mémorise l'existant sans notifier
  python immo_alerte.py --gratuit  # ignore les providers Scrapfly (0 crédit)
  python immo_alerte.py --dev      # met en cache les appels Scrapfly (0 crédit
                                   # sur les rejeux) pour mettre au point le parsing
  python immo_alerte.py --budget   # état de la consommation de crédits du mois
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

import providers_gratuits
import zone as zone_mod

BASE = Path(__file__).parent
CONFIG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
ZONE = zone_mod.charger(CONFIG)
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


def titre_exclu(titre):
    """Mots qui ne disqualifient une annonce que dans son TITRE.

    « bureau » désigne un local professionnel quand il titre l'annonce, mais
    un simple meuble dès qu'il apparaît dans la description — et sur une
    recherche de meublés, « lit, armoire, table, bureau » est la norme. Le
    chercher partout écartait donc en silence une bonne part du stock visé,
    sur tous les providers à la fois.
    """
    t = (titre or "").lower()
    return any(m in t for m in CONFIG.get("mots_exclus_titre") or [])


def est_chambre(titre):
    """Les 'chambres à louer' sont de la coloc déguisée.

    On cherche le mot n'importe où dans le titre : Leboncoin est plein de
    'Location Chambres' et de 'LES IRIS location chambres' qu'un simple
    startswith laissait passer.

    Mais SeLoger titre ses annonces '2 pièces - 1 chambre - 39 m2', où
    'chambre' compte les pièces de nuit et ne dit rien du mode de location.
    On neutralise donc les occurrences précédées d'un nombre avant de
    chercher, sans quoi tous les T2 de SeLoger seraient écartés à tort.
    """
    t = re.sub(r"\d+\s*chambres?", "", (titre or "").lower())
    return bool(re.search(r"\bchambres?\b", t))


def est_demande(titre):
    """Leboncoin étiquette 'offer' des annonces qui sont des DEMANDES.

    'Recherche studio sur Lyon', 'Recherche logement étudiant'... Le champ
    ad_type vaut 'offer' pour toutes, seul le titre trahit. On se limite au
    début du titre : 'recherche locataire sérieux' est bien une offre.
    """
    return bool(re.match(r"(je\s+)?(re)?cherche\b", (titre or "").strip().lower()))


# Qui gagne quand la même annonce sort de plusieurs sites : on garde la
# source la plus utile (particuliers d'abord, lien direct, pas de frais).
ORDRE_SOURCES = {"Leboncoin": 0, "PAP": 1, "ParuVendu": 2, "Immojeune": 3,
                 "Bien'ici": 4, "SeLoger": 5, "Logic-Immo": 6}


def empreinte(annonce):
    """Clé de rapprochement inter-sites : ville + prix + surface arrondie.

    Renvoie None si la surface manque. C'est volontaire : sans elle, deux
    studios distincts à 650 € dans la même commune auraient la même clé et
    l'un des deux ne serait jamais notifié. Rater une annonce coûte plus cher
    qu'en recevoir une en double, donc en cas de doute on ne rapproche pas.
    """
    prix, surface, ville = annonce.get("prix"), annonce.get("surface"), annonce.get("ville")
    if not prix or not surface or not ville:
        return None
    # même normalisation que la zone : un simple retrait des non-lettres
    # donnait 'lyone' pour « Lyon 8e » et 'lyon' pour « Lyon 08 », donc deux
    # empreintes distinctes pour un même arrondissement — et le doublon
    # inter-sites passait au travers
    return f"emp:{zone_mod._norm(ville)}|{prix}|{round(surface)}"


def rapprocher(annonces):
    """Fusionne les annonces identiques venant de sites différents."""
    annonces.sort(key=lambda a: ORDRE_SOURCES.get(a["source"], 9))
    par_empreinte, uniques = {}, []
    for a in annonces:
        emp = empreinte(a)
        garde = par_empreinte.get(emp) if emp else None
        if garde is not None:
            if a["source"] not in garde["aussi"]:
                garde["aussi"].append(a["source"])
            continue
        a["aussi"] = []
        if emp:
            par_empreinte[emp] = a
        uniques.append(a)
    return uniques


def garder(titre, description):
    """Filtre commun aux providers Scrapfly (mots exclus + coloc + demandes)."""
    if texte_exclu(titre) or texte_exclu(description):
        return False
    if titre_exclu(titre):
        return False
    if est_demande(titre):
        return False
    if CONFIG.get("exclure_coloc") and est_chambre(titre):
        return False
    return True


# ---------------------------------------------------------------- Bien'ici

def bienici_zone_ids():
    """Résout les zoneIds Bien'ici pour chaque commune de la zone.

    Les identifiants sont mis en cache sur disque : ils ne changent jamais,
    et depuis que la zone compte une trentaine de communes les redemander à
    chaque passage représentait l'essentiel du temps d'exécution.

    On interroge le nom de requête, pas le nom affiché : les neuf
    arrondissements de Lyon se replient sur une seule recherche « Lyon ».
    """
    ids, a_resoudre = [], []
    for nom in {c.requete for c in ZONE.communes if not c.cp_only}:
        connus = zone_mod.cache_lire("bienici", nom)
        if connus is None:
            a_resoudre.append(nom)
        else:
            ids.extend(connus)

    for nom in a_resoudre:
        trouves = []
        try:
            r = requests.get("https://res.bienici.com/suggest.json",
                             params={"q": nom}, headers=UA, timeout=15)
            r.raise_for_status()
            for s in r.json():
                if s.get("type") == "city" and s.get("zoneIds"):
                    trouves = s["zoneIds"]
                    break
            if trouves:  # même raison que pour PAP : on ne fige pas un échec
                zone_mod.cache_ecrire("bienici", nom, trouves)
        except Exception as e:
            log(f"  Bien'ici suggest KO pour {nom}: {e}")
        ids.extend(trouves)
        time.sleep(0.5)
    return ids


def provider_bienici():
    zone_ids = bienici_zone_ids()
    if not zone_ids:
        log("  Bien'ici: aucune zone résolue, provider ignoré")
        return []
    filters = {
        # la zone couvre une trentaine de communes dont Lyon : 60 résultats
        # se remplissaient d'annonces lyonnaises et masquaient la périphérie
        "size": 100, "from": 0, "page": 1,
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
        if not garder(titre, desc):
            continue
        prix = ad.get("price")
        if prix is None or prix > CONFIG["prix_max"]:
            continue
        # la recherche « Lyon » ramène les neuf arrondissements : on ne garde
        # que ceux qui sont réellement dans la zone
        if not ZONE.accepte(ville=ad.get("city"), cp=ad.get("postalCode")):
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


# ---------------------------------------------------------------- Notification

def topics_ntfy():
    """Tous les topics destinataires, dédoublonnés en gardant l'ordre.

    On additionne les sources au lieu de les faire se remplacer, pour pouvoir
    diffuser la même alerte sur plusieurs appareils (ou plusieurs personnes) :
      - NTFY_TOPIC : secret GitHub Actions, plusieurs topics séparés par ','
      - config.json 'ntfy_topic' : une chaîne ou une liste
      - topic.txt : fichier local gitignoré (une ligne par topic)
    """
    bruts = list((os.environ.get("NTFY_TOPIC") or "").split(","))

    depuis_config = CONFIG.get("ntfy_topic") or []
    bruts += depuis_config if isinstance(depuis_config, list) else depuis_config.split(",")

    local = BASE / "topic.txt"
    if local.exists():
        bruts += local.read_text(encoding="utf-8").replace(",", "\n").splitlines()

    vus, topics = set(), []
    for t in bruts:
        t = t.strip()
        if t and t not in vus:
            vus.add(t)
            topics.append(t)
    return topics


def notifier(annonce):
    surface = f" - {annonce['surface']:.0f}m2" if annonce.get("surface") else ""
    aussi = annonce.get("aussi") or []
    doublons = f"\n(aussi sur {', '.join(aussi)})" if aussi else ""
    msg = (f"{annonce['prix']}EUR{surface} - {annonce['ville']}\n"
           f"{annonce['titre']}\n{annonce['url']}{doublons}")
    log(f"NOUVEAU [{annonce['source']}] {msg.replace(chr(10), ' | ')}")
    ok = True
    for topic in topics_ntfy():
        try:
            r = requests.post(f"https://ntfy.sh/{topic}",
                              data=msg.encode("utf-8"),
                              headers={"Title": f"Logement {annonce['prix']}EUR - {annonce['ville']}",
                                       "Priority": "high", "Tags": "house",
                                       "Click": annonce["url"]},
                              timeout=15)
            # ntfy.sh limite le débit : au-delà, il répond 429 et le message
            # est perdu. Sans ce contrôle l'échec passait inaperçu, et une
            # annonce non notifiée était malgré tout retenue dans seen.json,
            # donc jamais représentée.
            if r.status_code >= 300:
                ok = False
                log(f"  ntfy REFUS ({topic}): HTTP {r.status_code} {r.text[:80]}")
        except Exception as e:
            ok = False
            log(f"  ntfy KO ({topic}): {e}")
    return ok


# ---------------------------------------------------------------- Main

def providers_payants(dev):
    """Providers Scrapfly : chaque appel coûte des crédits, donc chaque échec
    de garde-fou (cadence, budget) est une info normale, pas une erreur."""
    from scrapfly_client import BudgetEpuise, ScrapflyKO, TropTot
    from providers_scrapfly import PROVIDERS_SCRAPFLY

    sf_cfg = CONFIG.get("scrapfly") or {}
    if not sf_cfg.get("enabled"):
        return []

    annonces = []
    for name, provider in PROVIDERS_SCRAPFLY:
        try:
            found = provider(CONFIG, ZONE, sf_cfg, garder, log, dev=dev)
            log(f"{name}: {len(found)} annonces correspondant aux criteres")
            annonces.extend(found)
        except TropTot as e:
            log(f"{name}: pas encore l'heure ({e})")
        except BudgetEpuise as e:
            log(f"{name}: BUDGET — {e}")
        except ScrapflyKO as e:
            log(f"{name}: scrape KO — {e}")
        except Exception as e:
            log(f"{name}: erreur provider: {e}")
    return annonces


def main():
    if "--budget" in sys.argv:
        from scrapfly_client import rapport
        print(rapport())
        return

    init_mode = "--init" in sys.argv
    dev_mode = "--dev" in sys.argv
    seen = load_seen()
    annonces = []
    gratuits = [("Bien'ici", provider_bienici)]
    # Immojeune et ParuVendu ont la même signature que les providers Scrapfly
    # (ils ont besoin de la zone), on les adapte ici pour garder une boucle
    gratuits += [(nom, lambda p=prov: p(CONFIG, ZONE, garder, log))
                 for nom, prov in providers_gratuits.PROVIDERS_GRATUITS]
    for name, provider in gratuits:
        try:
            found = provider()
            log(f"{name}: {len(found)} annonces correspondant aux criteres")
            annonces.extend(found)
        except Exception as e:
            log(f"{name}: erreur provider: {e}")

    if "--gratuit" not in sys.argv:
        annonces.extend(providers_payants(dev_mode))

    # dédoublonnage intra-passage (un même id peut sortir de deux sélecteurs) :
    # on garde la version avec le titre le plus riche
    uniques = {}
    for a in annonces:
        if a["id"] not in uniques or len(a["titre"]) > len(uniques[a["id"]]["titre"]):
            uniques[a["id"]] = a
    annonces = list(uniques.values())

    # rapprochement inter-sites : le même logement diffusé sur plusieurs
    # portails a des identifiants différents, seule l'empreinte les relie
    avant = len(annonces)
    annonces = rapprocher(annonces)
    if avant != len(annonces):
        log(f"Rapprochement: {avant - len(annonces)} doublon(s) inter-sites fusionne(s)")

    nouvelles = []
    for a in annonces:
        emp = empreinte(a)
        deja = a["id"] in seen or (emp is not None and emp in seen)
        if not deja:
            nouvelles.append(a)
        # on mémorise l'empreinte même pour une annonce déjà connue : c'est ce
        # qui permet à un jumeau publié plus tard sur un autre site d'être
        # reconnu, y compris pour les annonces mémorisées avant cette version
        seen.add(a["id"])
        if emp:
            seen.add(emp)

    for a in nouvelles:
        if not init_mode:
            notifier(a)
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
