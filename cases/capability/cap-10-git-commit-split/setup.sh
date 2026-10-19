# Uncommitted work: a bug fix with its test, a README typo fix, and a scratch file.
git config user.name "Dana Developer"
git config user.email "dana@example.com"
git config commit.gpgsign false

sed -i.bak 's/return len(text.split(" "))/return len(text.split())/' textstats.py && rm textstats.py.bak
cat > tests/test_word_count.py <<'PY'
from textstats import word_count


def test_repeated_whitespace():
    assert word_count("a  b\n c") == 3


def test_empty():
    assert word_count("") == 0
PY
sed -i.bak 's/Counts wrods/Counts words/' README.md && rm README.md.bak
printf 'DEBUG word_count("a  b") -> 3\n' > scratch.log
