"""Check each Prisma -> SQLAlchemy Core translation for a **bulk write**.

`verify_translations.py` is the read harness: it runs the Prisma call and the
SQLAlchemy call against the same rows and compares what comes back. A write
cannot be checked that way — running both sides against the same rows means the
second one sees what the first one did.

So each case here runs **twice, from a clean slate**: teardown, setup, the
Prisma call, observe the resulting rows, teardown; then teardown, setup, the
SQLAlchemy call, observe, teardown. A case passes when the two passes agree on
*both* the value the call returned (`create_many`/`update_many`/`delete_many` all
return a count) and the state of the rows afterwards.

Because the two passes happen at different instants, an observation may never
compare an absolute timestamp between them. What it compares instead is the
*shape*: how many distinct instants a batch produced, whether `createdAt` equals
`updatedAt`, whether a row's `updatedAt` moved past where it was seeded. Those
are the properties the runbook makes claims about.

    BENCH_DATABASE_URL=postgresql://... python verify_bulk_writes.py --workdir /tmp/pglab-sa

Exit code is the number of mismatches.

Everything this harness writes is prefixed `vbw-` and is deleted on the way in
and on the way out, so it can share a database with the seed data the read
harness needs. The one exception is `FeatureFlag`, which the seed never
populates and which the whole-table `delete_many()` case empties.
"""

from __future__ import annotations

import os
import sys
import argparse
import datetime
import importlib
import itertools
from typing import Any, Dict, List, Tuple, Callable, Iterable, Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

#: Prefix on every id, slug and name this harness writes. Teardown is "delete
#: everything starting with this", which is what lets the harness run against
#: the same database as the read harness without disturbing its seed rows.
NS = 'vbw'

#: Seeded into `createdAt`/`updatedAt` so that "did this row's timestamp move?"
#: is answerable without comparing two live clocks.
SEEDED = datetime.datetime(2020, 1, 1, 12, 0, 0)

#: (name, setup, prisma call, sqlalchemy call, observation, expectation).
#: `mismatch` pins a translation that is *wrong* — if one of those ever starts
#: agreeing, the case has gone vacuous and stops protecting anything.
Case = Tuple[
    str,
    Callable[[], None],
    Callable[[], Any],
    Callable[[], Any],
    Callable[[], Any],
    str,
]


def sqlalchemy_url(url: str) -> str:
    """`postgres://` -> the driver actually installed here."""
    for prefix in ('postgres://', 'postgresql://'):
        if url.startswith(prefix):
            return 'postgresql+psycopg://' + url[len(prefix) :]
    return url


def normalize(value: Any) -> Any:
    """Compare on values, not on the container the two APIs happen to use."""
    if isinstance(value, sa.engine.Row):
        return tuple(normalize(item) for item in value)
    if isinstance(value, sa.RowMapping):
        return {key: normalize(item) for key, item in dict(value).items()}
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def teardown(conn: sa.Connection) -> None:
    """Delete everything this harness could have written, in FK order."""
    statements = (
        f'DELETE FROM "Post" WHERE "siteId" LIKE \'{NS}-%\'',
        f'DELETE FROM "Site" WHERE id LIKE \'{NS}-%\'',
        f'DELETE FROM "Organization" WHERE id LIKE \'{NS}-%\' OR slug LIKE \'{NS}-%\'',
        f'DELETE FROM "User" WHERE id LIKE \'{NS}-%\'',
        f'DELETE FROM "Tag" WHERE name LIKE \'{NS}-%\'',
        # unseeded and used by nobody else; the whole-table delete case empties it
        'DELETE FROM "FeatureFlag"',
    )
    for statement in statements:
        conn.execute(sa.text(statement))


