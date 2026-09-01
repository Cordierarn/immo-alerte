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
LOG_FILE = BASE / "alertes.log"


def recherches():
    """Les veilles a executer. Chacune a ses criteres, sa zone, sa memoire
    et son topic : le parking stephanois n'a rien a voir avec le logement
    lyonnais, et melanger les deux dans un seul jeu de reglages obligerait a
    des exceptions partout."""
    return CONFIG["recherches"]


UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept-Language": "fr-FR,fr;q=0.9"}


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fichier_seen(cfg):
    return BASE / (cfg.get("seen") or "seen.json")


def load_seen(cfg):
    f = fichier_seen(cfg)
    if f.exists():
        return set(json.loads(f.read_text(encoding="utf-8")))
    return set()


def save_seen(cfg, seen):
    fichier_seen(cfg).write_text(json.dumps(sorted(seen)), encoding="utf-8")


def texte_exclu(cfg, texte):
    t = (texte or "").lower()
    return any(m in t for m in cfg.get("mots_exclus") or [])


def titre_exclu(cfg, titre):
    """Mots qui ne disqualifient une annonce que dans son TITRE.

    « bureau » désigne un local professionnel quand il titre l'annonce, mais
    un simple meuble dès qu'il apparaît dans la description — et sur une
    recherche de meublés, « lit, armoire, table, bureau » est la norme. Le
    chercher partout écartait donc en silence une bonne part du stock visé,
    sur tous les providers à la fois.
    """
    t = (titre or "").lower()
    return any(m in t for m in cfg.get("mots_exclus_titre") or [])


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


def faire_garder(cfg):
    """Fabrique le filtre d'une recherche.

    Les providers attendent tous un `garder(titre, description)` a deux
    arguments : on referme donc les criteres de la recherche dans une
    fermeture plutot que de propager cfg jusque dans chaque parseur.
    """
    def garder(titre, description):
        if texte_exclu(cfg, titre) or texte_exclu(cfg, description):
            return False
        if titre_exclu(cfg, titre):
            return False
        if est_demande(titre):
            return False
        # notion propre au logement : un parking n'a pas de colocataire
        if cfg.get("exclure_coloc") and est_chambre(titre):
            return False
        return True
    return garder


# ---------------------------------------------------------------- Bien'ici

def bienici_zone_ids(zone):
    """Résout les zoneIds Bien'ici pour chaque commune de la zone.

    Les identifiants sont mis en cache sur disque : ils ne changent jamais,
    et depuis que la zone compte une trentaine de communes les redemander à
    chaque passage représentait l'essentiel du temps d'exécution.

    On interroge le nom de requête, pas le nom affiché : les neuf
    arrondissements de Lyon se replient sur une seule recherche « Lyon ».
    """
    ids, a_resoudre = [], []
    for nom in {c.requete for c in zone.communes if not c.cp_only}:
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


def provider_bienici(cfg, zone, garder, log):
    zone_ids = bienici_zone_ids(zone)
    if not zone_ids:
        log("  Bien'ici: aucune zone résolue, provider ignoré")
        return []
    filters = {
        # la zone couvre une trentaine de communes dont Lyon : 60 résultats
        # se remplissaient d'annonces lyonnaises et masquaient la périphérie
        "size": 100, "from": 0, "page": 1,
        "filterType": "rent",
        # 'flat' pour un logement, 'parking' pour une place ou un garage
        "propertyType": (cfg.get("bienici") or {}).get("propertyType") or ["flat"],
        "maxPrice": cfg["prix_max"],
        "onTheMarket": [True],
        "sortBy": "publicationDate", "sortOrder": "desc",
        "zoneIdsByTypes": {"zoneIds": zone_ids},
    }
    # un parking n'a ni piece ni mobilier : ces filtres videraient la recherche
    if cfg.get("pieces_max"):
        filters["maxRooms"] = cfg["pieces_max"]
    if cfg.get("meuble"):
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
        if prix is None or prix > cfg["prix_max"]:
            continue
        # Bien'ici floute la position dans un disque de ~50 m, largement
        # assez precis pour trancher a 1 km
        pos = (ad.get("blurInfo") or {}).get("position") or {}
        lat, lon = pos.get("lat"), pos.get("lon")
        # la recherche « Lyon » ramène les neuf arrondissements : on ne garde
        # que ceux qui sont réellement dans la zone
        if not zone.accepte(ville=ad.get("city"), cp=ad.get("postalCode"),
                            lat=lat, lon=lon):
            continue
        results.append({
            "id": f"bienici-{ad['id']}",
            "titre": titre.strip(),
            "prix": prix,
            "surface": ad.get("surfaceArea"),
            "ville": ad.get("city", ""),
            "url": f"https://www.bienici.com/annonce/{ad['id']}",
            "source": "Bien'ici",
            "distance": zone.distance(lat, lon),
        })
    return results


