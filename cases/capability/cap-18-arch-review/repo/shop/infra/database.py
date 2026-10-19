"""Database access and a process-wide, thread-safe cache."""

import sqlite3
import threading


def connect(dsn: str = "shop.db") -> sqlite3.Connection:
    return sqlite3.connect(dsn)


class TTLCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, object] = {}

    def get(self, key: str) -> object | None:
        with self._lock:
            return self._data.get(key)

    def set(self, key: str, value: object) -> None:
        with self._lock:
            self._data[key] = value


cache = TTLCache()
