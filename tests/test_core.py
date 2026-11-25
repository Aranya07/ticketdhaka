from datetime import datetime, timedelta

from app.core.lru_cache import LRUCache
from app.core.rate_limiter import TokenBucketLimiter
from app.core.waitlist import PriorityWaitlist, WaitlistItem


def test_lru_evicts_least_recently_used():
    c = LRUCache(capacity=2, ttl_seconds=60)
    c.put("a", 1)
    c.put("b", 2)
    assert c.get("a") == 1      # a is now the most recent
    c.put("c", 3)               # evicts b
    assert c.get("b") is None
    assert c.keys_in_order() == ["c", "a"]


def test_lru_ttl_and_prefix_delete():
    c = LRUCache(capacity=10, ttl_seconds=60)
    c.put("x", 1, ttl=-1)
    assert c.get("x") is None
    c.put("event:1:seats", 1)
    c.put("event:1:other", 2)
    c.put("event:2:seats", 3)
    c.delete_prefix("event:1:")
    assert len(c) == 1 and c.get("event:2:seats") == 3


def test_lru_hit_rate():
    c = LRUCache(capacity=4)
    c.put("k", 1)
    c.get("k")
    c.get("k")
    c.get("missing")
    assert round(c.hit_rate, 2) == 0.67


def test_token_bucket_burst_then_refill():
    lim = TokenBucketLimiter(capacity=3, refill_per_sec=1.0)
    t = 1000.0
    assert all(lim.allow("ip", now=t)[0] for _ in range(3))
    ok, retry = lim.allow("ip", now=t)
    assert not ok and retry > 0
    assert lim.allow("ip", now=t + 1.0)[0]       # one token refilled
    assert lim.allow("other-ip", now=t)[0]       # buckets are per client


def test_waitlist_priority_then_fifo():
    t = datetime(2026, 1, 1)
    w = PriorityWaitlist([
        WaitlistItem(1, t, user_id=1),
        WaitlistItem(1, t - timedelta(minutes=5), user_id=2),
        WaitlistItem(0, t + timedelta(minutes=5), user_id=3),  # VIP jumps the queue
    ])
    assert [w.pop().user_id for _ in range(3)] == [3, 2, 1]
    assert w.pop() is None