# ---------------------------------------------------------------- Notification

def topics_ntfy(cfg):
    """Tous les topics destinataires, dédoublonnés en gardant l'ordre.

    On additionne les sources au lieu de les faire se remplacer, pour pouvoir
    diffuser la même alerte sur plusieurs appareils (ou plusieurs personnes) :
      - NTFY_TOPIC : secret GitHub Actions, plusieurs topics séparés par ','
      - config.json 'ntfy_topic' de la recherche : une chaîne ou une liste
      - topic.txt : fichier local gitignoré (une ligne par topic)

    Une recherche qui declare son propre topic ne recoit PAS le secret
    NTFY_TOPIC : c'est tout l'interet d'un canal separe, les alertes parking
    ne doivent pas atterrir aussi sur le fil logement. Le secret ne sert donc
    que de canal par defaut, pour les recherches sans topic propre.
    """
    # le depot est public : un topic ne doit JAMAIS figurer dans config.json,
    # seul le nom du secret qui le porte y est ecrit
    if cfg.get("ntfy_topic_env"):
        bruts = list((os.environ.get(cfg["ntfy_topic_env"]) or "").split(","))
    else:
        depuis_config = cfg.get("ntfy_topic") or []
        bruts = depuis_config if isinstance(depuis_config, list) else depuis_config.split(",")
    bruts = [b for b in bruts if b.strip()]
    if not bruts and not cfg.get("ntfy_topic_env"):
        bruts = list((os.environ.get("NTFY_TOPIC") or "").split(","))

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


