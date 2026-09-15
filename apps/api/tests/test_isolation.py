"""Garde-fou d'isolation : verifie qu'aucune reference au nom de l'appli-
cation source n'a ete oubliee nulle part dans le depot fripco-street
(code, commentaires, documentation, configuration, hotes/conteneurs). Cf.
CDC_Caisse_FripCo_Street.md §3.4 et fripco-street/CLAUDE.md.

Le mot interdit n'est jamais ecrit en toutes lettres dans ce fichier : il
est reconstruit par concatenation ("vin" + "tiz"), afin que ce test puisse
se scanner lui-meme sans se faire echouer par sa propre presence dans le
depot.
"""

from pathlib import Path

# Racine du depot : trois niveaux au-dessus de ce fichier
# (tests/ -> apps/api/ -> apps/ -> racine).
FRIPCO_STREET_ROOT = Path(__file__).resolve().parents[3]

# Mot interdit (insensible a la casse), construit sans jamais l'ecrire tel quel.
_FORBIDDEN_WORD = ("vin" + "tiz").lower()

# Repertoires generes/tiers, jamais du contenu du depot lui-meme.
EXCLUDED_DIR_NAMES = {
    ".git",
    "node_modules",
    ".next",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
}
EXCLUDED_DIR_SUFFIXES = (".egg-info",)

# Fichiers binaires : ne sont jamais des "fichiers texte".
EXCLUDED_SUFFIXES = {".png", ".jpg", ".jpeg", ".pdf"}


def _is_in_excluded_dir(path: Path) -> bool:
    for part in path.parts:
        if part in EXCLUDED_DIR_NAMES:
            return True
        if part.endswith(EXCLUDED_DIR_SUFFIXES):
            return True
    return False


def _iter_scanned_files():
    for path in FRIPCO_STREET_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if _is_in_excluded_dir(path):
            continue
        if path.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        yield path


def test_no_forbidden_reference_anywhere_in_the_repo():
    """Aucune occurrence du mot interdit, insensible a la casse, dans
    aucun fichier texte du depot — y compris ce fichier de test lui-meme,
    la documentation, les scripts, la configuration Docker/Caddy et les
    workflows GitHub. Couvre par la meme regle les hotes/conteneurs
    eventuels (ex. un domaine ou un nom de conteneur qui contiendrait ce
    mot en prefixe/suffixe), puisque c'est une simple recherche de
    sous-chaine, pas une liste de motifs exacts a maintenir.
    """
    offenders: list[str] = []
    for path in _iter_scanned_files():
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _FORBIDDEN_WORD in raw.lower():
            offenders.append(str(path.relative_to(FRIPCO_STREET_ROOT)))
    assert not offenders, (
        "Reference interdite trouvee (insensible a la casse) dans :\n"
        + "\n".join(sorted(offenders))
    )
