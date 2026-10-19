from sales import totals


def test_totals():
    assert totals() == {"units": 19, "revenue_cents": 66000}
