"""How much of a query is the engine? Prisma client vs SQLAlchemy Core.

Earlier measurements compared Prisma against hand-written psycopg, which
overstates the achievable win: psycopg is a floor nothing built on SQLAlchemy
can reach, because SQLAlchemy has its own statement-compilation cost. This runs
the *same* four queries through both paths against the *same* rows, with the
SQLAlchemy side built from `prisma.sa` — i.e. from the same schema metadata a
Stage 3 compiler would use. The gap here is the realistic ceiling for replacing
the engine, not an aspirational one.

Both sides are measured interleaved and in both directions, because sequential
A/B runs on a shared machine disagreed by 13 percentage points earlier in this
work.

Usage:
    BENCH_DATABASE_URL=postgresql://... python sa_vs_engine.py --workdir /tmp/pglab-sa
"""

from __future__ import annotations

import os
import sys
import json
import time
import argparse
import statistics
from typing import Any, Dict, List, Callable

import sqlalchemy as sa


def median_ms(samples: List[float]) -> float:
    return round(statistics.median(samples) * 1000, 3)


def p90_ms(samples: List[float]) -> float:
    ordered = sorted(samples)
    return round(ordered[int(len(ordered) * 0.9)] * 1000, 3)


# -- the four queries, in both dialects ---------------------------------------
#
# `find_many_include` is the interesting one: a to-many include is where the
# engine does real planning work, and where a naive SQL translation is most
# likely to be slower rather than faster.


def build_sa_queries(md: sa.MetaData) -> Dict[str, Any]:
    post = md.tables['Post']
    user = md.tables['User']
    comment = md.tables['Comment']

    return {
        'find_many_include': (
            sa.select(post).order_by(post.c.createdAt.desc()).limit(25),
            sa.select(user),
            comment,
        ),
        'find_unique': sa.select(post).where(post.c.id == 'p1'),
        'count': sa.select(sa.func.count()).select_from(comment).where(comment.c.isHidden.is_(False)),
        'query_raw': sa.text('SELECT id, title FROM "Post" LIMIT 10'),
    }


def sa_runners(conn: sa.Connection, md: sa.MetaData) -> Dict[str, Callable[[], Any]]:
    queries = build_sa_queries(md)
    post = md.tables['Post']
    user = md.tables['User']
    comment = md.tables['Comment']

    def find_many_include() -> Any:
        # The two-query shape the engine itself uses for a to-many include:
        # parents first, then children by parent id. Emulating `include` with a
        # single join would multiply parent rows by child count and need
        # de-duplication in Python, which is slower, not faster.
        parents = conn.execute(sa.select(post).order_by(post.c.createdAt.desc()).limit(25)).mappings().all()
        ids = [row['id'] for row in parents]
        authors = conn.execute(sa.select(user).where(user.c.id.in_([r['authorId'] for r in parents]))).mappings().all()
        children = conn.execute(sa.select(comment).where(comment.c.postId.in_(ids))).mappings().all()
        return parents, authors, children

    return {
        'find_many_include': find_many_include,
        'find_unique': lambda: conn.execute(queries['find_unique']).mappings().first(),
        'count': lambda: conn.execute(queries['count']).scalar_one(),
        'query_raw': lambda: conn.execute(queries['query_raw']).mappings().all(),
    }


def prisma_runners(db: Any) -> Dict[str, Callable[[], Any]]:
    return {
        'find_many_include': lambda: db.post.find_many(
            take=25, include={'author': True, 'comments': True}, order={'createdAt': 'desc'}
        ),
        'find_unique': lambda: db.post.find_unique(where={'id': 'p1'}),
        'count': lambda: db.comment.count(where={'isHidden': False}),
        'query_raw': lambda: db.query_raw('SELECT id, title FROM "Post" LIMIT 10'),
    }


