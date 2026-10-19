"""Reference cases for cap-03-roman-numerals (the visible tests plus hidden ones)."""

from agenteval.suites.cases import Case, Raises

SETUP = "from roman import from_roman, to_roman"

KNOWN = [
    (1, "I"),
    (4, "IV"),
    (9, "IX"),
    (14, "XIV"),
    (40, "XL"),
    (90, "XC"),
    (400, "CD"),
    (944, "CMXLIV"),
    (1994, "MCMXCIV"),
    (2024, "MMXXIV"),
    (3999, "MMMCMXCIX"),
]

CASES = [
    # visible tests
    Case("to_roman_additive", "to_roman(8)", "VIII"),
    Case("to_roman_subtractive", "to_roman(4)", "IV"),
    Case("from_roman_parse", "from_roman('XIV')", 14),
    # hidden tests
    *(Case(f"to_roman_{number}", f"to_roman({number})", numeral) for number, numeral in KNOWN),
    *(
        Case(f"from_roman_{numeral}", f"from_roman({numeral!r})", number)
        for number, numeral in KNOWN
    ),
    Case(
        "round_trip",
        "[from_roman(to_roman(n)) for n in range(1, 4000)]",
        list(range(1, 4000)),
    ),
    Case("lower_case", "from_roman('mcmxciv')", 1994),
    *(
        Case(f"malformed_{bad or 'empty'}", f"from_roman({bad!r})", Raises("ValueError"))
        for bad in ["", "ABC", "IIII", "IC", "VX", "IIV", "MMMM", "XM"]
    ),
    *(
        Case(f"out_of_range_{number}", f"to_roman({number})", Raises("ValueError"))
        for number in [0, -5, 4000]
    ),
]
