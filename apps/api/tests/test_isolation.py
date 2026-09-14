"""Garde-fou d'isolation : fripco-street ne doit porter aucune dependance,
hote ou nom de conteneur vers Vintiz (systemes totalement isoles, cf.
CDC_Caisse_FripCo_Street.md §3.4 et fripco-street/CLAUDE.md).

Deux niveaux :
1. Les hotes/noms de conteneur Vintiz (`vintiz.fr`, `vintiz-api`, ...) sont
   INTERDITS PARTOUT dans le code/config — meme en commentaire — car un
   copier-coller de configuration reseau serait une vraie fuite de
   dependance. Seuls les fichiers Markdown (documentation en prose, ou une
   regle peut legitimement CITER ce qu'il ne faut pas faire) et ce fichier
   de test lui-meme (qui liste ces motifs comme donnees) sont exemptes.
2. Le simple mot "vintiz" (projet source, cite pour la tracabilite) reste
   tolere dans les commentaires/docstrings et dans la documentation — c'est
   le but declare de ce depot (cf. CDC §1 : "on extrait, on debranche, on
   simplifie") — mais pas dans du code executable.
"""

import re
from pathlib import Path

FRIPCO_STREET_ROOT = Path(__file__).resolve().parents[3]
THIS_FILE = Path(__file__).resolve()

SCANNED_EXTENSIONS = {
    ".py",
    ".ts",
    ".tsx",
    ".yml",
    ".yaml",
    ".sh",
    ".toml",
    ".md",
}
SCANNED_BASENAME_PREFIXES = ("Caddyfile", "Dockerfile")

EXCLUDED_DIR_NAMES = {
    ".git",
    "node_modules",
    ".next",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    "dist",
    "build",
    ".ruff_cache",
}

# Hotes/conteneurs Vintiz : ne doivent jamais apparaitre dans du code/config.
FORBIDDEN_STRINGS = [
    "vintiz.fr",
    "vintiz-api",
    "vintiz-web",
    "vintiz-db",
    "app.vintiz",
    "api.vintiz",
]

PROSE_EXTENSIONS = {".md"}

_PY_COMMENT = re.compile(r"#.*")
_PY_TRIPLE_STRING = re.compile(r"'''(?:[^'\\]|\\.|'{1,2}(?!''))*'''|\"\"\"(?:[^\"\\]|\\.|\"{1,2}(?!\"\"))*\"\"\"", re.DOTALL)
_C_LINE_COMMENT = re.compile(r"//.*")
_C_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_HASH_COMMENT = re.compile(r"#.*")


def _iter_scanned_files():
    for path in FRIPCO_STREET_ROOT.rglob("*"):
        if not path.is_file() or path == THIS_FILE:
            continue
        if any(part in EXCLUDED_DIR_NAMES for part in path.parts):
            continue
        if path.suffix in SCANNED_EXTENSIONS or path.name.startswith(
            SCANNED_BASENAME_PREFIXES
        ):
            yield path


def _strip_comments_and_docstrings(path: Path, raw: str) -> str:
    """Retire commentaires/docstrings pour ne garder que le code "actif".

    Heuristique volontairement simple (regex, pas un vrai parseur) — cela
    suffit pour distinguer "vintiz mentionne en commentaire/documentation"
    de "vintiz code en dur dans une valeur active" sur ce perimetre de
    fichiers (Python, TS/TSX, YAML, shell, Caddyfile/Dockerfile).
    """
    if path.suffix == ".py":
        stripped = _PY_TRIPLE_STRING.sub("", raw)
        stripped = _PY_COMMENT.sub("", stripped)
        return stripped
    if path.suffix in {".ts", ".tsx"}:
        stripped = _C_BLOCK_COMMENT.sub("", raw)
        stripped = _C_LINE_COMMENT.sub("", stripped)
        return stripped
    # YAML / shell / toml / Caddyfile / Dockerfile : commentaires `# ...`
    return _HASH_COMMENT.sub("", raw)


def test_no_vintiz_hosts_or_container_names():
    offenders: list[str] = []
    for path in _iter_scanned_files():
        if path.suffix in PROSE_EXTENSIONS:
            continue  # prose documentaire : peut legitimement citer la regle
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lowered = raw.lower()
        for forbidden in FORBIDDEN_STRINGS:
            if forbidden in lowered:
                offenders.append(f"{path.relative_to(FRIPCO_STREET_ROOT)}: {forbidden!r}")
    assert not offenders, (
        "References interdites a Vintiz (hote/conteneur) trouvees :\n"
        + "\n".join(offenders)
    )


def test_vintiz_word_only_in_comments_or_docs():
    offenders: list[str] = []
    for path in _iter_scanned_files():
        if path.suffix in PROSE_EXTENSIONS:
            continue  # documentation : tolere partout
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        active_code = _strip_comments_and_docstrings(path, raw)
        if "vintiz" in active_code.lower():
            offenders.append(str(path.relative_to(FRIPCO_STREET_ROOT)))
    assert not offenders, (
        "Mention de 'vintiz' en dehors d'un commentaire/docstring (donc dans "
        "du code actif) trouvee dans :\n" + "\n".join(offenders)
    )
