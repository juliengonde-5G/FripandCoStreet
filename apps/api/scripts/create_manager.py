#!/usr/bin/env python3
# Extrait de l'application source (scripts/create_manager.py) — plus de role (compte
# unique) : refuse toujours de creer un deuxieme compte.
"""Cree interactivement le compte unique, sans mot de passe par defaut."""

from __future__ import annotations

import argparse
import asyncio
import getpass

from sqlalchemy import func, select

from app.core.database import async_session
from app.core.security import hash_password
from app.models.user import User


async def _create(username: str, email: str, password: str) -> None:
    async with async_session() as db:
        existing_count = (
            await db.execute(select(func.count(User.id)))
        ).scalar_one()
        if existing_count:
            raise RuntimeError(
                "Un compte existe deja ; fripco-street est mono-utilisateur."
            )
        duplicate = (
            await db.execute(
                select(User.id).where(
                    (User.username == username) | (User.email == email)
                )
            )
        ).scalar_one_or_none()
        if duplicate is not None:
            raise RuntimeError("Username or email already exists")
        db.add(
            User(
                username=username,
                email=email,
                hashed_password=hash_password(password),
                is_active=True,
            )
        )
        await db.commit()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cree le compte unique de fripco-street"
    )
    parser.add_argument("--username", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument(
        "--password",
        default=None,
        help="Mot de passe (CI uniquement) ; sinon demande de facon interactive.",
    )
    args = parser.parse_args()
    username = args.username.strip()
    email = args.email.strip().lower()
    if len(username) < 3 or "@" not in email:
        parser.error("valid --username and --email are required")

    if args.password is not None:
        password = args.password
        if len(password) < 12:
            parser.error("Le mot de passe doit contenir au moins 12 caracteres.")
    else:
        password = getpass.getpass("Mot de passe (12+ caracteres) : ")
        if len(password) < 12:
            parser.error("Le mot de passe doit contenir au moins 12 caracteres.")
        confirmation = getpass.getpass("Confirmer le mot de passe : ")
        if password != confirmation:
            parser.error("Les mots de passe ne correspondent pas.")

    asyncio.run(_create(username, email, password))
    print(f"Compte {username!r} cree ; aucun mot de passe par defaut n'a ete stocke")


if __name__ == "__main__":
    main()