def scaffold(conn: sa.Connection) -> None:
    """An org, a user and a site — the FK targets every `Post` case needs."""
    conn.execute(
        sa.text(
            'INSERT INTO "Organization"(id,slug,name,"createdAt","updatedAt") '
            'VALUES (:id,:slug,:name,:moment,:moment)'
        ),
        {'id': f'{NS}-org', 'slug': f'{NS}-org', 'name': 'org', 'moment': SEEDED},
    )
    conn.execute(
        sa.text(
            'INSERT INTO "User"(id,email,handle,"createdAt","updatedAt") VALUES (:id,:email,:handle,:moment,:moment)'
        ),
        {'id': f'{NS}-user', 'email': f'{NS}@example.test', 'handle': NS, 'moment': SEEDED},
    )
    conn.execute(
        sa.text('INSERT INTO "Site"(id,"orgId",domain,title) VALUES (:id,:org,:domain,:title)'),
        {'id': f'{NS}-site', 'org': f'{NS}-org', 'domain': f'{NS}.example.test', 'title': 'site'},
    )


def seed_posts(conn: sa.Connection, specs: Sequence[Tuple[str, str, str]]) -> None:
    """`(slug, title, status)` rows with a known, fixed `updatedAt`."""
    for index, (slug, title, status) in enumerate(specs):
        conn.execute(
            sa.text(
                'INSERT INTO "Post"(id,"siteId","authorId",slug,title,status,"createdAt","updatedAt") '
                'VALUES (:id,:site,:author,:slug,:title,CAST(:status AS "PostStatus"),:moment,:moment)'
            ),
            {
                'id': f'{NS}-post-{index}',
                'site': f'{NS}-site',
                'author': f'{NS}-user',
                'slug': slug,
                'title': title,
                'status': status,
                'moment': SEEDED,
            },
        )


def seed_flags(conn: sa.Connection, keys: Iterable[str]) -> None:
    for key in keys:
        conn.execute(
            sa.text('INSERT INTO "FeatureFlag"(id,key,enabled,"updatedAt") VALUES (:id,:key,false,:moment)'),
            {'id': f'{NS}-flag-{key}', 'key': key, 'moment': SEEDED},
        )


# ---------------------------------------------------------------------------
# observations
# ---------------------------------------------------------------------------


def tag_names(conn: sa.Connection) -> List[str]:
    return sorted(conn.execute(sa.text(f'SELECT name FROM "Tag" WHERE name LIKE \'{NS}-%\'')).scalars())


def tag_shape(conn: sa.Connection) -> Dict[str, Any]:
    rows = conn.execute(sa.text(f'SELECT id, name FROM "Tag" WHERE name LIKE \'{NS}-%\'')).mappings().all()
    return {
        'names': sorted(row['name'] for row in rows),
        'distinct ids': len({row['id'] for row in rows}),
        'ids look like cuids': sorted({len(row['id']) for row in rows}),
    }


def org_batch_shape(conn: sa.Connection) -> Dict[str, Any]:
    """The two claims about a `create_many` batch: own id, shared instant."""
    rows = (
        conn.execute(
            sa.text(f'SELECT id, slug, "createdAt", "updatedAt" FROM "Organization" WHERE slug LIKE \'{NS}-o%\'')
        )
        .mappings()
        .all()
    )
    return {
        'rows': len(rows),
        'distinct ids': len({row['id'] for row in rows}),
        'distinct createdAt': len({row['createdAt'] for row in rows}),
        'distinct updatedAt': len({row['updatedAt'] for row in rows}),
        'createdAt == updatedAt': all(row['createdAt'] == row['updatedAt'] for row in rows),
        'millisecond resolution': sorted({row['createdAt'].microsecond % 1000 for row in rows}),
    }


def post_rows(conn: sa.Connection) -> List[Tuple[Any, ...]]:
    rows = (
        conn.execute(
            sa.text(
                'SELECT slug, title, status::text AS status, "publishedAt", '
                '"createdAt" = :seeded AS created_untouched, "updatedAt" > :seeded AS moved '
                f'FROM "Post" WHERE "siteId" = \'{NS}-site\' ORDER BY slug'
            ),
            {'seeded': SEEDED},
        )
        .mappings()
        .all()
    )
    return [
        (row['slug'], row['title'], row['status'], row['publishedAt'], row['created_untouched'], row['moved'])
        for row in rows
    ]


