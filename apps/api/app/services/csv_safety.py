# Nouveau module (PR11, revue Codex #17) — neutralisation des cellules CSV.
#
# Un tableur (Excel, LibreOffice, Google Sheets) ne lit pas un CSV comme un
# fichier de donnees : toute cellule commencant par `=`, `+`, `-` ou `@` est
# interpretee comme une FORMULE a l'ouverture. Or nos exports contiennent du
# texte saisi librement en caisse — un libelle d'article, un nom de vendeuse,
# demain le nom d'une cliente. Un libelle `=1+1` s'affiche « 2 » ; un libelle
# mieux choisi appelle une fonction externe (`=HYPERLINK(...)`,
# `=WEBSERVICE(...)`, `=cmd|...`) et fait fuiter le contenu du fichier vers
# un serveur tiers des que quelqu'un clique « activer le contenu ».
#
# C'est une injection dont la victime n'est PAS notre application : elle
# s'execute chez la personne qui ouvre le fichier — la boutique, son
# comptable, l'outil d'e-mailing. Aucune validation en amont ne peut la
# prevenir : un libelle d'article a parfaitement le droit de commencer par
# un tiret, et on ne va pas refuser une vente pour ca. La neutralisation se
# fait donc a l'ECRITURE de chaque export, et nulle part ailleurs.
#
# Mecanique : on prefixe une apostrophe simple, la convention universelle
# des tableurs pour dire « ceci est du texte ». Elle n'est pas affichee dans
# la cellule — la valeur reste lisible telle quelle a l'ecran.
#
# Partage volontairement entre TOUS les exports CSV du depot (rapports M1,
# abonnees newsletter M4, exports par table) : un seul endroit a relire, un
# seul endroit a corriger si un tableur invente demain un cinquieme
# caractere declencheur.
from __future__ import annotations

from typing import Any

# Caracteres qui ouvrent une formule dans au moins un tableur courant.
# `-` en fait partie : `-1+1` est une formule valide, et c'est aussi le
# debut legitime d'un libelle ecrit a la main — d'ou la neutralisation
# plutot que le refus.
_FORMULA_PREFIXES = ("=", "+", "-", "@")

# Tabulation, retour chariot et saut de ligne en TETE de cellule : certains
# tableurs les ignorent avant d'interpreter le caractere suivant, ce qui
# permet de masquer un `=` derriere un blanc. On neutralise donc aussi.
_CONTROL_PREFIXES = ("\t", "\r", "\n")


def neutralize_csv_cell(value: Any) -> Any:
    """Rend une cellule CSV inoffensive a l'ouverture dans un tableur.

    Prefixe une apostrophe simple aux valeurs TEXTE qui commencent par un
    caractere d'ouverture de formule. La valeur affichee est conservee
    (l'apostrophe est une marque de texte, le tableur ne la montre pas).

    Les valeurs non textuelles (entiers, `Decimal`, `None`) sont rendues
    telles quelles : un montant ou un compteur ne peut pas porter de
    formule, et les prefixer casserait l'addition dans le tableur — ce qui
    est precisement ce qu'on vient y faire.
    """
    if not isinstance(value, str):
        return value
    if value.startswith(_FORMULA_PREFIXES) or value.startswith(_CONTROL_PREFIXES):
        return f"'{value}"
    return value


def neutralize_csv_row(row: list[Any]) -> list[Any]:
    """`neutralize_csv_cell` appliquee a une ligne entiere."""
    return [neutralize_csv_cell(cell) for cell in row]


__all__ = ["neutralize_csv_cell", "neutralize_csv_row"]
