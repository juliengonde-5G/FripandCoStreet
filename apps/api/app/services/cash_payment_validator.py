# Extrait de Vintiz (apps/api/app/services/cash_payment_validator.py), copie
# fidele.
"""Validation des plafonds legaux de paiement en especes.

Conformite francaise :
- Article L.112-6 du Code monetaire et financier + decret n° 2015-741
- Particuliers residents fiscaux francais -> professionnel : **1 000 € max**
- Non-residents fiscaux francais (touristes) : **15 000 € max**
- Sanction (CGI art. 1840 J) : amende de **5 % des sommes payees indument**,
  solidaire entre payeur et beneficiaire, minimum 150 €.

Les paiements especes qui depassent le plafond sont **bloques**, sans
override (pas de statut touriste saisi au POS Frip & Co Street — mono-
boutique, mono-utilisateur ; le plafond resident s'applique toujours).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

CASH_CAP_RESIDENT_EUR = Decimal("1000")
CASH_CAP_TOURIST_EUR = Decimal("15000")


@dataclass(frozen=True)
class CashValidationResult:
    """Resultat d'une validation de paiement especes.

    `cash_total_eur` : total cumule des paiements de methode `cash` sur une
    transaction (un paiement mixte especes + CB ne contourne pas le
    plafond — c'est le total especes qui compte).
    """

    cash_total_eur: Decimal
    cap_eur: Decimal
    is_tourist: bool
    over_cap: bool
    reason: str | None  # message lisible quand `over_cap=True`


def cap_for(is_tourist: bool) -> Decimal:
    return CASH_CAP_TOURIST_EUR if is_tourist else CASH_CAP_RESIDENT_EUR


_CASH_METHOD_ALIASES = {"cash", "especes", "espèces"}


def sum_cash_payments(payments: Iterable[object]) -> Decimal:
    """Somme les paiements especes quel que soit le DTO source.

    Accepte des objets exposant ``method`` (str ou Enum) et ``amount``
    (numerique). Les autres methodes (``card``) sont ignorees.
    """
    total = Decimal("0")
    for p in payments:
        method_raw = getattr(p, "method", None)
        if method_raw is None:
            continue
        method = (
            method_raw.value if hasattr(method_raw, "value") else str(method_raw)
        ).lower()
        if method not in _CASH_METHOD_ALIASES:
            continue
        amount = getattr(p, "amount", None)
        if amount is None:
            continue
        total += Decimal(str(amount))
    return total


def validate(
    payments: Iterable[object],
    *,
    is_tourist: bool = False,
) -> CashValidationResult:
    """Verifie qu'un panier de paiements respecte le plafond especes.

    Parametres :
        payments : liste des paiements de la transaction (PaymentInput,
            ORM Payment, ou n'importe quel objet avec ``method`` + ``amount``).
        is_tourist : reserve a un usage futur (non expose au POS PR2).

    Retour :
        ``CashValidationResult`` avec ``over_cap`` et ``reason`` lisible.
    """
    total = sum_cash_payments(payments)
    cap = cap_for(is_tourist)
    over = total > cap
    reason: str | None = None
    if over:
        reason = (
            f"Paiement especes de {total:.2f} € depassant le plafond legal de "
            f"{cap:.0f} € (CMF art. L.112-6). Sanction : amende 5 % solidaire "
            "(CGI art. 1840 J)."
        )
    return CashValidationResult(
        cash_total_eur=total,
        cap_eur=cap,
        is_tourist=is_tourist,
        over_cap=over,
        reason=reason,
    )
