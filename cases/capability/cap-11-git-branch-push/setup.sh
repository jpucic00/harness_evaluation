# A local bare "origin" (inside .git so it stays writable in sandboxed harnesses),
# main pushed to it, then uncommitted work on main.
git config user.name "Dana Developer"
git config user.email "dana@example.com"
git config commit.gpgsign false

git init -q --bare .git/origin.git
git remote add origin "$PWD/.git/origin.git"
git push -q -u origin main

cat > ratelimit.py <<'PY'
"""Fixed-window rate limiter."""


class RateLimiter:
    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window = window_seconds
        self._counts: dict[tuple[str, int], int] = {}

    def allow(self, client: str, now: float) -> bool:
        key = (client, int(now // self.window))
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key] <= self.limit
PY
cat > tests/test_ratelimit.py <<'PY'
from ratelimit import RateLimiter


def test_limit_per_window():
    limiter = RateLimiter(limit=2, window_seconds=60)
    assert [limiter.allow("a", t) for t in (0, 1, 2)] == [True, True, False]
    assert limiter.allow("a", 61)
PY
