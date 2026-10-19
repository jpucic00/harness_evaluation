from wordfreq import top_words, word_counts


def test_counts_ignore_case() -> None:
    assert word_counts("The cat and the hat") == {"the": 2, "cat": 1, "and": 1, "hat": 1}


def test_most_common_word() -> None:
    assert top_words("b a b", 1) == [("b", 2)]
