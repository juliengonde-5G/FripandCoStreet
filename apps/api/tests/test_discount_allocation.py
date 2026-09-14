# Nouveau test (§4.1 point 4 + §8 — cas d'arrondi explicitement exiges par
# le contrat) : ventilation de la remise globale au prorata des lignes, le
# reste allant sur la DERNIERE ligne, de sorte que Sigma(line_total) =
# brut - discount_amount exactement.
from app.services.pos import PosService


def test_allocate_discount_three_equal_lines_10_percent():
    gross_cents = [1000, 1000, 1000]  # 3 x 10.00 EUR
    shares = PosService._allocate_discount(gross_cents, 300)  # 10% de 30.00
    assert shares == [100, 100, 100]
    assert sum(shares) == 300


def test_allocate_discount_one_cent():
    gross_cents = [1000, 1000, 1000]
    shares = PosService._allocate_discount(gross_cents, 1)
    # Le reste d'arrondi va sur la derniere ligne.
    assert shares == [0, 0, 1]
    assert sum(shares) == 1


def test_allocate_discount_equals_gross_total():
    gross_cents = [1000, 1000, 1000]
    shares = PosService._allocate_discount(gross_cents, 3000)
    assert shares == gross_cents
    assert sum(shares) == 3000


def test_allocate_discount_uneven_lines():
    gross_cents = [333, 667, 1000]  # totaux impairs
    shares = PosService._allocate_discount(gross_cents, 100)
    assert sum(shares) == 100
    # Chaque part (sauf la derniere) est <= sa quote-part proportionnelle.
    assert shares[0] <= round(100 * 333 / 2000) + 1
    assert shares[1] <= round(100 * 667 / 2000) + 1


def test_allocate_discount_zero_is_all_zero_shares():
    gross_cents = [1000, 2000]
    shares = PosService._allocate_discount(gross_cents, 0)
    assert shares == [0, 0]


def test_resolve_discount_percent():
    from decimal import Decimal

    class _Discount:
        type = "percent"
        value = Decimal("10")

    discount_type, value, cents = PosService._resolve_discount(_Discount(), 3000)
    assert cents == 300
    assert value == Decimal("10")


def test_resolve_discount_amount():
    from decimal import Decimal

    class _Discount:
        type = "amount"
        value = Decimal("5.00")

    discount_type, value, cents = PosService._resolve_discount(_Discount(), 3000)
    assert cents == 500


def test_resolve_discount_none_returns_zero():
    discount_type, value, cents = PosService._resolve_discount(None, 3000)
    assert discount_type is None
    assert value is None
    assert cents == 0
