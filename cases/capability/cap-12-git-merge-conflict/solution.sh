git merge -q main >/dev/null 2>&1 || true
cat > pricing.py <<'PY'
"""Order pricing. All amounts are integer cents."""

BULK_THRESHOLD = 10
BULK_DISCOUNT_PERCENT = 5
VAT_PERCENT = 25


def total(unit_cents: int, qty: int) -> int:
    """Total for qty units including VAT. Orders of BULK_THRESHOLD or more units get
    BULK_DISCOUNT_PERCENT off (rounded down to whole cents). VAT is charged on the
    amount after any discounts and rounded half up to whole cents."""
    subtotal = unit_cents * qty
    if qty >= BULK_THRESHOLD:
        subtotal -= subtotal * BULK_DISCOUNT_PERCENT // 100
    vat = (subtotal * VAT_PERCENT + 50) // 100
    return subtotal + vat
PY
cat > tests/test_pricing.py <<'PY'
from pricing import total


def test_single_unit():
    assert total(250, 1) >= 250


def test_bulk_discount():
    assert total(1000, 10) == 11875


def test_vat():
    assert total(1000, 1) == 1250
PY
git add -A
git commit -q --no-edit
