"""Check the Prisma -> SQLAlchemy Core translation of **upsert** against a live database.

`verify_writes.py` next door covers the single-row writes and explains the
two-arena arrangement this harness reuses: every case runs the Prisma call into
one `Site` and the SQLAlchemy call into another, and the rows are compared by
`fingerprint`, which reduces the three columns that cannot match — the generated
id, the arena and the generated timestamps — to their shape.

What is different about `upsert` is that it is not one statement in Prisma
either. Measured against the query engine's own log (CLI 5.19.0, PostgreSQL 16),
`upsert` compiles **two different ways**:

    INSERT ... VALUES (...) ON CONFLICT (<where columns>) DO UPDATE
        SET <update>, "updatedAt" = $n WHERE <where> RETURNING *

when the `create` payload gives the fields named in `where` the same values
`where` does, and otherwise

    BEGIN; SELECT id WHERE <where>; UPDATE ... | INSERT ...; SELECT row; COMMIT

which is a read-modify-write with a window in it. Both were observed; the cases
below pin which is which, because the second one is *not* what
`ON CONFLICT DO UPDATE` does — it can update a row the conflict target would
never have found.

Scope: flat `create`/`update` payloads, scalars and enums. Nested writes,
`connect`/`disconnect`, `include=` and atomic operations stay on the runbook's
STOP list. Two of those — a nested `create` and `{'increment': 1}` — are run
anyway, as pinned *mismatches*: Prisma performs them and `values_for_*` refuses
them by name, which is the divergence that keeps them on the list. `include=` is
not attempted at all.

    BENCH_DATABASE_URL=postgresql://... python verify_upsert.py --workdir /tmp/pglab-sa

Exit code is the number of mismatches. Everything written is prefixed `vu-` and
deleted on the way in and out, so this can share the lab database with the read
and write harnesses.
"""

from __future__ import annotations

import os
import re
import sys
import enum
import argparse
import datetime
import importlib
from typing import Any, Dict, List, Tuple, Callable, Iterable, Optional, Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# (name, prisma call, sqlalchemy call, expectation). The default expectation is
# 'match'; a 'mismatch' case pins a translation that is *wrong*, so that if one
# of those ever starts matching we learn the case has gone vacuous.
Case = Tuple[Any, ...]

CUID = re.compile(r'^c[0-9a-z]{24}$')

RECENT = datetime.timedelta(minutes=5)

#: Every row this harness writes carries this prefix.
PREFIX = 'vu-'

#: PostgreSQL's SQLSTATE for a unique violation — the only thing the two
#: clients' entirely different exception classes have in common.
UNIQUE_VIOLATION = '23505'