def notifier(cfg, annonce):
    surface = f" - {annonce['surface']:.0f}m2" if annonce.get("surface") else ""
    aussi = annonce.get("aussi") or []
    doublons = f"\n(aussi sur {', '.join(aussi)})" if aussi else ""
    # la distance ne vaut que pour une recherche par rayon : pour un parking
    # c'est le critere decisif, plus encore que le prix
    d = annonce.get("distance")
    loin = f" - a {d:.0f} m" if d is not None else ""
    libelle = cfg.get("notif_titre") or "Logement"
    msg = (f"{annonce['prix']}EUR{surface} - {annonce['ville']}{loin}\n"
           f"{annonce['titre']}\n{annonce['url']}{doublons}")
    log(f"NOUVEAU [{annonce['source']}] {msg.replace(chr(10), ' | ')}")
    topics = topics_ntfy(cfg)
    if not topics:
        # aucun destinataire = erreur de configuration, pas un envoi reussi.
        # Sans ce garde-fou, une boucle sur zero topic renvoyait « ok » et
        # l'annonce etait retenue comme vue : elle n'aurait plus jamais ete
        # proposee, alors que personne ne l'a recue.
        log(f"  [{cfg['nom']}] AUCUN TOPIC configuré — rien n'est envoyé, "
            f"et l'annonce reste candidate au prochain passage")
        return False
    ok = True
    for topic in topics:
        try:
            r = requests.post(f"https://ntfy.sh/{topic}",
                              data=msg.encode("utf-8"),
                              headers={"Title": f"{libelle} {annonce['prix']}EUR - {annonce['ville']}{loin}",
                                       "Priority": "high",
                                       "Tags": cfg.get("notif_tag") or "house",
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

def providers_payants(cfg, zone, garder, dev):
    """Providers Scrapfly : chaque appel coûte des crédits, donc chaque échec
    de garde-fou (cadence, budget) est une info normale, pas une erreur.

    Le compte Scrapfly (enveloppe, reserve, creneaux) est commun a toutes les
    recherches ; la liste des providers et leurs URL sont propres a chacune.
    Le prefixe separe les cadences : sans lui, l'appel Leboncoin du parking
    consommerait le creneau du logement, et l'une des deux veilles serait
    silencieusement sautee un passage sur deux.
    """
    from scrapfly_client import BudgetEpuise, ScrapflyKO, TropTot
    from providers_scrapfly import PROVIDERS_SCRAPFLY

    compte = CONFIG.get("scrapfly") or {}
    if not compte.get("enabled"):
        return []
    sf_cfg = dict(compte)
    sf_cfg["providers"] = cfg.get("providers") or {}
    sf_cfg["prefixe"] = "" if cfg["nom"] == "logement" else cfg["nom"] + ":"

    annonces = []
    for name, provider in PROVIDERS_SCRAPFLY:
        if name.lower().replace("'", "").replace("-", "") not in \
           {k.lower().replace("-", "") for k in sf_cfg["providers"]}:
            continue
        try:
            found = provider(cfg, zone, sf_cfg, garder, log, dev=dev)
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


def passage(cfg, init_mode, dev_mode, gratuit_seul):
    """Un passage complet pour UNE recherche."""
    zone = zone_mod.charger(cfg)
    garder = faire_garder(cfg)
    seen = load_seen(cfg)
    annonces = []
    gratuits = [("Bien'ici", provider_bienici)]
    gratuits += list(providers_gratuits.PROVIDERS_GRATUITS)
    for name, provider in gratuits:
        try:
            found = provider(cfg, zone, garder, log)
            log(f"{name}: {len(found)} annonces correspondant aux criteres")
            annonces.extend(found)
        except Exception as e:
            log(f"{name}: erreur provider: {e}")

    if not gratuit_seul:
        annonces.extend(providers_payants(cfg, zone, garder, dev_mode))

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
        if a["id"] in seen or (emp is not None and emp in seen):
            # on mémorise l'empreinte même pour une annonce déjà connue : c'est
            # ce qui permet à un jumeau publié plus tard sur un autre site
            # d'être reconnu
            seen.add(a["id"])
            if emp:
                seen.add(emp)
        else:
            nouvelles.append(a)

    envoyees = []
    for a in nouvelles:
        if init_mode or notifier(cfg, a):
            envoyees.append(a)
    # on ne memorise que ce qui est reellement parti : une annonce dont la
    # notification a echoue doit rester candidate au prochain passage
    for a in envoyees:
        seen.add(a["id"])
        emp = empreinte(a)
        if emp:
            seen.add(emp)
    save_seen(cfg, seen)

    if init_mode:
        log(f"[{cfg['nom']}] Init: {len(nouvelles)} annonces memorisees (pas de notification).")
        for a in annonces:
            s = f" {a['surface']:.0f}m2" if a.get("surface") else ""
            print(f"  [{a['source']}] {a['prix']}EUR{s} - {a['ville']} - {a['titre'][:60]} - {a['url']}")
    else:
        perdues = len(nouvelles) - len(envoyees)
        reste = f", {perdues} non notifiee(s)" if perdues else ""
        log(f"[{cfg['nom']}] Passage termine: {len(nouvelles)} nouvelle(s) annonce(s){reste}.")
    return len(nouvelles)


def main():
    if "--budget" in sys.argv:
        from scrapfly_client import rapport
        print(rapport())
        return

    init_mode = "--init" in sys.argv
    dev_mode = "--dev" in sys.argv
    gratuit_seul = "--gratuit" in sys.argv
    # --recherche=parking pour n'en jouer qu'une, sans toucher a l'autre
    voulue = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--recherche=")), None)

    for cfg in recherches():
        if voulue and cfg["nom"] != voulue:
            continue
        log(f"===== recherche « {cfg['nom']} » =====")
        try:
            passage(cfg, init_mode, dev_mode, gratuit_seul)
        except Exception as e:
            # une veille qui casse ne doit pas emporter les autres
            log(f"[{cfg['nom']}] ECHEC du passage: {e}")


if __name__ == "__main__":
    main()
