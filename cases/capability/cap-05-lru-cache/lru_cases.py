"""Reference cases for cap-05-lru-cache (the visible tests plus hidden ones).

Each stateful case returns what the original test asserted on, as a tuple.
"""

from agenteval.suites.cases import Case, Raises

SETUP = """
from lru import LRUCache

def make(capacity, *keys):
    cache = LRUCache(capacity)
    for index, key in enumerate(keys):
        cache.put(key, index)
    return cache
"""

CASES = [
    # visible tests
    Case(
        "least_recently_inserted_evicted",
        "cache = make(2, 'a', 'b')\ncache.put('c', 3)\nresult = ('a' in cache, cache.get('c'))",
        (False, 3),
    ),
    Case(
        "get_marks_recently_used",
        "cache = LRUCache(2)\n"
        "cache.put('a', 1)\n"
        "cache.put('b', 2)\n"
        "got = cache.get('a')\n"
        "cache.put('c', 3)\n"
        "result = (got, 'a' in cache, 'b' in cache)",
        (1, True, False),
    ),
    # hidden tests
    Case(
        "update_does_not_evict",
        "cache = make(2, 'a', 'b')\n"
        "cache.put('b', 10)\n"
        "result = (len(cache), 'a' in cache, cache.get('b'))",
        (2, True, 10),
    ),
    Case(
        "update_refreshes",
        "cache = make(3, 'a', 'b', 'c')\n"
        "cache.put('b', 10)\n"
        "cache.put('d', 4)\n"
        "result = cache.keys()",
        ["c", "b", "d"],
    ),
    Case(
        "membership_does_not_refresh",
        "cache = make(2, 'a', 'b')\n"
        "found = 'a' in cache\n"
        "cache.put('c', 3)\n"
        "result = (found, cache.keys())",
        (True, ["b", "c"]),
    ),
    Case(
        "miss_does_not_change_order",
        "cache = make(3, 'a', 'b', 'c')\nresult = (cache.get('zzz', -1), cache.keys())",
        (-1, ["a", "b", "c"]),
    ),
    Case(
        "order_after_mixed_operations",
        "cache = make(3, 'a', 'b', 'c')\n"
        "cache.get('a')\n"
        "cache.put('b', 5)\n"
        "cache.put('d', 4)\n"
        "result = cache.keys()",
        ["a", "b", "d"],
    ),
    Case(
        "hit_and_miss_counters",
        "cache = make(2, 'a')\n"
        "cache.get('a')\n"
        "cache.get('a')\n"
        "cache.get('b')\n"
        "result = (cache.hits, cache.misses)",
        (2, 1),
    ),
    Case(
        "capacity_one",
        "cache = make(1, 'a')\n"
        "cache.put('a', 7)\n"
        "got = cache.get('a')\n"
        "cache.put('b', 8)\n"
        "result = (got, cache.keys())",
        (7, ["b"]),
    ),
    Case(
        "resize_evicts_lru",
        "cache = make(4, 'a', 'b', 'c', 'd')\n"
        "cache.get('b')\n"
        "cache.resize(2)\n"
        "result = (cache.keys(), cache.capacity)",
        (["d", "b"], 2),
    ),
    Case(
        "resize_up_keeps_everything",
        "cache = make(2, 'a', 'b')\n"
        "cache.resize(4)\n"
        "cache.put('c', 3)\n"
        "cache.put('d', 4)\n"
        "result = cache.keys()",
        ["a", "b", "c", "d"],
    ),
    Case("invalid_capacity", "LRUCache(0)", Raises("ValueError")),
    Case("invalid_resize", "make(2, 'a').resize(0)", Raises("ValueError")),
]
