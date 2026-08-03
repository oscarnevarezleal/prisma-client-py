"""Check each Prisma -> SQLAlchemy Core *write* translation against a live database.

The read harness next door (`verify_translations.py`) can run every case against
one seeded database and compare return values. A write cannot: it changes the
thing being measured, and the two clients cannot write the same row twice. So
this harness differs in three ways.

**Two arenas.** Every case runs the Prisma side into one `Site` and the
SQLAlchemy side into another, with identical data otherwise. Only `id`,
`siteId` and the generated timestamps can then legitimately differ, and those
are compared by *shape* (`fingerprint`) rather than by value — a cuid against a
cuid, a millisecond-truncated recent timestamp against another.

**The resulting rows, not the return value.** Each case reads its row back out
of the database with the same `SELECT` on both sides and compares that. A
translation that returns a plausible object while landing a different row is
exactly the failure this exists to catch. `RETURNING` is then compared
separately, as its own case, against what Prisma's call returned.

**Reset between cases.** `reset()` deletes both arenas' posts before every case,
so a case cannot pass on a row an earlier one left behind.

Scope: single-row `create` / `update` / `delete`, scalars only. Nested writes,
`connect`/`disconnect`, `*_many`, `upsert` and atomic operations are on the
runbook's STOP list and are not attempted here.

    BENCH_DATABASE_URL=postgresql://... python verify_writes.py --workdir /tmp/pglab-sa

Exit code is the number of mismatches.
"""

from __future__ import annotations

import os
import re
import sys
import enum
import argparse
import datetime
import importlib
from typing import Any, Dict, List, Tuple, Callable, Optional, Sequence

import sqlalchemy as sa

# (name, prisma call, sqlalchemy call, expectation). As in `verify_translations`,
# the default expectation is 'match' and a 'mismatch' case pins a translation
# that is *wrong* — if one of those ever starts matching it has gone vacuous.
Case = Tuple[Any, ...]

#: What the query engine generates for `@default(cuid())`. Compared as a class,
#: not as a value: two rows cannot share an id.
CUID = re.compile(r'^c[0-9a-z]{24}$')

#: A generated timestamp is "recent" if it is inside this window. Wide enough
#: that a slow case does not fail, narrow enough that a backdated value does.
RECENT = datetime.timedelta(minutes=5)

#: Every row this harness creates carries this prefix, so teardown can find them
#: all and the seeded data the read harness measures is left exactly as it was.
PREFIX = 'vw-'

#: One instant, in the two shapes a caller can hand it over in. Deliberately in
#: June and in a zone with daylight saving, so a wrong conversion is four hours
#: off rather than zero.
AWARE = datetime.datetime(2020, 6, 1, 12, 0, tzinfo=datetime.timezone.utc)
NAIVE = AWARE.replace(tzinfo=None)


