def add(db, topic: str, event: str, payload: dict) -> None:
    db.insert("outbox", {"topic": topic, "event": event, "payload": payload})
