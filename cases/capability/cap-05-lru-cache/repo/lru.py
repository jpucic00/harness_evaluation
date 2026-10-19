"""A small least-recently-used cache."""


class LRUCache[K, V]:
    """Mapping with a fixed capacity that evicts the least recently used key.

    Both ``get`` (on a hit) and ``put`` count as a use of the key. ``key in
    cache``, ``len(cache)`` and ``keys()`` do not change the order. ``hits``
    and ``misses`` count the results of ``get``.
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.capacity = capacity
        self.hits = 0
        self.misses = 0
        self._data: dict[K, V] = {}

    def get(self, key: K, default: V | None = None) -> V | None:
        if key in self._data:
            self.hits += 1
            return self._data[key]
        self.misses += 1
        return default

    def put(self, key: K, value: V) -> None:
        if len(self._data) >= self.capacity:
            oldest = next(iter(self._data))
            del self._data[oldest]
        self._data[key] = value

    def resize(self, capacity: int) -> None:
        """Change the capacity, evicting least recently used keys if needed."""
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.capacity = capacity
        while len(self._data) > capacity:
            self._data.popitem()

    def keys(self) -> list[K]:
        """Keys from least to most recently used."""
        return list(self._data)

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)
