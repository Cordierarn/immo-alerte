# -*- coding: utf-8 -*-
"""
Client Scrapfly « économe » : chaque crédit compte.

Le quota (200 000 crédits/mois, plan Discovery) est un plafond DUR : une fois
consommé, les scrapes échouent. Il est en plus partagé avec un autre projet.
Ce module met donc trois garde-fous en série avant toute dépense :

  1. cadence      : on n'appelle Scrapfly que si assez de temps s'est écoulé
                    depuis le dernier appel du provider (cadence variable
                    selon l'heure : les annonces sortent en journée).
  2. budget local : un ledger (budget.json) compte les crédits dépensés dans
                    le mois par ce projet et refuse de dépasser l'enveloppe.
  3. réserve API  : chaque réponse renvoie le crédit restant du COMPTE
                    (header X-Scrapfly-Remaining-Api-Credit). En dessous de
                    la réserve, on coupe — c'est ce qui protège l'autre projet.

Côté requête, les réglages par défaut sont les moins chers possibles :
pas de render_js, pas de proxy résidentiel forcé, pas d'extraction IA,
un cost_budget qui fait échouer (non facturé) toute escalade trop chère.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from zoneinfo import ZoneInfo
    PARIS = ZoneInfo("Europe/Paris")
except Exception:  # tzdata absent : on retombe sur l'heure locale
    PARIS = None

API = "https://api.scrapfly.io/scrape"

BASE = Path(__file__).parent
LEDGER_FILE = BASE / "budget.json"


class BudgetEpuise(Exception):
    """Plus de crédits disponibles pour ce projet : on ne scrape pas."""


class TropTot(Exception):
    """La cadence n'autorise pas encore un nouvel appel pour ce provider."""


class ScrapflyKO(Exception):
    """Le scrape a échoué (non facturé la plupart du temps)."""


# ---------------------------------------------------------------- Ledger

def _mois_courant():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def charger_ledger():
    """Ledger des crédits consommés. Remis à zéro au changement de mois."""
    vide = {"mois": _mois_courant(), "credits": 0, "appels": 0,
            "par_provider": {}, "dernier_appel": {}, "restant_compte": None}
    if not LEDGER_FILE.exists():
        return vide
    try:
        led = json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
    except Exception:
        return vide
    if led.get("mois") != _mois_courant():
        return vide
    for cle, defaut in vide.items():
        led.setdefault(cle, defaut)
    return led


