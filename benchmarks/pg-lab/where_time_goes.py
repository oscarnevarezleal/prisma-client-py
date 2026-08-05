"""Decompose a Prisma query into database time vs everything above it.

The client-side work this fork optimizes (deserialization, model memory) is a
slice of the Python side. This script measures how big that side actually is by
timing the same logical query at three levels:

  1. bare postgres      - psycopg, warm connection, no prisma involved
  2. prisma + raw SQL   - `query_raw`: the same SQL, through the client and the
                          engine, but with no structured query to parse or plan
  3. prisma + full ORM  - `find_unique` / `find_many`: the whole path

Differences between the levels attribute the time:

  (1)         = postgres itself, as psycopg sees it
  (2) - (1)   = everything the prisma stack adds to running that SQL
  (3) - (2)   = everything the structured-query path adds on top of raw SQL

**Neither difference is "query-engine overhead".** Level 2 minus level 1 spans
the Python client's request handling, the HTTP round-trip to the engine, the
engine's own connector, and the client's decoding of the reply; level 3 minus
level 2 spans GraphQL parse/plan, the engine's result serialization *and* the
construction of record objects, which psycopg never does at all — it returns
plain tuples. Read these as end-to-end stack differences. Splitting the engine
itself out from the client would need instrumentation inside the engine
process, which this script does not have.

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
        'prisma stack, raw SQL (query_raw)': (
            await timeit(lambda: db.query_raw('SELECT * FROM "Post" WHERE id = $1', 'p1'), reps_single),
            await timeit(lambda: db.query_raw('SELECT * FROM "Post" LIMIT 400'), reps_bulk),
        ),
        'prisma stack, full ORM path': (
            await timeit(lambda: db.post.find_unique(where={'id': 'p1'}), reps_single),
            await timeit(lambda: db.post.find_many(take=400), reps_bulk),
        ),
    }
    await conn.close()
    await db.disconnect()

    width = 38
    print(f'{"level":{width}s} {"1 row":>10s} {"400 rows":>10s}')
    for label, (a, b) in rows.items():
        print(f'{label:{width}s} {a:9.2f}ms {b:9.2f}ms')

    pg1, pg2 = rows['postgres (psycopg, no prisma)']
    rw1, rw2 = rows['prisma stack, raw SQL (query_raw)']
    or1, or2 = rows['prisma stack, full ORM path']

    print()
    print(f'{"attribution":{width}s} {"1 row":>10s} {"400 rows":>10s}')
    print(
        f'{"  postgres itself (psycopg)":{width}s} {pg1:9.2f}ms {pg2:9.2f}ms   '
        f'({pg1 / or1 * 100:.0f}% / {pg2 / or2 * 100:.0f}%)'
    )
    print(
        f'{"  client + engine, same SQL":{width}s} {rw1 - pg1:9.2f}ms {rw2 - pg2:9.2f}ms   '
        f'({(rw1 - pg1) / or1 * 100:.0f}% / {(rw2 - pg2) / or2 * 100:.0f}%)'
    )
    print(
        f'{"  structured query on top of that":{width}s} {or1 - rw1:9.2f}ms {or2 - rw2:9.2f}ms   '
        f'({(or1 - rw1) / or1 * 100:.0f}% / {(or2 - rw2) / or2 * 100:.0f}%)'
    )
    print()
    print(
        f'everything above postgres accounts for {(or1 - pg1) / or1 * 100:.0f}% (1 row) / '
        f'{(or2 - pg2) / or2 * 100:.0f}% (400 rows) of the full ORM path'
    )
    print(f'the same query is {or1 / pg1:.0f}x / {or2 / pg2:.0f}x slower through prisma than through psycopg')
    print()
    print('Read those as an end-to-end difference between two stacks, not as query-engine overhead:')
    print('  - row 2 also contains the Python client\'s request handling, the HTTP round-trip and')
    print('    the client-side decode of the reply, alongside the engine\'s connector and execution')
    print('  - row 3 also contains building record objects, which psycopg never does — it returns')
    print('    plain tuples, so level 1 is not doing the same amount of work as level 3')
    print('  - separating the engine process from the client would need instrumentation inside the')
    print('    engine; nothing here measures that split')


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
