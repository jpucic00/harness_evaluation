from health import handle


def test_health():
    assert handle("/health") == (200, "ok")


def test_unknown():
    assert handle("/nope")[0] == 404