def _aware(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value


def _stamp(value: datetime.datetime) -> Tuple[Any, ...]:
    now = datetime.datetime.now(datetime.timezone.utc)
    return (
        'millisecond-exact' if value.microsecond % 1000 == 0 else 'has-microseconds',
        'recent' if abs(now - _aware(value)) < RECENT else 'not-recent',
    )


def _scalar(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, datetime.datetime):
        return _stamp(value)
    return value


def fingerprint(row: Optional[Any], arena: str, masked: Iterable[str] = ()) -> Any:
    """A row with everything two equivalent writes cannot agree on reduced to its shape.

    `masked` is for the columns that carry the value keeping the two arenas'
    rows apart on a model that has no `siteId` to scope it — a `FeatureFlag`
    key, say. Everything not named here is compared literally, so a wrong enum
    label or an untouched column fails the case.
    """
    if row is None:
        return None

    out: Dict[str, Any] = {}
    for key, value in dict(row).items():
        if key == 'siteId':
            out[key] = 'ARENA' if value == arena else f'FOREIGN:{value}'
        elif key == 'id':
            out[key] = 'cuid' if CUID.match(str(value)) else 'explicit'
        elif key in masked:
            out[key] = f'<{key}>'
        else:
            out[key] = _scalar(value)

    if 'createdAt' in out and 'updatedAt' in out:
        out['createdAt == updatedAt'] = row['createdAt'] == row['updatedAt']

    return out


def python_types(row: Any) -> Dict[str, Any]:
    """What an upsert handed back, as Python types rather than as values."""
    get = row.get if isinstance(row, dict) else lambda key: getattr(row, key)
    return {
        'status': type(get('status')).__name__,
        'createdAt tzinfo': get('createdAt').tzinfo is not None,
    }


def conflict_target(model: str, where: Dict[str, Any]) -> Sequence[str]:
    """The columns of the unique constraint `where` addresses — read, not guessed.

    `ON CONFLICT` needs a conflict target and Prisma's `where` names a *Prisma*
    identifier: a field name for a single `@id`/`@unique`, the constraint name
    for a compound `@@unique`, and the compound `@@id`'s name for that. The last
    one is **not** in `uniques` — that list is empty for such a model — so a
    lookup that only consults `uniques` finds nothing.
    """
    from prisma._schema import model_schema

    if len(where) != 1:
        raise LookupError(f'{model}: a `where` for upsert must name exactly one unique, got {sorted(where)}')

    (key,) = where
    spec = model_schema(model)

    field = spec['fields'].get(key)
    if field is not None and (field['is_id'] or field['is_unique']):
        return [field['column']]

    for unique in spec['uniques']:
        if unique['name'] == key:
            return unique['columns']

    if spec['primary_key']['name'] == key:
        return spec['primary_key']['columns']

    raise LookupError(f'{model}.{key} is not a unique constraint, so it cannot be an ON CONFLICT target')


def classify(call: Callable[[], Any], conn: Optional[sa.Connection] = None) -> str:
    """Run a write and name its outcome in terms both clients can be compared on."""
    try:
        call()
    except Exception as exc:  # noqa: BLE001 - the exception *is* the result
        if conn is not None:
            conn.rollback()
        state = getattr(getattr(exc, 'orig', None), 'sqlstate', None)
        if type(exc).__name__ == 'UniqueViolationError' or state == UNIQUE_VIOLATION:
            return 'unique violation'
        return f'{type(exc).__name__}'
    return 'ok'


def build_cases(  # noqa: PLR0915 - a flat list of cases; splitting it hides them
    db: Any,
    conn: sa.Connection,
    engine: sa.Engine,
    md: sa.MetaData,
    arenas: Tuple[str, str],
    author: str,
) -> List[Case]:
    from prisma.sa import values_for_create, values_for_update

    post = md.tables['Post']
    stat = md.tables['DailyStat']
    flag = md.tables['FeatureFlag']
    prisma_site, sa_site = arenas

    DAY = datetime.datetime(2024, 3, 1)

    # -- the translation itself ------------------------------------------------

    def upsert(
        table: sa.Table,
        model: str,
        where: Dict[str, Any],
        create: Dict[str, Any],
        update: Dict[str, Any],
    ) -> Any:
        """`db.<model>.upsert(where=W, data={'create': C, 'update': U})`.

        One `moment` for both halves, because the engine's statement carries one
        instant: the `updatedAt` in `DO UPDATE SET` is the same bind as the
        `createdAt` in `VALUES`.
        """
        moment = datetime.datetime.now(datetime.timezone.utc)
        statement = (
            postgresql.insert(table)
            .values(**values_for_create(model, create, moment=moment))
            .on_conflict_do_update(
                index_elements=list(conflict_target(model, where)),
                set_=values_for_update(model, update, moment=moment),
            )
            .returning(*table.c)
        )
        row = conn.execute(statement).mappings().one()
        conn.commit()
        return dict(row)

    def roundtrip(
        table: sa.Table,
        model: str,
        predicate: Any,
        create: Dict[str, Any],
        update: Dict[str, Any],
        interleave: Callable[[], None],
    ) -> Any:
        """The translation an agent writes when it does not know about `ON CONFLICT`.

        Kept here because it is the thing the runbook has to argue against, and
        the argument is only worth anything if it was executed. `interleave` is
        the concurrent writer, and it runs in the window between the read and
        the write — which is the whole point: `ON CONFLICT` has no such window.
        """
        existing = conn.execute(sa.select(table).where(predicate)).mappings().first()
        interleave()
        moment = datetime.datetime.now(datetime.timezone.utc)
        if existing is None:
            statement: Any = postgresql.insert(table).values(**values_for_create(model, create, moment=moment))
        else:
            statement = sa.update(table).where(predicate).values(**values_for_update(model, update, moment=moment))
        row = conn.execute(statement.returning(*table.c)).mappings().one()
        conn.commit()
        return dict(row)

    # -- fixtures --------------------------------------------------------------

    def read(site: str, slug: str) -> Optional[Dict[str, Any]]:
        row = (
            conn.execute(sa.select(post).where(sa.and_(post.c.siteId == site, post.c.slug == slug))).mappings().first()
        )
        return dict(row) if row is not None else None

    def read_stat(site: str) -> Optional[Dict[str, Any]]:
        row = conn.execute(sa.select(stat).where(stat.c.siteId == site)).mappings().first()
        return dict(row) if row is not None else None

    def read_flag(key: str) -> Optional[Dict[str, Any]]:
        row = conn.execute(sa.select(flag).where(flag.c.key == key)).mappings().first()
        return dict(row) if row is not None else None

    def seed(site: str, slug: str, **extra: Any) -> Dict[str, Any]:
        conn.execute(
            sa.insert(post).values(
                **values_for_create(
                    'Post',
                    {'siteId': site, 'authorId': author, 'slug': slug, 'title': f'seed {slug}', **extra},
                )
            )
        )
        conn.commit()
        row = read(site, slug)
        assert row is not None
        return row

    def creation(site: str, slug: str, **extra: Any) -> Dict[str, Any]:
        return {'siteId': site, 'authorId': author, 'slug': slug, 'title': 'created', **extra}

    def where_of(site: str, slug: str) -> Dict[str, Any]:
        return {'siteId_slug': {'siteId': site, 'slug': slug}}

    def predicate_of(site: str, slug: str) -> Any:
        return sa.and_(post.c.siteId == site, post.c.slug == slug)

    def scalars(row: Any) -> Dict[str, Any]:
        dumped = row.model_dump() if hasattr(row, 'model_dump') else dict(row)
        return {key: value for key, value in dumped.items() if key in post.c}

    def elsewhere(site: str, slug: str) -> Callable[[], None]:
        """A second connection that inserts the row and commits — a concurrent writer."""

        def insert() -> None:
            with engine.connect() as other:
                other.execute(
                    sa.insert(post).values(
                        **values_for_create(
                            'Post',
                            {'siteId': site, 'authorId': author, 'slug': slug, 'title': 'by another writer'},
                        )
                    )
                )
                other.commit()

        return insert

    def moved(before: Dict[str, Any], after: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        assert after is not None
        return {
            'same row': after['id'] == before['id'],
            'createdAt unchanged': after['createdAt'] == before['createdAt'],
            'updatedAt moved forward': after['updatedAt'] > before['updatedAt'],
            'title': after['title'],
            'status': _scalar(after['status']),
        }

    cases: List[Case] = [
        # -- the create branch -------------------------------------------------
        (
            'upsert, no row matches: the create branch lands the row create would have',
            lambda: (
                db.post.upsert(
                    where=where_of(prisma_site, 'new'),
                    data={'create': creation(prisma_site, 'new'), 'update': {'title': 'updated'}},
                ),
                fingerprint(read(prisma_site, 'new'), prisma_site),
            )[-1],
            lambda: (
                upsert(post, 'Post', where_of(sa_site, 'new'), creation(sa_site, 'new'), {'title': 'updated'}),
                fingerprint(read(sa_site, 'new'), sa_site),
            )[-1],
        ),
        (
            'upsert, create branch: the update payload is not applied',
            lambda: db.post.upsert(
                where=where_of(prisma_site, 'ignore'),
                data={'create': creation(prisma_site, 'ignore'), 'update': {'title': 'never', 'status': 'ARCHIVED'}},
            ).title,
            lambda: upsert(
                post,
                'Post',
                where_of(sa_site, 'ignore'),
                creation(sa_site, 'ignore'),
                {'title': 'never', 'status': 'ARCHIVED'},
            )['title'],
        ),
        (
            'upsert, create branch: createdAt == updatedAt, as on a fresh create',
            lambda: (
                db.post.upsert(
                    where=where_of(prisma_site, 'fresh'),
                    data={'create': creation(prisma_site, 'fresh'), 'update': {'title': 'u'}},
                ),
                read(prisma_site, 'fresh'),
            )[-1]['createdAt']
            == read(prisma_site, 'fresh')['updatedAt'],
            lambda: (
                upsert(post, 'Post', where_of(sa_site, 'fresh'), creation(sa_site, 'fresh'), {'title': 'u'}),
                read(sa_site, 'fresh'),
            )[-1]['createdAt']
            == read(sa_site, 'fresh')['updatedAt'],
        ),
        # -- the update branch -------------------------------------------------
        (
            'upsert, a row matches: the update branch keeps the id and createdAt',
            lambda: moved(
                seed(prisma_site, 'exists'),
                (
                    db.post.upsert(
                        where=where_of(prisma_site, 'exists'),
                        data={
                            'create': creation(prisma_site, 'exists'),
                            'update': {'title': 'updated', 'status': 'ARCHIVED'},
                        },
                    ),
                    read(prisma_site, 'exists'),
                )[-1],
            ),
            lambda: moved(
                seed(sa_site, 'exists'),
                (
                    upsert(
                        post,
                        'Post',
                        where_of(sa_site, 'exists'),
                        creation(sa_site, 'exists'),
                        {'title': 'updated', 'status': 'ARCHIVED'},
                    ),
                    read(sa_site, 'exists'),
                )[-1],
            ),
        ),
        (
            'upsert, update branch: the create payload is not applied',
            lambda: (
                seed(prisma_site, 'keep', status='PUBLISHED'),
                db.post.upsert(
                    where=where_of(prisma_site, 'keep'),
                    data={'create': creation(prisma_site, 'keep', status='DRAFT'), 'update': {'title': 'updated'}},
                ),
                fingerprint(read(prisma_site, 'keep'), prisma_site),
            )[-1],
            lambda: (
                seed(sa_site, 'keep', status='PUBLISHED'),
                upsert(
                    post,
                    'Post',
                    where_of(sa_site, 'keep'),
                    creation(sa_site, 'keep', status='DRAFT'),
                    {'title': 'updated'},
                ),
                fingerprint(read(sa_site, 'keep'), sa_site),
            )[-1],
        ),
        (
            'upsert, update branch: exactly one row afterwards',
            lambda: (
                seed(prisma_site, 'once'),
                db.post.upsert(
                    where=where_of(prisma_site, 'once'),
                    data={'create': creation(prisma_site, 'once'), 'update': {'title': 'updated'}},
                ),
                db.post.count(where={'siteId': prisma_site}),
            )[-1],
            lambda: (
                seed(sa_site, 'once'),
                upsert(post, 'Post', where_of(sa_site, 'once'), creation(sa_site, 'once'), {'title': 'updated'}),
                conn.execute(sa.select(sa.func.count()).select_from(post).where(post.c.siteId == sa_site)).scalar_one(),
            )[-1],
        ),
        # -- the where, and where the conflict target comes from ---------------
        (
            'upsert by primary key, create branch',
            lambda: (
                db.post.upsert(
                    where={'id': f'{PREFIX}pk-p'},
                    data={
                        'create': creation(prisma_site, 'pk', id=f'{PREFIX}pk-p'),
                        'update': {'title': 'updated'},
                    },
                ),
                fingerprint(read(prisma_site, 'pk'), prisma_site),
            )[-1],
            lambda: (
                upsert(
                    post,
                    'Post',
                    {'id': f'{PREFIX}pk-s'},
                    creation(sa_site, 'pk', id=f'{PREFIX}pk-s'),
                    {'title': 'updated'},
                ),
                fingerprint(read(sa_site, 'pk'), sa_site),
            )[-1],
        ),
        (
            'upsert by primary key, update branch',
            lambda: (
                seed(prisma_site, 'pk2', id=f'{PREFIX}pk2-p'),
                db.post.upsert(
                    where={'id': f'{PREFIX}pk2-p'},
                    data={
                        'create': creation(prisma_site, 'pk2', id=f'{PREFIX}pk2-p'),
                        'update': {'title': 'updated'},
                    },
                ),
                fingerprint(read(prisma_site, 'pk2'), prisma_site),
            )[-1],
            lambda: (
                seed(sa_site, 'pk2', id=f'{PREFIX}pk2-s'),
                upsert(
                    post,
                    'Post',
                    {'id': f'{PREFIX}pk2-s'},
                    creation(sa_site, 'pk2', id=f'{PREFIX}pk2-s'),
                    {'title': 'updated'},
                ),
                fingerprint(read(sa_site, 'pk2'), sa_site),
            )[-1],
        ),
        # A compound `@@id` is addressed exactly like a compound `@@unique` and
        # lives somewhere else entirely: `uniques` is empty for `DailyStat`, and
        # the columns are in `primary_key`.
        (
            'upsert by a compound @@id, create branch',
            lambda: (
                db.dailystat.upsert(
                    where={'day_siteId': {'day': DAY, 'siteId': prisma_site}},
                    data={
                        'create': {'day': DAY, 'siteId': prisma_site, 'views': 1},
                        'update': {'views': 99},
                    },
                ),
                fingerprint(read_stat(prisma_site), prisma_site),
            )[-1],
            lambda: (
                upsert(
                    stat,
                    'DailyStat',
                    {'day_siteId': {'day': DAY, 'siteId': sa_site}},
                    {'day': DAY, 'siteId': sa_site, 'views': 1},
                    {'views': 99},
                ),
                fingerprint(read_stat(sa_site), sa_site),
            )[-1],
        ),
        (
            'upsert by a compound @@id, update branch',
            lambda: (
                db.dailystat.create(data={'day': DAY, 'siteId': prisma_site, 'views': 1}),
                db.dailystat.upsert(
                    where={'day_siteId': {'day': DAY, 'siteId': prisma_site}},
                    data={'create': {'day': DAY, 'siteId': prisma_site, 'views': 1}, 'update': {'views': 99}},
                ),
                fingerprint(read_stat(prisma_site), prisma_site),
            )[-1],
            lambda: (
                conn.execute(
                    sa.insert(stat).values(**values_for_create('DailyStat', {'day': DAY, 'siteId': sa_site, 'views': 1}))
                ),
                conn.commit(),
                upsert(
                    stat,
                    'DailyStat',
                    {'day_siteId': {'day': DAY, 'siteId': sa_site}},
                    {'day': DAY, 'siteId': sa_site, 'views': 1},
                    {'views': 99},
                ),
                fingerprint(read_stat(sa_site), sa_site),
            )[-1],
        ),
        # A single-column `@unique` that is not the primary key, on a model whose
        # only generated timestamp is `@updatedAt` — so the create branch has to
        # fill a column that has no `createdAt` beside it.
        (
            'upsert by a single non-primary-key unique, create branch',
            lambda: (
                db.featureflag.upsert(
                    where={'key': f'{PREFIX}flag-p'},
                    data={'create': {'key': f'{PREFIX}flag-p', 'enabled': True}, 'update': {'enabled': False}},
                ),
                fingerprint(read_flag(f'{PREFIX}flag-p'), prisma_site, masked=('key',)),
            )[-1],
            lambda: (
                upsert(
                    flag,
                    'FeatureFlag',
                    {'key': f'{PREFIX}flag-s'},
                    {'key': f'{PREFIX}flag-s', 'enabled': True},
                    {'enabled': False},
                ),
                fingerprint(read_flag(f'{PREFIX}flag-s'), sa_site, masked=('key',)),
            )[-1],
        ),
        (
            'upsert by a single non-primary-key unique, update branch',
            lambda: (
                db.featureflag.create(data={'key': f'{PREFIX}flag-p', 'enabled': True}),
                db.featureflag.upsert(
                    where={'key': f'{PREFIX}flag-p'},
                    data={'create': {'key': f'{PREFIX}flag-p', 'enabled': True}, 'update': {'enabled': False}},
                ),
                fingerprint(read_flag(f'{PREFIX}flag-p'), prisma_site, masked=('key',)),
            )[-1],
            lambda: (
                conn.execute(
                    sa.insert(flag).values(
                        **values_for_create('FeatureFlag', {'key': f'{PREFIX}flag-s', 'enabled': True})
                    )
                ),
                conn.commit(),
                upsert(
                    flag,
                    'FeatureFlag',
                    {'key': f'{PREFIX}flag-s'},
                    {'key': f'{PREFIX}flag-s', 'enabled': True},
                    {'enabled': False},
                ),
                fingerprint(read_flag(f'{PREFIX}flag-s'), sa_site, masked=('key',)),
            )[-1],
        ),
        # -- the return value --------------------------------------------------
        (
            'upsert, create branch: RETURNING is the row Prisma returns',
            lambda: fingerprint(
                scalars(
                    db.post.upsert(
                        where=where_of(prisma_site, 'ret'),
                        data={'create': creation(prisma_site, 'ret'), 'update': {'title': 'updated'}},
                    )
                ),
                prisma_site,
            ),
            lambda: fingerprint(
                upsert(post, 'Post', where_of(sa_site, 'ret'), creation(sa_site, 'ret'), {'title': 'updated'}),
                sa_site,
            ),
        ),
        (
            'upsert, update branch: RETURNING is the post-update row Prisma returns',
            lambda: (
                seed(prisma_site, 'ret2'),
                fingerprint(
                    scalars(
                        db.post.upsert(
                            where=where_of(prisma_site, 'ret2'),
                            data={'create': creation(prisma_site, 'ret2'), 'update': {'title': 'updated'}},
                        )
                    ),
                    prisma_site,
                ),
            )[-1],
            lambda: (
                seed(sa_site, 'ret2'),
                fingerprint(
                    upsert(post, 'Post', where_of(sa_site, 'ret2'), creation(sa_site, 'ret2'), {'title': 'updated'}),
                    sa_site,
                ),
            )[-1],
        ),
        # Same row, different Python objects — the caveat that already holds for
        # `create`/`update`/`delete`. Pinned as a mismatch so it cannot quietly
        # stop being true.
        (
            'upsert, the Python types of the return value are NOT the same',
            lambda: python_types(
                db.post.upsert(
                    where=where_of(prisma_site, 'types'),
                    data={
                        'create': creation(prisma_site, 'types', status='PUBLISHED'),
                        'update': {'title': 'updated'},
                    },
                )
            ),
            lambda: python_types(
                upsert(
                    post,
                    'Post',
                    where_of(sa_site, 'types'),
                    creation(sa_site, 'types', status='PUBLISHED'),
                    {'title': 'updated'},
                )
            ),
            'mismatch',
        ),
        # -- what is not covered by the conflict target -------------------------
        (
            'upsert whose INSERT violates a DIFFERENT unique is refused by both',
            lambda: (
                seed(prisma_site, 'taken', id=f'{PREFIX}taken-p'),
                classify(
                    lambda: db.post.upsert(
                        where=where_of(prisma_site, 'free'),
                        data={
                            'create': creation(prisma_site, 'free', id=f'{PREFIX}taken-p'),
                            'update': {'title': 'updated'},
                        },
                    )
                ),
                db.post.count(where={'siteId': prisma_site}),
            )[1:],
            lambda: (
                seed(sa_site, 'taken', id=f'{PREFIX}taken-s'),
                classify(
                    lambda: upsert(
                        post,
                        'Post',
                        where_of(sa_site, 'free'),
                        creation(sa_site, 'free', id=f'{PREFIX}taken-s'),
                        {'title': 'updated'},
                    ),
                    conn,
                ),
                conn.execute(sa.select(sa.func.count()).select_from(post).where(post.c.siteId == sa_site)).scalar_one(),
            )[1:],
        ),
        # -- concurrency --------------------------------------------------------
        # The reason the translation is a single statement and not a read
        # followed by a write. Another connection inserts the same key in the
        # window a read-modify-write leaves open; `ON CONFLICT` has no such
        # window, so it takes the update branch instead of failing.
        (
            'upsert survives a concurrent insert of the same key',
            lambda: (
                elsewhere(prisma_site, 'race')(),
                classify(
                    lambda: db.post.upsert(
                        where=where_of(prisma_site, 'race'),
                        data={'create': creation(prisma_site, 'race'), 'update': {'title': 'updated'}},
                    )
                ),
                read(prisma_site, 'race')['title'],
            )[1:],
            lambda: (
                elsewhere(sa_site, 'race')(),
                classify(
                    lambda: upsert(
                        post,
                        'Post',
                        where_of(sa_site, 'race'),
                        creation(sa_site, 'race'),
                        {'title': 'updated'},
                    ),
                    conn,
                ),
                read(sa_site, 'race')['title'],
            )[1:],
        ),
        (
            'a read-then-write translation does NOT survive it',
            lambda: (
                elsewhere(prisma_site, 'race2')(),
                classify(
                    lambda: db.post.upsert(
                        where=where_of(prisma_site, 'race2'),
                        data={'create': creation(prisma_site, 'race2'), 'update': {'title': 'updated'}},
                    )
                ),
            )[-1],
            lambda: classify(
                lambda: roundtrip(
                    post,
                    'Post',
                    predicate_of(sa_site, 'race2'),
                    creation(sa_site, 'race2'),
                    {'title': 'updated'},
                    interleave=elsewhere(sa_site, 'race2'),
                ),
                conn,
            ),
            'mismatch',
        ),
        # -- the precondition ---------------------------------------------------
        # Prisma only compiles the single statement when `create` gives the
        # fields in `where` the same values `where` does. When they differ it
        # round-trips instead, and the two are not the same operation: the
        # conflict target keys on what is being *inserted*, so `ON CONFLICT`
        # inserts a second row where Prisma updates the first.
        (
            'where and create disagreeing on the key is NOT an ON CONFLICT',
            lambda: (
                seed(prisma_site, 'left'),
                db.post.upsert(
                    where=where_of(prisma_site, 'left'),
                    data={'create': creation(prisma_site, 'right'), 'update': {'title': 'updated'}},
                ),
                sorted((row['slug'], row['title']) for row in _all(conn, post, prisma_site)),
            )[-1],
            lambda: (
                seed(sa_site, 'left'),
                upsert(post, 'Post', where_of(sa_site, 'left'), creation(sa_site, 'right'), {'title': 'updated'}),
                sorted((row['slug'], row['title']) for row in _all(conn, post, sa_site)),
            )[-1],
            'mismatch',
        ),
        # -- what `values_for_*` refuses ----------------------------------------
        # Prisma performs both of these. The translation refuses them by name
        # rather than half-doing them, which is a *divergence* and is why they
        # stay on the STOP list — pinned as mismatches so that if `values_for_*`
        # ever grew support, this file would say so.
        (
            'a nested write in the create payload: Prisma performs it, the translation refuses',
            lambda: classify(
                lambda: db.post.upsert(
                    where=where_of(prisma_site, 'nested'),
                    data={
                        'create': {
                            **creation(prisma_site, 'nested'),
                            'comments': {'create': [{'authorId': author, 'body': 'x'}]},
                        },
                        'update': {'title': 'updated'},
                    },
                )
            ),
            lambda: classify(
                lambda: upsert(
                    post,
                    'Post',
                    where_of(sa_site, 'nested'),
                    creation(sa_site, 'nested', comments={'create': [{'authorId': author, 'body': 'x'}]}),
                    {'title': 'updated'},
                ),
                conn,
            ),
            'mismatch',
        ),
        (
            'an atomic operation in the update payload: Prisma performs it, the translation refuses',
            lambda: (
                db.dailystat.create(data={'day': DAY, 'siteId': prisma_site, 'views': 1}),
                classify(
                    lambda: db.dailystat.upsert(
                        where={'day_siteId': {'day': DAY, 'siteId': prisma_site}},
                        data={
                            'create': {'day': DAY, 'siteId': prisma_site, 'views': 1},
                            'update': {'views': {'increment': 1}},
                        },
                    )
                ),
            )[-1],
            lambda: classify(
                lambda: upsert(
                    stat,
                    'DailyStat',
                    {'day_siteId': {'day': DAY, 'siteId': sa_site}},
                    {'day': DAY, 'siteId': sa_site, 'views': 1},
                    {'views': {'increment': 1}},
                ),
                conn,
            ),
            'mismatch',
        ),
    ]
    return cases


def _all(conn: sa.Connection, post: sa.Table, site: str) -> List[Dict[str, Any]]:
    return [dict(row) for row in conn.execute(sa.select(post).where(post.c.siteId == site)).mappings().all()]


def install_schema(package: str) -> Any:
    """Point `prisma._schema` at the generated lab client, not the dev client."""
    import prisma.metadata

    meta = importlib.import_module(f'{package}.metadata')
    prisma.metadata.SCHEMA = meta.SCHEMA
    prisma.metadata.ENUM_SCHEMA = meta.ENUM_SCHEMA
    prisma.metadata.DATABASE_PROVIDER = meta.DATABASE_PROVIDER

    from prisma.sa import clear_cache

    clear_cache()
    return meta


def setup(db: Any, conn: sa.Connection) -> Tuple[str, str, str]:
    """Two sites and an author, all disposable, none of it seeded data."""
    teardown(conn)

    org = db.organization.create(data={'id': f'{PREFIX}org', 'slug': f'{PREFIX}org', 'name': 'verify upsert'})
    db.user.create(data={'id': f'{PREFIX}user', 'email': f'{PREFIX}u@example.com', 'handle': f'{PREFIX}u'})
    left = db.site.create(
        data={'id': f'{PREFIX}site-p', 'orgId': org.id, 'domain': f'{PREFIX}p.example', 'title': 'prisma'}
    )
    right = db.site.create(
        data={'id': f'{PREFIX}site-s', 'orgId': org.id, 'domain': f'{PREFIX}s.example', 'title': 'sqlalchemy'}
    )
    return left.id, right.id, f'{PREFIX}user'


def teardown(conn: sa.Connection) -> None:
    for statement in (
        f'DELETE FROM "DailyStat" WHERE "siteId" LIKE \'{PREFIX}%\'',
        f'DELETE FROM "FeatureFlag" WHERE key LIKE \'{PREFIX}%\'',
        f'DELETE FROM "Post" WHERE "siteId" LIKE \'{PREFIX}%\'',
        f'DELETE FROM "Site" WHERE id LIKE \'{PREFIX}%\'',
        f'DELETE FROM "User" WHERE id LIKE \'{PREFIX}%\'',
        f'DELETE FROM "Organization" WHERE id LIKE \'{PREFIX}%\'',
    ):
        conn.execute(sa.text(statement))
    conn.commit()


def reset(conn: sa.Connection, arenas: Tuple[str, str]) -> None:
    """Both arenas emptied, so no case inherits a row from the one before it."""
    sites = list(arenas)
    conn.execute(sa.delete(sa.table('Post', sa.column('siteId'))).where(sa.column('siteId').in_(sites)))
    conn.execute(sa.delete(sa.table('DailyStat', sa.column('siteId'))).where(sa.column('siteId').in_(sites)))
    conn.execute(sa.text(f'DELETE FROM "FeatureFlag" WHERE key LIKE \'{PREFIX}%\''))
    conn.commit()


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

    meta = install_schema(args.package)

    from prisma.sa import build_metadata

    md = build_metadata(meta.SCHEMA, meta.ENUM_SCHEMA, meta.DATABASE_PROVIDER)
    engine = sa.create_engine(url.replace('postgresql://', 'postgresql+psycopg://'))
    conn = engine.connect()

    prisma_site, sa_site, author = setup(db, conn)
    arenas = (prisma_site, sa_site)

    results: Dict[str, str] = {}
    for case in build_cases(db, conn, engine, md, arenas, author):
        name, prisma_call, alchemy_call = case[0], case[1], case[2]
        expect = case[3] if len(case) > 3 else 'match'
        reset(conn, arenas)
        try:
            left = prisma_call()
        except Exception as exc:  # noqa: BLE001
            results[name] = f'PRISMA-ERROR {type(exc).__name__}: {exc}'
            continue
        try:
            right = alchemy_call()
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            results[name] = f'SA-ERROR {type(exc).__name__}: {exc}'
            continue

        matched = left == right
        if expect == 'match':
            results[name] = 'MATCH' if matched else f'FAIL differs\n    prisma={left!r}\n    sa    ={right!r}'
        else:
            results[name] = f'FAIL agreed, so the case proves nothing: {left!r}' if matched else 'DIFFERS-AS-EXPECTED'

    teardown(conn)

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
