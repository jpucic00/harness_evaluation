# feature/bulk-discount and main both change total() and its tests.
git config user.name "Dana Developer"
git config user.email "dana@example.com"
git config commit.gpgsign false
mkdir -p tests
cat > tests/test_pricing.py <<'PY'
from pricing import total


def test_single_unit():
    assert total(250, 1) >= 250
PY
git add -A && git commit -q -m "test: add pricing tests"

git switch -q -c feature/bulk-discount
cat > pricing.py <<'PY'
"""Order pricing. All amounts are integer cents."""

BULK_THRESHOLD = 10
BULK_DISCOUNT_PERCENT = 5


def total(unit_cents: int, qty: int) -> int:
    """Total for qty units. Orders of BULK_THRESHOLD or more units get
    BULK_DISCOUNT_PERCENT off (rounded down to whole cents)."""
    subtotal = unit_cents * qty
    if qty >= BULK_THRESHOLD:
        subtotal -= subtotal * BULK_DISCOUNT_PERCENT // 100
    return subtotal
PY
cat >> tests/test_pricing.py <<'PY'


def test_bulk_discount():
    assert total(1000, 10) == 9500
PY
git add -A && git commit -q -m "feat: bulk discount for 10+ units"

git switch -q main
cat > pricing.py <<'PY'
"""Order pricing. All amounts are integer cents."""

VAT_PERCENT = 25


def total(unit_cents: int, qty: int) -> int:
    """Total for qty units including VAT. VAT is charged on the amount after any
    discounts and rounded half up to whole cents."""
    subtotal = unit_cents * qty
    vat = (subtotal * VAT_PERCENT + 50) // 100
    return subtotal + vat
PY
cat >> tests/test_pricing.py <<'PY'


def test_vat():
    assert total(1000, 1) == 1250
PY
git add -A && git commit -q -m "feat: charge 25% VAT"

git switch -q feature/bulk-discount
