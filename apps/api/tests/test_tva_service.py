# Extrait de Vintiz (apps/api/tests/test_tva_service.py), adapte a la
# signature PR2 (`discount_amount` en euros, pas `discount_percent`).
from decimal import Decimal

import pytest

from app.services.tva_service import (
    DEFAULT_TVA_RATE,
    aggregate_totals,
    compute_line_totals,
    is_supported_rate,
)


def test_compute_line_totals_no_discount_20_percent():
    totals = compute_line_totals(Decimal("25.00"), 1, Decimal("0"), Decimal("20.00"))
    assert totals.line_ttc == Decimal("25.00")
    assert totals.line_ht == Decimal("20.83")
    assert totals.line_tva == Decimal("4.17")
    assert totals.line_ht + totals.line_tva == totals.line_ttc


def test_compute_line_totals_with_discount():
    totals = compute_line_totals(Decimal("10.00"), 3, Decimal("3.00"), Decimal("20.00"))
    # 3x10.00 - 3.00 = 27.00 TTC
    assert totals.line_ttc == Decimal("27.00")
    assert totals.line_ht + totals.line_tva == totals.line_ttc


def test_compute_line_totals_zero_rate():
    totals = compute_line_totals(Decimal("10.00"), 1, Decimal("0"), Decimal("0.00"))
    assert totals.line_ht == Decimal("10.00")
    assert totals.line_tva == Decimal("0.00")


def test_compute_line_totals_rejects_bad_quantity():
    with pytest.raises(ValueError):
        compute_line_totals(Decimal("10.00"), 0)


def test_compute_line_totals_rejects_discount_over_gross():
    with pytest.raises(ValueError):
        compute_line_totals(Decimal("10.00"), 1, Decimal("10.01"))


def test_compute_line_totals_rejects_negative_price():
    with pytest.raises(ValueError):
        compute_line_totals(Decimal("-1.00"), 1)


def test_aggregate_totals_sums_lines():
    lines = [
        compute_line_totals(Decimal("10.00"), 1, Decimal("0"), Decimal("20.00")),
        compute_line_totals(Decimal("15.00"), 1, Decimal("0"), Decimal("20.00")),
    ]
    agg = aggregate_totals(lines)
    assert agg.line_ttc == Decimal("25.00")
    assert agg.tva_rate == Decimal("20.00")


def test_aggregate_totals_mixed_rates_sentinel():
    lines = [
        compute_line_totals(Decimal("10.00"), 1, Decimal("0"), Decimal("20.00")),
        compute_line_totals(Decimal("10.00"), 1, Decimal("0"), Decimal("5.50")),
    ]
    agg = aggregate_totals(lines)
    assert agg.tva_rate == Decimal("0.00")  # sentinelle "mixte"


def test_aggregate_totals_empty():
    agg = aggregate_totals([])
    assert agg.line_ttc == Decimal("0.00")
    assert agg.tva_rate == DEFAULT_TVA_RATE


@pytest.mark.parametrize(
    "rate,expected",
    [
        ("20", True),
        ("20.00", True),
        (Decimal("5.50"), True),
        ("2.10", True),
        ("0", True),
        ("19.6", False),
        ("abc", False),
    ],
)
def test_is_supported_rate(rate, expected):
    assert is_supported_rate(rate) is expected
