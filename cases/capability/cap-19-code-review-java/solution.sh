# A complete review (the reference findings).
cat > findings.json <<'JSON'
[
  {"file": "src/main/java/com/example/orders/OrderRepository.java", "line": 54, "category": "security", "note": "name is concatenated into the SQL: injection."},
  {"file": "src/main/java/com/example/orders/OrderRepository.java", "line": 49, "category": "resource-leak", "note": "Connection, Statement and ResultSet are never closed."},
  {"file": "src/main/java/com/example/orders/OrderRepository.java", "line": 58, "category": "error-handling", "note": "SQLException swallowed; caller gets an empty list."},
  {"file": "src/main/java/com/example/orders/OrderService.java", "line": 26, "category": "performance", "note": "One customer-service call per order; use fetchAll."},
  {"file": "src/main/java/com/example/orders/OrderService.java", "line": 37, "category": "correctness", "note": "i <= to reads one past the page and throws on the last page."},
  {"file": "src/main/java/com/example/orders/OrderService.java", "line": 47, "category": "correctness", "note": "String compared with ==."},
  {"file": "src/main/java/com/example/orders/OrderKey.java", "line": 14, "category": "correctness", "note": "equals without hashCode breaks HashMap lookups."},
  {"file": "src/main/java/com/example/orders/ReportFormatter.java", "line": 9, "category": "concurrency", "note": "Static SimpleDateFormat is not thread-safe."}
]
JSON
