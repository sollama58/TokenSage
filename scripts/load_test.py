"""Replay realistic traffic against a running TokenSage and report latency and status mix.

    uv run python scripts/load_test.py --base http://localhost:10000 --key devkey123 \
        --cas load_cas.txt --rate 60 --duration 60 --depth basic

Each request follows the documented client behaviour: 202 -> poll until 200 (counted as one
logical request, with end-to-end latency). Exits 1 if any 5xx other than 503 appears.

--mode batch replays a prefetching consumer instead: every --scan-s seconds it POSTs the new
(cold, never-seen) coins as one batch and polls each job, reporting time-to-done.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

import httpx


async def one(
    client: httpx.AsyncClient, ca: str, depth: str, wait: int, stats: dict, sem: asyncio.Semaphore
) -> None:
    async with sem:
        t0 = time.perf_counter()
        polls = 0
        status = "error"
        try:
            for _ in range(20):
                r = await client.get(f"/v1/tokens/{ca}", params={"depth": depth, "wait": wait})
                stats["http"][r.status_code] += 1
                if r.status_code == 202:
                    polls += 1
                    await asyncio.sleep(float(r.headers.get("retry-after", "2")))
                    continue
                if r.status_code in (429, 503):
                    await asyncio.sleep(float(r.headers.get("retry-after", "3")))
                    continue
                status = (
                    r.json().get("status", str(r.status_code))
                    if r.status_code == 200
                    else f"http_{r.status_code}"
                )
                if r.status_code >= 500:
                    stats["server_errors"] += 1
                break
        except httpx.HTTPError as e:
            status = f"exc_{type(e).__name__}"
        stats["final"][status] += 1
        stats["polls"] += polls
        stats["latency"].append(time.perf_counter() - t0)


async def poll_job(
    client: httpx.AsyncClient, job_id: int, t0: float, poll_s: float, stats: dict
) -> None:
    """Follow one batch job to done/failed, the way a prefetching consumer would."""
    deadline = t0 + 300
    while time.perf_counter() < deadline:
        await asyncio.sleep(poll_s)
        try:
            r = await client.get(f"/v1/jobs/{job_id}")
        except httpx.HTTPError as e:
            stats["final"][f"exc_{type(e).__name__}"] += 1
            return
        stats["http"][r.status_code] += 1
        if r.status_code != 200:
            continue
        j = r.json()
        if j["status"] in ("done", "failed"):
            outcome = (j.get("result") or {}).get("status") if j["status"] == "done" else "failed"
            stats["final"][outcome or "done"] += 1
            stats["latency"].append(time.perf_counter() - t0)
            return
    stats["final"]["timeout"] += 1


async def batch_mode(args: argparse.Namespace, cas: list[str]) -> int:
    """TrenchScanner-style traffic: every --scan-s seconds POST the new (cold) coins as one
    batch, then poll each job until done. Reports time-to-done per coin."""
    stats: dict = {"http": Counter(), "final": Counter(), "latency": [], "server_errors": 0}
    per_scan = args.rate * args.scan_s / 60.0
    carry = 0.0
    idx = 0
    tasks: list[asyncio.Task[None]] = []
    async with httpx.AsyncClient(
        base_url=args.base, headers={"Authorization": f"Bearer {args.key}"}, timeout=40.0
    ) as client:
        t_end = time.perf_counter() + args.duration
        while time.perf_counter() < t_end and idx < len(cas):
            carry += per_scan
            n = int(carry)
            carry -= n
            new = cas[idx : idx + n]
            idx += n
            if new:
                t0 = time.perf_counter()
                r = await client.post("/v1/tokens:batch", json={"cas": new, "depth": args.depth})
                stats["http"][r.status_code] += 1
                if r.status_code >= 500 and r.status_code != 503:
                    stats["server_errors"] += 1
                if r.status_code == 200:
                    for it in r.json()["items"]:
                        if it.get("job_id") and it["status"] == "pending":
                            tasks.append(
                                asyncio.create_task(
                                    poll_job(client, it["job_id"], t0, args.poll_s, stats)
                                )
                            )
                        else:
                            stats["final"][it["status"]] += 1
                            if it["status"] in ("complete", "partial"):
                                stats["latency"].append(time.perf_counter() - t0)
            await asyncio.sleep(args.scan_s)
        await asyncio.gather(*tasks)

    lat = sorted(stats["latency"])
    pct = lambda p: lat[min(len(lat) - 1, int(len(lat) * p))] if lat else 0  # noqa: E731
    print(
        f"coins sent: {idx}  (rate {args.rate}/min, one batch every {args.scan_s:.0f}s, "
        f"depth={args.depth}, poll every {args.poll_s}s)"
    )
    print(f"http statuses: {dict(stats['http'])}")
    print(f"final outcomes: {dict(stats['final'])}")
    if lat:
        print(
            f"time-to-done: p50 {pct(0.5):.1f} s  p95 {pct(0.95):.1f} s  p99 {pct(0.99):.1f} s"
            f"  max {lat[-1]:.1f} s  mean {statistics.mean(lat):.1f} s"
        )
    return 1 if stats["server_errors"] else 0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--cas", required=True, help="file with one CA per line")
    ap.add_argument("--rate", type=float, default=60, help="requests per minute")
    ap.add_argument("--duration", type=float, default=60, help="seconds")
    ap.add_argument("--depth", default="basic")
    ap.add_argument("--wait", type=int, default=10)
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument(
        "--mode",
        choices=("get", "batch"),
        default="get",
        help="get: GET /v1/tokens with zipf-hot CAs; batch: cold coins via batch + job polling",
    )
    ap.add_argument("--scan-s", type=float, default=30.0, help="batch mode: seconds per scan")
    ap.add_argument("--poll-s", type=float, default=1.0, help="batch mode: job poll interval")
    args = ap.parse_args()

    cas = [c.strip() for c in Path(args.cas).read_text().splitlines() if c.strip()]
    if args.mode == "batch":
        return await batch_mode(args, cas)
    stats: dict = {
        "http": Counter(),
        "final": Counter(),
        "latency": [],
        "polls": 0,
        "server_errors": 0,
    }
    sem = asyncio.Semaphore(args.concurrency)
    interval = 60.0 / args.rate
    rnd = random.Random(42)
    async with httpx.AsyncClient(
        base_url=args.base, headers={"Authorization": f"Bearer {args.key}"}, timeout=40.0
    ) as client:
        tasks: list[asyncio.Task[None]] = []
        t_end = time.perf_counter() + args.duration
        n = 0
        while time.perf_counter() < t_end:
            # zipf-ish: a few hot CAs get most of the traffic, like real consumers
            ca = cas[min(int(rnd.paretovariate(1.2)) - 1, len(cas) - 1)]
            tasks.append(asyncio.create_task(one(client, ca, args.depth, args.wait, stats, sem)))
            n += 1
            await asyncio.sleep(interval)
        await asyncio.gather(*tasks)

    lat = sorted(stats["latency"])
    pct = lambda p: lat[min(len(lat) - 1, int(len(lat) * p))] if lat else 0  # noqa: E731
    print(
        f"requests sent: {n}  (rate {args.rate}/min for {args.duration:.0f}s, depth={args.depth})"
    )
    print(f"http statuses: {dict(stats['http'])}")
    print(f"final outcomes: {dict(stats['final'])}   extra polls: {stats['polls']}")
    if lat:
        ms = lambda v: f"{v * 1000:.0f} ms"  # noqa: E731
        print(
            f"end-to-end latency: p50 {ms(pct(0.5))}  p95 {ms(pct(0.95))}  "
            f"p99 {ms(pct(0.99))}  max {ms(lat[-1])}  mean {ms(statistics.mean(lat))}"
        )
    else:
        print("no latencies")
    return 1 if stats["server_errors"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