def _aware(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value


def _stamp(value: datetime.datetime) -> Tuple[Any, ...]:
    """A timestamp's comparable properties, since its value cannot be compared.

    The millisecond check is here because Prisma's columns are `timestamp(3)`
    and a value that kept its microseconds is a value the database silently
    rounded. Awareness is deliberately *not* here — it is the one property the
    two sides legitimately differ on (see `python_types`), so folding it into
    every case would just make every case fail for the same reason.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    return (
        'millisecond-exact' if value.microsecond % 1000 == 0 else 'has-microseconds',
        'recent' if abs(now - _aware(value)) < RECENT else 'not-recent',
    )


def _scalar(value: Any) -> Any:
    """One column value, reduced to something the two clients can be compared on.

    A Prisma model hands back an `Enum` member and an aware `datetime`; a
    `RETURNING` row hands back the stored label and whatever the column type
    says, which for `timestamp without time zone` is naive. Those are the same
    row — see the `python types` case, which pins the difference so this
    normalisation cannot quietly hide a real one.
    """
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, datetime.datetime):
        return _stamp(value)
    return value


def fingerprint(row: Optional[Any], arena: str) -> Any:
    """A row, with the three things that cannot match reduced to their shape.

    `id` and `siteId` differ by construction — the two sides write two rows into
    two sites. The timestamps differ because they are generated. Everything else
    is compared literally, so a wrong enum label or a dropped column fails.
    """
    if row is None:
        return None

    out: Dict[str, Any] = {}
    for key, value in dict(row).items():
        if key == 'siteId':
            out[key] = 'ARENA' if value == arena else f'FOREIGN:{value}'
        elif key == 'id':
            out[key] = 'cuid' if CUID.match(str(value)) else 'explicit'
        else:
            out[key] = _scalar(value)

    # A derived fact a per-column comparison would throw away: Prisma guarantees
    # these are the same instant on a fresh row.
    if 'createdAt' in out and 'updatedAt' in out:
        out['createdAt == updatedAt'] = row['createdAt'] == row['updatedAt']

    return out


def python_types(row: Any) -> Dict[str, Any]:
    """What a write handed back, as Python types rather than as values."""
    get = row.get if isinstance(row, dict) else lambda key: getattr(row, key)
    return {
        'status': type(get('status')).__name__,
        'createdAt tzinfo': get('createdAt').tzinfo is not None,
    }


def unique_columns(model: str, name: str) -> Sequence[str]:
    """The member columns of a compound unique, read rather than guessed.

    Prisma addresses a compound unique by its *constraint* name (`siteId_slug`).
    Splitting that on `_` is the obvious reconstruction and it is wrong on any
    schema with an underscore in a field name.
    """
    from prisma._schema import model_schema

    for unique in model_schema(model)['uniques']:
        if unique['name'] == name:
            return unique['columns']
    raise LookupError(f'{model} has no unique constraint named {name!r}')


def build_cases(  # noqa: PLR0915 - a flat list of cases; splitting it hides them
    db: Any,
    conn: sa.Connection,
    md: sa.MetaData,
    arenas: Tuple[str, str],
    author: str,
) -> List[Case]:
    from prisma.sa import values_for_create, values_for_update

    post = md.tables['Post']
    comment = md.tables['Comment']
    prisma_site, sa_site = arenas

    def read(site: str, slug: str) -> Optional[Dict[str, Any]]:
        row = (
            conn.execute(sa.select(post).where(sa.and_(post.c.siteId == site, post.c.slug == slug))).mappings().first()
        )
        return dict(row) if row is not None else None

    def seed(site: str, slug: str, **extra: Any) -> Dict[str, Any]:
        """A row for `update`/`delete` to act on, written the same way on both sides."""
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
        return {'siteId': site, 'authorId': author, 'slug': slug, 'title': 'Flat write', **extra}

    def sa_create(site: str, slug: str, **extra: Any) -> Any:
        values = values_for_create('Post', creation(site, slug, **extra))
        conn.execute(sa.insert(post).values(**values))
        conn.commit()
        return fingerprint(read(site, slug), site)

    def prisma_create(site: str, slug: str, **extra: Any) -> Any:
        db.post.create(data=creation(site, slug, **extra))
        return fingerprint(read(site, slug), site)

    def scalars(row: Any) -> Dict[str, Any]:
        """Prisma's returned model, reduced to the columns of the table."""
        dumped = row.model_dump() if hasattr(row, 'model_dump') else dict(row)
        return {key: value for key, value in dumped.items() if key in post.c}

    def attempt(call: Callable[[], Any]) -> Any:
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - the failure *is* the result
            conn.rollback()
            return f'{type(exc).__name__}'

    cases: List[Case] = [
        # -- create ------------------------------------------------------------
        (
            'create, scalars only',
            lambda: prisma_create(prisma_site, 'flat'),
            lambda: sa_create(sa_site, 'flat'),
        ),
        (
            'create, enum member',
            lambda: prisma_create(prisma_site, 'enum', status='PUBLISHED'),
            lambda: sa_create(sa_site, 'enum', status='PUBLISHED'),
        ),
        (
            'create, explicit NULL for a nullable column',
            lambda: prisma_create(prisma_site, 'null', publishedAt=None),
            lambda: sa_create(sa_site, 'null', publishedAt=None),
        ),
        (
            'create, explicit id and timestamps beat the generators',
            lambda: prisma_create(
                prisma_site,
                'explicit',
                id=f'{PREFIX}explicit-p',
                createdAt=datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc),
                updatedAt=datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc),
            ),
            lambda: sa_create(
                sa_site,
                'explicit',
                id=f'{PREFIX}explicit-s',
                createdAt=datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc),
                updatedAt=datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc),
            ),
        ),
        (
            'create, RETURNING is the row Prisma returns',
            lambda: fingerprint(scalars(db.post.create(data=creation(prisma_site, 'returning'))), prisma_site),
            lambda: fingerprint(
                dict(
                    conn.execute(
                        sa.insert(post)
                        .values(**values_for_create('Post', creation(sa_site, 'returning')))
                        .returning(*post.c)
                    )
                    .mappings()
                    .one()
                ),
                sa_site,
            ),
        ),
        # An explicit DateTime the caller supplies. `values_for_create` only
        # normalises the values it *generates*; a caller's aware datetime is
        # passed through, and PostgreSQL converts it into a `timestamp without
        # time zone` column using the session's TimeZone. Prisma does not — it
        # sends UTC and gets UTC. Both halves are cases, because the rule
        # ("pass naive UTC") is worthless without the measurement behind it.
        (
            'create, an explicit AWARE datetime under a non-UTC session lands elsewhere',
            lambda: _published(conn, db.post.create(data=creation(prisma_site, 'tz', publishedAt=AWARE)), post),
            lambda: _timezone(
                conn,
                'America/New_York',
                lambda: (
                    conn.execute(
                        sa.insert(post).values(**values_for_create('Post', creation(sa_site, 'tz', publishedAt=AWARE)))
                    ),
                    conn.commit(),
                    read(sa_site, 'tz'),
                )[-1],
            )['publishedAt'],
            'mismatch',
        ),
        (
            "create, an explicit NAIVE UTC datetime lands Prisma's value under any session",
            lambda: _published(conn, db.post.create(data=creation(prisma_site, 'tzn', publishedAt=AWARE)), post),
            lambda: _timezone(
                conn,
                'America/New_York',
                lambda: (
                    conn.execute(
                        sa.insert(post).values(**values_for_create('Post', creation(sa_site, 'tzn', publishedAt=NAIVE)))
                    ),
                    conn.commit(),
                    read(sa_site, 'tzn'),
                )[-1],
            )['publishedAt'],
        ),
        # The rows agree; the Python objects do not. Prisma parses the engine's
        # ISO-8601 into an *aware* datetime and the enum into a member of the
        # generated `Enum`; a RETURNING row is whatever the column type decodes
        # to, which for `timestamp without time zone` is naive and for an enum
        # column is the stored label. Pinned as a mismatch so the caveat in the
        # runbook cannot silently stop being true.
        (
            'create, the Python types of the return value are NOT the same',
            lambda: python_types(db.post.create(data=creation(prisma_site, 'types', status='PUBLISHED'))),
            lambda: python_types(
                dict(
                    conn.execute(
                        sa.insert(post)
                        .values(**values_for_create('Post', creation(sa_site, 'types', status='PUBLISHED')))
                        .returning(*post.c)
                    )
                    .mappings()
                    .one()
                )
            ),
            'mismatch',
        ),
        # Different exception classes, same classification and same effect. The
        # classification is `classify`, which is itself the translation being
        # documented — a caller catching `UniqueViolationError` has to start
        # catching `IntegrityError`.
        (
            'create, a duplicate unique is refused by both and writes nothing',
            lambda: (
                seed(prisma_site, 'dup'),
                classify(lambda: db.post.create(data=creation(prisma_site, 'dup'))),
                db.post.count(where={'siteId': prisma_site}),
            )[1:],
            lambda: (
                seed(sa_site, 'dup'),
                classify(
                    lambda: conn.execute(sa.insert(post).values(**values_for_create('Post', creation(sa_site, 'dup')))),
                    conn,
                ),
                conn.execute(sa.select(sa.func.count()).select_from(post).where(post.c.siteId == sa_site)).scalar_one(),
            )[1:],
        ),
        # The trap the whole `values_for_create` module exists for: the same
        # INSERT without it. `@updatedAt` has no server default, so this is a
        # NOT NULL violation rather than a wrong row — but it is a failure the
        # Alembic diff cannot see, because nothing about the DDL is wrong.
        (
            'create, a bare INSERT is NOT the same write',
            lambda: prisma_create(prisma_site, 'bare'),
            lambda: attempt(
                lambda: (
                    conn.execute(
                        sa.insert(post).values(siteId=sa_site, authorId=author, slug='bare', title='Flat write')
                    ),
                    conn.commit(),
                    fingerprint(read(sa_site, 'bare'), sa_site),
                )[-1]
            ),
            'mismatch',
        ),
        # -- update ------------------------------------------------------------
        (
            'update by primary key',
            lambda: (
                seed(prisma_site, 'upd'),
                db.post.update(where={'id': read(prisma_site, 'upd')['id']}, data={'title': 'moved'}),
                fingerprint(read(prisma_site, 'upd'), prisma_site),
            )[-1],
            lambda: (
                seed(sa_site, 'upd'),
                conn.execute(
                    sa.update(post)
                    .where(post.c.id == read(sa_site, 'upd')['id'])
                    .values(**values_for_update('Post', {'title': 'moved'}))
                ),
                conn.commit(),
                fingerprint(read(sa_site, 'upd'), sa_site),
            )[-1],
        ),
        (
            'update by compound unique',
            lambda: (
                seed(prisma_site, 'compound'),
                db.post.update(
                    where={'siteId_slug': {'siteId': prisma_site, 'slug': 'compound'}},
                    data={'title': 'moved', 'status': 'ARCHIVED'},
                ),
                fingerprint(read(prisma_site, 'compound'), prisma_site),
            )[-1],
            lambda: (
                seed(sa_site, 'compound'),
                conn.execute(
                    sa.update(post)
                    .where(
                        sa.and_(
                            *(
                                post.c[column] == value
                                for column, value in zip(unique_columns('Post', 'siteId_slug'), (sa_site, 'compound'))
                            )
                        )
                    )
                    .values(**values_for_update('Post', {'title': 'moved', 'status': 'ARCHIVED'}))
                ),
                conn.commit(),
                fingerprint(read(sa_site, 'compound'), sa_site),
            )[-1],
        ),
        (
            'update moves updatedAt and leaves createdAt alone',
            lambda: _moved(
                seed(prisma_site, 'stamps'),
                lambda: db.post.update(
                    where={'siteId_slug': {'siteId': prisma_site, 'slug': 'stamps'}}, data={'title': 'moved'}
                ),
                lambda: read(prisma_site, 'stamps'),
            ),
            lambda: _moved(
                seed(sa_site, 'stamps'),
                lambda: (
                    conn.execute(
                        sa.update(post)
                        .where(sa.and_(post.c.siteId == sa_site, post.c.slug == 'stamps'))
                        .values(**values_for_update('Post', {'title': 'moved'}))
                    ),
                    conn.commit(),
                ),
                lambda: read(sa_site, 'stamps'),
            ),
        ),
        (
            'update, RETURNING is the row Prisma returns',
            lambda: (
                seed(prisma_site, 'upret'),
                fingerprint(
                    scalars(
                        db.post.update(
                            where={'siteId_slug': {'siteId': prisma_site, 'slug': 'upret'}},
                            data={'title': 'moved'},
                        )
                    ),
                    prisma_site,
                ),
            )[-1],
            lambda: (
                seed(sa_site, 'upret'),
                fingerprint(
                    dict(
                        conn.execute(
                            sa.update(post)
                            .where(sa.and_(post.c.siteId == sa_site, post.c.slug == 'upret'))
                            .values(**values_for_update('Post', {'title': 'moved'}))
                            .returning(*post.c)
                        )
                        .mappings()
                        .one()
                    ),
                    sa_site,
                ),
            )[-1],
        ),
        (
            'update, no row matches -> None, not an error',
            lambda: db.post.update(where={'id': f'{PREFIX}absent'}, data={'title': 'moved'}),
            lambda: conn.execute(
                sa.update(post)
                .where(post.c.id == f'{PREFIX}absent')
                .values(**values_for_update('Post', {'title': 'moved'}))
                .returning(*post.c)
            )
            .mappings()
            .first(),
        ),
        (
            'update, no row matches -> nothing written',
            lambda: (
                db.post.update(where={'id': f'{PREFIX}absent'}, data={'title': 'moved'}),
                db.post.count(where={'siteId': prisma_site}),
            )[-1],
            lambda: (
                conn.execute(
                    sa.update(post)
                    .where(post.c.id == f'{PREFIX}absent')
                    .values(**values_for_update('Post', {'title': 'moved'}))
                ),
                conn.commit(),
                conn.execute(sa.select(sa.func.count()).select_from(post).where(post.c.siteId == sa_site)).scalar_one(),
            )[-1],
        ),
        # -- delete ------------------------------------------------------------
        (
            'delete by primary key',
            lambda: (
                seed(prisma_site, 'del'),
                db.post.delete(where={'id': read(prisma_site, 'del')['id']}),
                fingerprint(read(prisma_site, 'del'), prisma_site),
            )[-1],
            lambda: (
                seed(sa_site, 'del'),
                conn.execute(sa.delete(post).where(post.c.id == read(sa_site, 'del')['id'])),
                conn.commit(),
                fingerprint(read(sa_site, 'del'), sa_site),
            )[-1],
        ),
        (
            'delete by compound unique',
            lambda: (
                seed(prisma_site, 'delc'),
                db.post.delete(where={'siteId_slug': {'siteId': prisma_site, 'slug': 'delc'}}),
                fingerprint(read(prisma_site, 'delc'), prisma_site),
            )[-1],
            lambda: (
                seed(sa_site, 'delc'),
                conn.execute(
                    sa.delete(post).where(
                        sa.and_(
                            *(
                                post.c[column] == value
                                for column, value in zip(unique_columns('Post', 'siteId_slug'), (sa_site, 'delc'))
                            )
                        )
                    )
                ),
                conn.commit(),
                fingerprint(read(sa_site, 'delc'), sa_site),
            )[-1],
        ),
        (
            'delete, RETURNING is the row Prisma returns',
            lambda: (
                seed(prisma_site, 'delret'),
                fingerprint(
                    scalars(db.post.delete(where={'siteId_slug': {'siteId': prisma_site, 'slug': 'delret'}})),
                    prisma_site,
                ),
            )[-1],
            lambda: (
                seed(sa_site, 'delret'),
                fingerprint(
                    dict(
                        conn.execute(
                            sa.delete(post)
                            .where(sa.and_(post.c.siteId == sa_site, post.c.slug == 'delret'))
                            .returning(*post.c)
                        )
                        .mappings()
                        .one()
                    ),
                    sa_site,
                ),
            )[-1],
        ),
        (
            'delete, no row matches -> None, not an error',
            lambda: db.post.delete(where={'id': f'{PREFIX}absent'}),
            lambda: conn.execute(sa.delete(post).where(post.c.id == f'{PREFIX}absent').returning(*post.c))
            .mappings()
            .first(),
        ),
        # Not a relation *write* — nothing here names a relation. It is the
        # question of who performs the cascade, and the answer is the foreign
        # key, identically for both callers.
        (
            "delete with children, cascade is the database's and both agree",
            lambda: _cascade(
                conn,
                comment,
                author,
                seed(prisma_site, 'cascade')['id'],
                lambda: db.post.delete(where={'siteId_slug': {'siteId': prisma_site, 'slug': 'cascade'}}),
                lambda: read(prisma_site, 'cascade'),
            ),
            lambda: _cascade(
                conn,
                comment,
                author,
                seed(sa_site, 'cascade')['id'],
                lambda: (
                    conn.execute(sa.delete(post).where(sa.and_(post.c.siteId == sa_site, post.c.slug == 'cascade'))),
                    conn.commit(),
                ),
                lambda: read(sa_site, 'cascade'),
            ),
        ),
    ]
    return cases


