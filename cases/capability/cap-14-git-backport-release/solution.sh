fix=$(git log --format=%H -1 --grep='^fix: round half-cents up')
git switch -q release/1.2
git cherry-pick -x "$fix" >/dev/null
echo "1.2.1" > VERSION
git commit -qam "chore: release 1.2.1"
git tag -a v1.2.1 -m "Release 1.2.1"
git switch -q main
