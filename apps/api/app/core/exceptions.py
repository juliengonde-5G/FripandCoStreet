# Extrait de l'application source (apps/api/app/core/exceptions.py)
"""Hierarchie d'exceptions metier pour fripco-street.

Les services levent ces exceptions plutot que HTTPException pour rester
decouples de la couche HTTP. Les routers (et le handler d'exception global)
les attrapent et les traduisent vers le code de statut HTTP approprie.
"""

from __future__ import annotations


class FripcoError(Exception):
    """Classe de base pour toutes les exceptions metier."""


class ResourceNotFound(FripcoError):
    def __init__(self, resource: str, identifier=None) -> None:
        msg = f"{resource} not found"
        if identifier is not None:
            msg += f": {identifier}"
        super().__init__(msg)
        self.resource = resource
        self.identifier = identifier


class ResourceConflict(FripcoError):
    """L'operation entre en conflit avec l'etat existant (ex : doublon)."""


class AuthenticationError(FripcoError):
    """Identifiants invalides."""


class PermissionDenied(FripcoError):
    """L'appelant n'a pas la permission d'effectuer cette action."""
