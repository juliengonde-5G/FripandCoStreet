# Extrait de l'application source (apps/api/app/services/tva_service.py), copie fidele —
# regime normal France (CGI art. 256 et suivants, D8 du contrat PR2). A la
# difference de l'application source (ou ce module existait mais n'etait pas branche — ecart
# C-10 de l'audit), il est ici appele depuis `services/pos.py` a chaque vente.
"""Calcul TVA en regime normal France (CGI art. 256 et suivants).

- la base imposable est le prix de vente HT
- le taux par defaut est 20 % (taux normal France), parametrable en admin
  (`app_settings.fiscal.tva_rate`)
- chaque ligne de transaction stocke son `tva_rate` propre, fige au moment
  de la vente (cf. `models/pos.py:TransactionItem.tva_rate`)

Les `unit_price` des lignes de vente sont exprimes **TTC** (UX boutique :
la cliente voit le prix qu'elle paie). La fonction de calcul fait donc le
HT depuis le TTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

# Taux de TVA reconnus en France (referentiel BOI-TVA-LIQ-30-10-10) :
# - 20.00 — taux normal
# - 10.00 — taux intermediaire
# -  5.50 — taux reduit
# -  2.10 — taux super-reduit
# -  0.00 — exonere
SUPPORTED_TVA_RATES: tuple[Decimal, ...] = (
    Decimal("0.00"),
    Decimal("2.10"),
    Decimal("5.50"),
    Decimal("10.00"),
    Decimal("20.00"),
)

DEFAULT_TVA_RATE: Decimal = Decimal("20.00")


@dataclass(frozen=True)
class LineTotals:
    """Totaux d'une ligne de transaction au regime normal de TVA.

    Tous les montants sont arrondis au centime (2 decimales, demi-haut)
    pour coherence avec les colonnes ``Numeric(10, 2)`` cote ORM.
    """

    line_ht: Decimal
    line_tva: Decimal
    line_ttc: Decimal
    tva_rate: Decimal


def _round_eur(value: Decimal) -> Decimal:
    """Arrondi commercial au centime — strictement le meme que celui que
    PostgreSQL applique sur ``Numeric(10, 2)`` (pas de banker's rounding).
    """
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def is_supported_rate(rate: Decimal | float | int | str) -> bool:
    """Verifie qu'un taux fait partie du referentiel francais autorise.

    Permissif en entree pour faciliter la validation depuis l'admin
    (formulaire web qui envoie une string, ou JSON qui envoie un float).
    """
    try:
        normalized = Decimal(str(rate)).quantize(Decimal("0.01"))
    except Exception:
        return False
    return normalized in SUPPORTED_TVA_RATES


def compute_line_totals(
    unit_price_ttc: Decimal | float | int | str,
    quantity: int,
    discount_amount: Decimal | float | int | str = 0,
    tva_rate: Decimal | float | int | str = DEFAULT_TVA_RATE,
) -> LineTotals:
    """Calcule HT / TVA / TTC d'une ligne en regime normal.

    Args:
        unit_price_ttc  : prix unitaire TTC (Decimal ou compatible).
        quantity        : quantite vendue (int positif).
        discount_amount : part de la remise globale ventilee sur cette
            ligne, en euros TTC (>= 0, <= unit_price_ttc * quantity).
        tva_rate        : taux de TVA applicable (Decimal ou compatible).

    Le HT est derive du TTC, pas l'inverse :
        line_ttc = unit_price_ttc * quantity - discount_amount
        line_ht  = line_ttc / (1 + tva_rate/100)
        line_tva = line_ttc - line_ht

    Tous les resultats sont arrondis au centime.

    Raises:
        ValueError si quantity <= 0, discount_amount hors
        [0, unit_price_ttc * quantity], ou tva_rate negatif.
    """
    qty = int(quantity)
    if qty <= 0:
        raise ValueError(f"quantity must be > 0 (got {quantity})")
    price_ttc = Decimal(str(unit_price_ttc))
    if price_ttc < 0:
        raise ValueError("unit_price_ttc must be >= 0")
    gross = price_ttc * qty
    discount = Decimal(str(discount_amount))
    if discount < 0 or discount > gross:
        raise ValueError(
            f"discount_amount must be in [0, {gross}] (got {discount_amount})"
        )
    rate = Decimal(str(tva_rate))
    if rate < 0:
        raise ValueError(f"tva_rate must be >= 0 (got {tva_rate})")

    rate_factor = Decimal("1") + rate / Decimal("100")
    if rate_factor == 0:
        # Anomalie defensive — un taux de -100 % n'a aucun sens fiscal.
        raise ValueError("tva_rate would yield a zero divisor")

    # On arrondit d'abord le TTC (montant paye par la cliente, source de
    # verite comptable), puis on derive HT du TTC arrondi, puis TVA par
    # soustraction. Cette sequence garantit l'invariant `TTC = HT + TVA`
    # a 2 decimales — un arrondi independant de chaque montant peut creer
    # un ecart d'1 centime.
    line_ttc = _round_eur(gross - discount)
    line_ht = _round_eur(line_ttc / rate_factor)
    line_tva = line_ttc - line_ht

    return LineTotals(
        line_ht=line_ht,
        line_tva=line_tva,
        line_ttc=line_ttc,
        tva_rate=rate.quantize(Decimal("0.01")),
    )


def aggregate_totals(lines: list[LineTotals]) -> LineTotals:
    """Agrege plusieurs lignes en un total transaction (multi-taux safe).

    Les totaux finaux sont la somme des lignes individuelles arrondies, pas
    la somme arrondie — coherent avec la facon dont PostgreSQL accumule les
    ``Numeric(10, 2)``.

    Le ``tva_rate`` retourne est celui du panier *si toutes les lignes ont
    le meme taux*, sinon ``Decimal("0.00")`` comme sentinelle "mixte".
    """
    if not lines:
        return LineTotals(
            line_ht=Decimal("0.00"),
            line_tva=Decimal("0.00"),
            line_ttc=Decimal("0.00"),
            tva_rate=DEFAULT_TVA_RATE,
        )
    total_ht = sum((ln.line_ht for ln in lines), Decimal("0.00"))
    total_tva = sum((ln.line_tva for ln in lines), Decimal("0.00"))
    total_ttc = sum((ln.line_ttc for ln in lines), Decimal("0.00"))
    rates = {ln.tva_rate for ln in lines}
    headline_rate = next(iter(rates)) if len(rates) == 1 else Decimal("0.00")
    return LineTotals(
        line_ht=_round_eur(total_ht),
        line_tva=_round_eur(total_tva),
        line_ttc=_round_eur(total_ttc),
        tva_rate=headline_rate,
    )
