from roman import from_roman, to_roman


def test_additive_numeral() -> None:
    assert to_roman(8) == "VIII"


def test_subtractive_numeral() -> None:
    assert to_roman(4) == "IV"


def test_parse() -> None:
    assert from_roman("XIV") == 14
