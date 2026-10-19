bad=$(git log --format=%H --grep='^perf: precompile the slug pattern$')
git revert --no-edit "$bad" >/dev/null
