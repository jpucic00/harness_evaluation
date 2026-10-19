"""Reference cases for cap-02-word-frequency (the visible tests plus hidden ones)."""

from agenteval.suites.cases import Case

SETUP = "from wordfreq import top_words, word_counts"

CASES = [
    # visible tests
    Case(
        "counts_ignore_case",
        "word_counts('The cat and the hat')",
        {"the": 2, "cat": 1, "and": 1, "hat": 1},
    ),
    Case("most_common_word", "top_words('b a b', 1)", [("b", 2)]),
    # hidden tests
    Case(
        "any_whitespace",
        "word_counts('one  two\\nthree\\tone\\n')",
        {"one": 2, "two": 1, "three": 1},
    ),
    Case(
        "surrounding_punctuation",
        "word_counts('Hello, world! \"Hello\" (world).')",
        {"hello": 2, "world": 2},
    ),
    Case(
        "inner_apostrophes_and_hyphens",
        "word_counts(\"Don't stop: well-known, don't.\")",
        {"don't": 2, "stop": 1, "well-known": 1},
    ),
    Case("punctuation_only", "word_counts('-- ... !')", {}),
    Case("empty_text", "word_counts('')", {}),
    Case(
        "ties_alphabetical",
        "top_words('pear apple pear apple fig', 3)",
        [("apple", 2), ("pear", 2), ("fig", 1)],
    ),
    Case("n_larger_than_vocabulary", "top_words('b a', 5)", [("a", 1), ("b", 1)]),
    Case("n_zero", "top_words('a b', 0)", []),
    Case("top_words_normalised", "top_words('Go. go, GO! stop', 2)", [("go", 3), ("stop", 1)]),
]
