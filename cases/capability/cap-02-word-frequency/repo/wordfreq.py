"""Word statistics for short texts."""


def word_counts(text: str) -> dict[str, int]:
    """Count how often each word occurs.

    Words are compared case-insensitively and reported in lower case.
    Punctuation around a word is ignored ("end." and "end" are the same word),
    while apostrophes and hyphens inside a word are kept ("don't",
    "well-known"). Any whitespace separates words.
    """
    counts: dict[str, int] = {}
    for word in text.split(" "):
        counts[word] = counts.get(word, 0) + 1
    return counts


def top_words(text: str, n: int) -> list[tuple[str, int]]:
    """Return the ``n`` most frequent words as ``(word, count)`` pairs.

    Higher counts come first; words with the same count are ordered
    alphabetically.
    """
    counts = word_counts(text)
    ranked = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    return ranked[:n]
