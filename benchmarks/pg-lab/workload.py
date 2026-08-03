"""One measurement pass: import both clients, connect to PG, run a query mix.

Run in a fresh process per variant. Emits a single JSON line on stdout:

  import_async_ms / import_sync_ms   cold import of <pkg>.client (+ deps)
  rss_after_async / rss_after_sync   retained python RSS (MB) after each import
  connect_async_ms / connect_sync_ms client.connect() incl. engine spawn
  q_*_ms                             median latency per query shape
  engine_rss_mb                      summed RSS of spawned query-engine procs
  rss_final_mb                       python RSS after the workload

Usage: python workload.py <async_client_module> <sync_client_module> [--rounds N]
e.g. `workload.py cli_async.client cli_sync.client` for separate packages, or
`workload.py uni.client uni.client_sync` for a unified package. Modules must be
importable from CWD.
"""

from __future__ import annotations

import gc
import os
import json
import sys
import time
import asyncio
import argparse
import importlib
import statistics
from pathlib import Path

# the generated packages live in the caller's CWD, not next to this script
sys.path.insert(0, os.getcwd())


def rss_mb() -> float:
    with open('/proc/self/statm') as f:
        return int(f.read().split()[1]) * 4096 / 1024 / 1024


def child_engine_rss_mb() -> float:
    """Sum RSS of child processes (the spawned query engines)."""
    import os

    total = 0.0
    me = str(os.getpid())
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
        try:
            stat = Path(f'/proc/{pid}/stat').read_text().split()
            if stat[3] == me:  # ppid
                total += int(Path(f'/proc/{pid}/statm').read_text().split()[1]) * 4096 / 1024 / 1024
        except (OSError, IndexError, ValueError):
            continue
    return total


def timed_import(module: str) -> float:
    t0 = time.perf_counter()
    importlib.import_module(module)
    return (time.perf_counter() - t0) * 1000


def median_ms(samples: list[float]) -> float:
    return round(statistics.median(samples) * 1000, 2)


QUERIES = 'find_many_include', 'find_unique', 'count', 'create_delete', 'query_raw'


def sync_workload(module: str, rounds: int) -> dict[str, float]:
    mod = importlib.import_module(module)
    db = mod.Prisma()
    t0 = time.perf_counter()
    db.connect()
    connect_ms = (time.perf_counter() - t0) * 1000

    samples: dict[str, list[float]] = {q: [] for q in QUERIES}
    for i in range(rounds):
        t = time.perf_counter()
        db.post.find_many(take=25, include={'author': True, 'comments': True}, order={'createdAt': 'desc'})
        samples['find_many_include'].append(time.perf_counter() - t)

        t = time.perf_counter()
        db.post.find_unique(where={'id': 'p1'})
        samples['find_unique'].append(time.perf_counter() - t)

        t = time.perf_counter()
        db.comment.count(where={'isHidden': False})
        samples['count'].append(time.perf_counter() - t)

        t = time.perf_counter()
        u = db.user.create(data={'email': f'wl-s-{i}-{time.time_ns()}@x.com', 'handle': f'wl-s-{i}-{time.time_ns()}'})
        db.user.delete(where={'id': u.id})
        samples['create_delete'].append(time.perf_counter() - t)

        t = time.perf_counter()
        db.query_raw('SELECT id, title FROM "Post" LIMIT 10')
        samples['query_raw'].append(time.perf_counter() - t)

    result = {f'q_{k}_ms': median_ms(v) for k, v in samples.items()}
    result['connect_ms'] = round(connect_ms, 1)
    result['engine_rss_mb'] = round(child_engine_rss_mb(), 1)
    db.disconnect()
    return result


async def async_workload(module: str, rounds: int) -> dict[str, float]:
    mod = importlib.import_module(module)
    db = mod.Prisma()
    t0 = time.perf_counter()
    await db.connect()
    connect_ms = (time.perf_counter() - t0) * 1000

    samples: dict[str, list[float]] = {q: [] for q in QUERIES}
    for i in range(rounds):
        t = time.perf_counter()
        await db.post.find_many(take=25, include={'author': True, 'comments': True}, order={'createdAt': 'desc'})
        samples['find_many_include'].append(time.perf_counter() - t)

        t = time.perf_counter()
        await db.post.find_unique(where={'id': 'p1'})
        samples['find_unique'].append(time.perf_counter() - t)

        t = time.perf_counter()
        await db.comment.count(where={'isHidden': False})
        samples['count'].append(time.perf_counter() - t)

        t = time.perf_counter()
        u = await db.user.create(
            data={'email': f'wl-a-{i}-{time.time_ns()}@x.com', 'handle': f'wl-a-{i}-{time.time_ns()}'}
        )
        await db.user.delete(where={'id': u.id})
        samples['create_delete'].append(time.perf_counter() - t)

        t = time.perf_counter()
        await db.query_raw('SELECT id, title FROM "Post" LIMIT 10')
        samples['query_raw'].append(time.perf_counter() - t)

    result = {f'q_{k}_ms': median_ms(v) for k, v in samples.items()}
    result['connect_ms'] = round(connect_ms, 1)
    result['engine_rss_mb'] = round(child_engine_rss_mb(), 1)
    await db.disconnect()
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('async_client')
    parser.add_argument('sync_client')
    parser.add_argument('--rounds', type=int, default=20)
    args = parser.parse_args()

    out: dict[str, object] = {'rss_start_mb': round(rss_mb(), 1)}

    out['import_async_ms'] = round(timed_import(args.async_client), 1)
    gc.collect()
    out['rss_after_async_mb'] = round(rss_mb(), 1)

    out['import_sync_ms'] = round(timed_import(args.sync_client), 1)
    gc.collect()
    out['rss_after_sync_mb'] = round(rss_mb(), 1)

    a = asyncio.run(async_workload(args.async_client, args.rounds))
    out.update({f'async_{k}': v for k, v in a.items()})

    s = sync_workload(args.sync_client, args.rounds)
    out.update({f'sync_{k}': v for k, v in s.items()})

    gc.collect()
    out['rss_final_mb'] = round(rss_mb(), 1)
    print(json.dumps(out))


if __name__ == '__main__':
    main()
