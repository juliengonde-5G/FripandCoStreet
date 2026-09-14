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

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.services import escpos_service
from app.services.fiscal import PosServiceError
from app.services.settings_service import SettingsService

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
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Imprime un ticket de test sur la MUNBYN réseau (bouton "Tester" de
    l'écran Matériel). 409 si l'imprimante n'est pas en mode réseau ; 502 si
    le TCP échoue."""
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
        raise PosServiceError(str(exc), code="printer_unreachable", status_code=502)
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