def check_equivalence(prisma: Dict[str, Callable[[], Any]], alchemy: Dict[str, Callable[[], Any]]) -> None:
    parents, _authors, children = alchemy['find_many_include']()
    posts = prisma['find_many_include']()

    assert [p.id for p in posts] == [r['id'] for r in parents], 'find_many_include returned different rows'
    assert len(posts) == 25, f'expected 25 posts, got {len(posts)} — is the database seeded?'
    assert sum(len(p.comments or []) for p in posts) == len(children), 'include returned a different comment set'

    assert (prisma['find_unique']() is not None) == (alchemy['find_unique']() is not None)
    assert prisma['count']() == alchemy['count']()
    assert len(prisma['query_raw']()) == len(alchemy['query_raw']())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--workdir', required=True)
    parser.add_argument('--package', default='pkg_async')
    parser.add_argument('--rounds', type=int, default=60)
    parser.add_argument('--warmup', type=int, default=10)
    parser.add_argument('--json')
    args = parser.parse_args()

    url = os.environ['BENCH_DATABASE_URL']
    os.chdir(args.workdir)
    sys.path.insert(0, os.getcwd())

    import importlib

    mod = importlib.import_module(args.package)
    db = mod.Prisma()
    db.connect()
    prisma = prisma_runners(db)

    metadata_mod = importlib.import_module(f'{args.package}.metadata')
    from prisma.sa import build_metadata

    md = build_metadata(metadata_mod.SCHEMA, metadata_mod.ENUM_SCHEMA, metadata_mod.DATABASE_PROVIDER)

    engine = sa.create_engine(url.replace('postgresql://', 'postgresql+psycopg://'), pool_pre_ping=False)
    conn = engine.connect()
    alchemy = sa_runners(conn, md)

    # A benchmark that compares a query returning 25 rows against one returning
    # zero is not slow, it is meaningless. Check equivalence before timing.
    check_equivalence(prisma, alchemy)

    names = list(prisma)
    samples: Dict[str, Dict[str, List[float]]] = {n: {'prisma': [], 'sqlalchemy': []} for n in names}

    for name in names:
        for _ in range(args.warmup):
            prisma[name]()
            alchemy[name]()

    for i in range(args.rounds):
        for name in names:
            # alternate which side goes first so neither systematically pays for
            # cache warming the other benefits from
            order = ('prisma', 'sqlalchemy') if i % 2 == 0 else ('sqlalchemy', 'prisma')
            for side in order:
                run = prisma[name] if side == 'prisma' else alchemy[name]
                t = time.perf_counter()
                run()
                samples[name][side].append(time.perf_counter() - t)

    rows = []
    for name in names:
        prisma_med = median_ms(samples[name]['prisma'])
        sa_med = median_ms(samples[name]['sqlalchemy'])
        rows.append(
            {
                'query': name,
                'prisma_ms': prisma_med,
                'sqlalchemy_ms': sa_med,
                'prisma_p90_ms': p90_ms(samples[name]['prisma']),
                'sqlalchemy_p90_ms': p90_ms(samples[name]['sqlalchemy']),
                'speedup': round(prisma_med / sa_med, 2) if sa_med else None,
                'saved_pct': round((1 - sa_med / prisma_med) * 100, 1) if prisma_med else None,
            }
        )

    width = max(len(r['query']) for r in rows)
    print(f'{"query":<{width}}  {"prisma":>9}  {"sqlalchemy":>11}  {"speedup":>8}  {"saved":>7}')
    for row in rows:
        print(
            f'{row["query"]:<{width}}  {row["prisma_ms"]:>8.3f}ms  {row["sqlalchemy_ms"]:>10.3f}ms  '
            f'{row["speedup"]:>7}x  {row["saved_pct"]:>6}%'
        )

    total_prisma = sum(r['prisma_ms'] for r in rows)
    total_sa = sum(r['sqlalchemy_ms'] for r in rows)
    print(f'\ntotal  {total_prisma:.3f}ms -> {total_sa:.3f}ms  ({(1 - total_sa / total_prisma) * 100:.1f}% saved)')

    if args.json:
        with open(args.json, 'w') as f:
            json.dump({'rounds': args.rounds, 'queries': rows}, f, indent=1)

    # What the SQLAlchemy column above is *not* paying for. It returns row
    # mappings; the Prisma client returns record objects. Reporting the SQL time
    # alone would overstate the win, so measure the missing step explicitly
    # rather than leaving the reader to guess at it.
    post_model = importlib.import_module(f'{args.package}.models').Post
    rows = conn.execute(sa.select(md.tables['Post']).limit(25)).mappings().all()
    construct: List[float] = []
    for _ in range(args.rounds):
        t = time.perf_counter()
        [post_model.model_validate(dict(row)) for row in rows]
        construct.append(time.perf_counter() - t)

    print(
        f'\nnot counted on the SQLAlchemy side: {median_ms(construct):.3f}ms to build '
        f'{len(rows)} record objects from the rows'
    )

    conn.close()
    engine.dispose()
    db.disconnect()


if __name__ == '__main__':
    main()
