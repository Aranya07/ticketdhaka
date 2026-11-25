"""Measures p50/p95 latency of the hot read endpoints under concurrent load.

Usage:
  python bench/bench_latency.py http://localhost:8000 --requests 2000 --concurrency 20

Run it once with CACHE_ENABLED=false and once with caching on, then put both
numbers in the README.
"""
import argparse
import asyncio
import statistics
import time

import httpx


async def run(base: str, total: int, concurrency: int) -> None:
    async with httpx.AsyncClient(base_url=base, timeout=10) as client:
        events = (await client.get("/events")).json()["items"]
        paths = ["/events"] + [f"/events/{e['id']}/seats" for e in events]
        latencies: list[float] = []
        errors = 0
        sem = asyncio.Semaphore(concurrency)

        async def one(i: int) -> None:
            nonlocal errors
            async with sem:
                t = time.perf_counter()
                r = await client.get(paths[i % len(paths)])
                latencies.append((time.perf_counter() - t) * 1000)
                if r.status_code != 200:
                    errors += 1

        start = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(total)))
        wall = time.perf_counter() - start

    latencies.sort()
    q = statistics.quantiles(latencies, n=100)
    print(f"requests={total} concurrency={concurrency} errors={errors}")
    print(f"throughput={total / wall:.0f} req/s  p50={q[49]:.1f} ms  p95={q[94]:.1f} ms  p99={q[98]:.1f} ms")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("base", nargs="?", default="http://localhost:8000")
    ap.add_argument("--requests", type=int, default=2000)
    ap.add_argument("--concurrency", type=int, default=20)
    a = ap.parse_args()
    asyncio.run(run(a.base, a.requests, a.concurrency))
