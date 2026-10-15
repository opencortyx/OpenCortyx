"""Tiny in-process TTL + LRU cache — used for query embeddings and assembled context packages.

In-process (per worker), thread-safe, zero deps. For a SHARED cache across many workers, back
this with Redis (settings.REDIS_URL is already available) — same get/set interface.
"""
import time
import threading
from collections import OrderedDict
from typing import Any, Optional


class TTLCache:
    def __init__(self, maxsize: int = 1024, ttl: int = 300):
        self.maxsize = max(1, int(maxsize))
        self.ttl = int(ttl)
        self._d: "OrderedDict[str, tuple]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        now = time.time()
        with self._lock:
            entry = self._d.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at and expires_at < now:
                self._d.pop(key, None)
                return None
            self._d.move_to_end(key)          # mark recently used
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            expires_at = (time.time() + self.ttl) if self.ttl else 0
            self._d[key] = (expires_at, value)
            self._d.move_to_end(key)
            while len(self._d) > self.maxsize:
                self._d.popitem(last=False)   # evict least-recently-used

    def clear(self) -> None:
        with self._lock:
            self._d.clear()
