# -*- coding: utf-8 -*-
"""
Lanceur local : synchronise la mémoire des annonces vues avec GitHub
(le cloud tourne aussi), puis exécute un passage de veille.

Le topic ntfy est lu dans topic.txt (fichier local, jamais commité).
Planifié via la tâche Windows "ImmoAlerte".
"""
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).parent
os.chdir(BASE)


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True)


# topic.txt est lu directement par immo_alerte.topics_ntfy(), rien à faire ici.
# Seule la clé Scrapfly doit passer par l'environnement.
key_file = BASE / "scrapfly_key.txt"
if key_file.exists():
    os.environ["SCRAPFLY_KEY"] = key_file.read_text(encoding="utf-8").strip()

# récupère la mémoire mise à jour par le cloud
git("pull", "--rebase", "--quiet")

# passage de veille
subprocess.run([sys.executable, str(BASE / "immo_alerte.py")])

# repartage la mémoire locale (PAP notamment, invisible depuis le cloud)
# et le ledger de crédits, pour que le cloud connaisse la dépense locale
git("add", "seen.json", "budget.json")
if git("diff", "--cached", "--quiet").returncode != 0:
    git("commit", "--quiet", "-m", "maj: annonces vues (local)")
    if git("pull", "--rebase", "--quiet").returncode == 0:
        git("push", "--quiet")
