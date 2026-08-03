"""Decompose a Prisma query into database time vs query-engine time.

The client-side work this fork optimizes (deserialization, model memory) is a
slice of the Python side. This script measures how big that side actually is by
timing the same logical query at three levels:

  1. bare postgres     - psycopg, warm connection, no prisma involved
  2. engine + raw SQL  - `query_raw`: the engine executes, but does no
                         GraphQL parsing/planning of a structured query
  3. engine + full ORM - `find_unique` / `find_many`: the whole path

Differences between the levels attribute the time:

  (1)         = postgres itself
  (2) - (1)   = engine transport + execution overhead
  (3) - (2)   = GraphQL query planning + result serialization

    BENCH_DATABASE_URL=postgresql://... python where_time_goes.py --workdir .
"""

from __future__ import annotations

import os
import sys
import time
import asyncio
import argparse
import statistics
from pathlib import Path


def med_ms(samples: list[float]) -> float:
    return statistics.median(samples) * 1000


async def timeit(fn: object, n: int) -> float:
    await fn()  # warm
    samples = []
    for _ in range(n):
        t = time.perf_counter()
        await fn()  # type: ignore[operator]
        samples.append(time.perf_counter() - t)
    return med_ms(samples)


async def run(reps_single: int, reps_bulk: int) -> None:
    import psycopg

    from pkg.client import Prisma  # type: ignore[import-not-found]

    dsn = os.environ['BENCH_DATABASE_URL']
    db = Prisma()
    await db.connect()

    conn = await psycopg.AsyncConnection.connect(dsn)
    cur = conn.cursor()

    async def pg_single() -> None:
        await cur.execute('SELECT * FROM "Post" WHERE id = %s', ('p1',))
        await cur.fetchall()

    async def pg_bulk() -> None:
        await cur.execute('SELECT * FROM "Post" LIMIT 400')
        await cur.fetchall()

    rows = {
        'postgres (psycopg, no prisma)': (
            await timeit(pg_single, reps_single),
            await timeit(pg_bulk, reps_bulk),
        ),
        'engine, raw SQL (query_raw)': (
            await timeit(lambda: db.query_raw('SELECT * FROM "Post" WHERE id = $1', 'p1'), reps_single),
            await timeit(lambda: db.query_raw('SELECT * FROM "Post" LIMIT 400'), reps_bulk),
        ),
        'engine, full ORM path': (
            await timeit(lambda: db.post.find_unique(where={'id': 'p1'}), reps_single),
            await timeit(lambda: db.post.find_many(take=400), reps_bulk),
        ),
    }
    await conn.close()
    await db.disconnect()

    print(f'{"level":34s} {"1 row":>10s} {"400 rows":>10s}')
    for label, (a, b) in rows.items():
        print(f'{label:34s} {a:9.2f}ms {b:9.2f}ms')

    pg1, pg2 = rows['postgres (psycopg, no prisma)']
    rw1, rw2 = rows['engine, raw SQL (query_raw)']
    or1, or2 = rows['engine, full ORM path']

    print()
    print(f'{"attribution":34s} {"1 row":>10s} {"400 rows":>10s}')
    print(f'{"  postgres itself":34s} {pg1:9.2f}ms {pg2:9.2f}ms   ({pg1 / or1 * 100:.0f}% / {pg2 / or2 * 100:.0f}%)')
    print(
        f'{"  engine transport + execution":34s} {rw1 - pg1:9.2f}ms {rw2 - pg2:9.2f}ms   '
        f'({(rw1 - pg1) / or1 * 100:.0f}% / {(rw2 - pg2) / or2 * 100:.0f}%)'
    )
    print(
        f'{"  GraphQL planning + serialize":34s} {or1 - rw1:9.2f}ms {or2 - rw2:9.2f}ms   '
        f'({(or1 - rw1) / or1 * 100:.0f}% / {(or2 - rw2) / or2 * 100:.0f}%)'
    )
    print()
    print(f'query engine accounts for {(or1 - pg1) / or1 * 100:.0f}% (1 row) / {(or2 - pg2) / or2 * 100:.0f}% (400 rows)')
    print(f'the same query is {or1 / pg1:.0f}x / {or2 / pg2:.0f}x slower through prisma than through psycopg')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--workdir', default='.', help='dir containing the generated `pkg` package')
    parser.add_argument('--reps-single', type=int, default=60)
    parser.add_argument('--reps-bulk', type=int, default=30)
    args = parser.parse_args()

    sys.path.insert(0, str(Path(args.workdir).resolve()))
    asyncio.run(run(args.reps_single, args.reps_bulk))


if __name__ == '__main__':
    main()
