# -*- coding: utf-8 -*-
"""Zone géographique de la veille : quelles communes, et comment les
reconnaître dans le texte que renvoie chaque site.

Pourquoi un module dédié plutôt qu'une simple liste de noms : depuis que la
zone inclut Lyon, le rapprochement par nom ne suffit plus. Les sites écrivent
le même arrondissement « Lyon 8ème », « Lyon 08 », « Lyon 8e » ou « Lyon »
tout court, si bien que le test historique `"Lyon 8e" in adresse` échouait sur
trois de ces quatre formes. Le code postal, lui, est non ambigu et présent
presque partout : c'est devenu la clé primaire, le nom n'est qu'un repli.
"""

import json
import math
import re
import unicodedata
from pathlib import Path

BASE = Path(__file__).parent
CACHE_FILE = BASE / "geo_cache.json"

CP_RE = re.compile(r"\b(\d{5})\b")


def _norm(s):
    """Normalise un nom de commune pour le comparer.

    'Lyon 8ème' / 'Lyon 08' / 'LYON-8e' donnent tous 'lyon8', et
    'Vénissieux' donne 'venissieux'. On enlève les accents, on réduit les
    ordinaux (8ème -> 8), on retire tout ce qui n'est pas alphanumérique,
    puis on supprime les zéros de tête d'un numéro d'arrondissement.
    """
    t = unicodedata.normalize("NFKD", (s or "").lower())
    t = t.encode("ascii", "ignore").decode()
    t = re.sub(r"(\d+)\s*(?:eme|ere|er|es|e)s?\b", r"\1", t)
    t = re.sub(r"[^a-z0-9]", "", t)
    return re.sub(r"(?<=[a-z])0+(\d)", r"\1", t)


class Commune:
    """Une commune de la zone : son nom, ses codes postaux, son rang."""

    def __init__(self, brut):
        # une simple chaîne reste acceptée : la zone d'origine s'écrivait
        # ainsi, et un ajout rapide à la main ne doit pas casser
        if isinstance(brut, str):
            brut = {"nom": brut}
        self.nom = brut["nom"]
        # nom à employer pour INTERROGER les sites, quand il diffère du nom
        # affiché : aucun portail ne connaît « Lyon 8e », tous connaissent
        # « Lyon » (+ code postal quand ils l'acceptent)
        self.requete = brut.get("requete") or brut["nom"]
        self.cps = [str(c) for c in (brut.get("cp") or [])]
        self.prioritaire = bool(brut.get("prioritaire"))
        # 'Lyon' tout court désigne n'importe quel arrondissement : on
        # l'accepte sur le code postal mais jamais sur le nom, sans quoi il
        # ferait entrer dans la zone les huit arrondissements hors périmètre
        self.cp_only = bool(brut.get("cp_only"))
        self.noms_norm = {_norm(n) for n in [self.nom] + list(brut.get("alias") or [])}

    def __repr__(self):
        return f"<Commune {self.nom}>"


def distance_m(lat1, lon1, lat2, lon2):
    """Distance a vol d'oiseau en metres (haversine)."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


class Zone:
    """L'ensemble des communes surveillees, avec les tests d'appartenance.

    Deux modes, qui repondent a deux questions differentes :

    - par COMMUNES, pour un logement : on accepte une liste de villes, et
      le code postal sert de cle de rattachement.
    - par RAYON autour d'un point, pour un parking : la, seule la distance
      reelle compte. Un garage a 300 m est utile, le meme a 3 km ne l'est
      pas, alors que les deux sont « a Saint-Etienne ».

    Le mode rayon ne s'applique qu'aux annonces geolocalisees. Les sites
    qui ne publient pas de coordonnees retombent sur le test par commune,
    volontairement permissif : mieux vaut une alerte de trop qu'un garage
    a 200 m jamais signale.
    """

    def __init__(self, villes, centre=None):
        self.communes = [Commune(v) for v in villes or []]
        self.cps = {cp for c in self.communes for cp in c.cps}
        self.noms_norm = {n for c in self.communes if not c.cp_only for n in c.noms_norm}
        self.centre = centre or None

    # ------------------------------------------------------------ accès
    @property
    def noms(self):
        """Noms lisibles, pour les providers qui interrogent par commune."""
        return [c.nom for c in self.communes if not c.cp_only]

    @property
    def prioritaires(self):
        """Communes assez importantes pour justifier un appel payant."""
        return [c for c in self.communes if c.prioritaire]

    def par_cp(self, cp):
        for c in self.communes:
            if str(cp) in c.cps:
                return c
        return None

    # ------------------------------------------------------------ tests
    def distance(self, lat, lon):
        """Distance au centre en metres, ou None hors mode rayon."""
        if not self.centre or lat is None or lon is None:
            return None
        return distance_m(self.centre["lat"], self.centre["lon"], float(lat), float(lon))

    def accepte(self, ville=None, cp=None, adresse=None, lat=None, lon=None):
        """L'annonce est-elle dans la zone ?

        Trois niveaux, du plus fiable au plus permissif :
          1. un code postal fourni explicitement par le site,
          2. un code postal repéré dans l'adresse,
          3. à défaut seulement, le nom de la commune.

        Dès qu'un code postal est disponible il tranche seul, y compris pour
        refuser : c'est ce qui écarte les communes « aux alentours » que
        PAP, SeLoger et Immojeune glissent dans leurs résultats.

        On ne cherche jamais de code postal dans une description libre : un
        « 69001 » cité dans un texte de vente parlerait d'autre chose que de
        la localisation du bien.
        """
        # en mode rayon, des coordonnees tranchent seules
        d = self.distance(lat, lon)
        if d is not None:
            return d <= self.centre.get("rayon_m", 1000)

        if cp:
            m = CP_RE.search(str(cp))
            if m:
                return m.group(1) in self.cps
        if adresse:
            trouves = CP_RE.findall(adresse)
            if trouves:
                return any(t in self.cps for t in trouves)
        for texte in (ville, adresse):
            if not texte:
                continue
            plat = _norm(texte)
            if any(n and n in plat for n in self.noms_norm):
                return True
        return False


def charger(cfg):
    return Zone(cfg.get("villes"), cfg.get("centre"))


# ---------------------------------------------------------------- cache géo

def cache_lire(espace, cle):
    """Les identifiants de zone Bien'ici et les g-codes PAP ne changent
    jamais : les redemander à chaque passage coûte une requête par commune
    et par site, soit le gros du temps d'exécution une fois la zone élargie
    à une trentaine de communes."""
    if not CACHE_FILE.exists():
        return None
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8")).get(espace, {}).get(cle)
    except (json.JSONDecodeError, OSError):
        return None


def cache_ecrire(espace, cle, valeur):
    data = {}
    if CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    data.setdefault(espace, {})[cle] = valeur
    CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True),
                          encoding="utf-8")
