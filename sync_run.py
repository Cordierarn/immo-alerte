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


# topic ntfy depuis le fichier local (gitignoré)
topic_file = BASE / "topic.txt"
if topic_file.exists():
    os.environ["NTFY_TOPIC"] = topic_file.read_text(encoding="utf-8").strip()

# récupère la mémoire mise à jour par le cloud
git("pull", "--rebase", "--quiet")

# passage de veille
subprocess.run([sys.executable, str(BASE / "immo_alerte.py")])

# repartage la mémoire locale (PAP notamment, invisible depuis le cloud)
git("add", "seen.json")
if git("diff", "--cached", "--quiet").returncode != 0:
    git("commit", "--quiet", "-m", "maj: annonces vues (local)")
    if git("pull", "--rebase", "--quiet").returncode == 0:
        git("push", "--quiet")
