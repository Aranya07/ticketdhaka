"""A hand-written LRU cache with TTL.

O(1) get / put / delete using a hash map + doubly linked list:
- the dict maps key -> node, so lookup is O(1)
- the linked list keeps recency order; the head is most recent, the tail is least recent
- on overflow we evict the tail in O(1)

Thread-safe via a single lock (FastAPI runs sync endpoints in a threadpool).
"""
from __future__ import annotations

import threading
import time
from typing import Any


class _Node:
    __slots__ = ("key", "value", "expires_at", "prev", "next")

    def __init__(self, key: str | None, value: Any, expires_at: float) -> None:
        self.key = key
        self.value = value
        self.expires_at = expires_at
        self.prev: _Node | None = None
        self.next: _Node | None = None


class LRUCache:
    def __init__(self, capacity: int = 128, ttl_seconds: float = 30.0) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self.ttl = ttl_seconds
        self._map: dict[str, _Node] = {}
        # Sentinel nodes remove all the "is this the first/last node?" edge cases
        self._head = _Node(None, None, 0)
        self._tail = _Node(None, None, 0)
        self._head.next = self._tail
        self._tail.prev = self._head
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    # ---- linked list helpers -------------------------------------------------
    def _unlink(self, node: _Node) -> None:
        node.prev.next = node.next  # type: ignore[union-attr]
        node.next.prev = node.prev  # type: ignore[union-attr]

    def _push_front(self, node: _Node) -> None:
        node.prev = self._head
        node.next = self._head.next
        self._head.next.prev = node  # type: ignore[union-attr]
        self._head.next = node

    # ---- public API ----------------------------------------------------------
    def get(self, key: str) -> Any | None:
        with self._lock:
            node = self._map.get(key)
            if node is None:
                self.misses += 1
                return None
            if node.expires_at < time.monotonic():
                self._unlink(node)
                del self._map[key]
                self.misses += 1
                return None
            self._unlink(node)
            self._push_front(node)
            self.hits += 1
            return node.value

    def put(self, key: str, value: Any, ttl: float | None = None) -> None:
        expires = time.monotonic() + (self.ttl if ttl is None else ttl)
        with self._lock:
            node = self._map.get(key)
            if node is not None:
                node.value = value
                node.expires_at = expires
                self._unlink(node)
                self._push_front(node)
                return
            node = _Node(key, value, expires)
            self._map[key] = node
            self._push_front(node)
            if len(self._map) > self.capacity:
                lru = self._tail.prev
                self._unlink(lru)  # type: ignore[arg-type]
                del self._map[lru.key]  # type: ignore[union-attr, arg-type]

    def delete(self, key: str) -> None:
        with self._lock:
            node = self._map.pop(key, None)
            if node is not None:
                self._unlink(node)

    def delete_prefix(self, prefix: str) -> None:
        with self._lock:
            for key in [k for k in self._map if k.startswith(prefix)]:
                self._unlink(self._map.pop(key))

    def keys_in_order(self) -> list[str]:
        """Most recent first. Handy for tests and debugging."""
        out, node = [], self._head.next
        while node is not self._tail:
            out.append(node.key)  # type: ignore[union-attr]
            node = node.next  # type: ignore[union-attr]
        return out

    def __len__(self) -> int:
        return len(self._map)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0
