# Code review format

Write `findings.json` as a JSON list, one object per defect:

```json
[
  {"file": "src/main/java/com/example/orders/Example.java", "line": 42, "category": "correctness", "note": "why"}
]
```

- `file`: path relative to the repository root.
- `line`: line number in the file as it is on the PR branch (the working tree).
- `category`: one of the ids below.

| Category | Use for |
| --- | --- |
| `security` | Injection, unsafe input handling, secrets |
| `correctness` | Wrong results, crashes, broken contracts |
| `performance` | Avoidable remote calls or queries, quadratic work |
| `error-handling` | Errors that are lost, hidden or mishandled |
| `resource-leak` | Connections, streams or handles that are never closed |
| `concurrency` | Unsafe shared state between threads |

Report each defect once.
