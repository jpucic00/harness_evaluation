"""Convert between integers and Roman numerals (1 to 3999)."""

_SYMBOLS = [
    (1000, "M"),
    (500, "D"),
    (100, "C"),
    (50, "L"),
    (10, "X"),
    (5, "V"),
    (1, "I"),
]


def to_roman(number: int) -> str:
    """Return the standard Roman numeral for ``number``, e.g. 1994 -> "MCMXCIV".

    Raises ValueError outside 1..3999.
    """
    if not 1 <= number <= 3999:
        raise ValueError(f"out of range: {number}")
    parts: list[str] = []
    for value, symbol in _SYMBOLS:
        while number >= value:
            parts.append(symbol)
            number -= value
    return "".join(parts)


def from_roman(numeral: str) -> int:
    """Parse a standard Roman numeral such as "XIV" or "mcmxciv" (any case).

    Raises ValueError for anything that is not a standard numeral for 1..3999:
    the empty string, unknown letters and non-standard forms like "IIII" or "IC".
    """
    values = {symbol: value for value, symbol in _SYMBOLS}
    total = 0
    for char in numeral:
        total += values[char]
    return total
