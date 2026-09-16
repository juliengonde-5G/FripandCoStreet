# Nouveau routeur (PR3b, décision Julien, contraire au CDC initial) — état
# et test du matériel d'impression : la caisse imprime les tickets avec le
# MÊME matériel que l'application source (imprimante MUNBYN 047P ESC/POS
# 80 mm, réseau TCP 9100 ou USB-OTG via WebUSB depuis la tablette Android,
# tiroir-caisse Safescan SD-4141 branché sur l'imprimante). La configuration
# elle-même vit dans `app_settings` (clé `hardware`, voir
# `app/api/admin/router.py::HardwareSettingsIn` — `GET/PUT
# /admin/settings/hardware`), jamais de secret : ce routeur ne fait que
# sonder/tester le matériel déjà configuré.
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.services import escpos_service
from app.services.fiscal import PosServiceError
from app.services.jet import EVENT_PRINTER_UNREACHABLE, JournalService
from app.services.settings_service import SettingsService

logger = logging.getLogger("fripco")

router = APIRouter(prefix="/hardware", tags=["hardware"])


@router.get("/printer/status")
async def printer_status(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """État de l'imprimante ticket — pastille 🟢/🔴 de l'écran Matériel.

    Sonde TCP (2 s max), jamais d'exception non gérée : hors mode réseau
    (webusb/none), ``online``/``latency_ms`` valent ``None`` (pas de sonde
    serveur possible pour une imprimante branchée en USB sur la tablette).
    """
    hardware = await SettingsService(db).get("hardware")
    mode = hardware.get("printer_mode", "none")
    host = hardware.get("printer_host") or ""
    port = int(hardware.get("printer_port") or escpos_service.DEFAULT_PORT)

    if mode != "network":
        return {
            "mode": mode,
            "host": host or None,
            "port": port,
            "online": None,
            "latency_ms": None,
        }

    status = await escpos_service.ping_printer(host, port)
    return {
        "mode": mode,
        "host": host or None,
        "port": port,
        "online": status.online,
        "latency_ms": status.latency_ms,
    }


@router.post("/receipt/test")
async def receipt_test(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Imprime un ticket de test sur la MUNBYN réseau (bouton "Tester" de
    l'écran Matériel). 409 si l'imprimante n'est pas en mode réseau ; 502 si
    le TCP échoue.

    L'écran Admin Matériel peut afficher ``host:port`` dans le message
    d'erreur (l'opérateur les a saisis lui-même) — mais jamais l'errno
    système brut, qui reste réservé au log serveur et au JET
    `printer.unreachable`.
    """
    hardware = await SettingsService(db).get("hardware")
    if hardware.get("printer_mode") != "network":
        raise PosServiceError(
            "Imprimante ticket non configurée en réseau (Paramètres > Matériel).",
            code="printer_disabled",
            status_code=409,
        )
    host = hardware.get("printer_host") or ""
    port = int(hardware.get("printer_port") or escpos_service.DEFAULT_PORT)
    shop = await SettingsService(db).get("shop")
    payload = escpos_service.build_test_ticket(shop_name=shop.get("name") or "")
    try:
        await escpos_service.send_to_printer(host, port, payload)
    except escpos_service.PrinterUnreachable as exc:
        logger.warning("Imprimante injoignable (%s:%s) : %s", exc.host, exc.port, exc)
        await JournalService(db).record(
            EVENT_PRINTER_UNREACHABLE,
            user_id=user.id,
            payload={"host": exc.host, "port": exc.port, "context": "hardware_test"},
        )
        await db.commit()
        raise PosServiceError(
            escpos_service.printer_unreachable_admin_message(exc.host, exc.port),
            code="printer_unreachable",
            status_code=502,
        )
    return {"printed": True, "host": host, "port": port}


@router.get("/receipt/test-escpos")
async def receipt_test_escpos(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Octets ESC/POS bruts du ticket de test, pour le bouton "Tester" en
    mode WebUSB (tablette)."""
    shop = await SettingsService(db).get("shop")
    payload = escpos_service.build_test_ticket(shop_name=shop.get("name") or "")
    return Response(
        content=payload,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# PR12 (N4) — tableau du matériel compatible
# ---------------------------------------------------------------------------

# Liste STATIQUE, volontairement dans le code et non en base : ce n'est pas un
# réglage de la boutique mais un état de nos essais. Elle se met à jour avec
# une PR (et sa relecture), pas depuis un écran d'administration où personne
# ne saurait dire si « testé » a été constaté ou espéré.
#
# `status` :
#   tested        — matériel réellement branché et exercé de bout en bout ;
#   recommended   — compatible et conseillé, non nécessaire au fonctionnement ;
#   not_supported — ne marche pas, avec la raison (pour éviter l'achat).
HARDWARE_COMPATIBILITY: tuple[dict[str, str], ...] = (
    {
        "category": "Tablette de caisse",
        "model": "Tablette Android + Chrome",
        "connection": "Wi-Fi de la boutique",
        "status": "tested",
        "notes": (
            "Le poste de caisse. Chrome est nécessaire pour l'impression USB "
            "(WebUSB) et pour installer l'application sur l'écran d'accueil."
        ),
    },
    {
        "category": "Tablette de caisse",
        "model": "iPad / Safari",
        "connection": "Wi-Fi de la boutique",
        "status": "not_supported",
        "notes": (
            "Safari n'a pas WebUSB : l'imprimante branchée en USB sur la "
            "tablette est impossible, et Chrome ne s'installe pas sur iPad. "
            "Envisageable uniquement avec une imprimante en réseau."
        ),
    },
    {
        "category": "Imprimante ticket",
        "model": "MUNBYN 047P (ESC/POS 80 mm)",
        "connection": "Réseau, TCP 9100",
        "status": "tested",
        "notes": (
            "IP fixe (réservation DHCP) ; l'API doit pouvoir joindre cette IP. "
            "Se configure dans Administration → Matériel."
        ),
    },
    {
        "category": "Imprimante ticket",
        "model": "MUNBYN 047P (ESC/POS 80 mm)",
        "connection": "USB-OTG sur la tablette (WebUSB)",
        "status": "tested",
        "notes": (
            "C'est la tablette qui envoie les octets, pas le serveur : aucune "
            "contrainte réseau. Association une fois depuis Administration → "
            "Matériel ; HTTPS obligatoire."
        ),
    },
    {
        "category": "Tiroir-caisse",
        "model": "Safescan SD-4141",
        "connection": "RJ-12 sur l'imprimante ticket",
        "status": "tested",
        "notes": (
            "Ouvert par l'impulsion envoyée par l'imprimante (ESC p m) : il "
            "n'existe pas de branchement direct sur la tablette, le tiroir "
            "suppose donc une imprimante configurée."
        ),
    },
    {
        "category": "Terminal de paiement",
        "model": "SumUp Solo",
        "connection": "Wi-Fi, compte SumUp",
        "status": "tested",
        "notes": (
            "Le montant est poussé depuis la caisse quand l'identifiant du "
            "terminal est configuré : rien à retaper sur le terminal."
        ),
    },
    {
        "category": "Douchette code-barres",
        "model": "Douchette USB HID (mode clavier)",
        "connection": "USB sur la tablette",
        "status": "recommended",
        "notes": (
            "Non nécessaire : la caisse est en saisie libre, sans catalogue "
            "d'articles à scanner."
        ),
    },
)


@router.get("/compatibility")
async def hardware_compatibility(
    _user: Annotated[User, Depends(get_current_user)],
):
    """Matériel testé, conseillé ou à éviter — carte « Matériel compatible ».

    Lecture pure : aucun accès base, aucun réglage, aucun secret. La liste
    répond à la question posée avant un achat (« est-ce que ça marchera ? »),
    pas à celle de l'état courant du matériel installé, qui est le rôle de
    `GET /hardware/printer/status`.
    """
    return {"items": [dict(item) for item in HARDWARE_COMPATIBILITY]}
