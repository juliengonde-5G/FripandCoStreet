# Nouveau service (PR10, docs/ARCHITECTURE_PR10.md, L5) — accuse de
# reception de la demande de suppression d'une fiche cliente.
#
# Meme discipline que l'e-mail du ticket (`receipt_email.py`) : AUCUN lien
# de suivi, AUCUNE image externe, un HTML sobre et un texte brut complet.
# C'est un e-mail transactionnel qui repond a l'exercice d'un droit — il
# doit arriver, se lire dans n'importe quel client mail, et ne rien
# mesurer.
#
# Il n'y a volontairement PAS de lien d'annulation dans le message : un
# lien qui effacerait (ou retablirait) une fiche sans authentification
# serait exactement le contraire d'une protection des donnees. L'annulation
# se fait en boutique, de vive voix, comme la demande elle-meme.
from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from app.services.email_gateway import EmailMessage

_PARIS = ZoneInfo("Europe/Paris")

SUBJECT = "Votre demande de suppression est enregistrée"


def format_effective_date(moment: datetime) -> str:
    """Date d'effet telle qu'elle est ecrite a la cliente : JJ/MM/AAAA dans
    le fuseau de la BOUTIQUE, pas celui du serveur."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo("UTC"))
    return moment.astimezone(_PARIS).strftime("%d/%m/%Y")


def build_deletion_request_email(
    *,
    to: str,
    scheduled_for: datetime,
    shop: dict[str, Any] | None = None,
) -> EmailMessage:
    """Accuse de reception : date d'effet, possibilite d'annuler en
    boutique, contact du delegue a la protection des donnees."""
    shop = shop or {}
    shop_name = (shop.get("name") or "Frip & Co Street").strip()
    dpo_email = (shop.get("dpo_email") or "").strip()
    effective = format_effective_date(scheduled_for)

    lines = [
        "Bonjour,",
        "",
        f"Nous avons bien enregistré votre demande de suppression de vos "
        f"données chez {shop_name}.",
        "",
        f"Elle prendra effet le {effective}. D'ici là, vos informations "
        "restent inchangées : si vous changez d'avis, il vous suffit de "
        "nous le dire en boutique et nous annulerons la demande.",
        "",
        "Passé cette date, votre fiche est vidée de toute donnée "
        "personnelle. Vos tickets de caisse, eux, sont conservés sans "
        "aucune information vous concernant : la loi nous impose de garder "
        "la trace comptable des ventes.",
    ]
    if dpo_email:
        lines += [
            "",
            f"Pour toute question sur vos données : {dpo_email}.",
        ]
    lines += ["", shop_name]
    text = "\n".join(lines)

    html = (
        '<div style="font-family:sans-serif;max-width:480px;margin:0 auto;color:#111">'
        f'<h2 style="margin:0 0 12px">{escape(shop_name)}</h2>'
        "<p>Bonjour,</p>"
        "<p>Nous avons bien enregistré votre demande de suppression de vos "
        f"données chez {escape(shop_name)}.</p>"
        f"<p>Elle prendra effet le <b>{escape(effective)}</b>. D'ici là, vos "
        "informations restent inchangées : si vous changez d'avis, il vous "
        "suffit de nous le dire en boutique et nous annulerons la demande.</p>"
        "<p>Passé cette date, votre fiche est vidée de toute donnée "
        "personnelle. Vos tickets de caisse, eux, sont conservés sans aucune "
        "information vous concernant : la loi nous impose de garder la trace "
        "comptable des ventes.</p>"
        + (
            '<p style="font-size:12px;color:#666;margin-top:24px">Pour toute '
            f"question sur vos données : {escape(dpo_email)}.</p>"
            if dpo_email
            else ""
        )
        + "</div>"
    )

    return EmailMessage(to=to, subject=SUBJECT, html=html, text=text)