def post_stamp_shape(conn: sa.Connection) -> Dict[str, Any]:
    rows = (
        conn.execute(sa.text(f'SELECT "createdAt", "updatedAt" FROM "Post" WHERE "siteId" = \'{NS}-site\''))
        .mappings()
        .all()
    )
    return {
        'rows': len(rows),
        'distinct updatedAt': len({row['updatedAt'] for row in rows}),
        'createdAt untouched': sorted({row['createdAt'] for row in rows}) == [SEEDED],
        'updatedAt moved': sum(1 for row in rows if row['updatedAt'] > SEEDED),
    }


def flag_shape(conn: sa.Connection) -> Dict[str, Any]:
    rows = conn.execute(sa.text('SELECT id, key, "updatedAt" FROM "FeatureFlag"')).mappings().all()
    return {
        'rows': len(rows),
        'keys': sorted(row['key'] for row in rows),
        'distinct ids': len({row['id'] for row in rows}),
        'distinct updatedAt': len({row['updatedAt'] for row in rows}),
    }


def refused(call: Callable[[], Any]) -> Any:
    """`('raised', ...)` rather than the exception class.

    Prisma raises `UniqueViolationError` and SQLAlchemy raises `IntegrityError`;
    the claim under test is not that the names match, it is that both refuse the
    batch and that neither leaves half of it behind.
    """
    try:
        return ('returned', call())
    except Exception:  # noqa: BLE001 - the type is deliberately not part of the comparison
        return ('raised',)


# ---------------------------------------------------------------------------
# cases
# ---------------------------------------------------------------------------


