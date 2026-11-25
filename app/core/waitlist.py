"""Priority waitlist built on a binary min-heap (heapq).

Ordering: lower priority number first (0 = VIP, 1 = regular), then earlier
join time (FIFO within a tier). push and pop are O(log n); peek is O(1).
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(order=True)
class WaitlistItem:
    priority: int
    joined_at: datetime
    user_id: int = field(compare=False)
    entry_id: int = field(compare=False, default=0)


class PriorityWaitlist:
    def __init__(self, items: list[WaitlistItem] | None = None) -> None:
        self._heap: list[WaitlistItem] = list(items or [])
        heapq.heapify(self._heap)  # O(n)

    def push(self, item: WaitlistItem) -> None:
        heapq.heappush(self._heap, item)

    def pop(self) -> WaitlistItem | None:
        return heapq.heappop(self._heap) if self._heap else None

    def peek(self) -> WaitlistItem | None:
        return self._heap[0] if self._heap else None

    def __len__(self) -> int:
        return len(self._heap)