def _timezone(conn: sa.Connection, zone: str, call: Callable[[], Any]) -> Any:
    """Run one write with the session in `zone`, then put the session back.

    Not a contrivance: an application server whose `TimeZone` is not UTC is the
    normal case, and it is the connection setting — not anything in the query —
    that decides where an aware datetime lands in a `timestamp` column.
    """
    conn.execute(sa.text(f"SET TIME ZONE '{zone}'"))
    try:
        return call()
    finally:
        # `SET TIME ZONE` outlives the transaction, so every case after this one
        # would inherit the zone if it were not put back.
        conn.execute(sa.text('RESET TimeZone'))
        conn.commit()


def _published(conn: sa.Connection, row: Any, post: sa.Table) -> Any:
    """What the database *stored*, not what Prisma handed back."""
    return conn.execute(sa.select(post.c.publishedAt).where(post.c.id == row.id)).scalar_one()


#: PostgreSQL's SQLSTATE for a unique violation. The two clients raise entirely
#: different exception classes for it, so the code is the only common ground.
UNIQUE_VIOLATION = '23505'


def classify(call: Callable[[], Any], conn: Optional[sa.Connection] = None) -> str:
    """Run a write and name its outcome in terms both clients can be compared on.

    Prisma raises its own `UniqueViolationError`; SQLAlchemy raises
    `IntegrityError` wrapping the driver's error. Neither name is portable, so
    the classification goes through the SQLSTATE, which is.
    """
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


