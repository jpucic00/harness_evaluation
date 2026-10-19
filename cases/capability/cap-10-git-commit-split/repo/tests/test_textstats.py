from textstats import line_count, word_count


def test_word_count():
    assert word_count("one two three") == 3


def test_line_count():
    assert line_count("a\nb\n") == 2
