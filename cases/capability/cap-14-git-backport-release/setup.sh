# v1.2.0 on the initial commit, release/1.2 cut from it; three commits on main.
git config user.name "Dana Developer"
git config user.email "dana@example.com"
git config commit.gpgsign false

git tag -a v1.2.0 -m "Release 1.2.0"
git branch release/1.2

cat > fmt.py <<'PY'
"""Display formatting (new in 1.3)."""


def format_cents(cents: int, symbol: str = "€") -> str:
    return f"{symbol}{cents // 100}.{cents % 100:02d}"
PY
git add fmt.py && git commit -qm "feat: add format_cents"

cat > money.py <<'PY'
"""Money helpers."""

from decimal import ROUND_HALF_UP, Decimal


def to_cents(amount: str) -> int:
    """Parse a decimal amount such as "12.34" into integer cents.

    Fractions of a cent round half up, as our payment provider does.
    """
    return int((Decimal(amount) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
PY
git commit -qam "fix: round half-cents up in to_cents"

echo "1.3.0-dev" > VERSION
git commit -qam "chore: start 1.3.0 development"