def _moved(
    before: Dict[str, Any],
    change: Callable[[], Any],
    after: Callable[[], Optional[Dict[str, Any]]],
) -> Dict[str, Any]:
    """The two halves of `@updatedAt`, as facts rather than as values."""
    change()
    row = after()
    assert row is not None
    return {
        'updatedAt moved forward': row['updatedAt'] > before['updatedAt'],
        'createdAt unchanged': row['createdAt'] == before['createdAt'],
        'title': row['title'],
    }


def _cascade(
    conn: sa.Connection,
    comment: sa.Table,
    author: str,
    post_id: str,
    delete: Callable[[], Any],
    after: Callable[[], Optional[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Give the post a child, delete the post, report what is left.

    The interesting outcome is not the count — it is that neither caller raises
    a foreign key violation, which is the thing a reader assumes Prisma handles
    in the client and SQLAlchemy does not.
    """
    from prisma.sa import values_for_create

    conn.execute(
        sa.insert(comment).values(
            **values_for_create('Comment', {'postId': post_id, 'authorId': author, 'body': 'child'})
        )
    )
    conn.commit()
    before = conn.execute(
        sa.select(sa.func.count()).select_from(comment).where(comment.c.postId == post_id)
    ).scalar_one()

    delete()

    return {
        'children before': before,
        'children after': conn.execute(
            sa.select(sa.func.count()).select_from(comment).where(comment.c.postId == post_id)
        ).scalar_one(),
        'post after': after(),
    }


def install_schema(package: str) -> Any:
    """Point `prisma._schema` at the generated lab client, not the dev client.

    `values_for_create` reads `prisma.metadata`, which in a checkout belongs to
    whatever client was last generated into the library. Without this the model
    names would not be the ones in the database being written to.
    """
    import prisma.metadata

    meta = importlib.import_module(f'{package}.metadata')
    prisma.metadata.SCHEMA = meta.SCHEMA
    prisma.metadata.ENUM_SCHEMA = meta.ENUM_SCHEMA
    prisma.metadata.DATABASE_PROVIDER = meta.DATABASE_PROVIDER

    from prisma.sa import clear_cache

    clear_cache()
    return meta


def setup(db: Any, conn: sa.Connection) -> Tuple[str, str, str]:
    """Two sites and an author, all disposable, none of it seeded data.

    Two sites because every case runs one write through Prisma and the same
    write through SQLAlchemy, and `@@unique([siteId, slug])` will not have both.
    """
    teardown(conn)

    org = db.organization.create(data={'id': f'{PREFIX}org', 'slug': f'{PREFIX}org', 'name': 'verify writes'})
    user = db.user.create(data={'id': f'{PREFIX}user', 'email': f'{PREFIX}u@example.com', 'handle': f'{PREFIX}u'})
    left = db.site.create(
        data={'id': f'{PREFIX}site-p', 'orgId': org.id, 'domain': f'{PREFIX}p.example', 'title': 'prisma'}
    )
    right = db.site.create(
        data={'id': f'{PREFIX}site-s', 'orgId': org.id, 'domain': f'{PREFIX}s.example', 'title': 'sqlalchemy'}
    )
    return left.id, right.id, user.id


def teardown(conn: sa.Connection) -> None:
    for statement in (
        f'DELETE FROM "Post" WHERE "siteId" LIKE \'{PREFIX}%\'',
        f'DELETE FROM "Site" WHERE id LIKE \'{PREFIX}%\'',
        f'DELETE FROM "User" WHERE id LIKE \'{PREFIX}%\'',
        f'DELETE FROM "Organization" WHERE id LIKE \'{PREFIX}%\'',
    ):
        conn.execute(sa.text(statement))
    conn.commit()


def reset(conn: sa.Connection, arenas: Tuple[str, str]) -> None:
    """Both arenas emptied, so no case inherits a row from the one before it."""
    conn.execute(sa.delete(sa.table('Post', sa.column('siteId'))).where(sa.column('siteId').in_(list(arenas))))
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
    db = mod.Prisma()
    db.connect()

    meta = install_schema(args.package)

    from prisma.sa import build_metadata

    md = build_metadata(meta.SCHEMA, meta.ENUM_SCHEMA, meta.DATABASE_PROVIDER)
    engine = sa.create_engine(url.replace('postgresql://', 'postgresql+psycopg://'))
    conn = engine.connect()

    prisma_site, sa_site, author = setup(db, conn)
    arenas = (prisma_site, sa_site)

    results: Dict[str, str] = {}
    for case in build_cases(db, conn, md, arenas, author):
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
        print(f'{status:9} {name:<{width}}  {outcome[len(status):].strip()}')

    print(f'\n{len(results) - failures}/{len(results)} verified')

    conn.close()
    engine.dispose()
    db.disconnect()
    sys.exit(failures)


if __name__ == '__main__':
    main()