def sauver_ledger(led):
    LEDGER_FILE.write_text(json.dumps(led, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")


# ---------------------------------------------------------------- Cadence

def cadence_requise(cfg, override=None, maintenant=None):
    """Intervalle minimum (en secondes) entre deux appels, selon l'heure.

    Les annonces de location sortent massivement en journée ouvrée. Scanner
    toutes les 5 min à 3h du matin coûte le même prix qu'à 14h pour dix fois
    moins de nouveautés : on ralentit la nuit et le week-end. C'est le levier
    d'économie le plus rentable — il ne fait perdre aucune annonce utile.
    """
    now = maintenant or (datetime.now(PARIS) if PARIS else datetime.now())
    cad = dict(cfg.get("cadence") or {})
    cad.update(override or {})
    pleine = cad.get("pleine", 300)      # 5 min  — heures ouvrées
    creuse = cad.get("creuse", 1800)     # 30 min — soir et week-end
    nuit = cad.get("nuit", 7200)         # 2 h    — 22h-7h

    h = now.hour
    if h < 7 or h >= 22:
        return nuit
    if 9 <= h < 19 and now.weekday() < 5:
        return pleine
    return creuse


def _peut_appeler(led, provider, cfg, override=None):
    dernier = led["dernier_appel"].get(provider)
    if dernier is None:
        return True, 0
    ecoule = time.time() - dernier
    requis = cadence_requise(cfg, override)
    return ecoule >= requis, max(0, requis - ecoule)


# ---------------------------------------------------------------- Scrape

def _b(v):
    return "true" if v else "false"


def scrape(url, *, provider, cfg, log=print,
           asp=True, render_js=False, country="fr",
           cache=False, cache_ttl=None, session=None,
           cost_budget=None, retry=False, tags=None,
           cadence=None, ignorer_cadence=False):
    """Un appel Scrapfly, avec tous les garde-fous.

    Renvoie {"content", "status_code", "cost", "url", "cache_hit"}.
    Lève TropTot / BudgetEpuise / ScrapflyKO — jamais de dépense silencieuse.

    Réglages par défaut et pourquoi :
      asp=True        ASP est gratuit sur une page non protégée ; il ne coûte
                      que s'il doit réellement contourner quelque chose.
      render_js=False +5 crédits. Leboncoin et SeLoger servent leur JSON dans
                      le HTML initial : le navigateur est inutile.
      proxy_pool      NON transmis volontairement. Forcer le résidentiel, c'est
                      payer 25 crédits d'office ; on laisse l'ASP n'escalader
                      que si le datacenter (1 crédit) se fait bloquer.
      retry=False     Scrapfly réessaie par défaut, ce qui peut enchaîner les
                      escalades. Ici un échec coûte zéro et le prochain passage
                      est dans 10 min : mieux vaut échouer que surpayer.
      cost_budget     Plafond DUR côté serveur. Au-delà, la requête échoue
                      (non facturée) au lieu de partir à 60+ crédits.
      format          absent = 'raw'. Les modes extraction_* (IA) sont facturés
                      en plus : on parse en local, c'est gratuit.
    """
    key = (os.environ.get("SCRAPFLY_KEY") or cfg.get("api_key", "")).strip()
    if not key:
        raise ScrapflyKO("SCRAPFLY_KEY absente (secret GitHub ou config.json)")

    led = charger_ledger()

    # --- garde-fou 1 : cadence
    if not ignorer_cadence:
        ok, reste = _peut_appeler(led, provider, cfg, cadence)
        if not ok:
            raise TropTot(f"{provider}: prochain appel dans {reste / 60:.0f} min")

    # --- garde-fou 2 : enveloppe mensuelle du projet
    enveloppe = cfg.get("budget_mensuel", 150000)
    plafond_req = cost_budget or cfg.get("cout_max_par_requete", 35)
    if led["credits"] + plafond_req > enveloppe:
        raise BudgetEpuise(
            f"enveloppe immo atteinte ({led['credits']}/{enveloppe} crédits ce mois)")

    # --- garde-fou 3 : réserve pour l'autre projet (crédit restant du compte)
    reserve = cfg.get("reserve_autre_projet", 20000)
    restant = led.get("restant_compte")
    if restant is not None and restant - plafond_req < reserve:
        raise BudgetEpuise(
            f"réserve compte atteinte ({restant} crédits restants, réserve {reserve})")

    params = {
        "key": key,
        "url": url,
        "asp": _b(asp),
        "render_js": _b(render_js),
        "retry": _b(retry),
        "cost_budget": plafond_req,
        "tags": ",".join(["immo-alerte", provider] + list(tags or [])),
    }
    if country:
        params["country"] = country
    # cache et session sont mutuellement exclusifs côté Scrapfly
    if session:
        params["session"] = session
    elif cache:
        params["cache"] = "true"
        params["cache_ttl"] = cache_ttl or 3600

    cout = 0
    try:
        r = requests.get(API, params=params, timeout=180)
        cout = int(r.headers.get("X-Scrapfly-Api-Cost", 0) or 0)
        restant_hdr = r.headers.get("X-Scrapfly-Remaining-Api-Credit")
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    except Exception as e:
        raise ScrapflyKO(f"{provider}: appel API impossible: {e}")
    finally:
        # on enregistre l'appel même en cas d'échec : la cadence doit tenir
        led["dernier_appel"][provider] = time.time()
        led["appels"] += 1
        if cout:
            led["credits"] += cout
            led["par_provider"][provider] = led["par_provider"].get(provider, 0) + cout
        sauver_ledger(led)

    if restant_hdr:
        try:
            led["restant_compte"] = int(restant_hdr)
            sauver_ledger(led)
        except ValueError:
            pass

    result = (data or {}).get("result") or {}
    contenu = result.get("content")
    statut = result.get("status_code")
    cache_hit = bool(((data.get("context") or {}).get("cache") or {}).get("state") == "HIT")

    if r.status_code != 200 or not contenu:
        msg = ((data.get("result") or {}).get("error") or {}).get("message") \
              or data.get("message") or r.text[:200]
        raise ScrapflyKO(f"{provider}: HTTP {r.status_code} — {msg} (coût {cout})")

    log(f"  Scrapfly [{provider}] {statut} — {cout} crédits"
        f"{' (CACHE)' if cache_hit else ''}"
        f" — cumul mois {led['credits']}/{enveloppe}"
        + (f", compte {led['restant_compte']}" if led.get("restant_compte") else ""))

    return {"content": contenu, "status_code": statut, "cost": cout,
            "url": result.get("url", url), "cache_hit": cache_hit}


# ---------------------------------------------------------------- Rapport

def rapport():
    led = charger_ledger()
    lignes = [f"Mois {led['mois']} — {led['credits']} crédits sur {led['appels']} appel(s)"]
    for p, c in sorted(led["par_provider"].items(), key=lambda kv: -kv[1]):
        lignes.append(f"  {p:<12} {c:>7} crédits")
    if led.get("restant_compte") is not None:
        lignes.append(f"  restant compte (tous projets) : {led['restant_compte']}")
    return "\n".join(lignes)


if __name__ == "__main__":
    print(rapport())
