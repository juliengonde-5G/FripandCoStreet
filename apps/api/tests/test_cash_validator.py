# Extrait de l'application source (apps/api/tests/test_cash_payment_validator.py), copie
# fidele — le module n'a pas change de contrat public.
from decimal import Decimal

from app.services.cash_payment_validator import (
    CASH_CAP_RESIDENT_EUR,
    CASH_CAP_TOURIST_EUR,
    cap_for,
    sum_cash_payments,
    validate,
)


class _Payment:
    def __init__(self, method, amount):
        self.method = method
        self.amount = amount


def test_sum_cash_payments_ignores_card():
    payments = [_Payment("cash", "100.00"), _Payment("card", "500.00")]
    assert sum_cash_payments(payments) == Decimal("100.00")


def test_sum_cash_payments_accepts_especes_alias():
    payments = [_Payment("especes", "50.00")]
    assert sum_cash_payments(payments) == Decimal("50.00")


def test_validate_under_cap_ok():
    result = validate([_Payment("cash", "999.99")])
    assert result.over_cap is False
    assert result.reason is None


def test_validate_over_cap_resident():
    result = validate([_Payment("cash", "1000.01")])
    assert result.over_cap is True
    assert "1000" in result.reason
    assert result.cap_eur == CASH_CAP_RESIDENT_EUR


def test_validate_exactly_at_cap_is_ok():
    result = validate([_Payment("cash", "1000.00")])
    assert result.over_cap is False


def test_cap_for_tourist():
    assert cap_for(is_tourist=True) == CASH_CAP_TOURIST_EUR
    assert cap_for(is_tourist=False) == CASH_CAP_RESIDENT_EUR


def test_mixed_cash_and_card_only_sums_cash():
    result = validate([_Payment("cash", "900.00"), _Payment("card", "5000.00")])
    assert result.cash_total_eur == Decimal("900.00")
    assert result.over_cap is False
