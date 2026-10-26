# -*- coding: utf-8 -*-
"""
Mesure l'apport d'une source avant de la brancher : combien de ses annonces
passent les filtres d'une recherche, et combien sont NEUVES, c'est-à-dire
inconnues de la mémoire (même règle que le passage : identifiant, ou
empreinte vue sur un autre site).

Rien n'est notifié ni mémorisé : seen*.json n'est jamais écrit.

Usage :
  python mesure_source.py logement --agence site.json      # une entrée au format 'agences'
  python mesure_source.py logement --scrapfly seloger [URL ...]
  python mesure_source.py logement --gratuit Immojeune
  python mesure_source.py coloc --annonces annonces.json   # liste brute issue d'une enquête

Le format brut : [{"titre", "description", "prix", "surface", "pieces",
"cp", "ville", "meuble": true|false|null, "coloc": true|false|null, "url"}].
"""

import json
import sys

import immo_alerte
import providers_agences
import providers_gratuits
import zone as zone_mod


def _filtrer_brutes(brutes, cfg, zone, garder):
    """Les critères qu'un provider applique d'habitude lui-même."""
    retenues, meuble_inconnu = [], 0
    for b in brutes:
        if not zone.accepte(cp=b.get("cp"), ville=b.get("ville")):
            continue
        if not b.get("prix") or b["prix"] > cfg.get("prix_max", 10 ** 9):
            continue
        if b.get("pieces") and cfg.get("pieces_max") and b["pieces"] > cfg["pieces_max"]:
            continue
        if cfg.get("meuble"):
            if b.get("meuble") is False:
                continue
            meuble_inconnu += b.get("meuble") is None
        if not garder(b.get("titre", ""), b.get("description", ""), coloc=b.get("coloc")):
            continue
        # la ville de l'empreinte doit être celle des autres providers : le
        # nom de la commune de la zone, déduit du code postal
        commune = zone.par_cp(str(b["cp"])) if b.get("cp") else None
        retenues.append({**b, "id": b.get("id") or b["url"], "source": b.get("source", "candidat"),
                         "ville": commune.nom if commune else b.get("ville", "")})
    if meuble_inconnu:
        immo_alerte.log(f"  {meuble_inconnu} retenue(s) dont le meublé n'est pas connu")
    return retenues


def collecter(cfg, mode, args):
    zone = zone_mod.charger(cfg)
    garder = immo_alerte.faire_garder(cfg)
    log = immo_alerte.log
    if mode == "--annonces":
        brutes = json.loads(open(args[0], encoding="utf-8").read())
        return _filtrer_brutes(brutes, cfg, zone, garder)
    if mode == "--agence":
        site = json.loads(open(args[0], encoding="utf-8").read())
        providers_agences.CONFIG = {**providers_agences.CONFIG, "agences": [site]}
        return providers_agences.provider_agences({**cfg, "agences": True}, zone, garder, log)
    if mode == "--gratuit":
        provider = dict(providers_gratuits.PROVIDERS_GRATUITS)[args[0]]
        return provider(cfg, zone, garder, log)
    if mode == "--scrapfly":
        cle, urls = args[0], args[1:]
        p = dict((cfg.get("providers") or {}).get(cle) or {})
        p["enabled"] = True
        if urls:
            p["search_url"] = urls
        return immo_alerte.providers_payants({**cfg, "providers": {cle: p}}, zone, garder, dev=True)
    raise SystemExit(__doc__)


def main():
    if len(sys.argv) < 4:
        raise SystemExit(__doc__)
    nom, mode, args = sys.argv[1], sys.argv[2], sys.argv[3:]
    cfg = next((r for r in immo_alerte.recherches() if r["nom"] == nom), None)
    if cfg is None:
        raise SystemExit(f"recherche inconnue : {nom}")
    seen = immo_alerte.load_seen(cfg)
    retenues = {a["id"]: a for a in collecter(cfg, mode, args)}.values()
    neuves = [a for a in retenues if not immo_alerte.est_connue(a, seen)]
    print(f"{len(retenues)} retenue(s), {len(neuves)} neuve(s)")
    for a in sorted(neuves, key=lambda a: a["prix"]):
        s = f"{a['surface']:.0f} m²" if a.get("surface") else "? m²"
        print(f"  {a['prix']} € | {s} | {a.get('ville', '')} | {a['url']}")


if __name__ == "__main__":
    main()
