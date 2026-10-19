from src.stats import mean, spread


def test_mean():
    assert mean([1.0, 2.0, 3.0]) == 2.0


def test_mean_empty():
    assert mean([]) == 0.0


def test_spread():
    assert spread([4.0, 1.0, 9.0]) == 8.0
    assert spread([]) == 0.0
