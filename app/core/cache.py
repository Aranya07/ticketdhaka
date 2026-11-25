"""Cache layer, using the Strategy pattern: the service code talks to the `Cache`
interface and doesn't care whether it's backed by our own LRU or by Redis."""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any

from app.core.lru_cache import LRUCache

log = logging.getLogger("ticketdhaka.cache")


class Cache(ABC):
    @abstractmethod
    def get(self, key: str) -> Any | None: ...

    @abstractmethod
    def set(self, key: str, value: Any) -> None: ...

    @abstractmethod
    def invalidate_prefix(self, prefix: str) -> None: ...

    @abstractmethod
    def stats(self) -> dict: ...


class InMemoryLRUCache(Cache):
    def __init__(self, capacity: int, ttl: float) -> None:
        self._lru = LRUCache(capacity, ttl)

    def get(self, key: str) -> Any | None:
        return self._lru.get(key)

    def set(self, key: str, value: Any) -> None:
        self._lru.put(key, value)

    def invalidate_prefix(self, prefix: str) -> None:
        self._lru.delete_prefix(prefix)

    def stats(self) -> dict:
        return {
            "backend": "lru",
            "size": len(self._lru),
            "hits": self._lru.hits,
            "misses": self._lru.misses,
            "hit_rate": round(self._lru.hit_rate, 3),
        }


class RedisCache(Cache):
    """L1 = in-process LRU (no network hop), L2 = Redis (shared across instances)."""

    def __init__(self, url: str, capacity: int, ttl: int) -> None:
        import redis

        self._r = redis.Redis.from_url(url, decode_responses=True, socket_timeout=0.5)
        self._l1 = LRUCache(capacity, ttl / 2)
        self._ttl = ttl
        self.hits = self.misses = 0

    def get(self, key: str) -> Any | None:
        v = self._l1.get(key)
        if v is not None:
            self.hits += 1
            return v
        try:
            raw = self._r.get(key)
        except Exception as e:  # Redis down -> degrade gracefully to a DB read
            log.warning("redis get failed: %s", e)
            raw = None
        if raw is None:
            self.misses += 1
            return None
        self.hits += 1
        value = json.loads(raw)
        self._l1.put(key, value)
        return value

    def set(self, key: str, value: Any) -> None:
        self._l1.put(key, value)
        try:
            self._r.set(key, json.dumps(value, default=str), ex=self._ttl)
        except Exception as e:
            log.warning("redis set failed: %s", e)

    def invalidate_prefix(self, prefix: str) -> None:
        self._l1.delete_prefix(prefix)
        try:
            for k in self._r.scan_iter(match=f"{prefix}*", count=200):
                self._r.delete(k)
        except Exception as e:
            log.warning("redis invalidate failed: %s", e)

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "backend": "redis+lru",
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
        }


class NullCache(Cache):
    """Caching disabled: used for before/after benchmarks."""

    def get(self, key: str) -> Any | None:
        return None

    def set(self, key: str, value: Any) -> None:
        pass

    def invalidate_prefix(self, prefix: str) -> None:
        pass

    def stats(self) -> dict:
        return {"backend": "none"}


def build_cache(redis_url: str | None, capacity: int, ttl: int, enabled: bool = True) -> Cache:
    if not enabled:
        return NullCache()
    if redis_url:
        try:
            c = RedisCache(redis_url, capacity, ttl)
            c._r.ping()
            log.info("cache: redis + lru")
            return c
        except Exception as e:
            log.warning("redis unavailable (%s); falling back to in-process LRU", e)
    return InMemoryLRUCache(capacity, ttl)
