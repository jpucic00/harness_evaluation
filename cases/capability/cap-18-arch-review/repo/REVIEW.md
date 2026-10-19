# Review format

Write `findings.json` as a JSON list, one object per violation:

```json
[
  {"file": "shop/api/example_api.py", "line": 3, "rule": "layer-violation", "note": "why"}
]
```

- `file`: path relative to the repository root.
- `line`: the offending import or statement.
- `rule`: one of the ids below.

| Rule id | Architecture rule |
| --- | --- |
| `layer-violation` | 1. Layering |
| `domain-impurity` | 2. Pure domain |
| `dependency-cycle` | 3. No cycles (report it once, at an import that is part of the cycle) |
| `global-mutable-state` | 4. No shared mutable state in services |
| `direct-external-call` | 5. External calls through adapters |
| `n-plus-one-query` | 6. No per-row queries |

Report each violation once. Code that follows the rules must not be reported.