def build_cases(db: Any, conn: sa.Connection, md: sa.MetaData) -> List[Case]:
    from prisma.sa import values_for_create, values_for_update

    tag = md.tables['Tag']
    org = md.tables['Organization']
    post = md.tables['Post']
    flag = md.tables['FeatureFlag']

    def moment() -> datetime.datetime:
        return datetime.datetime.now(datetime.timezone.utc)

    def insert_many(
        table: sa.Table,
        model: str,
        data: Sequence[Dict[str, Any]],
        *,
        skip_duplicates: bool = False,
    ) -> int:
        """The translation of `create_many`, exactly as the runbook states it.

        One `moment` for the whole batch, one INSERT per distinct key set, and
        the count read from `RETURNING` — `CursorResult.rowcount` is -1 for an
        INSERT under psycopg, so it cannot be the source of the count.
        """
        stamp = moment()
        rows = [values_for_create(model, item, moment=stamp) for item in data]
        inserted = 0
        for _, group in itertools.groupby(sorted(rows, key=lambda row: sorted(row)), key=lambda row: tuple(sorted(row))):
            batch = list(group)
            statement = postgresql.insert(table).values(batch) if skip_duplicates else sa.insert(table).values(batch)
            if skip_duplicates:
                statement = statement.on_conflict_do_nothing()
            inserted += len(conn.execute(statement.returning(*table.primary_key.columns)).fetchall())
        return inserted

    # `update_many` and `delete_many` are set-based: an unscoped `where` would
    # reach the read harness's seed rows in the same database. Every Post filter
    # below is therefore ANDed with "belongs to this harness's site", which is
    # also what a real migration does — and it makes the AND translation part of
    # every case rather than only the one named for it.
    def scoped(where: Dict[str, Any]) -> Dict[str, Any]:
        return {'AND': [{'siteId': f'{NS}-site'}, where]}

    def sa_scoped(clause: Any) -> Any:
        return sa.and_(post.c.siteId == f'{NS}-site', clause)

    def tag_data(*names: str) -> List[Dict[str, Any]]:
        return [{'name': f'{NS}-{name}'} for name in names]

    def org_data(count: int) -> List[Dict[str, Any]]:
        return [{'slug': f'{NS}-o{index}', 'name': f'org {index}'} for index in range(count)]

    heterogeneous = [
        {'siteId': f'{NS}-site', 'authorId': f'{NS}-user', 'slug': 'a', 'title': 'A'},
        {
            'siteId': f'{NS}-site',
            'authorId': f'{NS}-user',
            'slug': 'b',
            'title': 'B',
            'status': 'PUBLISHED',
            'publishedAt': datetime.datetime(2021, 6, 1, 9, 30),
        },
    ]

    def nothing() -> None:
        return None

    def scaffold_only() -> None:
        scaffold(conn)

    def five_posts() -> None:
        scaffold(conn)
        seed_posts(
            conn,
            [
                ('p0', 'zero', 'DRAFT'),
                ('p1', 'one', 'DRAFT'),
                ('p2', 'two', 'DRAFT'),
                ('p3', 'three', 'PUBLISHED'),
                ('p4', 'four', 'PUBLISHED'),
            ],
        )

    def posts_two_already_equal() -> None:
        scaffold(conn)
        seed_posts(
            conn,
            [
                ('p0', 'same', 'DRAFT'),
                ('p1', 'same', 'DRAFT'),
                ('p2', 'different', 'DRAFT'),
                ('p3', 'different', 'DRAFT'),
            ],
        )

    def one_existing_tag() -> None:
        conn.execute(sa.text('INSERT INTO "Tag"(id,name) VALUES (:id,:name)'), {'id': f'{NS}-t', 'name': f'{NS}-1'})

    def three_flags() -> None:
        seed_flags(conn, ('alpha', 'beta', 'gamma'))

    cases: List[Case] = [
        # -- create_many -------------------------------------------------------
        (
            'create_many returns a count, not rows',
            nothing,
            lambda: db.tag.create_many(data=tag_data('1', '2', '3', '4')),
            lambda: insert_many(tag, 'Tag', tag_data('1', '2', '3', '4')),
            lambda: tag_shape(conn),
            'match',
        ),
        (
            'create_many: every row gets its own id, the batch shares one instant',
            nothing,
            lambda: db.organization.create_many(data=org_data(5)),
            lambda: insert_many(org, 'Organization', org_data(5)),
            lambda: org_batch_shape(conn),
            'match',
        ),
        (
            'create_many fills @updatedAt on a model that has no createdAt',
            nothing,
            lambda: db.featureflag.create_many(data=[{'key': f'{NS}-{n}'} for n in ('a', 'b', 'c')]),
            lambda: insert_many(flag, 'FeatureFlag', [{'key': f'{NS}-{n}'} for n in ('a', 'b', 'c')]),
            lambda: flag_shape(conn),
            'match',
        ),
        (
            'create_many(skip_duplicates=True) against an existing row',
            one_existing_tag,
            lambda: db.tag.create_many(data=tag_data('1', '2', '3'), skip_duplicates=True),
            lambda: insert_many(tag, 'Tag', tag_data('1', '2', '3'), skip_duplicates=True),
            lambda: tag_names(conn),
            'match',
        ),
        (
            'create_many(skip_duplicates=True) with the duplicate inside the batch',
            nothing,
            lambda: db.tag.create_many(data=tag_data('a', 'a', 'b'), skip_duplicates=True),
            lambda: insert_many(tag, 'Tag', tag_data('a', 'a', 'b'), skip_duplicates=True),
            lambda: tag_names(conn),
            'match',
        ),
        (
            'create_many without skip_duplicates: the conflict aborts the whole batch',
            one_existing_tag,
            lambda: refused(lambda: db.tag.create_many(data=tag_data('1', '2', '3'))),
            lambda: refused(lambda: insert_many(tag, 'Tag', tag_data('1', '2', '3'))),
            lambda: tag_names(conn),
            'match',
        ),
        (
            'create_many with per-row key sets: one INSERT per key set',
            scaffold_only,
            lambda: db.post.create_many(data=heterogeneous),
            lambda: insert_many(post, 'Post', heterogeneous),
            lambda: post_rows(conn),
            'match',
        ),
        # The trap. `insert().values([...])` compiles the VALUES clause from the
        # *first* mapping, so a key only present in a later row is dropped with
        # no error at all — row `b` lands as a DRAFT with a NULL publishedAt.
        # Same for `conn.execute(insert(t), [...])`.
        (
            'create_many with per-row key sets: a single VALUES clause loses the extra keys',
            scaffold_only,
            lambda: db.post.create_many(data=heterogeneous),
            lambda: len(
                conn.execute(
                    sa.insert(post)
                    .values([values_for_create('Post', item, moment=moment()) for item in heterogeneous])
                    .returning(post.c.id)
                ).fetchall()
            ),
            lambda: post_rows(conn),
            'mismatch',
        ),
        # -- update_many -------------------------------------------------------
        (
            'update_many returns the number of matched rows',
            five_posts,
            lambda: db.post.update_many(where=scoped({'status': 'DRAFT'}), data={'status': 'ARCHIVED'}),
            lambda: conn.execute(
                sa.update(post)
                .where(sa_scoped(post.c.status == 'DRAFT'))
                .values(**values_for_update('Post', {'status': 'ARCHIVED'}))
            ).rowcount,
            lambda: post_rows(conn),
            'match',
        ),
        (
            'update_many stamps @updatedAt on rows whose value did not change',
            posts_two_already_equal,
            lambda: db.post.update_many(where=scoped({'status': 'DRAFT'}), data={'title': 'same'}),
            lambda: conn.execute(
                sa.update(post)
                .where(sa_scoped(post.c.status == 'DRAFT'))
                .values(**values_for_update('Post', {'title': 'same'}))
            ).rowcount,
            lambda: post_stamp_shape(conn),
            'match',
        ),
        (
            'update_many with a compound where',
            five_posts,
            lambda: db.post.update_many(
                where=scoped({'AND': [{'status': 'DRAFT'}, {'slug': {'in': ['p0', 'p1']}}]}),
                data={'title': 'renamed'},
            ),
            lambda: conn.execute(
                sa.update(post)
                .where(sa_scoped(sa.and_(post.c.status == 'DRAFT', post.c.slug.in_(['p0', 'p1']))))
                .values(**values_for_update('Post', {'title': 'renamed'}))
            ).rowcount,
            lambda: post_rows(conn),
            'match',
        ),
        (
            'update_many matching zero rows',
            five_posts,
            lambda: db.post.update_many(where=scoped({'slug': 'absent'}), data={'title': 'renamed'}),
            lambda: conn.execute(
                sa.update(post)
                .where(sa_scoped(post.c.slug == 'absent'))
                .values(**values_for_update('Post', {'title': 'renamed'}))
            ).rowcount,
            lambda: post_stamp_shape(conn),
            'match',
        ),
        # -- delete_many -------------------------------------------------------
        (
            'delete_many matching zero rows',
            five_posts,
            lambda: db.post.delete_many(where=scoped({'slug': 'absent'})),
            lambda: conn.execute(sa.delete(post).where(sa_scoped(post.c.slug == 'absent'))).rowcount,
            lambda: post_rows(conn),
            'match',
        ),
        (
            'delete_many matching many rows',
            five_posts,
            lambda: db.post.delete_many(where=scoped({'status': 'DRAFT'})),
            lambda: conn.execute(sa.delete(post).where(sa_scoped(post.c.status == 'DRAFT'))).rowcount,
            lambda: post_rows(conn),
            'match',
        ),
        (
            'delete_many with a compound where',
            five_posts,
            lambda: db.post.delete_many(
                where=scoped({'OR': [{'slug': {'in': ['p0', 'p3']}}, {'title': {'contains': 'wo'}}]})
            ),
            lambda: conn.execute(
                sa.delete(post).where(sa_scoped(sa.or_(post.c.slug.in_(['p0', 'p3']), post.c.title.like('%wo%'))))
            ).rowcount,
            lambda: post_rows(conn),
            'match',
        ),
        (
            'delete_many with no where empties the table',
            three_flags,
            lambda: db.featureflag.delete_many(),
            lambda: conn.execute(sa.delete(flag)).rowcount,
            lambda: flag_shape(conn),
            'match',
        ),
    ]
    return cases


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


