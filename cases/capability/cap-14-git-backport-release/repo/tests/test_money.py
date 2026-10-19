from money import to_cents


def test_whole_cents():
    assert to_cents("12.34") == 1234
