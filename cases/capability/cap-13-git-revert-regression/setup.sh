# Four commits on main; the second one breaks accented input.
git config user.name "Dana Developer"
git config user.email "dana@example.com"
git config commit.gpgsign false

cat > slug.py <<'PY'
"""URL slugs."""

import re
import unicodedata


def slugify(text: str, separator: str = "-") -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", separator, text.lower())
    return text.strip(separator)
PY
git commit -qam "feat: configurable slug separator"

cat > slug.py <<'PY'
"""URL slugs."""

import re

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(text: str, separator: str = "-") -> str:
    text = _NON_ALNUM.sub(separator, text.lower())
    return text.strip(separator)
PY
git commit -qam "perf: precompile the slug pattern"

cat > titles.py <<'PY'
"""Title helpers."""

SMALL_WORDS = {"a", "an", "and", "of", "the", "to"}


def titlecase(text: str) -> str:
    words = text.lower().split()
    return " ".join(
        w if i and w in SMALL_WORDS else w.capitalize() for i, w in enumerate(words)
    )
PY
git add titles.py && git commit -qm "feat: add titlecase helper"

cat > CHANGELOG.md <<'MD'
# Changelog

## Unreleased

- Configurable slug separator.
- `titlecase()` helper.
MD
git add CHANGELOG.md && git commit -qm "docs: start a changelog"