def run_pass(
    conn: sa.Connection,
    setup: Callable[[], None],
    call: Callable[[], Any],
    observe: Callable[[], Any],
) -> Tuple[Any, Any]:
    teardown(conn)
    setup()
    returned = normalize(call())
    observed = normalize(observe())
    teardown(conn)
    return returned, observed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--workdir', required=True)
    parser.add_argument('--package', default='pkg_sync')
    args = parser.parse_args()

    url = os.environ['BENCH_DATABASE_URL']
    os.chdir(args.workdir)
    sys.path.insert(0, os.getcwd())

    mod = importlib.import_module(args.package)
    db = mod.Prisma(datasource={'url': url})
    db.connect()

    meta = importlib.import_module(f'{args.package}.metadata')

    # `values_for_create` reads `prisma.metadata`, which in a checkout belongs to
    # the *dev* client. Point it at the lab client, or the model names below are
    # not the ones in the database being written to.
    import prisma.sa
    import prisma.metadata

    prisma.metadata.SCHEMA = meta.SCHEMA
    prisma.metadata.ENUM_SCHEMA = meta.ENUM_SCHEMA
    prisma.metadata.DATABASE_PROVIDER = 'postgresql'
    prisma.sa.clear_cache()

    from prisma.sa import build_metadata

    md = build_metadata(meta.SCHEMA, meta.ENUM_SCHEMA, 'postgresql')

    # AUTOCOMMIT: the Prisma client is a separate process on a separate
    # connection, so anything this harness sets up has to be committed to be
    # visible to it — and vice versa.
    engine = sa.create_engine(sqlalchemy_url(url), isolation_level='AUTOCOMMIT')
    conn = engine.connect()

    results: Dict[str, str] = {}
    for name, setup, prisma_call, alchemy_call, observe, expect in build_cases(db, conn, md):
        try:
            left = run_pass(conn, setup, prisma_call, observe)
        except Exception as exc:  # noqa: BLE001
            teardown(conn)
            results[name] = f'PRISMA-ERROR {type(exc).__name__}: {exc}'
            continue
        try:
            right = run_pass(conn, setup, alchemy_call, observe)
        except Exception as exc:  # noqa: BLE001
            teardown(conn)
            results[name] = f'SA-ERROR {type(exc).__name__}: {exc}'
            continue

        matched = left == right
        if expect == 'match':
            results[name] = 'MATCH' if matched else f'FAIL differs\n    prisma={left!r}\n    sa    ={right!r}'
        else:
            results[name] = f'FAIL agreed, so the case proves nothing: {left!r}' if matched else 'DIFFERS-AS-EXPECTED'

    width = max(len(name) for name in results)
    failures = 0
    for name, outcome in results.items():
        status = outcome.split()[0]
        if status == 'FAIL' or status.endswith('ERROR'):
            failures += 1
        print(f'{status:9} {name:<{width}}  {outcome[len(status) :].strip()}')

    print(f'\n{len(results) - failures}/{len(results)} verified')

    conn.close()
    engine.dispose()
    db.disconnect()
    sys.exit(failures)


if __name__ == '__main__':
    main()
