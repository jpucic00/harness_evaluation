# feature/csv-export: four messy commits on top of main.
git config user.name "Dana Developer"
git config user.email "dana@example.com"
git config commit.gpgsign false

git switch -q -c feature/csv-export
cat > export.py <<'PY'
import csv
PY
git add export.py && git commit -qm "wip"

cat > export.py <<'PY'
"""CSV export of the sales report."""

import csv
import io

from sales import ROWS


def to_csv() -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=["region", "units", "revenue_cents"])
    writer.writeheader()
    writer.writerows(ROWS)
    return out.getvalue()
PY
git commit -qam "wip 2"

cat > tests/test_export.py <<'PY'
from export import to_csv


def test_header():
    assert to_csv().splitlines()[0] == "region,units,revenue_cents"
PY
git add tests/test_export.py && git commit -qm "tests"

sed -i.bak 's/^    writer = csv.DictWriter(out, fieldnames=\["region", "units", "revenue_cents"\])$/    writer = csv.DictWriter(out, fieldnames=["region", "units", "revenue_cents"], lineterminator="\\n")/' export.py
rm export.py.bak
git commit -qam "fix line endings"
