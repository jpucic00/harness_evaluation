git switch -q -c feature/rate-limiter
git add ratelimit.py tests/test_ratelimit.py
git commit -q -m "feat: add fixed-window rate limiter"
git push -q -u origin feature/rate-limiter
